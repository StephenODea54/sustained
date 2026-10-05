"""
The PostgreSQL context read: the version, the `TimeZone` and
`lock_timeout` settings, each table's size, the partitions of each
partitioned table, the columns each table's indexes use, the type of
each array column, and the types that are domains with a constraint.

`pg_total_relation_size()` takes ACCESS SHARE on each table it sizes,
which waits behind a session with ACCESS EXCLUSIVE on it. The size read
runs under `SIZE_LOCK_TIMEOUT`, and the setting read before it is put
back after it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    Collection,
    Dict,
    Generator,
    List,
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
    Relation,
    Rows,
    TableStats,
    attempt,
)
from sustained.impact.rules import common

_SETTINGS_SQL = (
    "SELECT current_setting('server_version_num'), "
    "current_setting('TimeZone'), current_setting('lock_timeout')"
)

# The filter that leaves out the system schemas, for `n.nspname`.
SYSTEM_SCHEMAS = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname !~ '^pg_(toast|temp_)'"
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
_SIZES_SQL = f"""SELECT n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid),
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
  AND {SYSTEM_SCHEMAS}"""


# How long the size read waits for each lock it takes.
SIZE_LOCK_TIMEOUT = "1s"


# One row per partitioned table and per partition: its oid, schema, and
# name, whether an unqualified name finds it, whether it is partitioned,
# the oid of the table it is a partition of, and the oid of its DEFAULT
# partition. The read takes no lock on any table.
_PARTITIONS_SQL = f"""SELECT c.oid, n.nspname, c.relname,
  pg_catalog.pg_table_is_visible(c.oid), c.relkind = 'p', i.inhparent,
  nullif(p.partdefid, 0)
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_inherits i ON i.inhrelid = c.oid AND c.relispartition
LEFT JOIN pg_catalog.pg_partitioned_table p ON p.partrelid = c.oid
WHERE (c.relkind = 'p' OR c.relispartition) AND {SYSTEM_SCHEMAS}"""

# One row per column an index uses, as a key column or inside an
# expression or predicate, with the collation the column is declared
# with: its table's schema and name, whether an unqualified name finds
# the table, the column, and the collation's name.
_INDEXED_SQL = f"""SELECT n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid),
  a.attname, co.collname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a
  ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation
WHERE c.relkind IN ('r', 'p', 'm') AND {SYSTEM_SCHEMAS}
  AND EXISTS (
    SELECT 1 FROM pg_catalog.pg_index x
    WHERE x.indrelid = c.oid AND (
      a.attnum = ANY (x.indkey)
      OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_depend d
        WHERE d.classid = 'pg_catalog.pg_class'::pg_catalog.regclass
          AND d.objid = x.indexrelid
          AND d.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass
          AND d.refobjid = c.oid AND d.refobjsubid = a.attnum)))"""

# One row per array column: its table's schema and name, whether an
# unqualified name finds the table, the column, and its type with the
# length of its elements.
_ARRAYS_SQL = f"""SELECT n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid),
  a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod)
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a
  ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
JOIN pg_catalog.pg_type y ON y.oid = a.atttypid
WHERE c.relkind IN ('r', 'p', 'm') AND {SYSTEM_SCHEMAS} AND y.typcategory = 'A'"""

# One row per type outside the system schemas, other than an array type
# and the row type of a table: its oid, schema, and name, whether an
# unqualified name finds it, the oid of the type a domain is over, and
# whether it is a domain with a NOT NULL or a CHECK of its own.
_TYPES_SQL = f"""SELECT t.oid, n.nspname, t.typname, pg_catalog.pg_type_is_visible(t.oid),
  nullif(t.typbasetype, 0),
  t.typtype = 'd' AND (t.typnotnull OR EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.contypid = t.oid))
