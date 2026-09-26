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
  algorithm, lock, fulltext
- `drop_index`: name, names, concurrently, if_exists, algorithm, lock
- `alter_table`: actions, plus if_exists, only, algorithm, lock, and
  nocheck (SQL Server WITH NOCHECK)
- `create_table`: if_not_exists, temporary, references, partition_of,
  as_select
- `drop_type`: names
- `drop_table`, `truncate`, `drop_view`, `optimize_table`,
  `lock_table`, `vacuum`, `analyze`: tables, plus if_exists where the
  statement may spell it, `mode` and `nowait` for LOCK, `full` for
  VACUUM
- `rename_table`: new, renames (every old and new pair)
- `update`, `delete`: where, limited (a LIMIT or TOP caps the rows)
- `insert`: source (`values`, `select`, or `default`), rows (the row
  count of a VALUES list)
- `reindex`: target (`index`, `table`, ...), name, concurrently
- `cluster`: index
- `refresh_materialized_view`: concurrently, with_data
- `create_trigger`, `drop_trigger`: name
- `comment_on`: object, column
- `create_type`: enum; `alter_type_add_value`, `alter_type_rename_value`
- `create_view`: materialized
- `create_object`, `drop_object`: object, such as `schema`, `sequence`,
  `function`, `procedure`, `extension`, or `domain`
- `set`: settings, a tuple of (scope, name, value), where scope is
  `session`, `local`, `global`, `persist`, or `pragma`, and the name is
  lower case

An ALTER TABLE action's kind is one of `ACTION_KINDS`; the options each
one sets are named where it is read below.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.model import UNKNOWN_KIND, Action, ParsedStatement
from sustained.impact.tokens import (
    ERROR,
    IDENT,
    NUMBER,
    OP,
    PUNCT,
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
        "enable_trigger",
        "disable_trigger",
        "row_security",
        "engine",
        "convert_charset",
        "force",
    }
)

_Options = Dict[str, object]


