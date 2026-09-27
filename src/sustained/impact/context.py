"""
What the rules know about the server a run targets.

An `EngineContext` holds the rule profile, the server version, the
edition, the settings the rules read, per-table size estimates, and the
schema. The schema answers what a statement leaves unsaid: a column's
current type, the table an index is on, and the tables at either end
of a foreign key. `read` names the facts that were actually read from a server;
a fact outside it was assumed, and a rule that leans on it lowers its
confidence.

Without a live connection the rules assume the profile's support floor,
the oldest version `support.json` claims, which is the worst supported
case. `assumed()` builds that context. A test keeps `FLOORS` in step
with `support.json`.

`read_context()` reads a context from a blocking connection, and
`async_read_context()` from an async adapter. Both read the schema as
`introspect_schema()` does, then run the profile's catalog read plan.
With `statements`, the plan reads the sizes of the tables
`named_tables()` finds in them, instead of every table. A statement
that fails, for example on a missing privilege, leaves its facts out of
`read`, and the read goes on without them.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Collection,
    FrozenSet,
    Generator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from sustained.types import Connection, RowValue

if TYPE_CHECKING:
    from sustained.aio import AsyncAdapter
    from sustained.dialects import Dialects
    from sustained.impact.rules import Profile
    from sustained.introspect.model import IntrospectedTable, Snapshot

# The oldest server version each profile supports, from support.json.
FLOORS: Mapping[str, Tuple[int, ...]] = MappingProxyType(
    {
        "postgres": (12,),
        "mysql": (8, 0, 19),
        "mariadb": (10, 6),
        "sqlite": (3, 35),
        "duckdb": (1, 0),
        "mssql": (11,),
    }
)


class TableStats(NamedTuple):
    """
    One table's size, as estimates, and the storage facts the InnoDB
    rules read; None where it was not read. `row_format` is the InnoDB
    row format in upper case, such as `DYNAMIC` or `COMPRESSED`.
    `row_versions` counts the instant column changes MySQL has recorded
    since the table was last rebuilt, and `fulltext` says whether the
    table has a FULLTEXT index. On SQL Server, `heap` says whether the
    table has no clustered index, and `clustered` names the one it has.
    `collation` is the table's default collation on MySQL and MariaDB,
    in lower case, which a restated text column takes unless the
    statement names another.
    """

    rows: Optional[int] = None
    bytes: Optional[int] = None
    row_format: Optional[str] = None
    row_versions: Optional[int] = None
    fulltext: Optional[bool] = None
    heap: Optional[bool] = None
    clustered: Optional[str] = None
    collation: Optional[str] = None


class Relation(NamedTuple):
    """
    The PostgreSQL catalog facts about one table that its size does not
    give. `partitioned` says whether it is a partitioned table, `parent`
    names the partitioned table it is a partition of, `default` names
    its DEFAULT partition, and `partitions` names its partitions, one
    level down. Each name is the bare name when the search path finds
    the table under it, and `schema.table` otherwise. `indexed` maps
    each column an index uses, in lower case, to the collation the
    column is declared with, or None for a column that has none.
    `arrays` maps each array column, in lower case, to its type as
    format_type() writes it, such as `character varying(10)[]`, which
    the schema read reports as `ARRAY`.
    """

    partitioned: bool = False
    parent: Optional[str] = None
    default: Optional[str] = None
    partitions: Tuple[str, ...] = ()
    indexed: Mapping[str, Optional[str]] = MappingProxyType({})
    arrays: Mapping[str, str] = MappingProxyType({})


class EngineContext(NamedTuple):
    """
    The server facts the rules read. `tables` maps a lower case table
    name to its stats. `read` names what came from the server, such as
    `version` or `sizes`; an empty set means everything was assumed.
    `relations` maps a lower case table name to its PostgreSQL catalog
    facts, and `types` maps a lower case type name to whether the type
    is a domain with a constraint, for the types outside the system
    schemas.
    """

    profile: str
    version: Tuple[int, ...]
    edition: Optional[str] = None
    settings: Mapping[str, str] = MappingProxyType({})
    tables: Mapping[str, TableStats] = MappingProxyType({})
    schema: Optional["Snapshot"] = None
    read: FrozenSet[str] = frozenset()
    relations: Mapping[str, Relation] = MappingProxyType({})
    types: Mapping[str, bool] = MappingProxyType({})

    def stats(self, table: str) -> TableStats:
        """The table's stats, or unknown stats when none were read."""
        return self.tables.get(table.lower(), TableStats())

    def table(self, name: str) -> Optional["IntrospectedTable"]:
        """
        The table as the schema read reports it, or None when the schema
        was not read or does not hold the table. A dotted name finds the
        table by its last part, since the read covers one schema.
        """
        if self.schema is None:
            return None
        found = self.schema.get(name.lower())
        if found is None and "." in name:
            found = self.schema.get(name.rsplit(".", 1)[-1].lower())
        return found

    def column_type(self, table: str, column: str) -> Optional[str]:
        """
        A column's current type as the schema read reports it, or None
        when the schema was not read or does not hold the column.
        """
        found = self.table(table)
        if found is None:
            return None
        for name, spec in found.columns.items():
            if name.lower() == column.lower():
                return spec.raw_type
        return None

    def index_table(self, index: str) -> Optional[str]:
        """
        The table an index is on, as the schema read reports it, or None
        when the schema was not read or holds no index of that name.
        """
        if self.schema is None:
            return None
        key = index.rsplit(".", 1)[-1].lower()
        for name, table in self.schema.items():
            if key in table.indexes:
                return table.name or name
        return None

    def references(
        self, table: str, columns: Optional[Sequence[str]] = None
    ) -> Tuple[str, ...]:
        """
        The tables the table's foreign keys point at, in the order the
        schema read lists the keys. With `columns`, only the keys that
        use one of those columns count.
        """
        found = self.table(table)
        if found is None:
            return ()
        wanted = None if columns is None else {c.lower() for c in columns}
        targets: List[str] = []
        for key in found.foreign_keys.values():
            if wanted is not None and not wanted & {c.lower() for c in key.columns}:
                continue
            target = key.target_table
            if key.target_schema:
                target = f"{key.target_schema}.{target}"
            if target != "?" and target not in targets:
                targets.append(target)
        return tuple(targets)

    def foreign_key_target(self, table: str, name: str) -> Optional[str]:
        """
        The table a foreign key of the table points at, found by the
        key's name, or None when the schema read holds no such key.
        """
        found = self.table(table)
        if found is None:
            return None
        for key_name, key in found.foreign_keys.items():
            if (key.name or key_name).lower() == name.lower():
                if key.target_table == "?":
                    return None
                if key.target_schema:
                    return f"{key.target_schema}.{key.target_table}"
                return key.target_table
        return None

    def referenced_by(
        self, table: str, columns: Optional[Sequence[str]] = None
    ) -> Tuple[str, ...]:
        """
        The tables in the schema read whose foreign keys point at the
        table. With `columns`, only the keys that point at one of those
        columns count, and so does a key whose target columns the read
        did not report.
        """
        if self.schema is None or self.table(table) is None:
            return ()
        bare = table.rsplit(".", 1)[-1].lower()
        wanted = None if columns is None else {c.lower() for c in columns}
        found: List[str] = []
        for name, other in self.schema.items():
            for key in other.foreign_keys.values():
                if key.target_schema or key.target_table.lower() != bare:
                    continue
                targets = {c.lower() for c in key.target_columns}
                if wanted is not None and targets and not wanted & targets:
                    continue
                spelled = other.name or name
                if spelled.lower() != bare and spelled not in found:
                    found.append(spelled)
        return tuple(found)


