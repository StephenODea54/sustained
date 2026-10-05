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

    def test_the_tables_an_insert_reads(self):
        def reads(sql):
            return recognize(sql).options.get("reads")

        self.assertEqual(
            reads('INSERT INTO "new" ("id") SELECT "id" FROM "old"'), ("old",)
        )
        self.assertEqual(
            reads("INSERT INTO t SELECT * FROM app.a JOIN b ON a.i = b.i, c AS cc"),
            ("app.a", "b", "c"),
        )
        self.assertEqual(reads("INSERT INTO t TABLE u"), ("u",))
        self.assertEqual(reads("INSERT INTO t SELECT 1"), ())
        self.assertIsNone(reads("INSERT INTO t SELECT * FROM generate_series(1, 5)"))
        self.assertIsNone(reads("INSERT INTO t VALUES (1)"))

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

    def test_a_statement_after_a_sql_server_write_is_unread(self):
        second = "ALTER TABLE big ALTER COLUMN a bigint NOT NULL"
        self.assertUnknown(f"UPDATE t SET a = 1 WHERE id = 1 {second}", MSSQL, "t")
        self.assertUnknown(f"CREATE TABLE t (a int) {second}", MSSQL, "t")
        self.assertUnknown(f"INSERT INTO t (a) SELECT 1 {second}", MSSQL, "t")
        self.assertUnknown(
            "ALTER TABLE t ADD c int DEFAULT 1 UPDATE x SET a = 2", MSSQL, "t"
        )

    def test_sql_server_insert_sources(self):
        union = recognize(
            "INSERT INTO t SELECT a FROM u UNION ALL SELECT b FROM v", MSSQL
        )
        self.assertEqual((union.kind, union.options["source"]), ("insert", "select"))
        self.assertEqual(
            recognize("INSERT INTO t VALUES (1), (2)", MSSQL).options["rows"], 2
        )
        self.assertEqual(
            recognize("INSERT INTO t DEFAULT VALUES", MSSQL).options["source"],
            "default",
        )

    def test_multi_table_writes_are_unknown(self):
        for sql in (
            "UPDATE a JOIN b ON a.id = b.id SET b.x = 1",
            "UPDATE a x INNER JOIN b ON x.id = b.id SET b.x = 1",
            "UPDATE a, b SET b.x = 1",
            "DELETE a, b FROM a JOIN b ON a.id = b.id",
            "DELETE FROM a, b USING a JOIN b ON a.id = b.id",
        ):
            with self.subTest(sql):
                self.assertUnknown(sql, MYSQL, table="a")

    def test_a_delete_through_an_alias_reports_its_table(self):
        for sql, dialect in (
            ("DELETE a FROM items a JOIN b ON a.id = b.id WHERE b.x = 1", MYSQL),
            ("DELETE FROM x USING items AS x JOIN y ON x.id = y.id", MYSQL),
            ("DELETE x FROM items x WITH (NOLOCK) JOIN y ON x.id = y.id", MSSQL),
            ("DELETE FROM x FROM items x, y WHERE x.id = y.id", MSSQL),
            ("DELETE items FROM items JOIN y ON items.id = y.id", MYSQL),
        ):
            with self.subTest(sql):
                self.assertEqual(recognize(sql, dialect).table, "items")
        self.assertEqual(
            recognize(
                "UPDATE x SET a = 1 FROM app.items x WHERE x.id = 1", MSSQL
            ).table,
            "app.items",
        )
        self.assertUnknown("DELETE d FROM (SELECT 1 AS id) d", MSSQL, table="d")

    def test_where_and_limit_are_read_in_their_clause(self):
        aliased = recognize("UPDATE t limit SET a = 1 WHERE id = 1", SQLITE)
        self.assertTrue(aliased.options["where"])
        self.assertFalse(aliased.options["limited"])
        dotless = recognize("UPDATE t SET a = 1 WHERE id = 1 l\u0131m\u0131t 5", MYSQL)
        self.assertFalse(dotless.options["limited"])
        inner = recognize("UPDATE t SET a = (SELECT 1 LIMIT 1)", MYSQL)
        self.assertFalse(inner.options["limited"])
        for tail in ("LIMIT 10", "LIMIT ?", "LIMIT 10 OFFSET 5", "LIMIT 5, 10"):
            with self.subTest(tail):
                sql = f"DELETE FROM t WHERE a < 5 ORDER BY a {tail}"
                self.assertTrue(recognize(sql, MYSQL).options["limited"])
        self.assertFalse(
            recognize("DELETE FROM t WHERE a < 5 LIMIT a", MYSQL).options["limited"]
        )
        self.assertFalse(
            recognize("DELETE FROM t WHERE a < 5 LIMIT 5 x 1", MYSQL).options["limited"]
        )
        self.assertFalse(
            recognize("UPDATE t SET a = 1 WHERE id = 1 LIMIT 10", PG).options["limited"]
        )


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

    def test_mysql_analyze_table_names_its_table(self):
        # No rule reads MySQL's ANALYZE TABLE; the unknown statement
        # names the table, never the keyword.
        for text in (
            "ANALYZE TABLE it_impact_orders, b",
            "ANALYZE NO_WRITE_TO_BINLOG TABLE it_impact_orders",
            "ANALYZE LOCAL TABLE it_impact_orders",
        ):
            for dialect in (MYSQL, PG):
                parsed = recognize(text, dialect)
                self.assertEqual(
                    (parsed.kind, parsed.table), ("unknown", "it_impact_orders")
                )
                self.assertEqual(
                    parsed.options["reason"], "no rule reads ANALYZE TABLE"
                )
        self.assertEqual(recognize("ANALYZE local").table, "local")

    def test_a_bare_table_keyword_is_never_the_table(self):
        for text in ("VACUUM TABLE t", "VACUUM FULL TABLE t", "CLUSTER TABLE t"):
            parsed = recognize(text)
            self.assertEqual((parsed.kind, parsed.table), ("unknown", None))
        self.assertEqual(recognize('VACUUM "table"').table, "table")

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

    def test_resets(self):
        self.assertEqual(
            self.settings("RESET lock_timeout"),
            (("reset", "lock_timeout", "DEFAULT"),),
        )
        for sql in ("RESET ALL", "DISCARD ALL"):
            self.assertEqual(self.settings(sql), (("reset", "all", "DEFAULT"),))
        self.assertUnknown("RESET SESSION AUTHORIZATION")
        self.assertUnknown("RESET ROLE")
        self.assertUnknown("DISCARD PLANS")

    def test_rollbacks(self):
        for sql, dialect in (
            ("ROLLBACK", PG),
            ("ROLLBACK WORK", PG),
            ("ROLLBACK TO SAVEPOINT s", PG),
            ("ROLLBACK TO s", PG),
            ("ROLLBACK AND NO CHAIN", PG),
            ("ROLLBACK WORK AND CHAIN", MYSQL),
            ("ROLLBACK TRANSACTION t1", MSSQL),
            ("ROLLBACK TRAN", MSSQL),
        ):
            with self.subTest(sql):
                self.assertEqual(
                    self.settings(sql, dialect), (("rollback", "all", ""),)
                )
        self.assertUnknown("ROLLBACK PREPARED 'tx1'")

    def test_set_config(self):
        self.assertEqual(
            self.settings(
                "SELECT set_config('lock_timeout', '5s', true), "
                "pg_catalog.set_config('Search_Path', 'app', 'f')"
            ),
            (("local", "lock_timeout", "5s"), ("session", "search_path", "app")),
        )
        for sql in (
            "SELECT now()",
            "SELECT set_config('lock_timeout', '5s')",
            "SELECT set_config('lock_timeout', $1, false)",
            "SELECT set_config('lock_timeout', '5s', is_local)",
            "SELECT set_config('lock_timeout', '5s', false) FROM t",
        ):
            with self.subTest(sql):
                self.assertUnknown(sql)


if __name__ == "__main__":
    unittest.main()
