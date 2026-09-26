"""Tests for analyze(): run state, severity, windows, and intents."""

import unittest

from sustained.analysis import MigrationStatement, with_intent
from sustained.dialects import Dialects
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    Evidence,
    Hold,
    Severity,
    TableStats,
    Thresholds,
    Work,
    analyze,
    attach_impact,
    supported,
)
from sustained.impact.analyzer import intent_agrees
from sustained.impact.model import Intent, ParsedStatement
from sustained.impact.recognizer import recognize
from sustained.impact.state import RunState, TimeoutScope, sets_a_timeout

PG = Dialects.POSTGRES


def m(sql, migration_id="m1", transactional=True):
    return MigrationStatement(sql, migration_id, transactional)


def rules(statement):
    return [f.rule for f in statement.findings]


def sized(rows=None, size=None, **extra):
    tables = {"orders": TableStats(rows, size)}
    tables.update(extra)
    return EngineContext("postgres", (16,), tables=tables, read=frozenset({"sizes"}))


class ReportTestCase(unittest.TestCase):
    def test_statements_group_by_migration(self):
        report = analyze(
            [
                m("SET lock_timeout = '1s'"),
                m("DROP TABLE a"),
                m("DROP TABLE b", "m2", False),
            ],
            PG,
        )
        self.assertEqual(report.profile, "postgres")
        self.assertEqual(report.version, (12,))
        self.assertEqual(report.evidence, Evidence.STATIC)
        self.assertEqual(
            [
                (x.migration_id, x.transactional, len(x.statements))
                for x in report.migrations
            ],
            [("m1", True, 2), ("m2", False, 1)],
        )
        self.assertEqual(len(report.statements), 3)

    def test_plain_strings_form_one_unnamed_migration(self):
        report = analyze(["DROP TABLE a", "DROP TABLE b"], PG)
        (migration,) = report.migrations
        self.assertIsNone(migration.migration_id)
        self.assertTrue(migration.transactional)

    def test_a_read_context_raises_the_evidence(self):
        report = analyze(["DROP TABLE a"], PG, sized(1))
        self.assertEqual(report.evidence, Evidence.CATALOG)
        self.assertEqual(report.statements[0].evidence, Evidence.CATALOG)
        self.assertEqual(report.version, (16,))

    def test_counts(self):
        report = analyze(["CREATE INDEX ix ON orders (c)"], PG)
        self.assertEqual(report.count(Severity.WARN), 2)
        self.assertEqual(report.count(Severity.DANGER), 0)
        self.assertEqual(report.statements[0].severity, Severity.WARN)

    def test_a_dialect_without_rules_is_refused(self):
        self.assertTrue(supported(PG))
        self.assertFalse(supported(Dialects.MSSQL))
        with self.assertRaises(ValueError):
            analyze(["DROP TABLE a"], Dialects.MSSQL)


class SeverityTestCase(unittest.TestCase):
    SQL = "CREATE INDEX ix ON orders (c)"

    def finding(self, context=None, thresholds=Thresholds()):
        (statement,) = analyze([self.SQL], PG, context, thresholds).statements
        return next(f for f in statement.findings if f.rule == "pg.create_index")

    def test_unknown_size_is_a_warning_that_says_so(self):
        finding = self.finding()
        self.assertEqual(finding.severity, Severity.WARN)
        self.assertIn("the size of orders is unknown", finding.message)

    def test_a_large_table_is_danger(self):
        self.assertEqual(self.finding(sized(rows=5_000_000)).severity, Severity.DANGER)
        self.assertEqual(self.finding(sized(size=2 << 30)).severity, Severity.DANGER)

    def test_a_small_table_is_info(self):
        finding = self.finding(sized(rows=10, size=8192))
        self.assertEqual(finding.severity, Severity.INFO)
        self.assertNotIn("unknown", finding.message)

    def test_thresholds_are_configurable(self):
        context = sized(rows=500)
        self.assertEqual(
            self.finding(context, Thresholds(rows=100)).severity, Severity.DANGER
        )

    def test_the_table_carries_its_size(self):
        (statement,) = analyze([self.SQL], PG, sized(rows=10, size=8192)).statements
        self.assertEqual(
            (statement.tables[0].rows, statement.tables[0].bytes), (10, 8192)
        )

    def test_catalog_work_draws_no_size_finding(self):
        (statement,) = analyze(["ALTER TABLE orders ADD COLUMN c int"], PG).statements
        self.assertEqual(rules(statement), ["pg.lock_timeout"])

    def test_default_wording_for_blocking_work_without_a_message(self):
        (statement,) = analyze(["VACUUM FULL orders"], PG).statements
        self.assertIn(
            "reads and writes on orders wait while the table and its indexes are "
            "rewritten",
            statement.findings[0].message,
        )
        (statement,) = analyze(
            ["ALTER TABLE orders ADD COLUMN c int UNIQUE"], PG
        ).statements
        self.assertIn("for the whole index build", statement.findings[0].message)

    def test_blocking_catalog_work_with_advice_is_info(self):
        (statement,) = analyze(["DROP INDEX ix"], PG).statements
        self.assertEqual(statement.findings[0].severity, Severity.INFO)
        self.assertEqual(statement.findings[0].remedy, ("DROP INDEX CONCURRENTLY ix",))


