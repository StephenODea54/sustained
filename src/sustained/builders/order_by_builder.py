from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple, Type

from ..expressions import Literal
from ..rendering import compiler_or_default

if TYPE_CHECKING:
    from ..compilers import Compiler
    from ..model import Model
    from ..types import ColumnReference


def reject_literal(column: object, method: str) -> None:
    """
    Raises ValueError for a Literal. In ORDER BY and GROUP BY the integer 1
    names the first select-list column, not the value 1. Literal means a
    value everywhere else, so it raises here, and raw('1') writes the
    position.
    """
    if isinstance(column, Literal):
        raise ValueError(
            f"{method}() does not take a Literal. "
            "Use raw('1') to name a select-list position, or col() for a column."
        )


class OrderByClauseBuilder:
    """
    A builder for creating and managing ORDER BY clauses in a SQL query.
    """

    def __init__(
        self, model_class: Type["Model"], compiler: Optional["Compiler"] = None
    ):
        """
        Initializes the OrderByClauseBuilder.

        Args:
            model_class (Type[Model]): The Model class associated with this query.
        """
        self._model_class = model_class
        self._compiler = compiler_or_default(compiler)
        self._clauses: List[Tuple[ColumnReference, str, Optional[str]]] = []

    def orderBy(
        self,
        column: ColumnReference,
        direction: str = "asc",
        nulls: Optional[str] = None,
    ) -> "OrderByClauseBuilder":
        """
        Adds an ORDER BY clause to the query.

        Args:
            column: The column to order by, or raw() SQL.
            direction (str, optional): The direction of ordering ('asc' or 'desc').
                                     Defaults to 'asc'.
            nulls (str, optional): 'first' or 'last' to place NULL values at
                that end of the order. None keeps the engine's placement.

        Returns:
            OrderByClauseBuilder: The builder instance for chaining.
        """
        reject_literal(column, "orderBy")
        normalized_direction = direction.upper()
        if normalized_direction not in ["ASC", "DESC"]:
            raise ValueError("Order by direction must be 'asc' or 'desc'.")
        normalized_nulls = None if nulls is None else nulls.upper()
        if normalized_nulls not in (None, "FIRST", "LAST"):
            raise ValueError("Order by nulls must be 'first', 'last', or None.")

        self._clauses.append((column, normalized_direction, normalized_nulls))
        return self

    def __str__(self) -> str:
        """
        Builds and returns the final ORDER BY clause string.

        Returns:
            str: The complete ORDER BY clause, or an empty string if no clauses exist.
        """
        if not self._clauses:
            return ""

        clauses_str = ", ".join(
            [
                self._compiler.compile_order_entry(
                    self._compiler.quote_column_reference(col), direction, nulls
                )
                for col, direction, nulls in self._clauses
            ]
        )
        return f"ORDER BY {clauses_str}"
