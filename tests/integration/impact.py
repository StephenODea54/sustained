"""
The impact analysis, run against every server whose support.json row
claims the `impact` cover: the server facts read_context() reads,
Migrator.impact() on a live connection, rehearse(trace=True), and the
ground truth for every rule: each rule fixture runs on the server under
the observer, and what the server did must match what the rule
predicted for that server's version and settings.
"""

import asyncio
import io
import time
import unittest
from contextlib import redirect_stderr

from sustained.aio_migrations import AsyncMigrator
from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.exceptions import GuardBlocked, PreflightBlocked
from sustained.guards import max_blocking
from sustained.impact import (
    Evidence,
    Severity,
    TableStats,
    Work,
    analyze,
    async_preflight,
    async_read_context,
    preflight,
    read_context,
)
from sustained.impact.rules import profile_for
from sustained.impact.rules.postgres.trace import observe, sighting_plan, tables_plan
from sustained.introspect.runner import run_plan
from sustained.migrations import Migration, Migrator
from sustained.model import Model
from sustained.schema import Check, Index, Integer, String

from . import aio_lifecycle, harness

TABLES = (
    "it_impact_orders",
    "it_impact_parts",
    "it_impact_notes",
    "it_impact_migrations",
    "it_impact_rehearsals",
)

# The schema the rule fixtures run in, and the schema their fixtures
# move a table into and drop.
FIXTURE_SCHEMA = "it_impact_fixtures"
FIXTURE_SCHEMAS = (FIXTURE_SCHEMA, "s")

# Fixtures the ground truth cannot observe, with the reason. A statement
# that refuses to run inside a transaction block cannot be read between
# two sightings in one transaction.
UNOBSERVED = {
    "ALTER TABLE pt DETACH PARTITION pt1 CONCURRENTLY": "no transaction block",
    "ALTER TABLE t SET TABLESPACE pg_default": (
        "t is already in pg_default, and the test server has no other "
        "tablespace to move it to, so nothing is copied"
    ),
    "CREATE INDEX CONCURRENTLY ix2 ON t (c)": "no transaction block",
    "DROP INDEX CONCURRENTLY ix": "no transaction block",
    "REINDEX TABLE CONCURRENTLY t": "no transaction block",
    "VACUUM t": "no transaction block",
    "VACUUM FULL t": "no transaction block",
    "ALTER TABLE pi DETACH PARTITION pi1 CONCURRENTLY": "no transaction block",
}

# Fixtures the server refuses on some versions, with the versions. The
# rules predict a `danger` finding for each, which a test checks.
REFUSED = {
    "CREATE INDEX CONCURRENTLY ix2 ON pt (id)": lambda version: True,
    "DROP INDEX CONCURRENTLY pi_c": lambda version: True,
    "ALTER TABLE pt DETACH PARTITION pt1 CONCURRENTLY": lambda version: True,
    "ALTER TABLE pt ADD CONSTRAINT fk FOREIGN KEY (id) REFERENCES r (id) "
    "NOT VALID": lambda version: version < (18,),
}

# Statements the server refuses inside a transaction block, beside the
# fixtures UNOBSERVED lists for that reason.
IN_TRANSACTION = (
    "REINDEX TABLE pt",
    "REINDEX INDEX pi_c",
    f"REINDEX SCHEMA {FIXTURE_SCHEMA}",
    "CLUSTER",
)


