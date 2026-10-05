"""
Tests for the SQL Server rules, the context read, the edition and
version conditions, and the trace's observations.
"""

import json
import unittest
from pathlib import Path
from types import MappingProxyType

from sustained.analysis import MigrationStatement, with_intent
from sustained.dialects import Dialects
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    Evidence,
    Severity,
    TableStats,
    Work,
    analyze,
)
from sustained.impact.context import FLOORS, assumed
from sustained.impact.model import Intent
from sustained.impact.report import render, summary
from sustained.impact.rules import profile_for
from sustained.impact.rules.mssql import (
    LOCKS,
    PROFILE,
    blocks,
    context_plan,
    lock_rank,
    release,
    server_version,
    timeout_statement,
)
from sustained.impact.rules.mssql.alter import column_type
from sustained.impact.rules.mssql.locks import bounded
from sustained.impact.rules.mssql.trace import (
    Partition,
    Sighting,
    observe,
    sighting_plan,
    tables_plan,
)
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedTable,
    Snapshot,
)
from tests.test_impact_context import drive

ROOT = Path(__file__).resolve().parent.parent
MSSQL = Dialects.MSSQL


def schema():
    """t with a nullable int, a NOT NULL int, and varchar columns; r; p."""
    return Snapshot(
        {
            "t": IntrospectedTable(
                {
                    "id": IntrospectedColumn("int", False, True),
                    "n": IntrospectedColumn("int", True, False),
                    "m": IntrospectedColumn("int", False, False),
                    "v": IntrospectedColumn("varchar(10)", True, False),
                    "w": IntrospectedColumn("varchar(10)", False, False),
                    "name": IntrospectedColumn("nvarchar(50)", True, False),
                    "big": IntrospectedColumn("nvarchar(max)", True, False),
                    "d": IntrospectedColumn("decimal(10,2)", True, False),
                    "r_id": IntrospectedColumn("int", True, False),
                },
                primary_key=("id",),
                foreign_keys={
                    "fk_t_r": IntrospectedForeignKey(
                        ("r_id",), "r", ("id",), name="fk_t_r"
                    )
                },
                name="t",
            ),
            "r": IntrospectedTable(
                {"id": IntrospectedColumn("int", False, True)}, name="r"
            ),
        }
    )


def context(
    engine="3",
    edition="Developer Edition (64-bit)",
    version=(16, 0, 4135, 4),
    snapshot="off",
    rows=6000,
    heap=False,
    clustered="pk_t",
    lock_timeout="-1",
):
    """A read SQL Server with the edition, settings, and t's size given."""
    stats = TableStats(rows, rows * 64, heap=heap, clustered=clustered)
    settings = {"lock_timeout": lock_timeout, "read_committed_snapshot": snapshot}
    if engine is not None:
        settings["EngineEdition"] = engine
    return EngineContext(
        "mssql",
        version,
        edition if engine is not None else None,
        MappingProxyType(settings),
        MappingProxyType(
            {
                "t": stats,
                "h": TableStats(6000, 1 << 20, heap=True),
                "k": TableStats(6000, 1 << 20, heap=False, clustered="cx_k"),
            }
        ),
        schema(),
        frozenset({"version", "edition", "settings", "sizes", "clustered", "schema"}),
    )


STANDARD = dict(engine="2", edition="Standard Edition (64-bit)")


def impact(sql, ctx=None, transactional=True, intent=None):
    statement = MigrationStatement(sql, "001", transactional)
    if intent is not None:
        statement = with_intent(
            statement, intent.kind, intent.table, intent.column, **intent.details
        )
    (found,) = analyze([statement], MSSQL, ctx).statements
    return found


def table(sql, ctx=None, name="t", **kwargs):
    found = impact(sql, ctx, **kwargs)
    return next(t for t in found.tables if t.table == name)


def rules(statement):
    return [f.rule for f in statement.findings]


def finding(statement, rule):
    return next(f for f in statement.findings if f.rule == rule)


