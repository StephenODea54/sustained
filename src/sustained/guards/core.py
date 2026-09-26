"""
The verdicts a guard returns, the guard type, and running guards over a
run's statements.
"""

from __future__ import annotations

from typing import Callable, List, NamedTuple, Sequence

from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects

# The two verdicts a rule can return. There is no third severity: a rule
# either stops the run or tells the operator about it.
BLOCK = "block"
WARN = "warn"


class Verdict(NamedTuple):
    """
    One rule's objection to one statement: the rule that objected, whether
    it blocks or warns, and the statement it read.
    """

    rule: str
    verdict: str
    statement: str


# A guard reads the statements an up run would apply and returns its
# verdicts. A MigrationStatement is a str that also names the migration
# it came from, so a guard written against Sequence[str] fits here and
# reads the statements as plain strings.
Guard = Callable[[Sequence[MigrationStatement], Dialects], List[Verdict]]


def run_guards(
    guards: Sequence[Guard], statements: Sequence[str], dialect: Dialects
) -> List[Verdict]:
    """
    Runs every guard over the statements and returns the verdicts, in
    guard order. An empty guard list returns an empty list, so a caller
    with no guards pays nothing.

    A plain string is wrapped in a MigrationStatement that names no
    migration, so every guard reads the same kind of value.
    """
    tagged = [
        s if isinstance(s, MigrationStatement) else MigrationStatement(s)
        for s in statements
    ]
    verdicts: List[Verdict] = []
    for guard in guards:
        verdicts.extend(guard(tagged, dialect))
    return verdicts


def blocking(verdicts: Sequence[Verdict]) -> List[Verdict]:
    """The verdicts that stop a run."""
    return [v for v in verdicts if v.verdict == BLOCK]


def warnings_only(verdicts: Sequence[Verdict]) -> List[Verdict]:
    """The verdicts that only tell the operator."""
    return [v for v in verdicts if v.verdict == WARN]
