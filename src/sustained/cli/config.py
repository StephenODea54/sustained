"""
Loading the config module, and building the migrator and the connection
it names.
"""

from __future__ import annotations

import argparse
import importlib
import math
import os
import sys
from types import ModuleType
from typing import (
    Callable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from sustained.dialects import Dialects
from sustained.migration_files import load_migrations
from sustained.migrations import (
    CallbackResult,
    Callbacks,
    Migration,
    Migrator,
    PreflightCheck,
)
from sustained.types import Connection


def _resolve_dialect(value: object) -> Dialects:
    if value is None:
        return Dialects.DEFAULT
    if isinstance(value, Dialects):
        return value
    name = str(value).upper()
    try:
        return Dialects[name]
    except KeyError:
        names = ", ".join(d.name.lower() for d in Dialects)
        raise ValueError(f"Unknown dialect {value!r}. Choose one of: {names}.")


def _load_config(module_name: str) -> ModuleType:
    """
    Imports the config module with the working directory on sys.path.

    The entry is removed again by value, not by position. A config module
    may add its own entries to the front of sys.path while it imports, and
    removing the first entry would drop one of those and leave the working
    directory in place for the rest of the process.
    """
    cwd = os.getcwd()
    sys.path.insert(0, cwd)
    try:
        return importlib.import_module(module_name)
    finally:
        try:
            sys.path.remove(cwd)
        except ValueError:
            pass


def _assert_algorithm(config: ModuleType, args: argparse.Namespace) -> bool:
    """
    Whether the models' statements get asserted ALGORITHM and LOCK
    clauses: the command's --assert-algorithm flag, or the config
    module's assert_algorithm attribute.
    """
    return bool(
        getattr(args, "assert_algorithm", False)
        or getattr(config, "assert_algorithm", False)
    )


# The dialects each flag of the models' diff changes anything on.
_FLAG_DIALECTS = {
    "online": (
        "--online",
        frozenset({Dialects.POSTGRES, Dialects.MYSQL}),
        "PostgreSQL, MySQL, and MariaDB",
    ),
    "assert_algorithm": (
        "--assert-algorithm",
        frozenset({Dialects.MYSQL}),
        "MySQL and MariaDB",
    ),
}


def _check_dialect_flags(args: argparse.Namespace, dialect: Dialects) -> None:
    """
    Raises ValueError for a flag the command was given that changes
    nothing on the dialect, such as --online on SQLite, so the flag is
    not read as having done something.
    """
    from sustained.impact.rules import engine

    for attribute, (flag, dialects, names) in _FLAG_DIALECTS.items():
        if getattr(args, attribute, False) and dialect not in dialects:
            raise ValueError(
                f"{flag} changes the migration the models generate on {names} "
                f"only, not {engine(dialect)}."
            )


def _exact_counts(config: ModuleType, args: argparse.Namespace) -> bool:
    """
    Whether the SQLite context read counts rows: the command's
    --exact-counts flag, or the config module's exact_counts attribute.
    """
    return bool(
        getattr(args, "exact_counts", False) or getattr(config, "exact_counts", False)
    )


def _online(config: ModuleType, args: argparse.Namespace) -> bool:
    """
    Whether the models generate the online form of their migration: the
    command's --online flag, or the config module's online attribute.
    """
    return bool(getattr(args, "online", False) or getattr(config, "online", False))


def _preflight(
    config: ModuleType, args: argparse.Namespace
) -> Optional[PreflightCheck]:
    """
    The preflight check for `migrate`: the mode from the --preflight
    flag or the config module's preflight attribute, with the age from
    _older_than(), or None without a mode.
    """
    mode = getattr(args, "preflight", None) or getattr(config, "preflight", None)
    if mode is None:
        return None
    return PreflightCheck(str(mode), _older_than(config, args))


def _older_than(config: ModuleType, args: argparse.Namespace) -> float:
    """
    The age from which the preflight lists an open transaction: the
    --older-than flag, or the config module's preflight_older_than
    attribute, or 60 seconds. Raises ValueError, naming the attribute,
    for a value that is not a number of seconds, 0 or more.
    """
    flag = getattr(args, "older_than", None)
    if flag is not None:
        return float(flag)
    value = getattr(config, "preflight_older_than", 60.0)
    seconds = _as_seconds(value)
    if seconds is None:
        raise ValueError(
            f"preflight_older_than must be a number of seconds, 0 or more, "
            f"not {value!r}."
        )
    return seconds


def _as_seconds(value: object) -> Optional[float]:
    """A number of seconds, 0 or more, as a float, or None for anything else."""
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(seconds) or seconds < 0:
        return None
    return seconds


def _seconds(value: str) -> float:
    """
    Reads an --older-than value and refuses anything that is not a
    number of seconds, 0 or more, such as `nan` or a negative age.
    """
    seconds = _as_seconds(value)
    if seconds is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a number of seconds, 0 or more."
        )
    return seconds


def _close_quietly(connection: object) -> None:
    if hasattr(connection, "close"):
        try:
            connection.close()
        except Exception:
            pass


def _rehearsal_lock_timeout(config: ModuleType) -> Optional[float]:
    """
    The config module's rehearsal_lock_timeout, in seconds, or None when
    it sets none. rehearse() checks the value.
    """
    return getattr(config, "rehearsal_lock_timeout", None)


def _migrator_on(
    connection: Connection, config: ModuleType, extra: Sequence[Migration] = ()
) -> Migrator:
    """
    Builds a migrator for the config module on the given connection.
    `extra` joins the registered migrations after the config's own.
    """
    migrations: List[Migration] = list(getattr(config, "migrations", []))
    directory = getattr(config, "migrations_dir", None)
    if directory is not None:
        migrations.extend(
            load_migrations(
                directory, placeholders=getattr(config, "placeholders", None)
            )
        )
    migrations.extend(extra)
    return Migrator(
        connection,
        migrations,
        table=getattr(config, "table", "sustained_migrations"),
        rehearsal_table=getattr(config, "rehearsal_table", "sustained_rehearsals"),
        dialect=_resolve_dialect(getattr(config, "dialect", None)),
        tracking_table_options=getattr(config, "tracking_table_options", None),
        guards=list(getattr(config, "guards", None) or []),
        callbacks=_config_callbacks(config),
    )


def _callback(config: ModuleType, name: str) -> Optional[Callable[..., CallbackResult]]:
    """The named callback from the config module, or None when it has none."""
    hook = getattr(config, name, None)
    return hook if callable(hook) else None


def _config_callbacks(config: ModuleType) -> Callbacks:
    """
    The config module's callbacks, as the Callbacks the migrator takes. The
    module is how the CLI gathers them; the migrator is what calls them.
    """
    return Callbacks(
        before_migrate=_callback(config, "before_migrate"),
        after_migrate=_callback(config, "after_migrate"),
        on_error=_callback(config, "on_error"),
    )


def _build_migrator(config: ModuleType) -> Tuple[Migrator, Connection]:
    if hasattr(config, "connection"):
        connection = config.connection
    elif hasattr(config, "get_connection"):
        connection = config.get_connection()
    else:
        raise ValueError(
            "The config module must define 'connection' or 'get_connection()'."
        )
    try:
        migrator = _migrator_on(connection, config)
    except Exception:
        # The connection never reaches the caller on a setup failure, so it
        # must close here.
        _close_quietly(connection)
        raise
    return migrator, connection
