"""
The MySQL and MariaDB live preflight: the table metadata locks other
connections were granted or are waiting for, and the InnoDB transactions
open on the server.

The metadata locks come from `performance_schema.metadata_locks`, which
needs the Performance Schema on and its `wait/lock/metadata/sql/mdl`
instrument enabled, as MySQL 8.0 has them by default. MariaDB ships
with the Performance Schema off, so there the read falls back to
`information_schema.METADATA_LOCK_INFO`, which the `metadata_lock_info`
plugin adds and which lists granted locks only. Without either, `locks`
is left out of `read`.

A statement whose lock is a DML statement's row locks takes a
`SHARED_WRITE` metadata lock, which waits for `SHARED_NO_WRITE`,
`SHARED_NO_READ_WRITE`, `SHARED_READ_ONLY`, and `EXCLUSIVE`. Every
other statement the rules name needs the `EXCLUSIVE` metadata lock at
least at its start or its end, whatever its `ALGORITHM` and `LOCK`,
which waits for every other metadata lock on the table.

The transactions come from `information_schema.INNODB_TRX`, with each
connection's user, command, and statement from
`information_schema.PROCESSLIST`, and its `program_name` connection
attribute from `performance_schema.session_connect_attrs` where the
client sent one.
"""

from __future__ import annotations

from typing import Dict, Generator, List, Mapping, Optional, Sequence, Set

from sustained.impact.context import Rows, attempt
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
from sustained.impact.rules.mysql.context import server_version
from sustained.impact.rules.mysql.locks import ROW_LOCKS

_SERVER_SQL = "SELECT VERSION(), DATABASE()"

_SYSTEM_SCHEMAS = "('mysql', 'information_schema', 'performance_schema', 'sys')"

# Whether the Performance Schema records metadata locks.
_INSTRUMENT_SQL = (
    "SELECT @@performance_schema, ENABLED FROM performance_schema.setup_instruments "
    "WHERE NAME = 'wait/lock/metadata/sql/mdl'"
)

# One row per table metadata lock another connection was granted or is
# waiting for.
_LOCKS_SQL = f"""SELECT t.PROCESSLIST_ID, m.OBJECT_SCHEMA, m.OBJECT_NAME,
  m.LOCK_TYPE, m.LOCK_STATUS = 'GRANTED', t.PROCESSLIST_USER,
  t.PROCESSLIST_COMMAND, TIMESTAMPDIFF(SECOND, x.trx_started, NOW()),
  t.PROCESSLIST_INFO
FROM performance_schema.metadata_locks m
JOIN performance_schema.threads t ON t.THREAD_ID = m.OWNER_THREAD_ID
LEFT JOIN information_schema.INNODB_TRX x
  ON x.trx_mysql_thread_id = t.PROCESSLIST_ID
WHERE m.OBJECT_TYPE = 'TABLE'
  AND t.PROCESSLIST_ID IS NOT NULL
  AND t.PROCESSLIST_ID <> CONNECTION_ID()
  AND m.OBJECT_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"""

# The same from MariaDB's metadata_lock_info plugin, granted locks only.
_LOCK_INFO_SQL = f"""SELECT i.THREAD_ID, i.TABLE_SCHEMA, i.TABLE_NAME, i.LOCK_MODE,
  p.USER, p.COMMAND, TIMESTAMPDIFF(SECOND, x.trx_started, NOW()), p.INFO
FROM information_schema.METADATA_LOCK_INFO i
LEFT JOIN information_schema.PROCESSLIST p ON p.ID = i.THREAD_ID
LEFT JOIN information_schema.INNODB_TRX x ON x.trx_mysql_thread_id = i.THREAD_ID
WHERE i.LOCK_TYPE = 'Table metadata lock'
  AND i.THREAD_ID <> CONNECTION_ID()
  AND i.TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"""

# One row per InnoDB transaction of another connection.
_TRANSACTIONS_SQL = """SELECT x.trx_mysql_thread_id, p.USER, p.COMMAND,
  TIMESTAMPDIFF(SECOND, x.trx_started, NOW()), COALESCE(x.trx_query, p.INFO)
FROM information_schema.INNODB_TRX x
LEFT JOIN information_schema.PROCESSLIST p ON p.ID = x.trx_mysql_thread_id
WHERE x.trx_mysql_thread_id <> CONNECTION_ID()"""

