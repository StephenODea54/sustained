"""
The runs themselves: up() and down() with the callbacks around them, one
migration applied or reverted inside its scope, and the diff against the
models that up(models=[...]) applies.

Each function here is the body of a migrator method, or a helper of one,
written once for both migrators. What a public method promises is on its
docstring in Migrator; the notes here are about how.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

from sustained.dialects import Dialects
from sustained.impact.preflight import OLDER_THAN
from sustained.migrations import planning
from sustained.migrations.checks import (
    _changed_down_message,
    _changed_since_applied,
    _failed_attempt_problem,
    _is_current,
    _validation_problems,
    check_statements,
    report_danger,
    run_statements,
)
from sustained.migrations.core import bookkeeping
from sustained.migrations.core.base import MigratorBase
from sustained.migrations.core.requests import (
    Autocommit,
    Commit,
    Core,
    DiffSource,
    Execute,
    Fire,
    ReadCatalog,
    ReadContext,
    RefuseOpenTransaction,
    RunStep,
    T,
    Transaction,
    run_in,
)
from sustained.migrations.migration import (
    Migration,
    MigrationStep,
    PreflightCheck,
    _checked_steps,
    _stored_steps,
    _tag_applied,
    _tag_migration,
    migration_checksum,
)
from sustained.migrations.planning import (
    asserted_migration,
    drift_lines,
    plan_migration,
)
from sustained.migrations.rehearsal import (
    REHEARSAL_OVERRIDE,
    _destructive_in,
    rehearsal_key,
)
from sustained.migrations.tracking import _next_seq
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.analysis import MigrationStatement
    from sustained.guards import Verdict
    from sustained.impact import ImpactReport
    from sustained.impact.preflight import Preflight
    from sustained.introspect import Snapshot
    from sustained.model import Model


def migration_scope(
    m: MigratorBase, body: Core[T], transactional: bool = True
) -> Core[T]:
    """
    Runs one migration's body: inside a transaction on engines whose
    schema changes roll back; bare and followed by a commit on engines
    whose do not.

    `transactional` is the migration's own flag. A migration with
    transactional=False runs outside a transaction on every engine, so
    a statement the engine refuses inside a transaction block, such as
    CREATE INDEX CONCURRENTLY on Postgres, can run. Its tracking row is
    written after its statements, in the same bare mode, so a finished
    migration is still recorded. An async adapter over a driver that
    opens its own transaction, such as DbApiAsyncAdapter over psycopg2,
    is the limit here: the driver still opens one, and such a statement
    still fails. Run it on an adapter that executes bare, such as
    AsyncpgAdapter.

    A rehearsal opens one transaction around the whole run and rolls it
    back at the end, so each migration runs bare and nothing commits.

    Nothing takes a failed non-transactional migration back. The
    statements that already ran stay in the database, and the tracking
    row says the attempt failed. The operator finishes or undoes the
    rest by hand and then runs repair(). On Postgres a failed CREATE
    INDEX CONCURRENTLY also leaves an invalid index, which needs a
    DROP INDEX of its own.
    """
    if m._rehearsing:
        return (yield from body)
    if transactional and m._compiler.supports_transactional_ddl():
        return (yield from run_in(Transaction, body))
    if not transactional:
        return (yield from run_in(Autocommit, body))
    result = yield from body
    yield Commit()
    return result


def fire_on_error(m: MigratorBase, error: BaseException) -> Core[None]:
    """
    Hands a failed run to the on_error callback. A callback that raises
    must not replace the error it was told about, so its own failure is
    reported on stderr and set aside. before_migrate and after_migrate
    are called plainly: a failure there is the operator's own and stops
    the run.
    """
    hook = m._callbacks.on_error
    if hook is None:
        return
    try:
        yield Fire(hook, (getattr(error, "migration_id", None), error))
    except Exception as callback_error:
        print(f"error: on_error raised {callback_error!r}", file=sys.stderr)


class RunReads(NamedTuple):
    """
    What up() reads from the server around the run: `assert_algorithm`
    writes the predicted ALGORITHM and LOCK clauses on the generated
    migration, `exact_counts` passes on to the context read, and
    `preflight` is the live preflight check, or None for none. `online`
    generates the online form of the migration, as
    autogenerate_migrations() describes.
    """

    assert_algorithm: bool = False
    exact_counts: bool = False
    preflight: Optional[PreflightCheck] = None
    online: bool = False


def up(
    m: MigratorBase,
    target: Optional[str],
    validate: bool,
    allow_out_of_order: bool,
    models: Optional[List[Type["Model"]]],
    allow_drops: bool,
    ignore_changed_columns: bool,
    migration_id: Optional[str],
    renames: Optional[Dict[str, str]],
    table_renames: Optional[Dict[str, str]],
    type_casts: Optional[Dict[str, str]],
    unrehearsed: bool,
    reads: RunReads = RunReads(),
) -> Core[List[str]]:
    yield RefuseOpenTransaction("up")
    callbacks = m._callbacks
    if callbacks.before_migrate is not None:
        yield Fire(callbacks.before_migrate)
    try:
        applied = yield from run_up(
            m,
            target=target,
            validate=validate,
            allow_out_of_order=allow_out_of_order,
            models=models,
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            migration_id=migration_id,
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
            unrehearsed=unrehearsed,
            reads=reads,
        )
    except Exception as error:
        yield from fire_on_error(m, error)
        raise
    if applied and callbacks.after_migrate is not None:
        yield Fire(callbacks.after_migrate, (applied,))
    return applied


def run_up(
    m: MigratorBase,
    target: Optional[str],
    validate: bool,
    allow_out_of_order: bool,
    models: Optional[List[Type["Model"]]],
    allow_drops: bool,
    ignore_changed_columns: bool,
    migration_id: Optional[str],
    renames: Optional[Dict[str, str]],
    table_renames: Optional[Dict[str, str]],
    type_casts: Optional[Dict[str, str]],
    unrehearsed: bool,
    reads: RunReads = RunReads(),
) -> Core[List[str]]:
    """The run itself, without the callbacks up() wraps it in."""
    from sustained.exceptions import MigrationError

    if models is not None and target is not None:
        raise ValueError(
            "up() cannot take both models and a target: the generated "
            "migration always runs last, so a target would leave it out."
        )
    require_registered = models is None

    def locked() -> Core[List[str]]:
        if models is not None:
            yield from bookkeeping.ensure_tracking_table(m)

        migrations = m._versioned()
        if target is not None:
            ids = [x.id for x in migrations]
            if target not in ids:
                if any(x.id == target for x in m._repeatables()):
                    raise ValueError(
                        f"Migration target {target!r} is repeatable; a "
                        "target must name a versioned migration."
                    )
                raise ValueError(f"Unknown migration target: {target!r}.")
            migrations = migrations[: ids.index(target) + 1]

        records = yield from bookkeeping.applied_records(m)
        if validate:
            problems = _validation_problems(
                m._migrations,
                records,
                allow_out_of_order,
                require_registered=require_registered,
            )
            if problems:
                raise MigrationError(problems)
        records_by_id = {r.id: r for r in records}
        already_applied = {r.id for r in records if r.success}
        next_seq = _next_seq(records)
        applied_now: List[str] = []
        versioned_now = [x for x in migrations if x.id not in already_applied]
        repeatables_now = [
            x
            for x in (m._repeatables() if target is None else [])
            if not _is_current(records_by_id.get(x.id), x, True)
        ]
        # The registered set is checked before anything runs. The
        # order matches pending(), so a rehearsal of the same set
        # produces the same key.
        registered_run = versioned_now + repeatables_now
        warned: Set["Verdict"] = set()
        dangers: Set[Tuple[str, str]] = set()
        shown: Set[str] = set()
        checked = yield from guard_run(
            m, registered_run, warned, dangers, reads.exact_counts
        )
        yield from check_preflight(m, checked, reads.preflight, shown)
        yield from bookkeeping.require_rehearsal_row(
            m, records, registered_run, unrehearsed, target
        )
        final_run = list(registered_run)
        # A migration applied before a failure stays applied and
        # committed, so the error lists it for the caller.
        try:
            for migration in versioned_now:
                yield from apply(m, migration, next_seq, update=False)
                next_seq += 1
                applied_now.append(migration.id)
            if models is not None:
                generated = yield from plan_migrations(
                    m,
                    models,
                    allow_drops=allow_drops,
                    ignore_changed_columns=ignore_changed_columns,
                    migration_id=migration_id,
                    renames=renames,
                    table_renames=table_renames,
                    type_casts=type_casts,
                    assert_algorithm=reads.assert_algorithm,
                    online=reads.online,
                )
                if generated:
                    # The generated statements are known only now, after
                    # the registered migrations left the schema they diff
                    # against, so both gates run a second time before the
                    # migrations they could not see. The registered
                    # migrations are already applied and committed by
                    # then, so a block here reports what it stopped after.
                    final_run = registered_run + generated
                    checked = yield from guard_run(
                        m, final_run, warned, dangers, reads.exact_counts
                    )
                    generated_ids = {g.id for g in generated}
                    yield from check_preflight(
                        m,
                        [s for s in checked if s.migration_id in generated_ids],
                        reads.preflight,
                        shown,
                    )
                    yield from bookkeeping.require_rehearsal_row(
                        m, records, final_run, unrehearsed, target
                    )
                    for migration in generated:
                        # A migration joins the registered list only after
                        # it applied. A failed one left there would run
                        # again on the next up() of a long-lived migrator,
                        # and would run alongside a fresh diff of the same
                        # models.
                        yield from apply(
                            m, migration, next_seq, update=False, generated=True
                        )
                        m._migrations.append(migration)
                        next_seq += 1
                        applied_now.append(migration.id)
            for migration in repeatables_now:
                record = records_by_id.get(migration.id)
                yield from apply(m, migration, next_seq, update=record is not None)
                if record is None:
                    next_seq += 1
                applied_now.append(migration.id)
            if unrehearsed and _destructive_in(final_run, m._compiler):
                # The proof was waived, so the row says so. It never
                # unlocks a later run: only 'passed' does that.
                yield from bookkeeping.record_rehearsal(
                    m, rehearsal_key(records, final_run), REHEARSAL_OVERRIDE
                )
            return applied_now
        except Exception as error:
            _tag_applied(error, applied_now)
            raise

    return (yield from bookkeeping.lock_scope(m, locked()))


def guard_run(
    m: MigratorBase,
    run: List[Migration],
    warned: Set["Verdict"],
    dangers: Set[Tuple[str, str]],
    exact_counts: bool = False,
) -> Core[List["MigrationStatement"]]:
    """
    Runs the guards over the statements a run would apply, and returns
    the statements. On a dialect the impact analysis covers, the server
    facts are read first and each statement's impact is attached, so an
    impact rule reads it. When no guard reads impact, each `danger`
    finding prints on stderr after the guards pass. `warned` and
    `dangers` collect what was already printed, for a run checked
    twice. `exact_counts` passes on to the read.
    """
    from sustained.guards import reads_impact
    from sustained.impact import attach_impact, supported

    statements = run_statements(run, m._compiler)
    if statements and supported(m._dialect):
        context = yield ReadContext(exact_counts)
        statements = attach_impact(statements, m._dialect, context)
        check_statements(m._guards, statements, m._dialect, warned)
        if not any(reads_impact(guard) for guard in m._guards):
            report_danger(statements, dangers)
        return statements
    check_statements(m._guards, statements, m._dialect, warned)
    return statements


def preflight_check(
    preflight: Union[None, str, PreflightCheck],
    dialect: Dialects,
) -> Optional[PreflightCheck]:
    """
    up()'s preflight argument as a PreflightCheck, or None for no check.
    Raises ValueError for a mode other than 'warn' and 'refuse', and
    DialectError for a dialect without a live preflight, as impact()
    raises it with live=True. up() calls this before the run starts.
    """
    from sustained.exceptions import DialectError
    from sustained.impact.preflight import covered

    if preflight is None:
        return None
    check = (
        preflight
        if isinstance(preflight, PreflightCheck)
        else PreflightCheck(preflight)
    )
    if check.mode not in ("warn", "refuse"):
        raise ValueError(
            f"preflight must be None, 'warn', or 'refuse', not {check.mode!r}."
        )
    if not covered(dialect):
        raise DialectError(f"The live preflight does not cover {dialect.name}.")
    return check


def check_preflight(
    m: MigratorBase,
    statements: List["MigrationStatement"],
    check: Optional[PreflightCheck],
    shown: Set[str],
) -> Core[None]:
    """
    Reads what the statements would wait behind on the live server, for
    up(preflight=...). With `refuse`, PreflightBlocked is raised for a
    blocker, for a read the blockers come from that failed, and for a
    statement the analysis cannot read, whose locks the preflight cannot
    check. Otherwise each blocker, each transaction open the check's
    `older_than` seconds or longer, each read that failed, and each
    statement the preflight cannot check prints on stderr, once per run:
    `shown` collects the lines already printed. A dialect without a
    preflight reads nothing.
    """
    from sustained.exceptions import PreflightBlocked
    from sustained.impact.preflight import covered, preflight_plan
    from sustained.impact.report import blocker_line, transaction_line, unread_line

    if check is None or not covered(m._dialect) or not statements:
        return
    impacts = [s.impact for s in statements if s.impact is not None]
    found: "Preflight" = yield ReadCatalog(
        preflight_plan(m._dialect, impacts, check.older_than)
    )
    # A statement that reached the check without an impact is one the
    # preflight cannot check either.
    found = found._replace(
        unread=found.unread + tuple(str(s) for s in statements if s.impact is None)
    )
    if check.mode == "refuse" and not found.clear:
        raise PreflightBlocked(found)
    lines = [blocker_line(b) for b in found.blockers]
    lines.extend(transaction_line(s) for s in found.transactions)
    missing = sorted({"locks", "transactions"} - found.read)
    if missing:
        lines.append(f"could not read {' or '.join(missing)}")
    lines.extend(unread_line(statement) for statement in found.unread)
    for line in lines:
        if line not in shown:
            shown.add(line)
            print(f"preflight: {line}", file=sys.stderr)


def apply(
    m: MigratorBase,
    migration: Migration,
    seq: int,
    update: bool,
    generated: bool = False,
) -> Core[None]:
    """
    Runs one migration's up step and records it: an INSERT for a
    first run, an UPDATE in place when a repeatable re-runs, keeping
    its original seq. `generated` marks a migration the diff against
    the models produced, whose id nothing on disk carries. Its
    statements go on the tracking row, so down() can take it back.
    """
    try:
        yield from migration_scope(
            m,
            _apply_body(m, migration, seq, update, generated),
            migration.transactional,
        )
    except Exception as error:
        yield from bookkeeping.record_failure(
            m, migration, seq, update=update, generated=generated
        )
        _tag_migration(error, migration.id)
        raise


def _apply_body(
    m: MigratorBase, migration: Migration, seq: int, update: bool, generated: bool
) -> Core[None]:
    """The step and its tracking row, inside the migration's scope."""
    started = time.perf_counter()
    if m._tracer is not None:
        yield from m._tracer.run_step(migration)
    else:
        yield RunStep(migration.up)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    timestamp = datetime.now(timezone.utc).isoformat()
    checksum = migration_checksum(migration)
    steps = _stored_steps(migration, generated, m._compiler)
    # The row is written on the transaction's cursor where a block is
    # open, so a failed migration takes its row back with it on the
    # engines that roll DDL back, and on DuckDB, where every fresh cursor
    # is a session of its own.
    if update:
        yield Execute(
            m._update_sql(),
            (checksum, timestamp, elapsed_ms, True, generated, steps, migration.id),
            pinned=True,
        )
    else:
        yield Execute(
            m._insert_sql(),
            (
                migration.id,
                seq,
                checksum,
                timestamp,
                elapsed_ms,
                True,
                generated,
                steps,
            ),
            pinned=True,
        )


