"""
DML, maintenance, SET, LOCK, and the other statements.
"""

from __future__ import annotations

from typing import (
    Dict,
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
    depths,
    frozen,
    read_name,
)
from sustained.impact.recognizer.sources import tables_read
from sustained.impact.tokens import (
    IDENT,
    NUMBER,
    PARAM,
    WORD,
    Token,
)

# The words that join another table to the target of a MySQL UPDATE.
_JOIN_WORDS = ("JOIN", "INNER", "LEFT", "RIGHT", "CROSS", "STRAIGHT_JOIN", "NATURAL")
# The words that end a FROM or USING list.
_REFS_END = ("WHERE", "ORDER", "LIMIT", "OPTION", "RETURNING", "OUTPUT", "GROUP")
# The words after a table in a FROM list that are not its alias.
_NOT_ALIASES = frozenset(
    _JOIN_WORDS
    + _REFS_END
    + ("FULL", "OUTER", "ON", "USING", "WITH", "PARTITION", "USE", "FORCE")
    + ("IGNORE", "FROM", "SET", "TABLESAMPLE")
)


class Statements(Cursor):
    """DML, maintenance, SET, LOCK, and the other statements."""

    def update(self) -> ParsedStatement:
        if self.mssql and self.accept("STATISTICS"):
            return self.update_statistics()
        limited = self.top()
        self.accept("ONLY")
        if self.dialect_named("MYSQL"):
            self.accept("LOW_PRIORITY")
            self.accept("IGNORE")
        table = self.target()
        head = self.up_to_word("SET")
        if self.joins_tables(head):
            raise Unrecognized("a multi-table UPDATE may write any of its tables")
        if not self.accept("SET"):
            raise Unrecognized("expected SET in UPDATE")
        tail = self.rest()
        if self.mssql:
            table = self.resolve_alias(table, tail)
        return self.write_statement("update", table, tail, limited)

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
        self, kind: str, table: str, tail: Sequence[Token], limited: bool
    ) -> ParsedStatement:
        """
        An UPDATE or DELETE. `tail` is the text after SET, or after the
        DELETE target. `where` is a WHERE outside parentheses in it, and
        `limited` is a TOP, or on MySQL and SQLite a tail that ends with
        `LIMIT n`, `LIMIT n OFFSET m`, or `LIMIT m, n`.
        """
        options: Options = {
            "where": self.top_level_word(tail, "WHERE"),
            "limited": limited or self.ends_with_limit(tail),
        }
        self.table = table
        return ParsedStatement(kind, table, options=frozen(options))

    def ends_with_limit(self, tail: Sequence[Token]) -> bool:
        """Whether the tail ends with a MySQL or SQLite LIMIT clause."""
        if not (self.sqlite or self.dialect_named("MYSQL")):
            return False
        counts = (NUMBER, PARAM)
        for size, middle in ((2, None), (4, "OFFSET"), (4, ",")):
            clause = tail[-size:]
            if len(clause) < size or not clause[0].is_word("LIMIT"):
                continue
            if any(token.kind not in counts for token in clause[1::2]):
                continue
            if middle is None or clause[2].value == middle:
                return True
        return False

    def joins_tables(self, tokens: Sequence[Token]) -> bool:
        """Whether a comma or a JOIN outside brackets names another table."""
        return any(
            depth == 0 and (token.is_punct(",") or token.is_word(*_JOIN_WORDS))
            for _, token, depth in depths(tokens)
        )

    def resolve_alias(self, target: str, tail: Sequence[Token]) -> str:
        """
        The table a write target names when the statement's FROM or USING
        list follows: the table an alias in that list stands for, or the
        target itself. A target that aliases a derived table is not read.
        """
        refs = self.table_refs(tail)
        key = target.lower()
        if key not in refs:
            return target
        table = refs[key]
        if table is None:
            raise Unrecognized(f"the write target {target} is a derived table")
        return table

    def table_refs(self, tokens: Sequence[Token]) -> Dict[str, Optional[str]]:
        """
        The tables of the FROM or USING list in `tokens`, keyed by lower
        case table name and alias. A derived table maps to None.
        """
        refs: Dict[str, Optional[str]] = {}
        depth = 0
        index = 0
        opened = False
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token.is_punct("(", ")"):
                depth += 1 if token.text == "(" else -1
                continue
            if depth:
                continue
            if token.is_word(*_REFS_END):
                opened = False
                continue
            if token.is_word("FROM", "USING"):
                opened = True
            elif not (opened and (token.is_word("JOIN") or token.is_punct(","))):
                continue
            table, index = self.table_ref(tokens, index)
            alias, index = self.table_alias(tokens, index)
            if table is not None:
                refs.setdefault(table.lower(), table)
            if alias is not None:
                refs[alias.lower()] = table
        return refs

    def table_ref(
        self, tokens: Sequence[Token], index: int
    ) -> Tuple[Optional[str], int]:
        """The dotted table name at `index`, or None for a derived table."""
        if index < len(tokens) and tokens[index].is_punct("("):
            depth = 0
            while index < len(tokens):
                if tokens[index].is_punct("(", ")"):
                    depth += 1 if tokens[index].text == "(" else -1
                index += 1
                if depth == 0:
                    break
            return None, index
        parts, index = read_name(tokens, index)
        return (".".join(parts) if parts else None), index

    @staticmethod
    def table_alias(tokens: Sequence[Token], index: int) -> Tuple[Optional[str], int]:
        """The alias after a table in a FROM list, if one follows."""
        if index < len(tokens) and tokens[index].is_word("AS"):
            index += 1
        elif (
            index < len(tokens)
            and tokens[index].kind == WORD
            and (tokens[index].value in _NOT_ALIASES)
        ):
            return None, index
        if index < len(tokens) and tokens[index].kind in (WORD, IDENT):
            return tokens[index].name, index + 1
        return None, index

    def delete(self) -> ParsedStatement:
        limited = self.top()
        if self.dialect_named("MYSQL"):
            while self.accept_any("LOW_PRIORITY", "QUICK", "IGNORE"):
                pass
        multiple = "a multi-table DELETE may delete from any of its tables"
        if self.accept("FROM"):
            self.accept("ONLY")
            table = self.target()
            if self.is_punct(","):
                raise Unrecognized(multiple)
            tail = self.rest()
            if not self.dialect_named("POSTGRES", "DUCKDB", "DEFAULT"):
                table = self.resolve_alias(table, tail)
        else:
            # SQL Server and MySQL: DELETE t [FROM ...] [WHERE ...].
            table = self.target()
            if self.is_punct(","):
                raise Unrecognized(multiple)
            tail = self.rest()
            table = self.resolve_alias(table, tail)
        return self.write_statement("delete", table, tail, limited)

    def dialect_named(self, *names: str) -> bool:
        return self.dialect is not None and self.dialect.name in names

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
        options: Options = {"source": "select", "rows": None}
        if self.accept("VALUES"):
            options["source"] = "values"
            options["rows"] = self.value_rows(self.rest())
        elif self.accept("DEFAULT"):
            self.accept("VALUES")
            options["source"] = "default"
            options["rows"] = 1
        elif self.is_punct("(") or self.is_word("SELECT", "WITH", "TABLE"):
            # The query's first word would end rest() on SQL Server.
            start = self.pos
            if not self.accept_any("SELECT", "WITH", "TABLE"):
                self.group()
            self.rest()
            options["reads"] = tables_read(self.tokens[start : self.pos])
        else:
            raise Unrecognized("expected VALUES or SELECT in INSERT")
        return ParsedStatement("insert", table, options=frozen(options))

    def select_group_follows(self) -> bool:
        """Whether the parenthesized group ahead is a query, not columns."""
        token = self.peek(1)
        return token is not None and token.is_word("SELECT", "WITH", "VALUES")

    def value_rows(self, tokens: Sequence[Token]) -> int:
        rows = 0
        for _, token, depth in depths(tokens):
            if depth == 0 and token.is_punct("("):
                rows += 1
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
