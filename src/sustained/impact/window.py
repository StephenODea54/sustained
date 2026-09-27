"""
Transaction windows: how long each table stays blocked in a migration.

Inside a transaction, a lock is held until the commit, so a brief
ACCESS EXCLUSIVE followed by a long backfill keeps the table blocked for
the whole backfill. A tool that reads one statement at a time misses
this. `aggregate()` reads a migration's statements together:

- `locks`: every lock that blocks something, with the statement that
  took it
- `windows`: for each table blocked for writes or worse, the heaviest
  work that runs while the lock is held
- a `window.held` finding when that work belongs to a later statement,
  naming each level the table is blocked for and the first statement to
  block it that far
- a `window.lock_order` finding when more than one table is blocked for
  reads and writes at once, which can deadlock against application
  transactions that lock the same tables in another order

When the locks do not outlive their statement, because the migration
runs outside a transaction or the engine commits each DDL statement,
every statement is a window of its own. On an engine that commits each
DDL statement, the row locks of the INSERT, UPDATE, and DELETE
statements in a transaction last until the next DDL statement commits
them, or the migration does, so `row_scopes()` makes each run of
statements that commit nothing a window.

On an engine whose writes lock the whole database, as SQLite's do, a
lock on one table blocks writes to every other, so `aggregate()` reads
the migration's tables as one window named `DATABASE`. Every write
there blocks the same sessions, so the heavy work itself blocks as much
as the lock held across it, and the window draws no `window.held`
finding.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from sustained.impact.model import (
    Blocks,
    Confidence,
    Finding,
    Hold,
    Lock,
    Severity,
    StatementImpact,
    Window,
    Work,
)

# The name of the one window a database-wide lock makes, and of the
# table line for work on the whole database, such as SQLite's VACUUM.
DATABASE = "(database)"


def statement_work(impact: StatementImpact) -> Work:
    """The heaviest work a statement does; unknown for an unknown one."""
    if impact.confidence is Confidence.UNKNOWN and not impact.tables:
        return Work.UNKNOWN
    return max((t.work for t in impact.tables), default=Work.CATALOG)


def _scopes(count: int, spans_transaction: bool) -> List[range]:
    if spans_transaction:
        return [range(count)]
    return [range(i, i + 1) for i in range(count)]


# The statement kinds that do not commit the transaction on an engine
# whose DDL commits it.
_UNCOMMITTED = frozenset({"insert", "update", "delete", "set"})


def row_scopes(statements: Sequence[StatementImpact]) -> List[range]:
    """
    The windows of a transactional migration on an engine that commits
    each DDL statement: each run of statements that commit nothing, and
    each other statement on its own. A statement the recognizer did not
    read counts as one that commits nothing, which makes the longer
    window.
    """
    scopes: List[range] = []
    start = 0
    for index, impact in enumerate(statements):
        parsed = impact.parsed
        if parsed is not None and parsed.kind not in _UNCOMMITTED:
            if start < index:
                scopes.append(range(start, index))
            scopes.append(range(index, index + 1))
            start = index + 1
    if start < len(statements):
        scopes.append(range(start, len(statements)))
    return scopes


def held_in_scopes(
    statements: Sequence[StatementImpact], scopes: Sequence[range]
) -> Tuple[StatementImpact, ...]:
    """
    The statements with the locks of each one before the last of its
    scope held to the end of the scope.
    """
    found = list(statements)
    for scope in scopes:
        for index in scope[:-1]:
            impact = found[index]
            tables = tuple(
                t._replace(hold=Hold.TRANSACTION) if t.blocks > Blocks.NOTHING else t
                for t in impact.tables
            )
            found[index] = impact._replace(tables=tables)
    return tuple(found)


def aggregate(
    statements: Sequence[StatementImpact],
    spans_transaction: bool,
    locks_database: bool = False,
    scopes: Optional[Sequence[range]] = None,
) -> Tuple[Tuple[Lock, ...], Tuple[Window, ...], Tuple[Finding, ...]]:
    """
    The locks, windows, and findings of one migration's statements.
    `spans_transaction` says whether locks are held to the commit, and
    `locks_database` whether a lock on a table locks the whole database.
    `scopes`, when given, are the windows in place of the ones
    `spans_transaction` makes, such as `row_scopes()`.
    """
    locks = tuple(
        Lock(table.table, table.lock, table.blocks, position)
        for position, impact in enumerate(statements, 1)
        for table in impact.tables
        if table.blocks > Blocks.NOTHING
    )
    works = [statement_work(s) for s in statements]
    windows: List[Window] = []
    findings: List[Finding] = []
    database = DATABASE if locks_database else None
    if scopes is None:
        scopes = _scopes(len(statements), spans_transaction)
    for scope in scopes:
        scoped = _windows(statements, works, scope, database)
        windows.extend(window for window, _ in scoped)
        if not locks_database:
            findings.extend(_held(scoped, _end(scope, len(statements))))
        if spans_transaction:
            findings.extend(_lock_order([window for window, _ in scoped]))
    return locks, tuple(windows), tuple(findings)


def _windows(
    statements: Sequence[StatementImpact],
    works: Sequence[Work],
    scope: range,
    database: Optional[str] = None,
) -> List[Tuple[Window, Tuple[Tuple[Blocks, int], ...]]]:
    """
    Each blocked table's window, with the levels it reaches: each level
    the table is blocked for, weakest first, with the position of the
    first statement that blocks it that far. With `database`, every
    table's lock falls in the one window of that name.
    """
    levels: Dict[str, List[Tuple[Blocks, int]]] = {}
    names: Dict[str, str] = {}
    for index in scope:
        for table in statements[index].tables:
            if table.blocks < Blocks.WRITES:
                continue
            key = database or table.table.lower()
            names.setdefault(key, database or table.table)
            reached = levels.setdefault(key, [])
            if not reached or table.blocks > reached[-1][0]:
                reached.append((table.blocks, index + 1))
    windows = []
    for key, reached in levels.items():
        kept = range(reached[0][1] - 1, scope.stop)
        during = max(kept, key=lambda i: (works[i], -i))
        blocked, taken_by = reached[-1]
        window = Window(names[key], blocked, taken_by, works[during], during + 1)
        windows.append((window, tuple(reached)))
    return windows


def _end(scope: range, count: int) -> str:
    """
    When the locks of the scope end: at the migration's commit, or, for
    a scope `row_scopes()` ends early, at the implicit commit before the
    next statement, a DDL statement.
    """
    if scope.stop < count:
        return f"until the implicit commit before statement {scope.stop + 1}"
    return "until the migration commits"


def _held(
    windows: Sequence[Tuple[Window, Tuple[Tuple[Blocks, int], ...]]],
    end: str,
) -> List[Finding]:
    findings = []
    for window, levels in windows:
        opened = levels[0][1]
        if window.heaviest <= Work.CATALOG or window.during == opened:
            continue
        blocked = " and ".join(
            f"for {level} from statement {position}" for level, position in levels
        )
        findings.append(
            Finding(
                "window.held",
                Severity.WARN,
                f"{window.table} stays blocked {blocked} {end}, across the "
                f"{window.heaviest} work of statement {window.during}; move that "
                "work to a migration of its own",
            )
        )
    return findings


def _lock_order(windows: Sequence[Window]) -> List[Finding]:
    exclusive = [w.table for w in windows if w.blocks is Blocks.READS_AND_WRITES]
    if len(exclusive) < 2:
        return []
    return [
        Finding(
            "window.lock_order",
            Severity.WARN,
            f"the migration blocks reads and writes on {', '.join(exclusive)} at "
            "once; application transactions that lock these tables in another "
            "order can deadlock with it",
        )
    ]