def revert_body(
    m: MigratorBase, step: MigrationStep, migration_id: str, pinned: bool
) -> Core[None]:
    """
    One migration's down step and the removal of its tracking row, inside
    the migration's scope. `pinned` is Execute's flag for the removal.
    """
    yield RunStep(step)
    yield Execute(
        f"DELETE FROM {m._table_sql()} WHERE "
        f"{m._compiler.quote_identifier('id')} = {m._compiler.placeholder()}",
        (migration_id,),
        pinned=pinned,
    )


def applied_versioned(
    m: MigratorBase, ids: Optional[List[str]] = None
) -> Core[List[str]]:
    """
    Applied ids with the repeatables left out; down() skips them. The
    ids are read from the tracking table unless the caller passes a
    list it has already read.
    """
    repeatable_ids = {x.id for x in m._repeatables()}
    source = (yield from bookkeeping.applied(m)) if ids is None else ids
    return [i for i in source if i not in repeatable_ids]


def down_to(m: MigratorBase, target: str, allow_changed: bool) -> Core[List[str]]:
    yield RefuseOpenTransaction("down_to")
    applied = yield from applied_versioned(m)
    if target not in applied:
        raise ValueError(f"Migration '{target}' is not applied.")
    steps = len(applied) - applied.index(target) - 1
    if not steps:
        return []
    return (yield from down(m, steps, allow_changed))


