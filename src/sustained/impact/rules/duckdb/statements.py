"""
The handlers for each DuckDB statement kind, and the names of the
conflicts each one opens.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from sustained.impact.model import (
    Action,
    Blocks,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.rules import Effect, Facts, LockOrder, Outcome, Rule, common
from sustained.impact.rules.duckdb.catalog import (
    ADD_COLUMN,
    ADD_COLUMN_VOLATILE,
    ALTER_COLUMN,
    ALTER_COLUMN_TYPE,
    ANALYZE,
    COMMENT,
    CREATE_INDEX,
    CREATE_TABLE,
    DROP_COLUMN,
    DROP_INDEX,
    DROP_TABLE,
    RENAME,
    SCHEMA_CHANGE,
    SET_NOT_NULL,
    WRITE_ROWS,
)
from sustained.impact.window import DATABASE

# DuckDB takes no locks. Each name stands for the conflict an
# uncommitted change opens on a table, after the error DuckDB raises in
# the other transaction.
ALTERED_TABLE = "altered table"
CATALOG_ENTRY = "catalog entry"
CHANGED_ROWS = "changed rows"
DROPPED_TABLE = "dropped table"

# Weakest first. Other transactions abort with a conflict error instead
# of waiting: every write and schema change after an ALTER TABLE that
# changes the table's storage (`altered table`), an UPDATE or DELETE of
# the same rows after a row change (`changed rows`), and a schema change
# after any other change to the table's catalog entry (`catalog entry`).
# After a DROP TABLE (`dropped table`) a schema change on the table
# aborts, and a transaction that wrote to the table before the DROP
# fails to commit.
ORDER = LockOrder(
    (
        (CATALOG_ENTRY, Blocks.DDL),
        (CHANGED_ROWS, Blocks.WRITES),
        (ALTERED_TABLE, Blocks.WRITES),
        (DROPPED_TABLE, Blocks.WRITES),
    )
)
blocks = ORDER.blocks
lock_rank = ORDER.rank

# The column constraints DuckDB refuses on ADD COLUMN, as the recognizer
# names them.
_REFUSED_ON_ADD = ("not_null", "check", "references", "unique", "primary_key")


def timeout_statement(transactional: bool) -> str:
    """
    DuckDB has no lock timeout, since a conflicting transaction aborts
    at once; the profile never draws the finding that would offer this.
    """
    return ""


def _until(facts: Facts) -> str:
    return "the migration commits" if facts.transactional else "it ends"


def _altered(facts: Facts, table: str, what: Optional[str] = None) -> str:
    """The finding for an ALTER TABLE that changes the table's storage."""
    text = (
        f"until {_until(facts)}, INSERT, UPDATE, DELETE, and schema changes on "
        f"{table} in other transactions abort with a conflict error instead of "
        f"waiting, and a transaction that wrote to {table} before it fails to "
        "commit; reads go on"
    )
    return f"{what}; {text}" if what else text


def _effect(
    rule: Rule,
    table: str,
    lock: Optional[str],
    work: Work,
    message: Optional[str] = None,
    confidence: Confidence = Confidence.KNOWN,
    notes: Tuple[Finding, ...] = (),
) -> Effect:
    return Effect(rule, table, lock, work, confidence, message=message, notes=notes)


def _add_column(facts: Facts, action: Action) -> Optional[Effect]:
    table = common.table(facts)
    options = action.options
    if options.get("generated") or any(options.get(k) for k in _REFUSED_ON_ADD):
        return None
    if options.get("default_volatility") != "volatile":
        # DuckDB fills the new column in every row group, in time that
        # grows with the rows.
        return _effect(
            ADD_COLUMN, table, ALTERED_TABLE, Work.ROWS, _altered(facts, table)
        )
    reason, confidence = common.volatile_default(options)
    return _effect(
        ADD_COLUMN_VOLATILE,
        table,
        ALTERED_TABLE,
        Work.REWRITE,
        _altered(facts, table, f"{reason}, so DuckDB writes the column for every row"),
        confidence,
    )


