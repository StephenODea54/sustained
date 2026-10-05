"""Tests for the MySQL and MariaDB impact rules."""

import json
import pathlib
import unittest

from sustained.dialects import Dialects
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    TableStats,
    analyze,
)
from sustained.impact.context import FLOORS, assumed
from sustained.impact.recognizer import recognize
from sustained.impact.rules import mysql, profile_for, profiles_for
from sustained.impact.rules.mysql import context as mysql_context
from sustained.impact.rules.mysql import locks as mysql_locks
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
        "ci": IntrospectedTable(
            {
                "id": IntrospectedColumn("int", False, True),
                "name": IntrospectedColumn(
                    "varchar(100)", True, False, collation="utf8mb4_0900_ai_ci"
                ),
                "code": IntrospectedColumn(
                    "varchar(50)", True, False, collation="utf8mb4_0900_ai_ci"
                ),
                "mid": IntrospectedColumn(
                    "varchar(40)", True, False, collation="utf8mb4_0900_ai_ci"
                ),
                "l": IntrospectedColumn(
                    "varchar(100)", True, False, collation="latin1_swedish_ci"
                ),
                "s3": IntrospectedColumn(
                    "varchar(30)", True, False, collation="utf8mb3_general_ci"
                ),
                "m3": IntrospectedColumn(
                    "varchar(80)", True, False, collation="utf8mb3_general_ci"
                ),
            },
            primary_key=("id",),
            indexes={"code_ix": IntrospectedIndex(("code",), False, name="code_ix")},
            name="ci",
        ),
    }
)

