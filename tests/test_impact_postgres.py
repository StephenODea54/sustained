"""Tests for the PostgreSQL impact rules."""

import json
import pathlib
import unittest

from sustained.dialects import Dialects
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    Work,
    analyze,
)
from sustained.impact.context import FLOORS, assumed
from sustained.impact.rules import all_rules
from sustained.impact.rules import postgres as pg
from sustained.impact.rules import profile_for
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
)

PG = Dialects.POSTGRES
ROOT = pathlib.Path(__file__).resolve().parent.parent

# The fixture schema's two keyed tables, as the schema read reports them.
FIXTURE_SCHEMA = Snapshot(
    {
        "r": IntrospectedTable(
            {"id": IntrospectedColumn("integer", False, True)},
            primary_key=("id",),
            name="r",
        ),
        "t": IntrospectedTable(
            {
                "id": IntrospectedColumn("integer", False, True),
                "c": IntrospectedColumn("integer", True, False),
                "name": IntrospectedColumn("character varying(100)", True, False),
                "r_id": IntrospectedColumn("integer", True, False),
            },
            primary_key=("id",),
            foreign_keys={
                "t_r_id_fkey": IntrospectedForeignKey(
                    ("r_id",), "r", ("id",), name="t_r_id_fkey"
                )
            },
            indexes={"ix": IntrospectedIndex(("c",), True, name="ix")},
            checks={"ck": "c > 0", "name_present": "((name IS NOT NULL))"},
            check_names={"ck": "ck", "name_present": "name_present"},
            name="t",
        ),
    }
)

# The newest server, with the fixture schema read.
FIXTURE_CONTEXT = EngineContext("postgres", (18,), schema=FIXTURE_SCHEMA)


def impact(sql, context=None):
    """The impact of one statement, alone in a migration of its own."""
    (statement,) = analyze([sql], PG, context).statements
    return statement


def table(sql, name=None, context=None):
    statement = impact(sql, context)
    if name is None:
        (only,) = statement.tables
        return only
    return next(t for t in statement.tables if t.table == name)


def rules(statement):
    return [f.rule for f in statement.findings]


