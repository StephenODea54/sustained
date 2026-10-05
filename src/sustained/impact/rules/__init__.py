"""
The rule registry: what each engine does for each statement kind.

A `Rule` names one piece of engine behaviour: its id, such as
`pg.create_index`, the documentation it relies on, the versions it
holds for, and the fixture statements the ground-truth tests run.

A `Profile` is one engine's rule set. Its `effects()` reads a statement,
with the `Facts` around it, and returns one `Effect` per table the
statement touches, plus any findings about the statement as a whole.
The analyzer turns effects into `TableImpact`s, and it decides the
severity of blocking work from the table's size, so every profile rates
a scan the same way.

`profile_for()` picks the profile for a dialect. MySQL and MariaDB
share the MYSQL dialect, so a context read from the server names which
of the two applies. SQLite connections use the DEFAULT dialect, whose
profile is SQLite's. A dialect without a profile has no impact analysis
yet.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Collection,
    FrozenSet,
    Generator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.context import ContextPlan, EngineContext, Rows
from sustained.impact.model import (
    Blocks,
    Confidence,
    Finding,
    Intent,
    ParsedStatement,
    Severity,
    Work,
)
from sustained.impact.state import RunState, sets_a_timeout

if TYPE_CHECKING:
    from sustained.dialects import Dialects
    from sustained.impact.model import ImpactReport, StatementImpact
    from sustained.impact.preflight import PreflightPlan


def _every_version(version: Tuple[int, ...]) -> bool:
    return True


def _no_refusal(error: BaseException) -> Optional[str]:
    return None


class Rule(NamedTuple):
    """
    One piece of engine behaviour. `versions` says whether the rule
    holds on a server version; `fixtures` are statements that exercise
    it, which the ground-truth tests run against real servers. Each
    fixture runs alone against the objects its profile's
    `fixture_schema` creates.
    """

    id: str
    source: str
    fixtures: Tuple[str, ...] = ()
    versions: Callable[[Tuple[int, ...]], bool] = _every_version

    def finding(
        self, severity: Severity, message: str, remedy: Tuple[str, ...] = ()
    ) -> Finding:
        """A finding of this rule, with the rule's id and source."""
        return Finding(self.id, severity, message, remedy, self.source)


class Effect(NamedTuple):
    """
    What a statement does to one table, as a rule answers it: the lock,
    the work, how sure the rule is, and what to say when the work blocks
    other sessions. `message` replaces the analyzer's default wording
    for blocking work, and `remedy` holds the safer statements. `notes`
    are findings that hold whatever the table's size, such as a rename
    breaking running code. A table the run created skips them all.

    `blocks` overrides what the lock name alone would block, for a
    statement whose row locks block more than its table lock, such as an
    UPDATE. `waits` is False for a lock the statement refuses to queue
    for, such as `LOCK TABLE ... NOWAIT`, which needs no lock timeout.
    `at_end` is True for a lock the statement takes once its work is
    done, such as the one a SQL Server online index build takes at its
    end, so the work blocks nothing and its finding is `info` whatever
    the table's size.
    """

    rule: Rule
    table: str
    lock: Optional[str]
    work: Work
    confidence: Confidence = Confidence.KNOWN
    message: Optional[str] = None
    remedy: Tuple[str, ...] = ()
    notes: Tuple[Finding, ...] = ()
    blocks: Optional[Blocks] = None
    waits: bool = True
    at_end: bool = False


class Facts(NamedTuple):
    """
    What a rule reads about one statement: the text, what the recognizer
    understood of it, the intent its generator attached, the server
    context, the run so far, and whether its migration runs inside a
    transaction.
    """

    statement: str
    parsed: ParsedStatement
    intent: Optional[Intent]
    context: EngineContext
    state: RunState
    transactional: bool


