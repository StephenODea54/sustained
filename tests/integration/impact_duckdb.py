"""
The impact analysis on DuckDB: the facts read_context() reads from a
database file, and the ground truth for every rule. Each rule fixture
runs twice on a fresh database file holding the fixture schema:

- inside a transaction, while a second connection to the same database
  reads the table, then inserts, updates, and deletes a row of it, and
  adds a column to it, each in a transaction of its own, which shows
  which of those abort with a conflict and that none of them wait; and,
  on a fresh database for each write, after a second connection's open
  transaction has written a row, which shows whether that transaction
  fails to commit
- on its own, between two checkpoints, so the column segments that
  `pragma_storage_info()` places in blocks the table did not use before
  count the rows the statement wrote, and `pragma_database_size()`
  shows whether the file needs more blocks

A statement the rules say rewrites a column must write every row of
some column again, one they say builds an index must write no column
but take more blocks, and one they say changes only the catalog, or
only reads, must write no column and take no more blocks. A row write
may write any number of rows.
"""

import os
import shutil
import tempfile
import unittest

from sustained.dialects import Dialects
from sustained.impact import Blocks, Work, analyze, read_context
from sustained.impact.rules import profile_for

from . import harness

# The column the second connection's UPDATE sets on each fixture table,
# one the fixtures' own UPDATEs set too.
UPDATED = {"t": "name", "p": "v"}


