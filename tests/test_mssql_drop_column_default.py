"""
Tests that a column drop on SQL Server removes the column's default
constraint first, since the engine refuses to drop a column that a
default constraint depends on.
"""

import unittest
from unittest import mock

from sustained import Model
from sustained.autogenerate import autogenerate
from sustained.ddl import add_column
from sustained.dialects import Dialects
from sustained.introspect import IntrospectedColumn, IntrospectedTable, Snapshot
from sustained.schema import Integer

DROP_DEFAULT_MARK = "sys.default_constraints"


def _table(**extra):
    columns = {
        "id": IntrospectedColumn(raw_type="int", nullable=False, primary_key=True)
    }
    columns.update(extra)
    return IntrospectedTable(columns=columns, primary_key=("id",))


def _generate(model, snapshot):
    with mock.patch("sustained.autogenerate.introspect_schema", return_value=snapshot):
        return autogenerate(
            None, [model], id="m", dialect=Dialects.MSSQL, allow_drops=True
        )


class TestMssqlDropColumnDefault(unittest.TestCase):
    def test_extra_column_with_default_drops_the_default_first(self):
        class Post(Model):
            tableName = "posts"
            tableColumns = {"id": Integer(primary_key=True)}

        score = IntrospectedColumn(
            raw_type="int", nullable=False, primary_key=False, default="((0))"
        )
        up = _generate(Post, Snapshot(tables={"posts": _table(score=score)})).up
        self.assertEqual(len(up), 2)
        self.assertIn(DROP_DEFAULT_MARK, up[0])
        self.assertEqual(up[1], "ALTER TABLE [posts] DROP COLUMN [score]")

    def test_extra_column_without_default_drops_only_the_column(self):
        class Post(Model):
            tableName = "posts"
            tableColumns = {"id": Integer(primary_key=True)}

        score = IntrospectedColumn(raw_type="int", nullable=True, primary_key=False)
        self.assertEqual(
            _generate(Post, Snapshot(tables={"posts": _table(score=score)})).up,
            ["ALTER TABLE [posts] DROP COLUMN [score]"],
        )

    def test_added_column_with_default_down_drops_the_default_first(self):
        class Post(Model):
            tableName = "posts"
            tableColumns = {
                "id": Integer(primary_key=True),
                "score": Integer(default=0),
            }

        down = _generate(Post, Snapshot(tables={"posts": _table()})).down
        self.assertEqual(len(down), 2)
        self.assertIn(DROP_DEFAULT_MARK, down[0])
        self.assertEqual(down[1], "ALTER TABLE [posts] DROP COLUMN [score]")

    def test_ddl_add_column_inverse_drops_the_default_first(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        inverse = add_column("posts", "score", Integer(default=0)).inverse()
        rendered = inverse.render(compiler)
        self.assertEqual(len(rendered), 2)
        self.assertIn(DROP_DEFAULT_MARK, rendered[0])
        self.assertIn("DROP COLUMN [score]", rendered[1])


if __name__ == "__main__":
    unittest.main()
