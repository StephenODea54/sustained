"""
The PostgreSQL live preflight: the table locks other backends in this
database were granted or are waiting for, from `pg_locks`, with each
backend's state, user, application, transaction age, and last statement
from `pg_stat_activity`, and the transactions open in this database,
prepared transactions included.

A planned lock waits behind a lock the conflict table says it conflicts
with. Some statements wait for more than their own lock allows:

- `CREATE INDEX CONCURRENTLY` waits for every session that writes to
  the table, as a `SHARE` lock would, and then for every transaction
  with a snapshot, whatever table it reads.
- `REINDEX CONCURRENTLY` waits for every session with any lock on the
  table, and for every transaction with a snapshot.
- `DROP INDEX CONCURRENTLY` and `DETACH PARTITION ... CONCURRENTLY`
  wait for every session with any lock on the table.

A lock on a partition is not matched to its partitioned table, and row
locks are not read.
"""

from __future__ import annotations

import re
from typing import Dict, FrozenSet, List, Mapping, Sequence, Set

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
from sustained.impact.rules.postgres import locks

# Which modes conflict with each mode, from the table-level lock
# conflict table in the PostgreSQL documentation.
_CONFLICTS: Mapping[str, FrozenSet[str]] = {
    locks.ACCESS_SHARE: frozenset({locks.ACCESS_EXCLUSIVE}),
    locks.ROW_SHARE: frozenset({locks.EXCLUSIVE, locks.ACCESS_EXCLUSIVE}),
    locks.ROW_EXCLUSIVE: frozenset(
        {
            locks.SHARE,
            locks.SHARE_ROW_EXCLUSIVE,
            locks.EXCLUSIVE,
            locks.ACCESS_EXCLUSIVE,
        }
    ),
    locks.SHARE_UPDATE_EXCLUSIVE: frozenset(
        {
            locks.SHARE_UPDATE_EXCLUSIVE,
            locks.SHARE,
            locks.SHARE_ROW_EXCLUSIVE,
            locks.EXCLUSIVE,
            locks.ACCESS_EXCLUSIVE,
        }
    ),
    locks.SHARE: frozenset(
        {
            locks.ROW_EXCLUSIVE,
            locks.SHARE_UPDATE_EXCLUSIVE,
            locks.SHARE_ROW_EXCLUSIVE,
            locks.EXCLUSIVE,
            locks.ACCESS_EXCLUSIVE,
        }
    ),
    locks.SHARE_ROW_EXCLUSIVE: frozenset(
        {
            locks.ROW_EXCLUSIVE,
            locks.SHARE_UPDATE_EXCLUSIVE,
            locks.SHARE,
            locks.SHARE_ROW_EXCLUSIVE,
            locks.EXCLUSIVE,
            locks.ACCESS_EXCLUSIVE,
        }
    ),
    locks.EXCLUSIVE: frozenset(set(locks.LOCKS) - {locks.ACCESS_SHARE}),
    locks.ACCESS_EXCLUSIVE: frozenset(locks.LOCKS),
}

# The lock whose conflicts a statement waits for, where it waits for
# more than its own lock conflicts with.
_WAITS_AS: Mapping[str, str] = {
    "pg.create_index.concurrently": locks.SHARE,
    "pg.reindex.concurrently": locks.ACCESS_EXCLUSIVE,
    "pg.drop_index.concurrently": locks.ACCESS_EXCLUSIVE,
    "pg.detach_partition.concurrently": locks.ACCESS_EXCLUSIVE,
}

# The statements that wait for every transaction with a snapshot.
_WAITS_FOR_SNAPSHOTS = frozenset(
    {"pg.create_index.concurrently", "pg.reindex.concurrently"}
)

_SYSTEM_SCHEMAS = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname !~ '^pg_(toast|temp_)'"
)

# One row per table lock another backend in this database was granted
# or is waiting for. A prepared transaction's locks have no pid; its
# virtual transaction is -1 and its transaction id.
_LOCKS_SQL = f"""SELECT l.pid, p.gid, n.nspname, c.relname,
  pg_catalog.pg_table_is_visible(c.oid), l.mode, l.granted,
  coalesce(a.usename, p.owner::text), a.application_name, a.state,
  extract(epoch FROM now() - coalesce(a.xact_start, p.prepared))::float8, a.query
FROM pg_catalog.pg_locks l
JOIN pg_catalog.pg_class c ON c.oid = l.relation
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_stat_activity a ON a.pid = l.pid
LEFT JOIN pg_catalog.pg_prepared_xacts p
  ON l.pid IS NULL AND l.virtualtransaction = '-1/' || p.transaction::text
WHERE l.locktype = 'relation'
  AND l.database = (SELECT oid FROM pg_catalog.pg_database
                    WHERE datname = current_database())
  AND l.pid IS DISTINCT FROM pg_catalog.pg_backend_pid()
  AND c.relkind IN ('r', 'p', 'm')
  AND {_SYSTEM_SCHEMAS}"""

