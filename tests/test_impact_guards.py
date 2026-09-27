"""
Tests for the guards that read statement impact, the impact the
migrator attaches before the guards run, and the danger findings up()
prints when no guard reads impact.
"""

import asyncio
import io
import sqlite3
import unittest
from contextlib import redirect_stderr
from unittest import mock

from sustained.aio_migrations import AsyncMigrator
from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.exceptions import GuardBlocked
from sustained.guards import (
    BLOCK,
    Verdict,
    lock_timeout_required,
    max_blocking,
    max_statements,
    no_drops,
    no_lock_without_timeout,
    no_rewrite,
    no_unknown_impact,
    reads_impact,
    run_guards,
    statement_impacts,
)
from sustained.impact import Blocks, EngineContext, TableStats, attach_impact
from sustained.migrations import Migration, Migrator
from tests.test_impact_context import (
    INDEX,
    RESTORE_SQL,
    TIMEOUT_SQL,
    ScriptedAdapter,
    ScriptedConnection,
)

PG = Dialects.POSTGRES

ADD = "ALTER TABLE orders ADD COLUMN note text"
RETYPE = "ALTER TABLE orders ALTER COLUMN note TYPE integer"
GRANT = "GRANT SELECT ON orders TO app"
TIMEOUT = "SET lock_timeout = '5s'"


def context(rows, size):
    return EngineContext(
        "postgres",
        (16, 4),
        tables={"orders": TableStats(rows, size)},
        read=frozenset({"version", "sizes"}),
    )


def attached(statements, facts):
    tagged = [MigrationStatement(s, "001_orders") for s in statements]
    return attach_impact(tagged, PG, facts)


def flagged(guard, statements, dialect=PG):
    return [v.statement for v in run_guards([guard], statements, dialect)]


class MaxBlockingTestCase(unittest.TestCase):
    def test_blocks_a_lock_worse_than_the_limit(self):
        self.assertEqual(flagged(max_blocking("writes"), [ADD, INDEX]), [ADD])
        self.assertEqual(flagged(max_blocking("ddl"), [ADD, INDEX]), [ADD, INDEX])

    def test_takes_a_blocks_member(self):
        self.assertEqual(flagged(max_blocking(Blocks.READS_AND_WRITES), [ADD]), [])

    def test_names_the_limit_and_thresholds_in_the_verdict(self):
        verdicts = run_guards([max_blocking("writes", over_rows=10)], [ADD], PG)
        self.assertEqual(
            verdicts, [Verdict("max_blocking(writes, over_rows=10)", BLOCK, ADD)]
        )

    def test_an_unknown_size_counts_as_over_the_threshold(self):
        self.assertEqual(flagged(max_blocking("writes", over_rows=10), [ADD]), [ADD])

    def test_assume_small_passes_an_unknown_size(self):
        guard = max_blocking("writes", over_rows=10, assume_small=True)
        self.assertEqual(flagged(guard, [ADD]), [])

    def test_reads_the_size_the_migrator_attached(self):
        guard = max_blocking("writes", over_rows=100_000)
        self.assertEqual(flagged(guard, attached([ADD], context(50, 8192))), [])
        large = attached([ADD], context(2_000_000, 3 << 30))
        self.assertEqual(flagged(guard, large), [ADD])

    def test_either_threshold_passed_counts(self):
        guard = max_blocking("writes", over_rows=100_000, over_bytes=1 << 20)
        self.assertEqual(flagged(guard, attached([ADD], context(50, 1 << 30))), [ADD])

    def test_a_table_created_in_the_run_never_counts(self):
        create = "CREATE TABLE orders (id int)"
        self.assertEqual(flagged(max_blocking("nothing"), [create, INDEX]), [])

    def test_refuses_an_unknown_limit_and_a_negative_threshold(self):
        with self.assertRaises(ValueError):
            max_blocking("everything")
        with self.assertRaises(ValueError):
            max_blocking("writes", over_rows=-1)


