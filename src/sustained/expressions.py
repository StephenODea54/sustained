"""
SQL expression classes.
"""

import warnings
from typing import TYPE_CHECKING, Callable, List, Optional, Sequence, Tuple, Union

from .rendering import Renderable, render_part
from .types import Expression, SqlValue

if TYPE_CHECKING:
    from .rendering import RenderContext
    from .types import AnyQuery, CaseCondition, CaseResult


def refuse_null_member(values: "Sequence[object]") -> None:
    """
    Raises ValueError when a NOT IN list has a None member. The member
    renders as NULL, and `x NOT IN (1, NULL)` is NULL for every row
    rather than true, so the filter would match no rows.
    """
    if any(value is None for value in values):
        raise ValueError(
            "NOT IN with a None member matches no rows, because "
            "x NOT IN (..., NULL) is never true. Remove None from the list. "
            "To match rows where the column is NULL as well, add "
            "orWhereNull() or col(...).is_null()."
        )


RenderFn = Callable[["RenderContext"], str]
"""Renders one predicate with the statement's render context."""

# The operators that compare a column with None, and the null test each
# one means.
_NULL_TESTS = {
    "=": "IS NULL",
    "IS": "IS NULL",
    "!=": "IS NOT NULL",
    "<>": "IS NOT NULL",
    "IS NOT": "IS NOT NULL",
}


def null_test(column: Renderable, negate: bool = False) -> RenderFn:
    """Builds `column IS NULL`, or `column IS NOT NULL` with negate."""
    test = "IS NOT NULL" if negate else "IS NULL"
    return lambda ctx: f"{render_part(column, ctx)} {test}"


def compare_with_none(column: Renderable, operator: str) -> Optional[RenderFn]:
    """
    Builds the null test that a comparison with None means: = and IS give
    IS NULL, and !=, <>, and IS NOT give IS NOT NULL. Returns None for any
    other operator, which has no meaning against None.
    """
    test = _NULL_TESTS.get(operator)
    if test is None:
        return None
    return null_test(column, negate=test == "IS NOT NULL")


def compare(column: Renderable, operator: str, value: SqlValue) -> RenderFn:
    """Builds `column <operator> value`, with the value as an operand."""

    def render(ctx: "RenderContext") -> str:
        # The column renders first, because a subquery in it binds values
        # that come before the operand's in the statement text.
        column_sql = render_part(column, ctx)
        operand = ctx.compiler.format_operand(value, ctx)
        return f"{column_sql} {operator} {operand}"

    return render


def between(
    column: Renderable, low: SqlValue, high: SqlValue, negate: bool = False
) -> RenderFn:
    """Builds `column BETWEEN low AND high`, or NOT BETWEEN with negate."""
    operator = "NOT BETWEEN" if negate else "BETWEEN"

    def render(ctx: "RenderContext") -> str:
        column_sql = render_part(column, ctx)
        low_sql = ctx.compiler.format_operand(low, ctx)
        high_sql = ctx.compiler.format_operand(high, ctx)
        return f"{column_sql} {operator} {low_sql} AND {high_sql}"

    return render


def like(column: Renderable, pattern: SqlValue, operator: str) -> RenderFn:
    """Builds a LIKE or ILIKE test through the dialect's compile_like()."""
    return lambda ctx: ctx.compiler.compile_like(
        render_part(column, ctx), ctx.value(pattern), operator
    )


def in_list(
    column: Renderable, values: Sequence[SqlValue], negate: bool = False
) -> RenderFn:
    """
    Builds `column IN (values)`, or NOT IN with negate.

    Raises:
        ValueError: If the list is empty, or a NOT IN list has a None
            member.
    """
    if not values:
        raise ValueError("IN/NOT IN requires a non-empty list of values.")
    items = list(values)
    if negate:
        refuse_null_member(items)
    operator = "NOT IN" if negate else "IN"

    def render(ctx: "RenderContext") -> str:
        column_sql = render_part(column, ctx)
        rendered = ", ".join(ctx.compiler.format_operand(v, ctx) for v in items)
        return f"{column_sql} {operator} ({rendered})"

    return render


