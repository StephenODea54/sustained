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

`profile_for()` picks the profile for a dialect. A dialect without one
has no impact analysis yet.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
)

from sustained.impact.context import ContextPlan, EngineContext
from sustained.impact.model import (
    Blocks,
    Confidence,
    Finding,
    Intent,
    ParsedStatement,
    Work,
)
from sustained.impact.state import RunState

if TYPE_CHECKING:
    from sustained.dialects import Dialects


def _every_version(version: Tuple[int, ...]) -> bool:
    return True


class Rule(NamedTuple):
    """
    One piece of engine behaviour. `versions` says whether the rule
    holds on a server version; `fixtures` are statements that exercise
    it, which the ground-truth tests run against real servers.
    """

    id: str
    source: str
    fixtures: Tuple[str, ...] = ()
    versions: Callable[[Tuple[int, ...]], bool] = _every_version


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
    """A profile's answer for one statement."""

    effects: Tuple[Effect, ...] = ()
    findings: Tuple[Finding, ...] = ()
    confidence: Confidence = Confidence.KNOWN


class Profile(NamedTuple):
    """
    One engine's rules. `blocks()` maps the engine's lock name to what
    it blocks, `lock_rank()` orders lock names weakest first, and
    `timeout_statement()` renders the statement that sets a lock
    timeout, for a migration inside a transaction or not.
    `transactional_ddl` says whether DDL holds its locks until the
    migration commits. `prefix` starts the id of every rule the profile
    owns, such as `pg` in `pg.create_index`. `context_plan()` is the
    catalog read that fills an `EngineContext` from a live server.
    """

    name: str
    prefix: str
    effects: Callable[[Facts], Outcome]
    blocks: Callable[[Optional[str]], Blocks]
    lock_rank: Callable[[Optional[str]], int]
    timeout_setting: str
    timeout_statement: Callable[[bool], str]
    transactional_ddl: bool
    rules: Tuple[Rule, ...]
    timeout_source: str
    context_plan: Callable[[], ContextPlan]


def _profiles() -> Mapping[str, Profile]:
    from sustained.impact.rules import postgres

    return {"POSTGRES": postgres.PROFILE}


def profile_for(dialect: "Dialects") -> Optional[Profile]:
    """The rule profile for a dialect, or None when it has none yet."""
    return _profiles().get(dialect.name)


def supported(dialect: "Dialects") -> bool:
    """Whether the impact analysis has rules for the dialect."""
    return profile_for(dialect) is not None


def all_rules() -> List[Rule]:
    """Every rule of every profile."""
    return [rule for profile in _profiles().values() for rule in profile.rules]
