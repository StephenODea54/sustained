"""
The impact analysis on MySQL and MariaDB, run against every server whose
support.json row claims the `impact` cover and runs InnoDB: the server
facts read_context() reads, Migrator.impact() on a live connection, the
parent-table metadata locks the rules predict for foreign keys, checked
by holding a read on the parent in a second session,
rehearse(scratch=True, trace=True), the clauses assert_algorithm writes
on the migration the models generate, and the ground truth for every rule:
each rule fixture runs under the algorithm probe, and the clause the
server accepts must match what the rule predicted for that server's
version and settings.

The tests run in the scratch database, since MySQL schema changes do not
roll back. The rule fixtures run in a database of their own, created
again for each fixture.
"""

import unittest

from sustained.dialects import Dialects
from sustained.impact import Evidence, Work, analyze, read_context
from sustained.impact.rules import Probe, mysql, profile_for
from sustained.impact.rules.mysql.trace import attempts, observe, refused, tables_plan
from sustained.introspect.runner import run_plan
from sustained.migrations import Migration, Migrator
from sustained.model import Model
from sustained.schema import Index, Integer, String

from . import harness

TABLES = (
    "sustained_rehearsals",
    "it_impact_child",
    "it_impact_orders",
    "it_impact_notes",
    "it_impact_texts",
    "it_impact_parent",
    "it_impact_migrations",
)

# MySQL's error for a lock wait that ran out of lock_wait_timeout.
LOCK_WAIT_TIMEOUT = 1205