class AddColumnTestCase(unittest.TestCase):
    def test_a_nullable_column_changes_the_catalog(self):
        for sql in (
            "ALTER TABLE t ADD d int NULL",
            "ALTER TABLE t ADD d int NULL DEFAULT 0",
            "ALTER TABLE t ADD d AS (n * 2)",
        ):
            with self.subTest(sql=sql):
                found = table(sql)
                self.assertEqual((found.lock, found.work), ("Sch-M", Work.CATALOG))
                self.assertIs(found.blocks, Blocks.READS_AND_WRITES)
                self.assertEqual(found.rule, "mssql.add_column")
                self.assertNotIn("mssql.add_column", rules(impact(sql)))

    def test_a_not_null_column_without_a_default_is_refused_on_rows(self):
        found = impact("ALTER TABLE t ADD d int NOT NULL")
        self.assertEqual(found.tables[0].rule, "mssql.add_column")
        note = finding(found, "mssql.add_column")
        self.assertIs(note.severity, Severity.WARN)
        self.assertIn("refuses", note.message)

    def test_a_filled_default_is_catalog_on_the_enterprise_editions(self):
        for sql in (
            "ALTER TABLE t ADD d int NOT NULL DEFAULT 0",
            "ALTER TABLE t ADD d int NULL DEFAULT 0 WITH VALUES",
            "ALTER TABLE t ADD d datetime2 NOT NULL DEFAULT SYSDATETIME()",
        ):
            with self.subTest(sql=sql):
                found = table(sql, context())
                self.assertEqual(found.work, Work.CATALOG)
                self.assertEqual(found.rule, "mssql.add_column.default")
                standard = impact(sql, context(**STANDARD))
                self.assertEqual(standard.tables[0].work, Work.REWRITE)
                self.assertIs(standard.confidence, Confidence.KNOWN)
                message = finding(standard, "mssql.add_column.default").message
                self.assertIn("Standard Edition", message)

    def test_without_the_edition_a_filled_default_is_assumed_to_write_every_row(self):
        found = impact("ALTER TABLE t ADD d int NOT NULL DEFAULT 0")
        self.assertEqual(found.tables[0].work, Work.REWRITE)
        self.assertIs(found.confidence, Confidence.LIKELY)
        message = finding(found, "mssql.add_column.default").message
        self.assertIn("the edition was not read", message)
        unread = context(engine=None)
        self.assertIs(
            impact("ALTER TABLE t ADD d int NOT NULL DEFAULT 0", unread).confidence,
            Confidence.LIKELY,
        )

    def test_a_value_per_row_writes_every_row(self):
        for sql in (
            "ALTER TABLE t ADD d uniqueidentifier NOT NULL DEFAULT NEWID()",
            "ALTER TABLE t ADD d int IDENTITY",
            "ALTER TABLE t ADD d AS (n * 2) PERSISTED",
        ):
            with self.subTest(sql=sql):
                found = table(sql, context())
                self.assertEqual(found.work, Work.REWRITE)
                self.assertEqual(found.rule, "mssql.add_column.rewrite")
        unknown = impact(
            "ALTER TABLE t ADD d int NOT NULL DEFAULT dbo.next_id()", context()
        )
        self.assertIs(unknown.confidence, Confidence.LIKELY)
        self.assertIn("next_id", finding(unknown, "mssql.add_column.rewrite").message)

    def test_a_rowversion_column_writes_every_row(self):
        for sql in (
            "ALTER TABLE t ADD d rowversion",
            "ALTER TABLE t ADD d rowversion NULL",
            "ALTER TABLE t ADD d timestamp",
        ):
            with self.subTest(sql=sql):
                found = impact(sql, context())
                self.assertEqual(found.tables[0].work, Work.REWRITE)
                self.assertEqual(found.tables[0].rule, "mssql.add_column.rewrite")
                self.assertIs(found.confidence, Confidence.KNOWN)
                self.assertIn(
                    "into every row",
                    finding(found, "mssql.add_column.rewrite").message,
                )

    def test_a_large_type_default_writes_every_row_on_every_edition(self):
        for kind, default in (
            ("nvarchar(max)", "N'x'"),
            ("varchar(max)", "'x'"),
            ("varbinary(max)", "0x01"),
            ("xml", "N'<a/>'"),
            ("text", "'x'"),
            ("ntext", "N'x'"),
            ("image", "0x01"),
            ("hierarchyid", "'/'"),
            ("geography", "geography::Point(0, 0, 4326)"),
            ("geometry", "geometry::Point(0, 0, 0)"),
            ("json", "'{}'"),
        ):
            sql = f"ALTER TABLE t ADD d {kind} NOT NULL DEFAULT {default}"
            for ctx in (context(), context(**STANDARD)):
                with self.subTest(sql=sql, edition=ctx.edition):
                    found = impact(sql, ctx)
                    self.assertEqual(found.tables[0].work, Work.REWRITE)
                    self.assertIs(found.confidence, Confidence.KNOWN)
                    rewrite = finding(found, "mssql.add_column.rewrite")
                    self.assertIn("on every edition", rewrite.message)
                    self.assertIn("backfill t in batches", rewrite.remedy[0])
        with_values = impact(
            "ALTER TABLE t ADD d nvarchar(max) NULL DEFAULT N'x' WITH VALUES", context()
        )
        self.assertEqual(with_values.tables[0].rule, "mssql.add_column.rewrite")
        nullable = table(
            "ALTER TABLE t ADD d nvarchar(max) NULL DEFAULT N'x'", context()
        )
        self.assertEqual(
            (nullable.work, nullable.rule), (Work.CATALOG, "mssql.add_column")
        )

    def test_a_system_type_default_stays_catalog(self):
        for kind, default in (
            ("nvarchar(4000)", "N'x'"),
            ("sql_variant", "1"),
            ("sysname", "N'x'"),
            ("decimal(10, 2)", "0"),
        ):
            sql = f"ALTER TABLE t ADD d {kind} NOT NULL DEFAULT {default}"
            with self.subTest(sql=sql):
                found = table(sql, context())
                self.assertEqual(
                    (found.work, found.rule), (Work.CATALOG, "mssql.add_column.default")
                )

    def test_an_alias_or_clr_type_default_likely_writes_every_row(self):
        found = impact("ALTER TABLE t ADD d dbo.mytype NOT NULL DEFAULT 0", context())
        self.assertEqual(found.tables[0].work, Work.REWRITE)
        self.assertIs(found.confidence, Confidence.LIKELY)
        self.assertIn(
            "CLR type, which was not read",
            finding(found, "mssql.add_column.rewrite").message,
        )

    def test_drop_column_changes_the_catalog_and_keeps_the_space(self):
        found = impact("ALTER TABLE t DROP COLUMN v", context())
        self.assertEqual(found.tables[0].work, Work.CATALOG)
        self.assertIn("until the table", finding(found, "mssql.drop_column").message)


