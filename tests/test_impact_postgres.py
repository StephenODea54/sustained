"""Tests for the PostgreSQL impact rules."""

import json
import pathlib
import unittest

from sustained.analysis import MigrationStatement, with_intent
from sustained.dialects import Dialects
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    Severity,
    Work,
    analyze,
)
from sustained.impact.context import FLOORS, assumed
from sustained.impact.rules import all_rules
from sustained.impact.rules import postgres as pg
from sustained.impact.rules import profile_for
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
)

PG = Dialects.POSTGRES
ROOT = pathlib.Path(__file__).resolve().parent.parent

# The fixture schema's two keyed tables, as the schema read reports them.
FIXTURE_SCHEMA = Snapshot(
    {
        "r": IntrospectedTable(
            {"id": IntrospectedColumn("integer", False, True)},
            primary_key=("id",),
            name="r",
        ),
        "t": IntrospectedTable(
            {
                "id": IntrospectedColumn("integer", False, True),
                "c": IntrospectedColumn("integer", True, False),
                "name": IntrospectedColumn("character varying(100)", True, False),
                "r_id": IntrospectedColumn("integer", True, False),
            },
            primary_key=("id",),
            foreign_keys={
                "t_r_id_fkey": IntrospectedForeignKey(
                    ("r_id",), "r", ("id",), name="t_r_id_fkey"
                )
            },
            indexes={"ix": IntrospectedIndex(("c",), True, name="ix")},
            checks={"ck": "c > 0"},
            check_names={"ck": "ck"},
            name="t",
        ),
    }
)

# The newest server, with the fixture schema read.
FIXTURE_CONTEXT = EngineContext("postgres", (18,), schema=FIXTURE_SCHEMA)


def impact(sql, context=None):
    """The impact of one statement, alone in a migration of its own."""
    (statement,) = analyze([sql], PG, context).statements
    return statement


def table(sql, name=None, context=None):
    statement = impact(sql, context)
    if name is None:
        (only,) = statement.tables
        return only
    return next(t for t in statement.tables if t.table == name)


def rules(statement):
    return [f.rule for f in statement.findings]


