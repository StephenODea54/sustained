"""
Command-line migration runner.

`sustained <command> --config <module>` drives a Migrator from the shell.
The config module names the pieces the migrator needs:

- `connection` (a DB-API connection) or `get_connection()` returning one
- `migrations`: a list of Migration objects, and/or `migrations_dir`: a
  directory of `<id>.up.sql` / `<id>.down.sql` / `<id>.repeat.sql` files
  loaded after the list
- `placeholders`: a dict filling `${key}` markers in the SQL files
  (optional)
- `models`: a list of Model classes, which lets `plan` report drift
  between the models and the database (optional)
- `dialect`: a Dialects member or its name, such as 'postgres' (optional)
- `table`: the tracking table name (optional)
- `rehearsal_table`: the rehearsal table name (optional)
- `tracking_table_options`: TableOptions for the tracking table (optional)
- `get_rehearsal_connection()`: a connection to a scratch database, which
  `rehearse` then uses instead of the real one (optional)
- `guards`: a list of rules over the statements a run would apply; see
  sustained.guards (optional)
- `before_migrate(connection)`, `after_migrate(connection, applied)`, and
  `on_error(connection, migration_id, error)`: callbacks around the
  `migrate` command; `on_error` also runs when `down` fails (optional)

Commands: status, plan, impact, migrate, rehearse, down, validate,
repair, script, baseline. Every command exits 0 on success and 1 on
failure.
`plan` exits 2 when work is waiting. `plan` and `migrate` exit 3 when a
guard blocked a statement. A run with problems exits 1 even when a guard
also blocked: a plan that cannot be trusted outranks the rest.

`migrate` refuses to apply statements that remove data until a passing
rehearsal has covered them, and exits 4. `--unrehearsed` applies them
anyway and records the override on the database.

`impact` prints the locks each statement of the run would take, what
they block, and the work each does; see sustained.impact. It never
gates: blocking on impact is the guards' job. `plan` lists the
statements whose impact merits a look in an `impact` section. When no
guard reads impact, `migrate` prints each `danger` finding on stderr, as
`Migrator.up()` does.
`script --annotate` prints each statement's impact above it as SQL
comments. `rehearse --trace` observes each statement on Postgres and prints the
impact report with what the server did in place of the prediction.

`status`, `validate`, `plan`, `impact`, and `rehearse` take `--json`, which prints
one JSON object instead of the plain lines. A failure prints the object
too, with every key null and `error` set to the message. The exit code
stays the same either way.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from sustained.cli.commands import (
    _cmd_baseline,
    _cmd_down,
    _cmd_impact,
    _cmd_migrate,
    _cmd_rehearse,
    _cmd_repair,
    _cmd_script,
    _cmd_status,
    _cmd_validate,
    _step_count,
)
from sustained.cli.config import (
    _build_migrator,
    _close_quietly,
    _load_config,
)
from sustained.cli.output import (
    _JSON_KEYS,
    JsonValue,
    _print_json,
)
from sustained.cli.plan import (
    _cmd_plan,
)
from sustained.exceptions import GuardBlocked, MigrationError, RehearsalRequired


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sustained", description="Run sustained schema migrations."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(
        name: str, help_text: str, machine_readable: bool = False
    ) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--config",
            default="sustained_config",
            help="Config module to import (default: sustained_config).",
        )
        if machine_readable:
            sub.add_argument(
                "--json",
                action="store_true",
                help="Print one JSON object instead of the plain lines.",
            )
        return sub

    command(
        "status",
        "Show every migration's state: applied, pending, or changed.",
        machine_readable=True,
    )
    command(
        "plan",
        "Show the pending migrations, the problems, and the model drift.",
        machine_readable=True,
    )
    command(
        "impact",
        "Show the locks, blocking, and work of each statement in the run.",
        machine_readable=True,
    )

    # Ordered as they are used: plan reads, rehearse proves, migrate applies.
    rehearse = command(
        "rehearse",
        "Run the pending migrations up and back down, then roll it all back.",
        machine_readable=True,
    )
    rehearse.add_argument(
        "--trace",
        action="store_true",
        help="Observe the locks and rewrites of each statement (Postgres).",
    )

    migrate = command("migrate", "Apply pending migrations in order.")
    migrate.add_argument("--target", help="Stop after this migration id.")
    migrate.add_argument(
        "--no-validate", action="store_true", help="Skip validation before the run."
    )
    migrate.add_argument(
        "--allow-out-of-order",
        action="store_true",
        help="Accept a pending migration ordered before an applied one.",
    )
    migrate.add_argument(
        "--unrehearsed",
        action="store_true",
        help="Apply statements that remove data without a passing rehearsal.",
    )

    down = command("down", "Revert applied migrations, newest first.")
    group = down.add_mutually_exclusive_group()
    group.add_argument(
        "--steps",
        type=_step_count,
        default=1,
        help="How many migrations to revert (0 or more).",
    )
    group.add_argument("--to", help="Revert until this id is the newest applied.")
    down.add_argument(
        "--allow-changed",
        action="store_true",
        help="Revert a migration that was edited after it was applied.",
    )

    command(
        "validate",
        "Check the tracking table against the migrations.",
        machine_readable=True,
    )
    command("repair", "Fix tracking rows after failures or intentional edits.")

    script = command("script", "Print the SQL a run would execute.")
    script.add_argument("direction", nargs="?", choices=("up", "down"), default="up")
    script.add_argument(
        "--annotate",
        action="store_true",
        help="Print each statement's impact above it as -- impact: comments.",
    )

    baseline = command("baseline", "Record migrations as applied without running them.")
    baseline.add_argument("target", help="Record up to and including this id.")

    return parser


_COMMANDS = {
    "status": _cmd_status,
    "plan": _cmd_plan,
    "impact": _cmd_impact,
    "migrate": _cmd_migrate,
    "down": _cmd_down,
    "rehearse": _cmd_rehearse,
    "validate": _cmd_validate,
    "repair": _cmd_repair,
    "script": _cmd_script,
    "baseline": _cmd_baseline,
}


def _print_applied(error: BaseException) -> None:
    """
    Names the migrations that were already applied when a run stopped.
    They stay applied and committed, so the operator needs to know what
    the run left behind before fixing the failure.
    """
    for migration_id in getattr(error, "applied", None) or []:
        print(f"applied  {migration_id}")


def _fail(
    args: argparse.Namespace, error: BaseException, code: int, where: str = ""
) -> int:
    """
    Reports a failure on stderr and returns the exit code. Under --json,
    stdout still gets its one object: every key null, since nothing was
    evaluated, and `error` set to the message.
    """
    print(f"error{where}: {error}", file=sys.stderr)
    if getattr(args, "json", False):
        _print_json({key: None for key in _JSON_KEYS[args.command]}, str(error))
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
        migrator, connection = _build_migrator(config)
    except Exception as error:
        # A config module that will not import, a connection that will not
        # open, a migrations directory that will not load: none of them
        # leave a connection behind, and none should reach the shell as a
        # traceback.
        return _fail(args, error, 1)
    try:
        return _COMMANDS[args.command](migrator, args, config)
    except GuardBlocked as error:
        # Exit 3 says a guard blocked the run, which plan reports the same
        # way.
        _print_applied(error)
        return _fail(args, error, 3)
    except RehearsalRequired as error:
        # Exit 4 says the run needs a rehearsal it does not have, which is
        # a different thing to do from fixing a failure.
        _print_applied(error)
        return _fail(args, error, 4)
    except MigrationError as error:
        _print_applied(error)
        return _fail(args, error, 1)
    except Exception as error:
        # A driver raises its own error class, so a failing statement would
        # otherwise reach the shell as a traceback.
        _print_applied(error)
        migration_id = getattr(error, "migration_id", None)
        where = f" in '{migration_id}'" if migration_id else ""
        return _fail(args, error, 1, where)
    finally:
        _close_quietly(connection)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["JsonValue", "main"]
