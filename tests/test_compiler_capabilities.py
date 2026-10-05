import unittest

from sustained.compilers.base import Compiler
from sustained.compilers.postgres import PostgresCompiler
from sustained.dialects import Dialects


class TestCompilerCapabilities(unittest.TestCase):
    def test_method_override_still_answers(self):
        # A subclass that overrides the method, not the attribute, keeps
        # its answer, and escapes_percent follows its placeholder.
        class PyformatCompiler(Compiler):
            def placeholder(self) -> str:
                return "%s"

            def supports_qualify(self) -> bool:
                return True

        compiler = PyformatCompiler(Dialects.DEFAULT)
        self.assertTrue(compiler.escapes_percent())
        self.assertTrue(compiler.supports_qualify())

    def test_attribute_sets_the_answer(self):
        class QuotingCompiler(Compiler):
            _IDENT_QUOTES = ("[", "]")
            _supports_qualify = True

        compiler = QuotingCompiler(Dialects.DEFAULT)
        self.assertEqual(compiler.quote_identifier("a]b"), "[a]]b]")
        self.assertTrue(compiler.supports_qualify())
        self.assertFalse(compiler.escapes_percent())

    def test_postgres_reads_percent_as_placeholder(self):
        compiler = PostgresCompiler(Dialects.POSTGRES)
        self.assertEqual(compiler.placeholder(), "%s")
        self.assertTrue(compiler.escapes_percent())


class TestUnsupportedMessages(unittest.TestCase):
    def test_one_wording_with_and_without_hint(self):
        from sustained.exceptions import DialectError

        mysql = Dialects.get_compiler(Dialects.MYSQL)
        with self.assertRaises(DialectError) as caught:
            mysql.compile_grouping_sets("a")
        self.assertEqual(
            str(caught.exception),
            "The MySQL dialect does not support GROUPING SETS.",
        )
        mssql = Dialects.get_compiler(Dialects.MSSQL)
        with self.assertRaises(DialectError) as caught:
            mssql.compile_explain(False)
        self.assertEqual(
            str(caught.exception),
            "The SQL Server dialect does not support EXPLAIN. "
            "Use SET SHOWPLAN_XML via raw SQL.",
        )

    def test_every_dialect_has_a_product_name(self):
        expected = {
            Dialects.ATHENA: "Athena",
            Dialects.PRESTO: "Presto",
            Dialects.MSSQL: "SQL Server",
            Dialects.POSTGRES: "PostgreSQL",
            Dialects.MYSQL: "MySQL",
            Dialects.DUCKDB: "DuckDB",
            Dialects.DEFAULT: "default",
        }
        for dialect, name in expected.items():
            with self.subTest(dialect=dialect.name):
                compiler = Dialects.get_compiler(dialect)
                self.assertEqual(compiler.display_name(), name)


if __name__ == "__main__":
    unittest.main()
