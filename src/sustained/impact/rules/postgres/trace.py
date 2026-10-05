"""
Observed impact on PostgreSQL: what a statement did on a live server, read before and
after it runs inside a transaction, and set against what the rules
predicted.

An observation (`Sighting`) reads two things for our own backend:

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

from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Dict,
    FrozenSet,
    Generator,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from sustained.impact.context import Rows, attempt
from sustained.impact.model import (
    ImpactReport,
    StatementImpact,
    TableImpact,
    Work,
)
from sustained.impact.rules.common import name_filter
from sustained.impact.rules.postgres.context import SYSTEM_SCHEMAS, literal
from sustained.impact.rules.postgres.locks import lock_name
from sustained.impact.rules.sighted import add_name, ids_plan, observe_sightings

if TYPE_CHECKING:
    from sustained.impact.rules import Profile


# Every table, partitioned table, and materialized view that exists.
_TABLES_SQL = f"""SELECT c.oid FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm') AND {SYSTEM_SCHEMAS}"""

# The table locks our own backend holds, one row per table and mode.
_LOCKS_SQL = f"""SELECT c.oid, n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid),
  l.mode
FROM pg_catalog.pg_locks l
JOIN pg_catalog.pg_class c ON c.oid = l.relation
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE l.pid = pg_catalog.pg_backend_pid() AND l.locktype = 'relation'
  AND l.granted AND c.relkind IN ('r', 'p', 'm') AND {SYSTEM_SCHEMAS}"""

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
WHERE c.relkind IN ('r', 'p', 'm') AND {SYSTEM_SCHEMAS}
  AND {{names}}"""

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


def _key(name: str) -> str:
    return ".".join(part.strip('"') for part in name.split(".")).lower()


def tables_plan() -> Generator[str, Rows, Optional[FrozenSet[int]]]:
    """The oids of every table that exists, or None when the read failed."""
    return ids_plan(_TABLES_SQL)


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
            add_name(names, int(str(oid)), str(schema), str(name), bool(visible))
            locks.setdefault(int(str(oid)), set()).add(lock_name(str(mode)))
    wanted = sorted({_key(t).rsplit(".", 1)[-1] for t in tables})
    if wanted:
        rows = yield from attempt(
            _STORAGE_SQL.format(names=name_filter("lower(c.relname)", wanted, literal))
        )
        if rows is not None:
            read.add("storage")
            for oid, schema, name, visible, relid, index, node, size in rows:
                table = int(str(oid))
                add_name(names, table, str(schema), str(name), bool(visible))
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
    difference. `existing` contains the oids of the tables that existed
    before the run; a table outside it was created by the run, and is
    left as predicted. The impact is returned unchanged when the locks
    were not read both times.
    """

    def work_of(position: int, table: TableImpact, oid: int) -> Optional[Work]:
        old, new = before.storage.get(oid), after.storage.get(oid)
        if old is None or new is None:
            return None
        return _observed_work(old, new)

    return observe_sightings(
        impact,
        before,
        after,
        existing,
        profile,
        key=_key,
        work_of=work_of,
        reported=_REPORTED_MODE,
        nothing="copied no file",
    )


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
    from sustained.impact.rules import common

    return common.with_observations(
        report,
        observations,
        profile,
        lambda statement, pair: observe(statement, pair[0], pair[1], existing, profile),
    )
