"""
CREATE and DROP statements.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import (
    List,
    Mapping,
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
    depths,
    frozen,
    read_name,
)
from sustained.impact.recognizer.sources import tables_read
from sustained.impact.tokens import (
    Token,
)

# The options of a plain `CREATE TABLE name (...)`, with no reads. The
# analyzer builds a create_table statement from an intent with them.
CREATE_TABLE_DEFAULTS: Mapping[str, object] = MappingProxyType(
    {
        "temporary": False,
        "if_not_exists": False,
        "references": (),
        "partition_of": None,
        "default_partition": False,
        "partitioned": False,
        "as_select": False,
    }
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
            parsed = self.create_index(unique, fulltext is not None)
            if fulltext == "SPATIAL":
                parsed = parsed.with_options({"spatial": True})
            if clustered is None:
                return parsed
            return parsed.with_options({"clustered": clustered == "CLUSTERED"})
        if unique or clustered or fulltext:
            raise Unrecognized(f"expected INDEX {self.where()}")
        temporary = bool(self.accept_any("TEMP", "TEMPORARY", "UNLOGGED"))
        if self.accept("TABLE"):
            return self.create_table(temporary)
        materialized = self.accept("MATERIALIZED")
        if self.accept("VIEW"):
            self.body()
            return self.parsed("create_view", options={"materialized": materialized})
        if self.accept("TRIGGER") or self.accept("CONSTRAINT", "TRIGGER"):
            return self.create_trigger()
        if self.accept("TYPE"):
            return self.create_type()
        if self.accept("DOMAIN"):
            return self.create_domain()
        found = self.accept_any(*OBJECT_WORDS)
        if found:
            if found in ("FUNCTION", "PROCEDURE"):
                self.body()
            else:
                self.rest()
            return self.parsed("create_object", options={"object": found.lower()})
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
        if self.accept("USING"):
            # MySQL names the index type before ON as well as after the
            # columns.
            options["using"] = self.value().lower()
        self.expect("ON")
        options["only"] = self.accept("ONLY")
        table = self.target()
        if self.accept("USING"):
            options["using"] = self.value().lower()
        columns = self.group()
        options["columns"] = len(self.split_top(columns))
        self.index_tail(options)
        return self.parsed("create_index", table, options)

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
                if self.mssql:
                    # SQL Server's WITH and ON may follow the predicate.
                    self.up_to_word("WITH", "ON")
                else:
                    self.rest()
                options["partial"] = True
            elif self.is_word("ALGORITHM"):
                options["algorithm"] = self.mysql_option("ALGORITHM")
            elif self.is_word("LOCK"):
                options["lock"] = self.mysql_option("LOCK")
            elif self.accept("USING"):
                options["using"] = self.value().lower()
            elif self.is_word("WAIT", "NOWAIT"):
                options["wait"] = self.wait()
            elif self.accept("ON"):
                # SQL Server: ON a filegroup or partition scheme.
                self.name()
                if self.is_punct("("):
                    self.group()
            else:
                raise Unrecognized(f"no rule reads the index option {self.where()}")

    def create_table(self, temporary: bool) -> ParsedStatement:
        options: Options = {**CREATE_TABLE_DEFAULTS, "temporary": temporary}
        options["if_not_exists"] = self.accept("IF", "NOT", "EXISTS")
        table = self.target()
        if self.accept("PARTITION", "OF"):
            options["partition_of"] = self.name()
        if self.is_punct("("):
            options["references"] = self.references_in(self.group())
        if options["partition_of"] is not None:
            options["default_partition"] = self.is_word("DEFAULT")
        if self.accept("AS") or self.top_level_word(
            self.tokens[self.pos :], "AS", "SELECT"
        ):
            options["as_select"] = True
        # The tail holds storage options, which change nothing about a
        # table that does not exist yet, and the query of AS SELECT.
        tail = self.rest()
        options["partitioned"] = not options["as_select"] and self.top_level_word(
            tail, "PARTITION"
        )
        if options["as_select"]:
            options["reads"] = tables_read(tail)
        return self.parsed("create_table", table, options)

    def references_in(self, tokens: Sequence[Token]) -> Tuple[str, ...]:
        """The tables a CREATE TABLE body's foreign keys point at."""
        found: List[str] = []
        for index, token in enumerate(tokens):
            if not token.is_word("REFERENCES"):
                continue
            parts, _ = read_name(tokens, index + 1)
            if parts:
                found.append(".".join(parts))
        return tuple(found)

    def create_trigger(self) -> ParsedStatement:
        self.accept("IF", "NOT", "EXISTS")
        name = self.name()
        tokens = self.body()
        for index, token, depth in depths(tokens):
            if depth == 0 and token.is_word("ON"):
                sub = Cursor(self.sql, list(tokens[index + 1 :]), self.dialect)
                table = sub.name()
                return self.parsed("create_trigger", table, {"name": name})
        raise Unrecognized("expected ON <table> in CREATE TRIGGER")

    def create_type(self) -> ParsedStatement:
        self.name()
        enum = self.accept("AS", "ENUM")
        self.rest()
        return self.parsed("create_type", options={"enum": enum})

    def create_domain(self) -> ParsedStatement:
        """
        PostgreSQL's `CREATE DOMAIN name [AS] type ...`: its name, the
        type it is over, and whether it has a NOT NULL or a CHECK. A NOT
        NULL outside parentheses anywhere after the type counts, so one
        in a DEFAULT expression counts too.
        """
        name = self.name()
        self.accept("AS")
        type_tokens = self.up_to_word(
            "COLLATE", "DEFAULT", "CONSTRAINT", "NOT", "NULL", "CHECK"
        )
        if not type_tokens:
            raise Unrecognized(f"expected a type {self.where()}")
        tail = self.rest()
        constrained = self.top_level_word(tail, "CHECK") or any(
            first.is_word("NOT") and second.is_word("NULL")
            for first, second in zip(tail, tail[1:])
        )
        options = {
            "object": "domain",
            "name": name,
            "type": self.text(type_tokens),
            "constrained": constrained,
        }
        return self.parsed("create_object", options=options)

    def drop(self) -> ParsedStatement:
        if self.accept("INDEX"):
            return self.drop_index()
        if self.accept("TABLE"):
            return self.drop_many("drop_table")
        materialized = self.accept("MATERIALIZED")
        if self.accept("VIEW"):
            parsed = self.drop_many("drop_view")
            return parsed.with_options({"materialized": materialized})
        if self.accept("TRIGGER"):
            return self.drop_trigger()
        if self.accept("TYPE"):
            self.accept("IF", "EXISTS")
            names = self.names()
            self.accept_any("CASCADE", "RESTRICT")
            return self.parsed("drop_type", options={"names": tuple(names)})
        if self.accept("DOMAIN"):
            self.accept("IF", "EXISTS")
            names = self.names()
            self.accept_any("CASCADE", "RESTRICT")
            return self.parsed(
                "drop_object", options={"object": "domain", "names": tuple(names)}
            )
        found = self.accept_any(*OBJECT_WORDS)
        if found:
            self.rest()
            return self.parsed("drop_object", options={"object": found.lower()})
        raise Unrecognized(f"no rule reads DROP {self.where()}")

    def drop_many(self, kind: str) -> ParsedStatement:
        if_exists = self.accept("IF", "EXISTS")
        tables = self.names()
        cascade = self.accept_any("CASCADE", "RESTRICT") == "CASCADE"
        return self.parsed(
            kind,
            tables[0],
            {"tables": tuple(tables), "if_exists": if_exists, "cascade": cascade},
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
            if self.mssql and self.accept("WITH"):
                options["with"] = self.with_options()
            elif self.is_word("ALGORITHM"):
                options["algorithm"] = self.mysql_option("ALGORITHM")
            elif self.is_word("LOCK"):
                options["lock"] = self.mysql_option("LOCK")
            elif self.is_word("WAIT", "NOWAIT"):
                options["wait"] = self.wait()
            else:
                raise Unrecognized(f"unread text {self.where()}")
        return self.parsed("drop_index", table, options)

    def drop_trigger(self) -> ParsedStatement:
        self.accept("IF", "EXISTS")
        name = self.name()
        table: Optional[str] = None
        if self.accept("ON"):
            table = self.target()
        self.accept_any("CASCADE", "RESTRICT")
        return self.parsed("drop_trigger", table, {"name": name})
