"""Tests for autogenerate_migrations() and its online split on PostgreSQL."""

import asyncio
import json
import unittest

from sustained.aio_migrations import AsyncMigrator
from sustained.analysis import destructive_statements, with_intent
from sustained.autogenerate import autogenerate, autogenerate_migrations, diff_schema
from sustained.autogenerate.online import (
    concurrently,
    constraint_name,
    if_exists,
    not_null_check_name,
    object_name,
    partition_index_name,
)
from sustained.dialects import Dialects
from sustained.impact import EngineContext, Severity, analyze
from sustained.impact.context import TableStats
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedPartition,
    IntrospectedTable,
    Snapshot,
)
from sustained.migrations import Migration, Migrator
from sustained.migrations.checks import run_statements
from sustained.migrations.migration import _restore_migration, _stored_steps
from sustained.schema import Check, ForeignKey, Index, IndexColumn, Integer, String
from sustained.types import Expression
from tests.test_autogenerate import make_model

PG = Dialects.POSTGRES


class Rows:
    """A connection whose every table holds a row."""

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        pass

    def fetchall(self):
        return [(1,)]

    def fetchone(self):
        return (1,)

    def close(self):
        pass


def models():
    customers = make_model(
        "OnlineCustomers",
        "customers",
        {"id": Integer(primary_key=True), "code": String(10, unique=True)},
    )
    orders = make_model(
        "OnlineOrders",
        "orders",
        {
            "id": Integer(primary_key=True),
            "customer_id": Integer(references="customers.id"),
            "status": String(10, nullable=False, backfill="new"),
            "email": String(120, nullable=False, backfill="x"),
            "ref": String(10),
        },
    )
    orders.indexes = [Index("ix_orders_status", "status")]
    orders.tableConstraints = [
        Check("ck_status", "status <> ''"),
        ForeignKey("fk_orders_ref", "ref", "customers.code"),
    ]
    for model in (customers, orders):
        model.set_dialect(PG)
    return [customers, orders]


def snapshot(**orders_extra):
    return Snapshot(
        {
            "customers": IntrospectedTable(
                {"id": IntrospectedColumn("integer", False, True)},
                primary_key=("id",),
                name="customers",
            ),
            "orders": IntrospectedTable(
                {
                    "id": IntrospectedColumn("integer", False, True),
                    "email": IntrospectedColumn("character varying(120)", True, False),
                    "ref": IntrospectedColumn("character varying(10)", True, False),
                },
                primary_key=("id",),
                name="orders",
                **orders_extra,
            ),
        },
        constraints_read=True,
        checks_read=True,
    )


def orders_snapshot(**extra):
    """The schema with the orders table alone."""
    found = snapshot(**extra)
    del found["customers"]
    return found


def generate(online, found=None, given=None, **options):
    return autogenerate_migrations(
        Rows(),
        given or models(),
        id="m1",
        dialect=PG,
        snapshot=found or snapshot(),
        online=online,
        **options,
    )


class DirectTestCase(unittest.TestCase):
    def test_without_online_the_list_holds_what_autogenerate_returns(self):
        (only,) = generate(False)
        single = autogenerate(
            Rows(), models(), id="m1", dialect=PG, snapshot=snapshot()
        )
        self.assertEqual(
            (only.id, only.up, only.down), (single.id, single.up, single.down)
        )
        self.assertTrue(only.transactional)

    def test_an_up_to_date_schema_generates_nothing(self):
        customers, _ = models()
        found = Snapshot(
            {
                "customers": IntrospectedTable(
                    {
                        "id": IntrospectedColumn("integer", False, True),
                        "code": IntrospectedColumn(
                            "character varying(10)", True, False
                        ),
                    },
                    primary_key=("id",),
                    indexes={
                        "customers_code_key": IntrospectedIndex(
                            ("code",),
                            True,
                            name="customers_code_key",
                            constraint=True,
                        )
                    },
                    name="customers",
                )
            }
        )
        self.assertEqual(generate(True, found, [customers]), [])

    def test_online_is_ignored_on_another_dialect(self):
        sqlite = make_model("OnlineSqlite", "notes", {"id": Integer(primary_key=True)})
        sqlite.indexes = [Index("ix_notes_id", "id")]
        found = Snapshot(
            {
                "notes": IntrospectedTable(
                    {"id": IntrospectedColumn("integer", False, True)},
                    primary_key=("id",),
                    name="notes",
                )
            }
        )
        (only,) = autogenerate_migrations(
            Rows(), [sqlite], id="m1", snapshot=found, online=True
        )
        self.assertEqual(only.up, ['CREATE INDEX "ix_notes_id" ON "notes" ("id")'])
        self.assertTrue(only.transactional)