def down(m: MigratorBase, steps: int, allow_changed: bool) -> Core[List[str]]:
    _checked_steps(steps)
    yield RefuseOpenTransaction("down")
    try:
        return (yield from run_down(m, steps, allow_changed))
    except Exception as error:
        yield from fire_on_error(m, error)
        raise


def run_down(m: MigratorBase, steps: int, allow_changed: bool) -> Core[List[str]]:
    """The run itself, without the callback down() wraps it in."""
    from sustained.exceptions import MigrationError

    def locked() -> Core[List[str]]:
        yield from bookkeeping.ensure_tracking_table(m)
        records = yield from bookkeeping.read_records(m)
        failed = [_failed_attempt_problem(r.id) for r in records if not r.success]
        if failed:
            raise MigrationError(failed)
        by_record = {r.id: r for r in records}
        applied = yield from applied_versioned(m, [r.id for r in records if r.success])
        by_id = {x.id: x for x in m._migrations}
        reverted: List[str] = []
        # Every migration in the window is read and checked before the
        # first one is reverted. A refusal in the middle of the loop
        # would leave the newer migrations reverted and committed for a
        # condition that was knowable before any of them ran.
        window: List[Tuple[str, Migration, MigrationStep]] = []
        for migration_id in reversed(applied[-steps:] if steps else []):
            migration = by_id.get(migration_id)
            if migration is not None and not allow_changed:
                if _changed_since_applied(migration, by_record.get(migration_id)):
                    raise MigrationError([_changed_down_message(migration_id)])
            if migration is None:
                migration = yield from bookkeeping.generated_migration(m, migration_id)
            if migration is None:
                raise ValueError(
                    f"Applied migration '{migration_id}' is not registered "
                    "with this migrator; cannot revert."
                )
            if migration.down is None:
                raise ValueError(f"Migration '{migration_id}' has no down step.")
            window.append((migration_id, migration, migration.down))
        for migration_id, migration, down_step in window:
            try:
                yield from migration_scope(
                    m,
                    revert_body(m, down_step, migration_id, pinned=True),
                    migration.transactional,
                )
            except Exception as error:
                yield from bookkeeping.record_down_failure(m, migration)
                _tag_migration(error, migration_id)
                raise
            reverted.append(migration_id)
        return reverted

    return (yield from bookkeeping.lock_scope(m, locked()))


