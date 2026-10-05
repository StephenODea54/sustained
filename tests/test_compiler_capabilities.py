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


if __name__ == "__main__":
    unittest.main()
