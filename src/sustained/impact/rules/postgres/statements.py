"""
The handlers for each PostgreSQL statement kind other than ALTER TABLE.
"""

from __future__ import annotations

from typing import Optional

from sustained.impact.model import (
    Blocks,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, common
from sustained.impact.rules.postgres.catalog import (
    COMMENT,
    CREATE_INDEX,
    CREATE_INDEX_CONCURRENTLY,
    CREATE_TABLE,
    DROP_FOREIGN_KEY,
    DROP_INDEX,
    DROP_INDEX_CONCURRENTLY,
    DROP_SCHEMA,
    DROP_TABLE,
    DROP_VIEW,
    INSERT_ROWS,
    LOCK_TABLE,
    REFRESH,
    REFRESH_CONCURRENTLY,
    REINDEX,
    REINDEX_CONCURRENTLY,
    TRIGGER,
    VACUUM,
    VACUUM_FULL,
    WRITE_ROWS,
)
from sustained.impact.rules.postgres.locks import (
    ACCESS_EXCLUSIVE,
    EXCLUSIVE,
    ROW_EXCLUSIVE,
    SHARE,
    SHARE_ROW_EXCLUSIVE,
    SHARE_UPDATE_EXCLUSIVE,
)
from sustained.impact.rules.postgres.remedies import (
    TRANSACTION_NOTE,
    insert_after,
    trimmed,
)


def _table_label(index: str, table: Optional[str]) -> str:
    return table if table else f"(table of index {index})"


def _create_index(facts: Facts) -> Outcome:
    parsed = facts.parsed
    table = common.table(facts)
    if parsed.options.get("concurrently"):
        return Outcome(
            (
                Effect(
                    CREATE_INDEX_CONCURRENTLY,
                    table,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.INDEX_BUILD,
                ),
            ),
            (
                Finding(
                    CREATE_INDEX_CONCURRENTLY.id,
                    Severity.INFO,
                    "the build scans the table twice and waits for every "
                    "older transaction; a failed build leaves an invalid "
                    "index behind, which must be dropped before a retry",
                    source=CREATE_INDEX_CONCURRENTLY.source,
                ),
            ),
        )
    concurrent = insert_after(facts.statement, "INDEX", "CONCURRENTLY")
    remedy = (trimmed(concurrent),) if concurrent else ()
    return Outcome(
        (
            Effect(
                CREATE_INDEX,
                table,
                SHARE,
                Work.INDEX_BUILD,
                message=f"writes to {table} wait for the whole index build; "
                f"build it CONCURRENTLY {TRANSACTION_NOTE}",
                remedy=remedy,
            ),
        )
    )


def _drop_index(facts: Facts) -> Outcome:
    options = facts.parsed.options
    names = [str(n) for n in facts.parsed.items("names")]
    concurrently = bool(options.get("concurrently"))
    effects = []
    for name in names:
        table = facts.state.index_table(name) or facts.context.index_table(name)
        if table is None and facts.intent is not None and len(names) == 1:
            table = facts.intent.table
        label = _table_label(name, table)
        if concurrently:
            effects.append(
                Effect(
                    DROP_INDEX_CONCURRENTLY,
                    label,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.CATALOG,
                )
            )
            continue
        concurrent = (
            insert_after(facts.statement, "INDEX", "CONCURRENTLY")
            if len(names) == 1
            else None
        )
        effects.append(
            Effect(
                DROP_INDEX,
                label,
                ACCESS_EXCLUSIVE,
                Work.CATALOG,
                message=f"reads and writes on {label} wait until the drop commits; "
                f"drop it CONCURRENTLY {TRANSACTION_NOTE}",
                remedy=(trimmed(concurrent),) if concurrent else (),
            )
        )
    return Outcome(tuple(effects))


def _create_table(facts: Facts) -> Outcome:
    options = facts.parsed.options
    effects = [
        Effect(
            CREATE_TABLE,
            str(referenced),
            SHARE_ROW_EXCLUSIVE,
            Work.CATALOG,
            message=f"writes to {referenced} wait while the new table's "
            "foreign key is created",
        )
        for referenced in facts.parsed.items("references")
    ]
    parent = options.get("partition_of")
    if parent:
        effects.append(
            Effect(
                CREATE_TABLE,
                str(parent),
                ACCESS_EXCLUSIVE,
                Work.CATALOG,
                message=f"reads and writes on {parent} wait while the partition "
                "is created",
            )
        )
    return Outcome(tuple(effects))


def _drop_table(facts: Facts) -> Outcome:
    """
    DROP TABLE and TRUNCATE lock each named table. A dropped table's
    foreign keys go with it, and so does the lock on each table they
    point at. With CASCADE, DROP also drops the keys that point at the
    table, and TRUNCATE also empties the tables those keys belong to,
    and the tables that point at those in turn.
    """
    named = common.tables(facts)
    effects = [
        Effect(DROP_TABLE, table, ACCESS_EXCLUSIVE, Work.CATALOG) for table in named
    ]
    seen = {table.lower() for table in named}
    context = facts.context
    cascade = bool(facts.parsed.options.get("cascade"))
    if facts.parsed.kind == "truncate":
        pending = list(named) if cascade else []
        while pending:
            table = pending.pop(0)
            for other in context.referenced_by(facts.state.original(table)):
                if other.lower() in seen:
                    continue
                seen.add(other.lower())
                pending.append(other)
                effects.append(
                    Effect(
                        DROP_TABLE,
                        other,
                        ACCESS_EXCLUSIVE,
                        Work.CATALOG,
                        message=f"TRUNCATE ... CASCADE also empties {other}, whose "
                        f"foreign key points at {table}; reads and writes on "
                        f"{other} wait until it commits",
                    )
                )
        return Outcome(tuple(effects))
    for table in named:
        live = facts.state.original(table)
        for other in context.references(live):
            if other.lower() not in seen:
                seen.add(other.lower())
                effects.append(foreign_key_effect(table, other, "dropped"))
        if cascade:
            for other in context.referenced_by(live):
                if other.lower() not in seen:
                    seen.add(other.lower())
                    effects.append(foreign_key_effect(other, table, "dropped", other))
    return Outcome(tuple(effects))


def foreign_key_effect(
    source: str,
    target: str,
    change: str,
    locked: Optional[str] = None,
    work: Work = Work.CATALOG,
    confidence: Confidence = Confidence.KNOWN,
) -> Effect:
    """
    The lock on one end of a foreign key from `source` to `target` that
    the statement drops or re-creates: on `target` unless `locked` names
    the other end. Postgres takes ACCESS EXCLUSIVE on both tables of a
    key it drops, to remove the key's triggers from each.
    """
    table = locked or target
    return Effect(
        DROP_FOREIGN_KEY,
        table,
        ACCESS_EXCLUSIVE,
        work,
        confidence,
        message=f"the foreign key from {source} to {target} is {change}, which "
        f"locks {table} ACCESS EXCLUSIVE: reads and writes on {table} wait until "
        "the statement commits",
    )


def _drop_view(facts: Facts) -> Outcome:
    return Outcome(
        tuple(
            Effect(DROP_VIEW, table, ACCESS_EXCLUSIVE, Work.CATALOG)
            for table in common.tables(facts)
        )
    )


def _write_rows(facts: Facts) -> Outcome:
    table = common.table(facts)
    message = common.row_write_message(facts, table)
    return Outcome(
        (
            Effect(
                WRITE_ROWS,
                table,
                ROW_EXCLUSIVE,
                Work.ROWS,
                message=message,
                blocks=Blocks.WRITES,
            ),
        )
    )


def _insert(facts: Facts) -> Outcome:
    table = common.table(facts)
    return Outcome((Effect(INSERT_ROWS, table, ROW_EXCLUSIVE, Work.ROWS),))


def _reindex(facts: Facts) -> Outcome:
    options = facts.parsed.options
    target = str(options.get("target"))
    name = str(options.get("name"))
    if target == "table":
        label = name
    elif target == "index":
        table = facts.state.index_table(name) or facts.context.index_table(name)
        label = _table_label(name, table)
    else:
        label = f"(every table in {target} {name})"
    if options.get("concurrently"):
        return Outcome(
            (
                Effect(
                    REINDEX_CONCURRENTLY,
                    label,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.INDEX_BUILD,
                ),
            )
        )
    concurrent = insert_after(facts.statement, target.upper(), "CONCURRENTLY")
    return Outcome(
        (
            Effect(
                REINDEX,
                label,
                SHARE,
                Work.INDEX_BUILD,
                message=f"writes to {label} wait for the rebuild, and so do reads "
                "that would use an index being rebuilt, which is locked ACCESS "
                f"EXCLUSIVE; rebuild it CONCURRENTLY {TRANSACTION_NOTE}",
                remedy=(trimmed(concurrent),) if concurrent else (),
                blocks=Blocks.READS_AND_WRITES,
            ),
        )
    )


def _vacuum(facts: Facts) -> Outcome:
    tables = common.tables(facts)
    if not tables:
        return Outcome(
            findings=(
                Finding(
                    VACUUM.id,
                    Severity.INFO,
                    f"{facts.parsed.kind.upper()} with no table reads every table "
                    "in the database",
                    source=VACUUM.source,
                ),
            ),
            confidence=Confidence.LIKELY,
        )
    if facts.parsed.options.get("full"):
        return Outcome(
            tuple(
                Effect(VACUUM_FULL, table, ACCESS_EXCLUSIVE, Work.REWRITE)
                for table in tables
            )
        )
    return Outcome(
        tuple(
            Effect(VACUUM, table, SHARE_UPDATE_EXCLUSIVE, Work.SCAN) for table in tables
        )
    )


def _cluster(facts: Facts) -> Outcome:
    if facts.parsed.table is None:
        return _vacuum(facts)
    return Outcome(
        (Effect(VACUUM_FULL, facts.parsed.table, ACCESS_EXCLUSIVE, Work.REWRITE),)
    )


def _refresh(facts: Facts) -> Outcome:
    view = facts.parsed.table or "(unnamed view)"
    options = facts.parsed.options
    work = Work.REWRITE if options.get("with_data", True) else Work.CATALOG
    if options.get("concurrently"):
        # The query runs in full, and only the rows that differ are
        # written into the view, whose file stays in place.
        return Outcome((Effect(REFRESH_CONCURRENTLY, view, EXCLUSIVE, Work.ROWS),))
    concurrent = insert_after(facts.statement, "VIEW", "CONCURRENTLY")
    return Outcome(
        (
            Effect(
                REFRESH,
                view,
                ACCESS_EXCLUSIVE,
                work,
                message=f"reads of {view} wait for the whole refresh; refresh it "
                "CONCURRENTLY, which needs a unique index on the view",
                remedy=(trimmed(concurrent),) if concurrent else (),
            ),
        )
    )


def _trigger(facts: Facts) -> Outcome:
    table = common.table(facts)
    lock = (
        SHARE_ROW_EXCLUSIVE
        if facts.parsed.kind == "create_trigger"
        else ACCESS_EXCLUSIVE
    )
    return Outcome((Effect(TRIGGER, table, lock, Work.CATALOG),))


def _comment(facts: Facts) -> Outcome:
    if facts.parsed.options.get("object") not in ("table", "column"):
        return Outcome()
    table = common.table(facts)
    return Outcome((Effect(COMMENT, table, SHARE_UPDATE_EXCLUSIVE, Work.CATALOG),))


def _lock_table(facts: Facts) -> Outcome:
    options = facts.parsed.options
    mode = str(options.get("mode"))
    waits = not options.get("nowait")
    return Outcome(
        tuple(
            Effect(LOCK_TABLE, table, mode, Work.CATALOG, waits=waits)
            for table in common.tables(facts)
        )
    )


def _drop_object(facts: Facts) -> Outcome:
    if facts.parsed.options.get("object") != "schema":
        return Outcome()
    return Outcome(
        findings=(
            Finding(
                DROP_SCHEMA.id,
                Severity.INFO,
                "dropping a schema locks every table in it ACCESS EXCLUSIVE; "
                "the tables are not named in the statement",
                source=DROP_SCHEMA.source,
            ),
        ),
        confidence=Confidence.LIKELY,
    )
