"""
Tests for observed impact on MySQL and MariaDB: the clauses a probe
tries, the errors that refuse one, the comparison of a prediction
against the clause the server accepted, and the Tracer that runs them.
"""

import sqlite3
import unittest
from unittest import mock

from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.impact import Blocks, Evidence, Hold, Work, analyze
from sustained.impact.context import assumed
from sustained.impact.rules import Probe, mysql
from sustained.impact.rules.mysql.locks import Online
from sustained.impact.rules.mysql.trace import (
    attempts,
    candidates,
    observe,
    refused,
    tables_plan,
    with_observations,
)
from sustained.migrations import Migrator
from sustained.migrations.core.requests import Execute
from sustained.migrations.core.tracing import Tracer
from tests.test_impact_context import drive

MY = Dialects.MYSQL
EXISTING = frozenset({"orders", "shop.orders", "parents", "shop.parents"})


class Refusal(Exception):
    """An error as PyMySQL raises it: the code, then the message."""


def predicted(sql, profile="mysql"):
    (statement,) = analyze([sql], MY, assumed(profile)).statements
    return statement


class TablesPlanTestCase(unittest.TestCase):
    def test_reads_each_table_by_both_names(self):
        rows = [("shop", "Orders", 1), ("other", "parts", 0)]
        _, names = drive(tables_plan(), [rows])
        self.assertEqual(names, {"shop.orders", "orders", "other.parts"})

    def test_a_failed_read_is_none(self):
        _, names = drive(tables_plan(), [RuntimeError("denied")])
        self.assertIsNone(names)


