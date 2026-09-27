"""
The SQL Server context read: the version, the edition, the session's
`LOCK_TIMEOUT`, whether the database reads committed rows from row
versions, and each table's rows, bytes, and clustered index.
"""

from __future__ import annotations

import re
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

_SERVER_SQL = (
    "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)), "
    "CAST(SERVERPROPERTY('EngineEdition') AS int), "
    "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)), @@LOCK_TIMEOUT"
)

_SNAPSHOT_SQL = (
    "SELECT is_read_committed_snapshot_on FROM sys.databases "
    "WHERE database_id = DB_ID()"
)

# Each table's rows from its heap or clustered index, its used pages
# across every index and allocation unit, and that heap or clustered
# index: index_id 0 is a heap and 1 a clustered index. The catalog views
# need no permission beyond seeing the table.
_TABLES_SQL = """SELECT s.name, t.name,
  CASE WHEN s.name = SCHEMA_NAME() THEN 1 ELSE 0 END,
  (SELECT SUM(p.rows) FROM sys.partitions p
   WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)),
  (SELECT SUM(a.used_pages) FROM sys.partitions p
   JOIN sys.allocation_units a ON a.container_id = p.partition_id
   WHERE p.object_id = t.object_id),
  i.index_id, i.name
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.indexes i ON i.object_id = t.object_id AND i.index_id IN (0, 1)"""

_PAGE_BYTES = 8192


def _sized(tables: Optional[Collection[str]]) -> str:
    """
    The table read, for the tables of the given names, or for all. Each
    name is spelled as its UTF-16 bytes, so no quote in it reaches the
    statement.
    """
    if tables is None:
        return _TABLES_SQL
    listed = ", ".join(
        f"CONVERT(nvarchar(128), 0x{name.encode('utf-16-le').hex()})"
        for name in sorted(set(tables))
    )
    return f"{_TABLES_SQL}\nWHERE LOWER(t.name) IN ({listed or 'NULL'})"


def server_version(text: str) -> Tuple[int, ...]:
    """A `ProductVersion` value as a version, such as (16, 0, 4135, 4)."""
    match = re.match(r"(\d+(?:\.\d+)*)", text.strip())
    if match is None:
        return FLOORS["mssql"]
    return tuple(int(part) for part in match.group(1).split("."))


def context_plan(
    exact_counts: bool = False, tables: Optional[Collection[str]] = None
) -> ContextPlan:
    """
    Reads the version, the edition, the settings, and the tables. A
    statement that fails leaves its facts out of `read`, and the rules
    assume the floor or the worst case for them. `sys.partitions` keeps
    each table's row count, so `exact_counts` changes nothing here. With
    `tables`, lower case table names, only the tables of those names are
    read.
    """
    version = FLOORS["mssql"]
    edition = None
    settings: Dict[str, str] = {}
    stats: Dict[str, TableStats] = {}
    read: Set[str] = set()
    rows = yield from attempt(_SERVER_SQL)
    if rows:
        text, engine, name, timeout = rows[0]
        version = server_version(str(text))
        edition = str(name)
        settings["EngineEdition"] = str(engine)
        settings["lock_timeout"] = str(timeout)
        read.update({"version", "edition", "settings"})
    snapshot = yield from attempt(_SNAPSHOT_SQL)
    if snapshot:
        settings["read_committed_snapshot"] = "on" if snapshot[0][0] else "off"
        read.add("settings")
    found = yield from attempt(_sized(tables))
    if found is not None:
        for schema, table, bare, count, pages, index_id, index in found:
            table_stats = TableStats(
                None if count is None else int(str(count)),
                None if pages is None else int(str(pages)) * _PAGE_BYTES,
                heap=int(str(index_id)) == 0,
                clustered=None if index is None else str(index),
            )
            common.add_stats(stats, str(schema), str(table), bool(bare), table_stats)
        read.update({"sizes", "clustered"})
    return EngineContext(
        "mssql",
        version,
        edition,
        MappingProxyType(settings),
        MappingProxyType(stats),
        read=frozenset(read),
    )
