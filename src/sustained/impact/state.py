"""
What the analysis carries from one statement of a run to the next.

The rules read each statement against what came before it in the run:

- A table created earlier in the run is empty, and nobody else reads
  it yet, so work on it blocks nothing. `CREATE TABLE IF NOT EXISTS`
  creates a table only when the context reads no table of that name,
  and neither the run nor the context says it is gone; without a
  schema or size read, the table may exist, so it is not new.
- A table the run created and then filled from a query, with
  `CREATE TABLE ... AS SELECT` or `INSERT ... SELECT`, is not empty. Its
  size is the sum of the sizes of the tables the query reads, and is
  unknown when the query reads rows from something other than a table.
- A table renamed earlier in the run is the live table under a new
  name, so its size is the size of the table it was. A table the run
  filled keeps its size under the new name, so after a table swap
  (`CREATE TABLE big2`, a copy, `DROP TABLE big`, `ALTER TABLE big2
  RENAME TO big`) `big` has the size of the rows copied into it.
- An index created earlier in the run names its table, which a later
  `DROP INDEX` leaves unsaid.
- A check of the form `column IS NOT NULL` that the run added, and
  validated or added without `NOT VALID`, proves the column has no
  NULL, so a later `SET NOT NULL` on Postgres reads no rows. The run
  state follows `RENAME CONSTRAINT`, `RENAME COLUMN`, `DROP CONSTRAINT`,
  and `DROP COLUMN` for these checks and for the checks the schema
  read reports (`schema_check_kept()`, `schema_column()`).
- A statement that changes how a table is stored changes what later
  statements on the table can do. On InnoDB, an instant column change
  uses one of the table's row versions, a rebuild gives them back, and
  a FULLTEXT index or `ROW_FORMAT=COMPRESSED` stops instant column
  changes. The rules record these with `record_storage()`.
- On PostgreSQL, a partitioned table the run created with `PARTITION
  BY`, and the partitions it created with `PARTITION OF`, attached with
  `ATTACH PARTITION`, or detached with `DETACH PARTITION`, change the
  partitions the context read (`relation()`). A partitioned table the
  run created has the rows of each partition attached to it, and an
  `INSERT ... SELECT` into a partitioned table fills each partition
  below it that the run created. `DROP TABLE` drops the partitions
  below the table, and a rename keeps the table's place.
- A `ROLLBACK`, or `ROLLBACK TO SAVEPOINT`, in a migration's
  transaction undoes what the migration did to tables, indexes, checks,
  columns, storage, and partitions, on an engine whose DDL runs in the
  transaction, so the facts go back to what they were as the migration
  began (`rollback()`). Outside a transaction there is
  nothing to undo, and on MySQL and MariaDB each DDL statement commits,
  so a ROLLBACK there leaves the facts as they are. A table an `INSERT
  ... SELECT` filled on MySQL then stays filled after a ROLLBACK that
  empties it, which reports more work than the run does.
- A lock timeout set earlier covers the statements after it, as far as
  its scope reaches (`TimeoutScope`). `RESET` of the setting, `RESET
  ALL`, and `DISCARD ALL` end it, and so does a `ROLLBACK` that undoes
  the `SET`.
- Session settings, such as MySQL's `foreign_key_checks`, change what
  later statements do. A `SET GLOBAL` or `SET PERSIST` leaves the
  session's own value as it was, and so does a user variable, so none
  of them counts.

Names compare case-insensitively, as the recognizer's docstring asks.
"""

from __future__ import annotations

import copy
import re
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from sustained.impact.context import Relation, TableStats
from sustained.impact.model import Action, ParsedStatement

if TYPE_CHECKING:
    from sustained.impact.context import EngineContext

# A timeout of zero, however spelled, turns the timeout off, and so
# does DEFAULT, which falls back to the server's setting.
_NO_TIMEOUT_RE = re.compile(r"(0+(\.0*)?\s*(us|ms|s|min|h|d)?|default)", re.IGNORECASE)

_UNSET = object()

# The SET scopes that leave the session's own value unchanged.
_NOT_THE_SESSION = frozenset({"global", "persist", "persist_only", "user"})

