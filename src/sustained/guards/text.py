"""
The textual guards, which match on the words in each statement and
never parse SQL. Comments and the text inside quotes are kept out of the
scan. Each guard reads the statement as its dialect does: where a
comment ends, which characters are whitespace, and whether a MySQL
`/*! ... */` body runs.
"""

from __future__ import annotations

import re
from typing import Callable, List, Sequence

from sustained.analysis import (
    _ALTER_DROP_RE,
    MigrationStatement,
    normalize_statement,
    scannable_forms,
    scannable_statement,
    statement_scope,
)
from sustained.dialects import Dialects
from sustained.guards.core import BLOCK, WARN, Guard, Verdict

_DROP_RE = re.compile(
    r"\bDROP\s+(TABLE|COLUMN|MATERIALIZED\s+VIEW|VIEW|SCHEMA|DATABASE|TYPE"
    r"|CONSTRAINT|CHECK|FOREIGN\s+KEY)\b",
    re.IGNORECASE,
)
_CREATE_INDEX_RE = re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", re.IGNORECASE)
_CONCURRENTLY_RE = re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE)
_ON_ONLY_RE = re.compile(r"\bON\s+ONLY\b", re.IGNORECASE)
_TYPE_CHANGE_RE = re.compile(
    r"\bALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b|\bMODIFY\s+(COLUMN\s+)?\S+\s",
    re.IGNORECASE,
)
_SET_NOT_NULL_RE = re.compile(r"\bSET\s+NOT\s+NULL\b", re.IGNORECASE)
_ADD_NOT_NULL_RE = re.compile(r"\bADD\s+(COLUMN\s+)?\S+.*\bNOT\s+NULL\b", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.IGNORECASE)
_LOCK_TAKING_RE = re.compile(r"\bALTER\s+TABLE\b|\bDROP\s+TABLE\b", re.IGNORECASE)
# Only a statement that starts with `SET lock_timeout` counts, so an
# `UPDATE t SET lock_timeout = 5` on a column of that name does not pass
# for a timeout. `SESSION` and `LOCAL` are the two words Postgres allows
# between the two.
_LOCK_TIMEOUT_RE = re.compile(
    r"^SET\s+(SESSION\s+|LOCAL\s+)?lock_timeout\b", re.IGNORECASE
)