class SplitTestCase(unittest.TestCase):
    def setUp(self):
        self.ddl, self.online = generate(True)

    def test_the_ddl_migration_changes_only_the_catalog(self):
        self.assertEqual(self.ddl.id, "m1")
        self.assertTrue(self.ddl.transactional)
        self.assertEqual(
            self.ddl.up,
            [
                'ALTER TABLE "customers" ADD COLUMN "code" VARCHAR(10)',
                'ALTER TABLE "orders" ADD COLUMN "customer_id" INTEGER',
                'ALTER TABLE "orders" ADD COLUMN "status" VARCHAR(10) NOT NULL '
                "DEFAULT 'new'",
                'ALTER TABLE "orders" ALTER COLUMN "status" DROP DEFAULT',
                'ALTER TABLE "orders" ADD CONSTRAINT "orders_customer_id_fkey" '
                'FOREIGN KEY ("customer_id") REFERENCES "customers" ("id") NOT VALID',
                'ALTER TABLE "orders" ADD CONSTRAINT "ck_status" '
                "CHECK (status <> '') NOT VALID",
            ],
        )
        self.assertEqual(
            self.ddl.down,
            [
                'ALTER TABLE "orders" DROP CONSTRAINT "ck_status"',
                'ALTER TABLE "orders" DROP CONSTRAINT "orders_customer_id_fkey"',
                'ALTER TABLE "orders" DROP COLUMN "status"',
                'ALTER TABLE "orders" DROP COLUMN "customer_id"',
                'ALTER TABLE "customers" DROP COLUMN "code"',
            ],
        )

    def test_the_online_migration_runs_the_row_work_in_group_order(self):
        self.assertEqual(self.online.id, "m1_online")
        self.assertFalse(self.online.transactional)
        self.assertEqual(
            self.online.up,
            [
                'UPDATE "orders" SET "email" = \'x\' WHERE "email" IS NULL',
                'DROP INDEX CONCURRENTLY IF EXISTS "customers_code_key"',
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
                '"customers_code_key" ON "customers" ("code")',
                'ALTER TABLE "customers" ADD CONSTRAINT "customers_code_key" '
                'UNIQUE USING INDEX "customers_code_key"',
                'DROP INDEX CONCURRENTLY IF EXISTS "ix_orders_status"',
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_orders_status" '
                'ON "orders" ("status")',
                'ALTER TABLE "orders" ADD CONSTRAINT "fk_orders_ref" '
                'FOREIGN KEY ("ref") REFERENCES "customers" ("code") NOT VALID',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "orders_customer_id_fkey"',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "ck_status"',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "fk_orders_ref"',
                'ALTER TABLE "orders" DROP CONSTRAINT IF EXISTS '
                '"orders_email_not_null_check"',
                'ALTER TABLE "orders" ADD CONSTRAINT "orders_email_not_null_check" '
                'CHECK ("email" IS NOT NULL) NOT VALID',
                'UPDATE "orders" SET "email" = \'x\' WHERE "email" IS NULL',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "orders_email_not_null_check"',
                'ALTER TABLE "orders" ALTER COLUMN "email" SET NOT NULL',
                'ALTER TABLE "orders" DROP CONSTRAINT IF EXISTS '
                '"orders_email_not_null_check"',
            ],
        )

    def test_the_online_down_step_leaves_what_the_ddl_migration_made(self):
        self.assertEqual(
            self.online.down,
            [
                'ALTER TABLE "orders" ALTER COLUMN "email" DROP NOT NULL',
                'ALTER TABLE "orders" DROP CONSTRAINT "fk_orders_ref"',
                'DROP INDEX CONCURRENTLY IF EXISTS "ix_orders_status"',
                'ALTER TABLE "customers" DROP CONSTRAINT "customers_code_key"',
            ],
        )

    def test_each_statement_carries_the_intent_of_its_form(self):
        kinds = [s.intent.kind for s in self.online.up]
        self.assertEqual(
            kinds[:4], ["backfill", "drop_index", "create_index", "add_unique"]
        )
        self.assertEqual(kinds[7], "validate_constraint")
        self.assertEqual(
            kinds[-6:],
            [
                "drop_constraint",
                "add_check",
                "backfill",
                "validate_constraint",
                "set_not_null",
                "drop_constraint",
            ],
        )

    def test_the_temporary_check_drop_is_not_labelled_destructive(self):
        self.assertEqual(destructive_statements(self.online.up), [])
        drop = self.online.up[-1]
        self.assertTrue(drop.intent.get("transient"))
        # The same text without the mark is a constraint drop.
        self.assertEqual(len(destructive_statements([str(drop)])), 1)
        plain = with_intent(str(drop), "drop_constraint", "orders", name="x")
        self.assertEqual(len(destructive_statements([plain])), 1)

    def test_the_analysis_finds_no_danger_but_the_backfills(self):
        tables = {
            "orders": TableStats(rows=50_000_000, bytes=20 << 30),
            "customers": TableStats(rows=2_000_000, bytes=1 << 30),
        }
        context = EngineContext(
            "postgres",
            (16,),
            settings={"lock_timeout": "5s"},
            tables=tables,
            read=frozenset({"version", "settings", "sizes"}),
        )
        compiler = Dialects.get_compiler(PG)
        report = analyze(run_statements([self.ddl, self.online], compiler), PG, context)
        dangers = [
            (s.statement, f.rule)
            for s in report.statements
            for f in s.findings
            if f.severity is Severity.DANGER
        ]
        self.assertEqual([rule for _, rule in dangers], ["pg.write_rows"] * 2)
        self.assertTrue(all(s.startswith("UPDATE") for s, _ in dangers))


