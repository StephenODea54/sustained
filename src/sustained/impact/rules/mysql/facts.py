"""
What the InnoDB handlers read about the server and the table, and
`Change`, what the server does for one ALTER TABLE action.
"""

from __future__ import annotations

from typing import (
    NamedTuple,
    Optional,
    Tuple,
)

from sustained.impact.context import (
    TableStats,
)
from sustained.impact.model import (
    Confidence,
    Work,
)
from sustained.impact.rules import Facts
from sustained.impact.rules.mysql.catalog import (
    RULE_SETS,
    RuleSet,
)
from sustained.impact.rules.mysql.locks import (
    Online,
)


def is_mariadb(facts: Facts) -> bool:
    return facts.context.profile == "mariadb"


def rules_for(facts: Facts) -> RuleSet:
    return RULE_SETS[facts.context.profile]


def version_of(facts: Facts) -> Tuple[int, ...]:
    return facts.context.version


def copy_algorithm(facts: Facts) -> Online:
    """COPY as the server runs it: MariaDB 11.2 and later allow writes."""
    if is_mariadb(facts) and version_of(facts) >= (11, 2):
        return Online("COPY", "NONE")
    return Online("COPY", "SHARED")


def nocopy_algorithm(facts: Facts) -> Online:
    """An in-place change that rebuilds nothing, as each server names it."""
    return Online("NOCOPY", "NONE") if is_mariadb(facts) else Online("INPLACE", "NONE")


def row_version_limit(version: Tuple[int, ...]) -> int:
    return 255 if version >= (9, 1) else 64


def _off(value: Optional[str]) -> bool:
    return value is not None and value.strip().upper() in ("0", "OFF", "FALSE")


def foreign_key_checks_off(facts: Facts) -> bool:
    setting = facts.state.settings.get("foreign_key_checks")
    if setting is None:
        setting = facts.context.settings.get("foreign_key_checks")
    return _off(setting)


def table_stats(facts: Facts, table: str) -> TableStats:
    if facts.state.is_new(table):
        return TableStats(0, 0, "DYNAMIC", 0, False)
    return facts.context.stats(facts.state.original(table))


class Change(NamedTuple):
    """What the server does for one ALTER TABLE action."""

    online: Online
    work: Work
    rule: str
    reason: str
    confidence: Confidence = Confidence.KNOWN
    notes: Tuple[str, ...] = ()


def rename_note(what: str, old: str) -> str:
    return f"running application code that names the {what} {old} fails once the rename runs"
