"""
The impact analysis on SQL Server: the facts read_context() reads, a
traced rehearsal on a scratch database, and the ground truth for every
rule. Each rule fixture runs inside a transaction on a scratch database
holding the fixture schema, between two sightings of the locks the
session has, the partitions of the tables it names, and the log the
transaction has written, and the transaction is then rolled back. What
the server did must match what the rule predicted for that server's
version, edition, and settings.

The test containers run the Developer edition, which has the
Enterprise features, so the rules for the other editions are checked by
the unit tests alone.
"""

import unittest

from sustained.dialects import Dialects
from sustained.exceptions import PreflightBlocked
from sustained.impact import Evidence, Work, analyze, preflight, read_context
from sustained.impact.rules import profile_for
from sustained.impact.rules.mssql.trace import observe, sighting_plan, tables_plan
from sustained.introspect.runner import run_plan
from sustained.migrations import Migration, Migrator

from . import harness

# The fixture schema's objects, in an order that drops each before the
# objects it depends on.
FIXTURE_OBJECTS = (
    ("VIEW", "vw"),
    ("VIEW", "v2"),
    ("TABLE", "n"),
    ("TABLE", "t9"),
    ("TABLE", "t"),
    ("TABLE", "r"),
    ("TABLE", "h"),
    ("TABLE", "k"),
    ("TABLE", "u"),
    ("TABLE", "u2"),
    ("TABLE", "p"),
)

TABLES = ("it_impact_orders", "it_impact_migrations", "it_impact_rehearsals")


