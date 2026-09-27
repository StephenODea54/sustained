"""
Tests for the live preflight: the conflict tables and read plans of the
PostgreSQL, MySQL and MariaDB, and SQL Server profiles, the generic
matching, the report's preflight lines, preflight() and
async_preflight(), and the migrator's impact(live=True), preflight(),
and up(preflight=...).
"""

import asyncio
import io
import sqlite3
import unittest
from contextlib import redirect_stderr
from unittest import mock

from sustained.aio_migrations import AsyncMigrator
from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.exceptions import DialectError, PreflightBlocked
from sustained.impact import (
    Blocker,
    EngineContext,
    LiveSession,
    Preflight,
    analyze,
    async_preflight,
    attach_impact,
    preflight,
)
from sustained.impact.preflight import (
    Granted,
    Planned,
    blockers,
    covered,
    names,
    older,
    planned,
    preflight_plan,
)
from sustained.impact.report import (
    blocker_line,
    preflight_data,
    preflight_summary,
    render,
    render_preflight,
    report_data,
    transaction_line,
)
from sustained.impact.rules import postgres
from sustained.impact.rules.mssql import preflight as mssql_preflight
from sustained.impact.rules.mysql import preflight as mysql_preflight
from sustained.impact.rules.postgres import preflight as pg_preflight
from sustained.migrations import Migration, Migrator, PreflightCheck
from tests.test_impact_context import (
    SETTINGS_ROW,
    SIZE_ROWS,
    ScriptedAdapter,
    drive,
)

PG = Dialects.POSTGRES

INDEX = "CREATE INDEX ix_orders_customer ON orders (customer_id)"
CONCURRENT = "CREATE INDEX CONCURRENTLY ix_orders_note ON orders (note)"
ADD = "ALTER TABLE orders ADD COLUMN note text"
UPDATE = "UPDATE orders SET note = 'x'"

# pid, gid, schema, table, visible, mode, granted, user, app, state,
# transaction age, query
READER = (
    4121,
    None,
    "public",
    "orders",
    True,
    "AccessShareLock",
    True,
    "billing",
    "billing-worker",
    "idle in transaction",
    2520.0,
    "SELECT * FROM orders WHERE id = 1",
)
WRITER = (
    4122,
    None,
    "public",
    "orders",
    True,
    "RowExclusiveLock",
    True,
    "app",
    "",
    "active",
    3.0,
    "UPDATE orders SET total = 0",
)
PREPARED_LOCK = (
    None,
    "tx1",
    "public",
    "orders",
    True,
    "RowExclusiveLock",
    True,
    "app",
    None,
    None,
    90.0,
    None,
)
ELSEWHERE = (
    4123,
    None,
    "audit",
    "orders",
    False,
    "AccessExclusiveLock",
    True,
    "ops",
    None,
    "active",
    1.0,
    "LOCK TABLE audit.orders",
)
# pid, user, app, state, transaction age, query, has a snapshot
REPORTER = (5003, "report", "metabase", "active", 720.0, "SELECT count(*)", True)
IDLE = (5004, "app", None, "idle in transaction", 30.0, "BEGIN", False)
# gid, owner, age
PREPARED = ("tx1", "app", 90.0)


def impacts(statements, dialect=PG, context=None):
    return analyze(statements, dialect, context).statements


def pg_answer(locks, transactions=(), prepared=()):
    """The rows a Postgres server gives each preflight read, in order."""
    return [list(locks), list(transactions), list(prepared)]