class GroupsTestCase(unittest.TestCase):
    def test_an_index_alone_generates_only_the_online_migration(self):
        _, orders = models()
        orders.tableColumns = {
            "id": Integer(primary_key=True),
            "email": String(120),
            "ref": String(10),
        }
        orders.columns = tuple(orders.tableColumns)
        orders.tableConstraints = []
        (only,) = generate(True, orders_snapshot(), [orders])
        self.assertEqual(only.id, "m1_online")
        self.assertEqual(
            only.up,
            [
                'DROP INDEX CONCURRENTLY IF EXISTS "ix_orders_status"',
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_orders_status" '
                'ON "orders" ("status")',
            ],
        )

    def test_a_validation_alone_has_an_empty_down_step(self):
        _, orders = models()
        orders.tableColumns = {
            "id": Integer(primary_key=True),
            "email": String(120),
            "ref": String(10),
        }
        orders.columns = tuple(orders.tableColumns)
        orders.indexes = []
        orders.tableConstraints = [Check("ck_ref", "ref <> ''")]
        ddl, online = generate(True, orders_snapshot(), [orders])
        self.assertEqual(
            ddl.up,
            [
                'ALTER TABLE "orders" ADD CONSTRAINT "ck_ref" CHECK (ref <> \'\') NOT VALID'
            ],
        )
        self.assertEqual(
            online.up, ['ALTER TABLE "orders" VALIDATE CONSTRAINT "ck_ref"']
        )
        self.assertEqual(online.down, [])

    def test_drops_run_last_in_the_online_migration(self):
        _, orders = models()
        orders.tableColumns = {"id": Integer(primary_key=True), "email": String(120)}
        orders.columns = tuple(orders.tableColumns)
        orders.indexes = []
        orders.tableConstraints = []
        found = orders_snapshot(
            indexes={"ix_old": IntrospectedIndex(("email",), False, name="ix_old")}
        )
        (online,) = generate(True, found, [orders], allow_drops=True)
        self.assertEqual(
            online.up,
            [
                'DROP INDEX CONCURRENTLY IF EXISTS "ix_old"',
                'ALTER TABLE "orders" DROP COLUMN IF EXISTS "ref"',
            ],
        )
        self.assertIsNone(online.down)

    def test_a_dropped_index_alone_reverts_concurrently(self):
        _, orders = models()
        orders.tableColumns = {
            "id": Integer(primary_key=True),
            "email": String(120),
            "ref": String(10),
        }
        orders.columns = tuple(orders.tableColumns)
        orders.indexes = []
        orders.tableConstraints = []
        found = orders_snapshot(
            indexes={"ix_old": IntrospectedIndex(("email",), False, name="ix_old")}
        )
        (online,) = generate(True, found, [orders], allow_drops=True)
        self.assertEqual(online.up, ['DROP INDEX CONCURRENTLY IF EXISTS "ix_old"'])
        self.assertEqual(
            online.down,
            [
                'DROP INDEX CONCURRENTLY IF EXISTS "ix_old"',
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_old" ON "orders" ("email")',
            ],
        )

    def test_a_changed_index_is_dropped_and_built_concurrently(self):
        _, orders = models()
        orders.tableColumns = {
            "id": Integer(primary_key=True),
            "email": String(120),
            "ref": String(10),
        }
        orders.columns = tuple(orders.tableColumns)
        orders.indexes = [Index("ix_mail", "email", "ref")]
        orders.tableConstraints = []
        found = orders_snapshot(
            indexes={"ix_mail": IntrospectedIndex(("email",), False, name="ix_mail")}
        )
        (online,) = generate(True, found, [orders])
        self.assertEqual(
            online.up,
            [
                'DROP INDEX CONCURRENTLY IF EXISTS "ix_mail"',
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_mail" '
                'ON "orders" ("email", "ref")',
            ],
        )
        self.assertEqual(
            online.down,
            [
                'DROP INDEX CONCURRENTLY IF EXISTS "ix_mail"',
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_mail" ON "orders" ("email")',
            ],
        )


class NoAdapter:
    """An adapter that refuses every call, for a plan that reads nothing."""

    def __getattr__(self, name):
        raise AssertionError(f"the plan asked the adapter for {name}")


def notes():
    """A model whose table exists and whose one index does not."""
    model = make_model("OnlineNotes", "notes", {"id": Integer(primary_key=True)})
    model.indexes = [Index("ix_notes_id", "id")]
    model.set_dialect(PG)
    found = Snapshot(
        {
            "notes": IntrospectedTable(
                {"id": IntrospectedColumn("integer", False, True)},
                primary_key=("id",),
                name="notes",
            )
        }
    )
    return model, found


class MigratorPlanTestCase(unittest.TestCase):
    """plan(online=True), and a snapshot handed to either migrator."""

    def test_plan_refuses_a_split_into_two(self):
        migrator = Migrator(Rows(), [], dialect=PG)
        with self.assertRaises(ValueError) as caught:
            migrator.plan(models(), migration_id="m1", snapshot=snapshot(), online=True)
        self.assertIn("m1, m1_online", str(caught.exception))
        self.assertIn("plan_migrations(online=True)", str(caught.exception))
        self.assertEqual(
            [
                m.id
                for m in migrator.plan_migrations(
                    models(), migration_id="m1", snapshot=snapshot(), online=True
                )
            ],
            ["m1", "m1_online"],
        )

    def test_plan_returns_the_one_migration_a_split_generates(self):
        model, found = notes()
        planned = Migrator(Rows(), [], dialect=PG).plan(
            [model], migration_id="m1", snapshot=found, online=True
        )
        self.assertEqual(planned.id, "m1_online")
        self.assertFalse(planned.transactional)
        self.assertTrue(planned.up[-1].startswith("CREATE INDEX CONCURRENTLY"))
        self.assertIn('"ix_notes_id" ON "notes" ("id")', planned.up[-1])
        self.assertIsNone(
            Migrator(Rows(), [], dialect=PG).plan(
                [model],
                snapshot=Snapshot(
                    {
                        "notes": IntrospectedTable(
                            {"id": IntrospectedColumn("integer", False, True)},
                            primary_key=("id",),
                            indexes={"ix_notes_id": IntrospectedIndex(("id",), False)},
                            name="notes",
                        )
                    }
                ),
                online=True,
            )
        )

    def test_the_async_migrator_plans_a_snapshot_without_reading(self):
        migrator = AsyncMigrator(NoAdapter(), [], dialect=PG)
        planned = asyncio.run(
            migrator.plan_migrations(
                models(), migration_id="m1", snapshot=snapshot(), online=True
            )
        )
        expected = generate(True)
        self.assertEqual(
            [(m.id, m.up, m.transactional) for m in planned],
            [(m.id, m.up, m.transactional) for m in expected],
        )
        model, found = notes()
        single = asyncio.run(
            migrator.plan([model], migration_id="m1", snapshot=found, online=True)
        )
        self.assertEqual(single.id, "m1_online")
        without = asyncio.run(migrator.plan([model], migration_id="m1", snapshot=found))
        self.assertEqual(without.up, ['CREATE INDEX "ix_notes_id" ON "notes" ("id")'])


