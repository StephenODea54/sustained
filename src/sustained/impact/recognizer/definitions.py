"""
The column and constraint definitions of CREATE TABLE and ALTER TABLE.
"""

from __future__ import annotations

from typing import (
    List,
    Optional,
    Sequence,
)

from sustained.impact.model import Action
from sustained.impact.recognizer.cursor import (
    COLUMN_CONSTRAINT_WORDS,
    DEFAULT_END_WORDS,
    REFERENTIAL_ACTIONS,
    SERIAL_TYPES,
    Cursor,
    Options,
    Unrecognized,
    frozen,
)
from sustained.impact.recognizer.volatility import (
    classify_default,
)
from sustained.impact.tokens import (
    IDENT,
    NUMBER,
    PUNCT,
    WORD,
    Token,
)


def not_null_column(tokens: Sequence[Token]) -> Optional[str]:
    """
    The column a check expression of the form `column IS NOT NULL`
    tests, with any parentheses around it, or None for another
    expression.
    """
    while (
        len(tokens) > 2
        and tokens[0].kind == PUNCT
        and tokens[0].text == "("
        and tokens[-1].kind == PUNCT
        and tokens[-1].text == ")"
    ):
        tokens = tokens[1:-1]
    if len(tokens) != 4 or tokens[0].name is None:
        return None
    if not all(t.is_word(w) for t, w in zip(tokens[1:], ("IS", "NOT", "NULL"))):
        return None
    return tokens[0].name


class Definitions(Cursor):
    """The column and constraint definitions of CREATE TABLE and ALTER TABLE."""

    def constraint(self, name: Optional[str]) -> Action:
        options: Options = {"name": name, "not_valid": False, "using_index": None}
        if self.accept("DEFAULT"):
            # SQL Server: ADD [CONSTRAINT name] DEFAULT expr FOR column.
            default = self.expression(frozenset({"FOR"}))
            self.expect("FOR")
            column = self.name()
            return Action(
                "set_default", column, frozen({"default": self.text(default)})
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
            tested = not_null_column(self.group())
            if tested is not None:
                options["not_null"] = tested
        elif self.accept("EXCLUDE"):
            options["constraint"] = "exclude"
            if self.accept("USING"):
                self.value()
            self.group()
        else:
            raise Unrecognized(f"no rule reads the constraint {self.where()}")
        self.constraint_tail(options)
        return Action("add_constraint", None, frozen(options))

    def key_columns(self, options: Options) -> None:
        """
        The column list of a key, or Postgres's USING INDEX form. SQL
        Server's CLUSTERED or NONCLUSTERED sets `clustered`, and MySQL's
        USING BTREE or USING HASH sets `using`, in lower case.
        """
        clustered = self.accept_any("CLUSTERED", "NONCLUSTERED")
        if clustered is not None:
            options["clustered"] = clustered == "CLUSTERED"
        if self.accept("NULLS"):
            self.accept("NOT")
            self.expect("DISTINCT")
        if self.accept("USING", "INDEX"):
            options["using_index"] = self.name()
            return
        # MySQL's index type, USING BTREE or USING HASH, before or after
        # the columns.
        if self.accept("USING"):
            options["using"] = self.value().lower()
        self.group()
        if self.is_word("USING") and not self.is_words("USING", "INDEX"):
            self.pos += 1
            options["using"] = self.value().lower()

    def foreign_key_tail(self) -> None:
        while True:
            if self.accept("ON"):
                self.expect_one_of_words("DELETE", "UPDATE")
                if not any(self.accept(*words) for words in REFERENTIAL_ACTIONS):
                    raise Unrecognized(f"expected a referential action {self.where()}")
                if self.is_punct("("):
                    self.group()
            elif self.accept("MATCH"):
                self.expect_one_of_words("FULL", "PARTIAL", "SIMPLE")
            else:
                return

    def constraint_tail(self, options: Options) -> None:
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

    def column_action(self, kind: str) -> Action:
        column = self.name()
        return Action(kind, column, frozen(self.column_definition()))

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
            elif token.value in COLUMN_CONSTRAINT_WORDS:
                break
            elif self.is_words("SERIAL", "DEFAULT", "VALUE"):
                break
            else:
                self.pos += 1
        return self.tokens[start : self.pos]

    def column_definition(self) -> Options:
        """
        A loose column definition: the type and the constraints this
        analysis reads. Sets type, serial, not_null (None when the
        statement leaves it unsaid), default, default_volatility,
        default_function, default_certain, generated (`stored`,
        `virtual`, or `identity`), identity, references, unique,
        primary_key, check, position (MySQL FIRST or AFTER), and comment,
        charset, and collate when the definition gives them. A SQL Server
        computed column, `name AS (expression) [PERSISTED]`, has no type,
        and type is None.
        """
        computed = self.mssql and self.is_word("AS")
        type_tokens = [] if computed else self.column_type()
        options: Options = {
            "type": self.text(type_tokens) if type_tokens else None,
            "serial": bool(type_tokens) and type_tokens[0].is_word(*SERIAL_TYPES),
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

    def default_options(self, tokens: Sequence[Token]) -> Options:
        volatility, function, certain = classify_default(tokens)
        return {
            "default": self.text(tokens),
            "default_volatility": volatility,
            "default_function": function,
            "default_certain": certain,
        }

    def column_constraint(self, options: Options) -> bool:
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
            options.update(self.default_options(self.expression(DEFAULT_END_WORDS)))
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

    def simple_column_word(self, options: Options) -> bool:
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

    def generated(self, options: Options) -> None:
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

    def column_attribute(self, options: Options) -> bool:
        """The attributes after the type and the column constraints."""
        if self.accept("COLLATE"):
            options["collate"] = self.value()
        elif self.accept("CHARSET") or self.accept("CHARACTER", "SET"):
            options["charset"] = self.value()
        elif self.accept("IDENTITY"):
            options["identity"] = True
            if self.is_punct("("):
                self.group()
        elif self.accept_any("AUTO_INCREMENT", "AUTOINCREMENT"):
            options["identity"] = True
        elif self.accept("SERIAL", "DEFAULT", "VALUE"):
            # MySQL: NOT NULL AUTO_INCREMENT UNIQUE.
            options["identity"] = True
            options["not_null"] = True
            options["unique"] = True
        elif self.accept("COMMENT"):
            options["comment"] = self.value()
        elif self.accept("FIRST"):
            options["position"] = "first"
        elif self.accept("AFTER"):
            options["position"] = f"after {self.name()}"
        elif self.accept("ON", "UPDATE"):
            self.expression(DEFAULT_END_WORDS)
        elif self.accept_any("SPARSE", "ROWGUIDCOL", "VISIBLE", "INVISIBLE"):
            pass
        elif self.accept("WITH", "VALUES"):
            options["with_values"] = True
        else:
            # Whatever follows is the caller's to read, and an unread
            # word makes the statement unknown there.
            return False
        return True


_SIMPLE_COLUMN_WORDS = frozenset({"NOT", "NULL", "PRIMARY", "UNIQUE"})