def plan(
    m: MigratorBase,
    models: List[Type["Model"]],
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    migration_id: Optional[str] = None,
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    type_casts: Optional[Dict[str, str]] = None,
    ignore_undeclared: bool = True,
    snapshot: Optional["Snapshot"] = None,
    assert_algorithm: bool = False,
    online: bool = False,
) -> Core[Optional[Migration]]:
    """
    The migration a diff of the models produces. The async driver's
    source is a replay of a schema read, which writes nothing and cannot
    ask whether a table holds a row, so a table it cannot read counts as
    one that holds rows there. The snapshot read for the replay is not
    passed on: the diff reads the replay itself. A caller's snapshot
    asks for no read, and the async driver's replay then answers no
    statement.

    With assert_algorithm, the server facts are read after the diff, and
    the migration's statements take the ALGORITHM and LOCK clause the
    impact rules predict from them (asserted_migration()).

    With online, the diff is plan_migrations()'s, and a split into two
    migrations raises ValueError, since plan() returns one migration.
    """
    from sustained.autogenerate import declared_schemas

    if online:
        split = yield from plan_migrations(
            m,
            models,
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            migration_id=migration_id,
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
            ignore_undeclared=ignore_undeclared,
            snapshot=snapshot,
            assert_algorithm=assert_algorithm,
            online=True,
        )
        if len(split) > 1:
            raise ValueError(
                f"plan(online=True) generated {len(split)} migrations, "
                f"{', '.join(g.id for g in split)}. Call "
                "plan_migrations(online=True) to get each of them."
            )
        return split[0] if split else None
    source: Tuple[Connection, Optional["Snapshot"]] = yield DiffSource(
        declared_schemas(models), read=snapshot is None
    )
    generated = plan_migration(
        source[0],
        models,
        m._dialect,
        m._own_tables(),
        allow_drops=allow_drops,
        ignore_changed_columns=ignore_changed_columns,
        migration_id=migration_id,
        renames=renames,
        table_renames=table_renames,
        type_casts=type_casts,
        ignore_undeclared=ignore_undeclared,
        snapshot=snapshot,
    )
    if generated is None or not assert_algorithm or m._dialect is not Dialects.MYSQL:
        return generated
    context = yield ReadContext()
    return asserted_migration(generated, m._dialect, m._compiler, context)


