"""
Observed impact: what a statement did on a live server, read before and
after it runs inside a transaction, and set against what the rules
predicted.

On PostgreSQL an observation (`Sighting`) reads two things for our own
backend:

- `pg_locks`: every table lock the transaction holds, by mode. Locks are
  held until the commit, so a lock the statement took is one held after
  it and not before. A mode the transaction already held shows no
  difference, so a predicted lock counts as observed when it is held
  after the statement.
- `pg_relation_filenode()` and `pg_relation_size()` for each named
  table and its indexes. A table whose file changed, and which is not
  empty after the change, was rewritten. An index that is new, or whose
  file changed, was built. A partitioned table has no file of its own,
  so its leaf partitions stand for it.

`observe()` turns one statement's two sightings into observed facts:
the observed lock and work replace the predicted ones, the evidence
becomes `observed`, and each difference is an `impact.mismatch`
finding. An observation cannot tell a scan from a catalog change, so a
predicted scan stands unless a copy was seen. A table the run created
earlier is left out, as the analyzer leaves it out.

The read plans yield SQL and take rows back, like the context read, so
a migrator runs them inside a savepoint and a failed read leaves the
transaction usable.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    FrozenSet,
    Generator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from sustained.impact.context import Rows, attempt
from sustained.impact.model import (
    Evidence,
    Finding,
    Hold,
    ImpactReport,
    MigrationImpact,
    Severity,
    StatementImpact,
    TableImpact,
    Work,
)
from sustained.impact.window import aggregate

if TYPE_CHECKING:
    from sustained.dialects import Dialects
    from sustained.impact.rules import Profile


_SYSTEM_SCHEMAS = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname !~ '^pg_(toast|temp_)'"
)

# Every table, partitioned table, and materialized view that exists.
_TABLES_SQL = f"""SELECT c.oid FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm') AND {_SYSTEM_SCHEMAS}"""

# The table locks our own backend holds, one row per table and mode.
_LOCKS_SQL = f"""SELECT c.oid, n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid),
  l.mode
FROM pg_catalog.pg_locks l
JOIN pg_catalog.pg_class c ON c.oid = l.relation
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE l.pid = pg_catalog.pg_backend_pid() AND l.locktype = 'relation'
  AND l.granted AND c.relkind IN ('r', 'p', 'm') AND {_SYSTEM_SCHEMAS}"""

# The files of the named tables and their indexes: one row per table and
# relation with storage, which is the table itself, or each leaf
# partition of a partitioned table, and each index on those.
_STORAGE_SQL = f"""SELECT c.oid, n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid),
  f.relid, f.is_index, pg_catalog.pg_relation_filenode(f.relid),
  pg_catalog.pg_relation_size(f.relid)
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL (
  SELECT c.oid WHERE c.relkind <> 'p'
  UNION ALL
  SELECT t.relid FROM pg_catalog.pg_partition_tree(c.oid) t
  WHERE c.relkind = 'p' AND t.isleaf
) leaf (oid)
CROSS JOIN LATERAL (
  SELECT leaf.oid, false
  UNION ALL
  SELECT i.indexrelid, true FROM pg_catalog.pg_index i WHERE i.indrelid = leaf.oid
) f (relid, is_index)
WHERE c.relkind IN ('r', 'p', 'm') AND {_SYSTEM_SCHEMAS}
  AND lower(c.relname) IN ({{names}})"""

# A mode an unpredicted table must reach before it counts as a mismatch.
# Weaker modes are the ones foreign key checks and catalog lookups take
# on tables a statement never names.
_REPORTED_MODE = "SHARE UPDATE EXCLUSIVE"


class File(NamedTuple):
    """One relation with storage: whether it is an index, its file, its size."""

    is_index: bool
    filenode: Optional[int]
    size: int


class Sighting(NamedTuple):
    """
    What one read saw: the table locks held, by table oid; the names
    that find each table, lower case, as `schema.table` and as the bare
    name when the search path finds it; and the files of the named
    tables. `read` names the parts that were read, `locks` and
    `storage`; a part that failed is left out.
    """

    locks: Mapping[int, FrozenSet[str]] = MappingProxyType({})
    names: Mapping[str, int] = MappingProxyType({})
    storage: Mapping[int, Mapping[int, File]] = MappingProxyType({})
    read: FrozenSet[str] = frozenset()


def traces(dialect: "Dialects") -> bool:
    """Whether a rehearsal can observe the statements it runs on a dialect."""
    return dialect.name == "POSTGRES"


def lock_name(mode: str) -> str:
    """A `pg_locks.mode` value as the rules name it: `ShareLock` is `SHARE`."""
    if mode.endswith("Lock"):
        mode = mode[: -len("Lock")]
    return " ".join(re.findall(r"[A-Z][a-z]*", mode)).upper()


