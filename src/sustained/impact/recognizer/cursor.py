"""
The token reader the recognizer's parser is built on, the words it
matches, and the error it raises at the first thing it cannot read.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.model import ParsedStatement
from sustained.impact.tokens import (
    IDENT,
    NUMBER,
    OP,
    STRING,
    WORD,
    Token,
)

if TYPE_CHECKING:
    from sustained.dialects import Dialects

Options = Dict[str, object]


def depths(tokens: Sequence[Token], start: int = 0) -> Iterator[Tuple[int, Token, int]]:
    """
    Each token from `start`, with its index and the number of parentheses
    and square brackets open around it. A bracket counts at the depth
    outside it, so the `(` and `)` of a group at the top have depth 0.
    """
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.is_punct(")", "]"):
            depth -= 1
        yield index, token, depth
        if token.is_punct("(", "["):
            depth += 1


def read_name(tokens: Sequence[Token], index: int) -> Tuple[List[str], int]:
    """
    The parts of the dotted name that starts at `index`, and the index
    after it. The parts are empty when no name starts there. A `.` with
    no name after it is read, so the token before the index is that `.`.
    """
    parts: List[str] = []
    while index < len(tokens) and (part := tokens[index].name) is not None:
        parts.append(part)
        index += 1
        if not (index < len(tokens) and tokens[index].is_punct(".")):
            break
        index += 1
    return parts, index


class Unrecognized(Exception):
    """Raised inside the recognizer at the first thing it cannot read."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


COLUMN_CONSTRAINT_WORDS = frozenset(
    {
        "NOT",
        "NULL",
        "DEFAULT",
        "PRIMARY",
        "UNIQUE",
        "REFERENCES",
        "CHECK",
        "CONSTRAINT",
        "GENERATED",
        "COLLATE",
        "IDENTITY",
        "AUTO_INCREMENT",
        "AUTOINCREMENT",
        "COMMENT",
        "FIRST",
        "AFTER",
        "ON",
        "CHARSET",
        "AS",
        "STORED",
        "VIRTUAL",
        "SPARSE",
        "ROWGUIDCOL",
        "INVISIBLE",
        "VISIBLE",
        "FOR",
        "USING",
    }
)
# Words that end a default expression.
DEFAULT_END_WORDS = COLUMN_CONSTRAINT_WORDS - {"NULL", "AS", "USING"} | {"WITH"}
SERIAL_TYPES = frozenset({"SERIAL", "BIGSERIAL", "SMALLSERIAL", "SERIAL4", "SERIAL8"})
OBJECT_WORDS = frozenset(
    {"SCHEMA", "SEQUENCE", "FUNCTION", "PROCEDURE", "EXTENSION", "DOMAIN"}
)
REFERENTIAL_ACTIONS = (
    ("CASCADE",),
    ("RESTRICT",),
    ("NO", "ACTION"),
    ("SET", "NULL"),
    ("SET", "DEFAULT"),
)
LOCK_MODES = (
    ("ACCESS", "SHARE"),
    ("ROW", "SHARE"),
    ("ROW", "EXCLUSIVE"),
    ("SHARE", "UPDATE", "EXCLUSIVE"),
    ("SHARE", "ROW", "EXCLUSIVE"),
    ("SHARE",),
    ("EXCLUSIVE",),
    ("ACCESS", "EXCLUSIVE"),
)


