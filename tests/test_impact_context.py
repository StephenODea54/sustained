"""
Tests for the server facts the impact rules read: the Postgres catalog
read plan, read_context() and async_read_context(), and what a read
context changes in a report.
"""

import asyncio
import unittest
from unittest import mock

from sustained.aio import AsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.impact import (
    EngineContext,
    Evidence,
    Severity,
    TableStats,
    analyze,
    async_read_context,
    read_context,
)
from sustained.impact.context import FLOORS, named_tables
from sustained.impact.report import report_data, summary
from sustained.impact.rules.postgres import context_plan, server_version
from sustained.migrations import Migration, Migrator

PG = Dialects.POSTGRES

INDEX = "CREATE INDEX ix_orders_customer ON orders (customer_id)"

SETTINGS_ROW = ("160004", "UTC", "0")
# The session's lock timeout before the size read, and the size read's.
TIMED = ("0", "1s")
TIMEOUT_SQL = (
    "SELECT pg_catalog.current_setting('lock_timeout'), "
    "pg_catalog.set_config('lock_timeout', '1s', false)"
)
RESTORE_SQL = (
    "SELECT pg_catalog.set_config('lock_timeout', "
    f"convert_from(decode('{b'0'.hex()}', 'hex'), 'UTF8'), false)"
)
SIZE_ROWS = [
    ("public", "orders", True, 2_000_000, 3 << 30, False),
    ("audit", "orders", False, 10, 8192, False),
    ("public", "fresh", True, 0, 16384, True),
]


def drive(plan, answers):
    """
    Runs a plan to its end. Each answer is the rows for the next
    statement, or an exception to throw in. Returns the statements the
    plan asked and the context it returned.
    """
    asked = []
    answers = list(answers)
    try:
        sql = next(plan)
        while True:
            asked.append(sql)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                sql = plan.throw(answer)
            else:
                sql = plan.send(answer)
    except StopIteration as stop:
        return asked, stop.value


def answer(sql):
    """The rows a Postgres server with the facts above gives a statement."""
    if "current_setting('server_version_num')" in sql:
        return [SETTINGS_ROW]
    if "pg_total_relation_size" in sql:
        return SIZE_ROWS
    if "current_setting('lock_timeout'), " in sql:
        return [TIMED]
    if "set_config('lock_timeout'" in sql:
        return [("0",)]
    return []


class ScriptedCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    @property
    def description(self):
        return None

    @property
    def rowcount(self):
        return len(self.rows)

    def execute(self, sql, params=()):
        self.connection.log.append(sql)
        if self.connection.refuse and self.connection.refuse in sql:
            raise RuntimeError(f"permission denied: {sql}")
        self.rows = answer(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class ScriptedConnection:
    """A Postgres connection stand-in that answers the context read."""

    def __init__(self, refuse=None):
        self.log = []
        self.refuse = refuse

    def cursor(self):
        return ScriptedCursor(self)

    def commit(self):
        self.log.append("<commit>")

    def rollback(self):
        self.log.append("<rollback>")


class ScriptedAdapter(AsyncAdapter):
    def __init__(self, refuse=None):
        self.log = []
        self.refuse = refuse

    async def fetch(self, sql, params):
        self.log.append(sql)
        if self.refuse and self.refuse in sql:
            raise RuntimeError(f"permission denied: {sql}")
        return [], answer(sql)

    async def execute(self, sql, params):
        self.log.append(sql)
        return 0

    async def commit(self):
        pass

    async def rollback(self):
        pass


class PostgresPlanTestCase(unittest.TestCase):
    def test_reads_the_version_settings_and_sizes(self):
        asked, context = drive(
            context_plan(), [[SETTINGS_ROW], [TIMED], SIZE_ROWS, [("0",)]]
        )
        self.assertEqual(len(asked), 4)
        self.assertEqual(asked[1], TIMEOUT_SQL)
        self.assertEqual(asked[3], RESTORE_SQL)
        self.assertEqual(context.profile, "postgres")
        self.assertEqual(context.version, (16, 4))
        self.assertEqual(context.settings["TimeZone"], "UTC")
        self.assertEqual(context.settings["lock_timeout"], "0")
        self.assertEqual(context.read, {"version", "settings", "sizes"})

    def test_keys_each_table_by_schema_and_by_its_visible_name(self):
        _, context = drive(
            context_plan(), [[SETTINGS_ROW], [TIMED], SIZE_ROWS, [("0",)]]
        )
        self.assertEqual(context.stats("orders"), TableStats(2_000_000, 3 << 30))
        self.assertEqual(context.stats("public.orders"), TableStats(2_000_000, 3 << 30))
        self.assertEqual(context.stats("audit.orders"), TableStats(10, 8192))
        self.assertEqual(context.stats("ORDERS").rows, 2_000_000)

    def test_a_table_never_analyzed_has_no_row_estimate(self):
        _, context = drive(
            context_plan(), [[SETTINGS_ROW], [TIMED], SIZE_ROWS, [("0",)]]
        )
        self.assertEqual(context.stats("fresh"), TableStats(None, 16384))

    def test_a_failed_settings_read_assumes_the_floor(self):
        _, context = drive(
            context_plan(), [RuntimeError("denied"), [TIMED], SIZE_ROWS, [("0",)]]
        )
        self.assertEqual(context.version, FLOORS["postgres"])
        self.assertEqual(dict(context.settings), {})
        self.assertEqual(context.read, {"sizes"})

    def test_a_failed_size_read_leaves_the_sizes_unknown(self):
        _, context = drive(
            context_plan(), [[SETTINGS_ROW], [TIMED], RuntimeError("denied"), [("0",)]]
        )
        self.assertEqual(context.read, {"version", "settings"})
        self.assertEqual(context.stats("orders"), TableStats())

    def test_server_version(self):
        self.assertEqual(server_version("120022"), (12, 22))
        self.assertEqual(server_version("180000"), (18, 0))

    def test_the_size_read_holds_no_percent_sign(self):
        asked, _ = drive(context_plan(), [[SETTINGS_ROW], [TIMED], SIZE_ROWS, [("0",)]])
        self.assertTrue(all("%" not in sql for sql in asked))


class ReadContextTestCase(unittest.TestCase):
    def test_reads_the_facts_and_the_schema(self):
        connection = ScriptedConnection()
        context = read_context(connection, PG)
        self.assertEqual(context.version, (16, 4))
        self.assertEqual(context.read, {"version", "settings", "sizes", "schema"})
        self.assertIsNotNone(context.schema)

    def test_each_statement_runs_inside_a_savepoint(self):
        connection = ScriptedConnection()
        read_context(connection, PG)
        position = next(
            i for i, sql in enumerate(connection.log) if "server_version_num" in sql
        )
        self.assertTrue(connection.log[position - 1].startswith("SAVEPOINT"))

    def test_a_refused_statement_leaves_its_facts_out(self):
        connection = ScriptedConnection(refuse="pg_total_relation_size")
        context = read_context(connection, PG)
        self.assertNotIn("sizes", context.read)
        self.assertIn("version", context.read)
        rolled_back = [s for s in connection.log if s.startswith("ROLLBACK TO")]
        self.assertTrue(rolled_back)

    def test_refuses_a_dialect_without_rules(self):
        with self.assertRaises(ValueError):
            read_context(ScriptedConnection(), Dialects.PRESTO)

    def test_the_async_read_matches(self):
        adapter = ScriptedAdapter(refuse="pg_total_relation_size")
        context = asyncio.run(async_read_context(adapter, PG))
        self.assertEqual(context.version, (16, 4))
        self.assertEqual(context.read, {"version", "settings", "schema"})


class ContextReportTestCase(unittest.TestCase):
    def context(self, **settings):
        return EngineContext(
            "postgres",
            (16, 4),
            settings=settings,
            tables={"orders": TableStats(2_000_000, 3 << 30)},
            read=frozenset({"version", "settings", "sizes"}),
        )

    def test_a_large_table_read_from_the_server_is_danger(self):
        report = analyze([INDEX], PG, self.context(lock_timeout="0"))
        (statement,) = report.statements
        self.assertEqual(statement.tables[0].rows, 2_000_000)
        severities = {f.rule: f.severity for f in statement.findings}
        self.assertIs(severities["pg.create_index"], Severity.DANGER)

    def test_a_timeout_on_the_connection_covers_the_run(self):
        report = analyze([INDEX], PG, self.context(lock_timeout="5s"))
        rules = [f.rule for f in report.findings]
        self.assertNotIn("pg.lock_timeout", rules)

    def test_a_zero_timeout_on_the_connection_covers_nothing(self):
        report = analyze([INDEX], PG, self.context(lock_timeout="0"))
        rules = [f.rule for f in report.findings]
        self.assertIn("pg.lock_timeout", rules)

    def test_a_later_zero_timeout_turns_the_connections_off(self):
        statements = [
            MigrationStatement("SET lock_timeout = 0", "m1", True),
            MigrationStatement(INDEX, "m1", True),
        ]
        report = analyze(statements, PG, self.context(lock_timeout="5s"))
        rules = [f.rule for f in report.findings]
        self.assertIn("pg.lock_timeout", rules)

    def test_the_report_names_what_was_read(self):
        report = analyze([INDEX], PG, self.context())
        self.assertIs(report.evidence, Evidence.CATALOG)
        self.assertEqual(report.read, {"version", "settings", "sizes"})
        self.assertEqual(report_data(report)["read"], ["settings", "sizes", "version"])
        self.assertTrue(summary(report).endswith("Evidence: catalog (PostgreSQL 16.4)"))

    def test_a_version_not_read_is_assumed_in_the_summary(self):
        context = EngineContext("postgres", (12,), read=frozenset({"sizes"}))
        report = analyze([INDEX], PG, context)
        self.assertTrue(
            summary(report).endswith("Evidence: catalog (assumed PostgreSQL 12)")
        )

    def test_a_static_report_reads_nothing(self):
        report = analyze([INDEX], PG)
        self.assertEqual(report.read, frozenset())
        self.assertEqual(report_data(report)["read"], [])
        self.assertTrue(
            summary(report).endswith("Evidence: static (assumed PostgreSQL 12)")
        )


class MigratorContextTestCase(unittest.TestCase):
    def test_impact_reads_the_context_from_the_connection(self):
        migrator = Migrator(
            ScriptedConnection(), [Migration("001_orders", up=[INDEX])], dialect=PG
        )
        report = migrator.impact()
        self.assertEqual(report.version, (16, 4))
        self.assertIs(report.evidence, Evidence.CATALOG)
        self.assertEqual(report.statements[0].tables[0].bytes, 3 << 30)
        self.assertEqual(report.statements[0].severity, Severity.DANGER)

    def test_the_async_impact_reads_it_through_the_adapter(self):
        migrator = AsyncMigrator(
            ScriptedAdapter(), [Migration("001_orders", up=[INDEX])], dialect=PG
        )
        report = asyncio.run(migrator.impact())
        self.assertEqual(report.version, (16, 4))
        self.assertEqual(report.statements[0].tables[0].rows, 2_000_000)


class ScopedReadTestCase(unittest.TestCase):
    """The size read of each profile, scoped to the tables a run names."""

    def test_postgres_names_the_tables_in_one_statement(self):
        asked, context = drive(
            context_plan(False, {"orders", "Items"}),
            [[SETTINGS_ROW], [TIMED], SIZE_ROWS, [("0",)]],
        )
        self.assertEqual(len(asked), 4)
        self.assertIn(
            "AND lower(c.relname) IN "
            f"(convert_from(decode('{b'Items'.hex()}', 'hex'), 'UTF8'), "
            f"convert_from(decode('{b'orders'.hex()}', 'hex'), 'UTF8'))",
            asked[2],
        )
        self.assertEqual(context.stats("orders").rows, 2_000_000)
        self.assertIn("sizes", context.read)

    def test_postgres_quotes_nothing_from_a_name(self):
        asked, _ = drive(
            context_plan(False, ["o'rders%\\"]), [[SETTINGS_ROW], [TIMED], [], [("0",)]]
        )
        self.assertTrue(all("%" not in sql and "o'r" not in sql for sql in asked))

    def test_a_table_whose_lock_is_not_granted_is_the_only_one_unknown(self):
        timeout = RuntimeError("canceling statement due to lock timeout")
        answers = [[SETTINGS_ROW], [TIMED], timeout, timeout, [SIZE_ROWS[0]], [("0",)]]
        asked, context = drive(context_plan(False, {"items", "orders"}), answers)
        self.assertEqual(len(asked), 6)
        self.assertIn(f"'{b'items'.hex()}'", asked[3])
        self.assertNotIn(f"'{b'orders'.hex()}'", asked[3])
        self.assertIn(f"'{b'orders'.hex()}'", asked[4])
        self.assertEqual(asked[5], RESTORE_SQL)
        self.assertEqual(context.stats("items"), TableStats())
        self.assertEqual(context.stats("orders").rows, 2_000_000)
        self.assertIn("sizes", context.read)

    def test_a_single_table_not_granted_leaves_the_sizes_unread(self):
        timeout = RuntimeError("canceling statement due to lock timeout")
        asked, context = drive(
            context_plan(False, {"orders"}),
            [[SETTINGS_ROW], [TIMED], timeout, [("0",)]],
        )
        self.assertEqual(len(asked), 4)
        self.assertNotIn("sizes", context.read)

    def test_no_named_table_reads_no_size(self):
        asked, context = drive(context_plan(False, set()), [[SETTINGS_ROW]])
        self.assertEqual(len(asked), 1)
        self.assertEqual(dict(context.tables), {})

    def test_the_timeout_put_back_is_the_one_the_session_had(self):
        denied = RuntimeError("denied")
        answers = [denied, [("5s", "1s")], SIZE_ROWS, [("5s",)]]
        asked, _ = drive(context_plan(), answers)
        self.assertIn(f"decode('{b'5s'.hex()}', 'hex')", asked[3])
        self.assertTrue(asked[3].startswith("SELECT pg_catalog.set_config("))

    def test_a_refused_timeout_is_not_put_back(self):
        denied = RuntimeError("denied")
        asked, context = drive(context_plan(), [[SETTINGS_ROW], denied, SIZE_ROWS])
        self.assertEqual(len(asked), 3)
        self.assertIn("sizes", context.read)

    def test_mysql_names_the_tables_in_hex(self):
        from sustained.impact.rules import mysql

        asked, _ = drive(
            mysql.context_plan(False, {"orders"}),
            [[("8.0.19", 1, 50)], [], []],
        )
        self.assertTrue(
            asked[1].endswith(f"AND LOWER(TABLE_NAME) IN (X'{b'orders'.hex()}')")
        )
        asked, _ = drive(
            mysql.context_plan(False, set()), [[("8.0.19", 1, 50)], [], []]
        )
        self.assertTrue(asked[1].endswith("IN (NULL)"))

    def test_mssql_names_the_tables_as_utf16(self):
        from sustained.impact.rules import mssql

        answers = [[("16.0.1", 3, "Developer", -1)], [(False,)], []]
        asked, _ = drive(mssql.context_plan(False, {"orders"}), answers)
        spelled = "orders".encode("utf-16-le").hex()
        self.assertTrue(
            asked[2].endswith(
                f"WHERE LOWER(t.name) IN (CONVERT(nvarchar(128), 0x{spelled}))"
            )
        )

    def test_duckdb_reads_the_named_tables_only(self):
        from sustained.impact.rules.duckdb import context_plan as duckdb_plan

        asked, _ = drive(duckdb_plan(False, {"t", "o'x"}), [[("v1.1.0",)], []])
        self.assertTrue(asked[1].endswith("AND lower(table_name) IN ('o''x', 't')"))


class NamedTablesTestCase(unittest.TestCase):
    def test_names_each_table_a_statement_acts_on(self):
        statements = [
            "ALTER TABLE app.Orders ADD COLUMN c int",
            "ALTER TABLE items RENAME TO things",
            "ALTER TABLE things ADD COLUMN d int",
        ]
        self.assertEqual(named_tables(statements, PG), {"orders", "items", "things"})

    def test_the_schema_names_the_table_of_an_index(self):
        import sqlite3

        from sustained.introspect import introspect_schema

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE t (id INTEGER)")
        connection.execute("CREATE INDEX ix ON t (id)")
        schema = introspect_schema(connection, Dialects.DEFAULT)
        self.assertNotIn("t", named_tables(["DROP INDEX ix"], Dialects.DEFAULT))
        self.assertEqual(
            named_tables(["DROP INDEX ix"], Dialects.DEFAULT, schema), {"t"}
        )

    def test_read_context_sizes_only_the_named_tables(self):
        connection = ScriptedConnection()
        context = read_context(connection, PG, statements=[INDEX])
        sizes = [sql for sql in connection.log if "pg_total_relation_size" in sql]
        self.assertEqual(len(sizes), 1)
        self.assertIn(f"'{b'orders'.hex()}'", sizes[0])
        self.assertIn("schema", context.read)
        adapter = ScriptedAdapter()
        asyncio.run(async_read_context(adapter, PG, statements=[INDEX]))
        self.assertEqual(
            [sql for sql in adapter.log if "pg_total_relation_size" in sql], sizes
        )

    def test_a_failed_schema_read_still_reads_the_sizes(self):
        connection = ScriptedConnection()
        with mock.patch(
            "sustained.introspect.runner.introspect_schema",
            side_effect=RuntimeError("denied"),
        ):
            context = read_context(connection, PG, statements=[INDEX])
        self.assertNotIn("schema", context.read)
        self.assertIn("sizes", context.read)
        with mock.patch(
            "sustained.introspect.runner.async_introspect_schema",
            side_effect=RuntimeError("denied"),
        ):
            context = asyncio.run(async_read_context(ScriptedAdapter(), PG))
        self.assertNotIn("schema", context.read)


if __name__ == "__main__":
    unittest.main()
