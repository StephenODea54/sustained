"""
Tests for script(annotate=True) and `sustained script --annotate`, and
for the danger findings `sustained migrate` prints, against a scripted
Postgres connection.
"""

import asyncio
import os
import sqlite3
import unittest
from unittest import mock

from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.migrations import Migration, Migrator
from tests.test_cli import CliBase
from tests.test_impact_context import INDEX, ScriptedAdapter, ScriptedConnection

PG = Dialects.POSTGRES

TIMEOUT = "SET LOCAL lock_timeout = '5s'"
ADD = "ALTER TABLE orders ADD COLUMN note text"


def orders_run():
    return [
        Migration(
            "001_orders",
            up=[INDEX, TIMEOUT, ADD],
            down="ALTER TABLE orders DROP COLUMN note",
        )
    ]


def without_timestamps(script):
    """The script with the bookkeeping lines, which hold a timestamp, left out."""
    return [line for line in script.splitlines() if "sustained_migrations" not in line]


class AnnotatedScriptTestCase(unittest.TestCase):
    def test_prints_each_statements_impact_above_it(self):
        script = Migrator(ScriptedConnection(), orders_run(), dialect=PG).script(
            annotate=True
        )
        self.assertEqual(
            without_timestamps(script),
            [
                "-- impact: 3 statements, 1 danger, 1 warn. "
                "Evidence: catalog (PostgreSQL 16.4)",
                "-- up: 001_orders",
                "-- impact: orders  SHARE  blocks writes  index_build  transaction  "
                "~2.0M rows, 3.0 GB  [pg.create_index]",
                "-- impact: danger  writes to orders wait for the whole index build; "
                "build it CONCURRENTLY in a migration with transactional=False",
                "-- impact: fix     CREATE INDEX CONCURRENTLY ix_orders_customer "
                "ON orders (customer_id)",
                "-- impact: warn    no lock_timeout in scope: while this statement "
                "waits for its lock, every query that conflicts with it on orders "
                "queues behind it, for as long as the longest open transaction runs",
                f"-- impact: fix     {TIMEOUT}",
                f"{INDEX};",
                "-- impact: locks no table",
                f"{TIMEOUT};",
                "-- impact: orders  ACCESS EXCLUSIVE  blocks reads_and_writes  "
                "catalog  transaction  ~2.0M rows, 3.0 GB  [pg.add_column]",
                f"{ADD};",
                "-- impact: window  orders: SHARE from statement 1, "
                "ACCESS EXCLUSIVE from statement 3, held to commit",
            ],
        )

    def test_without_annotate_the_script_is_unchanged(self):
        script = Migrator(ScriptedConnection(), orders_run(), dialect=PG).script()
        self.assertNotIn("-- impact:", script)
        self.assertEqual(
            without_timestamps(script),
            ["-- up: 001_orders", f"{INDEX};", f"{TIMEOUT};", f"{ADD};"],
        )

    def test_the_async_migrator_renders_the_same_comments(self):
        sync = Migrator(ScriptedConnection(), orders_run(), dialect=PG).script(
            annotate=True
        )
        migrator = AsyncMigrator(ScriptedAdapter(), orders_run(), dialect=PG)
        async_script = asyncio.run(migrator.script(annotate=True))
        self.assertEqual(without_timestamps(sync), without_timestamps(async_script))

    def test_nothing_pending_prints_no_summary(self):
        script = Migrator(ScriptedConnection(), [], dialect=PG).script(annotate=True)
        self.assertEqual(script, "")

    @mock.patch("sustained.impact.rules._profiles", return_value={})
    def test_refuses_a_dialect_without_rules(self, _):
        migrator = Migrator(sqlite3.connect(":memory:"), orders_run())
        with self.assertRaises(DialectError):
            migrator.script(annotate=True)


POSTGRES_CONFIG = """
from sustained.migrations import Migration
from tests.test_impact_context import INDEX, ScriptedConnection

connection = ScriptedConnection()
dialect = "postgres"
migrations = [Migration("001_orders", up=[INDEX])]
"""


class ImpactCliTestCase(CliBase):
    def use_postgres(self):
        with open(os.path.join(self.dir.name, f"{self.config_name}.py"), "w") as f:
            f.write(POSTGRES_CONFIG)

    def test_script_annotate_prints_the_comments(self):
        self.use_postgres()
        code, out, _ = self.run_cli("script", "--annotate")
        self.assertEqual(code, 0)
        self.assertIn("-- impact: danger  writes to orders wait", out)

    @mock.patch("sustained.impact.rules._profiles", return_value={})
    def test_script_annotate_fails_on_a_dialect_without_rules(self, _):
        code, _, err = self.run_cli("script", "--annotate")
        self.assertEqual(code, 1)
        self.assertIn("Impact analysis does not cover", err)

    def test_migrate_prints_the_danger_lines_up_prints(self):
        self.use_postgres()
        code, _, err = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn(f"danger: pg.create_index  {INDEX}: ", err)


if __name__ == "__main__":
    unittest.main()