FROM pg_catalog.pg_type t
JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE {SYSTEM_SCHEMAS} AND t.typcategory <> 'A'
  AND (t.typtype <> 'c' OR EXISTS (
    SELECT 1 FROM pg_catalog.pg_class r WHERE r.oid = t.typrelid AND r.relkind = 'c'))"""


def server_version(number: str) -> Tuple[int, ...]:
    """A `server_version_num` value as a version, such as (16, 4)."""
    value = int(number)
    return (value // 10000, value % 10000)


def context_plan(
    exact_counts: bool = False, tables: Optional[Collection[str]] = None
) -> ContextPlan:
    """
    Reads the version, the settings, the table sizes, the partitions,
    the indexed columns, the array columns, and the types. A statement
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
    relations: Dict[str, Relation] = {}
    partitions = yield from attempt(_PARTITIONS_SQL)
    if partitions is not None:
        _partitions(relations, partitions)
        read.add("partitions")
    indexed = yield from attempt(_INDEXED_SQL)
    if indexed is not None:
        _columns(relations, indexed, "indexed")
        read.add("indexes")
    arrays = yield from attempt(_ARRAYS_SQL)
    if arrays is not None:
        _columns(relations, arrays, "arrays")
        read.add("arrays")
    types: Mapping[str, bool] = {}
    rows = yield from attempt(_TYPES_SQL)
    if rows is not None:
        types = _types(rows)
        read.add("types")
    return EngineContext(
        "postgres",
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(found),
        read=frozenset(read),
        relations=MappingProxyType(relations),
        types=MappingProxyType(types),
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


def _spelled(schema: str, name: str, visible: bool) -> str:
    """A table's name as a statement would write it."""
    return name if visible else f"{schema}.{name}"


def _partitions(
    relations: Dict[str, Relation], rows: Sequence[Sequence[object]]
) -> None:
    """Each partitioned table and partition, with its parent and partitions."""
    names: Dict[int, str] = {}
    for oid, schema, name, visible, *_ in rows:
        names[int(str(oid))] = _spelled(str(schema), str(name), bool(visible))
    children: Dict[int, List[str]] = {}
    for oid, _, _, _, _, parent, _ in rows:
        if parent is not None:
            children.setdefault(int(str(parent)), []).append(names[int(str(oid))])
    for oid, schema, name, visible, partitioned, parent, default in rows:
        key = int(str(oid))
        relation = Relation(
            partitioned=bool(partitioned),
            parent=None if parent is None else names.get(int(str(parent))),
            default=None if default is None else names.get(int(str(default))),
            partitions=tuple(sorted(children.get(key, ()))),
        )
        for found in common.keys(str(schema), str(name), bool(visible)):
            relations[found] = relation


def _columns(
    relations: Dict[str, Relation], rows: Sequence[Sequence[object]], field: str
) -> None:
    """
    Adds a fact about each column to its table's facts, under `field`:
    `indexed` for the collation of each indexed column, and `arrays`
    for the type of each array column.
    """
    columns: Dict[Tuple[str, ...], Dict[str, Optional[str]]] = {}
    for schema, name, visible, column, value in rows:
        keys = common.keys(str(schema), str(name), bool(visible))
        found = columns.setdefault(keys, {})
        found[str(column).lower()] = None if value is None else str(value)
    for keys, values in columns.items():
        relation = relations.get(keys[0], Relation())
        facts = MappingProxyType(values)
        if field == "indexed":
            relation = relation._replace(indexed=facts)
        else:
            relation = relation._replace(
                arrays=MappingProxyType({k: str(v) for k, v in values.items()})
            )
        for key in keys:
            relations[key] = relation


def _types(rows: Sequence[Sequence[object]]) -> Mapping[str, bool]:
    """
    Each type's name, mapped to whether it is a domain with a constraint
    of its own or on a domain it is over.
    """
    own: Dict[int, bool] = {}
    bases: Dict[int, Optional[int]] = {}
    for oid, _, _, _, base, checked in rows:
        own[int(str(oid))] = bool(checked)
        bases[int(str(oid))] = None if base is None else int(str(base))

    def constrained(oid: int) -> bool:
        seen: Set[int] = set()
        current: Optional[int] = oid
        while current is not None and current in own and current not in seen:
            if own[current]:
                return True
            seen.add(current)
            current = bases[current]
        return False

    types: Dict[str, bool] = {}
    for oid, schema, name, visible, _, _ in rows:
        for key in common.keys(str(schema), str(name), bool(visible)):
            types[key] = constrained(int(str(oid)))
    return types
