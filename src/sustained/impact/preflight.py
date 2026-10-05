"""
What a run would wait behind on a live server at the moment of the read.

A statement that needs a table lock waits while another session has a
lock that conflicts with it, or has asked for one first. On PostgreSQL,
MySQL, and MariaDB every later query on the table then waits behind the
statement, so one idle transaction can stop every query on a table.
`preflight()` reads the other sessions' table locks, keeps the ones that
conflict with the locks the run's statements take, and names each
session with its state, user, application, transaction age, and last
statement. It also lists the transactions open longer than
`older_than` seconds, which may take a conflicting lock at any moment.
It never ends a session.

A `Blocker` is one session a statement would wait behind: for a lock it
has been granted or is waiting for, or, for a statement such as
PostgreSQL's `CREATE INDEX CONCURRENTLY`, for its transaction to end.
Each session appears once per table and lock, beside the first
statement of the run that would wait for it.

Each profile with a preflight sets `Profile.preflight`, a read plan that
yields SQL and takes rows back, like the context read, so either
migrator runs it and a failed read leaves the transaction usable. A read
that fails, for example for lack of a privilege, is left out of `read`,
and its sessions are not reported, so a caller that refuses to run
past a blocker also refuses when a read in `needs` is missing, or when
a statement is in `unread`. PostgreSQL, MySQL, MariaDB, and SQL
Server have one; SQLite and DuckDB do not, since neither has other
sessions to wait behind in the same way.
"""

from __future__ import annotations

