"""
ADD COLUMN, DROP COLUMN, MODIFY, CHANGE, and RENAME COLUMN: which ones
InnoDB runs instantly, in place, or with a table copy.
"""

from __future__ import annotations

from typing import (
    List,
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.model import (
    Action,
    Confidence,
    Work,
)
from sustained.impact.rules import Facts, common
from sustained.impact.rules.mysql.column_types import (
    type_change,
)
from sustained.impact.rules.mysql.facts import (
    Change,
    copy_algorithm,
    foreign_key_checks_off,
    is_mariadb,
    nocopy_algorithm,
    rename_note,
    row_version_limit,
    table_stats,
    version_of,
)
from sustained.impact.rules.mysql.locks import (
    ALGORITHMS,
    Online,
)


def _storage_limits(facts: Facts, table: str) -> Tuple[Optional[str], Confidence]:
    """
    Why an instant column change would not be instant on this table, or
    None when nothing stops it, with how sure the answer is.
    """
    stats = table_stats(facts, table)
    unread: List[str] = []
    if stats.fulltext:
        return "the table has a FULLTEXT index", Confidence.KNOWN
    if stats.row_format == "COMPRESSED":
        return "the table's ROW_FORMAT is COMPRESSED", Confidence.KNOWN
    if stats.fulltext is None:
        unread.append("its FULLTEXT indexes")
    if stats.row_format is None:
        unread.append("its row format")
    if not is_mariadb(facts) and version_of(facts) >= (8, 0, 29):
        limit = row_version_limit(version_of(facts))
        if stats.row_versions is not None and stats.row_versions >= limit:
            return (
                f"the table has used all {limit} instant row versions",
                Confidence.KNOWN,
            )
        if stats.row_versions is None:
            unread.append("its instant row versions")
    if unread:
        return None, Confidence.LIKELY
    return None, Confidence.KNOWN


def _unread_note(facts: Facts, table: str) -> str:
    stats = table_stats(facts, table)
    missing = []
    if stats.fulltext is None:
        missing.append("a FULLTEXT index")
    if stats.row_format is None:
        missing.append("ROW_FORMAT=COMPRESSED")
    if (
        not is_mariadb(facts)
        and version_of(facts) >= (8, 0, 29)
        and stats.row_versions is None
    ):
        missing.append("used-up instant row versions")
    return (
        "instant unless the table has "
        + " or ".join(missing)
        + ", which the rules did not read"
    )


def _add_column(facts: Facts, action: Action) -> Change:
    options = action.options
    table = common.table(facts)
    mariadb = is_mariadb(facts)
    default = str(options.get("default") or "")
    if options.get("generated") == "stored":
        return Change(
            copy_algorithm(facts),
            Work.REWRITE,
            "add_column.copy",
            "a stored generated column is computed for every row",
        )
    if options.get("check"):
        return Change(
            copy_algorithm(facts),
            Work.REWRITE,
            "add_column.copy",
            "a column with a CHECK clause has every row checked",
        )
    if options.get("references") and not foreign_key_checks_off(facts):
        return Change(
            copy_algorithm(facts),
            Work.REWRITE,
            "add_column.copy",
            "with foreign_key_checks on, a column with a REFERENCES clause is "
            "added by copying the table, which checks every row",
        )
    if default.startswith("("):
        volatile = options.get("default_volatility") == "volatile"
        if not mariadb or volatile:
            what = (
                "an expression default"
                if not mariadb
                else f"the default calls {options.get('default_function')}()"
            )
            return Change(
                copy_algorithm(facts),
                Work.REWRITE,
                "add_column.copy",
                f"{what} is computed for every row",
            )
    if options.get("identity"):
        return Change(
            Online("INPLACE", "SHARED"),
            Work.REWRITE,
            "add_column.rebuild",
            "an AUTO_INCREMENT column is filled for every row",
        )
    if options.get("primary_key") or options.get("unique"):
        return Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "add_column.rebuild",
            "a column with a key is added by rebuilding the table",
        )
    reason, confidence = _storage_limits(facts, table)
    if reason is not None and "FULLTEXT" in reason and not mariadb:
        return Change(
            copy_algorithm(facts),
            Work.REWRITE,
            "add_column.copy",
            f"{reason}, so the column is added by copying the table",
        )
    if reason is not None and "FULLTEXT" in reason:
        return Change(
            Online("INPLACE", "SHARED"),
            Work.REWRITE,
            "add_column.rebuild",
            f"{reason}, so the column is added by rebuilding the table",
        )
    if reason is not None:
        return Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "add_column.rebuild",
            f"{reason}, so the column is added by rebuilding the table",
        )
    if options.get("position") and not mariadb and version_of(facts) < (8, 0, 29):
        return Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "add_column.rebuild",
            "before MySQL 8.0.29 only a column added last is instant",
        )
    note = _unread_note(facts, table) if confidence is Confidence.LIKELY else ""
    return Change(
        Online("INSTANT"),
        Work.CATALOG,
        "add_column.instant",
        note or "the column is added in the data dictionary",
        confidence,
    )


def _indexed(facts: Facts, table: str, column: str) -> Optional[bool]:
    """Whether a column is part of any index, or None when not read."""
    found = facts.context.table(facts.state.original(table))
    if found is None:
        return None
    wanted = column.lower()
    if wanted in (c.lower() for c in found.primary_key):
        return True
    return any(
        wanted in (c.lower() for c in index.columns) for index in found.indexes.values()
    )


