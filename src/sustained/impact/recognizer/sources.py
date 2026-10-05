"""
The tables a query reads, for a statement that copies rows from it:
`CREATE TABLE ... AS SELECT` and `INSERT ... SELECT`.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

from sustained.impact.tokens import Token

# The words that may open a FROM item before its name.
_ITEM_PREFIXES = ("ONLY", "LATERAL")

# The words that open a query in parentheses.
_QUERY_OPENERS = ("SELECT", "WITH", "TABLE", "VALUES")

# The words that end a FROM list, after which a comma separates
# something else, such as the terms of a GROUP BY.
_LIST_ENDS = (
    "WHERE",
    "GROUP",
    "HAVING",
    "WINDOW",
    "ORDER",
    "LIMIT",
    "OFFSET",
    "FETCH",
    "UNION",
    "EXCEPT",
    "INTERSECT",
    "RETURNING",
)


def _cte_names(tokens: Sequence[Token]) -> Set[str]:
    """The lower case names the query's WITH clauses define."""
    names: Set[str] = set()
    for index, token in enumerate(tokens):
        if not token.is_word("AS") or index + 1 >= len(tokens):
            continue
        if not tokens[index + 1].is_punct("("):
            continue
        at = index - 1
        if at >= 0 and tokens[at].is_punct(")"):
            # `name (columns) AS (...)`: step back over the column list.
            depth = 0
            while at >= 0:
                if tokens[at].is_punct(")"):
                    depth += 1
                elif tokens[at].is_punct("("):
                    depth -= 1
                    if depth == 0:
                        break
                at -= 1
            at -= 1
        if at >= 0 and tokens[at].name is not None:
            names.add((tokens[at].name or "").lower())
    return names


def _item(tokens: Sequence[Token], at: int) -> Tuple[Optional[str], int, bool]:
    """
    The table a FROM item at `at` names, the index after its name, and
    whether the item reads a table at all. A subquery reads no table of
    its own here: its own FROM names its tables. A function call or a
    VALUES list reads rows no table holds.
    """
    while at < len(tokens) and tokens[at].is_word(*_ITEM_PREFIXES):
        at += 1
    if at >= len(tokens):
        return None, at, False
    if tokens[at].is_punct("("):
        inner = tokens[at + 1] if at + 1 < len(tokens) else None
        query = inner is not None and inner.is_word("SELECT", "WITH", "TABLE")
        return None, at + 1, query
    if tokens[at].is_word("VALUES") or tokens[at].name is None:
        return None, at, False
    parts: List[str] = []
    while at < len(tokens) and tokens[at].name is not None:
        parts.append(tokens[at].name or "")
        at += 1
        if at < len(tokens) and tokens[at].is_punct("."):
            at += 1
            continue
        break
    if at < len(tokens) and tokens[at].is_punct("("):
        return None, at, False
    return ".".join(parts), at, True


def tables_read(tokens: Sequence[Token]) -> Optional[Tuple[str, ...]]:
    """
    The tables a query reads, in the order it names them: each item of
    a FROM list, each JOIN, and the table of a `TABLE name` query. Empty
    for a query that reads no table, such as `SELECT 1` or VALUES. None
    when an item reads rows from something other than a table, such as
    a function call, so the rows it gives are unknown. A name a WITH
    clause of the query defines is not a table.
    """
    ctes = _cte_names(tokens)
    found: List[str] = []
    # Whether a FROM list is open at each parenthesis depth, so a comma
    # there starts another item.
    listing: Dict[int, bool] = {}
    # Whether each open parenthesis holds a query. The FROM of a
    # function's arguments, as in `extract(year FROM c)`, names no table.
    queries: List[bool] = [True]
    expect = bool(tokens) and tokens[0].is_word("TABLE")
    index = 1 if expect else 0
    while index < len(tokens):
        token = tokens[index]
        if expect:
            expect = False
            name, after, reads = _item(tokens, index)
            if not reads:
                return None
            if name is not None:
                if name.lower() not in ctes:
                    found.append(name)
                index = after
                continue
        if token.is_punct("("):
            inner = tokens[index + 1] if index + 1 < len(tokens) else None
            queries.append(inner is not None and inner.is_word(*_QUERY_OPENERS))
        elif token.is_punct(")"):
            listing.pop(len(queries) - 1, None)
            if len(queries) > 1:
                queries.pop()
        elif not queries[-1]:
            pass
        elif token.is_word("FROM", "JOIN"):
            listing[len(queries) - 1] = True
            expect = True
        elif token.is_punct(",") and listing.get(len(queries) - 1):
            expect = True
        elif token.is_word(*_LIST_ENDS):
            listing[len(queries) - 1] = False
        index += 1
    # A FROM, JOIN, or TABLE with no item after it names no table.
    return None if expect else tuple(found)