class GenericMatchingTestCase(unittest.TestCase):
    def test_planned_lists_each_table_lock_in_run_order(self):
        found = planned(impacts([ADD, "SET lock_timeout = '1s'", INDEX]))
        self.assertEqual(
            [(p.statement, p.table, p.lock) for p in found],
            [(ADD, "orders", "ACCESS EXCLUSIVE"), (INDEX, "orders", "SHARE")],
        )

    def test_names_include_the_bare_name_only_when_it_finds_the_table(self):
        self.assertEqual(names("Public", "Orders", True), {"public.orders", "orders"})
        self.assertEqual(names("audit", "orders", False), {"audit.orders"})

    def test_a_session_is_listed_once_beside_the_first_statement(self):
        session = LiveSession(1, "pid 1")
        granted = [Granted("public", "orders", True, "ROW EXCLUSIVE", True, session)]
        locks = [
            Planned(INDEX, "orders", "SHARE", None),
            Planned(ADD, "public.orders", "ACCESS EXCLUSIVE", None),
        ]
        (found,) = blockers(locks, granted, pg_preflight.conflicts)
        self.assertEqual(found.statement, INDEX)
        self.assertEqual(found.held, "ROW EXCLUSIVE")

    def test_older_leaves_out_blockers_and_young_transactions(self):
        old, young, blocked = (
            LiveSession(1, "pid 1", transaction_seconds=61.0),
            LiveSession(2, "pid 2", transaction_seconds=10.0),
            LiveSession(3, "pid 3", transaction_seconds=900.0),
        )
        oldest = LiveSession(4, "pid 4", transaction_seconds=600.0)
        found = [
            Blocker(ADD, "orders", "ACCESS EXCLUSIVE", "ACCESS SHARE", True, blocked)
        ]
        self.assertEqual(
            older([old, young, blocked, oldest, LiveSession(5, "pid 5")], 60.0, found),
            (oldest, old),
        )

    def test_covered_names_the_dialects_with_a_preflight(self):
        self.assertTrue(covered(PG))
        self.assertTrue(covered(Dialects.MYSQL))
        self.assertTrue(covered(Dialects.MSSQL))
        self.assertFalse(covered(Dialects.DEFAULT))
        self.assertFalse(covered(Dialects.DUCKDB))
        self.assertFalse(covered(Dialects.PRESTO))

    def test_preflight_plan_refuses_a_dialect_without_one(self):
        with self.assertRaises(ValueError):
            preflight_plan(Dialects.DUCKDB, [])


