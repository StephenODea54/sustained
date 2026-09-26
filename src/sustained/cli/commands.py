"""
The commands other than `plan`: status, impact, rehearse, migrate, down,
validate, repair, script, and baseline.
"""

from __future__ import annotations

import argparse
from types import ModuleType
from typing import (
    Optional,
)

from sustained.cli.config import (
    _assert_algorithm,
    _close_quietly,
    _exact_counts,
    _migrator_on,
    _older_than,
    _preflight,
)
from sustained.cli.output import (
    _print_json,
)
from sustained.impact.report import (
    render,
    report_data,
)
from sustained.migrations import (
    Migrator,
    Rehearsal,
    RehearsalResult,
)


def _cmd_status(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    states = migrator.statuses()
    if args.json:
        _print_json(
            {
                "migrations": [
                    {"id": migration_id, "state": state}
                    for migration_id, state in states
                ]
            }
        )
        return 0
    for migration_id, state in states:
        print(f"{state:8} {migration_id}")
    return 0


def _cmd_impact(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    models = list(getattr(config, "models", None) or []) or None
    report = migrator.impact(
        models,
        assert_algorithm=_assert_algorithm(config),
        exact_counts=_exact_counts(config, args),
        live=args.live,
        older_than=_older_than(config, args),
    )
    if args.json:
        _print_json(report_data(report))
    else:
        print(render(report))
    return 0


def _rehearsal_line(result: RehearsalResult, width: int) -> str:
    """
    One migration's line in the rehearsal report. The words after the id
    are what the rehearsal proved, in the order it proved them: the up
    step ran, the models landed, the down step ran, the schema came back.
    """
    if result.up_ok is None:
        return f"skipped   {result.id:<{width}}  {result.error}"
    if not result.up_ok:
        return f"failed    {result.id:<{width}}  up: {result.error}"
    proofs = ["up ok"]
    if result.landed is not None:
        proofs.append("landed" if not result.landed else "not landed")
    if result.down_ok:
        proofs.append("down ok")
    elif result.down_ok is False:
        proofs.append(f"down failed: {result.error}")
    else:
        proofs.append(str(result.error))
    if result.reversed is not None:
        proofs.append("reversed" if not result.reversed else "not reversed")
    return f"rehearsed {result.id:<{width}}  {', '.join(proofs)}"


def _report_rehearsal(results: Rehearsal, scratch: bool, note: Optional[str]) -> int:
    """
    Prints the rehearsal and returns the exit code: 1 when any step
    failed, when the models did not land, or when the schema did not come
    back, 0 otherwise. A migration whose down step could not be proved is
    not a failure; the line says so and the run still passes. A traced
    rehearsal prints its impact report after the results; a mismatch in
    it does not change the exit code.
    """
    if not results:
        print("Nothing to rehearse.")
        return 0
    width = max(len(r.id) for r in results)
    for result in results:
        print(_rehearsal_line(result, width))
        for gap in result.landed or []:
            print(f"    outstanding  {gap}")
        for leftover in result.reversed or []:
            print(f"    leftover     {leftover}")
    if results.impact is not None:
        print()
        print(render(results.impact))
        print()
    if scratch:
        print("rehearsal complete on the scratch database")
    else:
        print("rollback complete, database unchanged")
    if not results.ok:
        print("run: sustained plan")
        return 1
    if note is not None:
        print(note)
    return 0


def _rehearsal_json(
    results: Rehearsal, scratch: bool, recorded: bool, key: str
) -> None:
    """
    Prints the rehearsal as one JSON object. `landed` and `reversed` are
    null when the check did not run, an empty list when it passed, and
    the lines naming the trouble when it failed. `key` names the content
    the run covered, and `recorded` says whether the row reached the
    database migrate will read. `impact` is the traced impact report,
    or null without --trace.
    """
    _print_json(
        {
            "rehearsed": [
                {
                    "id": result.id,
                    "up_ok": result.up_ok,
                    "down_ok": result.down_ok,
                    "error": result.error,
                    "landed": result.landed,
                    "reversed": result.reversed,
                }
                for result in results
            ],
            "scratch": scratch,
            "key": key,
            "recorded": recorded,
            "ok": results.ok,
            "impact": (
                report_data(results.impact) if results.impact is not None else None
            ),
        }
    )


def _cmd_rehearse(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    models = list(getattr(config, "models", None) or []) or None
    factory = getattr(config, "get_rehearsal_connection", None)
    scratch = factory is not None
    note: Optional[str] = None
    if factory is None:
        results = migrator.rehearse(
            models=models,
            trace=args.trace,
            assert_algorithm=_assert_algorithm(config),
        )
        key, recorded = results.key, results.recorded
        if recorded and results.ok:
            note = "rehearsal row recorded"
    else:
        connection = factory()
        try:
            results = _migrator_on(connection, config).rehearse(
                scratch=True,
                models=models,
                trace=args.trace,
                assert_algorithm=_assert_algorithm(config),
            )
        finally:
            _close_quietly(connection)
        key, recorded = results.key, False
        if results.ok:
            recorded_key = migrator.record_scratch_rehearsal(results)
            if recorded_key is not None:
                key, recorded = recorded_key, True
                note = "rehearsal row recorded"
            elif migrator.pending():
                note = (
                    "rehearsal row not recorded: the scratch run did not cover "
                    "every pending migration"
                )
    if args.json:
        _rehearsal_json(results, scratch, recorded, key)
        return 0 if results.ok else 1
    return _report_rehearsal(results, scratch, note)


def _cmd_migrate(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    models = list(getattr(config, "models", None) or []) or None
    if args.target is not None:
        # A generated migration always runs last, so a targeted run
        # applies the registered migrations only.
        models = None
    applied = migrator.up(
        target=args.target,
        validate=not args.no_validate,
        allow_out_of_order=args.allow_out_of_order,
        models=models,
        unrehearsed=args.unrehearsed,
        assert_algorithm=_assert_algorithm(config),
        exact_counts=_exact_counts(config, args),
        preflight=_preflight(config, args),
    )
    if not applied:
        print("Nothing to apply.")
    for migration_id in applied:
        print(f"applied  {migration_id}")
    if models is not None:
        # Report only: the run has already happened, and a gap here is
        # something for the operator to look at, not a failure to raise.
        gaps = migrator.drift(models)
        for gap in gaps:
            print(f"drift    {gap}")
        if not gaps:
            print("schema matches the models")
    return 0


def _cmd_down(migrator: Migrator, args: argparse.Namespace, config: ModuleType) -> int:
    if args.to is not None:
        reverted = migrator.down_to(args.to, allow_changed=args.allow_changed)
    else:
        reverted = migrator.down(steps=args.steps, allow_changed=args.allow_changed)
    if not reverted:
        print("Nothing to revert.")
    for migration_id in reverted:
        print(f"reverted {migration_id}")
    return 0


def _cmd_validate(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    problems = migrator.validate(raise_on_problems=False)
    if args.json:
        _print_json({"ok": not problems, "problems": problems})
        return 1 if problems else 0
    if not problems:
        print("OK")
        return 0
    for problem in problems:
        print(f"problem  {problem}")
    return 1


def _cmd_repair(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    actions = migrator.repair()
    if not actions:
        print("Nothing to repair.")
    for action in actions:
        print(f"repaired {action}")
    return 0


def _cmd_script(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    print(
        migrator.script(
            args.direction,
            annotate=args.annotate,
            exact_counts=_exact_counts(config, args),
        )
    )
    return 0


def _cmd_baseline(
    migrator: Migrator, args: argparse.Namespace, config: ModuleType
) -> int:
    recorded = migrator.baseline(args.target)
    if not recorded:
        print("Nothing to baseline.")
    for migration_id in recorded:
        print(f"baselined {migration_id}")
    return 0


def _step_count(value: str) -> int:
    """
    Reads a --steps value and refuses anything below 0. A negative count
    asks the migrator to revert a slice from the front of the applied
    list, so the command stops at the command line instead. A count of 0
    reverts nothing.
    """
    try:
        steps = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number.")
    if steps < 0:
        raise argparse.ArgumentTypeError("--steps must be 0 or more.")
    return steps
