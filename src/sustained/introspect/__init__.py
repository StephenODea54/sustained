"""
Reading a live schema, and comparing two reads.

introspect_schema() and async_introspect_schema() report the tables,
columns, primary keys, unique constraints, foreign keys, defaults,
indexes, check constraints, column comments, and enum types a database
currently holds.
Both drive the same generator-based plan, so one dialect's reading code
serves a blocking connection and an async adapter alike.

diff_snapshots() compares two such reads. A rehearsal uses it to check
that the down steps put the schema back where it started.

The package keeps one module per dialect's plan. `model` defines what a
read reports, `normalize` the spelling reductions a diff compares on,
`scope` the schema filters the catalog reads share, `runner` the loops
that drive a plan, and `compare` diff_snapshots(). The package exports
the public names. Import a private helper from its own module.
"""

from __future__ import annotations

from sustained.introspect.compare import (
    diff_snapshots,
)
from sustained.introspect.information_schema import (
    ANSI_CATALOG,
    ATHENA_CATALOG,
    DUCKDB_CATALOG,
    MSSQL_CATALOG,
    MYSQL_CATALOG,
    PRESTO_CATALOG,
    Catalog,
)
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedPartition,
    IntrospectedTable,
    SchemaPlan,
    SchemaRecorder,
    Snapshot,
)
from sustained.introspect.normalize import (
    is_sequence_default,
    mysql_default_sql,
    normalize_check,
    normalize_default,
    normalize_type,
    parse_inline_enum,
    type_params,
)
from sustained.introspect.runner import (
    async_introspect_schema,
    introspect_schema,
)

__all__ = [
    "ANSI_CATALOG",
    "ATHENA_CATALOG",
    "Catalog",
    "DUCKDB_CATALOG",
    "IntrospectedColumn",
    "IntrospectedForeignKey",
    "IntrospectedIndex",
    "IntrospectedPartition",
    "IntrospectedTable",
    "MSSQL_CATALOG",
    "MYSQL_CATALOG",
    "PRESTO_CATALOG",
    "SchemaPlan",
    "SchemaRecorder",
    "Snapshot",
    "async_introspect_schema",
    "diff_snapshots",
    "introspect_schema",
    "is_sequence_default",
    "mysql_default_sql",
    "normalize_check",
    "normalize_default",
    "normalize_type",
    "parse_inline_enum",
    "type_params",
]
