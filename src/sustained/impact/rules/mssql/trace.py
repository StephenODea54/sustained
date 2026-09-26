"""
Observed impact on SQL Server: what a statement did on a live server,
read before and after it runs inside a transaction, and set against
what the rules predicted.

An observation (`Sighting`) reads three things for our own session:

- `sys.dm_tran_locks`: every table lock granted to the transaction, by mode.
  Locks are held until the commit, so a lock the statement took is one
  held after it and not before. A lock held only while the statement
  runs, such as the Sch-S of UPDATE STATISTICS, is gone by the second
  read, so a predicted lock that was not seen is no mismatch.
- `sys.partitions` and `sys.allocation_units` for each named table:
  each partition of the heap or clustered index, which stores the rows,
  and of each other index, with its used pages. A heap or clustered
  index whose partition changed was copied, unless the new partition
  belonged to another named table before, as a SWITCH moves it. An index
  whose partition is new or changed was built.
- `sys.dm_tran_database_transactions`: the log the transaction wrote.
  A statement that updates every row in place, as a size-of-data
  ALTER COLUMN does, keeps the table's partitions, but writes more log
  than the size of the table.

`observe()` turns one statement's two sightings into observed facts,
as the Postgres trace does: the observed lock and work replace the
predicted ones, the evidence becomes `observed`, and each difference is
an `impact.mismatch` finding. An observation cannot tell a scan from a
catalog change, so a predicted scan stands unless a copy was seen. A
write of rows logs every row it changes, so the log does not count as a
copy for a predicted write. A table the run created earlier is left
out, as the analyzer leaves it out.

The read plans yield SQL and take rows back, like the context read, so
a failed read leaves its part out, and the rehearsal goes on.
"""

from __future__ import annotations

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
    StatementImpact,
    TableImpact,
    Work,
)
from sustained.impact.rules.mssql.locks import S

if TYPE_CHECKING:
    from sustained.impact.rules import Profile

# Every user table that exists.
_TABLES_SQL = "SELECT object_id FROM sys.tables"

# The table locks granted to our own session, one row per table and mode. A
# table the transaction dropped is gone from sys.objects, so its name is
# NULL, and the sighting before names it.
_LOCKS_SQL = """SELECT l.resource_associated_entity_id, s.name, o.name,
  CASE WHEN s.name = SCHEMA_NAME() THEN 1 ELSE 0 END, l.request_mode
FROM sys.dm_tran_locks l
LEFT JOIN sys.objects o ON o.object_id = l.resource_associated_entity_id
LEFT JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE l.request_session_id = @@SPID AND l.resource_database_id = DB_ID()
  AND l.resource_type = 'OBJECT' AND l.request_status = 'GRANT'
  AND (o.type = 'U' OR o.object_id IS NULL)"""

# The partitions of the named tables, with the pages each uses.
_STORAGE_SQL = """SELECT t.object_id, s.name, t.name,
  CASE WHEN s.name = SCHEMA_NAME() THEN 1 ELSE 0 END,
  p.index_id, p.partition_number, p.partition_id,
  (SELECT SUM(a.used_pages) FROM sys.allocation_units a
   WHERE a.container_id = p.partition_id)
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.partitions p ON p.object_id = t.object_id
WHERE LOWER(t.name) IN ({names})"""

# The log the current transaction has written in this database. The
# view has no row until the transaction writes here.
_LOG_SQL = """SELECT d.database_transaction_log_bytes_used
FROM sys.dm_tran_database_transactions d
JOIN sys.dm_tran_current_transaction c ON c.transaction_id = d.transaction_id
WHERE d.database_id = DB_ID()"""

_PAGE_BYTES = 8192

# A statement that writes this many times the bytes of the table's heap
# or clustered index in log, and at least the floor below, wrote every
# row of it. A catalog change writes a few kilobytes of log; an update
# of every row writes about three times the table.
_COPY_RATIO = 2
_COPY_FLOOR = 64 * 1024


class Partition(NamedTuple):
    """One partition: whether it stores the rows, its id, and its pages."""

    base: bool
    partition_id: int
    pages: int


