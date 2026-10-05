"""
The state autogenerate() threads through its phases, and the phases
that work on whole tables: renames, enum types, new tables, rebuilds,
indexes, constraints, and drops. The column phases are in
sustained.autogenerate.column_steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, NamedTuple, Set, Tuple, Type

from sustained.analysis import with_intent
from sustained.autogenerate.diff import SchemaDiff, _enum_value_additions
from sustained.autogenerate.online import (
    ONLINE_GROUPS,
    concurrently,
    if_exists,
    not_valid,
    partitioned_index,
    rebuilt,
    transient_drop,
    unique_using_index,
    validate,
)
from sustained.autogenerate.statements import (
    KeyTarget,
    _add_enum_check,
    _create_table_steps,
    _declared_fk_intent,
    _declared_fk_sql,
    _declared_table_sql,
    _deferred_foreign_key_steps,
    _extra_table_drops,
    _foreign_keys_setting,
    _intent_table,
    _introspected_fk_sql,
    _key,
    _rebuild_needed,
    _reported_intent_table,
    _spelled_columns,
    _table_key,
    _tagged,
)
from sustained.compilers.base import table_qualifier
from sustained.introspect import IntrospectedIndex, Snapshot
from sustained.rebuild import (
    rebuild_renames_under_legacy,
    rebuild_steps,
    rebuild_turns_foreign_keys_off,
)
from sustained.schema import (
    ColumnState,
    Index,
    bare_table_name,
    create_index_sql,
    create_index_statement,
)
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model


class _LateForeignKey(NamedTuple):
    """
    A foreign key that goes in after the phase that builds the key it
    points at: its ADD CONSTRAINT and DROP CONSTRAINT statements, the
    table it is on as SQL and as an intent names it, its name, and the
    (table, columns) of the key it points at. With online, `validated`
    says the key goes in NOT VALID and the online migration validates
    it, and `partitioned` that its table is partitioned, where
    PostgreSQL before 18 refuses NOT VALID on a foreign key.
    """

    add: str
    drop: str
    table_sql: str
    table: str
    name: str
    target: KeyTarget
    validated: bool = False
    partitioned: bool = False


class _Generation:
    """
    What autogenerate() threads through its phases: the inputs every
    phase reads, the up and down steps they append to in order, and
    what one phase leaves for a later one.

    With `online`, which autogenerate() sets on PostgreSQL only, the
    phases put the statements that read or write rows in `online_up`
    instead, by the group of sustained.autogenerate.online they run in,
    and their down steps in `online_down`, for the migration that runs
    outside the DDL transaction.
    """

    def __init__(
        self,
        connection: Connection,
        compiler: "Compiler",
        diff: SchemaDiff,
        actual: Snapshot,
        models_by_table: Dict[str, Type["Model"]],
        allow_drops: bool,
        ignore_changed_columns: bool,
        type_casts: Dict[str, str],
        online: bool = False,
    ) -> None:
        self.connection = connection
        self.compiler = compiler
        self.diff = diff
        self.actual = actual
        self.models_by_table = models_by_table
        self.allow_drops = allow_drops
        self.ignore_changed_columns = ignore_changed_columns
        self.type_casts = type_casts
        self.up_steps: List[str] = []
        self.down_steps: List[str] = []
        self.reversible = True
        self.transactional = True
        self.online = online
        self.online_up: Dict[str, List[str]] = {g: [] for g in ONLINE_GROUPS}
        self.online_down: Dict[str, List[str]] = {g: [] for g in ONLINE_GROUPS}
        self.online_reversible = True
        # The (table, columns) of each unique key the online migration
        # builds, which a foreign key added in the run may point at.
        self.online_keys: Set[KeyTarget] = set()
        # The (table, columns) of each unique key _index_steps() builds,
        # and of each new column declared UNIQUE, which a foreign key in
        # the same diff may point at.
        self.index_keys: Set[KeyTarget] = {
            _key(model.tableName or "", index.columns)
            for model, index in diff.new_indexes
            + [(model, index) for model, index, _ in diff.changed_indexes]
            if index.unique
        }
        self.column_keys: Set[KeyTarget] = {
            _key(model.tableName or "", (name,))
            for model, name, coldef in diff.new_columns
            if coldef.unique and not coldef.primary_key
        }
        # The foreign keys _late_foreign_key_steps() adds once the keys
        # they point at are built.
        self.late_foreign_keys: List[_LateForeignKey] = []
        self.rebuild_tables: Dict[str, Type["Model"]] = {}
        # Enum types this migration creates, dropped last on the way down.
        self.created_enum_types: List[str] = []
        # Enum columns whose check comes off before the column changes
        # and goes back on after them.
        self.enum_check_adds: List[Tuple[Type["Model"], str]] = []
        # Indexes dropped before the column changes and created again
        # after the new columns are in.
        self.lift_drops: List[str] = []
        self.lift_creates: List[str] = []
        # The state each changed column is left in by its type and
        # nullability statements, for a comment statement that restates
        # the whole column after them.
        self.restated_states: Dict[Tuple[str, str], ColumnState] = {}

    def rebuild(self, model: Type["Model"]) -> None:
        """Sends the model's table to the rebuild path."""
        self.rebuild_tables[_table_key(model)] = model

    def skip(self, model: Type["Model"]) -> bool:
        """Whether the model's table goes through the rebuild path."""
        return _table_key(model) in self.rebuild_tables


