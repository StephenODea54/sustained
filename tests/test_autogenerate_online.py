"""Tests for autogenerate_migrations() and its online split on PostgreSQL."""

import json
import unittest

from sustained.autogenerate import autogenerate, autogenerate_migrations
from sustained.dialects import Dialects
from sustained.impact import EngineContext, Severity, analyze
from sustained.impact.context import TableStats
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
)
from sustained.migrations import Migration
from sustained.migrations.checks import run_statements
from sustained.migrations.migration import _restore_migration, _stored_steps
from sustained.schema import Check, ForeignKey, Index, Integer, String
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
                'ALTER TABLE "orders" ADD CONSTRAINT "orders_customer_id_fkey" '
                'FOREIGN KEY ("customer_id") REFERENCES "customers" ("id") NOT VALID',
                'ALTER TABLE "orders" ADD COLUMN "status" VARCHAR(10)',
                'ALTER TABLE "orders" ADD CONSTRAINT "ck_status" '
                "CHECK (status <> '') NOT VALID",
            ],
        )
        self.assertEqual(
            self.ddl.down,
            [
                'ALTER TABLE "orders" DROP CONSTRAINT "ck_status"',
                'ALTER TABLE "orders" DROP COLUMN "status"',
                'ALTER TABLE "orders" DROP CONSTRAINT "orders_customer_id_fkey"',
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
                'UPDATE "orders" SET "status" = \'new\' WHERE "status" IS NULL',
                'CREATE UNIQUE INDEX CONCURRENTLY "customers_code_key" '
                'ON "customers" ("code")',
                'ALTER TABLE "customers" ADD CONSTRAINT "customers_code_key" '
                'UNIQUE USING INDEX "customers_code_key"',
                'CREATE INDEX CONCURRENTLY "ix_orders_status" ON "orders" ("status")',
                'ALTER TABLE "orders" ADD CONSTRAINT "fk_orders_ref" '
                'FOREIGN KEY ("ref") REFERENCES "customers" ("code") NOT VALID',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "orders_customer_id_fkey"',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "ck_status"',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "fk_orders_ref"',
                'ALTER TABLE "orders" ADD CONSTRAINT "orders_email_not_null" '
                'CHECK ("email" IS NOT NULL) NOT VALID',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "orders_email_not_null"',
                'ALTER TABLE "orders" ALTER COLUMN "email" SET NOT NULL',
                'ALTER TABLE "orders" DROP CONSTRAINT "orders_email_not_null"',
                'ALTER TABLE "orders" ADD CONSTRAINT "orders_status_not_null" '
                'CHECK ("status" IS NOT NULL) NOT VALID',
                'ALTER TABLE "orders" VALIDATE CONSTRAINT "orders_status_not_null"',
                'ALTER TABLE "orders" ALTER COLUMN "status" SET NOT NULL',
                'ALTER TABLE "orders" DROP CONSTRAINT "orders_status_not_null"',
            ],
        )

    def test_the_online_down_step_leaves_what_the_ddl_migration_made(self):
        self.assertEqual(
            self.online.down,
            [
                'ALTER TABLE "orders" ALTER COLUMN "status" DROP NOT NULL',
                'ALTER TABLE "orders" ALTER COLUMN "email" DROP NOT NULL',
                'ALTER TABLE "orders" DROP CONSTRAINT "fk_orders_ref"',
                'DROP INDEX CONCURRENTLY "ix_orders_status"',
                'ALTER TABLE "customers" DROP CONSTRAINT "customers_code_key"',
            ],
        )

    def test_each_statement_carries_the_intent_of_its_form(self):
        kinds = [s.intent.kind for s in self.online.up]
        self.assertEqual(
            kinds[:4], ["backfill", "backfill", "create_index", "add_unique"]
        )
        self.assertEqual(kinds[6], "validate_constraint")
        self.assertEqual(
            kinds[-4:],
            ["add_check", "validate_constraint", "set_not_null", "drop_constraint"],
        )

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
            ['CREATE INDEX CONCURRENTLY "ix_orders_status" ON "orders" ("status")'],
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
                'DROP INDEX CONCURRENTLY "ix_old"',
                'ALTER TABLE "orders" DROP COLUMN "ref"',
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
        self.assertEqual(online.up, ['DROP INDEX CONCURRENTLY "ix_old"'])
        self.assertEqual(
            online.down,
            ['CREATE INDEX CONCURRENTLY "ix_old" ON "orders" ("email")'],
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
                'DROP INDEX CONCURRENTLY "ix_mail"',
                'CREATE INDEX CONCURRENTLY "ix_mail" ON "orders" ("email", "ref")',
            ],
        )
        self.assertEqual(
            online.down,
            [
                'DROP INDEX CONCURRENTLY "ix_mail"',
                'CREATE INDEX CONCURRENTLY "ix_mail" ON "orders" ("email")',
            ],
        )


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


if __name__ == "__main__":
    unittest.main()