# The RunState attributes that hold what the run did to tables, indexes,
# checks, columns, storage, and partitions, which a ROLLBACK undoes.
_FACTS = (
    "born",
    "created",
    "filled",
    "gone",
    "renamed",
    "indexes",
    "not_null_checks",
    "schema_checks",
    "schema_columns",
    "storage",
    "links",
    "defaults",
    "partitioned",
)


def sets_a_timeout(value: str) -> bool:
    """Whether a lock timeout value waits for a bounded time."""
    return not _NO_TIMEOUT_RE.fullmatch(value.strip())


class TimeoutScope:
    """
    Whether a lock timeout covers the next statement of a run, read in
    run order.

    A session setting (`SET lock_timeout`, with or without SESSION)
    covers every statement after it in the run. A `SET LOCAL` setting
    dies at the commit that ends its migration, so it covers only the
    statements after it in that migration. A migration that runs outside
    a transaction has no transaction block to attach a LOCAL setting to,
    so Postgres ignores it there.

    Call `enter()` with each statement's migration before reading
    `covered` or calling `set()` for it.
    """

    def __init__(self) -> None:
        self.session = False
        self.local = False
        self._migration: object = _UNSET
        # The session setting as the migration began, which a ROLLBACK
        # goes back to.
        self._session_at_start = False

    def enter(self, migration_id: Optional[str]) -> None:
        """Moves to a statement of the given migration."""
        if migration_id != self._migration:
            # A new migration ends the LOCAL setting of the one before
            # it, whose commit dropped the setting with it.
            self._migration = migration_id
            self.local = False
            self._session_at_start = self.session

    def set(self, scope: str, transactional: bool, enabled: bool = True) -> None:
        """
        Records a timeout statement of the given scope. A session
        setting replaces a LOCAL one for the rest of the transaction.
        """
        if scope != "local":
            self.session = enabled
            self.local = False
        elif transactional:
            self.local = enabled

    def reset(self) -> None:
        """
        Records a `RESET` of the timeout. It goes back to the value the
        connection started with, which the run does not know, so no
        timeout counts as set.
        """
        self.session = False
        self.local = False

    def rollback(self) -> None:
        """
        Records a `ROLLBACK`, which undoes the timeout statements since
        the transaction began. The migration's first statement is the
        latest point the transaction can have begun, so a session
        timeout covers the statements after the ROLLBACK only when it
        was set both before the migration and at the ROLLBACK.
        """
        self.session = self.session and self._session_at_start
        self.local = False

    @property
    def covered(self) -> bool:
        return self.session or self.local


