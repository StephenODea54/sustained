"""Tests for the recognizer's DML, maintenance, and SET statements."""

import unittest

from sustained.impact.recognizer import (
    recognize,
)
from tests.test_impact_recognizer import (
    MSSQL,
    MYSQL,
    PG,
    SQLITE,
    RecognizerTestCase,
)


class DmlTestCase(RecognizerTestCase):
    def test_update(self):
        parsed = recognize('UPDATE "t" SET "a" = 1 WHERE "a" IS NULL')
        self.assertEqual((parsed.kind, parsed.table), ("update", "t"))
        self.assertTrue(parsed.options["where"])
        self.assertFalse(parsed.options["limited"])
        every = recognize("UPDATE ONLY t AS x SET a = (SELECT 1 WHERE true)")
        self.assertFalse(every.options["where"])

    def test_capped_writes(self):
        self.assertTrue(
            recognize("UPDATE TOP (100) t SET a = 1", MSSQL).options["limited"]
        )
        self.assertTrue(
            recognize("DELETE FROM t WHERE a < 5 LIMIT 1000", MYSQL).options["limited"]
        )

    def test_delete_forms(self):
        parsed = recognize("DELETE FROM ONLY t USING u WHERE t.a = u.a")
        self.assertEqual((parsed.kind, parsed.table), ("delete", "t"))
        self.assertTrue(parsed.options["where"])
        self.assertEqual(recognize("DELETE t WHERE a = 1", MSSQL).table, "t")
        self.assertEqual(
            recognize("DELETE TOP (10) FROM t", MSSQL).options["limited"], True
        )
        self.assertFalse(recognize("DELETE FROM t").options["where"])

    def test_insert_forms(self):
        values = recognize("INSERT INTO t (a, b) VALUES (1, 2), (3, 4) RETURNING a")
        self.assertEqual((values.kind, values.table), ("insert", "t"))
        self.assertEqual(values.options["source"], "values")
        self.assertEqual(values.options["rows"], 2)
        select = recognize('INSERT INTO "new" ("id") SELECT "id" FROM "old"')
        self.assertEqual(select.options["source"], "select")
        self.assertIsNone(select.options["rows"])
        self.assertEqual(
            recognize("INSERT INTO t DEFAULT VALUES").options["source"], "default"
        )
        self.assertEqual(
            recognize("INSERT IGNORE INTO t (a) VALUES (1)", MYSQL).table, "t"
        )
        self.assertEqual(
            recognize("INSERT INTO t (SELECT * FROM u)").options["source"], "select"
        )
        self.assertUnknown("INSERT INTO t SET a = 1", MYSQL, table="t")

    def test_with_prefixed_writes(self):
        parsed = recognize(
            "WITH RECURSIVE ids (id) AS NOT MATERIALIZED (SELECT 1), "
            "more AS (SELECT 2) UPDATE t SET a = 1 FROM ids WHERE t.id = ids.id"
        )
        self.assertEqual((parsed.kind, parsed.table), ("update", "t"))

    def test_a_writing_cte_is_unknown(self):
        self.assertUnknown(
            "WITH gone AS (DELETE FROM t RETURNING *) INSERT INTO u SELECT * FROM gone"
        )
        self.assertUnknown("WITH x AS (SELECT 1) SELECT * FROM x")

    def test_update_without_set_is_unknown(self):
        self.assertUnknown("UPDATE t", table="t")


