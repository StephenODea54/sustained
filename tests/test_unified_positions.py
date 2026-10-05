"""
Tests that col(), Literal, and raw SQL work in every clause position, and
that plain strings keep their output.
"""

import unittest
import warnings

import sustained
from sustained.builder import QueryBuilder
from sustained.dialects import Dialects
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
    Literal,
    Subquery,
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
        case = CaseExpression("k", "z").when("a > 1", sustained.raw("b"))
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
            .where(sustained.raw("a + b"), ">", 1)
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
                "d": sustained.raw("now()"),
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


class TestLiteralInOrderAndGroup(unittest.TestCase):
    """A Literal raises in ORDER BY and GROUP BY, where 1 is a position."""

    def test_each_method_raises_with_raw_hint(self) -> None:
        calls = {
            "orderBy": lambda q: q.orderBy(Literal(1)),
            "groupBy": lambda q: q.groupBy("a", Literal(1)),
            "groupByRollup": lambda q: q.groupByRollup("a", Literal(1)),
            "groupByCube": lambda q: q.groupByCube(Literal(1)),
            "groupByGroupingSets": lambda q: q.groupByGroupingSets(
                ("a",), ("b", Literal(1))
            ),
        }
        for method, call in calls.items():
            with self.subTest(method=method):
                with self.assertRaises(ValueError) as caught:
                    call(Thing.query())
                self.assertIn(
                    f"{method}() does not take a Literal", str(caught.exception)
                )
                self.assertIn("raw('1')", str(caught.exception))

    def test_raw_names_a_position(self) -> None:
        sql = str(
            Thing.query()
            .select("a", "b")
            .groupBy(sustained.raw("1"))
            .orderBy(sustained.raw("2"), "desc")
        )
        self.assertEqual(sql, "SELECT a, b FROM things GROUP BY 1 ORDER BY 2 DESC")


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

    def test_conflict_and_merge_take_quoted_columns(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)
        sql = str(
            Thing.query()
            .insert({"[Full Name]": "x", "id": 1, "n": 2})
            .onConflict('"id"')
            .merge(["`n`"])
        )
        self.assertEqual(
            sql,
            'INSERT INTO "things" ("Full Name", "id", "n") '
            "VALUES ('x', 1, 2) ON CONFLICT (\"id\") "
            'DO UPDATE SET "n" = EXCLUDED."n"',
        )

    def test_conflict_quoted_column_not_inserted_raises(self) -> None:
        query = Thing.query().insert({"id": 1})
        with self.assertRaises(ValueError) as caught:
            query.onConflict('"other"')
        self.assertIn("['other']", str(caught.exception))

    def test_dotted_key_stays_one_name(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)
        sql = str(Thing.query().insert({"a.b": 1}))
        self.assertIn('("a.b") VALUES (1)', sql)


class TestFuncStringArguments(unittest.TestCase):
    """A Func string argument follows the column string rule."""

    def tearDown(self) -> None:
        Thing.set_dialect(Dialects.DEFAULT)

    def test_quoted_parts_and_calls(self) -> None:
        cases = (
            (Dialects.POSTGRES, '"t"."a.b"', 'COALESCE("t"."a.b", COUNT("x"))'),
            (Dialects.MYSQL, "[t].[a.b]", "COALESCE(`t`.`a.b`, COUNT(`x`))"),
            (Dialects.MSSQL, "`t`.`a.b`", "COALESCE([t].[a.b], COUNT([x]))"),
        )
        for dialect, arg, expected in cases:
            with self.subTest(dialect=dialect.name):
                Thing.set_dialect(dialect)
                sql = str(Thing.query().select(Func("COALESCE", arg, "COUNT(x)")))
                self.assertIn(expected, sql)

    def test_plain_paths_keep_their_output(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)
        sql = str(Thing.query().select(Func("LOWER", "t.name", alias="n")))
        self.assertEqual(sql, 'SELECT LOWER("t"."name") AS "n" FROM "things"')

    def test_sql_text_is_quoted_as_one_name(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)
        sql = str(Thing.query().select(Func("LOWER", "id; DROP TABLE things")))
        self.assertIn('LOWER("id; DROP TABLE things")', sql)

    def test_default_dialect_refuses_text_that_is_not_a_name(self) -> None:
        with self.assertRaises(ValueError):
            str(Thing.query().select(Func("LOWER", "not a column")))


