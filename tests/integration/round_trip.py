"""
The schema read round trip. A test builds a table with indexes on a real
server, reads it, drops it, builds it again from the read alone, and
reads it again. The two reads must match.

diff_snapshots() compares an element kind on a dialect only when this
round trip gives zero difference on that dialect's server. A rehearsal
restores an index from the catalog read in the same way, so a read that
does not come back as it was would report a difference the down steps
did not cause.
"""

import unittest

from sustained.dialects import Dialects
from sustained.introspect import introspect_schema
from sustained.introspect.compare import index_parts
from sustained.schema import catalog_index, create_index_sql

from . import harness

TABLE = "it_round_trip"

# The table every server builds. Each dialect adds the indexes it can
# declare.
COLUMNS = (
    ("id", "INTEGER NOT NULL PRIMARY KEY"),
    ("a", "INTEGER"),
    ("b", "VARCHAR(40)"),
    ("c", "INTEGER"),
)


def _indexes(dialect):
    """
    The CREATE INDEX statements for one dialect, after the table name:
    name, key parts, and an optional predicate.
    """
    common = [
        ("ix_rt_plain", "(a)", None),
        ("ix_rt_desc", "(a DESC, b)", None),
        ("ux_rt_b", "(b, c DESC)", None),
    ]
    if dialect in (Dialects.POSTGRES, Dialects.MSSQL, Dialects.DEFAULT):
        common.append(("ix_rt_part", "(c)", "c > 0 AND a IS NOT NULL"))
    if dialect == Dialects.MYSQL:
        common.append(("ix_rt_prefix", "(b(10), a DESC)", None))
    return common


class RoundTripCase(unittest.TestCase):
    """
    Base for one server. Subclasses set NAME to a row in support.json,
    DIALECT to the dialect that row names, and EXPECTED to what the read
    reports for each index: its direction flags, prefix lengths, and
    whether it has a predicate.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT

    @classmethod
    def setUpClass(cls):
        if not cls.NAME:
            raise unittest.SkipTest("base class")
        cls.connection = harness.connect(cls.NAME)
        cls.compiler = Dialects.get_compiler(cls.DIALECT)

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()

    def setUp(self):
        self.drop()

    def tearDown(self):
        self.drop()

    def execute(self, sql):
        self.connection.cursor().execute(sql)
        if hasattr(self.connection, "commit"):
            self.connection.commit()

    def drop(self):
        self.execute(f"DROP TABLE IF EXISTS {self.compiler.quote_identifier(TABLE)}")

    def read(self):
        return introspect_schema(self.connection, self.DIALECT)[TABLE]

    def build(self):
        quote = self.compiler.quote_identifier
        body = ", ".join(f"{quote(name)} {spec}" for name, spec in COLUMNS)
        self.execute(f"CREATE TABLE {quote(TABLE)} ({body})")
        for name, parts, where in _indexes(self.DIALECT):
            unique = "UNIQUE " if name.startswith("ux_") else ""
            sql = f"CREATE {unique}INDEX {name} ON {quote(TABLE)} {parts}"
            self.execute(sql + (f" WHERE {where}" if where else ""))

    def rebuild(self, table):
        """Builds the table again from a read of it, columns then indexes."""
        quote = self.compiler.quote_identifier
        columns = []
        for key, column in table.columns.items():
            text = f"{quote(table.spelled_column(key))} {column.raw_type}"
            if not column.nullable:
                text += " NOT NULL"
            if key in table.primary_key:
                text += " PRIMARY KEY"
            columns.append(text)
        self.execute(f"CREATE TABLE {quote(TABLE)} ({', '.join(columns)})")
        for name, index in table.indexes.items():
            if index.constraint:
                continue
            spelled = [table.spelled_column(column) for column in index.columns]
            declared = catalog_index(index.name or name, spelled, index)
            self.execute(create_index_sql(self.compiler, quote(TABLE), declared))

    def plain_indexes(self, table):
        return {
            name: index_parts(index)
            for name, index in table.indexes.items()
            if not index.constraint
        }

    def test_the_read_reports_each_key_part(self):
        self.build()
        table = self.read()
        found = {
            name: (index.descending, index.prefix_lengths, index.where is not None)
            for name, index in table.indexes.items()
            if not index.constraint
        }
        self.assertEqual(found, self.EXPECTED)
        self.assertTrue(all(index.details for index in table.indexes.values()))

    def test_indexes_come_back_as_they_were_read(self):
        self.build()
        first = self.read()
        self.drop()
        self.rebuild(first)
        second = self.read()
        self.assertEqual(set(self.plain_indexes(first)), set(self.EXPECTED))
        self.assertEqual(self.plain_indexes(second), self.plain_indexes(first))
