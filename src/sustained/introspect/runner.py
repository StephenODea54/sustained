"""
Running a schema read. introspect_schema() drives a dialect's plan on a
blocking connection and async_introspect_schema() on an async adapter,
both inside the same savepoint guard.

run_plan() and async_run_plan() are the two loops, for any plan that
yields SQL and takes rows back. The impact analysis reads its server
facts through them too.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    cast,
)

from sustained.dialects import Dialects
from sustained.execution import cursor_scope
from sustained.introspect.duckdb import _duckdb_plan
from sustained.introspect.information_schema import (
    ATHENA_CATALOG,
    PRESTO_CATALOG,
    _information_schema_plan,
)
from sustained.introspect.model import (
    ReadPlan,
    SchemaPlan,
    SchemaRecorder,
    Snapshot,
)
from sustained.introspect.mssql import _mssql_plan
from sustained.introspect.mysql import _mysql_plan
from sustained.introspect.postgres import _postgres_plan
from sustained.introspect.sqlite import _sqlite_plan
from sustained.types import Connection, RowValue

if TYPE_CHECKING:
    from sustained.aio import AsyncAdapter

T = TypeVar("T")

# The savepoint a guarded read takes before each statement. A read
# releases it whether the statement worked or failed. ROLLBACK TO
# SAVEPOINT leaves the savepoint in place, so without the release one
# savepoint per failed statement would pile up until the outer
# transaction ends. A driver that takes SAVEPOINT and refuses RELEASE
# SAVEPOINT must not lose the read, so the release ignores its own
# error: the rows are in hand and the transaction is not lost.
_READ_SAVEPOINT = "sustained_read"


def _dooms_transaction(dialect: Dialects) -> bool:
    """
    Whether one failed statement stops every later statement in the same
    transaction. A plan tries a view and falls back when it is not there,
    so on such an engine the failure of one query would carry away the
    whole read. Postgres works this way. Each statement runs inside a
    savepoint there, and a failure rolls back to it.
    """
    return dialect == Dialects.POSTGRES


def introspect_schema(
    connection: Connection,
    dialect: Dialects = Dialects.DEFAULT,
    schemas: Sequence[str] = (),
) -> Snapshot:
    """
    Reads tables, columns, primary keys, unique constraints, foreign keys,
    defaults, indexes, check constraints, and column comments from the
    database. Comments come from pg_description on Postgres,
    information_schema.columns on MySQL, Presto, and Athena, and
    duckdb_columns() on DuckDB, and sys.extended_properties on MSSQL;
    SQLite stores none, so its snapshots leave comments_read False. The
    default dialect reads SQLite's PRAGMA tables and the table SQL in
    sqlite_master. Postgres reads information_schema together with
    pg_index, pg_constraint, and pg_enum, so every index is visible,
    varchar lengths and numeric precision survive, enum columns report
    their type's name and values, foreign keys resolve with their names
    and actions, each check belongs to its own table, and the snapshot
    carries the database's enum types. MySQL and MariaDB add information_schema.statistics, MSSQL
    adds sys.indexes, and DuckDB adds duckdb_indexes(), so plain indexes
    are visible on those engines too. Other dialects read plain
    information_schema and degrade to column-only data when constraint
    views are unavailable. Names are keyed lowercase.
    """
    return run_plan(connection, dialect, _schema_plan(dialect, tuple(schemas)))


def run_plan(connection: Connection, dialect: Dialects, plan: ReadPlan[T]) -> T:
    """
    Drives a read plan on a blocking connection and returns what it
    read. On an engine where one failed statement stops the transaction,
    each statement runs inside a savepoint, so a failure the plan
    recovers from leaves the transaction usable.
    """
    guarded = [_dooms_transaction(dialect)]
    # An open transaction reads on its own cursor: a rehearsal introspects
    # a schema its uncommitted statements just changed, and on DuckDB a
    # fresh cursor is a separate session that cannot see it. One cursor
    # serves the whole plan and is given back when the read finishes; a
    # plan is a dozen or more queries, and the last one's rows would sit
    # unread on it otherwise.
    with cursor_scope(connection) as cursor:

        def release() -> None:
            """Takes the savepoint off the stack, ignoring a refusal."""
            try:
                cursor.execute(f"RELEASE SAVEPOINT {_READ_SAVEPOINT}")
            except Exception:
                pass

        def run(sql: str) -> List[Sequence[RowValue]]:
            if not guarded[0]:
                cursor.execute(sql)
                return list(cursor.fetchall())
            try:
                cursor.execute(f"SAVEPOINT {_READ_SAVEPOINT}")
            except Exception:
                # A connection in autocommit has no transaction to take a
                # savepoint in, and no transaction to lose either.
                guarded[0] = False
                cursor.execute(sql)
                return list(cursor.fetchall())
            try:
                cursor.execute(sql)
                rows = list(cursor.fetchall())
            except Exception:
                try:
                    cursor.execute(f"ROLLBACK TO SAVEPOINT {_READ_SAVEPOINT}")
                except Exception:
                    pass
                release()
                raise
            release()
            return rows

        sql = next(plan)
        while True:
            try:
                rows = run(sql)
            except Exception as error:
                try:
                    sql = plan.throw(error)
                except StopIteration as stop:
                    return cast(T, stop.value)
                continue
            try:
                sql = plan.send(rows)
            except StopIteration as stop:
                return cast(T, stop.value)


async def async_introspect_schema(
    adapter: "AsyncAdapter",
    dialect: Dialects = Dialects.DEFAULT,
    schemas: Sequence[str] = (),
    recorder: Optional["SchemaRecorder"] = None,
) -> Snapshot:
    """
    Reads the schema through an async adapter, returning what
    introspect_schema() returns. Both run the same reading code, so a
    dialect behaves the same on either path.

    `recorder` is given every plan statement and the rows it returned, or
    the error it raised. The savepoints a guarded read takes around each
    statement are not recorded: they are this read's own bookkeeping, and
    a replay takes its own.
    """
    return await async_run_plan(
        adapter, dialect, _schema_plan(dialect, tuple(schemas)), recorder
    )


async def async_run_plan(
    adapter: "AsyncAdapter",
    dialect: Dialects,
    plan: ReadPlan[T],
    recorder: Optional["SchemaRecorder"] = None,
) -> T:
    """
    Drives a read plan on an async adapter, as run_plan() does on a
    blocking connection. `recorder` is given every plan statement and
    what it returned or raised.
    """
    guarded = _dooms_transaction(dialect)

    async def release() -> None:
        """Takes the savepoint off the stack, ignoring a refusal."""
        try:
            await adapter.execute(f"RELEASE SAVEPOINT {_READ_SAVEPOINT}", ())
        except Exception:
            pass

    async def run(sql: str) -> List[Sequence[RowValue]]:
        nonlocal guarded
        if not guarded:
            _, rows = await adapter.fetch(sql, ())
            return list(rows)
        try:
            await adapter.execute(f"SAVEPOINT {_READ_SAVEPOINT}", ())
        except Exception:
            # No transaction to take a savepoint in, and none to lose.
            # The refusal stands for the whole read, the way it does on
            # the blocking path, so the plan does not try a savepoint
            # again for every statement left in it.
            guarded = False
            return list((await adapter.fetch(sql, ()))[1])
        try:
            _, rows = await adapter.fetch(sql, ())
        except Exception:
            try:
                await adapter.execute(f"ROLLBACK TO SAVEPOINT {_READ_SAVEPOINT}", ())
            except Exception:
                pass
            await release()
            raise
        await release()
        return list(rows)

    sql = next(plan)
    while True:
        try:
            rows = await run(sql)
        except Exception as error:
            if recorder is not None:
                recorder.record(sql, [], error)
            try:
                sql = plan.throw(error)
            except StopIteration as stop:
                return cast(T, stop.value)
            continue
        if recorder is not None:
            recorder.record(sql, list(rows))
        try:
            sql = plan.send(rows)
        except StopIteration as stop:
            return cast(T, stop.value)


# The plan each dialect reads its schema with. A dialect missing here
# reads plain information_schema the way Presto does.
_SCHEMA_PLANS: Dict[Dialects, Callable[[Tuple[str, ...]], SchemaPlan]] = {
    Dialects.DEFAULT: lambda schemas: _sqlite_plan(),
    Dialects.MYSQL: _mysql_plan,
    Dialects.POSTGRES: _postgres_plan,
    Dialects.MSSQL: _mssql_plan,
    Dialects.DUCKDB: _duckdb_plan,
    Dialects.ATHENA: lambda schemas: _information_schema_plan(ATHENA_CATALOG, schemas),
}


def _schema_plan(dialect: Dialects, schemas: Tuple[str, ...] = ()) -> SchemaPlan:
    plan = _SCHEMA_PLANS.get(dialect)
    if plan is None:
        return _information_schema_plan(PRESTO_CATALOG, schemas)
    return plan(schemas)
