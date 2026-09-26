"""
The statements autogenerate() writes with online=True on PostgreSQL,
where the diff splits into two migrations.

The first migration runs in one transaction and changes only the
catalog: a new column goes in nullable, without its UNIQUE or
REFERENCES clause, and each new foreign key and check goes in NOT
VALID. The second, `<id>_online`, runs with transactional=False, so
each of its statements commits on its own and no lock outlasts its
statement. It runs its statements in the order of `ONLINE_GROUPS`:

- `backfill`: the UPDATE that fills a column's NULLs
- `index`: CREATE INDEX CONCURRENTLY, and a new column's UNIQUE as a
  unique index built concurrently then attached with ADD CONSTRAINT ...
  USING INDEX
- `constraint`: a foreign key NOT VALID whose target key the `index`
  group builds, which cannot go in before the key exists
- `validate`: VALIDATE CONSTRAINT for each constraint added NOT VALID
- `not_null`: SET NOT NULL through a check, which the check lets skip
  its scan: ADD CONSTRAINT ... CHECK (c IS NOT NULL) NOT VALID,
  VALIDATE CONSTRAINT, SET NOT NULL, and DROP CONSTRAINT
- `drop`: the drops allow_drops generates, with DROP INDEX CONCURRENTLY
  for an index

The second migration's down step undoes its groups in the reverse
order, so that down() leaves the schema the first migration made.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

from sustained.analysis import MigrationStatement, with_intent

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler

ONLINE_GROUPS = ("backfill", "index", "constraint", "validate", "not_null", "drop")

# PostgreSQL cuts identifiers to 63 bytes.
_NAME_LIMIT = 63


def online_id(migration_id: str) -> str:
    """The id of the migration that runs outside the DDL transaction."""
    return f"{migration_id}_online"


def constraint_name(table: str, column: str, suffix: str) -> str:
    """
    The name PostgreSQL gives a column's constraint, such as
    `orders_code_key` for UNIQUE and `orders_customer_id_fkey` for
    REFERENCES, so the schema reads the same as after the direct form.
    """
    return f"{table}_{column}_{suffix}"[:_NAME_LIMIT]


def concurrently(statement: str) -> MigrationStatement:
    """
    A CREATE INDEX or DROP INDEX statement with CONCURRENTLY after
    INDEX, keeping the intent and marks the statement carries.
    """
    if not statement.startswith(
        ("CREATE INDEX ", "CREATE UNIQUE INDEX ", "DROP INDEX ")
    ):
        raise ValueError(f"Not a CREATE INDEX or DROP INDEX statement: {statement}")
    return _same(statement, statement.replace("INDEX ", "INDEX CONCURRENTLY ", 1))


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


def not_null_route(
    compiler: "Compiler",
    table_sql: str,
    table: str,
    bare_table: str,
    column: str,
    set_not_null: List[str],
) -> List[MigrationStatement]:
    """
    SET NOT NULL through a check: the check goes in NOT VALID, which
    reads no rows, VALIDATE CONSTRAINT reads them under a lock that lets
    reads and writes go on, SET NOT NULL then reads the valid check
    instead of the rows, and the check is dropped. The drop's intent has
    `transient=True`, so destructive_statements() does not label it: the
    check existed only for this route. `set_not_null` is the
    compiler's SET NOT NULL statement for the column.
    """
    check = constraint_name(bare_table, column, "not_null_check")
    check_sql = compiler.quote_ddl_identifier(check)
    column_sql = compiler.quote_ddl_identifier(column)
    return [
        with_intent(
            f"ALTER TABLE {table_sql} ADD CONSTRAINT {check_sql} "
            f"CHECK ({column_sql} IS NOT NULL) NOT VALID",
            "add_check",
            table,
            name=check,
        ),
        validate(compiler, table_sql, table, check),
        *(
            with_intent(statement, "set_not_null", table, column)
            for statement in set_not_null
        ),
        with_intent(
            compiler.compile_drop_constraint(table_sql, check),
            "drop_constraint",
            table,
            name=check,
            transient=True,
        ),
    ]


def _same(statement: str, text: str) -> MigrationStatement:
    """The new text with what the old statement carries."""
    if not isinstance(statement, MigrationStatement):
        return MigrationStatement(text)
    return MigrationStatement(
        text,
        statement.migration_id,
        statement.transactional,
        destructive=statement.destructive,
        intent=statement.intent,
    )
