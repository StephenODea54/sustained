"""
A traced rehearsal: each statement of each up step runs on its own,
between two reads of the locks the transaction holds and the files of
the tables it names, and the impact report the rehearsal returns puts
what the server did in place of what the rules predicted.

The reads and the comparison are the profile's `Trace`, such as the one
in sustained.impact.rules.postgres.trace. This module runs them inside
the rehearsal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, FrozenSet, List, Optional, Sequence, Tuple

from sustained.migrations.core.base import MigratorBase
from sustained.migrations.core.requests import (
    Core,
    Execute,
    ReadCatalog,
    ReadContext,
    RunStep,
)
from sustained.migrations.migration import (
    Migration,
    _render_elements,
    _step_elements,
)

if TYPE_CHECKING:
    from sustained.analysis import MigrationStatement
    from sustained.impact import EngineContext, ImpactReport
    from sustained.impact.rules import Trace


def _trace(m: MigratorBase) -> Optional["Trace"]:
    from sustained.impact.rules import profile_for

    profile = profile_for(m._dialect)
    return profile.trace if profile is not None else None


def check_traceable(m: MigratorBase) -> None:
    """Refuses a trace on a dialect whose locks it cannot read."""
    from sustained.exceptions import DialectError

    if _trace(m) is None:
        raise DialectError(
            f"rehearse(trace=True) reads the locks each statement takes, which "
            f"it can do on POSTGRES only, not {m._dialect.name}."
        )


class Tracer:
    """
    What a traced rehearsal has seen so far. `start()` reads the server
    facts and the tables that exist before the run; `run_step()` runs an
    up step one statement at a time between two sightings. `ran` lists
    the statements observed so far, which the analysis of the next one
    reads first, so a statement sees the run state before it.
    """

    def __init__(self, m: MigratorBase) -> None:
        trace = _trace(m)
        assert trace is not None
        self.m = m
        self.trace = trace
        self.context: Optional["EngineContext"] = None
        self.existing: Optional[FrozenSet[int]] = None
        self.ran: List["MigrationStatement"] = []
        self.sightings: Dict[Tuple[Optional[str], int], Tuple[object, object]] = {}

    def start(self) -> Core[None]:
        self.context = yield ReadContext()
        self.existing = yield ReadCatalog(self.trace.tables())

    def run_step(self, migration: Migration) -> Core[None]:
        """
        Runs the migration's up step. A callable step runs whole and is
        not observed, since its statements are not known.
        """
        from sustained.analysis import MigrationStatement

        elements = _step_elements(migration.up)
        if elements is None:
            yield RunStep(migration.up)
            return
        for index, sql in enumerate(_render_elements(elements, self.m._compiler)):
            statement = MigrationStatement(sql, migration.id, migration.transactional)
            tables = self.tables(statement)
            before = yield ReadCatalog(self.trace.sighting(tables))
            yield Execute(str(sql), pinned=True)
            after = yield ReadCatalog(self.trace.sighting(tables))
            self.sightings[(migration.id, index)] = (before, after)
            self.ran.append(statement)

    def tables(self, statement: "MigrationStatement") -> List[str]:
        """
        The tables the rules predict the statement touches, read after
        the statements the rehearsal already ran.
        """
        from sustained.impact import analyze

        report = analyze(self.ran + [statement], self.m._dialect, self.context)
        return [t.table for t in report.statements[-1].tables]

    def report(self, run: Sequence[Migration]) -> "ImpactReport":
        """The run's impact, with each observed statement's facts."""
        from sustained.impact import analyze
        from sustained.impact.rules import profile_for
        from sustained.migrations.checks import run_statements

        profile = profile_for(self.m._dialect, getattr(self.context, "profile", None))
        assert profile is not None
        predicted = analyze(
            run_statements(run, self.m._compiler), self.m._dialect, self.context
        )
        return self.trace.report(predicted, self.sightings, self.existing, profile)