class Outcome(NamedTuple):
    """
    A profile's answer for one statement. `partitions_unread` says the
    answer depends on partitions the context did not read, and
    `unnamed` holds the lock and work on each table the statement may
    lock that no read named, with the named table it belongs to.
    """

    effects: Tuple[Effect, ...] = ()
    findings: Tuple[Finding, ...] = ()
    confidence: Confidence = Confidence.KNOWN
    partitions_unread: bool = False
    unnamed: Tuple[Effect, ...] = ()

    @classmethod
    def of(
        cls,
        *effects: Effect,
        findings: Tuple[Finding, ...] = (),
        confidence: Confidence = Confidence.KNOWN,
        partitions_unread: bool = False,
        unnamed: Tuple[Effect, ...] = (),
    ) -> "Outcome":
        """The outcome with the effects given one by one."""
        return cls(effects, findings, confidence, partitions_unread, unnamed)


class Probe(NamedTuple):
    """
    What a probe saw of one statement: the clause the server accepted,
    and each clause it refused before that one, with the reason the
    server gave.
    """

    accepted: Any
    refused: Tuple[Tuple[Any, str], ...] = ()


class Trace(NamedTuple):
    """
    How a traced rehearsal observes a profile's statements. `tables()`
    is a read plan for the tables that exist before the run.
    `report(predicted, observations, existing, profile)` puts what the
    observations show in place of the prediction; `observations` maps
    each statement's migration id and position to what was seen of it.

    A trace observes a statement in one of two ways. `sighting(tables)`
    is a read plan for what the server shows about the named tables,
    which the rehearsal runs before and after the statement; the
    observation is the two sightings. `attempts(impact, profile)` is
    the statement written with each clause to try, in order, beside the
    clause each spells; the rehearsal runs each until the server accepts
    one, and the observation is a `Probe`. `refused(error)` is the
    server's reason when an error says it refused the clause, and None
    for any other error, which ends the attempts. A statement with no
    attempts runs as written and has no observation, and so does one
    whose attempts all failed.
    """

    tables: Callable[[], Generator[str, Rows, Optional[FrozenSet[Any]]]]
    report: Callable[..., "ImpactReport"]
    sighting: Optional[Callable[[Sequence[str]], Generator[str, Rows, Any]]] = None
    attempts: Optional[
        Callable[["StatementImpact", "Profile"], Sequence[Tuple[str, Any]]]
    ] = None
    refused: Callable[[BaseException], Optional[str]] = _no_refusal


class Profile(NamedTuple):
    """
    One engine's rules. `blocks()` maps the engine's lock name to what
    it blocks, `lock_rank()` orders lock names weakest first, and
    `timeout_statement()` renders the statement that sets a lock
    timeout, for a migration inside a transaction or not.
    `transactional_ddl` says whether DDL holds its locks until the
    migration commits. `prefix` starts the id of every rule the profile
    owns, such as `pg` in `pg.create_index`. `context_plan()` is the
    catalog read that fills an `EngineContext` from a live server;
    `context_plan(True, tables)` also counts the rows of tables the
    engine keeps no estimate for, where the profile can, and reads the
    sizes of the named tables only, or of every table when `tables` is
    None.
    `title` is the engine's name as its vendor writes it, such as
    `PostgreSQL`.
    `fixture_schema` creates the objects the rules' fixtures name, for
    the ground-truth tests.

    `queues()` says whether a statement waiting for the lock makes other
    sessions queue behind it, which draws the lock-timeout finding; by
    default a lock that blocks writes or more does. `bounded()` says
    whether a value of the timeout setting bounds the wait.
    `local_scope` says whether `SET LOCAL` ends with the transaction, as
    on Postgres, or means the session, as on MySQL. `trace` is how a
    traced rehearsal observes the statements, or None on an engine the
    rehearsal cannot observe. `locks_database` says whether a write
    locks the whole database, as on SQLite, so a migration's locks make
    one transaction window. `release` names a version as people know it,
    such as SQL Server's `2022 (16.0.4135.4)`, where the version number
    alone does not. `preflight(impacts, older_than)` is the live
    preflight's read plan for the statements' impacts, or None on an
    engine without one.
    """

    name: str
    title: str
    prefix: str
    effects: Callable[[Facts], Outcome]
    blocks: Callable[[Optional[str]], Blocks]
    lock_rank: Callable[[Optional[str]], int]
    timeout_setting: str
    timeout_statement: Callable[[bool], str]
    transactional_ddl: bool
    rules: Tuple[Rule, ...]
    timeout_source: str
    context_plan: Callable[[bool, Optional[Collection[str]]], ContextPlan]
    fixture_schema: Tuple[str, ...] = ()
    queues: Optional[Callable[[Optional[str]], bool]] = None
    bounded: Callable[[str], bool] = sets_a_timeout
    local_scope: bool = True
    trace: Optional[Trace] = None
    locks_database: bool = False
    release: Optional[Callable[[Tuple[int, ...]], str]] = None
    preflight: Optional[
        Callable[[Sequence["StatementImpact"], float], "PreflightPlan"]
    ] = None

    def waits_in_queue(self, lock: Optional[str]) -> bool:
        """Whether waiting for the lock queues other sessions behind it."""
        if self.queues is not None:
            return self.queues(lock)
        return self.blocks(lock) >= Blocks.WRITES


