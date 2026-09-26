"""
The SQLite context read: the version, the journal mode, each table's
estimated rows from `sqlite_stat1`, its bytes from `dbstat`, and the
size of the database file. With `exact_counts`, the read also counts
the rows of each table `sqlite_stat1` has no row count for.
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
from sustained.impact.window import DATABASE

# The row count leads each `stat` value of sqlite_stat1, such as
# `500 1`, and CAST reads the leading integer. ANALYZE writes the table,
# so a database never analyzed fails this read and has no row counts.
_ROWS_SQL = "SELECT tbl, MAX(CAST(stat AS INTEGER)) FROM sqlite_stat1 GROUP BY tbl"

# The bytes of each table with its indexes, from the dbstat virtual
# table, which a SQLite built without SQLITE_ENABLE_DBSTAT_VTAB lacks.
_BYTES_SQL = (
    "SELECT m.tbl_name, SUM(s.pgsize) FROM dbstat s "
    "JOIN sqlite_schema m ON m.name = s.name "
    "WHERE s.aggregate = 1 AND m.type IN ('table', 'index') "
    "AND m.tbl_name NOT LIKE 'sqlite_%' GROUP BY m.tbl_name"
)

# The tables a row count can read: virtual tables are left out, since
# counting one runs the module that implements it.
_TABLES_SQL = (
    "SELECT name FROM sqlite_schema WHERE type = 'table' "
    "AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%'"
)


def _count_sql(table: str) -> str:
    quoted = table.replace('"', '""')
    return f'SELECT COUNT(*) FROM "{quoted}"'


def sqlite_version(text: str) -> Tuple[int, ...]:
    """A `sqlite_version()` value as a version, such as (3, 45, 1)."""
    match = re.match(r"(\d+(?:\.\d+)*)", text.strip())
    if match is None:
        return FLOORS["sqlite"]
    return tuple(int(part) for part in match.group(1).split("."))


def context_plan(exact_counts: bool = False) -> ContextPlan:
    """
    Reads the version, the journal mode, and the sizes. A statement that
    fails leaves its facts out of `read`, and the rules assume the floor
    or the worst case for them. Without `exact_counts`, no statement
    counts a table's rows. With it, `SELECT COUNT(*)` reads each table
    `sqlite_stat1` has no row count for, which visits every page of the
    table, and `read` gains `counts` when the table list was read.
    """
    version = FLOORS["sqlite"]
    settings: Dict[str, str] = {}
    read: Set[str] = set()
    rows = yield from attempt("SELECT sqlite_version()")
    if rows:
        version = sqlite_version(str(rows[0][0]))
        read.add("version")
    mode = yield from attempt("PRAGMA journal_mode")
    if mode:
        settings["journal_mode"] = str(mode[0][0]).lower()
        read.add("settings")
    counts: Dict[str, int] = {}
    sizes: Dict[str, int] = {}
    stat = yield from attempt(_ROWS_SQL)
    if stat is not None:
        counts = {str(name).lower(): int(str(count)) for name, count in stat}
    if exact_counts:
        names = yield from attempt(_TABLES_SQL)
        if names is not None:
            read.add("counts")
            for (name,) in names:
                if str(name).lower() in counts:
                    continue
                counted = yield from attempt(_count_sql(str(name)))
                if counted:
                    counts[str(name).lower()] = int(str(counted[0][0]))
    pages = yield from attempt(_BYTES_SQL)
    if pages is not None:
        sizes = {str(name).lower(): int(str(size)) for name, size in pages}
    tables: Dict[str, TableStats] = {}
    for name in set(counts) | set(sizes):
        stats = TableStats(counts.get(name), sizes.get(name))
        tables[name] = stats
        tables[f"main.{name}"] = stats
    count = yield from attempt("PRAGMA page_count")
    size = yield from attempt("PRAGMA page_size")
    if count and size:
        tables[DATABASE] = TableStats(
            bytes=int(str(count[0][0])) * int(str(size[0][0]))
        )
    if stat is not None or pages is not None:
        read.add("sizes")
    return EngineContext(
        "sqlite",
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(tables),
        read=frozenset(read),
    )
