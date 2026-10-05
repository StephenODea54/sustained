"""
The InnoDB context read: `VERSION()`, the settings, and each table's
size, row format, default collation, FULLTEXT indexes, and instant row
versions.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import (
    Callable,
    Collection,
    Dict,
    Generator,
    Mapping,
    Optional,
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

SYSTEM_SCHEMAS = "('mysql', 'information_schema', 'performance_schema', 'sys')"

# One row per base table outside the system schemas: its schema, its
# name, whether it is in the current database, the estimated rows, the
# bytes of its data and indexes, its row format, and its default
# collation. The hint reads the figures from the storage engine instead
# of the cache MySQL keeps for a day by default; MariaDB reads the hint
# as a comment.
_SIZES_SQL = (
    "SELECT /*+ SET_VAR(information_schema_stats_expiry = 0) */ "
    "TABLE_SCHEMA, TABLE_NAME, TABLE_SCHEMA = DATABASE(), TABLE_ROWS, "
    "COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0), UPPER(ROW_FORMAT), "
    "LOWER(TABLE_COLLATION) "
    "FROM information_schema.TABLES "
    f"WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS}"
)

_FULLTEXT_SQL = (
    "SELECT DISTINCT TABLE_SCHEMA, TABLE_NAME FROM information_schema.STATISTICS "
    f"WHERE INDEX_TYPE = 'FULLTEXT' AND TABLE_SCHEMA NOT IN {SYSTEM_SCHEMAS}"
)

# MySQL 8.0.29 and later count the instant column changes of each table.
_ROW_VERSIONS_SQL = (
    "SELECT NAME, TOTAL_ROW_VERSIONS FROM information_schema.INNODB_TABLES "
    "WHERE TOTAL_ROW_VERSIONS > 0"
)

# The characters MySQL writes as they are in a file name, and so in
# INNODB_TABLES.NAME.
_FILE_NAME_SAFE = frozenset(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
)

# The code points MySQL may write as `@` and two characters from a table
# of its own, such as `@1i` for `ö`, instead of four hex digits.
_FILE_NAME_TABLES = (
    (0x00C0, 0x05FF),
    (0x1E00, 0x1FFF),
    (0x2160, 0x217F),
    (0x24B0, 0x24EF),
    (0xFF20, 0xFF5F),
)

# A partition's suffix on its table's name in INNODB_TABLES, such as
# `#p#p0` or `#p#p0#sp#s0`.
_PARTITION_RE = re.compile(r"#p#.*$", re.IGNORECASE)


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


def context_plan(
    exact_counts: bool = False, tables: Optional[Collection[str]] = None
) -> ContextPlan:
    """
    Reads the version, the settings, the table sizes, and the storage
    facts. A statement that fails leaves its facts out of `read`, and
    the rules assume the floor or the worst case for them. The sizes are
    the estimates the server keeps, so `exact_counts` changes nothing
    here. With `tables`, lower case table names, only the tables of
    those names are read.
    """
    profile, version = "mysql", FLOORS["mysql"]
    settings: Dict[str, str] = {}
    found: Dict[str, TableStats] = {}
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
    sizes = yield from attempt(_sized(tables))
    if sizes is not None:
        found, current, files = _sizes(sizes)
        read.add("sizes")
        yield from _storage(found, current, files, profile, version, read)
    return EngineContext(
        profile,
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(found),
        read=frozenset(read),
    )


def _sized(tables: Optional[Collection[str]]) -> str:
    """
    The size read, for the tables of the given names, or for all. Each
    name is a hex literal, so no quote, backslash, or percent sign in it
    reaches the statement.
    """
    if tables is None:
        return _SIZES_SQL
    listed = ", ".join(f"X'{name.encode().hex()}'" for name in sorted(set(tables)))
    return f"{_SIZES_SQL} AND LOWER(TABLE_NAME) IN ({listed or 'NULL'})"


_StoragePlan = Generator[str, Rows, None]


def _storage(
    tables: Dict[str, TableStats],
    current: Mapping[str, str],
    files: Mapping[str, Optional[str]],
    profile: str,
    version: Tuple[int, ...],
    read: Set[str],
) -> _StoragePlan:
    """
    Adds each table's FULLTEXT indexes and instant row versions to
    `tables`, whose `schema.table` keys `current` maps each bare key to.
    `files` maps each `schema.table` key to the name INNODB_TABLES gives
    the table, or None when the rules cannot write that name.
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
    # INNODB_TABLES names a table `schema/table` in the encoding MySQL
    # gives file names, and names each partition of a partitioned table
    # on its own. A partitioned table counts the most any partition has
    # used.
    counts: Dict[str, int] = {}
    for name, count in versions:
        key = _PARTITION_RE.sub("", str(name)).lower()
        counts[key] = max(counts.get(key, 0), int(str(count)))

    def row_versions(key: str, stats: TableStats) -> TableStats:
        file = files.get(key)
        found = None if file is None else counts.get(file, 0)
        return stats._replace(row_versions=found)

    _update(tables, current, row_versions)
    read.add("row_versions")


def file_name(name: str) -> Optional[str]:
    """
    A schema or table name as INNODB_TABLES spells it, in lower case:
    ASCII letters, digits, and `_` as they are, and any other character
    as `@` and its code point in four hex digits, such as `a@002db` for
    `a-b`. None for a name with a character MySQL may write from a table
    of its own, such as `ö`, which the rules do not follow.
    """
    found = []
    for char in name:
        point = ord(char)
        if char in _FILE_NAME_SAFE:
            found.append(char)
        elif point > 0xFFFF or any(a <= point <= b for a, b in _FILE_NAME_TABLES):
            return None
        else:
            found.append(f"@{point:04x}")
    return "".join(found).lower()


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
) -> Tuple[Dict[str, TableStats], Dict[str, str], Dict[str, Optional[str]]]:
    """
    Each table's stats, keyed `schema.table`, and also by the bare name
    when the table is in the current database, with the full key each
    bare key stands for, and the name INNODB_TABLES gives each full key.
    """
    tables: Dict[str, TableStats] = {}
    current: Dict[str, str] = {}
    files: Dict[str, Optional[str]] = {}
    for schema, name, here, count, size, row_format, collation in rows:
        stats = TableStats(
            None if count is None else int(str(count)),
            int(str(size)),
            None if row_format is None else str(row_format),
            collation=None if collation is None else str(collation),
        )
        bare = bool(here and int(str(here)))
        key = common.add_stats(tables, str(schema), str(name), bare, stats)
        if bare:
            current[str(name).lower()] = key
        parts = file_name(str(schema)), file_name(str(name))
        files[key] = None if None in parts else f"{parts[0]}/{parts[1]}"
    return tables, current, files
