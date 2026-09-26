"""Tests for the MySQL and MariaDB rules for ALTER TABLE and CREATE INDEX."""

import unittest

from sustained.analysis import with_intent
from sustained.impact import (
    Blocks,
    Confidence,
    Severity,
    TableStats,
    Work,
    analyze,
)
from sustained.impact.rules import mysql
from sustained.impact.rules.mysql.column_types import length_bytes
from tests.test_impact_mysql import (
    _STATS,
    MARIADB,
    MY,
    MYSQL,
    context,
    impact,
    rules,
    table,
)


class ColumnTestCase(unittest.TestCase):
    def test_an_added_column_is_instant(self):
        found = table("ALTER TABLE t ADD COLUMN d int")
        self.assertEqual(found.lock, "INSTANT")
        self.assertEqual(found.blocks, Blocks.READS_AND_WRITES)
        self.assertEqual(found.work, Work.CATALOG)
        self.assertEqual(found.rule, "mysql.add_column.instant")

    def test_a_column_added_in_place_before_8_0_29_rebuilds(self):
        sql = "ALTER TABLE t ADD COLUMN d int AFTER c"
        old = table(sql, ctx=context(version=(8, 0, 28)))
        new = table(sql, ctx=context(version=(8, 0, 29)))
        self.assertEqual((old.lock, old.work), ("INPLACE, LOCK=NONE", Work.REWRITE))
        self.assertEqual((new.lock, new.work), ("INSTANT", Work.CATALOG))

    def test_row_versions_at_the_limit_rebuild(self):
        sql = "ALTER TABLE t ADD COLUMN d int"
        full = dict(_STATS, t=_STATS["t"]._replace(row_versions=64))
        at_64 = context()._replace(tables=full)
        self.assertEqual(table(sql, ctx=at_64).lock, "INPLACE, LOCK=NONE")
        self.assertIn(
            "all 64 instant row versions", impact(sql, at_64).findings[0].message
        )
        # 9.1 raised the limit to 255.
        at_64_on_9 = at_64._replace(version=(9, 1, 0))
        self.assertEqual(table(sql, ctx=at_64_on_9).lock, "INSTANT")
        # MariaDB counts none.
        self.assertEqual(table(sql, ctx=MARIADB._replace(tables=full)).lock, "INSTANT")

    def test_a_fulltext_table_copies_on_mysql_and_rebuilds_on_mariadb(self):
        sql = "ALTER TABLE ft ADD COLUMN d int"
        self.assertEqual(table(sql, "ft").lock, "COPY, LOCK=SHARED")
        self.assertEqual(table(sql, "ft", MARIADB).lock, "INPLACE, LOCK=SHARED")

    def test_a_compressed_table_rebuilds(self):
        found = table("ALTER TABLE cz ADD COLUMN d int", "cz")
        self.assertEqual((found.lock, found.work), ("INPLACE, LOCK=NONE", Work.REWRITE))

    def test_an_expression_default_copies_on_mysql(self):
        sql = "ALTER TABLE t ADD COLUMN d int DEFAULT (1 + 1)"
        self.assertEqual(table(sql).lock, "COPY, LOCK=SHARED")
        self.assertEqual(table(sql, ctx=MARIADB).lock, "INSTANT")
        uuid = "ALTER TABLE t ADD COLUMN d varchar(36) DEFAULT (uuid())"
        self.assertEqual(table(uuid, ctx=MARIADB).lock, "COPY, LOCK=NONE")

    def test_mariadb_copies_under_a_shared_lock_before_11_2(self):
        sql = "ALTER TABLE t MODIFY COLUMN c bigint"
        old = context("mariadb", (11, 1, 2))
        self.assertEqual(table(sql, ctx=old).lock, "COPY, LOCK=SHARED")
        self.assertEqual(table(sql, ctx=MARIADB).lock, "COPY, LOCK=NONE")

    def test_a_copy_blocks_writes_on_a_big_table(self):
        big = dict(_STATS, t=TableStats(5_000_000, 3 << 30, "DYNAMIC", 0, False))
        statement = impact(
            "ALTER TABLE t MODIFY COLUMN c bigint", MYSQL._replace(tables=big)
        )
        (found,) = [
            f for f in statement.findings if f.rule == "mysql.modify_column.copy"
        ]
        self.assertIs(found.severity, Severity.DANGER)
        self.assertIn("gh-ost", found.message)
        self.assertIn("COPY, LOCK=SHARED", found.message)

    def test_a_column_added_with_a_check_or_a_reference(self):
        check = "ALTER TABLE t ADD COLUMN d int CHECK (d > 0)"
        self.assertEqual(table(check).lock, "COPY, LOCK=SHARED")
        refs = "ALTER TABLE t ADD COLUMN d int REFERENCES r (id)"
        self.assertEqual(table(refs).lock, "COPY, LOCK=SHARED")
        unchecked = context(foreign_key_checks="0")
        self.assertEqual(table(refs, ctx=unchecked).lock, "INSTANT")

    def test_a_column_is_dropped_instantly_on_8_0_29(self):
        sql = "ALTER TABLE t DROP COLUMN name"
        self.assertEqual(table(sql).lock, "INSTANT")
        old = table(sql, ctx=context(version=(8, 0, 28)))
        self.assertEqual((old.lock, old.work), ("INPLACE, LOCK=NONE", Work.REWRITE))

    def test_an_indexed_column_is_dropped_in_place(self):
        sql = "ALTER TABLE t DROP COLUMN c"
        self.assertEqual(table(sql).lock, "INPLACE, LOCK=NONE")
        self.assertEqual(table(sql).work, Work.REWRITE)
        self.assertEqual(table(sql, ctx=MARIADB).lock, "NOCOPY, LOCK=NONE")
        self.assertEqual(table(sql, ctx=MARIADB).work, Work.CATALOG)

    def test_without_the_schema_a_dropped_column_is_likely_instant(self):
        sql = "ALTER TABLE t DROP COLUMN name"
        statement = impact(sql, MYSQL._replace(schema=None))
        self.assertEqual(statement.tables[0].lock, "INSTANT")
        self.assertIs(statement.confidence, Confidence.LIKELY)

    def test_a_rename_is_instant_from_8_0_28(self):
        sql = "ALTER TABLE t RENAME COLUMN name TO label"
        self.assertEqual(table(sql).lock, "INSTANT")
        self.assertEqual(
            table(sql, ctx=context(version=(8, 0, 27))).lock, "INPLACE, LOCK=NONE"
        )
        self.assertIn("fails once the rename runs", impact(sql).findings[0].message)


