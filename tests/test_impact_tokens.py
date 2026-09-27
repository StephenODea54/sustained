"""Tests for the tokenizer the impact recognizer and the textual scan share."""

import time
import unittest

from sustained.analysis import destructive_statements, scannable_forms
from sustained.dialects import Dialects
from sustained.impact.tokens import (
    ERROR,
    IDENT,
    NUMBER,
    OP,
    PARAM,
    PUNCT,
    STRING,
    WORD,
    Token,
    lex,
    scan_readings,
    tokenize,
)


def kinds(sql, dialect=None):
    return [(t.kind, t.value) for t in tokenize(sql, dialect)]


class TokenizeTestCase(unittest.TestCase):
    def test_words_are_upper_cased_values_with_their_spelling_kept(self):
        tokens = tokenize("create Index ix_a")
        self.assertEqual([t.value for t in tokens], ["CREATE", "INDEX", "IX_A"])
        self.assertEqual([t.text for t in tokens], ["create", "Index", "ix_a"])
        self.assertTrue(all(t.kind == WORD for t in tokens))

    def test_offsets_point_at_the_token(self):
        sql = "ALTER  TABLE t"
        for token in tokenize(sql):
            self.assertEqual(
                sql[token.start : token.start + len(token.text)], token.text
            )

    def test_doubled_quote_escapes_a_literal(self):
        self.assertEqual(kinds("'it''s'"), [(STRING, "it's")])

    def test_backslash_is_plain_text_in_a_standard_literal(self):
        self.assertEqual(
            kinds(r"'a\' , 'b'"), [(STRING, "a\\"), (PUNCT, ","), (STRING, "b")]
        )

    def test_mysql_reads_a_backslash_escape(self):
        self.assertEqual(kinds(r"'it\'s'", Dialects.MYSQL), [(STRING, "it's")])

    def test_mysql_reads_double_quotes_as_a_string(self):
        self.assertEqual(kinds('"a\\"b"', Dialects.MYSQL), [(STRING, 'a"b')])

    def test_postgres_escape_string_reads_backslashes(self):
        self.assertEqual(kinds(r"E'a\'b'", Dialects.POSTGRES), [(STRING, "a'b")])

    def test_prefixed_literals(self):
        self.assertEqual(
            kinds("N'x' X'ff' U&'y'", Dialects.MSSQL),
            [(STRING, "x"), (STRING, "ff"), (STRING, "y")],
        )

    def test_a_prefix_glued_to_a_word_is_part_of_the_word(self):
        self.assertEqual(kinds("men'x'"), [(WORD, "MEN"), (STRING, "x")])

    def test_quoted_identifiers(self):
        self.assertEqual(
            kinds('"Order" "a""b" `c`'),
            [(IDENT, "Order"), (IDENT, 'a"b'), (IDENT, "c")],
        )

    def test_bracket_identifiers_on_mssql_and_sqlite(self):
        self.assertEqual(kinds("[a]]b]", Dialects.MSSQL), [(IDENT, "a]b")])
        self.assertEqual(kinds("[users]"), [(IDENT, "users")])
        self.assertEqual(kinds("[users]", Dialects.DEFAULT), [(IDENT, "users")])

    def test_brackets_are_punctuation_on_postgres(self):
        self.assertEqual(
            kinds("a[1]", Dialects.POSTGRES),
            [(WORD, "A"), (PUNCT, "["), (NUMBER, "1"), (PUNCT, "]")],
        )

    def test_comments_are_left_out(self):
        self.assertEqual(
            kinds("a -- DROP TABLE x\n/* DROP */ b"), [(WORD, "A"), (WORD, "B")]
        )

    def test_a_comment_marker_inside_a_literal_is_text(self):
        self.assertEqual(kinds("'-- x /* y */'"), [(STRING, "-- x /* y */")])

    def test_dollar_quotes(self):
        self.assertEqual(
            kinds("$$a;'b$$ $fn$ $$ inner $$ $fn$", Dialects.POSTGRES),
            [(STRING, "a;'b"), (STRING, " $$ inner $$ ")],
        )

    def test_dollar_parameters_and_words_are_not_quotes(self):
        self.assertEqual(
            kinds("$1 a$b$c", Dialects.POSTGRES), [(PARAM, "$1"), (WORD, "A$B$C")]
        )

    def test_other_parameters(self):
        self.assertEqual(
            kinds("? %s %(n)s :name"),
            [(PARAM, "?"), (PARAM, "%s"), (PARAM, "%(n)s"), (PARAM, ":name")],
        )

    def test_casts_and_operators(self):
        self.assertEqual(
            kinds("a::int <> 1.5e3", Dialects.POSTGRES),
            [(WORD, "A"), (OP, "::"), (WORD, "INT"), (OP, "<>"), (NUMBER, "1.5e3")],
        )

    def test_unterminated_input_ends_in_one_error_token(self):
        for sql, dialect in [
            ("SELECT 'open", None),
            ('SELECT "open', None),
            ("SELECT /* open", None),
            ("SELECT $$ open", Dialects.POSTGRES),
            ("SELECT [open", Dialects.MSSQL),
        ]:
            with self.subTest(sql=sql):
                tokens = tokenize(sql, dialect)
                self.assertEqual(tokens[0], Token(WORD, "SELECT", "SELECT", 0))
                self.assertEqual(tokens[-1].kind, ERROR)
                self.assertEqual(tokens[-1].text, sql[7:])

    def test_a_character_no_rule_reads_is_an_error(self):
        self.assertEqual(kinds("a \x00 b")[-1], (ERROR, "\x00 b"))

    def test_token_helpers(self):
        word, ident, number = tokenize('ADD "Col" 1')
        self.assertTrue(word.is_word())
        self.assertTrue(word.is_word("ADD", "DROP"))
        self.assertFalse(word.is_word("DROP"))
        self.assertFalse(ident.is_word())
        self.assertEqual(word.name, "ADD")
        self.assertEqual(ident.name, "Col")
        self.assertIsNone(number.name)


