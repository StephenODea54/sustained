"""
Tests for the DuckDB rules, their conflicts, and the context read.
"""

import json
import unittest
from pathlib import Path
from types import MappingProxyType

from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    Hold,
    TableStats,
    Work,
    analyze,
    read_context,
)
from sustained.impact.context import FLOORS, assumed
from sustained.impact.model import Intent
from sustained.impact.rules import duckdb, profile_for
from sustained.impact.rules.duckdb import context_plan, duckdb_version
from sustained.impact.window import DATABASE
from sustained.migrations import Migration, Migrator
from tests.test_impact_context import drive

try:
    import duckdb as duckdb_driver
except ImportError:  # pragma: no cover
    duckdb_driver = None

ROOT = Path(__file__).resolve().parent.parent
DUCKDB = Dialects.DUCKDB


def context(**tables):
    """A read DuckDB database with the row counts given."""
    stats = {name: TableStats(rows) for name, rows in tables.items()}
    return EngineContext(
        "duckdb",
        (1, 5, 5),
        tables=MappingProxyType(stats),
        read=frozenset({"version", "sizes"}),
    )


def impact(sql, ctx=None, transactional=True):
    statement = MigrationStatement(sql, "001", transactional)
    (found,) = analyze([statement], DUCKDB, ctx).statements
    return found


def table(sql, ctx=None):
    found = impact(sql, ctx)
    assert len(found.tables) == 1, found.tables
    return found.tables[0]


def rules(statement):
    return [f.rule for f in statement.findings]