def _refuse_undeclared(
    diff: SchemaDiff, allow_drops: bool, ignore_undeclared: bool
) -> None:
    """Refuses a diff with objects the models do not declare, unless told."""
    if (
        (
            diff.extra_tables
            or diff.extra_columns
            or diff.extra_indexes
            or diff.extra_foreign_keys
        )
        and not allow_drops
        and not ignore_undeclared
    ):
        dropped = (
            list(diff.extra_tables)
            + [f"{t}.{c}" for t, c in diff.extra_columns]
            + [f"index {n}" for _, n, _ in diff.extra_indexes]
            + [f"foreign key {n}" for _, n, _ in diff.extra_foreign_keys]
        )
        raise ValueError(
            "The database has objects the models do not declare: "
            f"{', '.join(dropped)}. Pass allow_drops=True to generate the "
            "drops, or add them to exclude_tables."
        )


def _table_rename_steps(state: _Generation, table_renames: Dict[str, str]) -> None:
    """RENAME TABLE for each table rename hint."""
    for old, new in table_renames.items():
        # The renamed table keeps its schema, so the old name takes the
        # schema the model declares for the new one.
        new_sql = _declared_table_sql(
            state.compiler, state.models_by_table, state.actual, new
        )
        old_sql = table_qualifier(new_sql) + state.compiler.quote_ddl_identifier(old)
        new_model = state.models_by_table.get(new.lower())
        schema = None if new_model is None else new_model.tableSchema
        state.up_steps.append(
            with_intent(
                state.compiler.compile_rename_table(old_sql, new_sql),
                "rename_table",
                f"{schema}.{old}" if schema else old,
                new=new,
            )
        )
        state.down_steps.insert(
            0, state.compiler.compile_rename_table(new_sql, old_sql)
        )


