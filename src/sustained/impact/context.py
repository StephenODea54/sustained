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
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, FrozenSet, Mapping, NamedTuple, Optional, Tuple

if TYPE_CHECKING:
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


def assumed(profile: str) -> EngineContext:
    """The context the rules assume without a server: the support floor."""
    return EngineContext(profile, FLOORS[profile])


def version_text(version: Tuple[int, ...]) -> str:
    """A version as people write it, such as `12` or `8.0.19`."""
    return ".".join(str(part) for part in version)