class TestRawAndColumn(unittest.TestCase):
    """raw() wraps raw SQL, and Column is a deprecated alias of it."""

    def test_raw_is_exported_and_matches_query_builder_raw(self) -> None:
        self.assertIsInstance(sustained.raw("1"), Expression)
        self.assertEqual(str(sustained.raw("now()")), "now()")
        self.assertEqual(str(QueryBuilder.raw("now()")), "now()")

    def test_column_warns_and_names_raw(self) -> None:
        with self.assertWarnsRegex(DeprecationWarning, r"Use raw\(\)"):
            column = Column("a + 1")
        self.assertIsInstance(column, Expression)
        self.assertEqual(column.name, "a + 1")

    def test_column_works_where_raw_works(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            flag = Column("TRUE")
            inner = Column("SELECT 1")
        for value, sub in (
            (flag, inner),
            (sustained.raw("TRUE"), sustained.raw("SELECT 1")),
        ):
            with self.subTest(value=type(value).__name__):
                sql = str(Thing.query().where("a", "IS", value).whereIn("b", sub))
                self.assertEqual(
                    sql,
                    "SELECT * FROM things WHERE a IS TRUE AND b IN (SELECT 1)",
                )


class TestCaseConditions(unittest.TestCase):
    """A CASE condition takes a Predicate or a raw SQL string."""

    def tearDown(self) -> None:
        Thing.set_dialect(Dialects.DEFAULT)

    def test_predicate_condition_quotes_per_dialect(self) -> None:
        cases = (
            (Dialects.POSTGRES, "CASE WHEN (\"age\" >= 18 AND \"name\" = 'O''Neil')"),
            (Dialects.MYSQL, "CASE WHEN (`age` >= 18 AND `name` = 'O''Neil')"),
        )
        for dialect, expected in cases:
            with self.subTest(dialect=dialect.name):
                Thing.set_dialect(dialect)
                case = CaseExpression("k", "minor").when(
                    (col("age") >= 18) & (col("name") == "O'Neil"), "adult"
                )
                sql, params = Thing.query().select(case).to_sql()
                self.assertIn(expected + " THEN 'adult' ELSE 'minor' END", sql)
                self.assertEqual(params, ())

    def test_select_case_takes_both_kinds(self) -> None:
        sql = str(
            Thing.query().select_case("k", 0, [(col("a").is_null(), 1), ("b > 2", 2)])
        )
        self.assertEqual(
            sql,
            "SELECT CASE WHEN a IS NULL THEN 1 WHEN b > 2 THEN 2 ELSE 0 END AS k "
            "FROM things",
        )


if __name__ == "__main__":
    unittest.main()


class TestSubqueryInColumnPositions(unittest.TestCase):
    """A Subquery in a column position binds its values through to_sql()."""

    def setUp(self) -> None:
        Thing.set_dialect(Dialects.POSTGRES)

    def tearDown(self) -> None:
        Thing.set_dialect(Dialects.DEFAULT)

    def _sub(self) -> Subquery:
        return Subquery(Thing.query().select("n").where("k", "=", "v"), "s")

    def _every_position(self, wrapper: object) -> QueryBuilder:
        window = WindowExpression("ROW_NUMBER", partition_by=[wrapper], alias="r")
        return (
            Thing.query()
            .select("a", window)
            .join("other", wrapper, "=", "other.id")  # type: ignore[arg-type]
            .where(wrapper, "=", 1)  # type: ignore[arg-type]
            .groupBy(wrapper)  # type: ignore[arg-type]
            .having(AggregateExpression("MAX", wrapper), ">", 2)  # type: ignore[arg-type]
            .orderBy(wrapper)  # type: ignore[arg-type]
        )

    def test_values_bind_in_text_order(self) -> None:
        sql, params = self._every_position(self._sub()).to_sql()
        inner = '(SELECT "n" FROM "things" WHERE "k" = %s)'
        self.assertEqual(
            sql,
            f'SELECT "a", ROW_NUMBER() OVER (PARTITION BY {inner}) AS "r" '
            f'FROM "things" JOIN "other" ON {inner} = "other"."id" '
            f"WHERE {inner} = %s GROUP BY {inner} "
            f"HAVING MAX({inner}) > %s ORDER BY {inner} ASC",
        )
        self.assertEqual(params, ("v", "v", "v", 1, "v", "v", 2, "v"))

    def test_str_keeps_inline_values(self) -> None:
        inner = '(SELECT "n" FROM "things" WHERE "k" = \'v\')'
        self.assertEqual(
            str(self._every_position(self._sub())),
            f'SELECT "a", ROW_NUMBER() OVER (PARTITION BY {inner}) AS "r" '
            f'FROM "things" JOIN "other" ON {inner} = "other"."id" '
            f"WHERE {inner} = 1 GROUP BY {inner} "
            f"HAVING MAX({inner}) > 2 ORDER BY {inner} ASC",
        )

    def test_other_wrappers_keep_their_output(self) -> None:
        cases = (
            (col("b"), '"b"'),
            (sustained.raw("b + 1"), "b + 1"),
            (Func("LOWER", "b", Literal("x")), "LOWER(\"b\", 'x')"),
        )
        for wrapper, rendered in cases:
            with self.subTest(rendered=rendered):
                sql, params = self._every_position(wrapper).to_sql()
                self.assertEqual(
                    sql,
                    f'SELECT "a", ROW_NUMBER() OVER (PARTITION BY {rendered}) '
                    f'AS "r" FROM "things" JOIN "other" ON {rendered} = '
                    f'"other"."id" WHERE {rendered} = %s GROUP BY {rendered} '
                    f"HAVING MAX({rendered}) > %s ORDER BY {rendered} ASC",
                )
                self.assertEqual(params, (1, 2))

    def test_column_binds_before_each_operand(self) -> None:
        sub = self._sub()
        inner = '(SELECT "n" FROM "things" WHERE "k" = %s)'
        sql, params = (
            Thing.query()
            .whereBetween(sub, 1, 2)  # type: ignore[arg-type]
            .whereIn(sub, [3])  # type: ignore[arg-type]
            .where(sub, "LIKE", "p%")  # type: ignore[arg-type]
            .where(sub, "IS", True)  # type: ignore[arg-type]
            .join("other", sub, "=", Thing.query().select("m").where("j", "=", 4))
            .to_sql()
        )
        self.assertEqual(
            sql,
            f'SELECT * FROM "things" JOIN "other" ON {inner} = '
            f'(SELECT "m" FROM "things" WHERE "j" = %s) '
            f"WHERE {inner} BETWEEN %s AND %s AND {inner} IN (%s) "
            f"AND {inner} LIKE %s AND {inner} IS TRUE",
        )
        self.assertEqual(params, ("v", 4, "v", 1, 2, "v", 3, "v", "p%", "v"))
