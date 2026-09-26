"""
The DuckDB context read: the version, and each table's estimated rows
from `duckdb_tables()`.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Dict, Set, Tuple

from sustained.impact.context import (
    FLOORS,
    ContextPlan,
    EngineContext,
    TableStats,
    attempt,
)
from sustained.impact.rules import common

# One row per table of the current database: its schema, its name,
# whether the schema is the current one, and the row count DuckDB keeps
# for it. DuckDB reports no size in bytes for a single table.
_SIZES_SQL = (
    "SELECT schema_name, table_name, schema_name = current_schema(), "
    "estimated_size FROM duckdb_tables() "
    "WHERE database_name = current_database() AND NOT internal"
)


def duckdb_version(text: str) -> Tuple[int, ...]:
    """A `version()` value as a version, such as (1, 5, 5) for `v1.5.5`."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if match is None:
        return FLOORS["duckdb"]
    return tuple(int(part) for part in match.group(1).split("."))


def context_plan(exact_counts: bool = False) -> ContextPlan:
    """
    Reads the version and the row counts. A statement that fails leaves
    its facts out of `read`, and the rules assume the floor or the worst
    case for them. DuckDB keeps each table's row count, so
    `exact_counts` changes nothing here.
    """
    version = FLOORS["duckdb"]
    read: Set[str] = set()
    rows = yield from attempt("SELECT version()")
    if rows:
        version = duckdb_version(str(rows[0][0]))
        read.add("version")
    tables: Dict[str, TableStats] = {}
    sizes = yield from attempt(_SIZES_SQL)
    if sizes is not None:
        for schema, name, current, count in sizes:
            stats = TableStats(None if count is None else int(str(count)))
            common.add_stats(tables, str(schema), str(name), bool(current), stats)
        read.add("sizes")
    return EngineContext(
        "duckdb",
        version,
        tables=MappingProxyType(tables),
        read=frozenset(read),
    )
