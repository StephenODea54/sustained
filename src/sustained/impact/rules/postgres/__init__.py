"""
The PostgreSQL rules, for 12 and later.

Each statement kind, and each ALTER TABLE action, has a handler that
names the lock the statement takes on each table and the work it does
there. The analyzer rates that work against the table's size and the
run so far; a handler only says what the engine does.

The lock names are the ones `pg_locks.mode` reports, without the `Lock`
suffix, ordered weakest first in `LOCKS`. What each blocks follows the
conflict table in the documentation's "Explicit Locking" chapter:

- ACCESS SHARE up to SHARE UPDATE EXCLUSIVE conflict with no reads or
  writes, only with other DDL (`ddl`)
- SHARE, SHARE ROW EXCLUSIVE, and EXCLUSIVE conflict with ROW EXCLUSIVE,
  so INSERT, UPDATE, and DELETE wait (`writes`)
- ACCESS EXCLUSIVE conflicts with every lock, reads included
  (`reads_and_writes`)

A statement spelled in another engine's syntax, such as MySQL's MODIFY,
is unknown here.

`context_plan()` reads the server facts the rules use: the version, the
`TimeZone` and `lock_timeout` settings, and each table's size from
`pg_class.reltuples` and `pg_total_relation_size()`. A partitioned
table's size is the sum of its leaf partitions. From the catalog, and
without a lock on any table, it also reads the partitions of each
partitioned table and its DEFAULT partition (`partitions`), the columns
each index uses and their collations (`indexes`), the type of each
array column (`arrays`), and which types are domains with a NOT NULL or
a CHECK (`types`). `partitions.py` gives the partitions a statement on a
partitioned table also locks. `preflight_plan()` reads
the other backends' table locks and open transactions for the live
preflight.
"""

from __future__ import annotations

from typing import (
    Dict,
    List,
)

from sustained.impact.model import Action, Confidence, Finding, Work
from sustained.impact.rules import Effect, Facts, Outcome, Profile, Trace, common
from sustained.impact.rules.common import ActionHandler
from sustained.impact.rules.postgres.alter import (
    _add_column,
    _add_constraint,
    _attach_partition,
    _detach_partition,
    _drop_column,
    _drop_constraint,
    _rename,
    _set_not_null,
    _set_parameters,
    _validate,
    simple,
)
from sustained.impact.rules.postgres.catalog import (
    COLUMN_CATALOG,
    DOCS,
    FIXTURE_SCHEMA,
    RULES,
    SET_STATISTICS,
    TABLE_CATALOG,
    TABLE_REWRITE,
    TRIGGER_STATE,
)
from sustained.impact.rules.postgres.column_types import (
    _alter_column_type,
    type_change,
)
from sustained.impact.rules.postgres.context import context_plan, server_version
from sustained.impact.rules.postgres.locks import (
    ACCESS_EXCLUSIVE,
    LOCKS,
    SHARE,
    SHARE_ROW_EXCLUSIVE,
    SHARE_UPDATE_EXCLUSIVE,
    blocks,
    lock_rank,
    timeout_statement,
)
from sustained.impact.rules.postgres.partitions import (
    descendants,
    locked_below,
    merge_unread,
    partitioned,
    unread,
)
from sustained.impact.rules.postgres.preflight import preflight_plan
from sustained.impact.rules.postgres.statements import (
    _attach_index,
    _cluster,
    _comment,
    _create_index,
    _create_table,
    _drop_index,
    _drop_object,
    _drop_table,
    _drop_view,
    _insert,
    _lock_table,
    _refresh,
    _reindex,
    _trigger,
    _vacuum,
    _write_rows,
)
from sustained.impact.rules.postgres.trace import (
    sighting_plan,
    tables_plan,
    with_observations,
)

