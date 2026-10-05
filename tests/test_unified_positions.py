"""
Tests that col(), Literal, and raw SQL work in every clause position, and
that plain strings keep their output.
"""

import unittest

from sustained.dialects import Dialects
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
    Literal,
    WindowExpression,
    col,
)
from sustained.model import Model
from sustained.types import Expression


class Thing(Model):
    tableName = "things"


class TestPlainStringOutput(unittest.TestCase):
    """Plain strings and raw SQL render as they do in every position."""

    def setUp(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)

    def tearDown(self) -> None:
        Thing.set_dialect(Dialects.DEFAULT)

    def test_where_order_group(self) -> None:
        sql = str(
            Thing.query()
            .where("things.a", "=", 1)
            .groupBy("things.b")
            .orderBy("things.c", "desc")
        )
        self.assertEqual(
            sql,
            'SELECT * FROM "things" WHERE "things"."a" = 1 '
            'GROUP BY "things"."b" ORDER BY "things"."c" DESC',
        )

    def test_join_on_plain_names(self) -> None:
        sql = str(Thing.query().join("other", "things.id", "=", "other.thing_id"))
        self.assertIn('ON "things"."id" = "other"."thing_id"', sql)

    def test_insert_and_update_plain_values(self) -> None:
        self.assertEqual(
            str(Thing.query().insert({"a": 1, "b": "x"})),
            'INSERT INTO "things" ("a", "b") VALUES (1, \'x\')',
        )
        update = Thing.query().where("id", "=", 1).update({"a": Expression("a + 1")})
        self.assertEqual(str(update), 'UPDATE "things" SET "a" = a + 1 WHERE "id" = 1')

    def test_case_results(self) -> None:
        case = CaseExpression("k", "z").when("a > 1", Column("b"))
        self.assertEqual(
            str(Thing.query().select(case)),
            'SELECT CASE WHEN a > 1 THEN b ELSE \'z\' END AS "k" FROM "things"',
        )


class TestWrappersInEveryPosition(unittest.TestCase):
    """col(), Literal, raw SQL, and Func render in each column position."""

    def setUp(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)

    def tearDown(self) -> None:
        Thing.set_dialect(Dialects.DEFAULT)

    def test_where_order_group_take_col(self) -> None:
        sql = str(
            Thing.query()
            .where(col("things.a"), "=", 1)
            .groupBy(col("things.b"))
            .orderBy(col("things.c"))
        )
        self.assertEqual(
            sql,
            'SELECT * FROM "things" WHERE "things"."a" = 1 '
            'GROUP BY "things"."b" ORDER BY "things"."c" ASC',
        )

    def test_where_column_takes_raw_func_and_literal(self) -> None:
        sql = str(
            Thing.query()
            .where(Column("a + b"), ">", 1)
            .where(Func("LOWER", "name"), "=", "x")
            .where(Literal(1), "=", 1)
        )
        self.assertEqual(
            sql,
            'SELECT * FROM "things" WHERE a + b > 1 AND '
            "LOWER(\"name\") = 'x' AND 1 = 1",
        )

    def test_unknown_object_still_raises(self) -> None:
        with self.assertRaises(TypeError):
            str(Thing.query().orderBy(object()))  # type: ignore[arg-type]

    def test_join_on_takes_col_and_quoted_names(self) -> None:
        sql = str(Thing.query().join("other", col("things.id"), "=", '"other"."a.b"'))
        self.assertIn('ON "things"."id" = "other"."a.b"', sql)

    def test_join_on_in_mysql_quotes_with_backticks(self) -> None:
        Thing.set_dialect(Dialects.MYSQL)
        sql = str(Thing.query().join("other", "things.id", "=", col("other.tid")))
        self.assertIn("ON `things`.`id` = `other`.`tid`", sql)

    def test_aggregate_and_window_take_col(self) -> None:
        agg = AggregateExpression("SUM", col("price"), "total")  # type: ignore[arg-type]
        window = WindowExpression(
            "ROW_NUMBER", "rn", partition_by=[col("g")], order_by=[col("d")]  # type: ignore[list-item]
        )
        sql = str(Thing.query().select(agg, window))
        self.assertIn('SUM("price") AS "total"', sql)
        self.assertIn('ROW_NUMBER() OVER (PARTITION BY "g" ORDER BY "d") AS "rn"', sql)

    def test_select_takes_literal(self) -> None:
        sql = str(Thing.query().select(Literal("x")))
        self.assertEqual(sql, "SELECT 'x' FROM \"things\"")

    def test_case_results_take_col_and_literal(self) -> None:
        case = CaseExpression("k", Literal("z")).when("a > 1", col("b"))
        self.assertEqual(
            str(Thing.query().select(case)),
            'SELECT CASE WHEN a > 1 THEN "b" ELSE \'z\' END AS "k" FROM "things"',
        )

    def test_insert_unwraps_every_wrapper(self) -> None:
        query = Thing.query().insert(
            {
                "a": Literal("x"),
                "b": col("c"),
                "d": Column("now()"),
                "e": Func("LOWER", Literal("Y")),
            }
        )
        sql, params = query.to_sql()
        self.assertEqual(
            sql,
            'INSERT INTO "things" ("a", "b", "d", "e") '
            "VALUES (%s, \"c\", now(), LOWER('Y'))",
        )
        self.assertEqual(params, ("x",))
        self.assertTrue(query._has_expression_values())

    def test_update_unwraps_every_wrapper(self) -> None:
        query = (
            Thing.query()
            .where("id", "=", 1)
            .update({"a": Literal(2), "b": col("c"), "d": Func("UPPER", "d")})
        )
        self.assertEqual(
            str(query),
            'UPDATE "things" SET "a" = 2, "b" = "c", "d" = UPPER("d") WHERE "id" = 1',
        )


class TestQuotedWriteKeys(unittest.TestCase):
    """insert() and update() keys accept a name already in quotes."""

    def tearDown(self) -> None:
        Thing.set_dialect(Dialects.DEFAULT)

    def test_quoted_keys_take_the_target_quotes(self) -> None:
        cases = (
            (Dialects.POSTGRES, "[Full Name]", '"Full Name"'),
            (Dialects.MYSQL, '"Full Name"', "`Full Name`"),
            (Dialects.MSSQL, "`Full Name`", "[Full Name]"),
        )
        for dialect, key, quoted in cases:
            with self.subTest(dialect=dialect.name):
                Thing.set_dialect(dialect)
                insert = str(Thing.query().insert({key: 1}))
                self.assertIn(f"({quoted}) VALUES (1)", insert)
                update = str(Thing.query().where("id", "=", 1).update({key: 1}))
                self.assertIn(f"SET {quoted} = 1", update)

    def test_insert_from_takes_quoted_columns(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)
        source = Thing.query().select("a")
        sql = str(Thing.query().insert_from(['"a.b"', "c"], source))
        self.assertIn('INSERT INTO "things" ("a.b", "c") SELECT', sql)

    def test_dotted_key_stays_one_name(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)
        sql = str(Thing.query().insert({"a.b": 1}))
        self.assertIn('("a.b") VALUES (1)', sql)


if __name__ == "__main__":
    unittest.main()