def _text_guard(
    rule: str, level: str, matches: Callable[[str], object], postgres_only: bool = False
) -> Guard:
    """
    A guard that gives `level` to each statement with a scanned form
    that `matches`. With `postgres_only`, it is silent on every other
    dialect.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        if postgres_only and dialect is not Dialects.POSTGRES:
            return []
        return [
            Verdict(rule, level, normalize_statement(statement))
            for statement in statements
            if any(matches(form) for form in scannable_forms(statement, dialect))
        ]

    return guard


def no_drops() -> Guard:
    """
    Blocks a statement that drops a table, a column, a view, a
    materialized view, a schema, a database, an enum type, or a
    constraint. A dropped constraint removes no rows, but putting it back
    needs the data to still satisfy it, so the drop is not freely
    reversible. Drops of indexes and keys pass.

    A column type change that narrows the type drops no object, so this
    rule passes it. The destructive label and the rehearsal gate in
    `migrate` still catch it.
    """
    return _text_guard(
        "no_drops",
        BLOCK,
        lambda form: _DROP_RE.search(form) or _ALTER_DROP_RE.search(form),
    )


def index_must_be_concurrent() -> Guard:
    """
    Blocks `CREATE INDEX` without `CONCURRENTLY` on Postgres, where a
    plain index build keeps a write lock on the table for its whole
    duration. Silent on every other dialect, which has no such keyword.

    `CREATE INDEX ... ON ONLY` passes. It creates an invalid index on a
    partitioned table alone and builds nothing, and Postgres refuses
    `CONCURRENTLY` in it, so no form of it could pass otherwise. This is
    the one statement without `CONCURRENTLY` the guard passes; the
    online split generates it for a partitioned table.

    Postgres refuses CREATE INDEX CONCURRENTLY inside a transaction
    block, and the migrator wraps a migration in one. Put the index in a
    migration of its own with transactional=False, or in a SQL file that
    carries the '-- sustained: no transaction' marker. Such a migration
    that fails part way leaves an invalid index behind, which you drop by
    hand before you run it again.

    `max_blocking("ddl")` reads the impact analysis instead, which also
    passes an index on a table the same run created.
    """
    return _text_guard(
        "index_must_be_concurrent",
        BLOCK,
        lambda form: _CREATE_INDEX_RE.search(form)
        and not _CONCURRENTLY_RE.search(form)
        and not _ON_ONLY_RE.search(form),
        postgres_only=True,
    )


def no_table_rewrite() -> Guard:
    """
    Warns on a statement that may rewrite the whole table: a column type
    change, or a NOT NULL added with no default to fill the existing
    rows.

    This rule warns where the others block. Whether a given change
    rewrites depends on the engine, its version, and whether the two
    types coerce, so a block here would stop safe statements. Read the
    warning against your own engine.

    `no_rewrite()` reads the impact analysis instead, which knows the
    engine version, which type changes coerce, and the table's size.
    """
    return _text_guard("no_table_rewrite", WARN, _rewrites)


def _rewrites(scanned: str) -> bool:
    """Whether one scanned form changes a column type or adds a NOT NULL."""
    if _TYPE_CHANGE_RE.search(scanned) or _SET_NOT_NULL_RE.search(scanned):
        return True
    return bool(_ADD_NOT_NULL_RE.search(scanned)) and not _DEFAULT_RE.search(scanned)


def no_lock_without_timeout() -> Guard:
    """
    Blocks a run that alters or drops a table without setting a lock
    timeout first, on Postgres, where a statement waiting behind a long
    transaction queues every other query on that table behind it. Silent
    on every other dialect, which has no such setting.

    The rule reads the statements in run order, and it reads how far each
    timeout reaches.

    `SET lock_timeout`, with or without SESSION, sets the timeout for the
    session. It covers every statement after it in the run, in its own
    migration and in the ones that follow, and none before it.

    `SET LOCAL lock_timeout` dies at the commit that ends its migration,
    so it covers only the statements after it in that same migration. The
    next migration starts uncovered.

    A migration that runs outside a transaction is the third case.
    Postgres has no transaction block to attach a LOCAL setting to there,
    so it ignores the `SET LOCAL` and the statements after it stay
    uncovered. Write the plain `SET lock_timeout` in a migration like
    that.

    The impact analysis reads timeout scopes the same way, through
    `sustained.impact.timeouts.TimeoutScope`; its `pg.lock_timeout` finding
    covers every lock that blocks reads or writes, where this rule reads
    only ALTER TABLE and DROP TABLE.
    """

    def guard(
        statements: Sequence[MigrationStatement], dialect: Dialects
    ) -> List[Verdict]:
        from sustained.impact.timeouts import TimeoutScope

        if dialect is not Dialects.POSTGRES:
            return []
        found = []
        timeouts = TimeoutScope()
        for statement in statements:
            migration_id, transactional = statement_scope(statement)
            timeouts.enter(migration_id)
            scanned = scannable_statement(statement, dialect)
            match = _LOCK_TIMEOUT_RE.search(scanned)
            if match:
                scope = (match.group(1) or "session").strip().lower()
                timeouts.set(scope, transactional)
            elif not timeouts.covered and any(
                _LOCK_TAKING_RE.search(form)
                for form in scannable_forms(statement, dialect)
            ):
                found.append(
                    Verdict(
                        "no_lock_without_timeout",
                        BLOCK,
                        normalize_statement(statement),
                    )
                )
        return found

    return guard


def max_statements(limit: int) -> Guard:
    """
    Blocks a run longer than `limit` statements, which usually means
    several changes that should have been several deploys. The verdict
    names every statement past the limit.
    """
    if limit < 1:
        raise ValueError("max_statements needs a limit of at least 1.")
    rule = f"max_statements({limit})"

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        return [
            Verdict(rule, BLOCK, normalize_statement(statement))
            for statement in statements[limit:]
        ]

    return guard