class LockTableTestCase(unittest.TestCase):
    """What each statement locks, and the work it does, table by table."""

    CASES = [
        # sql, lock, blocks, work, rule
        (
            "ALTER TABLE t ADD COLUMN c integer",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_column",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer NOT NULL DEFAULT 0",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_column",
        ),
        (
            "ALTER TABLE t ADD COLUMN c timestamptz DEFAULT now()",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_column",
        ),
        (
            "ALTER TABLE t ADD COLUMN c uuid DEFAULT gen_random_uuid()",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.add_column.rewrite",
        ),
        (
            "ALTER TABLE t ADD COLUMN c bigserial",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.add_column.rewrite",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer GENERATED ALWAYS AS IDENTITY",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.add_column.rewrite",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer GENERATED ALWAYS AS (id) STORED",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.add_column.rewrite",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer UNIQUE",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.INDEX_BUILD,
            "pg.add_column.key",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer CHECK (c > 0)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.SCAN,
            "pg.add_column.checked",
        ),
        (
            "ALTER TABLE t DROP COLUMN c",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_column",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c TYPE bigint",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.alter_column_type",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c SET NOT NULL",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.SCAN,
            "pg.set_not_null",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c DROP NOT NULL",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.alter_column.catalog",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c SET DEFAULT 1",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.alter_column.catalog",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c SET STATISTICS 100",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.CATALOG,
            "pg.set_statistics",
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.SCAN,
            "pg.add_check",
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0) NOT VALID",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_check.not_valid",
        ),
        (
            "ALTER TABLE t VALIDATE CONSTRAINT ck",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.SCAN,
            "pg.validate_constraint",
        ),
        (
            "ALTER TABLE t ADD PRIMARY KEY (id)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.INDEX_BUILD,
            "pg.add_key",
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE USING INDEX ix",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_key.using_index",
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT ex EXCLUDE USING gist (c WITH =)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.INDEX_BUILD,
            "pg.add_exclusion",
        ),
        (
            "ALTER TABLE t DROP CONSTRAINT ck",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_constraint",
        ),
        (
            "CREATE INDEX ix ON t (c)",
            "SHARE",
            Blocks.WRITES,
            Work.INDEX_BUILD,
            "pg.create_index",
        ),
        (
            "CREATE INDEX CONCURRENTLY ix ON t (c)",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.INDEX_BUILD,
            "pg.create_index.concurrently",
        ),
        (
            "DROP TABLE t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_table",
        ),
        (
            "TRUNCATE t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_table",
        ),
        (
            "DROP VIEW t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_view",
        ),
        (
            "VACUUM FULL t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.vacuum_full",
        ),
        (
            "CLUSTER t USING ix",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.vacuum_full",
        ),
        (
            "VACUUM t",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.SCAN,
            "pg.vacuum",
        ),
        (
            "ANALYZE t",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.SCAN,
            "pg.vacuum",
        ),
        (
            "ALTER TABLE t SET TABLESPACE fast",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.table_rewrite",
        ),
        (
            "ALTER TABLE t SET LOGGED",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.table_rewrite",
        ),
        (
            "ALTER TABLE t SET (fillfactor = 70, autovacuum_enabled = false)",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.CATALOG,
            "pg.set_parameters",
        ),
        (
            "ALTER TABLE t SET (user_catalog_table = true)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.alter_table.catalog",
        ),
        (
            "ALTER TABLE t OWNER TO someone",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.alter_table.catalog",
        ),
        (
            "ALTER TABLE t DISABLE TRIGGER tr",
            "SHARE ROW EXCLUSIVE",
            Blocks.WRITES,
            Work.CATALOG,
            "pg.alter_trigger",
        ),
        (
            "CREATE TRIGGER tr BEFORE UPDATE ON t FOR EACH ROW EXECUTE FUNCTION f()",
            "SHARE ROW EXCLUSIVE",
            Blocks.WRITES,
            Work.CATALOG,
            "pg.trigger",
        ),
        (
            "DROP TRIGGER tr ON t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.trigger",
        ),
        (
            "COMMENT ON COLUMN t.c IS 'x'",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.CATALOG,
            "pg.comment",
        ),
        (
            "UPDATE t SET c = 0",
            "ROW EXCLUSIVE",
            Blocks.WRITES,
            Work.ROWS,
            "pg.write_rows",
        ),
        (
            "DELETE FROM t WHERE c < 0",
            "ROW EXCLUSIVE",
            Blocks.WRITES,
            Work.ROWS,
            "pg.write_rows",
        ),
        (
            "INSERT INTO t (c) VALUES (1)",
            "ROW EXCLUSIVE",
            Blocks.DDL,
            Work.ROWS,
            "pg.insert",
        ),
        (
            "REINDEX TABLE t",
            "SHARE",
            Blocks.READS_AND_WRITES,
            Work.INDEX_BUILD,
            "pg.reindex",
        ),
        (
            "REINDEX TABLE CONCURRENTLY t",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.INDEX_BUILD,
            "pg.reindex.concurrently",
        ),
        (
            "REFRESH MATERIALIZED VIEW t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.refresh_materialized_view",
        ),
        (
            "REFRESH MATERIALIZED VIEW CONCURRENTLY t",
            "EXCLUSIVE",
            Blocks.WRITES,
            Work.ROWS,
            "pg.refresh_materialized_view.concurrently",
        ),
        (
            "REFRESH MATERIALIZED VIEW t WITH NO DATA",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.refresh_materialized_view",
        ),
        (
            "LOCK TABLE t IN SHARE MODE",
            "SHARE",
            Blocks.WRITES,
            Work.CATALOG,
            "pg.lock_table",
        ),
        (
            "ALTER TABLE t RENAME CONSTRAINT a TO b",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.rename",
        ),
    ]

    def test_each_statement(self):
        for sql, lock, blocks, work, rule in self.CASES:
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual(found.table, "t")
                self.assertEqual(found.lock, lock)
                self.assertEqual(found.blocks, blocks)
                self.assertEqual(found.work, work)
                self.assertEqual(found.rule, rule)