class TrackingRowTestCase(unittest.TestCase):
    def test_a_migration_outside_a_transaction_stores_the_flag(self):
        migration = Migration(
            "m1_online", up=["SELECT 1"], down=[], transactional=False
        )
        stored = _stored_steps(migration, generated=True)
        self.assertEqual(
            json.loads(stored), {"up": ["SELECT 1"], "down": [], "transactional": False}
        )
        restored = _restore_migration("m1_online", stored)
        self.assertFalse(restored.transactional)
        self.assertEqual(restored.down, [])

    def test_a_row_without_the_flag_restores_a_transactional_migration(self):
        stored = json.dumps({"up": ["SELECT 1"], "down": None})
        self.assertTrue(_restore_migration("m1", stored).transactional)
        self.assertNotIn(
            "transactional",
            json.loads(_stored_steps(Migration("m1", up=["SELECT 1"]), generated=True)),
        )


def pg_model(name, table, columns, indexes=(), constraints=()):
    """A Postgres model with its indexes and table constraints."""
    model = make_model(name, table, columns)
    model.indexes = list(indexes)
    model.tableConstraints = list(constraints)
    model.set_dialect(PG)
    return model


ID = IntrospectedColumn("integer", False, True)


def found_table(name, columns=None, **extra):
    """A table as the Postgres read reports it, keyed on `id`."""
    return IntrospectedTable(
        {"id": ID, **(columns or {})}, primary_key=("id",), name=name, **extra
    )


def found(*tables):
    """A schema read of the given tables, constraints and checks read."""
    return Snapshot(
        {table.name: table for table in tables},
        constraints_read=True,
        checks_read=True,
    )


def text(migration):
    """The up and down statements of a migration, as text."""
    return [str(s) for s in migration.up], [str(s) for s in migration.down or []]


VARCHAR = IntrospectedColumn("character varying(120)", True, False)
NULLABLE_INT = IntrospectedColumn("integer", True, False)


class NamesTestCase(unittest.TestCase):
    TABLE = "lt_" + "x" * 40
    COLUMN = "long_column_name_" + "y" * 20

    def test_long_names_keep_the_label_in_63_bytes(self):
        self.assertEqual(
            object_name(self.TABLE, self.COLUMN, "fkey"),
            "lt_" + "x" * 26 + "_long_column_name_" + "y" * 11 + "_fkey",
        )
        self.assertEqual(
            constraint_name(self.TABLE, self.COLUMN, "key"),
            "lt_" + "x" * 26 + "_long_column_name_" + "y" * 12 + "_key",
        )
        names = {
            object_name(self.TABLE, self.COLUMN, label)
            for label in ("key", "fkey", "not_null_check")
        }
        self.assertEqual(len(names), 3)
        self.assertEqual({len(name.encode()) for name in names}, {63})
        self.assertTrue(
            not_null_check_name(self.TABLE, self.COLUMN).endswith("_not_null_check")
        )

    def test_a_multibyte_character_is_never_cut(self):
        name = object_name(
            "ünïcödé_täble_ñame_with_many_multibyte_chars_ééééééééé",
            "çolumn_ñame_ééééééééééééééé",
            "key",
        )
        self.assertEqual(name, "ünïcödé_täble_ñame_with_çolumn_ñame_ééééééé_key")
        self.assertEqual(len(name.encode()), 62)

    def test_an_empty_second_name_leaves_one_underscore(self):
        self.assertEqual(object_name("orders", "", "idx"), "orders_idx")
        self.assertEqual(object_name("a" * 70, "", "pkey"), "a" * 58 + "_pkey")
        self.assertEqual(object_name("orders", "code", "key"), "orders_code_key")

    def test_a_partition_index_joins_its_columns(self):
        self.assertEqual(
            partition_index_name("orders_a", ["a", "b"]), "orders_a_a_b_idx"
        )


