from __future__ import annotations

from .conditional_clause_builder import ConditionalClauseBuilder


class HavingClauseBuilder(ConditionalClauseBuilder):
    """A helper class for building complex HAVING clauses."""

    _clause_keyword = "HAVING"
