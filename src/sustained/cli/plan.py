"""
The `plan` command: the pending migrations, the problems, the model
drift, the guard verdicts, and the statements whose impact merits a look.
"""

from __future__ import annotations

import argparse
from types import ModuleType
from typing import (
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
)

from sustained.analysis import (
    MigrationStatement,
    PendingSummary,
    destructive_statements,
    normalize_statement,
    summarize,
)
from sustained.cli.config import _assert_algorithm, _exact_counts, _online
from sustained.cli.output import (
    JsonValue,
    _count,
    _print_json,
)
from sustained.dialects import Dialects
from sustained.guards import Verdict, blocking, run_guards
from sustained.impact import (
    EngineContext,
    StatementImpact,
    attach_impact,
    read_context,
    supported,
)
from sustained.impact.report import (
    flagged,
    flagged_line,
    statement_data,
)
from sustained.migrations import (
    REHEARSAL_PASSED,
    Migration,
    Migrator,
    migration_sql,
)


class _ModelPlans(NamedTuple):
    """
    The two plans plan reads from the config module's models, both
    diffed against one schema read, each a list of the migrations it
    generates. `preview` includes the drops, and `run` is what migrate
    would generate.
    """

    preview: List[Migration]
    run: List[Migration]


def _model_plans(
    migrator: Migrator, config: ModuleType, args: argparse.Namespace
) -> Optional[_ModelPlans]:
    """
    Both model plans from one read of the schema, or None when the config
    module names no models.
    """
    models = getattr(config, "models", None)
    if not models:
        return None
    snapshot = migrator.read_schema(list(models))
    asserted = _assert_algorithm(config, args)
    online = _online(config, args)
    return _ModelPlans(
        migrator.plan_migrations(
            list(models),
            allow_drops=True,
            snapshot=snapshot,
            assert_algorithm=asserted,
            online=online,
        ),
        migrator.plan_migrations(
            list(models), snapshot=snapshot, assert_algorithm=asserted, online=online
        ),
    )


def _drift_statements(
    migrator: Migrator, plans: Optional[_ModelPlans]
) -> Optional[List[str]]:
    """
    The statements that would close the gap between the config module's
    models and the database, or None when the module names no models.

    Drops are included: a preview reports every difference, including
    tables and columns the models no longer declare, which migrate does
    not generate. The statements print in full, so a drop reads as a drop
    without a separate label. Each statement is a MigrationStatement with
    the id of its migration in the preview, so the impact analysis reads
    the migrations apart.
    """
    if plans is None:
        return None
    return _generated_statements(migrator, plans.preview)


def _migrate_drift_statements(
    migrator: Migrator, plans: Optional[_ModelPlans]
) -> Optional[List[str]]:
    """
    The generated statements migrate would actually apply, or None when
    the config module names no models.

    This is the drift preview without the drops: migrate generates none
    unless it is called from Python with allow_drops=True. The guards read
    this set, so a verdict names a statement the run would run. Each
    statement is a MigrationStatement with its generated migration's id,
    so a rule that reads migration boundaries sees each generated
    migration as one of its own.
    """
    if plans is None:
        return None
    return _generated_statements(migrator, plans.run)


def _generated_statements(migrator: Migrator, migrations: List[Migration]) -> List[str]:
    """Each generated migration's up statements, with its id and flag."""
    return [
        MigrationStatement(sql, migration.id, migration.transactional)
        for migration in migrations
        for sql in migration_sql(migration, "up", migrator.compiler)
    ]


def _rehearsal_row_covers(migrator: Migrator, plans: Optional[_ModelPlans]) -> bool:
    """
    Whether a passing rehearsal already covers the run migrate would make,
    so the plan can point at migrate instead of rehearse.

    Two keys are tried: the pending migrations on their own, which is what
    a run without models applies, and the pending migrations plus the
    migration the models generate right now. The second is a guess. The
    real run diffs the models after the pending migrations have applied,
    and a pending migration that changes the same tables moves the
    generated statements, and with them the key. A wrong guess costs a
    stale suggestion; migrate still reads the row itself.
    """
    pending = migrator.pending()
    if not pending:
        return False
    records = migrator.read_applied_records()
    if migrator.run_outcome(records, pending) == REHEARSAL_PASSED:
        return True
    if plans is None or not plans.run:
        return False
    return migrator.run_outcome(records, pending + plans.run) == REHEARSAL_PASSED


