"""
The handler helpers every rule profile uses.

- `table()` and `tables()` name the tables a statement acts on.
- `dispatch()` hands a statement to the handler for its kind, and
  `unknown()` is the outcome for a statement or ALTER TABLE action no
  handler reads.
- `row_write_message()` words the finding for an UPDATE or DELETE.
- `add_stats()` keys a table's stats by `schema.table`, and by the bare
  name when an unqualified name finds the table.
- `with_observations()` puts a traced rehearsal's observations in place
  of a report's predictions, and `mismatch()` is the finding for a
  difference between the two. `settled_work()` picks the work to report
  from a prediction and an observation, and `work_mismatch()` words the
  difference.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from sustained.impact.context import TableStats
from sustained.impact.model import (
    Blocks,
    Confidence,
    Evidence,
    Finding,
    Hold,
    ImpactReport,
    MigrationImpact,
    Severity,
    StatementImpact,
    TableImpact,
    Work,
)
from sustained.impact.rules import Facts, Outcome, Profile, title
from sustained.impact.window import aggregate

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


def mismatch(message: str) -> Finding:
    """The finding for a difference between a prediction and an observation."""
    return Finding("impact.mismatch", Severity.WARN, message)


def settled_work(predicted: Work, observed: Optional[Work]) -> Optional[Work]:
    """
    The work to report, or None to keep the prediction. A copy that was
    seen stands. With no copy seen, a predicted scan, row write, or
    catalog change stands, since an observation cannot tell them apart,
    and a predicted copy falls to a scan, the heaviest work that copies
    nothing.
    """
    if observed is None or observed is Work.UNKNOWN:
        return None
    if observed in (Work.REWRITE, Work.INDEX_BUILD):
        return observed
    if predicted in (Work.REWRITE, Work.INDEX_BUILD):
        return Work.SCAN
    return None


def work_mismatch(table: TableImpact, observed: Optional[Work], nothing: str) -> str:
    """
    The mismatch message for observed work that differs from the
    prediction; `nothing` says what the server did when it copied
    nothing, such as `copied no file`.
    """
    if observed is Work.REWRITE:
        return f"the rules predicted {table.work} on {table.table}, and the server rewrote it"
    if observed is Work.INDEX_BUILD:
        return (
            f"the rules predicted {table.work} on {table.table}, and the server "
            "built an index on it"
        )
    what = "a rewrite" if table.work is Work.REWRITE else "an index build"
    return f"the rules predicted {what} on {table.table}, and the server {nothing}"


def _kept_to_commit(statement: StatementImpact) -> StatementImpact:
    """
    The statement with the hold of each lock that blocks something set
    to `transaction`, for a migration whose locks last until the commit,
    as the analysis sets it.
    """
    tables = tuple(
        t._replace(hold=Hold.TRANSACTION) if t.blocks > Blocks.NOTHING else t
        for t in statement.tables
    )
    return statement._replace(tables=tables)


def with_observations(
    report: ImpactReport,
    observations: Mapping[Tuple[Optional[str], int], Any],
    profile: Profile,
    observe: Callable[[StatementImpact, Any], StatementImpact],
) -> ImpactReport:
    """
    The report with each observed statement's facts in place of the
    predicted ones, and each migration's locks and windows read again
    from them. `observations` is keyed by each statement's migration id
    and its position in that migration, counting from 0, and
    `observe(statement, observation)` returns the observed impact. The
    report's evidence is `observed` when any statement was observed.
    """
    migrations: List[MigrationImpact] = []
    for migration in report.migrations:
        spans = migration.transactional and profile.transactional_ddl
        statements = []
        for index, statement in enumerate(migration.statements):
            key = (migration.migration_id, index)
            if key in observations:
                statement = observe(statement, observations[key])
                if spans:
                    statement = _kept_to_commit(statement)
            statements.append(statement)
        locks, windows, findings = aggregate(statements, spans, profile.locks_database)
        migrations.append(
            migration._replace(
                statements=tuple(statements),
                locks=locks,
                windows=windows,
                findings=findings,
            )
        )
    observed = any(
        s.evidence is Evidence.OBSERVED for m in migrations for s in m.statements
    )
    return report._replace(
        migrations=tuple(migrations),
        evidence=Evidence.OBSERVED if observed else report.evidence,
    )