class LockTableTestCase(unittest.TestCase):
    """What each statement locks, and the work it does, table by table."""

    CASES = [
        # sql, lock, blocks, work, rule
        (
            "ALTER TABLE t ADD COLUMN c integer",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_column",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer NOT NULL DEFAULT 0",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_column",
        ),
        (
            "ALTER TABLE t ADD COLUMN c timestamptz DEFAULT now()",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_column",
        ),
        (
            "ALTER TABLE t ADD COLUMN c uuid DEFAULT gen_random_uuid()",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.add_column.rewrite",
        ),
        (
            "ALTER TABLE t ADD COLUMN c bigserial",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.add_column.rewrite",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer GENERATED ALWAYS AS IDENTITY",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.add_column.rewrite",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer GENERATED ALWAYS AS (id) STORED",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.add_column.rewrite",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer UNIQUE",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.INDEX_BUILD,
            "pg.add_column.key",
        ),
        (
            "ALTER TABLE t ADD COLUMN c integer CHECK (c > 0)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.SCAN,
            "pg.add_column.checked",
        ),
        (
            "ALTER TABLE t DROP COLUMN c",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_column",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c TYPE bigint",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.alter_column_type",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c SET NOT NULL",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.SCAN,
            "pg.set_not_null",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c DROP NOT NULL",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.alter_column.catalog",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c SET DEFAULT 1",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.alter_column.catalog",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c SET STATISTICS 100",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.CATALOG,
            "pg.set_statistics",
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.SCAN,
            "pg.add_check",
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0) NOT VALID",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_check.not_valid",
        ),
        (
            "ALTER TABLE t VALIDATE CONSTRAINT ck",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.SCAN,
            "pg.validate_constraint",
        ),
        (
            "ALTER TABLE t ADD PRIMARY KEY (id)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.INDEX_BUILD,
            "pg.add_key",
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE USING INDEX ix",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.add_key.using_index",
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT ex EXCLUDE USING gist (c WITH =)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.INDEX_BUILD,
            "pg.add_exclusion",
        ),
        (
            "ALTER TABLE t DROP CONSTRAINT ck",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_constraint",
        ),
        (
            "CREATE INDEX ix ON t (c)",
            "SHARE",
            Blocks.WRITES,
            Work.INDEX_BUILD,
            "pg.create_index",
        ),
        (
            "CREATE INDEX CONCURRENTLY ix ON t (c)",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.INDEX_BUILD,
            "pg.create_index.concurrently",
        ),
        (
            "DROP TABLE t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_table",
        ),
        (
            "TRUNCATE t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_table",
        ),
        (
            "DROP VIEW t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.drop_view",
        ),
        (
            "VACUUM FULL t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.vacuum_full",
        ),
        (
            "CLUSTER t USING ix",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.vacuum_full",
        ),
        (
            "VACUUM t",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.SCAN,
            "pg.vacuum",
        ),
        (
            "ANALYZE t",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.SCAN,
            "pg.vacuum",
        ),
        (
            "ALTER TABLE t SET TABLESPACE fast",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.table_rewrite",
        ),
        (
            "ALTER TABLE t SET LOGGED",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.table_rewrite",
        ),
        (
            "ALTER TABLE t SET (fillfactor = 70, autovacuum_enabled = false)",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.CATALOG,
            "pg.set_parameters",
        ),
        (
            "ALTER TABLE t SET (user_catalog_table = true)",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.alter_table.catalog",
        ),
        (
            "ALTER TABLE t OWNER TO someone",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.alter_table.catalog",
        ),
        (
            "ALTER TABLE t DISABLE TRIGGER tr",
            "SHARE ROW EXCLUSIVE",
            Blocks.WRITES,
            Work.CATALOG,
            "pg.alter_trigger",
        ),
        (
            "CREATE TRIGGER tr BEFORE UPDATE ON t FOR EACH ROW EXECUTE FUNCTION f()",
            "SHARE ROW EXCLUSIVE",
            Blocks.WRITES,
            Work.CATALOG,
            "pg.trigger",
        ),
        (
            "DROP TRIGGER tr ON t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.trigger",
        ),
        (
            "COMMENT ON COLUMN t.c IS 'x'",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.CATALOG,
            "pg.comment",
        ),
        (
            "UPDATE t SET c = 0",
            "ROW EXCLUSIVE",
            Blocks.WRITES,
            Work.ROWS,
            "pg.write_rows",
        ),
        (
            "DELETE FROM t WHERE c < 0",
            "ROW EXCLUSIVE",
            Blocks.WRITES,
            Work.ROWS,
            "pg.write_rows",
        ),
        (
            "INSERT INTO t (c) VALUES (1)",
            "ROW EXCLUSIVE",
            Blocks.DDL,
            Work.ROWS,
            "pg.insert",
        ),
        (
            "REINDEX TABLE t",
            "SHARE",
            Blocks.READS_AND_WRITES,
            Work.INDEX_BUILD,
            "pg.reindex",
        ),
        (
            "REINDEX TABLE CONCURRENTLY t",
            "SHARE UPDATE EXCLUSIVE",
            Blocks.DDL,
            Work.INDEX_BUILD,
            "pg.reindex.concurrently",
        ),
        (
            "REFRESH MATERIALIZED VIEW t",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.REWRITE,
            "pg.refresh_materialized_view",
        ),
        (
            "REFRESH MATERIALIZED VIEW CONCURRENTLY t",
            "EXCLUSIVE",
            Blocks.WRITES,
            Work.ROWS,
            "pg.refresh_materialized_view.concurrently",
        ),
        (
            "REFRESH MATERIALIZED VIEW t WITH NO DATA",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.refresh_materialized_view",
        ),
        (
            "LOCK TABLE t IN SHARE MODE",
            "SHARE",
            Blocks.WRITES,
            Work.CATALOG,
            "pg.lock_table",
        ),
        (
            "ALTER TABLE t RENAME CONSTRAINT a TO b",
            "ACCESS EXCLUSIVE",
            Blocks.READS_AND_WRITES,
            Work.CATALOG,
            "pg.rename",
        ),
    ]

    def test_each_statement(self):
        for sql, lock, blocks, work, rule in self.CASES:
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual(found.table, "t")
                self.assertEqual(found.lock, lock)
                self.assertEqual(found.blocks, blocks)
                self.assertEqual(found.work, work)
                self.assertEqual(found.rule, rule)


class RuleCatalogTestCase(unittest.TestCase):
    def test_every_rule_has_a_source_and_a_fixture_that_reaches_it(self):
        reached = set()
        for rule in profile_for(PG).rules:
            self.assertTrue(rule.source.startswith("https://"), rule.id)
            self.assertTrue(rule.fixtures, rule.id)
            self.assertTrue(rule.id.startswith("pg."), rule.id)
            for fixture in rule.fixtures:
                statement = impact(fixture, FIXTURE_CONTEXT)
                found = {t.rule for t in statement.tables} | set(rules(statement))
                if rule.id in found:
                    reached.add(rule.id)
                self.assertNotEqual(statement.confidence, Confidence.UNKNOWN, fixture)
        self.assertEqual(reached, {rule.id for rule in profile_for(PG).rules})

    def test_rule_ids_are_unique(self):
        ids = [rule.id for rule in all_rules()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_floor_matches_the_support_table(self):
        support = json.loads((ROOT / "support.json").read_text())
        row = next(r for r in support["databases"] if r["name"] == "postgres")
        self.assertEqual(
            FLOORS["postgres"], tuple(int(p) for p in row["floor"].split("."))
        )
        self.assertEqual(assumed("postgres").version, (12,))

    def test_lock_order_and_blocks(self):
        self.assertEqual(pg.lock_rank(None), -1)
        self.assertEqual(pg.blocks(None), Blocks.NOTHING)
        ranks = [pg.lock_rank(lock) for lock in pg.LOCKS]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(pg.blocks("ACCESS SHARE"), Blocks.DDL)
        self.assertEqual(pg.blocks("EXCLUSIVE"), Blocks.WRITES)


if __name__ == "__main__":
    unittest.main()
