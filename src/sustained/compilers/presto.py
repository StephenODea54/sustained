from typing import TYPE_CHECKING, Optional, Sequence, Union

from sustained.exceptions import DialectError

from .base import Compiler

if TYPE_CHECKING:
    from sustained.schema import ColumnDef, ColumnState


class PrestoCompiler(Compiler):
    _TYPE_MAP = {**Compiler._TYPE_MAP, "BINARY": "VARBINARY"}

    _IDENT_QUOTES = ('"', '"')
    _comment_on_column = True
    # Trino has no TIMESTAMPTZ keyword. A TIMESTAMP literal with an
    # offset reads as a timestamp with time zone.
    _typed_temporal_literals = True
    _TEMPORAL_KEYWORDS = {"TIMESTAMPTZ": "TIMESTAMP"}

    _parenthesized_set_members = True
    # Presto and Trino query external storage; there are no CHECK or
    # FOREIGN KEY constraints to declare or enforce.
    _supports_constraints = False
    _stores_column_comments = True
    # CREATE TABLE takes the comment inside the column definition.
    _inline_column_comments = True

    def compile_is_boolean(self, column_sql: str, operator: str, value: bool) -> str:
        # Trino has no IS TRUE. IS NOT DISTINCT FROM gives the same answer,
        # a NULL column included.
        distinct = (
            "IS DISTINCT FROM" if operator == "IS NOT" else "IS NOT DISTINCT FROM"
        )
        return f"{column_sql} {distinct} {self.compile_boolean(value)}"

    def validate_column_def(self, column: "ColumnDef") -> None:
        if column.type_name == "ENUM":
            raise DialectError(
                "Presto has no enum types and cannot enforce a value "
                "list. Use String() and validate values in the "
                "application."
            )

    def rebuild_strategy(self) -> str:
        # Presto cannot alter a column, and it cannot run the rebuild
        # either: it has no DROP TABLE and rename swap that keeps the
        # data, and no CREATE INDEX at all.
        return "unsupported"

    def compile_add_check(
        self, table_sql: str, constraint: str, expression: str
    ) -> str:
        raise DialectError(
            f"{self.display_name()} tables have no CHECK "
            "constraints. Validate rows in the application."
        )

    def compile_add_foreign_key(
        self,
        table_sql: str,
        constraint: str,
        column: "Union[str, Sequence[str]]",
        ref_table_sql: str,
        ref_column: "Union[str, Sequence[str]]",
        on_delete: Optional[str] = None,
        on_update: Optional[str] = None,
    ) -> str:
        raise DialectError(
            f"{self.display_name()} tables have no foreign "
            "keys. Enforce the relationship in the application."
        )

    def compile_upsert_statement(
        self,
        table_sql: str,
        column_names: "list[str]",
        row_values_sql: "list[str]",
        conflict_columns: "list[str]",
        action: str,
        update_columns: "list[str]",
    ) -> str:
        raise self._unsupported("upserts")

    def compile_identity(self) -> str:
        raise self._unsupported("identity columns")

    def compile_returning(self, columns_sql: str) -> str:
        raise self._unsupported("RETURNING")

    def compile_limit_offset(
        self,
        limit: Optional[int],
        offset: Optional[int],
        has_order_by: bool = False,
    ) -> str:
        # Presto and Trino require OFFSET before LIMIT.
        parts = []
        if offset is not None:
            parts.append(f"OFFSET {offset}")
        if limit is not None:
            parts.append(f"LIMIT {limit}")
        return " ".join(parts)
