"""
The handlers for each PostgreSQL statement kind other than ALTER TABLE.
"""

from __future__ import annotations

from typing import List, Optional, Set

from sustained.impact.model import (
    Blocks,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, Rule, common
from sustained.impact.rules.postgres.catalog import (
    ATTACH_INDEX,
    COMMENT,
    CREATE_INDEX,
    CREATE_INDEX_CONCURRENTLY,
    CREATE_TABLE,
    DROP_FOREIGN_KEY,
    DROP_INDEX,
    DROP_INDEX_CONCURRENTLY,
    DROP_SCHEMA,
    DROP_TABLE,
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
    ACCESS_SHARE,
    EXCLUSIVE,
    ROW_EXCLUSIVE,
    SHARE,
    SHARE_ROW_EXCLUSIVE,
    SHARE_UPDATE_EXCLUSIVE,
)
from sustained.impact.rules.postgres.partitions import (
    cascade,
    default_partition,
    descendants,
    is_unread,
    locked_below,
    parent_of,
    partitioned,
    unread,
)
from sustained.impact.rules.postgres.remedies import (
    TRANSACTION_NOTE,
    insert_after,
    trimmed,
)


def refused(rule: Rule, message: str) -> Finding:
    """The finding for a statement the server refuses to run."""
    return rule.finding(Severity.DANGER, message)


def refused_in_transaction(facts: Facts, rule: Rule, what: str) -> List[Finding]:
    """
    The finding for a statement that cannot run inside a transaction
    block, when its migration runs inside one, or no finding.
    """
    if not facts.transactional:
        return []
    return [
        refused(
            rule,
            f"{what} cannot run inside a transaction block, and this migration "
            "runs inside one, so the server refuses it; run it in a migration "
            "with transactional=False",
        )
    ]


# How to index a partitioned table without blocking writes, since the
# server refuses CREATE INDEX CONCURRENTLY on one.
_PARTITIONED_INDEX = (
    "create the index ON ONLY the partitioned table, build a matching index "
    f"on each partition CONCURRENTLY {TRANSACTION_NOTE}, and attach each one "
    "with ALTER INDEX ... ATTACH PARTITION"
)


def _create_index(facts: Facts) -> Outcome:
    parsed = facts.parsed
    table = common.table(facts)
    parent = partitioned(facts, table)
    if parsed.options.get("concurrently"):
        name = parsed.options.get("name")
        left = "an invalid index" if name is None else f"the invalid index {name}"
        findings = refused_in_transaction(
            facts, CREATE_INDEX_CONCURRENTLY, "CREATE INDEX CONCURRENTLY"
        )
        if parent:
            findings.append(
                refused(
                    CREATE_INDEX_CONCURRENTLY,
                    f"the server refuses CREATE INDEX CONCURRENTLY on {table}, "
                    f"a partitioned table; {_PARTITIONED_INDEX}",
                )
            )
        if not findings:
            findings.append(
                CREATE_INDEX_CONCURRENTLY.finding(
                    Severity.INFO,
                    "the build scans the table twice and waits for every "
                    f"older transaction; a failed build leaves {left} "
                    "behind, which must be dropped before a retry",
                )
            )
        findings.extend(
            unread(
                facts,
                table,
                f"if {table} is a partitioned table, the server refuses CREATE "
                f"INDEX CONCURRENTLY on it; {_PARTITIONED_INDEX}",
            )
        )
        return Outcome.of(
            Effect(
                CREATE_INDEX_CONCURRENTLY,
                table,
                SHARE_UPDATE_EXCLUSIVE,
                Work.INDEX_BUILD,
            ),
            findings=tuple(findings),
        )
    if parent and parsed.options.get("only"):
        # ON ONLY creates an invalid index on the partitioned table
        # alone, which is valid once each partition's index is attached.
        return Outcome.of(Effect(CREATE_INDEX, table, SHARE, Work.CATALOG))
    if parent:
        return Outcome.of(
            Effect(
                CREATE_INDEX,
                table,
                SHARE,
                Work.INDEX_BUILD,
                message=f"writes to {table} and each of its partitions wait "
                f"while the index is built on every partition; "
                f"{_PARTITIONED_INDEX}",
            ),
            *cascade(facts, CREATE_INDEX, table, SHARE, Work.INDEX_BUILD),
        )
    concurrent = insert_after(facts.statement, "INDEX", "CONCURRENTLY")
    remedy = (trimmed(concurrent),) if concurrent else ()
    only = bool(parsed.options.get("only"))
    return Outcome.of(
        Effect(
            CREATE_INDEX,
            table,
            SHARE,
            Work.INDEX_BUILD,
            message=f"writes to {table} wait for the whole index build; "
            f"build it CONCURRENTLY {TRANSACTION_NOTE}",
            remedy=remedy,
        ),
        findings=(
            ()
            if only
            else unread(
                facts,
                table,
                f"{locked_below(table, SHARE)} while the index is built on it, and "
                "the server refuses CREATE INDEX CONCURRENTLY on it",
            )
        ),
    )


def _attach_index(facts: Facts) -> Outcome:
    """
    ALTER INDEX ... ATTACH PARTITION reads the catalog only. It takes
    ACCESS SHARE on the partitioned table and on the partition, SHARE
    UPDATE EXCLUSIVE on the partitioned table's index, and ACCESS
    EXCLUSIVE on the partition's index, which every query and write on
    the partition locks, so those wait until the attach commits.
    """
    name = str(facts.parsed.options.get("name"))
    partition = str(facts.parsed.options.get("partition"))
    intent = facts.intent
    parent_table = common.index_table(facts, name)
    if parent_table is None and intent is not None:
        parent_table = intent.table
    child_table = common.index_table(facts, partition)
    if child_table is None and intent is not None:
        reported = intent.get("partition")
        child_table = None if reported is None else str(reported)
    confidence = Confidence.KNOWN if parent_table and child_table else Confidence.LIKELY
    child_label = common.index_label(partition, child_table)
    return Outcome.of(
        Effect(
            ATTACH_INDEX,
            common.index_label(name, parent_table),
            ACCESS_SHARE,
            Work.CATALOG,
        ),
        Effect(
            ATTACH_INDEX,
            child_label,
            ACCESS_SHARE,
            Work.CATALOG,
            blocks=Blocks.READS_AND_WRITES,
            message=f"reads and writes on {child_label} wait until the "
            f"attach commits, which takes ACCESS EXCLUSIVE on the index "
            f"{partition}",
        ),
        confidence=confidence,
    )


def _drop_index(facts: Facts) -> Outcome:
    options = facts.parsed.options
    concurrently = bool(options.get("concurrently"))
    effects: List[Effect] = []
    findings: List[Finding] = []
    if concurrently:
        findings.extend(
            refused_in_transaction(
                facts, DROP_INDEX_CONCURRENTLY, "DROP INDEX CONCURRENTLY"
            )
        )
    dropped = common.dropped_indexes(facts)
    for name, table in dropped:
        label = common.index_label(name, table)
        parent = table is not None and partitioned(facts, table)
        named = table if table is not None else f"the table of index {name}"
        refusal = f"the server refuses DROP INDEX CONCURRENTLY of {name}"
        if concurrently:
            findings.extend(
                unread(facts, label, f"if {named} is a partitioned table, {refusal}")
            )
            effects.append(
                Effect(
                    DROP_INDEX_CONCURRENTLY,
                    label,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.CATALOG,
                )
            )
            if parent:
                findings.append(
                    refused(
                        DROP_INDEX_CONCURRENTLY,
                        f"the server refuses DROP INDEX CONCURRENTLY of {name}, "
                        f"an index on {label}, which is a partitioned table",
                    )
                )
            continue
        if parent:
            effects.append(
                Effect(
                    DROP_INDEX,
                    label,
                    ACCESS_EXCLUSIVE,
                    Work.CATALOG,
                    message=f"reads and writes on {label} and each of its "
                    "partitions wait until the drop commits; the server refuses "
                    "DROP INDEX CONCURRENTLY on a partitioned table",
                )
            )
            effects.extend(
                cascade(facts, DROP_INDEX, label, ACCESS_EXCLUSIVE, Work.CATALOG)
            )
            continue
        findings.extend(
            unread(
                facts,
                label,
                f"{locked_below(named, ACCESS_EXCLUSIVE)}, and {refusal}",
            )
        )
        concurrent = (
            insert_after(facts.statement, "INDEX", "CONCURRENTLY")
            if len(dropped) == 1
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
    return Outcome(tuple(effects), tuple(findings))


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
        default = default_partition(facts, str(parent))
        if default is not None:
            effects.append(default_scan(CREATE_TABLE, default, "the new partition"))
        return Outcome(
            tuple(effects),
            unread(
                facts, str(parent), default_clause(str(parent), "the new partition")
            ),
        )
    return Outcome(tuple(effects))


def default_clause(parent: str, partition: str) -> str:
    """The clause for the DEFAULT partition the partitions read did not name."""
    return (
        f"if {parent} has a DEFAULT partition, it is also locked ACCESS EXCLUSIVE "
        f"while each of its rows is checked against the bound of {partition}"
    )


def default_scan(rule: Rule, default: str, partition: str) -> Effect:
    """
    The lock on a DEFAULT partition when a partition is added beside it:
    PostgreSQL checks that none of its rows belongs in the new one,
    unless a valid CHECK constraint on it proves that.
    """
    return Effect(
        rule,
        default,
        ACCESS_EXCLUSIVE,
        Work.SCAN,
        Confidence.LIKELY,
        message=f"reads and writes on {default}, the DEFAULT partition, wait "
        f"while each of its rows is checked against the bound of {partition}",
    )


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
    findings: List[Finding] = []
    unnamed: List[Effect] = []
    for table in named:
        findings.extend(unread(facts, table, locked_below(table, ACCESS_EXCLUSIVE)))
        for other in descendants(facts, table):
            if other.lower() not in seen:
                seen.add(other.lower())
                effects.append(
                    Effect(DROP_TABLE, other, ACCESS_EXCLUSIVE, Work.CATALOG)
                )
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
        return Outcome(tuple(effects), tuple(findings))
    for table in named:
        effects.extend(_parent_locks(facts, table, seen))
        findings.extend(
            unread(
                facts,
                table,
                f"if {table} is a partition, its partitioned table and that "
                "table's DEFAULT partition are also locked ACCESS EXCLUSIVE",
            )
        )
        if is_unread(facts, table):
            # If the table is a partition, its partitioned table, which
            # may be larger, is locked too.
            unnamed.append(Effect(DROP_TABLE, table, ACCESS_EXCLUSIVE, Work.CATALOG))
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
    return Outcome(tuple(effects), tuple(findings), unnamed=tuple(unnamed))


def _parent_locks(facts: Facts, table: str, seen: Set[str]) -> List[Effect]:
    """
    Dropping a partition locks the partitioned table it belongs to, and
    that table's DEFAULT partition, ACCESS EXCLUSIVE.
    """
    parent = parent_of(facts, table)
    if parent is None:
        return []
    effects: List[Effect] = []
    for other in (parent, default_partition(facts, parent)):
        if other is None or other.lower() in seen:
            continue
        seen.add(other.lower())
        effects.append(
            Effect(
                DROP_TABLE,
                other,
                ACCESS_EXCLUSIVE,
                Work.CATALOG,
                message=f"dropping {table}, a partition of {parent}, locks "
                f"{other} ACCESS EXCLUSIVE: reads and writes on {other} wait "
                "until the statement commits",
            )
        )
    return effects


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


def _write_rows(facts: Facts) -> Outcome:
    table = common.table(facts)
    message = common.row_write_message(facts, table)
    return Outcome.of(
        Effect(
            WRITE_ROWS,
            table,
            ROW_EXCLUSIVE,
            Work.ROWS,
            message=message,
            blocks=Blocks.WRITES,
        )
    )


def _reindex(facts: Facts) -> Outcome:
    options = facts.parsed.options
    target = str(options.get("target"))
    name = str(options.get("name"))
    findings: List[Finding] = []
    parent = False
    named: Optional[str] = None
    if target == "table":
        label = named = name
        parent = partitioned(facts, name)
    elif target == "index":
        table = common.index_table(facts, name)
        label = common.index_label(name, table)
        named = table if table is not None else f"the table of index {name}"
        parent = table is not None and partitioned(facts, table)
    else:
        label = f"(every table in {target} {name})"
        findings.extend(
            refused_in_transaction(facts, REINDEX, f"REINDEX {target.upper()}")
        )
    if options.get("concurrently"):
        findings.extend(
            refused_in_transaction(facts, REINDEX_CONCURRENTLY, "REINDEX CONCURRENTLY")
        )
        rule, lock = REINDEX_CONCURRENTLY, SHARE_UPDATE_EXCLUSIVE
        effects = [Effect(rule, label, lock, Work.INDEX_BUILD)]
    else:
        if parent:
            findings.extend(
                refused_in_transaction(
                    facts, REINDEX, f"REINDEX of {label}, a partitioned table,"
                )
            )
        concurrent = insert_after(facts.statement, target.upper(), "CONCURRENTLY")
        rule, lock = REINDEX, SHARE
        effects = [
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
            )
        ]
    if parent:
        effects.extend(cascade(facts, rule, label, lock, Work.INDEX_BUILD))
    if named is not None:
        clause = f"{locked_below(named, lock)} while its indexes are rebuilt"
        if facts.transactional and not options.get("concurrently"):
            clause += ", and the server refuses the REINDEX inside a transaction block"
        findings.extend(unread(facts, label, clause))
    return Outcome(tuple(effects), tuple(findings))


def _vacuum(facts: Facts) -> Outcome:
    tables = common.tables(facts)
    findings: List[Finding] = []
    full = bool(facts.parsed.options.get("full"))
    if facts.parsed.kind == "vacuum":
        findings.extend(
            refused_in_transaction(facts, VACUUM_FULL if full else VACUUM, "VACUUM")
        )
    elif facts.parsed.kind == "cluster":
        findings.extend(
            refused_in_transaction(facts, VACUUM_FULL, "CLUSTER with no table")
        )
    if not tables:
        findings.append(
            VACUUM.finding(
                Severity.INFO,
                f"{facts.parsed.kind.upper()} with no table reads every table "
                "in the database",
            )
        )
        return Outcome(findings=tuple(findings), confidence=Confidence.LIKELY)
    if full:
        rule, lock, work = VACUUM_FULL, ACCESS_EXCLUSIVE, Work.REWRITE
    else:
        rule, lock, work = VACUUM, SHARE_UPDATE_EXCLUSIVE, Work.SCAN
    effects: List[Effect] = []
    for table in tables:
        effects.append(Effect(rule, table, lock, work))
        effects.extend(cascade(facts, rule, table, lock, work))
        findings.extend(unread(facts, table, locked_below(table, lock)))
    return Outcome(tuple(effects), tuple(findings))


def _cluster(facts: Facts) -> Outcome:
    if facts.parsed.table is None:
        return _vacuum(facts)
    return Outcome.of(
        Effect(VACUUM_FULL, facts.parsed.table, ACCESS_EXCLUSIVE, Work.REWRITE)
    )


def _refresh(facts: Facts) -> Outcome:
    view = facts.parsed.table or "(unnamed view)"
    options = facts.parsed.options
    work = Work.REWRITE if options.get("with_data", True) else Work.CATALOG
    if options.get("concurrently"):
        # The query runs in full, and only the rows that differ are
        # written into the view, whose file stays in place.
        return Outcome.of(Effect(REFRESH_CONCURRENTLY, view, EXCLUSIVE, Work.ROWS))
    concurrent = insert_after(facts.statement, "VIEW", "CONCURRENTLY")
    return Outcome.of(
        Effect(
            REFRESH,
            view,
            ACCESS_EXCLUSIVE,
            work,
            message=f"reads of {view} wait for the whole refresh; refresh it "
            "CONCURRENTLY, which needs a unique index on the view",
            remedy=(trimmed(concurrent),) if concurrent else (),
        )
    )


def _trigger(facts: Facts) -> Outcome:
    table = common.table(facts)
    lock = (
        SHARE_ROW_EXCLUSIVE
        if facts.parsed.kind == "create_trigger"
        else ACCESS_EXCLUSIVE
    )
    return Outcome.of(
        Effect(TRIGGER, table, lock, Work.CATALOG),
        *cascade(facts, TRIGGER, table, lock, Work.CATALOG),
        findings=unread(facts, table, locked_below(table, lock)),
    )


def _comment(facts: Facts) -> Outcome:
    if facts.parsed.options.get("object") not in ("table", "column"):
        return Outcome()
    table = common.table(facts)
    return Outcome.of(Effect(COMMENT, table, SHARE_UPDATE_EXCLUSIVE, Work.CATALOG))


def _lock_table(facts: Facts) -> Outcome:
    options = facts.parsed.options
    mode = str(options.get("mode"))
    waits = not options.get("nowait")
    effects: List[Effect] = []
    findings: List[Finding] = []
    for table in common.tables(facts):
        effects.append(Effect(LOCK_TABLE, table, mode, Work.CATALOG, waits=waits))
        if not options.get("only"):
            effects.extend(
                e._replace(waits=waits)
                for e in cascade(facts, LOCK_TABLE, table, mode, Work.CATALOG)
            )
            findings.extend(unread(facts, table, locked_below(table, mode)))
    return Outcome(tuple(effects), tuple(findings))


def _drop_object(facts: Facts) -> Outcome:
    if facts.parsed.options.get("object") != "schema":
        return Outcome()
    return Outcome(
        findings=(
            DROP_SCHEMA.finding(
                Severity.INFO,
                "dropping a schema locks every table in it ACCESS EXCLUSIVE; "
                "the tables are not named in the statement",
            ),
        ),
        confidence=Confidence.LIKELY,
    )
