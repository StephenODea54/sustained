"""
The PostgreSQL context read: the version, the `TimeZone` and
`lock_timeout` settings, and each table's size.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Dict, Sequence, Set, Tuple

from sustained.impact.context import (
    FLOORS,
    ContextPlan,
    EngineContext,
    TableStats,
    attempt,
)
from sustained.impact.rules import common

_SETTINGS_SQL = (
    "SELECT current_setting('server_version_num'), "
    "current_setting('TimeZone'), current_setting('lock_timeout')"
)

# One row per table, partitioned table, and materialized view outside
# the system schemas: its schema, its name, whether an unqualified name
# finds it on the search path, the estimated rows, the bytes of the
# table with its indexes and TOAST data, and whether any part of it has
# never been vacuumed or analyzed, which leaves the row estimate empty.
# A partitioned table holds no rows itself, so its figures sum its leaf
# partitions. pg_partition_tree() returns no rows for a table outside a
# partition tree, which then stands for itself. The statement holds no
# percent sign, which a driver could read as a placeholder.
_SIZES_SQL = """SELECT n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid),
  s.rows, s.bytes, s.unread
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL (
  SELECT coalesce(sum(greatest(l.reltuples, 0)), 0)::bigint,
    coalesce(sum(pg_catalog.pg_total_relation_size(l.oid)), 0)::bigint,
    coalesce(bool_or(l.reltuples < 0 OR (l.reltuples = 0 AND l.relpages = 0)), false)
  FROM (
    SELECT c.oid WHERE c.relkind <> 'p'
    UNION ALL
    SELECT t.relid FROM pg_catalog.pg_partition_tree(c.oid) t
    WHERE c.relkind = 'p' AND t.isleaf
  ) leaf (oid)
  JOIN pg_catalog.pg_class l ON l.oid = leaf.oid
) s (rows, bytes, unread)
WHERE c.relkind IN ('r', 'p', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_(toast|temp_)'"""


def server_version(number: str) -> Tuple[int, ...]:
    """A `server_version_num` value as a version, such as (16, 4)."""
    value = int(number)
    return (value // 10000, value % 10000)


def context_plan() -> ContextPlan:
    """
    Reads the version, the settings, and the table sizes. A statement
    that fails leaves its facts out of `read`, and the rules assume the
    floor or the worst case for them.
    """
    version = FLOORS["postgres"]
    settings: Dict[str, str] = {}
    tables: Dict[str, TableStats] = {}
    read: Set[str] = set()
    rows = yield from attempt(_SETTINGS_SQL)
    if rows:
        number, zone, timeout = rows[0]
        version = server_version(str(number))
        settings = {"TimeZone": str(zone), "lock_timeout": str(timeout)}
        read |= {"version", "settings"}
    sizes = yield from attempt(_SIZES_SQL)
    if sizes is not None:
        tables = _sizes(sizes)
        read.add("sizes")
    return EngineContext(
        "postgres",
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(tables),
        read=frozenset(read),
    )


def _sizes(rows: Sequence[Sequence[object]]) -> Dict[str, TableStats]:
    """
    Each table's stats, keyed `schema.table`, and also by the bare name
    when the search path finds the table under it.
    """
    tables: Dict[str, TableStats] = {}
    for schema, name, visible, count, size, unread in rows:
        stats = TableStats(None if unread else int(str(count)), int(str(size)))
        common.add_stats(tables, str(schema), str(name), bool(visible), stats)
    return tables
