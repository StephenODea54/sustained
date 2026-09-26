"""Tests for the MySQL and MariaDB impact rules."""

import json
import pathlib
import unittest

from sustained.analysis import MigrationStatement, with_intent
from sustained.dialects import Dialects
from sustained.guards import lock_timeout_required
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    Severity,
    TableStats,
    Work,
    analyze,
)
from sustained.impact.context import FLOORS, assumed
from sustained.impact.recognizer import recognize
from sustained.impact.report import render
from sustained.impact.rules import mysql, profile_for, profiles_for
from sustained.impact.rules.mysql.column_types import length_bytes
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
)

MY = Dialects.MYSQL
ROOT = pathlib.Path(__file__).resolve().parent.parent

# The fixture schema's tables, as the schema read reports them.
FIXTURE_SCHEMA = Snapshot(
    {
        "r": IntrospectedTable(
            {"id": IntrospectedColumn("int", False, True)},
            primary_key=("id",),
            name="r",
        ),
        "t": IntrospectedTable(
            {
                "id": IntrospectedColumn("int", False, True),
                "c": IntrospectedColumn("int", True, False),
                "name": IntrospectedColumn(
                    "varchar(100)", True, False, collation="utf8mb4_0900_ai_ci"
                ),
                "small": IntrospectedColumn(
                    "varchar(20)", True, False, collation="utf8mb4_0900_ai_ci"
                ),
                "e": IntrospectedColumn("enum('a','b')", True, False),
                "r_id": IntrospectedColumn("int", True, False),
                "nn": IntrospectedColumn("int", False, False, default="0"),
            },
            primary_key=("id",),
            foreign_keys={
                "t_r_fk": IntrospectedForeignKey(("r_id",), "r", ("id",), name="t_r_fk")
            },
            indexes={
                "ix": IntrospectedIndex(("c",), True, name="ix"),
                "r_ix": IntrospectedIndex(("r_id",), False, name="r_ix"),
            },
            checks={"ck": "`c` > 0"},
            check_names={"ck": "ck"},
            name="t",
        ),
        "p": IntrospectedTable(
            {
                "id": IntrospectedColumn("int", False, False),
                "v": IntrospectedColumn("int", True, False),
            },
            name="p",
        ),
    }
)

_STATS = {
    "t": TableStats(20, 49152, "DYNAMIC", 0, False),
    "r": TableStats(3, 16384, "DYNAMIC", 0, False),
    "p": TableStats(2, 16384, "DYNAMIC", 0, False),
    "ft": TableStats(1, 32768, "DYNAMIC", 0, True),
    "cz": TableStats(1, 8192, "COMPRESSED", 0, False),
}
_READ = frozenset({"version", "settings", "sizes", "fulltext", "schema"})


def context(profile="mysql", version=(8, 4, 11), **settings):
    """A server with the fixture schema read, and its settings."""
    base = {"foreign_key_checks": "1", "lock_wait_timeout": "31536000"}
    base.update(settings)
    read = _READ | ({"row_versions"} if profile == "mysql" else set())
    return EngineContext(
        profile,
        version,
        settings=base,
        tables=dict(_STATS),
        schema=FIXTURE_SCHEMA,
        read=read,
    )


MYSQL = context()
MARIADB = context("mariadb", (11, 4, 13), lock_wait_timeout="86400")


def impact(sql, ctx=MYSQL):
    """The impact of one statement, alone in a migration of its own."""
    (statement,) = analyze([sql], MY, ctx).statements
    return statement


def table(sql, name="t", ctx=MYSQL):
    return next(t for t in impact(sql, ctx).tables if t.table == name)


def rules(statement):
    return [f.rule for f in statement.findings]