def plan_migrations(
    m: MigratorBase,
    models: List[Type["Model"]],
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    migration_id: Optional[str] = None,
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    type_casts: Optional[Dict[str, str]] = None,
    ignore_undeclared: bool = True,
    snapshot: Optional["Snapshot"] = None,
    assert_algorithm: bool = False,
    online: bool = False,
) -> Core[List[Migration]]:
    """
    The migrations a diff of the models produces, as plan() plans the
    one, with online passed on to autogenerate_migrations(). On MySQL,
    online asks for the clauses assert_algorithm writes, and the server
    facts are read once for every migration.
    """
    from sustained.autogenerate import declared_schemas

    source: Tuple[Connection, Optional["Snapshot"]] = yield DiffSource(
        declared_schemas(models), read=snapshot is None
    )
    generated = planning.plan_migrations(
        source[0],
        models,
        m._dialect,
        m._own_tables(),
        allow_drops=allow_drops,
        ignore_changed_columns=ignore_changed_columns,
        migration_id=migration_id,
        renames=renames,
        table_renames=table_renames,
        type_casts=type_casts,
        ignore_undeclared=ignore_undeclared,
        snapshot=snapshot,
        online=online,
    )
    asserting = assert_algorithm or online
    if not generated or not asserting or m._dialect is not Dialects.MYSQL:
        return generated
    context = yield ReadContext()
    return [asserted_migration(g, m._dialect, m._compiler, context) for g in generated]


