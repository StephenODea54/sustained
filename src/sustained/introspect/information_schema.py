"""
The shared information_schema read, and the Catalog that says how one
engine's information_schema differs from it. Presto and Trino read
through it alone; SQL Server, DuckDB, and MySQL build on it.
"""

from __future__ import annotations

import re
from typing import Dict, Generator, List, NamedTuple, Optional, Sequence, Tuple

from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    SchemaPlan,
    Snapshot,
)
from sustained.introspect.normalize import mysql_default_sql
from sustained.introspect.scope import (
    _add_check,
    _foreign_keys,
    _scoped_filter,
    _TableColumns,
)
from sustained.types import RowValue

# System schemas excluded from information_schema introspection.
_SYSTEM_SCHEMAS = (
    "'information_schema'",
    "'pg_catalog'",
    "'sys'",
    "'INFORMATION_SCHEMA'",
)


class Catalog(NamedTuple):
    """
    How one engine's information_schema differs from the shared read.

    Attributes:
        schema_filter: The WHERE fragment that picks the schemas to
            read on an engine with no current-schema expression. It takes
            the column reference as {column}.
        type_column: The column holding the type spelling to compare on.
        current_schema_sql: The expression that names the schema the
            connection is on, or None on engines with no such expression.
            A read is scoped to it, plus every schema the models declare.
            An engine that leaves it None keeps schema_filter whatever
            the models declare, so its read stays as wide as it was.
        comment_column: The information_schema.columns column holding the
            column comment, or None on engines that store none there.
        reads_checks: Whether the engine fills
            information_schema.check_constraints. Presto and Athena have
            no CHECK constraints at all, so their snapshots must leave
            checks_read False rather than report an empty mapping.
        reads_default_sql: Whether the read also selects MySQL's EXTRA
            column and VERSION(), which mysql_default_sql() needs to
            write a MySQL default back as SQL.
        checks_by_table: Whether the check read first tries to match
            check_constraints.table_name as well as the name. MariaDB
            keys a check name on its table, and names a column check
            after its column, so two tables with a check on a price
            column each have a check named price. MySQL has no such
            column, and the read falls back to the name alone.
        reads_collation: Whether the read also selects COLLATION_NAME,
            which MySQL and SQL Server restate when they change a column.
        reads_type_params: Whether the read also selects
            CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, and NUMERIC_SCALE,
            last, for an engine whose type column carries no parameters.
            SQL Server reports nvarchar for an nvarchar(100) column, so
            without them a length or precision change never diffs.
    """

    schema_filter: str
    type_column: str
    current_schema_sql: Optional[str] = None
    comment_column: Optional[str] = None
    reads_checks: bool = True
    reads_default_sql: bool = False
    reads_collation: bool = False
    reads_type_params: bool = False
    checks_by_table: bool = False


ANSI_CATALOG = Catalog(
    schema_filter="{column} NOT IN (" + ", ".join(_SYSTEM_SCHEMAS) + ")",
    type_column="data_type",
)

MYSQL_CATALOG = ANSI_CATALOG._replace(
    # A MySQL schema is a database, and every other database on the server
    # belongs to someone else. DATABASE() is the one the connection is on.
    # Without it here, a model that declares tableSchema would take the
    # connection's own database out of the read.
    current_schema_sql="DATABASE()",
    # data_type reports 'varchar' and keeps the length in a column of its
    # own. column_type reports 'varchar(120)', which is what the compiler
    # emits, so a column never drifts against its own DDL.
    type_column="column_type",
    comment_column="column_comment",
    reads_default_sql=True,
    reads_collation=True,
    checks_by_table=True,
)

# Presto and Trino put the comment straight on information_schema.columns.
# Their tables are files on object storage and hold no CHECK constraints.
PRESTO_CATALOG = ANSI_CATALOG._replace(comment_column="comment", reads_checks=False)

# Athena reads only the connection's schema. Its catalog spans every Glue
# database in the account, so an unscoped read is slow and fails outright
# when any other database holds a table with broken metadata.
ATHENA_CATALOG = PRESTO_CATALOG._replace(current_schema_sql="current_schema")

