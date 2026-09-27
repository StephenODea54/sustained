"""Tests for the PostgreSQL rules that read the schema."""

import unittest

from sustained.analysis import MigrationStatement, with_intent
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    Severity,
    Work,
    analyze,
)
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedTable,
    Snapshot,
)
from tests.test_impact_postgres import (
    FIXTURE_CONTEXT,
    FIXTURE_SCHEMA,
    PG,
    impact,
    rules,
    table,
)


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


class ProvenNotNullTestCase(unittest.TestCase):
    CHECK = "ALTER TABLE t ADD CONSTRAINT k CHECK (c IS NOT NULL)"
    SET = "ALTER TABLE t ALTER COLUMN c SET NOT NULL"

    def work(self, *statements, context=None):
        """The work and rule of the last statement of a run."""
        found = analyze(list(statements), PG, context).statements[-1]
        (only,) = found.tables
        return only.work, only.rule

    def test_a_valid_check_the_run_added_proves_it(self):
        self.assertEqual(
            self.work(self.CHECK, self.SET),
            (Work.CATALOG, "pg.set_not_null.proven"),
        )

    def test_a_not_valid_check_proves_it_once_validated(self):
        added = f"{self.CHECK} NOT VALID"
        self.assertEqual(self.work(added, self.SET), (Work.SCAN, "pg.set_not_null"))
        self.assertEqual(
            self.work(added, "ALTER TABLE t VALIDATE CONSTRAINT k", self.SET),
            (Work.CATALOG, "pg.set_not_null.proven"),
        )

    def test_a_dropped_check_proves_nothing(self):
        for drop in (
            "ALTER TABLE t DROP CONSTRAINT k",
            "ALTER TABLE t DROP COLUMN c",
        ):
            with self.subTest(drop):
                self.assertEqual(
                    self.work(self.CHECK, drop, self.SET),
                    (Work.SCAN, "pg.set_not_null"),
                )

    def test_a_check_of_another_column_or_table_proves_nothing(self):
        self.assertEqual(
            self.work("ALTER TABLE t ADD CHECK (d IS NOT NULL)", self.SET),
            (Work.SCAN, "pg.set_not_null"),
        )
        self.assertEqual(
            self.work("ALTER TABLE u ADD CHECK (c IS NOT NULL)", self.SET),
            (Work.SCAN, "pg.set_not_null"),
        )

    def test_the_check_follows_a_rename(self):
        self.assertEqual(
            self.work(
                self.CHECK,
                "ALTER TABLE t RENAME TO t2",
                "ALTER TABLE t2 ALTER COLUMN c SET NOT NULL",
            ),
            (Work.CATALOG, "pg.set_not_null.proven"),
        )

    def test_a_valid_check_in_the_schema_proves_it(self):
        self.assertEqual(
            self.work(
                "ALTER TABLE t ALTER COLUMN name SET NOT NULL",
                context=FIXTURE_CONTEXT,
            ),
            (Work.CATALOG, "pg.set_not_null.proven"),
        )

    def test_a_not_valid_check_in_the_schema_proves_nothing(self):
        schema = Snapshot(
            {
                "t": IntrospectedTable(
                    {"c": IntrospectedColumn("integer", True, False)},
                    checks={"k": "((c IS NOT NULL)) NOT VALID"},
                    check_names={"k": "k"},
                    name="t",
                )
            }
        )
        context = EngineContext("postgres", (18,), schema=schema)
        self.assertEqual(
            self.work(self.SET, context=context), (Work.SCAN, "pg.set_not_null")
        )

    NAME = "ALTER TABLE t ALTER COLUMN name SET NOT NULL"

    def test_a_schema_check_the_run_dropped_proves_nothing(self):
        for steps in (
            ["ALTER TABLE t DROP CONSTRAINT name_present"],
            [
                "ALTER TABLE t RENAME CONSTRAINT name_present TO present",
                "ALTER TABLE t DROP CONSTRAINT present",
            ],
        ):
            with self.subTest(steps):
                self.assertEqual(
                    self.work(*steps, self.NAME, context=FIXTURE_CONTEXT),
                    (Work.SCAN, "pg.set_not_null"),
                )

    def test_a_renamed_schema_check_still_proves_it(self):
        self.assertEqual(
            self.work(
                "ALTER TABLE t RENAME CONSTRAINT name_present TO present",
                "ALTER TABLE t RENAME TO t2",
                "ALTER TABLE t2 ALTER COLUMN name SET NOT NULL",
                context=FIXTURE_CONTEXT,
            ),
            (Work.CATALOG, "pg.set_not_null.proven"),
        )

    def test_a_schema_check_follows_its_column_through_renames(self):
        # The check tests the column now named label, and the column
        # now named name is the schema's c, which it does not test.
        steps = [
            "ALTER TABLE t RENAME COLUMN name TO label",
            "ALTER TABLE t RENAME COLUMN c TO name",
        ]
        self.assertEqual(
            self.work(*steps, self.NAME, context=FIXTURE_CONTEXT),
            (Work.SCAN, "pg.set_not_null"),
        )
        self.assertEqual(
            self.work(
                *steps,
                "ALTER TABLE t ALTER COLUMN label SET NOT NULL",
                context=FIXTURE_CONTEXT,
            ),
            (Work.CATALOG, "pg.set_not_null.proven"),
        )
        self.assertEqual(
            self.work(
                "ALTER TABLE t DROP COLUMN name",
                "ALTER TABLE t ADD COLUMN name text",
                self.NAME,
                context=FIXTURE_CONTEXT,
            ),
            (Work.SCAN, "pg.set_not_null"),
        )

    def test_a_table_the_run_made_again_has_none_of_the_schema_checks(self):
        found = analyze(
            [
                "DROP TABLE t",
                "CREATE TABLE t (id int, name text)",
                "INSERT INTO t SELECT id, name FROM r",
                self.NAME,
            ],
            PG,
            FIXTURE_CONTEXT,
        ).statements[-1]
        self.assertEqual(found.tables[0].rule, "pg.set_not_null")

    def test_a_check_the_run_added_follows_a_column_rename(self):
        self.assertEqual(
            self.work(
                self.CHECK,
                "ALTER TABLE t RENAME COLUMN c TO d",
                "ALTER TABLE t ALTER COLUMN d SET NOT NULL",
            ),
            (Work.CATALOG, "pg.set_not_null.proven"),
        )
        self.assertEqual(
            self.work(
                self.CHECK,
                "ALTER TABLE t RENAME CONSTRAINT k TO k2",
                "ALTER TABLE t DROP CONSTRAINT k2",
                self.SET,
            ),
            (Work.SCAN, "pg.set_not_null"),
        )


if __name__ == "__main__":
    unittest.main()
