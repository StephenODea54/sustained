"""
DML, maintenance, SET, LOCK, and the other statements.
"""

from __future__ import annotations

from typing import (
    List,
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.model import Action, ParsedStatement
from sustained.impact.recognizer.cursor import (
    LOCK_MODES,
    Cursor,
    Options,
    Unrecognized,
    frozen,
)
from sustained.impact.recognizer.sources import tables_read
from sustained.impact.tokens import (
    PUNCT,
    WORD,
    Token,
)


class Statements(Cursor):
    """DML, maintenance, SET, LOCK, and the other statements."""

    def update(self) -> ParsedStatement:
        if self.mssql and self.accept("STATISTICS"):
            return self.update_statistics()
        limited = self.top()
        self.accept("ONLY")
        table = self.target()
        rest = self.rest()
        if not self.top_level_word(rest, "SET"):
            raise Unrecognized("expected SET in UPDATE")
        return self.write_statement("update", table, rest, limited)

    def update_statistics(self) -> ParsedStatement:
        """
        SQL Server's `UPDATE STATISTICS table [name | (names)] [WITH ...]`;
        `fullscan` says whether WITH FULLSCAN reads every row.
        """
        table = self.target()
        if self.is_punct("("):
            self.group()
        elif self.is_name() and not self.is_word("WITH"):
            self.name()
        fullscan = False
        if self.accept("WITH"):
            words = {t.value for t in self.rest() if t.kind == WORD}
            fullscan = "FULLSCAN" in words
        return ParsedStatement(
            "update_statistics", table, options=frozen({"fullscan": fullscan})
        )

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
        options: Options = {
            "where": self.top_level_word(rest, "WHERE"),
            "limited": limited or self.top_level_word(rest, "LIMIT"),
        }
        return ParsedStatement(kind, table, options=frozen(options))

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
        options: Options = {"source": "select", "rows": None}
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
        else:
            options["reads"] = tables_read(rest)
        return ParsedStatement("insert", table, options=frozen(options))

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
        return {"UPDATE": self.update, "DELETE": self.delete, "INSERT": self.insert}[
            writer
        ]()

    def truncate(self) -> ParsedStatement:
        self.accept("TABLE")
        self.accept("ONLY")
        tables = self.names()
        self.table = tables[0]
        self.accept_op("*")
        if self.accept_any("RESTART", "CONTINUE"):
            self.expect("IDENTITY")
        cascade = self.accept_any("CASCADE", "RESTRICT") == "CASCADE"
        return ParsedStatement(
            "truncate",
            tables[0],
            options=frozen({"tables": tuple(tables), "cascade": cascade}),
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
        options: Options = {"new": pairs[0][1], "renames": tuple(pairs)}
        return ParsedStatement("rename_table", pairs[0][0], options=frozen(options))

    def reindex(self) -> ParsedStatement:
        if self.sqlite:
            return self.sqlite_reindex()
        if self.is_punct("("):
            self.group()
        target = self.expect_one_of_words(
            "INDEX", "TABLE", "SCHEMA", "DATABASE", "SYSTEM"
        )
        concurrently = self.accept("CONCURRENTLY")
        name = self.name()
        table = name if target == "TABLE" else None
        self.table = table
        options: Options = {
            "target": target.lower(),
            "name": name,
            "concurrently": concurrently,
        }
        return ParsedStatement("reindex", table, options=frozen(options))

    def sqlite_reindex(self) -> ParsedStatement:
        """
        SQLite's REINDEX: every index with no name, or the indexes of
        the table, the index, or the collation the name finds, which
        the text cannot tell apart.
        """
        name = None if self.at_end() else self.name()
        options: Options = {
            "target": "database" if name is None else "any",
            "name": name,
            "concurrently": False,
        }
        return ParsedStatement("reindex", options=frozen(options))

    def vacuum(self) -> ParsedStatement:
        full = False
        if self.is_punct("("):
            full = any(t.is_word("FULL") for t in self.group())
        while self.accept_any("FULL", "FREEZE", "VERBOSE", "ANALYZE"):
            full = full or self.tokens[self.pos - 1].is_word("FULL")
        tables = self.table_list()
        options: Options = {"full": full, "tables": tables}
        return ParsedStatement(
            "vacuum", tables[0] if tables else None, options=frozen(options)
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
            options=frozen({"tables": tables}),
        )

    def cluster(self) -> ParsedStatement:
        self.accept("VERBOSE")
        if self.at_end():
            return ParsedStatement("cluster", options=frozen({"index": None}))
        first = self.name()
        if self.accept("ON"):
            # The older form: CLUSTER index ON table.
            table = self.target()
            return ParsedStatement("cluster", table, options=frozen({"index": first}))
        self.table = first
        index = self.name() if self.accept("USING") else None
        return ParsedStatement("cluster", first, options=frozen({"index": index}))

    def optimize(self) -> ParsedStatement:
        self.accept_any("NO_WRITE_TO_BINLOG", "LOCAL")
        self.expect("TABLE")
        tables = self.names()
        self.table = tables[0]
        return ParsedStatement(
            "optimize_table", tables[0], options=frozen({"tables": tuple(tables)})
        )

    def refresh(self) -> ParsedStatement:
        self.expect("MATERIALIZED", "VIEW")
        concurrently = self.accept("CONCURRENTLY")
        view = self.target()
        with_data = True
        if self.accept("WITH"):
            with_data = not self.accept("NO")
            self.expect("DATA")
        options: Options = {"concurrently": concurrently, "with_data": with_data}
        return ParsedStatement(
            "refresh_materialized_view", view, options=frozen(options)
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
        options: Options = {"object": kind, "column": column}
        return ParsedStatement("comment_on", table, options=frozen(options))

    def set_statement(self) -> ParsedStatement:
        scope = self.accept_any("SESSION", "LOCAL", "GLOBAL", "PERSIST", "PERSIST_ONLY")
        settings = [self.assignment(scope)]
        while self.accept_punct(","):
            # MySQL applies a leading GLOBAL or SESSION to every
            # assignment after it that names no scope of its own.
            settings.append(self.assignment(scope))
        return ParsedStatement("set", options=frozen({"settings": tuple(settings)}))

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
            "set", options=frozen({"settings": (("pragma", name, value),)})
        )

    def lock(self) -> ParsedStatement:
        if self.is_word("TABLES"):
            raise Unrecognized("no rule reads LOCK TABLES")
        self.accept("TABLE")
        only = self.accept("ONLY")
        tables = self.names()
        self.table = tables[0]
        mode = "ACCESS EXCLUSIVE"
        if self.accept("IN"):
            for words in LOCK_MODES:
                if self.accept(*words):
                    mode = " ".join(words)
                    break
            else:
                raise Unrecognized(f"expected a lock mode {self.where()}")
            self.expect("MODE")
        nowait = self.accept("NOWAIT")
        options: Options = {
            "tables": tuple(tables),
            "mode": mode,
            "nowait": nowait,
            "only": only,
        }
        return ParsedStatement("lock_table", tables[0], options=frozen(options))

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
                frozen({"old": path[-1], "new": arguments[1]}),
            )
            return ParsedStatement("alter_table", table, (action,))
        if kind == "INDEX" and len(path) >= 2:
            table = ".".join(path[:-1])
            self.table = table
            action = Action(
                "rename_index", None, frozen({"old": path[-1], "new": arguments[1]})
            )
            return ParsedStatement("alter_table", table, (action,))
        if kind == "OBJECT":
            self.table = arguments[0]
            options: Options = {
                "new": arguments[1],
                "renames": ((arguments[0], arguments[1]),),
            }
            return ParsedStatement(
                "rename_table", arguments[0], options=frozen(options)
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
