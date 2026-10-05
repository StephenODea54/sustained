from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

from ..expressions import (
    between,
    compare,
    compare_with_none,
    in_list,
    in_subquery,
    like,
    null_test,
)
from ..rendering import (
    Renderable,
    RenderContext,
    compiler_or_default,
    render_clause_list,
    render_nested,
    render_part,
)
from ..types import (
    ColumnReference,
    DbReturnValue,
    Expression,
    QueryResolvable,
    SqlValue,
)

if TYPE_CHECKING:
    from ..compilers import Compiler
    from ..expressions import Predicate
    from ..model import Model
    from ..types import AnyQuery


def _raw_subquery_message(operator: str, sql: str) -> str:
    return (
        f"{operator} takes a list, a query, or a callable, not the string "
        f"{sql!r}. A string here would go into the SQL as written. Pass a "
        "subquery as SQL through QueryBuilder.raw()."
    )


class ConditionalClauseBuilder:
    """The shared base for the WHERE and HAVING clause builders."""

    # The clause keyword, set by each subclass.
    _clause_keyword = ""

    _WHERE_METHOD_MAP = {
        "where": "_add_internal",
        "whereIn": "_add_in_internal",
        "whereNotIn": "_add_in_internal",
        "whereBetween": "_add_between_internal",
        "whereNotBetween": "_add_between_internal",
        "whereExists": "_add_exists_internal",
        "whereNotExists": "_add_exists_internal",
        "whereLike": "_add_like_internal",
        "whereILike": "_add_like_internal",
        "whereNull": "_add_null_internal",
        "whereNotNull": "_add_null_internal",
        "whereRaw": "_add_raw_internal",
    }

    def __init__(
        self, model_class: Type["Model"], compiler: Optional["Compiler"] = None
    ):
        self._model_class = model_class
        self._compiler = compiler_or_default(compiler)
        self._clauses: List[Tuple[str, Renderable]] = []

    def _quote_column(self, column: ColumnReference) -> str:
        """Quotes a column reference through the compiler.

        The compiler accepts an identifier path or a call on one column, such
        as an aggregate in a HAVING clause, and raises on any other string.
        """
        return self._compiler.quote_column_reference(column)

    def __getattr__(self, name: str) -> Callable[..., "ConditionalClauseBuilder"]:
        """
        Dynamically handles method calls for clauses.
        """
        # Private names are never clause methods; see QueryBuilder.__getattr__.
        if name.startswith("_"):
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )

        # Names match without regard to case or underscores, the same way
        # QueryBuilder resolves them, so WHERE_IN reaches whereIn.
        folded = name.replace("_", "")
        base_name = re.sub(r"^(or|and)", "", folded, flags=re.IGNORECASE)
        lookup_name = re.sub(r"^having", "where", base_name.lower())
        lowered = {k.lower(): v for k, v in self._WHERE_METHOD_MAP.items()}
        method_name = lowered.get(lookup_name)

        if method_name:
            conjunction_str = re.match(r"^(or|and)", folded, flags=re.IGNORECASE)
            if conjunction_str:
                conjunction = conjunction_str.group(0).upper()
            else:
                conjunction = "AND" if self._clauses else ""

            # Check if this is the first clause and an "or" or "and" prefix was used
            if not self._clauses and conjunction in ("OR", "AND"):
                raise RuntimeError(
                    f"Cannot start a {self._clause_keyword.lower()} clause with '{conjunction.lower()}'."
                )

            internal_method = getattr(self, method_name)

            # A pass-through to the internal handler resolved above. The
            # typed overloads a caller sees live in the stub beside this file.
            def dynamic_caller(*args: SqlValue) -> "ConditionalClauseBuilder":
                if "not" in base_name.lower():
                    op_override = True  # Flag to indicate "NOT" version
                else:
                    op_override = False

                if "ilike" in base_name.lower():
                    op_like_override = "ILIKE"
                elif "like" in base_name.lower():
                    op_like_override = "LIKE"
                else:
                    op_like_override = None

                internal_method(
                    conjunction,
                    *args,
                    op_override=op_override,
                    op_like_override=op_like_override,
                )
                return self

            return dynamic_caller

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _add_between_internal(
        self,
        conjunction: str,
        col: str,
        val1: DbReturnValue,
        val2: DbReturnValue,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `BETWEEN` and `NOT BETWEEN` clauses."""
        render = between(self._quote_column(col), val1, val2, negate=op_override)
        self._clauses.append((conjunction, render))

    def _add_exists_internal(
        self,
        conjunction: str,
        query: QueryResolvable,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `EXISTS` and `NOT EXISTS` clauses."""
        actual_op = "NOT EXISTS" if op_override else "EXISTS"
        inner = self._subquery(
            query,
            "EXISTS",
            "Argument for exists must be a callable, QueryBuilder.raw(), or "
            "QueryBuilder instance.",
        )

        def render(ctx: RenderContext) -> str:
            return f"{actual_op} ({inner(ctx)})"

        self._clauses.append((conjunction, render))

    def _subquery(
        self, arg: QueryResolvable, operator: str, wrong_type_message: str
    ) -> Callable[[RenderContext], str]:
        """
        Resolves a subquery argument: a QueryBuilder, a callable that
        builds one on a fresh query of this model, or raw SQL through
        QueryBuilder.raw(). Returns a function that renders the inner SQL.
        A plain string raises, because it would go into the SQL as written.
        """
        from ..builder import QueryBuilder

        if isinstance(arg, Expression):
            raw_sql = arg.value
            return lambda ctx: raw_sql
        if isinstance(arg, str):
            raise ValueError(_raw_subquery_message(operator, arg))
        sub_builder: "AnyQuery"
        if isinstance(arg, QueryBuilder):
            sub_builder = arg
        elif callable(arg):
            sub_builder = QueryBuilder(
                self._model_class, dialect=self._compiler._dialect
            )
            arg(sub_builder)
        else:
            raise ValueError(wrong_type_message)
        return lambda ctx: render_nested(sub_builder, ctx)

    def _add_like_internal(
        self,
        conjunction: str,
        col: str,
        pattern: str,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `LIKE` and `ILIKE` clauses."""
        actual_op = op_like_override if op_like_override else "LIKE"
        self._clauses.append(
            (conjunction, like(self._quote_column(col), pattern, actual_op))
        )

    def _add_raw_internal(
        self,
        conjunction: str,
        sql: str,
        params: Optional[Sequence[SqlValue]] = None,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """
        Internal handler for raw predicates with bound values. Values are
        marked with ? in the fragment and supplied separately, so they
        parameterize like every other clause.
        """
        from ..rendering import bind_raw, count_value_markers

        bound_params = list(params) if params else []
        # Validate the marker count at build time so mistakes surface early.
        # A question mark inside a string literal is text, not a marker.
        marker_count = count_value_markers(sql)
        if marker_count != len(bound_params):
            raise ValueError(
                f"Raw SQL fragment has {marker_count} value markers "
                f"but {len(bound_params)} parameters were given."
            )

        def render(ctx: RenderContext) -> str:
            return f"({bind_raw(sql, bound_params, ctx)})"

        self._clauses.append((conjunction, render))

    def _add_null_internal(
        self,
        conjunction: str,
        col: ColumnReference,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `IS NULL` and `IS NOT NULL` clauses."""
        render = null_test(self._quote_column(col), negate=op_override)
        self._clauses.append((conjunction, render))

    def _add_internal(
        self,
        conjunction: str,
        column_or_callable: Union[
            ColumnReference, Callable[["ConditionalClauseBuilder"], None], "Predicate"
        ],
        op: Optional[str] = None,
        val: Optional[Union[Expression, DbReturnValue]] = None,
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding clauses."""
        from ..expressions import Predicate

        if isinstance(column_or_callable, Predicate):
            if op is not None or val is not None:
                raise ValueError(
                    "A Predicate carries its own operator and value; pass it "
                    "as the only argument."
                )
            predicate = column_or_callable
            self._clauses.append((conjunction, predicate.render))
            return
        if callable(column_or_callable):
            # Create a new instance of the concrete subclass for nesting
            temp_builder = type(self)(self._model_class, self._compiler)
            column_or_callable(temp_builder)
            if temp_builder.has_clauses():

                def render(ctx: RenderContext) -> str:
                    return f"({temp_builder._build_clause_list_string(ctx)})"

                self._clauses.append((conjunction, render))
        else:
            if op is None:
                raise ValueError(
                    f"Operator must be provided for non-callable {self._clause_keyword.lower()} clause."
                )
            operator = self._compiler.validate_operator(op)
            if val is None:
                null = compare_with_none(
                    self._quote_column(column_or_callable), operator
                )
                if null is not None:
                    self._clauses.append((conjunction, null))
                    return
                raise ValueError(
                    f"Value must be provided for non-callable {self._clause_keyword.lower()} clause."
                )
            if operator in ("IS", "IS NOT") and not isinstance(val, (bool, Expression)):
                # IS compares against NULL and the truth values, never
                # against a bound value: `x IS 5` is not SQL.
                raise ValueError(
                    f"{operator} takes None, True, or False. Compare a value "
                    f"with = or !=, or write the comparison with raw SQL."
                )
            quoted_col = self._quote_column(column_or_callable)

            if operator in ("LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE"):

                def render(ctx: RenderContext) -> str:
                    return ctx.compiler.compile_like(
                        quoted_col, ctx.compiler.format_operand(val, ctx), operator
                    )

            elif operator in ("IS", "IS NOT") and isinstance(val, bool):
                truth = val

                def render(ctx: RenderContext) -> str:
                    return ctx.compiler.compile_is_boolean(quoted_col, operator, truth)

            else:
                render = compare(quoted_col, operator, val)

            self._clauses.append((conjunction, render))

    def _add_in_internal(
        self,
        conjunction: str,
        col: str,
        vals: Union[List[DbReturnValue], QueryResolvable],
        *,
        op_override: bool = False,
        op_like_override: Optional[str] = None,
    ) -> None:
        """Internal handler for adding `IN` and `NOT IN` clauses."""
        quoted_col = self._quote_column(col)
        if isinstance(vals, list):
            render = in_list(quoted_col, vals, negate=op_override)
        else:
            inner = self._subquery(
                vals,
                "NOT IN" if op_override else "IN",
                "Argument for In/NotIn must be a list, a callable, "
                "QueryBuilder.raw(), or QueryBuilder instance.",
            )
            render = in_subquery(quoted_col, inner, negate=op_override)
        self._clauses.append((conjunction, render))

    def _build_clause_list_string(self, ctx: RenderContext) -> str:
        """Builds the complete clause string from all parts."""
        return render_clause_list(self._clauses, ctx)

    def render(self, ctx: RenderContext) -> str:
        """Builds the final clause string with the given context."""
        if not self._clauses:
            return ""
        return f"{self._clause_keyword} " + self._build_clause_list_string(ctx)

    def __str__(self) -> str:
        """Builds the final clause string with values inlined as literals."""
        return self.render(RenderContext(self._compiler))

    def has_clauses(self) -> bool:
        return bool(self._clauses)
