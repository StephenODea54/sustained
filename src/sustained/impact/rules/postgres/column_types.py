"""
Column type changes: which ones PostgreSQL makes in the catalog alone,
and which rewrite the table and rebuild its indexes.
"""

from __future__ import annotations

import re
from typing import List, Mapping, Optional, Tuple

from sustained.impact.model import (
    Action,
    Confidence,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, common
from sustained.impact.rules.postgres.catalog import (
    ALTER_TYPE,
    ALTER_TYPE_COERCIBLE,
)
from sustained.impact.rules.postgres.locks import (
    ACCESS_EXCLUSIVE,
)
from sustained.impact.rules.postgres.statements import (
    foreign_key_effect,
)

_TYPE_ALIASES: Mapping[str, str] = {
    "character varying": "varchar",
    "char varying": "varchar",
    "character": "char",
    "bpchar": "char",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
    "decimal": "numeric",
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "int2": "smallint",
    "bool": "boolean",
    "float8": "double precision",
    "float": "double precision",
    "float4": "real",
}
# Types whose conversions no rule treats as binary coercible unless
# listed below, so a change between two of them is a known rewrite.
_BUILTIN_TYPES = frozenset(
    {
        "smallint",
        "integer",
        "bigint",
        "numeric",
        "real",
        "double precision",
        "text",
        "varchar",
        "char",
        "boolean",
        "date",
        "time",
        "timetz",
        "timestamp",
        "timestamptz",
        "interval",
        "uuid",
        "json",
        "jsonb",
        "bytea",
    }
)
_TYPE_ARGS_RE = re.compile(r"\(([^)]*)\)")


def _pg_type(text: str) -> Tuple[str, Optional[Tuple[int, ...]], bool]:
    """
    A type's base name, its numeric arguments, and whether it is an
    array. The arguments are None when they are not all numbers.
    """
    lowered = " ".join(text.lower().replace('"', "").split())
    array = lowered.endswith("[]")
    lowered = lowered.rstrip("[] ")
    match = _TYPE_ARGS_RE.search(lowered)
    args: Optional[Tuple[int, ...]] = ()
    if match:
        try:
            args = tuple(int(a) for a in match.group(1).split(","))
        except ValueError:
            args = None
        lowered = " ".join((lowered[: match.start()] + lowered[match.end() :]).split())
    if lowered.startswith("pg_catalog."):
        lowered = lowered[len("pg_catalog.") :]
    return _TYPE_ALIASES.get(lowered, lowered), args, array


def type_change(
    from_type: str, to_type: str, settings: Mapping[str, str]
) -> Tuple[Work, Confidence, str]:
    """
    The work a column type change does: `catalog` for a binary-coercible
    change, `rewrite` otherwise, with how sure the answer is and why.
    """
    old, new = _pg_type(from_type), _pg_type(to_type)
    if old == new:
        return Work.CATALOG, Confidence.KNOWN, "the type does not change"
    (old_base, old_args, old_array), (new_base, new_args, new_array) = old, new
    readable = old_args is not None and new_args is not None
    if old_array == new_array and old_args is not None and new_args is not None:
        verdict = _coercible(old_base, old_args, new_base, new_args, settings)
        if verdict is not None:
            return verdict
    known = readable and old_base in _BUILTIN_TYPES and new_base in _BUILTIN_TYPES
    return (
        Work.REWRITE,
        Confidence.KNOWN if known else Confidence.LIKELY,
        f"{from_type} to {to_type} is not binary coercible"
        + ("" if known else " as far as the rules know"),
    )


def _coercible(
    old: str,
    old_args: Tuple[int, ...],
    new: str,
    new_args: Tuple[int, ...],
    settings: Mapping[str, str],
) -> Optional[Tuple[Work, Confidence, str]]:
    widened = "a widening change is binary coercible"
    if old in ("varchar", "text") and new == "text":
        return Work.CATALOG, Confidence.KNOWN, widened
    if old in ("varchar", "text") and new == "varchar" and not new_args:
        return Work.CATALOG, Confidence.KNOWN, widened
    if old == new == "varchar" and old_args and new_args[0] >= old_args[0]:
        return Work.CATALOG, Confidence.KNOWN, widened
    if old == new == "numeric" and old_args and not new_args:
        return Work.CATALOG, Confidence.KNOWN, widened
    if old == new == "numeric" and len(old_args) == len(new_args) and old_args:
        same_scale = old_args[1:] == new_args[1:]
        if same_scale and new_args[0] >= old_args[0]:
            return Work.CATALOG, Confidence.KNOWN, widened
    if old == "timestamp" and new == "timestamptz" and old_args == new_args:
        zone = settings.get("TimeZone")
        if zone is None:
            return (
                Work.REWRITE,
                Confidence.LIKELY,
                "timestamp to timestamptz only changes the catalog when the "
                "TimeZone setting is UTC, and the setting was not read",
            )
        if zone.upper() in ("UTC", "ETC/UTC", "GMT", "Z"):
            return Work.CATALOG, Confidence.KNOWN, "TimeZone is UTC"
    return None


_TRIVIAL_USING_RE = re.compile(r'\s*"?(?P<column>[^"\s:]+)"?\s*(::.*)?', re.DOTALL)


def _alter_column_type(facts: Facts, action: Action) -> Outcome:
    outcome = _column_type(facts, action)
    keys = _type_keys(facts, action)
    if not keys:
        return outcome
    confidence = min([outcome.confidence] + [e.confidence for e in keys])
    return outcome._replace(
        effects=outcome.effects + tuple(keys), confidence=confidence
    )


def _column_type(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    column = action.column or "?"
    to_type = str(action.options.get("type"))
    using = action.options.get("using")
    intent = facts.intent
    from_type: Optional[str] = None
    if intent is not None and intent.get("from_type"):
        from_type = str(intent.get("from_type"))
    else:
        from_type = facts.context.column_type(facts.state.original(table), column)
    if using:
        match = _TRIVIAL_USING_RE.fullmatch(str(using))
        if not match or match.group("column").lower() != column.lower():
            return _type_rewrite(
                table,
                column,
                Confidence.KNOWN,
                "the USING clause computes a new value for every row",
            )
    if from_type is None:
        return _type_rewrite(
            table,
            column,
            Confidence.LIKELY,
            "the column's current type is not known; a binary-coercible change, "
            "such as widening a varchar, would only change the catalog",
        )
    work, confidence, reason = type_change(from_type, to_type, facts.context.settings)
    if work is Work.CATALOG:
        return Outcome(
            (Effect(ALTER_TYPE_COERCIBLE, table, ACCESS_EXCLUSIVE, Work.CATALOG),)
        )
    return _type_rewrite(table, column, confidence, reason)


def _type_keys(facts: Facts, action: Action) -> List[Effect]:
    """
    A column type change re-creates each foreign key that uses the
    column or points at it. The table at the key's other end is locked,
    and when that table holds the key, its rows are checked again
    unless the old and new types compare the same way.
    """
    table = common.table(facts)
    live = facts.state.original(table)
    column = [action.column] if action.column else []
    effects = [
        foreign_key_effect(table, other, "re-created")
        for other in facts.context.references(live, column)
    ]
    effects.extend(
        foreign_key_effect(
            other, table, "re-created", other, Work.SCAN, Confidence.LIKELY
        )
        for other in facts.context.referenced_by(live, column)
    )
    return effects


def _type_rewrite(
    table: str, column: str, confidence: Confidence, reason: str
) -> Outcome:
    return Outcome(
        (
            Effect(
                ALTER_TYPE,
                table,
                ACCESS_EXCLUSIVE,
                Work.REWRITE,
                confidence,
                message=f"{reason}, so {table} and its indexes are rewritten while "
                "reads and writes wait; the online route takes four steps: add a "
                f"new column, write to both, backfill it, and swap it for {column}",
            ),
        ),
        confidence=confidence,
    )
