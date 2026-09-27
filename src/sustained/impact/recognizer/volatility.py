"""
Default volatility: whether a column default is a constant, a value
fixed for the statement, or a value computed for each row.
"""

from __future__ import annotations

from typing import (
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.tokens import (
    IDENT,
    PUNCT,
    WORD,
    Token,
)

# Calls whose value is fixed for the statement, so a column default made
# of them is computed once and stored in the catalog. Stable functions
# such as now() count, since Postgres evaluates the default once at
# ALTER time.
_NON_VOLATILE_CALLS = frozenset(
    {
        "now",
        "transaction_timestamp",
        "statement_timestamp",
        "current_timestamp",
        "current_date",
        "localtimestamp",
        "current_setting",
        "current_user",
        "cast",
        "coalesce",
        "nullif",
        "greatest",
        "least",
        "lower",
        "upper",
        "trim",
        "length",
        "abs",
        "round",
        "floor",
        "ceil",
        "concat",
        "make_date",
        "make_time",
        "make_timestamp",
        "make_timestamptz",
        "make_interval",
        "to_char",
        "to_date",
        "to_timestamp",
        "date_trunc",
        "timezone",
        "jsonb_build_object",
        "jsonb_build_array",
        "json_build_object",
        "json_build_array",
        "array",
        "row",
        "getdate",
        "getutcdate",
        "sysdatetime",
        "sysutcdatetime",
        "sysdatetimeoffset",
        "utc_timestamp",
        "curdate",
        "curtime",
        "datetime",
        "date",
        "time",
        "strftime",
        "julianday",
        "today",
    }
)
# Calls known to give a new value per row, which forces a rewrite.
_VOLATILE_CALLS = frozenset(
    {
        "random",
        "gen_random_uuid",
        "uuid_generate_v1",
        "uuid_generate_v1mc",
        "uuid_generate_v4",
        "uuidv4",
        "uuidv7",
        "clock_timestamp",
        "timeofday",
        "nextval",
        "newid",
        "newsequentialid",
        "uuid",
        "rand",
    }
)
# Bare words a default may hold that read the clock or the session once.
_STABLE_WORDS = frozenset(
    {
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "LOCALTIME",
        "LOCALTIMESTAMP",
        "CURRENT_USER",
        "SESSION_USER",
        "USER",
        "CURRENT_SCHEMA",
        "CURRENT_CATALOG",
        "CURRENT_ROLE",
    }
)

VOLATILITIES = ("constant", "stable", "volatile")


def classify_default(tokens: Sequence[Token]) -> Tuple[str, Optional[str], bool]:
    """
    How a column default behaves when a column is added with it:
    `constant`, `stable` (read once, such as now()), or `volatile` (a
    new value per row, such as random()). Also returns the function
    that decided it, and whether the answer is certain.

    A function the recognizer does not know counts as volatile, which is
    the worst case, and the answer is then not certain. A quoted name
    followed by `(` is a call, compared as the quotes spell it. A call
    qualified by a schema other than `pg_catalog`, such as `app.now()`,
    is a function the recognizer does not know.
    """
    unknown: Optional[str] = None
    stable = False
    for index, token in enumerate(tokens):
        if token.kind not in (WORD, IDENT):
            continue
        start = _name_start(tokens, index)
        if start > 0 and tokens[start - 1].text == "::":
            continue
        is_call = (
            index + 1 < len(tokens)
            and tokens[index + 1].text == "("
            and tokens[index + 1].kind == PUNCT
        )
        if not is_call:
            if token.kind == WORD and start == index:
                stable = stable or token.value in _STABLE_WORDS
            continue
        name = token.value if token.kind == IDENT else token.text.lower()
        if start < index and not (
            start == index - 2 and (tokens[start].name or "").lower() == "pg_catalog"
        ):
            if unknown is None:
                unknown = ".".join(t.name or "" for t in tokens[start : index + 1 : 2])
            continue
        if name in _VOLATILE_CALLS:
            return "volatile", name, True
        if name in _NON_VOLATILE_CALLS:
            stable = True
        elif unknown is None:
            unknown = name
    if unknown is not None:
        return "volatile", unknown, False
    return ("stable" if stable else "constant"), None, True


def _name_start(tokens: Sequence[Token], index: int) -> int:
    """The index of the first part of the dotted name that ends at `index`."""
    start = index
    while (
        start >= 2
        and tokens[start - 1].kind == PUNCT
        and tokens[start - 1].text == "."
        and tokens[start - 2].kind in (WORD, IDENT)
    ):
        start -= 2
    return start
