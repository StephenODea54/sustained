"""
Tests for the SQLite rules, the database-wide window, and the context
read.
"""

import json
import sqlite3
import unittest
from pathlib import Path
from types import MappingProxyType

from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    TableStats,
    Work,
    analyze,
    read_context,
)
from sustained.impact.context import FLOORS, assumed
from sustained.impact.recognizer import recognize
from sustained.impact.report import render
from sustained.impact.rules import profile_for, sqlite
from sustained.impact.rules.sqlite import context_plan, sqlite_version
from sustained.impact.window import DATABASE
from sustained.migrations import Migration, Migrator
from sustained.model import Model
from sustained.schema import Integer, String
from tests.test_impact_context import drive

ROOT = Path(__file__).resolve().parent.parent
SQLITE = Dialects.DEFAULT


def context(journal_mode="delete", **tables):
    """A read SQLite server with the journal mode and table sizes given."""
    stats = {name: TableStats(rows, size) for name, (rows, size) in tables.items()}
    return EngineContext(
        "sqlite",
        (3, 45),
        settings=MappingProxyType({"journal_mode": journal_mode}),
        tables=MappingProxyType(stats),
        read=frozenset({"version", "settings", "sizes"}),
    )


def impact(sql, ctx=None, transactional=True):
    statement = MigrationStatement(sql, "001", transactional)
    (found,) = analyze([statement], SQLITE, ctx).statements
    return found


def table(sql, ctx=None):
    found = impact(sql, ctx)
    assert len(found.tables) == 1, found.tables
    return found.tables[0]


def rules(statement):
    return [f.rule for f in statement.findings]


