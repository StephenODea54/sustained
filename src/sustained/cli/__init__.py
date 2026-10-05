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
  `rehearse` then uses instead of the real one, and on which `impact`
  diffs the models after the pending migrations apply (optional)
- `rehearsal_lock_timeout`: the seconds a statement of `rehearse`, or of
  the rehearsal `impact` runs on the scratch database, waits for a lock
  before the rehearsal fails, as `Migrator.rehearse(lock_timeout=...)`
  sets it (optional)
- `guards`: a list of rules over the statements a run would apply; see
  sustained.guards (optional)
- `assert_algorithm`: True to write the predicted ALGORITHM and LOCK
  clause on the statements generated from the models on MySQL and
  MariaDB, as `Migrator.plan(assert_algorithm=True)` does, in `plan`,
  `impact`, `migrate`, and `rehearse`, as the `--assert-algorithm` flag
  of those commands does (optional)
- `exact_counts`: True to count the rows of each SQLite table that
  `sqlite_stat1` has no row count for when the impact analysis reads
  the database, in `plan`, `impact`, `migrate`, and `script --annotate`,
  as the `--exact-counts` flag of those commands does (optional)
- `online`: True to generate the online form of the models' migration,
  as `Migrator.up(online=True)` does, in `plan`, `impact`, `migrate`,
  and `rehearse`, as the `--online` flag of those commands does
  (optional)
- `preflight`: 'warn' or 'refuse' to read, before `migrate` applies
  anything, the other sessions a statement of the run would wait
  behind, as `Migrator.up(preflight=...)` does, and as `migrate
  --preflight` does (optional)
- `preflight_older_than`: the age in seconds from which the preflight
  lists an open transaction, 60 by default, for `migrate` with a
  preflight and `impact --live`; only those read it (optional)
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
anyway and records the override on the database. `migrate` with
`preflight` set to 'refuse' exits 5 when another session has a lock a
statement of the run would wait for, when a read the preflight needs
failed, or when a statement is one the analysis cannot read.

`impact` prints the locks each statement of the run would take, what
they block, and the work each does; see sustained.impact. It never
gates: blocking on impact is the guards' job. `plan` lists the
statements whose impact merits a look in an `impact` section. When no
guard reads impact, `migrate` prints each `danger` finding on stderr, as
`Migrator.up()` does.
`impact --live` also prints the sessions each statement would wait
behind on the server now, and the transactions open longer than
`--older-than` seconds.
`script --annotate` prints each statement's impact above it as SQL
comments. `rehearse --trace` observes each statement on PostgreSQL,
MySQL, MariaDB, and SQL Server, and prints the impact report with what
the server did in place of the prediction.

A flag that changes nothing is an error. `--older-than` without
`--live`, and `script --exact-counts` without `--annotate`, are usage
errors, which exit 2. `--online` on a dialect other than PostgreSQL,
MySQL, and MariaDB, and `--assert-algorithm` on a dialect other than
MySQL and MariaDB, exit 1.

`status`, `validate`, `plan`, `impact`, and `rehearse` take `--json`, which prints
one JSON object instead of the plain lines. A failure prints the object
too, with every key null and `error` set to the message. The exit code
stays the same either way.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence, Tuple, Type

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
    _check_dialect_flags,
    _close_quietly,
    _load_config,
    _seconds,
)
from sustained.cli.output import (
    _JSON_KEYS,
    JsonValue,
    _print_json,
)
from sustained.cli.plan import (
    _cmd_plan,
)
from sustained.exceptions import (
    GuardBlocked,
    MigrationError,
    PreflightBlocked,
    RehearsalRequired,
)


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

    def counts_rows(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument(
            "--exact-counts",
            action="store_true",
            help="Count the rows of SQLite tables sqlite_stat1 has no count for.",
        )
        return sub

    def generates(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument(
            "--online",
            action="store_true",
            help="Generate the models' migration in its online form "
            "(PostgreSQL; on MySQL and MariaDB, as --assert-algorithm).",
        )
        sub.add_argument(
            "--assert-algorithm",
            action="store_true",
            help="Write the predicted ALGORITHM and LOCK clauses on the models' "
            "statements (MySQL, MariaDB).",
        )
        return sub

    generates(
        counts_rows(
            command(
                "plan",
                "Show the pending migrations, the problems, and the model drift.",
                machine_readable=True,
            )
        )
    )
    impact = generates(
        counts_rows(
            command(
                "impact",
                "Show the locks, blocking, and work of each statement in the run.",
                machine_readable=True,
            )
        )
    )
    impact.add_argument(
        "--live",
        action="store_true",
        help="Also show the sessions each statement would wait behind now.",
    )
    impact.add_argument(
        "--older-than",
        type=_seconds,
        metavar="SECONDS",
        help="List open transactions at least this old with --live (default: 60).",
    )

    # Ordered as they are used: plan reads, rehearse proves, migrate applies.
    rehearse = generates(
        command(
            "rehearse",
            "Run the pending migrations up and back down, then roll it all back.",
            machine_readable=True,
        )
    )
    rehearse.add_argument(
        "--trace",
        action="store_true",
        help="Observe the locks and rewrites of each statement "
        "(PostgreSQL, MySQL, MariaDB, SQL Server).",
    )

    migrate = generates(
        counts_rows(command("migrate", "Apply pending migrations in order."))
    )
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
    migrate.add_argument(
        "--preflight",
        choices=("warn", "refuse"),
        help="Read the sessions the run would wait behind before it starts.",
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

    script = counts_rows(command("script", "Print the SQL a run would execute."))
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


def _check_usage(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """
    Refuses a flag that does nothing without another, as a usage error,
    so the flag is not read as having done something.
    """
    if getattr(args, "older_than", None) is not None and not args.live:
        parser.error("--older-than needs --live")
    if args.command == "script" and args.exact_counts and not args.annotate:
        parser.error("--exact-counts on script needs --annotate")


# The exit code for each error a command raises, checked in order. The
# three subclasses of MigrationError come before it.
_EXIT_CODES: Tuple[Tuple[Type[Exception], int], ...] = (
    # A guard blocked the run, which plan reports the same way.
    (GuardBlocked, 3),
    # The run needs a rehearsal it does not have, which is a different
    # thing to do from fixing a failure.
    (RehearsalRequired, 4),
    # Another session is in the way, which running again later can fix
    # without any change.
    (PreflightBlocked, 5),
    (MigrationError, 1),
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _check_usage(parser, args)
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
        _check_dialect_flags(args, migrator.dialect)
        return _COMMANDS[args.command](migrator, args, config)
    except Exception as error:
        # A driver raises its own error class, so a failing statement would
        # otherwise reach the shell as a traceback.
        _print_applied(error)
        for kind, code in _EXIT_CODES:
            if isinstance(error, kind):
                return _fail(args, error, code)
        migration_id = getattr(error, "migration_id", None)
        where = f" in '{migration_id}'" if migration_id else ""
        return _fail(args, error, 1, where)
    finally:
        _close_quietly(connection)


__all__ = ["JsonValue", "main"]