class Sighting(NamedTuple):
    """
    What one read saw: the table locks held, by object id; the names
    that find each table, lower case, as `schema.table` and as the bare
    name in the default schema; the partitions of the named tables, by
    index and partition number; and the log the transaction has written.
    `read` names the parts that were read: `locks`, `storage`, and `log`.
    """

    locks: Mapping[int, FrozenSet[str]] = MappingProxyType({})
    names: Mapping[str, int] = MappingProxyType({})
    storage: Mapping[int, Mapping[Tuple[int, int], Partition]] = MappingProxyType({})
    log: int = 0
    read: FrozenSet[str] = frozenset()


def _key(name: str) -> str:
    return ".".join(part.strip('[]"') for part in name.split(".")).lower()


def _literal(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


def tables_plan() -> Generator[str, Rows, Optional[FrozenSet[int]]]:
    """The object ids of every table that exists, or None when the read failed."""
    rows = yield from attempt(_TABLES_SQL)
    if rows is None:
        return None
    return frozenset(int(str(row[0])) for row in rows)


def sighting_plan(tables: Sequence[str]) -> Generator[str, Rows, Sighting]:
    """The locks held now, the partitions of the named tables, and the log."""
    locks: Dict[int, Set[str]] = {}
    names: Dict[str, int] = {}
    storage: Dict[int, Dict[Tuple[int, int], Partition]] = {}
    read: Set[str] = set()
    log = 0
    rows = yield from attempt(_LOCKS_SQL)
    if rows is not None:
        read.add("locks")
        for object_id, schema, name, visible, mode in rows:
            if name is not None:
                _name(names, int(str(object_id)), str(schema), str(name), bool(visible))
            locks.setdefault(int(str(object_id)), set()).add(str(mode))
    wanted = sorted({_key(t).rsplit(".", 1)[-1] for t in tables})
    if wanted:
        rows = yield from attempt(
            _STORAGE_SQL.format(names=", ".join(_literal(name) for name in wanted))
        )
        if rows is not None:
            read.add("storage")
            for object_id, schema, name, visible, index, number, pid, pages in rows:
                table = int(str(object_id))
                _name(names, table, str(schema), str(name), bool(visible))
                storage.setdefault(table, {})[(int(str(index)), int(str(number)))] = (
                    Partition(int(str(index)) <= 1, int(str(pid)), int(str(pages or 0)))
                )
    rows = yield from attempt(_LOG_SQL)
    if rows is not None:
        read.add("log")
        log = sum(int(str(row[0] or 0)) for row in rows)
    return Sighting(
        MappingProxyType({oid: frozenset(modes) for oid, modes in locks.items()}),
        MappingProxyType(names),
        MappingProxyType({oid: MappingProxyType(p) for oid, p in storage.items()}),
        log,
        frozenset(read),
    )


def _name(
    names: Dict[str, int], object_id: int, schema: str, name: str, visible: bool
) -> None:
    names[f"{schema}.{name}".lower()] = object_id
    if visible:
        names[name.lower()] = object_id


def _observed_work(
    before: Mapping[Tuple[int, int], Partition],
    after: Mapping[Tuple[int, int], Partition],
    logged: Optional[int],
    elsewhere: FrozenSet[int],
) -> Optional[Work]:
    """
    `rewrite` when the heap or clustered index was copied, or when
    `logged` passes the bytes it held; `index_build` when an index is new
    or its partition changed; `catalog` otherwise, and when the new
    partitions are ones `elsewhere` held; and None when the rows'
    partition changed but is empty, which a TRUNCATE leaves.
    """
    old_base = {p.partition_id for p in before.values() if p.base}
    new_base = {p.partition_id for p in after.values() if p.base}
    base_pages = sum(p.pages for p in before.values() if p.base)
    new_pages = sum(p.pages for p in after.values() if p.base)
    if old_base != new_base:
        if new_base <= elsewhere:
            return Work.CATALOG
        return Work.REWRITE if new_pages > 0 else None
    if logged is not None and logged >= max(
        _COPY_FLOOR, _COPY_RATIO * base_pages * _PAGE_BYTES
    ):
        return Work.REWRITE
    old_indexes = {p.partition_id for p in before.values() if not p.base}
    if any(p.partition_id not in old_indexes for p in after.values() if not p.base):
        return Work.INDEX_BUILD
    return Work.CATALOG


class _Seen(NamedTuple):
    lock: Optional[str]
    work: Optional[Work]


def observe(
    impact: StatementImpact,
    before: Sighting,
    after: Sighting,
    existing: Optional[FrozenSet[int]],
    profile: "Profile",
) -> StatementImpact:
    """
    The statement's impact with what the server did in place of what the
    rules predicted, and an `impact.mismatch` finding for each
    difference. `existing` is the set of object ids of the tables that
    existed before the run; a table outside it was created by the run,
    and is left as predicted. The impact is returned unchanged when the
    locks were not read both times.
    """
    if "locks" not in before.read or "locks" not in after.read:
        return impact
    rank = profile.lock_rank
    logged: Optional[int] = None
    if "log" in before.read and "log" in after.read:
        logged = after.log - before.log
    tables: List[TableImpact] = []
    findings: List[Finding] = []
    seen: Set[int] = set()
    for position, table in enumerate(impact.tables):
        key = _key(table.table)
        object_id = before.names.get(key, after.names.get(key))
        if object_id is None or (existing is not None and object_id not in existing):
            tables.append(table)
            continue
        seen.add(object_id)
        # The log is the statement's, so it speaks for the table the
        # statement names, and a write of rows logs each row it writes.
        log = logged if position == 0 and table.work is not Work.ROWS else None
        elsewhere = frozenset(
            p.partition_id
            for other, partitions in before.storage.items()
            if other != object_id
            for p in partitions.values()
        )
        observed = _seen(table, object_id, before, after, log, elsewhere, rank)
        updated, found = _compare(table, observed, profile)
        tables.append(updated)
        findings.extend(found)
    for object_id, modes in after.locks.items():
        taken = modes - before.locks.get(object_id, frozenset())
        if object_id in seen or not taken:
            continue
        if existing is not None and object_id not in existing:
            continue
        lock = max(taken, key=rank)
        if rank(lock) < rank(S):
            continue
        name = _display(before.names, after.names, object_id)
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
    object_id: int,
    before: Sighting,
    after: Sighting,
    logged: Optional[int],
    elsewhere: FrozenSet[int],
    rank: Callable[[Optional[str]], int],
) -> _Seen:
    held = after.locks.get(object_id, frozenset())
    taken = held - before.locks.get(object_id, frozenset())
    candidates = set(taken)
    if table.lock is not None and table.lock in held:
        candidates.add(table.lock)
    lock = max(candidates, key=rank) if candidates else None
    work: Optional[Work] = None
    if "storage" in before.read and "storage" in after.read:
        old = before.storage.get(object_id)
        new = after.storage.get(object_id)
        if old is not None and new is not None:
            work = _observed_work(old, new, logged, elsewhere)
    return _Seen(lock, work)