class DiffOptions(NamedTuple):
    """
    The diff options up() takes for the migration the models generate,
    which impact() and preflight() take to analyze that same migration.
    """

    allow_drops: bool = False
    ignore_changed_columns: bool = False
    migration_id: Optional[str] = None
    renames: Optional[Dict[str, str]] = None
    table_renames: Optional[Dict[str, str]] = None
    type_casts: Optional[Dict[str, str]] = None


def impact(
    m: MigratorBase,
    models: Optional[List[Type["Model"]]] = None,
    assert_algorithm: bool = False,
    exact_counts: bool = False,
    live: bool = False,
    older_than: float = OLDER_THAN,
    online: bool = False,
    diff: DiffOptions = DiffOptions(),
) -> Core["ImpactReport"]:
    """
    The pending run, plus the migration the models generate with the
    `diff` options, analyzed with the server facts read from the
    connection, and with `live`, the preflight of the analyzed
    statements. The dialect and `older_than` are checked before anything
    is read, so a dialect without rules costs no round trip.
    """
    from sustained.exceptions import DialectError
    from sustained.impact import analyze, supported
    from sustained.impact.preflight import (
        checked_older_than,
        covered,
        preflight_plan,
    )
    from sustained.impact.rules import engine

    if not supported(m._dialect):
        raise DialectError(f"Impact analysis does not cover {engine(m._dialect)} yet.")
    if live and not covered(m._dialect):
        raise DialectError(f"The live preflight does not cover {engine(m._dialect)}.")
    if live:
        checked_older_than(older_than)
    run = yield from bookkeeping.pending(m)
    if models:
        run = run + (
            yield from plan_migrations(
                m,
                list(models),
                allow_drops=diff.allow_drops,
                ignore_changed_columns=diff.ignore_changed_columns,
                migration_id=diff.migration_id,
                renames=diff.renames,
                table_renames=diff.table_renames,
                type_casts=diff.type_casts,
                assert_algorithm=assert_algorithm,
                online=online,
            )
        )
    context = yield ReadContext(exact_counts)
    report = analyze(run_statements(run, m._compiler), m._dialect, context)
    if not live:
        return report
    found = yield ReadCatalog(preflight_plan(m._dialect, report.statements, older_than))
    return report._replace(preflight=found)


