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
table's size is the sum of its leaf partitions.
"""

from __future__ import annotations

from typing import (
    Dict,
    List,
)

from sustained.impact.model import Confidence, Finding, Work
from sustained.impact.rules import Effect, Facts, Outcome, Profile, Trace, common
from sustained.impact.rules.postgres.alter import (
    ActionHandler,
    _add_column,
    _add_constraint,
    _attach_partition,
    _detach_partition,
    _drop_column,
    _drop_constraint,
    _rename,
    _set_not_null,
    _set_parameters,
    simple,
)
from sustained.impact.rules.postgres.catalog import (
    COLUMN_CATALOG,
    DOCS,
    FIXTURE_SCHEMA,
    SET_STATISTICS,
    TABLE_CATALOG,
    TABLE_REWRITE,
    TRIGGER_STATE,
    VALIDATE,
    all_rules,
)
from sustained.impact.rules.postgres.column_types import (
    _alter_column_type,
    type_change,
)
from sustained.impact.rules.postgres.context import context_plan, server_version
from sustained.impact.rules.postgres.locks import (
    ACCESS_EXCLUSIVE,
    LOCKS,
    SHARE_ROW_EXCLUSIVE,
    SHARE_UPDATE_EXCLUSIVE,
    blocks,
    lock_rank,
    timeout_statement,
)
from sustained.impact.rules.postgres.statements import (
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
    "validate_constraint": simple(VALIDATE, SHARE_UPDATE_EXCLUSIVE, Work.SCAN),
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
    "row_security": simple(TABLE_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_parameters": _set_parameters,
    "enable_trigger": simple(TRIGGER_STATE, SHARE_ROW_EXCLUSIVE, Work.CATALOG),
    "disable_trigger": simple(TRIGGER_STATE, SHARE_ROW_EXCLUSIVE, Work.CATALOG),
}


def _alter_table(facts: Facts) -> Outcome:
    effects: List[Effect] = []
    findings: List[Finding] = []
    confidence = Confidence.KNOWN
    for action in facts.parsed.actions:
        handler = _ACTIONS.get(action.kind)
        if handler is None:
            return common.unknown(facts, f"the ALTER TABLE action {action.kind}")
        outcome = handler(facts, action)
        effects.extend(outcome.effects)
        findings.extend(outcome.findings)
        confidence = min(confidence, outcome.confidence)
    if len(facts.parsed.actions) > 1:
        # A remedy rewrites one action, so it cannot stand for a
        # statement that holds others.
        effects = [e._replace(remedy=()) for e in effects]
    return Outcome(tuple(effects), tuple(findings), confidence)


_STATEMENTS: Dict[str, common.Handler] = {
    "alter_table": _alter_table,
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
    return common.dispatch(facts, _STATEMENTS)


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
    rules=all_rules(),
    timeout_source=DOCS + "runtime-config-client.html#GUC-LOCK-TIMEOUT",
    context_plan=context_plan,
    fixture_schema=FIXTURE_SCHEMA,
    trace=Trace(tables_plan, sighting_plan, with_observations),
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