class AlterColumnTestCase(unittest.TestCase):
    def work(self, sql, ctx=None):
        found = table(sql, ctx or context())
        return found.work, found.rule

    def test_a_longer_variable_length_changes_the_catalog(self):
        self.assertEqual(
            self.work("ALTER TABLE t ALTER COLUMN name nvarchar(100)"),
            (Work.CATALOG, "mssql.alter_column.metadata"),
        )
        self.assertEqual(
            self.work("ALTER TABLE t ALTER COLUMN big nvarchar(max)"),
            (Work.CATALOG, "mssql.alter_column.metadata"),
        )

    def test_dropping_not_null_changes_the_catalog(self):
        self.assertEqual(
            self.work("ALTER TABLE t ALTER COLUMN m int NULL"),
            (Work.CATALOG, "mssql.alter_column.metadata"),
        )
        # Restating a NOT NULL column as it is changes nothing either.
        self.assertEqual(
            self.work("ALTER TABLE t ALTER COLUMN m int NOT NULL"),
            (Work.CATALOG, "mssql.alter_column.metadata"),
        )

    def test_any_other_change_writes_every_row(self):
        for sql in (
            "ALTER TABLE t ALTER COLUMN n bigint",
            "ALTER TABLE t ALTER COLUMN v varchar(5)",
            "ALTER TABLE t ALTER COLUMN v nvarchar(10)",
            "ALTER TABLE t ALTER COLUMN name nvarchar(max)",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.work(sql), (Work.REWRITE, "mssql.alter_column"))
        found = impact("ALTER TABLE t ALTER COLUMN n bigint", context())
        self.assertIs(found.confidence, Confidence.KNOWN)
        self.assertIn("updates every row", finding(found, "mssql.alter_column").message)

    def test_a_precision_change_is_likely_a_rewrite(self):
        found = impact("ALTER TABLE t ALTER COLUMN d decimal(12,2)", context())
        self.assertEqual(found.tables[0].work, Work.REWRITE)
        self.assertIs(found.confidence, Confidence.LIKELY)

    def test_not_null_scans_a_fixed_length_column_and_rewrites_a_variable_one(self):
        self.assertEqual(
            self.work("ALTER TABLE t ALTER COLUMN n int NOT NULL"),
            (Work.SCAN, "mssql.set_not_null"),
        )
        self.assertEqual(
            self.work("ALTER TABLE t ALTER COLUMN v varchar(10) NOT NULL"),
            (Work.REWRITE, "mssql.set_not_null"),
        )
        found = impact("ALTER TABLE t ALTER COLUMN n int NOT NULL", context())
        self.assertIn("reads every row", finding(found, "mssql.set_not_null").message)

    def test_an_unread_type_is_likely_a_rewrite(self):
        found = impact("ALTER TABLE x ALTER COLUMN c varchar(20)", context())
        self.assertEqual(found.tables[0].work, Work.REWRITE)
        self.assertIs(found.confidence, Confidence.LIKELY)
        self.assertIn("the current type was not read", found.findings[0].message)

    def test_the_intent_names_the_current_type_and_nullability(self):
        intent = Intent(
            "set_not_null", "x", "c", MappingProxyType({"from_type": "INT"})
        )
        found = impact("ALTER TABLE x ALTER COLUMN c INT NOT NULL", intent=intent)
        self.assertEqual(found.tables[0].work, Work.SCAN)
        self.assertIs(found.confidence, Confidence.KNOWN)
        intent = Intent("set_not_null", "x", "c")
        found = impact("ALTER TABLE x ALTER COLUMN c INT NOT NULL", intent=intent)
        self.assertIs(found.confidence, Confidence.LIKELY)
        # With the type but not the nullability, NOT NULL may be new.
        schemaless = context()._replace(
            schema=Snapshot(
                {
                    "x": IntrospectedTable(
                        {"c": IntrospectedColumn("nvarchar(5)", True, False)}, name="x"
                    )
                }
            )
        )
        found = impact("ALTER TABLE x ALTER COLUMN c nvarchar(5) NOT NULL", schemaless)
        self.assertEqual(found.tables[0].work, Work.REWRITE)

    def test_unknown_nullability_is_likely(self):
        intent = Intent(
            "alter_column_type", "x", "c", MappingProxyType({"from_type": "INT"})
        )
        found = impact("ALTER TABLE x ALTER COLUMN c INT NOT NULL", intent=intent)
        self.assertEqual(found.tables[0].work, Work.SCAN)
        self.assertIs(found.confidence, Confidence.LIKELY)

    def test_the_remedy_is_the_online_form_from_2016(self):
        found = impact("ALTER TABLE t ALTER COLUMN n bigint", context())
        self.assertEqual(
            finding(found, "mssql.alter_column").remedy,
            ("ALTER TABLE t ALTER COLUMN n bigint WITH (ONLINE = ON)",),
        )
        old = impact("ALTER TABLE t ALTER COLUMN n bigint", context(version=(12, 0)))
        self.assertEqual(finding(old, "mssql.alter_column").remedy, ())
        standard = impact("ALTER TABLE t ALTER COLUMN n bigint", context(**STANDARD))
        self.assertEqual(finding(standard, "mssql.alter_column").remedy, ())

    def test_online_alter_column(self):
        sql = "ALTER TABLE t ALTER COLUMN n bigint WITH (ONLINE = ON)"
        found = impact(sql, context())
        (t,) = found.tables
        self.assertEqual(
            (t.lock, t.work, t.rule),
            ("Sch-M", Work.REWRITE, "mssql.alter_column.online"),
        )
        online = finding(found, "mssql.alter_column.online")
        self.assertIs(online.severity, Severity.INFO)
        self.assertIn("held until the migration commits", online.message)
        outside = impact(sql, context(), transactional=False)
        self.assertEqual(outside.tables[0].lock, "Sch-M")
        self.assertIs(outside.tables[0].blocks, Blocks.DDL)
        self.assertIn("mssql.lock_timeout", rules(outside))
        # ALTER COLUMN refuses WAIT_AT_LOW_PRIORITY, so no remedy offers it.
        self.assertEqual(finding(outside, "mssql.alter_column.online").remedy, ())
        old = impact(sql, context(version=(12, 0)))
        self.assertIn(
            "2016 or later", finding(old, "mssql.alter_column.online").message
        )
        standard = impact(sql, context(**STANDARD))
        edition = finding(standard, "mssql.online.edition")
        self.assertIs(edition.severity, Severity.DANGER)
        self.assertIn("fails on Standard Edition", edition.message)
        unread = finding(impact(sql), "mssql.online.edition")
        self.assertIs(unread.severity, Severity.INFO)