# MSSQL keys everything on the bare table name, so two schemas holding a
# table with one name would merge. The read covers the connection's own
# schema, plus every schema the models declare.
MSSQL_CATALOG = ANSI_CATALOG._replace(
    current_schema_sql="SCHEMA_NAME()", reads_collation=True, reads_type_params=True
)

# DuckDB keys on the bare table name the same way, and its own catalog
# functions carry a schema_name column to filter on.
DUCKDB_CATALOG = ANSI_CATALOG._replace(current_schema_sql="current_schema()")


def _catalog_filter(catalog: Catalog, column: str, schemas: Tuple[str, ...]) -> str:
    """
    The WHERE fragment one catalog's read puts on a schema column.

    An engine that can name the schema the connection is on reads that
    schema plus every schema the models declare. An engine that cannot
    keeps its own filter: Presto and Trino read every schema but the
    system ones, and a declared schema must not narrow that read to less
    than it covered before.
    """
    if catalog.current_schema_sql is None:
        return catalog.schema_filter.format(column=column)
    return _scoped_filter(column, catalog.current_schema_sql, schemas)


# The ON UPDATE clause MySQL reports in EXTRA, as in "DEFAULT_GENERATED
# on update CURRENT_TIMESTAMP(3)". MariaDB spells the call in lower case.
_MYSQL_ON_UPDATE_RE = re.compile(r"\bon update\s+(\S+)", re.IGNORECASE)


