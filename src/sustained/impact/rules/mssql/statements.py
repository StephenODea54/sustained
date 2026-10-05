"""
The handlers for each SQL Server statement kind other than ALTER TABLE.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from sustained.impact.model import Confidence, Finding, Severity, Work
from sustained.impact.rules import Effect, Facts, Outcome, common
from sustained.impact.rules.mssql.alter import alter_table
from sustained.impact.rules.mssql.catalog import (
    CREATE_CLUSTERED_INDEX,
    CREATE_INDEX,
    CREATE_INDEX_ONLINE,
    DISABLE_INDEX,
    DROP_CLUSTERED_INDEX,
    DROP_INDEX,
    DROP_TABLE,
    LOCK_ESCALATION,
    REBUILD,
    RENAME,
    REORGANIZE,
    RESUMABLE,
    SCHEMA_CHANGE,
    TRIGGER,
    TRUNCATE,
    UPDATE_STATISTICS,
    WRITE_ROWS,
)
from sustained.impact.rules.mssql.facts import (
    ESCALATION_LOCKS,
    LOW_PRIORITY,
    escalation,
    exclusive_blocks,
    is_online,
    online_effect,
    online_findings,
    resumable_findings,
    with_options,
)
from sustained.impact.rules.mssql.locks import IX, SCH_M, SCH_S, S, X, enterprise


def _online_remedy(facts: Facts, statement: str, resumable: bool) -> Tuple[str, ...]:
    """
    The statement with ONLINE = ON, where the edition may have it, and
    RESUMABLE = ON on a version that resumes it. A resumable operation
    refuses a transaction, so it goes with transactional=False.
    """
    if enterprise(facts.context) is False:
        return ()
    options = "ONLINE = ON"
    if resumable:
        options += ", RESUMABLE = ON"
    return (f"{statement} WITH ({options})",)


def _create_index(facts: Facts) -> Outcome:
    """
    A nonclustered index build takes S on the table, so writes wait and
    reads go on. A clustered one takes Sch-M and, on a heap, copies the
    table into the index and builds its other indexes again. With
    ONLINE = ON the build takes Sch-S, and takes S, or Sch-M for a
    clustered index, when it ends.
    """
    table = common.table(facts)
    options = facts.parsed.options
    clustered = bool(options.get("clustered"))
    rule = CREATE_CLUSTERED_INDEX if clustered else CREATE_INDEX
    lock = SCH_M if clustered else S
    work = Work.REWRITE if clustered else Work.INDEX_BUILD
    refused = resumable_findings(facts, RESUMABLE, options)
    if is_online(options):
        online_rule = CREATE_CLUSTERED_INDEX if clustered else CREATE_INDEX_ONLINE
        effect = online_effect(
            facts, online_rule, table, lock, work, "the index build", options, (16,)
        )
        return Outcome.of(
            effect, findings=online_findings(facts, online_rule) + refused
        )
    who = "reads and writes on" if clustered else "writes to"
    message = f"{who} {table} wait for the whole index build"
    remedy = _online_remedy(
        facts,
        facts.statement,
        not facts.transactional and facts.context.version >= (15,),
    )
    return Outcome.of(
        Effect(rule, table, lock, work, message=message, remedy=remedy),
        findings=refused,
    )


def _drop_index(facts: Facts) -> Outcome:
    """
    DROP INDEX changes the catalog under Sch-M. Dropping a clustered
    index turns the table into a heap, which copies every row.
    """
    effects: List[Effect] = []
    for name in facts.parsed.items("names"):
        index = str(name).rsplit(".", 1)[-1]
        table = facts.parsed.table or common.index_table(facts, index)
        if table is None and facts.intent is not None:
            table = facts.intent.table
        table = common.index_label(index, table)
        stats = facts.context.stats(facts.state.original(table))
        if stats.clustered and stats.clustered.lower() == index.lower():
            effects.append(
                Effect(
                    DROP_CLUSTERED_INDEX,
                    table,
                    SCH_M,
                    Work.REWRITE,
                    message=f"dropping the clustered index copies every row of "
                    f"{table} into a heap",
                )
            )
        else:
            effects.append(Effect(DROP_INDEX, table, SCH_M, Work.CATALOG))
    return Outcome(tuple(effects))


def _alter_index(facts: Facts) -> Outcome:
    table = common.table(facts)
    options = facts.parsed.options
    operation = options.get("operation")
    name = options.get("name")
    stats = facts.context.stats(facts.state.original(table))
    clustered = name is None or (
        stats.clustered is not None and stats.clustered.lower() == str(name).lower()
    )
    if operation == "rebuild":
        return _rebuild_index(facts, table, clustered and stats.heap is not True)
    if operation == "reorganize":
        return _reorganize(facts, table, clustered)
    if operation == "disable":
        notes: Tuple[Finding, ...] = ()
        if clustered and name is not None:
            notes = (
                DISABLE_INDEX.finding(
                    Severity.DANGER,
                    f"disabling the clustered index makes {table} unreadable until "
                    "the index is rebuilt",
                ),
            )
        return Outcome.of(
            Effect(DISABLE_INDEX, table, SCH_M, Work.CATALOG, notes=notes)
        )
    return common.unknown(facts, f"ALTER INDEX ... {str(operation).upper()}")


def _rebuild_index(facts: Facts, table: str, copies: bool) -> Outcome:
    """
    A rebuild takes Sch-M while it builds the index again. Rebuilding
    the clustered index, or ALL, copies the table.
    """
    work = Work.REWRITE if copies else Work.INDEX_BUILD
    options = facts.parsed.options
    refused = resumable_findings(facts, RESUMABLE, options)
    if is_online(options):
        effect = online_effect(
            facts, REBUILD, table, SCH_M, work, "the rebuild", options, (12,)
        )
        return Outcome.of(effect, findings=online_findings(facts, REBUILD) + refused)
    remedy: Tuple[str, ...] = ()
    if enterprise(facts.context) is not False:
        online = "ONLINE = ON"
        if facts.context.version >= (12,):
            online += f" ({LOW_PRIORITY})"
        if not facts.transactional and facts.context.version >= (14,):
            online += ", RESUMABLE = ON"
        if not with_options(options):
            remedy = (f"{facts.statement} WITH ({online})",)
    message = f"reads and writes on {table} wait for the whole rebuild"
    return Outcome.of(
        Effect(REBUILD, table, SCH_M, work, message=message, remedy=remedy),
        findings=refused,
    )


def _reorganize(facts: Facts, table: str, clustered: bool) -> Outcome:
    """
    REORGANIZE reads the index's pages and compacts them in place, taking
    short page locks as it goes. Inside a transaction those locks, and
    the X lock on the table they add up to, are held until the migration
    commits. On the clustered index, or ALL, it moves rows in proportion
    to the fragmentation, up to every row of the table.
    """
    work, confidence = Work.SCAN, Confidence.KNOWN
    if clustered:
        work, confidence = Work.REWRITE, Confidence.LIKELY
    if facts.transactional:
        return Outcome.of(
            Effect(
                REORGANIZE,
                table,
                X,
                work,
                confidence,
                message=f"inside a transaction, the lock REORGANIZE takes on "
                f"{table} is held until the migration commits; run it in a "
                "migration with transactional=False",
                blocks=exclusive_blocks(facts),
            )
        )
    return Outcome.of(Effect(REORGANIZE, table, IX, work, Confidence.LIKELY))


def _rename_table(facts: Facts) -> Outcome:
    table = common.table(facts)
    note = common.rename_note(RENAME, "table", table)
    return Outcome.of(Effect(RENAME, table, SCH_M, Work.CATALOG, notes=(note,)))


def _truncate(facts: Facts) -> Outcome:
    return Outcome(
        tuple(
            Effect(TRUNCATE, table, SCH_M, Work.CATALOG)
            for table in common.tables(facts)
        )
    )


def _drop_table(facts: Facts) -> Outcome:
    """
    DROP TABLE takes Sch-M on the table, and on each table its foreign
    keys point at.
    """
    effects: List[Effect] = []
    for table in common.tables(facts):
        effects.append(Effect(DROP_TABLE, table, SCH_M, Work.CATALOG))
        for target in facts.context.references(facts.state.original(table)):
            if target.lower() != table.lower():
                effects.append(Effect(DROP_TABLE, target, SCH_M, Work.CATALOG))
    return Outcome(tuple(effects))


def _create_table(facts: Facts) -> Outcome:
    """
    A new table locks no other table, except each one its foreign keys
    point at, which takes Sch-M.
    """
    table = common.table(facts)
    # Nobody else can hold a lock on a table that did not exist, so the
    # new table's lock never waits.
    effects = [Effect(SCHEMA_CHANGE, table, SCH_M, Work.CATALOG, waits=False)]
    for target in facts.parsed.items("references"):
        effects.append(Effect(SCHEMA_CHANGE, str(target), SCH_M, Work.CATALOG))
    return Outcome(tuple(effects))


def _trigger(facts: Facts) -> Outcome:
    """CREATE and DROP TRIGGER take Sch-M on the trigger's table."""
    table = facts.parsed.table
    if table is None:
        name = str(facts.parsed.options.get("name"))
        table = _trigger_table(facts, name)
    if table is None:
        return common.unknown(facts, "a trigger whose table the schema read lacks")
    return Outcome.of(Effect(TRIGGER, table, SCH_M, Work.CATALOG))


