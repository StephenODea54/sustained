"""
Tests that every statement of one migration step runs on one database
session outside a transaction block. On DuckDB each cursor is a session of
its own, so a TEMP table or a USE from one statement of a
transactional=False step must reach the next statement.
"""

import unittest

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False

from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.migrations import Migration, Migrator

TEMP_UP = [
    "CREATE TEMP TABLE tt (x INTEGER)",
    "INSERT INTO tt VALUES (1)",
    "CREATE TABLE keep AS SELECT * FROM tt",
]
TEMP_DOWN = [
    "CREATE TEMP TABLE gone (x INTEGER)",
    "DROP TABLE keep",
    "DROP TABLE gone",
]
USE_UP = ["CREATE SCHEMA s2", "USE memory.s2", "CREATE TABLE t2 (x INTEGER)"]
USE_DOWN = ["USE memory.s2", "DROP TABLE t2", "USE memory.main", "DROP SCHEMA s2"]


def migrations():
    return [
        Migration("001", TEMP_UP, down=TEMP_DOWN, transactional=False),
        Migration("002", USE_UP, down=USE_DOWN, transactional=False),
    ]


def tables(conn):
    rows = conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_name IN ('keep', 't2') ORDER BY table_name"
    ).fetchall()
    return [tuple(r) for r in rows]


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestBlockingStepSession(unittest.TestCase):
    def test_a_step_runs_its_statements_on_one_session(self):
        conn = duckdb.connect()
        migrator = Migrator(conn, migrations(), dialect=Dialects.DUCKDB)
        migrator.up()
        self.assertEqual(tables(conn), [("main", "keep"), ("s2", "t2")])
        self.assertEqual(conn.execute("SELECT x FROM keep").fetchall(), [(1,)])
        migrator.down(steps=2)
        self.assertEqual(tables(conn), [])


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestAsyncStepSession(unittest.IsolatedAsyncioTestCase):
    async def test_a_step_runs_its_statements_on_one_session(self):
        conn = duckdb.connect()
        adapter = DbApiAsyncAdapter(conn)
        migrator = AsyncMigrator(adapter, migrations(), dialect=Dialects.DUCKDB)
        await migrator.up()
        self.assertEqual(tables(conn), [("main", "keep"), ("s2", "t2")])
        await migrator.down(steps=2)
        self.assertEqual(tables(conn), [])