class NoRewriteTestCase(unittest.TestCase):
    def test_blocks_a_rewrite_and_passes_catalog_work(self):
        self.assertEqual(flagged(no_rewrite(), [ADD, RETYPE]), [RETYPE])

    def test_reads_the_thresholds(self):
        guard = no_rewrite(over_bytes=1 << 30)
        self.assertEqual(flagged(guard, attached([RETYPE], context(5, 8192))), [])
        self.assertEqual(flagged(guard, [RETYPE]), [RETYPE])
        self.assertEqual(
            flagged(no_rewrite(over_bytes=1 << 30, assume_small=True), [RETYPE]), []
        )


class LockTimeoutRequiredTestCase(unittest.TestCase):
    def test_blocks_a_queueing_lock_with_no_timeout(self):
        self.assertEqual(flagged(lock_timeout_required(), [INDEX]), [INDEX])

    def test_a_timeout_before_the_statement_covers_it(self):
        self.assertEqual(flagged(lock_timeout_required(), [TIMEOUT, INDEX]), [])

    def test_a_timeout_on_the_connection_covers_the_run(self):
        facts = EngineContext(
            "postgres",
            (16, 4),
            settings={"lock_timeout": "5s"},
            read=frozenset({"settings"}),
        )
        self.assertEqual(flagged(lock_timeout_required(), attached([ADD], facts)), [])


class NoUnknownImpactTestCase(unittest.TestCase):
    def test_blocks_a_statement_the_analysis_cannot_read(self):
        self.assertEqual(flagged(no_unknown_impact(), [GRANT, ADD]), [GRANT])

    def test_every_impact_rule_blocks_an_unknown_statement(self):
        # A DO block, a data-modifying CTE, and two statements in one
        # string name no table, and each may lock or rewrite any table.
        unknown = [
            "DO $$ BEGIN ALTER TABLE orders ALTER COLUMN note TYPE int; END $$",
            "WITH moved AS (DELETE FROM orders RETURNING *) "
            "INSERT INTO archive SELECT * FROM moved",
            "ALTER TABLE orders ADD COLUMN a int; "
            "ALTER TABLE orders ALTER COLUMN note TYPE int",
            GRANT,
        ]
        for guard in (
            max_blocking("reads_and_writes"),
            max_blocking("nothing", over_rows=10**9, assume_small=True),
            no_rewrite(over_rows=10**9, assume_small=True),
            lock_timeout_required(),
            no_unknown_impact(),
        ):
            with self.subTest(guard=guard):
                self.assertEqual(flagged(guard, unknown), unknown)

    def test_a_statement_read_to_its_end_is_not_unknown(self):
        self.assertEqual(flagged(no_unknown_impact(), [TIMEOUT, ADD]), [])


class ImpactRuleTestCase(unittest.TestCase):
    def test_the_impact_rules_are_marked_and_the_textual_ones_are_not(self):
        for guard in (
            max_blocking("writes"),
            no_rewrite(),
            lock_timeout_required(),
            no_unknown_impact(),
        ):
            self.assertTrue(reads_impact(guard))
        self.assertFalse(reads_impact(no_drops()))
        self.assertFalse(reads_impact(max_statements(5)))

    def test_silent_on_a_dialect_without_rules(self):
        for guard in (max_blocking("nothing"), no_rewrite(), no_unknown_impact()):
            self.assertEqual(flagged(guard, [RETYPE, GRANT], Dialects.PRESTO), [])

    def test_an_attached_impact_is_read_before_a_static_one(self):
        statements = attached([ADD], context(5, 8192))
        mixed = statements + [MigrationStatement(RETYPE, "001_orders")]
        impacts = statement_impacts(mixed, PG)
        self.assertIs(impacts[0], statements[0].impact)
        self.assertEqual(impacts[1].tables[0].rows, None)

    def test_a_statement_wrapped_again_keeps_its_impact(self):
        statement = attached([ADD], context(5, 8192))[0]
        self.assertIs(MigrationStatement(statement).impact, statement.impact)
        self.assertEqual(MigrationStatement(statement), ADD)

    def test_attaching_impact_keeps_each_statement_in_its_migration(self):
        run = [
            MigrationStatement("SET LOCAL lock_timeout = '1s'", "001"),
            MigrationStatement("ALTER TABLE a ADD COLUMN x int", "001"),
            MigrationStatement("ALTER TABLE b ADD COLUMN y int", "002", False),
        ]
        statements = attach_impact(run, PG)
        self.assertEqual([s.migration_id for s in statements], ["001", "001", "002"])
        self.assertFalse(statements[2].transactional)
        self.assertEqual(
            flagged(no_lock_without_timeout(), statements),
            ["ALTER TABLE b ADD COLUMN y int"],
        )