def _information_schema_plan(
    catalog: Catalog = ANSI_CATALOG, schemas: Tuple[str, ...] = ()
) -> SchemaPlan:
    column_filter = _catalog_filter(catalog, "c.table_schema", schemas)
    constraint_filter = _catalog_filter(catalog, "tc.table_schema", schemas)
    # Two schemas in one read can each hold a constraint with one name.
    # The join below matches schema names to keep them apart, and the
    # plain-join fallback cannot, so it only runs on a read that covers
    # one schema. That holds when nothing is declared and the catalog
    # scopes to the schema the connection is on. A catalog with no
    # current-schema expression reads every schema it can see, and its
    # constraint names can collide, but nothing can narrow that read: it
    # keeps the fallback, since the alternative is no constraints at all.
    single_schema = not schemas or catalog.current_schema_sql is None
    # A catalog with no current-schema expression reads every schema it
    # can see, and two schemas holding a table of one name is ordinary
    # there. Nothing can narrow that read, so the refusal below would
    # leave the caller with no way out.
    scoped_read = catalog.current_schema_sql is not None

    # The schema each table came from, so two tables of one name in two
    # schemas are caught instead of merged.
    tables = _TableColumns(schemas, one_schema_per_table=scoped_read)

    def column_fields(with_comment: bool) -> List[Tuple[str, str]]:
        """The (key, SQL) pairs of the column read's SELECT list, in order."""
        fields = [
            ("table", "c.table_name"),
            ("name", "c.column_name"),
            ("type", f"c.{catalog.type_column}"),
            ("nullable", "c.is_nullable"),
            ("default", "c.column_default"),
        ]
        if with_comment:
            fields.append(("comment", f"c.{catalog.comment_column}"))
        fields.append(("schema", "c.table_schema"))
        if catalog.reads_default_sql:
            fields += [("extra", "c.extra"), ("version", "VERSION()")]
        if catalog.reads_collation:
            fields.append(("collation", "c.collation_name"))
        if catalog.reads_type_params:
            fields += [
                ("length", "c.character_maximum_length"),
                ("precision", "c.numeric_precision"),
                ("scale", "c.numeric_scale"),
            ]
        return fields

    def columns_query(with_comment: bool) -> str:
        # The join to information_schema.tables keeps views out. A view's
        # columns would read as a table the models do not declare, so one
        # view in the database is enough to make every plan report drift,
        # and allow_drops would emit a DROP TABLE the engine refuses.
        select = ", ".join(sql for _, sql in column_fields(with_comment))
        return (
            f"SELECT {select} "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            f"WHERE {column_filter} AND t.table_type = 'BASE TABLE' "
            "ORDER BY c.table_name, c.ordinal_position"
        )

    # The comment travels with the column instead of in a second full read
    # of information_schema.columns. Engines that advertise a comment
    # column but do not have one fall back to the plain read, which is
    # what the second query's own fallback used to do.
    comments_read = catalog.comment_column is not None
    if comments_read:
        try:
            column_rows = yield columns_query(True)
        except Exception:
            comments_read = False
            column_rows = yield columns_query(False)
    else:
        column_rows = yield columns_query(False)

    # A row shorter than the SELECT list reads its missing columns as None.
    keys = [key for key, _ in column_fields(comments_read)]
    for values in column_rows:
        row: Dict[str, RowValue] = dict(zip(keys, values))
        table, name, default = row["table"], row["name"], row["default"]
        # MySQL reports an uncommented column as '', not NULL.
        raw_comment = row.get("comment")
        comment = str(raw_comment) if raw_comment not in (None, "") else None
        raw_type = str(row["type"]) if row["type"] else ""
        if catalog.reads_type_params:
            raw_type = _sized_type(
                raw_type, row.get("length"), row.get("precision"), row.get("scale")
            )
        default_sql = None
        # Only MySQL reads EXTRA and VERSION().
        mysql_row = "version" in row
        extra = str(row["extra"] or "") if mysql_row else ""
        # MariaDB reports its defaults as SQL already, quotes included.
        if (
            mysql_row
            and default is not None
            and "MARIADB" not in str(row["version"]).upper()
        ):
            default_sql = mysql_default_sql(str(default), extra, raw_type)
        collation = row.get("collation")
        on_update = _MYSQL_ON_UPDATE_RE.search(extra)
        tables.add(
            str(table),
            row.get("schema"),
            str(name),
            IntrospectedColumn(
                raw_type=raw_type,
                nullable=str(row["nullable"]).upper() == "YES",
                primary_key=False,
                default=default,
                comment=comment,
                default_sql=default_sql,
                autoincrement="AUTO_INCREMENT" in extra.upper(),
                collation=None if collation is None else str(collation),
                name=str(name),
                on_update=None if on_update is None else on_update.group(1),
            ),
        )

    primary_keys: Dict[str, List[str]] = {}
    unique_indexes: Dict[str, Dict[str, IntrospectedIndex]] = {}
    foreign_keys: Dict[str, Dict[str, IntrospectedForeignKey]] = {}
    constraints_read = False
    # A constraint name is only unique within its schema, so the join
    # matches schemas as well as names. Without that, two schemas each
    # holding a constraint with one name cross-multiply into a garbled
    # column list. An engine whose key_column_usage has no table_schema
    # column takes the plain join instead.
    joins = ["AND tc.table_schema = kcu.table_schema "]
    if single_schema:
        joins.append("")
    for schema_join in joins:
        try:
            constraint_rows = yield (
                "SELECT tc.table_name, tc.constraint_type, tc.constraint_name, "
                "kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON tc.constraint_name = kcu.constraint_name "
                "AND tc.table_name = kcu.table_name "
                f"{schema_join}"
                f"WHERE {constraint_filter} "
                "ORDER BY kcu.ordinal_position"
            )
        except Exception:
            # No constraint views, or no table_schema on this one.
            continue
        constraint_columns: Dict[Tuple[str, str, str], List[str]] = {}
        spelled_constraints: Dict[str, str] = {}
        for table, ctype, cname, column in constraint_rows:
            key = (table.lower(), ctype.upper(), cname.lower())
            constraint_columns.setdefault(key, []).append(column.lower())
            spelled_constraints.setdefault(cname.lower(), cname)
        for (table, ctype, cname), cols in constraint_columns.items():
            spelled = spelled_constraints[cname]
            if ctype == "PRIMARY KEY":
                primary_keys[table] = cols
            elif ctype == "UNIQUE":
                unique_indexes.setdefault(table, {})[cname] = IntrospectedIndex(
                    tuple(cols), True, constraint=True, name=spelled
                )
            elif ctype == "FOREIGN KEY":
                # The referenced table is engine-specific to resolve;
                # presence is enough for constraint notes.
                foreign_keys.setdefault(table, {})[cname] = IntrospectedForeignKey(
                    columns=tuple(cols), target_table="?", name=spelled
                )
        constraints_read = True
        break

    checks, check_names, checks_read = yield from _check_plan(
        catalog, constraint_filter
    )

    schema = Snapshot(
        constraints_read=constraints_read,
        checks_read=checks_read,
        comments_read=comments_read,
    )
    for table in tables.columns:
        schema[table] = tables.table(
            table,
            tuple(primary_keys.get(table, ())),
            foreign_keys.get(table, {}),
            unique_indexes.get(table, {}),
            checks.get(table, {}),
            check_names.get(table, {}),
        )
    return schema


