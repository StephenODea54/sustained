import datetime
import inspect
import re
from decimal import Decimal
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from sustained.exceptions import DialectError
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    ColumnExpr,
    Func,
    Literal,
    Subquery,
    WindowExpression,
)
from sustained.rendering import Renderable, RenderContext
from sustained.types import Expression, SqlValue

if TYPE_CHECKING:
    from sustained.dialects import Dialects
    from sustained.schema import ColumnDef, ColumnState, IndexColumn, TableOptions
    from sustained.types import (
        CaseCondition,
        CaseResult,
        ColumnReference,
        Selectable,
    )


# A plain identifier path such as "users", "users.id", or "db.dbo.users.id".
_IDENTIFIER_PATH_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$"
)

# A select list entry with an alias, such as "name AS label". The alias
# goes through quote_alias(), which refuses one that is not a plain name.
_SELECT_ALIAS_RE = re.compile(r"^(?P<column>.+?)\s+AS\s+(?P<alias>.+)$", re.IGNORECASE)

# How each dialect's name is written in prose, for error messages.
_DISPLAY_NAMES = {
    "ATHENA": "Athena",
    "DUCKDB": "DuckDB",
    "MYSQL": "MySQL",
    "POSTGRES": "Postgres",
    "PRESTO": "Presto",
}

# One plain identifier such as "users".
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# "users.*": every column of one table. The table is an identifier path.
_TABLE_STAR_RE = re.compile(r"^(?P<table>.+)\.\*$")

# A call with one column argument, such as "COUNT(*)", "SUM(tickets.price)",
# or "COUNT(DISTINCT user_id)". The argument is "*" or an identifier path.
_COLUMN_CALL_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\(\s*(?P<distinct>DISTINCT\s+)?"
    r"(?P<arg>.+?)\s*\)$",
    re.IGNORECASE,
)

# The closing character of each identifier quote a dialect writes.
_QUOTE_CLOSERS = {'"': '"', "`": "`", "[": "]"}


def _read_quoted(text: str, start: int, closer: str) -> Optional[Tuple[int, str]]:
    """
    Reads a quoted name that starts after its opening quote at start. A
    doubled closing quote is one quote character of the name. Returns the
    position after the closing quote and the name, or None when the quote
    does not close.
    """
    name: List[str] = []
    position = start
    while position < len(text):
        char = text[position]
        if char == closer:
            if text[position + 1 : position + 2] != closer:
                return position + 1, "".join(name)
            position += 1
        name.append(char)
        position += 1
    return None


def _identifier_parts(text: str) -> Optional[List[Tuple[str, bool]]]:
    """
    Splits an identifier path such as 'dbo.users.id' on its dots. A part
    in "..", [..] or `..` quotes is read without its quotes, so a dot
    inside it is part of the name. Each item is the name and whether it
    was quoted. Returns None when a part is empty.
    """
    parts: List[Tuple[str, bool]] = []
    position = 0
    while True:
        closer = _QUOTE_CLOSERS.get(text[position : position + 1])
        quoted = None if closer is None else _read_quoted(text, position + 1, closer)
        if quoted is not None and text[quoted[0] : quoted[0] + 1] in ("", "."):
            position, name = quoted
            parts.append((name, True))
        else:
            dot = text.find(".", position)
            end = len(text) if dot < 0 else dot
            name = text[position:end]
            position = end
            parts.append((name, False))
        if not name:
            return None
        if position == len(text):
            return parts
        position += 1


def write_column_name(name: str) -> str:
    """
    Reads a column key of insert() or update(). A key that is one name in
    "..", [..] or `..` quotes loses the quotes, so the target dialect quotes
    it again. Any other key stays as given, and the compiler quotes it as
    one name.
    """
    parts = _identifier_parts(name) if name else None
    if parts is not None and len(parts) == 1 and parts[0][1]:
        return parts[0][0]
    return name


def table_qualifier(table_sql: str) -> str:
    """
    Everything in front of the last name of a rendered table reference,
    the final dot included, or '' for a bare name. A dot inside a quoted
    name is part of the name, and a doubled closing quote stays inside
    it.
    """
    last_dot = -1
    position = 0
    while position < len(table_sql):
        char = table_sql[position]
        closer = _QUOTE_CLOSERS.get(char)
        if closer is not None:
            position += 1
            while position < len(table_sql):
                if table_sql[position] == closer:
                    if table_sql[position + 1 : position + 2] != closer:
                        break
                    position += 1
                position += 1
        elif char == ".":
            last_dot = position
        position += 1
    return table_sql[: last_dot + 1]


# Comparison operators accepted by the conditional clause builders. Anything
# else must be expressed with QueryBuilder.raw() so that intent is explicit.
_VALID_OPERATORS = frozenset(
    {
        "=",
        "!=",
        "<>",
        "<",
        "<=",
        ">",
        ">=",
        "LIKE",
        "NOT LIKE",
        "ILIKE",
        "NOT ILIKE",
        "IS",
        "IS NOT",
    }
)


# The methods that gained a render context after 2.20. A subclass written
# against the older one-argument spelling still overrides them, so the
# context is dropped for that override instead of raising TypeError.
_CONTEXT_METHODS = (
    "compile_function",
    "compile_function_call",
    "compile_window",
    "compile_window_call",
)


def _context_mode(method: Callable[..., str], leading: int) -> str:
    """
    How an override takes the render context: "positional" when it can
    take it after the expression, "keyword" when it takes it by name
    only, and "none" when it takes the expression alone.

    `leading` is how many arguments come before the expression: one for
    a normal method or a classmethod, none for a staticmethod.
    """
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        # A callable the inspect module cannot read is left alone.
        return "positional"
    head: Tuple[object, ...] = (None,) * leading
    if _binds(signature, head + (None, None), {}):
        return "positional"
    if _binds(signature, head + (None,), {"ctx": None}):
        return "keyword"
    return "none"


def _override_parts(override: object) -> Tuple[Optional[Callable[..., str]], int, str]:
    """
    The plain function inside an override, how many arguments come before
    the expression, and which descriptor the adapted function goes back
    into.

    A staticmethod and a classmethod are unwrapped here. A staticmethod
    object is callable on Python 3.10 and later, so calling it without
    unwrapping would read its signature as a method's and pass it the
    compiler as its first argument.
    """
    if isinstance(override, staticmethod):
        return override.__func__, 0, "static"
    if isinstance(override, classmethod):
        return override.__func__, 1, "class"
    if callable(override):
        return cast(Callable[..., str], override), 1, "plain"
    return None, 0, "plain"


def _rewrapped(method: Callable[..., str], kind: str) -> object:
    """The adapted function, back in the descriptor it came out of."""
    if kind == "static":
        return staticmethod(method)
    if kind == "class":
        return classmethod(method)
    return method


def _binds(
    signature: inspect.Signature,
    args: Sequence[object],
    kwargs: Mapping[str, object],
) -> bool:
    """Whether a call with these arguments fits the signature."""
    try:
        signature.bind(*args, **kwargs)
    except TypeError:
        return False
    return True


def _dropping_context(method: Callable[..., str], leading: int) -> Callable[..., str]:
    """
    The override, called without the context it does not take. The
    context arrives after the expression, either way the caller sends it.
    """

    @wraps(method)
    def call(*args: object, ctx: object = None) -> str:
        return method(*args[: leading + 1])

    return call


def _naming_context(method: Callable[..., str], leading: int) -> Callable[..., str]:
    """The override, given the context under the name it takes it by."""

    @wraps(method)
    def call(*args: object, ctx: object = None) -> str:
        head = args[: leading + 1]
        rest = args[leading + 1 :]
        if rest:
            ctx = rest[0]
        return method(*head, ctx=ctx)

    return call


def _quoted(identifier: str, quotes: Tuple[str, str]) -> str:
    opening, closing = quotes
    return opening + identifier.replace(closing, closing * 2) + closing


