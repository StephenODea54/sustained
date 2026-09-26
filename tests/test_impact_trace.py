"""
Tests for observed impact: the Postgres lock and file reads, the
comparison of a prediction against what the server did, and
rehearse(trace=True).
"""

import sqlite3
import unittest
from types import MappingProxyType
from unittest import mock

from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
from sustained.impact import Blocks, Evidence, Hold, TableImpact, Work, analyze
from sustained.impact.model import Severity
from sustained.impact.rules import profile_for
from sustained.impact.rules.postgres import PROFILE
from sustained.impact.rules.postgres.trace import (
    File,
    Sighting,
    lock_name,
    observe,
    sighting_plan,
    tables_plan,
    with_observations,
)
from sustained.migrations import Migration, Migrator
from sustained.migrations.core.tracing import Tracer
from tests.test_impact_context import drive

PG = Dialects.POSTGRES
ORDERS = 100
PARTS = 200
NEW = 300


def sighting(locks=None, storage=None, read=("locks", "storage")):
    names = {"orders": ORDERS, "public.orders": ORDERS, "parts": PARTS, "fresh": NEW}
    return Sighting(
        MappingProxyType(
            {oid: frozenset(modes) for oid, modes in (locks or {}).items()}
        ),
        MappingProxyType(names),
        MappingProxyType(storage or {}),
        frozenset(read),
    )


def table_file(filenode, size=8192):
    return File(False, filenode, size)


def index_file(filenode):
    return File(True, filenode, 16384)


def predicted(sql, **context):
    (statement,) = analyze([sql], PG).statements
    return statement


EXISTING = frozenset({ORDERS, PARTS})


class LockNameTestCase(unittest.TestCase):
    def test_names_each_mode_as_the_rules_do(self):
        self.assertEqual(lock_name("AccessExclusiveLock"), "ACCESS EXCLUSIVE")
        self.assertEqual(lock_name("ShareLock"), "SHARE")
        self.assertEqual(
            lock_name("ShareUpdateExclusiveLock"), "SHARE UPDATE EXCLUSIVE"
        )
        self.assertEqual(lock_name("RowExclusiveLock"), "ROW EXCLUSIVE")

    def test_postgres_and_mysql_trace(self):
        self.assertIsNotNone(profile_for(PG).trace.sighting)
        self.assertIsNotNone(profile_for(Dialects.MYSQL).trace.attempts)
        self.assertIsNone(profile_for(Dialects.DEFAULT))


class PlanTestCase(unittest.TestCase):
    def test_reads_the_tables_that_exist(self):
        _, existing = drive(tables_plan(), [[(1,), (2,)]])
        self.assertEqual(existing, {1, 2})

    def test_a_failed_table_read_is_none(self):
        _, existing = drive(tables_plan(), [RuntimeError("denied")])
        self.assertIsNone(existing)

    def test_reads_the_locks_and_the_files(self):
        asked, seen = drive(
            sighting_plan(["public.Orders", "o'brien"]),
            [
                [(100, "public", "orders", True, "ShareLock")],
                [
                    (100, "public", "orders", True, 101, False, 5001, 8192),
                    (100, "public", "orders", True, 102, True, 5002, 16384),
                ],
            ],
        )
        self.assertIn("pg_locks", asked[0])
        self.assertIn("IN ('o''brien', 'orders')", asked[1])
        self.assertEqual(seen.read, {"locks", "storage"})
        self.assertEqual(seen.locks[100], {"SHARE"})
        self.assertEqual(seen.names["orders"], 100)
        self.assertEqual(seen.names["public.orders"], 100)
        self.assertEqual(seen.storage[100][101], File(False, 5001, 8192))
        self.assertEqual(seen.storage[100][102], File(True, 5002, 16384))

    def test_a_table_off_the_search_path_is_found_by_its_schema(self):
        _, seen = drive(
            sighting_plan([]), [[(7, "audit", "orders", False, "ShareLock")]]
        )
        self.assertEqual(dict(seen.names), {"audit.orders": 7})

    def test_no_named_table_reads_no_files(self):
        asked, seen = drive(sighting_plan([]), [[]])
        self.assertEqual(len(asked), 1)
        self.assertEqual(seen.read, {"locks"})

    def test_a_failed_read_leaves_its_part_out(self):
        _, seen = drive(
            sighting_plan(["orders"]), [RuntimeError("denied"), RuntimeError("denied")]
        )
        self.assertEqual(seen.read, frozenset())

    def test_the_reads_hold_no_percent_sign(self):
        asked, _ = drive(sighting_plan(["orders"]), [[], []])
        asked.append(next(tables_plan()))
        self.assertTrue(all("%" not in sql for sql in asked))


