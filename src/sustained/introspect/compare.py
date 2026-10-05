"""
Comparing two schema reads. A rehearsal uses it to check that the down
steps put the schema back where it started.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Tuple

from sustained.dialects import Dialects
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
)
from sustained.introspect.normalize import (
    normalize_check,
    normalize_predicate,
    normalize_type,
    type_params,
)


def _column_parts(column: IntrospectedColumn) -> Tuple[str, Optional[str], bool, bool]:
    """
    One column reduced to the parts two snapshots are compared on: the
    logical type, its parameters, nullability, and key membership. The
    default is left out, since engines report a default they generated
    themselves in spellings that differ between an original column and a
    rebuilt one.
    """
    return (
        normalize_type(column.raw_type),
        type_params(column.raw_type),
        column.nullable,
        column.primary_key,
    )


# The dialects whose index reads give back the same index after a
# rebuild from the read, on the servers tests/integration/round_trip.py
# runs on. Dialects.MYSQL covers MySQL and MariaDB. On another dialect a
# read may spell an index differently after a rebuild, so diff_snapshots()
# does not compare indexes there.
INDEX_DIALECTS: FrozenSet[Dialects] = frozenset(
    {Dialects.POSTGRES, Dialects.MYSQL, Dialects.MSSQL, Dialects.DEFAULT}
)

# The dialects whose check, UNIQUE constraint, and foreign key reads give
# back the same constraints after a rebuild from the read, in the same
# round-trip test. MySQL and MariaDB report a UNIQUE constraint as a
# unique index, which the index comparison covers.
CONSTRAINT_DIALECTS: FrozenSet[Dialects] = frozenset(
    {Dialects.POSTGRES, Dialects.MYSQL, Dialects.MSSQL}
)

IndexParts = Tuple[
    Tuple[str, ...], bool, Tuple[bool, ...], Tuple[Optional[int], ...], Optional[str]
]


def index_parts(index: IntrospectedIndex) -> IndexParts:
    """
    One index reduced to the parts two snapshots are compared on: the
    key columns, uniqueness, the direction and prefix length of each key
    part, and the predicate as the catalog spells it.
    """
    return (
        index.columns,
        index.unique,
        index.descending,
        index.prefix_lengths,
        index.where,
    )


class _Described(NamedTuple):
    """
    One object of a snapshot: `key` is what two reads are compared on,
    and `text` is the phrase a difference report shows.
    """

    key: str
    text: str


def _plain(text: str) -> _Described:
    """An object compared on its description as it stands."""
    return _Described(text, text)


def _describe_index(index: IntrospectedIndex, where: Optional[str]) -> str:
    """An index definition in one readable phrase, with `where` as its predicate."""
    # A read without details reports no directions or prefix lengths.
    count = len(index.columns)
    parts = [
        column + (f"({prefix})" if prefix else "") + (" DESC" if desc else "")
        for column, desc, prefix in zip(
            index.columns,
            index.descending or (False,) * count,
            index.prefix_lengths or (None,) * count,
        )
    ]
    text = ("UNIQUE " if index.unique else "") + f"({', '.join(parts)})"
    return text + (f" WHERE {where}" if where else "")


def _describe_foreign_key(fk: IntrospectedForeignKey) -> str:
    """A foreign key definition in one readable phrase."""
    target = ".".join(part for part in (fk.target_schema, fk.target_table) if part)
    actions = "".join(
        f" ON {event} {action}"
        for event, action in (("DELETE", fk.on_delete), ("UPDATE", fk.on_update))
        if action
    )
    return (
        f"({', '.join(fk.columns)}) REFERENCES {target} "
        f"({', '.join(fk.target_columns)}){actions}"
    )


def _constraints(table: IntrospectedTable) -> Dict[str, Dict[str, _Described]]:
    """
    The constraints of a table by kind, each described in one phrase:
    each check's expression as the catalog spells it, the columns of each
    UNIQUE constraint, and each foreign key. A check compares on its
    normalize_check() form, because an engine can store an expression in
    a new spelling when a rename touches a column it names.
    """
    return {
        "check": {
            name: _Described(normalize_check(expression), expression)
            for name, expression in table.checks.items()
        },
        "unique constraint": {
            name: _plain(f"UNIQUE ({', '.join(index.columns)})")
            for name, index in table.indexes.items()
            if index.constraint
        },
        "foreign key": {
            name: _plain(_describe_foreign_key(fk))
            for name, fk in table.foreign_keys.items()
        },
    }


def _diff_named(
    kind: str,
    table: str,
    old: Mapping[str, _Described],
    new: Mapping[str, _Described],
) -> List[str]:
    """
    One line per object of one kind on `table` that differs between two
    reads, given each object's description by name.
    """
    lines = [
        f"{kind} '{table}.{name}' left behind" for name in sorted(set(new) - set(old))
    ]
    lines += [
        f"{kind} '{table}.{name}' missing" for name in sorted(set(old) - set(new))
    ]
    lines += [
        f"{kind} '{table}.{name}' changed: {old[name].text} became {new[name].text}"
        for name in sorted(set(old) & set(new))
        if old[name].key != new[name].key
    ]
    return lines


def _indexes(table: IntrospectedTable) -> Dict[str, _Described]:
    """
    The indexes no constraint owns, each described in one phrase. The
    predicate compares on its normalize_predicate() form. SQLite rewrites
    the identifiers in a stored CREATE INDEX on RENAME COLUMN, so a rename
    and its inverse leave WHERE NAME IS NOT NULL spelled
    WHERE "name" IS NOT NULL.
    """
    return {
        name: _Described(
            _describe_index(
                index, normalize_predicate(index.where) if index.where else None
            ),
            _describe_index(index, index.where),
        )
        for name, index in table.indexes.items()
        if not index.constraint
    }


def _describe_column(column: IntrospectedColumn) -> str:
    """A column definition in one readable phrase."""
    text = (column.raw_type or "?").upper()
    if not column.nullable:
        text += " NOT NULL"
    if column.primary_key:
        text += " PRIMARY KEY"
    return text


def diff_snapshots(
    before: Dict[str, IntrospectedTable],
    after: Dict[str, IntrospectedTable],
    dialect: Optional[Dialects] = None,
) -> List[str]:
    """
    Compares two introspected schemas and returns one line per difference,
    empty when they match. A rehearsal uses it to check that the down
    steps put the schema back where it started: `before` is the snapshot
    taken first, `after` is the schema once the down steps have run.

    Tables and columns are compared. Indexes are compared when `dialect`
    is in INDEX_DIALECTS: the key columns, uniqueness, the direction and
    prefix length of each key part, and the predicate in its
    normalize_predicate() form. An index behind a
    constraint is not compared as an index. Checks, in their
    normalize_check() form, UNIQUE constraints, and foreign keys are
    compared when `dialect` is in
    CONSTRAINT_DIALECTS. Defaults and comments are not compared, because
    engines report them in spellings that differ between an original
    object and a rebuilt one.
    """
    lines: List[str] = []
    for table in sorted(set(after) - set(before)):
        lines.append(f"table '{table}' left behind")
    for table in sorted(set(before) - set(after)):
        lines.append(f"table '{table}' missing")
    for table in sorted(set(before) & set(after)):
        old, new = before[table].columns, after[table].columns
        for column in sorted(set(new) - set(old)):
            lines.append(f"column '{table}.{column}' left behind")
        for column in sorted(set(old) - set(new)):
            lines.append(f"column '{table}.{column}' missing")
        for column in sorted(set(old) & set(new)):
            if _column_parts(old[column]) != _column_parts(new[column]):
                lines.append(
                    f"column '{table}.{column}' changed: "
                    f"{_describe_column(old[column])} became "
                    f"{_describe_column(new[column])}"
                )
        if dialect in INDEX_DIALECTS:
            lines += _diff_named(
                "index", table, _indexes(before[table]), _indexes(after[table])
            )
        if dialect in CONSTRAINT_DIALECTS:
            old_kinds = _constraints(before[table])
            new_kinds = _constraints(after[table])
            for kind in old_kinds:
                lines += _diff_named(kind, table, old_kinds[kind], new_kinds[kind])
    return lines