class HoldTestCase(unittest.TestCase):
    def holds(self, statements):
        report = analyze(statements, PG)
        return [t.hold for s in report.statements for t in s.tables]

    def test_a_lock_is_held_to_the_commit_of_its_transaction(self):
        self.assertEqual(
            self.holds(
                [m("ALTER TABLE a ADD COLUMN c int"), m("CREATE INDEX ix ON b (c)")]
            ),
            [Hold.TRANSACTION, Hold.STATEMENT],
        )

    def test_a_statement_alone_holds_by_its_work(self):
        self.assertEqual(
            self.holds([m("ALTER TABLE a ADD COLUMN c int")]), [Hold.BRIEF]
        )

    def test_outside_a_transaction_locks_end_with_their_statement(self):
        self.assertEqual(
            self.holds(
                [
                    m("ALTER TABLE a ADD COLUMN c int", transactional=False),
                    m("CREATE INDEX CONCURRENTLY ix ON b (c)", transactional=False),
                ]
            ),
            [Hold.BRIEF, Hold.STATEMENT],
        )


class RunStateTestCase(unittest.TestCase):
    def test_a_table_created_in_the_run_blocks_nothing(self):
        report = analyze(
            [
                m("CREATE TABLE orders (id int, c int)"),
                m("CREATE INDEX ix ON orders (c)"),
                m("ALTER TABLE orders ALTER COLUMN c SET NOT NULL", "m2"),
                m("UPDATE orders SET c = 1", "m2"),
            ],
            PG,
            sized(rows=5_000_000),
        )
        for statement in report.statements[1:]:
            with self.subTest(statement=statement.statement):
                (found,) = statement.tables
                self.assertEqual(found.blocks, Blocks.NOTHING)
                self.assertEqual(found.work, Work.CATALOG)
                self.assertEqual((found.rows, found.bytes), (0, 0))
                self.assertEqual(statement.findings, ())
        self.assertEqual(report.migrations[1].windows, ())

    def test_a_dropped_table_is_no_longer_new(self):
        report = analyze(
            [
                m("CREATE TABLE a (id int)"),
                m("DROP TABLE a"),
                m("CREATE INDEX ix ON a (id)"),
            ],
            PG,
        )
        self.assertEqual(report.statements[2].tables[0].blocks, Blocks.WRITES)

    def test_a_renamed_table_keeps_the_size_of_the_table_it_was(self):
        report = analyze(
            [
                m("ALTER TABLE orders RENAME TO purchases"),
                m("CREATE INDEX ix ON purchases (c)"),
            ],
            PG,
            sized(rows=5_000_000),
        )
        index = report.statements[1]
        self.assertEqual(index.tables[0].rows, 5_000_000)
        self.assertEqual(index.severity, Severity.DANGER)

    def test_a_renamed_new_table_stays_new(self):
        report = analyze(
            [
                m("CREATE TABLE app.a (id int)"),
                m("ALTER TABLE app.a RENAME TO b"),
                m("CREATE INDEX ix ON app.b (id)"),
            ],
            PG,
        )
        self.assertEqual(report.statements[2].tables[0].blocks, Blocks.NOTHING)

    def test_state_records_the_statements_it_reads(self):
        state = RunState("lock_timeout")
        for sql in [
            "CREATE TABLE a (id int)",
            "CREATE INDEX ix ON a (id)",
            "ALTER TABLE a RENAME TO b",
            "RENAME TABLE x TO y",
            "SET foreign_key_checks = 0",
        ]:
            dialect = Dialects.MYSQL if sql.startswith(("RENAME", "SET f")) else PG
            state.record(recognize(sql, dialect), True)
        self.assertTrue(state.is_new("B"))
        self.assertFalse(state.is_new("a"))
        self.assertEqual(state.index_table("IX"), "a")
        self.assertIsNone(state.index_table("other"))
        self.assertEqual(state.original("y"), "x")
        self.assertEqual(state.original("b"), "a")
        self.assertEqual(state.original("z"), "z")
        self.assertEqual(state.settings["foreign_key_checks"], "0")