class LexTestCase(unittest.TestCase):
    def test_the_scan_reading_takes_dollar_quotes_with_and_without_a_tag(self):
        rules = scan_readings(None, False)[0]
        found = [t.text for t in lex("$$ a $$ x $t$ b $t$ a$b$c", rules=rules)]
        self.assertEqual(found, ["$$ a $$", " ", "x", " ", "$t$ b $t$", " ", "a$b$c"])


class DialectCommentTestCase(unittest.TestCase):
    """Comment and whitespace rules, as each server reads them."""

    def words(self, sql, dialect):
        return [t.text for t in tokenize(sql, dialect)]

    def test_a_carriage_return_ends_a_line_comment_where_the_server_ends_it(self):
        sql = "a -- x\rb"
        for dialect in (
            Dialects.POSTGRES,
            Dialects.MSSQL,
            Dialects.DUCKDB,
            Dialects.ATHENA,
        ):
            with self.subTest(dialect):
                self.assertEqual(self.words(sql, dialect), ["a", "b"])
        for dialect in (Dialects.MYSQL, Dialects.DEFAULT):
            with self.subTest(dialect):
                self.assertEqual(self.words(sql, dialect), ["a"])

    def test_block_comments_nest_on_postgres_sql_server_and_duckdb(self):
        sql = "a /* x /* y */ b */ c"
        for dialect in (Dialects.POSTGRES, Dialects.MSSQL, Dialects.DUCKDB):
            with self.subTest(dialect):
                self.assertEqual(self.words(sql, dialect), ["a", "c"])
        for dialect in (Dialects.MYSQL, Dialects.DEFAULT):
            with self.subTest(dialect):
                self.assertEqual(self.words(sql, dialect)[:2], ["a", "b"])

    def test_a_hash_starts_a_line_comment_on_mysql_only(self):
        self.assertEqual(self.words("a # x ' y\nb", Dialects.MYSQL), ["a", "b"])
        self.assertEqual(
            kinds("a # x", Dialects.POSTGRES),
            [(WORD, "A"), (OP, "#"), (WORD, "X")],
        )

    def test_a_mysql_double_dash_needs_a_space_or_control_character(self):
        self.assertEqual(
            kinds("1--1", Dialects.MYSQL),
            [(NUMBER, "1"), (OP, "-"), (OP, "-"), (NUMBER, "1")],
        )
        for sql in ("a -- x\nb", "a --\tx\nb", "a --\nb"):
            with self.subTest(sql):
                self.assertEqual(self.words(sql, Dialects.MYSQL), ["a", "b"])
        self.assertEqual(self.words("a --", Dialects.MYSQL), ["a"])
        self.assertEqual(self.words("a --x\nb", Dialects.POSTGRES), ["a", "b"])

    def test_an_executable_comment_body_is_read_as_sql_on_mysql(self):
        for sql in ("a /*! b */ c", "a /*!50100 b */ c", "a /*M! b */ c"):
            with self.subTest(sql):
                self.assertEqual(self.words(sql, Dialects.MYSQL), ["a", "b", "c"])
        self.assertEqual(self.words("a /*! b */ c", Dialects.POSTGRES), ["a", "c"])
        texts = [t.text for t in lex("a /*!50100 b */", Dialects.MYSQL)]
        self.assertEqual(texts, ["a", " ", "/*!50100", " ", "b", " ", "*/"])

    def test_an_unclosed_executable_comment_is_an_error_from_its_opener(self):
        tokens = tokenize("a /*! b", Dialects.MYSQL)
        self.assertEqual(tokens[-1].kind, ERROR)
        self.assertEqual(tokens[-1].text, "/*! b")

    def test_a_no_break_space_is_part_of_a_postgres_word_and_space_elsewhere(self):
        self.assertEqual(self.words("x\xa0y", Dialects.POSTGRES), ["x\xa0y"])
        for dialect in (Dialects.MSSQL, Dialects.DUCKDB):
            with self.subTest(dialect):
                self.assertEqual(self.words("x\xa0y", dialect), ["x", "y"])

    def test_words_are_upper_cased_in_ascii_only(self):
        self.assertNotEqual(
            tokenize("l\u0131m\u0131t", Dialects.MYSQL)[0].value, "LIMIT"
        )

    def test_the_lexed_texts_join_to_the_statement(self):
        sql = "a /* x */ 'b' -- c\n\"d\" $$e$$ /*! f */ # g\n1"
        for dialect in (None,) + tuple(Dialects):
            with self.subTest(dialect):
                texts = "".join(t.text for t in lex(sql, dialect))
                self.assertEqual(texts, sql)


class LinearTimeTestCase(unittest.TestCase):
    """Unclosed quotes end the reading in time linear in the text."""

    def test_unclosed_dollar_tags_and_backslash_runs_are_fast(self):
        texts = (
            "".join(f"$a{n}$ " for n in range(20000)),
            "'\\" * 20000,
        )
        for text in texts:
            started = time.perf_counter()
            tokenize(text, Dialects.POSTGRES)
            tokenize(text, Dialects.MYSQL)
            scannable_forms(text)
            destructive_statements([text])
            self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
