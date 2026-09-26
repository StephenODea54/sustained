"""
The impact analysis on MySQL and MariaDB, run against every server whose
support.json row claims the `impact` cover and runs InnoDB: the server
facts read_context() reads, Migrator.impact() on a live connection, and
the parent-table metadata locks the rules predict for foreign keys,
checked by holding a read on the parent in a second session.

The tests run in the scratch database, since MySQL schema changes do not
roll back.
"""

import unittest

from sustained.dialects import Dialects
from sustained.impact import Evidence, Work, analyze, read_context
from sustained.impact.rules import mysql
from sustained.migrations import Migration, Migrator

from . import harness

TABLES = (
    "it_impact_child",
    "it_impact_orders",
    "it_impact_notes",
    "it_impact_texts",
    "it_impact_parent",
    "it_impact_migrations",
)

# MySQL's error for a lock wait that ran out of lock_wait_timeout.
LOCK_WAIT_TIMEOUT = 1205


class InnodbImpactCase(unittest.TestCase):
    """
    Base for one InnoDB server's `impact` cover. Subclasses set NAME to a
    row in support.json and PROFILE to the rule profile its server
    reads as.
    """

    NAME = ""
    PROFILE = ""
    DIALECT = Dialects.MYSQL

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

    def tearDown(self):
        self.drop()

    def drop(self):
        self.connection.rollback()
        self.execute("SET SESSION lock_wait_timeout = DEFAULT")
        self.execute("SET SESSION foreign_key_checks = 0")
        for table in TABLES:
            self.execute(f"DROP TABLE IF EXISTS {table}")
        self.execute("SET SESSION foreign_key_checks = 1")

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

    def orders(self, count=50):
        self.execute(
            "CREATE TABLE it_impact_orders (id int PRIMARY KEY, note varchar(20))"
        )
        cursor = self.connection.cursor()
        cursor.executemany(
            "INSERT INTO it_impact_orders VALUES (%s, %s)",
            [(i, f"n{i}") for i in range(1, count + 1)],
        )
        self.connection.commit()
        self.fetch("ANALYZE TABLE it_impact_orders")

    def context(self):
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        return context

    def test_reads_the_server_version_and_settings(self):
        ((text,),) = self.fetch("SELECT VERSION()")
        context = self.context()
        self.assertEqual((context.profile, context.version), mysql.server_version(text))
        self.assertEqual(context.profile, self.PROFILE)
        self.assertLessEqual(
            {"version", "settings", "sizes", "fulltext", "schema"}, context.read
        )
        self.assertIn("lock_wait_timeout", context.settings)
        self.assertEqual(context.settings["foreign_key_checks"], "1")
        if self.PROFILE == "mysql" and context.version >= (8, 0, 29):
            self.assertIn("row_versions", context.read)
        else:
            self.assertNotIn("row_versions", context.read)

    def test_reads_each_tables_size_and_storage(self):
        self.orders(500)
        self.execute(
            "CREATE TABLE it_impact_texts (id int PRIMARY KEY, body text, "
            "FULLTEXT KEY it_impact_body (body))",
            "CREATE TABLE it_impact_notes (id int PRIMARY KEY) ROW_FORMAT=COMPACT",
        )
        context = self.context()
        orders = context.stats("it_impact_orders")
        self.assertGreater(orders.rows, 0)
        self.assertGreater(orders.bytes, 0)
        self.assertEqual(orders.row_format, "DYNAMIC")
        self.assertFalse(orders.fulltext)
        self.assertTrue(context.stats("it_impact_texts").fulltext)
        self.assertEqual(context.stats("it_impact_notes").row_format, "COMPACT")
        ((database,),) = self.fetch("SELECT DATABASE()")
        self.assertEqual(context.stats(f"{database}.it_impact_orders"), orders)
        self.assertEqual(context.column_type("it_impact_orders", "note"), "varchar(20)")

    def test_reads_the_instant_row_versions(self):
        self.orders()
        before = self.context()
        if "row_versions" not in before.read:
            self.skipTest("the server counts no instant row versions")
        self.assertEqual(before.stats("it_impact_orders").row_versions, 0)
        self.execute(
            "ALTER TABLE it_impact_orders ADD COLUMN extra int, ALGORITHM=INSTANT"
        )
        after = self.context()
        self.assertEqual(after.stats("it_impact_orders").row_versions, 1)

    def test_impact_reports_the_live_size(self):
        self.orders()
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
        self.assertEqual(report.profile, self.PROFILE)
        (table,) = report.statements[0].tables
        self.assertGreater(table.rows, 0)
        self.assertEqual(table.work, Work.INDEX_BUILD)
        expected = (
            "NOCOPY, LOCK=NONE" if self.PROFILE == "mariadb" else "INPLACE, LOCK=NONE"
        )
        self.assertEqual(table.lock, expected)
        self.assertEqual(
            [f.rule for f in report.findings if f.rule.endswith(".lock_timeout")],
            [f"{self.PROFILE}.lock_timeout"],
        )

    # Each statement changes a foreign key between it_impact_child and
    # it_impact_parent, with the setup that makes it valid.
    PARENT_CASES = (
        (
            "ALTER TABLE it_impact_child ADD CONSTRAINT it_impact_fk2 "
            "FOREIGN KEY (parent_id) REFERENCES it_impact_parent (id)",
            False,
        ),
        ("ALTER TABLE it_impact_child DROP FOREIGN KEY it_impact_fk", True),
        (
            "CREATE TABLE it_impact_notes (id int, parent_id int, "
            "FOREIGN KEY (parent_id) REFERENCES it_impact_parent (id))",
            False,
        ),
        ("DROP TABLE it_impact_child", True),
    )

    def test_a_foreign_key_statement_locks_the_parent_as_predicted(self):
        for statement, keyed in self.PARENT_CASES:
            with self.subTest(statement=statement):
                self.drop()
                self.execute(
                    "CREATE TABLE it_impact_parent (id int PRIMARY KEY)",
                    "INSERT INTO it_impact_parent VALUES (1)",
                    "CREATE TABLE it_impact_child (id int PRIMARY KEY, parent_id int"
                    + (
                        ", CONSTRAINT it_impact_fk FOREIGN KEY (parent_id) "
                        "REFERENCES it_impact_parent (id))"
                        if keyed
                        else ")"
                    ),
                )
                (predicted,) = analyze(
                    [statement], self.DIALECT, self.context()
                ).statements
                locks_parent = any(
                    t.table == "it_impact_parent" and t.lock == mysql.MDL_EXCLUSIVE
                    for t in predicted.tables
                )
                self.assertEqual(
                    locks_parent, self.waits_behind_a_parent_read(statement)
                )

    def waits_behind_a_parent_read(self, statement):
        """
        Whether the statement waits for a lock on it_impact_parent while
        a second session has read it in a transaction still open.
        """
        reader = harness.connect_scratch(self.NAME)
        try:
            reader.cursor().execute("SELECT * FROM it_impact_parent")
            self.execute("SET SESSION lock_wait_timeout = 1")
            try:
                self.execute(statement)
            except Exception as error:
                if error.args and error.args[0] == LOCK_WAIT_TIMEOUT:
                    return True
                raise
            return False
        finally:
            reader.rollback()
            reader.close()
            self.execute("SET SESSION lock_wait_timeout = DEFAULT")