# The words that start a statement on SQL Server, which needs no `;`
# between statements: `UPDATE t SET a = 1 ALTER TABLE u ...` runs both.
# A reader stops before one of them outside parentheses, and the text
# after it makes the statement unknown. `Cursor.starts_statement()`
# reads the words that start a statement only before certain words, such
# as WITH, which starts one only as a CTE.
MSSQL_STATEMENT_WORDS = frozenset(
    {
        "ALTER",
        "BACKUP",
        "BEGIN",
        "BREAK",
        "BULK",
        "CHECKPOINT",
        "CLOSE",
        "COMMIT",
        "CONTINUE",
        "CREATE",
        "DBCC",
        "DEALLOCATE",
        "DECLARE",
        "DELETE",
        "DENY",
        "DROP",
        "EXEC",
        "EXECUTE",
        "FETCH",
        "GOTO",
        "GRANT",
        "IF",
        "INSERT",
        "KILL",
        "MERGE",
        "OPEN",
        "PRINT",
        "RAISERROR",
        "READTEXT",
        "RECONFIGURE",
        "RESTORE",
        "RETURN",
        "REVERT",
        "REVOKE",
        "ROLLBACK",
        "SAVE",
        "SELECT",
        "SET",
        "SETUSER",
        "SHUTDOWN",
        "TRUNCATE",
        "UPDATE",
        "UPDATETEXT",
        "USE",
        "WAITFOR",
        "WHILE",
        "WRITETEXT",
    }
)
# Words that start a SQL Server statement only before one of these.
_MSSQL_STATEMENT_PAIRS = {
    "ENABLE": ("TRIGGER",),
    "DISABLE": ("TRIGGER",),
    "ADD": ("SIGNATURE", "COUNTER"),
    "GET": ("CONVERSATION",),
    "MOVE": ("CONVERSATION",),
    "SEND": ("ON",),
}


def frozen(options: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(options))


