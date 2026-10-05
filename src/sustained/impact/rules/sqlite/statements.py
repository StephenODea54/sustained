"""
The handlers for each SQLite statement kind, and the one lock every
write takes.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from sustained.impact.model import (
    Action,
    Blocks,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, Rule, common
from sustained.impact.rules.sqlite.catalog import (
    ADD_COLUMN,
    ADD_COLUMN_CHECKED,
    ADD_COLUMN_REFUSED,
    ANALYZE,
    COPY,
    CREATE_INDEX,
    DROP_COLUMN,
    DROP_INDEX,
    DROP_TABLE,
    REBUILD,
    REINDEX,
    RENAME,
    SCHEMA_CHANGE,
    VACUUM,
    WRITE_ROWS,
)
from sustained.impact.window import DATABASE

WRITE_LOCK = "database write lock"


def blocks(lock: Optional[str]) -> Blocks:
    """
    What the write lock blocks outside WAL mode, the worst case: every
    write, and reads while the changes reach the database file. A
    handler lowers it to writes in WAL mode.
    """
    return Blocks.NOTHING if lock is None else Blocks.READS_AND_WRITES


def lock_rank(lock: Optional[str]) -> int:
    """The write lock's strength, and -1 for no lock."""
    return -1 if lock is None else 0


def timeout_statement(transactional: bool) -> str:
    """The statement that bounds how long a write waits for the lock."""
    return "PRAGMA busy_timeout = 5000"


def _journal_mode(facts: Facts) -> Optional[str]:
    return facts.context.settings.get("journal_mode")


def _blocked(facts: Facts) -> Blocks:
    """In WAL mode readers never wait for the writer."""
    return Blocks.WRITES if _journal_mode(facts) == "wal" else Blocks.READS_AND_WRITES


def _message(facts: Facts, what: str, advice: Optional[str] = None) -> str:
    """
    The finding for blocking work: `what` the statement does, then what
    waits for the write lock, and for how long, then any `advice`.
    """
    until = "the migration commits" if facts.transactional else "it ends"
    text = f"{what}; writes to every table in the database wait until {until}"
    mode = _journal_mode(facts)
    if mode is None:
        text += (
            ", and outside WAL mode reads wait while the changes are written "
            "to the database file; the journal mode was not read"
        )
    elif mode != "wal":
        text += (
            f", and in journal mode {mode} reads wait while the changes are "
            "written to the database file"
        )
    return f"{text}; {advice}" if advice else text


def _effect(
    facts: Facts,
    rule: Rule,
    table: str,
    work: Work,
    what: Optional[str] = None,
    notes: Tuple[Finding, ...] = (),
    confidence: Confidence = Confidence.KNOWN,
    advice: Optional[str] = None,
) -> Effect:
    return Effect(
        rule,
        table,
        WRITE_LOCK,
        work,
        confidence,
        message=_message(facts, what, advice) if what else None,
        notes=notes,
        blocks=_blocked(facts),
    )


def _action(facts: Facts, action: Action) -> Optional[Effect]:
    """One ALTER TABLE action's effect, or None for one no rule reads."""
    table = common.table(facts)
    options = action.options
    if action.kind == "add_column":
        checked = options.get("check") or (
            options.get("not_null") and options.get("generated")
        )
        if checked:
            return _effect(
                facts,
                ADD_COLUMN_CHECKED,
                table,
                Work.SCAN,
                f"SQLite checks the new column's constraint on every row of {table}",
            )
        return _effect(facts, ADD_COLUMN, table, Work.CATALOG)
    if action.kind == "drop_column":
        return _effect(
            facts,
            DROP_COLUMN,
            table,
            Work.REWRITE,
            f"DROP COLUMN rewrites every row of {table}",
        )
    if action.kind == "rename_column":
        note = common.rename_note(RENAME, "column", str(action.column))
        return _effect(facts, RENAME, table, Work.CATALOG, notes=(note,))
    if action.kind == "rename_to":
        return _effect(
            facts,
            RENAME,
            table,
            Work.CATALOG,
            notes=(common.rename_note(RENAME, "table", table),),
        )
    return None


# The defaults SQLite takes as not constant, which ADD COLUMN refuses on
# a table that has rows.
_TIME_DEFAULTS = frozenset({"CURRENT_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP"})


