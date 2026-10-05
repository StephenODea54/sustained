from __future__ import annotations

from .conditional_clause_builder import ConditionalClauseBuilder


class WhereClauseBuilder(ConditionalClauseBuilder):
    """A helper class for building complex WHERE clauses."""

    _clause_keyword = "WHERE"
