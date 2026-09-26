"""
Tests for the server facts the impact rules read: the Postgres catalog
read plan, read_context() and async_read_context(), and what a read
context changes in a report.
"""

import asyncio
import unittest

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
from sustained.impact.context import FLOORS
from sustained.impact.report import report_data, summary
from sustained.impact.rules.postgres import context_plan, server_version
from sustained.migrations import Migration, Migrator

PG = Dialects.POSTGRES

INDEX = "CREATE INDEX ix_orders_customer ON orders (customer_id)"

SETTINGS_ROW = ("160004", "UTC", "0")
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
        asked, context = drive(context_plan(), [[SETTINGS_ROW], SIZE_ROWS])
        self.assertEqual(len(asked), 2)
        self.assertEqual(context.profile, "postgres")
        self.assertEqual(context.version, (16, 4))
        self.assertEqual(context.settings["TimeZone"], "UTC")
        self.assertEqual(context.settings["lock_timeout"], "0")
        self.assertEqual(context.read, {"version", "settings", "sizes"})

    def test_keys_each_table_by_schema_and_by_its_visible_name(self):
        _, context = drive(context_plan(), [[SETTINGS_ROW], SIZE_ROWS])
        self.assertEqual(context.stats("orders"), TableStats(2_000_000, 3 << 30))
        self.assertEqual(context.stats("public.orders"), TableStats(2_000_000, 3 << 30))
        self.assertEqual(context.stats("audit.orders"), TableStats(10, 8192))
        self.assertEqual(context.stats("ORDERS").rows, 2_000_000)

    def test_a_table_never_analyzed_has_no_row_estimate(self):
        _, context = drive(context_plan(), [[SETTINGS_ROW], SIZE_ROWS])
        self.assertEqual(context.stats("fresh"), TableStats(None, 16384))

    def test_a_failed_settings_read_assumes_the_floor(self):
        _, context = drive(context_plan(), [RuntimeError("denied"), SIZE_ROWS])
        self.assertEqual(context.version, FLOORS["postgres"])
        self.assertEqual(dict(context.settings), {})
        self.assertEqual(context.read, {"sizes"})

    def test_a_failed_size_read_leaves_the_sizes_unknown(self):
        _, context = drive(context_plan(), [[SETTINGS_ROW], RuntimeError("denied")])
        self.assertEqual(context.read, {"version", "settings"})
        self.assertEqual(context.stats("orders"), TableStats())

    def test_server_version(self):
        self.assertEqual(server_version("120022"), (12, 22))
        self.assertEqual(server_version("180000"), (18, 0))

    def test_the_size_read_holds_no_percent_sign(self):
        asked, _ = drive(context_plan(), [[SETTINGS_ROW], SIZE_ROWS])
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
            read_context(ScriptedConnection(), Dialects.DEFAULT)

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


if __name__ == "__main__":
    unittest.main()