class TimeoutTestCase(unittest.TestCase):
    def covered(self, statements):
        """Whether each lock-taking statement is covered by a timeout."""
        report = analyze(statements, PG)
        return [
            "pg.lock_timeout" not in rules(s) for s in report.statements if s.tables
        ]

    def test_a_session_timeout_covers_the_rest_of_the_run(self):
        self.assertEqual(
            self.covered(
                [
                    m("DROP TABLE a"),
                    m("SET lock_timeout = '2s'"),
                    m("DROP TABLE b"),
                    m("DROP TABLE c", "m2"),
                ]
            ),
            [False, True, True],
        )

    def test_a_local_timeout_ends_with_its_migration(self):
        self.assertEqual(
            self.covered(
                [
                    m("SET LOCAL lock_timeout = '2s'"),
                    m("DROP TABLE b"),
                    m("DROP TABLE c", "m2"),
                ]
            ),
            [True, False],
        )

    def test_a_local_timeout_outside_a_transaction_does_nothing(self):
        self.assertEqual(
            self.covered(
                [
                    m("SET LOCAL lock_timeout = '2s'", transactional=False),
                    m("DROP TABLE b", transactional=False),
                ]
            ),
            [False],
        )

    def test_a_zero_timeout_turns_it_off(self):
        self.assertEqual(
            self.covered(
                [
                    m("SET lock_timeout = '2s'"),
                    m("SET lock_timeout = 0"),
                    m("DROP TABLE b"),
                ]
            ),
            [False],
        )

    def test_the_remedy_fits_the_migration(self):
        (statement,) = analyze([m("DROP TABLE a")], PG).statements
        self.assertEqual(
            statement.findings[-1].remedy, ("SET LOCAL lock_timeout = '5s'",)
        )
        (statement,) = analyze([m("DROP TABLE a", transactional=False)], PG).statements
        self.assertEqual(statement.findings[-1].remedy, ("SET lock_timeout = '5s'",))

    def test_a_lock_that_blocks_only_ddl_needs_no_timeout(self):
        self.assertEqual(
            self.covered([m("CREATE INDEX CONCURRENTLY ix ON a (c)")]), [True]
        )
        self.assertEqual(self.covered([m("UPDATE a SET c = 1")]), [True])

    def test_timeout_values(self):
        for value in ["0", "0ms", " 0.0 s", "DEFAULT", "0min"]:
            self.assertFalse(sets_a_timeout(value), value)
        for value in ["5s", "100", "1min"]:
            self.assertTrue(sets_a_timeout(value), value)

    def test_the_scope_on_its_own(self):
        scope = TimeoutScope()
        scope.enter("m1")
        scope.set("local", transactional=True)
        self.assertTrue(scope.covered)
        scope.enter("m1")
        self.assertTrue(scope.covered)
        scope.enter("m2")
        self.assertFalse(scope.covered)
        scope.set("session", transactional=False)
        scope.enter("m3")
        self.assertTrue(scope.covered)
        scope.set("session", transactional=False, enabled=False)
        self.assertFalse(scope.covered)