class ConstraintTestCase(unittest.TestCase):
    def test_a_check_scans_and_nocheck_changes_the_catalog(self):
        found = impact("ALTER TABLE t ADD CONSTRAINT ck CHECK (n > 0)", context())
        self.assertEqual(found.tables[0].work, Work.SCAN)
        self.assertEqual(
            finding(found, "mssql.add_check").remedy,
            ("ALTER TABLE t WITH NOCHECK ADD CONSTRAINT ck CHECK (n > 0)",),
        )
        nocheck = impact(
            "ALTER TABLE t WITH NOCHECK ADD CONSTRAINT ck CHECK (n > 0)", context()
        )
        self.assertEqual(nocheck.tables[0].work, Work.CATALOG)
        self.assertIn("untrusted", finding(nocheck, "mssql.add_check.nocheck").message)
        checked = impact(
            "ALTER TABLE t WITH CHECK ADD CONSTRAINT ck CHECK (n > 0)", context()
        )
        self.assertEqual(checked.tables[0].work, Work.SCAN)

    def test_a_foreign_key_locks_both_tables(self):
        found = impact(
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (r_id) REFERENCES r (id)",
            context(),
        )
        self.assertEqual(
            {(t.table, t.lock, t.work) for t in found.tables},
            {("t", "Sch-M", Work.SCAN), ("r", "Sch-M", Work.CATALOG)},
        )
        nocheck = impact(
            "ALTER TABLE t WITH NOCHECK ADD CONSTRAINT fk FOREIGN KEY (r_id) "
            "REFERENCES r (id)",
            context(),
        )
        self.assertEqual({t.work for t in nocheck.tables}, {Work.CATALOG})
        self.assertIn("mssql.add_foreign_key.nocheck", rules(nocheck))

    def test_check_constraint_scans_only_with_check(self):
        found = impact("ALTER TABLE t WITH CHECK CHECK CONSTRAINT fk_t_r", context())
        self.assertEqual(
            {(t.table, t.work, t.rule) for t in found.tables},
            {
                ("t", Work.SCAN, "mssql.check_constraint"),
                ("r", Work.CATALOG, "mssql.check_constraint"),
            },
        )
        for sql in (
            "ALTER TABLE t CHECK CONSTRAINT fk_t_r",
            "ALTER TABLE t NOCHECK CONSTRAINT fk_t_r",
            "ALTER TABLE t NOCHECK CONSTRAINT ALL",
        ):
            with self.subTest(sql=sql):
                statement = impact(sql, context())
                self.assertEqual({t.work for t in statement.tables}, {Work.CATALOG})
                self.assertEqual(statement.tables[0].rule, "mssql.constraint_state")
        self.assertEqual(
            len(impact("ALTER TABLE t NOCHECK CONSTRAINT ALL", context()).tables), 1
        )

    def test_a_key_builds_an_index_or_copies_a_heap(self):
        unique = impact("ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name)", context())
        self.assertEqual(
            (unique.tables[0].lock, unique.tables[0].work), ("Sch-M", Work.INDEX_BUILD)
        )
        self.assertEqual(
            finding(unique, "mssql.add_key").remedy,
            ("ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name) WITH (ONLINE = ON)",),
        )
        heap = table("ALTER TABLE h ADD CONSTRAINT pk PRIMARY KEY (id)", context(), "h")
        self.assertEqual(heap.work, Work.REWRITE)
        nonclustered = table(
            "ALTER TABLE h ADD CONSTRAINT pk PRIMARY KEY NONCLUSTERED (id)",
            context(),
            "h",
        )
        self.assertEqual(nonclustered.work, Work.INDEX_BUILD)
        clustered = table(
            "ALTER TABLE t ADD CONSTRAINT pk2 PRIMARY KEY (id)", context()
        )
        # t already has a clustered index, so the key is nonclustered.
        self.assertEqual(clustered.work, Work.INDEX_BUILD)
        unread = impact("ALTER TABLE x ADD CONSTRAINT pk PRIMARY KEY (id)")
        self.assertEqual(unread.tables[0].work, Work.REWRITE)
        self.assertIs(unread.confidence, Confidence.LIKELY)
        standard = impact(
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name)", context(**STANDARD)
        )
        self.assertEqual(finding(standard, "mssql.add_key").remedy, ())

    def test_an_online_key(self):
        found = impact(
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name) WITH (ONLINE = ON)",
            context(),
        )
        self.assertEqual(found.tables[0].rule, "mssql.add_key.online")
        self.assertIs(finding(found, "mssql.add_key.online").severity, Severity.INFO)

    def test_dropping_a_constraint(self):
        check = impact("ALTER TABLE t DROP CONSTRAINT ck_t", context())
        self.assertEqual(
            [(t.table, t.work) for t in check.tables], [("t", Work.CATALOG)]
        )
        key = impact("ALTER TABLE t DROP CONSTRAINT fk_t_r", context())
        self.assertEqual({t.table for t in key.tables}, {"t", "r"})
        clustered = impact("ALTER TABLE t DROP CONSTRAINT pk_t", context())
        self.assertEqual(clustered.tables[0].work, Work.REWRITE)
        self.assertIn(
            "into a heap", finding(clustered, "mssql.drop_constraint").message
        )

    def test_defaults_change_the_catalog(self):
        for sql in (
            "ALTER TABLE t ADD DEFAULT 5 FOR n",
            "ALTER TABLE t ADD CONSTRAINT df DEFAULT 5 FOR n",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(table(sql).rule, "mssql.default")

    def test_the_dynamic_default_drop_follows_its_intent(self):
        sql = (
            "DECLARE @sustained_default nvarchar(max); SELECT @sustained_default = "
            "N'ALTER TABLE [widgets] DROP CONSTRAINT ' + QUOTENAME(dc.name) FROM "
            "sys.default_constraints dc; EXEC sp_executesql @sustained_default"
        )
        found = impact(sql, intent=Intent("drop_column_default", "widgets", "size"))
        self.assertIsNot(found.confidence, Confidence.UNKNOWN)
        self.assertEqual(
            [(t.table, t.lock, t.rule) for t in found.tables],
            [("widgets", "Sch-M", "mssql.default")],
        )
        self.assertIn("impact.from_intent", rules(found))
        self.assertIs(impact(sql).confidence, Confidence.UNKNOWN)


class IndexTestCase(unittest.TestCase):
    def test_a_nonclustered_build_holds_s(self):
        found = impact("CREATE INDEX ix2 ON t (name)", context())
        (t,) = found.tables
        self.assertEqual(
            (t.lock, t.blocks, t.work), ("S", Blocks.WRITES, Work.INDEX_BUILD)
        )
        self.assertEqual(
            finding(found, "mssql.create_index").remedy,
            ("CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON)",),
        )
        outside = impact("CREATE INDEX ix2 ON t (name)", context(), transactional=False)
        self.assertEqual(
            finding(outside, "mssql.create_index").remedy,
            ("CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON, RESUMABLE = ON)",),
        )
        before_2019 = impact(
            "CREATE INDEX ix2 ON t (name)",
            context(version=(14, 0)),
            transactional=False,
        )
        self.assertEqual(
            finding(before_2019, "mssql.create_index").remedy,
            ("CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON)",),
        )
        standard = impact("CREATE INDEX ix2 ON t (name)", context(**STANDARD))
        self.assertEqual(finding(standard, "mssql.create_index").remedy, ())

    def test_an_online_build_takes_its_lock_at_the_end(self):
        found = impact(
            "CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON)", context(rows=10**8)
        )
        (t,) = found.tables
        self.assertEqual((t.lock, t.rule), ("S", "mssql.create_index.online"))
        # A large table does not make it danger, since the build runs online.
        self.assertIs(
            finding(found, "mssql.create_index.online").severity, Severity.INFO
        )

    def test_an_online_build_outside_a_transaction_waits_for_its_locks(self):
        sql = "CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON)"
        found = impact(sql, context(), transactional=False)
        (t,) = found.tables
        self.assertEqual((t.lock, t.blocks), ("S", Blocks.DDL))
        self.assertIn("mssql.lock_timeout", rules(found))
        note = finding(found, "mssql.create_index.online")
        self.assertIs(note.severity, Severity.INFO)
        self.assertIn("when it starts", note.message)
        self.assertEqual(
            note.remedy,
            (
                "CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY "
                "(MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)))",
            ),
        )
        before_2022 = impact(sql, context(version=(15, 0)), transactional=False)
        self.assertEqual(finding(before_2022, "mssql.create_index.online").remedy, ())
        rebuild = impact(
            "ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON)",
            context(version=(12, 0)),
            transactional=False,
        )
        self.assertEqual(rebuild.tables[0].lock, "Sch-M")
        self.assertIn(
            "WAIT_AT_LOW_PRIORITY", finding(rebuild, "mssql.rebuild").remedy[0]
        )
        self.assertIn("mssql.lock_timeout", rules(rebuild))
        key = impact(
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name) WITH (ONLINE = ON)",
            context(),
            transactional=False,
        )
        self.assertEqual(key.tables[0].lock, "Sch-M")
        self.assertEqual(finding(key, "mssql.add_key.online").remedy, ())

    def test_a_low_priority_wait_that_gives_up_needs_no_timeout(self):
        for abort in ("SELF", "BLOCKERS"):
            wait = (
                f"WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, "
                f"ABORT_AFTER_WAIT = {abort})"
            )
            for sql in (
                f"CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON ({wait}))",
                f"ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON ({wait}))",
            ):
                for transactional in (True, False):
                    with self.subTest(sql=sql, transactional=transactional):
                        found = impact(sql, context(), transactional=transactional)
                        self.assertNotIn("mssql.lock_timeout", rules(found))
                        # The statement spells the wait, so no remedy adds it.
                        for note in found.findings:
                            for remedy in note.remedy:
                                self.assertEqual(
                                    remedy.count("WAIT_AT_LOW_PRIORITY"), 1
                                )
            switch = impact(f"ALTER TABLE t SWITCH TO t9 WITH ({wait})", context())
            self.assertNotIn("mssql.lock_timeout", rules(switch))
        waits = (
            "WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = NONE)"
        )
        found = impact(
            f"CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON ({waits}))", context()
        )
        self.assertIn("mssql.lock_timeout", rules(found))

    def test_resumable_is_refused_inside_a_transaction(self):
        for sql in (
            "CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON, RESUMABLE = ON)",
            "ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON, RESUMABLE = ON)",
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name) "
            "WITH (ONLINE = ON, RESUMABLE = ON)",
        ):
            with self.subTest(sql=sql):
                inside = finding(impact(sql, context()), "mssql.resumable")
                self.assertIs(inside.severity, Severity.DANGER)
                self.assertIn("transactional=False", inside.message)
                outside = impact(sql, context(), transactional=False)
                self.assertNotIn("mssql.resumable", rules(outside))
        offline = impact(
            "CREATE INDEX ix2 ON t (name) WITH (RESUMABLE = ON)",
            context(),
            transactional=False,
        )
        refused = finding(offline, "mssql.resumable")
        self.assertIs(refused.severity, Severity.DANGER)
        self.assertIn("needs ONLINE = ON", refused.message)

    def test_a_clustered_index_copies_the_heap(self):
        found = table("CREATE CLUSTERED INDEX cx ON h (id)", context(), "h")
        self.assertEqual((found.lock, found.work), ("Sch-M", Work.REWRITE))
        self.assertEqual(found.rule, "mssql.create_index.clustered")
        online = table(
            "CREATE CLUSTERED INDEX cx ON h (id) WITH (ONLINE = ON)", context(), "h"
        )
        self.assertEqual(online.lock, "Sch-M")

    def test_drop_index(self):
        found = table("DROP INDEX ix ON t", context())
        self.assertEqual(
            (found.lock, found.work, found.rule),
            ("Sch-M", Work.CATALOG, "mssql.drop_index"),
        )
        self.assertEqual(table("DROP INDEX t.ix", context()).rule, "mssql.drop_index")
        clustered = table("DROP INDEX pk_t ON t", context())
        self.assertEqual(
            (clustered.work, clustered.rule),
            (Work.REWRITE, "mssql.drop_index.clustered"),
        )
        nameless = impact("DROP INDEX ix")
        self.assertEqual(nameless.tables[0].table, "(table of index ix)")

    def test_rebuild(self):
        cases = (
            ("ALTER INDEX ix ON t REBUILD", Work.INDEX_BUILD),
            ("ALTER INDEX ALL ON t REBUILD", Work.REWRITE),
            ("ALTER INDEX pk_t ON t REBUILD", Work.REWRITE),
            ("ALTER TABLE t REBUILD", Work.REWRITE),
        )
        for sql, work in cases:
            with self.subTest(sql=sql):
                found = table(sql, context())
                self.assertEqual(
                    (found.lock, found.work, found.rule),
                    ("Sch-M", work, "mssql.rebuild"),
                )
        found = impact("ALTER INDEX ix ON t REBUILD", context())
        self.assertEqual(
            finding(found, "mssql.rebuild").remedy,
            (
                "ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY "
                "(MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)))",
            ),
        )
        outside = impact("ALTER INDEX ix ON t REBUILD", context(), transactional=False)
        self.assertTrue(
            finding(outside, "mssql.rebuild").remedy[0].endswith(", RESUMABLE = ON)")
        )
        floor = impact("ALTER INDEX ix ON t REBUILD", context(version=(11, 0)))
        self.assertEqual(
            finding(floor, "mssql.rebuild").remedy,
            ("ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON)",),
        )
        online = impact("ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON)", context())
        self.assertIs(finding(online, "mssql.rebuild").severity, Severity.INFO)
        table_online = impact("ALTER TABLE t REBUILD WITH (ONLINE = ON)", context())
        self.assertIs(finding(table_online, "mssql.rebuild").severity, Severity.INFO)
        self.assertEqual(
            finding(
                impact("ALTER TABLE t REBUILD", context(**STANDARD)), "mssql.rebuild"
            ).remedy,
            (),
        )

    def test_reorganize(self):
        found = table("ALTER INDEX ix ON t REORGANIZE", context())
        self.assertEqual((found.lock, found.work), ("X", Work.SCAN))
        self.assertIs(
            table("ALTER INDEX ix ON t REORGANIZE", context(snapshot="on")).blocks,
            Blocks.WRITES,
        )
        clustered = impact("ALTER INDEX ALL ON t REORGANIZE", context())
        self.assertEqual(clustered.tables[0].work, Work.REWRITE)
        self.assertIs(clustered.confidence, Confidence.LIKELY)
        outside = table(
            "ALTER INDEX ix ON t REORGANIZE", context(), transactional=False
        )
        self.assertEqual(outside.lock, "IX")

    def test_disable(self):
        found = impact("ALTER INDEX ix ON t DISABLE", context())
        self.assertEqual(found.tables[0].work, Work.CATALOG)
        clustered = impact("ALTER INDEX pk_t ON t DISABLE", context())
        self.assertIs(
            finding(clustered, "mssql.disable_index").severity, Severity.DANGER
        )

    def test_other_alter_index_operations_are_unknown(self):
        found = impact("ALTER INDEX ix ON t SET (ALLOW_PAGE_LOCKS = OFF)", context())
        self.assertIs(found.confidence, Confidence.UNKNOWN)


