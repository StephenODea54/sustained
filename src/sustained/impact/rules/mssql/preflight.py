"""
The SQL Server live preflight: the table locks other sessions were
granted or are waiting for in this database, from `sys.dm_tran_locks`,
with each session's login, program, status, transaction age, and most
recent statement, and the user transactions of the sessions that are in
this database or have a transaction with work in it, from
`sys.dm_tran_database_transactions`, whatever the session's current
database. The dynamic management views need `VIEW SERVER STATE`, or
`VIEW SERVER PERFORMANCE STATE` on SQL Server 2022 and later, and
`VIEW DATABASE STATE` on Azure SQL Database; without it the reads fail
and are left out of `read`.

A planned lock waits behind a lock the compatibility matrix says it
conflicts with. The intent update modes `IU`, `SIU`, and `UIX`, which
the matrix in the documentation leaves out, count as conflicting with
`S`, `SIX`, and `X`, and `IU` counts as compatible with `IS` and `IX`.
"""

from __future__ import annotations

from typing import FrozenSet, List, Mapping, Sequence, Set

from sustained.impact.context import attempt
from sustained.impact.model import StatementImpact
from sustained.impact.preflight import (
    Blocker,
    Granted,
    LiveSession,
    Planned,
    Preflight,
    PreflightPlan,
    blockers,
    number,
    older,
    planned,
    seconds,
    text,
)
from sustained.impact.rules.mssql import locks

_EVERY_MODE = frozenset(
    {"Sch-S", "Sch-M", "IS", "IU", "IX", "S", "U", "SIU", "SIX", "UIX", "X", "BU"}
)

# The modes another session may have that each planned mode is
# compatible with.
_COMPATIBLE: Mapping[str, FrozenSet[str]] = {
    locks.SCH_S: _EVERY_MODE - {"Sch-M"},
    locks.IS: _EVERY_MODE - {"Sch-M", "X", "BU"},
    locks.IX: frozenset({"Sch-S", "IS", "IU", "IX"}),
    locks.S: frozenset({"Sch-S", "IS", "S", "U"}),
    locks.SIX: frozenset({"Sch-S", "IS"}),
    locks.X: frozenset({"Sch-S"}),
    locks.SCH_M: frozenset(),
}

# The session's transaction age and most recent statement, shared by
# both reads.
_SESSION_COLUMNS = """s.login_name, s.program_name, s.status,
  (SELECT DATEDIFF(SECOND, MIN(t.transaction_begin_time), GETDATE())
   FROM sys.dm_tran_session_transactions st
   JOIN sys.dm_tran_active_transactions t ON t.transaction_id = st.transaction_id
   WHERE st.session_id = s.session_id),
  q.text"""

# One row per table lock another session was granted or is waiting for
# in this database.
_LOCKS_SQL = f"""SELECT l.request_session_id,
  OBJECT_SCHEMA_NAME(l.resource_associated_entity_id, l.resource_database_id),
  OBJECT_NAME(l.resource_associated_entity_id, l.resource_database_id),
  CASE WHEN OBJECT_SCHEMA_NAME(l.resource_associated_entity_id,
    l.resource_database_id) = SCHEMA_NAME() THEN 1 ELSE 0 END,
  l.request_mode, CASE WHEN l.request_status = 'GRANT' THEN 1 ELSE 0 END,
  {_SESSION_COLUMNS}
FROM sys.dm_tran_locks l
LEFT JOIN sys.dm_exec_sessions s ON s.session_id = l.request_session_id
OUTER APPLY (SELECT TOP 1 c.most_recent_sql_handle FROM sys.dm_exec_connections c
             WHERE c.session_id = l.request_session_id) c
OUTER APPLY sys.dm_exec_sql_text(c.most_recent_sql_handle) q
WHERE l.resource_type = 'OBJECT'
  AND l.resource_database_id = DB_ID()
  AND l.request_session_id <> @@SPID"""

# One row per other session with a user transaction whose current
# database is this one, or whose transaction has work in this database.
_TRANSACTIONS_SQL = f"""SELECT s.session_id, {_SESSION_COLUMNS}
FROM sys.dm_exec_sessions s
OUTER APPLY (SELECT TOP 1 c.most_recent_sql_handle FROM sys.dm_exec_connections c
             WHERE c.session_id = s.session_id) c
OUTER APPLY sys.dm_exec_sql_text(c.most_recent_sql_handle) q
WHERE s.session_id <> @@SPID
  AND (s.database_id = DB_ID()
       OR EXISTS (SELECT 1 FROM sys.dm_tran_session_transactions st2
                  JOIN sys.dm_tran_database_transactions dt
                    ON dt.transaction_id = st2.transaction_id
                  WHERE st2.session_id = s.session_id AND dt.database_id = DB_ID()))
  AND EXISTS (SELECT 1 FROM sys.dm_tran_session_transactions st
              WHERE st.session_id = s.session_id AND st.is_user_transaction = 1)"""


def conflicts(plan: Planned, mode: str) -> bool:
    """Whether a planned lock waits for another session's lock mode."""
    return mode not in _COMPATIBLE.get(plan.lock or "", frozenset({"Sch-S"}))


def _session(
    identifier: object,
    user: object,
    program: object,
    status: object,
    age: object,
    query: object,
) -> LiveSession:
    return LiveSession(
        number(identifier),
        f"session {identifier}",
        text(user),
        text(program),
        text(status),
        seconds(age),
        text(query),
    )


def preflight_plan(
    impacts: Sequence[StatementImpact], older_than: float
) -> PreflightPlan:
    """
    Reads the other sessions' table locks and user transactions, and
    returns what the statements would wait behind. A read that fails is
    left out of `read`.
    """
    read: Set[str] = set()
    found: List[Blocker] = []
    rows = yield from attempt(_LOCKS_SQL)
    if rows is not None:
        granted = [
            Granted(
                str(schema),
                str(table),
                bool(bare),
                str(mode),
                bool(ok),
                _session(identifier, *rest),
            )
            for identifier, schema, table, bare, mode, ok, *rest in rows
            if schema is not None and table is not None
        ]
        found = blockers(planned(impacts), granted, conflicts)
        read.add("locks")
    sessions: List[LiveSession] = []
    open_rows = yield from attempt(_TRANSACTIONS_SQL)
    if open_rows is not None:
        sessions = [_session(*row) for row in open_rows]
        read.add("transactions")
    return Preflight(
        "mssql",
        tuple(found),
        older(sessions, older_than, found),
        older_than,
        frozenset(read),
    )
