"""
The part of a traced rehearsal that Postgres and SQL Server share. Each
reads a sighting of the server before and after a statement: the table
locks the transaction holds, by table id, and the names that find each
table. `observe_sightings()` sets the two sightings against the
statement's predicted impact. The engine reads the work a table's
storage shows itself, through `work_of`.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    FrozenSet,
    Generator,
    List,
    Mapping,
    Optional,
    Protocol,
    Set,
    Tuple,
)

from sustained.impact.context import Rows, attempt
from sustained.impact.model import (
    Evidence,
    Finding,
    Hold,
    StatementImpact,
    TableImpact,
    Work,
)
from sustained.impact.rules.common import keys, mismatch, settled_work, work_mismatch

if TYPE_CHECKING:
    from sustained.impact.rules import Profile


class Sighted(Protocol):
    """What the shared observation reads of a sighting."""

    @property
    def locks(self) -> Mapping[int, FrozenSet[str]]: ...

    @property
    def names(self) -> Mapping[str, int]: ...

    @property
    def read(self) -> FrozenSet[str]: ...


WorkOf = Callable[[int, TableImpact, int], Optional[Work]]
"""
`work_of(position, table, id)`: the work the storage of the table with
`id` shows, where `position` counts the statement's tables from 0. The
observation calls it only when both sightings read the storage.
"""


def ids_plan(sql: str) -> Generator[str, Rows, Optional[FrozenSet[int]]]:
    """The ids the query gives in its first column, or None when the read failed."""
    rows = yield from attempt(sql)
    if rows is None:
        return None
    return frozenset(int(str(row[0])) for row in rows)


def add_name(
    names: Dict[str, int], table: int, schema: str, name: str, visible: bool
) -> None:
    """Record each key that finds the table, as `keys()` gives them."""
    names.update(dict.fromkeys(keys(schema, name, visible), table))


def observe_sightings(
    impact: StatementImpact,
    before: Sighted,
    after: Sighted,
    existing: Optional[FrozenSet[int]],
    profile: "Profile",
    *,
    key: Callable[[str], str],
    work_of: WorkOf,
    reported: str,
    nothing: str,
) -> StatementImpact:
    """
    The statement's impact with what the server did in place of what
    the rules predicted, and an `impact.mismatch` finding for each
    difference. `existing` contains the ids of the tables that existed
    before the run; a table outside it was created by the run, and is
    left as predicted. A table no rule named joins the impact when the
    statement took a lock on it of `reported` or stronger. `key` turns a
    table name into a key of `names`, and `nothing` says that a
    predicted copy copied nothing. The impact is returned unchanged when
    the locks were not read both times.
    """
    if "locks" not in before.read or "locks" not in after.read:
        return impact
    rank = profile.lock_rank
    stored = "storage" in before.read and "storage" in after.read
    tables: List[TableImpact] = []
    findings: List[Finding] = []
    seen: Set[int] = set()
    for position, table in enumerate(impact.tables):
        name = key(table.table)
        found = before.names.get(name, after.names.get(name))
        if found is None or (existing is not None and found not in existing):
            tables.append(table)
            continue
        seen.add(found)
        held = after.locks.get(found, frozenset())
        candidates = set(held - before.locks.get(found, frozenset()))
        if table.lock is not None and table.lock in held:
            candidates.add(table.lock)
        lock = max(candidates, key=rank) if candidates else None
        work = work_of(position, table, found) if stored else None
        updated, notes = _compare(table, lock, work, profile, nothing)
        tables.append(updated)
        findings.extend(notes)
    for found, modes in after.locks.items():
        taken = modes - before.locks.get(found, frozenset())
        if (
            found in seen
            or not taken
            or (existing is not None and found not in existing)
        ):
            continue
        lock = max(taken, key=rank)
        if rank(lock) < rank(reported):
            continue
        name = _display(before.names, after.names, found)
        tables.append(
            TableImpact(name, lock, profile.blocks(lock), Work.CATALOG, Hold.BRIEF)
        )
        findings.append(
            mismatch(f"the server took {lock} on {name}, which no rule predicted")
        )
    return impact._replace(
        tables=tuple(tables),
        findings=impact.findings + tuple(findings),
        evidence=Evidence.OBSERVED,
    )


def _compare(
    table: TableImpact,
    lock: Optional[str],
    observed: Optional[Work],
    profile: "Profile",
    nothing: str,
) -> Tuple[TableImpact, List[Finding]]:
    findings: List[Finding] = []
    updated = table
    if lock is not None and lock != table.lock:
        findings.append(
            mismatch(
                f"the rules predicted {table.lock or 'no lock'} on {table.table}, "
                f"and the server took {lock}"
            )
        )
        # A rule that set what the table blocks itself, as for the row
        # locks of an UPDATE, keeps that when the server took more.
        blocks = profile.blocks(lock)
        if table.blocks != profile.blocks(table.lock):
            blocks = max(blocks, table.blocks)
        updated = updated._replace(lock=lock, blocks=blocks)
    work = settled_work(table.work, observed)
    if work is not None and work != table.work:
        findings.append(mismatch(work_mismatch(table, observed, nothing)))
        updated = updated._replace(work=work)
    return updated, findings


def _display(before: Mapping[str, int], after: Mapping[str, int], table: int) -> str:
    """The bare name of the table, else its qualified name, else its id."""
    matches = sorted(
        {
            name
            for names in (after, before)
            for name, found in names.items()
            if found == table
        }
    )
    bare = [name for name in matches if "." not in name]
    return (bare or matches or [str(table)])[0]