def _column_rename_steps(state: _Generation, renames: Dict[str, str]) -> None:
    """
    RENAME COLUMN for each column rename hint. On a dialect that keeps an
    enum column to its values with a named CHECK, the check named after
    the old column comes off before the rename and goes back on under the
    new name after it, since the engine renames the column inside the
    expression but keeps the constraint's name. A dialect that cannot add
    a constraint in place rebuilds the table instead.
    """
    for path, new_name in renames.items():
        table, old_name = path.rsplit(".", 1)
        table_sql = _declared_table_sql(
            state.compiler, state.models_by_table, state.actual, table
        )
        moved = state.actual.renamed_enum_checks.get((table.lower(), new_name.lower()))
        model = state.models_by_table.get(table.lower())
        coldef = None
        if model is not None:
            coldef = (model.tableColumns or {}).get(new_name)
        renames_check = (
            moved is not None
            and coldef is not None
            and coldef.type_name == "ENUM"
            and state.compiler.enum_strategy() == "check"
        )
        if renames_check and _rebuild_needed(state.compiler, "change a constraint"):
            assert model is not None
            state.rebuild_tables[table.lower()] = model
            renames_check = False
        if renames_check:
            assert moved is not None and model is not None
            old_check, expression = moved
            state.up_steps.append(
                with_intent(
                    state.compiler.compile_drop_constraint(table_sql, old_check),
                    "drop_constraint",
                    _intent_table(model),
                    old_name,
                    name=old_check,
                )
            )
            state.down_steps.insert(
                0, state.compiler.compile_add_check(table_sql, old_check, expression)
            )
        state.up_steps.append(
            with_intent(
                state.compiler.compile_rename_column(table_sql, old_name, new_name),
                "rename_column",
                _reported_intent_table(state.models_by_table, state.actual, table),
                old_name,
                new=new_name,
            )
        )
        state.down_steps.insert(
            0, state.compiler.compile_rename_column(table_sql, new_name, old_name)
        )
        if renames_check:
            assert model is not None and coldef is not None
            _add_enum_check(
                state.compiler,
                state.up_steps,
                state.down_steps,
                table_sql,
                model,
                new_name,
                coldef,
            )


def _enum_type_steps(state: _Generation) -> None:
    """Creates new enum types and appends declared values."""
    # Enum types first, before any table or column that references them.
    # New types are created; declared values that extend the database's
    # list are appended with ADD VALUE, which no engine takes back, so
    # such a migration has no down. Any other value change cannot run in
    # place and refuses with the recipe.
    for type_name, values in state.diff.new_enum_types:
        state.up_steps.append(
            with_intent(
                state.compiler.compile_create_enum_type(type_name, list(values)),
                "create_enum_type",
                None,
                name=type_name,
            )
        )
        state.created_enum_types.append(type_name)
    for type_name, actual_values, expected_values in state.diff.changed_enum_types:
        additions = _enum_value_additions(actual_values, expected_values)
        if additions is None:
            raise ValueError(
                f"Enum '{type_name}' has values removed or reordered: the "
                f"database has ({', '.join(actual_values)}), the models "
                f"declare ({', '.join(expected_values)}). The engine "
                "cannot do that in place. Write a migration that creates "
                "a new type, converts each column with ALTER COLUMN ... "
                "USING, and drops the old type."
            )
        for value in additions:
            state.up_steps.append(
                with_intent(
                    state.compiler.compile_add_enum_value(type_name, value),
                    "add_enum_value",
                    None,
                    name=type_name,
                    value=value,
                )
            )
        state.reversible = False


def _new_table_steps(state: _Generation) -> None:
    """CREATE TABLE for each missing table."""
    # New tables. Where the dialect can add a constraint to a table that
    # already exists, every foreign key is left out of CREATE TABLE and
    # added afterwards, so two new tables may point at each other in any
    # order. Where it cannot, the tables were sorted into dependency
    # order by the diff and the keys stay inside CREATE TABLE.
    defer_foreign_keys = state.compiler.supports_add_constraint()
    table_downs: List[str] = []
    fk_downs: List[str] = []
    for model in state.diff.missing_tables:
        state.up_steps.extend(
            _create_table_steps(state.compiler, model, defer_foreign_keys)
        )
        table_downs.insert(
            0, f"DROP TABLE IF EXISTS {model._qualified_table_sql(state.compiler)}"
        )
    # A key that points at a column or a unique index the same diff
    # adds to a table that exists goes in after that column and index.
    built = state.index_keys | state.column_keys
    if defer_foreign_keys:
        for model in state.diff.missing_tables:
            for add_sql, drop_sql, target in _deferred_foreign_key_steps(
                state.compiler, model
            ):
                if target in built:
                    state.late_foreign_keys.append(
                        _LateForeignKey(
                            add_sql,
                            drop_sql,
                            model._qualified_table_sql(state.compiler),
                            _intent_table(model),
                            _intent_name(add_sql),
                            target,
                        )
                    )
                    continue
                state.up_steps.append(add_sql)
                fk_downs.insert(0, drop_sql)
    state.down_steps[0:0] = fk_downs + table_downs