class RerunFormsTestCase(unittest.TestCase):
    def test_concurrently_adds_if_not_exists_and_if_exists(self):
        statement = with_intent(
            'CREATE UNIQUE INDEX "ix" ON "t" ("c")', "create_index", "t", name="ix"
        )
        built = concurrently(statement)
        self.assertEqual(
            str(built),
            'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "ix" ON "t" ("c")',
        )
        self.assertEqual(built.intent, statement.intent)
        self.assertEqual(
            str(concurrently('DROP INDEX "ix"')),
            'DROP INDEX CONCURRENTLY IF EXISTS "ix"',
        )
        with self.assertRaises(ValueError):
            concurrently('ALTER TABLE "t" DROP COLUMN "c"')

    def test_if_exists_goes_after_the_drop(self):
        cases = {
            'DROP TABLE "t"': 'DROP TABLE IF EXISTS "t"',
            'DROP TYPE "e"': 'DROP TYPE IF EXISTS "e"',
            'DROP INDEX "app"."ix"': 'DROP INDEX IF EXISTS "app"."ix"',
            'ALTER TABLE "app"."t" DROP CONSTRAINT "k"': (
                'ALTER TABLE "app"."t" DROP CONSTRAINT IF EXISTS "k"'
            ),
            'ALTER TABLE "a""b" DROP COLUMN "c"': (
                'ALTER TABLE "a""b" DROP COLUMN IF EXISTS "c"'
            ),
            'DROP TABLE IF EXISTS "t"': 'DROP TABLE IF EXISTS "t"',
            'ALTER TABLE "t" DROP COLUMN IF EXISTS "c"': (
                'ALTER TABLE "t" DROP COLUMN IF EXISTS "c"'
            ),
        }
        for statement, expected in cases.items():
            with self.subTest(statement):
                self.assertEqual(str(if_exists(statement)), expected)
        dropped = if_exists(with_intent('DROP TABLE "t"', "drop_table", "t"))
        self.assertEqual(dropped.intent.kind, "drop_table")
        for statement in ('CREATE TABLE "t" (id int)', 'ALTER TABLE "t" ADD "c" int'):
            with self.subTest(statement), self.assertRaises(ValueError):
                if_exists(statement)


class NotNullRouteTestCase(unittest.TestCase):
    def generate(self, columns):
        orders = pg_model(
            "RouteOrders", "orders", {"id": Integer(primary_key=True), **columns}
        )
        return generate(True, found(found_table("orders")), [orders])

    def test_a_default_alone_goes_in_with_the_column(self):
        (ddl,) = self.generate({"a": String(10, nullable=False, default="z")})
        self.assertEqual(
            text(ddl),
            (
                [
                    'ALTER TABLE "orders" ADD COLUMN "a" VARCHAR(10) NOT NULL '
                    "DEFAULT 'z'"
                ],
                ['ALTER TABLE "orders" DROP COLUMN "a"'],
            ),
        )

    def test_an_expression_backfill_takes_the_check_route(self):
        ddl, online = self.generate(
            {"b": String(40, nullable=False, backfill=Expression("now()::text"))}
        )
        self.assertEqual(ddl.up, ['ALTER TABLE "orders" ADD COLUMN "b" VARCHAR(40)'])
        backfill = 'UPDATE "orders" SET "b" = now()::text WHERE "b" IS NULL'
        drop = (
            'ALTER TABLE "orders" DROP CONSTRAINT IF EXISTS "orders_b_not_null_check"'
        )
        self.assertEqual(
            text(online),
            (
                [
                    backfill,
                    drop,
                    'ALTER TABLE "orders" ADD CONSTRAINT "orders_b_not_null_check" '
                    'CHECK ("b" IS NOT NULL) NOT VALID',
                    backfill,
                    'ALTER TABLE "orders" VALIDATE CONSTRAINT "orders_b_not_null_check"',
                    'ALTER TABLE "orders" ALTER COLUMN "b" SET NOT NULL',
                    drop,
                ],
                ['ALTER TABLE "orders" ALTER COLUMN "b" DROP NOT NULL'],
            ),
        )
        self.assertTrue(online.up[-1].intent.get("transient"))


PARTITIONS = (
    IntrospectedPartition("orders_a"),
    IntrospectedPartition(
        "orders_b", partitioned=True, partitions=(IntrospectedPartition("orders_b1"),)
    ),
)


def partitioned_orders(partitions=PARTITIONS, **extra):
    return found_table(
        "orders",
        {"email": VARCHAR, "ref": NULLABLE_INT},
        partitioned=True,
        partitions=partitions,
        **extra,
    )


def partition_build(index, columns, unique=False):
    """The statements that build an index on the partitioned orders table."""
    word = "UNIQUE INDEX" if unique else "INDEX"
    cols = ", ".join(f'"{c}"' for c in columns)
    part = "_".join(columns)
    return [
        f'CREATE {word} IF NOT EXISTS "{index}" ON ONLY "orders" ({cols})',
        f'CREATE {word} CONCURRENTLY IF NOT EXISTS "orders_a_{part}_idx" '
        f'ON "orders_a" ({cols})',
        f'ALTER INDEX "{index}" ATTACH PARTITION "orders_a_{part}_idx"',
        f'CREATE {word} IF NOT EXISTS "orders_b_{part}_idx" ON ONLY "orders_b" ({cols})',
        f'CREATE {word} CONCURRENTLY IF NOT EXISTS "orders_b1_{part}_idx" '
        f'ON "orders_b1" ({cols})',
        f'ALTER INDEX "orders_b_{part}_idx" ATTACH PARTITION "orders_b1_{part}_idx"',
        f'ALTER INDEX "{index}" ATTACH PARTITION "orders_b_{part}_idx"',
    ]