_ACTIONS: Dict[str, ActionHandler] = {
    "add_column": _add_column,
    "drop_column": _drop_column,
    "alter_column_type": _alter_column_type,
    "set_not_null": _set_not_null,
    "drop_not_null": simple(COLUMN_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_default": simple(COLUMN_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "drop_default": simple(COLUMN_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_storage": simple(COLUMN_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_statistics": simple(SET_STATISTICS, SHARE_UPDATE_EXCLUSIVE, Work.CATALOG),
    "add_constraint": _add_constraint,
    "drop_constraint": _drop_constraint,
    "validate_constraint": _validate,
    "rename_column": _rename,
    "rename_to": _rename,
    "rename_constraint": _rename,
    "attach_partition": _attach_partition,
    "detach_partition": _detach_partition,
    "set_tablespace": simple(TABLE_REWRITE, ACCESS_EXCLUSIVE, Work.REWRITE),
    "set_logged": simple(TABLE_REWRITE, ACCESS_EXCLUSIVE, Work.REWRITE),
    "set_unlogged": simple(TABLE_REWRITE, ACCESS_EXCLUSIVE, Work.REWRITE),
    "set_schema": simple(TABLE_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "owner_to": simple(TABLE_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "replica_identity": simple(TABLE_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "row_security": simple(TABLE_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_parameters": _set_parameters,
    "enable_trigger": simple(TRIGGER_STATE, SHARE_ROW_EXCLUSIVE, Work.CATALOG),
    "disable_trigger": simple(TRIGGER_STATE, SHARE_ROW_EXCLUSIVE, Work.CATALOG),
}


# The ALTER TABLE actions PostgreSQL applies to a partitioned table
# alone, without locking its partitions.
_PARENT_ONLY = frozenset(
    {
        "rename_to",
        "set_schema",
        "owner_to",
        "replica_identity",
        "row_security",
        "set_tablespace",
        "set_logged",
        "set_unlogged",
        "set_parameters",
        "attach_partition",
        "detach_partition",
    }
)
# The actions that copy a partitioned table's file, which it does not
# have: they change its catalog entry, for the partitions added later.
_NO_FILE = frozenset({"set_tablespace", "set_logged", "set_unlogged"})


def _alter_table(facts: Facts) -> Outcome:
    table = common.table(facts)
    parent = partitioned(facts, table)
    only = bool(facts.parsed.options.get("only"))

    def adjust(action: Action, outcome: Outcome) -> Outcome:
        found = list(outcome.effects)
        if parent and action.kind in _NO_FILE:
            found = [
                e._replace(work=Work.CATALOG) if e.table.lower() == table.lower() else e
                for e in found
            ]
        if parent and not only and action.kind not in _PARENT_ONLY:
            found.extend(_on_partitions(facts, action, table, found))
        return outcome._replace(effects=tuple(found))

    joined = common.each_action(facts, common.by_kind(_ACTIONS), adjust)
    if joined.confidence is Confidence.UNKNOWN:
        return joined
    effects = list(joined.effects)
    findings = list(joined.findings)
    if not only and any(a.kind not in _PARENT_ONLY for a in facts.parsed.actions):
        own = [e.lock for e in effects if e.table.lower() == table.lower()]
        if own:
            strongest = max(own, key=lock_rank)
            findings.extend(unread(facts, table, locked_below(table, strongest)))
    if len(facts.parsed.actions) > 1:
        # A remedy rewrites one action, so it cannot stand for a
        # statement that holds others.
        effects = [e._replace(remedy=()) for e in effects]
    return joined._replace(effects=tuple(effects), findings=tuple(findings))


def _on_partitions(
    facts: Facts, action: Action, table: str, effects: List[Effect]
) -> List[Effect]:
    """
    The effects an action on a partitioned table has on the table, on
    each partition below it, with the same lock and work. A unique key
    or an exclusion constraint takes SHARE on each partition before 18,
    while each partition's index is built.
    """
    lock = None
    if action.kind == "add_constraint" and action.options.get("constraint") in (
        "unique",
        "exclude",
    ):
        lock = SHARE if facts.context.version < (18,) else ACCESS_EXCLUSIVE
    own = [e for e in effects if e.table.lower() == table.lower()]
    return [
        Effect(
            e.rule,
            name,
            lock or e.lock,
            e.work,
            e.confidence,
            blocks=e.blocks,
        )
        for name in descendants(facts, table)
        for e in own
    ]


_STATEMENTS: Dict[str, common.Handler] = {
    "alter_table": _alter_table,
    "attach_index": _attach_index,
    "create_index": _create_index,
    "drop_index": _drop_index,
    "create_table": _create_table,
    "drop_table": _drop_table,
    "truncate": _drop_table,
    "drop_view": _drop_view,
    "update": _write_rows,
    "delete": _write_rows,
    "insert": _insert,
    "reindex": _reindex,
    "vacuum": _vacuum,
    "analyze": _vacuum,
    "cluster": _cluster,
    "refresh_materialized_view": _refresh,
    "create_trigger": _trigger,
    "drop_trigger": _trigger,
    "comment_on": _comment,
    "lock_table": _lock_table,
    "drop_object": _drop_object,
    "create_object": common.nothing,
    "create_type": common.nothing,
    "drop_type": common.nothing,
    "alter_type_add_value": common.nothing,
    "alter_type_rename_value": common.nothing,
    "create_view": common.nothing,
    "set": common.nothing,
}


def effects(facts: Facts) -> Outcome:
    """What the statement does on PostgreSQL, table by table."""
    return merge_unread(facts, common.dispatch(facts, _STATEMENTS))


PROFILE = Profile(
    name="postgres",
    title="PostgreSQL",
    prefix="pg",
    effects=effects,
    blocks=blocks,
    lock_rank=lock_rank,
    timeout_setting="lock_timeout",
    timeout_statement=timeout_statement,
    transactional_ddl=True,
    rules=RULES,
    timeout_source=DOCS + "runtime-config-client.html#GUC-LOCK-TIMEOUT",
    context_plan=context_plan,
    fixture_schema=FIXTURE_SCHEMA,
    trace=Trace(tables_plan, with_observations, sighting=sighting_plan),
    preflight=preflight_plan,
)

__all__ = [
    "FIXTURE_SCHEMA",
    "LOCKS",
    "PROFILE",
    "blocks",
    "context_plan",
    "effects",
    "lock_rank",
    "server_version",
    "timeout_statement",
    "type_change",
]