class PostgresPreflightTestCase(unittest.TestCase):
    def test_mode_name(self):
        self.assertEqual(pg_preflight.mode_name("AccessShareLock"), "ACCESS SHARE")
        self.assertEqual(
            pg_preflight.mode_name("ShareUpdateExclusiveLock"), "SHARE UPDATE EXCLUSIVE"
        )
        self.assertEqual(pg_preflight.mode_name("ExclusiveLock"), "EXCLUSIVE")

    def test_conflicts_follow_the_documented_table(self):
        def conflicts(lock, mode, rule=None):
            return pg_preflight.conflicts(Planned("", "t", lock, rule), mode)

        self.assertTrue(conflicts("ACCESS EXCLUSIVE", "ACCESS SHARE"))
        self.assertFalse(conflicts("SHARE", "ACCESS SHARE"))
        self.assertTrue(conflicts("SHARE", "ROW EXCLUSIVE"))
        self.assertFalse(conflicts("SHARE", "SHARE"))
        self.assertFalse(conflicts("ROW EXCLUSIVE", "ROW EXCLUSIVE"))
        self.assertTrue(conflicts("ROW EXCLUSIVE", "SHARE"))
        self.assertFalse(conflicts("SHARE UPDATE EXCLUSIVE", "ROW EXCLUSIVE"))
        self.assertTrue(conflicts("SHARE UPDATE EXCLUSIVE", "SHARE UPDATE EXCLUSIVE"))
        self.assertTrue(conflicts("EXCLUSIVE", "ROW SHARE"))
        self.assertFalse(conflicts("EXCLUSIVE", "ACCESS SHARE"))

    def test_a_concurrent_build_waits_for_writers(self):
        rule = "pg.create_index.concurrently"
        plan = Planned("", "t", "SHARE UPDATE EXCLUSIVE", rule)
        self.assertTrue(pg_preflight.conflicts(plan, "ROW EXCLUSIVE"))
        self.assertFalse(pg_preflight.conflicts(plan, "ACCESS SHARE"))
        drop = Planned("", "t", "SHARE UPDATE EXCLUSIVE", "pg.drop_index.concurrently")
        self.assertTrue(pg_preflight.conflicts(drop, "ACCESS SHARE"))

    def test_an_unknown_lock_conflicts_with_every_mode(self):
        plan = Planned("", "t", "SOMETHING", None)
        self.assertTrue(pg_preflight.conflicts(plan, "ACCESS SHARE"))

    def test_reads_the_blockers_and_old_transactions(self):
        asked, found = drive(
            pg_preflight.preflight_plan(impacts([ADD]), 60.0),
            pg_answer([READER, ELSEWHERE], [REPORTER, IDLE], []),
        )
        self.assertEqual(len(asked), 3)
        self.assertTrue(all("%" not in sql for sql in asked))
        self.assertEqual(found.profile, "postgres")
        self.assertEqual(found.read, {"locks", "transactions"})
        (blocker,) = found.blockers
        self.assertEqual(blocker.statement, ADD)
        self.assertEqual(blocker.held, "ACCESS SHARE")
        self.assertTrue(blocker.granted)
        self.assertEqual(
            blocker.session,
            LiveSession(
                4121,
                "pid 4121",
                "billing",
                "billing-worker",
                "idle in transaction",
                2520.0,
                "SELECT * FROM orders WHERE id = 1",
            ),
        )
        self.assertEqual([s.label for s in found.transactions], ["pid 5003"])

    def test_a_weaker_lock_passes_a_reader(self):
        _, found = drive(
            pg_preflight.preflight_plan(impacts([INDEX]), 60.0),
            pg_answer([READER, WRITER]),
        )
        self.assertEqual([b.session.label for b in found.blockers], ["pid 4122"])
        self.assertIsNone(found.blockers[0].session.application)

    def test_a_prepared_transaction_is_named_by_its_gid(self):
        _, found = drive(
            pg_preflight.preflight_plan(impacts([INDEX]), 60.0),
            pg_answer([PREPARED_LOCK], [], [PREPARED]),
        )
        (blocker,) = found.blockers
        self.assertEqual(blocker.session.label, "prepared transaction 'tx1'")
        self.assertIsNone(blocker.session.id)
        self.assertEqual(found.transactions, ())

    def test_a_concurrent_build_waits_for_every_snapshot(self):
        _, found = drive(
            pg_preflight.preflight_plan(impacts([CONCURRENT]), 60.0),
            pg_answer([WRITER], [REPORTER, IDLE], [PREPARED]),
        )
        self.assertEqual(
            [(b.session.label, b.held) for b in found.blockers],
            [
                ("pid 4122", "ROW EXCLUSIVE"),
                ("pid 5003", None),
                ("prepared transaction 'tx1'", None),
            ],
        )

    def test_a_failed_read_is_left_out(self):
        denied = RuntimeError("permission denied")
        _, found = drive(
            pg_preflight.preflight_plan(impacts([ADD]), 60.0),
            [denied, [REPORTER], denied],
        )
        self.assertEqual(found.read, {"transactions"})
        self.assertEqual(found.blockers, ())
        _, found = drive(
            pg_preflight.preflight_plan(impacts([ADD]), 60.0), [[READER], denied]
        )
        self.assertEqual(found.read, {"locks"})
        self.assertEqual(len(found.blockers), 1)


# connection, schema, table, lock type, granted, user, command, age, query
MDL_READ = (12, "shop", "orders", "SHARED_READ", 1, "app", "Sleep", 400, None)
MDL_NO_WRITE = (13, "shop", "orders", "SHARED_NO_WRITE", 1, "ops", "Query", None, "x")
MDL_PENDING = (14, "shop", "orders", "EXCLUSIVE", 0, "ops", "Query", 5, "ALTER")
MDL_OTHER_DB = (15, "other", "orders", "SHARED_READ", 1, "app", "Sleep", 400, None)
# connection, user, command, age, query
TRX_OLD = (16, "report", "Query", 900, "SELECT SLEEP(1000)")
TRX_READER = (12, "app", "Sleep", 400, None)


