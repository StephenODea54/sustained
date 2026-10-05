"""
The handler for each ALTER TABLE action but a column type change.
"""

from __future__ import annotations

import re
from typing import (
    Callable,
    List,
    Optional,
    Tuple,
)

from sustained.dialects import Dialects
from sustained.impact.context import (
    EngineContext,
)
from sustained.impact.model import (
    Action,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.recognizer.definitions import not_null_column
from sustained.impact.rules import Effect, Facts, Outcome, Rule, common
from sustained.impact.rules.common import TRANSACTION_NOTE, ActionHandler
from sustained.impact.rules.postgres.catalog import (
    ADD_CHECK,
    ADD_CHECK_NOT_VALID,
    ADD_COLUMN,
    ADD_COLUMN_CHECKED,
    ADD_COLUMN_KEY,
    ADD_COLUMN_REWRITE,
    ADD_EXCLUSION,
    ADD_FOREIGN_KEY,
    ADD_FOREIGN_KEY_NOT_VALID,
    ADD_KEY,
    ADD_KEY_USING_INDEX,
    ATTACH_PARTITION,
    DETACH_PARTITION,
    DETACH_PARTITION_CONCURRENTLY,
    DROP_COLUMN,
    DROP_CONSTRAINT,
    RENAME,
    SET_NOT_NULL,
    SET_NOT_NULL_PROVEN,
    TABLE_CATALOG,
    TABLE_PARAMETERS,
    VALIDATE,
)
from sustained.impact.rules.postgres.column_types import domain_check
from sustained.impact.rules.postgres.locks import (
    ACCESS_EXCLUSIVE,
    ROW_SHARE,
    SHARE_ROW_EXCLUSIVE,
    SHARE_UPDATE_EXCLUSIVE,
)
from sustained.impact.rules.postgres.partitions import (
    cascade,
    default_partition,
    descendants,
    is_unread,
    locked_below,
    partitioned,
    relation,
    unread,
)
from sustained.impact.rules.postgres.remedies import (
    key_columns,
    last_part,
    quoted,
    spelled,
    trimmed,
)
from sustained.impact.rules.postgres.statements import (
    default_clause,
    default_scan,
    foreign_key_effect,
    refused,
    refused_in_transaction,
)
from sustained.impact.tokens import tokenize


def _add_column(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    options = action.options
    column = action.column or "?"
    volatility = options.get("default_volatility")
    rewrite_reason: Optional[str] = None
    confidence = Confidence.KNOWN
    if options.get("serial"):
        rewrite_reason = "a serial column fills every row from its sequence"
    elif options.get("identity"):
        rewrite_reason = "an identity column fills every row from its sequence"
    elif options.get("generated") == "stored":
        rewrite_reason = "a stored generated column is computed for every row"
    elif volatility == "volatile":
        rewrite_reason, confidence = common.volatile_default(options)
    else:
        checked = domain_check(facts, str(options.get("type") or ""))
        if checked is not None:
            rewrite_reason, confidence = checked
    effects: List[Effect] = []
    if rewrite_reason is not None:
        remedy: Tuple[str, ...] = ()
        default = options.get("default")
        function = options.get("default_function")
        if volatility == "volatile" and default and function and options.get("type"):
            t, c = spelled(facts.statement, table), spelled(facts.statement, column)
            remedy = (
                f"ALTER TABLE {t} ADD COLUMN {c} {options.get('type')}",
                f"ALTER TABLE {t} ALTER COLUMN {c} SET DEFAULT {default}",
            )
        effects.append(
            Effect(
                ADD_COLUMN_REWRITE,
                table,
                ACCESS_EXCLUSIVE,
                Work.REWRITE,
                confidence,
                message=f"{rewrite_reason}, so the table is rewritten while reads "
                "and writes wait"
                + (
                    "; add the column without a default, set the default, then "
                    "backfill in batches"
                    if remedy
                    else ""
                ),
                remedy=remedy,
            )
        )
    elif options.get("primary_key") or options.get("unique"):
        effects.append(
            Effect(ADD_COLUMN_KEY, table, ACCESS_EXCLUSIVE, Work.INDEX_BUILD)
        )
    elif options.get("not_null") and options.get("default") is None:
        # With no default the new column is NULL in every row, so the
        # server scans the table and refuses the statement at the first row.
        effects.append(
            Effect(
                ADD_COLUMN_CHECKED,
                table,
                ACCESS_EXCLUSIVE,
                Work.SCAN,
                message=f"the server refuses a NOT NULL column with no DEFAULT "
                f"unless {table} has no rows, which it scans for while reads and "
                "writes wait; give the column a DEFAULT, or add it NULL, backfill "
                "it, then SET NOT NULL",
            )
        )
    elif options.get("check") or options.get("references"):
        effects.append(Effect(ADD_COLUMN_CHECKED, table, ACCESS_EXCLUSIVE, Work.SCAN))
    else:
        effects.append(Effect(ADD_COLUMN, table, ACCESS_EXCLUSIVE, Work.CATALOG))
    referenced = options.get("references")
    if referenced:
        effects.append(
            Effect(
                ADD_COLUMN_CHECKED, str(referenced), SHARE_ROW_EXCLUSIVE, Work.CATALOG
            )
        )
    return Outcome(tuple(effects), confidence=confidence)


def _drop_column(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    live = facts.state.original(table)
    column = [action.column] if action.column else []
    effects = [Effect(DROP_COLUMN, table, ACCESS_EXCLUSIVE, Work.CATALOG)]
    for other in facts.context.references(live, column):
        effects.append(foreign_key_effect(table, other, "dropped with the column"))
    if action.options.get("cascade"):
        for other in facts.context.referenced_by(live, column):
            effects.append(
                foreign_key_effect(other, table, "dropped with the column", other)
            )
    return Outcome(tuple(effects))


def _drop_constraint(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    live = facts.state.original(table)
    effects = [Effect(DROP_CONSTRAINT, table, ACCESS_EXCLUSIVE, Work.CATALOG)]
    name = action.options.get("name")
    if not name:
        return Outcome(tuple(effects))
    context = facts.context
    target = context.foreign_key_target(live, str(name))
    if target is not None:
        if target.lower() != table.lower():
            effects.append(foreign_key_effect(table, target, "dropped"))
    elif action.options.get("cascade"):
        columns = _constraint_columns(context, live, str(name))
        if columns:
            for other in context.referenced_by(live, columns):
                effects.append(foreign_key_effect(other, table, "dropped", other))
    return Outcome(tuple(effects))


def _constraint_columns(
    context: EngineContext, table: str, name: str
) -> Tuple[str, ...]:
    """
    The columns of a unique or primary key constraint, from the schema
    read. The read keeps the columns of a primary key but not its name,
    so a named constraint that is neither a unique constraint nor a
    check is taken for the primary key.
    """
    found = context.table(table)
    if found is None:
        return ()
    index = found.indexes.get(name.lower())
    if index is not None:
        return index.columns if index.constraint else ()
    if name.lower() in found.check_names or name.lower() in found.checks:
        return ()
    return found.primary_key


def _proven_not_null(facts: Facts, table: str, column: str) -> bool:
    """
    Whether a valid check of the form `column IS NOT NULL`, which the
    run added or the schema read reports, proves the column has no NULL.
    pg_get_constraintdef() writes NOT VALID after a check not yet
    validated, so a check read with it proves nothing. A schema check
    the run dropped proves nothing, and the run's renames of checks and
    columns decide which column a schema check tests now. A table the
    run created has none of the schema's checks.
    """
    state = facts.state
    if state.proves_not_null(table, column):
        return True
    if state.created_in_run(table):
        return False
    found = facts.context.table(state.original(table))
    schema = state.schema_column(table, column)
    if found is None or schema is None:
        return False
    for name, expression in found.checks.items():
        if not state.schema_check_kept(table, name):
            continue
        tested = not_null_column(tokenize(expression, Dialects.POSTGRES))
        if tested is not None and tested.lower() == schema:
            return True
    return False


def _set_not_null(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    column = action.column or "?"
    if _proven_not_null(facts, table, column):
        return Outcome.of(
            Effect(SET_NOT_NULL_PROVEN, table, ACCESS_EXCLUSIVE, Work.CATALOG)
        )
    check = f"{last_part(table, facts.statement)}_{column}_not_null"[:63]
    t, c = spelled(facts.statement, table), spelled(facts.statement, column)
    k = quoted(check)
    remedy = (
        f"ALTER TABLE {t} ADD CONSTRAINT {k} CHECK ({c} IS NOT NULL) NOT VALID",
        f"ALTER TABLE {t} VALIDATE CONSTRAINT {k}",
        f"ALTER TABLE {t} ALTER COLUMN {c} SET NOT NULL",
        f"ALTER TABLE {t} DROP CONSTRAINT {k}",
    )
    return Outcome.of(
        Effect(
            SET_NOT_NULL,
            table,
            ACCESS_EXCLUSIVE,
            Work.SCAN,
            Confidence.LIKELY,
            message=f"reads and writes on {table} wait while every row is "
            f"checked for NULL, unless a valid CHECK ({column} IS NOT NULL) "
            "already proves it; add that check NOT VALID, validate it, then "
            "SET NOT NULL skips the scan",
            remedy=remedy,
        ),
        confidence=Confidence.LIKELY,
    )


def _add_constraint(facts: Facts, action: Action) -> Outcome:
    constraint = action.options.get("constraint")
    if constraint == "check":
        return _add_check(facts, action)
    if constraint == "foreign_key":
        return _add_foreign_key(facts, action)
    if constraint in ("primary_key", "unique"):
        return _add_key(facts, action)
    table = common.table(facts)
    findings: Tuple[Finding, ...] = ()
    if partitioned(facts, table) and facts.context.version < (17,):
        findings = (
            refused(
                ADD_EXCLUSION,
                f"PostgreSQL before 17 refuses an exclusion constraint on {table}, "
                "a partitioned table",
            ),
        )
    elif facts.context.version < (17,):
        findings = unread(
            facts,
            table,
            f"if {table} is a partitioned table, PostgreSQL before 17 refuses an "
            "exclusion constraint on it",
        )
    return Outcome.of(
        Effect(ADD_EXCLUSION, table, ACCESS_EXCLUSIVE, Work.INDEX_BUILD),
        findings=findings,
    )


def _validate_later(facts: Facts, action: Action) -> Tuple[str, ...]:
    name = action.options.get("name")
    if not name:
        return ()
    return (
        trimmed(facts.statement) + " NOT VALID",
        f"ALTER TABLE {spelled(facts.statement, common.table(facts))} "
        f"VALIDATE CONSTRAINT {spelled(facts.statement, str(name))}",
    )


def _add_check(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    if action.options.get("not_valid"):
        return Outcome.of(
            Effect(ADD_CHECK_NOT_VALID, table, ACCESS_EXCLUSIVE, Work.CATALOG)
        )
    return Outcome.of(
        Effect(
            ADD_CHECK,
            table,
            ACCESS_EXCLUSIVE,
            Work.SCAN,
            message=f"reads and writes on {table} wait while every row is "
            "checked; add the check NOT VALID, then VALIDATE CONSTRAINT in a "
            "later migration, which lets reads and writes go on",
            remedy=_validate_later(facts, action),
        )
    )


def _add_foreign_key(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    referenced = str(action.options.get("references"))
    if action.options.get("not_valid"):
        findings: Tuple[Finding, ...] = ()
        if partitioned(facts, table) and facts.context.version < (18,):
            findings = (
                refused(
                    ADD_FOREIGN_KEY_NOT_VALID,
                    f"PostgreSQL before 18 refuses a NOT VALID foreign key on "
                    f"{table}, a partitioned table; add the key to each partition "
                    "NOT VALID instead",
                ),
            )
        elif facts.context.version < (18,):
            findings = unread(
                facts,
                table,
                f"if {table} is a partitioned table, PostgreSQL before 18 refuses "
                "a NOT VALID foreign key on it",
            )
        return Outcome(
            tuple(
                Effect(
                    ADD_FOREIGN_KEY_NOT_VALID, name, SHARE_ROW_EXCLUSIVE, Work.CATALOG
                )
                for name in (table, referenced)
            ),
            findings,
        )
    return Outcome.of(
        Effect(
            ADD_FOREIGN_KEY,
            table,
            SHARE_ROW_EXCLUSIVE,
            Work.SCAN,
            message=f"writes to {table} and {referenced} wait while every row "
            f"of {table} is checked; add the key NOT VALID, then VALIDATE "
            "CONSTRAINT in a later migration",
            remedy=_validate_later(facts, action),
        ),
        Effect(ADD_FOREIGN_KEY, referenced, SHARE_ROW_EXCLUSIVE, Work.CATALOG),
    )


def _add_key(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    options = action.options
    if options.get("using_index"):
        return Outcome.of(
            Effect(ADD_KEY_USING_INDEX, table, ACCESS_EXCLUSIVE, Work.CATALOG)
        )
    primary = options.get("constraint") == "primary_key"
    columns = key_columns(facts.statement)
    remedy: Tuple[str, ...] = ()
    if columns is not None:
        given = options.get("name")
        suffix = "pkey" if primary else "key"
        name = str(given or f"{last_part(table, facts.statement)}_{suffix}")
        index = f"{name}_idx"[:63]
        kind = "PRIMARY KEY" if primary else "UNIQUE"
        t = spelled(facts.statement, table)
        k = spelled(facts.statement, name) if given else quoted(name)
        remedy = (
            f"CREATE UNIQUE INDEX CONCURRENTLY {quoted(index)} ON {t} {columns}",
            f"ALTER TABLE {t} ADD CONSTRAINT {k} {kind} USING INDEX {quoted(index)}",
        )
    return Outcome.of(
        Effect(
            ADD_KEY,
            table,
            ACCESS_EXCLUSIVE,
            Work.INDEX_BUILD,
            message=f"reads and writes on {table} wait for the whole index "
            f"build; build a unique index CONCURRENTLY {TRANSACTION_NOTE}, "
            "then add the constraint USING INDEX",
            remedy=remedy,
        )
    )


def _validate(facts: Facts, action: Action) -> Outcome:
    """
    VALIDATE CONSTRAINT scans the table under SHARE UPDATE EXCLUSIVE.
    A foreign key's rows are looked up in the table it points at, which
    takes ROW SHARE there. Without the schema read, or with a constraint
    it does not list, whether the constraint is a foreign key is not
    known.
    """
    table = common.table(facts)
    live = facts.state.original(table)
    name = str(action.options.get("name") or "")
    effects = [Effect(VALIDATE, table, SHARE_UPDATE_EXCLUSIVE, Work.SCAN)]
    target = facts.context.foreign_key_target(live, name) if name else None
    if target is not None:
        if target.lower() != table.lower():
            effects.append(Effect(VALIDATE, target, ROW_SHARE, Work.CATALOG))
        return Outcome(tuple(effects))
    found = facts.context.table(live)
    known = found is not None and (
        name.lower() in found.check_names or name.lower() in found.checks
    )
    confidence = Confidence.KNOWN if known else Confidence.LIKELY
    return Outcome(
        tuple(e._replace(confidence=confidence) for e in effects),
        confidence=confidence,
    )


def _rename(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    if action.kind == "rename_constraint":
        return Outcome.of(Effect(RENAME, table, ACCESS_EXCLUSIVE, Work.CATALOG))
    what = "column" if action.kind == "rename_column" else "table"
    old = action.column if action.kind == "rename_column" else table
    note = common.rename_note(RENAME, what, old)
    return Outcome.of(
        Effect(RENAME, table, ACCESS_EXCLUSIVE, Work.CATALOG, notes=(note,))
    )


def _attach_partition(facts: Facts, action: Action) -> Outcome:
    """
    ATTACH PARTITION checks each row of the new partition, and of the
    partitions below it, against the bound, and checks that no row of
    the DEFAULT partition belongs in it. Each index on the partitioned
    table needs a matching index on the new partition, which is built
    when the partition has none.
    """
    table = common.table(facts)
    partition = str(action.options.get("partition"))
    effects = [
        Effect(ATTACH_PARTITION, table, SHARE_UPDATE_EXCLUSIVE, Work.CATALOG),
        Effect(
            ATTACH_PARTITION,
            partition,
            ACCESS_EXCLUSIVE,
            Work.SCAN,
            Confidence.LIKELY,
            message=f"reads and writes on {partition} wait while every row is "
            "checked against the partition bound, unless a valid CHECK "
            "constraint already proves it; add one NOT VALID and validate "
            "it first",
        ),
    ]
    effects.extend(
        cascade(
            facts,
            ATTACH_PARTITION,
            partition,
            ACCESS_EXCLUSIVE,
            Work.SCAN,
            Confidence.LIKELY,
        )
    )
    default = default_partition(facts, table)
    if default is not None and default.lower() != partition.lower():
        effects.append(default_scan(ATTACH_PARTITION, default, partition))
    findings = unread(
        facts,
        partition,
        f"{locked_below(partition, ACCESS_EXCLUSIVE)} while its rows are checked "
        "against the bound",
    ) + unread(facts, table, default_clause(table, partition))
    # If the table has a DEFAULT partition, which may be larger than the
    # partition, reads and writes on it wait while its rows are checked.
    unnamed = (
        (Effect(ATTACH_PARTITION, table, ACCESS_EXCLUSIVE, Work.SCAN),)
        if is_unread(facts, table)
        else ()
    )
    if _has_indexes(facts, table) is not False:
        effects.append(
            Effect(
                ATTACH_PARTITION,
                partition,
                ACCESS_EXCLUSIVE,
                Work.INDEX_BUILD,
                Confidence.LIKELY,
                message=f"each index on {table} that {partition} has no matching "
                f"index for is built on {partition} while reads and writes on it "
                "wait; build those indexes on it CONCURRENTLY before attaching it",
            )
        )
    return Outcome(tuple(effects), findings, Confidence.LIKELY, unnamed=unnamed)


def _has_indexes(facts: Facts, table: str) -> Optional[bool]:
    """
    Whether the table has an index, or None when no read says. The
    schema read leaves out expression indexes, so it only proves the
    table has one.
    """
    if "indexes" in facts.context.read:
        found = relation(facts, table)
        return found is not None and bool(found.indexed)
    schema = facts.context.table(facts.state.original(table))
    if schema is not None and (schema.primary_key or schema.indexes):
        return True
    return None


def _detach_partition(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    partition = str(action.options.get("partition"))
    default = default_partition(facts, table)
    if action.options.get("concurrently"):
        findings: List[Finding] = []
        if not DETACH_PARTITION_CONCURRENTLY.versions(facts.context.version):
            findings.append(
                _needs_version(facts.context, DETACH_PARTITION_CONCURRENTLY, (14,))
            )
        findings.extend(
            refused_in_transaction(
                facts, DETACH_PARTITION_CONCURRENTLY, "DETACH PARTITION CONCURRENTLY"
            )
        )
        if default is not None:
            findings.append(
                refused(
                    DETACH_PARTITION_CONCURRENTLY,
                    f"the server refuses DETACH PARTITION CONCURRENTLY while {table} "
                    f"has a DEFAULT partition, {default}",
                )
            )
        findings.extend(
            unread(
                facts,
                table,
                f"if {table} has a DEFAULT partition, the server refuses DETACH "
                "PARTITION CONCURRENTLY",
            )
        )
        return Outcome.of(
            Effect(
                DETACH_PARTITION_CONCURRENTLY,
                table,
                SHARE_UPDATE_EXCLUSIVE,
                Work.CATALOG,
            ),
            Effect(
                DETACH_PARTITION_CONCURRENTLY,
                partition,
                SHARE_UPDATE_EXCLUSIVE,
                Work.CATALOG,
            ),
            findings=tuple(findings),
        )
    effects = [
        Effect(DETACH_PARTITION, table, ACCESS_EXCLUSIVE, Work.CATALOG),
        Effect(DETACH_PARTITION, partition, ACCESS_EXCLUSIVE, Work.CATALOG),
    ]
    locked = descendants(facts, partition)
    if default is not None and default.lower() != partition.lower():
        locked.append(default)
    effects.extend(
        Effect(DETACH_PARTITION, name, ACCESS_EXCLUSIVE, Work.CATALOG)
        for name in locked
    )
    below = unread(facts, partition, locked_below(partition, ACCESS_EXCLUSIVE))
    return Outcome(
        tuple(effects),
        below
        + unread(
            facts,
            table,
            f"if {table} has a DEFAULT partition, it is also locked ACCESS EXCLUSIVE",
        ),
    )


def _needs_version(
    context: EngineContext, rule: Rule, version: Tuple[int, ...]
) -> Finding:
    from sustained.impact.context import version_text

    assumed = "" if "version" in context.read else "assumed "
    return rule.finding(
        Severity.WARN,
        f"this needs PostgreSQL {version_text(version)} or later; the {assumed}"
        f"server version is {version_text(context.version)}",
    )


# The storage parameters Postgres changes under SHARE UPDATE EXCLUSIVE;
# any other parameter takes ACCESS EXCLUSIVE.
_LIGHT_PARAMETERS_RE = re.compile(
    r"(TOAST\.)?(AUTOVACUUM_.*|FILLFACTOR|TOAST_TUPLE_TARGET|PARALLEL_WORKERS"
    r"|LOG_AUTOVACUUM_MIN_DURATION|VACUUM_TRUNCATE|VACUUM_INDEX_CLEANUP)"
)


def _set_parameters(facts: Facts, action: Action) -> Outcome:
    light = all(_LIGHT_PARAMETERS_RE.fullmatch(key.upper()) for key in action.options)
    if light:
        return Outcome.of(
            Effect(
                TABLE_PARAMETERS,
                common.table(facts),
                SHARE_UPDATE_EXCLUSIVE,
                Work.CATALOG,
            )
        )
    return Outcome.of(
        Effect(TABLE_CATALOG, common.table(facts), ACCESS_EXCLUSIVE, Work.CATALOG)
    )
