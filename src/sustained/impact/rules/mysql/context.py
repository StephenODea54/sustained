"""
The InnoDB context read: `VERSION()`, the settings, and each table's
size, row format, FULLTEXT indexes, and instant row versions.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import (
    Callable,
    Dict,
    Generator,
    Mapping,
    Sequence,
    Set,
    Tuple,
)

from sustained.impact.context import (
    FLOORS,
    ContextPlan,
    EngineContext,
    Rows,
    TableStats,
    attempt,
)
from sustained.impact.rules import common

_SETTINGS_SQL = "SELECT VERSION(), @@foreign_key_checks, @@lock_wait_timeout"

_SYSTEM_SCHEMAS = "('mysql', 'information_schema', 'performance_schema', 'sys')"

# One row per base table outside the system schemas: its schema, its
# name, whether it is in the current database, the estimated rows, the
# bytes of its data and indexes, and its row format. The hint reads the
# figures from the storage engine instead of the cache MySQL keeps for
# a day by default; MariaDB reads the hint as a comment.
_SIZES_SQL = (
    "SELECT /*+ SET_VAR(information_schema_stats_expiry = 0) */ "
    "TABLE_SCHEMA, TABLE_NAME, TABLE_SCHEMA = DATABASE(), TABLE_ROWS, "
    "COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0), UPPER(ROW_FORMAT) "
    "FROM information_schema.TABLES "
    f"WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"
)

_FULLTEXT_SQL = (
    "SELECT DISTINCT TABLE_SCHEMA, TABLE_NAME FROM information_schema.STATISTICS "
    f"WHERE INDEX_TYPE = 'FULLTEXT' AND TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"
)

# MySQL 8.0.29 and later count the instant column changes of each table.
_ROW_VERSIONS_SQL = (
    "SELECT NAME, TOTAL_ROW_VERSIONS FROM information_schema.INNODB_TABLES "
    "WHERE TOTAL_ROW_VERSIONS > 0"
)


def server_version(text: str) -> Tuple[str, Tuple[int, ...]]:
    """
    The profile and version a `VERSION()` value names, such as
    ('mariadb', (11, 4, 13)) for `11.4.13-MariaDB-ubu2404`.
    """
    profile = "mariadb" if "mariadb" in text.lower() else "mysql"
    match = re.match(r"(\d+(?:\.\d+)*)", text.strip())
    if match is None:
        return profile, FLOORS[profile]
    version = tuple(int(part) for part in match.group(1).split("."))
    return profile, version


def context_plan() -> ContextPlan:
    """
    Reads the version, the settings, the table sizes, and the storage
    facts. A statement that fails leaves its facts out of `read`, and
    the rules assume the floor or the worst case for them.
    """
    profile, version = "mysql", FLOORS["mysql"]
    settings: Dict[str, str] = {}
    tables: Dict[str, TableStats] = {}
    read: Set[str] = set()
    rows = yield from attempt(_SETTINGS_SQL)
    if rows:
        text, checks, timeout = rows[0]
        profile, version = server_version(str(text))
        settings = {
            "foreign_key_checks": str(checks),
            "lock_wait_timeout": str(timeout),
        }
        read |= {"version", "settings"}
    sizes = yield from attempt(_SIZES_SQL)
    if sizes is not None:
        tables, current = _sizes(sizes)
        read.add("sizes")
        yield from _storage(tables, current, profile, version, read)
    return EngineContext(
        profile,
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(tables),
        read=frozenset(read),
    )


_StoragePlan = Generator[str, Rows, None]


def _storage(
    tables: Dict[str, TableStats],
    current: Mapping[str, str],
    profile: str,
    version: Tuple[int, ...],
    read: Set[str],
) -> _StoragePlan:
    """
    Adds each table's FULLTEXT indexes and instant row versions to
    `tables`, whose `schema.table` keys `current` maps each bare key to.
    """
    fulltext = yield from attempt(_FULLTEXT_SQL)
    if fulltext is not None:
        marked = {f"{schema}.{name}".lower() for schema, name in fulltext}
        _update(tables, current, lambda key, s: s._replace(fulltext=key in marked))
        read.add("fulltext")
    if profile != "mysql" or version < (8, 0, 29):
        return
    versions = yield from attempt(_ROW_VERSIONS_SQL)
    if versions is None:
        return
    # INNODB_TABLES names a table `schema/table`.
    counts = {
        str(name).replace("/", ".", 1).lower(): int(str(count))
        for name, count in versions
    }
    _update(tables, current, lambda key, s: s._replace(row_versions=counts.get(key, 0)))
    read.add("row_versions")


def _update(
    tables: Dict[str, TableStats],
    current: Mapping[str, str],
    change: Callable[[str, TableStats], TableStats],
) -> None:
    """Replaces each table's stats, under its full key and its bare key."""
    for key in [k for k in tables if "." in k]:
        tables[key] = change(key, tables[key])
    for bare, key in current.items():
        tables[bare] = tables[key]


def _sizes(
    rows: Sequence[Sequence[object]],
) -> Tuple[Dict[str, TableStats], Dict[str, str]]:
    """
    Each table's stats, keyed `schema.table`, and also by the bare name
    when the table is in the current database, with the full key each
    bare key stands for.
    """
    tables: Dict[str, TableStats] = {}
    current: Dict[str, str] = {}
    for schema, name, here, count, size, row_format in rows:
        stats = TableStats(
            None if count is None else int(str(count)),
            int(str(size)),
            None if row_format is None else str(row_format),
        )
        bare = bool(here and int(str(here)))
        key = common.add_stats(tables, str(schema), str(name), bare, stats)
        if bare:
            current[str(name).lower()] = key
    return tables, current
