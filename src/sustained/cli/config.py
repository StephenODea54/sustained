"""
Loading the config module, and building the migrator and the connection
it names.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from types import ModuleType
from typing import (
    Callable,
    List,
    Optional,
    Tuple,
)

from sustained.dialects import Dialects
from sustained.migration_files import load_migrations
from sustained.migrations import (
    CallbackResult,
    Callbacks,
    Migration,
    Migrator,
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


def _assert_algorithm(config: ModuleType) -> bool:
    """Whether the config module asks for asserted ALGORITHM and LOCK clauses."""
    return bool(getattr(config, "assert_algorithm", False))


def _exact_counts(config: ModuleType, args: argparse.Namespace) -> bool:
    """
    Whether the SQLite context read counts rows: the command's
    --exact-counts flag, or the config module's exact_counts attribute.
    """
    return bool(
        getattr(args, "exact_counts", False) or getattr(config, "exact_counts", False)
    )


def _close_quietly(connection: object) -> None:
    if hasattr(connection, "close"):
        try:
            connection.close()
        except Exception:
            pass


def _migrator_on(connection: Connection, config: ModuleType) -> Migrator:
    """Builds a migrator for the config module on the given connection."""
    migrations: List[Migration] = list(getattr(config, "migrations", []))
    directory = getattr(config, "migrations_dir", None)
    if directory is not None:
        migrations.extend(
            load_migrations(
                directory, placeholders=getattr(config, "placeholders", None)
            )
        )
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
