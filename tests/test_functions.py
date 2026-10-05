import unittest

from sustained import Model, create_model
from sustained.dialects import Dialects
from sustained.exceptions import DialectError


class TestFunctionValidation(unittest.TestCase):
    def test_unsupported_function_raises_dialect_error(self):
        class User(Model):
            tableName = "users"

        # Presto and Trino have no STRING_AGG; they spell it LISTAGG or
        # array_join(array_agg(...)).
        User.set_dialect(Dialects.PRESTO)
        query = User.query()

        with self.assertRaisesRegex(
            DialectError,
            "Function 'STRING_AGG' is not supported by the 'PRESTO' dialect.",
        ):
            query.select_func("STRING_AGG", "name")

        # Reset dialect
        User.set_dialect(Dialects.DEFAULT)

    def test_string_agg_on_athena_raises(self):
        Athena = create_model("FuncAthenaUser", "users")
        Athena.set_dialect(Dialects.ATHENA)
        with self.assertRaises(DialectError):
            Athena.query().select_func("STRING_AGG", "name")

    def test_string_agg_renders_on_mssql(self):
        # SQL Server 2017 and later have STRING_AGG(expression, separator).
        from sustained.expressions import Literal

        Ms = create_model("FuncMsUser", "users")
        Ms.set_dialect(Dialects.MSSQL)
        query = Ms.query().select_func("STRING_AGG", "name", Literal(", "))
        self.assertIn("STRING_AGG([name], N', ')", str(query))

    def test_unregistered_function_passes_through(self):
        class User(Model):
            tableName = "users"

        from sustained.expressions import raw

        query = User.query().select_func("my_awesome_func", raw("name"))

        self.assertIn("MY_AWESOME_FUNC(name)", str(query))


User = create_model("FuncSemanticsUser", "users")


class TestFunctionArgumentSemantics(unittest.TestCase):
    def test_string_args_are_columns(self):
        query = User.query().select_func("LOWER", "name", alias="n")
        self.assertEqual(str(query), "SELECT LOWER(name) AS n FROM users")

    def test_string_args_are_quoted_per_dialect(self):
        from sustained.dialects import Dialects

        Pg = create_model("FuncPg", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        query = Pg.query().select_func("LOWER", "users.name", alias="n")
        self.assertEqual(str(query), 'SELECT LOWER("users"."name") AS "n" FROM "users"')

    def test_literal_wrapper_renders_literal(self):
        from sustained.expressions import Literal

        query = User.query().select_func("COALESCE", "nickname", Literal("N/A"))
        self.assertEqual(str(query), "SELECT COALESCE(nickname, 'N/A') FROM users")

    def test_non_identifier_string_rejected(self):
        query = User.query().select_func("LOWER", "not a column!")
        with self.assertRaises(ValueError):
            str(query)

    def test_numeric_args_render_as_literals(self):
        query = User.query().select_func("ROUND", "price", 2)
        self.assertEqual(str(query), "SELECT ROUND(price, 2) FROM users")


class TestFunctionHelperArguments(unittest.TestCase):
    """The function helpers take their arguments as builder.pyi declares."""

    def _sql(self, method, *args, **kwargs):
        query = create_model("FuncArgThing", "t").query()
        return str(getattr(query, method)(*args, **kwargs))

    def test_positional_alias_after_the_fixed_arguments(self):
        cases = (
            ("lower", ("name", "l"), "LOWER(name) AS l"),
            ("UPPER", ("name", "u"), "UPPER(name) AS u"),
            ("trim", ("name", "t"), "TRIM(name) AS t"),
            ("length", ("name", "n"), "LENGTH(name) AS n"),
            ("abs", ("x", "a"), "ABS(x) AS a"),
            ("ceiling", ("x", "c"), "CEILING(x) AS c"),
            ("floor", ("x", "f"), "FLOOR(x) AS f"),
            ("round", ("x", 2, "r"), "ROUND(x, 2) AS r"),
            ("mod", ("x", 3, "m"), "MOD(x, 3) AS m"),
            ("substring", ("n", 1, 3, "s"), "SUBSTRING(n, 1, 3) AS s"),
            ("substring", ("n", 1, None, "s"), "SUBSTRING(n, 1) AS s"),
        )
        for method, args, select in cases:
            with self.subTest(method=method, args=args):
                self.assertEqual(self._sql(method, *args), f"SELECT {select} FROM t")

    def test_fixed_arguments_without_alias_keep_their_output(self):
        self.assertEqual(self._sql("round", "x"), "SELECT ROUND(x) FROM t")
        self.assertEqual(
            self._sql("substring", "n", 1, 3), "SELECT SUBSTRING(n, 1, 3) FROM t"
        )
        self.assertEqual(
            self._sql("lower", "name", alias="l"), "SELECT LOWER(name) AS l FROM t"
        )

    def test_too_many_arguments_raise(self):
        with self.assertRaisesRegex(TypeError, "lower\\(\\) takes"):
            self._sql("lower", "name", "l", "extra")

    def test_alias_given_twice_raises(self):
        with self.assertRaisesRegex(TypeError, "alias"):
            self._sql("lower", "name", "l", alias="m")

    def test_other_functions_take_every_positional_argument(self):
        self.assertEqual(
            self._sql("coalesce", "a", "b", alias="c"),
            "SELECT COALESCE(a, b) AS c FROM t",
        )