class PartitionedTestCase(unittest.TestCase):
    COLUMNS = {"id": Integer(primary_key=True), "email": String(120), "ref": Integer()}

    def test_a_new_index_key_and_foreign_key(self):
        customers = pg_model(
            "PartCustomers", "customers", {"id": Integer(primary_key=True)}
        )
        orders = pg_model(
            "PartOrders",
            "orders",
            {**self.COLUMNS, "code": String(10, unique=True)},
            [Index("ix_orders_email", "email")],
            [ForeignKey("fk_orders_ref", "ref", "customers.id")],
        )
        ddl, online = generate(
            True,
            found(found_table("customers"), partitioned_orders()),
            [customers, orders],
        )
        self.assertEqual(ddl.up, ['ALTER TABLE "orders" ADD COLUMN "code" VARCHAR(10)'])
        self.assertEqual(
            text(online),
            (
                partition_build("orders_code_key", ["code"], unique=True)
                + partition_build("ix_orders_email", ["email"])
                # A foreign key on a partitioned table cannot go in NOT
                # VALID, so it goes in validated.
                + [
                    'ALTER TABLE "orders" ADD CONSTRAINT "fk_orders_ref" '
                    'FOREIGN KEY ("ref") REFERENCES "customers" ("id")'
                ],
                [
                    'ALTER TABLE "orders" DROP CONSTRAINT "fk_orders_ref"',
                    'DROP INDEX IF EXISTS "ix_orders_email"',
                    'DROP INDEX IF EXISTS "orders_code_key"',
                ],
            ),
        )
        attach = online.up[2]
        self.assertEqual(attach.intent.kind, "attach_index")
        self.assertEqual(
            (attach.intent.table, attach.intent.get("partition")),
            ("orders", "orders_a"),
        )

    def test_a_changed_index_and_a_dropped_one(self):
        orders = pg_model(
            "PartChanged", "orders", self.COLUMNS, [Index("ix_mail", "email", "ref")]
        )
        schema = found(
            partitioned_orders(
                indexes={
                    "ix_mail": IntrospectedIndex(("email",), False, name="ix_mail"),
                    "ix_old": IntrospectedIndex(("ref",), False, name="ix_old"),
                }
            )
        )
        (online,) = generate(True, schema, [orders], allow_drops=True)
        # DROP INDEX CONCURRENTLY is refused on a partitioned table.
        self.assertEqual(
            text(online),
            (
                ['DROP INDEX IF EXISTS "ix_mail"']
                + partition_build("ix_mail", ["email", "ref"])
                + ['DROP INDEX IF EXISTS "ix_old"'],
                partition_build("ix_old", ["ref"])
                + ['DROP INDEX IF EXISTS "ix_mail"']
                + partition_build("ix_mail", ["email"]),
            ),
        )

    def test_a_partition_in_another_schema_is_named_with_it(self):
        orders = pg_model(
            "PartSchema", "orders", self.COLUMNS, [Index("ix_orders_email", "email")]
        )
        partitions = (IntrospectedPartition("orders_a", schema="arch"),)
        (online,) = generate(True, found(partitioned_orders(partitions)), [orders])
        self.assertEqual(
            online.up,
            [
                'CREATE INDEX IF NOT EXISTS "ix_orders_email" ON ONLY "orders" ("email")',
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS "orders_a_email_idx" '
                'ON "arch"."orders_a" ("email")',
                'ALTER INDEX "ix_orders_email" ATTACH PARTITION '
                '"arch"."orders_a_email_idx"',
            ],
        )
        self.assertEqual(online.up[1].intent.table, "arch.orders_a")
        self.assertEqual(online.up[2].intent.get("partition"), "arch.orders_a")

    def test_each_build_keeps_the_directions_and_the_predicate(self):
        index = Index(
            "ix_orders_email", IndexColumn("email", desc=True), where="email <> ''"
        )
        orders = pg_model("PartDetails", "orders", self.COLUMNS, [index])
        partitions = (IntrospectedPartition("orders_a"),)
        (online,) = generate(True, found(partitioned_orders(partitions)), [orders])
        self.assertEqual(
            online.up,
            [
                'CREATE INDEX IF NOT EXISTS "ix_orders_email" ON ONLY "orders" '
                "(\"email\" DESC) WHERE email <> ''",
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS "orders_a_email_idx" '
                'ON "orders_a" ("email" DESC) WHERE email <> \'\'',
                'ALTER INDEX "ix_orders_email" ATTACH PARTITION "orders_a_email_idx"',
            ],
        )

    def test_without_online_the_index_is_built_directly(self):
        orders = pg_model(
            "PartDirect", "orders", self.COLUMNS, [Index("ix_orders_email", "email")]
        )
        (only,) = generate(False, found(partitioned_orders()), [orders])
        self.assertEqual(
            only.up, ['CREATE INDEX "ix_orders_email" ON "orders" ("email")']
        )

    def test_a_partition_is_not_an_extra_table(self):
        orders = pg_model("PartOnly", "orders", self.COLUMNS)
        schema = found(
            partitioned_orders(partitions=(IntrospectedPartition("orders_a"),)),
            found_table(
                "orders_a",
                {"email": VARCHAR, "ref": NULLABLE_INT},
                partition_of="orders",
            ),
        )
        diff = diff_schema(Rows(), [orders], PG, snapshot=schema)
        self.assertEqual(diff.extra_tables, [])
        self.assertTrue(diff.is_empty())