class RunState:
    """
    The facts a run has built up so far. `timeout_setting` is the lower
    case name of the engine's lock timeout setting, such as
    `lock_timeout`, and `bounded` says whether a value of it bounds the
    wait. `local_scope` is False on an engine where `SET LOCAL` means
    the session, as on MySQL; where it is True, a `ROLLBACK` also undoes
    a session `SET`. `transactional_ddl` says whether a `ROLLBACK` in a
    migration's transaction undoes its DDL, as on PostgreSQL, and not on
    MySQL, where each DDL statement commits. `context` is what the
    analysis read from the server, which says whether the table a
    `CREATE TABLE IF NOT EXISTS` names exists already.

    Call `enter()` with each statement's migration before the rules
    read the statement.
    """

    def __init__(
        self,
        timeout_setting: Optional[str] = None,
        bounded: Callable[[str], bool] = sets_a_timeout,
        local_scope: bool = True,
        context: Optional["EngineContext"] = None,
        transactional_ddl: bool = False,
    ) -> None:
        self.timeout_setting = timeout_setting
        self.bounded = bounded
        self.local_scope = local_scope
        self.context = context
        self.transactional_ddl = transactional_ddl
        # The tables the run created, and of those the ones still empty.
        self.born: Set[str] = set()
        self.created: Set[str] = set()
        # The tables the run created and filled from a query: the live
        # names of the tables whose rows were copied into them, or None
        # when the query read rows from something else.
        self.filled: Dict[str, Optional[Tuple[str, ...]]] = {}
        # The names the run dropped or renamed away.
        self.gone: Set[str] = set()
        self.renamed: Dict[str, str] = {}
        self.indexes: Dict[str, str] = {}
        # The checks of the form `column IS NOT NULL` the run added, by
        # table and check name: the column, and whether the check is valid.
        self.not_null_checks: Dict[str, Dict[str, Tuple[str, bool]]] = {}
        # By table, the checks of the schema read that the run renamed
        # or dropped: the name each has now, or None once dropped.
        self.schema_checks: Dict[str, Dict[str, Optional[str]]] = {}
        # By table, the columns of the schema read that the run renamed
        # or dropped: the name each has now, or None once dropped.
        self.schema_columns: Dict[str, Dict[str, Optional[str]]] = {}
        self.settings: Dict[str, str] = {}
        self.timeouts = TimeoutScope()
        # The storage facts the rules recorded, by live table name, such
        # as InnoDB's instant row versions.
        self.storage: Dict[str, Dict[str, object]] = {}
        # The partitions the run attached, created, or detached, by lower
        # case name: the name as the statement spells it, and the
        # partitioned table it is a partition of now, or None once
        # detached.
        self.links: Dict[str, Tuple[str, Optional[str]]] = {}
        # Of those, the ones that are the DEFAULT partition of their
        # partitioned table.
        self.defaults: Set[str] = set()
        # The partitioned tables the run created.
        self.partitioned: Set[str] = set()
        self._migration: object = _UNSET
        # The facts as the current migration began, which a ROLLBACK in
        # its transaction goes back to.
        self._at_start = self._facts()

    def enter(self, migration_id: Optional[str]) -> None:
        """Moves to a statement of the given migration."""
        if migration_id != self._migration:
            self._migration = migration_id
            self._at_start = self._facts()
        self.timeouts.enter(migration_id)

    def _facts(self) -> Dict[str, object]:
        """A copy of what the run recorded about tables."""
        return {name: copy.deepcopy(getattr(self, name)) for name in _FACTS}

    def rollback(self) -> None:
        """
        Takes in a `ROLLBACK` in a migration's transaction. The
        transaction began with the migration, so the facts go back to
        what they were as the migration began, and the statements after
        the ROLLBACK read the tables as the earlier migrations left
        them, or as the context read them for the first migration.
        """
        for name, value in self._at_start.items():
            setattr(self, name, copy.deepcopy(value))

    def is_new(self, table: str) -> bool:
        """Whether the run created the table earlier, and it is still empty."""
        return table.lower() in self.created

    def created_in_run(self, table: str) -> bool:
        """Whether the run created the table, empty or filled since."""
        return table.lower() in self.born

    def original(self, table: str) -> str:
        """The live name of a table the run may have renamed."""
        return self.renamed.get(table.lower(), table)

    def stats(self, context: "EngineContext", table: str) -> TableStats:
        """
        The table's size: none for a table the run created and left
        empty, the sum of the sizes of the tables its rows came from for
        one the run filled, and otherwise the context's stats for its
        live name.
        """
        key = table.lower()
        if key in self.created:
            return TableStats(0, 0)
        if key not in self.filled:
            return context.stats(self.original(table))
        sources = self.filled[key]
        if sources is None:
            return TableStats()
        read = [context.stats(source) for source in sources]
        return TableStats(
            _total([r.rows for r in read]), _total([r.bytes for r in read])
        )

    def index_table(self, index: str) -> Optional[str]:
        """The table of an index the run created, or None."""
        return self.indexes.get(index.lower())

    def stored(self, table: str) -> Mapping[str, object]:
        """The storage facts the rules recorded for a table in the run."""
        return self.storage.get(self.original(table).lower(), {})

    def record_storage(self, table: str, **facts: object) -> None:
        """
        Records storage facts a statement changed on a table, such as
        `row_format="COMPRESSED"`, for the statements after it. The
        facts stay with the live table across a rename.
        """
        self.storage.setdefault(self.original(table).lower(), {}).update(facts)

    def proves_not_null(self, table: str, column: str) -> bool:
        """
        Whether a valid check the run added proves the column has no
        NULL.
        """
        checks = self.not_null_checks.get(table.lower(), {})
        return (column.lower(), True) in checks.values()

    def schema_check_kept(self, table: str, name: str) -> bool:
        """Whether a check the schema read reports on the table is still there."""
        renamed = self.schema_checks.get(table.lower(), {})
        return renamed.get(name.lower(), name) is not None

    def schema_column(self, table: str, column: str) -> Optional[str]:
        """
        The lower case name the schema read gives the column the table
        now names `column`, or None when the run added the column or
        moved the name onto it.
        """
        return _schema_name(self.schema_columns.get(table.lower(), {}), column)

    def relation(
        self, table: str, context: Optional["EngineContext"] = None
    ) -> Optional[Relation]:
        """
        The PostgreSQL catalog facts about a table at this point of the
        run: those the context read, or `context` when given, with the
        run's partitioned tables and partitions put in, and with the
        names the run renamed or dropped followed. None when neither the
        read nor the run has any.
        """
        context = context or self.context
        key = table.lower()
        read = None
        if context is not None and key not in self.born | self.gone:
            read = context.relations.get(self.original(table).lower())
        below = [
            name
            for name, parent in self.links.values()
            if parent is not None and parent.lower() == key
        ]
        if read is None and not below and key not in self.links:
            if key not in self.partitioned:
                return None
        read = read or Relation()
        partitions = [
            name
            for name in (self.now_named(p) for p in read.partitions)
            if name is not None and name.lower() not in self.links
        ]
        default = self.now_named(read.default) if read.default else None
        if default is None or default.lower() in self.links:
            default = None
        for name in below:
            partitions.append(name)
            if name.lower() in self.defaults:
                default = name
        if key in self.links:
            parent: Optional[str] = self.links[key][1]
        else:
            parent = self.now_named(read.parent) if read.parent else None
        return read._replace(
            partitioned=read.partitioned or key in self.partitioned,
            parent=parent,
            default=default,
            partitions=tuple(partitions),
        )

    def now_named(self, live: str) -> Optional[str]:
        """
        The name a table the context read has now: the name the run
        renamed it to, or None once the run dropped it.
        """
        wanted = live.lower()
        for now, was in self.renamed.items():
            if was.lower() == wanted:
                return now
        if wanted in self.gone or wanted in self.born or wanted in self.renamed:
            # The name is gone, or names a table the run made or renamed.
            return None
        return live

    def below(self, table: str) -> List[str]:
        """Every partition below a table, nearest first."""
        found: List[str] = []
        seen = {table.lower()}
        pending = [table]
        while pending:
            current = self.relation(pending.pop(0))
            for name in current.partitions if current is not None else ():
                if name.lower() not in seen:
                    seen.add(name.lower())
                    found.append(name)
                    pending.append(name)
        return found

    def above(self, table: str) -> List[str]:
        """The partitioned tables a table is below, nearest first."""
        found: List[str] = []
        seen = {table.lower()}
        current = self.relation(table)
        while current is not None and current.parent is not None:
            if current.parent.lower() in seen:
                break
            seen.add(current.parent.lower())
            found.append(current.parent)
            current = self.relation(current.parent)
        return found

    def record(self, parsed: ParsedStatement, transactional: bool) -> None:
        """Takes in what the statement changes, after the rules read it."""
        kind = parsed.kind
        options = parsed.options
        if kind == "create_table" and parsed.table:
            self.record_create(parsed.table, options)
        elif kind == "insert" and parsed.table and options.get("source") == "select":
            self.insert(parsed.table, options.get("reads"))
        elif kind == "drop_table":
            for table in parsed.items("tables"):
                self.drop(str(table))
        elif kind == "create_index" and parsed.table and options.get("name"):
            self.indexes[str(options["name"]).lower()] = parsed.table
        elif kind == "rename_table":
            for old, new in parsed.items("renames"):
                self.rename(str(old), str(new))
        elif kind == "alter_table" and parsed.table:
            for action in parsed.actions:
                self.record_checks(parsed.table, action)
                if action.kind == "rename_to":
                    self.rename(parsed.table, str(action.options["new"]))
                elif action.kind == "attach_partition":
                    self.attach(
                        parsed.table,
                        str(action.options["partition"]),
                        bool(action.options.get("default")),
                    )
                elif action.kind == "detach_partition":
                    self.detach(str(action.options["partition"]))
        elif kind == "set":
            self.record_settings(parsed, transactional)

    def record_create(self, table: str, options: Mapping[str, object]) -> None:
        """Takes in a CREATE TABLE."""
        if options.get("if_not_exists") and self.may_exist(table):
            return
        key = table.lower()
        self.forget(key)
        self.born.add(key)
        self.created.add(key)
        if options.get("partitioned"):
            self.partitioned.add(key)
        parent = options.get("partition_of")
        if parent:
            self.links[key] = (table, str(parent))
            if options.get("default_partition"):
                self.defaults.add(key)
        if options.get("as_select"):
            self.fill(table, options.get("reads"))

    def may_exist(self, table: str) -> bool:
        """
        Whether a table of this name may exist at this point of the run:
        the run made it, or the context reads it, or the context read
        neither the schema nor the sizes and so cannot say it is absent.
        """
        key = table.lower()
        if key in self.born or key in self.renamed:
            return True
        if key in self.gone:
            return False
        context = self.context
        if context is None or not {"schema", "sizes"} & context.read:
            return True
        bare = key.rsplit(".", 1)[-1]
        return (
            context.table(table) is not None
            or key in context.tables
            or bare in context.tables
        )

    def fill(self, table: str, reads: object) -> None:
        """
        Takes in rows copied from a query into a table the run created.
        `reads` is the recognizer's tuple of the tables the query reads,
        or None when the query reads rows from something else.
        """
        key = table.lower()
        sources: Optional[Tuple[str, ...]] = self.filled.get(key, ())
        if not isinstance(reads, tuple):
            sources = None
        else:
            for source in reads:
                sources = _joined(sources, self.sources_of(str(source)))
        if sources == ():
            # The query read only empty tables, or none, as SELECT 1
            # does, so the table has at most a few rows.
            return
        self.filled[key] = sources
        self.created.discard(key)

    def insert(self, table: str, reads: object) -> None:
        """
        Takes in `INSERT ... SELECT`. The rows go to the table, or on a
        partitioned table to some of the partitions below it, which the
        statement does not say, so each table the run created among them,
        and above them, counts as filled from the query.
        """
        for name in [table, *self.below(table), *self.above(table)]:
            if self.created_in_run(name):
                self.fill(name, reads)

    def attach(self, parent: str, child: str, default: bool) -> None:
        """
        Takes in `ATTACH PARTITION`. A partitioned table the run created,
        and each one it is below, then has the rows of the partition.
        """
        key = child.lower()
        self.links[key] = (child, parent)
        if default:
            self.defaults.add(key)
        else:
            self.defaults.discard(key)
        for name in [parent, *self.above(parent)]:
            if self.created_in_run(name):
                self.fill(name, (child,))

    def detach(self, child: str) -> None:
        """
        Takes in `DETACH PARTITION`. A partitioned table the run filled
        keeps the size its partitions gave it, which may be more than it
        has now.
        """
        key = child.lower()
        self.links[key] = (child, None)
        self.defaults.discard(key)

    def sources_of(self, table: str) -> Optional[Tuple[str, ...]]:
        """The live tables whose rows a table has, as `filled` records them."""
        key = table.lower()
        if key in self.created:
            return ()
        if key in self.filled:
            return self.filled[key]
        return (self.original(table),)

    def drop(self, table: str) -> None:
        """
        Takes in a DROP TABLE, which drops each partition below the
        table too.
        """
        for name in [table, *self.below(table)]:
            key = name.lower()
            self.forget(key)
            self.gone.add(key)

    def forget(self, key: str) -> None:
        """Clears what the run knew of the table a lower case name named."""
        self.born.discard(key)
        self.created.discard(key)
        self.filled.pop(key, None)
        self.renamed.pop(key, None)
        self.not_null_checks.pop(key, None)
        self.schema_checks.pop(key, None)
        self.schema_columns.pop(key, None)
        self.links.pop(key, None)
        self.defaults.discard(key)
        self.partitioned.discard(key)
        self.gone.discard(key)

    def record_checks(self, table: str, action: Action) -> None:
        """
        Takes in the `IS NOT NULL` checks, and the schema's checks and
        columns, that an ALTER TABLE action changes.
        """
        key = table.lower()
        checks = self.not_null_checks.setdefault(key, {})
        name = str(action.options.get("name") or "").lower()
        if action.kind == "add_constraint" and action.options.get("not_null"):
            column = str(action.options["not_null"]).lower()
            checks[name] = (column, not action.options.get("not_valid"))
        elif action.kind == "validate_constraint" and name in checks:
            checks[name] = (checks[name][0], True)
        elif action.kind == "drop_constraint":
            if checks.pop(name, None) is None:
                _move(self.schema_checks.setdefault(key, {}), name, None)
        elif action.kind == "rename_constraint":
            old = str(action.options.get("old") or "").lower()
            new = str(action.options.get("new") or "").lower()
            if old in checks:
                checks[new] = checks.pop(old)
            else:
                _move(self.schema_checks.setdefault(key, {}), old, new)
        elif action.kind == "drop_column" and action.column:
            dropped = action.column.lower()
            for check, (column, _) in list(checks.items()):
                if column == dropped:
                    del checks[check]
            _move(self.schema_columns.setdefault(key, {}), dropped, None)
        elif action.kind == "rename_column" and action.column:
            old = action.column.lower()
            new = str(action.options.get("new") or "").lower()
            for check, (column, valid) in list(checks.items()):
                if column == old:
                    checks[check] = (new, valid)
            _move(self.schema_columns.setdefault(key, {}), old, new)

    def rename(self, old: str, new: str) -> None:
        # A rename keeps the old schema when the new name has none.
        if "." in old and "." not in new:
            new = f"{old.rsplit('.', 1)[0]}.{new}"
        was, now = old.lower(), new.lower()
        live = self.original(old)
        born, created = was in self.born, was in self.created
        filled = was in self.filled
        sources = self.filled.get(was)
        checks = self.not_null_checks.get(was)
        schema_checks = self.schema_checks.get(was)
        columns = self.schema_columns.get(was)
        link = self.links.get(was)
        default, partitioned = was in self.defaults, was in self.partitioned
        self.forget(was)
        self.forget(now)
        self.gone.add(was)
        if born:
            self.born.add(now)
        if created:
            self.created.add(now)
        if filled:
            self.filled[now] = sources
        if checks is not None:
            self.not_null_checks[now] = checks
        if schema_checks is not None:
            self.schema_checks[now] = schema_checks
        if columns is not None:
            self.schema_columns[now] = columns
        if link is not None:
            self.links[now] = (new, link[1])
        if default:
            self.defaults.add(now)
        if partitioned:
            self.partitioned.add(now)
        for child, (name, parent) in list(self.links.items()):
            if parent is not None and parent.lower() == was:
                self.links[child] = (name, new)
        self.renamed[now] = live

    def record_settings(self, parsed: ParsedStatement, transactional: bool) -> None:
        for scope, name, value in parsed.items("settings"):
            if scope in _NOT_THE_SESSION:
                continue
            if scope == "reset":
                self.reset(name)
                continue
            if scope == "rollback":
                if self.local_scope:
                    self.timeouts.rollback()
                if transactional and self.transactional_ddl:
                    self.rollback()
                continue
            if scope == "local" and not self.local_scope:
                scope = "session"
            self.settings[name] = value
            if name == self.timeout_setting:
                self.timeouts.set(scope, transactional, self.bounded(value))

    def reset(self, name: str) -> None:
        """Takes in a `RESET` of one setting, or of `all`."""
        if name == "all":
            self.settings.clear()
        else:
            self.settings.pop(name, None)
        if name in ("all", self.timeout_setting):
            self.timeouts.reset()


def _total(values: Sequence[Optional[int]]) -> Optional[int]:
    """The sum of the values, or None when any is unknown."""
    known = [value for value in values if value is not None]
    return sum(known) if len(known) == len(values) else None


def _joined(
    first: Optional[Tuple[str, ...]], second: Optional[Tuple[str, ...]]
) -> Optional[Tuple[str, ...]]:
    if first is None or second is None:
        return None
    return first + second


def _schema_name(renamed: Mapping[str, Optional[str]], name: str) -> Optional[str]:
    """
    The schema's name for what the run now calls `name`, from the
    schema names the run renamed or dropped and the names they have now.
    """
    wanted = name.lower()
    for schema, now in renamed.items():
        if now == wanted:
            return schema
    if wanted in renamed:
        # The schema's object of that name was renamed away or dropped.
        return None
    return wanted


def _move(renamed: Dict[str, Optional[str]], old: str, new: Optional[str]) -> None:
    """Records that what the run calls `old` is now called `new`."""
    schema = _schema_name(renamed, old)
    if schema is not None:
        renamed[schema] = new
