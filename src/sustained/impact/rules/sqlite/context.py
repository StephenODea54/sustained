"""
The SQLite context read: the version, the journal mode, each table's
estimated rows from `sqlite_stat1`, and the size of the database file.
A table's bytes are the file's bytes in proportion to its share of the
rows `sqlite_stat1` counts. With `exact_counts`, the read also counts
the rows of each table `sqlite_stat1` has no row count for, and reads
each table's bytes from `dbstat`, which visits every page of the table
and its indexes.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Collection, Dict, Optional, Set, Tuple

from sustained.impact.context import (
    FLOORS,
    ContextPlan,
    EngineContext,
    TableStats,
    attempt,
)
from sustained.impact.rules import common
from sustained.impact.window import DATABASE

# The row count leads each `stat` value of sqlite_stat1, such as
# `500 1`, and CAST reads the leading integer. ANALYZE writes the table,
# so a database never analyzed fails this read and has no row counts.
_ROWS_SQL = "SELECT tbl, MAX(CAST(stat AS INTEGER)) FROM sqlite_stat1 GROUP BY tbl"

# The bytes of each table with its indexes, from the dbstat virtual
# table, which a SQLite built without SQLITE_ENABLE_DBSTAT_VTAB lacks.
# The join on the name lets dbstat visit only the b-trees of the tables
# the read names.
_BYTES_SQL = (
    "SELECT m.tbl_name, SUM(s.pgsize) FROM sqlite_schema m "
    "JOIN dbstat s ON s.name = m.name "
    "WHERE s.aggregate = 1 AND m.type IN ('table', 'index') "
    "AND m.tbl_name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
)

# The tables a row count can read: virtual tables are left out, since
# counting one runs the module that implements it.
_TABLES_SQL = (
    "SELECT name FROM sqlite_schema WHERE type = 'table' "
    "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND sql NOT LIKE 'CREATE VIRTUAL%'"
)


def _count_sql(table: str) -> str:
    quoted = table.replace('"', '""')
    return f'SELECT COUNT(*) FROM "{quoted}"'


def _bytes_sql(tables: Optional[Collection[str]]) -> str:
    """The byte read, for the tables of the given names, or for all."""
    if tables is None:
        return f"{_BYTES_SQL} GROUP BY m.tbl_name"
    listed = ", ".join(
        "'" + name.replace("'", "''") + "'" for name in sorted(set(tables))
    )
    return f"{_BYTES_SQL} AND lower(m.tbl_name) IN ({listed}) GROUP BY m.tbl_name"


def sqlite_version(text: str) -> Tuple[int, ...]:
    """A `sqlite_version()` value as a version, such as (3, 45, 1)."""
    return common.dotted_version(text, FLOORS["sqlite"])


def context_plan(
    exact_counts: bool = False, tables: Optional[Collection[str]] = None
) -> ContextPlan:
    """
    Reads the version, the journal mode, and the sizes. A statement that
    fails leaves its facts out of `read`, and the rules assume the floor
    or the worst case for them. Without `exact_counts`, no statement
    visits a table's pages: a table's bytes are the database file's
    bytes times its share of the rows `sqlite_stat1` counts. With it,
    `SELECT COUNT(*)` reads each table `sqlite_stat1` has no row count
    for, `dbstat` reads each table's bytes, and `read` gains `counts`
    when the table list was read. With `tables`, lower case table
    names, the counts and the bytes cover only the tables of those
    names.
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
    wanted = None if tables is None else {name.lower() for name in tables}
    pages = None
    if exact_counts and wanted != set():
        names = yield from attempt(_TABLES_SQL)
        if names is not None:
            read.add("counts")
            for (name,) in names:
                key = str(name).lower()
                if key in counts or (wanted is not None and key not in wanted):
                    continue
                counted = yield from attempt(_count_sql(str(name)))
                if counted:
                    counts[key] = int(str(counted[0][0]))
        pages = yield from attempt(_bytes_sql(wanted))
        if pages is not None:
            sizes = {str(name).lower(): int(str(size)) for name, size in pages}
    found: Dict[str, TableStats] = {}
    count = yield from attempt("PRAGMA page_count")
    size = yield from attempt("PRAGMA page_size")
    if count and size:
        database = int(str(count[0][0])) * int(str(size[0][0]))
        found[DATABASE] = TableStats(bytes=database)
        if not exact_counts and stat is not None:
            sizes = _shares(counts, database)
    for name in set(counts) | set(sizes):
        stats = TableStats(counts.get(name), sizes.get(name))
        found[name] = stats
        found[f"main.{name}"] = stats
    if stat is not None or pages is not None:
        read.add("sizes")
    return EngineContext(
        "sqlite",
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(found),
        read=frozenset(read),
    )


def _shares(counts: Dict[str, int], database: int) -> Dict[str, int]:
    """
    Each counted table's bytes, as the database's bytes in proportion
    to the table's share of every counted row.
    """
    total = sum(counts.values())
    return {
        name: database * rows // total if total else 0 for name, rows in counts.items()
    }
