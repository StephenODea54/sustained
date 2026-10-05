"""
The recognizer: statement text to a `ParsedStatement`.

The recognizer reads the token stream `sustained.impact.tokens` gives
and understands only the statements the impact rules need. It is
recursive descent over those tokens, not a full SQL parser: a clause it
has no rule for is skipped only where skipping cannot hide a change,
such as the body of a view or the tail of a CREATE TABLE.

Every other statement becomes `ParsedStatement(kind="unknown")`, and so
does a recognized statement with any part the recognizer cannot read,
such as an ALTER TABLE action it does not know. A partly understood statement
never counts as understood. An unknown statement keeps the table the
recognizer had read when it stopped, when it got that far, and names
what stopped it in `options["reason"]`.

Names come back dotted and unquoted, as the statement spells them:
`"app"."Items"` reads as `app.Items`. A caller that matches names
compares them case-insensitively.

The statement kinds, and the options each one sets:

- `create_index`: name, unique, concurrently, if_not_exists, only,
  using, partial, columns, with (a dict of the WITH (...) options),
  algorithm, lock, fulltext, spatial (MySQL SPATIAL), wait (MariaDB
  WAIT n or NOWAIT, as seconds), and on SQL Server clustered when the
  statement spells CLUSTERED or NONCLUSTERED
- `drop_index`: name, names, concurrently, if_exists, algorithm, lock,
  wait, with
- `alter_index` (SQL Server): name (None for ALL), operation, such as
  `rebuild` or `reorganize`, partition, with
- `attach_index` (PostgreSQL): name, partition (the index ALTER INDEX
  ... ATTACH PARTITION attaches)
- `update_statistics` (SQL Server): fullscan
- `alter_table`: actions, plus if_exists, only, algorithm, lock, wait,
  online and ignore (MariaDB ALTER ONLINE and ALTER IGNORE), and
  nocheck (SQL Server WITH NOCHECK)
- `create_table`: if_not_exists, temporary, references, partition_of,
  default_partition (a PARTITION OF with the bound DEFAULT),
  partitioned (a PARTITION BY clause), as_select, and reads for `AS
  SELECT` (`sources.py`)
- `drop_type`: names
- `drop_table`, `truncate`, `drop_view`, `optimize_table`,
  `lock_table`, `vacuum`, `analyze`: tables, plus if_exists where the
  statement may spell it, `cascade` for DROP and TRUNCATE, `mode` and
  `nowait` for LOCK, `full` for VACUUM
- `rename_table`: new, renames (every old and new pair)
- `update`, `delete`: where, limited (a LIMIT or TOP caps the rows)
- `insert`: source (`values`, `select`, or `default`), rows (the row
  count of a VALUES list), and reads for a query source: the tables
  the query reads, or None when it reads rows from something else
  (`sources.py`)
- `reindex`: target (`index`, `table`, ..., or on SQLite `database`
  for every index and `any` for a name the text cannot place), name,
  concurrently
- `cluster`: index
- `refresh_materialized_view`: concurrently, with_data
- `create_trigger`, `drop_trigger`: name
- `comment_on`: object, column
- `create_type`: enum
- `alter_type_add_value`: value
- `alter_type_rename_value`
- `create_view`: materialized
- `create_object`, `drop_object`: object, such as `schema`, `sequence`,
  `function`, `procedure`, `extension`, or `domain`. For a domain,
  CREATE also sets name, type (the type the domain is over, as the
  statement spells it), and constrained (whether it has a NOT NULL or
  a CHECK of its own), and DROP sets names. `ALTER DOMAIN` is unknown
- `set`: settings, a tuple of (scope, name, value), where scope is
  `session`, `local`, `global`, `persist`, `pragma`, `reset`, or
  `rollback`, and the name is lower case. `RESET`, `DISCARD ALL`,
  `ROLLBACK`, and `SELECT set_config(...)` read as `set` too; see
  `session.py`

An ALTER TABLE action's kind is one of `ACTION_KINDS`; the options each
one sets are named where `alter.py` or `definitions.py` reads it.

The parser is `Cursor` (`cursor.py`), which reads tokens, extended by
one class per group of statements: `Definitions` for column and
constraint definitions, `AlterTable`, `CreateDrop`, and `Statements`
for the rest. `_Parser` here combines them and dispatches on the first
word. `classify_default()` (`volatility.py`) rates a column default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence

from sustained.impact.model import UNKNOWN_KIND, ParsedStatement
from sustained.impact.recognizer import session
from sustained.impact.recognizer.alter import AlterTable
from sustained.impact.recognizer.create_drop import CREATE_TABLE_DEFAULTS, CreateDrop
from sustained.impact.recognizer.cursor import Unrecognized, depths, frozen, named
from sustained.impact.recognizer.definitions import COLUMN_DEFAULTS
from sustained.impact.recognizer.statements import Statements
from sustained.impact.recognizer.volatility import VOLATILITIES, classify_default
from sustained.impact.tokens import (
    ERROR,
    IDENT,
    NUMBER,
    OP,
    STRING,
    WORD,
    Token,
    tokenize,
)

if TYPE_CHECKING:
    from sustained.dialects import Dialects

STATEMENT_KINDS = frozenset(
    {
        "create_index",
        "drop_index",
        "alter_table",
        "create_table",
        "drop_table",
        "truncate",
        "rename_table",
        "update",
        "delete",
        "insert",
        "reindex",
        "vacuum",
        "analyze",
        "cluster",
        "optimize_table",
        "refresh_materialized_view",
        "create_trigger",
        "drop_trigger",
        "comment_on",
        "create_type",
        "drop_type",
        "alter_type_add_value",
        "alter_type_rename_value",
        "alter_index",
        "attach_index",
        "update_statistics",
        "create_view",
        "drop_view",
        "create_object",
        "drop_object",
        "set",
        "lock_table",
        UNKNOWN_KIND,
    }
)

ACTION_KINDS = frozenset(
    {
        "add_column",
        "drop_column",
        "alter_column_type",
        "alter_column",
        "set_not_null",
        "drop_not_null",
        "set_default",
        "drop_default",
        "set_statistics",
        "set_storage",
        "modify_column",
        "change_column",
        "add_constraint",
        "drop_constraint",
        "validate_constraint",
        "enable_constraint",
        "disable_constraint",
        "add_index",
        "drop_index",
        "rename_column",
        "rename_to",
        "rename_constraint",
        "rename_index",
        "attach_partition",
        "detach_partition",
        "set_tablespace",
        "set_logged",
        "set_unlogged",
        "set_schema",
        "set_parameters",
        "owner_to",
        "replica_identity",
        "enable_trigger",
        "disable_trigger",
        "row_security",
        "engine",
        "convert_charset",
        "force",
        "rebuild",
        "switch",
        "table_option",
        "index_visibility",
    }
)


class _Parser(AlterTable, CreateDrop, Statements):
    """The recognizer's parser: every statement kind's reader."""

    def statement(self) -> ParsedStatement:
        token = self.peek()
        if token is None or token.kind != WORD:
            raise Unrecognized("the statement does not start with a keyword")
        handler = _STATEMENTS.get(token.value)
        if handler is None:
            raise Unrecognized(f"no rule reads a {token.value} statement")
        self.pos += 1
        return handler(self)


_Handler = Callable[[_Parser], ParsedStatement]
_STATEMENTS: Dict[str, _Handler] = {
    "CREATE": _Parser.create,
    "DROP": _Parser.drop,
    "ALTER": _Parser.alter,
    "UPDATE": _Parser.update,
    "DELETE": _Parser.delete,
    "INSERT": _Parser.insert,
    "WITH": _Parser.with_query,
    "TRUNCATE": _Parser.truncate,
    "RENAME": _Parser.rename,
    "REINDEX": _Parser.reindex,
    "VACUUM": _Parser.vacuum,
    "ANALYZE": _Parser.analyze,
    "CLUSTER": _Parser.cluster,
    "OPTIMIZE": _Parser.optimize,
    "REFRESH": _Parser.refresh,
    "COMMENT": _Parser.comment,
    "SET": _Parser.set_statement,
    "PRAGMA": _Parser.pragma,
    "LOCK": _Parser.lock,
    "EXEC": _Parser.execute,
    "EXECUTE": _Parser.execute,
    "IF": _Parser.if_statement,
    "RESET": session.reset,
    "DISCARD": session.discard,
    "ROLLBACK": session.rollback,
    "SELECT": session.select,
}


_ROUTINE_WORDS = ("TRIGGER", "FUNCTION", "PROCEDURE")
# The words after which a BEGIN is a name or part of an expression, not
# the opener of a routine body.
_NOT_BEFORE_BODY = frozenset(
    {
        "TRIGGER", "FUNCTION", "PROCEDURE", "EXISTS", "ON", "OF", "FOLLOWS",
        "PRECEDES", "RETURNS", "SETOF", "WHEN", "AND", "OR", "NOT", "IS",
        "IN", "LIKE", "GLOB", "MATCH", "REGEXP", "BETWEEN", "ESCAPE",
        "COLLATE", "THEN", "ELSE", "SET", "TO", "LANGUAGE", "CHARSET", "CASE",
    }
)  # fmt: skip
# The tokens after which a BEGIN inside a body starts a compound statement.
_BODY_STATEMENT_STARTS = frozenset({"BEGIN", "THEN", "ELSE", "DO", "LOOP", "REPEAT"})
# The words after END that close a statement other than BEGIN or CASE.
_END_OF_OTHER = frozenset({"IF", "LOOP", "WHILE", "REPEAT", "FOR"})


def _routine_word(tokens: Sequence[Token]) -> int:
    """
    The index of the TRIGGER, FUNCTION, or PROCEDURE word a CREATE
    statement creates, or -1 when the statement creates something else.
    The words the index skips are `OR REPLACE`, `OR ALTER`, TEMP,
    TEMPORARY, CONSTRAINT, and MySQL's `DEFINER = user[@host]`.
    """
    if not tokens[0].is_word("CREATE"):
        return -1
    index = 1
    if index + 1 < len(tokens) and tokens[index].is_word("OR"):
        if not tokens[index + 1].is_word("REPLACE", "ALTER"):
            return -1
        index += 2
    if index + 2 < len(tokens) and tokens[index].is_word("DEFINER"):
        if tokens[index + 1].kind != OP or tokens[index + 1].text != "=":
            return -1
        index += 2
        if tokens[index].is_word("CURRENT_USER"):
            index += 1
            if index + 1 < len(tokens) and tokens[index].is_punct("("):
                if not tokens[index + 1].is_punct(")"):
                    return -1
                index += 2
        else:
            if tokens[index].kind not in (WORD, IDENT, STRING):
                return -1
            index += 1
            if index + 1 < len(tokens) and tokens[index].text == "@":
                if tokens[index + 1].kind not in (WORD, IDENT, STRING):
                    return -1
                index += 2
    while index < len(tokens) and tokens[index].is_word(
        "TEMP", "TEMPORARY", "CONSTRAINT"
    ):
        index += 1
    if index < len(tokens) and tokens[index].is_word(*_ROUTINE_WORDS):
        return index
    return -1


def _depth_zero(tokens: Sequence[Token], start: int) -> List[bool]:
    """For each token from `start`, whether it is outside every bracket."""
    return [depth == 0 for _, _, depth in depths(tokens, start)]


def _body_opener(tokens: Sequence[Token], kind: int) -> int:
    """
    The index of the BEGIN that opens a MySQL or SQLite routine body, or
    -1 when a `;` comes before one. A BEGIN after an operator, after
    punctuation other than `)`, after a word in `_NOT_BEFORE_BODY`, or
    before anything but a word is a name or part of an expression.
    """
    outside = _depth_zero(tokens, kind + 1)
    for index in range(kind + 1, len(tokens)):
        token = tokens[index]
        if token.is_punct(";"):
            return -1
        if not (token.is_word("BEGIN") and outside[index - kind - 1]):
            continue
        before = tokens[index - 1]
        if before.kind == OP or (before.is_punct() and not before.is_punct(")")):
            continue
        if before.kind == WORD and before.value in _NOT_BEFORE_BODY:
            continue
        if index + 1 < len(tokens) and tokens[index + 1].kind == WORD:
            return index
    return -1


def _block_ends_last(tokens: Sequence[Token], opener: int, compound: bool) -> bool:
    """
    Whether the BEGIN at `opener` is closed by the statement's last token.
    CASE opens a level and END closes one. With `compound` (MySQL and
    SQLite), a BEGIN at the start of a statement in the body opens a
    level, any other BEGIN fails the check, and an END that closes IF,
    LOOP, WHILE, REPEAT, or FOR closes no level. The count never exceeds
    the depth the server reads, so a body that reads as closed early
    fails the check.
    """
    depth = 1
    last = len(tokens) - 1
    for index in range(opener + 1, len(tokens)):
        token = tokens[index]
        before = tokens[index - 1]
        if token.is_word("BEGIN"):
            if not compound:
                return False
            label = (
                before.kind == OP
                and before.text == ":"
                and index >= 2
                and tokens[index - 2].kind in (WORD, IDENT)
            )
            starts = (
                before.is_punct(";") or label or before.is_word(*_BODY_STATEMENT_STARTS)
            )
            if not starts:
                return False
            depth += 1
        elif token.is_word("CASE") and not before.is_word("END"):
            depth += 1
        elif token.is_word("END"):
            following = tokens[index + 1] if index < last else None
            if compound and following is not None and following.is_word(*_END_OF_OTHER):
                continue
            depth -= 1
            if depth == 0:
                return index == last or (
                    index + 1 == last
                    and following is not None
                    and following.kind in (WORD, IDENT)
                )
    return False


def _semicolons_in_body(tokens: Sequence[Token], dialect: Optional["Dialects"]) -> bool:
    """
    Whether every `;` in the statement is inside the body of the trigger,
    function, or procedure the statement creates. On SQL Server the body
    is every token after the first `AS` outside parentheses, since the
    server stores the rest of the batch as the body. On Postgres and
    DuckDB a dollar-quoted body is one string token, and any other body
    is `BEGIN ATOMIC ... END` ending at the last token. On MySQL, SQLite,
    and with no dialect, the body is a `BEGIN ... END` block ending at
    the last token.
    """
    kind = _routine_word(tokens)
    if kind < 0:
        return False
    semicolons = [i for i, t in enumerate(tokens) if t.is_punct(";")]
    if not semicolons:
        return True
    if named(dialect, "MSSQL"):
        outside = _depth_zero(tokens, kind + 1)
        for index in range(kind + 1, len(tokens)):
            if tokens[index].is_word("AS") and outside[index - kind - 1]:
                return semicolons[0] > index
        return False
    if named(dialect, "POSTGRES", "DUCKDB"):
        outside = _depth_zero(tokens, kind + 1)
        for index in range(kind + 1, semicolons[0]):
            if (
                tokens[index].is_word("BEGIN")
                and outside[index - kind - 1]
                and index + 1 < len(tokens)
                and tokens[index + 1].is_word("ATOMIC")
            ):
                return _block_ends_last(tokens, index + 1, False)
        return False
    opener = _body_opener(tokens, kind)
    return opener >= 0 and _block_ends_last(tokens, opener, True)


def unknown(reason: str, table: Optional[str] = None) -> ParsedStatement:
    """An unknown statement, naming what stopped the recognizer."""
    return ParsedStatement(UNKNOWN_KIND, table, options=frozen({"reason": reason}))


def _on_its_own_line(sql: str, tokens: Sequence[Token], index: int) -> int:
    """
    The number of tokens a `GO` batch separator at `index` spans, with its
    optional repeat count, or 0 when the word shares its line with other
    tokens and so is not a separator.
    """
    token = tokens[index]
    if index > 0:
        previous = tokens[index - 1]
        if "\n" not in sql[previous.start + len(previous.text) : token.start]:
            return 0
    end = index + 1
    if end < len(tokens) and tokens[end].kind == NUMBER:
        between = sql[token.start + len(token.text) : tokens[end].start]
        if "\n" not in between:
            end += 1
    if end < len(tokens):
        last = tokens[end - 1]
        if "\n" not in sql[last.start + len(last.text) : tokens[end].start]:
            return 0
    return end - index


def _batches(sql: str, tokens: List[Token]) -> List[List[Token]]:
    """
    The statement's tokens split at each SQL Server `GO` line, without
    the separators and without batches that have no statement.
    """
    batches: List[List[Token]] = [[]]
    index = 0
    while index < len(tokens):
        if tokens[index].is_word("GO"):
            span = _on_its_own_line(sql, tokens, index)
            if span:
                batches.append([])
                index += span
                continue
        batches[-1].append(tokens[index])
        index += 1
    return [b for b in batches if any(not t.is_punct(";") for t in b)]


def recognize(sql: str, dialect: Optional["Dialects"] = None) -> ParsedStatement:
    """
    One statement, parsed, or an unknown statement. The dialect decides
    the lexical rules, as `tokenize()` reads them, and the few spellings
    that differ between engines, such as SQL Server's `DROP INDEX t.ix`.

    A string that contains more than one statement is unknown: the analysis
    reads one statement at a time. On SQL Server a line that reads `GO`,
    with an optional repeat count, separates batches; the separators are
    dropped, and a string with statements in two batches is unknown.
    """
    tokens = tokenize(sql, dialect)
    if tokens and tokens[-1].kind == ERROR:
        return unknown("the statement has a quote or comment that never closes")
    if named(dialect, "MSSQL"):
        batches = _batches(sql, tokens)
        if len(batches) > 1:
            return unknown("the text contains more than one batch")
        tokens = batches[0] if batches else []
    while tokens and tokens[-1].is_punct(";"):
        tokens.pop()
    if not tokens:
        return unknown("the statement is empty")
    if any(t.is_punct(";") for t in tokens) and not _semicolons_in_body(
        tokens, dialect
    ):
        return unknown("the text contains more than one statement")
    parser = _Parser(sql, tokens, dialect)
    try:
        parsed = parser.statement()
        parser.finish()
    except Unrecognized as error:
        return unknown(error.reason, parser.table)
    return parsed


__all__ = [
    "ACTION_KINDS",
    "COLUMN_DEFAULTS",
    "CREATE_TABLE_DEFAULTS",
    "STATEMENT_KINDS",
    "VOLATILITIES",
    "Unrecognized",
    "classify_default",
    "recognize",
    "unknown",
]
