"""
The impact analysis, run against every server whose support.json row
claims the `impact` cover: the server facts read_context() reads,
Migrator.impact() on a live connection, and rehearse(trace=True).
"""

import asyncio
import unittest

from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.impact import Evidence, Work, async_read_context, read_context
from sustained.migrations import Migration, Migrator

from . import aio_lifecycle, harness

TABLES = (
    "it_impact_orders",
    "it_impact_parts",
    "it_impact_notes",
    "it_impact_migrations",
    "it_impact_rehearsals",
)


class ImpactCase(unittest.TestCase):
    """
    Base for one server's `impact` cover. Subclasses set NAME to a row in
    support.json and DIALECT to the dialect that row names.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT

    @classmethod
    def setUpClass(cls):
        if not cls.NAME:
            raise unittest.SkipTest("base class")
        cls.connection = harness.connect(cls.NAME)

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()

    def setUp(self):
        self.drop()

    def tearDown(self):
        self.drop()

    def drop(self):
        self.connection.rollback()
        for table in TABLES:
            self.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        self.execute("DROP DOMAIN IF EXISTS it_impact_text")

    def execute(self, *statements):
        cursor = self.connection.cursor()
        for sql in statements:
            cursor.execute(sql)
        self.connection.commit()

    def fetch(self, sql):
        cursor = self.connection.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        self.connection.commit()
        return rows

    def test_reads_the_server_version_and_settings(self):
        ((number,),) = self.fetch("SHOW server_version_num")
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        number = int(number)
        self.assertEqual(context.version, (number // 10000, number % 10000))
        self.assertLessEqual({"version", "settings", "sizes", "schema"}, context.read)
        self.assertIn("TimeZone", context.settings)
        self.assertIn("lock_timeout", context.settings)

    def test_reads_each_tables_size(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id integer PRIMARY KEY, note text)",
            "INSERT INTO it_impact_orders SELECT g, 'n' FROM generate_series(1, 500) g",
            "ANALYZE it_impact_orders",
            "CREATE TABLE it_impact_notes (id integer)",
        )
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        orders = context.stats("it_impact_orders")
        self.assertEqual(orders.rows, 500)
        self.assertGreater(orders.bytes, 0)
        self.assertEqual(context.stats("public.it_impact_orders"), orders)
        # Never vacuumed or analyzed: the row estimate is empty.
        self.assertIsNone(context.stats("it_impact_notes").rows)
        self.assertIsNotNone(context.column_type("it_impact_orders", "note"))

    def test_the_async_read_matches_the_blocking_one(self):
        if self.NAME not in aio_lifecycle.ADAPTERS:
            self.skipTest(f"{self.NAME} has no async adapter")
        self.execute(
            "CREATE TABLE it_impact_orders (id integer PRIMARY KEY, note text)",
            "INSERT INTO it_impact_orders SELECT g, 'n' FROM generate_series(1, 20) g",
            "ANALYZE it_impact_orders",
        )
        blocking = read_context(self.connection, self.DIALECT)
        self.connection.rollback()

        async def read():
            adapter, close = await aio_lifecycle.ADAPTERS[self.NAME]()
            try:
                return await async_read_context(adapter, self.DIALECT)
            finally:
                await close()

        context = asyncio.run(read())
        self.assertEqual(context.version, blocking.version)
        self.assertEqual(context.read, blocking.read)
        self.assertEqual(
            context.stats("it_impact_orders"), blocking.stats("it_impact_orders")
        )

    def test_a_partitioned_table_sums_its_partitions(self):
        self.execute(
            "CREATE TABLE it_impact_parts (id integer) PARTITION BY RANGE (id)",
            "CREATE TABLE it_impact_parts_a PARTITION OF it_impact_parts "
            "FOR VALUES FROM (0) TO (100)",
            "CREATE TABLE it_impact_parts_b PARTITION OF it_impact_parts "
            "FOR VALUES FROM (100) TO (1000)",
            "INSERT INTO it_impact_parts SELECT generate_series(1, 300)",
            "ANALYZE it_impact_parts",
        )
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        parent = context.stats("it_impact_parts")
        leaves = [context.stats(f"it_impact_parts_{p}") for p in "ab"]
        self.assertEqual(parent.rows, 300)
        self.assertEqual(parent.bytes, sum(leaf.bytes for leaf in leaves))

    def test_the_read_leaves_an_open_transaction_usable(self):
        cursor = self.connection.cursor()
        cursor.execute("CREATE TABLE it_impact_notes (id integer)")
        read_context(self.connection, self.DIALECT)
        cursor.execute("INSERT INTO it_impact_notes VALUES (1)")
        cursor.execute("SELECT count(*) FROM it_impact_notes")
        self.assertEqual(cursor.fetchone()[0], 1)
        self.connection.rollback()

    def test_impact_reports_the_live_size(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id integer PRIMARY KEY, note text)",
            "INSERT INTO it_impact_orders SELECT g, 'n' FROM generate_series(1, 50) g",
            "ANALYZE it_impact_orders",
        )
        migrator = Migrator(
            self.connection,
            [
                Migration(
                    "001_note_index",
                    up=["CREATE INDEX it_impact_note_ix ON it_impact_orders (note)"],
                )
            ],
            dialect=self.DIALECT,
            table="it_impact_migrations",
        )
        report = migrator.impact()
        self.connection.rollback()
        self.assertIs(report.evidence, Evidence.CATALOG)
        self.assertIn("version", report.read)
        (table,) = report.statements[0].tables
        self.assertEqual(table.rows, 50)
        # A small table's blocking work is info, not a warning.
        index = [f for f in report.findings if f.rule == "pg.create_index"]
        self.assertEqual([str(f.severity) for f in index], ["info"])

    def migrator(self, migrations):
        return Migrator(
            self.connection,
            migrations,
            dialect=self.DIALECT,
            table="it_impact_migrations",
            rehearsal_table="it_impact_rehearsals",
        )

    def orders(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id integer PRIMARY KEY, note varchar(10))",
            "INSERT INTO it_impact_orders SELECT g, 'n' FROM generate_series(1, 50) g",
            "ANALYZE it_impact_orders",
        )

    TRACED = [
        Migration(
            "001_traced",
            up=[
                "CREATE INDEX it_impact_note_ix ON it_impact_orders (note)",
                "ALTER TABLE it_impact_orders ALTER COLUMN id TYPE bigint",
            ],
            down=None,
        )
    ]

    def test_a_traced_rehearsal_observes_each_statement(self):
        self.orders()
        results = self.migrator(self.TRACED).rehearse(trace=True)
        self.assertTrue(results.ok)
        report = results.impact
        self.assertIs(report.evidence, Evidence.OBSERVED)
        index, retype = report.statements
        self.assertEqual(
            [(t.table, t.lock, t.work) for t in index.tables],
            [("it_impact_orders", "SHARE", Work.INDEX_BUILD)],
        )
        self.assertEqual(
            [(t.table, t.lock, t.work) for t in retype.tables],
            [("it_impact_orders", "ACCESS EXCLUSIVE", Work.REWRITE)],
        )
        self.assertNotIn("impact.mismatch", [f.rule for f in report.findings])
        # The rehearsal rolled everything back.
        rows = self.fetch(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'it_impact_note_ix'"
        )
        self.assertEqual(rows, [(0,)])

    def test_a_trace_reports_a_wrong_prediction(self):
        self.orders()
        self.execute("CREATE DOMAIN it_impact_text AS text")
        results = self.migrator(
            [
                Migration(
                    "001_domain",
                    up="ALTER TABLE it_impact_orders ALTER COLUMN note "
                    "TYPE it_impact_text",
                    down=None,
                )
            ]
        ).rehearse(trace=True)
        (statement,) = results.impact.statements
        # A domain without constraints is binary coercible, which the
        # rules do not know, so they predicted a rewrite.
        self.assertIs(statement.tables[0].work, Work.SCAN)
        mismatches = [f for f in statement.findings if f.rule == "impact.mismatch"]
        self.assertEqual(len(mismatches), 1)
        self.assertIn("copied no file", mismatches[0].message)

    def test_the_async_trace_matches_the_blocking_one(self):
        if self.NAME not in aio_lifecycle.ADAPTERS:
            self.skipTest(f"{self.NAME} has no async adapter")
        self.orders()
        blocking = self.migrator(self.TRACED).rehearse(trace=True).impact

        async def rehearse():
            adapter, close = await aio_lifecycle.ADAPTERS[self.NAME]()
            try:
                migrator = AsyncMigrator(
                    adapter,
                    self.TRACED,
                    dialect=self.DIALECT,
                    table="it_impact_migrations",
                    rehearsal_table="it_impact_rehearsals",
                )
                return (await migrator.rehearse(trace=True)).impact
            finally:
                await close()

        report = asyncio.run(rehearse())
        self.assertIs(report.evidence, Evidence.OBSERVED)
        self.assertEqual(
            [s.tables for s in report.statements],
            [s.tables for s in blocking.statements],
        )