class Compiler:
    # The opening and closing quote for identifiers. None writes names
    # bare and refuses one that is not a plain name. A closing quote
    # inside the name doubles, so a name can never end the quoted span
    # early.
    _IDENT_QUOTES: Optional[Tuple[str, str]] = None
    # The quotes for DDL identifiers. None follows _IDENT_QUOTES.
    _DDL_IDENT_QUOTES: Optional[Tuple[str, str]] = None
    # Dialect syntax that the base methods render when the flag is set.
    _native_ilike = False
    _native_nulls_order = True
    _distinct_on = False
    _for_update = False
    _bare_offset = False
    _comment_on_column = False
    # Date and timestamp literals take their type name in front, mapped
    # through _TEMPORAL_KEYWORDS, when this is set.
    _typed_temporal_literals = False
    _TEMPORAL_KEYWORDS: Mapping[str, str] = {}
    # The ALTER COLUMN keyword in front of a new type, such as TYPE or
    # SET DATA TYPE. None means the dialect has no ANSI ALTER COLUMN.
    _ALTER_TYPE_KEYWORD: Optional[str] = None

    def __init__(self, dialect: "Dialects") -> None:
        self._dialect = dialect

    def __init_subclass__(cls, **kwargs: object) -> None:
        """
        Keeps a compiler subclass written against the older signatures
        working.

        compile_function(), compile_function_call(), compile_window() and
        compile_window_call() take a render context after the expression,
        and every caller passes one. An override that was written before
        that takes the expression only, so the call would raise
        TypeError. Such an override is wrapped here to accept the context
        and drop it. It renders what it always rendered, which inlines a
        subquery argument's values instead of parameterizing them. An
        override that takes the context by name only is wrapped to be
        given it by name, so it keeps the context it asked for.

        A staticmethod or classmethod override is unwrapped first and put
        back in the same descriptor, so it keeps the call signature it was
        written with.
        """
        super().__init_subclass__(**kwargs)
        for name in _CONTEXT_METHODS:
            method, leading, kind = _override_parts(cls.__dict__.get(name))
            if method is None:
                continue
            mode = _context_mode(method, leading)
            if mode == "keyword":
                setattr(cls, name, _rewrapped(_naming_context(method, leading), kind))
            elif mode == "none":
                setattr(cls, name, _rewrapped(_dropping_context(method, leading), kind))

    def dialect_name(self) -> str:
        """The dialect's name, for error messages."""
        return self._dialect.name

    def display_name(self) -> str:
        """The dialect's name as prose writes it, such as "MySQL"."""
        return _DISPLAY_NAMES.get(self.dialect_name(), self.dialect_name())

    def _unsupported(self, feature: str, hint: str = "") -> DialectError:
        """
        The error for a feature this dialect lacks, in one wording:
        "The MySQL dialect does not support RETURNING." with the hint
        after it. Callers raise the result.
        """
        message = f"The {self.display_name()} dialect does not support {feature}."
        return DialectError(f"{message} {hint}" if hint else message)

    def quote_identifier(self, identifier: str) -> str:
        """
        Quotes one identifier. This dialect writes identifiers bare, so a
        name that is not letters, digits, underscores and dollar signs
        would run as SQL, and it raises ValueError instead. A dialect that
        sets _IDENT_QUOTES quotes every name.
        """
        if self._IDENT_QUOTES is not None:
            return _quoted(identifier, self._IDENT_QUOTES)
        if not _IDENTIFIER_RE.match(identifier):
            raise ValueError(
                f"Identifier {identifier!r} is not a plain name. The "
                f"{self.dialect_name()} dialect writes identifiers without "
                "quotes, so a name takes letters, digits, underscores, and "
                "dollar signs, and starts with a letter or an underscore."
            )
        return identifier

    def quote_fully_qualified_identifier(self, identifier: str) -> str:
        return ".".join(self.quote_identifier(part) for part in identifier.split("."))

    def quote_ddl_identifier(self, identifier: str) -> str:
        """
        Quotes an identifier for a DDL statement. Most engines quote DDL
        and query identifiers the same way, so the default follows
        quote_identifier(). Athena does not: its DDL goes through a Hive
        parser that takes backticks or bare names only, while its queries
        run on a Trino engine that takes double quotes.
        """
        if self._DDL_IDENT_QUOTES is not None:
            return _quoted(identifier, self._DDL_IDENT_QUOTES)
        return self.quote_identifier(identifier)

    def quote_fully_qualified_ddl_identifier(self, identifier: str) -> str:
        return ".".join(
            self.quote_ddl_identifier(part) for part in identifier.split(".")
        )

    def quote_alias(self, alias: str) -> str:
        """
        Quotes a result alias.

        An alias arrives from the caller as free text, and the default
        dialect writes identifiers bare, so an alias that is not a plain
        name would run outside the quotes. Aliases take letters, digits,
        and underscores only.
        """
        if not _IDENTIFIER_RE.match(alias):
            raise ValueError(
                f"Alias {alias!r} is not a plain identifier. An alias takes "
                "letters, digits, and underscores, and starts with a letter "
                "or an underscore."
            )
        return self.quote_identifier(alias)

    def quote_table_reference(self, table: str) -> str:
        """
        Quotes a table name such as "users" or "sales.orders" that arrives
        as free text. Any other string raises ValueError, so SQL in a table
        name does not reach the FROM clause.
        """
        if not _IDENTIFIER_PATH_RE.match(table):
            raise ValueError(
                f"Table {table!r} is not a table name. A table name takes "
                "letters, digits, and underscores, with a dot before a "
                "schema-qualified part."
            )
        return self.quote_fully_qualified_identifier(table)

    def quote_column_reference(
        self, column: "ColumnReference", ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """
        Quotes a column reference for use inside a clause.

        A string is "*", "table.*", a call with one column argument such as
        "COUNT(*)" or "SUM(tickets.price)", or else an identifier path. A
        path splits on its dots, and each part goes through
        quote_identifier(), so "Employee ID" and "prénom" are names too. A
        part already in "..", [..] or `..` quotes loses those quotes and
        takes the quotes of this dialect, so 'dbo."a.b"' names one column
        "a.b". A string can arrive from a request, such as a sort
        parameter, and every part of it is quoted, so text in it never runs
        as SQL. The default dialect writes names bare and raises ValueError
        for a part that is not a plain name. Expression and Column objects
        are raw SQL. col() names a column by the same rule as a string.
        Literal renders its value as an inline SQL literal, and a Func,
        aggregate, window, or CASE object renders as its call. A Subquery
        renders through `ctx`, so its values join the statement's
        parameters. With no context they inline.
        """
        if isinstance(column, Expression):
            return str(column)
        if not isinstance(column, str):
            nested = self._compile_nested(column, ctx)
            if nested is not None:
                return nested
            raise TypeError(
                f"Column reference must be a string or Expression, got {type(column).__name__}."
            )
        if column == "*":
            return column
        star = _TABLE_STAR_RE.match(column)
        if star:
            return f"{self._quote_column_path(column, star['table'])}.*"
        call = _COLUMN_CALL_RE.match(column)
        if call:
            distinct = "DISTINCT " if call["distinct"] else ""
            arg = call["arg"]
            if arg != "*":
                arg = self._quote_column_path(column, arg)
            return f"{call['name']}({distinct}{arg})"
        return self._quote_column_path(column, column)

    def column_part(self, column: "ColumnReference") -> Renderable:
        """
        A column reference as a clause fragment. A string quotes now. Any
        other object quotes now as well, so a wrong type raises when the
        clause is built, and then renders again with the statement's
        context, so a Subquery inside it binds its values.
        """
        quoted = self.quote_column_reference(column)
        if isinstance(column, str):
            return quoted
        return lambda ctx: self.quote_column_reference(column, ctx)

    def _quote_column_path(self, column: str, path: str) -> str:
        """
        Quotes each part of an identifier path from the column string
        column. An empty part raises ValueError.
        """
        parts = _identifier_parts(path)
        if parts is None:
            raise ValueError(
                f"Column reference {column!r} is not a column name. A string "
                "names a column, such as 'users.id', 'users.*', or a call on "
                "one column such as 'COUNT(*)' or 'SUM(price)', and no part "
                "of a dotted name is empty. Pass any other SQL through "
                "QueryBuilder.raw()."
            )
        return ".".join(self.quote_identifier(name) for name, _ in parts)

    def validate_operator(self, operator: str) -> str:
        """
        Normalizes and validates a comparison operator.

        Raises:
            ValueError: If the operator is not a recognized SQL comparison
                operator. Raw predicates should use QueryBuilder.raw().
        """
        if not isinstance(operator, str):
            raise TypeError("Operator must be a string.")
        normalized = " ".join(operator.strip().upper().split())
        if normalized not in _VALID_OPERATORS:
            raise ValueError(
                f"Unsupported SQL operator: {operator!r}. "
                "Use QueryBuilder.raw() for raw SQL predicates."
            )
        return normalized

    _supports_qualify = False

    def supports_qualify(self) -> bool:
        """Reports whether the dialect supports the QUALIFY clause."""
        return self._supports_qualify

    def compile_with_keyword(self, recursive: bool) -> str:
        """Renders the WITH keyword, adding RECURSIVE where required."""
        return "WITH RECURSIVE" if recursive else "WITH"

    _with_leads_write = False

    def with_leads_write(self) -> bool:
        """
        Reports whether the WITH clause of a write goes in front of the
        verb: the WITH of an INSERT ... SELECT source in front of INSERT,
        and the WITH of every subquery inside an UPDATE or DELETE in front
        of UPDATE or DELETE, as one clause. Most engines take the WITH
        after INSERT INTO, as part of the SELECT, and inside each
        parenthesized subquery. SQL Server takes it in front only, and
        MySQL takes it after only.
        """
        return self._with_leads_write

    def compile_group_by_mode(self, mode: str, columns_sql: str) -> str:
        """Renders GROUP BY ROLLUP or GROUP BY CUBE over quoted columns."""
        return f"GROUP BY {mode} ({columns_sql})"

    def compile_grouping_sets(self, groups_sql: str) -> str:
        """Renders GROUP BY GROUPING SETS over parenthesized groups."""
        return f"GROUP BY GROUPING SETS ({groups_sql})"

    _parenthesized_set_members = False

    def parenthesized_set_members(self) -> bool:
        """
        Reports whether UNION, INTERSECT, and EXCEPT members render inside
        parentheses. SQLite rejects a parenthesized member outright, so the
        portable default renders members bare; a bare member cannot carry
        its own ORDER BY or LIMIT, and the builder refuses one that does.
        """
        return self._parenthesized_set_members

    def compile_distinct_on(self, columns_sql: "list[str]") -> str:
        """
        Renders DISTINCT ON. A Postgres extension also supported by DuckDB;
        other dialects raise.
        """
        if self._distinct_on:
            return f"DISTINCT ON ({', '.join(columns_sql)})"
        raise self._unsupported(
            "DISTINCT ON", "Use a window function with a row filter instead."
        )

    def compile_locking(self, skip_locked: bool, nowait: bool) -> str:
        """
        Renders a FOR UPDATE locking clause. Dialects without it raise.
        """
        if self._for_update:
            clause = "FOR UPDATE"
            if skip_locked:
                clause += " SKIP LOCKED"
            elif nowait:
                clause += " NOWAIT"
            return clause
        raise self._unsupported("FOR UPDATE")

    def compile_explain(self, analyze: bool) -> str:
        """Renders the EXPLAIN prefix. Dialects without EXPLAIN raise."""
        return "EXPLAIN ANALYZE" if analyze else "EXPLAIN"

    def compile_like(self, column_sql: str, pattern_sql: str, operator: str) -> str:
        """
        Renders a LIKE or ILIKE predicate. ILIKE is a Postgres extension, so
        the base compiler emulates it by lowercasing both sides, unless
        the dialect sets _native_ilike.
        """
        if self._native_ilike:
            return f"{column_sql} {operator} {pattern_sql}"
        if operator == "ILIKE":
            return f"LOWER({column_sql}) LIKE LOWER({pattern_sql})"
        if operator == "NOT ILIKE":
            return f"LOWER({column_sql}) NOT LIKE LOWER({pattern_sql})"
        return f"{column_sql} {operator} {pattern_sql}"

    _placeholder = "?"

    def placeholder(self) -> str:
        return self._placeholder

    def escapes_percent(self) -> bool:
        """
        Reports whether the driver reads a % sign in a statement with
        parameters as the start of a placeholder. to_sql() then writes each
        literal % sign as %%, which the driver reads back as one. The
        drivers that take %s placeholders (psycopg, psycopg2, PyMySQL, and
        mysqlclient) all read %% this way.
        """
        return self.placeholder() == "%s"

    def prepare_execution(
        self, sql: str, params: "tuple[SqlValue, ...]"
    ) -> "tuple[str, tuple[SqlValue, ...]]":
        """
        Adapts one parameterized statement to what this dialect's driver
        can execute. Most engines bind Python values as they are. Athena
        overrides this: its execution parameters must be strings, and a
        None parameter's placeholder becomes a literal NULL.
        """
        return sql, params

    def format_value(self, value: SqlValue) -> str:
        if isinstance(value, Expression):
            return str(value)
        if value is None:
            return "NULL"
        # bool must be checked before int because bool subclasses int.
        if isinstance(value, bool):
            return self.compile_boolean(value)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            escaped_value = value.replace("'", "''")
            return f"'{escaped_value}'"
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError(
                    f"Cannot render {value} as a SQL literal. Bind it as a "
                    "parameter instead."
                )
            # "f" keeps every digit and never writes an exponent.
            return format(value, "f")
        # datetime before date, because datetime subclasses date.
        if isinstance(value, datetime.datetime):
            type_name = "TIMESTAMP" if value.tzinfo is None else "TIMESTAMPTZ"
            return self.compile_temporal_literal(type_name, value.isoformat(sep=" "))
        if isinstance(value, datetime.date):
            return self.compile_temporal_literal("DATE", value.isoformat())
        if isinstance(value, bytes):
            return self.compile_binary_literal(value.hex())
        raise TypeError(
            f"Cannot render a value of type {type(value).__name__} as a SQL literal."
        )

    def compile_temporal_literal(self, type_name: str, text: str) -> str:
        """
        Renders a date or timestamp literal from its ISO text. `type_name`
        is DATE, TIMESTAMP, or TIMESTAMPTZ for a timestamp with an offset.

        SQLite, which the default dialect targets, stores dates as ISO
        text, the way the sqlite3 module binds them, so the literal is the
        quoted text alone. A typed dialect writes the type name in front,
        mapped through _TEMPORAL_KEYWORDS.
        """
        if self._typed_temporal_literals:
            keyword = self._TEMPORAL_KEYWORDS.get(type_name, type_name)
            return f"{keyword} '{text}'"
        return f"'{text}'"

    def compile_binary_literal(self, hex_text: str) -> str:
        """Renders a bytes literal from its hexadecimal digits."""
        return f"X'{hex_text}'"

    def compile_boolean(self, value: bool) -> str:
        return "TRUE" if value else "FALSE"

    def compile_is_boolean(self, column_sql: str, operator: str, value: bool) -> str:
        """
        Renders `column IS TRUE`, `IS NOT FALSE`, and the other two forms.
        The truth value is written as a keyword, because `IS ?` with a
        bound value is a syntax error on Postgres and DuckDB. A NULL column
        reads as neither TRUE nor FALSE.
        """
        return f"{column_sql} {operator} {self.compile_boolean(value)}"

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        """
        Renders an insert with conflict handling. The base form is the
        ON CONFLICT syntax shared by Postgres, SQLite, and DuckDB. Dialects
        with a different upsert statement override this.
        """
        columns_sql = ", ".join(self.quote_identifier(c) for c in column_names)
        sql = (
            f"INSERT INTO {table_sql} ({columns_sql}) "
            f"VALUES {', '.join(row_values_sql)}"
        )
        conflict_sql = ", ".join(self.quote_identifier(c) for c in conflict_columns)
        if action == "ignore":
            return f"{sql} ON CONFLICT ({conflict_sql}) DO NOTHING"
        assignments = ", ".join(
            f"{self.quote_identifier(c)} = EXCLUDED.{self.quote_identifier(c)}"
            for c in update_columns
        )
        return f"{sql} ON CONFLICT ({conflict_sql}) DO UPDATE SET {assignments}"

    def compile_merge_upsert(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
        set_prefix: str = "",
        terminator: str = "",
    ) -> str:
        """
        Renders an upsert as MERGE INTO ... USING (VALUES ...), for
        dialects with no ON CONFLICT. `set_prefix` goes in front of each
        column on the left of SET, and `terminator` ends the statement.
        """
        quote = self.quote_identifier
        columns_sql = ", ".join(quote(c) for c in column_names)
        on_sql = " AND ".join(
            f"target.{quote(c)} = source.{quote(c)}" for c in conflict_columns
        )
        sql = (
            f"MERGE INTO {table_sql} AS target "
            f"USING (VALUES {', '.join(row_values_sql)}) AS source ({columns_sql}) "
            f"ON {on_sql}"
        )
        if action == "merge":
            assignments = ", ".join(
                f"{set_prefix}{quote(c)} = source.{quote(c)}" for c in update_columns
            )
            sql += f" WHEN MATCHED THEN UPDATE SET {assignments}"
        insert_values = ", ".join(f"source.{quote(c)}" for c in column_names)
        return (
            f"{sql} WHEN NOT MATCHED THEN INSERT ({columns_sql}) "
            f"VALUES ({insert_values}){terminator}"
        )

    # Logical column types mapped to this dialect's SQL types. Dialects
    # override entries as needed.
    _TYPE_MAP = {
        "INTEGER": "INTEGER",
        "BIGINT": "BIGINT",
        "VARCHAR": "VARCHAR",
        "TEXT": "TEXT",
        "BOOLEAN": "BOOLEAN",
        "FLOAT": "DOUBLE PRECISION",
        "NUMERIC": "NUMERIC",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "BINARY": "BLOB",
        "JSON": "JSON",
    }

    _enum_strategy = "check"

    def enum_strategy(self) -> str:
        """
        How this dialect renders an enum column. One of:

        - 'native': a named type object, created with CREATE TYPE and
          referenced by name (Postgres, DuckDB).
        - 'inline': the value list written into the column type
          (MySQL ENUM('a', 'b')).
        - 'check': VARCHAR sized to the longest value, held to the list
          by a named CHECK constraint (ANSI, SQLite, MSSQL).

        Presto and Athena refuse enum columns in validate_column_def,
        so their strategy is never consulted.
        """
        return self._enum_strategy

    def normalize_diff_type(self, type_name: str) -> str:
        """
        Maps a logical type name to the name a schema diff compares on.
        A dialect that stores several logical types as one physical type
        folds them together here, so a model column does not read as
        changed against the type the engine reports back.
        """
        return type_name

    def compile_column_type(self, column: "ColumnDef") -> str:
        """
        Renders a ColumnDef's logical type as this dialect's SQL type.
        """
        if column.type_name == "ENUM":
            return self.compile_enum_column_type(column)
        base = self._TYPE_MAP.get(column.type_name)
        if base is None:
            raise ValueError(f"Unknown column type: {column.type_name!r}.")
        if column.type_name == "VARCHAR" and column.length is not None:
            return f"{base}({column.length})"
        if column.type_name == "NUMERIC" and column.precision is not None:
            return f"{base}({column.precision}, {column.scale})"
        return base

    def compile_enum_column_type(self, column: "ColumnDef") -> str:
        """
        Renders an ENUM column's type per the dialect's enum strategy.
        """
        assert column.enum_name is not None and column.enum_values is not None
        strategy = self.enum_strategy()
        if strategy == "native":
            return self.quote_ddl_identifier(column.enum_name)
        if strategy == "inline":
            values_sql = ", ".join(self.format_value(v) for v in column.enum_values)
            return f"ENUM({values_sql})"
        longest = max(len(v) for v in column.enum_values)
        return f"{self._TYPE_MAP['VARCHAR']}({longest})"

    def compile_create_enum_type(self, name: str, values: "list[str]") -> str:
        """
        Renders CREATE TYPE for a named enum, on dialects that have one.
        """
        if self.enum_strategy() == "native":
            values_sql = ", ".join(self.format_value(v) for v in values)
            return f"CREATE TYPE {self.quote_identifier(name)} AS ENUM ({values_sql})"
        raise self._unsupported(
            "named enum types",
            "Enum columns render per the dialect's enum strategy instead.",
        )

    def compile_drop_enum_type(self, name: str, if_exists: bool = False) -> str:
        """
        Renders DROP TYPE for a named enum, on dialects that have one.
        """
        if self.enum_strategy() == "native":
            exists_sql = "IF EXISTS " if if_exists else ""
            return f"DROP TYPE {exists_sql}{self.quote_identifier(name)}"
        raise self._unsupported("named enum types", "There is no enum type to drop.")

    def compile_add_enum_value(self, name: str, value: str) -> str:
        """
        Renders the statement that appends one value to a named enum
        type, on dialects that can.
        """
        raise self._unsupported("adding a value to an enum type in place")

    _stores_column_comments = False

    def stores_column_comments(self) -> bool:
        """
        Reports whether the engine keeps a column comment in its catalog.
        On an engine that does not, a declared comment stays on the model
        and renders nothing.
        """
        return self._stores_column_comments

    _inline_column_comments = False

    def inline_column_comments(self) -> bool:
        """
        Reports whether a column comment is written inside the column
        definition, as MySQL, Presto, and Athena spell it. Postgres and
        DuckDB store it with a COMMENT ON COLUMN statement instead.
        """
        return self._inline_column_comments

    def compile_set_column_comment(
        self,
        table_sql: str,
        column_name: str,
        comment: Optional[str],
        column: Optional["ColumnDef"] = None,
        state: Optional["ColumnState"] = None,
    ) -> "list[str]":
        """
        Renders the statements that set or clear one column's comment on
        an existing table. None clears. MySQL restates the column
        definition and needs it passed, either as the declared `column`
        or as the `state` the column is in when the statement runs. A
        `state` wins over `column`; its own comment is replaced by
        `comment`. A dialect that sets _comment_on_column renders COMMENT
        ON COLUMN. Dialects that store no column comments raise.
        """
        if self._comment_on_column:
            column_sql = self.quote_identifier(column_name)
            value = "NULL" if comment is None else self.format_value(comment)
            return [f"COMMENT ON COLUMN {table_sql}.{column_sql} IS {value}"]
        raise self._unsupported(
            "column comments",
            "Keep the description on the model or in the migration file.",
        )

    def compile_identity(self) -> str:
        """
        Renders the identity modifier for an autoincrement column. An empty
        string means the engine generates values without a modifier, as
        SQLite does for INTEGER PRIMARY KEY. Dialects without identity
        columns raise.
        """
        return ""

    def compile_add_column(self, table_sql: str, column_sql: str) -> str:
        """Renders an ALTER TABLE statement that adds one column."""
        return f"ALTER TABLE {table_sql} ADD COLUMN {column_sql}"

    def compile_drop_column(self, table_sql: str, column_name: str) -> str:
        """Renders an ALTER TABLE statement that drops one column."""
        quoted = self.quote_ddl_identifier(column_name)
        return f"ALTER TABLE {table_sql} DROP COLUMN {quoted}"

    def compile_drop_column_statements(
        self, table_sql: str, column_name: str, has_default: bool
    ) -> "list[str]":
        """
        Renders the statements that drop one column. `has_default` tells
        a dialect that keeps a default in its own constraint to drop
        that constraint first.
        """
        return [self.compile_drop_column(table_sql, column_name)]

    def compile_rename_column(
        self, table_sql: str, old_name: str, new_name: str
    ) -> str:
        """Renders a column rename."""
        old_sql = self.quote_ddl_identifier(old_name)
        new_sql = self.quote_ddl_identifier(new_name)
        return f"ALTER TABLE {table_sql} RENAME COLUMN {old_sql} TO {new_sql}"

    _inline_references = True

    def inline_references(self) -> bool:
        """
        Reports whether a REFERENCES clause written beside a column
        definition creates a foreign key. MySQL parses one and creates
        nothing, so it says no and takes its foreign keys as table
        constraints instead.
        """
        return self._inline_references

    def foreign_key_clause(
        self,
        constraint: str,
        column: "Union[str, Sequence[str]]",
        ref_table_sql: str,
        ref_column: "Union[str, Sequence[str]]",
        on_delete: Optional[str] = None,
        on_update: Optional[str] = None,
    ) -> str:
        """
        Renders a named FOREIGN KEY clause for a table body or an ADD.
        `column` and `ref_column` take one name or a matching sequence of
        names for a composite key. Actions render as given; validate them
        before calling.
        """
        columns = (column,) if isinstance(column, str) else tuple(column)
        targets = (ref_column,) if isinstance(ref_column, str) else tuple(ref_column)
        columns_sql = ", ".join(self.quote_ddl_identifier(c) for c in columns)
        sql = (
            f"CONSTRAINT {self.quote_ddl_identifier(constraint)} FOREIGN KEY "
            f"({columns_sql}) REFERENCES {ref_table_sql}"
        )
        if targets:
            # An empty target list means the key references the target
            # table's primary key, so the column list is left off.
            targets_sql = ", ".join(self.quote_ddl_identifier(c) for c in targets)
            sql += f" ({targets_sql})"
        if on_delete is not None:
            sql += f" ON DELETE {on_delete}"
        if on_update is not None:
            sql += f" ON UPDATE {on_update}"
        return sql

    def check_clause(self, constraint: str, expression: str) -> str:
        """
        Renders a named CHECK clause for a table body or an ADD. The
        expression is SQL and renders as written.
        """
        return (
            f"CONSTRAINT {self.quote_ddl_identifier(constraint)} CHECK ({expression})"
        )

    def unique_clause(self, constraint: str, columns: Sequence[str]) -> str:
        """Renders a named UNIQUE clause for a table body or an ADD."""
        columns_sql = ", ".join(self.quote_ddl_identifier(c) for c in columns)
        return (
            f"CONSTRAINT {self.quote_ddl_identifier(constraint)} UNIQUE ({columns_sql})"
        )

    def compile_add_foreign_key(
        self,
        table_sql: str,
        constraint: str,
        column: "Union[str, Sequence[str]]",
        ref_table_sql: str,
        ref_column: "Union[str, Sequence[str]]",
        on_delete: Optional[str] = None,
        on_update: Optional[str] = None,
    ) -> str:
        """
        Renders a named foreign key added to an existing table, with the
        arguments of foreign_key_clause().
        """
        clause = self.foreign_key_clause(
            constraint, column, ref_table_sql, ref_column, on_delete, on_update
        )
        return f"ALTER TABLE {table_sql} ADD {clause}"

    def compile_add_check(
        self, table_sql: str, constraint: str, expression: str
    ) -> str:
        """
        Renders a named CHECK constraint added to an existing table. The
        expression is SQL and renders as written.
        """
        return (
            f"ALTER TABLE {table_sql} ADD {self.check_clause(constraint, expression)}"
        )

    def compile_add_unique(
        self, table_sql: str, constraint: str, columns: "list[str]"
    ) -> str:
        """Renders a named UNIQUE constraint added to an existing table."""
        return f"ALTER TABLE {table_sql} ADD {self.unique_clause(constraint, columns)}"

    def equivalent_fk_action(self, action: str) -> str:
        """
        The referential action a diff compares, given one in upper case.
        Most engines tell every action apart, so the default returns it
        as given.
        """
        return action

    def compile_drop_foreign_key(self, table_sql: str, constraint: str) -> str:
        """Renders the statement that takes back an added foreign key."""
        return (
            f"ALTER TABLE {table_sql} DROP CONSTRAINT "
            f"{self.quote_ddl_identifier(constraint)}"
        )

    def compile_drop_constraint(self, table_sql: str, constraint: str) -> str:
        """Renders the statement that drops a named table constraint."""
        return (
            f"ALTER TABLE {table_sql} DROP CONSTRAINT "
            f"{self.quote_ddl_identifier(constraint)}"
        )

    def compile_rename_table(self, old_sql: str, new_sql: str) -> str:
        """
        Renders a table rename. RENAME TO takes a bare name on Postgres,
        DuckDB, and SQLite, and the table keeps its schema, so a schema
        in front of the new name comes off.
        """
        bare_sql = new_sql[len(table_qualifier(new_sql)) :]
        return f"ALTER TABLE {old_sql} RENAME TO {bare_sql}"

    # Whether CREATE INDEX accepts a WHERE predicate (partial index).
    supports_partial_index = False
    # Whether the engine stores a partial index predicate in its own
    # spelling rather than as written. Autogenerate reports a predicate
    # mismatch on such an engine as a note and never rebuilds the index.
    rewrites_index_predicate = False
    # Whether a key part accepts a prefix length, as in `col(10)`.
    supports_index_prefix = False
    # Whether a key part accepts DESC.
    supports_index_desc = True

    def compile_index_column(self, column: "Union[str, IndexColumn]") -> str:
        """Renders one key part of a CREATE INDEX column list."""
        from sustained.schema import IndexColumn

        part = column if isinstance(column, IndexColumn) else IndexColumn(column)
        sql = self.quote_ddl_identifier(part.name)
        if part.prefix_length is not None:
            if not self.supports_index_prefix:
                raise DialectError(
                    f"The {self.dialect_name()} dialect has no index prefix "
                    f"length; index column '{part.name}' declares one."
                )
            sql = f"{sql}({part.prefix_length})"
        if part.desc:
            if not self.supports_index_desc:
                raise DialectError(
                    f"The {self.dialect_name()} dialect has no DESC index "
                    f"column; index column '{part.name}' declares one."
                )
            sql = f"{sql} DESC"
        return sql

    def compile_create_index(
        self,
        index_name: str,
        table_sql: str,
        columns: "Sequence[Union[str, IndexColumn]]",
        unique: bool,
        where: Optional[str] = None,
    ) -> str:
        """
        Renders a CREATE INDEX statement. `columns` takes plain names or
        IndexColumn parts, and `where` is the predicate of a partial
        index as SQL text.
        """
        unique_sql = "UNIQUE " if unique else ""
        name_sql = self.quote_ddl_identifier(index_name)
        columns_sql = ", ".join(self.compile_index_column(c) for c in columns)
        sql = f"CREATE {unique_sql}INDEX {name_sql} ON {table_sql} ({columns_sql})"
        if where is None:
            return sql
        if not self.supports_partial_index:
            raise DialectError(
                f"The {self.dialect_name()} dialect has no partial index; "
                f"index '{index_name}' declares a WHERE predicate."
            )
        return f"{sql} WHERE {where}"

    def compile_drop_index(self, index_name: str, table_sql: str) -> str:
        """
        Renders a DROP INDEX statement. An index lives in the schema of
        its table, and the statement names no table, so the index takes
        the table's schema in front of it. Without it, Postgres looks
        for the index in the schemas on the search path.
        """
        name_sql = self.quote_ddl_identifier(index_name)
        return f"DROP INDEX {table_qualifier(table_sql)}{name_sql}"

    def compile_create_table(
        self, table_sql: str, body: str, suffix_sql: str, if_missing: bool
    ) -> str:
        """
        Renders a CREATE TABLE statement. With if_missing, the statement
        does nothing when the table is already there, which most engines
        spell as an IF NOT EXISTS clause.
        """
        exists_sql = "IF NOT EXISTS " if if_missing else ""
        return f"CREATE TABLE {exists_sql}{table_sql} ({body}){suffix_sql}"

    _supports_alter_column = False

    def supports_alter_column(self) -> bool:
        """
        Reports whether the dialect can change a column's type or
        nullability with ALTER TABLE. SQLite cannot and needs a table
        rebuild instead.
        """
        return self._supports_alter_column

    def rebuild_strategy(self) -> str:
        """
        How the dialect applies a column change that ALTER TABLE cannot
        make. One of:

        - 'alter': the dialect changes the column in place, so nothing is
          ever rebuilt.
        - 'rebuild': the table is created again from the model, the rows
          are copied across, and the old table is replaced. SQLite works
          this way.
        - 'unsupported': neither. Generation refuses and the change is
          written by hand. Presto and Trino answer this: they cannot
          alter a column, and the create-copy-drop-rename plan uses
          statements they do not have.
        """
        return "alter" if self.supports_alter_column() else "rebuild"

    def rebuild_setup_sql(self) -> "list[str]":
        """
        Statements that open a table rebuild. SQLite checks foreign keys
        while it drops a table, so dropping the old table fails whenever
        rows in another table still point at it. Turning enforcement off
        for the rebuild is SQLite's own recipe for this.

        SQLite ignores PRAGMA foreign_keys inside an open transaction, so
        the statement only bites when the rebuild runs outside one, and
        rebuild_finish_sql() is ignored the same way. A migration that
        carries these statements is generated with transactional=False,
        and the migrator turns the driver's own transaction control off
        for it, so both statements land.
        PRAGMA defer_foreign_keys does not help here: a dropped table
        leaves its children dangling, and the deferred check fails at
        COMMIT instead. Dialects that never rebuild never send this.
        """
        return ["PRAGMA foreign_keys = OFF"]

    def rebuild_finish_sql(self) -> "list[str]":
        """
        Statements that close a table rebuild, putting foreign key
        enforcement back where rebuild_setup_sql() took it away.
        """
        return ["PRAGMA foreign_keys = ON"]

    _alter_column_index_scope = "none"

    def alter_column_index_scope(self) -> str:
        """
        Which indexes stop an ALTER COLUMN statement, so a migration
        drops them before the statement and creates them again after.
        One of:

        - "none": the engine rebuilds its indexes itself, as Postgres
          and MySQL do.
        - "column": an index that contains the column, and a UNIQUE
          constraint on it. SQL Server refuses the statement with
          error 5074 while one is there.
        - "table": every index on the table. DuckDB refuses a change to
          any column of a table that has an index.
        """
        return self._alter_column_index_scope

    _index_drop_waits_for_commit = False

    def index_drop_waits_for_commit(self) -> bool:
        """
        Reports whether an index dropped inside a transaction still stops
        an ALTER COLUMN statement until the transaction commits. DuckDB
        works this way, so a migration that drops indexes around a column
        change runs outside a transaction there.
        """
        return self._index_drop_waits_for_commit

    _alter_type_keeps_default = True

    def alter_type_keeps_default(self) -> bool:
        """
        Reports whether a column keeps its default through a change of
        its type. SQL Server keeps a default as a constraint of its own
        and refuses to change the type of a column that has one, and
        Postgres refuses the change when the default does not cast to
        the new type on its own, so on both the default comes off before
        the change and goes back on after it.
        """
        return self._alter_type_keeps_default

    def lifted_default_sql(
        self, model_default_sql: Optional[str], live_default_sql: str
    ) -> str:
        """
        The DEFAULT text a type change writes back after a lifted
        default. The text the table has goes back as it is, since the
        engine reports it as SQL for the column.
        """
        return live_default_sql

    def compile_drop_column_default(self, table_sql: str, column_name: str) -> str:
        """Renders a statement that takes a column's default off."""
        column_sql = self.quote_ddl_identifier(column_name)
        return f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} DROP DEFAULT"

    def compile_add_column_default(
        self, table_sql: str, column_name: str, default_sql: str
    ) -> str:
        """Renders a statement that gives a column a default."""
        column_sql = self.quote_ddl_identifier(column_name)
        return (
            f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} "
            f"SET DEFAULT {default_sql}"
        )

    def supports_add_constraint(self) -> bool:
        """
        Reports whether the dialect can add a named constraint to a table
        that already exists, with ALTER TABLE ADD CONSTRAINT. A dialect
        that cannot must carry every foreign key inside CREATE TABLE, so
        new tables have to be created in dependency order. SQLite cannot,
        and neither can DuckDB, which says yes to supports_alter_column()
        but takes no constraint after the table is made.
        """
        return self.supports_constraints() and self.supports_alter_column()

    _keeps_constraint_names = True

    def keeps_constraint_names(self) -> bool:
        """
        Reports whether the catalog returns the name a constraint was
        created with. DuckDB does not: it names every constraint after
        its table and columns, such as k_pid_id_fkey, so a diff there
        pairs declared constraints with the catalog's by content.
        """
        return self._keeps_constraint_names

    _supports_constraints = True

    def supports_constraints(self) -> bool:
        """
        Reports whether the dialect supports column and table constraints
        such as PRIMARY KEY, UNIQUE, DEFAULT, and REFERENCES. Athena does
        not; its tables are files on object storage.
        """
        return self._supports_constraints

    _supports_transactions = True

    def supports_transactions(self) -> bool:
        """
        Reports whether the engine supports transactions. The migration
        runner wraps each migration in a transaction only when it does.
        """
        return self._supports_transactions

    def supports_transactional_ddl(self) -> bool:
        """
        Reports whether a schema change taken back by a rollback really
        goes away. On most engines this follows supports_transactions(),
        so that is the default. MySQL is the exception: its transactions
        work for rows, but every DDL statement commits as it runs, and a
        migration that fails halfway leaves the statements before it in
        place.

        The migration runner reads this rather than supports_transactions()
        when it decides whether to wrap a migration and whether a failed
        migration needs a failure row recorded for repair().
        """
        return self.supports_transactions()

    def begin_transaction_sql(self) -> Optional[str]:
        """
        The statement that opens a transaction, or None on engines that
        have no transactions.

        A rehearsal opens and closes its transaction with these statements
        rather than with the driver's own calls, because drivers disagree:
        SQLite starts a transaction for INSERT but not for CREATE TABLE,
        and asyncpg runs in autocommit until a transaction is opened. On a
        driver that already opened one, an explicit BEGIN would warn, so
        the rehearsal rolls back first.
        """
        return "BEGIN" if self.supports_transactions() else None

    def rollback_transaction_sql(self) -> Optional[str]:
        """
        The statement that takes back the transaction begin_transaction_sql()
        opened, or None on engines that have no transactions.
        """
        return "ROLLBACK" if self.supports_transactions() else None

    def commit_transaction_sql(self) -> Optional[str]:
        """
        The statement that commits the transaction begin_transaction_sql()
        opened, or None on engines that have no transactions.
        """
        return "COMMIT" if self.supports_transactions() else None

    _driver_transaction_control = True

    def driver_transaction_control(self) -> bool:
        """
        Reports whether the driver's own commit() and rollback() calls
        control the transaction a transaction() block opens, which is what
        DB-API 2.0 promises. DuckDB's driver does not: it runs every
        statement in autocommit and gives every cursor its own session, so
        transaction() drives it with BEGIN, COMMIT, and ROLLBACK statements
        on one cursor instead.
        """
        return self._driver_transaction_control

    def savepoint_sql(self, name: str) -> Optional[str]:
        """
        The statement that sets a savepoint inside an open transaction, or
        None on engines that have no savepoints. transaction() sets one per
        nested block, so an inner failure rolls back only that block.
        """
        return f"SAVEPOINT {name}" if self.supports_transactions() else None

    def rollback_savepoint_sql(self, name: str) -> Optional[str]:
        """The statement that rolls back to the named savepoint."""
        if not self.supports_transactions():
            return None
        return f"ROLLBACK TO SAVEPOINT {name}"

    def release_savepoint_sql(self, name: str) -> Optional[str]:
        """
        The statement that discards the named savepoint once its block
        succeeds, or None on engines that keep savepoints until the
        transaction ends.
        """
        return f"RELEASE SAVEPOINT {name}" if self.supports_transactions() else None

    def migration_lock_sql(self, name: str) -> "list[str]":
        """
        Statements that take an exclusive, session-scoped advisory lock so
        two migrators cannot run at once. An empty list means the engine
        has no such lock; SQLite and DuckDB serialize writers on their own,
        and Athena offers nothing to lock with.
        """
        return []

    def migration_unlock_sql(self, name: str) -> "list[str]":
        """Statements that release the advisory lock taken for migrations."""
        return []

    def migration_lock_problem(
        self, row: "Optional[Sequence[object]]"
    ) -> Optional[str]:
        """
        What the result of a migration_lock_sql() statement says went
        wrong, or None when the lock was granted. `row` is the row the
        statement returned, or None when it returned no row.

        Postgres and the engines without an advisory lock read nothing
        here: their lock statement waits until it holds the lock and
        raises otherwise. MySQL and MSSQL report a refused lock in the
        value instead, so they override this.
        """
        return None

    def lock_status(self, row: "Optional[Sequence[object]]") -> Optional[int]:
        """
        The number a lock statement returned, or None when it returned no
        row or something that is not a number. Drivers hand back an int,
        a decimal, or a string depending on the engine, so the overrides
        of migration_lock_problem() read the value through this.
        """
        value = row[0] if row else None
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, (str, bytes)):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    def validate_column_def(self, column: "ColumnDef") -> None:
        """
        Rejects ColumnDef features the dialect cannot express in DDL.
        The base compiler accepts everything.
        """

    def compile_table_options(self, options: Optional["TableOptions"]) -> str:
        """
        Renders the clause that follows the column list of CREATE TABLE:
        partitioning, storage location, and table properties. Dialects
        without these clauses raise when options are given.
        """
        if options is None:
            return ""
        raise self._unsupported(
            "table options (partitioning, location, or table properties)"
        )

    def compile_alter_column_type(
        self,
        table_sql: str,
        column_name: str,
        column: "ColumnState",
        using: Optional[str] = None,
    ) -> "list[str]":
        """
        Renders statements that change a column's type. `column` carries
        the whole state the column holds afterwards, because MySQL and
        SQL Server restate the definition and drop what it leaves off.
        A dialect that sets _ALTER_TYPE_KEYWORD renders the ANSI form.
        """
        if self._ALTER_TYPE_KEYWORD is not None:
            column_sql = self.quote_identifier(column_name)
            statement = (
                f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} "
                f"{self._ALTER_TYPE_KEYWORD} {column.type_sql}"
            )
            if using:
                statement += f" USING {using}"
            return [statement]
        raise self._unsupported("altering a column type in place")

    def compile_alter_column_nullability(
        self,
        table_sql: str,
        column_name: str,
        column: "ColumnState",
    ) -> "list[str]":
        """
        Renders statements that change a column's nullability. `column`
        carries the whole state the column holds afterwards, including
        the nullability the statement sets. A dialect that sets
        _ALTER_TYPE_KEYWORD renders the ANSI form.
        """
        if self._ALTER_TYPE_KEYWORD is not None:
            column_sql = self.quote_identifier(column_name)
            action = "DROP NOT NULL" if column.nullable else "SET NOT NULL"
            return [f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} {action}"]
        raise self._unsupported("altering column nullability in place")

    def compile_backfill(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        filler_sql: str,
    ) -> "list[str]":
        """
        Renders the statements that give every NULL in a column a value,
        run before the column tightens to NOT NULL. A plain UPDATE works
        everywhere but DuckDB, which overrides this.
        """
        quoted = self.quote_identifier(column_name)
        return [
            f"UPDATE {table_sql} SET {quoted} = {filler_sql} " f"WHERE {quoted} IS NULL"
        ]

    def compile_returning(self, columns_sql: str) -> str:
        """
        Renders a RETURNING clause for DML statements. Dialects without
        support raise DialectError.
        """
        return f"RETURNING {columns_sql}"

    def compile_ctas(self, table_sql: str, select_sql: str, temporary: bool) -> str:
        """
        Renders a CREATE TABLE ... AS statement. Dialects that spell it
        differently raise DialectError.
        """
        keyword = "CREATE TEMPORARY TABLE" if temporary else "CREATE TABLE"
        return f"{keyword} {table_sql} AS {select_sql}"

    def compile_top(self, value: int) -> str:
        raise self._unsupported("TOP", "Use limit() instead.")

    def compile_order_entry(
        self, column_sql: str, direction: str, nulls: Optional[str] = None
    ) -> str:
        """
        Renders one ORDER BY key. Nulls is FIRST, LAST, or None for the
        engine's own placement of NULL values. A dialect without NULLS
        FIRST and NULLS LAST clears _native_nulls_order.
        """
        if not self._native_nulls_order:
            return self.compile_emulated_nulls_order(column_sql, direction, nulls)
        if nulls is None:
            return f"{column_sql} {direction}"
        return f"{column_sql} {direction} NULLS {nulls}"

    def compile_emulated_nulls_order(
        self, column_sql: str, direction: str, nulls: Optional[str]
    ) -> str:
        """
        Renders one ORDER BY key for an engine with no NULLS FIRST or
        NULLS LAST. A CASE key in front of the column sorts the NULL rows
        to the requested end, and the column then sorts the rest.
        """
        entry = f"{column_sql} {direction}"
        if nulls is None:
            return entry
        null_rank, value_rank = (0, 1) if nulls == "FIRST" else (1, 0)
        return (
            f"CASE WHEN {column_sql} IS NULL THEN {null_rank} "
            f"ELSE {value_rank} END, {entry}"
        )

    _limit_needs_order_by = False

    def limit_needs_order_by(self) -> bool:
        """
        Reports whether compile_limit_offset() raises DialectError for a
        query with no ORDER BY. first() then caps the query with TOP.
        """
        return self._limit_needs_order_by

    def compile_limit_offset(
        self,
        limit: Optional[int],
        offset: Optional[int],
        has_order_by: bool = False,
    ) -> str:
        if limit is None and offset is not None:
            return self.compile_offset_without_limit(offset)
        parts = []
        if limit is not None:
            parts.append(f"LIMIT {limit}")
        if offset is not None:
            parts.append(f"OFFSET {offset}")
        return " ".join(parts)

    def compile_offset_without_limit(self, offset: int) -> str:
        """
        Renders an OFFSET that has no LIMIT with it.

        SQLite, which the default dialect targets, needs a row cap before
        OFFSET, and LIMIT -1 is its spelling of "all rows". Dialects that
        take a bare OFFSET, and reject a negative LIMIT, set _bare_offset.
        """
        if self._bare_offset:
            return f"OFFSET {offset}"
        return f"LIMIT -1 OFFSET {offset}"

    def compile_function(
        self, func: Func, ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """
        Renders a Func expression with its alias, for the select list.

        With a render context, an argument that holds values renders through
        it, so a subquery argument parameterizes with the rest of the
        statement. With no context the values inline as literals.
        """
        sql = self.compile_function_call(func, ctx)
        if func.alias:
            sql += f" AS {self.quote_alias(func.alias)}"
        return sql

    def compile_function_call(
        self, func: Func, ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """
        Renders the function call without the alias, translating the
        function name to the dialect's spelling when the registry defines
        one. Use this where the call sits inside another expression, because
        an alias is only valid at the top of a select list.
        """
        # Imported here because the dialects module imports the compilers
        # at module load time.
        from sustained.functions import FunctionRegistry

        function_name = FunctionRegistry.resolve_name(func.function_name, self._dialect)
        formatted_args = ", ".join(self._format_arg(arg, ctx) for arg in func.args)
        return f"{function_name}({formatted_args})"

    def compile_aggregate(self, agg: AggregateExpression) -> str:
        """
        Renders an aggregate expression with its alias, for the select list.
        The column is quoted for the dialect.
        """
        sql = self.compile_aggregate_call(agg)
        if agg.alias:
            sql += f" AS {self.quote_alias(agg.alias)}"
        return sql

    def compile_aggregate_call(
        self, agg: AggregateExpression, ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """
        Renders the aggregate call without the alias. Use this where the
        aggregate sits inside another expression, because an alias is only
        valid at the top of a select list.
        """
        column = self.quote_column_reference(agg.column, ctx)
        return f"{agg.function_name}({column})"

    def compile_window(
        self, window: WindowExpression, ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """
        Renders a window expression with dialect quoting for partition and
        order columns and the alias.
        """
        alias_sql = self.quote_alias(window.alias)
        return f"{self.compile_window_call(window, ctx)} AS {alias_sql}"

    def compile_window_call(
        self, window: WindowExpression, ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """
        Renders the window function call and its OVER clause, without the
        alias. Use this where the window sits inside another expression,
        because an alias is only valid at the top of a select list.
        """
        over_clauses = []
        if window.partition_by:
            partition_cols = ", ".join(
                self.quote_column_reference(c, ctx) for c in window.partition_by
            )
            over_clauses.append(f"PARTITION BY {partition_cols}")
        if window.order_by:
            order_cols = ", ".join(
                self._quote_order_entry(c, ctx) for c in window.order_by
            )
            over_clauses.append(f"ORDER BY {order_cols}")
        if window.frame:
            over_clauses.append(window.frame)
        over_sql = " ".join(over_clauses)
        args_sql = ", ".join(self._format_arg(arg, ctx) for arg in window.args)
        return f"{window.function_name}({args_sql}) OVER ({over_sql})"

    def _quote_order_entry(
        self, entry: "ColumnReference", ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """Quotes an ORDER BY entry that may carry an ASC or DESC suffix."""
        if not isinstance(entry, str):
            return self.quote_column_reference(entry, ctx)
        parts = entry.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].upper() in ("ASC", "DESC"):
            return f"{self.quote_column_reference(parts[0])} {parts[1].upper()}"
        return self.quote_column_reference(entry)

    def compile_case(self, case: CaseExpression) -> str:
        """
        Renders a CASE expression with its alias, for the select list.
        Results go through the dialect's value formatting, so booleans and
        NULL render correctly per dialect.
        """
        return f"{self.compile_case_expr(case)} AS {self.quote_alias(case.alias)}"

    def compile_case_expr(self, case: CaseExpression) -> str:
        """
        Renders the CASE expression without the alias. Use this where the
        CASE sits inside another expression, because an alias is only valid
        at the top of a select list.
        """
        sql = "CASE"
        for condition, result in case.whens:
            condition_sql = self._format_case_condition(condition)
            sql += f" WHEN {condition_sql} THEN {self._format_case_result(result)}"
        sql += f" ELSE {self._format_case_result(case.else_result)}"
        sql += " END"
        return sql

    def _format_case_condition(self, condition: "CaseCondition") -> str:
        """
        Renders a WHEN condition. A string is raw SQL. A Predicate renders
        with an inline render context, so its values become literals, as
        CASE results do.
        """
        if isinstance(condition, str):
            return condition
        return condition.render(RenderContext(self))

    def _format_case_result(self, result: "CaseResult") -> str:
        nested = self._compile_nested(result, None)
        return self.format_value(result) if nested is None else nested

    def format_operand(self, value: SqlValue, ctx: "RenderContext") -> str:
        """
        Formats a value that stands on one side of a comparison.

        An expression object renders as SQL text through the given context,
        so a subquery inside it parameterizes with the rest of the
        statement. Every other value goes to the context, which binds it or
        inlines it as a literal.
        """
        if isinstance(value, Literal):
            return ctx.value(value.value)
        nested = self._compile_nested(value, ctx)
        return ctx.value(value) if nested is None else nested

    def compile_select_item(
        self, item: "Selectable", ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """
        Renders one entry of the select list, with its alias where the
        expression has one. A string is a column reference, with an
        optional "col AS alias" suffix. A subquery renders through the
        given context, or inlines its values with no context.
        """
        if isinstance(item, str):
            return self._compile_select_string(item)
        if isinstance(item, Func):
            return self.compile_function(item, ctx)
        if isinstance(item, AggregateExpression):
            return self.compile_aggregate(item)
        if isinstance(item, WindowExpression):
            return self.compile_window(item, ctx)
        if isinstance(item, CaseExpression):
            return self.compile_case(item)
        if isinstance(item, Subquery):
            return str(item) if ctx is None else item.render(ctx)
        return self._compile_nested(item, ctx) or str(item)

    def _compile_select_string(self, column: str) -> str:
        """
        Quotes a string select entry, supporting an optional "col AS alias"
        suffix so aliased selections quote correctly in every dialect. A
        string that is one identifier path with every part in quotes is a
        column, so '"Cost AS Pct"' names one column.
        """
        parts = _identifier_parts(column)
        if parts is not None and all(quoted for _, quoted in parts):
            return self.quote_column_reference(column)
        alias_match = _SELECT_ALIAS_RE.match(column)
        if alias_match:
            quoted = self.quote_column_reference(alias_match.group("column").strip())
            return f"{quoted} AS {self.quote_alias(alias_match.group('alias'))}"
        return self.quote_column_reference(column)

    def _compile_nested(
        self, value: object, ctx: 'Optional["RenderContext"]'
    ) -> Optional[str]:
        """
        Renders an expression object where it sits inside another
        expression, without the alias, which belongs to the select list.
        Returns None for a value that is not an expression object.
        """
        if isinstance(value, Func):
            return self.compile_function_call(value, ctx)
        if isinstance(value, AggregateExpression):
            return self.compile_aggregate_call(value, ctx)
        if isinstance(value, WindowExpression):
            return self.compile_window_call(value, ctx)
        if isinstance(value, Subquery):
            return value.render_operand(ctx)
        if isinstance(value, CaseExpression):
            return self.compile_case_expr(value)
        if isinstance(value, (Column, Expression)):
            return str(value)
        if isinstance(value, ColumnExpr):
            return self.quote_column_reference(value.name)
        if isinstance(value, Literal):
            return self.format_value(value.value)
        return None

    def _format_arg(
        self, arg: SqlValue, ctx: 'Optional["RenderContext"]' = None
    ) -> str:
        """
        Formats a function argument for inclusion in the SQL string.

        Strings and ColumnExpr objects are column references, read by the
        rule of quote_column_reference(). A string value must be wrapped in
        Literal(), or it names a column. Numbers, booleans, and None render
        as literals directly. A subquery argument renders
        through the given context, so its values become placeholders and
        join the statement's parameter list. With no context they inline.
        """
        if isinstance(arg, Literal):
            return self.format_value(arg.value)
        nested = self._compile_nested(arg, ctx)
        if nested is not None:
            return nested
        if isinstance(arg, str):
            return self.quote_column_reference(arg)
        if arg is None or isinstance(arg, (bool, int, float)):
            return self.format_value(arg)
        raise TypeError(
            f"Cannot render a function argument of type {type(arg).__name__}."
        )