def _intent_name(statement: str) -> str:
    """The constraint name the intent of an ADD CONSTRAINT statement gives."""
    intent = getattr(statement, "intent", None)
    return "" if intent is None else str(intent.get("name"))


def _late_foreign_key_steps(state: _Generation) -> None:
    """
    Adds the foreign keys that point at a key built after them: a new
    table's key to a column or unique index the diff adds, and, with
    online, a new column's key. With online, a key whose target the
    online migration builds goes in the online `constraint` group, after
    the `index` group has built the target.
    """
    for fk in state.late_foreign_keys:
        if not state.online:
            state.up_steps.append(fk.add)
            state.down_steps.insert(0, fk.drop)
            continue
        late = fk.target in state.online_keys
        up, down = (
            (state.online_up["constraint"], state.online_down["constraint"])
            if late or fk.partitioned
            else (state.up_steps, state.down_steps)
        )
        down.insert(0, fk.drop)
        if fk.partitioned or not (late or fk.validated):
            # PostgreSQL before 18 refuses NOT VALID on a partitioned
            # table, and a new table has no rows to validate.
            up.append(fk.add)
            continue
        up.append(not_valid(fk.add))
        state.online_up["validate"].append(
            validate(state.compiler, fk.table_sql, fk.table, fk.name)
        )


def _constraint_rebuild_scan(state: _Generation) -> None:
    """Marks the tables whose constraints change for a rebuild."""
    # A dialect that cannot alter a table in place takes its constraint
    # changes through the rebuild: the rebuilt CREATE TABLE renders the
    # declared tableConstraints. Extra and changed constraints only
    # trigger a rebuild under allow_drops, since replacing the table
    # drops what the declaration does not carry.
    if not state.compiler.supports_alter_column():
        constrained_tables: List[Type["Model"]] = [
            model for model, _ in state.diff.new_foreign_keys
        ]
        constrained_tables += [model for model, _ in state.diff.new_checks]
        constrained_tables += [model for model, _, _ in state.diff.changed_checks]
        if state.allow_drops:
            constrained_tables += [
                model for model, _, _ in state.diff.changed_foreign_keys
            ]
            constrained_tables += [
                state.models_by_table[table.lower()]
                for table, _, _ in state.diff.extra_foreign_keys
            ]
            constrained_tables += [
                state.models_by_table[table.lower()]
                for table, _, _ in state.diff.extra_checks
            ]
            constrained_tables += [
                state.models_by_table[table.lower()]
                for table, _, index in state.diff.extra_indexes
                if index.constraint
            ]
        for model in constrained_tables:
            if _rebuild_needed(state.compiler, "change a constraint"):
                state.rebuild(model)


def _table_rebuild_steps(state: _Generation) -> None:
    """Rebuilds each table marked for a rebuild."""
    # Table rebuilds for SQLite consume every remaining change on the
    # table. They run between the statements that turn foreign key
    # enforcement off and on again, since dropping the old table would
    # otherwise fail while rows in another table still point at it.
    if state.rebuild_tables:
        # The pragma statements only land outside a transaction, so a
        # migration that carries them runs bare. A rebuild nothing points
        # at needs no pragma and keeps its transaction.
        guarded = rebuild_turns_foreign_keys_off(state.actual, state.rebuild_tables)
        if guarded:
            state.up_steps.extend(
                _foreign_keys_setting(state.compiler.rebuild_setup_sql(), "OFF")
            )
        legacy_rename = rebuild_renames_under_legacy(state.actual)
        for table_key, model in state.rebuild_tables.items():
            state.up_steps.extend(
                _tagged(
                    rebuild_steps(
                        state.compiler,
                        model,
                        state.actual[table_key],
                        state.allow_drops,
                        legacy_rename,
                    ),
                    "rebuild_table",
                    _intent_table(model),
                )
            )
        if guarded:
            state.up_steps.extend(
                _foreign_keys_setting(state.compiler.rebuild_finish_sql(), "ON")
            )
            state.transactional = False
        state.reversible = False


