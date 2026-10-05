"""
Whether a lock timeout covers each statement of a run.

`sets_a_timeout()` reads a timeout value, and `TimeoutScope` follows
the timeout statements of a run in run order. The text guard
`no_lock_without_timeout` uses them without the table state of
`sustained.impact.state`.
"""

from __future__ import annotations

import re
from typing import Optional

# A timeout of zero, however spelled, turns the timeout off, and so
# does DEFAULT, which falls back to the server's setting.
_NO_TIMEOUT_RE = re.compile(r"(0+(\.0*)?\s*(us|ms|s|min|h|d)?|default)", re.IGNORECASE)

_UNSET = object()


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
        # The session setting as the migration began, which a ROLLBACK
        # goes back to.
        self._session_at_start = False

    def enter(self, migration_id: Optional[str]) -> None:
        """Moves to a statement of the given migration."""
        if migration_id != self._migration:
            # A new migration ends the LOCAL setting of the one before
            # it, whose commit dropped the setting with it.
            self._migration = migration_id
            self.local = False
            self._session_at_start = self.session

    def set(self, scope: str, transactional: bool, enabled: bool = True) -> None:
        """
        Records a timeout statement of the given scope. A session
        setting replaces a LOCAL one for the rest of the transaction.
        """
        if scope != "local":
            self.session = enabled
            self.local = False
        elif transactional:
            self.local = enabled

    def reset(self) -> None:
        """
        Records a `RESET` of the timeout. It goes back to the value the
        connection started with, which the run does not know, so no
        timeout counts as set.
        """
        self.session = False
        self.local = False

    def rollback(self) -> None:
        """
        Records a `ROLLBACK`, which undoes the timeout statements since
        the transaction began. The migration's first statement is the
        latest point the transaction can have begun, so a session
        timeout covers the statements after the ROLLBACK only when it
        was set both before the migration and at the ROLLBACK.
        """
        self.session = self.session and self._session_at_start
        self.local = False

    @property
    def covered(self) -> bool:
        return self.session or self.local
