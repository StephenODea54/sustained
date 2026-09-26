"""
The handlers for each MySQL and MariaDB statement kind.
"""

from __future__ import annotations

from typing import (
    Dict,
    List,
    Sequence,
)

from sustained.impact.model import (
    Action,
    Blocks,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, common
from sustained.impact.rules.mysql.actions import (
    ACTIONS,
    _drop_constraint,
    drop_index_change,
    index_change,
)
from sustained.impact.rules.mysql.columns import (
    heaviest,
)
from sustained.impact.rules.mysql.facts import (
    Change,
    copy_algorithm,
    is_mariadb,
    rename_note,
    rules_for,
    table_stats,
)
from sustained.impact.rules.mysql.locks import (
    MDL_EXCLUSIVE,
    ROW_LOCKS,
    Online,
)
from sustained.impact.rules.mysql.online import (
    blocking_message,
    online_outcome,
    parent_effect,
    unique_names,
)


def _alter_table(facts: Facts) -> Outcome:
    changes: List[Change] = []
    actions = facts.parsed.actions
    kinds = {(a.kind, a.options.get("constraint")) for a in actions}
    swaps_primary_key = ("add_constraint", "primary_key") in kinds and (
        ("drop_constraint", "primary_key") in kinds
    )
    for action in actions:
        handler = ACTIONS.get(action.kind)
        if handler is None:
            return common.unknown(facts, f"the ALTER TABLE action {action.kind}")
        if swaps_primary_key and action.kind == "drop_constraint":
            # Dropping the primary key and adding another in the same
            # statement rebuilds the table in place.
            continue
        changes.append(handler(facts, action))
    if not changes:
        return common.unknown(facts, f"the ALTER TABLE action {actions[0].kind}")
    return online_outcome(facts, heaviest(changes))


def _create_index(facts: Facts) -> Outcome:
    return online_outcome(
        facts, index_change(facts, bool(facts.parsed.options.get("fulltext")))
    )


def _drop_index(facts: Facts) -> Outcome:
    name = str(facts.parsed.options.get("name") or "")
    if name.upper() == "PRIMARY":
        change = _drop_constraint(
            facts, Action("drop_constraint", None, {"name": "PRIMARY"})
        )
    else:
        change = drop_index_change(facts)
    return online_outcome(facts, change)


def _metadata(facts: Facts, rule_name: str, tables: Sequence[str]) -> Outcome:
    rule = rules_for(facts)[rule_name]
    return Outcome(
        tuple(Effect(rule, table, MDL_EXCLUSIVE, Work.CATALOG) for table in tables)
    )


def _drop_table(facts: Facts) -> Outcome:
    named = common.tables(facts)
    outcome = _metadata(facts, "drop_table", named)
    if is_mariadb(facts) or facts.parsed.kind != "drop_table":
        return outcome
    rule = rules_for(facts)["foreign_key_parent"]
    parents: List[Effect] = []
    seen = {t.lower() for t in named}
    for table in named:
        for parent in facts.context.references(facts.state.original(table)):
            if parent.lower() not in seen:
                seen.add(parent.lower())
                parents.append(parent_effect(rule, table, parent))
    return outcome._replace(effects=outcome.effects + tuple(parents))


def _rename_table(facts: Facts) -> Outcome:
    rule = rules_for(facts)["rename"]
    effects = []
    for old, _ in facts.parsed.items("renames"):
        note = Finding(
            rule.id,
            Severity.INFO,
            rename_note("table", str(old)),
            source=rule.source,
        )
        effects.append(
            Effect(rule, str(old), MDL_EXCLUSIVE, Work.CATALOG, notes=(note,))
        )
    return Outcome(tuple(effects))


def _create_table(facts: Facts) -> Outcome:
    if is_mariadb(facts):
        return Outcome()
    rule = rules_for(facts)["foreign_key_parent"]
    table = common.table(facts)
    return Outcome(
        tuple(
            parent_effect(rule, table, str(parent))
            for parent in unique_names(
                [str(p) for p in facts.parsed.items("references")], table
            )
        )
    )


def _optimize(facts: Facts) -> Outcome:
    rule = rules_for(facts)["table_rebuild"]
    effects = []
    for table in common.tables(facts):
        if table_stats(facts, table).fulltext:
            online = copy_algorithm(facts)
        else:
            online = Online("INPLACE", "NONE")
        effects.append(
            Effect(
                rule,
                table,
                online.label,
                Work.REWRITE,
                message=blocking_message(
                    facts,
                    table,
                    online,
                    Work.REWRITE,
                    "OPTIMIZE TABLE rebuilds an InnoDB table",
                ),
            )
        )
    return Outcome(tuple(effects))


def _write_rows(facts: Facts) -> Outcome:
    rule = rules_for(facts)["write_rows"]
    table = common.table(facts)
    message = common.row_write_message(
        facts,
        table,
        ", and InnoDB locks every row the statement reads when no index narrows "
        "the WHERE clause",
    )
    return Outcome(
        (
            Effect(
                rule,
                table,
                ROW_LOCKS,
                Work.ROWS,
                message=message,
                blocks=Blocks.WRITES,
            ),
        )
    )


def _insert(facts: Facts) -> Outcome:
    rule = rules_for(facts)["insert"]
    table = common.table(facts)
    return Outcome((Effect(rule, table, ROW_LOCKS, Work.ROWS),))


def _trigger(facts: Facts) -> Outcome:
    table = facts.parsed.table
    if table is None:
        # DROP TRIGGER names no table, and the schema read on MySQL
        # reads no triggers.
        return Outcome(confidence=Confidence.LIKELY)
    return _metadata(facts, "trigger", [table])


def _drop_view(facts: Facts) -> Outcome:
    return _metadata(facts, "drop_view", common.tables(facts))


STATEMENTS: Dict[str, common.Handler] = {
    "alter_table": _alter_table,
    "create_index": _create_index,
    "drop_index": _drop_index,
    "create_table": _create_table,
    "drop_table": _drop_table,
    "truncate": _drop_table,
    "rename_table": _rename_table,
    "optimize_table": _optimize,
    "update": _write_rows,
    "delete": _write_rows,
    "insert": _insert,
    "create_trigger": _trigger,
    "drop_trigger": _trigger,
    "drop_view": _drop_view,
    "create_view": common.nothing,
    "create_object": common.nothing,
    "drop_object": common.nothing,
    "set": common.nothing,
}