class LabelTestCase(unittest.TestCase):
    def test_what_each_label_blocks(self):
        self.assertEqual(mysql.blocks(None), Blocks.NOTHING)
        self.assertEqual(mysql.blocks(mysql.ROW_LOCKS), Blocks.DDL)
        self.assertEqual(mysql.blocks(mysql.INSTANT), Blocks.READS_AND_WRITES)
        self.assertEqual(mysql.blocks(mysql.MDL_EXCLUSIVE), Blocks.READS_AND_WRITES)
        self.assertEqual(mysql.blocks(mysql.INPLACE_NONE), Blocks.DDL)
        self.assertEqual(mysql.blocks(mysql.NOCOPY_NONE), Blocks.DDL)
        self.assertEqual(mysql.blocks(mysql.COPY_SHARED), Blocks.WRITES)
        self.assertEqual(mysql.blocks(mysql.COPY_EXCLUSIVE), Blocks.READS_AND_WRITES)
        self.assertEqual(mysql.blocks("SOMETHING ELSE"), Blocks.READS_AND_WRITES)

    def test_lock_order(self):
        self.assertEqual(mysql.lock_rank(None), -1)
        ranks = [mysql.lock_rank(lock) for lock in mysql.LOCKS]
        self.assertEqual(ranks, sorted(ranks))
        self.assertGreater(
            mysql.lock_rank("SOMETHING ELSE"), mysql.lock_rank(mysql.MDL_EXCLUSIVE)
        )

    def test_a_label_outside_the_list_ranks_by_its_level(self):
        shared = mysql.lock_rank("NOCOPY, LOCK=SHARED")
        self.assertLess(mysql.lock_rank(mysql.COPY_NONE), shared)
        self.assertLess(shared, mysql.lock_rank(mysql.INPLACE_SHARED))
        self.assertLess(shared, mysql.lock_rank(mysql.MDL_EXCLUSIVE))

    def test_every_lock_but_row_locks_queues(self):
        self.assertFalse(mysql.queues(None))
        self.assertFalse(mysql.queues(mysql.ROW_LOCKS))
        self.assertTrue(mysql.queues(mysql.INPLACE_NONE))
        self.assertTrue(mysql.queues(mysql.INSTANT))

    def test_labels_read_back(self):
        self.assertEqual(mysql.parse_label("INSTANT"), mysql.Online("INSTANT"))
        self.assertEqual(
            mysql.parse_label("COPY, LOCK=SHARED"), mysql.Online("COPY", "SHARED")
        )
        self.assertIsNone(mysql.parse_label("MDL EXCLUSIVE"))
        self.assertIsNone(mysql.parse_label("INPLACE"))
        self.assertIsNone(mysql.parse_label("INPLACE, LOCK=SOME"))

    def test_combined(self):
        instant = mysql.Online("INSTANT")
        inplace = mysql.Online("INPLACE", "NONE")
        copy = mysql.Online("COPY", "SHARED")
        self.assertEqual(instant.combined(instant), instant)
        self.assertEqual(instant.combined(inplace), inplace)
        self.assertEqual(inplace.combined(copy), copy)

    def test_bounded(self):
        self.assertTrue(mysql.bounded("5"))
        self.assertTrue(mysql.bounded("3600"))
        self.assertFalse(mysql.bounded("86400"))
        self.assertFalse(mysql.bounded("31536000"))
        self.assertFalse(mysql.bounded("0"))
        self.assertFalse(mysql.bounded("DEFAULT"))