class Unrecognized(Exception):
    """Raised inside the recognizer at the first thing it cannot read."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- default volatility ------------------------------------------------

# Calls whose value is fixed for the statement, so a column default made
# of them is computed once and stored in the catalog. Stable functions
# such as now() count, since Postgres evaluates the default once at
# ALTER time.
_NON_VOLATILE_CALLS = frozenset(
    {
        "now",
        "transaction_timestamp",
        "statement_timestamp",
        "current_timestamp",
        "current_date",
        "localtimestamp",
        "current_setting",
        "current_user",
        "cast",
        "coalesce",
        "nullif",
        "greatest",
        "least",
        "lower",
        "upper",
        "trim",
        "length",
        "abs",
        "round",
        "floor",
        "ceil",
        "concat",
        "make_date",
        "make_time",
        "make_timestamp",
        "make_timestamptz",
        "make_interval",
        "to_char",
        "to_date",
        "to_timestamp",
        "date_trunc",
        "timezone",
        "jsonb_build_object",
        "jsonb_build_array",
        "json_build_object",
        "json_build_array",
        "array",
        "row",
        "getdate",
        "getutcdate",
        "sysdatetime",
        "sysutcdatetime",
        "sysdatetimeoffset",
        "utc_timestamp",
        "curdate",
        "curtime",
        "datetime",
        "date",
        "time",
        "strftime",
        "julianday",
        "today",
    }
)
# Calls known to give a new value per row, which forces a rewrite.
_VOLATILE_CALLS = frozenset(
    {
        "random",
        "gen_random_uuid",
        "uuid_generate_v1",
        "uuid_generate_v1mc",
        "uuid_generate_v4",
        "uuidv4",
        "uuidv7",
        "clock_timestamp",
        "timeofday",
        "nextval",
        "newid",
        "newsequentialid",
        "uuid",
        "rand",
    }
)
# Bare words a default may hold that read the clock or the session once.
_STABLE_WORDS = frozenset(
    {
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "LOCALTIME",
        "LOCALTIMESTAMP",
        "CURRENT_USER",
        "SESSION_USER",
        "USER",
        "CURRENT_SCHEMA",
        "CURRENT_CATALOG",
        "CURRENT_ROLE",
    }
)

VOLATILITIES = ("constant", "stable", "volatile")


def classify_default(tokens: Sequence[Token]) -> Tuple[str, Optional[str], bool]:
    """
    How a column default behaves when a column is added with it:
    `constant`, `stable` (read once, such as now()), or `volatile` (a
    new value per row, such as random()). Also returns the function
    that decided it, and whether the answer is certain.

    A function the recognizer does not know counts as volatile, which is
    the worst case, and the answer is then not certain.
    """
    unknown: Optional[str] = None
    stable = False
    for index, token in enumerate(tokens):
        if token.kind != WORD:
            continue
        after_cast = index > 0 and tokens[index - 1].text == "::"
        is_call = (
            index + 1 < len(tokens)
            and tokens[index + 1].text == "("
            and tokens[index + 1].kind == PUNCT
        )
        if after_cast:
            continue
        if not is_call:
            stable = stable or token.value in _STABLE_WORDS
            continue
        name = token.text.lower()
        if name in _VOLATILE_CALLS:
            return "volatile", name, True
        if name in _NON_VOLATILE_CALLS:
            stable = True
        elif unknown is None:
            unknown = name
    if unknown is not None:
        return "volatile", unknown, False
    return ("stable" if stable else "constant"), None, True


# --- the parser --------------------------------------------------------

# Words that end a column's type and open one of its constraints.
_COLUMN_CONSTRAINT_WORDS = frozenset(
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
_DEFAULT_END_WORDS = _COLUMN_CONSTRAINT_WORDS - {"NULL", "AS", "USING"} | {"WITH"}
_SERIAL_TYPES = frozenset({"SERIAL", "BIGSERIAL", "SMALLSERIAL", "SERIAL4", "SERIAL8"})
_OBJECT_WORDS = frozenset(
    {"SCHEMA", "SEQUENCE", "FUNCTION", "PROCEDURE", "EXTENSION", "DOMAIN"}
)
_REFERENTIAL_ACTIONS = (
    ("CASCADE",),
    ("RESTRICT",),
    ("NO", "ACTION"),
    ("SET", "NULL"),
    ("SET", "DEFAULT"),
)
_LOCK_MODES = (
    ("ACCESS", "SHARE"),
    ("ROW", "SHARE"),
    ("ROW", "EXCLUSIVE"),
    ("SHARE", "UPDATE", "EXCLUSIVE"),
    ("SHARE", "ROW", "EXCLUSIVE"),
    ("SHARE",),
    ("EXCLUSIVE",),
    ("ACCESS", "EXCLUSIVE"),
)


def _frozen(options: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(options))


class _Parser:
    """
    One pass over one statement's tokens. `table` holds the last target
    table read, so a statement that stops part way still names it.
    """

    def __init__(
        self, sql: str, tokens: List[Token], dialect: Optional["Dialects"]
    ) -> None:
        self.sql = sql
        self.tokens = tokens
        self.dialect = dialect
        self.pos = 0
        self.table: Optional[str] = None

    # --- reading tokens ------------------------------------------------

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

    # --- statements ----------------------------------------------------

    def statement(self) -> ParsedStatement:
        token = self.peek()
        if token is None or token.kind != WORD:
            raise Unrecognized("the statement does not start with a keyword")
        handler = _STATEMENTS.get(token.value)
        if handler is None:
            raise Unrecognized(f"no rule reads a {token.value} statement")
        self.pos += 1
        return handler(self)

    def finish(self) -> None:
        if not self.at_end():
            raise Unrecognized(f"unread text {self.where()}")

    # CREATE ...

    def create(self) -> ParsedStatement:
        self.accept("OR", "REPLACE")
        if self.is_word("DEFINER"):
            self.definer()
        unique = self.accept("UNIQUE")
        clustered = self.accept_any("CLUSTERED", "NONCLUSTERED")
        fulltext = self.accept_any("FULLTEXT", "SPATIAL")
        if self.accept("INDEX"):
            return self.create_index(unique, fulltext is not None)
        if unique or clustered or fulltext:
            raise Unrecognized(f"expected INDEX {self.where()}")
        temporary = bool(self.accept_any("TEMP", "TEMPORARY", "UNLOGGED"))
        if self.accept("TABLE"):
            return self.create_table(temporary)
        materialized = self.accept("MATERIALIZED")
        if self.accept("VIEW"):
            self.rest()
            return ParsedStatement(
                "create_view", options=_frozen({"materialized": materialized})
            )
        if self.accept("TRIGGER") or self.accept("CONSTRAINT", "TRIGGER"):
            return self.create_trigger()
        if self.accept("TYPE"):
            return self.create_type()
        found = self.accept_any(*_OBJECT_WORDS)
        if found:
            self.rest()
            return ParsedStatement(
                "create_object", options=_frozen({"object": found.lower()})
            )
        raise Unrecognized(f"no rule reads CREATE {self.where()}")

    def definer(self) -> None:
        """MySQL's `DEFINER = user@host` before TRIGGER or VIEW."""
        self.expect("DEFINER")
        while not self.at_end() and not self.is_word(
            "TRIGGER", "VIEW", "PROCEDURE", "FUNCTION", "EVENT"
        ):
            self.pos += 1

    def create_index(self, unique: bool, fulltext: bool) -> ParsedStatement:
        options: _Options = {"unique": unique, "fulltext": fulltext}
        options["concurrently"] = self.accept("CONCURRENTLY")
        options["if_not_exists"] = self.accept("IF", "NOT", "EXISTS")
        options["name"] = None if self.is_word("ON") else self.name()
        self.expect("ON")
        options["only"] = self.accept("ONLY")
        table = self.target()
        if self.accept("USING"):
            options["using"] = self.value().lower()
        columns = self.group()
        options["columns"] = len(self.split_top(columns))
        self.index_tail(options)
        return ParsedStatement("create_index", table, options=_frozen(options))

    def index_tail(self, options: _Options) -> None:
        """What may follow an index's column list, in any order."""
        options.setdefault("partial", False)
        options.setdefault("with", {})
        while not self.at_end():
            if self.accept("INCLUDE"):
                self.group()
            elif self.accept("NULLS"):
                self.accept("NOT")
                self.expect("DISTINCT")
            elif self.accept("WITH"):
                options["with"] = self.with_options()
            elif self.accept("TABLESPACE"):
                self.name()
            elif self.accept("WHERE"):
                self.rest()
                options["partial"] = True
            elif self.is_word("ALGORITHM"):
                options["algorithm"] = self.mysql_option("ALGORITHM")
            elif self.is_word("LOCK"):
                options["lock"] = self.mysql_option("LOCK")
            elif self.accept("ON"):
                # SQL Server: ON a filegroup or partition scheme.
                self.name()
                if self.is_punct("("):
                    self.group()
            else:
                raise Unrecognized(f"no rule reads the index option {self.where()}")

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

    def create_table(self, temporary: bool) -> ParsedStatement:
        options: _Options = {"temporary": temporary}
        options["if_not_exists"] = self.accept("IF", "NOT", "EXISTS")
        table = self.target()
        options["references"] = ()
        options["partition_of"] = None
        options["as_select"] = False
        if self.accept("PARTITION", "OF"):
            options["partition_of"] = self.name()
        if self.is_punct("("):
            options["references"] = self.references_in(self.group())
        if self.accept("AS") or self.top_level_word(
            self.tokens[self.pos :], "AS", "SELECT"
        ):
            options["as_select"] = True
        # The tail holds storage options, which change nothing about a
        # table that does not exist yet.
        self.rest()
        return ParsedStatement("create_table", table, options=_frozen(options))

    def references_in(self, tokens: Sequence[Token]) -> Tuple[str, ...]:
        """The tables a CREATE TABLE body's foreign keys point at."""
        found: List[str] = []
        for index, token in enumerate(tokens):
            if not token.is_word("REFERENCES"):
                continue
            parts: List[str] = []
            at = index + 1
            while at < len(tokens) and tokens[at].name is not None:
                parts.append(tokens[at].name or "")
                at += 1
                if not (at < len(tokens) and tokens[at].text == "."):
                    break
                at += 1
            if parts:
                found.append(".".join(parts))
        return tuple(found)

    def create_trigger(self) -> ParsedStatement:
        self.accept("IF", "NOT", "EXISTS")
        name = self.name()
        tokens = self.rest()
        depth = 0
        for index, token in enumerate(tokens):
            if token.kind == PUNCT and token.text in "()":
                depth += 1 if token.text == "(" else -1
            elif depth == 0 and token.is_word("ON"):
                sub = _Parser(self.sql, list(tokens[index + 1 :]), self.dialect)
                table = sub.name()
                self.table = table
                return ParsedStatement(
                    "create_trigger", table, options=_frozen({"name": name})
                )
        raise Unrecognized("expected ON <table> in CREATE TRIGGER")

    def create_type(self) -> ParsedStatement:
        self.name()
        enum = self.accept("AS", "ENUM")
        self.rest()
        return ParsedStatement("create_type", options=_frozen({"enum": enum}))

    # DROP ...

    def drop(self) -> ParsedStatement:
        if self.accept("INDEX"):
            return self.drop_index()
        if self.accept("TABLE"):
            return self.drop_many("drop_table")
        materialized = self.accept("MATERIALIZED")
        if self.accept("VIEW"):
            parsed = self.drop_many("drop_view")
            return parsed._replace(
                options=_frozen({**parsed.options, "materialized": materialized})
            )
        if self.accept("TRIGGER"):
            return self.drop_trigger()
        if self.accept("TYPE"):
            self.accept("IF", "EXISTS")
            names = self.names()
            self.accept_any("CASCADE", "RESTRICT")
            return ParsedStatement(
                "drop_type", options=_frozen({"names": tuple(names)})
            )
        found = self.accept_any(*_OBJECT_WORDS)
        if found:
            self.rest()
            return ParsedStatement(
                "drop_object", options=_frozen({"object": found.lower()})
            )
        raise Unrecognized(f"no rule reads DROP {self.where()}")

    def drop_many(self, kind: str) -> ParsedStatement:
        if_exists = self.accept("IF", "EXISTS")
        tables = self.names()
        self.table = tables[0]
        self.accept_any("CASCADE", "RESTRICT")
        return ParsedStatement(
            kind,
            tables[0],
            options=_frozen({"tables": tuple(tables), "if_exists": if_exists}),
        )

    def drop_index(self) -> ParsedStatement:
        options: _Options = {}
        options["concurrently"] = self.accept("CONCURRENTLY")
        options["if_exists"] = self.accept("IF", "EXISTS")
        parts = self.name_parts()
        names = [parts]
        while self.accept_punct(","):
            names.append(self.name_parts())
        table: Optional[str] = None
        if self.accept("ON"):
            table = self.target()
        elif self.mssql and len(parts) >= 2:
            # SQL Server's older form names the index as table.index.
            table = ".".join(parts[:-1])
            parts = parts[-1:]
            self.table = table
        options["name"] = ".".join(parts)
        options["names"] = tuple(".".join(n) for n in names)
        self.accept_any("CASCADE", "RESTRICT")
        while not self.at_end():
            if self.is_word("ALGORITHM"):
                options["algorithm"] = self.mysql_option("ALGORITHM")
            elif self.is_word("LOCK"):
                options["lock"] = self.mysql_option("LOCK")
            else:
                raise Unrecognized(f"unread text {self.where()}")
        return ParsedStatement("drop_index", table, options=_frozen(options))

    def drop_trigger(self) -> ParsedStatement:
        self.accept("IF", "EXISTS")
        name = self.name()
        table: Optional[str] = None
        if self.accept("ON"):
            table = self.target()
        self.accept_any("CASCADE", "RESTRICT")
        return ParsedStatement("drop_trigger", table, options=_frozen({"name": name}))

    @property
    def mssql(self) -> bool:
        return self.dialect is not None and self.dialect.name == "MSSQL"

    # ALTER ...

    def alter(self) -> ParsedStatement:
        if self.accept("TABLE"):
            return self.alter_table()
        if self.accept("TYPE"):
            return self.alter_type()
        raise Unrecognized(f"no rule reads ALTER {self.where()}")

    def alter_type(self) -> ParsedStatement:
        self.name()
        if self.accept("ADD", "VALUE"):
            self.accept("IF", "NOT", "EXISTS")
            value = self.value()
            if self.accept_any("BEFORE", "AFTER"):
                self.value()
            return ParsedStatement(
                "alter_type_add_value", options=_frozen({"value": value})
            )
        if self.accept("RENAME", "VALUE"):
            self.value()
            self.expect("TO")
            self.value()
            return ParsedStatement("alter_type_rename_value")
        raise Unrecognized(f"no rule reads ALTER TYPE {self.where()}")

    def alter_table(self) -> ParsedStatement:
        options: _Options = {}
        options["if_exists"] = self.accept("IF", "EXISTS")
        options["only"] = self.accept("ONLY")
        table = self.target()
        self.accept_op("*")
        actions: List[Action] = []
        while True:
            action = self.alter_action(options, actions)
            if action is not None:
                actions.append(action)
            if not self.accept_punct(","):
                break
        self.finish()
        if not actions:
            raise Unrecognized("the ALTER TABLE has no action")
        return ParsedStatement("alter_table", table, tuple(actions), _frozen(options))

    def alter_action(
        self, options: _Options, actions: Sequence[Action]
    ) -> Optional[Action]:
        """
        One ALTER TABLE action, or None for a MySQL table option such as
        ALGORITHM, which the statement's options record instead.
        """
        token = self.peek()
        if token is None:
            raise Unrecognized("the ALTER TABLE ends early")
        if token.is_word("ALGORITHM", "LOCK"):
            options[token.value.lower()] = self.mysql_option(token.value)
            return None
        if token.is_word("WITH") and self.peek(1) is not None:
            # SQL Server: WITH CHECK or WITH NOCHECK before the action.
            self.pos += 1
            checked = self.accept_any("CHECK", "NOCHECK")
            if checked is None:
                raise Unrecognized(f"expected CHECK or NOCHECK {self.where()}")
            options["nocheck"] = checked == "NOCHECK"
            return self.alter_action(options, actions)
        handler = _ACTIONS.get(token.value) if token.kind == WORD else None
        if handler is not None:
            self.pos += 1
            return handler(self)
        if self.mssql and actions and actions[-1].kind in ("add_column", "drop_column"):
            # SQL Server lists more columns after one ADD or DROP COLUMN.
            if actions[-1].kind == "drop_column":
                return Action("drop_column", self.name(), _frozen({}))
            return self.column_action("add_column")
        raise Unrecognized(f"no rule reads the ALTER TABLE action {self.where()}")

    def action_add(self) -> Action:
        if self.accept("CONSTRAINT"):
            return self.constraint(self.name())
        if self.is_word("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "EXCLUDE", "DEFAULT"):
            return self.constraint(None)
        kind = self.accept_any("INDEX", "KEY", "FULLTEXT", "SPATIAL")
        if kind is not None:
            return self.add_index(kind)
        if self.is_word("COLUMNS"):
            raise Unrecognized("no rule reads ADD COLUMNS")
        self.accept("COLUMN")
        if_not_exists = self.accept("IF", "NOT", "EXISTS")
        action = self.column_action("add_column")
        if if_not_exists:
            action = action._replace(
                options=_frozen({**action.options, "if_not_exists": True})
            )
        return action

    def add_index(self, kind: str) -> Action:
        fulltext = kind in ("FULLTEXT", "SPATIAL")
        if fulltext:
            self.accept_any("INDEX", "KEY")
        name = None if self.is_punct("(") else self.name()
        if self.accept("USING"):
            self.value()
        self.group()
        options: _Options = {"name": name, "fulltext": fulltext}
        return Action("add_index", None, _frozen(options))

    def constraint(self, name: Optional[str]) -> Action:
        options: _Options = {"name": name, "not_valid": False, "using_index": None}
        if self.accept("DEFAULT"):
            # SQL Server: ADD [CONSTRAINT name] DEFAULT expr FOR column.
            default = self.expression(frozenset({"FOR"}))
            self.expect("FOR")
            column = self.name()
            return Action(
                "set_default", column, _frozen({"default": self.text(default)})
            )
        if self.accept("PRIMARY", "KEY"):
            options["constraint"] = "primary_key"
            self.key_columns(options)
        elif self.accept("UNIQUE"):
            options["constraint"] = "unique"
            self.accept_any("KEY", "INDEX")
            if self.is_name() and not self.is_word(
                "USING", "NULLS", "CLUSTERED", "NONCLUSTERED"
            ):
                options["name"] = options["name"] or self.name()
            self.key_columns(options)
        elif self.accept("FOREIGN", "KEY"):
            options["constraint"] = "foreign_key"
            if self.is_name():
                options["name"] = options["name"] or self.name()
            self.group()
            self.expect("REFERENCES")
            options["references"] = self.name()
            if self.is_punct("("):
                self.group()
            self.foreign_key_tail()
        elif self.accept("CHECK"):
            options["constraint"] = "check"
            self.group()
        elif self.accept("EXCLUDE"):
            options["constraint"] = "exclude"
            if self.accept("USING"):
                self.value()
            self.group()
        else:
            raise Unrecognized(f"no rule reads the constraint {self.where()}")
        self.constraint_tail(options)
        return Action("add_constraint", None, _frozen(options))

    def key_columns(self, options: _Options) -> None:
        """The column list of a key, or Postgres's USING INDEX form."""
        self.accept_any("CLUSTERED", "NONCLUSTERED")
        if self.accept("NULLS"):
            self.accept("NOT")
            self.expect("DISTINCT")
        if self.accept("USING", "INDEX"):
            options["using_index"] = self.name()
            return
        self.group()

    def foreign_key_tail(self) -> None:
        while True:
            if self.accept("ON"):
                self.expect_one_of_words("DELETE", "UPDATE")
                if not any(self.accept(*words) for words in _REFERENTIAL_ACTIONS):
                    raise Unrecognized(f"expected a referential action {self.where()}")
                if self.is_punct("("):
                    self.group()
            elif self.accept("MATCH"):
                self.expect_one_of_words("FULL", "PARTIAL", "SIMPLE")
            else:
                return

    def expect_one_of_words(self, *words: str) -> str:
        found = self.accept_any(*words)
        if found is None:
            raise Unrecognized(f"expected {' or '.join(words)} {self.where()}")
        return found

    def constraint_tail(self, options: _Options) -> None:
        """What may follow a table constraint, in any order."""
        while True:
            if self.accept("NOT", "VALID"):
                options["not_valid"] = True
            elif self.accept("NOT", "DEFERRABLE") or self.accept("DEFERRABLE"):
                pass
            elif self.accept("INITIALLY"):
                self.expect_one_of_words("DEFERRED", "IMMEDIATE")
            elif self.accept("NO", "INHERIT") or self.accept("NOT", "ENFORCED"):
                pass
            elif self.accept("ENFORCED"):
                pass
            elif self.accept("INCLUDE"):
                self.group()
            elif self.accept("USING", "INDEX", "TABLESPACE"):
                self.name()
            elif self.accept("WITH"):
                options["with"] = self.with_options()
            elif self.accept("WHERE"):
                self.group()
            else:
                return

    def action_drop(self) -> Action:
        if self.accept("CONSTRAINT"):
            if_exists = self.accept("IF", "EXISTS")
            name = self.name()
            self.accept_any("CASCADE", "RESTRICT")
            options: _Options = {"name": name, "if_exists": if_exists}
            return Action("drop_constraint", None, _frozen(options))
        if self.accept("FOREIGN", "KEY"):
            return Action(
                "drop_constraint",
                None,
                _frozen({"name": self.name(), "constraint": "foreign_key"}),
            )
        if self.accept("PRIMARY", "KEY"):
            return Action(
                "drop_constraint", None, _frozen({"constraint": "primary_key"})
            )
        if self.accept("CHECK"):
            return Action(
                "drop_constraint",
                None,
                _frozen({"name": self.name(), "constraint": "check"}),
            )
        if self.accept_any("INDEX", "KEY"):
            return Action("drop_index", None, _frozen({"name": self.name()}))
        if self.is_word("PARTITION", "DEFAULT"):
            raise Unrecognized(f"no rule reads DROP {self.where()}")
        self.accept("COLUMN")
        if_exists = self.accept("IF", "EXISTS")
        column = self.name()
        self.accept_any("CASCADE", "RESTRICT")
        return Action("drop_column", column, _frozen({"if_exists": if_exists}))

    def action_alter(self) -> Action:
        self.accept("COLUMN")
        if self.is_word("CONSTRAINT", "INDEX", "CHECK"):
            raise Unrecognized(f"no rule reads ALTER {self.where()}")
        column = self.name()
        if self.accept("TYPE") or self.accept("SET", "DATA", "TYPE"):
            return self.alter_column_type(column)
        simple = (
            (("SET", "NOT", "NULL"), "set_not_null"),
            (("DROP", "NOT", "NULL"), "drop_not_null"),
            (("DROP", "DEFAULT"), "drop_default"),
        )
        for words, kind in simple:
            if self.accept(*words):
                return Action(kind, column, _frozen({}))
        if self.accept("SET", "DEFAULT"):
            default = self.expression(frozenset())
            return Action("set_default", column, _frozen(self.default_options(default)))
        if self.accept("SET", "STATISTICS"):
            self.value()
            return Action("set_statistics", column, _frozen({}))
        if self.accept("SET", "STORAGE"):
            self.value()
            return Action("set_storage", column, _frozen({}))
        if self.accept("SET", "VISIBLE") or self.accept("SET", "INVISIBLE"):
            return Action("set_storage", column, _frozen({}))
        if self.is_word("SET", "DROP", "ADD", "RESET", "OPTIONS"):
            raise Unrecognized(f"no rule reads ALTER COLUMN {self.where()}")
        # SQL Server restates the column: ALTER COLUMN c type [NULL].
        options = self.column_definition()
        if self.accept("WITH"):
            options["with"] = self.with_options()
        return Action("alter_column", column, _frozen(options))

    def alter_column_type(self, column: str) -> Action:
        type_tokens = self.column_type()
        options: _Options = {"type": self.text(type_tokens)}
        if self.accept("COLLATE"):
            options["collate"] = self.name()
        options["using"] = None
        if self.accept("USING"):
            options["using"] = self.text(self.expression(frozenset()))
        return Action("alter_column_type", column, _frozen(options))

    def action_modify(self) -> Action:
        self.accept("COLUMN")
        return self.column_action("modify_column")

    def action_change(self) -> Action:
        self.accept("COLUMN")
        old = self.name()
        new = self.name()
        options = self.column_definition()
        options["new"] = new
        return Action("change_column", old, _frozen(options))

    def action_rename(self) -> Action:
        if self.accept("COLUMN"):
            return self.rename_pair("rename_column")
        if self.accept_any("TO", "AS"):
            return Action("rename_to", None, _frozen({"new": self.name()}))
        if self.accept("CONSTRAINT"):
            return self.rename_pair("rename_constraint")
        if self.accept_any("INDEX", "KEY"):
            return self.rename_pair("rename_index")
        return self.rename_pair("rename_column")

    def rename_pair(self, kind: str) -> Action:
        old = self.name()
        self.expect("TO")
        new = self.name()
        column = old if kind == "rename_column" else None
        return Action(kind, column, _frozen({"old": old, "new": new}))

    def action_validate(self) -> Action:
        self.expect("CONSTRAINT")
        return Action("validate_constraint", None, _frozen({"name": self.name()}))

    def action_check(self) -> Action:
        # SQL Server: [WITH CHECK] CHECK CONSTRAINT name, which enables
        # the constraint, and checks the rows when WITH CHECK leads.
        self.expect("CONSTRAINT")
        return Action("enable_constraint", None, _frozen({"name": self.name()}))

    def action_nocheck(self) -> Action:
        self.expect("CONSTRAINT")
        return Action("disable_constraint", None, _frozen({"name": self.name()}))

    def action_attach(self) -> Action:
        self.expect("PARTITION")
        partition = self.name()
        if not (self.accept("DEFAULT") or self.accept("FOR", "VALUES")):
            raise Unrecognized(f"expected FOR VALUES or DEFAULT {self.where()}")
        self.item()
        return Action("attach_partition", None, _frozen({"partition": partition}))

    def action_detach(self) -> Action:
        self.expect("PARTITION")
        partition = self.name()
        mode = self.accept_any("CONCURRENTLY", "FINALIZE")
        options: _Options = {
            "partition": partition,
            "concurrently": mode == "CONCURRENTLY",
            "finalize": mode == "FINALIZE",
        }
        return Action("detach_partition", None, _frozen(options))

    def action_set(self) -> Action:
        if self.accept("TABLESPACE"):
            return Action("set_tablespace", None, _frozen({"name": self.name()}))
        if self.accept("LOGGED"):
            return Action("set_logged", None, _frozen({}))
        if self.accept("UNLOGGED"):
            return Action("set_unlogged", None, _frozen({}))
        if self.accept("SCHEMA"):
            return Action("set_schema", None, _frozen({"name": self.name()}))
        if self.is_punct("("):
            return Action("set_parameters", None, _frozen(self.with_options()))
        raise Unrecognized(f"no rule reads SET {self.where()}")

    def action_owner(self) -> Action:
        self.expect("TO")
        return Action("owner_to", None, _frozen({"name": self.name()}))

    def action_enable(self) -> Action:
        return self.toggle("enable")

    def action_disable(self) -> Action:
        return self.toggle("disable")

    def toggle(self, verb: str) -> Action:
        self.accept_any("ALWAYS", "REPLICA")
        if self.accept("TRIGGER"):
            return Action(f"{verb}_trigger", None, _frozen({"name": self.name()}))
        if self.accept("ROW", "LEVEL", "SECURITY"):
            return Action("row_security", None, _frozen({"enabled": verb == "enable"}))
        raise Unrecognized(f"no rule reads {verb.upper()} {self.where()}")

    def action_engine(self) -> Action:
        self.accept_op("=")
        return Action("engine", None, _frozen({"engine": self.value()}))

    def action_convert(self) -> Action:
        self.expect("TO")
        if not (self.accept("CHARACTER", "SET") or self.accept("CHARSET")):
            raise Unrecognized(f"expected CHARACTER SET {self.where()}")
        charset = self.value()
        if self.accept("COLLATE"):
            self.value()
        return Action("convert_charset", None, _frozen({"charset": charset}))

    def action_force(self) -> Action:
        return Action("force", None, _frozen({}))

    # --- column definitions --------------------------------------------

    def column_action(self, kind: str) -> Action:
        column = self.name()
        return Action(kind, column, _frozen(self.column_definition()))

    def column_type(self) -> List[Token]:
        """A column's type: a name, with arguments, arrays, and suffixes."""
        start = self.pos
        first = self.peek()
        if first is None or first.kind not in (WORD, IDENT):
            raise Unrecognized(f"expected a type {self.where()}")
        self.name()
        while not self.at_end():
            token = self.tokens[self.pos]
            if token.kind == PUNCT and token.text == "(":
                self.group()
            elif token.kind == PUNCT and token.text in "[]":
                self.pos += 1
            elif token.kind == NUMBER and self.tokens[self.pos - 1].text == "[":
                self.pos += 1
            elif token.kind != WORD:
                break
            elif token.value in ("WITH", "WITHOUT"):
                if not self.is_words(token.value, "TIME", "ZONE"):
                    break
                self.pos += 3
            elif token.value == "CHARACTER" and self.is_words("CHARACTER", "SET"):
                break
            elif token.value in _COLUMN_CONSTRAINT_WORDS:
                break
            else:
                self.pos += 1
        return self.tokens[start : self.pos]

    def column_definition(self) -> _Options:
        """
        A loose column definition: the type and the constraints this
        analysis reads. Sets type, serial, not_null (None when the
        statement leaves it unsaid), default, default_volatility,
        default_function, default_certain, generated (`stored`,
        `virtual`, or `identity`), identity, references, unique,
        primary_key, check, and position (MySQL FIRST or AFTER).
        """
        type_tokens = self.column_type()
        options: _Options = {
            "type": self.text(type_tokens),
            "serial": type_tokens[0].is_word(*_SERIAL_TYPES),
            "not_null": None,
            "default": None,
            "generated": None,
            "identity": False,
            "references": None,
            "unique": False,
            "primary_key": False,
            "check": False,
            "position": None,
        }
        while not self.at_end() and not self.is_punct(","):
            if not self.column_constraint(options):
                break
        return options

    def default_options(self, tokens: Sequence[Token]) -> _Options:
        volatility, function, certain = classify_default(tokens)
        return {
            "default": self.text(tokens),
            "default_volatility": volatility,
            "default_function": function,
            "default_certain": certain,
        }

    def column_constraint(self, options: _Options) -> bool:
        """Reads one column constraint into `options`; False at none."""
        token = self.peek()
        if token is None or token.kind != WORD:
            return False
        word = token.value
        if word == "CONSTRAINT":
            self.pos += 1
            self.name()
            return True
        if word in _SIMPLE_COLUMN_WORDS:
            return self.simple_column_word(options)
        if word == "DEFAULT":
            self.pos += 1
            options.update(self.default_options(self.expression(_DEFAULT_END_WORDS)))
            return True
        if word in ("GENERATED", "AS"):
            self.generated(options)
            return True
        if word == "REFERENCES":
            self.pos += 1
            options["references"] = self.name()
            if self.is_punct("("):
                self.group()
            self.foreign_key_tail()
            return True
        if word == "CHECK":
            self.pos += 1
            self.group()
            options["check"] = True
            self.accept("NO", "INHERIT")
            return True
        return self.column_attribute(options)

    def simple_column_word(self, options: _Options) -> bool:
        if self.accept("NOT", "NULL"):
            options["not_null"] = True
        elif self.accept("NULL"):
            options["not_null"] = False
        elif self.accept("PRIMARY", "KEY"):
            options["primary_key"] = True
            self.accept_any("CLUSTERED", "NONCLUSTERED")
        elif self.accept("UNIQUE"):
            self.accept("KEY")
            options["unique"] = True
            self.accept_any("CLUSTERED", "NONCLUSTERED")
        else:
            return False
        return True

    def generated(self, options: _Options) -> None:
        if self.accept("GENERATED"):
            if self.accept("BY", "DEFAULT") or self.accept("ALWAYS"):
                pass
            self.expect("AS")
            if self.accept("IDENTITY"):
                options["generated"] = "identity"
                options["identity"] = True
                if self.is_punct("("):
                    self.group()
                return
        else:
            self.expect("AS")
        self.group()
        kind = self.accept_any("STORED", "VIRTUAL", "PERSISTED")
        options["generated"] = (
            "stored" if kind in ("STORED", "PERSISTED") else "virtual"
        )

    def column_attribute(self, options: _Options) -> bool:
        """The attributes that change nothing this analysis reads."""
        if self.accept("COLLATE") or self.accept("CHARSET"):
            self.value()
        elif self.accept("CHARACTER", "SET"):
            self.value()
        elif self.accept("IDENTITY"):
            options["identity"] = True
            if self.is_punct("("):
                self.group()
        elif self.accept_any("AUTO_INCREMENT", "AUTOINCREMENT"):
            options["identity"] = True
        elif self.accept("COMMENT"):
            self.value()
        elif self.accept("FIRST"):
            options["position"] = "first"
        elif self.accept("AFTER"):
            options["position"] = f"after {self.name()}"
        elif self.accept("ON", "UPDATE"):
            self.expression(_DEFAULT_END_WORDS)
        elif self.accept_any("SPARSE", "ROWGUIDCOL", "VISIBLE", "INVISIBLE"):
            pass
        elif self.accept("WITH", "VALUES"):
            options["with_values"] = True
        else:
            # Whatever follows is the caller's to read, and an unread
            # word makes the statement unknown there.
            return False
        return True

    # UPDATE, DELETE, INSERT, WITH

    def update(self) -> ParsedStatement:
        limited = self.top()
        self.accept("ONLY")
        table = self.target()
        rest = self.rest()
        if not self.top_level_word(rest, "SET"):
            raise Unrecognized("expected SET in UPDATE")
        return self.write_statement("update", table, rest, limited)

    def top(self) -> bool:
        """SQL Server's `TOP (n)`, which caps the rows a write touches."""
        if not self.accept("TOP"):
            return False
        self.group()
        self.accept("PERCENT")
        return True

    def write_statement(
        self, kind: str, table: str, rest: Sequence[Token], limited: bool
    ) -> ParsedStatement:
        options: _Options = {
            "where": self.top_level_word(rest, "WHERE"),
            "limited": limited or self.top_level_word(rest, "LIMIT"),
        }
        return ParsedStatement(kind, table, options=_frozen(options))

    def delete(self) -> ParsedStatement:
        limited = self.top()
        if self.accept("FROM"):
            self.accept("ONLY")
            table = self.target()
        else:
            # SQL Server and MySQL: DELETE t [FROM ...] [WHERE ...].
            table = self.target()
        return self.write_statement("delete", table, self.rest(), limited)

    def insert(self) -> ParsedStatement:
        self.accept("IGNORE")
        self.accept("INTO")
        table = self.target()
        if self.accept("AS"):
            self.name()
        if self.is_punct("(") and not self.select_group_follows():
            self.group()
        self.accept("OVERRIDING")
        self.accept_any("SYSTEM", "USER")
        self.accept("VALUE")
        rest = self.rest()
        options: _Options = {"source": "select", "rows": None}
        if rest and rest[0].is_word("VALUES"):
            options["source"] = "values"
            options["rows"] = self.value_rows(rest[1:])
        elif rest and rest[0].is_word("DEFAULT"):
            options["source"] = "default"
            options["rows"] = 1
        elif not self.top_level_word(rest[:1], "SELECT", "WITH", "TABLE") and not (
            rest and rest[0].text == "("
        ):
            raise Unrecognized("expected VALUES or SELECT in INSERT")
        return ParsedStatement("insert", table, options=_frozen(options))

    def select_group_follows(self) -> bool:
        """Whether the parenthesized group ahead is a query, not columns."""
        token = self.peek(1)
        return token is not None and token.is_word("SELECT", "WITH", "VALUES")

    def value_rows(self, tokens: Sequence[Token]) -> int:
        rows = 0
        depth = 0
        for token in tokens:
            if token.kind == PUNCT and token.text == "(":
                if depth == 0:
                    rows += 1
                depth += 1
            elif token.kind == PUNCT and token.text == ")":
                depth -= 1
            elif depth == 0 and token.kind == WORD:
                break
        return rows

    def with_query(self) -> ParsedStatement:
        """A statement that opens with CTEs: the write after them."""
        self.accept("RECURSIVE")
        while True:
            self.name()
            if self.is_punct("("):
                self.group()
            self.expect("AS")
            self.accept("NOT")
            self.accept("MATERIALIZED")
            body = self.group()
            if body and body[0].is_word("INSERT", "UPDATE", "DELETE", "MERGE"):
                raise Unrecognized("a CTE writes data")
            if not self.accept_punct(","):
                break
        writer = self.accept_any("UPDATE", "DELETE", "INSERT")
        if writer is None:
            raise Unrecognized(f"no rule reads the query after WITH {self.where()}")
        return _STATEMENTS[writer](self)

    # The rest

    def truncate(self) -> ParsedStatement:
        self.accept("TABLE")
        self.accept("ONLY")
        tables = self.names()
        self.table = tables[0]
        self.accept_op("*")
        if self.accept_any("RESTART", "CONTINUE"):
            self.expect("IDENTITY")
        self.accept_any("CASCADE", "RESTRICT")
        return ParsedStatement(
            "truncate", tables[0], options=_frozen({"tables": tuple(tables)})
        )

    def rename(self) -> ParsedStatement:
        self.expect("TABLE")
        pairs: List[Tuple[str, str]] = []
        while True:
            old = self.name()
            self.expect("TO")
            pairs.append((old, self.name()))
            if not self.accept_punct(","):
                break
        self.table = pairs[0][0]
        options: _Options = {"new": pairs[0][1], "renames": tuple(pairs)}
        return ParsedStatement("rename_table", pairs[0][0], options=_frozen(options))

    def reindex(self) -> ParsedStatement:
        if self.is_punct("("):
            self.group()
        target = self.expect_one_of_words(
            "INDEX", "TABLE", "SCHEMA", "DATABASE", "SYSTEM"
        )
        concurrently = self.accept("CONCURRENTLY")
        name = self.name()
        table = name if target == "TABLE" else None
        self.table = table
        options: _Options = {
            "target": target.lower(),
            "name": name,
            "concurrently": concurrently,
        }
        return ParsedStatement("reindex", table, options=_frozen(options))

    def vacuum(self) -> ParsedStatement:
        full = False
        if self.is_punct("("):
            full = any(t.is_word("FULL") for t in self.group())
        while self.accept_any("FULL", "FREEZE", "VERBOSE", "ANALYZE"):
            full = full or self.tokens[self.pos - 1].is_word("FULL")
        tables = self.table_list()
        options: _Options = {"full": full, "tables": tables}
        return ParsedStatement(
            "vacuum", tables[0] if tables else None, options=_frozen(options)
        )

    def table_list(self) -> Tuple[str, ...]:
        """VACUUM and ANALYZE's optional tables, each with optional columns."""
        tables: List[str] = []
        while self.is_name():
            tables.append(self.name())
            if self.is_punct("("):
                self.group()
            if not self.accept_punct(","):
                break
        return tuple(tables)

    def analyze(self) -> ParsedStatement:
        self.accept("VERBOSE")
        if self.is_punct("("):
            self.group()
        tables = self.table_list()
        return ParsedStatement(
            "analyze",
            tables[0] if tables else None,
            options=_frozen({"tables": tables}),
        )

    def cluster(self) -> ParsedStatement:
        self.accept("VERBOSE")
        if self.at_end():
            return ParsedStatement("cluster", options=_frozen({"index": None}))
        first = self.name()
        if self.accept("ON"):
            # The older form: CLUSTER index ON table.
            table = self.target()
            return ParsedStatement("cluster", table, options=_frozen({"index": first}))
        self.table = first
        index = self.name() if self.accept("USING") else None
        return ParsedStatement("cluster", first, options=_frozen({"index": index}))

    def optimize(self) -> ParsedStatement:
        self.accept_any("NO_WRITE_TO_BINLOG", "LOCAL")
        self.expect("TABLE")
        tables = self.names()
        self.table = tables[0]
        return ParsedStatement(
            "optimize_table", tables[0], options=_frozen({"tables": tuple(tables)})
        )

    def refresh(self) -> ParsedStatement:
        self.expect("MATERIALIZED", "VIEW")
        concurrently = self.accept("CONCURRENTLY")
        view = self.target()
        with_data = True
        if self.accept("WITH"):
            with_data = not self.accept("NO")
            self.expect("DATA")
        options: _Options = {"concurrently": concurrently, "with_data": with_data}
        return ParsedStatement(
            "refresh_materialized_view", view, options=_frozen(options)
        )

    def comment(self) -> ParsedStatement:
        self.expect("ON")
        object_words: List[str] = []
        while self.is_word(
            "TABLE", "COLUMN", "INDEX", "VIEW", "MATERIALIZED", "TYPE", "SCHEMA"
        ) or self.is_word("SEQUENCE", "CONSTRAINT", "TRIGGER", "FUNCTION"):
            object_words.append(self.next().value)
        if not object_words:
            raise Unrecognized(f"no rule reads COMMENT ON {self.where()}")
        kind = " ".join(object_words).lower()
        parts = self.name_parts()
        table: Optional[str] = None
        column: Optional[str] = None
        if kind == "column":
            if len(parts) < 2:
                raise Unrecognized("COMMENT ON COLUMN needs table.column")
            table, column = ".".join(parts[:-1]), parts[-1]
        elif kind == "table":
            table = ".".join(parts)
        elif kind in ("constraint", "trigger"):
            self.expect("ON")
            table = self.name()
        elif kind == "function" and self.is_punct("("):
            self.group()
        self.table = table
        self.expect("IS")
        self.value()
        options: _Options = {"object": kind, "column": column}
        return ParsedStatement("comment_on", table, options=_frozen(options))

    def set_statement(self) -> ParsedStatement:
        scope = self.accept_any("SESSION", "LOCAL", "GLOBAL", "PERSIST", "PERSIST_ONLY")
        settings = [self.assignment(scope)]
        while self.accept_punct(","):
            settings.append(self.assignment(None))
        return ParsedStatement("set", options=_frozen({"settings": tuple(settings)}))

    def assignment(self, scope: Optional[str]) -> Tuple[str, str, str]:
        """One `name = value` of a SET: (scope, name, value)."""
        if self.accept_op("@"):
            if self.accept_op("@"):
                qualifier = self.accept_any("SESSION", "GLOBAL", "LOCAL", "PERSIST")
                if qualifier is not None:
                    scope = qualifier
                    self.expect_punct(".")
            else:
                scope = "user"
        if self.accept("TIME", "ZONE"):
            name = "timezone"
        else:
            name = self.name().lower()
        # SQL Server spells `SET LOCK_TIMEOUT 5000`, with neither.
        if not self.accept_op("="):
            self.accept("TO")
        # A MySQL SET separates assignments with commas; a Postgres value
        # may be a list, so a list stays one value there.
        if self.dialect is not None and self.dialect.name == "MYSQL":
            tokens = self.expression(frozenset())
        else:
            tokens = self.rest()
        if not tokens:
            raise Unrecognized(f"SET {name} has no value")
        value = tokens[0].value if len(tokens) == 1 else self.text(tokens)
        return ((scope or "session").lower(), name, value)

    def pragma(self) -> ParsedStatement:
        name = self.name().lower()
        value = ""
        if self.accept_op("="):
            value = self.value()
        elif self.is_punct("("):
            value = self.text(self.group())
        return ParsedStatement(
            "set", options=_frozen({"settings": (("pragma", name, value),)})
        )

    def lock(self) -> ParsedStatement:
        if self.is_word("TABLES"):
            raise Unrecognized("no rule reads LOCK TABLES")
        self.accept("TABLE")
        self.accept("ONLY")
        tables = self.names()
        self.table = tables[0]
        mode = "ACCESS EXCLUSIVE"
        if self.accept("IN"):
            for words in _LOCK_MODES:
                if self.accept(*words):
                    mode = " ".join(words)
                    break
            else:
                raise Unrecognized(f"expected a lock mode {self.where()}")
            self.expect("MODE")
        nowait = self.accept("NOWAIT")
        options: _Options = {"tables": tuple(tables), "mode": mode, "nowait": nowait}
        return ParsedStatement("lock_table", tables[0], options=_frozen(options))

    def execute(self) -> ParsedStatement:
        """SQL Server's `EXEC sp_rename 'path', 'new'[, 'kind']`."""
        procedure = self.name_parts()
        if procedure[-1].lower() != "sp_rename":
            raise Unrecognized(f"no rule reads EXEC {'.'.join(procedure)}")
        arguments = [self.value()]
        while self.accept_punct(","):
            arguments.append(self.value())
        if len(arguments) not in (2, 3):
            raise Unrecognized("sp_rename takes two or three arguments")
        kind = arguments[2].upper() if len(arguments) == 3 else "OBJECT"
        path = arguments[0].split(".")
        if kind == "COLUMN" and len(path) >= 2:
            table = ".".join(path[:-1])
            self.table = table
            action = Action(
                "rename_column",
                path[-1],
                _frozen({"old": path[-1], "new": arguments[1]}),
            )
            return ParsedStatement("alter_table", table, (action,))
        if kind == "OBJECT":
            self.table = arguments[0]
            options: _Options = {
                "new": arguments[1],
                "renames": ((arguments[0], arguments[1]),),
            }
            return ParsedStatement(
                "rename_table", arguments[0], options=_frozen(options)
            )
        raise Unrecognized(f"no rule reads sp_rename of a {kind.lower()}")

    def if_statement(self) -> ParsedStatement:
        """SQL Server's `IF OBJECT_ID(...) IS [NOT] NULL <statement>`."""
        self.expect("OBJECT_ID")
        self.group()
        self.expect("IS")
        self.accept("NOT")
        self.expect("NULL")
        return self.statement()


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
}