def _compare(
    table: TableImpact, observed: _Seen, profile: "Profile"
) -> Tuple[TableImpact, List[Finding]]:
    from sustained.impact.rules.common import settled_work, work_mismatch

    findings: List[Finding] = []
    updated = table
    if observed.lock is not None and observed.lock != table.lock:
        findings.append(
            _mismatch(
                f"the rules predicted {table.lock or 'no lock'} on {table.table}, "
                f"and the server took {observed.lock}"
            )
        )
        blocks = profile.blocks(observed.lock)
        if table.blocks != profile.blocks(table.lock):
            blocks = max(blocks, table.blocks)
        updated = updated._replace(lock=observed.lock, blocks=blocks)
    work = settled_work(table.work, observed.work)
    if work is not None and work != table.work:
        findings.append(
            _mismatch(work_mismatch(table, observed.work, "copied nothing"))
        )
        updated = updated._replace(work=work)
    return updated, findings


def _display(
    before: Mapping[str, int], after: Mapping[str, int], object_id: int
) -> str:
    matches = sorted(
        {
            name
            for names in (after, before)
            for name, found in names.items()
            if found == object_id
        }
    )
    bare = [name for name in matches if "." not in name]
    return (bare or matches or [str(object_id)])[0]


def _mismatch(message: str) -> Finding:
    from sustained.impact.rules.common import mismatch

    return mismatch(message)


def with_observations(
    report: ImpactReport,
    observations: Mapping[Tuple[Optional[str], int], Tuple[Sighting, Sighting]],
    existing: Optional[FrozenSet[int]],
    profile: "Profile",
) -> ImpactReport:
    """
    The report with each observed statement's facts in place of the
    predicted ones, and each migration's locks and windows read again
    from them.
    """
    from sustained.impact.rules import common

    return common.with_observations(
        report,
        observations,
        profile,
        lambda statement, pair: observe(statement, pair[0], pair[1], existing, profile),
    )