def _index_steps(state: _Generation) -> None:
    """Creates and rebuilds declared indexes."""
    up_steps = state.up_steps
    down_steps = state.down_steps
    # Index changes. A rebuilt table takes its declared indexes from the
    # rebuild, and its old indexes went with the old table, so none of
    # these statements apply to it.
    # With online, every index is built and dropped concurrently in the
    # migration that runs outside the DDL transaction, and on a
    # partitioned table on each partition.
    if state.online:
        up_steps = state.online_up["index"]
        down_steps = state.online_down["index"]
    for model, index in state.diff.new_indexes:
        if state.skip(model):
            continue
        table_sql = model._qualified_table_sql(state.compiler)
        intent_table = _intent_table(model)
        create = create_index_statement(state.compiler, table_sql, intent_table, index)
        drop = state.compiler.compile_drop_index(index.name, table_sql)
        if state.online:
            up_steps.extend(_built_online(state, model, index))
            down_steps.insert(0, _dropped_online(state, model, drop))
        else:
            up_steps.append(create)
            down_steps.insert(0, drop)
        if index.unique:
            state.online_keys.add(_key(model.tableName or "", index.columns))
    for model, index, actual_index in state.diff.changed_indexes:
        if state.skip(model):
            continue
        table_sql = model._qualified_table_sql(state.compiler)
        intent_table = _intent_table(model)
        drop = with_intent(
            state.compiler.compile_drop_index(index.name, table_sql),
            "drop_index",
            intent_table,
            name=index.name,
        )
        actual_columns = _spelled_columns(state.actual[_table_key(model)], actual_index)
        if state.online:
            up_steps.extend(_replaced_online(state, model, drop, index))
        else:
            up_steps.append(drop)
            up_steps.append(
                create_index_statement(state.compiler, table_sql, intent_table, index)
            )
        # An invalid index is rebuilt, and the down step leaves the
        # valid one in its place: the invalid index did nothing.
        if actual_index.valid:
            restore: List[str]
            if state.online:
                restore = _replaced_online(
                    state,
                    model,
                    str(drop),
                    Index(index.name, *actual_columns, unique=actual_index.unique),
                    intent=False,
                )
            else:
                restore = [
                    state.compiler.compile_drop_index(index.name, table_sql),
                    state.compiler.compile_create_index(
                        index.name, table_sql, actual_columns, actual_index.unique
                    ),
                ]
            down_steps[0:0] = restore
        if index.unique:
            state.online_keys.add(_key(model.tableName or "", index.columns))
    for model, column, actual_index in state.diff.invalid_keys:
        if state.skip(model):
            continue
        _key_rebuild_steps(state, up_steps, model, column, actual_index)


def _key_rebuild_steps(
    state: _Generation,
    up_steps: List[str],
    model: Type["Model"],
    column: str,
    actual_index: IntrospectedIndex,
) -> None:
    """
    Drops the invalid unique index behind a column declared UNIQUE and
    builds the column's key again under the same name. Without online
    the key goes in as ADD CONSTRAINT ... UNIQUE. With online the index
    builds concurrently and ADD CONSTRAINT ... USING INDEX attaches it,
    except on a partitioned table, where it stays a unique index built
    on each partition. The invalid index enforced nothing, so the down
    step leaves the key in place.
    """
    table_sql = model._qualified_table_sql(state.compiler)
    table = _intent_table(model)
    name = actual_index.name or ""
    drop = with_intent(
        state.compiler.compile_drop_index(name, table_sql),
        "drop_index",
        table,
        name=name,
    )
    if not state.online:
        up_steps.append(drop)
        up_steps.append(
            with_intent(
                state.compiler.compile_add_unique(table_sql, name, [column]),
                "add_unique",
                table,
                name=name,
            )
        )
        return
    up_steps.extend(
        _replaced_online(state, model, drop, Index(name, column, unique=True))
    )
    if not _is_partitioned(state, model):
        up_steps.append(unique_using_index(state.compiler, table_sql, table, name))
    state.online_keys.add(_key(model.tableName or "", (column,)))


