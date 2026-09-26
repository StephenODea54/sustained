"""
Planning a run without applying it: the migration a diff of the models
produces, the drift the models still report, and the script a run would
execute.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

from sustained.dialects import Dialects
from sustained.migrations.checks import _is_current
from sustained.migrations.migration import (
    AppliedRecord,
    Migration,
    migration_checksum,
    migration_sql,
)
from sustained.migrations.tracking import _next_seq, quoted_columns
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.analysis import MigrationStatement
    from sustained.compilers.base import Compiler
    from sustained.impact import EngineContext, ImpactReport
    from sustained.introspect import Snapshot
    from sustained.model import Model


def generated_id(migration_id: Optional[str] = None) -> str:
    """The id a migration generated from the models carries."""
    return migration_id or datetime.now(timezone.utc).strftime("auto_%Y%m%d%H%M%S_%f")


def plan_migration(
    connection: Connection,
    models: List[Type["Model"]],
    dialect: Dialects,
    exclude_tables: Tuple[str, ...],
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    migration_id: Optional[str] = None,
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    type_casts: Optional[Dict[str, str]] = None,
    ignore_undeclared: bool = True,
    snapshot: Optional["Snapshot"] = None,
) -> Optional[Migration]:
    """
    The migration a diff of the models against the database produces, or
    None when the schema already holds everything the models declare.
    Both migrators plan through this. A snapshot already read is used
    instead of reading the schema again.
    """
    from sustained.autogenerate import autogenerate

    return autogenerate(
        connection,
        models,
        id=generated_id(migration_id),
        dialect=dialect,
        allow_drops=allow_drops,
        ignore_changed_columns=ignore_changed_columns,
        exclude_tables=exclude_tables,
        renames=renames,
        table_renames=table_renames,
        type_casts=type_casts,
        ignore_undeclared=ignore_undeclared,
        snapshot=snapshot,
    )


def plan_migrations(
    connection: Connection,
    models: List[Type["Model"]],
    dialect: Dialects,
    exclude_tables: Tuple[str, ...],
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    migration_id: Optional[str] = None,
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    type_casts: Optional[Dict[str, str]] = None,
    ignore_undeclared: bool = True,
    snapshot: Optional["Snapshot"] = None,
    online: bool = False,
) -> List[Migration]:
    """
    The migrations a diff of the models against the database produces,
    as plan_migration() plans them, with online passed on to
    autogenerate_migrations() on PostgreSQL. The migrators write the
    MySQL clauses online asks for themselves, after the diff, so the
    diff is never asked for them.
    """
    from sustained.autogenerate import autogenerate_migrations

    return autogenerate_migrations(
        connection,
        models,
        id=generated_id(migration_id),
        dialect=dialect,
        allow_drops=allow_drops,
        ignore_changed_columns=ignore_changed_columns,
        exclude_tables=exclude_tables,
        renames=renames,
        table_renames=table_renames,
        type_casts=type_casts,
        ignore_undeclared=ignore_undeclared,
        snapshot=snapshot,
        online=online and dialect is Dialects.POSTGRES,
    )


# CONCURRENTLY after CREATE [UNIQUE] INDEX or DROP INDEX.
_CONCURRENTLY_RE = re.compile(
    r"^((?:CREATE\s+(?:UNIQUE\s+)?|DROP\s+)INDEX)\s+CONCURRENTLY\b",
    re.IGNORECASE,
)


def rehearsed_form(migration: Migration, dialect: Dialects) -> Optional[Migration]:
    """
    The form of a generated migration without a transaction that a
    rehearsal runs inside its transaction: on PostgreSQL, the same
    statements with CONCURRENTLY taken out of CREATE INDEX and DROP
    INDEX, which PostgreSQL refuses inside a transaction block, and with
    the same id. None on another dialect, where such a migration is
    reported as not rehearsable, and for a callable step.
    """
    if dialect is not Dialects.POSTGRES or callable(migration.up):
        return None
    down = migration.down
    if callable(down):
        return None
    return Migration(
        migration.id,
        up=[_CONCURRENTLY_RE.sub(r"\1", s) for s in _statements(migration.up)],
        down=(
            None
            if down is None
            else [_CONCURRENTLY_RE.sub(r"\1", s) for s in _statements(down)]
        ),
    )


def _statements(step: object) -> List[str]:
    """A generated step's statements; the diff writes a list of strings."""
    assert isinstance(step, list)
    return [str(s) for s in step]