class ModifyTestCase(unittest.TestCase):
    CASES = [
        ("MODIFY COLUMN c int", "INSTANT", "INSTANT", Work.CATALOG),
        ("MODIFY COLUMN c int(11)", "INSTANT", "INSTANT", Work.CATALOG),
        ("MODIFY COLUMN c int COMMENT 'note'", "INSTANT", "INSTANT", Work.CATALOG),
        ("MODIFY COLUMN e enum('a','b','c')", "INSTANT", "INSTANT", Work.CATALOG),
        (
            "MODIFY COLUMN e enum('b','a')",
            "COPY, LOCK=SHARED",
            "COPY, LOCK=NONE",
            Work.REWRITE,
        ),
        (
            "MODIFY COLUMN name varchar(200)",
            "INPLACE, LOCK=NONE",
            "INSTANT",
            Work.CATALOG,
        ),
        (
            "MODIFY COLUMN small varchar(63)",
            "INPLACE, LOCK=NONE",
            "INSTANT",
            Work.CATALOG,
        ),
        ("MODIFY COLUMN small varchar(64)", "COPY, LOCK=SHARED", "INSTANT", None),
        (
            "MODIFY COLUMN name varchar(50)",
            "COPY, LOCK=SHARED",
            "COPY, LOCK=NONE",
            Work.REWRITE,
        ),
        (
            "MODIFY COLUMN c int NOT NULL",
            "INPLACE, LOCK=NONE",
            "INPLACE, LOCK=NONE",
            Work.REWRITE,
        ),
        (
            "MODIFY COLUMN nn int NULL DEFAULT 0",
            "INPLACE, LOCK=NONE",
            "INPLACE, LOCK=NONE",
            Work.REWRITE,
        ),
        ("MODIFY COLUMN c int FIRST", "INPLACE, LOCK=NONE", "INSTANT", None),
        (
            "MODIFY COLUMN c bigint",
            "COPY, LOCK=SHARED",
            "COPY, LOCK=NONE",
            Work.REWRITE,
        ),
        (
            "MODIFY COLUMN c int AUTO_INCREMENT",
            "COPY, LOCK=SHARED",
            "COPY, LOCK=NONE",
            Work.REWRITE,
        ),
        ("CHANGE COLUMN name label varchar(100)", "INSTANT", "INSTANT", Work.CATALOG),
        (
            "CHANGE COLUMN name label text",
            "COPY, LOCK=SHARED",
            "COPY, LOCK=NONE",
            Work.REWRITE,
        ),
    ]

    def test_each_change_against_the_schema_read(self):
        for action, on_mysql, on_mariadb, work in self.CASES:
            sql = f"ALTER TABLE t {action}"
            with self.subTest(sql=sql):
                self.assertEqual(table(sql).lock, on_mysql)
                self.assertEqual(table(sql, ctx=MARIADB).lock, on_mariadb)
                if work is not None:
                    self.assertEqual(table(sql).work, work)

    def test_a_length_prefix_read_without_the_character_set(self):
        self.assertEqual(length_bytes(20, 63, None), (True, Confidence.KNOWN))
        self.assertEqual(length_bytes(20, 64, None), (False, Confidence.LIKELY))
        self.assertEqual(
            length_bytes(20, 200, "latin1_swedish_ci"), (True, Confidence.KNOWN)
        )
        self.assertEqual(length_bytes(300, 400, None), (True, Confidence.KNOWN))

    def test_without_the_schema_a_modify_counts_as_a_copy(self):
        statement = impact(
            "ALTER TABLE t MODIFY COLUMN c int", MYSQL._replace(schema=None)
        )
        self.assertEqual(statement.tables[0].lock, "COPY, LOCK=SHARED")
        self.assertIs(statement.confidence, Confidence.LIKELY)
        self.assertIn("not known", statement.findings[0].message)

    def test_the_diffs_intent_names_the_change(self):
        no_schema = MYSQL._replace(schema=None)
        retype = with_intent(
            "ALTER TABLE `t` MODIFY COLUMN `small` VARCHAR(64) NULL",
            "alter_column_type",
            "t",
            "small",
            from_type="varchar(20)",
            to_type="VARCHAR(64)",
        )
        self.assertEqual(table(retype, ctx=no_schema).lock, "COPY, LOCK=SHARED")
        tighten = with_intent(
            "ALTER TABLE `t` MODIFY COLUMN `c` INT NOT NULL", "set_not_null", "t", "c"
        )
        self.assertEqual(table(tighten, ctx=no_schema).lock, "INPLACE, LOCK=NONE")
        loosen = with_intent(
            "ALTER TABLE `t` MODIFY COLUMN `nn` INT NULL", "drop_not_null", "t", "nn"
        )
        self.assertEqual(table(loosen, ctx=no_schema).work, Work.REWRITE)
        comment = with_intent(
            "ALTER TABLE `t` MODIFY COLUMN `c` INT NULL COMMENT 'n'",
            "set_column_comment",
            "t",
            "c",
        )
        self.assertEqual(table(comment, ctx=no_schema).lock, "INSTANT")
        self.assertIs(impact(comment, no_schema).confidence, Confidence.KNOWN)

    def test_a_generated_column_is_computed_again(self):
        sql = "ALTER TABLE t MODIFY COLUMN c int GENERATED ALWAYS AS (id * 2) STORED"
        self.assertEqual(table(sql).lock, "COPY, LOCK=SHARED")


