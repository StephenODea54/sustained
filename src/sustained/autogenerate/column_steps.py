"""
The phases of autogenerate() that work on columns: enum checks, index
lifts around ALTER COLUMN, changed, added, and commented columns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple, Type

from sustained.analysis import MigrationStatement, with_intent
from sustained.autogenerate.diff import _column_type_changed
from sustained.autogenerate.online import (
    constraint_name,
    if_exists,
    not_null_route,
    partitioned_index,
    rebuilt,
    unique_using_index,
)
from sustained.autogenerate.statements import (
    _add_enum_check,
    _add_foreign_key,
    _column_fk_name,
    _column_foreign_key,
    _intent_table,
    _introspected_state,
    _key,
    _lift_statements,
    _lifted_indexes,
    _preserving_state,
    _rebuild_needed,
    _reference_target,
    _refuse_enum_value_removal,
    _relaxed_copy,
    _table_has_rows,
    _table_key,
    _tagged,
)
from sustained.autogenerate.steps import _Generation, _LateForeignKey
from sustained.exceptions import DialectError
from sustained.rebuild import add_column_needs_rebuild, tightening_filler
from sustained.schema import (
    ColumnState,
    Index,
    add_column_statements,
    bare_table_name,
    enum_check_name,
    render_column_sql,
)
from sustained.type_changes import type_change_loses_data
from sustained.types import Expression

if TYPE_CHECKING:
    from sustained.model import Model
    from sustained.schema import ColumnDef


def _enum_checks_off(state: _Generation) -> None:
    """Takes off the enum checks whose values changed."""
    # An enum check whose values changed comes off before the column
    # changes and goes back on after them: SQL Server refuses to alter a
    # column that a CHECK constraint names, and a longer value widens the
    # VARCHAR in the same migration. A dialect that cannot alter in place
    # rebuilds the table, and the rebuilt CREATE TABLE writes the check.
    for model, name, _, expression in state.diff.changed_enum_checks:
        if _rebuild_needed(state.compiler, "change a constraint"):
            state.rebuild(model)
            continue
        if expression is not None:
            table_sql = model._qualified_table_sql(state.compiler)
            constraint = enum_check_name(model.tableName or "", name)
            state.up_steps.append(
                with_intent(
                    state.compiler.compile_drop_constraint(table_sql, constraint),
                    "drop_constraint",
                    _intent_table(model),
                    name,
                    name=constraint,
                )
            )
            state.undo(
                state.compiler.compile_add_check(table_sql, constraint, expression)
            )
        state.enum_check_adds.append((model, name))


def _lift_index_steps(state: _Generation) -> None:
    """Drops the indexes an ALTER COLUMN cannot run under."""
    # An engine that refuses ALTER COLUMN while an index depends on the
    # column, or on the table, gets those indexes dropped before the
    # column changes and created again after the new columns are in.
    # The down steps wrap the reversing statements the same way.
    for table_key, index_name, lifted in _lifted_indexes(
        state.compiler, state.diff, state.actual, state.ignore_changed_columns
    ):
        drop_sql, create_sql = _lift_statements(
            state.compiler,
            state.models_by_table[table_key]._qualified_table_sql(state.compiler),
            state.actual[table_key],
            index_name,
            lifted,
        )
        state.lift_drops.append(drop_sql)
        state.lift_creates.append(create_sql)
    state.up_steps.extend(state.lift_drops)
    state.undo(*state.lift_creates)
    if state.lift_drops and state.compiler.index_drop_waits_for_commit():
        state.transactional = False


def _changed_column_steps(state: _Generation) -> None:
    """ALTER COLUMN for type and nullability changes."""
    # Changed columns: ALTER in place where the dialect can, otherwise
    # mark the table for a rebuild.
    # The state each changed column is left in by its type and
    # nullability statements, for a comment statement that restates the
    # whole column after them.
    if not state.ignore_changed_columns:
        for table, name, actual_desc, expected_desc in state.diff.changed_columns:
            model = state.models_by_table[table.lower()]
            assert model.tableColumns is not None
            coldef = model.tableColumns[name]
            actual_col = state.actual[table.lower()].columns[name.lower()]
            if _rebuild_needed(state.compiler, "change a column"):
                state.rebuild_tables[table.lower()] = model
                continue
            table_sql = model._qualified_table_sql(state.compiler)
            intent_table = _intent_table(model)
            expected_type = state.compiler.compile_column_type(coldef)
            if _column_type_changed(state.compiler, coldef, expected_type, actual_col):
                using = state.type_casts.get(f"{table}.{name}")
                _refuse_enum_value_removal(
                    state.compiler, table, name, coldef, actual_col
                )
                # A narrowing change converts every value, and the down
                # step gives back the type but not what the conversion
                # cut, so the statements carry the destructive mark.
                lossy = type_change_loses_data(
                    state.compiler, coldef, expected_type, actual_col.raw_type
                )
                # A type change keeps the nullability the table has now.
                # Tightening to NOT NULL is a separate step that runs
                # after the backfill, and on MySQL and SQL Server the
                # restated definition would otherwise apply it early.
                changed_state = _preserving_state(
                    state.compiler,
                    coldef,
                    actual_col,
                    expected_type,
                    actual_col.nullable,
                )
                state.restated_states[(table.lower(), name.lower())] = changed_state
                # SQL Server refuses a type change on a column that has a
                # default, and Postgres refuses one whose default does not
                # cast to the new type, so the default comes off around
                # it both ways. The compiler picks the text the up step
                # writes back.
                default_sql = actual_col.restated_default()
                lift_default = (
                    default_sql is not None
                    and not state.compiler.alter_type_keeps_default()
                )
                if lift_default:
                    state.up_steps.append(
                        with_intent(
                            state.compiler.compile_drop_column_default(table_sql, name),
                            "drop_column_default",
                            intent_table,
                            name,
                        )
                    )
                state.up_steps.extend(
                    with_intent(
                        MigrationStatement(statement, destructive=lossy),
                        "alter_column_type",
                        intent_table,
                        name,
                        from_type=actual_col.raw_type,
                        to_type=expected_type,
                        using=using,
                        lossy=lossy,
                    )
                    for statement in state.compiler.compile_alter_column_type(
                        table_sql, name, changed_state, using
                    )
                )
                if lift_default:
                    assert default_sql is not None
                    new_default_sql = state.compiler.lifted_default_sql(
                        (
                            None
                            if coldef.default is None
                            else state.compiler.format_value(coldef.default)
                        ),
                        default_sql,
                    )
                    state.up_steps.append(
                        with_intent(
                            state.compiler.compile_add_column_default(
                                table_sql, name, new_default_sql
                            ),
                            "set_column_default",
                            intent_table,
                            name,
                        )
                    )
                    state.undo(
                        state.compiler.compile_add_column_default(
                            table_sql, name, default_sql
                        )
                    )
                state.undo(
                    *state.compiler.compile_alter_column_type(
                        table_sql,
                        name,
                        _preserving_state(
                            state.compiler,
                            coldef,
                            actual_col,
                            actual_col.raw_type,
                            actual_col.nullable,
                        ),
                    )
                )
                if lift_default:
                    state.undo(
                        state.compiler.compile_drop_column_default(table_sql, name)
                    )
            if actual_col.nullable != coldef.nullable and not coldef.primary_key:
                backfill: List[str] = []
                if not coldef.nullable:
                    filler = tightening_filler(table, name, coldef)
                    backfill = state.compiler.compile_backfill(
                        table_sql,
                        name,
                        expected_type,
                        state.compiler.format_value(filler),
                    )
                    state.backfill_list().extend(
                        _tagged(backfill, "backfill", intent_table, name)
                    )
                changed_state = _preserving_state(
                    state.compiler, coldef, actual_col, expected_type, coldef.nullable
                )
                state.restated_states[(table.lower(), name.lower())] = changed_state
                tighten = state.compiler.compile_alter_column_nullability(
                    table_sql, name, changed_state
                )
                restore = state.compiler.compile_alter_column_nullability(
                    table_sql,
                    name,
                    _preserving_state(
                        state.compiler,
                        coldef,
                        actual_col,
                        expected_type,
                        actual_col.nullable,
                    ),
                )
                if state.online and not coldef.nullable:
                    _set_not_null_online(
                        state, model, table_sql, name, backfill, tighten, restore
                    )
                    continue
                state.up_steps.extend(
                    _tagged(
                        tighten,
                        "drop_not_null" if coldef.nullable else "set_not_null",
                        intent_table,
                        name,
                    )
                )
                state.undo(*restore)


def _enum_checks_on(state: _Generation) -> None:
    """Puts back the enum checks _enum_checks_off() took off."""
    for model, name in state.enum_check_adds:
        assert model.tableColumns is not None
        _add_enum_check(
            state.compiler,
            state.up_steps,
            state.down_steps,
            model._qualified_table_sql(state.compiler),
            model,
            name,
            model.tableColumns[name],
        )


def _add_column_rebuild_scan(state: _Generation) -> None:
    """Marks the tables SQLite cannot ADD COLUMN to for a rebuild."""
    # SQLite refuses some columns in ADD COLUMN, and the rebuilt CREATE
    # TABLE takes them. The scan runs before any column is added, so a
    # table headed for a rebuild gets no ADD COLUMN for its other new
    # columns either.
    if state.compiler.rebuild_strategy() == "rebuild":
        for model, _, coldef in state.diff.new_columns:
            if add_column_needs_rebuild(state.compiler, coldef):
                state.rebuild(model)


def _new_column_steps(state: _Generation) -> None:
    """ADD COLUMN for each new column."""
    # New columns. A NOT NULL column with no value for the rows already
    # there fails the same way on both paths, so the check runs before a
    # table headed for a rebuild is skipped. The rebuild would otherwise
    # copy NULL into the column and fail with a bare constraint error. An
    # empty table has no such rows and takes the column. A column that
    # another new column references goes in first, so the REFERENCES
    # clause names a column that exists.
    for model, name, coldef in _referenced_first(state.diff.new_columns):
        table_key = _table_key(model)
        rebuilding = table_key in state.rebuild_tables
        if (
            not coldef.nullable
            and coldef.default is None
            and coldef.backfill is None
            and not coldef.primary_key
            and _table_has_rows(
                state.connection,
                state.compiler,
                model._qualified_table_sql(state.compiler),
            )
        ):
            raise ValueError(
                f"Cannot add NOT NULL column '{model.tableName}.{name}' "
                "without a default or backfill; existing rows would "
                "have no value."
            )
        if rebuilding:
            continue
        if coldef.primary_key or coldef.autoincrement:
            raise ValueError(
                f"Cannot add '{model.tableName}.{name}' with ALTER TABLE: "
                "primary key and autoincrement columns need a hand-written "
                "migration."
            )
        table_sql = model._qualified_table_sql(state.compiler)
        intent_table = _intent_table(model)
        late = _late_reference(state, coldef)
        if not coldef.nullable and coldef.default is None:
            # A dialect that rebuilds took this table in the scan above.
            # One that can neither alter nor rebuild refuses here.
            _rebuild_needed(state.compiler, "add a NOT NULL column")
            if state.online and not isinstance(coldef.backfill, Expression):
                _new_not_null_online(state, model, table_sql, name, coldef)
                continue
            # Add nullable, backfill, then tighten.
            relaxed = render_column_sql(
                state.compiler,
                name,
                _keyless_copy(coldef, True) if state.online else _relaxed_copy(coldef),
                inline_pk=False,
                include_references=not state.online and not late,
            )
            state.up_steps.extend(
                add_column_statements(
                    state.compiler,
                    table_sql,
                    intent_table,
                    name,
                    relaxed,
                    coldef.comment,
                    nullable=True,
                    has_default=False,
                )
            )
            backfill = state.compiler.compile_backfill(
                table_sql,
                name,
                state.compiler.compile_column_type(coldef),
                state.compiler.format_value(coldef.backfill),
            )
            state.backfill_list().extend(
                _tagged(backfill, "backfill", intent_table, name)
            )
            tighten = state.compiler.compile_alter_column_nullability(
                table_sql,
                name,
                ColumnState.from_column(state.compiler, coldef, nullable=False),
            )
            state.undo(
                *state.compiler.compile_drop_column_statements(
                    table_sql, name, coldef.default is not None
                )
            )
            if state.online:
                loosen = state.compiler.compile_alter_column_nullability(
                    table_sql,
                    name,
                    ColumnState.from_column(state.compiler, coldef, nullable=True),
                )
                _set_not_null_online(
                    state, model, table_sql, name, backfill, tighten, loosen
                )
                _column_keys_online(state, model, table_sql, name, coldef)
                continue
            state.up_steps.extend(_tagged(tighten, "set_not_null", intent_table, name))
            _add_enum_check(
                state.compiler,
                state.up_steps,
                state.down_steps,
                table_sql,
                model,
                name,
                coldef,
            )
            _new_column_foreign_key(state, model, table_sql, name, coldef, late)
            continue
        column_sql = render_column_sql(
            state.compiler,
            name,
            _keyless_copy(coldef, coldef.nullable) if state.online else coldef,
            inline_pk=False,
            include_references=not state.online and not late,
        )
        state.up_steps.extend(
            add_column_statements(
                state.compiler,
                table_sql,
                intent_table,
                name,
                column_sql,
                coldef.comment,
                nullable=coldef.nullable,
                has_default=coldef.default is not None,
            )
        )
        state.undo(
            *state.compiler.compile_drop_column_statements(
                table_sql, name, coldef.default is not None
            )
        )
        if state.online:
            _column_keys_online(state, model, table_sql, name, coldef)
            continue
        _add_enum_check(
            state.compiler,
            state.up_steps,
            state.down_steps,
            table_sql,
            model,
            name,
            coldef,
        )
        _new_column_foreign_key(state, model, table_sql, name, coldef, late)


def _referenced_first(
    new_columns: List[Tuple[Type["Model"], str, "ColumnDef"]],
) -> List[Tuple[Type["Model"], str, "ColumnDef"]]:
    """
    The new columns with each column another new column references
    moved in front of the columns, in the order of the diff otherwise.
    """
    referenced = {_reference_target(coldef) for _, _, coldef in new_columns}
    first = [
        _key(model.tableName or "", (name,)) in referenced
        for model, name, _ in new_columns
    ]
    return [e for e, f in zip(new_columns, first) if f] + [
        e for e, f in zip(new_columns, first) if not f
    ]


def _late_reference(state: _Generation, coldef: "ColumnDef") -> bool:
    """
    Whether, without online, a new column's foreign key points at a
    unique index that _index_steps() builds, which is not there when
    ADD COLUMN runs, so the key goes in after the index.
    """
    if state.online or not state.compiler.supports_add_constraint():
        return False
    target = _reference_target(coldef)
    return target is not None and target in state.index_keys


def _new_column_foreign_key(
    state: _Generation,
    model: Type["Model"],
    table_sql: str,
    name: str,
    coldef: "ColumnDef",
    late: bool,
) -> None:
    """
    The foreign key of a new column added without online: the dialect's
    own statement after the column, or, when `late`, an ADD CONSTRAINT
    that _late_foreign_key_steps() puts after the index it points at.
    On a dialect that writes REFERENCES beside the column, the late key
    takes the name the server gives that clause.
    """
    if not late:
        _add_foreign_key(
            state.compiler,
            state.up_steps,
            state.down_steps,
            table_sql,
            model,
            name,
            coldef,
        )
        return
    fkey = (
        constraint_name(bare_table_name(model.tableName or ""), name, "fkey")
        if state.compiler.inline_references()
        else _column_fk_name(model, name)
    )
    fk = _column_foreign_key(state.compiler, model, table_sql, name, coldef, fkey)
    state.late_foreign_keys.append(
        _LateForeignKey(
            fk.add, fk.drop, table_sql, _intent_table(model), fkey, fk.target
        )
    )


def _restore_lifted_index_steps(state: _Generation) -> None:
    """Creates again the indexes _lift_index_steps() dropped."""
    state.up_steps.extend(state.lift_creates)
    state.undo(*state.lift_drops)


def _comment_steps(state: _Generation) -> None:
    """The comment statements for each drifted column comment."""
    # Comment changes. The down step writes the database's old comment
    # back. MySQL restates the whole column, so both directions restate
    # the column as the table has it: the state the type and nullability
    # statements above leave, or else the catalog's own report. The
    # model's declaration would change the type or the default in a
    # statement meant for the comment, and down would not change it back.
    for table, name, actual_comment, expected_comment in state.diff.changed_comments:
        model = state.models_by_table[table.lower()]
        assert model.tableColumns is not None
        coldef = model.tableColumns[name]
        table_sql = model._qualified_table_sql(state.compiler)
        column_state = state.restated_states.get((table.lower(), name.lower()))
        if column_state is None:
            column_state = _introspected_state(
                state.actual[table.lower()].columns[name.lower()]
            )
        try:
            set_new = state.compiler.compile_set_column_comment(
                table_sql, name, expected_comment, coldef, column_state
            )
            set_old = state.compiler.compile_set_column_comment(
                table_sql, name, actual_comment, coldef, column_state
            )
        except DialectError as error:
            # Athena reports comments but cannot change one in place.
            # The drift is real and worth saying, but it must not stop
            # the rest of the migration from being generated.
            state.diff.constraint_notes.append(
                f"{table}.{name} comment is {actual_comment or 'none'}, "
                f"model declares {expected_comment or 'none'}: {error}"
            )
            continue
        state.up_steps.extend(
            _tagged(set_new, "set_column_comment", _intent_table(model), name)
        )
        state.undo(*set_old)


def _new_not_null_online(
    state: _Generation,
    model: Type["Model"],
    table_sql: str,
    name: str,
    coldef: "ColumnDef",
) -> None:
    """
    With online, a new NOT NULL column whose backfill is a value, or
    that needs none because the table is empty. The column goes in NOT
    NULL with the backfill as its default, which PostgreSQL stores in
    the catalog and returns for the rows already there without writing
    them, and the default comes off in the same migration. No row is
    ever NULL, so the column needs no backfill and no check.
    """
    intent_table = _intent_table(model)
    column_sql = render_column_sql(
        state.compiler,
        name,
        _keyless_copy(coldef, False, coldef.backfill),
        inline_pk=False,
        include_references=False,
    )
    state.up_steps.extend(
        add_column_statements(
            state.compiler,
            table_sql,
            intent_table,
            name,
            column_sql,
            coldef.comment,
            nullable=False,
            has_default=coldef.backfill is not None,
        )
    )
    if coldef.backfill is not None:
        state.up_steps.append(
            with_intent(
                state.compiler.compile_drop_column_default(table_sql, name),
                "drop_column_default",
                intent_table,
                name,
            )
        )
    state.undo(
        *state.compiler.compile_drop_column_statements(
            table_sql, name, coldef.default is not None
        )
    )
    _column_keys_online(state, model, table_sql, name, coldef)


def _set_not_null_online(
    state: _Generation,
    model: Type["Model"],
    table_sql: str,
    name: str,
    backfill: List[str],
    tighten: List[str],
    loosen: List[str],
) -> None:
    """
    With online, SET NOT NULL through a check in the online migration,
    and its DROP NOT NULL, `loosen`, in that migration's down step.
    `backfill` is the column's backfill, which the route runs again once
    the check is in.
    """
    intent_table = _intent_table(model)
    route, cleanup = not_null_route(
        state.compiler,
        table_sql,
        intent_table,
        bare_table_name(model.tableName or ""),
        name,
        backfill,
        tighten,
    )
    state.online_up["not_null"].extend(route)
    state.online_up["cleanup"].extend(cleanup)
    state.undo(*loosen, group="not_null")


def _column_keys_online(
    state: _Generation,
    model: Type["Model"],
    table_sql: str,
    name: str,
    coldef: "ColumnDef",
) -> None:
    """
    With online, the UNIQUE and REFERENCES clauses of a new column, which
    the ADD COLUMN left off. The unique index builds concurrently in the
    online migration, which then attaches it as the constraint. On a
    partitioned table the unique index is built on each partition and
    stays an index, since PostgreSQL does not take ADD CONSTRAINT ...
    USING INDEX there. The foreign key goes in once the key it points
    at is there, as _late_foreign_key_steps() orders it. Both take the
    names PostgreSQL gives the clauses.
    """
    table = _intent_table(model)
    bare = bare_table_name(model.tableName or "")
    actual_table = state.actual.get(_table_key(model))
    partitioned = actual_table is not None and actual_table.partitioned
    if coldef.unique and not coldef.primary_key:
        key = constraint_name(bare, name, "key")
        if partitioned:
            assert actual_table is not None
            state.online_up["index"].extend(
                partitioned_index(
                    state.compiler,
                    table_sql,
                    table,
                    Index(key, name, unique=True),
                    actual_table.partitions,
                )
            )
            state.undo(
                if_exists(state.compiler.compile_drop_index(key, table_sql)),
                group="index",
            )
        else:
            state.online_up["index"].extend(
                rebuilt(
                    with_intent(
                        state.compiler.compile_drop_index(key, table_sql),
                        "drop_index",
                        table,
                        name=key,
                    ),
                    with_intent(
                        state.compiler.compile_create_index(
                            key, table_sql, [name], True
                        ),
                        "create_index",
                        table,
                        name=key,
                        columns=(name,),
                        unique=True,
                    ),
                )
            )
            state.online_up["index"].append(
                unique_using_index(state.compiler, table_sql, table, key)
            )
            state.undo(
                state.compiler.compile_drop_constraint(table_sql, key), group="index"
            )
        state.online_keys.add(_key(bare, (name,)))
    if coldef.references is None:
        return
    fkey = constraint_name(bare, name, "fkey")
    fk = _column_foreign_key(state.compiler, model, table_sql, name, coldef, fkey)
    state.late_foreign_keys.append(
        _LateForeignKey(
            fk.add,
            fk.drop,
            table_sql,
            table,
            fkey,
            fk.target,
            validated=True,
            partitioned=partitioned,
        )
    )


def _keyless_copy(
    coldef: "ColumnDef", nullable: bool, default: object = None
) -> "ColumnDef":
    """
    A copy of a ColumnDef without UNIQUE, for an ADD COLUMN whose key
    the online migration builds, with the nullability given. `default`,
    when given, stands in for the column's default.
    """
    from sustained.schema import ColumnDef

    return ColumnDef(
        coldef.type_name,
        length=coldef.length,
        precision=coldef.precision,
        scale=coldef.scale,
        nullable=nullable,
        default=coldef.default if default is None else default,
        references=coldef.references,
        enum_name=coldef.enum_name,
        enum_values=coldef.enum_values,
    )
