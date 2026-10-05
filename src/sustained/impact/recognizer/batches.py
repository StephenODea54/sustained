"""
Where one statement's text ends: SQL Server's `GO` batch separators, and
whether each `;` in a statement is inside the body of the trigger,
function, or procedure it creates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Sequence

from sustained.impact.recognizer.cursor import Cursor, depths, named
from sustained.impact.tokens import IDENT, NUMBER, OP, STRING, WORD, Token

if TYPE_CHECKING:
    from sustained.dialects import Dialects


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
    cursor = Cursor("", list(tokens), None)
    if not cursor.accept("CREATE"):
        return -1
    if cursor.is_word("OR"):
        if not (cursor.accept("OR", "REPLACE") or cursor.accept("OR", "ALTER")):
            return -1
    if cursor.accept("DEFINER") and not _definer_user(cursor):
        return -1
    while cursor.accept_any("TEMP", "TEMPORARY", "CONSTRAINT"):
        pass
    return cursor.pos if cursor.is_word(*_ROUTINE_WORDS) else -1


def _definer_user(cursor: Cursor) -> bool:
    """
    Reads `= user[@host]` or `= CURRENT_USER[()]` after DEFINER, and
    says whether it was there.
    """
    if not cursor.accept_op("="):
        return False
    if cursor.accept("CURRENT_USER"):
        return not cursor.accept_punct("(") or cursor.accept_punct(")")
    if not _user_part(cursor.peek()):
        return False
    cursor.pos += 1
    at = cursor.peek()
    if at is None or at.text != "@":
        return True
    cursor.pos += 1
    if not _user_part(cursor.peek()):
        return False
    cursor.pos += 1
    return True


def _user_part(token: Optional[Token]) -> bool:
    """Whether the token can be the user or the host of a DEFINER."""
    return token is not None and token.kind in (WORD, IDENT, STRING)


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


def semicolons_in_body(tokens: Sequence[Token], dialect: Optional["Dialects"]) -> bool:
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


def split_batches(sql: str, tokens: List[Token]) -> List[List[Token]]:
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
