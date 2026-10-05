"""
Select-clause builder.
"""

from typing import TYPE_CHECKING, List, Optional

from sustained.rendering import compiler_or_default

if TYPE_CHECKING:
    from sustained.compilers import Compiler
    from sustained.rendering import RenderContext
    from sustained.types import Selectable


class SelectClauseBuilder:
    """
    Manages the list of selected items for a SQL query.
    """

    def __init__(self, compiler: Optional["Compiler"] = None) -> None:
        self._compiler = compiler_or_default(compiler)
        self._selected_columns: List["Selectable"] = []

    def __str__(self) -> str:
        """
        Generates the final column list for the SQL query.

        Values inside a subquery are inlined as SQL literals, because no
        render context is available. Use render() to parameterize them.

        Returns:
            The SQL fragment for the SELECT clause.
        """
        return self._render(self._compiler, None)

    def render(self, ctx: "RenderContext") -> str:
        """
        Generates the column list with the statement's render context.

        A subquery in the select list renders through the context, so its
        values parameterize with the rest of the statement and land in the
        parameter list in the order they appear in the SQL text.

        Args:
            ctx: The render context of the statement being built.

        Returns:
            The SQL fragment for the SELECT clause.
        """
        return self._render(ctx.compiler, ctx)

    def _render(self, compiler: "Compiler", ctx: 'Optional["RenderContext"]') -> str:
        """
        Builds the column list. If no columns are selected, it defaults to
        '*'. Otherwise, it joins the selected columns, correctly handling
        both string and expression objects.
        """
        if not self._selected_columns:
            return "*"

        return ", ".join(
            compiler.compile_select_item(c, ctx) for c in self._selected_columns
        )

    def select(self, *columns: "Selectable") -> None:
        """
        Adds one or more columns or expressions to the select list.

        Args:
            *columns: A list of columns or expression objects to select.
        """
        self._selected_columns.extend(columns)