class StatementTestCase(unittest.TestCase):
    def test_each_statement_takes_the_write_lock(self):
        cases = [
            ("ALTER TABLE t ADD COLUMN d integer", Work.CATALOG, "sqlite.add_column"),
            (
                "ALTER TABLE t ADD COLUMN d integer CHECK (d > 0)",
                Work.SCAN,
                "sqlite.add_column.checked",
            ),
            (
                "ALTER TABLE t ADD COLUMN d integer GENERATED ALWAYS AS (id) "
                "VIRTUAL NOT NULL",
                Work.SCAN,
                "sqlite.add_column.checked",
            ),
            (
                "ALTER TABLE t ADD COLUMN d integer NOT NULL DEFAULT 0",
                Work.CATALOG,
                "sqlite.add_column",
            ),
            ("ALTER TABLE t DROP COLUMN c", Work.REWRITE, "sqlite.drop_column"),
            ("ALTER TABLE t RENAME COLUMN c TO d", Work.CATALOG, "sqlite.rename"),
            ("ALTER TABLE t RENAME TO u", Work.CATALOG, "sqlite.rename"),
            ("CREATE INDEX ix ON t (c)", Work.INDEX_BUILD, "sqlite.create_index"),
            ("REINDEX t", Work.INDEX_BUILD, "sqlite.reindex"),
            ("DROP TABLE t", Work.SCAN, "sqlite.drop_table"),
            ("ANALYZE t", Work.SCAN, "sqlite.analyze"),
            ("UPDATE t SET c = 1", Work.ROWS, "sqlite.write_rows"),
            ("DELETE FROM t", Work.ROWS, "sqlite.write_rows"),
            ("INSERT INTO t (c) VALUES (1)", Work.ROWS, "sqlite.write_rows"),
            (
                "CREATE TRIGGER tr AFTER INSERT ON t BEGIN SELECT 1; END",
                Work.CATALOG,
                "sqlite.schema_change",
            ),
        ]
        for sql, work, rule in cases:
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual(found.table, "t")
                self.assertEqual(found.lock, sqlite.WRITE_LOCK)
                self.assertEqual(found.work, work)
                self.assertEqual(found.rule, rule)

    def test_wal_mode_lets_reads_go_on(self):
        self.assertIs(table("DROP TABLE t", context("wal")).blocks, Blocks.WRITES)
        self.assertIs(
            table("DROP TABLE t", context("delete")).blocks, Blocks.READS_AND_WRITES
        )
        # Unread, the rules assume a rollback journal.
        self.assertIs(table("DROP TABLE t").blocks, Blocks.READS_AND_WRITES)

    def test_the_message_names_what_waits(self):
        (wal,) = impact("DROP TABLE t", context("wal")).findings
        self.assertIn("writes to every table in the database wait", wal.message)
        self.assertNotIn("reads wait", wal.message)
        (delete,) = impact("DROP TABLE t", context("delete")).findings
        self.assertIn("in journal mode delete reads wait", delete.message)
        (unread,) = impact("DROP TABLE t").findings
        self.assertIn("the journal mode was not read", unread.message)
        (bare,) = impact("DROP TABLE t", transactional=False).findings
        self.assertIn("wait until it ends", bare.message)

    def test_a_large_table_is_danger(self):
        ctx = context(t=(5_000_000, 1 << 31))
        (finding,) = impact("ALTER TABLE t DROP COLUMN c", ctx).findings
        self.assertEqual(str(finding.severity), "danger")
        self.assertEqual(table("ALTER TABLE t DROP COLUMN c", ctx).rows, 5_000_000)

    def test_a_rename_warns_running_code(self):
        statement = impact("ALTER TABLE t RENAME COLUMN c TO d")
        self.assertEqual(rules(statement), ["sqlite.rename"])
        self.assertIn("names the column c", statement.findings[0].message)

    def test_vacuum_rewrites_the_database_and_refuses_a_transaction(self):
        ctx = context()._replace(
            tables=MappingProxyType({DATABASE: TableStats(None, 4096)})
        )
        inside = impact("VACUUM", ctx)
        self.assertEqual(inside.tables[0].table, DATABASE)
        self.assertEqual(inside.tables[0].bytes, 4096)
        self.assertEqual(inside.tables[0].work, Work.REWRITE)
        self.assertEqual([str(f.severity) for f in inside.findings], ["danger", "info"])
        outside = impact("VACUUM", ctx, transactional=False)
        self.assertEqual([str(f.severity) for f in outside.findings], ["info"])

    def test_reindex_finds_the_table_of_an_index(self):
        ctx = context()
        run = [
            MigrationStatement("CREATE TABLE t (c integer)", "001"),
            MigrationStatement("CREATE INDEX ix ON t (c)", "001"),
            MigrationStatement("REINDEX ix", "002"),
        ]
        reindex = analyze(run, SQLITE, ctx).statements[-1]
        self.assertEqual(reindex.tables[0].table, "t")
        self.assertIs(reindex.confidence, Confidence.KNOWN)
        # A name the run and the schema read do not know may be a
        # collation.
        self.assertIs(impact("REINDEX nocase").confidence, Confidence.LIKELY)
        whole = impact("REINDEX")
        self.assertEqual(whole.tables[0].table, DATABASE)

    def test_drop_index_finds_its_table(self):
        run = [
            MigrationStatement("CREATE INDEX ix ON t (c)", "001"),
            MigrationStatement("DROP INDEX ix", "002"),
        ]
        dropped = analyze(run, SQLITE).statements[-1]
        self.assertEqual(dropped.tables[0].table, "t")
        unplaced = table("DROP INDEX ix")
        self.assertEqual(unplaced.table, "(table of index ix)")
        self.assertEqual(
            (unplaced.work, unplaced.rule), (Work.SCAN, "sqlite.drop_index")
        )

    def test_a_view_or_setting_changes_nothing_heavy(self):
        self.assertEqual(table("CREATE VIEW v AS SELECT 1").table, DATABASE)
        self.assertEqual(impact("PRAGMA foreign_keys = OFF").tables, ())

    def test_an_unknown_action_is_unknown(self):
        statement = impact("ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0)")
        self.assertIs(statement.confidence, Confidence.UNKNOWN)

    def test_no_lock_timeout_finding(self):
        statement = impact("ALTER TABLE t DROP COLUMN c")
        self.assertNotIn("sqlite.lock_timeout", rules(statement))


