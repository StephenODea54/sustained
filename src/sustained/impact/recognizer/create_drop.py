"""
CREATE and DROP statements.
"""

from __future__ import annotations

from typing import (
    List,
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.model import ParsedStatement
from sustained.impact.recognizer.cursor import (
    OBJECT_WORDS,
    Cursor,
    Options,
    Unrecognized,
    frozen,
)
from sustained.impact.tokens import (
    PUNCT,
    Token,
)


class CreateDrop(Cursor):
    """CREATE and DROP statements."""

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
                "create_view", options=frozen({"materialized": materialized})
            )
        if self.accept("TRIGGER") or self.accept("CONSTRAINT", "TRIGGER"):
            return self.create_trigger()
        if self.accept("TYPE"):
            return self.create_type()
        found = self.accept_any(*OBJECT_WORDS)
        if found:
            self.rest()
            return ParsedStatement(
                "create_object", options=frozen({"object": found.lower()})
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
        options: Options = {"unique": unique, "fulltext": fulltext}
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
        return ParsedStatement("create_index", table, options=frozen(options))

    def index_tail(self, options: Options) -> None:
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

    def create_table(self, temporary: bool) -> ParsedStatement:
        options: Options = {"temporary": temporary}
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
        return ParsedStatement("create_table", table, options=frozen(options))

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
                sub = Cursor(self.sql, list(tokens[index + 1 :]), self.dialect)
                table = sub.name()
                self.table = table
                return ParsedStatement(
                    "create_trigger", table, options=frozen({"name": name})
                )
        raise Unrecognized("expected ON <table> in CREATE TRIGGER")

    def create_type(self) -> ParsedStatement:
        self.name()
        enum = self.accept("AS", "ENUM")
        self.rest()
        return ParsedStatement("create_type", options=frozen({"enum": enum}))

    def drop(self) -> ParsedStatement:
        if self.accept("INDEX"):
            return self.drop_index()
        if self.accept("TABLE"):
            return self.drop_many("drop_table")
        materialized = self.accept("MATERIALIZED")
        if self.accept("VIEW"):
            parsed = self.drop_many("drop_view")
            return parsed._replace(
                options=frozen({**parsed.options, "materialized": materialized})
            )
        if self.accept("TRIGGER"):
            return self.drop_trigger()
        if self.accept("TYPE"):
            self.accept("IF", "EXISTS")
            names = self.names()
            self.accept_any("CASCADE", "RESTRICT")
            return ParsedStatement("drop_type", options=frozen({"names": tuple(names)}))
        found = self.accept_any(*OBJECT_WORDS)
        if found:
            self.rest()
            return ParsedStatement(
                "drop_object", options=frozen({"object": found.lower()})
            )
        raise Unrecognized(f"no rule reads DROP {self.where()}")

    def drop_many(self, kind: str) -> ParsedStatement:
        if_exists = self.accept("IF", "EXISTS")
        tables = self.names()
        self.table = tables[0]
        cascade = self.accept_any("CASCADE", "RESTRICT") == "CASCADE"
        return ParsedStatement(
            kind,
            tables[0],
            options=frozen(
                {"tables": tuple(tables), "if_exists": if_exists, "cascade": cascade}
            ),
        )

    def drop_index(self) -> ParsedStatement:
        options: Options = {}
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
        return ParsedStatement("drop_index", table, options=frozen(options))

    def drop_trigger(self) -> ParsedStatement:
        self.accept("IF", "EXISTS")
        name = self.name()
        table: Optional[str] = None
        if self.accept("ON"):
            table = self.target()
        self.accept_any("CASCADE", "RESTRICT")
        return ParsedStatement("drop_trigger", table, options=frozen({"name": name}))
