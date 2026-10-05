"""
Comparing two schema reads. A rehearsal uses it to check that the down
steps put the schema back where it started.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple

from sustained.dialects import Dialects
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedIndex,
    IntrospectedTable,
)
from sustained.introspect.normalize import normalize_type, type_params


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


def _plain_indexes(table: IntrospectedTable) -> Mapping[str, IntrospectedIndex]:
    """
    The indexes of a table that no constraint owns. An index behind a
    UNIQUE or PRIMARY KEY constraint belongs to the constraint.
    """
    return {
        name: index for name, index in table.indexes.items() if not index.constraint
    }


def _describe_index(index: IntrospectedIndex) -> str:
    """An index definition in one readable phrase."""
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
    return text + (f" WHERE {index.where}" if index.where else "")


def _diff_indexes(
    table: str,
    old: Mapping[str, IntrospectedIndex],
    new: Mapping[str, IntrospectedIndex],
) -> List[str]:
    """One line per index of `table` that differs between two reads."""
    lines = [
        f"index '{table}.{name}' left behind" for name in sorted(set(new) - set(old))
    ]
    lines += [f"index '{table}.{name}' missing" for name in sorted(set(old) - set(new))]
    for name in sorted(set(old) & set(new)):
        if index_parts(old[name]) != index_parts(new[name]):
            lines.append(
                f"index '{table}.{name}' changed: {_describe_index(old[name])} "
                f"became {_describe_index(new[name])}"
            )
    return lines


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
    prefix length of each key part, and the predicate. An index behind a
    constraint is not compared. Constraints, defaults, and comments are
    not compared, because engines report them in spellings that differ
    between an original object and a rebuilt one.
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
            lines += _diff_indexes(
                table,
                _plain_indexes(before[table]),
                _plain_indexes(after[table]),
            )
    return lines