class StatementTestCase(unittest.TestCase):
    def test_catalog_changes_under_sch_m(self):
        cases = (
            ("EXEC sp_rename 't.name', 'label', 'COLUMN'", "t", "mssql.rename"),
            ("EXEC sp_rename 't', 't9'", "t", "mssql.rename"),
            ("EXEC sp_rename 't.ix', 'ix9', 'INDEX'", "t", "mssql.rename"),
            ("TRUNCATE TABLE t", "t", "mssql.truncate"),
            (
                "CREATE TRIGGER tr ON t AFTER INSERT AS BEGIN SET NOCOUNT ON END",
                "t",
                "mssql.trigger",
            ),
            ("ALTER TABLE t DISABLE TRIGGER tr", "t", "mssql.trigger"),
            ("ALTER TABLE t SWITCH TO u", "t", "mssql.switch"),
        )
        for sql, name, rule in cases:
            with self.subTest(sql=sql):
                found = table(sql, context(), name)
                self.assertEqual(
                    (found.lock, found.work, found.rule), ("Sch-M", Work.CATALOG, rule)
                )

    def test_renames_warn_running_code(self):
        found = impact("EXEC sp_rename 't.ix', 'ix9', 'INDEX'", context())
        self.assertIn("index ix", finding(found, "mssql.rename").message)
        found = impact("EXEC sp_rename 't', 't9'", context())
        self.assertIn("table t", finding(found, "mssql.rename").message)

    def test_switch_locks_both_tables_and_waits_at_low_priority(self):
        found = impact("ALTER TABLE t SWITCH TO u", context())
        self.assertEqual({t.table for t in found.tables}, {"t", "u"})
        self.assertEqual(
            [f.remedy for f in found.findings if f.rule == "mssql.switch"],
            [
                (
                    "ALTER TABLE t SWITCH TO u WITH (WAIT_AT_LOW_PRIORITY (MAX_DURATION "
                    "= 1 MINUTES, ABORT_AFTER_WAIT = SELF))",
                )
            ],
        )
        self.assertNotIn(
            "mssql.switch",
            rules(impact("ALTER TABLE t SWITCH TO u", context(version=(11, 0)))),
        )

    def test_drop_table_locks_the_tables_its_keys_point_at(self):
        found = impact("DROP TABLE t", context())
        self.assertEqual(
            {(t.table, t.lock) for t in found.tables}, {("t", "Sch-M"), ("r", "Sch-M")}
        )

    def test_create_table_locks_the_tables_its_keys_point_at(self):
        found = impact("CREATE TABLE n (id int, r_id int REFERENCES r (id))", context())
        self.assertEqual({t.table for t in found.tables}, {"n", "r"})
        self.assertNotIn(
            "mssql.lock_timeout", rules(impact("CREATE TABLE n (id int)", context()))
        )

    def test_a_trigger_the_schema_does_not_place_is_unknown(self):
        self.assertIs(
            impact("DROP TRIGGER tr", context()).confidence, Confidence.UNKNOWN
        )
        self.assertIs(impact("DROP TRIGGER tr").confidence, Confidence.UNKNOWN)

    def test_views_and_settings_lock_nothing(self):
        for sql in (
            "CREATE VIEW v AS SELECT id FROM t",
            "DROP VIEW v",
            "SET XACT_ABORT ON",
            "CREATE SCHEMA s",
        ):
            with self.subTest(sql=sql):
                found = impact(sql, context())
                self.assertEqual(found.tables, ())
                self.assertIsNot(found.confidence, Confidence.UNKNOWN)

    def test_update_statistics_holds_sch_s(self):
        found = table("UPDATE STATISTICS t WITH FULLSCAN", context())
        self.assertEqual(
            (found.lock, found.blocks, found.work), ("Sch-S", Blocks.DDL, Work.SCAN)
        )
        self.assertNotIn("mssql.lock_timeout", rules(impact("UPDATE STATISTICS t")))