def index_run():
    return [Migration("001_orders", up=[INDEX])]


class MigratorImpactGuardTestCase(unittest.TestCase):
    """up() reads the server facts and attaches them before the guards."""

    def test_an_impact_guard_blocks_on_the_size_read_from_the_server(self):
        connection = ScriptedConnection()
        migrator = Migrator(
            connection,
            index_run(),
            dialect=PG,
            guards=[max_blocking("ddl", over_rows=1_000_000)],
        )
        with self.assertRaises(GuardBlocked):
            migrator.up()
        self.assertNotIn(INDEX, connection.log)

    def test_prints_danger_findings_when_no_guard_reads_impact(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            Migrator(ScriptedConnection(), index_run(), dialect=PG).up()
        self.assertIn(f"danger: pg.create_index  {INDEX}: ", stderr.getvalue())

    def logs(self, run, guards=(), refuse=None):
        """The statements a blocking and an async up() of the run sent."""
        connection = ScriptedConnection(refuse)
        adapter = ScriptedAdapter(refuse)
        for migrator in (
            Migrator(connection, run, dialect=PG, guards=list(guards)),
            AsyncMigrator(adapter, run, dialect=PG, guards=list(guards)),
        ):
            with redirect_stderr(io.StringIO()):
                result = migrator.up()
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
        return connection.log, adapter.log

    def test_the_size_read_names_only_the_tables_of_the_run(self):
        run = index_run() + [Migration("002_items", up="ALTER TABLE items ADD c int")]
        spelled = [
            f"convert_from(decode('{name.encode().hex()}', 'hex'), 'UTF8')"
            for name in ("items", "orders")
        ]
        for log in self.logs(run):
            (sizes,) = [sql for sql in log if "pg_total_relation_size" in sql]
            self.assertTrue(
                sizes.endswith(f"AND lower(c.relname) IN ({', '.join(spelled)})")
            )

    def test_the_size_read_runs_under_a_lock_timeout_it_puts_back(self):
        for log in self.logs(index_run()):
            timed = log.index(TIMEOUT_SQL)
            (sizes,) = [
                i for i, sql in enumerate(log) if "pg_total_relation_size" in sql
            ]
            self.assertLess(timed, sizes)
            self.assertEqual(log[sizes + 1 :].count(RESTORE_SQL), 1)
            self.assertLess(sizes, log.index(RESTORE_SQL))

    def test_a_size_the_read_cannot_read_counts_as_over_the_threshold(self):
        guard = max_blocking("ddl", over_rows=3_000_000)
        self.logs(index_run(), [guard])
        with self.assertRaises(GuardBlocked):
            self.logs(index_run(), [guard], refuse="pg_total_relation_size")
        with self.assertRaises(GuardBlocked):
            migrator = AsyncMigrator(
                ScriptedAdapter("pg_total_relation_size"),
                index_run(),
                dialect=PG,
                guards=[guard],
            )
            asyncio.run(migrator.up())

    def test_the_generated_migration_is_read_after_the_registered_apply(self):
        from sustained.aio import DbApiAsyncAdapter
        from sustained.impact import context as impact_context
        from sustained.model import Model
        from sustained.schema import Integer

        widget = type(
            "Widget",
            (Model,),
            {
                "tableName": "widgets",
                "tableColumns": {"id": Integer(primary_key=True)},
                "_dialect": Dialects.DEFAULT,
            },
        )
        guard = max_blocking("reads_and_writes")

        def run(asynchronous):
            connection = sqlite3.connect(":memory:", check_same_thread=False)
            self.addCleanup(connection.close)
            reads = []

            def seen(statements):
                tables = connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name = 't'"
                ).fetchall()
                reads.append(([str(s) for s in statements], bool(tables)))

            def blocking(conn, dialect, exact_counts=False, statements=None):
                seen(statements)
                return real(conn, dialect, exact_counts, statements)

            async def awaited(adapter, dialect, exact_counts=False, statements=None):
                seen(statements)
                return await real_async(adapter, dialect, exact_counts, statements)

            migrations = [Migration("001_t", up="CREATE TABLE t (id INTEGER)")]
            with (
                mock.patch("sustained.impact.read_context", blocking),
                mock.patch("sustained.impact.async_read_context", awaited),
                redirect_stderr(io.StringIO()),
            ):
                if asynchronous:
                    migrator = AsyncMigrator(
                        DbApiAsyncAdapter(connection),
                        migrations,
                        dialect=Dialects.DEFAULT,
                        guards=[guard],
                    )
                    asyncio.run(migrator.up(models=[widget], unrehearsed=True))
                else:
                    Migrator(
                        connection, migrations, dialect=Dialects.DEFAULT, guards=[guard]
                    ).up(models=[widget], unrehearsed=True)
            return reads

        real = impact_context.read_context
        real_async = impact_context.async_read_context
        reads = run(False)
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0], (["CREATE TABLE t (id INTEGER)"], False))
        generated, applied = reads[1]
        self.assertTrue(generated and all("widgets" in s for s in generated))
        self.assertTrue(applied)
        self.assertEqual(run(True), reads)

    def test_prints_no_danger_findings_when_a_guard_reads_impact(self):
        stderr = io.StringIO()
        migrator = Migrator(
            ScriptedConnection(), index_run(), dialect=PG, guards=[no_unknown_impact()]
        )
        with redirect_stderr(stderr):
            migrator.up()
        self.assertNotIn("danger:", stderr.getvalue())

    def test_a_custom_guard_reads_the_attached_impact(self):
        seen = []

        def guard(statements, dialect):
            seen.extend(s.impact for s in statements)
            return []

        migrator = Migrator(
            ScriptedConnection(), index_run(), dialect=PG, guards=[guard]
        )
        with redirect_stderr(io.StringIO()):
            migrator.up()
        self.assertEqual(seen[0].tables[0].rows, 2_000_000)

    def test_the_async_migrator_prints_the_same_lines(self):
        sync_err, async_err = io.StringIO(), io.StringIO()
        with redirect_stderr(sync_err):
            Migrator(ScriptedConnection(), index_run(), dialect=PG).up()
        with redirect_stderr(async_err):
            asyncio.run(AsyncMigrator(ScriptedAdapter(), index_run(), dialect=PG).up())
        self.assertEqual(sync_err.getvalue(), async_err.getvalue())
        self.assertIn("danger:", async_err.getvalue())

    def test_the_async_migrator_blocks_the_same_run(self):
        migrator = AsyncMigrator(
            ScriptedAdapter(),
            index_run(),
            dialect=PG,
            guards=[max_blocking("ddl", over_rows=1_000_000)],
        )
        with self.assertRaises(GuardBlocked):
            asyncio.run(migrator.up())

    @mock.patch("sustained.impact.rules._profiles", return_value={})
    def test_a_dialect_without_rules_reads_no_context(self, _):
        connection = sqlite3.connect(":memory:")
        stderr = io.StringIO()
        migrator = Migrator(
            connection,
            [Migration("001_t", up="CREATE TABLE t (id INTEGER)")],
            dialect=Dialects.DEFAULT,
            guards=[max_blocking("nothing")],
        )
        with redirect_stderr(stderr):
            self.assertEqual(migrator.up(), ["001_t"])
        self.assertEqual(stderr.getvalue(), "")


class PlanVerdictsTestCase(unittest.TestCase):
    """plan attaches the impact it read before the guards run."""

    def test_the_guards_read_the_context_plan_read(self):
        from types import SimpleNamespace

        from sustained.analysis import PendingSummary
        from sustained.cli.plan import _plan_verdicts

        config = SimpleNamespace(guards=[max_blocking("writes", over_rows=100)])
        summary = PendingSummary("001_orders", "pending", False, [ADD], [])
        small = _plan_verdicts(config, [summary], None, PG, context(5, 8192))
        self.assertEqual(small, {})
        unread = _plan_verdicts(config, [summary], None, PG)
        self.assertEqual(list(unread), [ADD])


if __name__ == "__main__":
    unittest.main()