ContextPlan = Generator[str, List[Sequence[RowValue]], "EngineContext"]
"""
A profile's catalog read: it yields one statement at a time, receives
its rows, has a failed statement's error thrown in, and returns the
context it read, without the schema.
"""

Rows = List[Sequence[RowValue]]


def attempt(statement: str) -> Generator[str, Rows, Optional[Rows]]:
    """
    One step of a read plan: the statement's rows, or None when the
    statement failed, so the plan goes on without its facts.
    """
    try:
        rows = yield statement
    except Exception:
        return None
    return rows


def read_context(
    connection: Connection,
    dialect: "Dialects",
    exact_counts: bool = False,
    statements: Optional[Sequence[str]] = None,
) -> EngineContext:
    """
    The server facts the dialect's rules read, from a blocking
    connection: the schema of the connection's own schema, the version,
    the settings, and the table sizes. Raises ValueError for a dialect
    that has no impact rules yet.

    With `exact_counts`, the SQLite read counts the rows of each table
    `sqlite_stat1` has no row count for, and reads each table's bytes
    from `dbstat`. The other profiles read the estimates the server
    keeps either way.

    With `statements`, the sizes are read only for the tables
    `named_tables()` finds in them; without, for every table.
    """
    from sustained.introspect.runner import introspect_schema, run_plan

    _profile(dialect)
    try:
        schema: Optional["Snapshot"] = introspect_schema(connection, dialect)
    except Exception:
        schema = None
    tables = _named(statements, dialect, schema)
    context = run_plan(connection, dialect, _plan(dialect, exact_counts, tables))
    return context if schema is None else _with_schema(context, schema)