class ObserveTestCase(unittest.TestCase):
    def test_a_confirmed_prediction_changes_only_the_evidence(self):
        statement = predicted("CREATE INDEX ix ON orders (c)")
        before = sighting(storage={ORDERS: {101: table_file(1)}})
        after = sighting(
            {ORDERS: {"SHARE", "ACCESS SHARE"}},
            {ORDERS: {101: table_file(1), 102: index_file(2)}},
        )
        observed = observe(statement, before, after, EXISTING, PROFILE)
        self.assertIs(observed.evidence, Evidence.OBSERVED)
        self.assertEqual(observed.tables, statement.tables)
        self.assertEqual(observed.findings, statement.findings)

    def test_a_stronger_lock_is_a_mismatch(self):
        statement = predicted("CREATE INDEX ix ON orders (c)")
        after = sighting({ORDERS: {"ACCESS EXCLUSIVE"}})
        observed = observe(statement, sighting(), after, EXISTING, PROFILE)
        (table,) = observed.tables
        self.assertEqual(table.lock, "ACCESS EXCLUSIVE")
        self.assertIs(table.blocks, Blocks.READS_AND_WRITES)
        mismatch = observed.findings[-1]
        self.assertEqual(mismatch.rule, "impact.mismatch")
        self.assertIs(mismatch.severity, Severity.WARN)
        self.assertIn("predicted SHARE on orders", mismatch.message)
        self.assertIn("took ACCESS EXCLUSIVE", mismatch.message)

    def test_a_lock_held_from_an_earlier_statement_counts_as_taken(self):
        statement = predicted("CREATE INDEX ix ON orders (c)")
        held = {ORDERS: {"SHARE", "ACCESS SHARE"}}
        observed = observe(statement, sighting(held), sighting(held), EXISTING, PROFILE)
        self.assertEqual(observed.tables[0].lock, "SHARE")
        self.assertNotIn("impact.mismatch", [f.rule for f in observed.findings])

    def test_a_weaker_lock_lowers_what_the_table_blocks(self):
        statement = predicted("ALTER TABLE orders ADD COLUMN c integer")
        after = sighting({ORDERS: {"SHARE UPDATE EXCLUSIVE"}})
        (table,) = observe(statement, sighting(), after, EXISTING, PROFILE).tables
        self.assertEqual(table.lock, "SHARE UPDATE EXCLUSIVE")
        self.assertIs(table.blocks, Blocks.DDL)

    def test_a_rule_that_set_what_a_table_blocks_keeps_it(self):
        statement = predicted("UPDATE orders SET c = 0")
        after = sighting({ORDERS: {"SHARE UPDATE EXCLUSIVE", "ROW EXCLUSIVE"}})
        (table,) = observe(statement, sighting(), after, EXISTING, PROFILE).tables
        self.assertEqual(table.lock, "SHARE UPDATE EXCLUSIVE")
        self.assertIs(table.blocks, Blocks.WRITES)

    def test_a_rewrite_that_was_seen_replaces_the_predicted_work(self):
        statement = predicted("ALTER TABLE orders ALTER COLUMN c SET NOT NULL")
        before = sighting(storage={ORDERS: {101: table_file(1)}})
        after = sighting({ORDERS: {"ACCESS EXCLUSIVE"}}, {ORDERS: {101: table_file(9)}})
        observed = observe(statement, before, after, EXISTING, PROFILE)
        self.assertIs(observed.tables[0].work, Work.REWRITE)
        self.assertIn("the server rewrote it", observed.findings[-1].message)

    def test_a_predicted_rewrite_with_no_file_copied_falls_to_a_scan(self):
        statement = predicted("ALTER TABLE orders ALTER COLUMN c TYPE bigint")
        files = {ORDERS: {101: table_file(1), 102: index_file(2)}}
        after = sighting({ORDERS: {"ACCESS EXCLUSIVE"}}, files)
        observed = observe(statement, sighting(storage=files), after, EXISTING, PROFILE)
        self.assertIs(observed.tables[0].work, Work.SCAN)
        self.assertIn("copied no file", observed.findings[-1].message)
        self.assertIn("a rewrite", observed.findings[-1].message)

    def test_a_predicted_index_build_with_no_index_is_a_mismatch(self):
        statement = predicted("CREATE INDEX ix ON orders (c)")
        files = {ORDERS: {101: table_file(1)}}
        after = sighting({ORDERS: {"SHARE"}}, files)
        observed = observe(statement, sighting(storage=files), after, EXISTING, PROFILE)
        self.assertIn("an index build", observed.findings[-1].message)

    def test_an_unexpected_index_build_is_a_mismatch(self):
        statement = predicted("ALTER TABLE orders ALTER COLUMN c DROP NOT NULL")
        before = sighting(storage={ORDERS: {101: table_file(1), 102: index_file(2)}})
        after = sighting(
            {ORDERS: {"ACCESS EXCLUSIVE"}},
            {ORDERS: {101: table_file(1), 102: index_file(3)}},
        )
        observed = observe(statement, before, after, EXISTING, PROFILE)
        self.assertIs(observed.tables[0].work, Work.INDEX_BUILD)
        self.assertIn("built an index", observed.findings[-1].message)

    def test_an_emptied_file_proves_nothing(self):
        statement = predicted("TRUNCATE orders")
        before = sighting(storage={ORDERS: {101: table_file(1)}})
        after = sighting(
            {ORDERS: {"ACCESS EXCLUSIVE"}}, {ORDERS: {101: table_file(2, size=0)}}
        )
        observed = observe(statement, before, after, EXISTING, PROFILE)
        self.assertIs(observed.tables[0].work, Work.CATALOG)
        self.assertNotIn("impact.mismatch", [f.rule for f in observed.findings])

    def test_files_not_read_leave_the_predicted_work(self):
        statement = predicted("ALTER TABLE orders ALTER COLUMN c TYPE bigint")
        after = sighting({ORDERS: {"ACCESS EXCLUSIVE"}}, read=("locks",))
        observed = observe(
            statement, sighting(read=("locks",)), after, EXISTING, PROFILE
        )
        self.assertIs(observed.tables[0].work, Work.REWRITE)

    def test_an_unpredicted_table_lock_is_a_mismatch(self):
        statement = predicted("DROP TABLE orders")
        after = sighting({PARTS: {"ACCESS EXCLUSIVE"}})
        observed = observe(statement, sighting(), after, EXISTING, PROFILE)
        parts = observed.tables[-1]
        self.assertEqual(
            parts,
            TableImpact(
                "parts",
                "ACCESS EXCLUSIVE",
                Blocks.READS_AND_WRITES,
                Work.CATALOG,
                Hold.BRIEF,
            ),
        )
        self.assertIn("took ACCESS EXCLUSIVE on parts", observed.findings[-1].message)

    def test_a_weak_unpredicted_lock_is_left_out(self):
        statement = predicted("DROP TABLE orders")
        after = sighting({PARTS: {"ROW SHARE", "ACCESS SHARE"}})
        observed = observe(statement, sighting(), after, EXISTING, PROFILE)
        self.assertEqual([t.table for t in observed.tables], ["orders"])

    def test_a_table_the_run_created_is_left_as_predicted(self):
        statement = predicted("CREATE INDEX ix ON fresh (c)")
        after = sighting({NEW: {"ACCESS EXCLUSIVE", "SHARE"}})
        observed = observe(statement, sighting(), after, EXISTING, PROFILE)
        self.assertEqual(observed.tables, statement.tables)
        self.assertEqual(observed.findings, statement.findings)

    def test_an_unnamed_table_is_left_as_predicted(self):
        statement = predicted("DROP INDEX ix")
        observed = observe(statement, sighting(), sighting(), EXISTING, PROFILE)
        self.assertEqual(observed.tables, statement.tables)

    def test_locks_not_read_leave_the_statement_unchanged(self):
        statement = predicted("CREATE INDEX ix ON orders (c)")
        observed = observe(statement, sighting(read=()), sighting(), EXISTING, PROFILE)
        self.assertEqual(observed, statement)

    def test_without_the_existing_tables_every_table_is_compared(self):
        statement = predicted("CREATE INDEX ix ON fresh (c)")
        after = sighting({NEW: {"ACCESS EXCLUSIVE"}})
        observed = observe(statement, sighting(), after, None, PROFILE)
        self.assertEqual(observed.tables[0].lock, "ACCESS EXCLUSIVE")


