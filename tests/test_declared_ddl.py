"""
Tests that the hand-written ddl steps and autogenerate render a declared
index and a declared column in full: each key part's direction, the
partial-index predicate, and the column comment.
"""

import unittest

import sustained.autogenerate as autogenerate_module
from sustained import Model
from sustained.autogenerate import autogenerate
from sustained.ddl import create_index, create_table
from sustained.dialects import Dialects
from sustained.introspect import IntrospectedColumn, IntrospectedTable, Snapshot
from sustained.migrations import Migration, migration_checksum
from sustained.schema import Index, IndexColumn, Integer, String

POSTGRES = Dialects.get_compiler(Dialects.POSTGRES)
PARTIAL = Index("ix_a", IndexColumn("a", desc=True), where="a > 0")
PARTIAL_SQL = 'CREATE INDEX "ix_a" ON "t" ("a" DESC) WHERE a > 0'


def make_model(name, columns, indexes=()):
    model = type(
        name,
        (Model,),
        {"tableName": "t", "tableColumns": columns, "indexes": list(indexes)},
    )
    model.set_dialect(Dialects.POSTGRES)
    return model


class GeneratedCase(unittest.TestCase):
    def stub_snapshot(self, columns):
        live = {
            name: IntrospectedColumn(
                POSTGRES.compile_column_type(coldef), coldef.nullable, False
            )
            for name, coldef in columns.items()
        }
        snapshot = Snapshot({"t": IntrospectedTable(columns=live)})
        original = autogenerate_module.introspect_schema
        autogenerate_module.introspect_schema = (
            lambda connection, dialect, schemas=(): snapshot
        )
        self.addCleanup(setattr, autogenerate_module, "introspect_schema", original)

    def generate(self, model):
        return autogenerate(None, [model], id="g1", dialect=Dialects.POSTGRES)


class TestIndexDetails(GeneratedCase):
    def test_create_index_step(self):
        self.assertEqual(create_index("t", PARTIAL).render(POSTGRES), [PARTIAL_SQL])

    def test_create_table_step(self):
        step = create_table("t", {"a": Integer()}, indexes=[PARTIAL])
        self.assertEqual(step.render(POSTGRES)[-1], PARTIAL_SQL)

    def test_autogenerate_new_index(self):
        columns = {"a": Integer()}
        self.stub_snapshot(columns)
        migration = self.generate(make_model("PartialNew", columns, [PARTIAL]))
        self.assertEqual(migration.up, [PARTIAL_SQL])

    def test_autogenerate_new_table(self):
        model = make_model("PartialTable", {"a": Integer()}, [PARTIAL])
        snapshot = Snapshot({})
        original = autogenerate_module.introspect_schema
        autogenerate_module.introspect_schema = lambda c, d, schemas=(): snapshot
        self.addCleanup(setattr, autogenerate_module, "introspect_schema", original)
        self.assertEqual(self.generate(model).up[-1], PARTIAL_SQL)

    def test_plain_index_checksum_is_unchanged(self):
        # The checksum of a migration applied before an index could carry
        # key-part details. A change here breaks every such migration.
        migration = Migration("m1", up=[create_index("t", Index("ix_b", "b"))])
        self.assertEqual(
            migration_checksum(migration),
            "e68e2070c7ecbd40e76d3bfb2a25b8faeb33daf211b55b44881bccbf5e0319c9",
        )

    def test_blank_predicate_matches_a_plain_index(self):
        plain = Migration("m1", up=[create_index("t", Index("ix_b", "b"))])
        for where in ("", "  \n "):
            with self.subTest(where=where):
                step = create_index("t", Index("ix_b", "b", where=where))
                self.assertEqual(
                    step.render(POSTGRES), ['CREATE INDEX "ix_b" ON "t" ("b")']
                )
                blank = Migration("m1", up=[step])
                self.assertEqual(migration_checksum(blank), migration_checksum(plain))

    def test_details_change_the_checksum(self):
        plain = Migration("m1", up=[create_index("t", Index("ix_a", "a"))])
        desc = Migration("m1", up=[create_index("t", PARTIAL)])
        self.assertNotEqual(migration_checksum(plain), migration_checksum(desc))


class TestAddedColumnComment(GeneratedCase):
    def test_autogenerate_comments_a_new_column(self):
        self.stub_snapshot({"id": Integer()})
        model = make_model(
            "CommentNew", {"id": Integer(), "note": String(20, comment="Hello")}
        )
        self.assertEqual(
            self.generate(model).up,
            [
                'ALTER TABLE "t" ADD COLUMN "note" VARCHAR(20)',
                'COMMENT ON COLUMN "t"."note" IS \'Hello\'',
            ],
        )

    def test_autogenerate_comments_a_new_not_null_column(self):
        self.stub_snapshot({"id": Integer()})
        model = make_model(
            "CommentNotNull",
            {
                "id": Integer(),
                "note": String(20, nullable=False, default="x", comment="Hello"),
            },
        )
        up = self.generate(model).up
        self.assertIn('COMMENT ON COLUMN "t"."note" IS \'Hello\'', up)


if __name__ == "__main__":
    unittest.main()
