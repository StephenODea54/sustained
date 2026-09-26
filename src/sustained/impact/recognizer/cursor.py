"""
The token reader the recognizer's parser is built on, the words it
matches, and the error it raises at the first thing it cannot read.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
)

from sustained.impact.model import ParsedStatement
from sustained.impact.tokens import (
    IDENT,
    NUMBER,
    OP,
    PUNCT,
    STRING,
    WORD,
    Token,
)

if TYPE_CHECKING:
    from sustained.dialects import Dialects

Options = Dict[str, object]


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
        return token is not None and token.kind == PUNCT and token.text == char

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
        parts: List[str] = []
        while True:
            token = self.peek()
            if token is None or token.name is None:
                raise Unrecognized(f"expected a name {self.where()}")
            parts.append(token.name)
            self.pos += 1
            if not self.accept_punct("."):
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
            if token.kind == PUNCT and token.text == "(":
                depth += 1
            elif token.kind == PUNCT and token.text == ")":
                depth -= 1
        return self.tokens[start : self.pos - 1]

    def rest(self) -> List[Token]:
        """Every token left, consumed."""
        tokens = self.tokens[self.pos :]
        self.pos = len(self.tokens)
        return tokens

    def item(self) -> List[Token]:
        """The tokens up to a comma at this depth or the end, consumed."""
        start = self.pos
        depth = 0
        while not self.at_end():
            token = self.tokens[self.pos]
            if token.kind == PUNCT and token.text in "([":
                depth += 1
            elif token.kind == PUNCT and token.text in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and token.kind == PUNCT and token.text == ",":
                break
            self.pos += 1
        return self.tokens[start : self.pos]

    def expression(self, end_words: frozenset[str]) -> List[Token]:
        """
        An expression: the tokens up to a comma, a closing parenthesis,
        or one of `end_words`, at this depth, consumed. It takes at least
        one token.
        """
        start = self.pos
        depth = 0
        while not self.at_end():
            token = self.tokens[self.pos]
            if token.kind == PUNCT and token.text in "([":
                depth += 1
            elif token.kind == PUNCT and token.text in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and self.pos > start:
                if token.kind == PUNCT and token.text == ",":
                    break
                if token.kind == WORD and token.value in end_words:
                    break
            elif depth == 0 and token.kind == PUNCT and token.text == ",":
                break
            self.pos += 1
        if self.pos == start:
            raise Unrecognized(f"expected an expression {self.where()}")
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
        """Whether any of `words` appears outside parentheses in `tokens`."""
        depth = 0
        for token in tokens:
            if token.kind == PUNCT and token.text == "(":
                depth += 1
            elif token.kind == PUNCT and token.text == ")":
                depth -= 1
            elif depth == 0 and token.is_word(*words):
                return True
        return False

    def mysql_option(self, *words: str) -> Optional[str]:
        """A MySQL `ALGORITHM [=] value` style option, or None."""
        if not self.accept(*words):
            return None
        self.accept_op("=")
        return self.value().upper()

    def finish(self) -> None:
        if not self.at_end():
            raise Unrecognized(f"unread text {self.where()}")

    @property
    def mssql(self) -> bool:
        return self.dialect is not None and self.dialect.name == "MSSQL"

    @staticmethod
    def split_top(tokens: Sequence[Token]) -> List[List[Token]]:
        """Splits tokens on the commas outside parentheses."""
        items: List[List[Token]] = [[]]
        depth = 0
        for token in tokens:
            if token.kind == PUNCT and token.text == "(":
                depth += 1
            elif token.kind == PUNCT and token.text == ")":
                depth -= 1
            elif depth == 0 and token.kind == PUNCT and token.text == ",":
                items.append([])
                continue
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