def _drop_column(facts: Facts, action: Action) -> Change:
    table = common.table(facts)
    column = action.column or "?"
    mariadb = is_mariadb(facts)
    indexed = _indexed(facts, table, column)
    if indexed:
        return Change(
            nocopy_algorithm(facts) if mariadb else Online("INPLACE", "NONE"),
            Work.CATALOG if mariadb else Work.REWRITE,
            "drop_column.rebuild",
            f"{column} is part of an index",
        )
    if not mariadb and version_of(facts) < (8, 0, 29):
        return Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "drop_column.rebuild",
            "before MySQL 8.0.29 a column is dropped by rebuilding the table",
        )
    reason, confidence = _storage_limits(facts, table)
    if reason is not None:
        return Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "drop_column.rebuild",
            f"{reason}, so the column is dropped by rebuilding the table",
        )
    if indexed is None:
        confidence = Confidence.LIKELY
    note = ""
    if confidence is Confidence.LIKELY:
        note = f"instant unless {column} is part of an index" + (
            "" if indexed is not None else ", which the schema read would show"
        )
    return Change(
        Online("INSTANT"),
        Work.CATALOG,
        "drop_column.instant",
        note or "the column is dropped in the data dictionary",
        confidence,
    )


# --- MODIFY and CHANGE -------------------------------------------------


def _modify_column(facts: Facts, action: Action) -> Change:
    table = common.table(facts)
    column = action.column or "?"
    options = action.options
    mariadb = is_mariadb(facts)
    changes: List[Change] = []
    new_name = options.get("new")
    if new_name and str(new_name).lower() != column.lower():
        changes.append(_rename_column(facts))
    if options.get("position"):
        if mariadb:
            changes.append(
                Change(
                    Online("INSTANT"),
                    Work.CATALOG,
                    "modify_column.instant",
                    "MariaDB reorders columns in the data dictionary",
                )
            )
        else:
            changes.append(
                Change(
                    Online("INPLACE", "NONE"),
                    Work.REWRITE,
                    "modify_column.rebuild",
                    "a column is moved by rebuilding the table",
                )
            )
    intent = facts.intent
    found = facts.context.table(facts.state.original(table))
    spec = None
    if found is not None:
        for name, candidate in found.columns.items():
            if name.lower() == column.lower():
                spec = candidate
    new_type = str(options.get("type"))
    new_nullable = not options.get("not_null") and not options.get("primary_key")
    if intent is not None and intent.kind == "alter_column_type":
        change = type_change(
            facts,
            str(intent.get("from_type")),
            str(intent.get("to_type") or new_type),
            spec.collation if spec is not None else None,
        )
        if change is not None:
            changes.append(change)
    elif intent is not None and intent.kind in ("set_not_null", "drop_not_null"):
        changes.append(_nullability(intent.kind == "drop_not_null"))
    elif intent is not None and intent.kind == "set_column_comment":
        pass
    elif spec is not None:
        change = type_change(facts, spec.raw_type, new_type, spec.collation)
        if change is not None:
            changes.append(change)
        if spec.nullable != new_nullable:
            changes.append(_nullability(new_nullable))
        if bool(options.get("identity")) != spec.autoincrement:
            changes.append(
                Change(
                    copy_algorithm(facts),
                    Work.REWRITE,
                    "modify_column.copy",
                    "a change to AUTO_INCREMENT fills every row",
                )
            )
    else:
        changes.append(
            Change(
                copy_algorithm(facts),
                Work.REWRITE,
                "modify_column.copy",
                f"the current definition of {column} is not known, so the change "
                "counts as a type change, which copies the table",
                Confidence.LIKELY,
            )
        )
    if options.get("generated"):
        changes.append(
            Change(
                copy_algorithm(facts),
                Work.REWRITE,
                "modify_column.copy",
                "a generated column is computed again for every row",
                Confidence.LIKELY,
            )
        )
    if not changes:
        return Change(
            Online("INSTANT"),
            Work.CATALOG,
            "modify_column.instant",
            "only the column's default or comment changes",
        )
    return heaviest(changes)


def _nullability(nullable: bool) -> Change:
    what = "NOT NULL to NULL" if nullable else "NULL to NOT NULL"
    return Change(
        Online("INPLACE", "NONE"),
        Work.REWRITE,
        "modify_column.rebuild",
        f"a column changed from {what} is rebuilt in place",
    )


def heaviest(changes: Sequence[Change]) -> Change:
    """The changes of one statement as the server runs them together."""
    online = changes[0].online
    for change in changes[1:]:
        online = online.combined(change.online)
    worst = max(
        changes,
        key=lambda c: (ALGORITHMS.index(c.online.algorithm), c.work),
    )
    confidence = min(c.confidence for c in changes)
    notes = tuple(n for c in changes for n in c.notes)
    return Change(
        online,
        max(c.work for c in changes),
        worst.rule,
        worst.reason,
        confidence,
        notes,
    )


def _rename_column(facts: Facts, action: Optional[Action] = None) -> Change:
    old = action.column if action is not None else None
    notes = (rename_note("column", old),) if old else ()
    if not is_mariadb(facts) and version_of(facts) < (8, 0, 28):
        return Change(
            Online("INPLACE", "NONE"),
            Work.CATALOG,
            "rename",
            "before MySQL 8.0.28 a column is renamed in place",
            notes=notes,
        )
    return Change(
        Online("INSTANT"),
        Work.CATALOG,
        "rename",
        "the column is renamed in the data dictionary",
        notes=notes,
    )


def _change_column(facts: Facts, action: Action) -> Change:
    change = _modify_column(facts, action)
    new = action.options.get("new")
    if new and action.column and str(new).lower() != action.column.lower():
        note = rename_note("column", action.column)
        change = change._replace(notes=change.notes + (note,))
    return change