class Cursor:
    """
    One pass over one statement's tokens, and the ways of reading them.
    `table` holds the last target table read, so a statement that stops
    part way still names it. The readers of each statement kind extend
    this class; `statement()` is the parser's in the package's
    `__init__.py`.
    """

    def __init__(
        self, sql: str, tokens: List[Token], dialect: Optional["Dialects"]
    ) -> None:
        self.sql = sql
        self.tokens = tokens
        self.dialect = dialect
        self.pos = 0
        self.table: Optional[str] = None

    def peek(self, offset: int = 0) -> Optional[Token]:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def at_end(self) -> bool:
        return self.pos >= len(self.tokens)

    def is_words(self, *words: str) -> bool:
        """Whether the next tokens are these bare words, in order."""
        for offset, word in enumerate(words):
            token = self.peek(offset)
            if token is None or not token.is_word(word):
                return False
        return True

    def is_word(self, *choices: str) -> bool:
        """Whether the next token is a bare word, one of `choices`."""
        token = self.peek()
        return token is not None and token.is_word(*choices)

    def accept(self, *words: str) -> bool:
        if self.is_words(*words):
            self.pos += len(words)
            return True
        return False

    def expect(self, *words: str) -> None:
        if not self.accept(*words):
            raise Unrecognized(f"expected {' '.join(words)} {self.where()}")

    def accept_any(self, *choices: str) -> Optional[str]:
        """Consumes one bare word from `choices` and returns it."""
        token = self.peek()
        if token is not None and token.is_word(*choices):
            self.pos += 1
            return token.value
        return None

    def is_punct(self, char: str) -> bool:
        token = self.peek()
        return token is not None and token.is_punct(char)

    def accept_punct(self, char: str) -> bool:
        if self.is_punct(char):
            self.pos += 1
            return True
        return False

    def expect_punct(self, char: str) -> None:
        if not self.accept_punct(char):
            raise Unrecognized(f"expected '{char}' {self.where()}")

    def accept_op(self, op: str) -> bool:
        token = self.peek()
        if token is not None and token.kind == OP and token.text == op:
            self.pos += 1
            return True
        return False

    def next(self) -> Token:
        token = self.peek()
        if token is None:
            raise Unrecognized("the statement ends early")
        self.pos += 1
        return token

    def where(self) -> str:
        token = self.peek()
        return "at the end" if token is None else f"at '{token.text}'"

    def is_name(self) -> bool:
        token = self.peek()
        return token is not None and token.kind in (WORD, IDENT)

    def name_parts(self) -> List[str]:
        """A dotted name: one or more identifiers, bare or quoted."""
        parts, self.pos = read_name(self.tokens, self.pos)
        if not parts or self.tokens[self.pos - 1].is_punct("."):
            raise Unrecognized(f"expected a name {self.where()}")
        return parts

    def name(self) -> str:
        return ".".join(self.name_parts())

    def target(self) -> str:
        """The statement's target table, remembered for an unknown statement."""
        self.table = self.name()
        return self.table

    def group(self) -> List[Token]:
        """A parenthesized group, consumed; returns the tokens inside."""
        self.expect_punct("(")
        start = self.pos
        depth = 1
        while depth:
            token = self.next()
            if token.is_punct("("):
                depth += 1
            elif token.is_punct(")"):
                depth -= 1
        return self.tokens[start : self.pos - 1]

    def body(self) -> List[Token]:
        """
        Every token left, consumed: the body of a view, a routine, or a
        trigger, which the server stores and does not run.
        """
        tokens = self.tokens[self.pos :]
        self.pos = len(self.tokens)
        return tokens

    def rest(self) -> List[Token]:
        """
        Every token left, consumed, up to a word that starts another
        statement on SQL Server outside parentheses. The caller's
        `finish()` reports the text from that word on.
        """
        start = self.pos
        depth = 0
        while not self.at_end():
            token = self.tokens[self.pos]
            if token.is_punct("("):
                depth += 1
            elif token.is_punct(")"):
                depth -= 1
            elif depth == 0 and self.starts_statement(self.pos):
                break
            self.pos += 1
        return self.tokens[start : self.pos]

    def starts_statement(self, index: int) -> bool:
        """
        Whether the token at `index` starts another statement, which only
        SQL Server lets follow a statement with no `;` between the two.
        WITH starts one as a CTE, `WITH name [(columns)] AS (`, SELECT
        does not after UNION, EXCEPT, INTERSECT, or ALL, and FETCH does
        not after the ROWS of an OFFSET.
        """
        if not self.mssql:
            return False
        token = self.tokens[index]
        if token.kind != WORD:
            return False
        before = self.tokens[index - 1] if index > 0 else None
        after = self.tokens[index + 1] if index + 1 < len(self.tokens) else None
        word = token.value
        if word == "WITH":
            return self.cte_at(index + 1)
        if word in _MSSQL_STATEMENT_PAIRS:
            return after is not None and after.is_word(*_MSSQL_STATEMENT_PAIRS[word])
        if word == "SELECT" and before is not None:
            return not before.is_word("UNION", "EXCEPT", "INTERSECT", "ALL")
        if word == "FETCH" and before is not None:
            return not before.is_word("ROWS", "ROW")
        return word in MSSQL_STATEMENT_WORDS

    def cte_at(self, index: int) -> bool:
        """Whether the tokens from `index` read `name [(columns)] AS (`."""
        tokens = self.tokens
        if index >= len(tokens) or tokens[index].name is None:
            return False
        index += 1
        if index < len(tokens) and tokens[index].is_punct("("):
            depth = 0
            while index < len(tokens):
                if tokens[index].is_punct("("):
                    depth += 1
                elif tokens[index].is_punct(")"):
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            index += 1
        return (
            index + 1 < len(tokens)
            and tokens[index].is_word("AS")
            and tokens[index + 1].is_punct("(")
        )

    def item(self) -> List[Token]:
        """The tokens up to a comma at this depth or the end, consumed."""
        start = self.pos
        depth = 0
        while not self.at_end():
            token = self.tokens[self.pos]
            if depth == 0 and self.starts_statement(self.pos):
                break
            if token.is_punct("(", "["):
                depth += 1
            elif token.is_punct(")", "]"):
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and token.is_punct(","):
                break
            self.pos += 1
        return self.tokens[start : self.pos]

    def expression(self, end_words: frozenset[str]) -> List[Token]:
        """
        An expression: the tokens up to a comma, a closing parenthesis,
        one of `end_words`, or a word that starts another statement on SQL
        Server, at this depth, consumed. It takes at least one token.
        """
        start = self.pos
        depth = 0
        while not self.at_end():
            token = self.tokens[self.pos]
            if token.is_punct("(", "["):
                depth += 1
            elif token.is_punct(")", "]"):
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and self.pos > start:
                if token.is_punct(","):
                    break
                if token.kind == WORD and token.value in end_words:
                    break
                if self.starts_statement(self.pos):
                    break
            elif depth == 0 and token.is_punct(","):
                break
            self.pos += 1
        if self.pos == start:
            raise Unrecognized(f"expected an expression {self.where()}")
        return self.tokens[start : self.pos]

    def up_to_word(self, *words: str) -> List[Token]:
        """
        The tokens up to one of the words, or a word that starts another
        statement on SQL Server, outside parentheses, consumed.
        """
        start = self.pos
        depth = 0
        while not self.at_end():
            token = self.tokens[self.pos]
            if token.is_punct("("):
                depth += 1
            elif token.is_punct(")"):
                depth -= 1
            elif depth == 0 and token.kind == WORD and token.value in words:
                break
            elif depth == 0 and self.starts_statement(self.pos):
                break
            self.pos += 1
        return self.tokens[start : self.pos]

    def text(self, tokens: Sequence[Token]) -> str:
        """The source text the tokens span, as the statement spells it."""
        if not tokens:
            return ""
        last = tokens[-1]
        return self.sql[tokens[0].start : last.start + len(last.text)]

    def value(self) -> str:
        """A single-token value: a literal's contents or a word's text."""
        token = self.next()
        if token.kind == STRING:
            return token.value
        if token.kind in (WORD, IDENT, NUMBER):
            return token.name or token.text
        raise Unrecognized(f"expected a value at '{token.text}'")

    def names(self) -> List[str]:
        """A comma-separated list of names."""
        names = [self.name()]
        while self.accept_punct(","):
            names.append(self.name())
        return names

    def top_level_word(self, tokens: Sequence[Token], *words: str) -> bool:
        """Whether any of `words` appears outside brackets in `tokens`."""
        return any(d == 0 and t.is_word(*words) for _, t, d in depths(tokens))

    def mysql_option(self, *words: str) -> Optional[str]:
        """A MySQL `ALGORITHM [=] value` style option, or None."""
        if not self.accept(*words):
            return None
        self.accept_op("=")
        return self.value().upper()

    def wait(self) -> Optional[str]:
        """
        MariaDB's `WAIT n` or `NOWAIT`, the seconds the statement waits
        for its metadata lock, as text: `0` for NOWAIT. None when
        neither is next.
        """
        if self.accept("NOWAIT"):
            return "0"
        if self.accept("WAIT"):
            return self.value()
        return None

    def finish(self) -> None:
        if not self.at_end():
            raise Unrecognized(f"unread text {self.where()}")

    @property
    def mssql(self) -> bool:
        return self.dialect is not None and self.dialect.name == "MSSQL"

    @property
    def postgres(self) -> bool:
        return self.dialect is not None and self.dialect.name == "POSTGRES"

    @property
    def sqlite(self) -> bool:
        """Whether the dialect is DEFAULT, which SQLite connections use."""
        return self.dialect is not None and self.dialect.name == "DEFAULT"

    @staticmethod
    def split_top(tokens: Sequence[Token]) -> List[List[Token]]:
        """Splits tokens on the commas outside brackets."""
        items: List[List[Token]] = [[]]
        for _, token, depth in depths(tokens):
            if depth == 0 and token.is_punct(","):
                items.append([])
            else:
                items[-1].append(token)
        return [item for item in items if item]

    def with_options(self) -> Dict[str, str]:
        """A `WITH (key = value, ...)` list, keys in upper case."""
        options: Dict[str, str] = {}
        for item in self.split_top(self.group()):
            if not item or item[0].kind not in (WORD, IDENT):
                raise Unrecognized("expected a WITH option name")
            key = (item[0].name or "").upper()
            value = item[2:] if len(item) > 2 and item[1].text == "=" else item[1:]
            options[key] = self.text(value).upper() if value else "ON"
        return options

    def expect_one_of_words(self, *words: str) -> str:
        found = self.accept_any(*words)
        if found is None:
            raise Unrecognized(f"expected {' or '.join(words)} {self.where()}")
        return found

    def statement(self) -> ParsedStatement:
        """One whole statement; the parser that extends this class reads it."""
        raise NotImplementedError
