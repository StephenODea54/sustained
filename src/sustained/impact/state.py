"""
What the analysis carries from one statement of a run to the next.

The rules read each statement against what came before it in the run:

- A table created earlier in the run is empty, and nobody else reads
  it yet, so work on it blocks nothing.
- A table renamed earlier in the run is the live table under a new
  name, so its size is the size of the table it was.
- An index created earlier in the run names its table, which a later
  `DROP INDEX` leaves unsaid.
- A check of the form `column IS NOT NULL` that the run added, and
  validated or added without `NOT VALID`, proves the column holds no
  NULL, so a later `SET NOT NULL` on Postgres reads no rows.
- A lock timeout set earlier covers the statements after it, as far as
  its scope reaches (`TimeoutScope`).
- Session settings, such as MySQL's `foreign_key_checks`, change what
  later statements do. A `SET GLOBAL` or `SET PERSIST` leaves the
  session's own value as it was, and so does a user variable, so none
  of them counts.

Names compare case-insensitively, as the recognizer's docstring asks.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Optional, Set, Tuple

from sustained.impact.model import Action, ParsedStatement

# A timeout of zero, however spelled, turns the timeout off, and so
# does DEFAULT, which falls back to the server's setting.
_NO_TIMEOUT_RE = re.compile(r"(0+(\.0*)?\s*(us|ms|s|min|h|d)?|default)", re.IGNORECASE)

_UNSET = object()

# The SET scopes that leave the session's own value unchanged.
_NOT_THE_SESSION = frozenset({"global", "persist", "persist_only", "user"})


def sets_a_timeout(value: str) -> bool:
    """Whether a lock timeout value waits for a bounded time."""
    return not _NO_TIMEOUT_RE.fullmatch(value.strip())


class TimeoutScope:
    """
    Whether a lock timeout covers the next statement of a run, read in
    run order.

    A session setting (`SET lock_timeout`, with or without SESSION)
    covers every statement after it in the run. A `SET LOCAL` setting
    dies at the commit that ends its migration, so it covers only the
    statements after it in that migration. A migration that runs outside
    a transaction has no transaction block to attach a LOCAL setting to,
    so Postgres ignores it there.

    Call `enter()` with each statement's migration before reading
    `covered` or calling `set()` for it.
    """

    def __init__(self) -> None:
        self.session = False
        self.local = False
        self._migration: object = _UNSET

    def enter(self, migration_id: Optional[str]) -> None:
        """Moves to a statement of the given migration."""
        if migration_id != self._migration:
            # A new migration ends the LOCAL setting of the one before
            # it, whose commit dropped the setting with it.
            self._migration = migration_id
            self.local = False

    def set(self, scope: str, transactional: bool, enabled: bool = True) -> None:
        """Records a timeout statement of the given scope."""
        if scope != "local":
            self.session = enabled
        elif transactional:
            self.local = enabled

    @property
    def covered(self) -> bool:
        return self.session or self.local


class RunState:
    """
    The facts a run has built up so far. `timeout_setting` is the lower
    case name of the engine's lock timeout setting, such as
    `lock_timeout`, and `bounded` says whether a value of it bounds the
    wait. `local_scope` is False on an engine where `SET LOCAL` means
    the session, as on MySQL.
    """

    def __init__(
        self,
        timeout_setting: Optional[str] = None,
        bounded: Callable[[str], bool] = sets_a_timeout,
        local_scope: bool = True,
    ) -> None:
        self.timeout_setting = timeout_setting
        self.bounded = bounded
        self.local_scope = local_scope
        self.created: Set[str] = set()
        self.renamed: Dict[str, str] = {}
        self.indexes: Dict[str, str] = {}
        # The checks of the form `column IS NOT NULL` the run added, by
        # table and check name: the column, and whether the check is valid.
        self.not_null_checks: Dict[str, Dict[str, Tuple[str, bool]]] = {}
        self.settings: Dict[str, str] = {}
        self.timeouts = TimeoutScope()

    def is_new(self, table: str) -> bool:
        """Whether the run created the table earlier."""
        return table.lower() in self.created

    def original(self, table: str) -> str:
        """The live name of a table the run may have renamed."""
        return self.renamed.get(table.lower(), table)

    def index_table(self, index: str) -> Optional[str]:
        """The table of an index the run created, or None."""
        return self.indexes.get(index.lower())

    def proves_not_null(self, table: str, column: str) -> bool:
        """
        Whether a valid check the run added proves the column holds no
        NULL.
        """
        checks = self.not_null_checks.get(table.lower(), {})
        return (column.lower(), True) in checks.values()

    def record(self, parsed: ParsedStatement, transactional: bool) -> None:
        """Takes in what the statement changes, after the rules read it."""
        kind = parsed.kind
        options = parsed.options
        if kind == "create_table" and parsed.table:
            self.created.add(parsed.table.lower())
        elif kind == "drop_table":
            for table in parsed.items("tables"):
                self.created.discard(str(table).lower())
        elif kind == "create_index" and parsed.table and options.get("name"):
            self.indexes[str(options["name"]).lower()] = parsed.table
        elif kind == "rename_table":
            for old, new in parsed.items("renames"):
                self.rename(str(old), str(new))
        elif kind == "alter_table" and parsed.table:
            for action in parsed.actions:
                self.record_checks(parsed.table, action)
                if action.kind == "rename_to":
                    self.rename(parsed.table, str(action.options["new"]))
        elif kind == "set":
            self.record_settings(parsed, transactional)

    def record_checks(self, table: str, action: Action) -> None:
        """Takes in the `IS NOT NULL` checks an ALTER TABLE action changes."""
        checks = self.not_null_checks.setdefault(table.lower(), {})
        name = str(action.options.get("name") or "").lower()
        if action.kind == "add_constraint" and action.options.get("not_null"):
            column = str(action.options["not_null"]).lower()
            checks[name] = (column, not action.options.get("not_valid"))
        elif action.kind == "validate_constraint" and name in checks:
            checks[name] = (checks[name][0], True)
        elif action.kind == "drop_constraint":
            checks.pop(name, None)
        elif action.kind == "drop_column" and action.column:
            dropped = action.column.lower()
            for check, (column, _) in list(checks.items()):
                if column == dropped:
                    del checks[check]

    def rename(self, old: str, new: str) -> None:
        # A rename keeps the old schema when the new name has none.
        if "." in old and "." not in new:
            new = f"{old.rsplit('.', 1)[0]}.{new}"
        if old.lower() in self.not_null_checks:
            self.not_null_checks[new.lower()] = self.not_null_checks.pop(old.lower())
        if old.lower() in self.created:
            self.created.discard(old.lower())
            self.created.add(new.lower())
        self.renamed[new.lower()] = self.original(old)

    def record_settings(self, parsed: ParsedStatement, transactional: bool) -> None:
        for scope, name, value in parsed.items("settings"):
            if scope in _NOT_THE_SESSION:
                continue
            if scope == "local" and not self.local_scope:
                scope = "session"
            self.settings[name] = value
            if name == self.timeout_setting:
                self.timeouts.set(scope, transactional, self.bounded(value))
