"""
The state both migrators hold, which the core's generators read and set.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, ClassVar, List, Optional, Sequence, Tuple

from sustained.dialects import Dialects
from sustained.migrations.migration import Callbacks, Migration, checked_unique_ids
from sustained.migrations.tracking import insert_sql, update_sql

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.guards import Guard
    from sustained.schema import TableOptions


class MigratorBase:
    """
    What a migrator holds apart from its connection: the registered
    migrations, the tracking and rehearsal tables, the dialect and its
    compiler, the guards and callbacks, and the flags a run sets.

    Migrator and AsyncMigrator add the connection or the adapter and the
    loop that answers the core's requests on it. The core is handed the
    migrator itself, so it reads every attribute here at the moment it
    needs it: a caller that swaps the compiler or the dialect on a live
    migrator is seen by the next run.
    """

    # The transaction context manager a caller opens around this
    # migrator, as the core's refusals name it.
    _block: ClassVar[str] = "transaction()"

    def __init__(
        self,
        migrations: List[Migration],
        table: str,
        dialect: Dialects,
        tracking_table_options: Optional["TableOptions"],
        rehearsal_table: str,
        guards: Optional[Sequence["Guard"]],
        callbacks: Optional[Callbacks],
    ) -> None:
        checked_unique_ids(migrations)
        self._guards = list(guards or [])
        self._callbacks = callbacks or Callbacks()
        self._migrations = list(migrations)
        self._table = table
        self._rehearsal_table = rehearsal_table
        self._dialect = dialect
        self._compiler: "Compiler" = Dialects.get_compiler(dialect)
        self._tracking_table_options = tracking_table_options
        self._tracking_ready = False
        self._rehearsal_ready = False
        self._rehearsing = False
        # The Tracer of a rehearsal run with trace=True, while it runs.
        self._tracer: Any = None

    @property
    def dialect(self) -> Dialects:
        """The dialect this migrator compiles for."""
        return self._dialect

    def _warn_sync_deprecated(self) -> None:
        """
        The warning sync() on either migrator gives before it calls
        up(models=[...]). The stack level names the caller of sync().
        """
        warnings.warn(
            f"{type(self).__name__}.sync() is deprecated and will be removed "
            "in 3.0. Call up(models=[...]) instead.",
            DeprecationWarning,
            stacklevel=3,
        )

    @property
    def compiler(self) -> "Compiler":
        """The compiler that renders this migrator's ddl steps."""
        return self._compiler

    def _table_sql(self) -> str:
        return self._compiler.quote_identifier(self._table)

    def _table_ddl_sql(self) -> str:
        return self._compiler.quote_ddl_identifier(self._table)

    def _rehearsal_table_sql(self) -> str:
        return self._compiler.quote_identifier(self._rehearsal_table)

    def _rehearsal_table_ddl_sql(self) -> str:
        return self._compiler.quote_ddl_identifier(self._rehearsal_table)

    def _own_tables(self) -> Tuple[str, ...]:
        """
        The tables Sustained keeps for itself. A diff against the models
        leaves them alone, and a rehearsal snapshot drops them, so its own
        bookkeeping never reads as drift or as an object left behind.
        """
        return (self._table, self._rehearsal_table)

    def _insert_sql(self) -> str:
        return insert_sql(self._compiler, self._table_sql())

    def _update_sql(self) -> str:
        return update_sql(self._compiler, self._table_sql())

    def _versioned(self) -> List[Migration]:
        return [m for m in self._migrations if not m.repeatable]

    def _repeatables(self) -> List[Migration]:
        return [m for m in self._migrations if m.repeatable]
