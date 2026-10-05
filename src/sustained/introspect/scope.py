"""
Holding a catalog read to the schemas it covers, and the row helpers
the catalog reads share.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sustained.introspect.model import IntrospectedColumn, IntrospectedForeignKey
from sustained.types import RowValue


def _schema_literal(name: str) -> str:
    """One schema name as a SQL string literal."""
    return "'{}'".format(name.replace("'", "''"))


def _schema_predicate(
    column: str, current_sql: Optional[str], schemas: Tuple[str, ...]
) -> Optional[str]:
    """
    The WHERE fragment that holds `column` to the schemas a read covers:
    the connection's own schema when the engine can name it, plus every
    schema the models declare. None when the engine has no expression for
    the current schema and the caller named none, which leaves the read
    unscoped.

    The declared schemas make their own IN list and the current schema
    is compared beside it with OR. Postgres returns NULL from
    current_schema() when the first search_path entry names a schema that
    does not exist, and then the OR branch matches no rows while the
    declared schemas still match.
    """
    parts: List[str] = []
    if schemas:
        literals: List[str] = []
        for name in schemas:
            literal = _schema_literal(name)
            if literal not in literals:
                literals.append(literal)
        parts.append("{} IN ({})".format(column, ", ".join(literals)))
    if current_sql is not None:
        parts.append(f"{column} = {current_sql}")
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "({})".format(" OR ".join(parts))


def _scoped_filter(column: str, current_sql: str, schemas: Tuple[str, ...]) -> str:
    """
    _schema_predicate() for an engine that can name the schema the
    connection is on, where the predicate is never empty.
    """
    predicate = _schema_predicate(column, current_sql, schemas)
    assert predicate is not None
    return predicate


def _one_schema_per_table(seen: Dict[str, str], table: str, schema: str) -> None:
    """
    Records the schema a table came from, and refuses a second one.

    A snapshot keys on the bare table name. A read covers the schema the
    connection is on plus every schema the models declare, so it can
    return two tables of one name. Their columns would merge into one
    entry, and the merged table matches no model: the diff would report
    columns to add and to drop that are not there. Reading one schema at
    a time is the way out.
    """
    first = seen.setdefault(table, schema)
    if first.lower() != schema.lower():
        raise ValueError(
            f"The schemas '{first}' and '{schema}' both hold a table named "
            f"'{table}'. A schema read keys on the bare table name, so the "
            "two cannot be told apart. Take the declared tableSchema off "
            "the models, or rename one of the tables. A read covers the "
            "schema the connection is on as well as the declared ones, so "
            "it cannot be narrowed past that."
        )


def _declared_schema(schemas: Tuple[str, ...], value: RowValue) -> Optional[str]:
    """
    The schema a row names, when it is one the models declare, or None.
    A table in the connection's own schema keeps None, so a statement
    names it the way the models do.
    """
    if value is None:
        return None
    declared = {name.lower() for name in schemas}
    return str(value) if str(value).lower() in declared else None


def _row_text(row: Sequence[RowValue], index: int) -> Optional[str]:
    """The text a row has at `index`, or None when it has none there."""
    if len(row) <= index or row[index] is None:
        return None
    return str(row[index])


def _is_generated_not_null_check(name: str, expression: str) -> bool:
    """
    Whether a check row is the constraint an engine writes for a NOT NULL
    column. Postgres and DuckDB both report one, named after the column
    with a _not_null suffix. It belongs to the column's own nullable
    flag, not to the table's checks, and a model never declares it.
    """
    return name.endswith("_not_null") and "IS NOT NULL" in expression.upper()


def _add_check(
    checks: Dict[str, Dict[str, str]],
    check_names: Dict[str, Dict[str, str]],
    table: RowValue,
    cname: RowValue,
    expression: str,
) -> Optional[str]:
    """
    Adds one check row to the per-table expressions and spelled names,
    and returns the lowercased check name. A check the engine wrote for
    a NOT NULL column is left out and returns None.
    """
    name = str(cname).lower()
    if _is_generated_not_null_check(name, expression):
        return None
    checks.setdefault(str(table).lower(), {})[name] = expression
    check_names.setdefault(str(table).lower(), {})[name] = str(cname)
    return name


def _foreign_keys(
    rows: Iterable[Sequence[RowValue]],
    action: Callable[[RowValue], Optional[str]],
) -> Dict[str, Dict[str, IntrospectedForeignKey]]:
    """
    The foreign keys in `rows`, by lowercased table and constraint name.
    Each row has the table, the constraint name, one constrained column,
    the table and column it references, the delete and update actions,
    and the schema of the referenced table when it is not the
    connection's own, one row per column in key order. `action` turns
    an engine's action spelling into SQL.
    """
    parts: Dict[Tuple[str, str], List[Sequence[RowValue]]] = {}
    for row in rows:
        parts.setdefault((str(row[0]).lower(), str(row[1]).lower()), []).append(row)
    foreign_keys: Dict[str, Dict[str, IntrospectedForeignKey]] = {}
    for (table, name), key_rows in parts.items():
        first = key_rows[0]
        foreign_keys.setdefault(table, {})[name] = IntrospectedForeignKey(
            columns=tuple(str(r[2]).lower() for r in key_rows),
            target_table=str(first[3]).lower(),
            target_columns=tuple(str(r[4]).lower() for r in key_rows),
            on_delete=action(first[5]),
            on_update=action(first[6]),
            name=str(first[1]),
            target_schema=_row_text(first, 7),
        )
    return foreign_keys


def _apply_comments(
    columns_by_table: Mapping[str, Dict[str, IntrospectedColumn]],
    rows: Iterable[Sequence[RowValue]],
    skip_empty: bool = False,
) -> None:
    """
    Sets the comment of each column that a (table, column, comment) row
    names. A row for a table or column the read did not find is skipped.
    With `skip_empty`, an empty comment counts as no comment.
    """
    for table, name, comment in rows:
        if comment is None or (skip_empty and comment == ""):
            continue
        columns = columns_by_table.get(str(table).lower())
        key = str(name).lower()
        if columns is not None and key in columns:
            columns[key] = columns[key]._replace(comment=str(comment))