class MysqlPreflightTestCase(unittest.TestCase):
    def mysql(self, statements):
        return impacts(statements, Dialects.MYSQL)

    def test_conflicts(self):
        write = Planned("", "t", "IX", None)
        alter = Planned("", "t", "INPLACE, LOCK=NONE", None)
        self.assertFalse(mysql_preflight.conflicts(write, "SHARED_READ"))
        self.assertTrue(mysql_preflight.conflicts(write, "SHARED_NO_WRITE"))
        self.assertTrue(mysql_preflight.conflicts(write, "EXCLUSIVE"))
        self.assertTrue(mysql_preflight.conflicts(alter, "SHARED_READ"))

    def test_reads_the_metadata_locks_and_transactions(self):
        asked, found = drive(
            mysql_preflight.preflight_plan(
                self.mysql([ADD.replace("text", "int")]), 60.0
            ),
            [
                [("8.0.40", "shop")],
                [(12, "billing-worker")],
                [("1", "YES")],
                [MDL_READ, MDL_OTHER_DB],
                [TRX_OLD, TRX_READER],
            ],
        )
        self.assertEqual(len(asked), 5)
        self.assertEqual(found.profile, "mysql")
        self.assertEqual(found.read, {"locks", "transactions"})
        (blocker,) = found.blockers
        self.assertEqual(blocker.session.label, "connection 12")
        self.assertEqual(blocker.session.application, "billing-worker")
        self.assertEqual(blocker.held, "SHARED_READ")
        self.assertEqual([s.label for s in found.transactions], ["connection 16"])

    def test_a_write_waits_only_for_the_locks_that_stop_writes(self):
        _, found = drive(
            mysql_preflight.preflight_plan(self.mysql([UPDATE]), 60.0),
            [
                [("11.4.2-MariaDB", "shop")],
                [],
                [("1", "YES")],
                [MDL_READ, MDL_NO_WRITE, MDL_PENDING],
                [],
            ],
        )
        self.assertEqual(found.profile, "mariadb")
        self.assertEqual(
            [(b.session.label, b.granted) for b in found.blockers],
            [("connection 13", True), ("connection 14", False)],
        )

    def test_falls_back_to_metadata_lock_info(self):
        info = (12, "shop", "orders", "MDL_SHARED_READ", "app", "Sleep", 400, None)
        asked, found = drive(
            mysql_preflight.preflight_plan(self.mysql(["DROP TABLE orders"]), 60.0),
            [
                [("10.6.18-MariaDB", "shop")],
                RuntimeError("no performance_schema"),
                [("0", "YES")],
                [info],
                [],
            ],
        )
        self.assertIn("METADATA_LOCK_INFO", asked[3])
        self.assertEqual(found.read, {"locks", "transactions"})
        (blocker,) = found.blockers
        self.assertEqual(blocker.held, "SHARED_READ")
        self.assertIsNone(blocker.session.application)

    def test_nothing_records_the_locks(self):
        denied = RuntimeError("denied")
        _, found = drive(
            mysql_preflight.preflight_plan(self.mysql(["DROP TABLE orders"]), 60.0),
            [denied, denied, [], denied, denied],
        )
        self.assertEqual(found.profile, "mysql")
        self.assertEqual(found.read, frozenset())

    def test_a_failed_lock_read_falls_back(self):
        _, found = drive(
            mysql_preflight.preflight_plan(self.mysql(["DROP TABLE orders"]), 60.0),
            [
                [("8.0.40", None)],
                [],
                [("1", "YES")],
                RuntimeError("denied"),
                RuntimeError("unknown table"),
                [],
            ],
        )
        self.assertEqual(found.read, {"transactions"})


# session, schema, table, bare, mode, granted, login, program, status,
# transaction age, text
SCH_S = (57, "dbo", "orders", 1, "Sch-S", 1, "app", "worker", "running", 5, "q")
IS_LOCK = (58, "dbo", "orders", 1, "IS", 1, "app", "report", "sleeping", 700, "q")
IX_LOCK = (59, "dbo", "orders", 1, "IX", 0, "app", None, "running", 1, None)
GONE = (60, None, None, 0, "Sch-M", 1, None, None, None, None, None)
# session, login, program, status, age, text
OPEN = (61, "etl", "ssis", "sleeping", 4000, "BEGIN TRAN")