def _is_partitioned(state: _Generation, model: Type["Model"]) -> bool:
    """Whether the snapshot reads the model's table as partitioned."""
    found = state.actual.get(_table_key(model))
    return found is not None and found.partitioned


def _built_online(
    state: _Generation,
    model: Type["Model"],
    index: Index,
    intent: bool = True,
) -> List[str]:
    """
    With online, the statements that build an index: on a partitioned
    table, the index ON ONLY the table with an index built concurrently
    on each partition and attached to it, and elsewhere a drop of an
    index an earlier attempt left under the name, then the build. With
    `intent` False, as in a down step, the statements have no intent.
    """
    table_sql = model._qualified_table_sql(state.compiler)
    table = _intent_table(model)
    found = state.actual.get(_table_key(model))
    if found is not None and found.partitioned:
        return list(
            partitioned_index(
                state.compiler,
                table_sql,
                table,
                index.name,
                list(index.columns),
                index.unique,
                found.partitions,
            )
        )
    drop = state.compiler.compile_drop_index(index.name, table_sql)
    if not intent:
        create = create_index_sql(state.compiler, table_sql, index)
        return [str(s) for s in rebuilt(drop, create)]
    return list(
        rebuilt(
            with_intent(drop, "drop_index", table, name=index.name),
            create_index_statement(state.compiler, table_sql, table, index),
        )
    )


def _replaced_online(
    state: _Generation,
    model: Type["Model"],
    drop: str,
    index: Index,
    intent: bool = True,
) -> List[str]:
    """
    With online, the statements that drop an index and build it again:
    the build, which drops the index of the same name first, and on a
    partitioned table, whose build drops nothing, `drop` before it.
    """
    built = _built_online(state, model, index, intent)
    if _is_partitioned(state, model):
        return [_dropped_online(state, model, drop)] + built
    return built


def _dropped_online(state: _Generation, model: Type["Model"], drop: str) -> str:
    """
    With online, a DROP INDEX with IF EXISTS: concurrently, except on a
    partitioned table, where PostgreSQL refuses DROP INDEX CONCURRENTLY
    and a plain drop takes the index of each partition with it.
    """
    if _is_partitioned(state, model):
        return if_exists(drop)
    return concurrently(drop)


def _as_is(statement: str) -> str:
    """The statement unchanged, where the online form is not in use."""
    return statement


