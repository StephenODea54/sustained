"""
Statements that build an index again from the catalog render the key
part directions, prefix lengths, and predicate the read reports.
"""

import unittest

from sustained.autogenerate import autogenerate_migrations
from sustained.dialects import Dialects
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
    with_details,
)
from sustained.rebuild import _undeclared_index_sql
from sustained.schema import BigInteger, Index, IndexColumn, Integer, catalog_index
from tests.test_autogenerate import make_model
from tests.test_autogenerate_online import Rows

PG = Dialects.POSTGRES
INT = IntrospectedColumn("integer", True, False)


def detailed(name, *columns, where=None, descending=None, unique=False):
    plain = IntrospectedIndex(tuple(columns), unique, name=name)
    return with_details(plain, where, descending or (False,) * len(columns))


def table(*indexes, columns=None):
    return IntrospectedTable(
        {
            "id": IntrospectedColumn("integer", False, True),
            **(columns or {"a": INT, "b": INT}),
        },
        primary_key=("id",),
        indexes={index.name: index for index in indexes},
        name="t",
    )


def model(dialect=PG, indexes=(), **columns):
    built = make_model(
        "RestoreDetails",
        "t",
        {
            "id": Integer(primary_key=True),
            **(columns or {"a": Integer(), "b": Integer()}),
        },
    )
    built.indexes = list(indexes)
    built.set_dialect(dialect)
    return built


def generate(given, found, dialect=PG, online=False):
    return autogenerate_migrations(
        Rows(),
        [given],
        id="m1",
        dialect=dialect,
        snapshot=Snapshot({"t": found}, constraints_read=True, checks_read=True),
        online=online,
        allow_drops=True,
    )


def down(migrations):
    return [str(s) for migration in migrations for s in migration.down or []]


DESC_PARTIAL = 'CREATE INDEX "ix_a" ON "t" ("a" DESC) WHERE (a > 0)'
ONLINE = [
    'DROP INDEX CONCURRENTLY IF EXISTS "ix_a"',
    'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_a" ON "t" ("a" DESC) WHERE (a > 0)',
]


class CatalogIndexTestCase(unittest.TestCase):
    def test_details_become_key_parts(self):
        found = with_details(IntrospectedIndex(("a",), True), None, [True], [10])
        index = catalog_index("ix_a", ["A"], found)
        self.assertEqual(index.key_parts, (IndexColumn("A", True, 10),))
        self.assertTrue(index.unique)
        self.assertIsNone(index.where)

    def test_a_read_without_details_gives_plain_parts(self):
        index = catalog_index("ix_a", ["a"], IntrospectedIndex(("a",), False))
        self.assertEqual(index.key_parts, (IndexColumn("a"),))
        self.assertIsNone(index.where)


class RestoreTestCase(unittest.TestCase):
    def test_a_changed_index_comes_back_with_its_details(self):
        found = table(detailed("ix_a", "a", where="(a > 0)", descending=(True,)))
        given = model(indexes=[Index("ix_a", "a", "b")])
        self.assertEqual(
            down(generate(given, found)), ['DROP INDEX "ix_a"', DESC_PARTIAL]
        )

    def test_a_changed_index_comes_back_online_with_its_details(self):
        found = table(detailed("ix_a", "a", where="(a > 0)", descending=(True,)))
        given = model(indexes=[Index("ix_a", "a", "b")])
        self.assertEqual(down(generate(given, found, online=True)), ONLINE)

    def test_a_dropped_index_comes_back_with_its_details(self):
        found = table(detailed("ix_a", "a", where="(a > 0)", descending=(True,)))
        self.assertEqual(down(generate(model(), found)), [DESC_PARTIAL])

    def test_a_dropped_index_comes_back_online_with_its_details(self):
        found = table(detailed("ix_a", "a", where="(a > 0)", descending=(True,)))
        self.assertEqual(down(generate(model(), found, online=True)), ONLINE)

    def test_a_lifted_index_comes_back_with_its_details(self):
        found = table(
            detailed("ix_a", "a", where="([a]>(0))", descending=(True,)),
            columns={"a": IntrospectedColumn("int", True, False)},
        )
        given = model(
            Dialects.MSSQL,
            [Index("ix_a", IndexColumn("a", desc=True), where="a > 0")],
            a=BigInteger(),
        )
        (migration,) = generate(given, found, Dialects.MSSQL)
        create = "CREATE INDEX [ix_a] ON [t] ([a] DESC) WHERE ([a]>(0))"
        self.assertEqual(str(migration.up[-1]), create)
        self.assertEqual(str(migration.down[-1]), create)
        self.assertEqual(migration.up[-1].intent.kind, "create_index")

    def test_a_rebuild_keeps_the_details_of_an_undeclared_index(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        found = table(detailed("ix_a", "a", where="a > 0", descending=(True,)))
        self.assertEqual(
            _undeclared_index_sql(compiler, '"t"', model(Dialects.DEFAULT), found),
            ['CREATE INDEX "ix_a" ON "t" ("a" DESC) WHERE a > 0'],
        )


if __name__ == "__main__":
    unittest.main()