def _action(facts: Facts, action: Action) -> Optional[Effect]:
    """One ALTER TABLE action's effect, or None for one no rule reads."""
    table = common.table(facts)
    kind = action.kind
    if kind == "add_column":
        return _add_column(facts, action)
    if kind == "drop_column":
        return _effect(
            DROP_COLUMN, table, ALTERED_TABLE, Work.CATALOG, _altered(facts, table)
        )
    if kind == "alter_column_type":
        what = f"SET DATA TYPE writes every value of {action.column} again"
        return _effect(
            ALTER_COLUMN_TYPE,
            table,
            ALTERED_TABLE,
            Work.REWRITE,
            _altered(facts, table, what),
        )
    if kind == "set_not_null":
        what = f"SET NOT NULL reads every row of {table} to check for NULLs"
        return _effect(
            SET_NOT_NULL, table, ALTERED_TABLE, Work.SCAN, _altered(facts, table, what)
        )
    if kind in ("drop_not_null", "set_default", "drop_default"):
        return _effect(ALTER_COLUMN, table, CATALOG_ENTRY, Work.CATALOG)
    if kind == "rename_column":
        note = common.rename_note(RENAME, "column", str(action.column))
        return _effect(RENAME, table, CATALOG_ENTRY, Work.CATALOG, notes=(note,))
    if kind == "rename_to":
        note = common.rename_note(RENAME, "table", table)
        return _effect(RENAME, table, CATALOG_ENTRY, Work.CATALOG, notes=(note,))
    return None


def _read(facts: Facts, action: Action) -> Optional[Outcome]:
    effect = _action(facts, action)
    return None if effect is None else Outcome.of(effect)


def _alter_table(facts: Facts) -> Outcome:
    return common.each_action(facts, _read)


def _create_index(facts: Facts) -> Outcome:
    table = common.table(facts)
    return Outcome.of(_effect(CREATE_INDEX, table, None, Work.INDEX_BUILD))


def _drop_index(facts: Facts) -> Outcome:
    return Outcome(
        tuple(
            _effect(
                DROP_INDEX, common.index_label(name, table), CATALOG_ENTRY, Work.CATALOG
            )
            for name, table in common.dropped_indexes(facts)
        )
    )


def _comment(facts: Facts) -> Outcome:
    table = facts.parsed.table or DATABASE
    return Outcome.of(_effect(COMMENT, table, CATALOG_ENTRY, Work.CATALOG))


def _create_table(facts: Facts) -> Outcome:
    """
    A new table, which no other transaction sees, and a catalog entry on
    each table its foreign keys reference.
    """
    effects = [_effect(CREATE_TABLE, common.table(facts), None, Work.CATALOG)]
    for target in facts.parsed.items("references"):
        effects.append(_effect(CREATE_TABLE, str(target), CATALOG_ENTRY, Work.CATALOG))
    return Outcome(tuple(effects))


def _schema_change(facts: Facts) -> Outcome:
    table = facts.parsed.table or DATABASE
    return Outcome.of(_effect(SCHEMA_CHANGE, table, None, Work.CATALOG))


def _drop_table(facts: Facts) -> Outcome:
    return Outcome(
        tuple(
            _effect(
                DROP_TABLE,
                table,
                DROPPED_TABLE,
                Work.CATALOG,
                f"until {_until(facts)}, schema changes on {table} in other "
                "transactions abort with a conflict error instead of waiting, and "
                f"a transaction that wrote to {table} before the DROP fails to "
                "commit; reads go on",
            )
            for table in common.tables(facts)
        )
    )


def _write_rows(facts: Facts) -> Outcome:
    kind = facts.parsed.kind
    if kind == "insert":
        return Outcome.of(_effect(WRITE_ROWS, common.table(facts), None, Work.ROWS))
    effects = []
    for table in common.tables(facts):
        advice = "; on a large table, backfill in batches outside the DDL migration"
        if kind == "delete":
            advice = "; on a large table, delete in batches outside the DDL migration"
        if kind == "update":
            what = "the UPDATE changes rows"
            other = "updates the same columns of the same rows"
        elif kind == "delete":
            what = "the DELETE deletes rows"
            other = "deletes the same rows"
        else:
            what = "TRUNCATE deletes every row"
            other = f"deletes a row of {table}"
            advice = ""
        message = (
            f"{what} of {table}; until {_until(facts)}, another transaction that "
            f"{other} aborts with a conflict error instead of waiting{advice}"
        )
        effects.append(_effect(WRITE_ROWS, table, CHANGED_ROWS, Work.ROWS, message))
    return Outcome(tuple(effects))


def _analyze(facts: Facts) -> Outcome:
    tables = common.tables(facts) or [DATABASE]
    return Outcome(tuple(_effect(ANALYZE, table, None, Work.SCAN) for table in tables))


STATEMENTS: Dict[str, common.Handler] = {
    "alter_table": _alter_table,
    "create_index": _create_index,
    "drop_index": _drop_index,
    "comment_on": _comment,
    "create_table": _create_table,
    "create_view": _schema_change,
    "drop_view": _schema_change,
    "create_type": _schema_change,
    "drop_type": _schema_change,
    "create_object": _schema_change,
    "drop_object": _schema_change,
    "drop_table": _drop_table,
    "insert": _write_rows,
    "update": _write_rows,
    "delete": _write_rows,
    "truncate": _write_rows,
    "analyze": _analyze,
    "set": common.nothing,
}
