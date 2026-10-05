from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
    cast,
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
    BasicJoinMapping,
    ColumnReference,
    Expression,
    JoinMappingWithThrough,
)

if TYPE_CHECKING:
    from ..builder import QueryBuilder
    from ..compilers import Compiler
    from ..model import Model
    from ..types import AnyQuery


def _column_on(reference: str, model_class: Type["Model"]) -> Optional[str]:
    """
    The column a join mapping's "to" reference names on the related model,
    or None when the reference names another table. The reference may name
    the table bare, as "orders.id", or qualified, as "sales.orders.id".
    """
    from ..model import names_model_table

    if "." not in reference:
        return None
    table, column = reference.rsplit(".", 1)
    if names_model_table(table, model_class):
        return column
    return None


class OnClauseBuilder:
    """
    A helper class for building complex JOIN ... ON clauses.
    An instance of this is passed to the lambda in `...join(..., lambda j: ...)` calls.
    """

    def __init__(self, compiler: Optional["Compiler"] = None) -> None:
        self._compiler = compiler_or_default(compiler)
        self._conditions: List[Tuple[str, Renderable]] = []

    def __getattr__(self, name: str) -> Callable[..., "OnClauseBuilder"]:
        """Resolves `ON`, `and_on`, and any other spelling of on, andOn, and orOn."""
        canonical = (
            None if name.startswith("_") else resolve_public_name(type(self), name)
        )
        if canonical is None:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )
        return cast(Callable[..., "OnClauseBuilder"], getattr(self, canonical))

    def on(
        self, col1: ColumnReference, op: str, col2: Union[ColumnReference, "AnyQuery"]
    ) -> "OnClauseBuilder":
        """Adds an ON condition. If this is not the first condition, it's treated as AND ON."""
        conjunction = "AND" if self._conditions else ""
        self._add_condition(conjunction, col1, op, col2)
        return self

    def andOn(
        self, col1: ColumnReference, op: str, col2: Union[ColumnReference, "AnyQuery"]
    ) -> "OnClauseBuilder":
        """Adds an AND ON condition."""
        if not self._conditions:
            raise RuntimeError(
                "Cannot use 'andOn' for the first join condition. Use 'on' instead."
            )
        self._add_condition("AND", col1, op, col2)
        return self

    def orOn(
        self, col1: ColumnReference, op: str, col2: Union[ColumnReference, "AnyQuery"]
    ) -> "OnClauseBuilder":
        """Adds an OR ON condition."""
        if not self._conditions:
            raise RuntimeError(
                "Cannot use 'orOn' for the first join condition. Use 'on' instead."
            )
        self._add_condition("OR", col1, op, col2)
        return self

    def _add_condition(
        self,
        conjunction: str,
        col1: ColumnReference,
        op: str,
        col2: Union[ColumnReference, "AnyQuery"],
    ) -> None:
        # Late import to avoid circular dependency
        from ..builder import QueryBuilder

        # The operator arrives as free text and lands between two quoted
        # identifiers, so it goes through the same check where() applies.
        op = self._compiler.validate_operator(op)
        left = self._compiler.column_part(col1)
        right: Renderable
        if isinstance(col2, QueryBuilder):
            sub_query = col2
            right = lambda ctx: f"({render_nested(sub_query, ctx)})"  # noqa: E731
        elif isinstance(col2, Expression):
            right = str(col2)
        else:
            right = self._compiler.column_part(col2)
        condition: Renderable
        if isinstance(left, str) and isinstance(right, str):
            condition = f"{left} {op} {right}"
        else:
            parts = (left, right)
            condition = lambda ctx: (  # noqa: E731
                f"{render_part(parts[0], ctx)} {op} {render_part(parts[1], ctx)}"
            )

        self._conditions.append((conjunction, condition))

    def render(self, ctx: RenderContext) -> str:
        """
        Builds the ON clause with the statement's render context. A
        subquery on the right of a condition renders through it, so its
        values join the statement's parameter list.
        """
        if not self._conditions:
            raise RuntimeError("A join condition must be specified inside the lambda.")

        return render_clause_list(self._conditions, ctx)

    def __str__(self) -> str:
        """Builds the ON clause with the values inlined as literals."""
        return self.render(RenderContext(self._compiler))