# One row per other client backend in this database with a transaction
# open, and whether the transaction has a snapshot or a transaction id.
_TRANSACTIONS_SQL = """SELECT a.pid, a.usename, a.application_name, a.state,
  extract(epoch FROM now() - a.xact_start)::float8, a.query,
  a.backend_xid IS NOT NULL OR a.backend_xmin IS NOT NULL
FROM pg_catalog.pg_stat_activity a
WHERE a.xact_start IS NOT NULL
  AND a.pid <> pg_catalog.pg_backend_pid()
  AND a.datname = current_database()
  AND a.backend_type = 'client backend'"""

# Prepared transactions stay open until COMMIT PREPARED or ROLLBACK
# PREPARED, and keep their snapshot until then.
_PREPARED_SQL = """SELECT gid, owner::text,
  extract(epoch FROM now() - prepared)::float8
FROM pg_catalog.pg_prepared_xacts
WHERE database = current_database()"""


def mode_name(mode: str) -> str:
    """A `pg_locks` mode, such as `AccessShareLock`, as `ACCESS SHARE`."""
    if mode.endswith("Lock"):
        mode = mode[: -len("Lock")]
    words = re.findall(r"[A-Z][a-z]*", mode)
    return " ".join(word.upper() for word in words)


def conflicts(plan: Planned, mode: str) -> bool:
    """Whether a planned lock waits for another session's lock mode."""
    lock = _WAITS_AS.get(plan.rule or "", plan.lock)
    return mode in _CONFLICTS.get(lock or "", frozenset(locks.LOCKS))


def _session(pid: object, gid: object) -> LiveSession:
    if pid is None:
        return LiveSession(None, f"prepared transaction '{gid}'")
    return LiveSession(number(pid), f"pid {pid}")


def preflight_plan(
    impacts: Sequence[StatementImpact], older_than: float
) -> PreflightPlan:
    """
    Reads the other backends' table locks and open transactions, and
    returns what the statements would wait behind. A read that fails is
    left out of `read`.
    """
    read: Set[str] = set()
    found: List[Blocker] = []
    rows = yield from attempt(_LOCKS_SQL)
    if rows is not None:
        granted = []
        for (
            pid,
            gid,
            schema,
            table,
            visible,
            mode,
            ok,
            user,
            app,
            state,
            age,
            query,
        ) in rows:
            session = _session(pid, gid)._replace(
                user=text(user),
                application=text(app),
                state=text(state),
                transaction_seconds=seconds(age),
                query=text(query),
            )
            granted.append(
                Granted(
                    str(schema),
                    str(table),
                    bool(visible),
                    mode_name(str(mode)),
                    bool(ok),
                    session,
                )
            )
        found = blockers(planned(impacts), granted, conflicts)
        read.add("locks")
    sessions: List[LiveSession] = []
    snapshots: Dict[str, LiveSession] = {}
    open_rows = yield from attempt(_TRANSACTIONS_SQL)
    if open_rows is not None:
        for pid, user, app, state, age, query, snapshot in open_rows:
            session = LiveSession(
                number(pid),
                f"pid {pid}",
                text(user),
                text(app),
                text(state),
                seconds(age),
                text(query),
            )
            sessions.append(session)
            if snapshot:
                snapshots[session.label] = session
        read.add("transactions")
        prepared = yield from attempt(_PREPARED_SQL)
        for gid, owner, age in prepared or ():
            session = _session(None, gid)._replace(
                user=text(owner), transaction_seconds=seconds(age)
            )
            sessions.append(session)
            snapshots[session.label] = session
    found.extend(_snapshot_blockers(impacts, snapshots, found))
    return Preflight(
        "postgres",
        tuple(found),
        older(sessions, older_than, found),
        older_than,
        frozenset(read),
    )


def _snapshot_blockers(
    impacts: Sequence[StatementImpact],
    snapshots: Mapping[str, LiveSession],
    found: Sequence[Blocker],
) -> List[Blocker]:
    """
    The transactions with a snapshot that the first statement waiting
    for snapshots would wait for, leaving out sessions already listed
    for a lock on the same table.
    """
    for plan in planned(impacts):
        if plan.rule not in _WAITS_FOR_SNAPSHOTS:
            continue
        listed = {
            b.session.label
            for b in found
            if b.table is not None and b.table.lower() == plan.table.lower()
        }
        return [
            Blocker(plan.statement, plan.table, plan.lock, None, True, session)
            for label, session in snapshots.items()
            if label not in listed
        ]
    return []