class WriteRowsTestCase(unittest.TestCase):
    def test_a_write_holds_ix_and_row_locks(self):
        for sql in (
            "UPDATE t SET name = N'x' WHERE id < 3",
            "DELETE FROM t WHERE id > 5990",
            "INSERT INTO t (id) VALUES (9000)",
            "UPDATE TOP (100) t SET name = N'x'",
        ):
            with self.subTest(sql=sql):
                found = table(sql, context())
                self.assertEqual(
                    (found.lock, found.work, found.rule),
                    ("IX", Work.ROWS, "mssql.write_rows"),
                )
                self.assertIs(found.blocks, Blocks.READS_AND_WRITES)
        versioned = table(
            "UPDATE t SET name = N'x' WHERE id < 3", context(snapshot="on")
        )
        self.assertIs(versioned.blocks, Blocks.WRITES)
        found = impact("UPDATE t SET name = N'x' WHERE id < 3", context())
        self.assertIn("5,000 rows", finding(found, "mssql.lock_escalation").message)
        self.assertNotIn(
            "mssql.lock_escalation",
            rules(impact("INSERT INTO t (id) VALUES (1)", context())),
        )

    def test_an_insert_select_can_escalate(self):
        found = impact("INSERT INTO h SELECT id, c FROM t", context())
        t = next(t for t in found.tables if t.table == "h")
        self.assertEqual((t.lock, t.rule), ("IX", "mssql.write_rows"))
        note = finding(found, "mssql.lock_escalation")
        self.assertIs(note.severity, Severity.INFO)
        self.assertIn("INSERT ... SELECT", note.message)
        self.assertNotIn(
            "mssql.lock_escalation",
            rules(impact("INSERT INTO h (id) VALUES (1), (2)", context())),
        )

    def test_a_write_to_every_row_escalates(self):
        found = impact("DELETE FROM t", context())
        (t,) = found.tables
        self.assertEqual((t.lock, t.rule), ("X", "mssql.lock_escalation"))
        self.assertIn(
            "escalates its locks", finding(found, "mssql.lock_escalation").message
        )
        small = table("DELETE FROM t", context(rows=100))
        self.assertEqual((small.lock, small.rule), ("IX", "mssql.write_rows"))
        unread = impact("UPDATE t SET name = N'x'")
        self.assertEqual(unread.tables[0].lock, "X")
        self.assertIs(unread.confidence, Confidence.LIKELY)


class TimeoutTestCase(unittest.TestCase):
    def test_set_lock_timeout_covers_the_run(self):
        run = [
            MigrationStatement("SET LOCK_TIMEOUT 5000", "001"),
            MigrationStatement("ALTER TABLE t ADD d int NULL", "001"),
            MigrationStatement("ALTER TABLE t ADD e int NULL", "002"),
        ]
        report = analyze(run, MSSQL, context())
        self.assertNotIn("mssql.lock_timeout", [f.rule for f in report.findings])

    def test_minus_one_waits_for_ever(self):
        run = [
            MigrationStatement("SET LOCK_TIMEOUT -1", "001"),
            MigrationStatement("ALTER TABLE t ADD d int NULL", "001"),
        ]
        report = analyze(run, MSSQL, context())
        timeout = next(f for f in report.findings if f.rule == "mssql.lock_timeout")
        self.assertEqual(timeout.remedy, ("SET LOCK_TIMEOUT 5000",))

    def test_a_timeout_the_session_has_covers_the_run(self):
        found = impact("ALTER TABLE t ADD d int NULL", context(lock_timeout="3000"))
        self.assertNotIn("mssql.lock_timeout", rules(found))

    def test_bounded(self):
        self.assertTrue(bounded("0"))
        self.assertTrue(bounded("5000"))
        self.assertFalse(bounded("-1"))
        self.assertFalse(bounded("soon"))


