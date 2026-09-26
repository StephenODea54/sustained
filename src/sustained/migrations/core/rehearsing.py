"""
A rehearsal: every pending migration up and back down inside one
transaction that rolls back, the schema read before and after, and the
row that records what the run proved.

Each function here is the body of a migrator method, or a helper of one,
written once for both migrators. What rehearse() promises is on its
docstring in Migrator; the notes here are about how.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Type, cast

from sustained.migrations.core import bookkeeping, runs
from sustained.migrations.core.base import MigratorBase
from sustained.migrations.core.requests import (
    BeginPinned,
    Core,
    Execute,
    PinnedTransaction,
    ReadSchema,
    RefuseRehearsal,
    Session,
    rollback_quietly,
    run_in,
)
from sustained.migrations.core.tracing import Tracer, check_traceable
from sustained.migrations.migration import AppliedRecord, Migration, MigrationStep
from sustained.migrations.planning import rehearsed_form
from sustained.migrations.rehearsal import (
    REHEARSAL_FAILED,
    Rehearsal,
    RehearsalResult,
    _check_rehearsable,
    _down_sweep,
    _passed_rehearsal_keys,
    _rehearsal_results,
    _reversal_provable,
    _skipped_results,
    rehearsal_failed,
    rehearsal_key,
)
from sustained.migrations.tracking import _next_seq

if TYPE_CHECKING:
    from sustained.autogenerate import IntrospectedTable
    from sustained.impact import ImpactReport
    from sustained.introspect import Snapshot
    from sustained.model import Model

# What each migration's down step proved: down_ok and the error, if any.
Outcomes = Dict[str, Tuple[Optional[bool], Optional[str]]]


def rehearse(
    m: MigratorBase,
    scratch: bool,
    models: Optional[List[Type["Model"]]],
    allow_drops: bool,
    ignore_changed_columns: bool,
    migration_id: Optional[str],
    renames: Optional[Dict[str, str]],
    table_renames: Optional[Dict[str, str]],
    type_casts: Optional[Dict[str, str]],
    trace: bool = False,
    assert_algorithm: bool = False,
    online: bool = False,
) -> Core[Rehearsal]:
    if not scratch:
        _check_rehearsable(m._dialect)
    if trace:
        check_traceable(m)
    yield RefuseRehearsal()

    def locked() -> Core[Rehearsal]:
        yield from bookkeeping.validate(m)
        pending = yield from bookkeeping.pending(m)
        record_list = yield from bookkeeping.applied_records(m)
        if not pending and models is None:
            return Rehearsal([], rehearsal_key(record_list, []))
        if models is not None:
            yield from bookkeeping.ensure_tracking_table(m)
        before = yield from snapshot(m)
        # Close whatever transaction the reads above opened, so the
        # rehearsal's BEGIN starts a fresh one instead of warning.
        yield from rollback_quietly()
        # The rehearsal's statements share this transaction, so on an
        # engine that gives every cursor its own session they land in the
        # transaction the rollback takes back. The migrator is registered
        # as inside a transaction, so a callable step that runs a query
        # skips its commit and a nested transaction block takes a
        # savepoint.
        pinned: Tuple[
            List[RehearsalResult], List[Migration], Optional["ImpactReport"]
        ] = yield from run_in(
            PinnedTransaction,
            _rehearse_pinned(
                m,
                pending,
                record_list,
                before,
                models,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                migration_id=migration_id,
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
                trace=trace,
                assert_algorithm=assert_algorithm,
                online=online,
            ),
        )
        results, drifts, report = pinned
        # What the row covers: the pending set, plus the generated
        # migrations when the diff produced any.
        attempted = list(pending) + drifts
        # The rehearsal row is written after the rollback, in its own
        # committed transaction, and still inside the lock: everything
        # the rehearsal itself wrote has just been taken back.
        key = rehearsal_key(record_list, attempted)
        passed = not any(rehearsal_failed(r) for r in results)
        recorded = False
        if not scratch:
            if passed:
                yield from bookkeeping.record_rehearsals(
                    m,
                    _passed_rehearsal_keys(
                        record_list, pending, key, bool(drifts), m._compiler
                    ),
                )
            else:
                yield from bookkeeping.record_rehearsals(m, [key], REHEARSAL_FAILED)
            recorded = True
        return Rehearsal(results, key, recorded, report)

    # The lock sits outside the rehearsal transaction, so the rollback
    # runs before the lock is released. The state reads sit inside it,
    # so a concurrent migrator cannot apply between the read and the
    # rehearsal. The session keeps BEGIN, the rehearsed work and the
    # rollback on one database session: DbApiAsyncAdapter over DuckDB
    # would otherwise open a session per statement, and the work would
    # commit.
    return (yield from bookkeeping.lock_scope(m, run_in(Session, locked())))


def _rehearse_pinned(
    m: MigratorBase,
    pending: List[Migration],
    record_list: List[AppliedRecord],
    before: Optional[Dict[str, "IntrospectedTable"]],
    models: Optional[List[Type["Model"]]],
    allow_drops: bool,
    ignore_changed_columns: bool,
    migration_id: Optional[str],
    renames: Optional[Dict[str, str]],
    table_renames: Optional[Dict[str, str]],
    type_casts: Optional[Dict[str, str]],
    trace: bool,
    assert_algorithm: bool = False,
    online: bool = False,
) -> Core[Tuple[List[RehearsalResult], List[Migration], Optional["ImpactReport"]]]:
    """
    The run inside the rehearsal transaction, which it takes back itself
    at the end whatever happened. Returns the results, the migrations the
    diff against the models generated, and with `trace` the run's impact
    as the server showed it.
    """
    records = {r.id: r for r in record_list}
    seq = _next_seq(record_list)
    m._rehearsing = True
    try:
        yield BeginPinned()
        tracer: Optional[Tracer] = None
        if trace:
            tracer = Tracer(m)
            yield from tracer.start()
            m._tracer = tracer
        ran: List[Migration] = []
        skipped: List[Migration] = []
        # Every migration the up sweep reached, in run order, which the
        # traced report covers.
        reached: List[Migration] = []
        up_error: Optional[Tuple[str, str]] = None

        def apply_each(group: List[Migration]) -> Core[None]:
            nonlocal seq, up_error
            for migration in group:
                reached.append(migration)
                if not migration.transactional:
                    # The rehearsal runs inside one transaction,
                    # which this migration's statements refuse or
                    # ignore. It is reported as unproved rather
                    # than run and failed.
                    skipped.append(migration)
                    continue
                try:
                    yield from runs.apply(
                        m, migration, seq, update=migration.id in records
                    )
                except Exception as error:
                    up_error = (migration.id, str(error))
                    return
                seq += 1
                ran.append(migration)

        # The order matches up(): the versioned migrations, then
        # the generated one, then the repeatables, which may read
        # objects the generated migration creates.
        yield from apply_each([x for x in pending if not x.repeatable])
        landed: Dict[str, List[str]] = {}
        drifts: List[Migration] = []
        if models is not None and up_error is None:
            # The diff is taken here, inside the rehearsal, so it
            # sees the schema the pending migrations just left. The
            # generated migrations join the run without being
            # registered: nothing outside the rehearsal should see a
            # migration the rollback is about to take back.
            drifts = yield from runs.plan_migrations(
                m,
                models,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                migration_id=migration_id,
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
                assert_algorithm=assert_algorithm,
                online=online,
            )
            applied = 0
            for drift in drifts:
                reached.append(drift)
                # A generated migration without a transaction runs in
                # the form rehearsed_form() gives. A generated SQLite
                # rebuild has none: its pragmas are ignored inside the
                # rehearsal transaction, so it is reported as unproved.
                form = (
                    drift if drift.transactional else rehearsed_form(drift, m._dialect)
                )
                if form is None:
                    skipped.append(drift)
                    continue
                try:
                    yield from _apply_generated(m, form, seq, traced=form is drift)
                except Exception as error:
                    up_error = (drift.id, str(error))
                    break
                seq += 1
                ran.append(form)
                applied += 1
            if drifts and applied == len(drifts):
                # The renames have already run, so the schema holds the
                # new names. Passing the hints again would ask to rename
                # objects that are gone.
                landed[drifts[-1].id] = yield from runs.drift(
                    m,
                    models,
                    ignore_changed_columns=ignore_changed_columns,
                )
        if up_error is None:
            yield from apply_each([x for x in pending if x.repeatable])
        m._tracer = None
        report = tracer.report(reached) if tracer is not None else None
        outcomes = {} if up_error else (yield from rehearse_down(m, ran))
        reverted = None
        if before is not None and _reversal_provable(ran, outcomes):
            from sustained.autogenerate import diff_snapshots

            after = yield from snapshot(m)
            if after is not None:
                reverted = diff_snapshots(before, after)
        results = _rehearsal_results(
            ran, up_error, outcomes, landed, reverted
        ) + _skipped_results(skipped)
        return results, drifts, report
    finally:
        m._rehearsing = False
        m._tracer = None
        yield from roll_back_rehearsal(m)


def _apply_generated(
    m: MigratorBase, migration: Migration, seq: int, traced: bool
) -> Core[None]:
    """
    Applies a generated migration in the rehearsal. A form other than
    the migration itself runs untraced: its statements are not the ones
    the report predicts, so an observation of them would read as a
    mismatch.
    """
    tracer = m._tracer
    if not traced:
        m._tracer = None
    try:
        yield from runs.apply(m, migration, seq, update=False, generated=True)
    finally:
        m._tracer = tracer


def snapshot(m: MigratorBase) -> Core[Optional[Dict[str, "IntrospectedTable"]]]:
    """
    The live schema, without Sustained's own tables, or None when the
    database will not report it. A rehearsal compares two of these,
    and the tracking and rehearsal tables are created by the rehearsal
    itself, so leaving them in would report them as objects left
    behind.

    A read that raises leaves the rehearsal's other proofs standing
    and reports the comparison as not checked, which is what a
    scratch database on an engine Sustained cannot introspect gives.
    """
    try:
        schema: "Snapshot" = yield ReadSchema()
    except Exception:
        return None
    for name in m._own_tables():
        schema.pop(name.lower(), None)
    return dict(schema)


def roll_back_rehearsal(m: MigratorBase) -> Core[None]:
    """
    Takes back everything the rehearsal did. The statement runs first,
    on the rehearsal's own cursor, because a driver's own rollback()
    does nothing on connections that never opened a transaction of their
    own, and asyncpg runs in autocommit until a transaction is opened;
    the driver call follows to leave its bookkeeping straight.
    """
    statement = m._compiler.rollback_transaction_sql()
    if statement is not None:
        try:
            yield Execute(statement, pinned=True)
        except Exception:
            pass
    yield from rollback_quietly()


def rehearse_down(m: MigratorBase, ran: List[Migration]) -> Core[Outcomes]:
    """
    Runs the down steps of a rehearsal, newest-first, and reports what
    each one proved. A step that raises stops the sweep; the
    migrations under it report that they were not reached.
    """
    outcomes: Outcomes = {}
    failed: Optional[str] = None
    for migration, reason in _down_sweep(ran):
        if failed is not None:
            outcomes[migration.id] = (
                None,
                f"down not reached: '{failed}' down failed",
            )
        elif reason is not None:
            outcomes[migration.id] = (None, reason)
        else:
            try:
                yield from runs.migration_scope(
                    m,
                    runs.revert_body(
                        m,
                        cast(MigrationStep, migration.down),
                        migration.id,
                        pinned=False,
                    ),
                    migration.transactional,
                )
            except Exception as error:
                outcomes[migration.id] = (False, str(error))
                failed = migration.id
            else:
                outcomes[migration.id] = (True, None)
    return outcomes