def in_subquery(column: Renderable, inner: RenderFn, negate: bool = False) -> RenderFn:
    """Builds `column IN (subquery)`, or NOT IN with negate."""
    operator = "NOT IN" if negate else "IN"
    return lambda ctx: f"{render_part(column, ctx)} {operator} ({inner(ctx)})"


class Predicate:
    """
    A composable SQL condition. Build predicates from ColumnExpr comparisons
    and combine them with & (AND), | (OR), and ~ (NOT). Pass the result to
    where() or having().
    """

    def __init__(self, render: "Callable[[RenderContext], str]") -> None:
        self._render = render

    def render(self, ctx: "RenderContext") -> str:
        return self._render(ctx)

    def __and__(self, other: "Predicate") -> "Predicate":
        if not isinstance(other, Predicate):
            return NotImplemented
        return Predicate(lambda ctx: f"({self.render(ctx)} AND {other.render(ctx)})")

    def __or__(self, other: "Predicate") -> "Predicate":
        if not isinstance(other, Predicate):
            return NotImplemented
        return Predicate(lambda ctx: f"({self.render(ctx)} OR {other.render(ctx)})")

    def __invert__(self) -> "Predicate":
        return Predicate(lambda ctx: f"NOT ({self.render(ctx)})")

    def __bool__(self) -> bool:
        raise TypeError(
            "A Predicate has no truth value. Combine predicates with & and | "
            "instead of 'and' and 'or'."
        )


