"""
The handlers for each SQL Server ALTER TABLE action, and the column type
reading ALTER COLUMN needs.

Every ALTER TABLE takes Sch-M on the table, which blocks reads and
writes. A foreign key takes Sch-M on the table it points at too. What
differs between actions is the work done under that lock.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple

from sustained.impact.model import Action, Confidence, Finding, Severity, Work
from sustained.impact.rules import Effect, Facts, Outcome, Rule, common
from sustained.impact.rules.mssql.catalog import (
    ADD_CHECK,
    ADD_CHECK_NOCHECK,
    ADD_COLUMN,
    ADD_COLUMN_DEFAULT,
    ADD_COLUMN_REWRITE,
    ADD_FOREIGN_KEY,
    ADD_FOREIGN_KEY_NOCHECK,
    ADD_KEY,
    ADD_KEY_ONLINE,
    ALTER_COLUMN,
    ALTER_COLUMN_METADATA,
    ALTER_COLUMN_ONLINE,
    CHECK_CONSTRAINT,
    CONSTRAINT_STATE,
    DEFAULT,
    DROP_COLUMN,
    DROP_CONSTRAINT,
    REBUILD,
    RENAME,
    RESUMABLE,
    SET_NOT_NULL,
    SWITCH,
    TRIGGER,
)
from sustained.impact.rules.mssql.facts import (
    ENTERPRISE_EDITIONS,
    LOW_PRIORITY,
    is_online,
    online_effect,
    online_findings,
    queues,
    resumable_findings,
    unread_type,
    with_options,
)
from sustained.impact.rules.mssql.locks import SCH_M, enterprise

# The types whose values vary in length, where a longer declared length
# changes only the catalog.
_VARIABLE = frozenset({"varchar", "nvarchar", "varbinary"})

# The types whose default ADD COLUMN writes into every row on every
# edition, as the large value types (max) do.
_WRITTEN = frozenset(
    {"xml", "text", "ntext", "image", "hierarchyid", "geography", "geometry", "json"}
)

# The types a rowversion column spells, whose value SQL Server writes
# into every row whether or not the column has a default.
_ROWVERSION = frozenset({"rowversion", "timestamp"})

# The system type names, with the synonyms the grammar takes. Any other
# name is an alias type or a CLR type, which the schema read does not
# tell apart.
_SYSTEM = (
    frozenset(
        {
            "bigint",
            "int",
            "integer",
            "smallint",
            "tinyint",
            "bit",
            "decimal",
            "dec",
            "numeric",
            "money",
            "smallmoney",
            "float",
            "real",
            "double precision",
            "date",
            "time",
            "datetime",
            "datetime2",
            "datetimeoffset",
            "smalldatetime",
            "char",
            "character",
            "nchar",
            "national char",
            "national character",
            "char varying",
            "character varying",
            "national char varying",
            "national character varying",
            "binary",
            "binary varying",
            "uniqueidentifier",
            "sql_variant",
            "sysname",
            "vector",
        }
    )
    | _VARIABLE
    | _WRITTEN
    | _ROWVERSION
)


class ColumnType(NamedTuple):
    """A column type read from its text: the base name and its arguments."""

    base: str
    arguments: Tuple[str, ...] = ()

    @property
    def length(self) -> Optional[int]:
        """The declared length, -1 for max, or None without one."""
        if not self.arguments:
            return None
        first = self.arguments[0].lower()
        if first in ("max", "-1"):
            return -1
        return int(first) if first.isdigit() else None


def column_type(text: str) -> ColumnType:
    """A type such as `NVARCHAR(50)` or `[dbo].[int]` as a ColumnType."""
    match = re.match(r"\s*\[?([A-Za-z_][\w ]*?)\]?\s*(?:\((.*)\))?\s*$", text)
    if match is None:
        return ColumnType(text.strip().lower())
    base = " ".join(match.group(1).lower().split())
    arguments = tuple(a.strip() for a in (match.group(2) or "").split(",") if a.strip())
    return ColumnType(base, arguments)


def _effect(
    rule: Rule,
    table: str,
    work: Work,
    confidence: Confidence = Confidence.KNOWN,
    message: Optional[str] = None,
    remedy: Tuple[str, ...] = (),
    notes: Tuple[Finding, ...] = (),
) -> Effect:
    return Effect(rule, table, SCH_M, work, confidence, message, remedy, notes)


def _add_column(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    options = action.options
    generated = options.get("generated")
    if options.get("identity") or generated == "stored":
        return Outcome.of(
            _effect(
                ADD_COLUMN_REWRITE,
                table,
                Work.REWRITE,
                message=f"adding the column writes a value into every row of "
                f"{table}",
            )
        )
    if generated:
        return Outcome.of(_effect(ADD_COLUMN, table, Work.CATALOG))
    kind = column_type(str(options.get("type", "")))
    if kind.base in _ROWVERSION:
        return Outcome.of(
            _effect(
                ADD_COLUMN_REWRITE,
                table,
                Work.REWRITE,
                message=f"adding a {kind.base} column writes a value into every "
                f"row of {table}",
            )
        )
    default = options.get("default")
    fills = default is not None and (
        options.get("not_null") or options.get("with_values")
    )
    if not fills:
        notes: Tuple[Finding, ...] = ()
        if default is None and options.get("not_null"):
            notes = (
                ADD_COLUMN.finding(
                    Severity.WARN,
                    f"the server refuses a NOT NULL column with no DEFAULT unless "
                    f"{table} has no rows; give the column a DEFAULT, or add it "
                    "NULL, backfill it, then make it NOT NULL",
                ),
            )
        return Outcome.of(_effect(ADD_COLUMN, table, Work.CATALOG, notes=notes))
    written = _written(options.get("type"), table)
    if written is not None:
        message, confidence = written
        return Outcome.of(
            _effect(
                ADD_COLUMN_REWRITE,
                table,
                Work.REWRITE,
                confidence,
                message=message,
                remedy=(
                    f"add the column as NULL without a default, backfill {table} "
                    "in batches, then make it NOT NULL",
                ),
            )
        )
    if options.get("default_volatility") == "volatile":
        function = options.get("default_function")
        gives = (
            f"{function} gives each row its own value"
            if function
            else "the default was not read, so it counts as giving each row its "
            "own value"
        )
        _, confidence = common.volatile_default(options)
        return Outcome.of(
            _effect(
                ADD_COLUMN_REWRITE,
                table,
                Work.REWRITE,
                confidence,
                message=f"{gives}, so adding the column writes every row of "
                f"{table}",
                remedy=(
                    f"add the column as NULL without a default, backfill {table} "
                    "in batches, then make it NOT NULL",
                ),
            )
        )
    allowed = enterprise(facts.context)
    if allowed:
        return Outcome.of(_effect(ADD_COLUMN_DEFAULT, table, Work.CATALOG))
    if allowed is None:
        return Outcome.of(
            _effect(
                ADD_COLUMN_DEFAULT,
                table,
                Work.REWRITE,
                Confidence.LIKELY,
                message=f"on the {ENTERPRISE_EDITIONS} editions this changes only "
                f"the catalog; on the others it writes the default into every row "
                f"of {table}; the edition was not read",
            )
        )
    return Outcome.of(
        _effect(
            ADD_COLUMN_DEFAULT,
            table,
            Work.REWRITE,
            message=f"{facts.context.edition} writes the default into every row "
            f"of {table}; the {ENTERPRISE_EDITIONS} editions change only the "
            "catalog",
            remedy=(
                f"add the column as NULL without a default, backfill {table} in "
                "batches, then add the default and make it NOT NULL",
            ),
        )
    )


def _written(text: object, table: str) -> Optional[Tuple[str, Confidence]]:
    """
    The message and confidence for a default that ADD COLUMN writes into
    every row on every edition because of the column's type: a large
    value type, xml, a spatial type, hierarchyid, json, the deprecated
    text, ntext, and image, and a CLR type. A type name that is not a
    system type may be a CLR type, and so may a type that was not read.
    None for a system type whose default can change only the catalog.
    """
    if not text:
        return (
            f"a default is written into every row of {table} when the column's "
            "type is a large value type or a CLR type; the type was not read",
            Confidence.LIKELY,
        )
    kind = column_type(str(text))
    if kind.base in _WRITTEN or (kind.base in _VARIABLE and kind.length == -1):
        return (
            f"a default of type {text} is written into every row of {table}, on "
            "every edition",
            Confidence.KNOWN,
        )
    if kind.base not in _SYSTEM:
        return (
            f"a default of type {text} is written into every row of {table} when "
            f"{text} is a CLR type, which was not read",
            Confidence.LIKELY,
        )
    return None


def _drop_column(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    note = DROP_COLUMN.finding(
        Severity.INFO,
        f"DROP COLUMN leaves the column's space in each row of {table} until the "
        "table or its clustered index is rebuilt",
    )
    return Outcome.of(_effect(DROP_COLUMN, table, Work.CATALOG, notes=(note,)))


class _Column(NamedTuple):
    """What the handler knows of a column before the statement runs."""

    type: Optional[ColumnType]
    nullable: Optional[bool]


def _current(facts: Facts, table: str, column: str) -> _Column:
    """
    The column's type and nullability before the statement: from the
    intent the diff attached, or from the schema read.
    """
    intent = facts.intent
    nullable: Optional[bool] = None
    text: Optional[str] = None
    if intent is not None and intent.column and intent.column.lower() == column.lower():
        found = intent.get("from_type")
        text = str(found) if found else None
        if intent.kind == "set_not_null":
            nullable = True
        elif intent.kind == "drop_not_null":
            nullable = False
    spec = None
    read = facts.context.table(facts.state.original(table))
    if read is not None:
        for name, candidate in read.columns.items():
            if name.lower() == column.lower():
                spec = candidate
    if spec is not None:
        text = text or spec.raw_type
        nullable = spec.nullable if nullable is None else nullable
    return _Column(None if text is None else column_type(text), nullable)


def _alter_column(facts: Facts, action: Action) -> Outcome:
    """
    ALTER COLUMN restates the column's type and nullability. The work is
    a catalog change for a longer variable length, for dropping NOT
    NULL, or for restating the column as it is; a scan for adding NOT
    NULL to a fixed-length column; and an update of every row for any
    other change, including NOT NULL on a variable-length column.
    """
    table = common.table(facts)
    column = str(action.column)
    new = column_type(str(action.options.get("type", "")))
    not_null = bool(action.options.get("not_null"))
    current = _current(facts, table, column)
    rule, work, confidence, note = _column_change(current, new, not_null)
    message = None
    if work > Work.CATALOG:
        verb = "reads" if work is Work.SCAN else "updates"
        message = f"ALTER COLUMN {verb} every row of {table}"
        if note:
            message += f"; {note}"
    if is_online(action.options):
        return _online_column(facts, table, work, confidence, message, action.options)
    remedy: Tuple[str, ...] = ()
    if work is Work.REWRITE and enterprise(facts.context) is not False:
        if facts.context.version >= (13,):
            remedy = (f"{facts.statement} WITH (ONLINE = ON)",)
    effect = _effect(rule, table, work, confidence, message, remedy)
    return Outcome.of(effect)


def _column_change(
    current: _Column, new: ColumnType, not_null: bool
) -> Tuple[Rule, Work, Confidence, Optional[str]]:
    """The rule, work, confidence, and any caveat of an ALTER COLUMN."""
    old = current.type
    if old is None:
        return (
            ALTER_COLUMN,
            Work.REWRITE,
            Confidence.LIKELY,
            "the current type was not read",
        )
    same_type = old == new
    widened = (
        old.base == new.base
        and old.base in _VARIABLE
        and old.length is not None
        and new.length is not None
        and old.length != -1
        and new.length != -1
        and new.length >= old.length
    ) or (same_type and old.base in _VARIABLE)
    if not (same_type or widened):
        confidence = Confidence.KNOWN
        if old.base == new.base:
            # A precision or scale change of the same type may fit the
            # same storage, which the rules do not work out.
            confidence = Confidence.LIKELY
        return ALTER_COLUMN, Work.REWRITE, confidence, None
    if not not_null or current.nullable is False:
        return ALTER_COLUMN_METADATA, Work.CATALOG, Confidence.KNOWN, None
    if current.nullable is None:
        return (
            SET_NOT_NULL,
            Work.REWRITE if new.base in _VARIABLE else Work.SCAN,
            Confidence.LIKELY,
            "whether the column is already NOT NULL was not read",
        )
    if new.base in _VARIABLE:
        return SET_NOT_NULL, Work.REWRITE, Confidence.KNOWN, None
    return SET_NOT_NULL, Work.SCAN, Confidence.KNOWN, None


def _online_column(
    facts: Facts,
    table: str,
    work: Work,
    confidence: Confidence,
    message: Optional[str],
    options: Mapping[str, object],
) -> Outcome:
    findings = online_findings(facts, ALTER_COLUMN_ONLINE)
    if not ALTER_COLUMN_ONLINE.versions(facts.context.version):
        findings += (
            ALTER_COLUMN_ONLINE.finding(
                Severity.DANGER,
                "ALTER COLUMN with ONLINE = ON needs SQL Server 2016 or later, so "
                "the statement fails on this server",
            ),
        )
    # The online form builds the table again beside the old one.
    work = Work.REWRITE if work > Work.CATALOG else Work.CATALOG
    effect = online_effect(
        facts, ALTER_COLUMN_ONLINE, table, SCH_M, work, "ALTER COLUMN", options
    )
    return Outcome.of(effect._replace(confidence=confidence), findings=findings)


def _add_constraint(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    kind = action.options.get("constraint")
    nocheck = bool(facts.parsed.options.get("nocheck"))
    if kind == "check":
        return _add_checked(facts, table, ADD_CHECK, ADD_CHECK_NOCHECK, nocheck, None)
    if kind == "foreign_key":
        target = action.options.get("references")
        return _add_checked(
            facts,
            table,
            ADD_FOREIGN_KEY,
            ADD_FOREIGN_KEY_NOCHECK,
            nocheck,
            None if target is None else str(target),
        )
    if kind in ("primary_key", "unique"):
        return _add_key(facts, table, action)
    return common.unknown(facts, f"an ADD CONSTRAINT of kind {kind}")


def _untrusted(rule: Rule, table: str, name: object) -> Finding:
    return rule.finding(
        Severity.INFO,
        f"WITH NOCHECK leaves the constraint untrusted, so the optimizer does not "
        f"rely on it; ALTER TABLE {table} WITH CHECK CHECK CONSTRAINT {name} checks "
        "every row later, under the same Sch-M lock",
    )


def _add_checked(
    facts: Facts,
    table: str,
    checked: Rule,
    unchecked: Rule,
    nocheck: bool,
    target: Optional[str],
) -> Outcome:
    """
    A CHECK or FOREIGN KEY constraint: WITH NOCHECK changes only the
    catalog and leaves the constraint untrusted; otherwise SQL Server
    reads every row under Sch-M. A foreign key also takes Sch-M on the
    table it points at.
    """
    effects: List[Effect] = []
    name = facts.parsed.actions[0].options.get("name") or "the constraint"
    if nocheck:
        effects.append(
            _effect(
                unchecked,
                table,
                Work.CATALOG,
                notes=(_untrusted(unchecked, table, name),),
            )
        )
    else:
        match = re.match(r"(?is)\s*ALTER\s+TABLE\s+(\S+)\s+(ADD\s.*)", facts.statement)
        remedy: Tuple[str, ...] = ()
        if match is not None:
            remedy = (f"ALTER TABLE {match.group(1)} WITH NOCHECK {match.group(2)}",)
        effects.append(
            _effect(
                checked,
                table,
                Work.SCAN,
                message=f"reads and writes on {table} wait while every row is checked",
                remedy=remedy,
            )
        )
    if target is not None:
        effects.append(_effect(effects[0].rule, target, Work.CATALOG))
    return Outcome(tuple(effects))


def _add_key(facts: Facts, table: str, action: Action) -> Outcome:
    """
    A PRIMARY KEY or UNIQUE constraint builds its index under Sch-M. A
    clustered one on a heap copies the table into the new index, and
    builds its other indexes again. A primary key is clustered unless it
    says NONCLUSTERED or the table already has a clustered index.
    """
    options = action.options
    clustered = options.get("clustered")
    confidence = Confidence.KNOWN
    if clustered is None and options.get("constraint") == "primary_key":
        stats = facts.context.stats(facts.state.original(table))
        if stats.heap is None:
            confidence = Confidence.LIKELY
        clustered = stats.heap is not False
    work = Work.REWRITE if clustered else Work.INDEX_BUILD
    refused = resumable_findings(facts, RESUMABLE, options)
    if is_online(options):
        what = "the key's index build"
        effect = online_effect(facts, ADD_KEY_ONLINE, table, SCH_M, work, what, options)
        return Outcome.of(
            effect._replace(confidence=confidence),
            findings=online_findings(facts, ADD_KEY_ONLINE) + refused,
        )
    remedy: Tuple[str, ...] = ()
    if enterprise(facts.context) is not False:
        remedy = (f"{facts.statement} WITH (ONLINE = ON)",)
    verb = "copies every row of" if clustered else "builds an index over"
    message = f"reads and writes on {table} wait while the key {verb} {table}"
    return Outcome.of(
        _effect(ADD_KEY, table, work, confidence, message, remedy), findings=refused
    )


def _drop_constraint(facts: Facts, action: Action) -> Outcome:
    """
    Dropping a constraint changes the catalog under Sch-M, and a foreign
    key takes Sch-M on the table it points at too. Dropping the key
    behind the clustered index turns the table into a heap, which
    copies every row.
    """
    table = common.table(facts)
    name = action.options.get("name")
    effects: List[Effect] = []
    stats = facts.context.stats(facts.state.original(table))
    if (
        name is not None
        and stats.clustered
        and stats.clustered.lower() == str(name).lower()
    ):
        effects.append(
            _effect(
                DROP_CONSTRAINT,
                table,
                Work.REWRITE,
                message=f"dropping the clustered key copies every row of {table} "
                "into a heap",
            )
        )
    else:
        effects.append(_effect(DROP_CONSTRAINT, table, Work.CATALOG))
    if name is not None:
        target = facts.context.foreign_key_target(
            facts.state.original(table), str(name)
        )
        if target is not None:
            effects.append(_effect(DROP_CONSTRAINT, target, Work.CATALOG))
    return Outcome(tuple(effects))


def _check_constraint(facts: Facts, action: Action) -> Outcome:
    """
    `WITH CHECK CHECK CONSTRAINT` reads every row under Sch-M; enabling
    or disabling a constraint without it changes only the catalog. A
    foreign key takes Sch-M on the table it points at too.
    """
    table = common.table(facts)
    name = str(action.options.get("name"))
    checks = (
        action.kind == "enable_constraint"
        and facts.parsed.options.get("nocheck") is False
    )
    effects: List[Effect] = []
    if checks:
        effects.append(
            _effect(
                CHECK_CONSTRAINT,
                table,
                Work.SCAN,
                message=f"reads and writes on {table} wait while every row is checked",
            )
        )
    else:
        effects.append(_effect(CONSTRAINT_STATE, table, Work.CATALOG))
    target = None
    if name.upper() != "ALL":
        target = facts.context.foreign_key_target(facts.state.original(table), name)
    if target is not None:
        effects.append(_effect(effects[0].rule, target, Work.CATALOG))
    return Outcome(tuple(effects))


def _rename(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    if action.kind == "rename_index":
        note = common.rename_note(RENAME, "index", action.options.get("old"))
    else:
        note = common.rename_note(RENAME, "column", action.column)
    return Outcome.of(_effect(RENAME, table, Work.CATALOG, notes=(note,)))


def _rebuild(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    if is_online(action.options):
        effect = online_effect(
            facts, REBUILD, table, SCH_M, Work.REWRITE, "REBUILD", action.options, (12,)
        )
        return Outcome.of(effect, findings=online_findings(facts, REBUILD))
    remedy: Tuple[str, ...] = ()
    if enterprise(facts.context) is not False:
        remedy = (f"ALTER TABLE {table} REBUILD WITH (ONLINE = ON)",)
    message = f"reads and writes on {table} wait while REBUILD copies every row"
    return Outcome.of(
        _effect(REBUILD, table, Work.REWRITE, message=message, remedy=remedy)
    )


def _switch(facts: Facts, action: Action) -> Outcome:
    table = common.table(facts)
    target = str(action.options.get("target"))
    remedy: Tuple[str, ...] = ()
    options = with_options(action.options)
    if "WAIT_AT_LOW_PRIORITY" not in options and facts.context.version >= (12,):
        remedy = (f"{facts.statement} WITH ({LOW_PRIORITY})",)
    # With WAIT_AT_LOW_PRIORITY and ABORT_AFTER_WAIT = SELF or BLOCKERS,
    # other sessions do not queue behind the SWITCH.
    waits = queues(action.options)
    effect = _effect(SWITCH, table, Work.CATALOG, remedy=remedy)._replace(waits=waits)
    other = _effect(SWITCH, target, Work.CATALOG)._replace(waits=waits)
    return Outcome((effect, other))


ACTIONS: Dict[str, common.ActionHandler] = {
    "add_column": _add_column,
    "drop_column": _drop_column,
    "alter_column": _alter_column,
    "add_constraint": _add_constraint,
    "drop_constraint": _drop_constraint,
    "enable_constraint": _check_constraint,
    "disable_constraint": _check_constraint,
    "set_default": common.fixed_action(DEFAULT, SCH_M, Work.CATALOG),
    "drop_default": common.fixed_action(DEFAULT, SCH_M, Work.CATALOG),
    "rename_column": _rename,
    "rename_index": _rename,
    "rebuild": _rebuild,
    "switch": _switch,
    "enable_trigger": common.fixed_action(TRIGGER, SCH_M, Work.CATALOG),
    "disable_trigger": common.fixed_action(TRIGGER, SCH_M, Work.CATALOG),
}


def alter_table(facts: Facts) -> Outcome:
    """Each action's outcome, together."""
    return common.each_action(facts, common.by_kind(ACTIONS))
