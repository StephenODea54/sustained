"""
Explicit, ordered schema migrations.

A Migration pairs an id with an up step and an optional down step. Steps
are a SQL string, a list of SQL strings, or a callable that receives the
connection. The Migrator applies pending migrations in order, records each
applied id in a tracking table, and reverts through the down steps.

The tracking table stores one row per applied migration: the id, a
monotonic sequence number, a SHA-256 checksum of the up step, the apply
timestamp, the execution time in milliseconds, and a success flag.
Tracking tables written by earlier versions of Sustained, which held only
the id and the timestamp, are upgraded in place on first use.

A second table holds rehearsal rows: one row per set of statements a
rehearsal proved, keyed to the applied history it started from. A run that
would remove data reads that table first and stops when nothing covers it.

Migrations are written by hand, generated from a model with
create_table_migration(), or produced by schema diffing through
sustained.autogenerate and Migrator.up(models=[...]).

The package splits the work across modules: the migration model
(`migration`), rehearsals (`rehearsal`), the tracking table (`tracking`),
the checks around a run (`checks`), recorded schema reads (`replay`),
offline planning (`planning`), the runs Migrator and AsyncMigrator share,
written once as generators (`core`), and the Migrator itself (`migrator`).
The public names import from here. A private helper imports from the
module that defines it.
"""

from __future__ import annotations

import warnings
from typing import Any

from sustained.migrations.checks import (
    check_guards,
    check_statements,
    report_danger,
    run_statements,
)
from sustained.migrations.migration import (
    AppliedRecord,
    CallbackResult,
    Callbacks,
    CallbackTarget,
    Migration,
    MigrationStep,
    PreflightCheck,
    checked_unique_ids,
    create_table_migration,
    migration_checksum,
    migration_sql,
)
from sustained.migrations.migrator import Migrator
from sustained.migrations.planning import (
    drift_lines,
    generated_id,
    plan_migration,
    render_script,
)
from sustained.migrations.rehearsal import (
    NOT_REHEARSABLE,
    REHEARSAL_FAILED,
    REHEARSAL_OVERRIDE,
    REHEARSAL_PASSED,
    Digest,
    Rehearsal,
    RehearsalResult,
    rehearsal_failed,
    rehearsal_key,
)
from sustained.migrations.replay import (
    SchemaRead,
)
from sustained.migrations.tracking import (
    insert_sql,
    quoted_columns,
    records_from_rows,
    records_select,
    update_sql,
)

__all__ = [
    # from migration
    "CallbackTarget",
    "CallbackResult",
    "MigrationStep",
    "Callbacks",
    "PreflightCheck",
    "Migration",
    "migration_checksum",
    "AppliedRecord",
    "checked_unique_ids",
    "migration_sql",
    "create_table_migration",
    # from rehearsal
    "RehearsalResult",
    "rehearsal_failed",
    "Rehearsal",
    "REHEARSAL_PASSED",
    "REHEARSAL_FAILED",
    "REHEARSAL_OVERRIDE",
    "Digest",
    "rehearsal_key",
    "NOT_REHEARSABLE",
    # from tracking
    "quoted_columns",
    "records_select",
    "insert_sql",
    "update_sql",
    "records_from_rows",
    # from checks
    "run_statements",
    "check_guards",
    "check_statements",
    "report_danger",
    # from replay
    "SchemaRead",
    # from planning
    "generated_id",
    "plan_migration",
    "drift_lines",
    "render_script",
    # from migrator
    "Migrator",
]


# The old names for the rehearsal row and its key, kept so code written
# against 2.19 and earlier still imports. Deprecated since 2.20.0, removed
# in 3.0.
_RENAMED = {
    "receipt_key": "rehearsal_key",
    "RECEIPT_PASSED": "REHEARSAL_PASSED",
    "RECEIPT_FAILED": "REHEARSAL_FAILED",
    "RECEIPT_OVERRIDE": "REHEARSAL_OVERRIDE",
}


def __getattr__(name: str) -> Any:
    """The renamed names, with a warning naming what to import instead."""
    current = _RENAMED.get(name)
    if current is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"sustained.migrations.{name} is deprecated and will be removed "
        f"in 3.0. Import {current} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return globals()[current]