async def async_read_context(
    adapter: "AsyncAdapter",
    dialect: "Dialects",
    exact_counts: bool = False,
    statements: Optional[Sequence[str]] = None,
) -> EngineContext:
    """What read_context() reads, through an async adapter."""
    from sustained.introspect.runner import async_introspect_schema, async_run_plan

    _profile(dialect)
    try:
        schema: Optional["Snapshot"] = await async_introspect_schema(adapter, dialect)
    except Exception:
        schema = None
    tables = _named(statements, dialect, schema)
    context = await async_run_plan(
        adapter, dialect, _plan(dialect, exact_counts, tables)
    )
    return context if schema is None else _with_schema(context, schema)


def named_tables(
    statements: Sequence[str],
    dialect: "Dialects",
    schema: Optional["Snapshot"] = None,
) -> FrozenSet[str]:
    """
    The tables the statements act on, as the analysis finds them with
    the schema and no other server fact: each table a statement names,
    and each table the schema answers for it, such as the table of an
    index it drops or the other end of a foreign key. Each name is the
    last part of a dotted name, in lower case, since a size read
    matches a table by its name alone. Raises ValueError for a dialect
    that has no impact rules yet.
    """
    from sustained.impact.analyzer import analyze

    profile = _profile(dialect)
    context = EngineContext(profile.name, FLOORS[profile.name], schema=schema)
    report = analyze(statements, dialect, context)
    return frozenset(
        table.table.rsplit(".", 1)[-1].lower()
        for statement in report.statements
        for table in statement.tables
    )


def _named(
    statements: Optional[Sequence[str]],
    dialect: "Dialects",
    schema: Optional["Snapshot"],
) -> Optional[FrozenSet[str]]:
    if statements is None:
        return None
    return named_tables(statements, dialect, schema)


def _profile(dialect: "Dialects") -> "Profile":
    from sustained.impact.rules import engine, profile_for

    profile = profile_for(dialect)
    if profile is None:
        raise ValueError(f"Impact analysis does not cover {engine(dialect)} yet.")
    return profile


def _plan(
    dialect: "Dialects", exact_counts: bool, tables: Optional[Collection[str]]
) -> ContextPlan:
    return _profile(dialect).context_plan(exact_counts, tables)


def _with_schema(context: EngineContext, schema: "Snapshot") -> EngineContext:
    return context._replace(schema=schema, read=context.read | {"schema"})


def assumed(profile: str) -> EngineContext:
    """The context the rules assume without a server: the support floor."""
    return EngineContext(profile, FLOORS[profile])


def version_text(version: Tuple[int, ...]) -> str:
    """A version as people write it, such as `12` or `8.0.19`."""
    return ".".join(str(part) for part in version)
