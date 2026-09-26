"""
Tests for the MySQL and MariaDB rules for table statements, the
timeout finding, and the report.
"""

import unittest

from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.guards import lock_timeout_required
from sustained.impact import (
    Blocks,
    Confidence,
    Work,
    analyze,
)
from sustained.impact.report import render
from tests.test_impact_mysql import (
    MARIADB,
    MY,
    MYSQL,
    context,
    impact,
    rules,
    table,
)


class TableTestCase(unittest.TestCase):
    def test_drop_and_truncate_take_the_metadata_lock(self):
        for sql in ("DROP TABLE p", "TRUNCATE TABLE p"):
            with self.subTest(sql=sql):
                found = table(sql, "p")
                self.assertEqual(
                    (found.lock, found.blocks),
                    ("MDL EXCLUSIVE", Blocks.READS_AND_WRITES),
                )

    def test_dropping_a_child_table_locks_its_parent_on_mysql(self):
        self.assertEqual([t.table for t in impact("DROP TABLE t").tables], ["t", "r"])
        self.assertEqual(
            [t.table for t in impact("DROP TABLE t", MARIADB).tables], ["t"]
        )

    def test_creating_a_child_table_locks_its_parent_on_mysql(self):
        sql = "CREATE TABLE n (id int, r_id int, FOREIGN KEY (r_id) REFERENCES r (id))"
        self.assertEqual([t.table for t in impact(sql).tables], ["r"])
        self.assertEqual(impact(sql, MARIADB).tables, ())

    def test_rename_table(self):
        statement = impact("RENAME TABLE t TO u, p TO q")
        self.assertEqual([t.lock for t in statement.tables], ["MDL EXCLUSIVE"] * 2)
        self.assertEqual(
            len([f for f in statement.findings if f.rule == "mysql.rename"]), 2
        )
        self.assertEqual(table("ALTER TABLE t RENAME TO u").lock, "INSTANT")

    def test_table_rebuilds_and_copies(self):
        cases = [
            (
                "ALTER TABLE t ENGINE=InnoDB",
                "INPLACE, LOCK=NONE",
                "mysql.table_rebuild",
            ),
            ("ALTER TABLE t ENGINE=MyISAM", "COPY, LOCK=SHARED", "mysql.table_copy"),
            ("ALTER TABLE t FORCE", "INPLACE, LOCK=NONE", "mysql.table_rebuild"),
            (
                "ALTER TABLE t ROW_FORMAT=COMPACT",
                "INPLACE, LOCK=NONE",
                "mysql.table_rebuild",
            ),
            (
                "ALTER TABLE t CONVERT TO CHARACTER SET latin1",
                "COPY, LOCK=SHARED",
                "mysql.table_copy",
            ),
            (
                "ALTER TABLE t COMMENT = 'note'",
                "INPLACE, LOCK=NONE",
                "mysql.table_option",
            ),
            ("OPTIMIZE TABLE t", "INPLACE, LOCK=NONE", "mysql.table_rebuild"),
        ]
        for sql, lock, rule in cases:
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual((found.lock, found.rule), (lock, rule))
        self.assertEqual(
            table("ALTER TABLE t COMMENT = 'note'", ctx=MARIADB).lock, "INSTANT"
        )
        self.assertEqual(table("OPTIMIZE TABLE ft", "ft").lock, "COPY, LOCK=SHARED")

    def test_a_trigger_takes_the_metadata_lock(self):
        sql = "CREATE TRIGGER tr BEFORE UPDATE ON t FOR EACH ROW SET NEW.c = NEW.c"
        self.assertEqual(table(sql).lock, "MDL EXCLUSIVE")
        dropped = impact("DROP TRIGGER tr")
        self.assertEqual((dropped.tables, dropped.confidence), ((), Confidence.LIKELY))

    def test_writes_hold_row_locks_and_draw_no_timeout_finding(self):
        statement = impact("UPDATE t SET name = 'x' WHERE id < 3")
        found = statement.tables[0]
        self.assertEqual(
            (found.lock, found.blocks, found.work), ("IX", Blocks.WRITES, Work.ROWS)
        )
        self.assertNotIn("mysql.lock_timeout", rules(statement))
        self.assertEqual(table("INSERT INTO t (id) VALUES (1)").blocks, Blocks.DDL)

    def test_an_unknown_action_or_statement(self):
        statement = impact("ALTER TABLE t VALIDATE CONSTRAINT ck")
        self.assertIs(statement.confidence, Confidence.UNKNOWN)
        self.assertIn(
            "no MySQL rule reads the ALTER TABLE action", statement.findings[0].message
        )
        statement = impact("VACUUM t", MARIADB)
        self.assertIn(
            "no MariaDB rule reads a vacuum statement", statement.findings[0].message
        )

    def test_a_table_the_run_created_draws_nothing(self):
        report = analyze(
            ["CREATE TABLE n (id int)", "ALTER TABLE n ADD COLUMN d int"], MY, MYSQL
        )
        second = report.statements[1]
        self.assertEqual(second.tables[0].blocks, Blocks.NOTHING)
        self.assertEqual(second.findings, ())


