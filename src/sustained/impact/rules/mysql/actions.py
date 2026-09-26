"""
The ALTER TABLE actions other than the column changes, and the table
that maps each action kind to its handler.
"""

from __future__ import annotations

from typing import (
    Callable,
    Dict,
    Optional,
)

from sustained.impact.model import (
    Action,
    Confidence,
    Work,
)
from sustained.impact.rules import Facts, common
from sustained.impact.rules.mysql.columns import (
    _add_column,
    _change_column,
    _drop_column,
    _modify_column,
    _rename_column,
)
from sustained.impact.rules.mysql.facts import (
    Change,
    copy_algorithm,
    foreign_key_checks_off,
    is_mariadb,
    nocopy_algorithm,
    rename_note,
    table_stats,
)
from sustained.impact.rules.mysql.locks import (
    Online,
)


def _fixed(
    online: Callable[[Facts], Online], work: Work, rule: str, reason: str
) -> Callable[[Facts, Action], Change]:
    def handler(facts: Facts, action: Action) -> Change:
        return Change(online(facts), work, rule, reason)

    return handler


def _instant(facts: Facts) -> Online:
    return Online("INSTANT")


def _inplace(facts: Facts) -> Online:
    return Online("INPLACE", "NONE")


def _instant_on_mariadb(facts: Facts) -> Online:
    return Online("INSTANT") if is_mariadb(facts) else Online("INPLACE", "NONE")


def _rename_to(facts: Facts, action: Action) -> Change:
    return Change(
        Online("INSTANT"),
        Work.CATALOG,
        "rename",
        "the table is renamed in the data dictionary",
        notes=(rename_note("table", common.table(facts)),),
    )


def _rename_action(facts: Facts, action: Action) -> Change:
    return _rename_column(facts, action)


def _add_index(facts: Facts, action: Action) -> Change:
    return index_change(facts, bool(action.options.get("fulltext")))


def index_change(facts: Facts, fulltext: bool) -> Change:
    if not fulltext:
        return Change(
            nocopy_algorithm(facts),
            Work.INDEX_BUILD,
            "add_index",
            "the index is built while reads and writes go on",
        )
    table = common.table(facts)
    existing = table_stats(facts, table).fulltext
    if existing:
        # The table has its FTS_DOC_ID column already, so MariaDB builds
        # the index without touching the table's rows.
        return Change(
            Online("NOCOPY" if is_mariadb(facts) else "INPLACE", "SHARED"),
            Work.INDEX_BUILD,
            "add_fulltext",
            "a FULLTEXT index is built while writes wait",
        )
    return Change(
        Online("INPLACE", "SHARED"),
        Work.REWRITE,
        "add_fulltext",
        "the table's first FULLTEXT index adds a hidden FTS_DOC_ID column, "
        "which rebuilds the table while writes wait",
        Confidence.KNOWN if existing is not None else Confidence.LIKELY,
    )


def _add_constraint(facts: Facts, action: Action) -> Change:
    options = action.options
    constraint = options.get("constraint")
    if constraint == "primary_key":
        return Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "add_primary_key",
            "a primary key orders the rows, so the table is rebuilt in place",
        )
    if constraint == "unique":
        return index_change(facts, False)
    if constraint == "foreign_key":
        return _add_foreign_key(facts, action)
    if constraint == "check":
        return Change(
            copy_algorithm(facts),
            Work.REWRITE,
            "add_check",
            "the check is added by copying the table, which checks every row",
        )
    return _unread(action)


def _add_foreign_key(facts: Facts, action: Action) -> Change:
    table = common.table(facts)
    if foreign_key_checks_off(facts):
        return Change(
            Online("INSTANT") if is_mariadb(facts) else Online("INPLACE", "NONE"),
            Work.CATALOG,
            "add_foreign_key.unchecked",
            "foreign_key_checks is off, so the key is added without reading a row",
            notes=(
                f"with foreign_key_checks off, the rows already in {table} are not "
                "checked against the key, and a row that breaks it stays",
            ),
        )
    return Change(
        copy_algorithm(facts),
        Work.REWRITE,
        "add_foreign_key",
        "with foreign_key_checks on, the key is added by copying the table, "
        "which checks every row; with foreign_key_checks = 0 it is added in "
        "place, but the rows already there go unchecked",
    )