class MaintenanceTestCase(RecognizerTestCase):
    def test_truncate_and_rename(self):
        truncated = recognize("TRUNCATE TABLE ONLY a, b RESTART IDENTITY CASCADE")
        self.assertEqual(truncated.options["tables"], ("a", "b"))
        renamed = recognize("RENAME TABLE a TO b, c TO d", MYSQL)
        self.assertEqual((renamed.kind, renamed.table), ("rename_table", "a"))
        self.assertEqual(renamed.options["renames"], (("a", "b"), ("c", "d")))

    def test_reindex(self):
        parsed = recognize("REINDEX (VERBOSE) TABLE CONCURRENTLY app.t")
        self.assertEqual((parsed.kind, parsed.table), ("reindex", "app.t"))
        self.assertTrue(parsed.options["concurrently"])
        index = recognize("REINDEX INDEX ix")
        self.assertIsNone(index.table)
        self.assertEqual(index.options["target"], "index")

    def test_vacuum_and_analyze(self):
        self.assertTrue(recognize("VACUUM FULL ANALYZE t").options["full"])
        self.assertTrue(recognize("VACUUM (FULL, VERBOSE) t (a), u").options["full"])
        self.assertEqual(recognize("VACUUM (FULL) t, u").options["tables"], ("t", "u"))
        self.assertFalse(recognize("VACUUM", SQLITE).options["full"])
        self.assertEqual(recognize("ANALYZE VERBOSE t").table, "t")
        self.assertIsNone(recognize("ANALYZE").table)

    def test_cluster_and_optimize(self):
        self.assertEqual(recognize("CLUSTER t USING ix").options["index"], "ix")
        self.assertEqual(recognize("CLUSTER ix ON t").table, "t")
        self.assertIsNone(recognize("CLUSTER").table)
        self.assertEqual(recognize("CLUSTER VERBOSE t").table, "t")
        parsed = recognize("OPTIMIZE LOCAL TABLE a, b", MYSQL)
        self.assertEqual(parsed.options["tables"], ("a", "b"))

    def test_refresh(self):
        parsed = recognize("REFRESH MATERIALIZED VIEW CONCURRENTLY v WITH DATA")
        self.assertTrue(parsed.options["concurrently"])
        self.assertTrue(parsed.options["with_data"])
        self.assertFalse(
            recognize("REFRESH MATERIALIZED VIEW v WITH NO DATA").options["with_data"]
        )

    def test_comments(self):
        column = recognize("COMMENT ON COLUMN app.t.c IS 'x'")
        self.assertEqual((column.table, column.options["column"]), ("app.t", "c"))
        self.assertEqual(recognize("COMMENT ON TABLE t IS NULL").table, "t")
        self.assertEqual(recognize("COMMENT ON CONSTRAINT ck ON t IS 'x'").table, "t")
        self.assertIsNone(recognize("COMMENT ON INDEX ix IS 'x'").table)
        self.assertIsNone(recognize("COMMENT ON FUNCTION f(int) IS 'x'").table)
        self.assertUnknown("COMMENT ON COLUMN c IS 'x'")
        self.assertUnknown("COMMENT ON ROLE r IS 'x'")

    def test_enum_values(self):
        added = recognize("ALTER TYPE mood ADD VALUE IF NOT EXISTS 'sad' AFTER 'ok'")
        self.assertEqual(added.kind, "alter_type_add_value")
        self.assertEqual(added.options["value"], "sad")
        self.assertEqual(
            recognize("ALTER TYPE mood RENAME VALUE 'a' TO 'b'").kind,
            "alter_type_rename_value",
        )
        self.assertUnknown("ALTER TYPE mood OWNER TO app")

    def test_lock_table(self):
        parsed = recognize("LOCK TABLE ONLY a, b IN SHARE ROW EXCLUSIVE MODE NOWAIT")
        self.assertEqual(parsed.options["mode"], "SHARE ROW EXCLUSIVE")
        self.assertTrue(parsed.options["nowait"])
        self.assertEqual(recognize("LOCK t").options["mode"], "ACCESS EXCLUSIVE")
        self.assertUnknown("LOCK TABLE t IN SOME MODE", table="t")
        self.assertUnknown("LOCK TABLES t WRITE", MYSQL)


class SetTestCase(RecognizerTestCase):
    def settings(self, sql, dialect=PG):
        parsed = recognize(sql, dialect)
        self.assertEqual(parsed.kind, "set", parsed)
        return parsed.options["settings"]

    def test_postgres_scopes(self):
        self.assertEqual(
            self.settings("SET lock_timeout = '5s'"),
            (("session", "lock_timeout", "5s"),),
        )
        self.assertEqual(
            self.settings("SET LOCAL lock_timeout TO 0"),
            (("local", "lock_timeout", "0"),),
        )
        self.assertEqual(
            self.settings("SET search_path TO app, public"),
            (("session", "search_path", "app, public"),),
        )
        self.assertEqual(
            self.settings("SET TIME ZONE 'UTC'"), (("session", "timezone", "UTC"),)
        )

    def test_mysql_forms(self):
        self.assertEqual(
            self.settings("SET foreign_key_checks = 0, unique_checks = 0", MYSQL),
            (
                ("session", "foreign_key_checks", "0"),
                ("session", "unique_checks", "0"),
            ),
        )
        self.assertEqual(
            self.settings("SET @@GLOBAL.lock_wait_timeout = 5", MYSQL),
            (("global", "lock_wait_timeout", "5"),),
        )
        self.assertEqual(self.settings("SET @x = 1", MYSQL), (("user", "x", "1"),))

    def test_mssql_and_sqlite_forms(self):
        self.assertEqual(
            self.settings("SET LOCK_TIMEOUT 5000", MSSQL),
            (("session", "lock_timeout", "5000"),),
        )
        self.assertEqual(
            self.settings("PRAGMA foreign_keys = OFF", SQLITE),
            (("pragma", "foreign_keys", "OFF"),),
        )
        self.assertEqual(
            self.settings("PRAGMA main.journal_mode", SQLITE),
            (("pragma", "main.journal_mode", ""),),
        )
        self.assertEqual(
            self.settings("PRAGMA busy_timeout(500)", SQLITE),
            (("pragma", "busy_timeout", "500"),),
        )

    def test_a_setting_with_no_value_is_unknown(self):
        self.assertUnknown("SET lock_timeout =")


if __name__ == "__main__":
    unittest.main()