_ActionHandler = Callable[[_Parser], Action]
_ACTIONS: Dict[str, _ActionHandler] = {
    "ADD": _Parser.action_add,
    "DROP": _Parser.action_drop,
    "ALTER": _Parser.action_alter,
    "MODIFY": _Parser.action_modify,
    "CHANGE": _Parser.action_change,
    "RENAME": _Parser.action_rename,
    "VALIDATE": _Parser.action_validate,
    "CHECK": _Parser.action_check,
    "NOCHECK": _Parser.action_nocheck,
    "ATTACH": _Parser.action_attach,
    "DETACH": _Parser.action_detach,
    "SET": _Parser.action_set,
    "OWNER": _Parser.action_owner,
    "ENABLE": _Parser.action_enable,
    "DISABLE": _Parser.action_disable,
    "ENGINE": _Parser.action_engine,
    "CONVERT": _Parser.action_convert,
    "FORCE": _Parser.action_force,
}

_SIMPLE_COLUMN_WORDS = frozenset({"NOT", "NULL", "PRIMARY", "UNIQUE"})


def _holds_body(tokens: Sequence[Token]) -> bool:
    """
    Whether the statement creates a trigger, function, or procedure,
    whose body may hold statements of its own ended by semicolons.
    """
    if not tokens[0].is_word("CREATE"):
        return False
    return any(t.is_word("TRIGGER", "FUNCTION", "PROCEDURE") for t in tokens[1:8])