class RebuildTestCase(unittest.TestCase):
    def rebuild(self, ctx=None):
        """The statements the diff generates to rebuild a table."""
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, n TEXT)")
        items = type(
            "Items",
            (Model,),
            {
                "tableName": "items",
                "tableColumns": {"id": Integer(primary_key=True), "n": Integer()},
                "_dialect": SQLITE,
            },
        )
        migrator = Migrator(connection, [], dialect=SQLITE)
        generated = migrator.plan([items])
        return analyze(generated.up, SQLITE, ctx)

    def test_the_copy_rewrites_the_table_it_rebuilds(self):
        report = self.rebuild(context(items=(5_000_000, 1 << 31)))
        (copy,) = [s for s in report.statements if s.parsed.kind == "insert"]
        self.assertEqual([t.table for t in copy.tables], ["items"])
        self.assertEqual(copy.tables[0].work, Work.REWRITE)
        self.assertEqual(copy.tables[0].rule, "sqlite.rebuild")
        self.assertEqual([str(f.severity) for f in copy.findings], ["danger"])
        self.assertNotIn(Confidence.UNKNOWN, [s.confidence for s in report.statements])

    def test_the_migration_is_one_database_window(self):
        report = self.rebuild()
        (migration,) = report.migrations
        (window,) = migration.windows
        self.assertEqual(window.table, DATABASE)
        self.assertIs(window.heaviest, Work.REWRITE)
        # The copy itself blocks what the lock held across it blocks.
        self.assertNotIn("window.held", [f.rule for f in migration.findings])
        self.assertIn(
            "window  (database): database write lock from statement 1, held to commit",
            render(report),
        )


class WindowTestCase(unittest.TestCase):
    def test_locks_on_two_tables_are_one_window(self):
        run = [
            MigrationStatement("UPDATE a SET c = 1", "001"),
            MigrationStatement("UPDATE b SET c = 1", "001"),
        ]
        (migration,) = analyze(run, SQLITE).migrations
        self.assertEqual([w.table for w in migration.windows], [DATABASE])
        self.assertEqual([lock.table for lock in migration.locks], ["a", "b"])
        self.assertNotIn("window.lock_order", [f.rule for f in migration.findings])

    def test_outside_a_transaction_each_statement_is_a_window(self):
        run = [
            MigrationStatement("UPDATE a SET c = 1", "001", False),
            MigrationStatement("UPDATE b SET c = 1", "001", False),
        ]
        (migration,) = analyze(run, SQLITE).migrations
        self.assertEqual([w.taken_by for w in migration.windows], [1, 2])
        self.assertFalse(migration.held_to_commit)