class DuckdbImpactCase(unittest.TestCase):
    """
    Base for DuckDB's `impact` cover. Subclasses set NAME to the row in
    support.json.
    """

    NAME = ""
    DIALECT = Dialects.DUCKDB

    def setUp(self):
        if not self.NAME:
            self.skipTest("base class")
        self.duckdb = harness.driver(self.NAME)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def database(self, name):
        """A database file holding the fixture schema, checkpointed."""
        connection = self.duckdb.connect(os.path.join(self.dir, f"{name}.db"))
        self.addCleanup(connection.close)
        for sql in profile_for(self.DIALECT).fixture_schema:
            connection.execute(sql)
        connection.execute("FORCE CHECKPOINT")
        return connection

    def test_reads_the_version_and_row_counts(self):
        connection = self.database("context")
        context = read_context(connection, self.DIALECT)
        self.assertLessEqual({"version", "sizes", "schema"}, context.read)
        (version,) = connection.execute(
            "SELECT library_version FROM pragma_version()"
        ).fetchone()
        self.assertEqual(
            ".".join(str(part) for part in context.version),
            version.lstrip("v").split("-")[0],
        )
        self.assertEqual(context.stats("t").rows, 10000)
        self.assertEqual(context.stats("main.t").rows, 10000)
        self.assertIsNone(context.stats("t").bytes)

    def test_each_rule_fixture_does_what_its_rule_predicts(self):
        profile = profile_for(self.DIALECT)
        fixtures = [(rule, f) for rule in profile.rules for f in rule.fixtures]
        for number, (rule, fixture) in enumerate(fixtures):
            with self.subTest(rule=rule.id, fixture=fixture):
                connection = self.database(f"fixture{number}")
                context = read_context(connection, self.DIALECT)
                (predicted,) = analyze([fixture], self.DIALECT, context).statements
                reached = {t.rule for t in predicted.tables}
                reached |= {f.rule for f in predicted.findings}
                self.assertIn(rule.id, reached)
                table, blocks = self.watched(predicted, context)
                found = self.conflicts(connection, fixture, table)
                if self.written_first(f"fixture{number}", fixture, table):
                    found = Blocks.WRITES
                self.assertEqual(found, blocks)
                work = max(t.work for t in predicted.tables)
                fresh = self.database(f"fixture{number}_alone")
                self.check_writes(fresh, fixture, table, work)

    def watched(self, predicted, context):
        """
        The table the second connection works on, the existing table the
        statement blocks most, and what the rules say it blocks there. A
        statement on no existing table is watched on t.
        """
        existing = [t for t in predicted.tables if context.stats(t.table).rows]
        if not existing:
            return "t", Blocks.NOTHING
        worst = max(existing, key=lambda t: t.blocks)
        return worst.table, worst.blocks

    def conflicts(self, connection, fixture, table):
        """
        What the fixture's open transaction blocks on the table, as a
        second connection sees it: `writes` when an INSERT, UPDATE, or
        DELETE aborts with a conflict, `ddl` when only ADD COLUMN does,
        and `nothing` when none does. Its reads must always go on.
        """
        other = connection.cursor()
        column = UPDATED.get(table, "id")
        writes = [
            f"INSERT INTO {table} (id) VALUES (900001)",
            f"UPDATE {table} SET {column} = {column} WHERE id = 1",
            f"DELETE FROM {table} WHERE id = 1",
        ]
        connection.execute("BEGIN")
        try:
            connection.execute(fixture)
            (count,) = other.execute(f"SELECT count(*) FROM {table}").fetchone()
            self.assertGreater(count, 0)
            aborted = [
                self.aborts(other, sql)
                for sql in writes + [f"ALTER TABLE {table} ADD COLUMN zz integer"]
            ]
        finally:
            connection.execute("ROLLBACK")
            other.close()
        if any(aborted[:3]):
            return Blocks.WRITES
        return Blocks.DDL if aborted[3] else Blocks.NOTHING

    def written_first(self, name, fixture, table):
        """
        Whether a second connection's transaction that wrote to the
        table before the fixture ran conflicts with it: the fixture, or
        its commit, aborts, or the other transaction fails to commit.
        Each write runs on a fresh database, since the fixture commits.
        """
        writes = [
            f"INSERT INTO {table} (id) VALUES (900001)",
            f"UPDATE {table} SET {UPDATED.get(table, 'id')} = "
            f"{UPDATED.get(table, 'id')} WHERE id = 1",
            f"DELETE FROM {table} WHERE id = 1",
        ]
        conflicted = False
        for number, sql in enumerate(writes):
            connection = self.database(f"{name}_first{number}")
            other = connection.cursor()
            other.execute("BEGIN")
            other.execute(sql)
            connection.execute("BEGIN")
            try:
                connection.execute(fixture)
                connection.execute("COMMIT")
            except self.duckdb.Error as error:
                self.assertIsInstance(error, self.duckdb.TransactionException)
                connection.execute("ROLLBACK")
                conflicted = True
            try:
                other.execute("COMMIT")
            except self.duckdb.Error as error:
                self.assertIsInstance(error, self.duckdb.TransactionException)
                conflicted = True
            other.close()
        return conflicted

    def aborts(self, other, sql):
        """
        Whether the statement, or the commit of its transaction, aborts
        with a conflict. DuckDB raises every conflict as a
        TransactionException, and a failed COMMIT ends the transaction.
        """
        other.execute("BEGIN")
        try:
            other.execute(sql)
        except self.duckdb.Error as error:
            self.assertIsInstance(error, self.duckdb.TransactionException)
            other.execute("ROLLBACK")
            return True
        try:
            other.execute("COMMIT")
        except self.duckdb.Error as error:
            self.assertIsInstance(error, self.duckdb.TransactionException)
            return True
        return False

    def check_writes(self, connection, fixture, table, work):
        """Runs the fixture on its own, and checks what it wrote."""
        (rows,) = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        before = {location for _, location, _ in self.segments(connection, table)}
        used = self.used_blocks(connection)
        connection.execute(fixture)
        connection.execute("FORCE CHECKPOINT")
        after = "u" if fixture.endswith("RENAME TO u") else table
        written = {}
        for column, location, count in self.segments(connection, after):
            if location not in before:
                written[column] = written.get(column, 0) + count
        grown = self.used_blocks(connection) > used
        if work is Work.REWRITE:
            self.assertGreaterEqual(max(written.values(), default=0), rows)
        elif work is Work.INDEX_BUILD:
            self.assertEqual(written, {})
            self.assertTrue(grown)
        elif work in (Work.CATALOG, Work.SCAN):
            self.assertEqual(written, {})
            self.assertFalse(grown)

    def segments(self, connection, table):
        """
        Each column segment of the table stored in a block, as its column,
        its place in the file, and its row count; none for a table that
        is gone.
        """
        try:
            rows = connection.execute(
                "SELECT column_name, block_id, block_offset, count "
                f"FROM pragma_storage_info('{table}') "
                "WHERE segment_type <> 'VALIDITY' AND block_id >= 0"
            ).fetchall()
        except self.duckdb.Error:
            return []
        return [
            (column, (block, offset), count) for column, block, offset, count in rows
        ]

    def used_blocks(self, connection):
        (used,) = connection.execute(
            "SELECT used_blocks FROM pragma_database_size()"
        ).fetchone()
        return used
