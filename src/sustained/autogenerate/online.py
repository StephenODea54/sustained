"""
The statements autogenerate() writes with online=True on PostgreSQL,
where the diff splits into two migrations.

The first migration runs in one transaction and changes only the
catalog: a new column goes in nullable, without its UNIQUE or
REFERENCES clause, and each new foreign key and check goes in NOT
VALID. A new NOT NULL column whose backfill is a value goes in with
that value as its default, which PostgreSQL stores in the catalog
without writing a row, and the default comes off in the same
migration. The second, `<id>_online`, runs with transactional=False, so
each of its statements commits on its own and no lock outlasts its
statement. It runs its statements in the order of `ONLINE_GROUPS`:

- `backfill`: the UPDATE that fills a column's NULLs
- `index`: CREATE INDEX CONCURRENTLY, and a new column's UNIQUE as a
  unique index built concurrently then attached with ADD CONSTRAINT ...
  USING INDEX. On a partitioned table the index is created with ON ONLY
  on the table, built concurrently on each partition, and attached
- `constraint`: a foreign key NOT VALID whose target key the `index`
  group builds, which cannot go in before the key exists, and a foreign
  key on a partitioned table
- `validate`: VALIDATE CONSTRAINT for each constraint added NOT VALID
- `not_null`: SET NOT NULL through a check, which the check lets skip
  its scan: ADD CONSTRAINT ... CHECK (c IS NOT NULL) NOT VALID, the
  backfill again for the rows written NULL before the check, VALIDATE
  CONSTRAINT, and SET NOT NULL
- `drop`: the drops allow_drops generates, with DROP INDEX CONCURRENTLY
  for an index
- `cleanup`: DROP CONSTRAINT for the checks the `not_null` group added

A failed `<id>_online` runs again from its first statement once
repair() clears its row, so each statement is one that can run again
over what an earlier attempt left: CREATE INDEX CONCURRENTLY IF NOT
EXISTS after DROP INDEX CONCURRENTLY IF EXISTS of the same name, which
drops the invalid index a failed build leaves, the `not_null` check
dropped with IF EXISTS before it goes in, and IF EXISTS on every drop.
ADD CONSTRAINT ... USING INDEX and ADD CONSTRAINT ... FOREIGN KEY have
no IF NOT EXISTS form, so a run again after one of them fails on the
constraint it added.

The second migration's down step undoes its groups in the reverse
order, so that down() leaves the schema the first migration made.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

from sustained.analysis import MigrationStatement, with_intent
from sustained.schema import dotted_name

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.introspect import IntrospectedPartition

ONLINE_GROUPS = (
    "backfill",
    "index",
    "constraint",
    "validate",
    "not_null",
    "drop",
    "cleanup",
)

# PostgreSQL cuts identifiers to 63 bytes: NAMEDATALEN less the byte
# that ends the string.
_NAME_LIMIT = 63

# The start of an ALTER TABLE that drops a constraint or a column, as
# the Postgres compiler writes it, every identifier quoted.
_ALTER_DROP_RE = re.compile(
    r'^(ALTER TABLE (?:"(?:[^"]|"")*"\.)*"(?:[^"]|"")*" DROP (?:CONSTRAINT|COLUMN) )'
)


def online_id(migration_id: str) -> str:
    """The id of the migration that runs outside the DDL transaction."""
    return f"{migration_id}_online"


def object_name(name1: str, name2: str, label: str) -> str:
    """
    The name PostgreSQL's makeObjectName() builds for an index or a
    constraint from a table name, a column part, and a label such as
    `key`, `fkey`, or `idx`. When the three parts and the underscores
    between them are longer than 63 bytes, the longer of the two names
    loses its last byte until they fit, and neither is cut inside a
    character. The label is kept whole, so two labels never give the
    same name.
    """
    first = name1.encode("utf-8")
    second = name2.encode("utf-8")
    room = _NAME_LIMIT - len(label.encode("utf-8")) - (2 if name2 else 1)
    first_bytes, second_bytes = len(first), len(second)
    while first_bytes + second_bytes > room:
        if first_bytes > second_bytes:
            first_bytes -= 1
        else:
            second_bytes -= 1
    parts = [_clipped(first, first_bytes)]
    if name2:
        parts.append(_clipped(second, second_bytes))
    return "_".join(parts + [label])


def _clipped(name: bytes, length: int) -> str:
    """The first `length` bytes of a UTF-8 name, less a cut character."""
    return name[:length].decode("utf-8", "ignore")


def constraint_name(table: str, column: str, suffix: str) -> str:
    """
    The name PostgreSQL gives a column's constraint, such as
    `orders_code_key` for UNIQUE and `orders_customer_id_fkey` for
    REFERENCES, so the schema reads the same as after the direct form
    and a later diff sees no rename. `table` is the bare table name.
    """
    return object_name(table, column, suffix)


def partition_index_name(partition: str, columns: Sequence[str]) -> str:
    """
    The name PostgreSQL gives the index it builds on a partition for an
    index on the partitioned table: the partition, the columns joined
    by underscores, and `idx`.
    """
    return object_name(partition, "_".join(columns), "idx")


def not_null_check_name(table: str, column: str) -> str:
    """The name of the check the SET NOT NULL route adds and drops."""
    return constraint_name(table, column, "not_null_check")


def concurrently(statement: str) -> MigrationStatement:
    """
    A CREATE INDEX or DROP INDEX statement with CONCURRENTLY after
    INDEX, and IF NOT EXISTS or IF EXISTS after that, so the statement
    runs again over the index an earlier attempt left. The intent and
    marks of the statement are kept.
    """
    for start, words in (
        ("CREATE INDEX ", "CONCURRENTLY IF NOT EXISTS "),
        ("CREATE UNIQUE INDEX ", "CONCURRENTLY IF NOT EXISTS "),
        ("DROP INDEX ", "CONCURRENTLY IF EXISTS "),
    ):
        if statement.startswith(start):
            return _same(statement, start + words + statement[len(start) :])
    raise ValueError(f"Not a CREATE INDEX or DROP INDEX statement: {statement}")


def if_exists(statement: str) -> MigrationStatement:
    """
    A drop with IF EXISTS, so it runs again after an earlier attempt
    dropped the object: DROP TABLE, DROP TYPE, DROP INDEX, and ALTER
    TABLE ... DROP CONSTRAINT or DROP COLUMN, as the Postgres compiler
    writes them. The intent and marks of the statement are kept.
    """
    for start in ("DROP TABLE ", "DROP TYPE ", "DROP INDEX "):
        if statement.startswith(start):
            rest = statement[len(start) :]
            if rest.startswith("IF EXISTS "):
                return _same(statement, statement)
            return _same(statement, f"{start}IF EXISTS {rest}")
    match = _ALTER_DROP_RE.match(statement)
    if match is None:
        raise ValueError(f"Not a drop statement: {statement}")
    rest = statement[match.end() :]
    if rest.startswith("IF EXISTS "):
        return _same(statement, statement)
    return _same(statement, f"{match.group(1)}IF EXISTS {rest}")


def rebuilt(drop: str, create: str) -> List[MigrationStatement]:
    """
    The statements that build an index concurrently: a drop of any
    index of the same name, then the build. On the first run no such
    index exists and the drop does nothing. On a run again it drops the
    index an earlier attempt built, which a failed build leaves invalid.
    """
    return [concurrently(drop), concurrently(create)]


def not_valid(statement: str) -> MigrationStatement:
    """An ADD CONSTRAINT statement with NOT VALID, keeping its intent."""
    return _same(statement, f"{statement} NOT VALID")


def validate(
    compiler: "Compiler", table_sql: str, table: str, name: str
) -> MigrationStatement:
    """The VALIDATE CONSTRAINT statement for a constraint added NOT VALID."""
    return with_intent(
        f"ALTER TABLE {table_sql} VALIDATE CONSTRAINT "
        f"{compiler.quote_ddl_identifier(name)}",
        "validate_constraint",
        table,
        name=name,
    )


def unique_using_index(
    compiler: "Compiler", table_sql: str, table: str, name: str
) -> MigrationStatement:
    """ADD CONSTRAINT ... UNIQUE USING INDEX for an index of the same name."""
    name_sql = compiler.quote_ddl_identifier(name)
    return with_intent(
        f"ALTER TABLE {table_sql} ADD CONSTRAINT {name_sql} UNIQUE USING INDEX {name_sql}",
        "add_unique",
        table,
        name=name,
    )


def transient_drop(
    compiler: "Compiler", table_sql: str, table: str, name: str
) -> MigrationStatement:
    """
    DROP CONSTRAINT IF EXISTS for a check the online route adds for its
    own use. Its intent has `transient=True`, so destructive_statements()
    does not label it.
    """
    return if_exists(
        with_intent(
            compiler.compile_drop_constraint(table_sql, name),
            "drop_constraint",
            table,
            name=name,
            transient=True,
        )
    )


def not_null_route(
    compiler: "Compiler",
    table_sql: str,
    table: str,
    bare_table: str,
    column: str,
    backfill: Sequence[str],
    set_not_null: Sequence[str],
) -> Tuple[List[MigrationStatement], List[MigrationStatement]]:
    """
    SET NOT NULL through a check, as the `not_null` group statements and
    the `cleanup` group statement. The check goes in NOT VALID, which
    reads no rows, and from then on no row is written with a NULL in the
    column. `backfill`, the column's backfill, runs again for the rows
    written NULL between the first backfill and the check. VALIDATE
    CONSTRAINT then reads the rows under a lock that lets reads and
    writes go on, and SET NOT NULL reads the valid check instead of the
    rows. The check is dropped last, in the `cleanup` group.

    A drop of the check comes before it goes in, so the group runs again
    after a failed attempt left the check. `set_not_null` is the
    compiler's SET NOT NULL statement for the column.
    """
    check = not_null_check_name(bare_table, column)
    check_sql = compiler.quote_ddl_identifier(check)
    column_sql = compiler.quote_ddl_identifier(column)
    route = [
        transient_drop(compiler, table_sql, table, check),
        with_intent(
            f"ALTER TABLE {table_sql} ADD CONSTRAINT {check_sql} "
            f"CHECK ({column_sql} IS NOT NULL) NOT VALID",
            "add_check",
            table,
            name=check,
        ),
        *(with_intent(s, "backfill", table, column) for s in backfill),
        validate(compiler, table_sql, table, check),
        *(with_intent(s, "set_not_null", table, column) for s in set_not_null),
    ]
    return route, [transient_drop(compiler, table_sql, table, check)]


def partitioned_index(
    compiler: "Compiler",
    table_sql: str,
    table: str,
    name: str,
    columns: Sequence[str],
    unique: bool,
    partitions: Sequence["IntrospectedPartition"],
) -> List[MigrationStatement]:
    """
    The statements that build an index on a partitioned table without
    blocking writes, since PostgreSQL refuses CREATE INDEX CONCURRENTLY
    there. CREATE INDEX ... ON ONLY creates the index on the table alone,
    invalid and empty. Each partition gets its own index built
    concurrently, named as PostgreSQL names it, and ALTER INDEX ...
    ATTACH PARTITION attaches it. The index on the table turns valid
    when an index of every partition is attached. A partition that is
    partitioned in turn gets an index ON ONLY, with its own partitions
    attached to it first.

    Each statement runs again after a failed attempt: the builds take
    IF NOT EXISTS, and ATTACH PARTITION does nothing to an index that
    is already attached. A partition index an attempt left invalid is
    not dropped, because a valid one attached to the table's index
    cannot be, so a run again fails on the attach of the invalid index
    and names it.
    """
    index = compiler.quote_ddl_identifier(name)
    columns_sql = ", ".join(compiler.quote_ddl_identifier(c) for c in columns)
    unique_sql = "UNIQUE " if unique else ""
    statements: List[MigrationStatement] = [
        with_intent(
            f"CREATE {unique_sql}INDEX IF NOT EXISTS {index} ON ONLY {table_sql} "
            f"({columns_sql})",
            "create_index",
            table,
            name=name,
            columns=tuple(columns),
            unique=unique,
        )
    ]
    for partition in partitions:
        child = partition_index_name(partition.name, columns)
        child_sql = _qualified(compiler, partition.schema, partition.name)
        if partition.partitioned:
            statements.extend(
                partitioned_index(
                    compiler,
                    child_sql,
                    _reported(partition),
                    child,
                    columns,
                    unique,
                    partition.partitions,
                )
            )
        else:
            statements.append(
                concurrently(
                    with_intent(
                        compiler.compile_create_index(
                            child, child_sql, list(columns), unique
                        ),
                        "create_index",
                        _reported(partition),
                        name=child,
                        columns=tuple(columns),
                        unique=unique,
                    )
                )
            )
        statements.append(
            with_intent(
                f"ALTER INDEX {_index_sql(compiler, table_sql, name)} "
                f"ATTACH PARTITION "
                f"{_qualified(compiler, partition.schema, child)}",
                "attach_index",
                table,
                name=name,
                partition=_reported(partition),
            )
        )
    return statements


def _qualified(compiler: "Compiler", schema: Optional[str], name: str) -> str:
    """A name with its schema in front, when it has one."""
    quoted = compiler.quote_ddl_identifier(name)
    return (
        quoted
        if schema is None
        else f"{compiler.quote_ddl_identifier(schema)}.{quoted}"
    )


def _reported(partition: "IntrospectedPartition") -> str:
    """A partition as an intent names it: schema.name, or the name alone."""
    return dotted_name(partition.schema, partition.name)


def _index_sql(compiler: "Compiler", table_sql: str, name: str) -> str:
    """An index name with the schema of its table in front of it."""
    from sustained.compilers.base import table_qualifier

    return table_qualifier(table_sql) + compiler.quote_ddl_identifier(name)


def _same(statement: str, text: str) -> MigrationStatement:
    """The new text with the intent and marks of the old statement."""
    if not isinstance(statement, MigrationStatement):
        return MigrationStatement(text)
    return MigrationStatement(
        text,
        statement.migration_id,
        statement.transactional,
        destructive=statement.destructive,
        intent=statement.intent,
    )
