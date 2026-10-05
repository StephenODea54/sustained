from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
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
from ..naming import resolve_public_name
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

    # Each where-family method: the handler it calls and the variant
    # arguments it passes. A having name and an and/or prefix reach the
    # same entry; see _CLAUSE_METHODS below.
    _WHERE_METHOD_MAP: Dict[str, Tuple[str, Dict[str, object]]] = {
        "where": ("_add_internal", {}),
        "whereIn": ("_add_in_internal", {"negate": False}),
        "whereNotIn": ("_add_in_internal", {"negate": True}),
        "whereBetween": ("_add_between_internal", {"negate": False}),
        "whereNotBetween": ("_add_between_internal", {"negate": True}),
        "whereExists": ("_add_exists_internal", {"negate": False}),
        "whereNotExists": ("_add_exists_internal", {"negate": True}),
        "whereLike": ("_add_like_internal", {"operator": "LIKE"}),
        "whereILike": ("_add_like_internal", {"operator": "ILIKE"}),
        "whereNull": ("_add_null_internal", {"negate": False}),
        "whereNotNull": ("_add_null_internal", {"negate": True}),
        "whereRaw": ("_add_raw_internal", {}),
    }

    def __init__(
        self, model_class: Type["Model"], compiler: Optional["Compiler"] = None
    ):
        self._model_class = model_class
        self._compiler = compiler_or_default(compiler)
        self._clauses: List[Tuple[str, Renderable]] = []

    def _quote_column(self, column: ColumnReference) -> Renderable:
        """Quotes a column reference through the compiler.

        The compiler accepts an identifier path or a call on one column, such
        as an aggregate in a HAVING clause, and raises on any other string.
        An expression object renders with the statement's context.
        """
        return self._compiler.column_part(column)

    def __getattr__(self, name: str) -> Callable[..., "ConditionalClauseBuilder"]:
        """
        Resolves any other spelling of a clause method, such as WHERE_IN
        or orwherein, without regard to case or underscores.
        """
        # Private names are never clause methods; see QueryBuilder.__getattr__.
        canonical = (
            None if name.startswith("_") else resolve_public_name(type(self), name)
        )
        if canonical is None:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )
        return cast(Callable[..., "ConditionalClauseBuilder"], getattr(self, canonical))

    def _conjunction(self, prefix: str) -> str:
        """
        The conjunction a new clause joins with. A clause with no and/or
        prefix joins with AND after the first clause. A prefixed first
        clause raises, because it has nothing to join.
        """
        if not prefix:
            return "AND" if self._clauses else ""
        if not self._clauses:
            raise RuntimeError(
                f"Cannot start a {self._clause_keyword.lower()} clause with '{prefix}'."
            )
        return prefix.upper()

    def _add_between_internal(
        self,
        conjunction: str,
        col: str,
        val1: DbReturnValue,
        val2: DbReturnValue,
        *,
        negate: bool,
    ) -> None:
        """Internal handler for adding `BETWEEN` and `NOT BETWEEN` clauses."""
        render = between(self._quote_column(col), val1, val2, negate=negate)
        self._clauses.append((conjunction, render))

    def _add_exists_internal(
        self,
        conjunction: str,
        query: QueryResolvable,
        *,
        negate: bool,
    ) -> None:
        """Internal handler for adding `EXISTS` and `NOT EXISTS` clauses."""
        actual_op = "NOT EXISTS" if negate else "EXISTS"
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
        operator: str,
    ) -> None:
        """Internal handler for adding `LIKE` and `ILIKE` clauses."""
        self._clauses.append(
            (conjunction, like(self._quote_column(col), pattern, operator))
        )

    def _add_raw_internal(
        self,
        conjunction: str,
        sql: str,
        params: Optional[Sequence[SqlValue]] = None,
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
        negate: bool,
    ) -> None:
        """Internal handler for adding `IS NULL` and `IS NOT NULL` clauses."""
        render = null_test(self._quote_column(col), negate=negate)
        self._clauses.append((conjunction, render))

    def _add_internal(
        self,
        conjunction: str,
        column_or_callable: Union[
            ColumnReference, Callable[["ConditionalClauseBuilder"], None], "Predicate"
        ],
        op: Optional[str] = None,
        val: Optional[Union[Expression, DbReturnValue, AnyQuery]] = None,
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
                    column_sql = render_part(quoted_col, ctx)
                    return ctx.compiler.compile_like(
                        column_sql, ctx.compiler.format_operand(val, ctx), operator
                    )

            elif operator in ("IS", "IS NOT") and isinstance(val, bool):
                truth = val

                def render(ctx: RenderContext) -> str:
                    return ctx.compiler.compile_is_boolean(
                        render_part(quoted_col, ctx), operator, truth
                    )

            else:
                render = compare(quoted_col, operator, val)

            self._clauses.append((conjunction, render))

    def _add_in_internal(
        self,
        conjunction: str,
        col: str,
        vals: Union[List[DbReturnValue], QueryResolvable],
        *,
        negate: bool,
    ) -> None:
        """Internal handler for adding `IN` and `NOT IN` clauses."""
        quoted_col = self._quote_column(col)
        if isinstance(vals, list):
            render = in_list(quoted_col, vals, negate=negate)
        else:
            inner = self._subquery(
                vals,
                "NOT IN" if negate else "IN",
                "Argument for In/NotIn must be a list, a callable, "
                "QueryBuilder.raw(), or QueryBuilder instance.",
            )
            render = in_subquery(quoted_col, inner, negate=negate)
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


class ClauseMethod(NamedTuple):
    """
    One generated clause method: the clause family it belongs to ("where"
    or "having"), its and/or prefix ("" for none), the handler it calls,
    and the variant arguments it passes to the handler.
    """

    family: str
    prefix: str
    handler: str
    variant: Dict[str, object]


def _clause_methods() -> Dict[str, ClauseMethod]:
    """
    Every clause method name: each where-family entry under its where and
    having names, each with no prefix, an and prefix, and an or prefix.
    """
    methods = {}
    for where_name, (
        handler,
        variant,
    ) in ConditionalClauseBuilder._WHERE_METHOD_MAP.items():
        for family in ("where", "having"):
            base = family + where_name[len("where") :]
            for prefix in ("", "and", "or"):
                name = prefix + base[0].upper() + base[1:] if prefix else base
                methods[name] = ClauseMethod(family, prefix, handler, variant)
    return methods


_CLAUSE_METHODS = _clause_methods()
"""The generated clause methods by name. QueryBuilder delegates each one."""


def _clause_method(
    name: str, method: ClauseMethod
) -> Callable[..., ConditionalClauseBuilder]:
    """
    Builds one clause method. Both clause builders accept the where and
    the having names, so a nested callable can use either. The typed
    overloads a caller sees live in the stub beside this file.
    """

    def call(
        self: ConditionalClauseBuilder, *args: object, **kwargs: object
    ) -> ConditionalClauseBuilder:
        handler = getattr(self, method.handler)
        handler(self._conjunction(method.prefix), *args, **method.variant, **kwargs)
        return self

    call.__name__ = name
    call.__qualname__ = f"ConditionalClauseBuilder.{name}"
    return call


for _name, _method in _CLAUSE_METHODS.items():
    setattr(ConditionalClauseBuilder, _name, _clause_method(_name, _method))
del _name, _method