def _print_pending(summaries: List[PendingSummary]) -> None:
    print("pending")
    width = max(len(s.id) for s in summaries)
    for summary in summaries:
        if summary.sql is None:
            size = "callable step"
        else:
            size = _count(len(summary.sql), "statement")
        marker = ""
        if summary.repeatable:
            marker = "  repeat" + (" changed" if summary.state == "changed" else "")
        print(f"  {summary.id:<{width}}  {size}{marker}")
        for statement in summary.destructive:
            print(f"    destructive  {statement}")


def _plan_verdicts(
    config: ModuleType,
    summaries: List[PendingSummary],
    drift: Optional[List[str]],
    dialect: Dialects,
    context: Optional[EngineContext] = None,
) -> Dict[str, List[Verdict]]:
    """
    The guards' verdicts on the statements migrate would apply, keyed by
    the normalized form of the statement they flag. A guard may report the
    statement in any form, so the key is normalized here and the readers
    normalize the statement they look up.

    The guards read the whole run at once, pending migrations and
    generated statements together, so a rule over the run as a whole sees
    what migrate will see. Each statement is a MigrationStatement with the
    id of the migration it belongs to, so a rule that reads migration
    boundaries reads the same ones migrate would. The drift the plan prints is the wider set: it
    includes the drops migrate does not generate, and no verdict is
    reported on those, because no run would read them.

    With a context, each statement's impact is analyzed with it and
    attached before the guards run, as migrate attaches it.
    """
    guards = list(getattr(config, "guards", None) or [])
    statements: List[str] = [s for summary in summaries for s in summary.sql or []]
    statements.extend(drift or [])
    if guards and context is not None:
        statements = list(attach_impact(statements, dialect, context))
    by_statement: Dict[str, List[Verdict]] = {}
    for verdict in run_guards(guards, statements, dialect):
        by_statement.setdefault(normalize_statement(verdict.statement), []).append(
            verdict
        )
    return by_statement


def _with_plan_impact(
    migrator: Migrator,
    summaries: List[PendingSummary],
    drift: Optional[List[str]],
    context: Optional[EngineContext],
) -> Tuple[List[PendingSummary], Optional[List[str]]]:
    """
    The pending migrations and the drift preview with each statement's
    impact attached, analyzed as one run in that order with the server
    facts the connection gives. Without a context, on a dialect the
    analysis does not cover, both come back as they are.
    """
    if context is None:
        return summaries, drift
    statements = [s for summary in summaries for s in summary.sql or []]
    statements.extend(drift or [])
    attached = iter(attach_impact(statements, migrator.dialect, context))

    def take(group: Optional[List[str]]) -> Optional[List[str]]:
        if group is None:
            return None
        return [next(attached) for _ in group]

    return [s._replace(sql=take(s.sql)) for s in summaries], take(drift)


def _impact_of(statement: str) -> Optional[StatementImpact]:
    impact: Optional[StatementImpact] = getattr(statement, "impact", None)
    return impact


def _statement_json(
    statements: Optional[List[str]],
    verdicts: Dict[str, List[Verdict]],
) -> Optional[List[Dict[str, JsonValue]]]:
    """
    One JSON object per statement, with the same keys everywhere a command
    reports SQL: the statement, whether it removes data, the guard
    verdicts on it, and its impact, null on a dialect the analysis does
    not cover. None stays None, for a callable step that renders no SQL.
    A verdict is reported on the statement it flags and nowhere else.
    """
    if statements is None:
        return None
    return [
        {
            "sql": statement,
            "destructive": bool(destructive_statements([statement])),
            "guards": [
                {"rule": v.rule, "verdict": v.verdict}
                for v in verdicts.get(normalize_statement(statement), [])
            ],
            "impact": _impact_json(statement),
        }
        for statement in statements
    ]


def _impact_json(statement: str) -> Optional[Dict[str, JsonValue]]:
    impact = _impact_of(statement)
    return statement_data(impact) if impact is not None else None


def _plan_json(
    summaries: List[PendingSummary],
    problems: List[str],
    drift: Optional[List[str]],
    verdicts: Dict[str, List[Verdict]],
) -> None:
    """
    Prints the plan as one JSON object. `drift` is null, not an empty
    list, when the config module names no models: nothing was compared,
    which differs from comparing and finding no gap.
    """
    _print_json(
        {
            "pending": [
                {
                    "id": summary.id,
                    "state": summary.state,
                    "repeatable": summary.repeatable,
                    "statements": _statement_json(summary.sql, verdicts),
                    "destructive": summary.destructive,
                }
                for summary in summaries
            ],
            "problems": problems,
            "drift": _statement_json(drift, verdicts),
        }
    )


