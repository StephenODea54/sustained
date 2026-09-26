"""
Tests for Migrator.impact(), AsyncMigrator.impact(), and the text and
JSON forms of a report in sustained.impact.report.
"""

import asyncio
import json
import unittest

from sustained import create_model
from sustained.aio_migrations import AsyncMigrator
from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.impact import (
    Blocks,
    EngineContext,
    Hold,
    Severity,
    TableImpact,
    TableStats,
    Work,
    analyze,
)
from sustained.impact.report import (
    flagged,
    flagged_line,
    render,
    report_data,
    statement_data,
    summary,
    table_line,
)
from sustained.migrations import Migration, Migrator
from sustained.schema import Integer
from tests.test_migration_locking import FakePostgresAdapter, FakePostgresConnection

PG = Dialects.POSTGRES

INDEX = "CREATE INDEX ix_orders_customer ON orders (customer_id)"
ADD = "ALTER TABLE orders ADD COLUMN note text"


def m(sql, migration_id="m1", transactional=True):
    return MigrationStatement(sql, migration_id, transactional)


def notes_model():
    notes = create_model("Notes", "notes")
    notes.tableColumns = {"id": Integer(primary_key=True)}
    notes.columns = ("id",)
    return notes


class MigratorImpactTestCase(unittest.TestCase):
    def migrator(self, *migrations, dialect=PG):
        return Migrator(FakePostgresConnection(), list(migrations), dialect=dialect)

    def test_analyzes_the_pending_run(self):
        migrator = self.migrator(Migration("001_orders", up=[INDEX, ADD]))
        report = migrator.impact()
        (migration,) = report.migrations
        self.assertEqual(migration.migration_id, "001_orders")
        self.assertTrue(migration.transactional)
        self.assertEqual([s.statement for s in migration.statements], [INDEX, ADD])
        self.assertEqual(migration.statements[0].tables[0].lock, "SHARE")

    def test_keeps_the_transaction_flag(self):
        migrator = self.migrator(
            Migration("001_orders", up=[INDEX], transactional=False)
        )
        (migration,) = migrator.impact().migrations
        self.assertFalse(migration.transactional)

    def test_adds_the_migration_the_models_generate(self):
        migrator = self.migrator(Migration("001_orders", up=[INDEX]))
        report = migrator.impact([notes_model()])
        self.assertEqual(len(report.migrations), 2)
        generated = report.migrations[1]
        self.assertNotEqual(generated.migration_id, "001_orders")
        self.assertIn('CREATE TABLE "notes"', generated.statements[0].statement)

    def test_skips_a_callable_step(self):
        migrator = self.migrator(
            Migration("001_backfill", up=lambda c: None, checksum="fixed")
        )
        self.assertEqual(migrator.impact().migrations, ())

    def test_refuses_a_dialect_without_rules_before_reading(self):
        connection = FakePostgresConnection()
        migrator = Migrator(connection, [], dialect=Dialects.DEFAULT)
        with self.assertRaises(DialectError) as caught:
            migrator.impact()
        self.assertIn("does not cover DEFAULT", str(caught.exception))
        self.assertEqual(connection.log, [])


class AsyncMigratorImpactTestCase(unittest.TestCase):
    def test_analyzes_the_pending_run(self):
        migrator = AsyncMigrator(
            FakePostgresAdapter(), [Migration("001_orders", up=[INDEX])], dialect=PG
        )
        report = asyncio.run(migrator.impact())
        (migration,) = report.migrations
        self.assertEqual(migration.migration_id, "001_orders")
        self.assertEqual(migration.statements[0].tables[0].lock, "SHARE")

    def test_adds_the_migration_the_models_generate(self):
        migrator = AsyncMigrator(FakePostgresAdapter(), [], dialect=PG)
        report = asyncio.run(migrator.impact([notes_model()]))
        (migration,) = report.migrations
        self.assertIn('CREATE TABLE "notes"', migration.statements[0].statement)

    def test_refuses_a_dialect_without_rules_before_reading(self):
        adapter = FakePostgresAdapter()
        migrator = AsyncMigrator(adapter, [], dialect=Dialects.DEFAULT)
        with self.assertRaises(DialectError):
            asyncio.run(migrator.impact())
        self.assertEqual(adapter.log, [])


