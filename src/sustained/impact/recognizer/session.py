"""
The statements that change session settings without SET: `RESET`,
`DISCARD ALL`, `ROLLBACK`, and `SELECT set_config(...)`.

Each reads as a `set` statement, so the run state takes in what it does
to the settings. `RESET name` and `RESET ALL` give the scope `reset`,
with the name `all` for every setting, and so does `DISCARD ALL`.
`ROLLBACK`, and `ROLLBACK TO SAVEPOINT`, give the scope `rollback` with
the name `all`: on PostgreSQL they undo each `SET` since the transaction
or the savepoint began. `set_config(name, value, is_local)` gives the
scope `local` when `is_local` is true, and `session` when it is false.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from sustained.impact.model import ParsedStatement
from sustained.impact.recognizer.cursor import Cursor, Unrecognized, frozen
from sustained.impact.tokens import STRING

Setting = Tuple[str, str, str]


def set_parsed(settings: Sequence[Setting]) -> ParsedStatement:
    """A `set` statement that makes each of `settings`."""
    return ParsedStatement("set", options=frozen({"settings": tuple(settings)}))


def reset(cursor: Cursor) -> ParsedStatement:
    """`RESET name` or `RESET ALL`."""
    if cursor.accept("ALL"):
        return set_parsed([("reset", "all", "DEFAULT")])
    if cursor.is_word("SESSION", "ROLE"):
        raise Unrecognized(f"no rule reads RESET {cursor.where()}")
    return set_parsed([("reset", cursor.name().lower(), "DEFAULT")])


def discard(cursor: Cursor) -> ParsedStatement:
    """`DISCARD ALL`, which resets every setting."""
    if not cursor.accept("ALL"):
        raise Unrecognized(f"no rule reads DISCARD {cursor.where()}")
    return set_parsed([("reset", "all", "DEFAULT")])


def rollback(cursor: Cursor) -> ParsedStatement:
    """`ROLLBACK`, with the words that may follow it."""
    if cursor.accept_any("WORK", "TRANSACTION", "TRAN") and cursor.mssql:
        # SQL Server may name the transaction.
        if cursor.is_name():
            cursor.name()
    if cursor.accept("TO"):
        cursor.accept("SAVEPOINT")
        cursor.name()
    elif cursor.accept("AND"):
        cursor.accept("NO")
        cursor.expect("CHAIN")
    return set_parsed([("rollback", "all", "")])


def select(cursor: Cursor) -> ParsedStatement:
    """`SELECT set_config(name, value, is_local)`, one call or a list."""
    settings = [_set_config(cursor)]
    while cursor.accept_punct(","):
        settings.append(_set_config(cursor))
    return set_parsed(settings)


def _set_config(cursor: Cursor) -> Setting:
    parts = cursor.name_parts() if cursor.is_name() else []
    if not (
        [p.lower() for p in parts] in (["set_config"], ["pg_catalog", "set_config"])
    ):
        raise Unrecognized("no rule reads a SELECT statement")
    arguments = cursor.split_top(cursor.group())
    if len(arguments) != 3 or any(len(a) != 1 for a in arguments):
        raise Unrecognized("no rule reads set_config() with these arguments")
    (name,), (value,), (local,) = arguments
    if name.kind != STRING or value.kind != STRING:
        raise Unrecognized("no rule reads set_config() without literal arguments")
    if local.is_word("TRUE") or (local.kind == STRING and local.value == "t"):
        scope = "local"
    elif local.is_word("FALSE") or (local.kind == STRING and local.value == "f"):
        scope = "session"
    else:
        raise Unrecognized("no rule reads set_config() with this is_local")
    return (scope, name.value.lower(), value.value)