class JoinClauseBuilder:
    """A helper class for building JOIN clauses."""

    _JOIN_METHOD_MAP = {
        "": "JOIN",
        "inner": "INNER JOIN",
        "left": "LEFT JOIN",
        "leftOuter": "LEFT OUTER JOIN",
        "right": "RIGHT JOIN",
        "rightOuter": "RIGHT OUTER JOIN",
        "full": "FULL JOIN",
        "fullOuter": "FULL OUTER JOIN",
        "cross": "CROSS JOIN",
    }
    # The join to the link table of a many-to-many relation, by the join
    # type asked for. A join type not listed takes an INNER JOIN.
    _LINK_JOIN_TYPES = {
        "LEFT JOIN": "LEFT JOIN",
        "LEFT OUTER JOIN": "LEFT OUTER JOIN",
        "FULL JOIN": "LEFT JOIN",
        "FULL OUTER JOIN": "LEFT OUTER JOIN",
    }

    def __init__(
        self, model_class: Type["Model"], compiler: Optional["Compiler"] = None
    ):
        self._model_class = model_class
        self._compiler = compiler_or_default(compiler)
        self._joins: List[Renderable] = []
        # Link tables that a many-to-many join has already added. A second
        # join through the same link table renders it under an alias, so each
        # copy of the link table has its own name.
        self._link_tables: Set[str] = set()

    def render(self, ctx: RenderContext) -> str:
        """
        Builds the join clauses with the statement's render context, so a
        subquery in an ON condition contributes its values to the
        statement's parameter list.
        """
        return " ".join(render_part(join, ctx) for join in self._joins)

    def __str__(self) -> str:
        """Builds the join clauses with the values inlined as literals."""
        return self.render(RenderContext(self._compiler))

    def __getattr__(self, name: str) -> Callable[..., "JoinClauseBuilder"]:
        """
        Resolves any other spelling of a join method, such as LEFT_JOIN or
        leftjoin, without regard to case or underscores.
        """
        # Private names are never join methods; see QueryBuilder.__getattr__.
        canonical = (
            None if name.startswith("_") else resolve_public_name(type(self), name)
        )
        if canonical is None:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )
        return cast(Callable[..., "JoinClauseBuilder"], getattr(self, canonical))

    def _add_join(
        self,
        join_type: str,
        table: str,
        *args: "JoinArgument",
        using: Optional[List[str]] = None,
    ) -> None:
        """
        Adds a raw join. One name covers three call signatures, so the
        arguments are sorted out here rather than in the signature. The
        typed overloads a caller sees live in join_builder.pyi.
        """
        quoted_table = self._compiler.quote_fully_qualified_identifier(table)

        if using:
            if args:
                raise ValueError(
                    "Cannot use both an ON clause and a USING clause in the same join."
                )
            if not isinstance(using, list):
                raise TypeError("The 'using' argument must be a list of column names.")
            quoted_using = ", ".join(self._compiler.quote_identifier(u) for u in using)
            join_condition = f"USING ({quoted_using})"
        elif len(args) == 3 or (len(args) == 1 and callable(args[0])):
            on_builder = OnClauseBuilder(self._compiler)
            if len(args) == 3:
                # Static syntax: .join('table', 'col1', '=', 'col2')
                col1, op, col2 = cast(Tuple[str, str, OnOperand], args)
                on_builder.on(col1, op, col2)
            else:
                # Composable syntax: .join('table', lambda j: ...)
                cast(Callable[[OnClauseBuilder], None], args[0])(on_builder)

            # The ON clause can contain a subquery, whose values belong to
            # the statement, so it renders later.
            def render_join(ctx: RenderContext) -> str:
                return f"{join_type} {quoted_table} ON {on_builder.render(ctx)}"

            self._joins.append(render_join)
            return
        elif not args and join_type == "CROSS JOIN":
            # A cross join pairs every row with every row, so it takes no
            # condition.
            join_condition = ""
        else:
            raise ValueError(
                "Invalid arguments for join method. Use `join(table, col1, op, col2)`, `join(table, lambda j: ...)`, or `join(table, using=['col1', 'col2'])`."
            )

        self._joins.append(f"{join_type} {quoted_table} {join_condition}".rstrip())

    def _join_related_internal(
        self, join_type: str, relation_name: str, alias: Optional[str] = None
    ) -> None:
        """Internal handler for adding a join based on a defined relation."""
        from ..model import qualified_table_name, resolve_relation

        relation, related_model_class = resolve_relation(
            self._model_class, relation_name
        )
        related_table = qualified_table_name(related_model_class)
        if not alias and related_table == qualified_table_name(self._model_class):
            # Both sides of the ON clause would name the same table, which
            # the database rejects as ambiguous or reads as a self-match.
            raise ValueError(
                f"The relation '{relation_name}' joins the table "
                f"'{related_table}' to itself. Pass alias= to name the "
                "joined copy."
            )
        join_info = relation["join"]
        if "through" in join_info:
            # Cast to the more specific TypedDict to satisfy mypy
            through_join_info = cast(JoinMappingWithThrough, join_info)
            self._add_through_join(
                join_type, through_join_info, related_model_class, alias
            )
        else:
            basic_join_info = join_info
            self._add_basic_join(join_type, basic_join_info, related_model_class, alias)

    def _add_basic_join(
        self,
        join_type: str,
        join_info: BasicJoinMapping,
        related_model_class: Type["Model"],
        alias: Optional[str] = None,
    ) -> None:
        """Adds a basic (e.g., one-to-one, one-to-many) join to the query."""
        join_table_part, to_col = self._join_target(
            related_model_class, join_info["to"], alias
        )
        from_col = self._compiler.quote_fully_qualified_identifier(join_info["from"])
        self._joins.append(f"{join_type} {join_table_part} ON {from_col} = {to_col}")

    def _join_target(
        self, related_model_class: Type["Model"], to_ref: str, alias: Optional[str]
    ) -> Tuple[str, str]:
        """
        Returns the joined table, with its alias when one is given, and the
        column on it that the ON clause compares. With an alias, a column
        named on the related table points at the alias instead.
        """
        from ..model import qualified_table_name

        table_sql = self._compiler.quote_fully_qualified_identifier(
            qualified_table_name(related_model_class)
        )
        to_col = self._compiler.quote_fully_qualified_identifier(to_ref)
        if not alias:
            return table_sql, to_col
        quoted_alias = self._compiler.quote_alias(alias)
        to_column = _column_on(to_ref, related_model_class)
        if to_column is not None:
            to_col = f"{quoted_alias}.{self._compiler.quote_identifier(to_column)}"
        return f"{table_sql} AS {quoted_alias}", to_col

    def _add_through_join(
        self,
        join_type: str,
        join_info: JoinMappingWithThrough,
        related_model_class: Type["Model"],
        alias: Optional[str] = None,
    ) -> None:
        """Adds a many-to-many join using a 'through' table."""
        from ..model import qualified_table_name

        # First join: from the base model's table to the 'through' table.
        from_col = self._compiler.quote_fully_qualified_identifier(join_info["from"])
        through_from_mapping = join_info["through"]["from"]

        through_table_ref = through_from_mapping["table"]
        through_table_name: str
        if isinstance(through_table_ref, str):
            through_table_name = through_table_ref
        else:
            through_table_name = qualified_table_name(through_table_ref)
        quoted_through_table = self._compiler.quote_fully_qualified_identifier(
            through_table_name
        )
        through_from_key = self._compiler.quote_identifier(through_from_mapping["key"])

        through_table_part = quoted_through_table
        if through_table_name in self._link_tables:
            if not alias:
                raise ValueError(
                    f"The link table '{through_table_name}' is already joined. "
                    "Pass alias= to join through it again."
                )
            link_alias = f"{alias}_{through_table_name.rsplit('.', 1)[-1]}"
            quoted_through_table = self._compiler.quote_alias(link_alias)
            through_table_part = f"{through_table_part} AS {quoted_through_table}"
        self._link_tables.add(through_table_name)

        on_clause1 = f"{from_col} = {quoted_through_table}.{through_from_key}"
        # A base row with no link row drops at an INNER JOIN to the link
        # table, so a left or full join takes a LEFT JOIN there. A right
        # join keeps every far row at its own hop, so INNER covers it.
        link_join_type = self._LINK_JOIN_TYPES.get(join_type, "INNER JOIN")
        join_clause1 = f"{link_join_type} {through_table_part} ON {on_clause1}"
        self._joins.append(join_clause1)

        # Second join: from the 'through' table to the final related model's table.
        through_to_mapping = join_info["through"]["to"]
        through_to_key = self._compiler.quote_identifier(through_to_mapping["key"])
        join_table_part, to_col = self._join_target(
            related_model_class, join_info["to"], alias
        )
        on_clause2 = f"{quoted_through_table}.{through_to_key} = {to_col}"

        second_join_type = "INNER JOIN" if join_type == "JOIN" else join_type
        join_clause2 = f"{second_join_type} {join_table_part} ON {on_clause2}"
        self._joins.append(join_clause2)


