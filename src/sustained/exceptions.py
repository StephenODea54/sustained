"""
Custom exceptions for the Sustained query builder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Sequence

if TYPE_CHECKING:
    from sustained.guards import Verdict
    from sustained.impact.preflight import Preflight


class SustainedError(Exception):
    """Base exception for all Sustained-related errors."""

    pass


class DialectError(SustainedError):
    """Raised when a feature is not supported by the current SQL dialect."""

    pass


class AmbiguousColumns(SustainedError):
    """
    Raised when a result set repeats a column name. `columns` holds the
    repeated names, in the order the result set returned them.
    """

    def __init__(self, columns: Sequence[str]) -> None:
        self.columns = list(columns)
        names = ", ".join(repr(c) for c in self.columns)
        super().__init__(
            f"This result set returns {names} more than once, usually from a "
            "join over tables that share a column name. A row keeps one "
            "value per name, so the others would be lost. Alias them in "
            "select(), such as select('users.id AS user_id', "
            "'accounts.id AS account_id')."
        )


class RehearsalRequired(SustainedError):
    """
    Raised when a run would apply SQL that removes data and no passing
    rehearsal covers that exact set of statements.
    """

    pass


class GuardBlocked(SustainedError):
    """
    Raised when a guard blocks a statement the run would apply. `verdicts`
    holds the blocking verdicts, in the order the guards returned them.
    """

    def __init__(self, verdicts: Sequence["Verdict"]) -> None:
        self.verdicts = list(verdicts)
        width = max((len(v.rule) for v in self.verdicts), default=0)
        lines = [f"  {v.rule:<{width}}  {v.statement}" for v in self.verdicts]
        super().__init__(
            "\n".join(
                ["A guard blocked this run:"]
                + lines
                + [
                    "Fix the statement, or take the rule out of the guard "
                    "list to run it anyway."
                ]
            )
        )


class PreflightBlocked(SustainedError):
    """
    Raised by up(preflight='refuse') when another session has a table
    lock, or has asked for one, that a statement of the run would wait
    for; when a read the blockers come from failed, so the preflight
    could not see them; or when a statement is one the analysis cannot
    read, whose locks the preflight cannot check. `preflight` is the
    whole read. Nothing of the run had applied when it was raised,
    except the registered migrations of a run with models when the
    generated migration is the one that would wait.
    """

    def __init__(self, preflight: "Preflight") -> None:
        from sustained.impact.report import blocker_line, unread_line

        self.preflight = preflight
        lines: List[str] = []
        if preflight.blockers:
            lines.append("Other sessions have locks this run would wait for:")
            lines.extend(f"  {blocker_line(b)}" for b in preflight.blockers)
            lines.append("End those transactions, or run again once they finish.")
        if preflight.missing:
            lines.append(
                f"The preflight could not read {' or '.join(preflight.missing)}, "
                "so it cannot see the sessions this run would wait for. Make "
                "that read work on the server, or run with preflight='warn'."
            )
        if preflight.unread:
            lines.append("The preflight cannot check these statements:")
            lines.extend(f"  {unread_line(s)}" for s in preflight.unread)
            lines.append(
                "Split them into statements the analysis reads, or run with "
                "preflight='warn'."
            )
        super().__init__("\n".join(lines))


class MigrationError(SustainedError):
    """Raised when migration validation finds problems."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "Migration validation failed:\n"
            + "\n".join(f"- {p}" for p in self.problems)
        )