class RuleCatalogTestCase(unittest.TestCase):
    # A rule that needs a session setting set first, which a fixture run
    # alone cannot have.
    NEEDS_A_SETTING = {"add_foreign_key.unchecked"}

    def test_every_rule_has_a_source_and_a_fixture_that_reaches_it(self):
        for name, ctx in (("mysql", MYSQL), ("mariadb", MARIADB)):
            profile = profile_for(MY, name)
            reached = set()
            for rule in profile.rules:
                self.assertTrue(rule.source.startswith("https://"), rule.id)
                self.assertTrue(rule.id.startswith(f"{name}."), rule.id)
                suffix = rule.id.split(".", 1)[1]
                if suffix in self.NEEDS_A_SETTING:
                    reached.add(rule.id)
                    continue
                self.assertTrue(rule.fixtures, rule.id)
                for fixture in rule.fixtures:
                    statement = impact(fixture, ctx)
                    found = {t.rule for t in statement.tables} | set(rules(statement))
                    if rule.id in found:
                        reached.add(rule.id)
                    self.assertNotEqual(
                        statement.confidence, Confidence.UNKNOWN, fixture
                    )
            self.assertEqual(reached, {rule.id for rule in profile.rules})

    def test_the_floors_match_the_support_table(self):
        support = json.loads((ROOT / "support.json").read_text())
        for name in ("mysql", "mariadb"):
            row = next(r for r in support["databases"] if r["name"] == name)
            floor = tuple(int(p) for p in row["floor"].split("."))
            self.assertEqual(FLOORS[name], floor)
            self.assertEqual(assumed(name).version, floor)

    def test_the_dialect_has_both_profiles_mysql_first(self):
        self.assertEqual([p.name for p in profiles_for(MY)], ["mysql", "mariadb"])
        self.assertEqual(profile_for(MY).name, "mysql")
        self.assertEqual(profile_for(MY, "mariadb").name, "mariadb")
        self.assertEqual(profile_for(MY, "postgres").name, "mysql")


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
        self.assertEqual(more.work, Work.INDEX_BUILD)
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


class TableTestCase(unittest.TestCase):
    def test_drop_and_truncate_take_the_metadata_lock(self):
        for sql in ("DROP TABLE p", "TRUNCATE TABLE p"):
            with self.subTest(sql=sql):
                found = table(sql, "p")
                self.assertEqual(
                    (found.lock, found.blocks),
                    ("MDL EXCLUSIVE", Blocks.READS_AND_WRITES),
                )

    def test_dropping_a_child_table_locks_its_parent_on_mysql(self):
        self.assertEqual([t.table for t in impact("DROP TABLE t").tables], ["t", "r"])
        self.assertEqual(
            [t.table for t in impact("DROP TABLE t", MARIADB).tables], ["t"]
        )

    def test_creating_a_child_table_locks_its_parent_on_mysql(self):
        sql = "CREATE TABLE n (id int, r_id int, FOREIGN KEY (r_id) REFERENCES r (id))"
        self.assertEqual([t.table for t in impact(sql).tables], ["r"])
        self.assertEqual(impact(sql, MARIADB).tables, ())

    def test_rename_table(self):
        statement = impact("RENAME TABLE t TO u, p TO q")
        self.assertEqual([t.lock for t in statement.tables], ["MDL EXCLUSIVE"] * 2)
        self.assertEqual(
            len([f for f in statement.findings if f.rule == "mysql.rename"]), 2
        )
        self.assertEqual(table("ALTER TABLE t RENAME TO u").lock, "INSTANT")

    def test_table_rebuilds_and_copies(self):
        cases = [
            (
                "ALTER TABLE t ENGINE=InnoDB",
                "INPLACE, LOCK=NONE",
                "mysql.table_rebuild",
            ),
            ("ALTER TABLE t ENGINE=MyISAM", "COPY, LOCK=SHARED", "mysql.table_copy"),
            ("ALTER TABLE t FORCE", "INPLACE, LOCK=NONE", "mysql.table_rebuild"),
            (
                "ALTER TABLE t ROW_FORMAT=COMPACT",
                "INPLACE, LOCK=NONE",
                "mysql.table_rebuild",
            ),
            (
                "ALTER TABLE t CONVERT TO CHARACTER SET latin1",
                "COPY, LOCK=SHARED",
                "mysql.table_copy",
            ),
            (
                "ALTER TABLE t COMMENT = 'note'",
                "INPLACE, LOCK=NONE",
                "mysql.table_option",
            ),
            ("OPTIMIZE TABLE t", "INPLACE, LOCK=NONE", "mysql.table_rebuild"),
        ]
        for sql, lock, rule in cases:
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual((found.lock, found.rule), (lock, rule))
        self.assertEqual(
            table("ALTER TABLE t COMMENT = 'note'", ctx=MARIADB).lock, "INSTANT"
        )
        self.assertEqual(table("OPTIMIZE TABLE ft", "ft").lock, "COPY, LOCK=SHARED")

    def test_a_trigger_takes_the_metadata_lock(self):
        sql = "CREATE TRIGGER tr BEFORE UPDATE ON t FOR EACH ROW SET NEW.c = NEW.c"
        self.assertEqual(table(sql).lock, "MDL EXCLUSIVE")
        dropped = impact("DROP TRIGGER tr")
        self.assertEqual((dropped.tables, dropped.confidence), ((), Confidence.LIKELY))

    def test_writes_hold_row_locks_and_draw_no_timeout_finding(self):
        statement = impact("UPDATE t SET name = 'x' WHERE id < 3")
        found = statement.tables[0]
        self.assertEqual(
            (found.lock, found.blocks, found.work), ("IX", Blocks.WRITES, Work.ROWS)
        )
        self.assertNotIn("mysql.lock_timeout", rules(statement))
        self.assertEqual(table("INSERT INTO t (id) VALUES (1)").blocks, Blocks.DDL)

    def test_an_unknown_action_or_statement(self):
        statement = impact("ALTER TABLE t VALIDATE CONSTRAINT ck")
        self.assertIs(statement.confidence, Confidence.UNKNOWN)
        self.assertIn(
            "no MySQL rule reads the ALTER TABLE action", statement.findings[0].message
        )
        statement = impact("VACUUM t", MARIADB)
        self.assertIn(
            "no MariaDB rule reads a vacuum statement", statement.findings[0].message
        )

    def test_a_table_the_run_created_draws_nothing(self):
        report = analyze(
            ["CREATE TABLE n (id int)", "ALTER TABLE n ADD COLUMN d int"], MY, MYSQL
        )
        second = report.statements[1]
        self.assertEqual(second.tables[0].blocks, Blocks.NOTHING)
        self.assertEqual(second.findings, ())


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