def _key(name: str) -> str:
    return ".".join(part.strip('"') for part in name.split(".")).lower()


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def tables_plan() -> Generator[str, Rows, Optional[FrozenSet[int]]]:
    """The oids of every table that exists, or None when the read failed."""
    rows = yield from attempt(_TABLES_SQL)
    if rows is None:
        return None
    return frozenset(int(str(row[0])) for row in rows)


def sighting_plan(tables: Sequence[str]) -> Generator[str, Rows, Sighting]:
    """The locks held now, and the files of the named tables."""
    locks: Dict[int, Set[str]] = {}
    names: Dict[str, int] = {}
    storage: Dict[int, Dict[int, File]] = {}
    read: Set[str] = set()
    rows = yield from attempt(_LOCKS_SQL)
    if rows is not None:
        read.add("locks")
        for oid, schema, name, visible, mode in rows:
            _name(names, int(str(oid)), str(schema), str(name), bool(visible))
            locks.setdefault(int(str(oid)), set()).add(lock_name(str(mode)))
    wanted = sorted({_key(t).rsplit(".", 1)[-1] for t in tables})
    if wanted:
        rows = yield from attempt(
            _STORAGE_SQL.format(names=", ".join(_literal(name) for name in wanted))
        )
        if rows is not None:
            read.add("storage")
            for oid, schema, name, visible, relid, index, node, size in rows:
                table = int(str(oid))
                _name(names, table, str(schema), str(name), bool(visible))
                storage.setdefault(table, {})[int(str(relid))] = File(
                    bool(index),
                    None if node is None else int(str(node)),
                    int(str(size or 0)),
                )
    return Sighting(
        MappingProxyType({oid: frozenset(modes) for oid, modes in locks.items()}),
        MappingProxyType(names),
        MappingProxyType({oid: MappingProxyType(f) for oid, f in storage.items()}),
        frozenset(read),
    )


def _name(
    names: Dict[str, int], oid: int, schema: str, name: str, visible: bool
) -> None:
    names[f"{schema}.{name}".lower()] = oid
    if visible:
        names[name.lower()] = oid


class _Seen(NamedTuple):
    """What an observation says about one table."""

    lock: Optional[str]
    work: Optional[Work]


def _observed_work(
    before: Mapping[int, File], after: Mapping[int, File]
) -> Optional[Work]:
    """
    `rewrite` when a table file changed and holds data, `index_build`
    when an index is new or its file changed, `catalog` when no file
    changed, and None when a file changed but is empty, which a rewrite
    of an empty table and a TRUNCATE both leave.
    """
    empty_change = False
    rewritten = built = False
    for relid, file in after.items():
        old = before.get(relid)
        changed = old is None or old.filenode != file.filenode
        if not changed:
            continue
        if file.is_index:
            built = True
        elif old is not None:
            if file.size > 0:
                rewritten = True
            else:
                empty_change = True
    if rewritten:
        return Work.REWRITE
    if empty_change:
        return None
    return Work.INDEX_BUILD if built else Work.CATALOG


def observe(
    impact: StatementImpact,
    before: Sighting,
    after: Sighting,
    existing: Optional[FrozenSet[int]],
    profile: "Profile",
) -> StatementImpact:
    """
    The statement's impact with what the server did in place of what
    the rules predicted, and an `impact.mismatch` finding for each
    difference. `existing` holds the oids of the tables that existed
    before the run; a table outside it was created by the run, and is
    left as predicted. The impact is returned unchanged when the locks
    were not read both times.
    """
    if "locks" not in before.read or "locks" not in after.read:
        return impact
    rank = profile.lock_rank
    tables: List[TableImpact] = []
    findings: List[Finding] = []
    seen: Set[int] = set()
    for table in impact.tables:
        oid = before.names.get(_key(table.table), after.names.get(_key(table.table)))
        if oid is None or (existing is not None and oid not in existing):
            tables.append(table)
            continue
        seen.add(oid)
        observed = _seen(table, oid, before, after, rank)
        updated, found = _compare(table, observed, profile)
        tables.append(updated)
        findings.extend(found)
    for oid, modes in after.locks.items():
        taken = modes - before.locks.get(oid, frozenset())
        if oid in seen or not taken or (existing is not None and oid not in existing):
            continue
        lock = max(taken, key=rank)
        if rank(lock) < rank(_REPORTED_MODE):
            continue
        name = _display(after.names, oid)
        tables.append(
            TableImpact(name, lock, profile.blocks(lock), Work.CATALOG, Hold.BRIEF)
        )
        findings.append(
            _mismatch(f"the server took {lock} on {name}, which no rule predicted")
        )
    return impact._replace(
        tables=tuple(tables),
        findings=impact.findings + tuple(findings),
        evidence=Evidence.OBSERVED,
    )