class StatementTestCase(unittest.TestCase):
    def test_each_statement_names_its_conflict_and_work(self):
        altered, entry, changed = (
            duckdb.ALTERED_TABLE,
            duckdb.CATALOG_ENTRY,
            duckdb.CHANGED_ROWS,
        )
        cases = [
            ("ALTER TABLE t ADD COLUMN d integer", altered, Work.ROWS, "add_column"),
            (
                "ALTER TABLE t ADD COLUMN d integer DEFAULT 5",
                altered,
                Work.ROWS,
                "add_column",
            ),
            (
                "ALTER TABLE t ADD COLUMN d timestamp DEFAULT now()",
                altered,
                Work.ROWS,
                "add_column",
            ),
            (
                "ALTER TABLE t ADD COLUMN d double DEFAULT random()",
                altered,
                Work.REWRITE,
                "add_column.volatile",
            ),
            ("ALTER TABLE t DROP COLUMN c", altered, Work.CATALOG, "drop_column"),
            (
                "ALTER TABLE t ALTER COLUMN c TYPE bigint",
                altered,
                Work.REWRITE,
                "alter_column_type",
            ),
            (
                "ALTER TABLE t ALTER COLUMN c SET NOT NULL",
                altered,
                Work.SCAN,
                "set_not_null",
            ),
            (
                "ALTER TABLE t ALTER COLUMN c DROP NOT NULL",
                entry,
                Work.CATALOG,
                "alter_column",
            ),
            (
                "ALTER TABLE t ALTER COLUMN c SET DEFAULT 1",
                entry,
                Work.CATALOG,
                "alter_column",
            ),
            (
                "ALTER TABLE t ALTER COLUMN c DROP DEFAULT",
                entry,
                Work.CATALOG,
                "alter_column",
            ),
            ("ALTER TABLE t RENAME COLUMN c TO d", entry, Work.CATALOG, "rename"),
            ("ALTER TABLE t RENAME TO u", entry, Work.CATALOG, "rename"),
            ("CREATE INDEX ix ON t (c)", None, Work.INDEX_BUILD, "create_index"),
            ("COMMENT ON COLUMN t.c IS 'x'", entry, Work.CATALOG, "comment"),
            ("DROP TABLE t", duckdb.DROPPED_TABLE, Work.CATALOG, "drop_table"),
            ("UPDATE t SET c = 1", changed, Work.ROWS, "write_rows"),
            ("DELETE FROM t", changed, Work.ROWS, "write_rows"),
            ("TRUNCATE t", changed, Work.ROWS, "write_rows"),
            ("INSERT INTO t (c) VALUES (1)", None, Work.ROWS, "write_rows"),
            ("ANALYZE t", None, Work.SCAN, "analyze"),
        ]
        for sql, lock, work, rule in cases:
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual(found.table, "t")
                self.assertEqual(found.lock, lock)
                self.assertEqual(found.blocks, duckdb.blocks(lock))
                self.assertEqual(found.work, work)
                self.assertEqual(found.rule, f"duckdb.{rule}")

    def test_a_conflict_lasts_until_the_commit(self):
        run = [
            MigrationStatement("ALTER TABLE t DROP COLUMN c", "001"),
            MigrationStatement("ALTER TABLE u DROP COLUMN c", "001"),
        ]
        first = analyze(run, DUCKDB).statements[0]
        self.assertIs(first.tables[0].hold, Hold.TRANSACTION)
        outside = impact("ALTER TABLE t DROP COLUMN c", transactional=False)
        self.assertIs(outside.tables[0].hold, Hold.BRIEF)
        self.assertIn("until it ends", outside.findings[0].message)

    def test_the_message_says_other_transactions_abort(self):
        (finding,) = impact(
            "ALTER TABLE t ADD COLUMN d integer", context(t=100)
        ).findings
        self.assertEqual(str(finding.severity), "info")
        self.assertIn("abort with a conflict error instead of waiting", finding.message)
        self.assertIn("until the migration commits", finding.message)
        self.assertIn("reads go on", finding.message)
        (update,) = impact("UPDATE t SET c = 1").findings
        self.assertIn("updates the same columns of the same rows", update.message)
        self.assertIn("backfill in batches", update.message)
        (delete,) = impact("DELETE FROM t").findings
        self.assertIn("deletes the same rows", delete.message)
        self.assertIn("delete in batches", delete.message)
        self.assertNotIn("backfill", delete.message)
        (drop,) = impact("DROP TABLE t").findings
        self.assertIn("wrote to t before the DROP fails to commit", drop.message)
        self.assertIn("reads go on", drop.message)
        (truncate,) = impact("TRUNCATE t").findings
        self.assertIn("deletes a row of t", truncate.message)
        self.assertNotIn("backfill", truncate.message)

    def test_a_catalog_entry_draws_no_finding(self):
        self.assertEqual(
            impact("ALTER TABLE t ALTER COLUMN c DROP DEFAULT").findings, ()
        )
        self.assertEqual(impact("CREATE INDEX ix ON t (c)").findings, ())

    def test_a_rewrite_is_rated_by_the_row_count(self):
        sql = "ALTER TABLE t ALTER COLUMN c TYPE bigint"
        (large,) = impact(sql, context(t=5_000_000)).findings
        self.assertEqual(str(large.severity), "danger")
        self.assertIn("writes every value of c again", large.message)
        (small,) = impact(sql, context(t=10)).findings
        self.assertEqual(str(small.severity), "info")
        # Without a read, the size is unknown.
        (unread,) = impact(sql).findings
        self.assertEqual(str(unread.severity), "warn")
        self.assertIn("the size of t is unknown", unread.message)

    def test_the_backfill_rewrites_the_column(self):
        statement = MigrationStatement(
            'ALTER TABLE "w" ALTER COLUMN "a" SET DATA TYPE INTEGER '
            'USING coalesce("a", 0)',
            "001",
        )
        statement.intent = Intent("backfill", "w", "a")
        (found,) = analyze([statement], DUCKDB).statements
        self.assertEqual(found.tables[0].work, Work.REWRITE)
        self.assertEqual(found.tables[0].rule, "duckdb.alter_column_type")
        self.assertNotIn("impact.intent_mismatch", rules(found))

    def test_a_volatile_default_the_rules_do_not_know_is_likely(self):
        known = impact("ALTER TABLE t ADD COLUMN d uuid DEFAULT gen_random_uuid()")
        self.assertIs(known.confidence, Confidence.KNOWN)
        self.assertIn("gen_random_uuid()", known.findings[0].message)
        guessed = impact("ALTER TABLE t ADD COLUMN d integer DEFAULT my_func()")
        self.assertIs(guessed.confidence, Confidence.LIKELY)
        self.assertIn("which no rule knows", guessed.findings[0].message)

    def test_a_refused_add_column_is_unknown(self):
        for sql in (
            "ALTER TABLE t ADD COLUMN d integer NOT NULL DEFAULT 0",
            "ALTER TABLE t ADD COLUMN d integer CHECK (d > 0)",
            "ALTER TABLE t ADD COLUMN d integer REFERENCES r (id)",
            "ALTER TABLE t ADD COLUMN d integer GENERATED ALWAYS AS (c * 2)",
        ):
            with self.subTest(sql=sql):
                self.assertIs(impact(sql).confidence, Confidence.UNKNOWN)

    def test_an_unknown_action_is_unknown(self):
        for sql in (
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0)",
            "ALTER TABLE t DROP CONSTRAINT ck",
            "ALTER TABLE t ADD PRIMARY KEY (id)",
        ):
            with self.subTest(sql=sql):
                statement = impact(sql)
                self.assertIs(statement.confidence, Confidence.UNKNOWN)
                self.assertIn("no DuckDB rule reads", statement.findings[0].message)

    def test_a_rename_warns_running_code(self):
        column = impact("ALTER TABLE t RENAME COLUMN c TO d")
        self.assertEqual(rules(column), ["duckdb.rename"])
        self.assertIn("names the column c", column.findings[0].message)
        renamed = impact("ALTER TABLE t RENAME TO u")
        self.assertIn("names the table t", renamed.findings[0].message)

    def test_drop_index_finds_its_table(self):
        run = [
            MigrationStatement("CREATE INDEX ix ON t (c)", "001"),
            MigrationStatement("DROP INDEX ix", "002"),
        ]
        dropped = analyze(run, DUCKDB).statements[-1]
        self.assertEqual(dropped.tables[0].table, "t")
        self.assertEqual(dropped.tables[0].lock, duckdb.CATALOG_ENTRY)
        generated = MigrationStatement('DROP INDEX "ix"', "001")
        generated.intent = Intent("drop_index", "w")
        (found,) = analyze([generated], DUCKDB).statements
        self.assertEqual(found.tables[0].table, "w")
        self.assertEqual(table("DROP INDEX ix").table, "(table of index ix)")

    def test_a_new_table_opens_a_catalog_entry_on_what_it_references(self):
        found = impact("CREATE TABLE n (id integer, r_id integer REFERENCES r (id))")
        tables = {t.table: t for t in found.tables}
        self.assertEqual(set(tables), {"n", "r"})
        self.assertIs(tables["n"].blocks, Blocks.NOTHING)
        self.assertIs(tables["r"].blocks, Blocks.DDL)
        self.assertEqual(
            table("CREATE TABLE n (id integer)").rule, "duckdb.create_table"
        )

    def test_schema_objects_open_no_conflict(self):
        for sql in (
            "CREATE VIEW v AS SELECT 1",
            "CREATE TYPE mood AS ENUM ('a')",
            "DROP TYPE mood",
            "CREATE SEQUENCE s",
            "DROP SCHEMA s",
        ):
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual(found.table, DATABASE)
                self.assertIs(found.blocks, Blocks.NOTHING)
                self.assertEqual(found.rule, "duckdb.schema_change")
        self.assertEqual(table("DROP VIEW v").table, "v")
        self.assertEqual(table("COMMENT ON SCHEMA s IS 'x'").table, DATABASE)
        self.assertEqual(table("ANALYZE").table, DATABASE)
        self.assertEqual(impact("SET threads = 4").tables, ())

    def test_no_lock_timeout_finding(self):
        statement = impact("ALTER TABLE t ALTER COLUMN c TYPE bigint")
        self.assertNotIn("duckdb.lock_timeout", rules(statement))

    def test_a_statement_on_a_table_the_run_created_blocks_nothing(self):
        run = [
            MigrationStatement("CREATE TABLE n (c integer)", "001"),
            MigrationStatement("ALTER TABLE n ALTER COLUMN c TYPE bigint", "001"),
        ]
        retyped = analyze(run, DUCKDB).statements[-1]
        self.assertIs(retyped.tables[0].blocks, Blocks.NOTHING)
        self.assertEqual(retyped.findings, ())