class MssqlPreflightTestCase(unittest.TestCase):
    def test_conflicts(self):
        def conflicts(lock, mode):
            return mssql_preflight.conflicts(Planned("", "t", lock, None), mode)

        self.assertTrue(conflicts("Sch-M", "Sch-S"))
        self.assertFalse(conflicts("S", "IS"))
        self.assertTrue(conflicts("S", "IX"))
        self.assertTrue(conflicts("S", "IU"))
        self.assertFalse(conflicts("IX", "IX"))
        self.assertTrue(conflicts("IX", "S"))
        self.assertFalse(conflicts("X", "Sch-S"))
        self.assertTrue(conflicts("X", "IS"))
        self.assertFalse(conflicts("Sch-S", "X"))
        self.assertTrue(conflicts("OTHER", "IS"))

    def test_reads_the_locks_and_transactions(self):
        statements = impacts(["CREATE INDEX ix ON orders (a)"], Dialects.MSSQL)
        asked, found = drive(
            mssql_preflight.preflight_plan(statements, 60.0),
            [[SCH_S, IS_LOCK, IX_LOCK, GONE], [OPEN]],
        )
        self.assertEqual(len(asked), 2)
        self.assertEqual(found.profile, "mssql")
        self.assertEqual(
            [(b.session.label, b.held, b.granted) for b in found.blockers],
            [("session 59", "IX", False)],
        )
        self.assertEqual([s.label for s in found.transactions], ["session 61"])

    def test_a_failed_read_is_left_out(self):
        denied = RuntimeError("VIEW SERVER STATE permission was denied")
        _, found = drive(
            mssql_preflight.preflight_plan(impacts([ADD], Dialects.MSSQL), 60.0),
            [denied, denied],
        )
        self.assertEqual(found.read, frozenset())


BLOCKED = Preflight(
    "postgres",
    (
        Blocker(
            ADD,
            "orders",
            "ACCESS EXCLUSIVE",
            "ACCESS SHARE",
            True,
            LiveSession(
                4121,
                "pid 4121",
                "billing",
                "billing-worker",
                "idle in transaction",
                2520.0,
                "SELECT *\n  FROM orders",
            ),
        ),
    ),
    (LiveSession(5003, "pid 5003", "report", None, "active", 720.0),),
    60.0,
    frozenset({"locks", "transactions"}),
)


class PreflightReportTestCase(unittest.TestCase):
    def test_blocker_line(self):
        self.assertEqual(
            blocker_line(BLOCKED.blockers[0]),
            f"{ADD} would queue behind pid 4121 (idle in transaction for 42m, "
            "user=billing, app=billing-worker, has ACCESS SHARE on orders)",
        )

    def test_a_waiting_lock_and_a_snapshot(self):
        waiting = BLOCKED.blockers[0]._replace(granted=False)
        self.assertIn("waits for ACCESS SHARE on orders", blocker_line(waiting))
        snapshot = BLOCKED.blockers[0]._replace(
            held=None, session=LiveSession(7, "pid 7", transaction_seconds=5.0)
        )
        self.assertEqual(
            blocker_line(snapshot),
            f"{ADD} would wait for the transaction of pid 7 to end "
            "(transaction open for 5s)",
        )

    def test_transaction_line(self):
        self.assertEqual(
            transaction_line(BLOCKED.transactions[0]),
            "pid 5003 has had a transaction open for 12m (active, user=report)",
        )
        bare = LiveSession(8, "pid 8", transaction_seconds=10_000.0)
        self.assertEqual(
            transaction_line(bare), "pid 8 has had a transaction open for 2h 46m"
        )

    def test_render_preflight(self):
        self.assertEqual(
            render_preflight(BLOCKED).splitlines(),
            [
                "preflight",
                f"  {blocker_line(BLOCKED.blockers[0])}",
                "    last statement: SELECT * FROM orders",
                f"  {transaction_line(BLOCKED.transactions[0])}",
                "  1 blocker, 1 transaction open 60s or longer. "
                "Read: locks, transactions",
            ],
        )

    def test_a_long_statement_is_cut(self):
        session = LiveSession(9, "pid 9", transaction_seconds=100.0, query="x" * 300)
        text = render_preflight(Preflight("postgres", (), (session,), 60.0))
        self.assertIn("x" * 197 + "...", text)
        self.assertNotIn("x" * 198, text)

    def test_the_summary_names_what_was_not_read(self):
        self.assertEqual(
            preflight_summary(Preflight("postgres", (), (), 30.0)),
            "0 blockers, 0 transactions open 30s or longer. Read: nothing. "
            "Not read: locks, transactions",
        )

    def test_preflight_data(self):
        data = preflight_data(BLOCKED)
        self.assertEqual(data["read"], ["locks", "transactions"])
        self.assertEqual(data["older_than"], 60.0)
        (blocker,) = data["blockers"]
        self.assertEqual(blocker["held"], "ACCESS SHARE")
        self.assertEqual(blocker["session"]["id"], 4121)
        self.assertEqual(data["transactions"][0]["transaction_seconds"], 720.0)

    def test_the_report_ends_with_its_preflight(self):
        report = analyze([ADD], PG)._replace(preflight=BLOCKED)
        self.assertTrue(render(report).endswith(render_preflight(BLOCKED)))
        self.assertEqual(report_data(report)["preflight"], preflight_data(BLOCKED))
        self.assertIsNone(report_data(analyze([ADD], PG))["preflight"])