class IndexTestCase(unittest.TestCase):
    def test_an_index_builds_while_writes_go_on(self):
        for sql in (
            "CREATE INDEX ix2 ON t (name)",
            "ALTER TABLE t ADD INDEX ix2 (name)",
        ):
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual(found.lock, "INPLACE, LOCK=NONE")
                self.assertEqual(found.blocks, Blocks.DDL)
                self.assertEqual(found.work, Work.INDEX_BUILD)
                self.assertEqual(table(sql, ctx=MARIADB).lock, "NOCOPY, LOCK=NONE")

    def test_a_first_fulltext_index_rebuilds_the_table(self):
        first = table("CREATE FULLTEXT INDEX f ON t (name)")
        self.assertEqual(
            (first.lock, first.work), ("INPLACE, LOCK=SHARED", Work.REWRITE)
        )
        more = table("ALTER TABLE ft ADD FULLTEXT INDEX f2 (body)", "ft")
        self.assertEqual(
            (more.lock, more.work), ("INPLACE, LOCK=SHARED", Work.INDEX_BUILD)
        )
        # MariaDB builds a second one without rebuilding the table.
        more = table("ALTER TABLE ft ADD FULLTEXT INDEX f2 (body)", "ft", MARIADB)
        self.assertEqual(more.lock, "NOCOPY, LOCK=SHARED")
        unread = impact(
            "CREATE FULLTEXT INDEX f ON t (name)", MYSQL._replace(tables={})
        )
        self.assertIs(unread.confidence, Confidence.LIKELY)

    def test_a_dropped_index_changes_the_catalog(self):
        found = table("DROP INDEX ix ON t")
        self.assertEqual((found.lock, found.work), ("INPLACE, LOCK=NONE", Work.CATALOG))
        self.assertEqual(
            table("DROP INDEX ix ON t", ctx=MARIADB).lock, "NOCOPY, LOCK=NONE"
        )

    def test_dropping_the_primary_key_copies(self):
        for sql in ("DROP INDEX `PRIMARY` ON t", "ALTER TABLE t DROP PRIMARY KEY"):
            with self.subTest(sql=sql):
                self.assertEqual(table(sql).lock, "COPY, LOCK=SHARED")
        swap = table("ALTER TABLE t DROP PRIMARY KEY, ADD PRIMARY KEY (id, nn)")
        self.assertEqual((swap.lock, swap.work), ("INPLACE, LOCK=NONE", Work.REWRITE))

    def test_renaming_an_index(self):
        sql = "ALTER TABLE t RENAME INDEX ix TO ix2"
        self.assertEqual(table(sql).lock, "INPLACE, LOCK=NONE")
        self.assertEqual(table(sql, ctx=MARIADB).lock, "INSTANT")