def _refused(
    facts: Facts, options: Mapping[str, object]
) -> Optional[Tuple[Finding, Confidence]]:
    """
    The `danger` finding for a column ADD COLUMN refuses. A UNIQUE or
    PRIMARY KEY column is refused always. A NOT NULL column whose
    default is missing or NULL, a default in parentheses or of the
    current time, and a STORED generated column are refused when the
    table has rows, so a table the run created, or one read as empty,
    gets no finding, and one whose row count was not read makes the
    statement's confidence `likely`.
    """
    table = common.table(facts)
    remedy = (
        f"rebuild {table} with the new column: create the new table, copy the "
        f"rows into it, drop {table}, and rename the new table to {table}",
    )
    if options.get("unique") or options.get("primary_key"):
        refusal = ADD_COLUMN_REFUSED.finding(
            Severity.DANGER,
            "SQLite refuses to add a UNIQUE or PRIMARY KEY column, so the "
            "migration fails",
            remedy,
        )
        return refusal, Confidence.KNOWN
    default = options.get("default")
    text = str(default).strip().upper() if default is not None else None
    if options.get("not_null") and text in (None, "NULL"):
        what = "a NOT NULL column without a default other than NULL"
    elif text is not None and (text.startswith("(") or text in _TIME_DEFAULTS):
        what = "a column whose default is an expression or the current time"
    elif options.get("generated") == "stored":
        what = "a STORED generated column"
    else:
        return None
    rows = facts.context.stats(table).rows
    if facts.state.is_new(table) or rows == 0:
        return None
    if rows is None:
        confidence = Confidence.LIKELY
        has = f"unless {table} is empty; its row count was not read"
    else:
        confidence = Confidence.KNOWN
        has = f"and {table} has {rows:,}"
    refusal = ADD_COLUMN_REFUSED.finding(
        Severity.DANGER,
        f"SQLite refuses to add {what} to a table that has rows, so the migration "
        f"fails, {has}",
        remedy,
    )
    return refusal, confidence


def _alter_table(facts: Facts) -> Outcome:
    effects: List[Effect] = []
    findings: List[Finding] = []
    for action in facts.parsed.actions:
        effect = _action(facts, action)
        if effect is None:
            return common.unknown(facts, f"the ALTER TABLE action {action.kind}")
        if action.kind == "add_column":
            refused = _refused(facts, action.options)
            if refused is not None:
                refusal, confidence = refused
                findings.append(refusal)
                effect = effect._replace(confidence=confidence)
        effects.append(effect)
    return Outcome(tuple(effects), tuple(findings))


def _rebuilt(facts: Facts) -> Optional[str]:
    """
    The table a statement of the diff's rebuild recipe rebuilds, when it
    is not the table the statement names: the recipe copies the rows
    into a new table, which then takes the old one's place.
    """
    intent = facts.intent
    if intent is None or intent.kind != "rebuild_table" or intent.table is None:
        return None
    if common.table(facts).lower() == intent.table.lower():
        return None
    return intent.table


def _insert(facts: Facts) -> Outcome:
    rebuilt = _rebuilt(facts)
    if rebuilt is not None:
        return Outcome.of(
            _effect(
                facts,
                REBUILD,
                rebuilt,
                Work.REWRITE,
                f"the rebuild copies every row of {rebuilt} into a new table "
                "and builds its indexes again",
            )
        )
    table = common.table(facts)
    if facts.parsed.options.get("source") == "select" and facts.state.is_new(table):
        copied = _copy(facts, table)
        if copied is not None:
            return copied
    return _write_rows(facts)


def _create_table(facts: Facts) -> Outcome:
    if facts.parsed.options.get("as_select"):
        copied = _copy(facts, common.table(facts))
        if copied is not None:
            return copied
    return _schema_change(facts)


def _copy(facts: Facts, target: str) -> Optional[Outcome]:
    """
    A copy of a query's rows into a table the statement creates, or one
    the run created, reported on each table the query reads, since the
    write lock lasts while every row of each is read and written again.
    A query that reads rows from something other than a table is
    reported on the whole database. None for a query that reads no
    table, whose rows are few.
    """
    reads = facts.parsed.options.get("reads")
    if isinstance(reads, tuple) and not reads:
        return None
    sources = [str(s) for s in reads] if isinstance(reads, tuple) else [DATABASE]
    confidence = Confidence.KNOWN if isinstance(reads, tuple) else Confidence.LIKELY
    return Outcome(
        tuple(
            _effect(
                facts,
                COPY,
                source,
                Work.REWRITE,
                (
                    f"the copy reads every row of {source} and writes it into {target}"
                    if source != DATABASE
                    else f"the copy writes rows the query gives into {target}, and "
                    "their number was not read"
                ),
                confidence=confidence,
            )
            for source in sources
        )
    )