def asserted_migration(
    migration: Migration,
    dialect: Dialects,
    compiler: "Compiler",
    context: "EngineContext",
) -> Migration:
    """
    The generated migration with the ALGORITHM and LOCK clause the
    impact rules predict written on each ALTER TABLE, CREATE INDEX, and
    DROP INDEX, as sustained.impact.rules.mysql.asserted_statements()
    writes them, so the server refuses the statement instead of running
    it with a slower algorithm or a stronger lock. The migration is
    returned as it is on a dialect other than MySQL, and when no
    statement changed. The down step is left as it is.
    """
    if dialect is not Dialects.MYSQL or callable(migration.up):
        return migration
    from sustained.impact.rules.mysql import asserted_statements

    statements = migration_sql(migration, "up", compiler)
    asserted = asserted_statements(statements, context)
    if asserted == statements:
        return migration
    return Migration(
        migration.id,
        up=asserted,
        down=migration.down,
        transactional=migration.transactional,
    )


def drift_lines(
    connection: Connection,
    models: List[Type["Model"]],
    dialect: Dialects,
    exclude_tables: Tuple[str, ...],
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    ignore_changed_columns: bool = False,
    snapshot: Optional["Snapshot"] = None,
) -> List[str]:
    """
    What the models still ask for, one readable line each. Both migrators
    report drift through this. A snapshot already read is used instead of
    reading the database again.
    """
    from sustained.autogenerate import diff_schema

    diff = diff_schema(
        connection,
        models,
        dialect=dialect,
        exclude_tables=exclude_tables,
        renames=renames,
        table_renames=table_renames,
        snapshot=snapshot,
    )
    return diff.outstanding(ignore_changed_columns=ignore_changed_columns)


class _Steps:
    """One migration's statements, in a script before they are rendered."""

    def __init__(self, migration: Migration, statements: List[str]) -> None:
        self.migration = migration
        self.statements = statements


def render_script(
    compiler: "Compiler",
    table_sql: str,
    migrations: Sequence[Migration],
    records: Sequence[AppliedRecord],
    direction: str = "up",
    generated: Optional[Mapping[str, Migration]] = None,
    annotate: Optional[
        Callable[[Sequence["MigrationStatement"]], "ImpactReport"]
    ] = None,
) -> str:
    """
    The SQL a run would execute, rendered from the migrations and the
    tracking rows that were read, without touching a database. Both
    migrators call this, so either one renders the same script.

    `generated` maps the id of each migration generated from the models
    to the migration its tracking row stores. A 'down' script reverts
    those from the stored statements, the same way down() does, and
    stops at an applied id found in neither place.

    `annotate` analyzes the script's migration statements, bookkeeping
    left out, as one run in script order. Each statement's impact then
    prints above it as `-- impact:` comments, each migration's windows
    and findings after its last statement, and the report's summary on
    the first line.
    """
    lines = _script_lines(
        compiler, table_sql, migrations, records, direction, generated
    )
    if annotate is None:
        return "\n".join(
            (
                "\n".join(f"{s};" for s in line.statements)
                if isinstance(line, _Steps)
                else line
            )
            for line in lines
            if not (isinstance(line, _Steps) and not line.statements)
        )
    return _annotated(lines, annotate)


def _annotated(
    lines: List[Union[str, _Steps]],
    annotate: Callable[[Sequence["MigrationStatement"]], "ImpactReport"],
) -> str:
    """The script with each statement's impact above it as comments."""
    from sustained.analysis import MigrationStatement
    from sustained.impact.report import (
        migration_annotation,
        statement_annotation,
        summary,
    )

    groups = [line for line in lines if isinstance(line, _Steps)]
    statements = [
        MigrationStatement(s, g.migration.id, g.migration.transactional)
        for g in groups
        for s in g.statements
    ]
    report = annotate(statements)
    impacts = iter(report.migrations)
    out: List[str] = []
    if statements:
        out.append(f"-- impact: {summary(report)}")
    for line in lines:
        if not isinstance(line, _Steps):
            out.append(line)
            continue
        if not line.statements:
            continue
        migration = next(impacts)
        for statement, impact in zip(line.statements, migration.statements):
            out.extend(_comments(statement_annotation(impact)))
            out.append(f"{statement};")
        out.extend(_comments(migration_annotation(migration)))
    return "\n".join(out)


