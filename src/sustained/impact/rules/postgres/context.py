"""
The PostgreSQL context read: the version, the `TimeZone` and
`lock_timeout` settings, and each table's size.

`pg_total_relation_size()` takes ACCESS SHARE on each table it sizes,
which waits behind a session with ACCESS EXCLUSIVE on it. The size read
runs under `SIZE_LOCK_TIMEOUT`, and the setting read before it is put
back after it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Collection, Dict, Generator, Optional, Sequence, Set, Tuple

from sustained.impact.context import (
    FLOORS,
    ContextPlan,
    EngineContext,
    Rows,
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


# How long the size read waits for each lock it takes.
SIZE_LOCK_TIMEOUT = "1s"


def server_version(number: str) -> Tuple[int, ...]:
    """A `server_version_num` value as a version, such as (16, 4)."""
    value = int(number)
    return (value // 10000, value % 10000)


def context_plan(
    exact_counts: bool = False, tables: Optional[Collection[str]] = None
) -> ContextPlan:
    """
    Reads the version, the settings, and the table sizes. A statement
    that fails leaves its facts out of `read`, and the rules assume the
    floor or the worst case for them. The sizes are the estimates the
    server keeps, so `exact_counts` changes nothing here.

    With `tables`, lower case table names, only the tables of those
    names are sized, all in one statement. When that statement fails,
    each table is read in a statement of its own, so a table whose lock
    is not granted within the timeout is the only one left unknown.
    """
    version = FLOORS["postgres"]
    settings: Dict[str, str] = {}
    found: Dict[str, TableStats] = {}
    read: Set[str] = set()
    rows = yield from attempt(_SETTINGS_SQL)
    if rows:
        number, zone, timeout = rows[0]
        version = server_version(str(number))
        settings = {"TimeZone": str(zone), "lock_timeout": str(timeout)}
        read |= {"version", "settings"}
    names = None if tables is None else sorted(set(tables))
    if names != []:
        found = yield from _read_sizes(names, read)
    return EngineContext(
        "postgres",
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(found),
        read=frozenset(read),
    )


def _read_sizes(
    names: Optional[Sequence[str]], read: Set[str]
) -> Generator[str, Rows, Dict[str, TableStats]]:
    """
    The stats of the named tables, or of every table, read under
    `SIZE_LOCK_TIMEOUT`, with the session's own lock timeout put back
    after. Adds `sizes` to `read` when a size statement succeeded.
    """
    found: Dict[str, TableStats] = {}
    timed = yield from attempt(_TIMEOUT_SQL)
    sizes = yield from attempt(_sized(names))
    if sizes is not None:
        found = _sizes(sizes)
        read.add("sizes")
    elif names is not None and len(names) > 1:
        for name in names:
            one = yield from attempt(_sized([name]))
            if one is not None:
                found.update(_sizes(one))
                read.add("sizes")
    if timed:
        yield from attempt(_set_timeout(str(timed[0][0])))
    return found


def _set_timeout(value: str) -> str:
    """
    The statement that sets the session's lock timeout. `set_config()`
    returns a row, where `SET` returns none for the read to fetch.
    """
    return f"SELECT pg_catalog.set_config('lock_timeout', {_text(value)}, false)"


# Reads the session's lock timeout, then sets the size read's. The
# target list is evaluated in order, so the value read is the one before.
_TIMEOUT_SQL = (
    "SELECT pg_catalog.current_setting('lock_timeout'), "
    f"pg_catalog.set_config('lock_timeout', '{SIZE_LOCK_TIMEOUT}', false)"
)


def _sized(names: Optional[Sequence[str]]) -> str:
    """The size read, for the tables of the given names, or for all."""
    if names is None:
        return _SIZES_SQL
    listed = ", ".join(_text(name) for name in names)
    return f"{_SIZES_SQL}\n  AND lower(c.relname) IN ({listed})"


def _text(value: str) -> str:
    """
    A text literal spelled in hex, so no quote, backslash, or percent
    sign in the value reaches the statement.
    """
    return f"convert_from(decode('{value.encode().hex()}', 'hex'), 'UTF8')"


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