class MssqlImpactCase(unittest.TestCase):
    """
    Base for SQL Server's `impact` cover. Subclasses set NAME to the row
    in support.json.
    """

    NAME = ""
    DIALECT = Dialects.MSSQL

    @classmethod
    def setUpClass(cls):
        if not cls.NAME:
            raise unittest.SkipTest("base class")
        cls.connection = harness.connect_scratch(cls.NAME)

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()

    def setUp(self):
        self.drop()
        self.addCleanup(self.drop)

    def drop(self):
        self.connection.rollback()
        for kind, name in FIXTURE_OBJECTS:
            self.execute(f"DROP {kind} IF EXISTS {name}")
        for table in TABLES:
            self.execute(f"DROP TABLE IF EXISTS {table}")

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

    def test_reads_the_server_version_edition_and_settings(self):
        ((version, engine),) = self.fetch(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)), "
            "CAST(SERVERPROPERTY('EngineEdition') AS int)"
        )
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        self.assertEqual(context.profile, "mssql")
        self.assertEqual(context.version, tuple(int(p) for p in version.split(".")))
        self.assertLessEqual(
            {"version", "edition", "settings", "sizes", "clustered", "schema"},
            context.read,
        )
        self.assertEqual(context.settings["EngineEdition"], str(engine))
        self.assertEqual(context.settings["lock_timeout"], "-1")
        self.assertIn(context.settings["read_committed_snapshot"], ("on", "off"))
        self.assertIn("Edition", context.edition)

    def test_reads_each_tables_size_and_clustered_index(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id int CONSTRAINT pk_it_impact PRIMARY "
            "KEY, note nvarchar(20))",
            "INSERT INTO it_impact_orders SELECT TOP 300 ROW_NUMBER() OVER "
            "(ORDER BY (SELECT 1)), N'n' FROM sys.all_objects",
            "CREATE TABLE it_impact_migrations (id int)",
        )
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        orders = context.stats("it_impact_orders")
        self.assertEqual(orders.rows, 300)
        self.assertGreater(orders.bytes, 0)
        self.assertFalse(orders.heap)
        self.assertEqual(orders.clustered, "pk_it_impact")
        self.assertEqual(context.stats("dbo.it_impact_orders"), orders)
        heap = context.stats("it_impact_migrations")
        self.assertEqual((heap.rows, heap.heap, heap.clustered), (0, True, None))

    def reader(self):
        """
        A second session that has read it_impact_orders under HOLDLOCK in
        a transaction it leaves open, and its session id.
        """
        other = harness.connect_scratch(self.NAME)
        self.addCleanup(other.close)
        self.addCleanup(other.rollback)
        cursor = other.cursor()
        cursor.execute("SELECT @@SPID")
        ((session,),) = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM it_impact_orders WITH (HOLDLOCK)")
        cursor.fetchall()
        return session

    def orders(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id int PRIMARY KEY, note varchar(10))",
            "INSERT INTO it_impact_orders VALUES (1, 'n'), (2, 'n')",
        )

    def test_preflight_names_a_session_that_read_the_table(self):
        self.orders()
        session = self.reader()
        add = "ALTER TABLE it_impact_orders ADD extra int"
        found = preflight(self.connection, self.DIALECT, [add], older_than=0.0)
        self.connection.rollback()
        self.assertEqual(found.read, {"locks", "transactions"})
        (blocker,) = [b for b in found.blockers if b.session.id == session]
        self.assertEqual(blocker.held, "IS")
        self.assertTrue(blocker.granted)
        self.assertEqual(blocker.session.state, "sleeping")
        self.assertIn("it_impact_orders", blocker.session.query)
        self.assertNotIn(session, [s.id for s in found.transactions])

    def test_preflight_passes_a_reader_for_a_shared_lock(self):
        self.orders()
        session = self.reader()
        index = "CREATE INDEX it_impact_note ON it_impact_orders (note)"
        found = preflight(self.connection, self.DIALECT, [index], older_than=0.0)
        self.connection.rollback()
        self.assertNotIn(session, [b.session.id for b in found.blockers])
        self.assertIn(session, [s.id for s in found.transactions])

    def test_up_refuses_while_a_session_reads_the_table(self):
        self.orders()
        self.reader()
        migrator = Migrator(
            self.connection,
            [Migration("001_extra", up="ALTER TABLE it_impact_orders ADD extra int")],
            dialect=self.DIALECT,
            table="it_impact_migrations",
        )
        with self.assertRaises(PreflightBlocked):
            migrator.up(preflight="refuse")
        self.connection.rollback()
        ((length,),) = self.fetch("SELECT COL_LENGTH('it_impact_orders', 'extra')")
        self.assertIsNone(length)

    def test_each_rule_fixture_does_what_its_rule_predicts(self):
        profile = profile_for(self.DIALECT)
        self.execute(*profile.fixture_schema)
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        fixtures = [(rule, f) for rule in profile.rules for f in rule.fixtures]
        for rule, fixture in fixtures:
            with self.subTest(rule=rule.id, fixture=fixture):
                statement = self.observe_fixture(fixture, context, profile)
                self.assertIs(statement.evidence, Evidence.OBSERVED)
                reached = {t.rule for t in statement.tables}
                reached |= {f.rule for f in statement.findings}
                self.assertIn(rule.id, reached)
                mismatches = [
                    f.message for f in statement.findings if f.rule == "impact.mismatch"
                ]
                self.assertEqual(mismatches, [])

    def observe_fixture(self, fixture, context, profile):
        """
        The fixture's predicted impact with what the server did in its
        place, read between two sightings in a transaction that is then
        rolled back.
        """
        (predicted,) = analyze([fixture], self.DIALECT, context).statements
        tables = [t.table for t in predicted.tables]
        try:
            existing = run_plan(self.connection, self.DIALECT, tables_plan())
            before = run_plan(self.connection, self.DIALECT, sighting_plan(tables))
            self.connection.cursor().execute(fixture)
            after = run_plan(self.connection, self.DIALECT, sighting_plan(tables))
        finally:
            self.connection.rollback()
        read = {"locks", "log"} | ({"storage"} if tables else set())
        self.assertLessEqual(read, after.read)
        return observe(predicted, before, after, existing, profile)

    def test_a_traced_rehearsal_observes_each_statement(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id int PRIMARY KEY, note varchar(10), "
            "size varchar(10))",
            "INSERT INTO it_impact_orders SELECT TOP 2000 ROW_NUMBER() OVER "
            "(ORDER BY (SELECT 1)), 'n', 's' FROM sys.all_objects a CROSS JOIN "
            "sys.all_objects b",
        )
        migrator = Migrator(
            self.connection,
            [
                Migration(
                    "001_traced",
                    up=[
                        "CREATE INDEX it_impact_note_ix ON it_impact_orders (note)",
                        "ALTER TABLE it_impact_orders ALTER COLUMN size varchar(5)",
                    ],
                    down=None,
                )
            ],
            dialect=self.DIALECT,
            table="it_impact_migrations",
            rehearsal_table="it_impact_rehearsals",
        )
        results = migrator.rehearse(scratch=True, trace=True)
        self.assertTrue(results.ok, results)
        report = results.impact
        self.assertIs(report.evidence, Evidence.OBSERVED)
        index, retype = report.statements
        self.assertEqual(
            [(t.table, t.lock, t.work) for t in index.tables],
            [("it_impact_orders", "S", Work.INDEX_BUILD)],
        )
        self.assertEqual(
            [(t.table, t.lock, t.work) for t in retype.tables],
            [("it_impact_orders", "Sch-M", Work.REWRITE)],
        )
        self.assertNotIn("impact.mismatch", [f.rule for f in report.findings])
        rows = self.fetch(
            "SELECT COUNT(*) FROM sys.indexes WHERE name = 'it_impact_note_ix'"
        )
        self.assertEqual(rows[0][0], 0)
