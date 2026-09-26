"""
The handler helpers every rule profile uses.

- `table()` and `tables()` name the tables a statement acts on.
- `dispatch()` hands a statement to the handler for its kind, and
  `unknown()` is the outcome for a statement or ALTER TABLE action no
  handler reads.
- `row_write_message()` words the finding for an UPDATE or DELETE.
- `add_stats()` keys a table's stats by `schema.table`, and by the bare
  name when an unqualified name finds the table.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Mapping

from sustained.impact.context import TableStats
from sustained.impact.model import Confidence, Finding, Severity
from sustained.impact.rules import Facts, Outcome, title

UNNAMED_TABLE = "(unnamed table)"

Handler = Callable[[Facts], Outcome]


def table(facts: Facts) -> str:
    """The table the statement names, or a placeholder when it names none."""
    return facts.parsed.table or UNNAMED_TABLE


def tables(facts: Facts) -> List[str]:
    """Every table a statement that may name several acts on."""
    named = facts.parsed.items("tables")
    if named:
        return [str(t) for t in named]
    return [facts.parsed.table] if facts.parsed.table else []


def nothing(facts: Facts) -> Outcome:
    """The outcome of a statement that locks no table."""
    return Outcome()


def unknown(facts: Facts, what: str) -> Outcome:
    """The outcome of a statement or action no rule of the profile reads."""
    return Outcome(
        findings=(
            Finding(
                "impact.unknown",
                Severity.INFO,
                f"no {title(facts.context.profile)} rule reads {what}",
            ),
        ),
        confidence=Confidence.UNKNOWN,
    )


def dispatch(facts: Facts, handlers: Mapping[str, Handler]) -> Outcome:
    """The outcome of the handler for the statement's kind."""
    handler = handlers.get(facts.parsed.kind)
    if handler is None:
        return unknown(facts, f"a {facts.parsed.kind} statement")
    return handler(facts)


def row_write_message(facts: Facts, table: str, detail: str = "") -> str:
    """
    The finding for an UPDATE or DELETE: writes to the rows it changes
    wait until it ends, or until the migration commits inside a
    transaction. `detail` adds the engine's own reason after that.
    """
    if facts.intent is not None and facts.intent.kind == "backfill":
        what = "the backfill"
    else:
        what = f"the {facts.parsed.kind.upper()}"
    until = "the migration commits" if facts.transactional else "it ends"
    return (
        f"writes to the rows {what} changes on {table} wait until {until}"
        f"{detail}; on a large table, backfill in batches outside the DDL "
        "migration"
    )


def add_stats(
    found: Dict[str, TableStats],
    schema: str,
    name: str,
    bare: bool,
    stats: TableStats,
) -> str:
    """
    Adds a table's stats under `schema.table`, and under the bare name
    when `bare` says an unqualified name finds it. Returns the full key.
    """
    key = f"{schema}.{name}".lower()
    found[key] = stats
    if bare:
        found[name.lower()] = stats
    return key
