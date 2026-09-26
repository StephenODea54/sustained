"""
A traced rehearsal: each statement of each up step runs on its own, and
the impact report the rehearsal returns puts what the server did in
place of what the rules predicted.

On Postgres a statement runs between two reads of the locks the
transaction has taken and the files of the tables it names. On SQL
Server, which rehearses on a scratch database, it runs between two reads
of the locks, the partitions of the tables it names, and the log the
transaction has written. On MySQL and MariaDB, which rehearse on a
scratch database, an ALTER TABLE, CREATE INDEX, or DROP INDEX runs with
each ALGORITHM and LOCK clause in turn until the server accepts one.

The reads, the clauses, and the comparison are the profile's `Trace`,
such as the ones in sustained.impact.rules.postgres.trace,
sustained.impact.rules.mysql.trace, and
sustained.impact.rules.mssql.trace. This module runs them inside the
rehearsal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

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
    from sustained.impact import EngineContext, ImpactReport, StatementImpact
    from sustained.impact.rules import Profile, Trace


def _trace(m: MigratorBase) -> Optional["Trace"]:
    from sustained.impact.rules import profile_for

    profile = profile_for(m._dialect)
    return profile.trace if profile is not None else None


def check_traceable(m: MigratorBase) -> None:
    """Refuses a trace on a dialect whose locks it cannot read."""
    from sustained.exceptions import DialectError

    if _trace(m) is None:
        raise DialectError(
            f"rehearse(trace=True) observes the locks each statement takes, "
            f"which it can do on POSTGRES, MYSQL, and MSSQL only, not "
            f"{m._dialect.name}."
        )


class Tracer:
    """
    What a traced rehearsal has seen so far. `start()` reads the server
    facts and the tables that exist before the run; `run_step()` runs an
    up step one statement at a time, observing each. `ran` lists the
    statements observed so far, which the analysis of the next one reads
    first, so a statement sees the run state before it.
    """

    def __init__(self, m: MigratorBase) -> None:
        trace = _trace(m)
        assert trace is not None
        self.m = m
        self.trace = trace
        self.context: Optional["EngineContext"] = None
        self.existing: Optional[FrozenSet[Any]] = None
        self.ran: List["MigrationStatement"] = []
        self.observations: Dict[Tuple[Optional[str], int], object] = {}

    @property
    def profile(self) -> "Profile":
        """The rule profile the server facts name."""
        from sustained.impact.rules import profile_for

        profile = profile_for(self.m._dialect, getattr(self.context, "profile", None))
        assert profile is not None
        return profile

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
            observation = yield from self.observe(statement)
            if observation is not None:
                self.observations[(migration.id, index)] = observation
            self.ran.append(statement)

    def observe(self, statement: "MigrationStatement") -> Core[object]:
        """
        Runs the statement, and returns what was seen of it, or None
        when nothing was.
        """
        predicted = self.predicted(statement)
        if self.trace.sighting is None:
            return (yield from self.attempt(statement, predicted))
        tables = [t.table for t in predicted.tables]
        before = yield ReadCatalog(self.trace.sighting(tables))
        yield Execute(str(statement), pinned=True)
        after = yield ReadCatalog(self.trace.sighting(tables))
        return (before, after)

    def attempt(
        self, statement: "MigrationStatement", predicted: "StatementImpact"
    ) -> Core[object]:
        """
        Runs the statement with each of the trace's clauses until the
        server accepts one, and returns the `Probe`. An error that is
        not a refusal, or a refusal of every clause, runs the statement
        as written and returns None.
        """
        from sustained.impact.rules import Probe

        assert self.trace.attempts is not None
        refusals: List[Tuple[object, str]] = []
        for sql, clause in self.trace.attempts(predicted, self.profile):
            try:
                yield Execute(sql, pinned=True)
            except Exception as error:
                reason = self.trace.refused(error)
                if reason is None:
                    break
                refusals.append((clause, reason))
            else:
                return Probe(clause, tuple(refusals))
        yield Execute(str(statement), pinned=True)
        return None

    def predicted(self, statement: "MigrationStatement") -> "StatementImpact":
        """
        The statement's impact as the rules predict it, read after the
        statements the rehearsal already ran.
        """
        from sustained.impact import analyze

        report = analyze(self.ran + [statement], self.m._dialect, self.context)
        return report.statements[-1]

    def tables(self, statement: "MigrationStatement") -> List[str]:
        """The tables the rules predict the statement touches."""
        return [t.table for t in self.predicted(statement).tables]

    def report(self, run: Sequence[Migration]) -> "ImpactReport":
        """The run's impact, with each observed statement's facts."""
        from sustained.impact import analyze
        from sustained.migrations.checks import run_statements

        predicted = analyze(
            run_statements(run, self.m._compiler), self.m._dialect, self.context
        )
        return self.trace.report(
            predicted, self.observations, self.existing, self.profile
        )
