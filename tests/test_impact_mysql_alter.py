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
from sustained.impact.rules.mysql import locks as mysql_locks
from sustained.impact.rules.mysql import online as mysql_online
from sustained.impact.rules.mysql.column_types import (
    length_bytes,
    mariadb_widens_instantly,
)
from sustained.impact.rules.mysql.locks import INPLACE_NONE
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedIndex,
    Snapshot,
)
from tests.test_impact_mysql import (
    _STATS,
    FIXTURE_SCHEMA,
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
        online = mysql_locks.Online("INPLACE", "NONE")
        self.assertEqual(
            mysql_online.assertion("ALTER TABLE t FORCE;", "alter_table", online),
            "ALTER TABLE t FORCE, ALGORITHM=INPLACE, LOCK=NONE",
        )
        self.assertEqual(
            mysql_online.assertion(
                "DROP INDEX `ix` ON `app`.`t`", "drop_index", online, True
            ),
            "ALTER TABLE `app`.`t` DROP INDEX `ix`, ALGORITHM=INPLACE, LOCK=NONE",
        )
        self.assertIsNone(
            mysql_online.assertion("DROP INDEX ix", "drop_index", online, True)
        )

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
        # COPY takes LOCK=SHARED on MySQL, and on MariaDB before 11.2.
        copy = table("ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY")
        self.assertEqual((copy.lock, copy.work), ("COPY, LOCK=SHARED", Work.REWRITE))
        copy = table("ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY", ctx=MARIADB)
        self.assertEqual((copy.lock, copy.work), ("COPY, LOCK=NONE", Work.REWRITE))
        old = context("mariadb", (11, 1, 2))
        copy = table("ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY", ctx=old)
        self.assertEqual(copy.lock, "COPY, LOCK=SHARED")
        shared = table("ALTER TABLE t ADD INDEX ix2 (name), LOCK=SHARED")
        self.assertEqual(shared.lock, "INPLACE, LOCK=SHARED")
        # An instant ADD or DROP COLUMN run in place rebuilds the table
        # on MySQL.
        for sql in (
            "ALTER TABLE t ADD COLUMN d int, ALGORITHM=INPLACE",
            "ALTER TABLE t DROP COLUMN name, ALGORITHM=INPLACE, LOCK=NONE",
        ):
            inplace = impact(sql)
            (found,) = inplace.tables
            self.assertEqual((found.lock, found.work), (INPLACE_NONE, Work.REWRITE))
            self.assertIs(inplace.confidence, Confidence.KNOWN)
            self.assertIn(
                found.rule, ("mysql.add_column.rebuild", "mysql.drop_column.rebuild")
            )
        # Another instant change keeps its work, less sure.
        renamed = impact("ALTER TABLE t RENAME COLUMN name TO label, ALGORITHM=INPLACE")
        self.assertEqual(renamed.tables[0].lock, INPLACE_NONE)
        self.assertIs(renamed.tables[0].work, Work.CATALOG)
        self.assertIs(renamed.confidence, Confidence.LIKELY)
        # MariaDB runs a change a faster algorithm can run with it.
        mariadb = impact("ALTER TABLE t ADD COLUMN d int, ALGORITHM=INPLACE", MARIADB)
        self.assertEqual(mariadb.tables[0].lock, "INSTANT")
        self.assertIs(mariadb.tables[0].work, Work.CATALOG)
        # MySQL runs a bare LOCK clause in place.
        locked = impact("ALTER TABLE t ADD COLUMN d int, LOCK=SHARED")
        self.assertEqual(locked.tables[0].lock, "INPLACE, LOCK=SHARED")


def rules_text(statement):
    """The messages of a statement's findings, joined."""
    return " ".join(f.message for f in statement.findings)


def ci_stats(ctx, **stats):
    """The context with the stats of table ci changed."""
    tables = dict(ctx.tables, ci=ctx.tables["ci"]._replace(**stats))
    return ctx._replace(tables=tables)


class CollationTestCase(unittest.TestCase):
    CASES = [
        # A new collation of the same character set.
        (
            "MODIFY COLUMN name varchar(100) COLLATE utf8mb4_bin",
            ("INPLACE, LOCK=NONE", Work.CATALOG),
            ("INSTANT", Work.CATALOG),
        ),
        # The same on a column in an index.
        (
            "MODIFY COLUMN code varchar(50) COLLATE utf8mb4_bin",
            ("COPY, LOCK=SHARED", Work.REWRITE),
            ("NOCOPY, LOCK=NONE", Work.INDEX_BUILD),
        ),
        # Another character set.
        (
            "MODIFY COLUMN name varchar(100) CHARACTER SET latin1",
            ("COPY, LOCK=SHARED", Work.REWRITE),
            ("COPY, LOCK=NONE", Work.REWRITE),
        ),
        # A latin1 column takes the table's utf8mb4 default.
        (
            "MODIFY COLUMN l varchar(100)",
            ("COPY, LOCK=SHARED", Work.REWRITE),
            ("COPY, LOCK=NONE", Work.REWRITE),
        ),
        # The column's own character set, named again.
        (
            "MODIFY COLUMN l varchar(100) CHARACTER SET latin1",
            ("INSTANT", Work.CATALOG),
            ("INSTANT", Work.CATALOG),
        ),
        # utf8mb3 to utf8mb4 below 256 bytes, and past 255 bytes.
        (
            "MODIFY COLUMN s3 varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin",
            ("INPLACE, LOCK=NONE", Work.CATALOG),
            ("INSTANT", Work.CATALOG),
        ),
        (
            "MODIFY COLUMN m3 varchar(80) CHARACTER SET utf8mb4",
            ("COPY, LOCK=SHARED", Work.REWRITE),
            ("COPY, LOCK=NONE", Work.REWRITE),
        ),
        # A new collation beside a longer VARCHAR.
        (
            "MODIFY COLUMN name varchar(200) COLLATE utf8mb4_bin",
            ("INPLACE, LOCK=NONE", Work.CATALOG),
            ("INSTANT", Work.CATALOG),
        ),
    ]

    def test_each_change(self):
        for action, on_mysql, on_mariadb in self.CASES:
            sql = f"ALTER TABLE ci {action}"
            with self.subTest(sql=sql):
                found = table(sql, "ci")
                self.assertEqual((found.lock, found.work), on_mysql)
                found = table(sql, "ci", MARIADB)
                self.assertEqual((found.lock, found.work), on_mariadb)

    def test_the_known_changes_are_known(self):
        for sql in (
            "ALTER TABLE ci MODIFY COLUMN name varchar(100) COLLATE utf8mb4_bin",
            "ALTER TABLE ci MODIFY COLUMN code varchar(50) COLLATE utf8mb4_bin",
            "ALTER TABLE ci MODIFY COLUMN l varchar(100)",
        ):
            with self.subTest(sql=sql):
                self.assertIs(impact(sql).confidence, Confidence.KNOWN)
                self.assertEqual(impact(sql).tables[0].rule.split(".")[0], "mysql")

    def test_an_unread_table_collation_counts_as_a_copy(self):
        unread = ci_stats(MYSQL, collation=None)
        statement = impact("ALTER TABLE ci MODIFY COLUMN l varchar(100)", unread)
        self.assertEqual(statement.tables[0].lock, "COPY, LOCK=SHARED")
        self.assertEqual(statement.tables[0].rule, "mysql.modify_column.copy")
        self.assertIs(statement.confidence, Confidence.LIKELY)
        self.assertIn("did not read", statement.findings[0].message)
        # The same collation as the table's default changes nothing.
        same = impact("ALTER TABLE ci MODIFY COLUMN name varchar(100)")
        self.assertEqual(same.tables[0].lock, "INSTANT")

    def test_a_character_set_without_a_collation(self):
        # utf8mb4 takes utf8mb4_0900_ai_ci on MySQL, the column's own.
        sql = "ALTER TABLE ci MODIFY COLUMN name varchar(100) CHARACTER SET utf8mb4"
        statement = impact(sql)
        self.assertEqual(statement.tables[0].lock, "INSTANT")
        self.assertIs(statement.confidence, Confidence.KNOWN)
        sql = "ALTER TABLE ci MODIFY COLUMN s3 varchar(30) CHARACTER SET utf8mb4"
        self.assertIs(impact(sql).confidence, Confidence.KNOWN)
        # MariaDB's default for utf8mb4 differs between versions.
        sql = "ALTER TABLE ci MODIFY COLUMN code varchar(50) CHARACTER SET utf8mb4"
        statement = impact(sql, MARIADB)
        self.assertEqual(statement.tables[0].lock, "NOCOPY, LOCK=NONE")
        self.assertIs(statement.confidence, Confidence.LIKELY)
        # An instant change costs the same either way.
        sql = "ALTER TABLE ci MODIFY COLUMN name varchar(100) CHARACTER SET utf8mb4"
        self.assertIs(impact(sql, MARIADB).confidence, Confidence.KNOWN)

    def test_utf8_reads_as_utf8mb3(self):
        sql = "ALTER TABLE ci MODIFY COLUMN s3 varchar(30) CHARACTER SET utf8"
        self.assertEqual(table(sql, "ci").lock, "INSTANT")
        sql = "ALTER TABLE ci MODIFY COLUMN s3 varchar(30) COLLATE `utf8_bin`"
        self.assertEqual(table(sql, "ci").lock, "INPLACE, LOCK=NONE")

    def test_an_index_read_or_not(self):
        sql = "ALTER TABLE ci MODIFY COLUMN s3 varchar(30) CHARACTER SET utf8mb4"
        # utf8mb3 to utf8mb4 on an indexed column: 11.4 rebuilds the
        # indexes, and 12.3 changes it instantly.
        indexed = FIXTURE_SCHEMA["ci"]._replace(
            indexes={
                **FIXTURE_SCHEMA["ci"].indexes,
                "s3_ix": IntrospectedIndex(("s3",), False, name="s3_ix"),
            }
        )
        schema = Snapshot({**FIXTURE_SCHEMA, "ci": indexed})
        mariadb = impact(sql, MARIADB._replace(schema=schema))
        self.assertEqual(mariadb.tables[0].lock, "NOCOPY, LOCK=NONE")
        self.assertIs(mariadb.confidence, Confidence.LIKELY)
        self.assertEqual(
            table(sql, "ci", MYSQL._replace(schema=schema)).lock, "COPY, LOCK=SHARED"
        )

    def test_a_text_column_widened_to_utf8mb4(self):
        # Only VARCHAR lengths are read; another type is less sure.
        column = IntrospectedColumn("text", True, False, collation="utf8mb3_general_ci")
        ci = FIXTURE_SCHEMA["ci"]
        ci = ci._replace(columns={**ci.columns, "tx": column})
        schema = Snapshot({**FIXTURE_SCHEMA, "ci": ci})
        statement = impact(
            "ALTER TABLE ci MODIFY COLUMN tx text CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_bin",
            MYSQL._replace(schema=schema),
        )
        self.assertEqual(statement.tables[0].lock, "INPLACE, LOCK=NONE")
        self.assertIs(statement.confidence, Confidence.LIKELY)


class MariadbWideningTestCase(unittest.TestCase):
    def test_the_255_byte_boundary(self):
        cases = [
            ((31, 64, "utf8mb4_bin", "DYNAMIC"), (True, Confidence.KNOWN)),
            ((32, 64, "utf8mb4_bin", "DYNAMIC"), (False, Confidence.KNOWN)),
            ((32, 64, "utf8mb4_bin", "COMPACT"), (False, Confidence.KNOWN)),
            ((32, 64, "utf8mb4_bin", "compressed"), (False, Confidence.KNOWN)),
            ((32, 64, "utf8mb4_bin", "REDUNDANT"), (True, Confidence.KNOWN)),
            ((127, 256, "latin1_swedish_ci", "DYNAMIC"), (True, Confidence.KNOWN)),
            ((128, 256, "latin1_swedish_ci", "DYNAMIC"), (False, Confidence.KNOWN)),
            ((128, 255, "latin1_swedish_ci", "DYNAMIC"), (True, Confidence.KNOWN)),
            ((64, 100, "utf8mb3_general_ci", "DYNAMIC"), (False, Confidence.KNOWN)),
            # No row format read.
            ((32, 64, "utf8mb4_bin", None), (False, Confidence.LIKELY)),
            # No collation read: one byte and four to a character.
            ((128, 256, None, "DYNAMIC"), (False, Confidence.LIKELY)),
            ((20, 64, None, "DYNAMIC"), (True, Confidence.KNOWN)),
            ((20, 64, None, None), (True, Confidence.KNOWN)),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(mariadb_widens_instantly(*arguments), expected)

    def test_the_rules_read_the_row_format(self):
        sql = "ALTER TABLE ci MODIFY COLUMN mid varchar(64)"
        found = table(sql, "ci", MARIADB)
        self.assertEqual((found.lock, found.work), ("COPY, LOCK=NONE", Work.REWRITE))
        redundant = table(sql, "ci", ci_stats(MARIADB, row_format="REDUNDANT"))
        self.assertEqual(redundant.lock, "INSTANT")
        unread = impact(sql, ci_stats(MARIADB, row_format=None))
        self.assertEqual(unread.tables[0].lock, "COPY, LOCK=NONE")
        self.assertIs(unread.confidence, Confidence.LIKELY)
        # A VARBINARY counts one byte to a character.
        self.assertEqual(
            mariadb_widens_instantly(200, 300, "binary", "DYNAMIC"),
            (False, Confidence.KNOWN),
        )


class CombinedTestCase(unittest.TestCase):
    def test_an_instant_column_change_beside_an_index_rebuilds(self):
        cases = [
            (
                "ALTER TABLE t ADD COLUMN d int, ADD INDEX ix2 (name)",
                MYSQL,
                "mysql.add_column.rebuild",
            ),
            (
                "ALTER TABLE t ADD COLUMN d int, ADD INDEX ix2 (name)",
                MARIADB,
                "mariadb.add_column.rebuild",
            ),
            (
                "ALTER TABLE t DROP COLUMN name, ADD INDEX ix2 (small)",
                MYSQL,
                "mysql.drop_column.rebuild",
            ),
            (
                "ALTER TABLE t MODIFY COLUMN c int FIRST, ADD INDEX ix2 (name)",
                MARIADB,
                "mariadb.modify_column.rebuild",
            ),
            (
                "ALTER TABLE t ADD COLUMN d int, MODIFY COLUMN name varchar(200)",
                MYSQL,
                "mysql.add_column.rebuild",
            ),
        ]
        for sql, ctx, rule in cases:
            with self.subTest(sql=sql, profile=ctx.profile):
                statement = impact(sql, ctx)
                (found,) = statement.tables
                self.assertEqual((found.lock, found.work), (INPLACE_NONE, Work.REWRITE))
                self.assertEqual(found.rule, rule)
                self.assertIs(statement.confidence, Confidence.KNOWN)
                self.assertIn("rebuilds the table", statement.findings[0].message)

    def test_instant_changes_together_stay_instant(self):
        for sql, ctx in (
            ("ALTER TABLE t ADD COLUMN d int, RENAME COLUMN c TO c2", MYSQL),
            ("ALTER TABLE t ADD COLUMN d int, RENAME COLUMN c TO c2", MARIADB),
            (
                "ALTER TABLE t ADD COLUMN d int, MODIFY COLUMN name varchar(200)",
                MARIADB,
            ),
        ):
            with self.subTest(sql=sql, profile=ctx.profile):
                found = table(sql, ctx=ctx)
                self.assertEqual((found.lock, found.work), ("INSTANT", Work.CATALOG))

    def test_a_copy_keeps_its_own_rule(self):
        found = table("ALTER TABLE t ADD COLUMN d int, MODIFY COLUMN c bigint")
        self.assertEqual((found.lock, found.work), ("COPY, LOCK=SHARED", Work.REWRITE))
        self.assertEqual(found.rule, "mysql.modify_column.copy")
        found = table(
            "ALTER TABLE t ADD COLUMN d int, MODIFY COLUMN c bigint", ctx=MARIADB
        )
        self.assertEqual(found.lock, "COPY, LOCK=NONE")
        self.assertEqual(found.rule, "mariadb.modify_column.copy")


class SpatialAndFulltextTestCase(unittest.TestCase):
    def test_a_spatial_index_is_an_index_build(self):
        for sql in (
            "ALTER TABLE g ADD SPATIAL INDEX sp (p)",
            "CREATE SPATIAL INDEX sp ON g (p)",
        ):
            with self.subTest(sql=sql):
                found = table(sql, "g")
                self.assertEqual(
                    (found.lock, found.work, found.rule),
                    ("INPLACE, LOCK=SHARED", Work.INDEX_BUILD, "mysql.add_spatial"),
                )
                self.assertEqual(found.blocks, Blocks.WRITES)
                found = table(sql, "g", MARIADB)
                self.assertEqual(
                    (found.lock, found.rule),
                    ("NOCOPY, LOCK=SHARED", "mariadb.add_spatial"),
                )

    def test_a_spatial_index_takes_no_lock_none(self):
        statement = impact("ALTER TABLE g ADD SPATIAL INDEX sp (p), LOCK=NONE")
        self.assertEqual(statement.tables[0].rule, "mysql.refused")

    def test_a_column_added_to_a_fulltext_table_copies_on_mysql(self):
        # MySQL 8.4 and 26.7 accept only COPY, LOCK=SHARED for it.
        found = table("ALTER TABLE ft ADD COLUMN d int", "ft")
        self.assertEqual(
            (found.lock, found.work, found.rule),
            ("COPY, LOCK=SHARED", Work.REWRITE, "mysql.add_column.copy"),
        )

    def test_a_column_dropped_from_a_fulltext_table(self):
        found = table("ALTER TABLE ft DROP COLUMN x", "ft")
        self.assertEqual(
            (found.lock, found.work, found.rule),
            ("COPY, LOCK=SHARED", Work.REWRITE, "mysql.drop_column.rebuild"),
        )
        found = table("ALTER TABLE ft DROP COLUMN x", "ft", MARIADB)
        self.assertEqual(
            (found.lock, found.work, found.rule),
            ("INPLACE, LOCK=SHARED", Work.REWRITE, "mariadb.drop_column.rebuild"),
        )


class HashedUniqueKeyTestCase(unittest.TestCase):
    def test_mariadb_copies_for_a_unique_key_using_hash(self):
        for sql in (
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name) USING HASH",
            "ALTER TABLE t ADD UNIQUE INDEX uq USING HASH (name)",
            "CREATE UNIQUE INDEX uq ON t (name) USING HASH",
            "CREATE UNIQUE INDEX uq USING HASH ON t (name)",
        ):
            with self.subTest(sql=sql):
                found = table(sql, ctx=MARIADB)
                self.assertEqual(
                    (found.lock, found.work, found.rule),
                    ("COPY, LOCK=NONE", Work.REWRITE, "mariadb.add_index"),
                )
                self.assertEqual(table(sql).lock, INPLACE_NONE)

    def test_another_index_using_hash_is_built_as_ever(self):
        for sql in (
            "ALTER TABLE t ADD INDEX ix2 (name) USING HASH",
            "CREATE INDEX ix2 ON t (name) USING HASH",
            "ALTER TABLE t ADD UNIQUE KEY uq (name) USING BTREE",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(table(sql, ctx=MARIADB).lock, "NOCOPY, LOCK=NONE")


class ServerSyntaxTestCase(unittest.TestCase):
    def test_index_visibility_in_each_servers_spelling(self):
        mysql_form = "ALTER TABLE ci ALTER INDEX code_ix INVISIBLE"
        mariadb_form = "ALTER TABLE ci ALTER INDEX code_ix NOT IGNORED"
        found = table(mysql_form, "ci")
        self.assertEqual(
            (found.lock, found.work, found.rule),
            (INPLACE_NONE, Work.CATALOG, "mysql.index_visibility"),
        )
        found = table(mariadb_form, "ci", MARIADB)
        self.assertEqual(
            (found.lock, found.work, found.rule),
            ("INSTANT", Work.CATALOG, "mariadb.index_visibility"),
        )
        for sql, ctx, server in (
            (mariadb_form, MYSQL, "MySQL"),
            (mysql_form, MARIADB, "MariaDB"),
        ):
            with self.subTest(sql=sql, profile=ctx.profile):
                statement = impact(sql, ctx)
                self.assertIs(statement.confidence, Confidence.UNKNOWN)
                self.assertIn(f"which {server} does not accept", rules_text(statement))

    def test_mysql_reads_the_mariadb_forms_as_unknown(self):
        for sql in (
            "ALTER ONLINE TABLE t ADD COLUMN d int",
            "ALTER IGNORE TABLE t ADD COLUMN d int",
            "ALTER TABLE t WAIT 5 ADD COLUMN d int",
            "CREATE INDEX ix2 ON t (name) WAIT 3",
            "DROP INDEX r_ix ON t NOWAIT",
        ):
            with self.subTest(sql=sql):
                statement = impact(sql)
                self.assertIs(statement.confidence, Confidence.UNKNOWN)
                self.assertIn("which MySQL does not accept", rules_text(statement))

    def test_mariadb_online_asks_for_lock_none(self):
        added = impact("ALTER ONLINE TABLE t ADD COLUMN d int", MARIADB)
        self.assertEqual(added.tables[0].lock, "INSTANT")
        for sql, name in (
            ("ALTER ONLINE TABLE ft ADD FULLTEXT INDEX f2 (body)", "ft"),
            ("ALTER ONLINE TABLE g ADD SPATIAL INDEX sp (p)", "g"),
        ):
            with self.subTest(sql=sql):
                statement = impact(sql, MARIADB)
                self.assertEqual(table(sql, name, MARIADB).rule, "mariadb.refused")
                self.assertIn("ALTER ONLINE TABLE", statement.findings[0].message)
        # Before 11.2 a copy takes LOCK=SHARED, which ONLINE refuses.
        old = context("mariadb", (11, 1, 2))
        copied = table("ALTER ONLINE TABLE t MODIFY COLUMN c bigint", ctx=old)
        self.assertEqual(copied.rule, "mariadb.refused")
        copied = table("ALTER ONLINE TABLE t MODIFY COLUMN c bigint", ctx=MARIADB)
        self.assertEqual(copied.lock, "COPY, LOCK=NONE")

    def test_mariadb_ignore_copies_a_new_key_under_a_shared_lock(self):
        unique = impact("ALTER IGNORE TABLE t ADD CONSTRAINT uq UNIQUE (name)", MARIADB)
        (found,) = unique.tables
        self.assertEqual((found.lock, found.work), ("COPY, LOCK=SHARED", Work.REWRITE))
        self.assertIs(unique.confidence, Confidence.KNOWN)
        column = impact("ALTER IGNORE TABLE t ADD COLUMN d int UNIQUE", MARIADB)
        self.assertEqual(column.tables[0].lock, "COPY, LOCK=SHARED")
        self.assertIs(column.confidence, Confidence.LIKELY)
        copied = table("ALTER IGNORE TABLE t MODIFY COLUMN c bigint", ctx=MARIADB)
        self.assertEqual(copied.lock, "COPY, LOCK=SHARED")
        for sql in (
            "ALTER IGNORE TABLE t ADD COLUMN d int",
            "ALTER IGNORE TABLE t ADD INDEX ix2 (name)",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(
                    table(sql, ctx=MARIADB).lock,
                    table(sql.replace(" IGNORE", ""), ctx=MARIADB).lock,
                )

    def test_mariadb_wait_bounds_the_lock_wait(self):
        for sql in (
            "ALTER TABLE t WAIT 5 ADD COLUMN d int",
            "ALTER TABLE t NOWAIT ADD COLUMN d int",
            "CREATE INDEX ix2 ON t (name) WAIT 3",
            "DROP INDEX r_ix ON t NOWAIT",
        ):
            with self.subTest(sql=sql):
                self.assertNotIn("mariadb.lock_timeout", rules(impact(sql, MARIADB)))
        for sql in (
            "ALTER TABLE t WAIT 100000 ADD COLUMN d int",
            "ALTER TABLE t ADD COLUMN d int",
        ):
            with self.subTest(sql=sql):
                self.assertIn("mariadb.lock_timeout", rules(impact(sql, MARIADB)))


if __name__ == "__main__":
    unittest.main()
