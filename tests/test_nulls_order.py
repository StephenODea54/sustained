import sqlite3
import unittest

from sustained import Model, QueryBuilder
from sustained.dialects import Dialects


class Show(Model):
    tableName = "shows"


def ordered(dialect, *args):
    return str(QueryBuilder(Show, dialect=dialect).orderBy(*args))


class TestNullsOrderRendering(unittest.TestCase):
    def test_native_dialects(self):
        cases = {
            Dialects.DEFAULT: "SELECT * FROM shows ORDER BY starts_at DESC NULLS LAST",
            Dialects.POSTGRES: 'SELECT * FROM "shows" ORDER BY "starts_at" DESC NULLS LAST',
            Dialects.DUCKDB: 'SELECT * FROM "shows" ORDER BY "starts_at" DESC NULLS LAST',
            Dialects.PRESTO: 'SELECT * FROM "shows" ORDER BY "starts_at" DESC NULLS LAST',
            Dialects.ATHENA: 'SELECT * FROM "shows" ORDER BY "starts_at" DESC NULLS LAST',
        }
        for dialect, expected in cases.items():
            with self.subTest(dialect=dialect):
                self.assertEqual(
                    ordered(dialect, "starts_at", "desc", "last"), expected
                )

    def test_mysql_emulates_with_case_key(self):
        self.assertEqual(
            ordered(Dialects.MYSQL, "starts_at", "asc", "first"),
            "SELECT * FROM `shows` ORDER BY CASE WHEN `starts_at` IS NULL "
            "THEN 0 ELSE 1 END, `starts_at` ASC",
        )

    def test_mssql_emulates_with_case_key(self):
        self.assertEqual(
            ordered(Dialects.MSSQL, "starts_at", "desc", "last"),
            "SELECT * FROM [shows] ORDER BY CASE WHEN [starts_at] IS NULL "
            "THEN 1 ELSE 0 END, [starts_at] DESC",
        )

    def test_emulating_dialects_without_nulls_keep_plain_key(self):
        self.assertEqual(
            ordered(Dialects.MSSQL, "starts_at", "desc"),
            "SELECT * FROM [shows] ORDER BY [starts_at] DESC",
        )

    def test_nulls_is_case_insensitive(self):
        self.assertEqual(
            ordered(Dialects.DEFAULT, "starts_at", "asc", "First"),
            "SELECT * FROM shows ORDER BY starts_at ASC NULLS FIRST",
        )

    def test_raw_column_keeps_its_text(self):
        query = QueryBuilder(Show, dialect=Dialects.MYSQL).orderBy(
            QueryBuilder.raw("COALESCE(ends_at, starts_at)"), "asc", "last"
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM `shows` ORDER BY CASE WHEN COALESCE(ends_at, starts_at) "
            "IS NULL THEN 1 ELSE 0 END, COALESCE(ends_at, starts_at) ASC",
        )

    def test_unknown_nulls_rejected(self):
        with self.assertRaisesRegex(ValueError, "'first', 'last', or None"):
            Show.query().orderBy("starts_at", "asc", "middle")

    def test_keyword_spelling(self):
        self.assertEqual(
            str(Show.query().orderBy("starts_at", nulls="last")),
            "SELECT * FROM shows ORDER BY starts_at ASC NULLS LAST",
        )


class TestNullsOrderOnSqlite(unittest.TestCase):
    """
    Runs the native and the emulated form on SQLite, which accepts both,
    so the CASE key used for MySQL and SQL Server is checked against the
    engine's own NULLS FIRST and NULLS LAST.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE shows (starts_at INTEGER)")
        self.conn.executemany(
            "INSERT INTO shows VALUES (?)", [(2,), (None,), (1,), (None,), (3,)]
        )

    def tearDown(self):
        self.conn.close()

    def rows(self, sql):
        return [row[0] for row in self.conn.execute(sql)]

    def test_emulated_order_matches_native(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        for direction in ("ASC", "DESC"):
            for nulls in ("FIRST", "LAST"):
                with self.subTest(direction=direction, nulls=nulls):
                    native = compiler.compile_order_entry("starts_at", direction, nulls)
                    emulated = compiler.compile_emulated_nulls_order(
                        "starts_at", direction, nulls
                    )
                    expected = self.rows(f"SELECT * FROM shows ORDER BY {native}")
                    self.assertEqual(
                        self.rows(f"SELECT * FROM shows ORDER BY {emulated}"), expected
                    )
                    nulls_at = [0, 1] if nulls == "FIRST" else [3, 4]
                    self.assertEqual([expected[i] for i in nulls_at], [None, None])


if __name__ == "__main__":
    unittest.main()
