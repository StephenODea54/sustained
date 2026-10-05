"""
Identifier quoting rules that hold for every dialect.

A quoted identifier must contain its own delimiter safely, and an alias
must be a plain name because the default dialect writes identifiers bare.
A column string can come from a request, so every name in it is quoted, and
the default dialect, which writes names bare, refuses one that is not plain.
"""

import unittest

from sustained import Model, QueryBuilder, RelationType
from sustained.dialects import Dialects
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    ColumnExpr,
    Func,
    Subquery,
    WindowExpression,
)


class TestDelimiterDoubling(unittest.TestCase):
    def test_double_quote_dialects_double_the_quote(self):
        for dialect in (Dialects.POSTGRES, Dialects.DUCKDB, Dialects.PRESTO):
            with self.subTest(dialect=dialect.name):
                compiler = Dialects.get_compiler(dialect)
                self.assertEqual(compiler.quote_identifier('a"b'), '"a""b"')

    def test_mysql_doubles_the_backtick(self):
        compiler = Dialects.get_compiler(Dialects.MYSQL)
        self.assertEqual(compiler.quote_identifier("a`b"), "`a``b`")

    def test_mssql_doubles_the_closing_bracket(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        self.assertEqual(compiler.quote_identifier("a]b"), "[a]]b]")

    def test_mssql_qualified_name_doubles_each_part(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        self.assertEqual(
            compiler.quote_fully_qualified_identifier("dbo.a]b"),
            "[dbo].[a]]b]",
        )

    def test_athena_ddl_doubles_the_backtick(self):
        compiler = Dialects.get_compiler(Dialects.ATHENA)
        self.assertEqual(compiler.quote_ddl_identifier("a`b"), "`a``b`")

    def test_athena_queries_keep_double_quotes(self):
        compiler = Dialects.get_compiler(Dialects.ATHENA)
        self.assertEqual(compiler.quote_identifier('a"b'), '"a""b"')


class TestAliasValidation(unittest.TestCase):
    """An alias that is not a plain name is refused on every dialect."""

    def test_func_alias_with_quote_is_refused(self):
        for dialect in Dialects:
            with self.subTest(dialect=dialect.name):
                compiler = Dialects.get_compiler(dialect)
                with self.assertRaises(ValueError):
                    compiler.compile_function(Func("count", "*", alias='x") AS evil--'))

    def test_aggregate_alias_with_quote_is_refused(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        with self.assertRaises(ValueError):
            compiler.compile_aggregate(AggregateExpression("COUNT", "id", alias="a b"))

    def test_window_alias_with_quote_is_refused(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        with self.assertRaises(ValueError):
            compiler.compile_window(WindowExpression("ROW_NUMBER", "a-b"))

    def test_case_alias_with_quote_is_refused(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        case = CaseExpression("a;b", "no").when("1 = 1", "yes")
        with self.assertRaises(ValueError):
            compiler.compile_case(case)

    def test_plain_alias_is_quoted(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(compiler.quote_alias("total_count"), '"total_count"')

    def test_dotted_alias_is_refused(self):
        for dialect in Dialects:
            with self.subTest(dialect=dialect.name):
                with self.assertRaisesRegex(ValueError, "not a plain identifier"):
                    Dialects.get_compiler(dialect).quote_alias("a.b")

    def test_error_names_the_alias(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        with self.assertRaises(ValueError) as caught:
            compiler.quote_alias("a b")
        self.assertIn("'a b'", str(caught.exception))


class TestQuotedNamesInStatements(unittest.TestCase):
    def test_table_name_with_a_quote_stays_inside_the_quotes(self):
        class Odd(Model):
            tableName = 'we"ird'

        Odd.set_dialect(Dialects.POSTGRES)
        try:
            self.assertEqual(str(Odd.query()), 'SELECT * FROM "we""ird"')
        finally:
            Odd.set_dialect(Dialects.DEFAULT)


class Item(Model):
    tableName = "items"


INJECTIONS = (
    "name UNION SELECT s FROM secrets --",
    "id = 0 OR 1",
    "id; DROP TABLE items",
    "COUNT(id) OR 1",
    "LOWER(name, 1)",
    "items.* , 1",
    "",
)


class TestColumnStrings(unittest.TestCase):
    """Every clause that takes a column string keeps SQL in it out of the query."""

    def tearDown(self):
        Item.set_dialect(Dialects.DEFAULT)

    def clauses(self, text):
        yield "select", lambda: Item.query().select(text)
        yield "where", lambda: Item.query().where(text, "=", 1)
        yield "whereIn", lambda: Item.query().whereIn(text, [1])
        yield "orderBy", lambda: Item.query().orderBy(text)
        yield "groupBy", lambda: Item.query().groupBy(text)
        yield "having", lambda: Item.query().groupBy("id").having(text, ">", 1)
        yield "distinctOn", lambda: Item.query().distinctOn(text)
        yield "from_", lambda: Item.query().from_(text)

    def test_default_dialect_refuses_sql_in_a_column_string(self):
        for text in INJECTIONS:
            for clause, build in self.clauses(text):
                with self.subTest(clause=clause, text=text):
                    with self.assertRaises(ValueError):
                        str(build())

    def test_quoting_dialects_render_sql_as_one_quoted_name(self):
        for dialect, quoted in (
            (Dialects.POSTGRES, '"id; DROP TABLE items"'),
            (Dialects.MYSQL, "`id; DROP TABLE items`"),
            (Dialects.MSSQL, "[id; DROP TABLE items]"),
        ):
            Item.set_dialect(dialect)
            with self.subTest(dialect=dialect.name):
                sql = Item.query().where("id; DROP TABLE items", "=", 1).to_sql()[0]
                self.assertIn(f"WHERE {quoted} = ", sql)

    def test_hostile_strings_render_as_quoted_names(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        cases = {
            "name UNION SELECT s FROM secrets --": '"name UNION SELECT s FROM secrets --"',
            "id = 0 OR 1": '"id = 0 OR 1"',
            "a) OR (1=1": '"a) OR (1=1"',
            "COUNT(id) OR 1": '"COUNT(id) OR 1"',
            "LOWER(name, 1)": 'LOWER("name, 1")',
            "items.* , 1": '"items"."* , 1"',
            'a"b': '"a""b"',
            '"a"; DROP TABLE x': '"""a""; DROP TABLE x"',
            '"unclosed': '"""unclosed"',
            "SUM(a) + SUM(b)": 'SUM("a) + SUM(b")',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(compiler.quote_column_reference(text), expected)

    def test_mssql_and_mysql_double_the_closing_quote(self):
        mssql = Dialects.get_compiler(Dialects.MSSQL)
        mysql = Dialects.get_compiler(Dialects.MYSQL)
        self.assertEqual(mssql.quote_column_reference("a]; DROP x"), "[a]]; DROP x]")
        self.assertEqual(mysql.quote_column_reference("a`; DROP x"), "`a``; DROP x`")

    def test_empty_parts_are_refused_on_every_dialect(self):
        for dialect in Dialects:
            compiler = Dialects.get_compiler(dialect)
            for text in ("", "a..b", ".a", "a.", '""', "t.[].x", ".*", "COUNT(a.)"):
                with self.subTest(dialect=dialect.name, text=text):
                    with self.assertRaisesRegex(ValueError, "not a column name"):
                        compiler.quote_column_reference(text)

    def test_error_names_the_string_and_the_raw_path(self):
        Item.set_dialect(Dialects.POSTGRES)
        with self.assertRaises(ValueError) as caught:
            str(Item.query().orderBy("items..id"))
        self.assertIn("'items..id'", str(caught.exception))
        self.assertIn("QueryBuilder.raw()", str(caught.exception))

    def test_default_dialect_error_names_the_part(self):
        with self.assertRaisesRegex(ValueError, "'id DESC, \\(SELECT 1\\)'"):
            str(Item.query().orderBy("id DESC, (SELECT 1)"))

    def test_from_takes_only_a_table_name(self):
        for text in ("items.*", "COUNT(id)", "*"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "not a table name"):
                    Item.query().from_(text)
        Item.set_dialect(Dialects.POSTGRES)
        self.assertEqual(
            str(Item.query().from_("sales.items")), 'SELECT * FROM "sales"."items"'
        )

    def test_column_forms_are_quoted_per_dialect(self):
        Item.set_dialect(Dialects.POSTGRES)
        query = (
            Item.query()
            .select("items.*", "COUNT(*)", "count(DISTINCT items.maker_id) AS n")
            .groupBy("items.id")
            .having("SUM(items.price)", ">", 5)
            .orderBy("MAX(price)", "desc")
        )
        self.assertEqual(
            query.to_sql()[0],
            'SELECT "items".*, COUNT(*), count(DISTINCT "items"."maker_id") '
            'AS "n" FROM "items" GROUP BY "items"."id" '
            'HAVING SUM("items"."price") > %s ORDER BY MAX("price") DESC',
        )

    def test_raw_expressions_render_as_written(self):
        raw = QueryBuilder.raw("LOWER(name)")
        query = Item.query().select(raw).groupBy(raw).orderBy(raw)
        self.assertEqual(
            str(query),
            "SELECT LOWER(name) FROM items GROUP BY LOWER(name) "
            "ORDER BY LOWER(name) ASC",
        )


class Staff(Model):
    tableName = "staff"


class TestColumnNamesThatAreNotPlainWords(unittest.TestCase):
    """A column name with a space or a non-ASCII letter works on read paths."""

    QUOTES = {
        Dialects.POSTGRES: ('"', '"'),
        Dialects.MSSQL: ("[", "]"),
        Dialects.MYSQL: ("`", "`"),
        Dialects.DUCKDB: ('"', '"'),
        Dialects.PRESTO: ('"', '"'),
        Dialects.ATHENA: ('"', '"'),
    }

    def tearDown(self):
        Staff.set_dialect(Dialects.DEFAULT)

    def test_read_paths_quote_a_name_with_a_space(self):
        for dialect, (opening, closing) in self.QUOTES.items():
            Staff.set_dialect(dialect)

            def q(name):
                return f"{opening}{name}{closing}"

            with self.subTest(dialect=dialect.name):
                query = (
                    Staff.query()
                    .select("Employee ID", "staff.Employee ID AS eid")
                    .where("Employee ID", "=", 3)
                    .where(ColumnExpr("Employee ID") > 1)
                    .orderBy("Employee ID", "desc")
                )
                sql = query.to_sql()[0]
                self.assertIn(
                    f"SELECT {q('Employee ID')}, {q('staff')}.{q('Employee ID')} "
                    f"AS {q('eid')} FROM",
                    sql,
                )
                self.assertIn(f"WHERE {q('Employee ID')} = ", sql)
                self.assertIn(f"AND {q('Employee ID')} > ", sql)
                self.assertIn(f"ORDER BY {q('Employee ID')} DESC", sql)
                self.assertIn(
                    f"SUM({q('Employee ID')})",
                    Staff.query().sum("Employee ID").to_sql()[0],
                )
                self.assertIn(
                    f"COUNT({q('Employee ID')})",
                    Staff.query().count("Employee ID").to_sql()[0],
                )

    def test_non_ascii_name(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(compiler.quote_column_reference("prénom"), '"prénom"')
        self.assertEqual(
            compiler.quote_column_reference("staff.prénom"), '"staff"."prénom"'
        )

    def test_quoted_parts_take_the_quotes_of_the_dialect(self):
        cases = {
            'dbo."a.b"': ("dbo", "a.b"),
            "[Employee ID]": ("Employee ID",),
            "`a``b`.c": ("a`b", "c"),
            '"a""b"': ('a"b',),
            "[a]]b]": ("a]b",),
        }
        for dialect, (opening, closing) in self.QUOTES.items():
            compiler = Dialects.get_compiler(dialect)
            for text, names in cases.items():
                with self.subTest(dialect=dialect.name, text=text):
                    expected = ".".join(
                        opening + name.replace(closing, closing * 2) + closing
                        for name in names
                    )
                    self.assertEqual(compiler.quote_column_reference(text), expected)

    def test_calls_and_table_star_take_the_same_parts(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        self.assertEqual(
            compiler.quote_column_reference('SUM("Employee ID")'), "SUM([Employee ID])"
        )
        self.assertEqual(
            compiler.quote_column_reference("COUNT(DISTINCT staff.[Employee ID])"),
            "COUNT(DISTINCT [staff].[Employee ID])",
        )
        self.assertEqual(compiler.quote_column_reference("COUNT(*)"), "COUNT(*)")
        self.assertEqual(
            compiler.quote_column_reference('dbo."Staff List".*'),
            "[dbo].[Staff List].*",
        )

    def test_select_alias_and_names_with_spaces(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(
            compiler.compile_select_item("Employee ID AS eid"), '"Employee ID" AS "eid"'
        )
        self.assertEqual(
            compiler.compile_select_item('"Employee ID" as eid'),
            '"Employee ID" AS "eid"',
        )
        self.assertEqual(compiler.compile_select_item("Employee ID"), '"Employee ID"')
        self.assertEqual(compiler.compile_select_item('"Cost AS Pct"'), '"Cost AS Pct"')
        with self.assertRaisesRegex(ValueError, "not a plain identifier"):
            compiler.compile_select_item("id AS a b")

    def test_default_dialect_refuses_a_name_that_is_not_plain(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        for text in ("Employee ID", "SUM(Employee ID)", "Staff List.*", "prénom"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "DEFAULT dialect"):
                    compiler.quote_column_reference(text)
        self.assertEqual(compiler.quote_column_reference('"id"'), "id")


class TestSubqueryStrings(unittest.TestCase):
    """A string in subquery position is refused; raw() is the SQL path."""

    def clauses(self, text):
        yield "whereIn", lambda: Item.query().whereIn("id", text)
        yield "orWhereNotIn", lambda: Item.query().where("id", "=", 1).orWhereNotIn(
            "id", text
        )
        yield "havingIn", lambda: Item.query().groupBy("id").havingIn("id", text)
        yield "whereExists", lambda: Item.query().whereExists(text)
        yield "whereNotExists", lambda: Item.query().whereNotExists(text)
        yield "havingExists", lambda: Item.query().groupBy("id").havingExists(text)

    def test_a_string_is_refused_and_the_error_names_raw(self):
        for clause, build in self.clauses("0) OR (1=1"):
            with self.subTest(clause=clause):
                with self.assertRaises(ValueError) as caught:
                    build()
                self.assertIn("'0) OR (1=1'", str(caught.exception))
                self.assertIn("QueryBuilder.raw()", str(caught.exception))

    def test_raw_sql_renders_as_written(self):
        raw = QueryBuilder.raw("SELECT maker_id FROM makers")
        self.assertEqual(
            str(Item.query().whereIn("id", raw).orWhereNotExists(raw)),
            "SELECT * FROM items WHERE id IN (SELECT maker_id FROM makers) "
            "OR NOT EXISTS (SELECT maker_id FROM makers)",
        )

    def test_other_types_name_the_accepted_forms(self):
        with self.assertRaisesRegex(ValueError, "QueryBuilder.raw()"):
            Item.query().whereIn("id", 5)
        with self.assertRaisesRegex(ValueError, "QueryBuilder.raw()"):
            Item.query().whereExists(5)


class TestBareIdentifiers(unittest.TestCase):
    """The default dialect writes names bare, so it refuses one with SQL."""

    def test_default_dialect_refuses_a_name_that_is_not_plain(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        for name in ("a b", "a;b", 'a"b', "1a", ""):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "DEFAULT dialect"):
                    compiler.quote_identifier(name)

    def test_join_table_and_on_columns_are_checked(self):
        with self.assertRaises(ValueError):
            str(Item.query().join("makers; DROP TABLE items", "a", "=", "b"))
        with self.assertRaises(ValueError):
            str(Item.query().join("makers", "makers.id", "=", "1 OR 1=1"))

    def test_on_takes_a_raw_expression_as_its_right_side(self):
        query = Item.query().join(
            "makers",
            lambda j: j.on("makers.id", "=", "items.maker_id").andOn(
                "makers.rank", ">", QueryBuilder.raw("10")
            ),
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM items JOIN makers ON makers.id = items.maker_id "
            "AND makers.rank > 10",
        )


class Maker(Model):
    tableName = "makers"


class Tag(Model):
    tableName = "tags"


class Widget(Model):
    tableName = "widgets"
    relationMappings = {
        "maker": {
            "relation": RelationType.BelongsToOneRelation,
            "modelClass": Maker,
            "join": {"from": "widgets.maker_id", "to": "makers.id"},
        },
        "tags": {
            "relation": RelationType.ManyToManyRelation,
            "modelClass": Tag,
            "join": {
                "from": "widgets.id",
                "through": {
                    "from": {"table": "widget_tags", "key": "widget_id"},
                    "to": {"table": "widget_tags", "key": "tag_id"},
                },
                "to": "tags.id",
            },
        },
    }


ALIAS_INJECTION = "n) FROM users; DROP TABLE users; --"


class TestStatementAliases(unittest.TestCase):
    """Subquery, CTE, FROM, and join aliases go through quote_alias()."""

    def tearDown(self):
        Item.set_dialect(Dialects.DEFAULT)
        Widget.set_dialect(Dialects.DEFAULT)

    def aliased(self, alias):
        inner = Item.query().select("id")
        yield "Subquery", lambda: Item.query().select(Subquery(inner, alias))
        yield "with_", lambda: Item.query().with_(alias, inner)
        yield "from_ table", lambda: Item.query().from_("items", alias)
        yield "from_ subquery", lambda: Item.query().from_(inner, alias)
        yield "joinRelated", lambda: Widget.query().joinRelated("maker", alias=alias)
        yield "joinRelated through", lambda: Widget.query().joinRelated(
            "tags", alias=alias
        )
        yield "select AS", lambda: Item.query().select(f"id AS {alias}")

    def test_sql_in_an_alias_is_refused_on_every_dialect(self):
        for dialect in Dialects:
            Item.set_dialect(dialect)
            Widget.set_dialect(dialect)
            for position, build in self.aliased(ALIAS_INJECTION):
                with self.subTest(dialect=dialect.name, position=position):
                    with self.assertRaises(ValueError):
                        str(build())

    def test_plain_aliases_are_quoted_per_dialect(self):
        Item.set_dialect(Dialects.POSTGRES)
        Widget.set_dialect(Dialects.POSTGRES)
        inner = Item.query().select("id")
        rendered = {
            position: str(build()) for position, build in self.aliased("Recent")
        }
        self.assertEqual(
            rendered["Subquery"],
            'SELECT (SELECT "id" FROM "items") AS "Recent" FROM "items"',
        )
        self.assertEqual(rendered["from_ table"], 'SELECT * FROM "items" AS "Recent"')
        self.assertEqual(
            rendered["from_ subquery"],
            'SELECT * FROM (SELECT "id" FROM "items") AS "Recent"',
        )
        self.assertEqual(
            rendered["joinRelated"],
            'SELECT * FROM "widgets" JOIN "makers" AS "Recent" '
            'ON "widgets"."maker_id" = "Recent"."id"',
        )
        self.assertIn('JOIN "tags" AS "Recent"', rendered["joinRelated through"])
        self.assertEqual(rendered["select AS"], 'SELECT "id" AS "Recent" FROM "items"')
        cte = Item.query().with_("Recent", inner).from_("Recent")
        self.assertEqual(
            str(cte),
            'WITH "Recent" AS (SELECT "id" FROM "items") SELECT * FROM "Recent"',
        )

    def test_subquery_str_quotes_the_alias(self):
        Item.set_dialect(Dialects.MSSQL)
        sub = Subquery(Item.query().select("id"), "n")
        self.assertEqual(str(sub), "(SELECT [id] FROM [items]) AS [n]")
        with self.assertRaises(ValueError):
            str(Subquery(Item.query(), ALIAS_INJECTION))

    def test_repeated_link_table_alias_is_quoted(self):
        Widget.set_dialect(Dialects.MYSQL)
        query = (
            Widget.query().joinRelated("tags", alias="a").joinRelated("tags", alias="b")
        )
        self.assertIn("JOIN `widget_tags` AS `b_widget_tags`", str(query))


if __name__ == "__main__":
    unittest.main()