_APPLICATIONS_SQL = (
    "SELECT PROCESSLIST_ID, ATTR_VALUE FROM performance_schema.session_connect_attrs "
    "WHERE ATTR_NAME = 'program_name'"
)

# The metadata locks a DML statement's SHARED_WRITE lock waits for.
_BLOCKS_WRITES = frozenset(
    {"SHARED_NO_WRITE", "SHARED_NO_READ_WRITE", "SHARED_READ_ONLY", "EXCLUSIVE"}
)


def conflicts(plan: Planned, mode: str) -> bool:
    """Whether a planned lock waits for another connection's metadata lock."""
    if plan.lock == ROW_LOCKS:
        return mode in _BLOCKS_WRITES
    return True


def _mode(value: object) -> str:
    """A lock mode as `metadata_locks.LOCK_TYPE` spells it."""
    spelled = str(value).upper()
    return spelled[4:] if spelled.startswith("MDL_") else spelled


def _session(
    connection: object,
    user: object,
    command: object,
    age: object,
    query: object,
    applications: Mapping[int, str],
) -> LiveSession:
    identifier = number(connection)
    return LiveSession(
        identifier,
        f"connection {connection}",
        text(user),
        applications.get(identifier) if identifier is not None else None,
        text(command),
        seconds(age),
        text(query),
    )


def preflight_plan(
    impacts: Sequence[StatementImpact], older_than: float
) -> PreflightPlan:
    """
    Reads the other connections' metadata locks and InnoDB transactions,
    and returns what the statements would wait behind. A read that fails
    is left out of `read`.
    """
    profile, database = "mysql", ""
    server = yield from attempt(_SERVER_SQL)
    if server:
        version, current = server[0]
        profile = server_version(str(version))[0]
        database = "" if current is None else str(current)
    applications: Dict[int, str] = {}
    names = yield from attempt(_APPLICATIONS_SQL)
    for identifier, value in names or ():
        if value is not None:
            applications[int(str(identifier))] = str(value)
    read: Set[str] = set()
    granted = yield from _metadata_locks(database, applications)
    found: List[Blocker] = []
    if granted is not None:
        found = blockers(planned(impacts), granted, conflicts)
        read.add("locks")
    sessions: List[LiveSession] = []
    rows = yield from attempt(_TRANSACTIONS_SQL)
    if rows is not None:
        sessions = [
            _session(connection, user, command, age, query, applications)
            for connection, user, command, age, query in rows
        ]
        read.add("transactions")
    return Preflight(
        profile,
        tuple(found),
        older(sessions, older_than, found),
        older_than,
        frozenset(read),
    )


def _metadata_locks(
    database: str, applications: Mapping[int, str]
) -> Generator[str, Rows, Optional[List[Granted]]]:
    """
    The other connections' table metadata locks, from the Performance
    Schema when it records them and from `METADATA_LOCK_INFO` otherwise,
    or None when neither could be read.
    """
    instrument = yield from attempt(_INSTRUMENT_SQL)
    recorded = False
    if instrument:
        recorded = all(
            str(value).upper() in ("1", "YES", "ON") for value in instrument[0]
        )
    if recorded:
        rows = yield from attempt(_LOCKS_SQL)
        if rows is not None:
            return [
                Granted(
                    str(schema),
                    str(table),
                    str(schema).lower() == database.lower(),
                    _mode(mode),
                    bool(ok),
                    _session(connection, user, command, age, query, applications),
                )
                for connection, schema, table, mode, ok, user, command, age, query in rows
            ]
    info = yield from attempt(_LOCK_INFO_SQL)
    if info is None:
        return None
    return [
        Granted(
            str(schema),
            str(table),
            str(schema).lower() == database.lower(),
            _mode(mode),
            True,
            _session(connection, user, command, age, query, applications),
        )
        for connection, schema, table, mode, user, command, age, query in info
    ]