def _constraint_steps(state: _Generation) -> None:
    """Adds, changes, and drops constraints in place."""
    # Constraint changes on dialects that alter in place. A table headed
    # for a rebuild gets its constraints from the rebuilt CREATE TABLE.
    # With online, a check or foreign key goes in NOT VALID, and the
    # migration outside the DDL transaction validates it.
    added = not_valid if state.online else _as_is
    for model, check in state.diff.new_checks:
        if state.skip(model):
            continue
        table_sql = model._qualified_table_sql(state.compiler)
        state.up_steps.append(
            added(
                with_intent(
                    state.compiler.compile_add_check(
                        table_sql, check.name, check.expression
                    ),
                    "add_check",
                    _intent_table(model),
                    name=check.name,
                )
            )
        )
        state.down_steps.insert(
            0, state.compiler.compile_drop_constraint(table_sql, check.name)
        )
        _validate_online(state, table_sql, model, check.name)
    for model, fk in state.diff.new_foreign_keys:
        if state.skip(model):
            continue
        table_sql = model._qualified_table_sql(state.compiler)
        # A key the online migration builds is not there yet when the
        # DDL transaction runs, so a foreign key that points at it goes
        # in after the key is built. PostgreSQL before 18 refuses NOT
        # VALID on a foreign key of a partitioned table, so there the
        # key goes in validated, in the online migration.
        late = _key(fk.target_table, fk.target_columns) in state.online_keys
        partitioned = state.online and _is_partitioned(state, model)
        up, down = (
            (state.online_up["constraint"], state.online_down["constraint"])
            if late or partitioned
            else (state.up_steps, state.down_steps)
        )
        statement = _declared_fk_intent(
            _declared_fk_sql(state.compiler, table_sql, fk), _intent_table(model), fk
        )
        up.append(statement if partitioned else added(statement))
        down.insert(0, state.compiler.compile_drop_foreign_key(table_sql, fk.name))
        if not partitioned:
            _validate_online(state, table_sql, model, fk.name)
    # A declared constraint the catalog reads as not validated, such as
    # one whose VALIDATE CONSTRAINT failed, is validated where it is.
    for model, name in state.diff.unvalidated:
        if state.skip(model):
            continue
        table_sql = model._qualified_table_sql(state.compiler)
        (state.online_up["validate"] if state.online else state.up_steps).append(
            validate(state.compiler, table_sql, _intent_table(model), name)
        )
    # A check the online SET NOT NULL route added and a failed run left
    # comes off, unless the route runs again in this diff and drops it.
    for model, name in state.diff.route_checks:
        table_sql = model._qualified_table_sql(state.compiler)
        drop = transient_drop(state.compiler, table_sql, _intent_table(model), name)
        cleanup = state.online_up["cleanup"] if state.online else state.up_steps
        if drop not in cleanup:
            cleanup.append(drop)
    if state.allow_drops:
        for model, fk, actual_fk in state.diff.changed_foreign_keys:
            if state.skip(model):
                continue
            table_sql = model._qualified_table_sql(state.compiler)
            intent_table = _intent_table(model)
            state.up_steps.append(
                with_intent(
                    state.compiler.compile_drop_foreign_key(table_sql, fk.name),
                    "drop_foreign_key",
                    intent_table,
                    name=fk.name,
                )
            )
            state.up_steps.append(
                added(
                    _declared_fk_intent(
                        _declared_fk_sql(state.compiler, table_sql, fk),
                        intent_table,
                        fk,
                    )
                )
            )
            _validate_online(state, table_sql, model, fk.name)
            restore = _introspected_fk_sql(
                state.compiler,
                table_sql,
                fk.name,
                actual_fk,
                state.actual,
                model.tableName or "",
            )
            if restore is None:
                state.reversible = False
            else:
                state.down_steps.insert(0, restore)
                state.down_steps.insert(
                    0, state.compiler.compile_drop_foreign_key(table_sql, fk.name)
                )
        # With online, the drops run in the migration outside the DDL
        # transaction, after everything else, each with IF EXISTS.
        drop_up, drop_down = _drop_lists(state)
        dropped = if_exists if state.online else _as_is
        for table, name, actual_fk in state.diff.extra_foreign_keys:
            if table.lower() in state.rebuild_tables:
                continue
            table_sql = _declared_table_sql(
                state.compiler, state.models_by_table, state.actual, table
            )
            drop_up.append(
                dropped(
                    with_intent(
                        state.compiler.compile_drop_foreign_key(table_sql, name),
                        "drop_foreign_key",
                        _reported_intent_table(
                            state.models_by_table, state.actual, table
                        ),
                        name=name,
                    )
                )
            )
            restore = _introspected_fk_sql(
                state.compiler, table_sql, name, actual_fk, state.actual, table
            )
            if restore is None:
                _irreversible(state)
            else:
                drop_down.insert(0, restore)
        for table, name, expression in state.diff.extra_checks:
            if table.lower() in state.rebuild_tables:
                continue
            table_sql = _declared_table_sql(
                state.compiler, state.models_by_table, state.actual, table
            )
            drop_up.append(
                dropped(
                    with_intent(
                        state.compiler.compile_drop_constraint(table_sql, name),
                        "drop_constraint",
                        _reported_intent_table(
                            state.models_by_table, state.actual, table
                        ),
                        name=name,
                    )
                )
            )
            drop_down.insert(
                0, state.compiler.compile_add_check(table_sql, name, expression)
            )