def preflight_answer(sql):
    """The rows a Postgres server gives the context and preflight reads."""
    if "current_setting('server_version_num')" in sql:
        return [SETTINGS_ROW]
    if "pg_total_relation_size" in sql:
        return SIZE_ROWS
    if "FROM pg_catalog.pg_locks" in sql:
        return [READER]
    if "FROM pg_catalog.pg_stat_activity" in sql:
        return [REPORTER]
    return []


class ScriptedCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    description = None

    @property
    def rowcount(self):
        return len(self.rows)

    def execute(self, sql, params=()):
        self.connection.log.append(sql)
        if self.connection.refuse and self.connection.refuse in sql:
            raise RuntimeError("permission denied")
        self.rows = preflight_answer(sql) if self.connection.busy else []
        if not self.connection.busy and "pg_total_relation_size" in sql:
            self.rows = SIZE_ROWS

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class ScriptedConnection:
    """A Postgres stand-in whose other sessions are busy or not."""

    def __init__(self, busy=True, refuse=None):
        self.log = []
        self.busy = busy
        self.refuse = refuse

    def cursor(self):
        return ScriptedCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class Adapter(ScriptedAdapter):
    async def fetch(self, sql, params):
        self.log.append(sql)
        return [], preflight_answer(sql)


def run():
    return [Migration("001_orders", up=[ADD])]


class PreflightFunctionTestCase(unittest.TestCase):
    def test_reads_the_context_for_plain_statements(self):
        connection = ScriptedConnection()
        found = preflight(connection, PG, [ADD])
        self.assertEqual(found.blockers[0].session.label, "pid 4121")
        self.assertTrue(any("server_version_num" in sql for sql in connection.log))

    def test_uses_the_context_it_is_given(self):
        connection = ScriptedConnection()
        context = EngineContext("postgres", (16, 4))
        found = preflight(connection, PG, [ADD], older_than=1.0, context=context)
        self.assertEqual(found.older_than, 1.0)
        self.assertFalse(any("server_version_num" in sql for sql in connection.log))

    def test_uses_the_attached_impact(self):
        connection = ScriptedConnection()
        statements = attach_impact([MigrationStatement(ADD, "001")], PG)
        found = preflight(connection, PG, statements)
        self.assertEqual(len(found.blockers), 1)
        self.assertFalse(any("server_version_num" in sql for sql in connection.log))

    def test_refuses_a_dialect_without_one(self):
        with self.assertRaises(ValueError):
            preflight(sqlite3.connect(":memory:"), Dialects.DEFAULT, [ADD])

    def test_async_preflight_reads_the_same(self):
        found = asyncio.run(async_preflight(Adapter(), PG, [ADD]))
        self.assertEqual(found, preflight(ScriptedConnection(), PG, [ADD]))
        context = EngineContext("postgres", (16, 4))
        again = asyncio.run(async_preflight(Adapter(), PG, [ADD], context=context))
        self.assertEqual(len(again.blockers), 1)
        with self.assertRaises(ValueError):
            asyncio.run(async_preflight(Adapter(), Dialects.DUCKDB, [ADD]))


