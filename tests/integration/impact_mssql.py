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

import threading
import unittest

from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.exceptions import PreflightBlocked
from sustained.impact import (
    Evidence,
    Severity,
    Work,
    analyze,
    preflight,
    read_context,
)
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

# The fixtures the server refuses inside the fixture's transaction, with
# the error number it raises. Their rule predicts the refusal.
REFUSED = {
    "CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON, RESUMABLE = ON)": 574,
    "ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON, RESUMABLE = ON)": 574,
}

# How long each session of a two-session test waits for a lock, in
# milliseconds, and for a query, in seconds.
LOCK_WAIT = 2000
QUERY_WAIT = 30


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
                if fixture in REFUSED:
                    self.assert_refused(fixture, REFUSED[fixture], rule, context)
                    continue
                statement = self.observe_fixture(fixture, context, profile)
                self.assertIs(statement.evidence, Evidence.OBSERVED)
                reached = {t.rule for t in statement.tables}
                reached |= {f.rule for f in statement.findings}
                self.assertIn(rule.id, reached)
                mismatches = [
                    f.message for f in statement.findings if f.rule == "impact.mismatch"
                ]
                self.assertEqual(mismatches, [])

    def assert_refused(self, fixture, error, rule, context):
        """
        The server raises the error inside a transaction, and the rule
        predicts it with a `danger` finding.
        """
        (predicted,) = analyze([fixture], self.DIALECT, context).statements
        dangers = [f.rule for f in predicted.findings if f.severity is Severity.DANGER]
        self.assertIn(rule.id, dangers)
        pyodbc = harness.driver(self.NAME)
        try:
            with self.assertRaises(pyodbc.Error) as raised:
                self.connection.cursor().execute(fixture)
        finally:
            self.connection.rollback()
        self.assertIn(f"({error})", str(raised.exception))

    def session(self, database=None, autocommit=False):
        """
        Another connection to the server, which waits LOCK_WAIT for a
        lock and QUERY_WAIT for a query, and its session id. It is rolled
        back and closed when the test ends.
        """
        other = harness.connect_mssql(self.NAME, database, autocommit)
        other.timeout = QUERY_WAIT
        self.addCleanup(other.close)
        if not autocommit:
            self.addCleanup(other.rollback)
        cursor = other.cursor()
        cursor.execute(f"SET LOCK_TIMEOUT {LOCK_WAIT}")
        cursor.execute("SELECT @@SPID")
        ((identifier,),) = cursor.fetchall()
        return other, identifier

    def writer(self):
        """A session with an open transaction that has updated order 1."""
        other, _ = self.session()
        other.cursor().execute("UPDATE it_impact_orders SET note = 'w' WHERE id = 1")
        return other

    def run_aside(self, connection, sql):
        """
        Runs the statement on the connection in a thread, and returns the
        thread and a list that receives the error it raised, if any.
        """
        errors = []

        def run():
            try:
                connection.cursor().execute(sql)
            except Exception as error:  # noqa: BLE001 - the test reads it
                errors.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def test_an_online_build_outside_a_transaction_waits_behind_a_writer(self):
        self.orders()
        self.writer()
        index = (
            "CREATE INDEX it_impact_note ON it_impact_orders (note) WITH (ONLINE = ON)"
        )
        builder, _ = self.session(autocommit=True)
        pyodbc = harness.driver(self.NAME)
        with self.assertRaises(pyodbc.Error) as raised:
            builder.cursor().execute(index)
        self.assertIn("(1222)", str(raised.exception))
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        statement = MigrationStatement(index, "001", False)
        (predicted,) = analyze([statement], self.DIALECT, context).statements
        self.assertEqual([t.lock for t in predicted.tables], ["S"])
        self.assertIn("mssql.lock_timeout", [f.rule for f in predicted.findings])

    def test_a_low_priority_build_lets_later_writers_go_ahead(self):
        self.orders()
        blocker = self.writer()
        wait = (
            "WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)"
        )
        index = (
            "CREATE INDEX it_impact_note ON it_impact_orders (note) "
            f"WITH (ONLINE = ON ({wait}))"
        )
        builder, _ = self.session(autocommit=True)
        thread, errors = self.run_aside(builder, index)
        self.addCleanup(thread.join, QUERY_WAIT)
        later, _ = self.session()
        # Waits until the builder's request is in the low-priority queue.
        waiting = []
        for _ in range(50):
            waiting = self.fetch(
                "SELECT request_status FROM sys.dm_tran_locks WHERE "
                "request_status = 'LOW_PRIORITY_WAIT' AND resource_database_id = DB_ID()"
            )
            if waiting:
                break
            threading.Event().wait(0.1)
        self.assertTrue(waiting)
        later.cursor().execute("UPDATE it_impact_orders SET note = 'l' WHERE id = 2")
        later.rollback()
        blocker.rollback()
        thread.join(QUERY_WAIT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_preflight_names_a_transaction_from_another_database(self):
        self.orders()
        scratch = self.fetch("SELECT DB_NAME()")[0][0]
        other, session = self.session(database="master")
        other.cursor().execute(
            f"UPDATE [{scratch}].dbo.it_impact_orders SET note = 'm' WHERE id = 1"
        )
        found = preflight(self.connection, self.DIALECT, ["SELECT 1"], older_than=0.0)
        self.connection.rollback()
        self.assertIn(session, [s.id for s in found.transactions])

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
