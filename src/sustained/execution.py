"""
Execution support: running built queries against a DB-API 2.0 connection
and hydrating rows into model instances.

Bind a connection once with Model.bind(connection), or pass one to run()
per call. Any DB-API 2.0 connection works as long as its paramstyle matches
the dialect's placeholder: qmark for the default and MSSQL dialects
(sqlite3, pyodbc) and format for Postgres (psycopg, psycopg2).
"""

from __future__ import annotations

import importlib
import threading
import time
import warnings
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)

from sustained.exceptions import AmbiguousColumns
from sustained.types import (
    Binding,
    Connection,
    Cursor,
    JoinMappingWithThrough,
    RelationTree,
    RelationType,
    RowValue,
    SqlValue,
    WriteResult,
)

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.dialects import Dialects
    from sustained.model import Model
    from sustained.pool import ConnectionPool
    from sustained.types import AnyQuery


# Connections with an open transaction, keyed by id(). The value holds a
# strong reference to the connection, so the id cannot be reused while the
# entry exists, the current savepoint nesting depth, the cursor the
# transaction runs on, and the id of the thread that opened the block.
# Statements inside the block share that cursor: DuckDB gives every cursor
# its own session, so a statement on a fresh cursor would run outside the
# transaction there.
#
# The dict is read and written from every thread that runs a statement, so
# _TRANSACTION_LOCK guards it. The owner thread id makes a block belong to
# one thread: a second thread that opens transaction() on the same
# connection is refused instead of silently becoming a savepoint inside the
# first thread's transaction, on the first thread's cursor.
_ACTIVE_TRANSACTIONS: Dict[int, Tuple[Connection, int, Cursor, int]] = {}

_TRANSACTION_LOCK = threading.RLock()


def _transaction_entry(
    connection: Connection,
) -> Optional[Tuple[Connection, int, Cursor, int]]:
    """The open transaction on this connection, or None."""
    with _TRANSACTION_LOCK:
        entry = _ACTIVE_TRANSACTIONS.get(id(connection))
    if entry is not None and entry[0] is connection:
        return entry
    return None


def _own_transaction_entry(
    connection: Connection,
) -> Optional[Tuple[Connection, int, Cursor, int]]:
    """
    The open transaction on this connection when the calling thread owns
    it. Another thread's block reads as no transaction here, so its cursor
    is never borrowed.
    """
    entry = _transaction_entry(connection)
    if entry is not None and entry[3] == threading.get_ident():
        return entry
    return None


# Optional observer called after every executed statement.
_statement_listener: Optional[Callable[[str, Tuple[SqlValue, ...], float], None]] = None


def set_statement_listener(
    listener: Optional[Callable[[str, Tuple[SqlValue, ...], float], None]],
) -> None:
    """
    Registers a callable invoked after every statement run() executes, with
    the SQL text, the parameter tuple, and the duration in seconds. Pass
    None to remove the listener. Useful for logging and timing.
    """
    global _statement_listener
    _statement_listener = listener


def notify_statement(sql: str, params: Tuple[SqlValue, ...], duration: float) -> None:
    """Invokes the registered statement listener, if any."""
    if _statement_listener is not None:
        _statement_listener(sql, params, duration)


@contextmanager
def timed_statement(sql: str, params: Tuple[SqlValue, ...]) -> Iterator[None]:
    """
    Times the statement run inside the block and passes it to the
    statement listener. A statement that raises is not reported.
    """
    started = time.perf_counter()
    yield
    notify_statement(sql, params, time.perf_counter() - started)


def execute_timed(cursor: Cursor, sql: str, params: Tuple[SqlValue, ...]) -> None:
    """Executes one statement on the cursor and reports it to the listener."""
    with timed_statement(sql, params):
        cursor.execute(sql, params)


def cursor_columns(cursor: Cursor) -> List[str]:
    """
    The column names of the cursor's result set, or an empty list for a
    statement with no result set.

    Raises:
        AmbiguousColumns: If the result set repeats a column name.
    """
    if not cursor.description:
        return []
    return checked_columns([desc[0] for desc in cursor.description])


# Per-thread stack of connections pinned by transaction() blocks that were
# opened against a pool. Statements inside the block use the pinned
# connection instead of checking a fresh one out.
_thread_state = threading.local()


def _pinned_entry() -> Optional[Tuple["ConnectionPool", Connection]]:
    stack = getattr(_thread_state, "pinned", None)
    return stack[-1] if stack else None


def _pinned_connection() -> Optional[Connection]:
    entry = _pinned_entry()
    return entry[1] if entry is not None else None


def _pin(pool: "ConnectionPool", connection: Connection) -> None:
    stack = getattr(_thread_state, "pinned", None)
    if stack is None:
        stack = []
        _thread_state.pinned = stack
    stack.append((pool, connection))


def _unpin() -> None:
    _thread_state.pinned.pop()