def _check_plan(catalog: Catalog, constraint_filter: str) -> Generator[
    str,
    List[Sequence[RowValue]],
    Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]], bool],
]:
    """
    Reads the check constraints of each table: the expressions and the
    spelled names by lowercased check name, and whether a check view
    was read.
    """
    checks: Dict[str, Dict[str, str]] = {}
    check_names: Dict[str, Dict[str, str]] = {}
    checks_read = False
    check_joins = (
        ["AND cc.table_name = tc.table_name "] if catalog.checks_by_table else []
    )
    check_joins.append("")
    for table_join in check_joins if catalog.reads_checks else ():
        try:
            check_rows = yield (
                "SELECT tc.table_name, tc.constraint_name, cc.check_clause "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.check_constraints cc "
                "ON cc.constraint_schema = tc.constraint_schema "
                "AND cc.constraint_name = tc.constraint_name "
                f"{table_join}"
                "WHERE tc.constraint_type = 'CHECK' "
                f"AND {constraint_filter}"
            )
        except Exception:
            # No table_name on this check view, or an engine too old for
            # the view; the last case degrades to no checks.
            continue
        for table, cname, clause in check_rows:
            _add_check(checks, check_names, table, cname, str(clause))
        checks_read = True
        break
    return checks, check_names, checks_read


def _replace_foreign_keys(schema: Snapshot, rows: Sequence[Sequence[RowValue]]) -> None:
    """
    Replaces the foreign keys of the shared information_schema read with
    rows that say where each key points: the table, the constraint name,
    one constrained column, the table and column it references, the
    delete and update actions, and the schema of the referenced table
    when it is not the connection's own, one row per column in key
    order. The shared read joins no referential view, so its keys point
    at '?'. SQL Server spells an action with an underscore, as in
    SET_NULL.
    """
    foreign_keys = _foreign_keys(
        rows, lambda action: str(action).replace("_", " ").upper()
    )
    for table, existing in list(schema.items()):
        schema[table] = existing._replace(foreign_keys=foreign_keys.get(table, {}))
    schema.constraints_read = True


def _merge_plain_indexes(
    schema: Snapshot, plain: Dict[str, Dict[str, IntrospectedIndex]]
) -> None:
    """Adds engine-read indexes to the tables the shared read produced."""
    for table, indexes in plain.items():
        existing = schema.get(table)
        if existing is None:
            continue
        merged = dict(existing.indexes)
        merged.update(indexes)
        schema[table] = existing._replace(indexes=merged)


# The information_schema type names whose length goes back on, and the
# ones whose precision and scale do.
_LENGTH_TYPES = frozenset(
    {"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"}
)
_PRECISION_TYPES = frozenset({"decimal", "numeric"})


def _sized_type(
    data_type: str,
    length: Optional[RowValue],
    precision: Optional[RowValue],
    scale: Optional[RowValue],
) -> str:
    """
    The type spelling an information_schema column is compared on, with
    its length or precision put back: nvarchar(100), varbinary(MAX),
    decimal(10,2). A length of -1 is SQL Server's MAX. Every other type
    keeps its bare name, since an integer's numeric_precision is a
    property of the type rather than a declared parameter.
    """
    name = data_type.lower()
    if name in _LENGTH_TYPES and length is not None:
        size = "MAX" if int(str(length)) == -1 else str(length)
        return f"{data_type}({size})"
    if name in _PRECISION_TYPES and precision is not None:
        return f"{data_type}({precision},{scale if scale is not None else 0})"
    return data_type