class RenderTestCase(unittest.TestCase):
    def test_renders_statements_findings_and_windows(self):
        text = render(
            analyze([m(INDEX, "20260926_orders"), m(ADD, "20260926_orders")], PG)
        )
        lines = text.splitlines()
        self.assertEqual(lines[0], "20260926_orders  transaction")
        self.assertEqual(lines[1], f"  {INDEX}")
        self.assertEqual(
            lines[2],
            "    orders  SHARE  blocks writes  index_build  transaction  "
            "[pg.create_index]",
        )
        self.assertTrue(lines[3].startswith("    warn    writes to orders wait"))
        self.assertIn(
            "    fix     CREATE INDEX CONCURRENTLY ix_orders_customer ON orders "
            "(customer_id)",
            lines,
        )
        self.assertIn(
            "  window  orders: SHARE from statement 1, ACCESS EXCLUSIVE from "
            "statement 2, held to commit",
            lines,
        )
        self.assertEqual(
            lines[-1],
            "2 statements, 0 danger, 3 warn. Evidence: static (assumed "
            "PostgreSQL 12)",
        )

    def test_a_migration_without_a_transaction_has_no_window(self):
        text = render(analyze([m(ADD, "m1", False)], PG))
        self.assertIn("m1  no transaction", text)
        self.assertNotIn("window", text)

    def test_a_statement_without_a_migration(self):
        text = render(analyze(["DROP TABLE orders"], PG))
        self.assertTrue(text.startswith("(no migration)  transaction"))

    def test_an_empty_report_prints_only_the_summary(self):
        self.assertEqual(
            render(analyze([], PG)),
            "0 statements, 0 danger, 0 warn. Evidence: static (assumed "
            "PostgreSQL 12)",
        )

    def test_a_second_remedy_line_has_no_label(self):
        text = render(
            analyze([m("ALTER TABLE orders ALTER COLUMN note SET NOT NULL")], PG)
        )
        fixes = [line for line in text.splitlines() if line.startswith("    fix     ")]
        self.assertTrue(fixes)
        index = text.splitlines().index(fixes[0])
        self.assertTrue(text.splitlines()[index + 1].startswith("            "))

    def test_summary_counts_unknown_statements(self):
        report = analyze([m("GRANT SELECT ON orders TO app")], PG)
        self.assertIn("1 statement, 0 danger, 0 warn, 1 unknown.", summary(report))

    def test_summary_names_catalog_evidence(self):
        context = EngineContext(
            "postgres",
            (16, 4),
            tables={"orders": TableStats(10, 8192)},
            read=frozenset({"version", "sizes"}),
        )
        report = analyze([m(INDEX)], PG, context)
        self.assertTrue(summary(report).endswith("Evidence: catalog (PostgreSQL 16.4)"))

    def test_table_line_prints_the_size(self):
        table = TableImpact(
            "orders",
            "SHARE",
            Blocks.WRITES,
            Work.INDEX_BUILD,
            Hold.STATEMENT,
            41_200_000,
            12_400 * 1024 * 1024 * 1024 // 1000,
            "pg.create_index",
        )
        self.assertEqual(
            table_line(table),
            "orders  SHARE  blocks writes  index_build  statement  "
            "~41.2M rows, 12.4 GB  [pg.create_index]",
        )

    def test_table_line_small_sizes_and_no_lock(self):
        table = TableImpact(
            "orders", None, Blocks.NOTHING, Work.CATALOG, Hold.BRIEF, 12, 512
        )
        self.assertEqual(
            table_line(table),
            "orders  no lock  blocks nothing  catalog  brief  " "~12 rows, 512 B",
        )


class DataTestCase(unittest.TestCase):
    def test_report_data_is_json(self):
        report = analyze([m(INDEX), m(ADD)], PG)
        data = report_data(report)
        self.assertEqual(json.loads(json.dumps(data)), data)
        self.assertEqual(data["profile"], "postgres")
        self.assertEqual(data["version"], "12")
        self.assertEqual(data["counts"], {"info": 0, "warn": 3, "danger": 0})
        (migration,) = data["migrations"]
        self.assertEqual(migration["id"], "m1")
        self.assertEqual(migration["statements"][0]["sql"], INDEX)
        self.assertEqual(
            migration["locks"][0],
            {"table": "orders", "lock": "SHARE", "blocks": "writes", "statement": 1},
        )
        self.assertEqual(migration["windows"][0]["table"], "orders")

    def test_statement_data(self):
        (impact,) = analyze([m(INDEX)], PG).statements
        data = statement_data(impact)
        self.assertEqual(data["kind"], "create_index")
        self.assertEqual(data["severity"], "warn")
        self.assertEqual(data["confidence"], "known")
        self.assertEqual(data["evidence"], "static")
        self.assertEqual(data["tables"][0]["work"], "index_build")
        finding = data["findings"][0]
        self.assertEqual(finding["rule"], "pg.create_index")
        self.assertEqual(
            finding["remedy"],
            ["CREATE INDEX CONCURRENTLY ix_orders_customer ON orders (customer_id)"],
        )

    def test_an_unknown_statement_has_no_kind(self):
        (impact,) = analyze([m("GRANT SELECT ON orders TO app")], PG).statements
        data = statement_data(impact)
        self.assertIsNone(data["kind"])
        self.assertEqual(data["severity"], "info")
        self.assertEqual(data["confidence"], "unknown")

    def test_a_statement_without_findings_has_no_severity(self):
        (impact,) = analyze([m("CREATE TABLE t (id int)")], PG).statements
        self.assertIsNone(statement_data(impact)["severity"])


class FlaggedTestCase(unittest.TestCase):
    def test_lists_warn_and_unknown_statements(self):
        report = analyze(
            [
                m("CREATE TABLE t (id int)"),
                m(INDEX),
                m("GRANT SELECT ON orders TO app"),
            ],
            PG,
        )
        listed = flagged(report.statements)
        self.assertEqual(
            [s.statement for s in listed], [INDEX, "GRANT SELECT ON orders TO app"]
        )
        self.assertEqual(
            flagged_line(listed[0]),
            f"warn    {INDEX}  [pg.create_index, pg.lock_timeout]",
        )
        self.assertEqual(
            flagged_line(listed[1]),
            "info    GRANT SELECT ON orders TO app  [impact.unknown]",
        )

    def test_a_danger_statement_names_its_rules_once(self):
        context = EngineContext(
            "postgres",
            (16,),
            tables={"orders": TableStats(50_000_000, None)},
            read=frozenset({"sizes"}),
        )
        (impact,) = analyze([m(INDEX)], PG, context).statements
        self.assertIs(impact.severity, Severity.DANGER)
        line = flagged_line(impact)
        self.assertTrue(line.startswith("danger  "))
        self.assertEqual(line.count("pg.create_index"), 1)


if __name__ == "__main__":
    unittest.main()