def _trigger_table(facts: Facts, name: str) -> Optional[str]:
    schema = facts.context.schema
    if schema is None:
        return None
    for key, table in schema.items():
        if any(t.lower() == name.lower() for t in table.triggers):
            return table.name or key
    return None


def _write_rows(facts: Facts) -> Outcome:
    """
    A write takes IX on the table and X on each row it changes, which
    blocks writes to those rows, and reads of them too unless the
    database reads committed rows from row versions. Once one statement
    has 5,000 locks on the table, SQL Server tries to escalate them to X
    on the whole table. An INSERT ... SELECT inserts rows the recognizer
    does not count, so it gets the escalation note; one of 6,000 rows was
    observed to keep IX on SQL Server 2022 and 2025, and one of 6,500 rows
    to escalate.
    """
    table = common.table(facts)
    blocked = exclusive_blocks(facts)
    options = facts.parsed.options
    whole = (
        facts.parsed.kind in ("update", "delete")
        and not options.get("where")
        and not options.get("limited")
    )
    if whole:
        lock, confidence = escalation(facts, table)
        if lock is not None:
            return Outcome.of(
                Effect(
                    LOCK_ESCALATION,
                    table,
                    X,
                    Work.ROWS,
                    confidence,
                    message=common.row_write_message(
                        facts,
                        table,
                        f", since writing every row escalates its locks to X on "
                        f"the whole table once it has {ESCALATION_LOCKS:,}",
                    ),
                    blocks=blocked,
                )
            )
    notes: Tuple[Finding, ...] = ()
    if facts.parsed.kind == "insert":
        if options.get("source") == "select":
            notes = (
                LOCK_ESCALATION.finding(
                    Severity.INFO,
                    f"an INSERT ... SELECT that inserts more than {ESCALATION_LOCKS:,} "
                    f"rows can escalate its locks to X on the whole of {table}",
                ),
            )
    elif not options.get("limited"):
        notes = (
            LOCK_ESCALATION.finding(
                Severity.INFO,
                f"a write that changes {ESCALATION_LOCKS:,} rows or more escalates "
                f"its locks to X on the whole of {table}",
            ),
        )
    return Outcome.of(
        Effect(
            WRITE_ROWS,
            table,
            IX,
            Work.ROWS,
            message=common.row_write_message(facts, table),
            notes=notes,
            blocks=blocked,
        )
    )


def _update_statistics(facts: Facts) -> Outcome:
    """
    UPDATE STATISTICS reads the table, or a sample of it, holding Sch-S,
    which only other schema changes wait for.
    """
    table = common.table(facts)
    return Outcome.of(Effect(UPDATE_STATISTICS, table, SCH_S, Work.SCAN))


STATEMENTS: Dict[str, common.Handler] = {
    "alter_table": alter_table,
    "create_index": _create_index,
    "drop_index": _drop_index,
    "alter_index": _alter_index,
    "rename_table": _rename_table,
    "truncate": _truncate,
    "drop_table": _drop_table,
    "create_table": _create_table,
    "create_trigger": _trigger,
    "drop_trigger": _trigger,
    "update": _write_rows,
    "delete": _write_rows,
    "insert": _write_rows,
    "update_statistics": _update_statistics,
    "create_view": common.nothing,
    "drop_view": common.nothing,
    "create_object": common.nothing,
    "drop_object": common.nothing,
    "set": common.nothing,
}