class ContextTestCase(unittest.TestCase):
    def test_reads_a_live_database(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, n TEXT);"
            "CREATE INDEX ix ON t (n);"
            "CREATE TABLE u (id INTEGER);"
        )
        connection.executemany("INSERT INTO t (n) VALUES (?)", [("x",)] * 300)
        connection.execute("ANALYZE")
        connection.commit()
        ctx = read_context(connection, SQLITE)
        self.assertEqual(ctx.profile, "sqlite")
        self.assertEqual(ctx.version, sqlite_version(sqlite3.sqlite_version))
        self.assertEqual(ctx.settings["journal_mode"], "memory")
        self.assertLessEqual({"version", "settings", "sizes", "schema"}, ctx.read)
        self.assertEqual(ctx.stats("t").rows, 300)
        self.assertGreater(ctx.stats("t").bytes, 0)
        self.assertEqual(ctx.stats("main.t"), ctx.stats("t"))
        # An empty table is left out of sqlite_stat1.
        self.assertIsNone(ctx.stats("u").rows)
        self.assertGreater(ctx.stats(DATABASE).bytes, 0)

    def test_a_database_never_analyzed_has_no_row_counts(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE t (id INTEGER)")
        ctx = read_context(connection, SQLITE)
        self.assertIsNone(ctx.stats("t").rows)
        self.assertIn("sizes", ctx.read)

    def test_failed_reads_leave_their_facts_out(self):
        failure = RuntimeError("no such table")
        asked, ctx = drive(context_plan(), [failure] * 6)
        self.assertEqual(len(asked), 6)
        self.assertEqual(ctx.version, FLOORS["sqlite"])
        self.assertEqual(ctx.read, frozenset())
        self.assertEqual(dict(ctx.tables), {})

    def test_sqlite_version(self):
        self.assertEqual(sqlite_version("3.45.1"), (3, 45, 1))
        self.assertEqual(sqlite_version("unknown"), FLOORS["sqlite"])

    def test_the_floor_matches_the_support_table(self):
        support = json.loads((ROOT / "support.json").read_text())
        row = next(r for r in support["databases"] if r["name"] == "sqlite")
        floor = tuple(int(p) for p in row["floor"].split("."))
        self.assertEqual(FLOORS["sqlite"], floor)
        self.assertEqual(assumed("sqlite").version, floor)


class RuleCatalogTestCase(unittest.TestCase):
    def test_every_rule_has_a_source_and_a_fixture_that_reaches_it(self):
        profile = profile_for(SQLITE)
        reached = set()
        for rule in profile.rules:
            self.assertTrue(rule.source.startswith("https://"), rule.id)
            self.assertTrue(rule.id.startswith("sqlite."), rule.id)
            for fixture in rule.fixtures:
                statement = impact(fixture)
                found = {t.rule for t in statement.tables} | set(rules(statement))
                if rule.id in found:
                    reached.add(rule.id)
                self.assertNotEqual(statement.confidence, Confidence.UNKNOWN, fixture)
        # The rebuild is known by its intent, which a fixture lacks;
        # RebuildTestCase reaches it.
        self.assertEqual(
            reached, {rule.id for rule in profile.rules} - {"sqlite.rebuild"}
        )

    def test_the_fixture_schema_runs(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        for sql in sqlite.FIXTURE_SCHEMA:
            connection.execute(sql)

    def test_lock_rank_and_blocks(self):
        self.assertEqual(sqlite.lock_rank(None), -1)
        self.assertEqual(sqlite.lock_rank(sqlite.WRITE_LOCK), 0)
        self.assertIs(sqlite.blocks(None), Blocks.NOTHING)
        self.assertIs(sqlite.blocks(sqlite.WRITE_LOCK), Blocks.READS_AND_WRITES)
        self.assertEqual(sqlite.timeout_statement(True), "PRAGMA busy_timeout = 5000")


class RecognizerTestCase(unittest.TestCase):
    def test_sqlite_reindex(self):
        whole = recognize("REINDEX", SQLITE)
        self.assertEqual((whole.kind, whole.options["target"]), ("reindex", "database"))
        named = recognize("REINDEX main.ix", SQLITE)
        self.assertEqual(
            (named.options["target"], named.options["name"]), ("any", "main.ix")
        )
        self.assertFalse(recognize("REINDEX t", Dialects.POSTGRES).known)


class MigratorTestCase(unittest.TestCase):
    def test_impact_reads_the_connection(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE t (id INTEGER, c TEXT)")
        migrator = Migrator(
            connection,
            [Migration("001_drop", up="ALTER TABLE t DROP COLUMN c")],
            dialect=SQLITE,
        )
        report = migrator.impact()
        self.assertEqual(report.profile, "sqlite")
        self.assertIn("version", report.read)
        (statement,) = report.statements
        self.assertEqual(statement.tables[0].work, Work.REWRITE)


if __name__ == "__main__":
    unittest.main()
