import unittest
from typing import Optional

from sustained import DialectError
from sustained.autogenerate.diff import (
    SchemaDiff,
    _diff_indexes,
    index_details_differ,
    index_predicate_differs,
)
from sustained.dialects import Dialects
from sustained.introspect.model import IntrospectedIndex, IntrospectedTable
from sustained.model import Model
from sustained.schema import Index, IndexColumn, Integer


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
        with self.assertRaises(DialectError) as caught:
            compiler.compile_create_index("ix_a", "`t`", ["a"], False, "a > 0")
        self.assertEqual(
            str(caught.exception),
            "The MySQL dialect does not support partial indexes. "
            "Index 'ix_a' declares a WHERE predicate.",
        )

    def test_postgres_refuses_prefix(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        with self.assertRaises(DialectError) as caught:
            compiler.compile_create_index(
                "ix_a", '"t"', [IndexColumn("a", prefix_length=4)], False
            )
        self.assertEqual(
            str(caught.exception),
            "The Postgres dialect does not support index prefix lengths. "
            "Index column 'a' declares one.",
        )

    def test_refuses_desc_without_support(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        compiler.supports_index_desc = False
        with self.assertRaises(DialectError) as caught:
            compiler.compile_index_column(IndexColumn("a", desc=True))
        self.assertEqual(
            str(caught.exception),
            "The Postgres dialect does not support DESC index columns. "
            "Index column 'a' declares one.",
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
        self.assertTrue(index_predicate_differs(index, actual))

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
        self.assertFalse(index_predicate_differs(index, actual))

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
        self.assertFalse(index_predicate_differs(index, actual))

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
        self.assertFalse(index_predicate_differs(index, actual))

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
        self.assertTrue(index_predicate_differs(index, actual))
        index = Index("ix_a", "a", where="seen < '2020-01-01 00:00:00'")
        self.assertFalse(index_predicate_differs(index, actual))

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
        self.assertTrue(index_predicate_differs(index, actual))

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
        self.assertTrue(index_predicate_differs(index, actual))

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


def _indexed_model(index: Index) -> type:
    return type(
        "T",
        (Model,),
        {
            "tableName": "t",
            "tableColumns": {"id": Integer(primary_key=True), "a": Integer()},
            "indexes": [index],
        },
    )


def _diff_one_index(dialect: Dialects, index: Index, live: IntrospectedIndex):
    table = IntrospectedTable(columns={}, indexes={"ix_a": live})
    diff = SchemaDiff()
    _diff_indexes(Dialects.get_compiler(dialect), diff, _indexed_model(index), table)
    return diff


def _live(where: Optional[str], desc: bool = False) -> IntrospectedIndex:
    return IntrospectedIndex(
        ("a",),
        False,
        where=where,
        descending=(desc,),
        prefix_lengths=(None,),
        details=True,
    )


class TestPredicateDriftByEngine(unittest.TestCase):
    def test_rewriting_engines_note_a_predicate_change(self):
        index = Index("ix_a", "a", where="a > 1")
        for dialect in (Dialects.POSTGRES, Dialects.MSSQL):
            with self.subTest(dialect=dialect):
                diff = _diff_one_index(dialect, index, _live("(a > 0)"))
                self.assertEqual(diff.changed_indexes, [])
                self.assertEqual(len(diff.constraint_notes), 1)
                note = diff.constraint_notes[0]
                self.assertIn("ix_a", note)
                self.assertIn("(a > 0)", note)
                self.assertIn("a > 1", note)

    def test_rewriting_engines_note_an_added_predicate(self):
        index = Index("ix_a", "a", where="a > 1")
        diff = _diff_one_index(Dialects.POSTGRES, index, _live(None))
        self.assertEqual(diff.changed_indexes, [])
        self.assertIn("no predicate", diff.constraint_notes[0])
        diff = _diff_one_index(Dialects.POSTGRES, Index("ix_a", "a"), _live("a > 1"))
        self.assertEqual(diff.changed_indexes, [])
        self.assertIn("declares no predicate", diff.constraint_notes[0])

    def test_rewriting_engines_rebuild_a_key_change(self):
        index = Index("ix_a", "a", where="a > 1")
        diff = _diff_one_index(Dialects.POSTGRES, index, _live("(a > 0)", desc=True))
        self.assertEqual(len(diff.changed_indexes), 1)
        self.assertEqual(diff.constraint_notes, [])

    def test_matching_predicate_is_quiet(self):
        index = Index("ix_a", "a", where="a > 0 AND a < 9")
        diff = _diff_one_index(Dialects.POSTGRES, index, _live("((a > 0) AND (a < 9))"))
        self.assertTrue(diff.is_empty())
        self.assertEqual(diff.constraint_notes, [])

    def test_sqlite_rebuilds_on_a_predicate_change(self):
        index = Index("ix_a", "a", where="a > 1")
        diff = _diff_one_index(Dialects.DEFAULT, index, _live("a > 0"))
        self.assertEqual(len(diff.changed_indexes), 1)
        self.assertEqual(diff.constraint_notes, [])

    def test_compiler_flags(self):
        self.assertTrue(
            Dialects.get_compiler(Dialects.POSTGRES).rewrites_index_predicate
        )
        self.assertTrue(Dialects.get_compiler(Dialects.MSSQL).rewrites_index_predicate)
        self.assertFalse(
            Dialects.get_compiler(Dialects.DEFAULT).rewrites_index_predicate
        )


if __name__ == "__main__":
    unittest.main()
