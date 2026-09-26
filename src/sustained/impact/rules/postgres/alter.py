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
)
from sustained.impact.rules.postgres.locks import (
    ACCESS_EXCLUSIVE,
    SHARE_ROW_EXCLUSIVE,
    SHARE_UPDATE_EXCLUSIVE,
)
from sustained.impact.rules.postgres.remedies import (
    TRANSACTION_NOTE,
    ident,
    key_columns,
    last_part,
    trimmed,
)
from sustained.impact.rules.postgres.statements import (
    foreign_key_effect,
)
from sustained.impact.tokens import tokenize

ActionHandler = Callable[[Facts, Action], Outcome]


def simple(rule: Rule, lock: str, work: Work) -> ActionHandler:
    def handler(facts: Facts, action: Action) -> Outcome:
        return Outcome((Effect(rule, common.table(facts), lock, work),))

    return handler


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
        function = options.get("default_function")
        rewrite_reason = (
            f"the default calls {function}(), which gives each row a new value"
        )
        if not options.get("default_certain", True):
            confidence = Confidence.LIKELY
            rewrite_reason = (
                f"the default calls {function}(), which no rule knows, so it "
                "counts as volatile: a new value for each row"
            )
    effects: List[Effect] = []
    if rewrite_reason is not None:
        remedy: Tuple[str, ...] = ()
        default = options.get("default")
        if volatility == "volatile" and default:
            remedy = (
                f"ALTER TABLE {ident(table)} ADD COLUMN {ident(column)} "
                f"{options.get('type')}",
                f"ALTER TABLE {ident(table)} ALTER COLUMN {ident(column)} "
                f"SET DEFAULT {default}",
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
    run added or the schema read holds, proves the column has no NULL.
    pg_get_constraintdef() writes NOT VALID after a check not yet
    validated, so a check read with it proves nothing.
    """
    if facts.state.proves_not_null(table, column):
        return True
    found = facts.context.table(facts.state.original(table))
    if found is None:
        return False
    for expression in found.checks.values():
        tested = not_null_column(tokenize(expression, Dialects.POSTGRES))
        if tested is not None and tested.lower() == column.lower():
            return True
    return False


def _set_not_null(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    column = action.column or "?"
    if _proven_not_null(facts, table, column):
        return Outcome(
            (Effect(SET_NOT_NULL_PROVEN, table, ACCESS_EXCLUSIVE, Work.CATALOG),)
        )
    check = f"{last_part(table)}_{column}_not_null"[:63]
    t, c, k = ident(table), ident(column), ident(check)
    remedy = (
        f"ALTER TABLE {t} ADD CONSTRAINT {k} CHECK ({c} IS NOT NULL) NOT VALID",
        f"ALTER TABLE {t} VALIDATE CONSTRAINT {k}",
        f"ALTER TABLE {t} ALTER COLUMN {c} SET NOT NULL",
        f"ALTER TABLE {t} DROP CONSTRAINT {k}",
    )
    return Outcome(
        (
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
    return Outcome(
        (
            Effect(
                ADD_EXCLUSION, common.table(facts), ACCESS_EXCLUSIVE, Work.INDEX_BUILD
            ),
        )
    )


def _validate_later(facts: Facts, action: Action) -> Tuple[str, ...]:
    name = action.options.get("name")
    if not name:
        return ()
    return (
        trimmed(facts.statement) + " NOT VALID",
        f"ALTER TABLE {ident(common.table(facts))} VALIDATE CONSTRAINT {ident(str(name))}",
    )


def _add_check(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    if action.options.get("not_valid"):
        return Outcome(
            (Effect(ADD_CHECK_NOT_VALID, table, ACCESS_EXCLUSIVE, Work.CATALOG),)
        )
    return Outcome(
        (
            Effect(
                ADD_CHECK,
                table,
                ACCESS_EXCLUSIVE,
                Work.SCAN,
                message=f"reads and writes on {table} wait while every row is "
                "checked; add the check NOT VALID, then VALIDATE CONSTRAINT in a "
                "later migration, which lets reads and writes go on",
                remedy=_validate_later(facts, action),
            ),
        )
    )


def _add_foreign_key(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    referenced = str(action.options.get("references"))
    if action.options.get("not_valid"):
        return Outcome(
            tuple(
                Effect(
                    ADD_FOREIGN_KEY_NOT_VALID, name, SHARE_ROW_EXCLUSIVE, Work.CATALOG
                )
                for name in (table, referenced)
            )
        )
    return Outcome(
        (
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
    )


def _add_key(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    options = action.options
    if options.get("using_index"):
        return Outcome(
            (Effect(ADD_KEY_USING_INDEX, table, ACCESS_EXCLUSIVE, Work.CATALOG),)
        )
    primary = options.get("constraint") == "primary_key"
    columns = key_columns(facts.statement)
    remedy: Tuple[str, ...] = ()
    if columns is not None:
        name = str(
            options.get("name") or f"{last_part(table)}_{'pkey' if primary else 'key'}"
        )
        index = f"{name}_idx"[:63]
        kind = "PRIMARY KEY" if primary else "UNIQUE"
        remedy = (
            f"CREATE UNIQUE INDEX CONCURRENTLY {ident(index)} ON {ident(table)} "
            f"{columns}",
            f"ALTER TABLE {ident(table)} ADD CONSTRAINT {ident(name)} {kind} "
            f"USING INDEX {ident(index)}",
        )
    return Outcome(
        (
            Effect(
                ADD_KEY,
                table,
                ACCESS_EXCLUSIVE,
                Work.INDEX_BUILD,
                message=f"reads and writes on {table} wait for the whole index "
                f"build; build a unique index CONCURRENTLY {TRANSACTION_NOTE}, "
                "then add the constraint USING INDEX",
                remedy=remedy,
            ),
        )
    )


def _rename(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    if action.kind == "rename_constraint":
        return Outcome((Effect(RENAME, table, ACCESS_EXCLUSIVE, Work.CATALOG),))
    what = "column" if action.kind == "rename_column" else "table"
    old = action.column if action.kind == "rename_column" else table
    note = Finding(
        RENAME.id,
        Severity.INFO,
        f"running application code that names the {what} {old} fails once "
        "the rename commits",
        source=RENAME.source,
    )
    return Outcome(
        (Effect(RENAME, table, ACCESS_EXCLUSIVE, Work.CATALOG, notes=(note,)),)
    )


def _attach_partition(facts: Facts, action: Action) -> Outcome:
    partition = str(action.options.get("partition"))
    return Outcome(
        (
            Effect(
                ATTACH_PARTITION,
                common.table(facts),
                SHARE_UPDATE_EXCLUSIVE,
                Work.CATALOG,
            ),
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
        ),
        confidence=Confidence.LIKELY,
    )


def _detach_partition(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    partition = str(action.options.get("partition"))
    if action.options.get("concurrently"):
        findings: Tuple[Finding, ...] = ()
        if not DETACH_PARTITION_CONCURRENTLY.versions(facts.context.version):
            findings = (
                _needs_version(facts.context, DETACH_PARTITION_CONCURRENTLY, (14,)),
            )
        return Outcome(
            (
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
            ),
            findings,
        )
    return Outcome(
        (
            Effect(DETACH_PARTITION, table, ACCESS_EXCLUSIVE, Work.CATALOG),
            Effect(DETACH_PARTITION, partition, ACCESS_EXCLUSIVE, Work.CATALOG),
        )
    )


def _needs_version(
    context: EngineContext, rule: Rule, version: Tuple[int, ...]
) -> Finding:
    from sustained.impact.context import version_text

    assumed = "" if "version" in context.read else "assumed "
    return Finding(
        rule.id,
        Severity.WARN,
        f"this needs PostgreSQL {version_text(version)} or later; the {assumed}"
        f"server version is {version_text(context.version)}",
        source=rule.source,
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
        return Outcome(
            (
                Effect(
                    TABLE_PARAMETERS,
                    common.table(facts),
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.CATALOG,
                ),
            )
        )
    return Outcome(
        (Effect(TABLE_CATALOG, common.table(facts), ACCESS_EXCLUSIVE, Work.CATALOG),)
    )