class AttemptsTestCase(unittest.TestCase):
    def test_mysql_tries_no_nocopy(self):
        labels = [online.label for online in candidates(False)]
        self.assertEqual(
            labels,
            [
                "INSTANT",
                "INPLACE, LOCK=NONE",
                "INPLACE, LOCK=SHARED",
                "INPLACE, LOCK=EXCLUSIVE",
                "COPY, LOCK=NONE",
                "COPY, LOCK=SHARED",
                "COPY, LOCK=EXCLUSIVE",
            ],
        )

    def test_mariadb_tries_nocopy_after_instant(self):
        labels = [online.label for online in candidates(True)]
        self.assertEqual(
            labels[:3], ["INSTANT", "NOCOPY, LOCK=NONE", "NOCOPY, LOCK=SHARED"]
        )
        self.assertEqual(len(labels), 10)

    def test_an_alter_table_takes_each_clause_after_a_comma(self):
        found = attempts(predicted("ALTER TABLE orders ADD COLUMN d int;"), mysql.MYSQL)
        self.assertEqual(
            found[:2],
            [
                (
                    "ALTER TABLE orders ADD COLUMN d int, ALGORITHM=INSTANT",
                    Online("INSTANT"),
                ),
                (
                    "ALTER TABLE orders ADD COLUMN d int, ALGORITHM=INPLACE, LOCK=NONE",
                    Online("INPLACE", "NONE"),
                ),
            ],
        )

    def test_a_create_index_takes_each_clause_without_a_comma(self):
        found = attempts(predicted("CREATE INDEX ix ON orders (c)"), mysql.MYSQL)
        self.assertEqual(
            found[1][0], "CREATE INDEX ix ON orders (c) ALGORITHM=INPLACE LOCK=NONE"
        )

    def test_a_mariadb_drop_index_becomes_an_alter_table(self):
        found = attempts(predicted("DROP INDEX ix ON orders", "mariadb"), mysql.MARIADB)
        self.assertEqual(
            found[0][0], "ALTER TABLE orders DROP INDEX ix, ALGORITHM=INSTANT"
        )

    def test_a_statement_that_spells_its_clause_runs_as_written(self):
        for sql in (
            "ALTER TABLE orders ADD COLUMN d int, ALGORITHM=INPLACE",
            "ALTER TABLE orders ADD COLUMN d int, LOCK=NONE",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(attempts(predicted(sql), mysql.MYSQL), [])

    def test_other_statements_run_as_written(self):
        for sql in ("UPDATE orders SET c = 1", "DROP TABLE orders", "SELECT 1"):
            with self.subTest(sql=sql):
                self.assertEqual(attempts(predicted(sql), mysql.MYSQL), [])


class RefusedTestCase(unittest.TestCase):
    def test_a_refusal_code_gives_the_servers_reason(self):
        for code in (1845, 1846, 4092):
            with self.subTest(code=code):
                self.assertEqual(
                    refused(Refusal(code, "Reason: FULLTEXT")), "Reason: FULLTEXT"
                )

    def test_errno_and_msg_are_read_where_a_driver_names_them(self):
        error = Exception("1846 (HY000): no")
        error.errno = 1846
        error.msg = "no"
        self.assertEqual(refused(error), "no")

    def test_a_code_without_a_message_gives_the_whole_error(self):
        self.assertEqual(refused(Refusal(1845)), "1845")

    def test_any_other_error_is_no_refusal(self):
        self.assertIsNone(refused(Refusal(1060, "Duplicate column name 'c'")))
        self.assertIsNone(refused(RuntimeError()))


class ObserveTestCase(unittest.TestCase):
    def test_a_confirmed_prediction_changes_only_the_evidence(self):
        statement = predicted("ALTER TABLE orders ADD COLUMN d int")
        observed = observe(statement, Probe(Online("INSTANT")), EXISTING, mysql.MYSQL)
        self.assertIs(observed.evidence, Evidence.OBSERVED)
        self.assertEqual(observed.tables, statement.tables)
        self.assertEqual(observed.findings, statement.findings)

    def test_another_clause_is_a_mismatch_that_quotes_the_refusal(self):
        statement = predicted("ALTER TABLE orders ADD COLUMN d int")
        probe = Probe(
            Online("INPLACE", "NONE"),
            (
                (
                    Online("INSTANT"),
                    "Reason: InnoDB presently supports one FULLTEXT index",
                ),
            ),
        )
        observed = observe(statement, probe, EXISTING, mysql.MYSQL)
        (table,) = observed.tables
        self.assertEqual(table.lock, "INPLACE, LOCK=NONE")
        self.assertIs(table.blocks, Blocks.DDL)
        (mismatch,) = [f for f in observed.findings if f.rule == "impact.mismatch"]
        self.assertEqual(
            mismatch.message,
            "the rules predicted INSTANT on orders, and the server ran it with "
            "INPLACE, LOCK=NONE; it refused INSTANT: Reason: InnoDB presently "
            "supports one FULLTEXT index",
        )

    def test_a_copy_proves_a_rewrite(self):
        statement = predicted("ALTER TABLE orders ADD COLUMN d int")
        observed = observe(
            statement, Probe(Online("COPY", "SHARED")), EXISTING, mysql.MYSQL
        )
        (table,) = observed.tables
        self.assertIs(table.work, Work.REWRITE)
        self.assertIs(table.hold, Hold.STATEMENT)
        messages = [f.message for f in observed.findings if f.rule == "impact.mismatch"]
        self.assertIn(
            "the rules predicted catalog on orders, and the server ran it with "
            "COPY, LOCK=SHARED, which copies the table",
            messages,
        )

    def test_instant_proves_no_copy(self):
        statement = predicted("ALTER TABLE orders MODIFY COLUMN c bigint")
        self.assertIs(statement.tables[0].work, Work.REWRITE)
        observed = observe(statement, Probe(Online("INSTANT")), EXISTING, mysql.MYSQL)
        self.assertIs(observed.tables[0].work, Work.CATALOG)
        self.assertIs(observed.tables[0].hold, Hold.BRIEF)

    def test_inplace_leaves_the_predicted_work(self):
        statement = predicted("CREATE INDEX ix ON orders (c)")
        observed = observe(
            statement, Probe(Online("INPLACE", "NONE")), EXISTING, mysql.MYSQL
        )
        self.assertEqual(observed.tables, statement.tables)
        self.assertNotIn("impact.mismatch", [f.rule for f in observed.findings])

    def test_the_parent_of_a_foreign_key_is_left_as_predicted(self):
        statement = predicted(
            "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (p) REFERENCES parents (id)"
        )
        parent = next(t for t in statement.tables if t.table == "parents")
        observed = observe(
            statement, Probe(Online("COPY", "SHARED")), EXISTING, mysql.MYSQL
        )
        self.assertIn(parent, observed.tables)

    def test_a_table_the_run_created_is_left_as_predicted(self):
        statement = predicted("ALTER TABLE fresh ADD COLUMN d int")
        observed = observe(
            statement, Probe(Online("COPY", "SHARED")), EXISTING, mysql.MYSQL
        )
        self.assertEqual(observed.tables, statement.tables)

    def test_without_the_existing_tables_every_named_table_is_compared(self):
        statement = predicted("ALTER TABLE fresh ADD COLUMN d int")
        observed = observe(
            statement, Probe(Online("COPY", "SHARED")), None, mysql.MYSQL
        )
        self.assertEqual(observed.tables[0].lock, "COPY, LOCK=SHARED")

    def test_a_schema_qualified_name_is_matched(self):
        statement = predicted("ALTER TABLE `shop`.`orders` ADD COLUMN d int")
        observed = observe(
            statement, Probe(Online("COPY", "SHARED")), EXISTING, mysql.MYSQL
        )
        self.assertEqual(observed.tables[0].lock, "COPY, LOCK=SHARED")

    def test_a_statement_without_a_table_is_unchanged(self):
        statement = predicted("SELECT 1")
        self.assertIs(
            observe(statement, Probe(Online("INSTANT")), EXISTING, mysql.MYSQL),
            statement,
        )


class WithObservationsTestCase(unittest.TestCase):
    def test_each_probed_statement_is_observed(self):
        statements = [
            MigrationStatement("ALTER TABLE orders ADD COLUMN d int", "001", True),
            MigrationStatement("UPDATE orders SET d = 1", "001", True),
        ]
        report = analyze(statements, MY, assumed("mysql"))
        observed = with_observations(
            report, {("001", 0): Probe(Online("INSTANT"))}, EXISTING, mysql.MYSQL
        )
        self.assertIs(observed.evidence, Evidence.OBSERVED)
        first, second = observed.migrations[0].statements
        self.assertIs(first.evidence, Evidence.OBSERVED)
        self.assertIsNot(second.evidence, Evidence.OBSERVED)


class TracerTestCase(unittest.TestCase):
    """The Tracer's probe, driven by hand with the MySQL profile standing in."""

    def tracer(self, profile=mysql.MYSQL):
        patcher = mock.patch(
            "sustained.impact.rules._profiles",
            return_value={"DEFAULT": (profile,)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        migrator = Migrator(sqlite3.connect(":memory:"), [], dialect=Dialects.DEFAULT)
        tracer = Tracer(migrator)
        tracer.context = assumed(profile.name)
        return tracer

    def run_probe(self, tracer, sql, answers):
        """
        Drives the Tracer's observation of one statement. Each answer is
        None for a statement the server runs or an exception it raises.
        Returns the statements run and what was observed.
        """
        ran = []
        answers = list(answers)
        core = tracer.observe(MigrationStatement(sql, "001"))
        try:
            request = next(core)
            while True:
                self.assertIsInstance(request, Execute)
                ran.append(request.sql)
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    request = core.throw(answer)
                else:
                    request = core.send(answer)
        except StopIteration as stop:
            return ran, stop.value

    def test_the_first_accepted_clause_is_the_observation(self):
        tracer = self.tracer()
        refusal = Refusal(1846, "Reason: Cannot change column type INPLACE")
        ran, probe = self.run_probe(
            tracer,
            "ALTER TABLE orders MODIFY c bigint",
            [
                Refusal(1845, "no"),
                refusal,
                refusal,
                refusal,
                Refusal(1846, "COPY"),
                None,
            ],
        )
        self.assertEqual(len(ran), 6)
        self.assertEqual(
            ran[-1], "ALTER TABLE orders MODIFY c bigint, ALGORITHM=COPY, LOCK=SHARED"
        )
        self.assertEqual(probe.accepted, Online("COPY", "SHARED"))
        self.assertEqual(
            [online.label for online, _ in probe.refused],
            [
                "INSTANT",
                "INPLACE, LOCK=NONE",
                "INPLACE, LOCK=SHARED",
                "INPLACE, LOCK=EXCLUSIVE",
                "COPY, LOCK=NONE",
            ],
        )

    def test_another_error_runs_the_statement_as_written(self):
        tracer = self.tracer()
        duplicate = Refusal(1060, "Duplicate column name 'c'")
        with self.assertRaises(Refusal) as caught:
            self.run_probe(
                tracer, "ALTER TABLE orders ADD COLUMN c int", [duplicate, duplicate]
            )
        self.assertIs(caught.exception, duplicate)

    def test_an_error_after_a_refusal_runs_the_statement_as_written(self):
        tracer = self.tracer()
        ran, probe = self.run_probe(
            tracer,
            "ALTER TABLE orders ADD COLUMN d int",
            [Refusal(1846, "no"), Refusal(1064, "syntax"), None],
        )
        self.assertEqual(ran[-1], "ALTER TABLE orders ADD COLUMN d int")
        self.assertIsNone(probe)

    def test_a_statement_with_no_attempts_runs_as_written(self):
        tracer = self.tracer()
        ran, probe = self.run_probe(tracer, "UPDATE orders SET c = 1", [None])
        self.assertEqual(ran, ["UPDATE orders SET c = 1"])
        self.assertIsNone(probe)

    def test_the_profile_follows_the_server_facts(self):
        tracer = self.tracer(mysql.MARIADB)
        _, probe = self.run_probe(
            tracer, "CREATE INDEX ix ON orders (c)", [Refusal(1846, "no"), None]
        )
        self.assertEqual(probe.accepted, Online("NOCOPY", "NONE"))


if __name__ == "__main__":
    unittest.main()
