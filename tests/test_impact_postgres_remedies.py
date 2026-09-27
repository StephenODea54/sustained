"""Tests for the PostgreSQL remedies, default volatility, type changes, and findings."""

import unittest

from sustained.analysis import MigrationStatement, with_intent
from sustained.impact import (
    Confidence,
    EngineContext,
    Severity,
    Work,
)
from sustained.impact.rules import postgres as pg
from tests.test_impact_postgres import (
    FIXTURE_CONTEXT,
    impact,
    rules,
    table,
)


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
                'ALTER TABLE "app"."Orders" ADD CONSTRAINT "Orders_Paid_not_null" '
                'CHECK ("Paid" IS NOT NULL) NOT VALID',
                'ALTER TABLE "app"."Orders" VALIDATE CONSTRAINT "Orders_Paid_not_null"',
                'ALTER TABLE "app"."Orders" ALTER COLUMN "Paid" SET NOT NULL',
                'ALTER TABLE "app"."Orders" DROP CONSTRAINT "Orders_Paid_not_null"',
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
        context = EngineContext(
            "postgres",
            (16,),
            settings={"TimeZone": "UTC"},
            read=frozenset({"settings", "indexes"}),
        )
        found = table(self.generated("timestamp", "timestamptz"), context=context)
        self.assertEqual(found.work, Work.CATALOG)


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
                statement = impact(MigrationStatement(sql, "m1", transactional=False))
                self.assertEqual(statement.tables, ())
                self.assertEqual(statement.confidence, Confidence.LIKELY)
                self.assertEqual(
                    [f.severity for f in statement.findings], [Severity.INFO]
                )


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
        statement = impact(
            MigrationStatement(
                "CREATE INDEX CONCURRENTLY ix ON t (c)", "m1", transactional=False
            )
        )
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