class TimeoutTestCase(unittest.TestCase):
    ALTER = "ALTER TABLE t ADD COLUMN d int"

    def timeout_findings(self, statements, ctx=MYSQL):
        report = analyze(statements, MY, ctx)
        return [
            s.statement
            for s in report.statements
            if "mysql.lock_timeout" in rules(s) or "mariadb.lock_timeout" in rules(s)
        ]

    def test_every_alter_draws_the_finding(self):
        statement = impact("ALTER TABLE t ADD INDEX ix2 (name)")
        (found,) = [f for f in statement.findings if f.rule == "mysql.lock_timeout"]
        self.assertEqual(found.remedy, ("SET SESSION lock_wait_timeout = 5",))
        self.assertIn("lock_wait_timeout", found.message)

    def test_a_session_timeout_covers_the_rest_of_the_run(self):
        for setting in (
            "SET SESSION lock_wait_timeout = 5",
            "SET lock_wait_timeout = 5",
            "SET LOCAL lock_wait_timeout = 5",
            "SET @@SESSION.lock_wait_timeout = 5",
        ):
            with self.subTest(setting=setting):
                first = MigrationStatement(setting, "001")
                later = MigrationStatement(self.ALTER, "002")
                self.assertEqual(self.timeout_findings([first, later]), [])

    def test_a_global_timeout_covers_nothing(self):
        found = self.timeout_findings(["SET GLOBAL lock_wait_timeout = 5", self.ALTER])
        self.assertEqual(found, [self.ALTER])

    def test_a_default_timeout_on_the_connection_covers_nothing(self):
        self.assertEqual(self.timeout_findings([self.ALTER]), [self.ALTER])
        self.assertEqual(self.timeout_findings([self.ALTER], MARIADB), [self.ALTER])
        short = context(lock_wait_timeout="10")
        self.assertEqual(self.timeout_findings([self.ALTER], short), [])

    def test_the_guard_blocks_the_mysql_form(self):
        guard = lock_timeout_required()
        (verdict,) = guard([self.ALTER], MY)
        self.assertEqual(verdict.rule, "lock_timeout_required")
        self.assertEqual(
            guard(["SET SESSION lock_wait_timeout = 5", self.ALTER], MY), []
        )


class ReportTestCase(unittest.TestCase):
    def test_statements_are_windows_of_their_own(self):
        report = analyze(
            [
                MigrationStatement("ALTER TABLE t ADD COLUMN d int", "001"),
                MigrationStatement("UPDATE t SET d = 1", "001"),
            ],
            MY,
            MYSQL,
        )
        (migration,) = report.migrations
        self.assertTrue(migration.transactional)
        self.assertFalse(migration.held_to_commit)
        self.assertNotIn("window", render(report))
        self.assertNotIn("window.held", [f.rule for f in migration.findings])

    def test_a_static_report_says_which_profile_it_assumed(self):
        report = analyze(["ALTER TABLE t ADD COLUMN d int"], MY)
        self.assertEqual(report.profile, "mysql")
        self.assertEqual(report.version, (8, 0, 19))
        (note,) = report.migrations[0].findings
        self.assertEqual(note.rule, "impact.assumed_profile")
        self.assertIn("MySQL 8.0.19", note.message)
        self.assertIn("MariaDB", note.message)
        self.assertTrue(
            render(report).endswith("Evidence: static (assumed MySQL 8.0.19)")
        )
        # A static ADD COLUMN depends on storage facts it did not read.
        self.assertIs(report.statements[0].confidence, Confidence.LIKELY)
        self.assertIn("did not read", report.statements[0].findings[0].message)

    def test_a_mariadb_context_picks_the_mariadb_rules(self):
        report = analyze(["ALTER TABLE t ADD COLUMN d int"], MY, MARIADB)
        self.assertEqual(report.profile, "mariadb")
        self.assertEqual(report.migrations[0].findings, ())
        self.assertTrue(render(report).endswith("Evidence: catalog (MariaDB 11.4.13)"))
        self.assertIn("[mariadb.add_column.instant]", render(report))

    def test_a_postgres_report_has_no_profile_note(self):
        report = analyze(["CREATE INDEX ix ON t (c)"], Dialects.POSTGRES)
        self.assertEqual(report.migrations[0].findings, ())


if __name__ == "__main__":
    unittest.main()
