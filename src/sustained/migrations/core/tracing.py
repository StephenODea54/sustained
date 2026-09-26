"""
A traced rehearsal: each statement of each up step runs on its own,
between two reads of the locks the transaction holds and the files of
the tables it names, and the impact report the rehearsal returns puts
what the server did in place of what the rules predicted.

The reads and the comparison live in sustained.impact.trace. This
module runs them inside the rehearsal.
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
    from sustained.impact import EngineContext, ImpactReport
    from sustained.impact.trace import Sighting


def check_traceable(m: MigratorBase) -> None:
    """Refuses a trace on a dialect whose locks it cannot read."""
    from sustained.exceptions import DialectError
    from sustained.impact.trace import traces

    if not traces(m._dialect):
        raise DialectError(
            f"rehearse(trace=True) reads the locks each statement takes, which "
            f"it can do on POSTGRES only, not {m._dialect.name}."
        )


class Tracer:
    """
    What a traced rehearsal has seen so far. `start()` reads the server
    facts and the tables that exist before the run; `run_step()` runs an
    up step one statement at a time between two sightings.
    """

    def __init__(self, m: MigratorBase) -> None:
        self.m = m
        self.context: Optional["EngineContext"] = None
        self.existing: Optional[FrozenSet[int]] = None
        self.sightings: Dict[
            Tuple[Optional[str], int], Tuple["Sighting", "Sighting"]
        ] = {}

    def start(self) -> Core[None]:
        from sustained.impact.trace import tables_plan

        self.context = yield ReadContext()
        self.existing = yield ReadCatalog(tables_plan())

    def run_step(self, migration: Migration) -> Core[None]:
        """
        Runs the migration's up step. A callable step runs whole and is
        not observed, since its statements are not known.
        """
        from sustained.analysis import MigrationStatement
        from sustained.impact.trace import sighting_plan

        elements = _step_elements(migration.up)
        if elements is None:
            yield RunStep(migration.up)
            return
        for index, sql in enumerate(_render_elements(elements, self.m._compiler)):
            statement = MigrationStatement(sql, migration.id, migration.transactional)
            tables = self.tables(statement)
            before: "Sighting" = yield ReadCatalog(sighting_plan(tables))
            yield Execute(str(sql), pinned=True)
            after: "Sighting" = yield ReadCatalog(sighting_plan(tables))
            self.sightings[(migration.id, index)] = (before, after)

    def tables(self, statement: str) -> List[str]:
        """The tables the rules predict the statement touches."""
        from sustained.impact import analyze

        report = analyze([statement], self.m._dialect, self.context)
        return [t.table for s in report.statements for t in s.tables]

    def report(self, run: Sequence[Migration]) -> "ImpactReport":
        """The run's impact, with each observed statement's facts."""
        from sustained.impact import analyze
        from sustained.impact.rules import profile_for
        from sustained.impact.trace import with_observations
        from sustained.migrations.checks import run_statements

        profile = profile_for(self.m._dialect)
        assert profile is not None
        predicted = analyze(
            run_statements(run, self.m._compiler), self.m._dialect, self.context
        )
        return with_observations(predicted, self.sightings, self.existing, profile)