_STATS = {
    "t": TableStats(20, 49152, "DYNAMIC", 0, False, collation="utf8mb4_0900_ai_ci"),
    "r": TableStats(3, 16384, "DYNAMIC", 0, False),
    "p": TableStats(2, 16384, "DYNAMIC", 0, False),
    "ft": TableStats(1, 32768, "DYNAMIC", 0, True),
    "cz": TableStats(1, 8192, "COMPRESSED", 0, False),
    "ci": TableStats(2, 16384, "DYNAMIC", 0, False, collation="utf8mb4_0900_ai_ci"),
    "g": TableStats(1, 16384, "DYNAMIC", 0, False),
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
        self.assertEqual(mysql.blocks(mysql_locks.ROW_LOCKS), Blocks.DDL)
        self.assertEqual(mysql.blocks(mysql_locks.INSTANT), Blocks.READS_AND_WRITES)
        self.assertEqual(
            mysql.blocks(mysql_locks.MDL_EXCLUSIVE), Blocks.READS_AND_WRITES
        )
        self.assertEqual(mysql.blocks(mysql_locks.INPLACE_NONE), Blocks.DDL)
        self.assertEqual(mysql.blocks(mysql_locks.NOCOPY_NONE), Blocks.DDL)
        self.assertEqual(mysql.blocks(mysql_locks.COPY_SHARED), Blocks.WRITES)
        self.assertEqual(
            mysql.blocks(mysql_locks.COPY_EXCLUSIVE), Blocks.READS_AND_WRITES
        )
        self.assertEqual(mysql.blocks("SOMETHING ELSE"), Blocks.READS_AND_WRITES)

    def test_lock_order(self):
        self.assertEqual(mysql.lock_rank(None), -1)
        ranks = [mysql.lock_rank(lock) for lock in mysql_locks.LOCKS]
        self.assertEqual(ranks, sorted(ranks))
        self.assertGreater(
            mysql.lock_rank("SOMETHING ELSE"),
            mysql.lock_rank(mysql_locks.MDL_EXCLUSIVE),
        )

    def test_a_label_outside_the_list_ranks_by_its_level(self):
        shared = mysql.lock_rank("NOCOPY, LOCK=SHARED")
        self.assertLess(mysql.lock_rank(mysql_locks.COPY_NONE), shared)
        self.assertLess(shared, mysql.lock_rank(mysql_locks.INPLACE_SHARED))
        self.assertLess(shared, mysql.lock_rank(mysql_locks.MDL_EXCLUSIVE))

    def test_every_lock_but_row_locks_queues(self):
        self.assertFalse(mysql.queues(None))
        self.assertFalse(mysql.queues(mysql_locks.ROW_LOCKS))
        self.assertTrue(mysql.queues(mysql_locks.INPLACE_NONE))
        self.assertTrue(mysql.queues(mysql_locks.INSTANT))

    def test_labels_read_back(self):
        self.assertEqual(
            mysql_locks.parse_label("INSTANT"), mysql_locks.Online("INSTANT")
        )
        self.assertEqual(
            mysql_locks.parse_label("COPY, LOCK=SHARED"),
            mysql_locks.Online("COPY", "SHARED"),
        )
        self.assertIsNone(mysql_locks.parse_label("MDL EXCLUSIVE"))
        self.assertIsNone(mysql_locks.parse_label("INPLACE"))
        self.assertIsNone(mysql_locks.parse_label("INPLACE, LOCK=SOME"))

    def test_combined(self):
        instant = mysql_locks.Online("INSTANT")
        inplace = mysql_locks.Online("INPLACE", "NONE")
        copy = mysql_locks.Online("COPY", "SHARED")
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
                    self.assertIn(rule.id, found, fixture)
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
    ("app", "orders", 1, 2_000_000, 3 << 30, "DYNAMIC", "utf8mb4_0900_ai_ci"),
    ("audit", "orders", 0, 10, 8192, "COMPACT", "latin1_swedish_ci"),
    ("app", "fresh", 1, None, 16384, "DYNAMIC", None),
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
        orders = TableStats(
            2_000_000, 3 << 30, "DYNAMIC", 12, True, collation="utf8mb4_0900_ai_ci"
        )
        self.assertEqual(ctx.stats("orders"), orders)
        self.assertEqual(ctx.stats("app.orders"), orders)
        self.assertEqual(
            ctx.stats("audit.orders"),
            TableStats(10, 8192, "COMPACT", 0, False, collation="latin1_swedish_ci"),
        )
        self.assertIsNone(ctx.stats("fresh").collation)
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

    def test_row_versions_follow_the_file_name_encoding_and_partitions(self):
        sizes = [
            ("app", "a-b", 1, 2, 16384, "DYNAMIC", None),
            ("app", "pt", 1, 2, 16384, "DYNAMIC", None),
            ("app", "Größe", 1, 2, 16384, "DYNAMIC", None),
        ]
        versions = [
            ("app/a@002db", 3),
            ("app/pt#p#p0", 1),
            ("app/pt#P#p1#SP#s0", 4),
            ("app/Gr@1i@1je", 2),
        ]
        _, ctx = drive(mysql.context_plan(), [[SETTINGS_ROW], sizes, [], versions])
        self.assertEqual(ctx.stats("a-b").row_versions, 3)
        self.assertEqual(ctx.stats("pt").row_versions, 4)
        self.assertIsNone(ctx.stats("Größe").row_versions)

    def test_file_name(self):
        self.assertEqual(mysql_context.file_name("a-b"), "a@002db")
        self.assertEqual(mysql_context.file_name("Orders_2"), "orders_2")
        self.assertEqual(mysql_context.file_name("a b"), "a@0020b")
        self.assertIsNone(mysql_context.file_name("Größe"))
        self.assertIsNone(mysql_context.file_name("x\U0001f600"))

    def test_server_version(self):
        self.assertEqual(mysql_context.server_version("8.0.19"), ("mysql", (8, 0, 19)))
        self.assertEqual(
            mysql_context.server_version("8.0.36-log"), ("mysql", (8, 0, 36))
        )
        self.assertEqual(
            mysql_context.server_version("10.6.18-MariaDB-log"),
            ("mariadb", (10, 6, 18)),
        )
        self.assertEqual(
            mysql_context.server_version("unknown"), ("mysql", FLOORS["mysql"])
        )


if __name__ == "__main__":
    unittest.main()
