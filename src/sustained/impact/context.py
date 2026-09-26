"""
What the rules know about the server a run targets.

An `EngineContext` holds the rule profile, the server version, the
edition, the settings the rules read, per-table size estimates, and the
schema. `read` names the facts that were actually read from a server;
a fact outside it was assumed, and a rule that leans on it lowers its
confidence.

Without a live connection the rules assume the profile's support floor,
the oldest version `support.json` claims, which is the worst supported
case. `assumed()` builds that context. A test keeps `FLOORS` in step
with `support.json`.

`read_context()` reads a context from a blocking connection, and
`async_read_context()` from an async adapter. Both run the profile's
catalog read plan, then read the schema as `introspect_schema()` does.
A statement that fails, for example on a missing privilege, leaves its
facts out of `read`, and the read goes on without them.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
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
    from sustained.introspect.model import Snapshot

# The oldest server version each profile supports, from support.json.
FLOORS: Mapping[str, Tuple[int, ...]] = MappingProxyType(
    {
        "postgres": (12,),
    }
)


class TableStats(NamedTuple):
    """One table's size, as estimates; None where it was not read."""

    rows: Optional[int] = None
    bytes: Optional[int] = None


class EngineContext(NamedTuple):
    """
    The server facts the rules read. `tables` maps a lower case table
    name to its stats. `read` names what came from the server, such as
    `version` or `sizes`; an empty set means everything was assumed.
    """

    profile: str
    version: Tuple[int, ...]
    edition: Optional[str] = None
    settings: Mapping[str, str] = MappingProxyType({})
    tables: Mapping[str, TableStats] = MappingProxyType({})
    schema: Optional["Snapshot"] = None
    read: FrozenSet[str] = frozenset()

    def stats(self, table: str) -> TableStats:
        """The table's stats, or unknown stats when none were read."""
        return self.tables.get(table.lower(), TableStats())

    def column_type(self, table: str, column: str) -> Optional[str]:
        """
        A column's current type as the schema read reports it, or None
        when the schema was not read or does not hold the column.
        """
        if self.schema is None:
            return None
        found = self.schema.get(table.lower())
        if found is None and "." in table:
            found = self.schema.get(table.rsplit(".", 1)[-1].lower())
        if found is None:
            return None
        for name, spec in found.columns.items():
            if name.lower() == column.lower():
                return spec.raw_type
        return None


ContextPlan = Generator[str, List[Sequence[RowValue]], "EngineContext"]
"""
A profile's catalog read: it yields one statement at a time, receives
its rows, has a failed statement's error thrown in, and returns the
context it read, without the schema.
"""


def read_context(connection: Connection, dialect: "Dialects") -> EngineContext:
    """
    The server facts the dialect's rules read, from a blocking
    connection: the version, the settings, the table sizes, and the
    schema of the connection's own schema. Raises ValueError for a
    dialect that has no impact rules yet.
    """
    from sustained.introspect.runner import introspect_schema, run_plan

    context = run_plan(connection, dialect, _plan(dialect))
    try:
        schema = introspect_schema(connection, dialect)
    except Exception:
        return context
    return _with_schema(context, schema)


async def async_read_context(
    adapter: "AsyncAdapter", dialect: "Dialects"
) -> EngineContext:
    """What read_context() reads, through an async adapter."""
    from sustained.introspect.runner import async_introspect_schema, async_run_plan

    context = await async_run_plan(adapter, dialect, _plan(dialect))
    try:
        schema = await async_introspect_schema(adapter, dialect)
    except Exception:
        return context
    return _with_schema(context, schema)


def _plan(dialect: "Dialects") -> ContextPlan:
    from sustained.impact.rules import profile_for

    profile = profile_for(dialect)
    if profile is None:
        raise ValueError(f"Impact analysis does not cover {dialect.name} yet.")
    return profile.context_plan()


def _with_schema(context: EngineContext, schema: "Snapshot") -> EngineContext:
    return context._replace(schema=schema, read=context.read | {"schema"})


def assumed(profile: str) -> EngineContext:
    """The context the rules assume without a server: the support floor."""
    return EngineContext(profile, FLOORS[profile])


def version_text(version: Tuple[int, ...]) -> str:
    """A version as people write it, such as `12` or `8.0.19`."""
    return ".".join(str(part) for part in version)