class LocksTestCase(unittest.TestCase):
    def test_blocks_and_rank(self):
        self.assertIs(blocks(None), Blocks.NOTHING)
        self.assertEqual(
            [blocks(lock) for lock in LOCKS],
            [Blocks.DDL] * 3 + [Blocks.WRITES] * 2 + [Blocks.READS_AND_WRITES] * 2,
        )
        self.assertEqual([lock_rank(lock) for lock in LOCKS], list(range(len(LOCKS))))
        self.assertEqual(lock_rank(None), -1)
        self.assertEqual(lock_rank("RangeS-S"), -1)
        self.assertEqual(timeout_statement(True), "SET LOCK_TIMEOUT 5000")

    def test_release(self):
        self.assertEqual(release((16, 0, 4135, 4)), "2022 (16.0.4135.4)")
        self.assertEqual(release((11,)), "2012 (11)")
        self.assertEqual(release((18, 0)), "18.0")
        self.assertEqual(release(()), "")

    def test_column_type(self):
        self.assertEqual(column_type("NVARCHAR(50)").length, 50)
        self.assertEqual(column_type("varchar(max)").length, -1)
        self.assertEqual(column_type("varchar(-1)").length, -1)
        self.assertEqual(column_type("[int]").base, "int")
        self.assertIsNone(column_type("int").length)
        self.assertIsNone(column_type("decimal(p, 2)").length)
        self.assertEqual(column_type("double precision").base, "double precision")
        self.assertEqual(column_type("%").base, "%")


class ContextTestCase(unittest.TestCase):
    SERVER = ("16.0.4135.4", 3, "Developer Edition (64-bit)", -1)
    TABLES = [
        ("dbo", "t", 1, 6000, 42, 1, "pk_t"),
        ("audit", "t", 0, 5, 1, 0, None),
        ("dbo", "fresh", 1, None, None, 0, None),
    ]

    def test_reads_the_server_and_the_tables(self):
        asked, ctx = drive(context_plan(), [[self.SERVER], [(True,)], self.TABLES])
        self.assertEqual(len(asked), 3)
        self.assertEqual(ctx.version, (16, 0, 4135, 4))
        self.assertEqual(ctx.edition, "Developer Edition (64-bit)")
        self.assertEqual(
            dict(ctx.settings),
            {
                "EngineEdition": "3",
                "lock_timeout": "-1",
                "read_committed_snapshot": "on",
            },
        )
        self.assertEqual(
            ctx.stats("t"), TableStats(6000, 42 * 8192, heap=False, clustered="pk_t")
        )
        self.assertEqual(ctx.stats("dbo.t"), ctx.stats("t"))
        self.assertEqual(ctx.stats("audit.t"), TableStats(5, 8192, heap=True))
        self.assertEqual(ctx.stats("fresh"), TableStats(None, None, heap=True))
        self.assertEqual(
            ctx.read, {"version", "edition", "settings", "sizes", "clustered"}
        )

    def test_failed_reads_leave_their_facts_out(self):
        failure = RuntimeError("denied")
        _, ctx = drive(context_plan(exact_counts=True), [failure, failure, failure])
        self.assertEqual(ctx.version, FLOORS["mssql"])
        self.assertIsNone(ctx.edition)
        self.assertEqual(ctx.read, frozenset())
        _, ctx = drive(context_plan(), [failure, [(False,)], []])
        self.assertEqual(ctx.settings["read_committed_snapshot"], "off")
        self.assertEqual(ctx.read, {"settings", "sizes", "clustered"})

    def test_server_version(self):
        self.assertEqual(server_version("15.0.2000.5"), (15, 0, 2000, 5))
        self.assertEqual(server_version("unknown"), FLOORS["mssql"])

    def test_the_floor_matches_the_support_table(self):
        support = json.loads((ROOT / "support.json").read_text())
        row = next(r for r in support["databases"] if r["name"] == "mssql")
        # support.json names the release; the floor is its major version.
        self.assertEqual(release(FLOORS["mssql"]).split(" ")[0], row["floor"])
        self.assertEqual(assumed("mssql").version, FLOORS["mssql"])

    def test_the_report_names_the_release(self):
        report = analyze(["ALTER TABLE t ADD d int NULL"], MSSQL, context())
        self.assertTrue(summary(report).endswith("(SQL Server 2022 (16.0.4135.4))"))
        static = analyze(["ALTER TABLE t ADD d int NULL"], MSSQL)
        self.assertTrue(summary(static).endswith("(assumed SQL Server 2012 (11))"))
        self.assertIn("Sch-M", render(static))