@contextmanager
def connection_scope(
    explicit: Optional[Binding], binding: Optional[Binding]
) -> Iterator[Connection]:
    """
    Resolves the connection for one statement. An explicit argument wins;
    then a connection pinned by an open transaction() block on this thread;
    then the model binding. Pools check a connection out for the scope.

    An explicit pool that already pinned a connection to this thread is the
    one exception: the statement runs on the pinned connection, so a query
    given the pool inside transaction(pool) stays in that transaction. A
    second checkout would commit on its own, and would deadlock against its
    own block on a pool of one connection.
    """
    from sustained.pool import ConnectionPool

    if explicit is not None:
        if isinstance(explicit, ConnectionPool):
            entry = _pinned_entry()
            if entry is not None and entry[0] is explicit:
                yield entry[1]
                return
            with explicit.connection() as conn:
                yield conn
        else:
            yield explicit
        return

    pinned = _pinned_connection()
    if pinned is not None:
        yield pinned
        return

    if binding is None:
        raise RuntimeError(
            "No database connection. Bind one with Model.bind(connection) "
            "or pass it to run()."
        )
    if isinstance(binding, ConnectionPool):
        with binding.connection() as conn:
            yield conn
    else:
        yield binding


def legacy_sqlite_control(connection: Connection) -> bool:
    """
    Whether this is a sqlite3 connection in legacy transaction control.

    Such a connection opens a transaction of its own before a data
    statement, and PRAGMA foreign_keys is ignored inside one. Python 3.12
    and later report legacy control as `autocommit` == -1. Older versions
    have no `autocommit` attribute, and legacy control is all they have.
    """
    try:
        import sqlite3
    except ImportError:  # pragma: no cover - sqlite3 ships with Python
        sqlite3_connection: Optional[type] = None
    else:
        sqlite3_connection = sqlite3.Connection
    if sqlite3_connection is not None and isinstance(connection, sqlite3_connection):
        # A subclass passed as connect(factory=...) reports its own
        # module, so the class itself is the test rather than the name.
        return getattr(connection, "autocommit", -1) == -1
    if type(connection).__module__.partition(".")[0] not in ("sqlite3", "pysqlite3"):
        return False
    return getattr(connection, "autocommit", -1) == -1


def enter_autocommit(connection: Connection) -> Callable[[], None]:
    """
    Turns the driver's own transaction control off, and returns the call
    that turns it back on.

    A DB-API driver such as psycopg2 opens a transaction before the first
    statement of its own accord, so a bare run is still a run inside a
    transaction block. Setting `autocommit` on the connection is what
    stops that.

    The sqlite3 driver in legacy transaction control does the same before
    a data statement, and it reports `autocommit` as -1 or has no such
    attribute at all. Its switch is `isolation_level`, and None turns the
    implicit transaction off. PRAGMA foreign_keys is ignored inside that
    transaction, so a table rebuild that runs one after its INSERT would
    leave foreign keys off. The returned call puts the level back where it
    found it.

    A driver with neither switch, or one already in autocommit, runs as it
    is, and the returned call commits.

    A driver that refuses to switch back leaves the connection in
    autocommit for the rest of its life. The returned call drops that
    error, because it runs after a block that may already be raising, and
    the block's own error is the one worth reporting. Close the connection
    and open a new one to get transaction control back.
    """
    switchable = getattr(connection, "autocommit", None) is False
    if switchable:
        # psycopg2 refuses the switch while a transaction is open.
        _commit_if_supported(connection)
        setattr(connection, "autocommit", True)
        return lambda: _set_quietly(connection, "autocommit", False)
    if legacy_sqlite_control(connection):
        isolation = getattr(connection, "isolation_level", None)
        _commit_if_supported(connection)
        setattr(connection, "isolation_level", None)
        return lambda: _set_quietly(connection, "isolation_level", isolation)
    return lambda: _commit_if_supported(connection)


def _commit_if_supported(connection: Connection) -> None:
    if hasattr(connection, "commit"):
        connection.commit()


def commit_unless_in_transaction(connection: Connection) -> None:
    """
    Commits the statements just run on the connection. Inside a
    transaction() context the context manager owns the commit, so a
    commit here would end that transaction early, and this does nothing.
    """
    if not in_transaction(connection):
        _commit_if_supported(connection)


def _set_quietly(connection: Connection, name: str, value: object) -> None:
    """Sets a driver switch, dropping a refusal (see enter_autocommit())."""
    try:
        setattr(connection, name, value)
    except Exception:
        pass


def rollback_quietly(connection: Connection) -> None:
    """
    Rolls back after a statement that failed, dropping a rollback error so
    the statement's own error is the one the caller sees.
    """
    try:
        if hasattr(connection, "rollback"):
            connection.rollback()
    except Exception:
        pass


def needs_explicit_begin(connection: Connection) -> bool:
    """
    Reports whether a transaction() block must open the transaction with
    its own BEGIN, although the driver controls transactions.

    This is true for a sqlite3 connection in legacy transaction control.
    That driver starts its implicit transaction before data statements
    only; a schema statement runs outside it and commits at once, so a
    rollback would keep the change. An explicit BEGIN puts every statement
    of the block under the commit or rollback that closes it. Connections
    in the new autocommit=False control open their transaction before
    every statement already and are left alone.
    """
    return legacy_sqlite_control(connection) and not getattr(
        connection, "in_transaction", False
    )