class ColumnExpr:
    """
    A typed column reference that builds Predicate objects from Python
    comparison operators.

    Create one with col('users.age') or through a model's column namespace,
    Model.c.age. Comparing against None with == or != renders IS NULL or
    IS NOT NULL.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"ColumnExpr({self.name!r})"

    def __hash__(self) -> int:
        return hash(self.name)

    def _quoted(self, ctx: "RenderContext") -> str:
        return ctx.compiler.quote_column_reference(self.name)

    def _compare(self, operator: str, value: SqlValue) -> Predicate:
        if value is None:
            null = compare_with_none(self._quoted, operator)
            if null is None:
                raise ValueError(
                    f"Cannot compare a column to None with the '{operator}' operator."
                )
            return Predicate(null)
        return Predicate(compare(self._quoted, operator, value))

    def __eq__(self, value: object) -> Predicate:  # type: ignore[override]
        return self._compare("=", value)

    def __ne__(self, value: object) -> Predicate:  # type: ignore[override]
        return self._compare("!=", value)

    def __gt__(self, value: SqlValue) -> Predicate:
        return self._compare(">", value)

    def __ge__(self, value: SqlValue) -> Predicate:
        return self._compare(">=", value)

    def __lt__(self, value: SqlValue) -> Predicate:
        return self._compare("<", value)

    def __le__(self, value: SqlValue) -> Predicate:
        return self._compare("<=", value)

    def like(self, pattern: str) -> Predicate:
        return Predicate(like(self._quoted, pattern, "LIKE"))

    def not_like(self, pattern: str) -> Predicate:
        return Predicate(like(self._quoted, pattern, "NOT LIKE"))

    def ilike(self, pattern: str) -> Predicate:
        return Predicate(like(self._quoted, pattern, "ILIKE"))

    def in_(self, values: "Union[Sequence[SqlValue], AnyQuery]") -> Predicate:
        return self._in("IN", values)

    def not_in(self, values: "Union[Sequence[SqlValue], AnyQuery]") -> Predicate:
        return self._in("NOT IN", values)

    def _in(
        self, operator: str, values: "Union[Sequence[SqlValue], AnyQuery]"
    ) -> Predicate:
        from .builder import QueryBuilder

        negate = operator == "NOT IN"
        if isinstance(values, QueryBuilder):
            subquery = values

            def render_sub(ctx: "RenderContext") -> str:
                from .rendering import render_nested

                return render_nested(subquery, ctx)

            return Predicate(in_subquery(self._quoted, render_sub, negate))

        if isinstance(values, (str, bytes)):
            # A string is a sequence of characters, so list() would turn
            # in_("active") into IN ('a', 'c', 't', ...).
            raise ValueError(
                f"{operator} takes a list of values or a query, not the string "
                f"{values!r}. Pass [{values!r}] to match one value."
            )
        return Predicate(in_list(self._quoted, values, negate))

    def between(self, low: SqlValue, high: SqlValue) -> Predicate:
        return Predicate(between(self._quoted, low, high))

    def not_between(self, low: SqlValue, high: SqlValue) -> Predicate:
        return Predicate(between(self._quoted, low, high, negate=True))

    def is_null(self) -> Predicate:
        return Predicate(null_test(self._quoted))

    def not_null(self) -> Predicate:
        return Predicate(null_test(self._quoted, negate=True))


def col(name: str) -> ColumnExpr:
    """Creates a typed column reference, e.g. col('users.age') > 21."""
    return ColumnExpr(name)


def raw(sql: str) -> Expression:
    """
    Wraps raw SQL that renders as written, without quotes or parameters.
    It is accepted in every position that takes a column or a value.
    Never pass text from a request through raw(), because it runs as SQL.
    """
    return Expression(sql)


class Column(Expression):
    """
    Raw SQL that renders as written. Deprecated: use raw(), which returns
    the same kind of object. Column will be removed in 3.0.
    """

    def __init__(self, name: str):
        warnings.warn(
            "Column() is deprecated and will be removed in 3.0. Use raw() "
            "for raw SQL, or col() for a quoted column name.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(name)

    @property
    def name(self) -> str:
        """The raw SQL text."""
        return self.value


class Literal:
    """
    Wraps a Python value that should render as a SQL literal.

    Bare strings passed to functions are treated as column references, so a
    string literal argument must be wrapped: Func('COALESCE', 'nickname',
    Literal('N/A')).
    """

    def __init__(self, value: SqlValue):
        self.value = value


class Func:
    """
    Represents a generic SQL function call.
    """

    def __init__(
        self, function_name: str, *args: SqlValue, alias: Optional[str] = None
    ):
        """
        Initializes the function expression.

        Args:
            function_name: The name of the SQL function (e.g., 'COALESCE').
            *args: The arguments to the function.
            alias: An optional alias for the function expression.
        """
        self.function_name = function_name
        self.args = args
        self.alias = alias


class Subquery:
    """
    Represents a subquery in a SELECT clause.
    """

    def __init__(self, query: "AnyQuery", alias: str):
        """
        Initializes the subquery expression.

        Args:
            query: The QueryBuilder instance for the subquery.
            alias: The alias for the subquery result.
        """
        self.query = query
        self.alias = alias

    def render(self, ctx: "RenderContext") -> str:
        """
        Renders the subquery with the outer statement's render context.

        The inner query's values go through the same context as the rest
        of the statement. In parameterized mode they become placeholders
        and join the outer parameter list in the order they appear in the
        SQL text.
        """
        return f"{self.render_operand(ctx)} AS {ctx.compiler.quote_alias(self.alias)}"

    def render_operand(self, ctx: "Optional[RenderContext]") -> str:
        """
        Renders the subquery with no alias, for a place where it stands as
        a value: a function argument, or one side of a comparison. An alias
        is not valid SQL there.

        Values render through the given context. With no context they
        inline as literals.
        """
        from .rendering import render_nested

        if ctx is None:
            return f"({self.query})"
        return f"({render_nested(self.query, ctx)})"

    def __str__(self) -> str:
        """
        Renders the subquery expression as a SQL string with the inner
        values inlined as literals. Used where no render context is
        available, such as debugging output.
        """
        return f"({self.query}) AS {self.query._compiler.quote_alias(self.alias)}"


class AggregateExpression:
    """
    Represents a SQL aggregate function call, like COUNT() or SUM().
    """

    def __init__(self, function_name: str, column: str, alias: Optional[str] = None):
        """
        Initializes the aggregate expression.

        Args:
            function_name: The name of the aggregate function (e.g., 'COUNT').
            column: The column to aggregate.
            alias: An optional alias for the expression.
        """
        self.function_name = function_name
        self.column = column
        self.alias = alias

    def __str__(self) -> str:
        """
        Renders the aggregate expression as a SQL string.

        Returns:
            The SQL string representation.
        """
        sql = f"{self.function_name}({self.column})"
        if self.alias:
            sql += f" AS {self.alias}"
        return sql


class WindowExpression:
    """
    Represents a SQL window function call, like ROW_NUMBER() OVER (...).
    """

    def __init__(
        self,
        function_name: str,
        alias: str,
        partition_by: Optional[List[str]] = None,
        order_by: Optional[List[str]] = None,
        args: Optional[List[SqlValue]] = None,
        frame: Optional[str] = None,
    ):
        """
        Initializes the window function expression.

        Args:
            function_name: The name of the window function (e.g., 'ROW_NUMBER').
            alias: The alias for the resulting column.
            partition_by: A list of columns to partition the window by.
            order_by: A list of columns to order the window by. Entries may
                carry a direction suffix, e.g. 'created_at DESC'.
            args: Arguments for the window function itself, e.g. the column
                for LAG or SUM. Strings are column references; wrap literal
                values in Literal().
            frame: An optional frame clause, e.g.
                'ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW'.
        """
        self.function_name = function_name
        self.alias = alias
        self.partition_by = partition_by
        self.order_by = order_by
        self.args = args or []
        self.frame = frame

    def __str__(self) -> str:
        """
        Renders the window function as a SQL string.

        The rendering comes from the default dialect's compiler, which is
        the same code the query compiler runs. The string form is the
        nested form, so it carries the function arguments and the frame
        clause but no alias. An alias is only valid at the top of a select
        list, where the compiler adds it.

        Returns:
            The SQL string representation.
        """
        # Late import to avoid a circular dependency: the compilers import
        # this module.
        from .dialects import Dialects

        return Dialects.get_compiler(Dialects.DEFAULT).compile_window_call(self)


class CaseExpression:
    """
    Represents a SQL CASE expression.
    """

    def __init__(self, alias: str, else_result: "CaseResult"):
        """
        Initializes the CASE expression.

        Args:
            alias: The alias for the resulting column.
            else_result: The result to return if no WHEN conditions match.
        """
        self.alias = alias
        self.else_result = else_result
        self._whens: List[Tuple["CaseCondition", "CaseResult"]] = []

    @property
    def whens(self) -> List[Tuple["CaseCondition", "CaseResult"]]:
        """The accumulated (condition, result) pairs."""
        return list(self._whens)

    def when(
        self, condition: "CaseCondition", result: "CaseResult"
    ) -> "CaseExpression":
        """
        Adds a WHEN/THEN clause to the CASE expression.

        Args:
            condition: The condition for the WHEN clause. A Predicate, such
                as col("age") >= 18, quotes its columns and renders its
                values as inline literals. A string is raw SQL and renders
                as written.
            result: The result to return if the condition is met.

        Returns:
            The CaseExpression instance for chaining.
        """
        self._whens.append((condition, result))
        return self

    def __str__(self) -> str:
        """
        Renders the CASE expression as a SQL string.

        The rendering comes from the default dialect's compiler, which is
        the same code the query compiler runs. Result values are escaped
        in one place, so a result that holds a quote renders correctly
        wherever the expression appears.

        Returns:
            The SQL string representation.
        """
        # Late import to avoid a circular dependency: the compilers import
        # this module.
        from .dialects import Dialects

        return Dialects.get_compiler(Dialects.DEFAULT).compile_case(self)