def _drop_constraint(facts: Facts, action: Action) -> Change:
    options = action.options
    constraint = options.get("constraint")
    name = str(options.get("name") or "")
    table = common.table(facts)
    live = facts.state.original(table)
    if constraint is None and name:
        constraint = _constraint_kind(facts, live, name)
    if constraint == "primary_key" or name.upper() == "PRIMARY":
        return Change(
            copy_algorithm(facts),
            Work.REWRITE,
            "drop_primary_key",
            "without a primary key the rows are ordered again, by copying the table",
        )
    if constraint == "foreign_key":
        return Change(
            _instant_on_mariadb(facts),
            Work.CATALOG,
            "drop_foreign_key",
            "the key is dropped in the data dictionary",
        )
    if constraint == "check":
        return Change(
            Online("INSTANT"),
            Work.CATALOG,
            "drop_check",
            "the check is dropped in the data dictionary",
        )
    if constraint == "unique":
        return drop_index_change(facts)
    return Change(
        nocopy_algorithm(facts),
        Work.CATALOG,
        "drop_index",
        f"the rules do not know what kind of constraint {name} is",
        Confidence.LIKELY,
    )


def _constraint_kind(facts: Facts, table: str, name: str) -> Optional[str]:
    """What kind of constraint a name is on the table, from the schema read."""
    found = facts.context.table(table)
    if found is None:
        return None
    lowered = name.lower()
    if facts.context.foreign_key_target(table, name) is not None:
        return "foreign_key"
    if lowered in found.check_names or lowered in found.checks:
        return "check"
    if lowered in found.indexes:
        return "unique"
    return None


def drop_index_change(facts: Facts) -> Change:
    return Change(
        nocopy_algorithm(facts),
        Work.CATALOG,
        "drop_index",
        "the index is dropped in the data dictionary",
    )


def _drop_index_action(facts: Facts, action: Action) -> Change:
    if str(action.options.get("name") or "").upper() == "PRIMARY":
        return _drop_constraint(
            facts, Action("drop_constraint", None, {"name": "PRIMARY"})
        )
    return drop_index_change(facts)


def _engine(facts: Facts, action: Action) -> Change:
    engine = str(action.options.get("engine") or "").upper()
    if engine == "INNODB":
        return Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "table_rebuild",
            "ENGINE=InnoDB on an InnoDB table rebuilds it in place",
        )
    return Change(
        copy_algorithm(facts),
        Work.REWRITE,
        "table_copy",
        f"moving the table to {engine} copies it",
    )


def _table_option(facts: Facts, action: Action) -> Change:
    name = str(action.options.get("name"))
    if name in ("row_format", "key_block_size"):
        return Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "table_rebuild",
            f"a new {name.upper()} rebuilds the table in place",
        )
    return Change(
        _instant_on_mariadb(facts),
        Work.CATALOG,
        "table_option",
        f"{name.upper()} changes in the data dictionary",
    )


def _unread(action: Action) -> Change:
    return Change(
        Online("COPY", "EXCLUSIVE"),
        Work.UNKNOWN,
        "",
        f"no rule reads the ALTER TABLE action {action.kind}",
        Confidence.UNKNOWN,
    )


_ActionHandler = Callable[[Facts, Action], Change]
ACTIONS: Dict[str, _ActionHandler] = {
    "add_column": _add_column,
    "drop_column": _drop_column,
    "modify_column": _modify_column,
    "change_column": _change_column,
    "set_default": _fixed(
        _instant,
        Work.CATALOG,
        "column_default",
        "the default changes in the data dictionary",
    ),
    "drop_default": _fixed(
        _instant,
        Work.CATALOG,
        "column_default",
        "the default changes in the data dictionary",
    ),
    "set_storage": _fixed(
        _instant,
        Work.CATALOG,
        "column_default",
        "the column's visibility changes in the data dictionary",
    ),
    "rename_column": _rename_action,
    "rename_to": _rename_to,
    "rename_index": _fixed(
        _instant_on_mariadb,
        Work.CATALOG,
        "rename_index",
        "the index is renamed in the data dictionary",
    ),
    "add_index": _add_index,
    "drop_index": _drop_index_action,
    "add_constraint": _add_constraint,
    "drop_constraint": _drop_constraint,
    "convert_charset": _fixed(
        copy_algorithm,
        Work.REWRITE,
        "table_copy",
        "every text column is converted, by copying the table",
    ),
    "engine": _engine,
    "force": _fixed(
        _inplace, Work.REWRITE, "table_rebuild", "FORCE rebuilds the table in place"
    ),
    "table_option": _table_option,
}
