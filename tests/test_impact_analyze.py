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
from sustained.impact.context import Relation
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
        self.assertFalse(supported(Dialects.PRESTO))
        with self.assertRaises(ValueError) as caught:
            analyze(["DROP TABLE a"], Dialects.PRESTO)
        self.assertEqual(
            str(caught.exception), "Impact analysis does not cover Presto yet."
        )

    def test_a_message_names_the_engine_a_dialect_stands_for(self):
        from sustained.impact.rules import engine, listed

        self.assertEqual(engine(Dialects.DEFAULT), "SQLite")
        self.assertEqual(engine(Dialects.MYSQL), "MySQL and MariaDB")
        self.assertEqual(engine(Dialects.MSSQL), "SQL Server")
        self.assertEqual(engine(Dialects.ATHENA), "Athena")
        self.assertEqual(listed(["A"]), "A")
        self.assertEqual(listed(["A", "B"]), "A and B")
        self.assertEqual(listed(["A", "B", "C"]), "A, B, and C")

    def test_the_preflight_refusal_names_the_engine(self):
        from sustained.impact.preflight import covered_or_raise, preflight_plan

        with self.assertRaises(ValueError) as caught:
            covered_or_raise(Dialects.DEFAULT)
        self.assertEqual(
            str(caught.exception), "The live preflight does not cover SQLite."
        )
        with self.assertRaises(ValueError) as caught:
            preflight_plan(Dialects.DUCKDB, ())
        self.assertEqual(
            str(caught.exception), "The live preflight does not cover DuckDB."
        )


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
        self.assertEqual(rules(statement), ["pg.partitions_unread", "pg.lock_timeout"])

    def test_default_wording_for_blocking_work_without_a_message(self):
        vacuum = MigrationStatement("VACUUM FULL orders", "m1", transactional=False)
        (statement,) = analyze([vacuum], PG).statements
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
            [Hold.TRANSACTION, Hold.TRANSACTION],
        )

    def test_the_last_statement_keeps_its_lock_to_the_commit(self):
        self.assertEqual(
            self.holds([m("ALTER TABLE a ADD COLUMN c int")]), [Hold.TRANSACTION]
        )

    def test_a_lock_on_an_engine_whose_ddl_commits_ends_with_its_statement(self):
        report = analyze([m("ALTER TABLE a ADD COLUMN c int")], Dialects.MYSQL)
        self.assertEqual(
            [t.hold for s in report.statements for t in s.tables], [Hold.BRIEF]
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


class FilledTableTestCase(unittest.TestCase):
    """Tables the run creates with IF NOT EXISTS, or fills from a query."""

    def index(self, statements, context=None):
        """The table impact of a CREATE INDEX that ends the run."""
        report = analyze(
            [m(sql) for sql in statements] + [m("CREATE INDEX ix ON x (id)")],
            PG,
            context,
        )
        (found,) = report.statements[-1].tables
        return found

    def test_if_not_exists_on_a_table_the_context_reads_creates_nothing(self):
        found = self.index(
            ["CREATE TABLE IF NOT EXISTS x (id int)"],
            sized(5_000_000, x=TableStats(5_000_000, 10**9)),
        )
        self.assertEqual((found.work, found.rows), (Work.INDEX_BUILD, 5_000_000))

    def test_if_not_exists_on_an_absent_table_creates_it(self):
        found = self.index(["CREATE TABLE IF NOT EXISTS x (id int)"], sized())
        self.assertEqual((found.blocks, found.rows), (Blocks.NOTHING, 0))

    def test_if_not_exists_without_a_read_may_name_a_table_that_exists(self):
        found = self.index(["CREATE TABLE IF NOT EXISTS x (id int)"])
        self.assertEqual((found.blocks, found.rows), (Blocks.WRITES, None))

    def test_if_not_exists_after_a_drop_creates_it(self):
        found = self.index(
            ["DROP TABLE x", "CREATE TABLE IF NOT EXISTS x (id int)"],
            sized(x=TableStats(5_000_000, 10**9)),
        )
        self.assertEqual(found.blocks, Blocks.NOTHING)

    def test_create_table_as_select_has_the_rows_it_reads(self):
        found = self.index(
            ["CREATE TABLE x AS SELECT * FROM orders"], sized(5_000_000, 10**9)
        )
        self.assertEqual(
            (found.work, found.rows, found.bytes),
            (Work.INDEX_BUILD, 5_000_000, 10**9),
        )

    def test_insert_select_fills_a_new_table_from_every_table_it_reads(self):
        found = self.index(
            [
                "CREATE TABLE x (id int)",
                "INSERT INTO x SELECT o.id FROM orders o JOIN items i ON o.id = i.id",
            ],
            sized(5_000_000, 10**9, items=TableStats(1_000, 10**6)),
        )
        self.assertEqual((found.rows, found.bytes), (5_001_000, 10**9 + 10**6))

    def test_a_source_of_unknown_size_leaves_the_size_unknown(self):
        for source in (
            "SELECT * FROM generate_series(1, 10)",
            "SELECT * FROM orders, items",
        ):
            with self.subTest(source):
                found = self.index(
                    ["CREATE TABLE x (id int)", f"INSERT INTO x {source}"],
                    sized(5_000_000, 10**9),
                )
                self.assertEqual((found.blocks, found.rows), (Blocks.WRITES, None))

    def test_a_query_that_reads_no_table_leaves_it_new(self):
        found = self.index(
            ["CREATE TABLE x (id int)", "INSERT INTO x SELECT 1"], sized(5)
        )
        self.assertEqual((found.blocks, found.rows), (Blocks.NOTHING, 0))

    def test_a_copy_of_a_new_table_is_new(self):
        found = self.index(
            [
                "CREATE TABLE e (id int)",
                "CREATE TABLE x AS SELECT * FROM e",
            ],
            sized(5),
        )
        self.assertEqual(found.blocks, Blocks.NOTHING)

    def test_a_table_swap_gives_the_live_name_the_rows_copied_into_it(self):
        report = analyze(
            [
                m("CREATE TABLE orders_new (id bigint, c int)"),
                m("INSERT INTO orders_new SELECT * FROM orders"),
                m("DROP TABLE orders"),
                m("ALTER TABLE orders_new RENAME TO orders"),
                m("ALTER TABLE orders ALTER COLUMN c TYPE bigint", "m2"),
                m("CREATE INDEX ix ON orders (c)", "m2"),
            ],
            PG,
            sized(5_000_000, 10**9),
        )
        retype, index = (s.tables[0] for s in report.statements[-2:])
        self.assertEqual((retype.work, retype.rows), (Work.REWRITE, 5_000_000))
        self.assertEqual((index.work, index.rows), (Work.INDEX_BUILD, 5_000_000))
        self.assertEqual(report.statements[-1].severity, Severity.DANGER)

    def test_a_swap_with_an_empty_table_leaves_it_new(self):
        report = analyze(
            [
                m("CREATE TABLE orders_new (id int)"),
                m("DROP TABLE orders"),
                m("ALTER TABLE orders_new RENAME TO orders"),
                m("CREATE INDEX ix ON orders (id)"),
            ],
            PG,
            sized(5_000_000),
        )
        self.assertEqual(report.statements[-1].tables[0].blocks, Blocks.NOTHING)

    def test_the_state_on_its_own(self):
        context = sized(10, 100, a=TableStats(1, 2))
        state = RunState("lock_timeout", context=context)
        for sql in [
            "CREATE TABLE n (id int)",
            "CREATE TABLE f AS SELECT * FROM orders JOIN a USING (id)",
            "INSERT INTO f SELECT * FROM n",
            "CREATE TABLE IF NOT EXISTS a (id int)",
            "INSERT INTO a SELECT * FROM orders",
            "ALTER TABLE f RENAME TO g",
        ]:
            state.record(recognize(sql, PG), True)
        self.assertTrue(state.is_new("n"))
        self.assertTrue(state.created_in_run("G"))
        self.assertFalse(state.is_new("g"))
        self.assertFalse(state.created_in_run("a"))
        self.assertFalse(state.may_exist("f"))
        self.assertEqual(state.stats(context, "n"), TableStats(0, 0))
        self.assertEqual(state.stats(context, "g"), TableStats(11, 102))
        self.assertEqual(state.stats(context, "a"), TableStats(1, 2))
        self.assertEqual(state.sources_of("g"), ("orders", "a"))
        state.record(
            recognize("INSERT INTO g SELECT * FROM unnest('{1}'::int[])", PG), True
        )
        self.assertEqual(state.stats(context, "g"), TableStats())


class RollbackTestCase(unittest.TestCase):
    """A ROLLBACK in a migration's transaction undoes the run's table facts."""

    def index(self, statements, dialect=PG):
        """The table impact of a CREATE INDEX on big that ends the run."""
        context = sized(big=TableStats(5_000_000, 10**9))
        report = analyze(
            statements
            + [m("CREATE INDEX ix ON big (id)", statements[-1].migration_id)],
            dialect,
            context,
        )
        (found,) = report.statements[-1].tables
        return found

    def swap(self, rollback, migration_id="m1", transactional=True):
        return [
            m("ALTER TABLE big RENAME TO old"),
            m("CREATE TABLE big (id int)"),
            m(rollback, migration_id, transactional),
        ]

    def test_a_rollback_undoes_a_table_swap(self):
        for rollback in ("ROLLBACK", "ROLLBACK TO SAVEPOINT s"):
            with self.subTest(rollback):
                found = self.index(self.swap(rollback))
                self.assertEqual(
                    (found.work, found.blocks, found.rows),
                    (Work.INDEX_BUILD, Blocks.WRITES, 5_000_000),
                )

    def test_without_a_rollback_the_swapped_table_is_new(self):
        found = self.index(self.swap("SET a = '1'"))
        self.assertEqual((found.blocks, found.rows), (Blocks.NOTHING, 0))

    def test_a_rollback_outside_a_transaction_undoes_nothing(self):
        statements = [
            m("ALTER TABLE big RENAME TO old", transactional=False),
            m("CREATE TABLE big (id int)", transactional=False),
            m("ROLLBACK", transactional=False),
        ]
        found = self.index(statements)
        self.assertEqual((found.blocks, found.rows), (Blocks.NOTHING, 0))

    def test_a_rollback_keeps_what_earlier_migrations_did(self):
        found = self.index(
            [m("ALTER TABLE big RENAME TO old"), m("CREATE TABLE big (id int)")]
            + [m("ALTER TABLE old ADD COLUMN a int", "m2"), m("ROLLBACK", "m2")]
        )
        self.assertEqual((found.blocks, found.rows), (Blocks.NOTHING, 0))

    def test_a_rollback_on_mysql_keeps_the_committed_ddl(self):
        state = RunState("lock_wait_timeout", local_scope=False)
        state.enter("m1")
        for sql in ("RENAME TABLE big TO old", "CREATE TABLE big (id int)", "ROLLBACK"):
            state.record(recognize(sql, Dialects.MYSQL), True)
        self.assertTrue(state.is_new("big"))
        self.assertEqual(state.original("old"), "big")

    def test_a_rollback_clears_every_table_fact(self):
        state = RunState("lock_timeout", transactional_ddl=True)
        state.enter("m1")
        for sql in (
            "CREATE TABLE a (id int)",
            "INSERT INTO a SELECT * FROM orders",
            "CREATE INDEX ix ON a (id)",
            "ALTER TABLE orders ADD CONSTRAINT c CHECK (id IS NOT NULL)",
            "ALTER TABLE orders RENAME COLUMN x TO y",
            "DROP TABLE b",
            "ROLLBACK",
        ):
            state.record(recognize(sql, PG), True)
        self.assertFalse(state.created_in_run("a"))
        self.assertIsNone(state.index_table("ix"))
        self.assertFalse(state.proves_not_null("orders", "id"))
        self.assertEqual(state.schema_column("orders", "x"), "x")
        self.assertEqual((state.filled, state.gone), ({}, set()))


class PartitionStateTestCase(unittest.TestCase):
    """The partitioned tables and partitions the run creates and links."""

    # pt is partitioned, with pt1 and its DEFAULT partition ptd.
    CONTEXT = EngineContext(
        "postgres",
        (16,),
        tables={"pt1": TableStats(10, 100), "big": TableStats(5_000_000, 10**9)},
        read=frozenset({"sizes", "partitions"}),
        relations={
            "pt": Relation(partitioned=True, default="ptd", partitions=("pt1", "ptd")),
            "pt1": Relation(parent="pt"),
            "ptd": Relation(parent="pt"),
        },
    )

    def state(self, *statements):
        state = RunState("lock_timeout", context=self.CONTEXT, transactional_ddl=True)
        state.enter("m1")
        for sql in statements:
            state.record(recognize(sql, PG), True)
        return state

    def test_attach_fills_a_partitioned_table_the_run_created(self):
        state = self.state(
            "CREATE TABLE n (id int) PARTITION BY RANGE (id)",
            "ALTER TABLE n ATTACH PARTITION big FOR VALUES FROM (1) TO (9)",
        )
        self.assertFalse(state.is_new("n"))
        self.assertEqual(state.stats(self.CONTEXT, "n"), TableStats(5_000_000, 10**9))
        found = state.relation("n")
        self.assertEqual((found.partitioned, found.partitions), (True, ("big",)))
        self.assertEqual(state.relation("big").parent, "n")

    def test_attaching_a_new_empty_table_leaves_the_parent_empty(self):
        state = self.state(
            "CREATE TABLE n (id int) PARTITION BY LIST (id)",
            "CREATE TABLE n1 (id int)",
            "ALTER TABLE n ATTACH PARTITION n1 DEFAULT",
        )
        self.assertTrue(state.is_new("n"))
        self.assertEqual(state.relation("n").default, "n1")

    def test_partition_of_is_new_and_linked(self):
        state = self.state("CREATE TABLE pt2 PARTITION OF pt DEFAULT")
        self.assertTrue(state.is_new("pt2"))
        found = state.relation("pt")
        self.assertEqual(found.partitions, ("pt1", "ptd", "pt2"))
        self.assertEqual(found.default, "pt2")
        self.assertEqual(state.relation("pt2").parent, "pt")

    def test_detach_unlinks_the_partition(self):
        state = self.state("ALTER TABLE pt DETACH PARTITION ptd CONCURRENTLY")
        found = state.relation("pt")
        self.assertEqual((found.partitions, found.default), (("pt1",), None))
        self.assertIsNone(state.relation("ptd").parent)

    def test_rename_and_drop_follow_the_partitions(self):
        state = self.state(
            "ALTER TABLE pt1 RENAME TO pt_one",
            "ALTER TABLE pt RENAME TO p",
            "CREATE TABLE p2 PARTITION OF p FOR VALUES IN (2)",
            "ALTER TABLE p2 RENAME TO p_two",
        )
        found = state.relation("p")
        self.assertEqual(found.partitions, ("pt_one", "ptd", "p_two"))
        self.assertEqual(state.relation("pt_one").parent, "p")
        self.assertEqual(state.relation("p_two").parent, "p")
        self.assertIsNone(state.relation("pt"))
        state.record(recognize("DROP TABLE ptd", PG), True)
        self.assertEqual(state.relation("p").partitions, ("pt_one", "p_two"))
        self.assertIsNone(state.relation("p").default)
        state.record(recognize("DROP TABLE p", PG), True)
        self.assertEqual(state.gone >= {"p", "pt_one", "p_two"}, True)
        self.assertIsNone(state.relation("p_two"))

    def test_insert_into_a_partitioned_table_fills_its_new_partitions(self):
        state = self.state(
            "CREATE TABLE n (id int) PARTITION BY RANGE (id)",
            "CREATE TABLE n1 PARTITION OF n FOR VALUES FROM (1) TO (9)",
            "INSERT INTO n SELECT id FROM big",
        )
        self.assertFalse(state.is_new("n1"))
        self.assertFalse(state.is_new("n"))

    def test_a_rollback_undoes_the_links(self):
        state = self.state(
            "CREATE TABLE n (id int) PARTITION BY RANGE (id)",
            "ALTER TABLE n ATTACH PARTITION big FOR VALUES FROM (1) TO (9)",
            "ROLLBACK",
        )
        self.assertIsNone(state.relation("n"))
        self.assertIsNone(state.relation("big"))


class CheckStateTestCase(unittest.TestCase):
    def state(self, *statements):
        state = RunState()
        for sql in statements:
            state.record(recognize(sql, PG), True)
        return state

    def test_schema_checks_follow_renames_and_drops(self):
        state = self.state(
            "ALTER TABLE t RENAME CONSTRAINT a TO b",
            "ALTER TABLE t RENAME CONSTRAINT b TO c",
            "ALTER TABLE t DROP CONSTRAINT d",
        )
        self.assertTrue(state.schema_check_kept("t", "a"))
        self.assertFalse(state.schema_check_kept("t", "d"))
        self.assertTrue(state.schema_check_kept("t", "e"))
        self.assertTrue(state.schema_check_kept("u", "d"))
        state = self.state(
            "ALTER TABLE t RENAME CONSTRAINT a TO b",
            "ALTER TABLE t DROP CONSTRAINT b",
        )
        self.assertFalse(state.schema_check_kept("t", "a"))

    def test_schema_columns_follow_renames_and_drops(self):
        state = self.state(
            "ALTER TABLE t RENAME COLUMN a TO b",
            "ALTER TABLE t RENAME COLUMN c TO a",
            "ALTER TABLE t DROP COLUMN d",
        )
        self.assertEqual(state.schema_column("t", "B"), "a")
        self.assertEqual(state.schema_column("t", "a"), "c")
        self.assertIsNone(state.schema_column("t", "c"))
        self.assertIsNone(state.schema_column("t", "d"))
        self.assertEqual(state.schema_column("t", "e"), "e")
        state.record(recognize("ALTER TABLE t RENAME TO u", PG), True)
        self.assertEqual(state.schema_column("u", "b"), "a")
        state.record(recognize("DROP TABLE u", PG), True)
        self.assertEqual(state.schema_column("u", "b"), "b")


class IntentFallbackTestCase(unittest.TestCase):
    """A generated statement the recognizer cannot read, read from its intent."""

    UNREAD = "DO $$ BEGIN EXECUTE 'ALTER TABLE orders ADD COLUMN c int'; END $$"

    def impact(self, kind, dialect=PG, column=None, context=None, **details):
        statement = with_intent(self.UNREAD, kind, "orders", column, **details)
        (found,) = analyze([statement], dialect, context).statements
        return found

    def test_a_column_with_a_default_is_a_rewrite_at_most_likely(self):
        for dialect in (PG, Dialects.MSSQL, Dialects.MYSQL):
            with self.subTest(dialect):
                found = self.impact(
                    "add_column", dialect, "c", nullable=False, has_default=True
                )
                self.assertEqual(found.tables[0].work, Work.REWRITE)
                self.assertEqual(found.confidence, Confidence.LIKELY)
                self.assertEqual(found.findings[0].rule, "impact.from_intent")
                self.assertIn(
                    "does not give the default or the column's type",
                    found.findings[0].message,
                )

    def test_an_intent_without_details_takes_the_worst_case(self):
        found = self.impact("add_column", column="c")
        self.assertEqual(found.tables[0].work, Work.REWRITE)
        self.assertIn(
            "whether the column is nullable, the default, or the column's type",
            found.findings[0].message,
        )

    def test_a_nullable_column_without_a_default_is_catalog_work(self):
        found = self.impact("add_column", column="c", nullable=True, has_default=False)
        self.assertEqual(found.tables[0].work, Work.CATALOG)
        self.assertEqual(found.confidence, Confidence.LIKELY)

    def test_a_foreign_key_counts_as_validated(self):
        found = self.impact("add_foreign_key", name="fk", references="customers")
        self.assertEqual(
            [(t.table, t.work) for t in found.tables],
            [("orders", Work.SCAN), ("customers", Work.CATALOG)],
        )
        self.assertEqual(found.confidence, Confidence.LIKELY)
        self.assertTrue(
            found.findings[0].message.endswith(
                "whether the key is NOT VALID, so the analysis assumes the worst case"
            )
        )
        found = self.impact("add_check", name="ck")
        self.assertEqual(found.tables[0].work, Work.SCAN)

    def test_every_detail_given_keeps_the_confidence(self):
        # With the partitions read, orders reads as not partitioned.
        read = EngineContext("postgres", (12,), read=frozenset({"partitions"}))
        found = self.impact("drop_table", context=read)
        self.assertEqual(found.tables[0].rule, "pg.drop_table")
        self.assertEqual(found.confidence, Confidence.KNOWN)
        self.assertEqual(
            self.impact("rename_column", column="c", new="d", context=read).confidence,
            Confidence.KNOWN,
        )
        self.assertEqual(
            self.impact("drop_constraint", name="ck", context=read).confidence,
            Confidence.KNOWN,
        )

    def test_a_missing_detail_lowers_the_confidence(self):
        self.assertEqual(
            self.impact("rename_column", column="c").confidence, Confidence.LIKELY
        )
        found = self.impact("create_table")
        self.assertEqual(found.confidence, Confidence.LIKELY)
        self.assertIn("so no lock on them is reported", found.findings[0].message)


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

    def test_each_reset_ends_the_timeout(self):
        for reset in (
            "RESET lock_timeout",
            "RESET ALL",
            "DISCARD ALL",
            "SELECT set_config('lock_timeout', '0', false)",
        ):
            with self.subTest(reset):
                self.assertEqual(
                    self.covered(
                        [
                            m("SET lock_timeout = '2s'"),
                            m("DROP TABLE a"),
                            m(reset),
                            m("DROP TABLE b"),
                        ]
                    ),
                    [True, False],
                )

    def test_another_setting_reset_keeps_the_timeout(self):
        self.assertEqual(
            self.covered(
                [
                    m("SET lock_timeout = '2s'"),
                    m("RESET statement_timeout"),
                    m("DROP TABLE b"),
                ]
            ),
            [True],
        )

    def test_set_config_sets_the_timeout_in_its_scope(self):
        self.assertEqual(
            self.covered(
                [
                    m("SELECT set_config('lock_timeout', '2s', true)"),
                    m("DROP TABLE a"),
                    m("DROP TABLE b", "m2"),
                    m("SELECT pg_catalog.set_config('lock_timeout', '2s', 'f')", "m2"),
                    m("DROP TABLE c", "m3"),
                ]
            ),
            [True, False, True],
        )

    def test_a_rollback_undoes_the_timeout_its_transaction_set(self):
        self.assertEqual(
            self.covered(
                [
                    m("SET lock_timeout = '2s'"),
                    m("ROLLBACK"),
                    m("DROP TABLE a"),
                ]
            ),
            [False],
        )
        # A timeout set before the migration began is not undone.
        self.assertEqual(
            self.covered(
                [
                    m("SET lock_timeout = '2s'"),
                    m("SET LOCAL lock_timeout = '5s'", "m2"),
                    m("ROLLBACK TO SAVEPOINT s", "m2"),
                    m("DROP TABLE a", "m2"),
                ]
            ),
            [True],
        )

    def test_a_session_setting_replaces_a_local_one(self):
        self.assertEqual(
            self.covered(
                [
                    m("SET LOCAL lock_timeout = '2s'"),
                    m("SET lock_timeout = 0"),
                    m("DROP TABLE a"),
                ]
            ),
            [False],
        )

    def test_a_rollback_on_mysql_keeps_the_session_setting(self):
        state = RunState("lock_wait_timeout", local_scope=False)
        state.timeouts.enter("m1")
        for sql in ("SET lock_wait_timeout = 5", "ROLLBACK"):
            state.record(recognize(sql, Dialects.MYSQL), True)
        self.assertTrue(state.timeouts.covered)

    def test_a_reset_forgets_the_settings(self):
        state = RunState("lock_timeout")
        for sql in ("SET a = '1'", "SET b = '2'", "RESET a"):
            state.record(recognize(sql, PG), True)
        self.assertEqual(state.settings, {"b": "2"})
        state.record(recognize("DISCARD ALL", PG), True)
        self.assertEqual(state.settings, {})

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
        self.assertIn("for reads_and_writes from statement 1 until", finding.message)
        self.assertIn("rows work of statement 2", finding.message)
        self.assertEqual(
            [(lock.table, lock.lock, lock.statement) for lock in migration.locks],
            [
                ("orders", "ACCESS EXCLUSIVE", 1),
                ("orders", "ROW EXCLUSIVE", 2),
                ("orders", "ACCESS EXCLUSIVE", 3),
            ],
        )

    def test_the_finding_names_the_statement_that_takes_each_level(self):
        report = analyze(
            [
                m("UPDATE orders SET c = 0"),
                m("CREATE INDEX ix ON orders (c)"),
                m("ALTER TABLE orders ADD COLUMN d int"),
            ],
            PG,
        )
        (migration,) = report.migrations
        (window,) = migration.windows
        self.assertEqual((window.blocks, window.taken_by), (Blocks.READS_AND_WRITES, 3))
        (finding,) = [f for f in migration.findings if f.rule == "window.held"]
        self.assertEqual(
            finding.message,
            "orders stays blocked for writes from statement 1 and for "
            "reads_and_writes from statement 3 until the migration commits, "
            "across the index_build work of statement 2; move that work to a "
            "migration of its own",
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
            attach_impact(["SELECT 1"], Dialects.PRESTO)


if __name__ == "__main__":
    unittest.main()