def _comments(lines: List[str]) -> List[str]:
    """Each line as an `-- impact:` comment, a line break included."""
    return [
        f"-- impact: {part}" for line in lines for part in line.splitlines() or [""]
    ]


def _script_lines(
    compiler: "Compiler",
    table_sql: str,
    migrations: Sequence[Migration],
    records: Sequence[AppliedRecord],
    direction: str,
    generated: Optional[Mapping[str, Migration]],
) -> List[Union[str, _Steps]]:
    """
    The script's lines, with each migration's statements held in a
    `_Steps` entry until render_script() renders them.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    format_value = compiler.format_value
    column = compiler.quote_identifier
    insert_columns = quoted_columns(
        compiler, "id", "seq", "checksum", "applied_at", "execution_ms", "success"
    )
    versioned = [m for m in migrations if not m.repeatable]
    repeatables = [m for m in migrations if m.repeatable]
    lines: List[Union[str, _Steps]] = []
    if direction == "up":
        records_by_id = {r.id: r for r in records}
        applied = {r.id for r in records if r.success}
        next_seq = _next_seq(list(records))
        for migration in versioned:
            if migration.id in applied:
                continue
            lines.append(f"-- up: {migration.id}")
            lines.append(_Steps(migration, migration_sql(migration, "up", compiler)))
            lines.append(
                f"INSERT INTO {table_sql} "
                f"({insert_columns}) "
                f"VALUES ({format_value(migration.id)}, {next_seq}, "
                f"{format_value(migration_checksum(migration))}, "
                f"{format_value(timestamp)}, NULL, "
                f"{compiler.compile_boolean(True)});"
            )
            next_seq += 1
        for migration in repeatables:
            record = records_by_id.get(migration.id)
            checksum = migration_checksum(migration)
            if _is_current(record, migration, True):
                continue
            lines.append(f"-- repeat: {migration.id}")
            lines.append(_Steps(migration, migration_sql(migration, "up", compiler)))
            if record is None:
                lines.append(
                    f"INSERT INTO {table_sql} "
                    f"({insert_columns}) "
                    f"VALUES ({format_value(migration.id)}, {next_seq}, "
                    f"{format_value(checksum)}, "
                    f"{format_value(timestamp)}, NULL, "
                    f"{compiler.compile_boolean(True)});"
                )
                next_seq += 1
            else:
                lines.append(
                    f"UPDATE {table_sql} "
                    f"SET {column('checksum')} = {format_value(checksum)}, "
                    f"{column('applied_at')} = "
                    f"{format_value(timestamp)}, "
                    f"{column('execution_ms')} = NULL, "
                    f"{column('success')} = "
                    f"{compiler.compile_boolean(True)} "
                    f"WHERE {column('id')} = {format_value(migration.id)};"
                )
    elif direction == "down":
        by_id = {m.id: m for m in migrations}
        repeatable_ids = {m.id for m in repeatables}
        applied_ids = [
            r.id for r in records if r.success and r.id not in repeatable_ids
        ]
        stored = generated or {}
        for migration_id in reversed(applied_ids):
            registered = by_id.get(migration_id) or stored.get(migration_id)
            if registered is None or registered.down is None:
                lines.append(
                    f"-- down: {migration_id} has no reversible step; stopping"
                )
                break
            lines.append(f"-- down: {migration_id}")
            lines.append(
                _Steps(registered, migration_sql(registered, "down", compiler))
            )
            lines.append(
                f"DELETE FROM {table_sql} WHERE {column('id')} = "
                f"{format_value(migration_id)};"
            )
    else:
        raise ValueError("direction must be 'up' or 'down'.")
    return lines