def preflight(
    m: MigratorBase,
    models: Optional[List[Type["Model"]]] = None,
    older_than: float = OLDER_THAN,
    exact_counts: bool = False,
    online: bool = False,
    diff: DiffOptions = DiffOptions(),
) -> Core["Preflight"]:
    """The preflight of the run impact() analyzes."""
    report = yield from impact(
        m,
        models,
        exact_counts=exact_counts,
        live=True,
        older_than=older_than,
        online=online,
        diff=diff,
    )
    assert report.preflight is not None
    return report.preflight


def drift(
    m: MigratorBase,
    models: List[Type["Model"]],
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    ignore_changed_columns: bool = False,
) -> Core[List[str]]:
    """
    What the models still ask for. The async driver hands over the
    snapshot it read along with the replay, so the diff reads no schema
    of its own; the blocking driver hands over none, and the diff reads
    the connection.
    """
    from sustained.autogenerate import declared_schemas

    source: Tuple[Connection, Optional["Snapshot"]] = yield DiffSource(
        declared_schemas(models)
    )
    connection, snapshot = source
    return drift_lines(
        connection,
        models,
        m._dialect,
        m._own_tables(),
        renames=renames,
        table_renames=table_renames,
        ignore_changed_columns=ignore_changed_columns,
        snapshot=snapshot,
    )