def _seen(
    table: TableImpact,
    oid: int,
    before: Sighting,
    after: Sighting,
    rank: Callable[[Optional[str]], int],
) -> _Seen:
    held = after.locks.get(oid, frozenset())
    taken = held - before.locks.get(oid, frozenset())
    candidates = set(taken)
    if table.lock is not None and table.lock in held:
        candidates.add(table.lock)
    lock = max(candidates, key=rank) if candidates else None
    work: Optional[Work] = None
    if "storage" in before.read and "storage" in after.read:
        old, new = before.storage.get(oid), after.storage.get(oid)
        if old is not None and new is not None:
            work = _observed_work(old, new)
    return _Seen(lock, work)


def _compare(
    table: TableImpact, observed: _Seen, profile: "Profile"
) -> Tuple[TableImpact, List[Finding]]:
    findings: List[Finding] = []
    updated = table
    if observed.lock is not None and observed.lock != table.lock:
        findings.append(
            _mismatch(
                f"the rules predicted {table.lock or 'no lock'} on {table.table}, "
                f"and the server took {observed.lock}"
            )
        )
        # A rule that set what the table blocks itself, as for the row
        # locks of an UPDATE, keeps that when the server took more.
        blocks = profile.blocks(observed.lock)
        if table.blocks != profile.blocks(table.lock):
            blocks = max(blocks, table.blocks)
        updated = updated._replace(lock=observed.lock, blocks=blocks)
    work = _work(table.work, observed.work)
    if work is not None and work != table.work:
        findings.append(_mismatch(_work_message(table, observed.work)))
        updated = updated._replace(work=work)
    return updated, findings


def _work(predicted: Work, observed: Optional[Work]) -> Optional[Work]:
    """
    The work to report. A copy that was seen stands. With no copy seen,
    a predicted scan, row write, or catalog change stands, since an
    observation cannot tell them apart, and a predicted copy falls to
    a scan, the heaviest work that copies nothing.
    """
    if observed is None or observed is Work.UNKNOWN:
        return None
    if observed in (Work.REWRITE, Work.INDEX_BUILD):
        return observed
    if predicted in (Work.REWRITE, Work.INDEX_BUILD):
        return Work.SCAN
    return None


def _work_message(table: TableImpact, observed: Optional[Work]) -> str:
    if observed is Work.REWRITE:
        return f"the rules predicted {table.work} on {table.table}, and the server rewrote it"
    if observed is Work.INDEX_BUILD:
        return (
            f"the rules predicted {table.work} on {table.table}, and the server "
            "built an index on it"
        )
    what = "a rewrite" if table.work is Work.REWRITE else "an index build"
    return (
        f"the rules predicted {what} on {table.table}, and the server " "copied no file"
    )


def _display(names: Mapping[str, int], oid: int) -> str:
    matches = sorted(name for name, found in names.items() if found == oid)
    bare = [name for name in matches if "." not in name]
    return (bare or matches or [str(oid)])[0]


def _mismatch(message: str) -> Finding:
    return Finding("impact.mismatch", Severity.WARN, message)


Observations = Mapping[Tuple[Optional[str], int], Tuple[Sighting, Sighting]]
"""
The two sightings of each observed statement, keyed by its migration's
id and its position in that migration, counting from 0.
"""


def with_observations(
    report: ImpactReport,
    observations: Observations,
    existing: Optional[FrozenSet[int]],
    profile: "Profile",
) -> ImpactReport:
    """
    The report with each observed statement's facts in place of the
    predicted ones, and each migration's locks and windows read again
    from them. The report's evidence is `observed` when any statement
    was observed.
    """
    migrations: List[MigrationImpact] = []
    for migration in report.migrations:
        statements = []
        for index, statement in enumerate(migration.statements):
            pair = observations.get((migration.migration_id, index))
            if pair is not None:
                statement = observe(statement, pair[0], pair[1], existing, profile)
            statements.append(statement)
        spans = migration.transactional and profile.transactional_ddl
        locks, windows, findings = aggregate(statements, spans)
        migrations.append(
            migration._replace(
                statements=tuple(statements),
                locks=locks,
                windows=windows,
                findings=findings,
            )
        )
    observed = any(
        s.evidence is Evidence.OBSERVED for m in migrations for s in m.statements
    )
    return report._replace(
        migrations=tuple(migrations),
        evidence=Evidence.OBSERVED if observed else report.evidence,
    )