class WithObservationsTestCase(unittest.TestCase):
    def test_the_windows_are_read_from_the_observed_facts(self):
        from sustained.analysis import MigrationStatement

        statements = [
            MigrationStatement("CREATE INDEX ix ON orders (c)", "001", True),
            MigrationStatement("UPDATE parts SET c = 0", "001", True),
        ]
        report = analyze(statements, PG)
        after = sighting({ORDERS: {"ACCESS EXCLUSIVE"}})
        observed = with_observations(
            report, {("001", 0): (sighting(), after)}, EXISTING, PROFILE
        )
        self.assertIs(observed.evidence, Evidence.OBSERVED)
        (migration,) = observed.migrations
        self.assertIs(migration.statements[0].evidence, Evidence.OBSERVED)
        self.assertIs(migration.statements[1].evidence, Evidence.STATIC)
        window = next(w for w in migration.windows if w.table == "orders")
        self.assertIs(window.blocks, Blocks.READS_AND_WRITES)

    def test_no_observation_keeps_the_evidence(self):
        report = analyze(["CREATE INDEX ix ON orders (c)"], PG)
        observed = with_observations(report, {}, EXISTING, PROFILE)
        self.assertIs(observed.evidence, Evidence.STATIC)


class RehearseTraceTestCase(unittest.TestCase):
    """
    rehearse(trace=True) on SQLite, with the Postgres profile and the
    trace standing in. SQLite refuses the catalog reads, so every
    statement keeps its prediction; the Postgres suite observes real
    locks.
    """

    def migrator(self, connection, migrations):
        return Migrator(connection, migrations, dialect=Dialects.DEFAULT)

    def stand_in(self):
        patches = [
            mock.patch(
                "sustained.impact.rules._profiles",
                return_value={"DEFAULT": (PROFILE,)},
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_statement_is_read_after_the_ones_before_it(self):
        self.stand_in()
        migrator = self.migrator(sqlite3.connect(":memory:"), [])
        tracer = Tracer(migrator)
        tracer.ran.append(MigrationStatement("CREATE INDEX ix ON orders (c)", "001"))
        dropped = MigrationStatement("DROP INDEX ix", "002")
        self.assertEqual(tracer.tables(dropped), ["orders"])

    def test_refuses_a_dialect_it_cannot_observe(self):
        connection = sqlite3.connect(":memory:")
        migrator = self.migrator(connection, [Migration("001", up="SELECT 1")])
        with self.assertRaises(DialectError) as caught:
            migrator.rehearse(trace=True)
        self.assertIn("POSTGRES", str(caught.exception))

    def test_without_trace_the_result_has_no_impact(self):
        connection = sqlite3.connect(":memory:")
        migrator = self.migrator(connection, [Migration("001", up="SELECT 1")])
        self.assertIsNone(migrator.rehearse().impact)

    def test_a_traced_rehearsal_reports_the_run(self):
        self.stand_in()
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE orders (id INTEGER, c INTEGER)")
        connection.commit()
        seen = []

        def callable_step(conn):
            seen.append(conn)

        migrator = self.migrator(
            connection,
            [
                Migration(
                    "001",
                    up=["CREATE INDEX ix ON orders (c)", "UPDATE orders SET c = 0"],
                    down="DROP INDEX ix",
                ),
                Migration("002", up=callable_step, down=None),
                Migration(
                    "003",
                    up="CREATE INDEX CONCURRENTLY ix2 ON orders (c)",
                    down=None,
                    transactional=False,
                ),
            ],
        )
        results = migrator.rehearse(trace=True)
        self.assertTrue(results.ok)
        self.assertEqual(len(seen), 1)
        report = results.impact
        self.assertEqual([m.migration_id for m in report.migrations], ["001", "003"])
        self.assertEqual(len(report.migrations[0].statements), 2)
        # SQLite refused every read, so nothing was observed.
        self.assertIs(report.evidence, Evidence.CATALOG)
        rows = connection.execute("SELECT name FROM sqlite_master").fetchall()
        self.assertNotIn(("ix",), rows)
        self.assertIsNone(migrator._tracer)

    def test_a_failed_statement_stops_the_traced_run(self):
        self.stand_in()
        connection = sqlite3.connect(":memory:")
        migrator = self.migrator(
            connection,
            [Migration("001", up=["CREATE TABLE a (id INTEGER)", "CRATE oops"])],
        )
        results = migrator.rehearse(trace=True)
        self.assertFalse(results.ok)
        self.assertEqual(results.impact.migrations[0].migration_id, "001")
        self.assertIsNone(migrator._tracer)


if __name__ == "__main__":
    unittest.main()