class ConstraintTestCase(unittest.TestCase):
    FK = "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (r_id) REFERENCES r (id)"

    def test_a_foreign_key_copies_the_table_and_locks_its_parent(self):
        statement = impact(self.FK)
        child, parent = statement.tables
        self.assertEqual(child.lock, "COPY, LOCK=SHARED")
        self.assertEqual((parent.table, parent.lock), ("r", "MDL EXCLUSIVE"))
        self.assertEqual(parent.rule, "mysql.foreign_key_parent")

    def test_mariadb_leaves_the_parent_alone(self):
        statement = impact(self.FK, MARIADB)
        self.assertEqual([t.table for t in statement.tables], ["t"])
        self.assertEqual(statement.tables[0].lock, "COPY, LOCK=NONE")

    def test_with_foreign_key_checks_off_the_rows_go_unchecked(self):
        statements = analyze(
            ["SET foreign_key_checks = 0", self.FK], MY, MYSQL
        ).statements
        found = statements[1].tables[0]
        self.assertEqual((found.lock, found.work), ("INPLACE, LOCK=NONE", Work.CATALOG))
        self.assertEqual(found.rule, "mysql.add_foreign_key.unchecked")
        self.assertTrue(any("not checked" in f.message for f in statements[1].findings))
        on_mariadb = analyze(["SET foreign_key_checks = 0", self.FK], MY, MARIADB)
        self.assertEqual(on_mariadb.statements[1].tables[0].lock, "INSTANT")

    def test_a_global_setting_leaves_the_session_checking(self):
        statements = analyze(
            ["SET GLOBAL foreign_key_checks = 0", self.FK], MY, MYSQL
        ).statements
        self.assertEqual(statements[1].tables[0].lock, "COPY, LOCK=SHARED")

    def test_a_dropped_foreign_key_locks_the_parent_the_schema_names(self):
        statement = impact("ALTER TABLE t DROP FOREIGN KEY t_r_fk")
        self.assertEqual(
            [(t.table, t.lock) for t in statement.tables],
            [("t", "INPLACE, LOCK=NONE"), ("r", "MDL EXCLUSIVE")],
        )
        self.assertEqual(
            table("ALTER TABLE t DROP FOREIGN KEY t_r_fk", ctx=MARIADB).lock, "INSTANT"
        )

    def test_drop_constraint_reads_the_kind_from_the_schema(self):
        self.assertEqual(
            table("ALTER TABLE t DROP CONSTRAINT ck").rule, "mysql.drop_check"
        )
        self.assertEqual(
            table("ALTER TABLE t DROP CONSTRAINT t_r_fk").rule, "mysql.drop_foreign_key"
        )
        self.assertEqual(
            table("ALTER TABLE t DROP CONSTRAINT ix").rule, "mysql.drop_index"
        )
        unknown = impact("ALTER TABLE t DROP CONSTRAINT nope")
        self.assertIs(unknown.confidence, Confidence.LIKELY)

    def test_a_check_copies_the_table(self):
        found = table("ALTER TABLE t ADD CONSTRAINT ck2 CHECK (c > -1)")
        self.assertEqual((found.lock, found.work), ("COPY, LOCK=SHARED", Work.REWRITE))

    def test_a_unique_constraint_is_an_index(self):
        found = table("ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name)")
        self.assertEqual(
            (found.lock, found.work), ("INPLACE, LOCK=NONE", Work.INDEX_BUILD)
        )