OnOperand = Union[str, Expression, "AnyQuery"]
"""The right side of an ON condition: a column, raw SQL, or a subquery."""

JoinArgument = Union[OnOperand, Callable[[OnClauseBuilder], None]]
"""A positional argument of a raw join after the table: an ON part or a lambda."""


class JoinMethod(NamedTuple):
    """One generated join method: its SQL join type and whether it joins a relation."""

    join_type: str
    related: bool


def _join_methods() -> Dict[str, JoinMethod]:
    """Every join method name: each join type as a raw join and a relation join."""
    methods = {}
    for prefix, join_type in JoinClauseBuilder._JOIN_METHOD_MAP.items():
        base = f"{prefix}Join" if prefix else "join"
        methods[base] = JoinMethod(join_type, False)
        methods[f"{base}Related"] = JoinMethod(join_type, True)
    return methods


_JOIN_METHODS = _join_methods()
"""The generated join methods by name. QueryBuilder delegates each one."""


def _raw_join_method(name: str, join_type: str) -> Callable[..., JoinClauseBuilder]:
    """Builds one raw join method. The typed overloads live in join_builder.pyi."""

    def call(
        self: JoinClauseBuilder,
        table: str,
        *args: JoinArgument,
        using: Optional[List[str]] = None,
    ) -> JoinClauseBuilder:
        self._add_join(join_type, table, *args, using=using)
        return self

    call.__name__ = name
    call.__qualname__ = f"JoinClauseBuilder.{name}"
    return call


def _related_join_method(name: str, join_type: str) -> Callable[..., JoinClauseBuilder]:
    """Builds one relation join method."""

    def call(
        self: JoinClauseBuilder, relation_name: str, alias: Optional[str] = None
    ) -> JoinClauseBuilder:
        self._join_related_internal(join_type, relation_name, alias)
        return self

    call.__name__ = name
    call.__qualname__ = f"JoinClauseBuilder.{name}"
    return call


for _name, _method in _JOIN_METHODS.items():
    _build = _related_join_method if _method.related else _raw_join_method
    setattr(JoinClauseBuilder, _name, _build(_name, _method.join_type))
del _name, _method, _build