class WindowTestCase(unittest.TestCase):
    def test_the_diffs_not_null_flow_holds_the_table_across_the_backfill(self):
        report = analyze(
            [
                m("ALTER TABLE orders ADD COLUMN c int"),
                with_intent(
                    m("UPDATE orders SET c = 0 WHERE c IS NULL"),
                    "backfill",
                    "orders",
                    "c",
                ),
                m("ALTER TABLE orders ALTER COLUMN c SET NOT NULL"),
            ],
            PG,
        )
        (migration,) = report.migrations
        (window,) = migration.windows
        self.assertEqual(window.table, "orders")
        self.assertEqual(window.blocks, Blocks.READS_AND_WRITES)
        self.assertEqual(
            (window.taken_by, window.heaviest, window.during), (1, Work.ROWS, 2)
        )
        (finding,) = migration.findings
        self.assertEqual(finding.rule, "window.held")
        self.assertIn("from statement 1", finding.message)
        self.assertIn("rows work of statement 2", finding.message)
        self.assertEqual(
            [(lock.table, lock.lock, lock.statement) for lock in migration.locks],
            [
                ("orders", "ACCESS EXCLUSIVE", 1),
                ("orders", "ROW EXCLUSIVE", 2),
                ("orders", "ACCESS EXCLUSIVE", 3),
            ],
        )

    def test_heavy_work_in_the_statement_that_takes_the_lock_draws_no_window_finding(
        self,
    ):
        report = analyze(
            [
                m("CREATE INDEX ix ON orders (c)"),
                m("ALTER TABLE orders ADD COLUMN d int"),
            ],
            PG,
        )
        (migration,) = report.migrations
        (window,) = migration.windows
        self.assertEqual((window.blocks, window.taken_by), (Blocks.READS_AND_WRITES, 2))
        self.assertEqual((window.heaviest, window.during), (Work.INDEX_BUILD, 1))
        self.assertEqual(migration.findings, ())

    def test_outside_a_transaction_each_statement_is_its_own_window(self):
        report = analyze(
            [
                m("ALTER TABLE orders ADD COLUMN c int", transactional=False),
                m("UPDATE orders SET c = 0", transactional=False),
            ],
            PG,
        )
        (migration,) = report.migrations
        self.assertEqual(len(migration.windows), 2)
        self.assertEqual(migration.findings, ())

    def test_two_exclusive_tables_risk_a_deadlock(self):
        report = analyze(
            [m("ALTER TABLE a ADD COLUMN c int"), m("ALTER TABLE b ADD COLUMN c int")],
            PG,
        )
        (finding,) = report.migrations[0].findings
        self.assertEqual(finding.rule, "window.lock_order")
        self.assertIn("a, b", finding.message)

    def test_an_unknown_statement_counts_as_unknown_work(self):
        report = analyze(
            [
                m("ALTER TABLE orders ADD COLUMN c int"),
                m("GRANT SELECT ON orders TO x"),
            ],
            PG,
        )
        (window,) = report.migrations[0].windows
        self.assertEqual(window.heaviest, Work.UNKNOWN)
        self.assertEqual(report.migrations[0].findings[0].rule, "window.held")


class IntentTestCase(unittest.TestCase):
    def test_an_intent_that_disagrees_with_the_text_is_reported(self):
        statement = with_intent("DROP TABLE orders", "create_index", "orders")
        (impact,) = analyze([statement], PG).statements
        self.assertEqual(impact.findings[0].rule, "impact.intent_mismatch")
        self.assertIn("follows the text", impact.findings[0].message)
        self.assertEqual(impact.tables[0].rule, "pg.drop_table")

    def test_an_intent_on_another_table_disagrees(self):
        statement = with_intent("DROP TABLE orders", "drop_table", "items")
        (impact,) = analyze([statement], PG).statements
        self.assertEqual(impact.findings[0].rule, "impact.intent_mismatch")

    def test_agreement(self):
        def agrees(kind, table, sql):
            return intent_agrees(Intent(kind, table), recognize(sql, PG))

        self.assertTrue(agrees("drop_table", "app.orders", 'DROP TABLE "orders"'))
        self.assertTrue(agrees("drop_table", "orders", "DROP TABLE app.orders"))
        self.assertFalse(agrees("drop_table", "a.orders", "DROP TABLE b.orders"))
        self.assertTrue(agrees("drop_index", "orders", "DROP INDEX ix"))
        self.assertTrue(agrees("rebuild_table", "orders", "DROP TABLE orders"))
        self.assertFalse(agrees("add_column", "t", "ALTER TABLE t DROP COLUMN c"))
        self.assertTrue(agrees("add_column", "t", "ALTER TABLE t ADD COLUMN c int"))
        self.assertTrue(agrees("create_enum_type", None, "CREATE TYPE m AS ENUM ('a')"))
        self.assertFalse(
            intent_agrees(
                Intent("drop_table", "t"), ParsedStatement("create_table", "t")
            )
        )


class UnknownTestCase(unittest.TestCase):
    def test_an_unknown_statement_is_never_safe(self):
        (impact,) = analyze(["GRANT SELECT ON t TO x"], PG).statements
        self.assertEqual(impact.confidence, Confidence.UNKNOWN)
        self.assertEqual(impact.findings[0].severity, Severity.INFO)
        self.assertIn("not understood", impact.findings[0].message)


class AttachImpactTestCase(unittest.TestCase):
    def test_each_statement_gets_its_impact_and_keeps_its_migration(self):
        statements = attach_impact(
            [m("ALTER TABLE t ADD COLUMN c int", "m1", False), "DROP TABLE t"], PG
        )
        first, second = statements
        self.assertEqual((first.migration_id, first.transactional), ("m1", False))
        self.assertEqual(first.impact.tables[0].table, "t")
        self.assertIsInstance(second, MigrationStatement)
        self.assertIsNone(second.migration_id)
        self.assertEqual(second.impact.statement, "DROP TABLE t")

    def test_refuses_a_dialect_without_rules(self):
        with self.assertRaises(ValueError):
            attach_impact(["SELECT 1"], Dialects.DEFAULT)


if __name__ == "__main__":
    unittest.main()