# The database the rule fixtures run in.
FIXTURE_DATABASE = "it_impact_fixtures"


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
        cursor = cls.connection.cursor()
        cursor.execute("SELECT DATABASE()")
        ((cls.database,),) = cursor.fetchall()

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.cursor().execute(f"DROP DATABASE IF EXISTS {FIXTURE_DATABASE}")
            connection.close()

    def setUp(self):
        self.drop()

    def tearDown(self):
        self.drop()

    def drop(self):
        self.connection.rollback()
        self.execute(f"USE `{self.database}`")
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

    def test_a_traced_rehearsal_probes_each_statement(self):
        self.orders()
        self.execute(
            "CREATE TABLE it_impact_texts (id int PRIMARY KEY, body text, "
            "FULLTEXT KEY it_impact_body (body))",
        )
        migrator = Migrator(
            self.connection,
            [
                Migration(
                    "001_traced",
                    up=[
                        "ALTER TABLE it_impact_orders ADD COLUMN extra int",
                        "ALTER TABLE it_impact_texts ADD COLUMN extra int",
                        "CREATE INDEX it_impact_note_ix ON it_impact_orders (note)",
                        "UPDATE it_impact_orders SET extra = 1",
                    ],
                    down=[
                        "DROP INDEX it_impact_note_ix ON it_impact_orders",
                        "ALTER TABLE it_impact_texts DROP COLUMN extra",
                        "ALTER TABLE it_impact_orders DROP COLUMN extra",
                    ],
                )
            ],
            dialect=self.DIALECT,
            table="it_impact_migrations",
        )
        results = migrator.rehearse(scratch=True, trace=True)
        self.assertTrue(results.ok, results)
        report = results.impact
        self.assertIs(report.evidence, Evidence.OBSERVED)
        added, texts, index, update = report.statements
        self.assertIs(added.evidence, Evidence.OBSERVED)
        self.assertEqual(added.tables[0].lock, "INSTANT")
        # InnoDB changes a table with a FULLTEXT index in place at best.
        self.assertNotEqual(texts.tables[0].lock, "INSTANT")
        expected = (
            "NOCOPY, LOCK=NONE" if self.PROFILE == "mariadb" else "INPLACE, LOCK=NONE"
        )
        self.assertEqual(index.tables[0].lock, expected)
        self.assertIsNot(update.evidence, Evidence.OBSERVED)
        self.assertNotIn("impact.mismatch", [f.rule for f in report.findings])
        # The down sweep put the table back.
        columns = self.fetch(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'it_impact_orders'"
        )
        self.assertEqual(sorted(c for (c,) in columns), ["id", "note"])

    def test_assert_algorithm_writes_the_clause_the_server_accepts(self):
        self.orders()
        orders = type(
            "Orders",
            (Model,),
            {
                "tableName": "it_impact_orders",
                "tableColumns": {
                    "id": Integer(primary_key=True),
                    "note": String(20),
                    "extra": Integer(),
                },
                "indexes": [Index("it_impact_note_ix", "note")],
                "_dialect": self.DIALECT,
            },
        )
        notes = type(
            "Notes",
            (Model,),
            {
                "tableName": "it_impact_notes",
                "tableColumns": {"id": Integer(primary_key=True), "body": String(20)},
                "indexes": [Index("it_impact_body_ix", "body")],
                "_dialect": self.DIALECT,
            },
        )
        migrator = Migrator(
            self.connection, [], dialect=self.DIALECT, table="it_impact_migrations"
        )
        models = [orders, notes]
        plain = migrator.plan(models)
        planned = migrator.plan(models, assert_algorithm=True)
        self.connection.rollback()
        index = (
            "ALGORITHM=NOCOPY LOCK=NONE"
            if self.PROFILE == "mariadb"
            else "ALGORITHM=INPLACE LOCK=NONE"
        )
        changed = [
            (before, after)
            for before, after in zip(plain.up, planned.up)
            if before != after
        ]
        self.assertEqual(len(changed), 2, planned.up)
        (added,) = [a for b, a in changed if "ADD COLUMN" in b]
        self.assertTrue(added.endswith(", ALGORITHM=INSTANT"), added)
        (built,) = [a for b, a in changed if "CREATE INDEX" in b]
        self.assertTrue(built.endswith(index), built)
        # The table the migration creates, and its index, are left.
        self.assertFalse(
            [s for s in planned.up if "it_impact_notes" in s and "ALGORITHM" in s]
        )
        applied = migrator.up(models=models, assert_algorithm=True)
        self.assertEqual(len(applied), 1)
        self.assertIsNone(migrator.plan(models))
        self.connection.rollback()

    def test_each_rule_fixture_does_what_its_rule_predicts(self):
        profile = profile_for(self.DIALECT, self.PROFILE)
        self.fixture_schema(profile)
        context = self.context()
        fixtures = [(rule, f) for rule in profile.rules for f in rule.fixtures]
        for rule, fixture in fixtures:
            with self.subTest(rule=rule.id, fixture=fixture):
                self.fixture_schema(profile)
                statement = self.observe_fixture(fixture, context, profile)
                reached = {t.rule for t in statement.tables}
                reached |= {f.rule for f in statement.findings}
                self.assertIn(rule.id, reached)
                mismatches = [
                    f.message for f in statement.findings if f.rule == "impact.mismatch"
                ]
                self.assertEqual(mismatches, [])

    def fixture_schema(self, profile):
        """Creates the fixture database again, with the fixtures' objects."""
        self.execute(
            f"DROP DATABASE IF EXISTS {FIXTURE_DATABASE}",
            f"CREATE DATABASE {FIXTURE_DATABASE}",
            f"USE {FIXTURE_DATABASE}",
            *profile.fixture_schema,
        )

    def observe_fixture(self, fixture, context, profile):
        """
        The fixture's predicted impact with the clause the server
        accepted in its place. A fixture the probe does not try runs as
        written: one the rules say the server refuses must fail with a
        refusal, and any other must run.
        """
        (predicted,) = analyze([fixture], self.DIALECT, context).statements
        existing = run_plan(self.connection, self.DIALECT, tables_plan())
        tried = attempts(predicted, profile)
        if not tried:
            refusal = f"{profile.prefix}.refused" in {
                f.rule for f in predicted.findings
            }
            try:
                self.execute(fixture)
            except Exception as error:
                if not refusal or refused(error) is None:
                    raise
            else:
                self.assertFalse(
                    refusal, "the server ran a statement predicted refused"
                )
            return predicted
        refusals = []
        for sql, clause in tried:
            try:
                self.execute(sql)
            except Exception as error:
                reason = refused(error)
                if reason is None:
                    raise
                refusals.append((clause, reason))
            else:
                probe = Probe(clause, tuple(refusals))
                observed = observe(predicted, probe, existing, profile)
                self.assertIs(observed.evidence, Evidence.OBSERVED)
                return observed
        self.fail(f"the server refused every clause: {refusals}")
