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
- a `window.held` finding when that work belongs to a later statement
- a `window.lock_order` finding when more than one table is blocked for
  reads and writes at once, which can deadlock against application
  transactions that lock the same tables in another order

When the locks do not outlive their statement, because the migration
runs outside a transaction or the engine commits each DDL statement,
every statement is a window of its own.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from sustained.impact.model import (
    Blocks,
    Confidence,
    Finding,
    Lock,
    Severity,
    StatementImpact,
    Window,
    Work,
)


def statement_work(impact: StatementImpact) -> Work:
    """The heaviest work a statement does; unknown for an unknown one."""
    if impact.confidence is Confidence.UNKNOWN and not impact.tables:
        return Work.UNKNOWN
    return max((t.work for t in impact.tables), default=Work.CATALOG)


def _scopes(count: int, spans_transaction: bool) -> List[range]:
    if spans_transaction:
        return [range(count)]
    return [range(i, i + 1) for i in range(count)]


def aggregate(
    statements: Sequence[StatementImpact], spans_transaction: bool
) -> Tuple[Tuple[Lock, ...], Tuple[Window, ...], Tuple[Finding, ...]]:
    """
    The locks, windows, and findings of one migration's statements.
    `spans_transaction` says whether locks are held to the commit.
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
    for scope in _scopes(len(statements), spans_transaction):
        scoped = _windows(statements, works, scope)
        windows.extend(window for window, _ in scoped)
        findings.extend(_held(scoped))
        if spans_transaction:
            findings.extend(_lock_order([window for window, _ in scoped]))
    return locks, tuple(windows), tuple(findings)


def _windows(
    statements: Sequence[StatementImpact], works: Sequence[Work], scope: range
) -> List[Tuple[Window, int]]:
    """Each blocked table's window, with the position that opened it."""
    first: Dict[str, int] = {}
    worst: Dict[str, Tuple[Blocks, int]] = {}
    names: Dict[str, str] = {}
    for index in scope:
        for table in statements[index].tables:
            if table.blocks < Blocks.WRITES:
                continue
            key = table.table.lower()
            names.setdefault(key, table.table)
            first.setdefault(key, index)
            if key not in worst or table.blocks > worst[key][0]:
                worst[key] = (table.blocks, index + 1)
    windows = []
    for key, start in first.items():
        held = range(start, scope.stop)
        during = max(held, key=lambda i: (works[i], -i))
        blocked, taken_by = worst[key]
        window = Window(names[key], blocked, taken_by, works[during], during + 1)
        windows.append((window, start + 1))
    return windows


def _held(windows: Sequence[Tuple[Window, int]]) -> List[Finding]:
    findings = []
    for window, opened in windows:
        if window.heaviest <= Work.CATALOG or window.during == opened:
            continue
        findings.append(
            Finding(
                "window.held",
                Severity.WARN,
                f"{window.table} stays blocked for {window.blocks} from statement "
                f"{opened} until the migration commits, across the "
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
