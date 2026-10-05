from typing import TYPE_CHECKING, Optional

from sustained.exceptions import DialectError

from .base import Compiler

if TYPE_CHECKING:
    from sustained.schema import ColumnDef, ColumnState


class DuckDbCompiler(Compiler):
    """
    Compiler for DuckDB. DuckDB follows Postgres syntax closely: double
    quoted identifiers, native ILIKE, RETURNING, ON CONFLICT upserts, and
    CREATE TABLE AS. Its Python driver uses qmark placeholders, which the
    base compiler already emits.
    """

    _IDENT_QUOTES = ('"', '"')
    _native_ilike = True
    _distinct_on = True
    # DuckDB takes a bare OFFSET and rejects a negative LIMIT.
    _bare_offset = True
    _comment_on_column = True
    _typed_temporal_literals = True
    _ALTER_TYPE_KEYWORD = "SET DATA TYPE"

    _supports_qualify = True
    _parenthesized_set_members = True
    _supports_alter_column = True
    _keeps_constraint_names = False
    _stores_column_comments = True
    # "Cannot alter entry because there are entries that depend on
    # it": any index on the table stops a change to any column.
    _alter_column_index_scope = "table"
    _index_drop_waits_for_commit = True
    _enum_strategy = "native"
    # The duckdb driver autocommits every statement and gives every
    # cursor its own session, so transaction() runs BEGIN, COMMIT, and
    # ROLLBACK itself on the one cursor the block shares.
    _driver_transaction_control = False

    def compile_binary_literal(self, hex_text: str) -> str:
        # DuckDB reads X'...' as a string, not as bytes.
        return f"from_hex('{hex_text}')"

    def normalize_diff_type(self, type_name: str) -> str:
        # DuckDB stores TEXT as VARCHAR and reports it back as VARCHAR, so
        # a Text() column would diff as changed on every plan.
        if type_name == "TEXT":
            return "VARCHAR"
        return type_name

    def supports_add_constraint(self) -> bool:
        # DuckDB takes no ALTER TABLE ADD CONSTRAINT; a constraint has to
        # be part of the CREATE TABLE statement.
        return False

    def compile_backfill(
        self,
        table_sql: str,
        column_name: str,
        type_sql: str,
        filler_sql: str,
    ) -> "list[str]":
        # An UPDATE followed by SET NOT NULL in the same transaction fails
        # with "Cannot create index with outstanding updates". Rewriting
        # the column through USING fills the NULLs without an UPDATE.
        column_sql = self.quote_identifier(column_name)
        return [
            f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} SET DATA "
            f"TYPE {type_sql} USING coalesce({column_sql}, {filler_sql})"
        ]

    def compile_identity(self) -> str:
        raise self._unsupported(
            "identity columns", "Use a sequence with a DEFAULT expression instead."
        )

    def savepoint_sql(self, name: str) -> Optional[str]:
        # DuckDB has transactions but no savepoints, so transaction()
        # cannot nest on it.
        return None

    def rollback_savepoint_sql(self, name: str) -> Optional[str]:
        return None

    def release_savepoint_sql(self, name: str) -> Optional[str]:
        return None

    def compile_add_enum_value(self, name: str, value: str) -> str:
        raise DialectError(
            "DuckDB cannot add a value to an enum type in place. Create a "
            "new type, cast the column with ALTER COLUMN ... SET DATA TYPE, "
            "then drop the old type."
        )