class AlgorithmTestCase(unittest.TestCase):
    def test_an_assertion_is_offered(self):
        statement = impact("ALTER TABLE t ADD COLUMN d int")
        (offer,) = [
            f for f in statement.findings if f.rule == "mysql.add_column.instant"
        ]
        self.assertEqual(
            offer.remedy, ("ALTER TABLE t ADD COLUMN d int, ALGORITHM=INSTANT",)
        )
        index = impact("CREATE INDEX ix2 ON t (name)")
        self.assertIn(
            "CREATE INDEX ix2 ON t (name) ALGORITHM=INPLACE LOCK=NONE",
            index.findings[0].remedy,
        )
        dropped = impact("DROP INDEX ix ON t", MARIADB)
        self.assertIn(
            "ALTER TABLE t DROP INDEX ix, ALGORITHM=NOCOPY, LOCK=NONE",
            dropped.findings[0].remedy,
        )

    def test_no_assertion_for_a_copy_or_a_statement_that_asserts(self):
        self.assertFalse(
            any(
                f.remedy
                for f in impact("ALTER TABLE t MODIFY COLUMN c bigint").findings
                if f.rule != "mysql.lock_timeout"
            )
        )
        asserted = impact("ALTER TABLE t ADD COLUMN d int, ALGORITHM=INSTANT")
        self.assertEqual(rules(asserted), ["mysql.lock_timeout"])

    def test_the_assertion_form(self):
        online = mysql.Online("INPLACE", "NONE")
        self.assertEqual(
            mysql.assertion("ALTER TABLE t FORCE;", "alter_table", online),
            "ALTER TABLE t FORCE, ALGORITHM=INPLACE, LOCK=NONE",
        )
        self.assertEqual(
            mysql.assertion("DROP INDEX `ix` ON `app`.`t`", "drop_index", online, True),
            "ALTER TABLE `app`.`t` DROP INDEX `ix`, ALGORITHM=INPLACE, LOCK=NONE",
        )
        self.assertIsNone(mysql.assertion("DROP INDEX ix", "drop_index", online, True))

    def test_a_refused_algorithm(self):
        cases = [
            (
                "ALTER TABLE t MODIFY COLUMN c bigint, ALGORITHM=INPLACE",
                MYSQL,
                "ALGORITHM=INPLACE cannot run it",
            ),
            (
                "ALTER TABLE t MODIFY COLUMN c bigint, LOCK=NONE",
                MYSQL,
                "LOCK=NONE cannot run it",
            ),
            (
                "ALTER TABLE t ADD COLUMN d int, ALGORITHM=INSTANT, LOCK=NONE",
                MYSQL,
                "no LOCK clause",
            ),
            (
                "ALTER TABLE t ADD COLUMN d int, ALGORITHM=NOCOPY",
                MYSQL,
                "no ALGORITHM=NOCOPY",
            ),
            (
                "CREATE INDEX ix2 ON t (name) ALGORITHM=INSTANT",
                MARIADB,
                "ALGORITHM=INSTANT cannot run it",
            ),
        ]
        for sql, ctx, reason in cases:
            with self.subTest(sql=sql):
                statement = impact(sql, ctx)
                (refused,) = [
                    f for f in statement.findings if f.rule.endswith(".refused")
                ]
                self.assertIs(refused.severity, Severity.WARN)
                self.assertIn(reason, refused.message)
                self.assertEqual(statement.tables[0].blocks, Blocks.NOTHING)

    def test_mariadb_accepts_a_lock_beside_instant(self):
        statement = impact(
            "ALTER TABLE t ADD COLUMN d int, ALGORITHM=INSTANT, LOCK=NONE", MARIADB
        )
        self.assertEqual(statement.tables[0].lock, "INSTANT")

    def test_a_heavier_algorithm_runs_as_asked(self):
        copy = table("ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY")
        self.assertEqual((copy.lock, copy.work), ("COPY, LOCK=NONE", Work.REWRITE))
        shared = table("ALTER TABLE t ADD INDEX ix2 (name), LOCK=SHARED")
        self.assertEqual(shared.lock, "INPLACE, LOCK=SHARED")
        inplace = impact("ALTER TABLE t ADD COLUMN d int, ALGORITHM=INPLACE")
        self.assertEqual(inplace.tables[0].lock, "INPLACE, LOCK=NONE")
        self.assertIs(inplace.confidence, Confidence.LIKELY)
        # MySQL runs a bare LOCK clause in place.
        locked = impact("ALTER TABLE t ADD COLUMN d int, LOCK=SHARED")
        self.assertEqual(locked.tables[0].lock, "INPLACE, LOCK=SHARED")


if __name__ == "__main__":
    unittest.main()