class RuleCatalogTestCase(unittest.TestCase):
    def test_every_rule_has_a_source_and_a_fixture_that_reaches_it(self):
        reached = set()
        for rule in profile_for(PG).rules:
            self.assertTrue(rule.source.startswith("https://"), rule.id)
            self.assertTrue(rule.fixtures, rule.id)
            self.assertTrue(rule.id.startswith("pg."), rule.id)
            for fixture in rule.fixtures:
                statement = impact(fixture, FIXTURE_CONTEXT)
                found = {t.rule for t in statement.tables} | set(rules(statement))
                if rule.id in found:
                    reached.add(rule.id)
                self.assertNotEqual(statement.confidence, Confidence.UNKNOWN, fixture)
        self.assertEqual(reached, {rule.id for rule in profile_for(PG).rules})

    def test_rule_ids_are_unique(self):
        ids = [rule.id for rule in all_rules()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_floor_matches_the_support_table(self):
        support = json.loads((ROOT / "support.json").read_text())
        row = next(r for r in support["databases"] if r["name"] == "postgres")
        self.assertEqual(
            FLOORS["postgres"], tuple(int(p) for p in row["floor"].split("."))
        )
        self.assertEqual(assumed("postgres").version, (12,))

    def test_lock_order_and_blocks(self):
        self.assertEqual(pg.lock_rank(None), -1)
        self.assertEqual(pg.blocks(None), Blocks.NOTHING)
        ranks = [pg.lock_rank(lock) for lock in pg.LOCKS]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(pg.blocks("ACCESS SHARE"), Blocks.DDL)
        self.assertEqual(pg.blocks("EXCLUSIVE"), Blocks.WRITES)


class ForeignKeyTestCase(unittest.TestCase):
    def test_a_foreign_key_locks_both_tables(self):
        statement = impact(
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (r_id) REFERENCES r (id)"
        )
        locks = {(t.table, t.lock, t.work) for t in statement.tables}
        self.assertEqual(
            locks,
            {
                ("t", "SHARE ROW EXCLUSIVE", Work.SCAN),
                ("r", "SHARE ROW EXCLUSIVE", Work.CATALOG),
            },
        )
        finding = next(f for f in statement.findings if f.rule == "pg.add_foreign_key")
        self.assertEqual(finding.severity, Severity.WARN)
        self.assertEqual(
            finding.remedy,
            (
                "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (r_id) REFERENCES r (id) "
                "NOT VALID",
                "ALTER TABLE t VALIDATE CONSTRAINT fk",
            ),
        )

    def test_not_valid_only_changes_the_catalog(self):
        statement = impact(
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (r_id) REFERENCES r (id) NOT VALID"
        )
        self.assertEqual({t.work for t in statement.tables}, {Work.CATALOG})
        self.assertNotIn("pg.add_foreign_key.not_valid", rules(statement))

    def test_an_unnamed_constraint_has_no_remedy(self):
        statement = impact("ALTER TABLE t ADD CHECK (c > 0)")
        finding = next(f for f in statement.findings if f.rule == "pg.add_check")
        self.assertEqual(finding.remedy, ())

    def test_an_added_column_with_references_locks_the_referenced_table(self):
        statement = impact("ALTER TABLE t ADD COLUMN r_id integer REFERENCES r (id)")
        self.assertEqual(
            {(t.table, t.lock) for t in statement.tables},
            {("t", "ACCESS EXCLUSIVE"), ("r", "SHARE ROW EXCLUSIVE")},
        )

    def test_create_table_locks_what_it_references(self):
        statement = impact(
            "CREATE TABLE n (id integer, r_id integer REFERENCES r (id))"
        )
        (only,) = statement.tables
        self.assertEqual((only.table, only.lock), ("r", "SHARE ROW EXCLUSIVE"))
        self.assertEqual(rules(statement), ["pg.create_table", "pg.lock_timeout"])

    def test_create_partition_locks_the_parent(self):
        found = table("CREATE TABLE p2 PARTITION OF t FOR VALUES IN (2)")
        self.assertEqual((found.table, found.lock), ("t", "ACCESS EXCLUSIVE"))


class DroppedForeignKeyTestCase(unittest.TestCase):
    """
    A statement that drops or re-creates a foreign key locks the table
    at the key's other end, which the schema read names.
    """

    def locks(self, sql, context=FIXTURE_CONTEXT):
        statement = impact(sql, context)
        return {(t.table, t.lock, t.work, t.rule) for t in statement.tables}

    def test_drop_table_locks_what_its_keys_reference(self):
        self.assertEqual(
            self.locks("DROP TABLE t"),
            {
                ("t", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_table"),
                ("r", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_foreign_key"),
            },
        )
        # Without the schema, the statement names only t.
        self.assertEqual({t.table for t in impact("DROP TABLE t").tables}, {"t"})

    def test_drop_table_cascade_locks_the_tables_whose_keys_point_at_it(self):
        self.assertEqual({t for t, *_ in self.locks("DROP TABLE r")}, {"r"})
        self.assertEqual(
            self.locks("DROP TABLE r CASCADE"),
            {
                ("r", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_table"),
                ("t", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_foreign_key"),
            },
        )

    def test_dropping_both_ends_locks_each_once(self):
        self.assertEqual(
            self.locks("DROP TABLE t, r"),
            {
                ("t", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_table"),
                ("r", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_table"),
            },
        )

    def test_a_renamed_table_reads_its_keys_under_the_old_name(self):
        report = analyze(
            [
                MigrationStatement("ALTER TABLE t RENAME TO u", "m1"),
                MigrationStatement("DROP TABLE u", "m2"),
            ],
            PG,
            FIXTURE_CONTEXT,
        )
        (statement,) = report.migrations[1].statements
        self.assertEqual({t.table for t in statement.tables}, {"u", "r"})

    def test_truncate_cascade_empties_every_table_that_points_at_it(self):
        self.assertEqual({t for t, *_ in self.locks("TRUNCATE t")}, {"t"})
        self.assertEqual({t for t, *_ in self.locks("TRUNCATE r")}, {"r"})
        schema = Snapshot(
            {
                **FIXTURE_SCHEMA,
                "q": IntrospectedTable(
                    {"t_id": IntrospectedColumn("integer", True, False)},
                    foreign_keys={
                        "q_t_id_fkey": IntrospectedForeignKey(("t_id",), "t", ("id",))
                    },
                    name="q",
                ),
            }
        )
        context = FIXTURE_CONTEXT._replace(schema=schema)
        self.assertEqual(
            self.locks("TRUNCATE r CASCADE", context),
            {
                ("r", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_table"),
                ("t", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_table"),
                ("q", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_table"),
            },
        )

    def test_drop_constraint_of_a_foreign_key_locks_its_table(self):
        self.assertIn(
            ("r", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_foreign_key"),
            self.locks("ALTER TABLE t DROP CONSTRAINT t_r_id_fkey"),
        )
        self.assertEqual(
            {t for t, *_ in self.locks("ALTER TABLE t DROP CONSTRAINT ck")}, {"t"}
        )

    def test_drop_constraint_cascade_of_a_key_locks_the_tables_pointing_at_it(self):
        self.assertEqual(
            {t for t, *_ in self.locks("ALTER TABLE r DROP CONSTRAINT r_pkey")}, {"r"}
        )
        self.assertEqual(
            {t for t, *_ in self.locks("ALTER TABLE r DROP CONSTRAINT r_pkey CASCADE")},
            {"r", "t"},
        )
        # A check constraint locks nothing more.
        self.assertEqual(
            {t for t, *_ in self.locks("ALTER TABLE t DROP CONSTRAINT ck CASCADE")},
            {"t"},
        )

    def test_drop_column_drops_the_keys_that_use_it(self):
        self.assertIn(
            ("r", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_foreign_key"),
            self.locks("ALTER TABLE t DROP COLUMN r_id"),
        )
        self.assertEqual(
            {t for t, *_ in self.locks("ALTER TABLE t DROP COLUMN c")}, {"t"}
        )
        self.assertEqual(
            {t for t, *_ in self.locks("ALTER TABLE r DROP COLUMN id CASCADE")},
            {"r", "t"},
        )

    def test_a_type_change_re_creates_the_keys_on_the_column(self):
        self.assertIn(
            ("r", "ACCESS EXCLUSIVE", Work.CATALOG, "pg.drop_foreign_key"),
            self.locks("ALTER TABLE t ALTER COLUMN r_id TYPE bigint"),
        )
        statement = impact("ALTER TABLE r ALTER COLUMN id TYPE bigint", FIXTURE_CONTEXT)
        found = next(t for t in statement.tables if t.table == "t")
        self.assertEqual((found.lock, found.work), ("ACCESS EXCLUSIVE", Work.SCAN))
        self.assertEqual(statement.confidence, Confidence.LIKELY)
        finding = next(f for f in statement.findings if f.rule == "pg.drop_foreign_key")
        self.assertIn("the foreign key from t to r is re-created", finding.message)


class RemedyTestCase(unittest.TestCase):
    def remedy(self, sql, rule):
        statement = impact(sql)
        return next(f for f in statement.findings if f.rule == rule).remedy

    def test_create_index(self):
        self.assertEqual(
            self.remedy('CREATE UNIQUE INDEX "ix" ON "t" ("c");', "pg.create_index"),
            ('CREATE UNIQUE INDEX CONCURRENTLY "ix" ON "t" ("c")',),
        )

    def test_drop_index(self):
        self.assertEqual(
            self.remedy("DROP INDEX IF EXISTS ix", "pg.drop_index"),
            ("DROP INDEX CONCURRENTLY IF EXISTS ix",),
        )

    def test_drop_of_several_indexes_has_no_remedy(self):
        # CONCURRENTLY drops one index at a time.
        self.assertEqual(self.remedy("DROP INDEX a, b", "pg.drop_index"), ())

    def test_reindex(self):
        self.assertEqual(
            self.remedy("REINDEX (VERBOSE) TABLE t", "pg.reindex"),
            ("REINDEX (VERBOSE) TABLE CONCURRENTLY t",),
        )

    def test_refresh(self):
        self.assertEqual(
            self.remedy("REFRESH MATERIALIZED VIEW t", "pg.refresh_materialized_view"),
            ("REFRESH MATERIALIZED VIEW CONCURRENTLY t",),
        )

    def test_set_not_null(self):
        self.assertEqual(
            self.remedy(
                'ALTER TABLE "app"."Orders" ALTER COLUMN "Paid" SET NOT NULL',
                "pg.set_not_null",
            ),
            (
                'ALTER TABLE app."Orders" ADD CONSTRAINT "Orders_Paid_not_null" '
                'CHECK ("Paid" IS NOT NULL) NOT VALID',
                'ALTER TABLE app."Orders" VALIDATE CONSTRAINT "Orders_Paid_not_null"',
                'ALTER TABLE app."Orders" ALTER COLUMN "Paid" SET NOT NULL',
                'ALTER TABLE app."Orders" DROP CONSTRAINT "Orders_Paid_not_null"',
            ),
        )

    def test_primary_key(self):
        self.assertEqual(
            self.remedy("ALTER TABLE t ADD PRIMARY KEY (a, b)", "pg.add_key"),
            (
                "CREATE UNIQUE INDEX CONCURRENTLY t_pkey_idx ON t (a, b)",
                "ALTER TABLE t ADD CONSTRAINT t_pkey PRIMARY KEY USING INDEX t_pkey_idx",
            ),
        )

    def test_unique(self):
        self.assertEqual(
            self.remedy("ALTER TABLE t ADD CONSTRAINT uq UNIQUE (a)", "pg.add_key"),
            (
                "CREATE UNIQUE INDEX CONCURRENTLY uq_idx ON t (a)",
                "ALTER TABLE t ADD CONSTRAINT uq UNIQUE USING INDEX uq_idx",
            ),
        )

    def test_volatile_default(self):
        self.assertEqual(
            self.remedy(
                "ALTER TABLE t ADD COLUMN c uuid DEFAULT gen_random_uuid()",
                "pg.add_column.rewrite",
            ),
            (
                "ALTER TABLE t ADD COLUMN c uuid",
                "ALTER TABLE t ALTER COLUMN c SET DEFAULT gen_random_uuid()",
            ),
        )

    def test_a_statement_with_several_actions_has_no_remedy(self):
        statement = impact(
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0), ALTER COLUMN c SET NOT NULL"
        )
        self.assertTrue(
            all(
                f.remedy == ()
                for f in statement.findings
                if f.rule != "pg.lock_timeout"
            )
        )
        self.assertEqual(
            table(
                "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0), ALTER COLUMN c SET NOT NULL"
            ).work,
            Work.SCAN,
        )


class DefaultVolatilityTestCase(unittest.TestCase):
    def test_an_unknown_function_counts_as_volatile_and_lowers_confidence(self):
        statement = impact("ALTER TABLE t ADD COLUMN c integer DEFAULT my_func()")
        self.assertEqual(statement.tables[0].work, Work.REWRITE)
        self.assertEqual(statement.confidence, Confidence.LIKELY)
        finding = next(
            f for f in statement.findings if f.rule == "pg.add_column.rewrite"
        )
        self.assertIn("my_func()", finding.message)

    def test_a_known_volatile_function_is_certain(self):
        statement = impact("ALTER TABLE t ADD COLUMN c float DEFAULT random()")
        self.assertEqual(statement.confidence, Confidence.KNOWN)
        self.assertIn("random()", statement.findings[0].message)


class TypeChangeTestCase(unittest.TestCase):
    def work(self, from_type, to_type, settings=None):
        return pg.type_change(from_type, to_type, settings or {})[:2]

    def test_binary_coercible_changes(self):
        for old, new in [
            ("varchar(10)", "varchar(20)"),
            ("character varying(10)", "text"),
            ("text", "varchar"),
            ("varchar(10)", "VARCHAR(10)"),
            ("numeric(10,2)", "numeric(12,2)"),
            ("numeric(10,2)", "numeric"),
            ("decimal(10)", "numeric(12)"),
            ("integer", "int4"),
        ]:
            with self.subTest(old=old, new=new):
                self.assertEqual(self.work(old, new), (Work.CATALOG, Confidence.KNOWN))

    def test_rewrites(self):
        for old, new in [
            ("varchar(20)", "varchar(10)"),
            ("text", "varchar(10)"),
            ("integer", "bigint"),
            ("numeric(10,2)", "numeric(12,3)"),
            ("numeric", "numeric(10,2)"),
            ("json", "jsonb"),
            ("integer[]", "bigint[]"),
            ("varchar(10)", "varchar(20)[]"),
        ]:
            with self.subTest(old=old, new=new):
                self.assertEqual(self.work(old, new), (Work.REWRITE, Confidence.KNOWN))

    def test_an_unknown_type_is_a_likely_rewrite(self):
        self.assertEqual(self.work("text", "citext"), (Work.REWRITE, Confidence.LIKELY))
        self.assertEqual(
            self.work("varchar(x)", "varchar(10)"), (Work.REWRITE, Confidence.LIKELY)
        )

    def test_timestamp_to_timestamptz_depends_on_the_time_zone(self):
        self.assertEqual(
            self.work("timestamp without time zone", "timestamp with time zone"),
            (Work.REWRITE, Confidence.LIKELY),
        )
        self.assertEqual(
            self.work("timestamp", "timestamptz", {"TimeZone": "UTC"}),
            (Work.CATALOG, Confidence.KNOWN),
        )
        self.assertEqual(
            self.work("timestamp", "timestamptz", {"TimeZone": "Europe/Paris"}),
            (Work.REWRITE, Confidence.KNOWN),
        )

    def generated(self, from_type, to_type, using=None):
        sql = f"ALTER TABLE t ALTER COLUMN c TYPE {to_type}"
        if using:
            sql += f" USING {using}"
        return with_intent(
            sql, "alter_column_type", "t", "c", from_type=from_type, to_type=to_type
        )

    def test_the_intent_supplies_the_current_type(self):
        found = table(self.generated("varchar(10)", "varchar(40)"))
        self.assertEqual(
            (found.work, found.rule),
            (Work.CATALOG, "pg.alter_column_type.binary_coercible"),
        )
        found = table(self.generated("integer", "bigint"))
        self.assertEqual(found.work, Work.REWRITE)

    def test_a_trivial_using_clause_keeps_the_change_coercible(self):
        found = table(self.generated("varchar(10)", "text", using='"c"::text'))
        self.assertEqual(found.work, Work.CATALOG)

    def test_a_computing_using_clause_rewrites(self):
        statement = impact(self.generated("varchar(10)", "text", using="upper(c)"))
        self.assertEqual(statement.tables[0].work, Work.REWRITE)
        self.assertEqual(statement.confidence, Confidence.KNOWN)
        statement = impact(self.generated("varchar(10)", "text", using="d::text"))
        self.assertEqual(statement.tables[0].work, Work.REWRITE)

    def test_without_the_current_type_the_change_is_a_likely_rewrite(self):
        statement = impact("ALTER TABLE t ALTER COLUMN c TYPE varchar(40)")
        self.assertEqual(statement.tables[0].work, Work.REWRITE)
        self.assertEqual(statement.confidence, Confidence.LIKELY)
        self.assertIn("current type is not known", statement.findings[0].message)

    def test_the_schema_supplies_the_current_type(self):
        sql = "ALTER TABLE app.t ALTER COLUMN NAME TYPE text"
        self.assertEqual(table(sql, context=FIXTURE_CONTEXT).work, Work.CATALOG)
        sql = "ALTER TABLE t ALTER COLUMN missing TYPE text"
        self.assertEqual(table(sql, context=FIXTURE_CONTEXT).work, Work.REWRITE)
        sql = "ALTER TABLE u ALTER COLUMN name TYPE text"
        self.assertEqual(table(sql, context=FIXTURE_CONTEXT).work, Work.REWRITE)

    def test_the_context_time_zone_reaches_the_rule(self):
        context = EngineContext("postgres", (16,), settings={"TimeZone": "UTC"})
        found = table(self.generated("timestamp", "timestamptz"), context=context)
        self.assertEqual(found.work, Work.CATALOG)


class IndexTableTestCase(unittest.TestCase):
    def test_drop_index_names_the_table_the_run_created_it_on(self):
        report = analyze(
            [
                MigrationStatement("CREATE INDEX ix ON t (c)", "m1"),
                MigrationStatement("DROP INDEX ix", "m2"),
            ],
            PG,
        )
        self.assertEqual(report.migrations[1].statements[0].tables[0].table, "t")

    def test_drop_index_reads_the_table_from_the_schema(self):
        self.assertEqual(table("DROP INDEX ix", context=FIXTURE_CONTEXT).table, "t")
        self.assertEqual(table("DROP INDEX app.ix", context=FIXTURE_CONTEXT).table, "t")
        found = table("REINDEX INDEX ix", context=FIXTURE_CONTEXT)
        self.assertEqual(found.table, "t")

    def test_drop_index_reads_the_table_from_its_intent(self):
        statement = with_intent("DROP INDEX ix", "drop_index", "orders", name="ix")
        self.assertEqual(table(statement).table, "orders")

    def test_an_unknown_index_table_is_labelled(self):
        self.assertEqual(table("DROP INDEX ix").table, "(table of index ix)")
        self.assertEqual(table("REINDEX INDEX ix").table, "(table of index ix)")

    def test_reindex_of_a_schema(self):
        self.assertEqual(
            table("REINDEX SCHEMA app").table, "(every table in schema app)"
        )

    def test_drop_index_concurrently(self):
        found = table("DROP INDEX CONCURRENTLY ix")
        self.assertEqual(
            (found.lock, found.blocks), ("SHARE UPDATE EXCLUSIVE", Blocks.DDL)
        )


class PartitionTestCase(unittest.TestCase):
    def test_attach_scans_the_partition(self):
        statement = impact("ALTER TABLE t ATTACH PARTITION p FOR VALUES IN (1)")
        self.assertEqual(
            {(t.table, t.lock, t.work) for t in statement.tables},
            {
                ("t", "SHARE UPDATE EXCLUSIVE", Work.CATALOG),
                ("p", "ACCESS EXCLUSIVE", Work.SCAN),
            },
        )
        self.assertEqual(statement.confidence, Confidence.LIKELY)

    def test_detach(self):
        statement = impact("ALTER TABLE t DETACH PARTITION p")
        self.assertEqual({t.lock for t in statement.tables}, {"ACCESS EXCLUSIVE"})

    def test_detach_concurrently_needs_14(self):
        statement = impact("ALTER TABLE t DETACH PARTITION p CONCURRENTLY")
        self.assertEqual({t.lock for t in statement.tables}, {"SHARE UPDATE EXCLUSIVE"})
        finding = statement.findings[0]
        self.assertEqual(finding.severity, Severity.WARN)
        self.assertIn("assumed server version is 12", finding.message)
        read = EngineContext("postgres", (13,), read=frozenset({"version"}))
        self.assertIn(
            "the server version is 13",
            impact("ALTER TABLE t DETACH PARTITION p CONCURRENTLY", read)
            .findings[0]
            .message,
        )
        newer = impact(
            "ALTER TABLE t DETACH PARTITION p CONCURRENTLY",
            EngineContext("postgres", (14,)),
        )
        self.assertEqual(newer.findings, ())


class NoTableTestCase(unittest.TestCase):
    def test_statements_on_no_table(self):
        for sql in [
            "CREATE TYPE mood AS ENUM ('a')",
            "DROP TYPE mood",
            "ALTER TYPE mood ADD VALUE 'b'",
            "ALTER TYPE mood RENAME VALUE 'a' TO 'c'",
            "CREATE VIEW v AS SELECT 1",
            "CREATE SCHEMA s",
            "DROP SEQUENCE s",
            "COMMENT ON SCHEMA s IS 'x'",
            "SET statement_timeout = 0",
        ]:
            with self.subTest(sql=sql):
                statement = impact(sql)
                self.assertEqual(statement.tables, ())
                self.assertEqual(statement.findings, ())
                self.assertEqual(statement.confidence, Confidence.KNOWN)

    def test_statements_on_every_table(self):
        for sql in ["VACUUM", "CLUSTER", "DROP SCHEMA s CASCADE"]:
            with self.subTest(sql=sql):
                statement = impact(sql)
                self.assertEqual(statement.tables, ())
                self.assertEqual(statement.confidence, Confidence.LIKELY)
                self.assertEqual(statement.findings[0].severity, Severity.INFO)


class UnknownTestCase(unittest.TestCase):
    def test_another_engines_action_is_unknown(self):
        statement = impact("ALTER TABLE t MODIFY c bigint")
        self.assertEqual(statement.confidence, Confidence.UNKNOWN)
        self.assertEqual(statement.tables, ())
        self.assertEqual(rules(statement), ["impact.unknown"])

    def test_another_engines_statement_is_unknown(self):
        statement = impact("OPTIMIZE TABLE t")
        self.assertEqual(statement.confidence, Confidence.UNKNOWN)
        self.assertIn("optimize_table", statement.findings[0].message)

    def test_an_unreadable_statement_is_unknown(self):
        statement = impact("GRANT SELECT ON t TO someone")
        self.assertIsNone(statement.parsed)
        self.assertEqual(statement.confidence, Confidence.UNKNOWN)
        self.assertEqual(statement.findings[0].rule, "impact.unknown")


class NotesTestCase(unittest.TestCase):
    def test_a_rename_breaks_running_code(self):
        statement = impact("ALTER TABLE t RENAME COLUMN c TO d")
        note = next(f for f in statement.findings if f.rule == "pg.rename")
        self.assertEqual(note.severity, Severity.INFO)
        self.assertIn("column c", note.message)
        statement = impact("ALTER TABLE t RENAME TO u")
        self.assertIn("table t", statement.findings[0].message)

    def test_create_index_concurrently_notes_the_invalid_index(self):
        statement = impact("CREATE INDEX CONCURRENTLY ix ON t (c)")
        self.assertEqual(rules(statement), ["pg.create_index.concurrently"])
        self.assertIn("invalid index", statement.findings[0].message)

    def test_nowait_needs_no_timeout(self):
        statement = impact("LOCK TABLE t IN ACCESS EXCLUSIVE MODE NOWAIT")
        self.assertNotIn("pg.lock_timeout", rules(statement))
        self.assertIn("pg.lock_timeout", rules(impact("LOCK TABLE t")))

    def test_the_backfill_names_itself(self):
        statement = impact(with_intent("UPDATE t SET c = 0", "backfill", "t", "c"))
        self.assertIn("the backfill", statement.findings[0].message)
        statement = impact(
            MigrationStatement("DELETE FROM t", "m1", transactional=False)
        )
        self.assertIn("until it ends", statement.findings[0].message)


if __name__ == "__main__":
    unittest.main()