class TimeoutTestCase(unittest.TestCase):
    ALTER = "ALTER TABLE t ADD COLUMN d int"

    def timeout_findings(self, statements, ctx=MYSQL):
        report = analyze(statements, MY, ctx)
        return [
            s.statement
            for s in report.statements
            if "mysql.lock_timeout" in rules(s) or "mariadb.lock_timeout" in rules(s)
        ]

    def test_every_alter_draws_the_finding(self):
        statement = impact("ALTER TABLE t ADD INDEX ix2 (name)")
        (found,) = [f for f in statement.findings if f.rule == "mysql.lock_timeout"]
        self.assertEqual(found.remedy, ("SET SESSION lock_wait_timeout = 5",))
        self.assertIn("lock_wait_timeout", found.message)

    def test_a_session_timeout_covers_the_rest_of_the_run(self):
        for setting in (
            "SET SESSION lock_wait_timeout = 5",
            "SET lock_wait_timeout = 5",
            "SET LOCAL lock_wait_timeout = 5",
            "SET @@SESSION.lock_wait_timeout = 5",
        ):
            with self.subTest(setting=setting):
                first = MigrationStatement(setting, "001")
                later = MigrationStatement(self.ALTER, "002")
                self.assertEqual(self.timeout_findings([first, later]), [])

    def test_a_global_timeout_covers_nothing(self):
        found = self.timeout_findings(["SET GLOBAL lock_wait_timeout = 5", self.ALTER])
        self.assertEqual(found, [self.ALTER])

    def test_a_default_timeout_on_the_connection_covers_nothing(self):
        self.assertEqual(self.timeout_findings([self.ALTER]), [self.ALTER])
        self.assertEqual(self.timeout_findings([self.ALTER], MARIADB), [self.ALTER])
        short = context(lock_wait_timeout="10")
        self.assertEqual(self.timeout_findings([self.ALTER], short), [])

    def test_the_guard_blocks_the_mysql_form(self):
        guard = lock_timeout_required()
        (verdict,) = guard([self.ALTER], MY)
        self.assertEqual(verdict.rule, "lock_timeout_required")
        self.assertEqual(
            guard(["SET SESSION lock_wait_timeout = 5", self.ALTER], MY), []
        )