class RepairTestCase(unittest.TestCase):
    """A schema that a failed `<id>_online` left."""

    def models(self):
        return [
            pg_model(
                "RepairOrders",
                "orders",
                {
                    "id": Integer(primary_key=True),
                    "email": String(120),
                    "code": String(10, unique=True),
                },
                [Index("ix_mail", "email")],
            )
        ]

    def schema(self):
        return found(
            found_table(
                "orders",
                {
                    "email": VARCHAR,
                    "code": IntrospectedColumn("character varying(10)", True, False),
                },
                indexes={
                    "ix_mail": IntrospectedIndex(
                        ("email",), False, name="ix_mail", valid=False
                    ),
                    "orders_code_key": IntrospectedIndex(
                        ("code",), True, name="orders_code_key", valid=False
                    ),
                },
            )
        )

    def test_invalid_indexes_are_built_again_online(self):
        (online,) = generate(True, self.schema(), self.models())
        self.assertEqual(
            text(online),
            (
                [
                    'DROP INDEX CONCURRENTLY IF EXISTS "ix_mail"',
                    'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_mail" '
                    'ON "orders" ("email")',
                    'DROP INDEX CONCURRENTLY IF EXISTS "orders_code_key"',
                    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
                    '"orders_code_key" ON "orders" ("code")',
                    'ALTER TABLE "orders" ADD CONSTRAINT "orders_code_key" '
                    'UNIQUE USING INDEX "orders_code_key"',
                ],
                # An invalid index is not put back.
                [],
            ),
        )

    def test_invalid_indexes_are_built_again_directly(self):
        (only,) = generate(False, self.schema(), self.models())
        self.assertEqual(
            text(only),
            (
                [
                    'DROP INDEX "ix_mail"',
                    'CREATE INDEX "ix_mail" ON "orders" ("email")',
                    'DROP INDEX "orders_code_key"',
                    'ALTER TABLE "orders" ADD CONSTRAINT "orders_code_key" '
                    'UNIQUE ("code")',
                ],
                [],
            ),
        )

    def test_the_diff_reports_the_invalid_indexes(self):
        diff = diff_schema(Rows(), self.models(), PG, snapshot=self.schema())
        self.assertFalse(diff.is_empty())
        self.assertEqual(diff.constraint_notes, [])
        self.assertEqual(
            diff.summary().splitlines(),
            [
                "rebuild index ix_mail on orders",
                "rebuild the invalid unique index orders_code_key on orders.code",
            ],
        )
        self.assertEqual(
            diff.outstanding(),
            [
                "index 'ix_mail' on 'orders' was not rebuilt",
                "the unique index on 'orders.code' is invalid and was not rebuilt",
            ],
        )


class ValidateTestCase(unittest.TestCase):
    """Constraints the catalog marks as not validated."""

    def models(self):
        customers = pg_model(
            "ValidCustomers", "customers", {"id": Integer(primary_key=True)}
        )
        orders = pg_model(
            "ValidOrders",
            "orders",
            {
                "id": Integer(primary_key=True),
                "cid": Integer(references="customers.id"),
                "ref": Integer(),
                "n": Integer(nullable=False),
            },
            constraints=[
                ForeignKey("fk_ref", "ref", "customers.id"),
                Check("ck_n", "n > 0"),
            ],
        )
        return [customers, orders]

    def schema(self, route_check=True):
        checks = {"ck_n": "(n > 0)"}
        if route_check:
            checks["orders_n_not_null_check"] = "(n IS NOT NULL)"
        orders = found_table(
            "orders",
            {
                "cid": NULLABLE_INT,
                "ref": NULLABLE_INT,
                "n": IntrospectedColumn("integer", False, False),
            },
            foreign_keys={
                "fk_ref": IntrospectedForeignKey(
                    ("ref",), "customers", ("id",), name="fk_ref"
                ),
                "orders_cid_fkey": IntrospectedForeignKey(
                    ("cid",), "customers", ("id",), name="orders_cid_fkey"
                ),
            },
            checks=checks,
            check_names={name: name for name in checks},
            not_valid=frozenset({"fk_ref", "orders_cid_fkey", "ck_n"}),
        )
        return found(found_table("customers"), orders)

    VALIDATE = [
        'ALTER TABLE "orders" VALIDATE CONSTRAINT "fk_ref"',
        'ALTER TABLE "orders" VALIDATE CONSTRAINT "orders_cid_fkey"',
        'ALTER TABLE "orders" VALIDATE CONSTRAINT "ck_n"',
    ]
    DROP_CHECK = (
        'ALTER TABLE "orders" DROP CONSTRAINT IF EXISTS "orders_n_not_null_check"'
    )

    def test_online_the_constraints_are_validated_and_the_check_dropped(self):
        (online,) = generate(True, self.schema(), self.models())
        self.assertEqual(online.id, "m1_online")
        self.assertEqual(text(online), (self.VALIDATE + [self.DROP_CHECK], []))
        self.assertTrue(online.up[-1].intent.get("transient"))
        self.assertEqual(destructive_statements(online.up), [])

    def test_directly_the_same_statements_run_in_the_one_migration(self):
        (only,) = generate(False, self.schema(), self.models())
        self.assertEqual(only.id, "m1")
        self.assertEqual(text(only), (self.VALIDATE + [self.DROP_CHECK], []))

    def test_the_diff_reports_each_constraint(self):
        diff = diff_schema(Rows(), self.models(), PG, snapshot=self.schema())
        self.assertEqual(diff.extra_checks, [])
        self.assertEqual(
            diff.summary().splitlines(),
            [
                "validate constraint fk_ref on orders",
                "validate constraint orders_cid_fkey on orders",
                "validate constraint ck_n on orders",
                "drop the SET NOT NULL check orders_n_not_null_check on orders",
            ],
        )
        self.assertEqual(
            diff.outstanding(),
            [
                f"constraint '{name}' on 'orders' was not validated"
                for name in ("fk_ref", "orders_cid_fkey", "ck_n")
            ],
        )

    def test_a_route_check_is_dropped_once_when_the_route_runs_again(self):
        customers, orders = self.models()
        orders.tableColumns["n"] = Integer(nullable=False, backfill=0)
        schema = self.schema()
        orders_table = schema["orders"]._replace(
            columns={**schema["orders"].columns, "n": NULLABLE_INT}
        )
        schema["orders"] = orders_table
        (online,) = generate(True, schema, [customers, orders])
        self.assertEqual(online.up.count(self.DROP_CHECK), 2)
        self.assertEqual(online.up[-1], self.DROP_CHECK)
        self.assertEqual(
            online.up[-2], 'ALTER TABLE "orders" ALTER COLUMN "n" SET NOT NULL'
        )


