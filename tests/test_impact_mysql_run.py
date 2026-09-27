"""
Tests for the MySQL and MariaDB rules that read earlier statements of a
run: the storage facts in the run state, and the row locks of DML in a
transactional migration. Also the lock an explicit ALGORITHM=COPY takes.
"""

import unittest

from sustained.analysis import MigrationStatement
from sustained.impact import Blocks, Confidence, Work, analyze
from sustained.impact.model import Hold
from sustained.impact.rules import profile_for
from sustained.impact.rules.common import with_observations
from sustained.impact.state import RunState
from tests.test_impact_mysql import _STATS, MARIADB, MY, MYSQL, context


def run(statements, ctx=MYSQL):
    """The first table of each statement's impact, in run order."""
    report = analyze(statements, MY, ctx)
    return [s.tables[0] if s.tables else None for s in report.statements]


def with_versions(ctx, used):
    """The context with table t's instant row versions at `used`."""
    tables = dict(ctx.tables, t=_STATS["t"]._replace(row_versions=used))
    return ctx._replace(tables=tables)


class RunStorageTestCase(unittest.TestCase):
    def test_a_fulltext_index_stops_a_later_instant_column(self):
        for first in (
            "ALTER TABLE t ADD FULLTEXT INDEX fx (name)",
            "CREATE FULLTEXT INDEX fx ON t (name)",
        ):
            with self.subTest(first=first):
                _, added = run([first, "ALTER TABLE t ADD COLUMN d int"])
                self.assertEqual(
                    (added.lock, added.rule),
                    ("COPY, LOCK=SHARED", "mysql.add_column.copy"),
                )
        _, added = run(
            [
                "ALTER TABLE t ADD FULLTEXT INDEX fx (name)",
                "ALTER TABLE t ADD COLUMN d int",
            ],
            MARIADB,
        )
        self.assertEqual(added.lock, "INPLACE, LOCK=SHARED")

    def test_a_fulltext_index_stays_after_it_is_dropped(self):
        # The hidden FTS_DOC_ID column stays with the table.
        *_, added = run(
            [
                "ALTER TABLE t ADD FULLTEXT INDEX fx (name)",
                "DROP INDEX fx ON t",
                "ALTER TABLE t ADD COLUMN d int",
            ]
        )
        self.assertEqual(added.lock, "COPY, LOCK=SHARED")

    def test_a_spatial_index_leaves_instant_columns(self):
        _, added = run(
            ["ALTER TABLE g ADD SPATIAL INDEX sp (p)", "ALTER TABLE g ADD COLUMN d int"]
        )
        self.assertEqual(added.lock, "INSTANT")

    def test_a_compressed_row_format_stops_a_later_instant_column(self):
        rebuilt, added = run(
            ["ALTER TABLE t ROW_FORMAT=COMPRESSED", "ALTER TABLE t ADD COLUMN d int"]
        )
        self.assertEqual(rebuilt.work, Work.REWRITE)
        self.assertEqual((added.lock, added.work), ("INPLACE, LOCK=NONE", Work.REWRITE))
        self.assertEqual(added.rule, "mysql.add_column.rebuild")

    def test_each_instant_column_statement_uses_a_row_version(self):
        ctx = with_versions(MYSQL, 62)
        first, second, third = run(
            [
                "ALTER TABLE t ADD COLUMN d int, ADD COLUMN e int",
                "ALTER TABLE t DROP COLUMN d",
                "ALTER TABLE t ADD COLUMN f int",
            ],
            ctx,
        )
        self.assertEqual((first.lock, second.lock), ("INSTANT", "INSTANT"))
        self.assertEqual(third.lock, "INPLACE, LOCK=NONE")

    def test_a_rebuild_gives_the_row_versions_back(self):
        ctx = with_versions(MYSQL, 63)
        for rebuild in ("ALTER TABLE t FORCE", "OPTIMIZE TABLE t"):
            with self.subTest(rebuild=rebuild):
                first, rebuilt, last = run(
                    [
                        "ALTER TABLE t ADD COLUMN d int",
                        rebuild,
                        "ALTER TABLE t ADD COLUMN e int",
                    ],
                    ctx,
                )
                self.assertEqual(first.lock, "INSTANT")
                self.assertEqual(rebuilt.work, Work.REWRITE)
                self.assertEqual(last.lock, "INSTANT")

    def test_other_instant_changes_use_no_row_version(self):
        ctx = with_versions(MYSQL, 63)
        *_, added = run(
            [
                "ALTER TABLE t RENAME COLUMN name TO label",
                "ALTER TABLE t ALTER COLUMN c SET DEFAULT 1",
                "ALTER TABLE t ADD COLUMN d int",
            ],
            ctx,
        )
        self.assertEqual(added.lock, "INSTANT")

    def test_unread_row_versions_stay_unread(self):
        report = analyze(
            ["ALTER TABLE t ADD COLUMN d int", "ALTER TABLE t ADD COLUMN e int"],
            MY,
            with_versions(MYSQL, None),
        )
        self.assertEqual([s.tables[0].lock for s in report.statements], ["INSTANT"] * 2)
        self.assertEqual(
            [s.confidence for s in report.statements], [Confidence.LIKELY] * 2
        )

    def test_a_refused_statement_changes_nothing(self):
        refused, added = run(
            [
                "ALTER TABLE t ADD FULLTEXT INDEX fx (name), LOCK=NONE",
                "ALTER TABLE t ADD COLUMN d int",
            ]
        )
        self.assertEqual(refused.rule, "mysql.refused")
        self.assertEqual(added.lock, "INSTANT")

    def test_the_facts_follow_a_renamed_table(self):
        *_, added = run(
            [
                "ALTER TABLE t ROW_FORMAT=COMPRESSED",
                "ALTER TABLE t RENAME TO t2",
                "ALTER TABLE t2 ADD COLUMN d int",
            ]
        )
        self.assertEqual(added.lock, "INPLACE, LOCK=NONE")

    def test_the_run_state_records_by_live_table(self):
        state = RunState()
        self.assertEqual(state.stored("t"), {})
        state.record_storage("T", row_versions=3)
        state.record_storage("t", fulltext=True)
        self.assertEqual(state.stored("t"), {"row_versions": 3, "fulltext": True})