class ReportTestCase(unittest.TestCase):
    def test_statements_are_windows_of_their_own(self):
        report = analyze(
            [
                MigrationStatement("ALTER TABLE t ADD COLUMN d int", "001"),
                MigrationStatement("UPDATE t SET d = 1", "001"),
            ],
            MY,
            MYSQL,
        )
        (migration,) = report.migrations
        self.assertTrue(migration.transactional)
        self.assertFalse(migration.held_to_commit)
        self.assertNotIn("window", render(report))
        self.assertNotIn("window.held", [f.rule for f in migration.findings])

    def test_a_static_report_says_which_profile_it_assumed(self):
        report = analyze(["ALTER TABLE t ADD COLUMN d int"], MY)
        self.assertEqual(report.profile, "mysql")
        self.assertEqual(report.version, (8, 0, 19))
        (note,) = report.migrations[0].findings
        self.assertEqual(note.rule, "impact.assumed_profile")
        self.assertIn("MySQL 8.0.19", note.message)
        self.assertIn("MariaDB", note.message)
        self.assertTrue(
            render(report).endswith("Evidence: static (assumed MySQL 8.0.19)")
        )
        # A static ADD COLUMN depends on storage facts it did not read.
        self.assertIs(report.statements[0].confidence, Confidence.LIKELY)
        self.assertIn("did not read", report.statements[0].findings[0].message)

    def test_a_mariadb_context_picks_the_mariadb_rules(self):
        report = analyze(["ALTER TABLE t ADD COLUMN d int"], MY, MARIADB)
        self.assertEqual(report.profile, "mariadb")
        self.assertEqual(report.migrations[0].findings, ())
        self.assertTrue(render(report).endswith("Evidence: catalog (MariaDB 11.4.13)"))
        self.assertIn("[mariadb.add_column.instant]", render(report))

    def test_a_postgres_report_has_no_profile_note(self):
        report = analyze(["CREATE INDEX ix ON t (c)"], Dialects.POSTGRES)
        self.assertEqual(report.migrations[0].findings, ())


class RecognizerTestCase(unittest.TestCase):
    def test_a_column_comment_is_read(self):
        (action,) = recognize("ALTER TABLE t MODIFY c int COMMENT 'n'", MY).actions
        self.assertEqual(action.options["comment"], "n")

    def test_table_options(self):
        parsed = recognize("ALTER TABLE t COMMENT = 'x', AUTO_INCREMENT = 5", MY)
        self.assertEqual(
            [(a.kind, a.options["name"], a.options["value"]) for a in parsed.actions],
            [("table_option", "comment", "x"), ("table_option", "auto_increment", "5")],
        )

    def test_a_leading_scope_applies_to_every_assignment(self):
        parsed = recognize(
            "SET GLOBAL lock_wait_timeout = 5, foreign_key_checks = 0", MY
        )
        self.assertEqual(
            [s[0] for s in parsed.options["settings"]], ["global", "global"]
        )
        parsed = recognize("SET @x = 1, lock_wait_timeout = 5", MY)
        self.assertEqual(
            [s[0] for s in parsed.options["settings"]], ["user", "session"]
        )