class WindowTestCase(unittest.TestCase):
    def test_a_conflict_held_across_a_rewrite_is_a_window(self):
        run = [
            MigrationStatement("ALTER TABLE t ADD COLUMN d integer", "001"),
            MigrationStatement("ALTER TABLE t ALTER COLUMN c TYPE bigint", "001"),
        ]
        (migration,) = analyze(run, DUCKDB).migrations
        (window,) = migration.windows
        self.assertEqual(window.table, "t")
        self.assertIs(window.heaviest, Work.REWRITE)
        self.assertIn("window.held", [f.rule for f in migration.findings])
        self.assertTrue(migration.held_to_commit)


class ContextTestCase(unittest.TestCase):
    @unittest.skipIf(duckdb_driver is None, "duckdb not installed")
    def test_reads_a_live_database(self):
        connection = duckdb_driver.connect()
        self.addCleanup(connection.close)
        connection.execute("CREATE SCHEMA other")
        connection.execute("CREATE TABLE t (id integer)")
        connection.execute("INSERT INTO t SELECT * FROM range(300)")
        connection.execute("CREATE TABLE other.u (id integer)")
        ctx = read_context(connection, DUCKDB)
        self.assertEqual(ctx.profile, "duckdb")
        self.assertEqual(ctx.version, duckdb_version(duckdb_driver.__version__))
        self.assertLessEqual({"version", "sizes", "schema"}, ctx.read)
        self.assertEqual(ctx.stats("t").rows, 300)
        self.assertEqual(ctx.stats("main.t"), ctx.stats("t"))
        self.assertIsNone(ctx.stats("t").bytes)
        # A table outside the current schema is found by its full name.
        self.assertEqual(ctx.stats("other.u").rows, 0)
        self.assertIsNone(ctx.stats("u").rows)

    def test_a_null_row_count_is_unknown(self):
        _, ctx = drive(context_plan(), [[("v1.1.0",)], [("main", "t", True, None)]])
        self.assertEqual(ctx.version, (1, 1, 0))
        self.assertIsNone(ctx.stats("t").rows)
        self.assertIn("sizes", ctx.read)

    def test_failed_reads_leave_their_facts_out(self):
        failure = RuntimeError("no such function")
        asked, ctx = drive(context_plan(), [failure] * 2)
        self.assertEqual(len(asked), 2)
        self.assertEqual(ctx.version, FLOORS["duckdb"])
        self.assertEqual(ctx.read, frozenset())
        self.assertEqual(dict(ctx.tables), {})

    def test_duckdb_version(self):
        self.assertEqual(duckdb_version("v1.5.5"), (1, 5, 5))
        self.assertEqual(duckdb_version("1.2.0-dev123"), (1, 2, 0))
        self.assertEqual(duckdb_version("unknown"), FLOORS["duckdb"])

    def test_the_floor_matches_the_support_table(self):
        support = json.loads((ROOT / "support.json").read_text())
        row = next(r for r in support["databases"] if r["name"] == "duckdb")
        floor = tuple(int(p) for p in row["floor"].split("."))
        self.assertEqual(FLOORS["duckdb"], floor)
        self.assertEqual(assumed("duckdb").version, floor)
        # Without a context, the rules assume the floor.
        report = analyze(["ALTER TABLE t ADD COLUMN d integer"], DUCKDB)
        self.assertEqual(report.version, floor)
        self.assertEqual(str(report.evidence), "static")