class ImpactCase(unittest.TestCase):
    """
    Base for one server's `impact` cover. Subclasses set NAME to a row in
    support.json and DIALECT to the dialect that row names.
    """

    NAME = ""
    DIALECT = Dialects.DEFAULT

    @classmethod
    def setUpClass(cls):
        if not cls.NAME:
            raise unittest.SkipTest("base class")
        cls.connection = harness.connect(cls.NAME)

    @classmethod
    def tearDownClass(cls):
        connection = getattr(cls, "connection", None)
        if connection is not None:
            connection.close()

    def setUp(self):
        self.drop()
        self.addCleanup(self.drop)

    def drop(self):
        self.connection.rollback()
        self.execute("RESET search_path")
        for table in TABLES:
            self.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        self.execute("DROP DOMAIN IF EXISTS it_impact_text")
        for schema in FIXTURE_SCHEMAS:
            self.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    def execute(self, *statements):
        cursor = self.connection.cursor()
        for sql in statements:
            cursor.execute(sql)
        self.connection.commit()

    def fetch(self, sql):
        cursor = self.connection.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        self.connection.commit()
        return rows

    def test_reads_the_server_version_and_settings(self):
        ((number,),) = self.fetch("SHOW server_version_num")
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        number = int(number)
        self.assertEqual(context.version, (number // 10000, number % 10000))
        self.assertLessEqual({"version", "settings", "sizes", "schema"}, context.read)
        self.assertIn("TimeZone", context.settings)
        self.assertIn("lock_timeout", context.settings)

    def test_reads_each_tables_size(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id integer PRIMARY KEY, note text)",
            "INSERT INTO it_impact_orders SELECT g, 'n' FROM generate_series(1, 500) g",
            "ANALYZE it_impact_orders",
            "CREATE TABLE it_impact_notes (id integer)",
        )
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        orders = context.stats("it_impact_orders")
        self.assertEqual(orders.rows, 500)
        self.assertGreater(orders.bytes, 0)
        self.assertEqual(context.stats("public.it_impact_orders"), orders)
        # Never vacuumed or analyzed: the row estimate is empty.
        self.assertIsNone(context.stats("it_impact_notes").rows)
        self.assertIsNotNone(context.column_type("it_impact_orders", "note"))

    def test_the_async_read_matches_the_blocking_one(self):
        if self.NAME not in aio_lifecycle.ADAPTERS:
            self.skipTest(f"{self.NAME} has no async adapter")
        self.execute(
            "CREATE TABLE it_impact_orders (id integer PRIMARY KEY, note text)",
            "INSERT INTO it_impact_orders SELECT g, 'n' FROM generate_series(1, 20) g",
            "ANALYZE it_impact_orders",
        )
        blocking = read_context(self.connection, self.DIALECT)
        self.connection.rollback()

        async def read():
            adapter, close = await aio_lifecycle.ADAPTERS[self.NAME]()
            try:
                return await async_read_context(adapter, self.DIALECT)
            finally:
                await close()

        context = asyncio.run(read())
        self.assertEqual(context.version, blocking.version)
        self.assertEqual(context.read, blocking.read)
        self.assertEqual(
            context.stats("it_impact_orders"), blocking.stats("it_impact_orders")
        )

    def test_a_partitioned_table_sums_its_partitions(self):
        self.execute(
            "CREATE TABLE it_impact_parts (id integer) PARTITION BY RANGE (id)",
            "CREATE TABLE it_impact_parts_a PARTITION OF it_impact_parts "
            "FOR VALUES FROM (0) TO (100)",
            "CREATE TABLE it_impact_parts_b PARTITION OF it_impact_parts "
            "FOR VALUES FROM (100) TO (1000)",
            "INSERT INTO it_impact_parts SELECT generate_series(1, 300)",
            "ANALYZE it_impact_parts",
        )
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        parent = context.stats("it_impact_parts")
        leaves = [context.stats(f"it_impact_parts_{p}") for p in "ab"]
        self.assertEqual(parent.rows, 300)
        self.assertEqual(parent.bytes, sum(leaf.bytes for leaf in leaves))

    def test_the_read_leaves_an_open_transaction_usable(self):
        cursor = self.connection.cursor()
        cursor.execute("CREATE TABLE it_impact_notes (id integer)")
        read_context(self.connection, self.DIALECT)
        cursor.execute("INSERT INTO it_impact_notes VALUES (1)")
        cursor.execute("SELECT count(*) FROM it_impact_notes")
        self.assertEqual(cursor.fetchone()[0], 1)
        self.connection.rollback()

    def test_impact_reports_the_live_size(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id integer PRIMARY KEY, note text)",
            "INSERT INTO it_impact_orders SELECT g, 'n' FROM generate_series(1, 50) g",
            "ANALYZE it_impact_orders",
        )
        migrator = Migrator(
            self.connection,
            [
                Migration(
                    "001_note_index",
                    up=["CREATE INDEX it_impact_note_ix ON it_impact_orders (note)"],
                )
            ],
            dialect=self.DIALECT,
            table="it_impact_migrations",
        )
        report = migrator.impact()
        self.connection.rollback()
        self.assertIs(report.evidence, Evidence.CATALOG)
        self.assertIn("version", report.read)
        (table,) = report.statements[0].tables
        self.assertEqual(table.rows, 50)
        # A small table's blocking work is info, not a warning.
        index = [f for f in report.findings if f.rule == "pg.create_index"]
        self.assertEqual([str(f.severity) for f in index], ["info"])

    def reader(self, isolation="READ COMMITTED"):
        """
        A second session that has read it_impact_orders in a transaction
        it leaves open at the isolation level given, and its pid.
        """
        other = harness.connect(self.NAME)
        self.addCleanup(other.close)
        self.addCleanup(other.rollback)
        cursor = other.cursor()
        cursor.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
        cursor.execute("SELECT pg_backend_pid()")
        ((pid,),) = cursor.fetchall()
        cursor.execute("SELECT count(*) FROM it_impact_orders")
        cursor.fetchall()
        return pid

    def test_preflight_names_a_session_that_read_the_table(self):
        self.orders()
        pid = self.reader()
        add = "ALTER TABLE it_impact_orders ADD COLUMN extra integer"
        found = preflight(self.connection, self.DIALECT, [add], older_than=0.0)
        self.connection.rollback()
        self.assertEqual(found.read, {"locks", "transactions"})
        (blocker,) = [b for b in found.blockers if b.session.id == pid]
        self.assertEqual(blocker.statement, add)
        self.assertEqual(blocker.held, "ACCESS SHARE")
        self.assertTrue(blocker.granted)
        self.assertEqual(blocker.session.state, "idle in transaction")
        self.assertIn("it_impact_orders", blocker.session.query)
        self.assertNotIn(pid, [s.id for s in found.transactions])

    def test_preflight_passes_a_reader_for_a_weaker_lock(self):
        self.orders()
        pid = self.reader()
        index = "CREATE INDEX it_impact_note ON it_impact_orders (note)"
        found = preflight(self.connection, self.DIALECT, [index], older_than=0.0)
        self.connection.rollback()
        self.assertNotIn(pid, [b.session.id for b in found.blockers])
        self.assertIn(pid, [s.id for s in found.transactions])

    def test_preflight_waits_for_every_snapshot_before_a_concurrent_build(self):
        self.orders()
        # A READ COMMITTED transaction has no snapshot between statements.
        pid = self.reader("REPEATABLE READ")
        index = "CREATE INDEX CONCURRENTLY it_impact_note ON it_impact_orders (note)"
        found = preflight(self.connection, self.DIALECT, [index])
        self.connection.rollback()
        (blocker,) = [b for b in found.blockers if b.session.id == pid]
        self.assertIsNone(blocker.held)

    def test_up_refuses_while_a_session_reads_the_table(self):
        self.orders()
        self.reader()
        migrator = self.migrator(
            [Migration("001_extra", up="ALTER TABLE it_impact_orders ADD extra int")]
        )
        with self.assertRaises(PreflightBlocked):
            migrator.up(preflight="refuse")
        self.connection.rollback()
        columns = self.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'it_impact_orders'"
        )
        self.assertNotIn(("extra",), columns)

    def test_a_rehearsal_stops_waiting_at_its_lock_timeout(self):
        self.orders()
        self.reader()
        migrator = self.migrator(
            [
                Migration(
                    "001_extra",
                    up="ALTER TABLE it_impact_orders ADD extra int",
                    down="ALTER TABLE it_impact_orders DROP COLUMN extra",
                )
            ]
        )
        started = time.monotonic()
        (result,) = migrator.rehearse(lock_timeout=0.2)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertFalse(result.up_ok)
        self.assertIn("lock timeout", result.error)
        # SET LOCAL ends with the rehearsal transaction.
        self.assertEqual(self.fetch("SHOW lock_timeout"), [("0",)])

    def test_async_preflight_reads_the_same_blocker(self):
        if self.NAME not in aio_lifecycle.ADAPTERS:
            self.skipTest(f"{self.NAME} has no async adapter")
        self.orders()
        pid = self.reader()
        add = "ALTER TABLE it_impact_orders ADD COLUMN extra integer"

        async def read():
            adapter, close = await aio_lifecycle.ADAPTERS[self.NAME]()
            try:
                return await async_preflight(adapter, self.DIALECT, [add])
            finally:
                await close()

        found = asyncio.run(read())
        self.assertIn(pid, [b.session.id for b in found.blockers])

    def locker(self, table):
        """A second session that has locked the table ACCESS EXCLUSIVE."""
        other = harness.connect(self.NAME)
        self.addCleanup(other.close)
        self.addCleanup(other.rollback)
        other.cursor().execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")

    def bounded(self):
        """Stops a statement of the test's session that would wait forever."""
        self.execute("SET statement_timeout = '10s'")
        self.addCleanup(self.execute, "RESET statement_timeout")

    def test_the_size_read_waits_for_a_locked_table_only_so_long(self):
        self.orders()
        self.execute("CREATE TABLE it_impact_notes (id integer)")
        self.locker("it_impact_orders")
        self.bounded()
        statements = [
            "ALTER TABLE it_impact_notes ADD COLUMN c integer",
            "ALTER TABLE it_impact_orders ADD COLUMN c integer",
        ]
        context = read_context(self.connection, self.DIALECT, statements=statements)
        self.connection.rollback()
        self.assertEqual(context.stats("it_impact_orders"), TableStats())
        self.assertIsNotNone(context.stats("it_impact_notes").bytes)
        self.assertNotIn("it_impact_parts", context.tables)
        self.assertEqual(self.fetch("SHOW lock_timeout"), [("0",)])

    def test_up_waits_behind_no_lock_on_a_table_the_run_does_not_name(self):
        self.orders()
        self.locker("it_impact_orders")
        self.bounded()
        migrator = self.migrator(
            [
                Migration("001_notes", up="CREATE TABLE it_impact_notes (id int)"),
                Migration("002_c", up="ALTER TABLE it_impact_notes ADD c int"),
            ]
        )
        with redirect_stderr(io.StringIO()):
            self.assertEqual(migrator.up(unrehearsed=True), ["001_notes", "002_c"])

    def test_a_guard_blocks_on_a_size_the_lock_left_unread(self):
        self.orders()
        self.locker("it_impact_orders")
        self.bounded()
        migrator = self.migrator(
            [Migration("001_c", up="ALTER TABLE it_impact_orders ADD c int")],
            guards=[max_blocking("writes", over_rows=1_000_000)],
        )
        with self.assertRaises(GuardBlocked):
            migrator.up(unrehearsed=True)

    def migrator(self, migrations, guards=()):
        return Migrator(
            self.connection,
            migrations,
            dialect=self.DIALECT,
            guards=list(guards),
            table="it_impact_migrations",
            rehearsal_table="it_impact_rehearsals",
        )

    def orders(self):
        self.execute(
            "CREATE TABLE it_impact_orders (id integer PRIMARY KEY, note varchar(10))",
            "INSERT INTO it_impact_orders SELECT g, 'n' FROM generate_series(1, 50) g",
            "ANALYZE it_impact_orders",
        )

    TRACED = [
        Migration(
            "001_traced",
            up=[
                "CREATE INDEX it_impact_note_ix ON it_impact_orders (note)",
                "ALTER TABLE it_impact_orders ALTER COLUMN id TYPE bigint",
            ],
            down=None,
        )
    ]

    def test_a_traced_rehearsal_observes_each_statement(self):
        self.orders()
        results = self.migrator(self.TRACED).rehearse(trace=True)
        self.assertTrue(results.ok)
        report = results.impact
        self.assertIs(report.evidence, Evidence.OBSERVED)
        index, retype = report.statements
        self.assertEqual(
            [(t.table, t.lock, t.work) for t in index.tables],
            [("it_impact_orders", "SHARE", Work.INDEX_BUILD)],
        )
        self.assertEqual(
            [(t.table, t.lock, t.work) for t in retype.tables],
            [("it_impact_orders", "ACCESS EXCLUSIVE", Work.REWRITE)],
        )
        self.assertNotIn("impact.mismatch", [f.rule for f in report.findings])
        # The rehearsal rolled everything back.
        rows = self.fetch(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'it_impact_note_ix'"
        )
        self.assertEqual(rows, [(0,)])

    def test_a_trace_reports_a_wrong_prediction(self):
        self.orders()
        self.execute("CREATE DOMAIN it_impact_text AS text")
        results = self.migrator(
            [
                Migration(
                    "001_domain",
                    up="ALTER TABLE it_impact_orders ALTER COLUMN note "
                    "TYPE it_impact_text",
                    down=None,
                )
            ]
        ).rehearse(trace=True)
        (statement,) = results.impact.statements
        # A domain without constraints is binary coercible, which the
        # rules do not know, so they predicted a rewrite.
        self.assertIs(statement.tables[0].work, Work.SCAN)
        mismatches = [f for f in statement.findings if f.rule == "impact.mismatch"]
        self.assertEqual(len(mismatches), 1)
        self.assertIn("copied no file", mismatches[0].message)

    def test_the_async_trace_matches_the_blocking_one(self):
        if self.NAME not in aio_lifecycle.ADAPTERS:
            self.skipTest(f"{self.NAME} has no async adapter")
        self.orders()
        blocking = self.migrator(self.TRACED).rehearse(trace=True).impact

        async def rehearse():
            adapter, close = await aio_lifecycle.ADAPTERS[self.NAME]()
            try:
                migrator = AsyncMigrator(
                    adapter,
                    self.TRACED,
                    dialect=self.DIALECT,
                    table="it_impact_migrations",
                    rehearsal_table="it_impact_rehearsals",
                )
                return (await migrator.rehearse(trace=True)).impact
            finally:
                await close()

        report = asyncio.run(rehearse())
        self.assertIs(report.evidence, Evidence.OBSERVED)
        self.assertEqual(
            [s.tables for s in report.statements],
            [s.tables for s in blocking.statements],
        )

    def online_models(self):
        parts = type(
            "Parts",
            (Model,),
            {
                "tableName": "it_impact_parts",
                "tableColumns": {
                    "id": Integer(primary_key=True),
                    "code": String(10),
                },
                "_dialect": self.DIALECT,
            },
        )
        orders = type(
            "Orders",
            (Model,),
            {
                "tableName": "it_impact_orders",
                "tableColumns": {
                    "id": Integer(primary_key=True),
                    "note": String(10),
                    "status": String(10, nullable=False, backfill="new"),
                    "part_id": Integer(references="it_impact_parts.id"),
                    "ref": String(10, unique=True),
                },
                "indexes": [Index("it_impact_note_ix", "note")],
                "tableConstraints": [Check("it_impact_note_ck", "note <> ''")],
                "_dialect": self.DIALECT,
            },
        )
        return [parts, orders]

    def online_schema(self):
        # PostgreSQL 18 also stores each NOT NULL as a row of contype 'n'.
        constraints = self.fetch(
            "SELECT conname, contype, convalidated FROM pg_constraint "
            "WHERE conrelid = 'it_impact_orders'::regclass AND contype <> 'n' "
            "ORDER BY conname"
        )
        indexes = self.fetch(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = 'it_impact_orders'::regclass ORDER BY c.relname"
        )
        columns = self.fetch(
            "SELECT attname, attnotnull FROM pg_attribute "
            "WHERE attrelid = 'it_impact_orders'::regclass AND attnum > 0 "
            "AND NOT attisdropped ORDER BY attname"
        )
        return constraints, indexes, columns

    def test_an_online_run_applies_two_migrations_and_reverts_them(self):
        self.orders()
        models = self.online_models()
        migrator = self.migrator([])
        applied = migrator.up(
            models=models, migration_id="002_online_orders", online=True
        )
        self.assertEqual(applied, ["002_online_orders", "002_online_orders_online"])
        self.assertEqual(migrator.plan_migrations(models, online=True), [])
        self.connection.rollback()
        constraints, indexes, columns = self.online_schema()
        self.assertEqual(
            constraints,
            [
                ("it_impact_note_ck", "c", True),
                ("it_impact_orders_part_id_fkey", "f", True),
                ("it_impact_orders_pkey", "p", True),
                ("it_impact_orders_ref_key", "u", True),
            ],
        )
        self.assertEqual(
            indexes,
            [
                ("it_impact_note_ix", True),
                ("it_impact_orders_pkey", True),
                ("it_impact_orders_ref_key", True),
            ],
        )
        self.assertIn(("status", True), columns)
        # On 18, the column's NOT NULL takes the name the direct form gives it.
        named = self.fetch(
            "SELECT conname FROM pg_constraint WHERE contype = 'n' "
            "AND conrelid = 'it_impact_orders'::regclass ORDER BY conname"
        )
        self.assertIn(
            named,
            [
                [],
                [
                    ("it_impact_orders_id_not_null",),
                    ("it_impact_orders_status_not_null",),
                ],
            ],
        )
        self.assertEqual(
            self.fetch("SELECT count(*) FROM it_impact_orders WHERE status = 'new'"),
            [(50,)],
        )
        # A migrator that never saw the diff reverts both from their rows.
        reverted = self.migrator([]).down(steps=2)
        self.assertEqual(reverted, ["002_online_orders_online", "002_online_orders"])
        self.connection.rollback()
        constraints, indexes, columns = self.online_schema()
        self.assertEqual(constraints, [("it_impact_orders_pkey", "p", True)])
        self.assertEqual(indexes, [("it_impact_orders_pkey", True)])
        self.assertEqual(columns, [("id", True), ("note", False)])
        self.assertEqual(self.fetch("SELECT to_regclass('it_impact_parts')"), [(None,)])

    def test_an_online_rehearsal_covers_the_online_run(self):
        self.orders()
        models = self.online_models()
        migrator = self.migrator([])
        results = migrator.rehearse(
            models=models, migration_id="002_online_orders", online=True
        )
        self.assertTrue(results.ok)
        self.assertEqual(
            [(r.id, r.up_ok, r.down_ok) for r in results],
            [
                ("002_online_orders", True, True),
                ("002_online_orders_online", True, True),
            ],
        )
        run = migrator.plan_migrations(
            models, migration_id="002_online_orders", online=True
        )
        self.assertEqual(
            migrator.run_outcome(migrator.applied_records(), run), "passed"
        )
        self.connection.rollback()
        # The rehearsal rolled everything back.
        self.assertEqual(self.online_schema()[1], [("it_impact_orders_pkey", True)])

    def test_an_async_online_run_applies_the_same_migrations(self):
        if self.NAME not in aio_lifecycle.ADAPTERS:
            self.skipTest(f"{self.NAME} has no async adapter")
        self.orders()
        models = self.online_models()

        async def run():
            adapter, close = await aio_lifecycle.ADAPTERS[self.NAME]()
            try:
                migrator = AsyncMigrator(
                    adapter,
                    [],
                    dialect=self.DIALECT,
                    table="it_impact_migrations",
                    rehearsal_table="it_impact_rehearsals",
                )
                rehearsal = await migrator.rehearse(
                    models=models, migration_id="002_online_orders", online=True
                )
                applied = await migrator.up(
                    models=models, migration_id="002_online_orders", online=True
                )
                return rehearsal, applied, await migrator.down(steps=2)
            finally:
                await close()

        rehearsal, applied, reverted = asyncio.run(run())
        self.assertTrue(rehearsal.ok)
        self.assertEqual(applied, ["002_online_orders", "002_online_orders_online"])
        self.assertEqual(reverted, applied[::-1])
        self.assertEqual(self.online_schema()[1], [("it_impact_orders_pkey", True)])

    def test_online_impact_reads_the_run_as_two_migrations(self):
        self.orders()
        report = self.migrator([]).impact(models=self.online_models(), online=True)
        self.connection.rollback()
        ddl, online = report.migrations
        self.assertEqual(online.migration_id, f"{ddl.migration_id}_online")
        self.assertEqual((ddl.transactional, online.transactional), (True, False))
        self.assertFalse(online.held_to_commit)
        # Only the backfill, one UPDATE of every row, is left a danger.
        dangers = {f.rule for f in report.findings if f.severity is Severity.DANGER}
        self.assertLessEqual(dangers, {"pg.write_rows"})

    def fixture_context(self):
        """
        The fixture schema, created under the UTC TimeZone the fixtures
        of a timestamp change need, and the context read of it.
        """
        profile = profile_for(self.DIALECT)
        self.execute("SET TimeZone TO 'UTC'")
        self.addCleanup(self.execute, "RESET TimeZone")
        self.execute(
            f"CREATE SCHEMA {FIXTURE_SCHEMA}",
            f"SET search_path TO {FIXTURE_SCHEMA}",
            *profile.fixture_schema,
            "ANALYZE",
        )
        context = read_context(self.connection, self.DIALECT)
        self.connection.rollback()
        return profile, context

    def test_each_rule_fixture_does_what_its_rule_predicts(self):
        profile, context = self.fixture_context()
        fixtures = [(rule, f) for rule in profile.rules for f in rule.fixtures]
        self.assertLessEqual(set(UNOBSERVED) | set(REFUSED), {f for _, f in fixtures})
        for rule, fixture in fixtures:
            if fixture in UNOBSERVED:
                continue
            if fixture in REFUSED and REFUSED[fixture](context.version):
                continue
            with self.subTest(rule=rule.id, fixture=fixture):
                statement = self.observe_fixture(fixture, context, profile)
                self.assertIs(statement.evidence, Evidence.OBSERVED)
                reached = {t.rule for t in statement.tables}
                reached |= {f.rule for f in statement.findings}
                self.assertIn(rule.id, reached)
                mismatches = [
                    f.message for f in statement.findings if f.rule == "impact.mismatch"
                ]
                self.assertEqual(mismatches, [])

    def dangers(self, statement, context, transactional):
        """The rule and message of each danger finding the statement draws."""
        (found,) = analyze(
            [MigrationStatement(statement, "m1", transactional=transactional)],
            self.DIALECT,
            context,
        ).statements
        return {
            (f.rule, f.message) for f in found.findings if f.severity is Severity.DANGER
        }

    def test_each_refused_fixture_is_refused_and_predicted_danger(self):
        profile, context = self.fixture_context()
        rules = {f: rule.id for rule in profile.rules for f in rule.fixtures}
        self.connection.autocommit = True
        self.addCleanup(setattr, self.connection, "autocommit", False)
        for fixture, refused in REFUSED.items():
            if not refused(context.version):
                continue
            with self.subTest(fixture=fixture):
                with self.assertRaises(Exception):
                    self.connection.cursor().execute(fixture)
                found = self.dangers(fixture, context, False)
                self.assertIn(rules[fixture], {rule for rule, _ in found})

    def test_a_statement_refused_in_a_transaction_block_is_danger(self):
        profile, context = self.fixture_context()
        blocked = [f for f, why in UNOBSERVED.items() if why == "no transaction block"]
        for statement in [*blocked, *IN_TRANSACTION]:
            with self.subTest(statement=statement):
                try:
                    with self.assertRaisesRegex(Exception, "transaction block"):
                        self.connection.cursor().execute(statement)
                finally:
                    self.connection.rollback()
                found = self.dangers(statement, context, True)
                self.assertTrue(any("transaction block" in m for _, m in found))
                found = self.dangers(statement, context, False)
                self.assertFalse(any("transaction block" in m for _, m in found))

    def test_the_read_finds_partitions_indexes_and_domains(self):
        profile, context = self.fixture_context()
        pt = context.relations["pt"]
        self.assertTrue(pt.partitioned)
        self.assertEqual(pt.partitions, ("pt1", "ptd"))
        self.assertEqual(pt.default, "ptd")
        self.assertEqual(context.relations[f"{FIXTURE_SCHEMA}.pt1"].parent, "pt")
        self.assertFalse(context.relations["pt1"].partitioned)
        self.assertEqual(dict(context.relations["pi"].indexed), {"c": None})
        self.assertEqual(
            dict(context.relations["d"].indexed),
            {"at": None, "atz": None, "label": "default"},
        )
        self.assertNotIn("r", context.relations["t"].indexed)
        self.assertIn("id", context.relations["t"].indexed)
        self.assertIs(context.types["positive"], True)
        self.assertIs(context.types[f"{FIXTURE_SCHEMA}.positive"], True)
        self.assertEqual(
            dict(context.relations["d"].arrays), {"tags": "character varying(10)[]"}
        )
        self.assertLessEqual({"partitions", "indexes", "arrays", "types"}, context.read)

    def observe_fixture(self, fixture, context, profile):
        """
        The fixture's predicted impact with what the server did in its
        place, read between two sightings in a transaction that is then
        rolled back.
        """
        (predicted,) = analyze([fixture], self.DIALECT, context).statements
        tables = [t.table for t in predicted.tables]
        try:
            existing = run_plan(self.connection, self.DIALECT, tables_plan())
            before = run_plan(self.connection, self.DIALECT, sighting_plan(tables))
            self.connection.cursor().execute(fixture)
            after = run_plan(self.connection, self.DIALECT, sighting_plan(tables))
        finally:
            self.connection.rollback()
        return observe(predicted, before, after, existing, profile)
