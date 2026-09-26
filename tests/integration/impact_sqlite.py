"""
The impact analysis on SQLite: the facts read_context() reads from a
database file, and the ground truth for every rule. Each rule fixture
runs twice on a fresh WAL database holding the fixture schema:

- inside a transaction, while a second connection tries to take the
  write lock and to read, which shows whether the statement took the
  database write lock and whether reads went on
- on its own, with automatic checkpoints off, so the frames it leaves in
  the WAL count the pages it wrote

A statement the rules say copies a table or builds an index must write
at least half as many pages as the table holds, and one they say
changes only the schema at most two. A scan or a row write may write
any number, so its page count proves nothing.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

from sustained.dialects import Dialects
from sustained.impact import Blocks, Work, analyze, read_context
from sustained.impact.rules import profile_for
from sustained.impact.window import DATABASE

# The most pages a schema change writes: the schema page, and the
# database header when the change adds a page.
CATALOG_PAGES = 2


class SqliteImpactCase(unittest.TestCase):
    """
    Base for SQLite's `impact` cover. Subclasses set NAME to the row in
    support.json.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT

    def setUp(self):
        if not self.NAME:
            self.skipTest("base class")
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def database(self, name):
        """A WAL database file holding the fixture schema, checkpointed."""
        path = os.path.join(self.dir, f"{name}.db")
        connection = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode = wal")
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        for sql in profile_for(self.DIALECT).fixture_schema:
            connection.execute(sql)
        connection.execute("ANALYZE")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return path, connection

    def test_reads_the_journal_mode_and_sizes(self):
        _, connection = self.database("context")
        context = read_context(connection, self.DIALECT)
        self.assertEqual(context.settings["journal_mode"], "wal")
        self.assertLessEqual({"version", "settings", "sizes", "schema"}, context.read)
        self.assertEqual(context.stats("t").rows, 2000)
        self.assertGreater(context.stats("t").bytes, 0)
        self.assertGreater(context.stats(DATABASE).bytes, context.stats("t").bytes)

    def test_each_rule_fixture_does_what_its_rule_predicts(self):
        profile = profile_for(self.DIALECT)
        fixtures = [(rule, f) for rule in profile.rules for f in rule.fixtures]
        for number, (rule, fixture) in enumerate(fixtures):
            with self.subTest(rule=rule.id, fixture=fixture):
                path, connection = self.database(f"fixture{number}")
                context = read_context(connection, self.DIALECT)
                (predicted,) = analyze([fixture], self.DIALECT, context).statements
                reached = {t.rule for t in predicted.tables}
                reached |= {f.rule for f in predicted.findings}
                self.assertIn(rule.id, reached)
                blocks = max((t.blocks for t in predicted.tables), default=None)
                if fixture != "VACUUM":
                    # VACUUM refuses to run inside a transaction.
                    writes, reads = self.lock_seen(path, connection, fixture)
                    self.assertEqual(
                        writes, blocks is not None and blocks >= Blocks.WRITES
                    )
                    self.assertTrue(reads)
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                pages = self.pages(connection, predicted.tables[0].table)
                connection.execute(fixture)
                _, written, _ = connection.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                work = max(t.work for t in predicted.tables)
                if work is Work.CATALOG:
                    self.assertLessEqual(written, CATALOG_PAGES)
                elif work in (Work.REWRITE, Work.INDEX_BUILD):
                    self.assertGreaterEqual(written, pages / 2)
                    self.assertGreater(written, CATALOG_PAGES)

    def lock_seen(self, path, connection, fixture):
        """
        Whether a second connection's writes waited, and whether its
        reads went on, while the fixture's transaction was open.
        """
        other = sqlite3.connect(path, timeout=0, isolation_level=None)
        try:
            connection.execute("BEGIN")
            connection.execute(fixture)
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                self.assertIn("locked", str(error))
                writes = True
            else:
                other.execute("ROLLBACK")
                writes = False
            reads = other.execute("SELECT count(*) FROM r").fetchone() == (3,)
        finally:
            connection.execute("ROLLBACK")
            other.close()
        return writes, reads

    def pages(self, connection, table):
        """The pages a table's rows fill, or the database's for DATABASE."""
        if table == DATABASE:
            return connection.execute("PRAGMA page_count").fetchone()[0]
        (count,) = connection.execute(
            "SELECT count(*) FROM dbstat WHERE name = ?", (table,)
        ).fetchone()
        return count