import math
from typing import (
    TYPE_CHECKING,
    Callable,
    FrozenSet,
    Generator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from sustained.impact.context import EngineContext, Rows, read_context

if TYPE_CHECKING:
    from sustained.aio import AsyncAdapter
    from sustained.dialects import Dialects
    from sustained.impact.model import StatementImpact
    from sustained.types import Connection

# How long a transaction stays open before the preflight lists it, in
# seconds, when it has no lock the run would wait for.
OLDER_THAN = 60.0


class LiveSession(NamedTuple):
    """
    Another session on the server, as the preflight read reports it.
    `id` is the number the engine names it by: the backend pid on
    PostgreSQL, the connection id on MySQL and MariaDB, and the session
    id on SQL Server. It is None for a PostgreSQL prepared transaction,
    which has no session. `label` is how the report names it, such as
    `pid 4121`. `state` is the engine's word for what it is doing, such
    as `idle in transaction` or `Sleep`. `transaction_seconds` is how
    long its transaction has been open, None when it has none or the
    read did not give it. `query` is its current or most recent
    statement. Any field but `label` is None where the read did not give
    it.
    """

    id: Optional[int]
    label: str
    user: Optional[str] = None
    application: Optional[str] = None
    state: Optional[str] = None
    transaction_seconds: Optional[float] = None
    query: Optional[str] = None


class Blocker(NamedTuple):
    """
    One session a statement would wait behind. `table` is the table as
    the statement names it, and `lock` the lock the statement takes on
    it. `held` is the engine's name for the lock the session was granted
    (`granted` True) or is waiting for (`granted` False). `held` is None
    when the statement waits for the session's transaction to end
    instead of for a lock.
    """

    statement: str
    table: Optional[str]
    lock: Optional[str]
    held: Optional[str]
    granted: bool
    session: LiveSession


class Preflight(NamedTuple):
    """
    What a run would wait behind. `blockers` are in run order.
    `transactions` are the other transactions open at least
    `older_than` seconds whose sessions are not among the blockers,
    oldest first. `read` names what came from the server: `locks` for
    the other sessions' table locks, and `transactions` for the open
    transactions. `profile` is the rule profile the server reads as.

    `needs` names the reads the blockers come from for these
    statements: `locks` when a statement takes a table lock, and on
    PostgreSQL `transactions` as well when a statement waits for every
    transaction with a snapshot. A read in `needs` and not in `read`
    leaves blockers the preflight could not see; `missing` names them.
    `unread` lists the statements whose impact has confidence
    `unknown`, whose locks the preflight cannot check, in run order.
    """

    profile: str
    blockers: Tuple[Blocker, ...]
    transactions: Tuple[LiveSession, ...]
    older_than: float
    read: FrozenSet[str] = frozenset()
    needs: FrozenSet[str] = frozenset()
    unread: Tuple[str, ...] = ()

    @property
    def missing(self) -> Tuple[str, ...]:
        """The reads the blockers come from that failed, in name order."""
        return tuple(sorted(self.needs - self.read))

    @property
    def clear(self) -> bool:
        """
        Whether the read shows nothing the run would wait behind: no
        blocker, no read it needs missing, and no statement it cannot
        check.
        """
        return not (self.blockers or self.missing or self.unread)


PreflightPlan = Generator[str, Rows, Preflight]
"""A profile's preflight read: yields SQL, takes rows, returns the result."""


class Planned(NamedTuple):
    """One lock a statement of the run takes, in run order."""

    statement: str
    table: str
    lock: Optional[str]
    rule: Optional[str]


class Granted(NamedTuple):
    """
    One table lock another session was granted or is waiting for:
    the table's schema and name, whether an unqualified name finds it,
    the engine's name for the lock, and the session.
    """

    schema: str
    table: str
    bare: bool
    mode: str
    granted: bool
    session: LiveSession


def planned(impacts: Sequence["StatementImpact"]) -> List[Planned]:
    """
    Every table lock the statements take, in run order. A statement
    with confidence `unknown` names no lock here; `unplanned()` lists
    it.
    """
    return [
        Planned(impact.statement, table.table, table.lock, table.rule)
        for impact in impacts
        for table in impact.tables
        if table.lock is not None
    ]


def unplanned(impacts: Sequence["StatementImpact"]) -> Tuple[str, ...]:
    """
    The statements whose impact has confidence `unknown`, in run order:
    the analysis cannot say which tables they lock, so the preflight
    cannot check them.
    """
    from sustained.impact.model import Confidence

    return tuple(
        impact.statement
        for impact in impacts
        if impact.confidence is Confidence.UNKNOWN
    )


def names(schema: str, table: str, bare: bool) -> FrozenSet[str]:
    """The lower case names a statement may give a table."""
    full = f"{schema}.{table}".lower()
    return frozenset({full, table.lower()}) if bare else frozenset({full})


def blockers(
    locks: Sequence[Planned],
    granted: Sequence[Granted],
    conflicts: Callable[[Planned, str], bool],
) -> List[Blocker]:
    """
    The sessions each planned lock would wait behind. A session is
    listed once per table and lock, beside the first planned lock that
    conflicts with it.
    """
    found: List[Blocker] = []
    seen: Set[Tuple[str, str, str, bool]] = set()
    for plan in locks:
        wanted = plan.table.lower()
        for other in granted:
            if wanted not in names(other.schema, other.table, other.bare):
                continue
            if not conflicts(plan, other.mode):
                continue
            mark = (
                other.session.label,
                f"{other.schema}.{other.table}".lower(),
                other.mode,
                other.granted,
            )
            if mark in seen:
                continue
            seen.add(mark)
            found.append(
                Blocker(
                    plan.statement,
                    plan.table,
                    plan.lock,
                    other.mode,
                    other.granted,
                    other.session,
                )
            )
    return found


def older(
    sessions: Sequence[LiveSession],
    older_than: float,
    found: Sequence[Blocker],
) -> Tuple[LiveSession, ...]:
    """
    The sessions whose transaction has been open at least `older_than`
    seconds, leaving out the blockers, oldest first.
    """
    listed = {b.session.label for b in found}
    kept = {
        s.label: s
        for s in sessions
        if s.label not in listed
        and s.transaction_seconds is not None
        and s.transaction_seconds >= older_than
    }
    return tuple(sorted(kept.values(), key=lambda s: -(s.transaction_seconds or 0.0)))


def text(value: object) -> Optional[str]:
    """A column value as text, or None for NULL or an empty string."""
    if value is None:
        return None
    spelled = str(value)
    return spelled or None


def seconds(value: object) -> Optional[float]:
    """A column value as seconds, or None for NULL."""
    return None if value is None else float(str(value))


def number(value: object) -> Optional[int]:
    """A column value as an integer, or None for NULL."""
    return None if value is None else int(str(value))


def checked_older_than(older_than: object) -> float:
    """
    `older_than` as seconds. Raises ValueError for a value that is not a
    number, is negative, or is NaN, any of which would list every open
    transaction or none of them without saying so. Infinity lists none.
    """
    if (
        isinstance(older_than, bool)
        or not isinstance(older_than, (int, float))
        or math.isnan(older_than)
        or older_than < 0
    ):
        raise ValueError(
            f"older_than must be a number of seconds, 0 or more, not {older_than!r}."
        )
    return float(older_than)


def covered(dialect: "Dialects") -> bool:
    """Whether the dialect's profile has a preflight."""
    from sustained.impact.rules import profile_for

    profile = profile_for(dialect)
    return profile is not None and profile.preflight is not None


def preflight_plan(
    dialect: "Dialects",
    impacts: Sequence["StatementImpact"],
    older_than: float = OLDER_THAN,
) -> PreflightPlan:
    """
    The dialect's preflight read for the statements' impacts, with
    `needs` and `unread` filled in. Raises ValueError for a dialect
    that has no preflight, and for an `older_than` checked_older_than()
    refuses.
    """
    from sustained.impact.rules import engine, profile_for

    seconds = checked_older_than(older_than)
    profile = profile_for(dialect)
    if profile is None or profile.preflight is None:
        raise ValueError(f"The live preflight does not cover {engine(dialect)}.")
    return _completed(profile.preflight(impacts, seconds), impacts)


def _completed(
    plan: PreflightPlan, impacts: Sequence["StatementImpact"]
) -> PreflightPlan:
    """The profile's read, then the reads it needs and the unread statements."""
    found = yield from plan
    needs = set(found.needs)
    if planned(impacts):
        needs.add("locks")
    return found._replace(needs=frozenset(needs), unread=unplanned(impacts))


def _impacts(
    statements: Sequence[str],
    dialect: "Dialects",
    context: Optional[EngineContext],
) -> Optional[List["StatementImpact"]]:
    """
    The statements' impacts: the attached impact of each statement that
    has one, and the others analyzed with the context. None when a
    statement has no attached impact and a context must be read first.
    """
    from sustained.impact.analyzer import impacts_of

    if context is None and any(getattr(s, "impact", None) is None for s in statements):
        return None
    return impacts_of(statements, dialect, context)


def preflight(
    connection: "Connection",
    dialect: "Dialects",
    statements: Sequence[str],
    older_than: float = OLDER_THAN,
    context: Optional[EngineContext] = None,
) -> Preflight:
    """
    The sessions the statements would wait behind on a blocking
    connection, and the transactions open at least `older_than` seconds.
    A statement with `impact` attached is read from it; the others are
    analyzed with `context`, read from the connection when it is not
    given. The connection's own session is never listed. Raises
    ValueError for a dialect that has no preflight, and for an
    `older_than` that is negative, NaN, or not a number.
    """
    from sustained.introspect.runner import run_plan

    covered_or_raise(dialect)
    checked_older_than(older_than)
    impacts = _impacts(statements, dialect, context)
    if impacts is None:
        impacts = _impacts(statements, dialect, read_context(connection, dialect))
    assert impacts is not None
    return run_plan(connection, dialect, preflight_plan(dialect, impacts, older_than))


async def async_preflight(
    adapter: "AsyncAdapter",
    dialect: "Dialects",
    statements: Sequence[str],
    older_than: float = OLDER_THAN,
    context: Optional[EngineContext] = None,
) -> Preflight:
    """What preflight() reads, through an async adapter."""
    from sustained.impact.context import async_read_context
    from sustained.introspect.runner import async_run_plan

    covered_or_raise(dialect)
    checked_older_than(older_than)
    impacts = _impacts(statements, dialect, context)
    if impacts is None:
        read = await async_read_context(adapter, dialect)
        impacts = _impacts(statements, dialect, read)
    assert impacts is not None
    return await async_run_plan(
        adapter, dialect, preflight_plan(dialect, impacts, older_than)
    )


def covered_or_raise(dialect: "Dialects") -> None:
    """Raises ValueError for a dialect that has no preflight."""
    from sustained.impact.rules import engine

    if not covered(dialect):
        raise ValueError(f"The live preflight does not cover {engine(dialect)}.")