SETTINGS_ROW = ("8.4.11", 1, 31536000)
MARIADB_ROW = ("11.4.13-MariaDB-ubu2404", 1, 86400)
SIZE_ROWS = [
    ("app", "orders", 1, 2_000_000, 3 << 30, "DYNAMIC"),
    ("audit", "orders", 0, 10, 8192, "COMPACT"),
    ("app", "fresh", 1, None, 16384, "DYNAMIC"),
]
FULLTEXT_ROWS = [("app", "orders")]
VERSION_ROWS = [("app/orders", 12)]


def drive(plan, answers):
    """Runs a plan to its end, as tests/test_impact_context.py does."""
    asked = []
    answers = list(answers)
    try:
        sql = next(plan)
        while True:
            asked.append(sql)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                sql = plan.throw(answer)
            else:
                sql = plan.send(answer)
    except StopIteration as stop:
        return asked, stop.value


class ContextPlanTestCase(unittest.TestCase):
    def test_reads_the_version_settings_sizes_and_storage(self):
        asked, ctx = drive(
            mysql.context_plan(),
            [[SETTINGS_ROW], SIZE_ROWS, FULLTEXT_ROWS, VERSION_ROWS],
        )
        self.assertEqual(len(asked), 4)
        self.assertEqual((ctx.profile, ctx.version), ("mysql", (8, 4, 11)))
        self.assertEqual(ctx.settings["lock_wait_timeout"], "31536000")
        self.assertEqual(
            ctx.read, {"version", "settings", "sizes", "fulltext", "row_versions"}
        )
        orders = TableStats(2_000_000, 3 << 30, "DYNAMIC", 12, True)
        self.assertEqual(ctx.stats("orders"), orders)
        self.assertEqual(ctx.stats("app.orders"), orders)
        self.assertEqual(
            ctx.stats("audit.orders"), TableStats(10, 8192, "COMPACT", 0, False)
        )
        self.assertEqual(ctx.stats("fresh").rows, None)
        self.assertTrue(all("%" not in sql for sql in asked))

    def test_mariadb_reads_no_row_versions(self):
        asked, ctx = drive(
            mysql.context_plan(), [[MARIADB_ROW], SIZE_ROWS, FULLTEXT_ROWS]
        )
        self.assertEqual(len(asked), 3)
        self.assertEqual((ctx.profile, ctx.version), ("mariadb", (11, 4, 13)))
        self.assertIsNone(ctx.stats("orders").row_versions)
        self.assertNotIn("row_versions", ctx.read)

    def test_mysql_before_8_0_29_reads_no_row_versions(self):
        asked, _ = drive(mysql.context_plan(), [[("8.0.28", 1, 50)], SIZE_ROWS, []])
        self.assertEqual(len(asked), 3)

    def test_failed_reads_leave_their_facts_out(self):
        _, ctx = drive(
            mysql.context_plan(),
            [
                RuntimeError("denied"),
                SIZE_ROWS,
                RuntimeError("denied"),
                RuntimeError("no view"),
            ],
        )
        self.assertEqual(ctx.version, FLOORS["mysql"])
        self.assertEqual(ctx.read, {"sizes"})
        self.assertIsNone(ctx.stats("orders").fulltext)
        _, ctx = drive(mysql.context_plan(), [[SETTINGS_ROW], RuntimeError("denied")])
        self.assertEqual(ctx.read, {"version", "settings"})

    def test_server_version(self):
        self.assertEqual(mysql.server_version("8.0.19"), ("mysql", (8, 0, 19)))
        self.assertEqual(mysql.server_version("8.0.36-log"), ("mysql", (8, 0, 36)))
        self.assertEqual(
            mysql.server_version("10.6.18-MariaDB-log"), ("mariadb", (10, 6, 18))
        )
        self.assertEqual(mysql.server_version("unknown"), ("mysql", FLOORS["mysql"]))


if __name__ == "__main__":
    unittest.main()
