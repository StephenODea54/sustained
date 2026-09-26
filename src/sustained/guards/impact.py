"""
The impact guards, which read each statement's `StatementImpact`
instead of its text; see sustained.impact.

Each rule is a test over one statement's impact. `_impact_guard()`
turns the test into a guard that blocks every statement it passes, and
marks the guard with `reads_impact`.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Union

from sustained.analysis import normalize_statement
from sustained.dialects import Dialects
from sustained.guards.core import BLOCK, Guard, Verdict
from sustained.impact.model import (
    Blocks,
    Confidence,
    StatementImpact,
    TableImpact,
    Work,
)


def reads_impact(guard: Guard) -> bool:
    """Whether a guard is an impact rule, which reads `statement.impact`."""
    return bool(getattr(guard, "reads_impact", False))


def statement_impacts(
    statements: Sequence[str], dialect: Dialects
) -> List[Optional[StatementImpact]]:
    """
    Each statement's impact, in order: the `impact` a MigrationStatement
    has, or else what `analyze()` gives with no context, reading the
    statements as one run. Every entry is None on a dialect the analysis
    does not cover.
    """
    from sustained.impact import analyze, supported

    if not supported(dialect):
        return [None] * len(statements)
    attached: List[Optional[StatementImpact]] = [
        getattr(s, "impact", None) for s in statements
    ]
    if all(impact is not None for impact in attached):
        return attached
    analyzed = analyze(statements, dialect).statements
    return [a if a is not None else b for a, b in zip(attached, analyzed)]


def _impact_guard(rule: str, blocks: Callable[[StatementImpact], bool]) -> Guard:
    """
    A guard that blocks each statement whose impact `blocks` returns
    True for, reporting it under `rule`.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        return [
            Verdict(rule, BLOCK, normalize_statement(statement))
            for statement, impact in zip(
                statements, statement_impacts(statements, dialect)
            )
            if impact is not None and blocks(impact)
        ]

    setattr(guard, "reads_impact", True)
    return guard


class _Size:
    """
    The size thresholds a rule reads a table against. With neither
    threshold every table counts. A known size past either threshold
    counts; a size a threshold needs and the analysis does not know
    counts unless `assume_small` is set.
    """

    def __init__(
        self,
        name: str,
        over_rows: Optional[int],
        over_bytes: Optional[int],
        assume_small: bool,
    ) -> None:
        if (over_rows is not None and over_rows < 0) or (
            over_bytes is not None and over_bytes < 0
        ):
            raise ValueError(f"{name} needs thresholds of 0 or more.")
        self.over_rows = over_rows
        self.over_bytes = over_bytes
        self.assume_small = assume_small

    def label(self) -> List[str]:
        """The thresholds given, as the verdict's rule name spells them."""
        parts = []
        if self.over_rows is not None:
            parts.append(f"over_rows={self.over_rows}")
        if self.over_bytes is not None:
            parts.append(f"over_bytes={self.over_bytes}")
        return parts

    def counts(self, table: TableImpact) -> bool:
        if self.over_rows is None and self.over_bytes is None:
            return True
        unknown = False
        for size, limit in (
            (table.rows, self.over_rows),
            (table.bytes, self.over_bytes),
        ):
            if limit is None:
                continue
            if size is None:
                unknown = True
            elif size > limit:
                return True
        return unknown and not self.assume_small


def max_blocking(
    limit: Union[Blocks, str],
    over_rows: Optional[int] = None,
    over_bytes: Optional[int] = None,
    assume_small: bool = False,
) -> Guard:
    """
    Blocks a statement that blocks more than `limit` on a table past the
    size thresholds. `limit` is a `Blocks` member or its name:
    `nothing`, `ddl`, `writes`, or `reads_and_writes`. So
    `max_blocking("writes")` passes a lock that stops writes and blocks
    one that stops reads as well.

    With neither `over_rows` nor `over_bytes`, every table counts. With
    either, a table counts when its estimated size passes one of them,
    and when the size the threshold reads is unknown, unless
    `assume_small=True`. A table the run created earlier blocks nothing
    in the analysis, so it never counts.
    """
    ceiling = Blocks(limit)
    size = _Size("max_blocking", over_rows, over_bytes, assume_small)
    rule = f"max_blocking({', '.join([str(ceiling)] + size.label())})"
    return _impact_guard(
        rule,
        lambda impact: any(
            table.blocks > ceiling and size.counts(table) for table in impact.tables
        ),
    )


def no_rewrite(
    over_rows: Optional[int] = None,
    over_bytes: Optional[int] = None,
    assume_small: bool = False,
) -> Guard:
    """
    Blocks a statement that rewrites a table past the size thresholds:
    work `rewrite`, or `unknown`, which the analysis ranks above it. The
    thresholds read as they do for `max_blocking()`. A table the run
    created earlier is never rewritten in the analysis.
    """
    size = _Size("no_rewrite", over_rows, over_bytes, assume_small)
    return _impact_guard(
        f"no_rewrite({', '.join(size.label())})",
        lambda impact: any(
            table.work >= Work.REWRITE and size.counts(table) for table in impact.tables
        ),
    )


def lock_timeout_required() -> Guard:
    """
    Blocks a statement whose lock would queue reads or writes with no
    lock timeout in scope: the statements the analysis gives a
    `<profile>.lock_timeout` finding, such as `pg.lock_timeout`. It
    reads timeout scopes as `no_lock_without_timeout()` does, and covers
    every such lock where that rule reads only ALTER TABLE and DROP
    TABLE. A timeout the connection already has covers the whole run
    when the migrator read it.
    """
    from sustained.impact.analyzer import is_lock_timeout

    return _impact_guard(
        "lock_timeout_required",
        lambda impact: any(is_lock_timeout(f) for f in impact.findings),
    )


def no_unknown_impact() -> Guard:
    """
    Blocks a statement the impact analysis cannot read, the strict mode
    for hand-written SQL. The other impact rules pass such a statement,
    since the analysis names no table for it.
    """
    return _impact_guard(
        "no_unknown_impact",
        lambda impact: impact.confidence is Confidence.UNKNOWN,
    )