class LateForeignKeyTestCase(unittest.TestCase):
    """A foreign key to a key the same diff adds."""

    def customers(self, name="LateCustomers", **extra):
        return pg_model(
            name,
            "customers",
            {
                "id": Integer(primary_key=True),
                **extra,
                "code": String(10, unique=True),
                "tag": String(10),
            },
            [Index("ux_tag", "tag", unique=True)],
        )

    def lines(self):
        return pg_model(
            "LateLines",
            "lines",
            {
                "id": Integer(primary_key=True),
                "code": String(10, references="customers.code"),
                "tag": String(10, references="customers.tag"),
            },
        )

    ADD_FKS = [
        'ALTER TABLE "lines" ADD CONSTRAINT "fk_lines_code" '
        'FOREIGN KEY ("code") REFERENCES "customers" ("code")',
        'ALTER TABLE "lines" ADD CONSTRAINT "fk_lines_tag" '
        'FOREIGN KEY ("tag") REFERENCES "customers" ("tag")',
    ]

    def test_a_new_table_references_keys_added_before_it(self):
        (only,) = generate(
            False, found(found_table("customers")), [self.customers(), self.lines()]
        )
        self.assertEqual(
            only.up,
            [
                'CREATE TABLE "lines" ("id" INTEGER PRIMARY KEY, "code" VARCHAR(10), '
                '"tag" VARCHAR(10))',
                'ALTER TABLE "customers" ADD COLUMN "code" VARCHAR(10) UNIQUE',
                'ALTER TABLE "customers" ADD COLUMN "tag" VARCHAR(10)',
                'CREATE UNIQUE INDEX "ux_tag" ON "customers" ("tag")',
                *self.ADD_FKS,
            ],
        )
        self.assertEqual(
            only.down[:2],
            [
                'ALTER TABLE "lines" DROP CONSTRAINT "fk_lines_tag"',
                'ALTER TABLE "lines" DROP CONSTRAINT "fk_lines_code"',
            ],
        )

    def test_online_the_keys_go_in_after_their_indexes(self):
        ddl, online = generate(
            True, found(found_table("customers")), [self.customers(), self.lines()]
        )
        self.assertNotIn("REFERENCES", " ".join(ddl.up))
        self.assertEqual(
            online.up[-4:],
            [
                *(f"{add} NOT VALID" for add in self.ADD_FKS),
                'ALTER TABLE "lines" VALIDATE CONSTRAINT "fk_lines_code"',
                'ALTER TABLE "lines" VALIDATE CONSTRAINT "fk_lines_tag"',
            ],
        )
        self.assertEqual(
            online.up[-5],
            'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "ux_tag" '
            'ON "customers" ("tag")',
        )

    def test_a_new_column_references_new_columns_of_its_table(self):
        customers = self.customers(
            "LateSelf",
            boss=String(10, references="customers.code"),
            t2=String(10, references="customers.tag"),
        )
        (only,) = generate(False, found(found_table("customers")), [customers])
        self.assertEqual(
            only.up,
            [
                'ALTER TABLE "customers" ADD COLUMN "code" VARCHAR(10) UNIQUE',
                'ALTER TABLE "customers" ADD COLUMN "tag" VARCHAR(10)',
                'ALTER TABLE "customers" ADD COLUMN "boss" VARCHAR(10) '
                'REFERENCES "customers" ("code")',
                'ALTER TABLE "customers" ADD COLUMN "t2" VARCHAR(10)',
                'CREATE UNIQUE INDEX "ux_tag" ON "customers" ("tag")',
                'ALTER TABLE "customers" ADD CONSTRAINT "customers_t2_fkey" '
                'FOREIGN KEY ("t2") REFERENCES "customers" ("tag")',
            ],
        )
        self.assertEqual(
            only.down[0], 'ALTER TABLE "customers" DROP CONSTRAINT "customers_t2_fkey"'
        )

    def test_a_reference_to_an_existing_key_is_unchanged(self):
        customers = pg_model(
            "LateBefore",
            "customers",
            {
                "id": Integer(primary_key=True),
                "boss": Integer(references="customers.id"),
            },
        )
        (only,) = generate(False, found(found_table("customers")), [customers])
        self.assertEqual(
            only.up,
            [
                'ALTER TABLE "customers" ADD COLUMN "boss" INTEGER '
                'REFERENCES "customers" ("id")'
            ],
        )


if __name__ == "__main__":
    unittest.main()