class MigratorPreflightTestCase(unittest.TestCase):
    def test_impact_live_adds_the_preflight(self):
        report = Migrator(ScriptedConnection(), run(), dialect=PG).impact(live=True)
        self.assertEqual(report.preflight.blockers[0].statement, ADD)
        self.assertIsNone(
            Migrator(ScriptedConnection(), run(), dialect=PG).impact().preflight
        )

    def test_preflight_returns_the_read(self):
        found = Migrator(ScriptedConnection(), run(), dialect=PG).preflight(
            older_than=10.0
        )
        self.assertEqual(found.older_than, 10.0)
        self.assertEqual([s.label for s in found.transactions], ["pid 5003"])

    @mock.patch(
        "sustained.impact.rules._profiles",
        return_value={"DEFAULT": (postgres.PROFILE._replace(preflight=None),)},
    )
    def test_live_refuses_a_dialect_without_a_preflight(self, _):
        migrator = Migrator(sqlite3.connect(":memory:"), [], dialect=Dialects.DEFAULT)
        with self.assertRaises(DialectError):
            migrator.impact(live=True)
        with self.assertRaises(DialectError):
            migrator.preflight()

    def test_up_refuses_when_a_session_is_in_the_way(self):
        connection = ScriptedConnection()
        migrator = Migrator(connection, run(), dialect=PG)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(PreflightBlocked) as caught:
                migrator.up(preflight="refuse")
        self.assertNotIn(ADD, connection.log)
        self.assertEqual(caught.exception.preflight.blockers[0].session.id, 4121)
        self.assertIn("would queue behind pid 4121", str(caught.exception))

    def test_up_warns_and_runs(self):
        connection = ScriptedConnection()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            applied = Migrator(connection, run(), dialect=PG).up(preflight="warn")
        self.assertEqual(applied, ["001_orders"])
        self.assertIn(ADD, connection.log)
        lines = stderr.getvalue().splitlines()
        self.assertIn(f"preflight: {blocker_line(BLOCKED.blockers[0])}", lines)
        self.assertIn(
            "preflight: pid 5003 has had a transaction open for 12m "
            "(active, user=report, app=metabase)",
            lines,
        )

    def test_up_refuses_nothing_on_an_idle_server(self):
        connection = ScriptedConnection(busy=False)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            Migrator(connection, run(), dialect=PG).up(preflight="refuse")
        self.assertIn(ADD, connection.log)
        self.assertNotIn("preflight:", stderr.getvalue())

    def test_up_names_a_read_that_failed(self):
        connection = ScriptedConnection(refuse="pg_locks")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            Migrator(connection, run(), dialect=PG).up(preflight="refuse")
        self.assertIn(ADD, connection.log)
        self.assertIn("preflight: could not read locks", stderr.getvalue())

    def test_up_without_a_preflight_reads_no_locks(self):
        connection = ScriptedConnection()
        with redirect_stderr(io.StringIO()):
            Migrator(connection, run(), dialect=PG).up()
        self.assertFalse(any("pg_locks" in sql for sql in connection.log))

    def test_up_takes_only_the_known_modes(self):
        with self.assertRaises(ValueError):
            Migrator(ScriptedConnection(), run(), dialect=PG).up(preflight="kill")
        with self.assertRaises(ValueError):
            Migrator(ScriptedConnection(), run(), dialect=PG).up(
                preflight=PreflightCheck("kill")
            )

    def test_a_check_sets_the_age_of_the_printed_transactions(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            Migrator(ScriptedConnection(), run(), dialect=PG).up(
                preflight=PreflightCheck("warn", older_than=3600.0)
            )
        self.assertIn("would queue behind pid 4121", stderr.getvalue())
        self.assertNotIn("pid 5003", stderr.getvalue())

    def test_up_refuses_a_preflight_on_a_dialect_without_one(self):
        for mode in ("warn", "refuse"):
            connection = sqlite3.connect(":memory:")
            migrator = Migrator(
                connection,
                [Migration("001_t", up="CREATE TABLE t (id INTEGER)")],
                dialect=Dialects.DEFAULT,
            )
            with self.assertRaises(DialectError):
                migrator.up(preflight=mode)
            tables = connection.execute("SELECT name FROM sqlite_master").fetchall()
            self.assertEqual(tables, [])

    def test_the_async_up_refuses_a_preflight_on_a_dialect_without_one(self):
        from sustained.aio import DbApiAsyncAdapter

        connection = sqlite3.connect(":memory:", check_same_thread=False)
        migrator = AsyncMigrator(
            DbApiAsyncAdapter(connection),
            [Migration("001_t", up="CREATE TABLE t (id INTEGER)")],
            dialect=Dialects.DUCKDB,
        )
        with self.assertRaises(DialectError):
            asyncio.run(migrator.up(preflight="refuse"))
        self.assertEqual(
            connection.execute("SELECT name FROM sqlite_master").fetchall(), []
        )

    def test_a_check_refuses_an_age_that_is_not_one(self):
        for age in (-1, -0.5, float("nan"), "60", None, True):
            with self.assertRaises(ValueError):
                PreflightCheck("warn", age)
        self.assertEqual(PreflightCheck("warn", 0).older_than, 0)
        self.assertEqual(PreflightCheck("warn", float("inf")).mode, "warn")

    def test_impact_and_preflight_refuse_an_age_that_is_not_one(self):
        connection = ScriptedConnection()
        migrator = Migrator(connection, run(), dialect=PG)
        with self.assertRaises(ValueError):
            migrator.impact(live=True, older_than=-1)
        with self.assertRaises(ValueError):
            migrator.preflight(older_than=float("nan"))
        self.assertEqual(connection.log, [])
        self.assertIsNone(migrator.impact(older_than=-1).preflight)
        with self.assertRaises(ValueError):
            asyncio.run(
                AsyncMigrator(Adapter(), run(), dialect=PG).preflight(older_than=-5)
            )
        with self.assertRaises(ValueError):
            preflight(ScriptedConnection(), PG, [ADD], older_than=-1)
        with self.assertRaises(ValueError):
            asyncio.run(async_preflight(Adapter(), PG, [ADD], older_than=float("nan")))

    def test_a_run_with_models_checks_only_the_generated_migration_again(self):
        from sustained.migrations.core import runs

        seen = []
        real = runs.check_preflight

        def spy(m, statements, check, shown):
            # SQLite has no preflight to read, so the spy records what
            # would be read and reads nothing.
            seen.append([str(s) for s in statements])
            return real(m, statements, None, shown)

        from sustained.model import Model
        from sustained.schema import Integer

        widget = type(
            "Widget",
            (Model,),
            {
                "tableName": "widgets",
                "tableColumns": {"id": Integer(primary_key=True)},
                "_dialect": Dialects.DEFAULT,
            },
        )

        connection = sqlite3.connect(":memory:")
        migrator = Migrator(
            connection,
            [Migration("001_t", up="CREATE TABLE t (id INTEGER)")],
            dialect=Dialects.DEFAULT,
        )
        with (
            mock.patch.object(runs, "check_preflight", spy),
            mock.patch("sustained.impact.preflight.covered", return_value=True),
        ):
            with redirect_stderr(io.StringIO()):
                migrator.up(models=[widget], preflight="warn")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], ["CREATE TABLE t (id INTEGER)"])
        self.assertTrue(all("widgets" in s for s in seen[1]))

    def test_the_async_migrator_reads_the_same(self):
        migrator = AsyncMigrator(Adapter(), run(), dialect=PG)
        report = asyncio.run(migrator.impact(live=True))
        self.assertEqual(
            report.preflight,
            Migrator(ScriptedConnection(), run(), dialect=PG)
            .impact(live=True)
            .preflight,
        )
        found = asyncio.run(migrator.preflight())
        self.assertEqual(found, report.preflight)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(PreflightBlocked):
                asyncio.run(
                    AsyncMigrator(Adapter(), run(), dialect=PG).up(preflight="refuse")
                )


if __name__ == "__main__":
    unittest.main()