class TraceTestCase(unittest.TestCase):
    """observe() over sightings built by hand, as the read plans return them."""

    def sighting(self, locks=None, storage=None, log=0, names=None):
        return Sighting(
            MappingProxyType({k: frozenset(v) for k, v in (locks or {}).items()}),
            MappingProxyType(names or {"t": 1, "dbo.t": 1, "r": 2, "u": 3}),
            MappingProxyType(
                {k: MappingProxyType(v) for k, v in (storage or {}).items()}
            ),
            log,
            frozenset({"locks", "storage", "log"}),
        )

    def storage(self, base=10, index=20, pages=40):
        return {
            (1, 1): Partition(True, base, pages),
            (2, 1): Partition(False, index, 10),
        }

    def observed(self, sql, before, after, ctx=None, existing=frozenset({1, 2, 3})):
        predicted = impact(sql, ctx or context())
        return observe(predicted, before, after, existing, PROFILE)

    def mismatches(self, statement):
        return [f.message for f in statement.findings if f.rule == "impact.mismatch"]

    def test_a_matching_observation(self):
        before = self.sighting(storage={1: self.storage()})
        after = self.sighting(
            {1: {"S", "Sch-S"}},
            {1: {**self.storage(), (3, 1): Partition(False, 30, 5)}},
        )
        found = self.observed("CREATE INDEX ix2 ON t (name)", before, after)
        self.assertIs(found.evidence, Evidence.OBSERVED)
        self.assertEqual(self.mismatches(found), [])
        self.assertEqual(found.tables[0].work, Work.INDEX_BUILD)

    def test_a_copy_seen_in_the_log(self):
        before = self.sighting(storage={1: self.storage()}, log=100)
        after = self.sighting(
            {1: {"Sch-M"}}, {1: self.storage()}, log=100 + 3 * 40 * 8192
        )
        found = self.observed(
            "ALTER TABLE t ALTER COLUMN name nvarchar(100)", before, after
        )
        self.assertEqual(found.tables[0].work, Work.REWRITE)
        self.assertEqual(
            self.mismatches(found),
            ["the rules predicted catalog on t, and the server rewrote it"],
        )

    def test_a_write_of_rows_is_not_a_copy(self):
        before = self.sighting(storage={1: self.storage()})
        after = self.sighting({1: {"X"}}, {1: self.storage()}, log=10**7)
        found = self.observed("UPDATE t SET name = N'x'", before, after)
        self.assertEqual(self.mismatches(found), [])

    def test_a_predicted_copy_that_copied_nothing(self):
        before = self.sighting(storage={1: self.storage()})
        after = self.sighting({1: {"Sch-M"}}, {1: self.storage()}, log=500)
        found = self.observed("ALTER TABLE t ALTER COLUMN n bigint", before, after)
        self.assertEqual(found.tables[0].work, Work.SCAN)
        self.assertEqual(
            self.mismatches(found),
            ["the rules predicted a rewrite on t, and the server copied nothing"],
        )

    def test_a_new_partition_for_the_rows(self):
        before = self.sighting(storage={1: self.storage()})
        after = self.sighting({1: {"Sch-M"}}, {1: self.storage(base=11)})
        found = self.observed("ALTER TABLE t REBUILD", before, after)
        self.assertEqual(self.mismatches(found), [])
        empty = self.sighting({1: {"Sch-M"}}, {1: self.storage(base=11, pages=0)})
        found = self.observed("TRUNCATE TABLE t", before, empty)
        self.assertEqual(found.tables[0].work, Work.CATALOG)
        self.assertEqual(self.mismatches(found), [])

    def test_a_switched_partition_is_no_copy(self):
        before = self.sighting(
            storage={1: self.storage(), 3: {(1, 1): Partition(True, 90, 0)}}
        )
        after = self.sighting(
            {1: {"Sch-M"}, 3: {"Sch-M"}},
            {1: {(1, 1): Partition(True, 90, 0)}, 3: {(1, 1): Partition(True, 10, 40)}},
        )
        found = self.observed("ALTER TABLE t SWITCH TO u", before, after)
        self.assertEqual(self.mismatches(found), [])

    def test_a_different_lock(self):
        before = self.sighting(storage={1: self.storage()})
        after = self.sighting({1: {"X"}}, {1: self.storage()})
        found = self.observed("UPDATE t SET name = N'x' WHERE id < 3", before, after)
        self.assertEqual(found.tables[0].lock, "X")
        self.assertEqual(
            self.mismatches(found),
            ["the rules predicted IX on t, and the server took X"],
        )

    def test_a_lock_released_at_the_end_is_no_mismatch(self):
        before = self.sighting(storage={1: self.storage()})
        after = self.sighting({}, {1: self.storage()})
        found = self.observed("UPDATE STATISTICS t", before, after)
        self.assertEqual(self.mismatches(found), [])
        self.assertEqual(found.tables[0].lock, "Sch-S")

    def test_an_unpredicted_lock(self):
        before = self.sighting(storage={1: self.storage()})
        after = self.sighting(
            {1: {"Sch-M"}, 2: {"Sch-M"}, 3: {"IS"}}, {1: self.storage()}
        )
        found = self.observed("ALTER TABLE t ADD d int NULL", before, after)
        self.assertEqual(
            self.mismatches(found),
            ["the server took Sch-M on r, which no rule predicted"],
        )

    def test_a_table_the_run_created_is_left_out(self):
        before = self.sighting(storage={1: self.storage()})
        after = self.sighting({1: {"Sch-M"}, 9: {"Sch-M"}}, {1: self.storage()})
        found = self.observed(
            "ALTER TABLE t ADD d int NULL", before, after, existing=frozenset({1})
        )
        self.assertEqual(self.mismatches(found), [])
        unnamed = self.observed(
            "ALTER TABLE zz ADD d int NULL", before, after, existing=frozenset({1})
        )
        self.assertEqual([t.table for t in unnamed.tables][0], "zz")

    def test_an_unread_sighting_leaves_the_prediction(self):
        predicted = impact("ALTER TABLE t ADD d int NULL", context())
        unread = Sighting()
        self.assertIs(observe(predicted, unread, unread, None, PROFILE), predicted)

    def test_the_read_plans(self):
        asked, existing = drive(tables_plan(), [[(1,), (2,)]])
        self.assertEqual(existing, frozenset({1, 2}))
        _, missing = drive(tables_plan(), [RuntimeError("denied")])
        self.assertIsNone(missing)
        locks = [
            (1, "dbo", "t", 1, "Sch-M"),
            (1, "dbo", "t", 1, "IX"),
            (7, None, None, 0, "Sch-M"),
        ]
        storage = [(1, "dbo", "t", 1, 1, 1, 10, 40), (1, "dbo", "t", 1, 2, 1, 20, None)]
        asked, seen = drive(sighting_plan(["[dbo].[t]"]), [locks, storage, [(4096,)]])
        self.assertIn("LOWER(t.name) IN (CONVERT(nvarchar(128), 0x7400))", asked[1])
        self.assertEqual(seen.locks[1], {"Sch-M", "IX"})
        self.assertEqual(seen.locks[7], {"Sch-M"})
        self.assertEqual(seen.names, {"dbo.t": 1, "t": 1})
        self.assertEqual(seen.storage[1][(2, 1)], Partition(False, 20, 0))
        self.assertEqual((seen.log, seen.read), (4096, {"locks", "storage", "log"}))
        failure = RuntimeError("denied")
        asked, seen = drive(sighting_plan([]), [failure, failure])
        self.assertEqual(len(asked), 2)
        self.assertEqual(seen.read, frozenset())


class RuleCatalogTestCase(unittest.TestCase):
    def test_every_rule_has_a_source_and_a_fixture_that_reaches_it(self):
        profile = profile_for(MSSQL)
        self.assertIs(profile, PROFILE)
        reached = set()
        for rule in profile.rules:
            self.assertTrue(rule.source.startswith("https://"), rule.id)
            self.assertTrue(rule.id.startswith("mssql."), rule.id)
            for fixture in rule.fixtures:
                statement = impact(fixture, context())
                found = {t.rule for t in statement.tables} | set(rules(statement))
                if rule.id in found:
                    reached.add(rule.id)
                self.assertNotEqual(statement.confidence, Confidence.UNKNOWN, fixture)
        # The edition finding names what an edition lacks, which the
        # Developer edition the fixtures run on has; OnlineTestCase and
        # the ALTER COLUMN tests reach it.
        self.assertEqual(
            reached, {rule.id for rule in profile.rules} - {"mssql.online.edition"}
        )


if __name__ == "__main__":
    unittest.main()