def unknown(reason: str, table: Optional[str] = None) -> ParsedStatement:
    """An unknown statement, naming what stopped the recognizer."""
    return ParsedStatement(UNKNOWN_KIND, table, options=_frozen({"reason": reason}))


def recognize(sql: str, dialect: Optional["Dialects"] = None) -> ParsedStatement:
    """
    One statement, parsed, or an unknown statement. The dialect decides
    the lexical rules, as `tokenize()` reads them, and the few spellings
    that differ between engines, such as SQL Server's `DROP INDEX t.ix`.

    A string that holds more than one statement is unknown: the analysis
    reads one statement at a time.
    """
    tokens = tokenize(sql, dialect)
    if tokens and tokens[-1].kind == ERROR:
        return unknown("the statement has a quote or comment that never closes")
    while tokens and tokens[-1].kind == PUNCT and tokens[-1].text == ";":
        tokens.pop()
    if not tokens:
        return unknown("the statement is empty")
    if any(t.kind == PUNCT and t.text == ";" for t in tokens) and not _holds_body(
        tokens
    ):
        return unknown("the text holds more than one statement")
    parser = _Parser(sql, tokens, dialect)
    try:
        parsed = parser.statement()
        parser.finish()
    except Unrecognized as error:
        return unknown(error.reason, parser.table)
    return parsed