class CopyTestCase(unittest.TestCase):
    def test_copy_with_lock_none_is_refused_where_copy_takes_shared(self):
        sql = "ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY, LOCK=NONE"
        for ctx in (MYSQL, context("mariadb", (11, 1, 2))):
            with self.subTest(profile=ctx.profile, version=ctx.version):
                (found,) = run([sql], ctx)
                self.assertTrue(found.rule.endswith(".refused"))
        (found,) = run([sql], MARIADB)
        self.assertEqual(found.lock, "COPY, LOCK=NONE")
        # ALTER IGNORE TABLE copies with LOCK=SHARED on 11.2 and later.
        (found,) = run([sql.replace("ALTER", "ALTER IGNORE", 1)], MARIADB)
        self.assertEqual(found.rule, "mariadb.refused")

    def test_copy_names_the_lock_it_needs(self):
        statement = analyze(
            ["ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY, LOCK=NONE"], MY, MYSQL
        ).statements[0]
        self.assertIn(
            "LOCK=NONE cannot run ALGORITHM=COPY, which needs LOCK=SHARED",
            statement.findings[0].message,
        )

    def test_copy_with_a_shared_lock_runs(self):
        (found,) = run(["ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY, LOCK=SHARED"])
        self.assertEqual(
            (found.lock, found.rule), ("COPY, LOCK=SHARED", "mysql.add_column.copy")
        )
        # A change with no copy rule of its own keeps its rule.
        (found,) = run(["ALTER TABLE t ADD INDEX ix2 (name), ALGORITHM=COPY"])
        self.assertEqual(
            (found.lock, found.work, found.rule),
            ("COPY, LOCK=SHARED", Work.REWRITE, "mysql.add_index"),
        )


class RowScopeTestCase(unittest.TestCase):
    def test_dml_row_locks_last_to_the_next_ddl(self):
        report = analyze(
            [
                "UPDATE t SET c = 1",
                "UPDATE r SET id = id",
                "ALTER TABLE t ADD COLUMN d int",
                "UPDATE t SET c = 2",
            ],
            MY,
            MYSQL,
        )
        (migration,) = report.migrations
        kept = [s.tables[0].hold for s in migration.statements]
        self.assertIs(kept[0], Hold.TRANSACTION)
        self.assertIsNot(kept[1], Hold.TRANSACTION)
        self.assertIsNot(kept[3], Hold.TRANSACTION)
        self.assertFalse(migration.held_to_commit)
        self.assertEqual(
            [(w.table, w.taken_by) for w in migration.windows],
            [("t", 1), ("r", 2), ("t", 3), ("t", 4)],
        )

    def test_a_long_statement_inside_a_dml_run_is_held(self):
        report = analyze(
            ["UPDATE t SET c = 1", "FROB t", "ALTER TABLE t ADD COLUMN d int"],
            MY,
            MYSQL,
        )
        (migration,) = report.migrations
        # A statement the recognizer does not read commits nothing, as
        # far as the windows go.
        window, _ = migration.windows
        self.assertEqual((window.table, window.taken_by), ("t", 1))
        self.assertEqual((window.blocks, window.during), (Blocks.WRITES, 2))
        self.assertIn("window.held", [f.rule for f in migration.findings])

    def test_outside_a_transaction_each_statement_is_a_window(self):
        statements = [
            MigrationStatement(sql, "m", False)
            for sql in ("UPDATE t SET c = 1", "UPDATE r SET id = id", "FROB t")
        ]
        (migration,) = analyze(statements, MY, MYSQL).migrations
        self.assertEqual(
            [s.tables[0].hold for s in migration.statements[:2]],
            [Hold.STATEMENT, Hold.STATEMENT],
        )
        self.assertNotIn("window.held", [f.rule for f in migration.findings])

    def test_the_observed_report_reads_the_same_windows(self):
        report = analyze(
            ["UPDATE t SET c = 1", "FROB t", "ALTER TABLE t ADD COLUMN d int"],
            MY,
            MYSQL,
        )
        observed = with_observations(
            report, {}, profile_for(MY, "mysql"), lambda s, o: s
        )
        self.assertEqual(observed.migrations[0].windows, report.migrations[0].windows)
        self.assertEqual(observed.migrations[0].findings, report.migrations[0].findings)


if __name__ == "__main__":
    unittest.main()