def declared(namespace: Mapping[str, object]) -> Tuple[Rule, ...]:
    """
    Every rule a catalog module declares, in declaration order, from the
    module's `globals()`. The scan would also list a Rule the module
    imports, and no catalog imports one.
    """
    return tuple(value for value in namespace.values() if isinstance(value, Rule))


def _profiles() -> Mapping[str, Tuple[Profile, ...]]:
    """Each dialect's profiles, the one assumed without a server first."""
    from sustained.impact.rules import duckdb, mssql, mysql, postgres, sqlite

    return {
        "POSTGRES": (postgres.PROFILE,),
        "MYSQL": (mysql.MYSQL, mysql.MARIADB),
        "MSSQL": (mssql.PROFILE,),
        "DEFAULT": (sqlite.PROFILE,),
        "DUCKDB": (duckdb.PROFILE,),
    }


def profile_for(dialect: "Dialects", name: Optional[str] = None) -> Optional[Profile]:
    """
    The rule profile for a dialect, or None when it has none yet. `name`
    picks one of the dialect's profiles, as a context read from the
    server names it; without it, or with a name the dialect has no
    profile for, the first is used.
    """
    profiles = _profiles().get(dialect.name, ())
    for profile in profiles:
        if profile.name == name:
            return profile
    return profiles[0] if profiles else None


def profiles_for(dialect: "Dialects") -> Tuple[Profile, ...]:
    """Every profile of a dialect, the one assumed without a server first."""
    return _profiles().get(dialect.name, ())


def title(name: str) -> str:
    """The vendor's name for the engine a profile name stands for."""
    for profiles in _profiles().values():
        for profile in profiles:
            if profile.name == name:
                return profile.title
    return name


# The engine each dialect stands for, for a dialect with no profile.
_ENGINES = {
    "ATHENA": "Athena",
    "PRESTO": "Presto",
    "MSSQL": "SQL Server",
    "POSTGRES": "PostgreSQL",
    "MYSQL": "MySQL",
    "DUCKDB": "DuckDB",
    "DEFAULT": "SQLite",
}


def engine(dialect: "Dialects") -> str:
    """
    The vendor's name for the engines a dialect stands for, such as
    `SQLite` for `Dialects.DEFAULT`, or `MySQL and MariaDB`: the titles
    of its profiles, for the messages that name a dialect.
    """
    titles = [profile.title for profile in profiles_for(dialect)]
    if not titles:
        return _ENGINES.get(dialect.name, dialect.name)
    return " and ".join(titles)


def listed(names: Sequence[str]) -> str:
    """Names as a sentence lists them: `A`, `A and B`, or `A, B, and C`."""
    if len(names) < 3:
        return " and ".join(names)
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def release(name: str, version: Tuple[int, ...]) -> str:
    """A version of the engine a profile name stands for, as people write it."""
    from sustained.impact.context import version_text

    for profiles in _profiles().values():
        for profile in profiles:
            if profile.name == name and profile.release is not None:
                return profile.release(version)
    return version_text(version)


def supported(dialect: "Dialects") -> bool:
    """Whether the impact analysis has rules for the dialect."""
    return profile_for(dialect) is not None


def all_profiles() -> List[Profile]:
    """Every profile of every dialect."""
    return [profile for profiles in _profiles().values() for profile in profiles]


def all_rules() -> List[Rule]:
    """Every rule of every profile."""
    return [
        rule
        for profiles in _profiles().values()
        for profile in profiles
        for rule in profile.rules
    ]