class RuleCatalogTestCase(unittest.TestCase):
    def test_every_rule_has_a_source_and_a_fixture_that_reaches_it(self):
        profile = profile_for(DUCKDB)
        for rule in profile.rules:
            self.assertTrue(rule.source.startswith("https://duckdb.org/"), rule.id)
            self.assertTrue(rule.id.startswith("duckdb."), rule.id)
            self.assertTrue(rule.fixtures, rule.id)
            reached = False
            for fixture in rule.fixtures:
                statement = impact(fixture)
                found = {t.rule for t in statement.tables} | set(rules(statement))
                reached = reached or rule.id in found
                self.assertNotEqual(statement.confidence, Confidence.UNKNOWN, fixture)
            self.assertTrue(reached, rule.id)

    @unittest.skipIf(duckdb_driver is None, "duckdb not installed")
    def test_the_fixture_schema_runs(self):
        connection = duckdb_driver.connect()
        self.addCleanup(connection.close)
        for sql in duckdb.FIXTURE_SCHEMA:
            connection.execute(sql)

    def test_lock_rank_and_blocks(self):
        self.assertEqual(duckdb.lock_rank(None), -1)
        self.assertLess(
            duckdb.lock_rank(duckdb.CATALOG_ENTRY),
            duckdb.lock_rank(duckdb.CHANGED_ROWS),
        )
        self.assertLess(
            duckdb.lock_rank(duckdb.CHANGED_ROWS),
            duckdb.lock_rank(duckdb.ALTERED_TABLE),
        )
        self.assertIs(duckdb.blocks(None), Blocks.NOTHING)
        self.assertIs(duckdb.blocks(duckdb.CATALOG_ENTRY), Blocks.DDL)
        self.assertIs(duckdb.blocks(duckdb.ALTERED_TABLE), Blocks.WRITES)
        self.assertEqual(duckdb.timeout_statement(True), "")
        self.assertFalse(duckdb.PROFILE.waits_in_queue(duckdb.ALTERED_TABLE))
        self.assertIsNone(duckdb.PROFILE.trace)


class MigratorTestCase(unittest.TestCase):
    @unittest.skipIf(duckdb_driver is None, "duckdb not installed")
    def test_impact_reads_the_connection(self):
        connection = duckdb_driver.connect()
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE t (id integer, c integer)")
        migrator = Migrator(
            connection,
            [Migration("001_retype", up="ALTER TABLE t ALTER COLUMN c TYPE bigint")],
            dialect=DUCKDB,
        )
        report = migrator.impact()
        self.assertEqual(report.profile, "duckdb")
        self.assertIn("version", report.read)
        (statement,) = report.statements
        self.assertEqual(statement.tables[0].work, Work.REWRITE)
        self.assertEqual(statement.tables[0].rows, 0)


if __name__ == "__main__":
    unittest.main()