def _print_guards(verdicts: List[Verdict]) -> None:
    """
    The guards section: one line per verdict, the rule that objected and
    the statement it read.
    """
    print("guards")
    width = max(len(v.rule) for v in verdicts)
    for verdict in verdicts:
        print(f"  {verdict.verdict:<5}  {verdict.rule:<{width}}  {verdict.statement}")


def _flagged_impact(
    summaries: List[PendingSummary], drift: Optional[List[str]]
) -> List[StatementImpact]:
    """
    The statements the impact section lists, in the order the plan
    prints them: those with a `warn` or `danger` finding, and those the
    analysis could not read.
    """
    statements = [s for summary in summaries for s in summary.sql or []]
    statements.extend(drift or [])
    impacts = [_impact_of(s) for s in statements]
    return flagged([impact for impact in impacts if impact is not None])


def _cmd_plan(migrator: Migrator, args: argparse.Namespace, config: ModuleType) -> int:
    states = dict(migrator.statuses())
    summaries = [
        summarize(m, states.get(m.id, "pending"), migrator.compiler)
        for m in migrator.pending()
    ]
    problems = migrator.validate(raise_on_problems=False)
    plans = _model_plans(migrator, config, args)
    drift = _drift_statements(migrator, plans)
    context = (
        read_context(migrator.connection, migrator.dialect, _exact_counts(config, args))
        if supported(migrator.dialect)
        else None
    )
    by_statement = _plan_verdicts(
        config,
        summaries,
        _migrate_drift_statements(migrator, plans),
        migrator.dialect,
        context,
    )
    verdicts = [v for group in by_statement.values() for v in group]
    blockers = blocking(verdicts)
    summaries, drift = _with_plan_impact(migrator, summaries, drift, context)

    # Problems mean the plan itself cannot be trusted, so they outrank a
    # blocked statement, which outranks work merely waiting.
    if problems:
        exit_code = 1
    elif blockers:
        exit_code = 3
    elif summaries or drift:
        exit_code = 2
    else:
        exit_code = 0

    if args.json:
        _plan_json(summaries, problems, drift, by_statement)
        return exit_code

    sections: List[str] = []
    if summaries:
        _print_pending(summaries)
        sections.append(_count(len(summaries), "pending migration"))
    if problems:
        if sections:
            print()
        print("problems")
        for problem in problems:
            print(f"  {problem}")
        sections.append(_count(len(problems), "problem"))
    if drift:
        if sections:
            print()
        print("drift")
        for statement in drift:
            print(f"  {statement}")
        sections.append(_count(len(drift), "drift statement"))
    if verdicts:
        if sections:
            print()
        _print_guards(verdicts)
        sections.append(_count(len(verdicts), "guard verdict"))
    listed = _flagged_impact(summaries, drift)
    if listed:
        if sections:
            print()
        print("impact")
        for impact_statement in listed:
            print(f"  {flagged_line(impact_statement)}")
        sections.append(_count(len(listed), "impact line"))

    if not sections:
        if drift is None:
            print(
                "Nothing pending, no problems. Drift unchecked: the config "
                "module names no models."
            )
        else:
            print("Nothing pending, no problems, no drift.")
        return exit_code
    print()
    print(", ".join(sections))
    if blockers and not problems:
        # migrate refuses a blocked statement, so there is one thing to do
        # and it is not running migrate.
        print(
            "blocked: fix the statement, or take the rule out of the guard list "
            "to run it anyway"
        )
    elif not problems:
        # migrate never generates drops, so a drift section with
        # nothing else is not work it can do.
        closable = [s for s in drift or [] if not destructive_statements([s])]
        if any(s.destructive for s in summaries) and not _rehearsal_row_covers(
            migrator, plans
        ):
            # migrate refuses these until a rehearsal has proved them.
            print("run: sustained rehearse")
        elif summaries or closable:
            print("run: sustained migrate")
        elif drift:
            print(
                "migrate does not generate drops: write the migration by "
                "hand, or call Migrator.up(models, allow_drops=True)."
            )
    return exit_code
