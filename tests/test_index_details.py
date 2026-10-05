import unittest

from sustained import DialectError
from sustained.autogenerate.diff import index_details_differ
from sustained.dialects import Dialects
from sustained.introspect.model import IntrospectedIndex
from sustained.schema import Index, IndexColumn


class TestIndexModel(unittest.TestCase):
    def test_plain_strings_keep_working(self):
        index = Index("ix_a", "a", "b", unique=True)
        self.assertEqual(index.columns, ("a", "b"))
        self.assertEqual(index.key_parts, (IndexColumn("a"), IndexColumn("b")))
        self.assertIsNone(index.where)

    def test_index_column_parts(self):
        index = Index("ix_a", IndexColumn("a", desc=True), "b", where="a > 0")
        self.assertEqual(index.columns, ("a", "b"))
        self.assertTrue(index.key_parts[0].desc)
        self.assertEqual(index.where, "a > 0")

    def test_prefix_length_must_be_positive(self):
        with self.assertRaises(ValueError):
            IndexColumn("a", prefix_length=0)


class TestIndexCompiler(unittest.TestCase):
    def test_postgres_partial_desc(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        sql = compiler.compile_create_index(
            "ix_a", '"t"', [IndexColumn("a", desc=True), "b"], False, "b IS NOT NULL"
        )
        self.assertEqual(
            sql, 'CREATE INDEX "ix_a" ON "t" ("a" DESC, "b") WHERE b IS NOT NULL'
        )

    def test_sqlite_partial(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        sql = compiler.compile_create_index("ix_a", "t", ["a"], True, "a > 0")
        self.assertEqual(sql, 'CREATE UNIQUE INDEX "ix_a" ON t ("a") WHERE a > 0')

    def test_mssql_filtered(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        sql = compiler.compile_create_index("ix_a", "[t]", ["a"], False, "a > 0")
        self.assertEqual(sql, "CREATE INDEX [ix_a] ON [t] ([a]) WHERE a > 0")

    def test_mysql_prefix(self):
        compiler = Dialects.get_compiler(Dialects.MYSQL)
        sql = compiler.compile_create_index(
            "ix_a", "`t`", [IndexColumn("a", prefix_length=10, desc=True)], False
        )
        self.assertEqual(sql, "CREATE INDEX `ix_a` ON `t` (`a`(10) DESC)")

    def test_mysql_refuses_partial(self):
        compiler = Dialects.get_compiler(Dialects.MYSQL)
        with self.assertRaises(DialectError):
            compiler.compile_create_index("ix_a", "`t`", ["a"], False, "a > 0")

    def test_postgres_refuses_prefix(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        with self.assertRaises(DialectError):
            compiler.compile_create_index(
                "ix_a", '"t"', [IndexColumn("a", prefix_length=4)], False
            )


class TestIndexDetailsDiff(unittest.TestCase):
    def test_unread_details_compare_equal(self):
        index = Index("ix_a", IndexColumn("a", desc=True), where="a > 0")
        actual = IntrospectedIndex(("a",), False)
        self.assertFalse(index_details_differ(index, actual))

    def test_matching_details(self):
        index = Index("ix_a", IndexColumn("a", desc=True), where="a > 0")
        actual = IntrospectedIndex(
            ("a",),
            False,
            where='("a" > 0)',
            descending=(True,),
            prefix_lengths=(None,),
            details=True,
        )
        self.assertFalse(index_details_differ(index, actual))

    def test_predicate_difference_is_drift(self):
        index = Index("ix_a", "a", where="a > 0")
        actual = IntrospectedIndex(
            ("a",), False, descending=(False,), prefix_lengths=(None,), details=True
        )
        self.assertTrue(index_details_differ(index, actual))

    def test_grouped_predicate_matches(self):
        index = Index("ix_a", "a", where="(a > 0) AND (b > 0)")
        actual = IntrospectedIndex(
            ("a",),
            False,
            where='(("a" > 0) AND ("b" > 0))',
            descending=(False,),
            prefix_lengths=(None,),
            details=True,
        )
        self.assertFalse(index_details_differ(index, actual))

    def test_engine_spelling_of_predicate_matches(self):
        # SQL Server reports the filter a > 0 as ([a]>(0)).
        index = Index("ix_a", "a", where="a > 0")
        actual = IntrospectedIndex(
            ("a",),
            False,
            where="([a]>(0))",
            descending=(False,),
            prefix_lengths=(None,),
            details=True,
        )
        self.assertFalse(index_details_differ(index, actual))

    def test_postgres_casts_in_predicate_match(self):
        # Postgres stores status = 'active' on a varchar column as
        # ((status)::text = 'active'::text).
        index = Index("ix_a", "a", where="(status = 'active') AND (a > 0)")
        actual = IntrospectedIndex(
            ("a",),
            False,
            where="(((status)::text = 'active'::text) AND (a > 0))",
            descending=(False,),
            prefix_lengths=(None,),
            details=True,
        )
        self.assertFalse(index_details_differ(index, actual))

    def test_postgres_multiword_cast_in_predicate_matches(self):
        index = Index("ix_a", "a", where="seen < '2020-01-01'")
        actual = IntrospectedIndex(
            ("a",),
            False,
            where="(seen < '2020-01-01 00:00:00'::timestamp(3) without time zone)",
            descending=(False,),
            prefix_lengths=(None,),
            details=True,
        )
        self.assertTrue(index_details_differ(index, actual))
        index = Index("ix_a", "a", where="seen < '2020-01-01 00:00:00'")
        self.assertFalse(index_details_differ(index, actual))

    def test_predicate_cast_inside_literal_is_kept(self):
        index = Index("ix_a", "a", where="note = 'x::text'")
        actual = IntrospectedIndex(
            ("a",),
            False,
            where="(note = 'x'::text)",
            descending=(False,),
            prefix_lengths=(None,),
            details=True,
        )
        self.assertTrue(index_details_differ(index, actual))

    def test_grouped_predicate_difference_is_drift(self):
        index = Index("ix_a", "a", where="(a > 0) AND (b > 0)")
        actual = IntrospectedIndex(
            ("a",),
            False,
            where="(a > 0) AND (b > 1)",
            descending=(False,),
            prefix_lengths=(None,),
            details=True,
        )
        self.assertTrue(index_details_differ(index, actual))

    def test_direction_difference_is_drift(self):
        index = Index("ix_a", "a")
        actual = IntrospectedIndex(
            ("a",), False, descending=(True,), prefix_lengths=(None,), details=True
        )
        self.assertTrue(index_details_differ(index, actual))

    def test_prefix_difference_is_drift(self):
        index = Index("ix_a", IndexColumn("a", prefix_length=10))
        actual = IntrospectedIndex(
            ("a",), False, descending=(False,), prefix_lengths=(None,), details=True
        )
        self.assertTrue(index_details_differ(index, actual))


if __name__ == "__main__":
    unittest.main()