def _drop_steps(state: _Generation) -> None:
    """Drops the extra indexes, columns, tables, and enum types."""
    up_steps, down_steps = _drop_lists(state)
    # With online, every drop takes IF EXISTS, an index drops
    # concurrently, and the index a down step builds again is built
    # concurrently. On a partitioned table the index drops without
    # CONCURRENTLY, which PostgreSQL refuses there.
    dropped = if_exists if state.online else _as_is
    if state.allow_drops:
        for table, name, actual_index in state.diff.extra_indexes:
            if table.lower() in state.rebuild_tables:
                continue
            table_sql = _declared_table_sql(
                state.compiler, state.models_by_table, state.actual, table
            )
            intent_table = _reported_intent_table(
                state.models_by_table, state.actual, table
            )
            actual_table = state.actual[table.lower()]
            if actual_index.constraint:
                # The index belongs to a UNIQUE constraint, and the engine
                # refuses DROP INDEX on it.
                up_steps.append(
                    dropped(
                        with_intent(
                            state.compiler.compile_drop_constraint(table_sql, name),
                            "drop_constraint",
                            intent_table,
                            name=name,
                        )
                    )
                )
                down_steps.insert(
                    0,
                    state.compiler.compile_add_unique(
                        table_sql, name, _spelled_columns(actual_table, actual_index)
                    ),
                )
                continue
            drop = with_intent(
                state.compiler.compile_drop_index(name, table_sql),
                "drop_index",
                intent_table,
                name=name,
            )
            columns = _spelled_columns(actual_table, actual_index)
            if not state.online:
                up_steps.append(drop)
                down_steps.insert(
                    0,
                    state.compiler.compile_create_index(
                        name, table_sql, columns, actual_index.unique
                    ),
                )
                continue
            model = state.models_by_table[table.lower()]
            up_steps.append(_dropped_online(state, model, drop))
            down_steps[0:0] = _built_online(
                state,
                model,
                Index(name, *columns, unique=actual_index.unique),
                intent=False,
            )
        for table, name in state.diff.extra_columns:
            if table.lower() in state.rebuild_tables:
                continue
            table_sql = _declared_table_sql(
                state.compiler, state.models_by_table, state.actual, table
            )
            intent_table = _reported_intent_table(
                state.models_by_table, state.actual, table
            )
            actual_column = state.actual[table.lower()].columns.get(name)
            drops = state.compiler.compile_drop_column_statements(
                table_sql,
                name,
                actual_column is not None and actual_column.default is not None,
            )
            up_steps.extend(
                with_intent(drop, "drop_column_default", intent_table, name)
                for drop in drops[:-1]
            )
            up_steps.append(
                dropped(with_intent(drops[-1], "drop_column", intent_table, name))
            )
            _irreversible(state)
        if state.diff.extra_tables:
            drops, bare = _extra_table_drops(
                state.compiler, state.actual, state.diff.extra_tables
            )
            up_steps.extend(dropped(drop) for drop in drops)
            if bare and not state.online:
                state.transactional = False
            _irreversible(state)
        # A type drops after every table and column that used it.
        for type_name in state.diff.extra_enum_types:
            up_steps.append(
                dropped(
                    with_intent(
                        state.compiler.compile_drop_enum_type(type_name),
                        "drop_enum_type",
                        None,
                        name=type_name,
                    )
                )
            )


def _drop_lists(state: _Generation) -> Tuple[List[str], List[str]]:
    """The up and down lists the drops go in."""
    if state.online:
        return state.online_up["drop"], state.online_down["drop"]
    return state.up_steps, state.down_steps


def _irreversible(state: _Generation) -> None:
    """Marks the migration a drop goes in as one down() cannot revert."""
    if state.online:
        state.online_reversible = False
    else:
        state.reversible = False


def _validate_online(
    state: _Generation, table_sql: str, model: Type["Model"], name: str
) -> None:
    """With online, validates a constraint the DDL transaction added NOT VALID."""
    if state.online:
        state.online_up["validate"].append(
            validate(state.compiler, table_sql, _intent_table(model), name)
        )


def _created_enum_type_downs(state: _Generation) -> None:
    """Drops the enum types this migration created."""
    # Types created in this migration drop last on the way down, after
    # every table that referenced them is gone.
    for type_name in state.created_enum_types:
        state.down_steps.append(state.compiler.compile_drop_enum_type(type_name))
