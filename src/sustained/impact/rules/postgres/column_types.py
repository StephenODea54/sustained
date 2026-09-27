"""
Column type changes: which ones PostgreSQL makes in the catalog alone,
which rebuild the indexes on the column, and which rewrite the table
and rebuild all its indexes.
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
    ALTER_TYPE_INDEXES,
)
from sustained.impact.rules.postgres.locks import (
    ACCESS_EXCLUSIVE,
)
from sustained.impact.rules.postgres.partitions import relation
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
# The types in pg_catalog a column can be added with, besides the ones
# above. None of them is a domain, so none checks the rows.
_SYSTEM_TYPES = _BUILTIN_TYPES | frozenset("""
    money inet cidr macaddr macaddr8 xml bit varbit tsvector tsquery jsonpath
    point line lseg box path polygon circle oid regclass regtype regproc
    regprocedure regoper regoperator regnamespace regrole regconfig
    regdictionary name pg_lsn pg_snapshot txid_snapshot int4range int8range
    numrange tsrange tstzrange daterange int4multirange int8multirange
    nummultirange tsmultirange tstzmultirange datemultirange
    """.split()) | frozenset({"bit varying"})
# The types a column takes a collation from, so a change of one of them
# without COLLATE gives the column the default collation.
_COLLATABLE_TYPES = frozenset({"text", "varchar", "char"})
_UTC_ZONES = ("UTC", "ETC/UTC", "GMT", "Z")
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
    known = readable and old_base in _BUILTIN_TYPES and new_base in _BUILTIN_TYPES
    if old_array or new_array:
        if old_array and new_array and old_base == new_base and not new_args:
            return (
                Work.CATALOG,
                Confidence.KNOWN,
                "dropping the length of an array's elements is binary coercible",
            )
        return (
            Work.REWRITE,
            Confidence.KNOWN if known else Confidence.LIKELY,
            f"{from_type} to {to_type} converts each element of the array, "
            "which computes every row",
        )
    if readable:
        verdict = _coercible(
            old_base, old_args or (), new_base, new_args or (), settings
        )
        if verdict is not None:
            return verdict
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
    if {old, new} == {"timestamp", "timestamptz"} and old_args == new_args:
        zone = settings.get("TimeZone")
        if zone is None:
            return (
                Work.REWRITE,
                Confidence.LIKELY,
                f"{old} to {new} only changes the catalog when the TimeZone "
                "setting is UTC, and the setting was not read",
            )
        if zone.upper() in _UTC_ZONES:
            return Work.CATALOG, Confidence.KNOWN, "TimeZone is UTC"
    return None


def domain_check(facts: Facts, type_text: str) -> Optional[Tuple[str, Confidence]]:
    """
    Why a column added with the type rewrites the table, and how sure
    the answer is, or None when the type adds no check. A domain with a
    NOT NULL or a CHECK, of its own or on the domain it is over, is
    checked against every row, which PostgreSQL does by rewriting the
    table. An array of a domain checks nothing.
    """
    base, _, array = _pg_type(type_text)
    if not type_text or array or base in _SYSTEM_TYPES or base.startswith("interval"):
        return None
    if "types" not in facts.context.read:
        return (
            f"no read says whether {type_text} is a domain with a constraint, "
            "which is checked against every row",
            Confidence.LIKELY,
        )
    constrained = facts.context.types.get(base)
    if constrained is None:
        return (
            f"the read did not find the type {type_text}; if it is a domain "
            "with a constraint, the constraint is checked against every row",
            Confidence.LIKELY,
        )
    if constrained:
        return (
            f"{type_text} is a domain with a constraint, which is checked "
            "against every row",
            Confidence.KNOWN,
        )
    return None


def _current_type(facts: Facts, table: str, column: str) -> Optional[str]:
    """
    The column's type before the change, from the schema read. The
    schema read reports an array column as `ARRAY`, so the type of an
    array column comes from the context read, and is not known without
    it.
    """
    found = facts.context.column_type(facts.state.original(table), column)
    if found is None or found.upper() != "ARRAY":
        return found
    known = relation(facts, table)
    return None if known is None else known.arrays.get(column.lower())


def _index_rebuild(
    facts: Facts, table: str, column: str, from_type: str, to_type: str, action: Action
) -> Tuple[Optional[str], Confidence]:
    """
    Why a type change that leaves the rows as they are still rebuilds
    each index on the column, or None when it does not, with how sure
    the answer is. An index is rebuilt when the column's operator class
    or its collation changes. Without the COLLATE clause, a column of a
    type that takes a collation gets the default collation.
    """
    old_base, _, old_array = _pg_type(from_type)
    new_base, _, new_array = _pg_type(to_type)
    if not old_array and {old_base, new_base} == {"timestamp", "timestamptz"}:
        return (
            f"{old_base} and {new_base} sort by different operator classes",
            Confidence.KNOWN,
        )
    collate = action.options.get("collate")
    new = str(collate).rsplit(".", 1)[-1] if collate else None
    if new is None and new_base in _COLLATABLE_TYPES:
        new = "default"
    found = relation(facts, table)
    if "indexes" in facts.context.read:
        if found is None or column.lower() not in found.indexed:
            return None, Confidence.KNOWN
        old = found.indexed[column.lower()]
        if old is not None and new is not None and old != new:
            return f"the collation changes from {old} to {new}", Confidence.KNOWN
        return None, Confidence.KNOWN
    if collate:
        return (
            f"the collation of {column} was not read, and COLLATE {collate} may "
            "change it",
            Confidence.LIKELY,
        )
    return None, Confidence.KNOWN


def _indexed(facts: Facts, table: str, column: str) -> Optional[bool]:
    """
    Whether an index uses the column, or None when no read says. The
    schema read leaves out expression indexes, so it only proves a
    column is indexed.
    """
    if "indexes" in facts.context.read:
        found = relation(facts, table)
        return found is not None and column.lower() in found.indexed
    schema = facts.context.table(facts.state.original(table))
    if schema is None:
        return None
    wanted = column.lower()
    if wanted in (c.lower() for c in schema.primary_key):
        return True
    for index in schema.indexes.values():
        if wanted in (c.lower() for c in index.columns):
            return True
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
        from_type = _current_type(facts, table, column)
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
    if work is not Work.CATALOG:
        return _type_rewrite(table, column, confidence, reason)
    rebuild, confidence = _index_rebuild(
        facts, table, column, from_type, to_type, action
    )
    indexed = _indexed(facts, table, column) if rebuild else False
    if not indexed and indexed is not None:
        return Outcome(
            (Effect(ALTER_TYPE_COERCIBLE, table, ACCESS_EXCLUSIVE, Work.CATALOG),)
        )
    if indexed is None:
        confidence = Confidence.LIKELY
    return Outcome(
        (
            Effect(
                ALTER_TYPE_INDEXES,
                table,
                ACCESS_EXCLUSIVE,
                Work.INDEX_BUILD,
                confidence,
                message=f"the rows stay as they are, but {rebuild}, so each index "
                f"on {column} is rebuilt while reads and writes on {table} wait",
            ),
        ),
        confidence=confidence,
    )


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