def _write_rows(facts: Facts) -> Outcome:
    table = common.table(facts)
    what = f"the {facts.parsed.kind.upper()}"
    if facts.intent is not None and facts.intent.kind == "backfill":
        what = "the backfill"
    batches = "delete" if facts.parsed.kind == "delete" else "backfill"
    return Outcome.of(
        _effect(
            facts,
            WRITE_ROWS,
            table,
            Work.ROWS,
            f"{what} writes rows of {table}",
            advice=f"on a large table, {batches} in batches outside the DDL "
            "migration",
        )
    )


def _create_index(facts: Facts) -> Outcome:
    table = common.table(facts)
    return Outcome.of(
        _effect(
            facts,
            CREATE_INDEX,
            table,
            Work.INDEX_BUILD,
            f"the index build reads every row of {table}",
        )
    )


def _index_table(facts: Facts, name: str) -> Optional[str]:
    return facts.state.index_table(name) or facts.context.index_table(name)


def _drop_index(facts: Facts) -> Outcome:
    effects = []
    for name in facts.parsed.items("names"):
        table = _index_table(facts, str(name))
        if table is None and facts.intent is not None:
            table = facts.intent.table
        effects.append(
            _effect(
                facts,
                DROP_INDEX,
                table or f"(table of index {name})",
                Work.SCAN,
                f"DROP INDEX visits every page of {name} to free it",
            )
        )
    return Outcome(tuple(effects))


def _reindex(facts: Facts) -> Outcome:
    name = facts.parsed.options.get("name")
    if name is None:
        return Outcome.of(
            _effect(
                facts,
                REINDEX,
                DATABASE,
                Work.INDEX_BUILD,
                "REINDEX builds every index in the database again",
            )
        )
    name = str(name)
    table = _index_table(facts, name)
    confidence = Confidence.KNOWN
    if table is None:
        known = facts.context.table(name) is not None or facts.state.is_new(name)
        table = name
        if not known:
            # The name may be a collation, whose indexes span tables.
            confidence = Confidence.LIKELY
    return Outcome.of(
        _effect(
            facts,
            REINDEX,
            table,
            Work.INDEX_BUILD,
            f"REINDEX builds the indexes of {table} again",
            confidence=confidence,
        )
    )


def _schema_change(facts: Facts) -> Outcome:
    table = facts.parsed.table or DATABASE
    return Outcome.of(_effect(facts, SCHEMA_CHANGE, table, Work.CATALOG))


def _drop_table(facts: Facts) -> Outcome:
    return Outcome(
        tuple(
            _effect(
                facts,
                DROP_TABLE,
                table,
                Work.SCAN,
                f"DROP TABLE visits every page of {table} to free it",
            )
            for table in common.tables(facts)
        )
    )


def _analyze(facts: Facts) -> Outcome:
    tables = common.tables(facts) or [DATABASE]
    return Outcome(
        tuple(
            _effect(
                facts,
                ANALYZE,
                table,
                Work.SCAN,
                f"ANALYZE reads every index of {table}",
            )
            for table in tables
        )
    )


def _vacuum(facts: Facts) -> Outcome:
    findings: Tuple[Finding, ...] = ()
    if facts.transactional:
        findings = (
            VACUUM.finding(
                Severity.DANGER,
                "VACUUM cannot run inside a transaction, so this migration "
                "fails; run it in a migration with transactional=False",
            ),
        )
    effect = _effect(
        facts,
        VACUUM,
        DATABASE,
        Work.REWRITE,
        "VACUUM copies the whole database into a new file",
    )
    return Outcome.of(effect, findings=findings)


STATEMENTS: Dict[str, common.Handler] = {
    "alter_table": _alter_table,
    "create_index": _create_index,
    "drop_index": _drop_index,
    "reindex": _reindex,
    "create_table": _create_table,
    "create_view": _schema_change,
    "drop_view": _schema_change,
    "create_trigger": _schema_change,
    "drop_trigger": _schema_change,
    "drop_table": _drop_table,
    "insert": _insert,
    "update": _write_rows,
    "delete": _write_rows,
    "analyze": _analyze,
    "vacuum": _vacuum,
    "set": common.nothing,
}