def _execute_or_close(cursor: Cursor, sql: str) -> None:
    """
    Runs the statement that opens a transaction. When it fails, no block
    owns the cursor yet, so the cursor is closed here before the error
    propagates.
    """
    try:
        cursor.execute(sql)
    except BaseException:
        cursor.close()
        raise


def in_transaction(connection: Connection) -> bool:
    """
    Reports whether the connection has an open transaction() context, on
    this thread or another one. A block another thread opened still holds
    the connection's transaction, so a statement here must not commit.
    """
    return _transaction_entry(connection) is not None


@contextmanager
def cursor_scope(connection: Connection) -> Iterator[Cursor]:
    """
    The cursor statements should run on, for one piece of work, given
    back at the end of the block. That is the transaction's own cursor
    when a transaction() block is open on the connection, and a new one
    when not. On DuckDB every cursor is its own session, so a statement
    on a fresh cursor would run outside the open transaction.

    A cursor holds a result set until something reads or closes it. Pyodbc
    and the MySQL drivers report "commands out of sync" when a connection
    accumulates cursors with unread rows, so a statement that opens its
    own cursor closes it again. The transaction's pinned cursor is never
    closed here: it belongs to the transaction() block, and closing it
    would leave the rest of that block without a session on DuckDB.
    """
    entry = _own_transaction_entry(connection)
    if entry is not None:
        yield entry[2]
        return
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def open_cursor(connection: Connection) -> Cursor:
    """
    Deprecated: use cursor_scope(), which also closes a cursor it opens.
    This function will be removed in 3.0.

    Returns the cursor cursor_scope() would yield: the transaction's own
    cursor when a transaction() block is open on the connection, and a
    new one when not. The caller closes a new cursor.
    """
    warnings.warn(
        "sustained.execution.open_cursor() is deprecated and will be removed "
        "in 3.0. Use sustained.execution.cursor_scope() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    entry = _own_transaction_entry(connection)
    if entry is not None:
        return entry[2]
    return connection.cursor()


@contextmanager
def pinned_transaction(connection: Connection) -> Iterator[Cursor]:
    """
    Pins a cursor for a transaction the caller opens and ends itself.

    transaction() decides the end of its block: commit when it finishes,
    rollback when it raises. A rehearsal decides for itself, because it
    keeps reading the schema after its down sweep and rolls back only when
    every proof is collected. It gets the rest of the machinery: the block
    is registered, so cursor_scope() hands out this cursor to every
    statement inside it, and in_transaction() reports the connection busy.
    On DuckDB, where each cursor is its own session, that is what keeps the
    rehearsed statements in the transaction that rolls back.

    The caller sends the BEGIN itself, through cursor_scope(), so it runs
    on the pinned cursor too. The cursor is yielded so the caller can end
    the block on it.

    Raises:
        ValueError: If a transaction is already open on the connection.
    """
    key = id(connection)
    with _TRANSACTION_LOCK:
        if key in _ACTIVE_TRANSACTIONS:
            raise ValueError("a transaction is already open on this connection")
        cursor = connection.cursor()
        _ACTIVE_TRANSACTIONS[key] = (connection, 0, cursor, threading.get_ident())
    try:
        yield cursor
    finally:
        with _TRANSACTION_LOCK:
            del _ACTIVE_TRANSACTIONS[key]
        cursor.close()


class Savepoint(NamedTuple):
    """
    The statements of one nested transaction block: `set` opens the
    savepoint, `release` drops it after the block, and `undo` rolls the
    block back to it and then drops it.
    """

    set: str
    release: Optional[str]
    undo: List[str]


def savepoint_for(
    dialect: "Dialects", compiler: "Compiler", depth: int, block: str
) -> Savepoint:
    """
    The savepoint statements for a block nested `depth` levels deep.
    `block` names the context manager in the error.

    A savepoint rolled back is still set, so `undo` drops it too, or
    every later block of the same name would stack on the connection.

    Raises:
        DialectError: If the dialect has no savepoints.
    """
    from sustained.exceptions import DialectError

    name = f"sustained_sp_{depth}"
    set_sql = compiler.savepoint_sql(name)
    if set_sql is None:
        raise DialectError(
            f"{dialect.name} has no savepoints, so a nested {block} block "
            "cannot roll back on its own. Run the statements inside the "
            "outer block instead."
        )
    release = compiler.release_savepoint_sql(name)
    rollback = compiler.rollback_savepoint_sql(name)
    undo = [sql for sql in (rollback, release) if sql is not None]
    return Savepoint(set_sql, release, undo)


def driver_controls(connection: Connection) -> bool:
    """
    Whether the DB-API driver ends the connection's transactions through
    commit() and rollback(). A connection the caller put in autocommit,
    such as sqlite3.connect(autocommit=True) or psycopg with autocommit
    on, sends no BEGIN of its own and reads rollback() as a no-op.
    """
    return getattr(connection, "autocommit", False) is not True


class TxnSql(NamedTuple):
    """
    The statements that open, commit, and roll back a block in SQL. A
    None field means the dialect has no such statement.
    """

    begin: Optional[str]
    commit: Optional[str]
    rollback: Optional[str]


def transaction_sql(compiler: "Compiler", driver_control: bool) -> Optional[TxnSql]:
    """
    The SQL that opens and ends a top-level block, or None when the
    driver's commit() and rollback() end it.
    """
    if driver_control:
        return None
    return TxnSql(
        compiler.begin_transaction_sql(),
        compiler.commit_transaction_sql(),
        compiler.rollback_transaction_sql(),
    )


def _undo_savepoint(cursor: Cursor, savepoint: Savepoint, error: BaseException) -> None:
    """
    Rolls a nested block back to its savepoint and drops the savepoint.

    The block failed for a reason the caller cares about, so a failure
    here does not replace it: the original error keeps propagating with
    the rollback failure as its cause.
    """
    for statement in savepoint.undo:
        try:
            cursor.execute(statement)
        except Exception as rollback_error:
            raise error from rollback_error


@contextmanager
def transaction(
    connection: Binding, dialect: "Dialects | None" = None
) -> Iterator[Connection]:
    """
    Runs the block atomically on the connection. Commits when the block
    finishes and rolls back when it raises. While the context is open,
    run() stops committing per statement.

    Nested contexts on the same connection use savepoints, spelled the way
    the dialect spells them; the default is the ANSI SAVEPOINT statement.
    Nesting raises DialectError on a dialect with no savepoints, such as
    DuckDB. Model.transaction() passes the model's dialect for you.

    When given a ConnectionPool, one connection is checked out, pinned to
    the calling thread for the duration of the block, and released after.

    A block belongs to the thread that opened it. A second thread that
    calls transaction() on the same connection gets a RuntimeError, because
    a connection carries one transaction and the nested spelling would put
    that thread's statements in the first thread's transaction, on the
    first thread's cursor. Give each thread its own connection, from a
    ConnectionPool or from a second Model.bind() target.
    """
    from sustained.dialects import Dialects
    from sustained.pool import ConnectionPool

    if dialect is None:
        dialect = Dialects.DEFAULT

    if isinstance(connection, ConnectionPool):
        pinned_entry = _pinned_entry()
        if pinned_entry is not None and pinned_entry[0] is connection:
            # This pool already has a transaction open on this thread; nest
            # on its connection with a savepoint instead of checking out
            # another. A different pool's pinned connection is left alone.
            pinned = pinned_entry[1]
            with transaction(pinned, dialect):
                yield pinned
            return
        conn = connection.acquire_raw()
        _pin(connection, conn)
        try:
            with transaction(conn, dialect):
                yield conn
        finally:
            _unpin()
            connection.release(conn)
        return

    key = id(connection)
    entry = _transaction_entry(connection)
    if entry is not None and entry[3] != threading.get_ident():
        raise RuntimeError(
            "Another thread has an open transaction() block on this "
            "connection. A connection carries one transaction, so a second "
            "thread cannot start or nest one on it. Give each thread its "
            "own connection, such as with a ConnectionPool."
        )

    if entry is not None:
        compiler = Dialects.get_compiler(dialect)
        depth = entry[1] + 1
        savepoint = savepoint_for(dialect, compiler, depth, "transaction()")
        cursor = entry[2]
        owner = entry[3]
        with _TRANSACTION_LOCK:
            _ACTIVE_TRANSACTIONS[key] = (connection, depth, cursor, owner)
        try:
            cursor.execute(savepoint.set)
            try:
                yield connection
            except BaseException as error:
                _undo_savepoint(cursor, savepoint, error)
                raise
            if savepoint.release is not None:
                cursor.execute(savepoint.release)
        finally:
            with _TRANSACTION_LOCK:
                _ACTIVE_TRANSACTIONS[key] = (connection, depth - 1, cursor, owner)
        return

    compiler = Dialects.get_compiler(dialect)
    cursor = connection.cursor()
    # On a connection in autocommit, driver calls would roll nothing back.
    # When the driver runs autocommit, the transaction is opened, kept,
    # and closed in SQL on this one cursor.
    sql = transaction_sql(
        compiler,
        compiler.driver_transaction_control() and driver_controls(connection),
    )
    if sql is None:
        if needs_explicit_begin(connection):
            _execute_or_close(cursor, "BEGIN")
    elif sql.begin is not None:
        _execute_or_close(cursor, sql.begin)
    with _TRANSACTION_LOCK:
        _ACTIVE_TRANSACTIONS[key] = (connection, 0, cursor, threading.get_ident())
    try:
        yield connection
        if sql is None:
            connection.commit()
        elif sql.commit is not None:
            cursor.execute(sql.commit)
    except BaseException as error:
        # A failed rollback, such as on a lost connection, does not replace
        # the block's error: it keeps propagating with the rollback failure
        # as its cause.
        try:
            if sql is None:
                connection.rollback()
            elif sql.rollback is not None:
                cursor.execute(sql.rollback)
        except Exception as rollback_error:
            raise error from rollback_error
        raise
    finally:
        with _TRANSACTION_LOCK:
            del _ACTIVE_TRANSACTIONS[key]
        cursor.close()


def total_row_count(counts: Sequence[int]) -> int:
    """
    Adds up the row counts of an insert sent one row per statement. A
    driver that reports -1 for a statement does not know its count, so
    the total is -1 too rather than a negative sum.
    """
    if any(count < 0 for count in counts):
        return -1
    return sum(counts)


class InsertBatch(NamedTuple):
    """
    A multi-row insert sent as one single-row template. `rows` lists
    each row's statement and values after prepare_execution(), and
    `batchable` is True when every row kept the template, so the rows
    can go to the driver's executemany() together.
    """

    sql: str
    rows: List[Tuple[str, Tuple[SqlValue, ...]]]
    batchable: bool

    def values(self) -> List[Tuple[SqlValue, ...]]:
        """Each row's parameters, in order."""
        return [values for _, values in self.rows]

    def flat_params(self) -> Tuple[SqlValue, ...]:
        """
        Every row's values, flattened in the order they were sent, so the
        statement listener's audit of a batch insert keeps the same
        information as an audit of single-row inserts.
        """
        return tuple(v for _, values in self.rows for v in values)


def insert_batch(query: "AnyQuery") -> Optional[InsertBatch]:
    """
    The batch plan of a multi-row insert, or None when the query runs as
    one statement.

    An Expression value renders as SQL text with no placeholder, so its
    row would bind one value too many, and RETURNING needs the rows the
    statement returns. Those inserts take the one-statement path, which
    renders every row. A row whose preparation rewrote the statement,
    such as a None parameter on Athena, cannot share the batch, so the
    plan is then not batchable and each row runs on its own.
    """
    if (
        query._stmt_type != "insert"
        or len(query._insert_rows) < 2
        or query._returning_columns
        or query._has_expression_values()
    ):
        return None
    sql = query._first_row_sql()
    columns = list(query._insert_rows[0].keys())
    rows = [
        query._compiler.prepare_execution(sql, tuple(row[c] for c in columns))
        for row in query._insert_rows
    ]
    return InsertBatch(sql, rows, all(row_sql == sql for row_sql, _ in rows))


def run_query(
    query: "AnyQuery", conn: Connection, cursor: Cursor
) -> Union[List["Model"], WriteResult]:
    """QueryBuilder.run() itself, on a connection and cursor already open."""
    per_row_count: Optional[int] = None
    batch = insert_batch(query)
    if batch is not None:
        with timed_statement(batch.sql, batch.flat_params()):
            if batch.batchable:
                cursor.executemany(batch.sql, batch.values())
            else:
                # The cursor reports the count of its last execute only, so
                # the per-row counts are added up here, as arun() does.
                counts = []
                for row_sql, row_values in batch.rows:
                    cursor.execute(row_sql, row_values)
                    counts.append(int(cursor.rowcount))
                per_row_count = total_row_count(counts)
    else:
        execute_timed(cursor, *query._compiler.prepare_execution(*query.to_sql()))

    if query._stmt_type == "select":
        models = fetch_models(query._model_class, cursor)
        eager_load_paths(
            query._model_class, conn, models, query._eager_relations, query._dialect
        )
        return models

    if query._returning_columns and cursor.description is not None:
        columns = cursor_columns(cursor)
        result: WriteResult = [dict(zip(columns, row)) for row in cursor.fetchall()]
    else:
        result = cursor.rowcount if per_row_count is None else per_row_count
    commit_unless_in_transaction(conn)
    return result


def run(
    query: "AnyQuery", connection: Optional[Binding]
) -> Union[List["Model"], WriteResult]:
    """QueryBuilder.run(): resolves the connection, then runs the query."""
    with (
        connection_scope(connection, query._model_class._connection) as conn,
        cursor_scope(conn) as cursor,
    ):
        try:
            return run_query(query, conn, cursor)
        except BaseException:
            # A write that raises outside a transaction() block would
            # leave its partial work pending, such as the rows an
            # executemany sent before the failing one, and the next
            # write's commit would keep them. On Postgres the failure
            # also leaves the session aborted.
            if query._stmt_type != "select" and not in_transaction(conn):
                rollback_quietly(conn)
            raise


def first_model(query: "AnyQuery", connection: Optional[Binding]) -> Optional["Model"]:
    """QueryBuilder.first(): runs the query capped at one row."""
    results = cast(List["Model"], run(query._first_query(), connection))
    return results[0] if results else None


def select_rows(
    query: "AnyQuery", connection: Optional[Binding]
) -> Tuple[List[str], Sequence[Sequence[RowValue]]]:
    """
    Executes a SELECT and returns (column names, raw rows).

    Raises:
        AmbiguousColumns: If the result set repeats a column name.
    """
    if query._stmt_type != "select":
        raise ValueError("Only SELECT queries return result sets.")
    with (
        connection_scope(connection, query._model_class._connection) as conn,
        cursor_scope(conn) as cursor,
    ):
        execute_timed(cursor, *query._compiler.prepare_execution(*query.to_sql()))
        return cursor_columns(cursor), cursor.fetchall()


def fetch_dicts(
    query: "AnyQuery", connection: Optional[Binding]
) -> List[Dict[str, RowValue]]:
    """QueryBuilder.to_dicts(): the SELECT's rows as dicts keyed by column."""
    columns, rows = select_rows(query, connection)
    return [dict(zip(columns, row)) for row in rows]


def count_rows(query: "AnyQuery", connection: Optional[Binding]) -> int:
    """QueryBuilder.total(): runs the query's COUNT(*) wrapper."""
    return int(fetch_dicts(query._count_query(), connection)[0]["total"])


# pandas and pyarrow are optional installs. Naming their types here would
# make Sustained fail to type check for anyone who skips them, so the
# modules load through importlib and the DataFrame and Table types are
# left open.
def fetch_dataframe(query: "AnyQuery", connection: Optional[Binding]) -> Any:
    """QueryBuilder.to_df(): the SELECT's rows as a pandas DataFrame."""
    try:
        pandas = importlib.import_module("pandas")
    except ImportError:
        raise RuntimeError(
            "to_df() requires pandas. Install it with: pip install pandas"
        ) from None
    columns, rows = select_rows(query, connection)
    return pandas.DataFrame.from_records(list(rows), columns=columns)


def fetch_arrow(query: "AnyQuery", connection: Optional[Binding]) -> Any:
    """QueryBuilder.to_arrow(): the SELECT's rows as a pyarrow Table."""
    try:
        pyarrow = importlib.import_module("pyarrow")
    except ImportError:
        raise RuntimeError(
            "to_arrow() requires pyarrow. Install it with: pip install pyarrow"
        ) from None
    columns, rows = select_rows(query, connection)
    data = {name: [row[i] for row in rows] for i, name in enumerate(columns)}
    return pyarrow.table(data)


def explain_plan(
    query: "AnyQuery", connection: Optional[Binding], analyze: bool
) -> List[Tuple[RowValue, ...]]:
    """QueryBuilder.explain(): runs the dialect's EXPLAIN on the query."""
    sql, params = query._compiler.prepare_execution(*query.to_sql())
    prefix = query._compiler.compile_explain(analyze)
    with (
        connection_scope(connection, query._model_class._connection) as conn,
        cursor_scope(conn) as cursor,
    ):
        execute_timed(cursor, f"{prefix} {sql}", params)
        return [tuple(row) for row in cursor.fetchall()]


def checked_columns(columns: Sequence[str]) -> List[str]:
    """
    Returns the result set's column names and refuses a repeated one.

    A row is keyed by column name, so two columns of the same name keep
    one value and drop the other. Callers run this once per result set,
    before the first row is hydrated.

    Raises:
        AmbiguousColumns: If any name appears more than once.
    """
    names = list(columns)
    if len(set(names)) != len(names):
        seen: Dict[str, int] = {}
        for name in names:
            seen[name] = seen.get(name, 0) + 1
        raise AmbiguousColumns([n for n, count in seen.items() if count > 1])
    return names


def fetch_models(model_class: Type["Model"], cursor: Cursor) -> List["Model"]:
    """
    Hydrates every row on the cursor into instances of model_class.

    Raises:
        AmbiguousColumns: If the result set repeats a column name.
    """
    if cursor.description is None:
        return []
    columns = cursor_columns(cursor)
    return [model_class(**dict(zip(columns, row))) for row in cursor.fetchall()]


def _split_column_ref(ref: str, relation_name: str) -> Tuple[str, str]:
    """Splits 'table.column' and rejects unqualified references."""
    if "." not in ref:
        raise ValueError(
            f"Relation '{relation_name}' join references must be qualified "
            f"as 'table.column', got {ref!r}."
        )
    table, column = ref.rsplit(".", 1)
    return table, column


def check_relation_path(model_class: Type["Model"], path: str) -> None:
    """
    Walks a dotted relation path and rejects the first unknown segment.

    Raises:
        ValueError: Naming the segment, the model that lacks it, and the
            full path when the path has more than one segment.
    """
    from sustained.model import resolve_relation

    current = model_class
    for segment in path.split("."):
        try:
            current = resolve_relation(current, segment)[1]
        except ValueError as exc:
            if "." in path:
                raise ValueError(f"{exc} (in relation path '{path}')") from None
            raise


def relation_tree(paths: List[str]) -> RelationTree:
    """
    Folds dotted relation paths into a nested dict, so paths sharing a
    prefix load that prefix once.
    """
    tree: RelationTree = {}
    for path in paths:
        node = tree
        for segment in path.split("."):
            node = node.setdefault(segment, {})
    return tree


def _attached_children(parents: List["Model"], relation_name: str) -> List["Model"]:
    """Flattens what an eager load attached, ready to be the next parents."""
    children: List["Model"] = []
    for parent in parents:
        loaded = getattr(parent, relation_name, None)
        if isinstance(loaded, list):
            children.extend(loaded)
        elif loaded is not None:
            children.append(loaded)
    return children


EagerSteps = Generator["AnyQuery", List["Model"], None]


def eager_load_steps(
    model_class: Type["Model"],
    parents: List["Model"],
    tree: RelationTree,
    dialect: Optional[Dialects] = None,
) -> EagerSteps:
    """
    Loads one level of the relation tree, then each child level, as a
    generator. It yields each batch query and receives the rows the query
    returned, so the sync and async loaders differ only in how they run
    a query. Every child query renders in the dialect of the parent query,
    which runs on the same connection.
    """
    from sustained.model import resolve_relation

    for relation_name, children in tree.items():
        if parents:
            plan = plan_eager_load(model_class, parents, relation_name, dialect)
            fetched: List["Model"] = []
            for query in plan.queries:
                fetched.extend((yield query))
            attach_eager_load(plan, parents, fetched)
        if not children:
            continue
        next_parents = _attached_children(parents, relation_name)
        if next_parents:
            yield from eager_load_steps(
                resolve_relation(model_class, relation_name)[1],
                next_parents,
                children,
                dialect,
            )


def _run_eager_steps(steps: EagerSteps, connection: Connection) -> None:
    """Runs each query eager_load_steps() yields on the connection."""
    try:
        query = next(steps)
        while True:
            query = steps.send(cast(List["Model"], query.run(connection)))
    except StopIteration:
        pass


def eager_load_paths(
    model_class: Type["Model"],
    connection: Connection,
    parents: List["Model"],
    paths: List[str],
    dialect: Optional[Dialects] = None,
) -> None:
    """
    Loads every dotted relation path for a list of parent instances. Each
    relation costs one query per level, batched over all the parents at
    that level.
    """
    _run_eager_steps(
        eager_load_steps(model_class, parents, relation_tree(paths), dialect),
        connection,
    )


# The most join keys one eager-load query binds. MSSQL accepts 2100
# parameters per statement and SQLite builds before 3.32 accept 999, so
# a single IN list over every parent key fails there for a large parent
# list. 900 keys leave room for any other parameter of the query.
EAGER_KEY_BATCH = 900


def _key_batches(keys: List[RowValue]) -> List[List[RowValue]]:
    """Splits the join keys into lists of at most EAGER_KEY_BATCH keys."""
    return [
        keys[start : start + EAGER_KEY_BATCH]
        for start in range(0, len(keys), EAGER_KEY_BATCH)
    ]


class EagerPlan:
    """
    One eager load, split into the query to run and how to attach its rows.
    The split lets the sync and async paths share the SQL and the grouping,
    since only the way they run the query differs.

    An empty query list means there is nothing to fetch, and attaching
    sets the empty value on every parent. The keys are split over more
    than one query when there are more of them than EAGER_KEY_BATCH.
    """

    def __init__(
        self,
        relation_name: str,
        parent_keys: List[RowValue],
        is_many: bool,
        queries: Optional[List["AnyQuery"]] = None,
        child_col: Optional[str] = None,
        through: bool = False,
    ) -> None:
        self.relation_name = relation_name
        self.parent_keys = parent_keys
        self.is_many = is_many
        self.queries: List["AnyQuery"] = queries or []
        self.child_col = child_col
        self.through = through


def plan_eager_load(
    model_class: Type["Model"],
    parents: List["Model"],
    relation_name: str,
    dialect: Optional[Dialects] = None,
) -> EagerPlan:
    """
    Builds the query that loads a relation for a list of parents, batched
    over their join keys. The query renders in the given dialect, or in the
    related model's own dialect when none is given.

    Raises:
        ValueError: If the model has no relation with that name, or the
            parent rows lack the join key column.
    """
    from sustained.model import names_model_table, resolve_relation

    relation, related_cls = resolve_relation(model_class, relation_name)
    join_info = relation["join"]

    from_table, from_col = _split_column_ref(join_info["from"], relation_name)
    to_table, to_col = _split_column_ref(join_info["to"], relation_name)

    if "through" in join_info:
        # The key test above is what tells the two join mappings apart; a
        # type checker cannot read it, so the narrowing is spelled out.
        through_join = cast(JoinMappingWithThrough, join_info)
        return _plan_eager_load_through(
            related_cls,
            parents,
            relation_name,
            through_join,
            from_col,
            to_col,
            dialect,
        )

    # The side whose table matches the parent model holds the parent key.
    if names_model_table(from_table, model_class):
        parent_col, child_col = from_col, to_col
    else:
        parent_col, child_col = to_col, from_col

    parent_keys = _collect_parent_keys(parents, parent_col, relation_name)
    unique_keys = [k for k in dict.fromkeys(parent_keys) if k is not None]
    is_many = relation["relation"] == RelationType.HasManyRelation
    if not unique_keys:
        return EagerPlan(relation_name, parent_keys, is_many)
    return EagerPlan(
        relation_name,
        parent_keys,
        is_many,
        queries=[
            _child_query(related_cls, dialect).whereIn(child_col, batch)
            for batch in _key_batches(unique_keys)
        ],
        child_col=child_col,
    )


def attach_eager_load(
    plan: EagerPlan, parents: List["Model"], children: List["Model"]
) -> None:
    """
    Groups fetched rows by their join key and attaches them to the parents
    under the relation name. HasManyRelation and ManyToManyRelation attach
    a list; the to-one types attach a single instance or None.

    Raises:
        ValueError: If the fetched rows lack the join key column.
    """
    if not plan.queries:
        for parent in parents:
            setattr(parent, plan.relation_name, [] if plan.is_many else None)
        return

    grouped: Dict[RowValue, List["Model"]] = {}
    for child in children:
        if plan.through:
            key = child.__dict__.pop(_PARENT_KEY_ALIAS, None)
        else:
            assert plan.child_col is not None
            if plan.child_col not in child.__dict__:
                raise ValueError(
                    f"Cannot eager load '{plan.relation_name}': related rows "
                    f"do not include the '{plan.child_col}' column."
                )
            key = child.__dict__[plan.child_col]
        grouped.setdefault(key, []).append(child)

    for parent, key in zip(parents, plan.parent_keys):
        # A new list per parent: two parents with the same join key must not
        # share one list, or an append on one parent shows up on the other.
        matches = list(grouped.get(key, ()))
        if plan.is_many:
            setattr(parent, plan.relation_name, matches)
        else:
            setattr(parent, plan.relation_name, matches[0] if matches else None)


def eager_load_relation(
    model_class: Type["Model"],
    connection: Connection,
    parents: List["Model"],
    relation_name: str,
) -> None:
    """
    Loads a relation for a list of parent instances with one extra query and
    attaches the results to each parent under the relation name.

    HasManyRelation and ManyToManyRelation attach a list; the to-one relation
    types attach a single instance or None.
    """
    _run_eager_steps(
        eager_load_steps(model_class, parents, {relation_name: {}}), connection
    )


def _child_query(related_cls: Type["Model"], dialect: Optional[Dialects]) -> "AnyQuery":
    """A query on the related model in the given dialect, or its own."""
    from sustained.builder import QueryBuilder

    return QueryBuilder(related_cls, dialect=dialect or related_cls._dialect)


def _collect_parent_keys(
    parents: List["Model"], parent_col: str, relation_name: str
) -> List[RowValue]:
    """
    Reads the join key from each parent's hydrated data. Values come from
    __dict__ so a missing column is an error instead of silently resolving
    to a qualified column string.
    """
    parent_keys = []
    for parent in parents:
        if parent_col not in parent.__dict__:
            raise ValueError(
                f"Cannot eager load '{relation_name}': parent rows were not "
                f"fetched with the '{parent_col}' column."
            )
        parent_keys.append(parent.__dict__[parent_col])
    return parent_keys


# Reserved alias for the parent join key in through-relation queries.
_PARENT_KEY_ALIAS = "sustained_parent_key"


def _plan_eager_load_through(
    related_cls: Type["Model"],
    parents: List["Model"],
    relation_name: str,
    join_info: JoinMappingWithThrough,
    parent_col: str,
    related_col: str,
    dialect: Optional[Dialects],
) -> EagerPlan:
    """
    Plans a many-to-many load as one query that joins the related table to
    the through table and exposes the parent key under a reserved alias for
    grouping.
    """
    through = join_info["through"]

    def through_table_name(ref: Union[Type["Model"], str]) -> str:
        if isinstance(ref, str):
            return ref
        name = ref.tableName
        assert name is not None, "Through table model must have a tableName"
        return str(name)

    through_table = through_table_name(through["from"]["table"])
    through_from_key = through["from"]["key"]
    through_to_key = through["to"]["key"]
    related_table = related_cls.tableName
    assert related_table is not None

    parent_keys = _collect_parent_keys(parents, parent_col, relation_name)
    unique_keys = [k for k in dict.fromkeys(parent_keys) if k is not None]
    if not unique_keys:
        return EagerPlan(relation_name, parent_keys, is_many=True)

    queries = [
        _child_query(related_cls, dialect)
        .select(
            f"{related_table}.*",
            f"{through_table}.{through_from_key} AS {_PARENT_KEY_ALIAS}",
        )
        .join(
            through_table,
            f"{through_table}.{through_to_key}",
            "=",
            f"{related_table}.{related_col}",
        )
        .whereIn(f"{through_table}.{through_from_key}", batch)
        for batch in _key_batches(unique_keys)
    ]
    return EagerPlan(
        relation_name, parent_keys, is_many=True, queries=queries, through=True
    )
