"""
Tests for the PostgreSQL rules that read the catalog facts beside the
sizes: partitioned tables, indexed columns and their collations, array
column types, and domains; and for the statements the server refuses.
"""

import unittest
from types import MappingProxyType

from sustained.analysis import MigrationStatement, with_intent
from sustained.dialects import Dialects
from sustained.guards import max_blocking, no_rewrite
from sustained.impact import (
    Blocks,
    Confidence,
    EngineContext,
    Severity,
    TableStats,
    UnnamedLock,
    Work,
    analyze,
    attach_impact,
)
from sustained.impact.context import Relation
from sustained.impact.recognizer import recognize
from sustained.impact.report import statement_data
from sustained.impact.rules.postgres import type_change
from sustained.impact.rules.postgres.context import _types, context_plan
from sustained.impact.rules.postgres.trace import _literal
from sustained.introspect.model import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedIndex,
    IntrospectedTable,
    Snapshot,
)
from tests.test_impact_context import SETTINGS_ROW, SIZE_ROWS, drive

PG = Dialects.POSTGRES

# pt is partitioned: pt1, the DEFAULT partition ptd, and ptx, which is
# partitioned in turn and has the partition ptx1. pi is partitioned with an index.
RELATIONS = MappingProxyType(
    {
        "pt": Relation(
            partitioned=True, default="ptd", partitions=("pt1", "ptd", "ptx")
        ),
        "pt1": Relation(parent="pt"),
        "ptd": Relation(parent="pt"),
        "ptx": Relation(partitioned=True, parent="pt", partitions=("ptx1",)),
        "ptx1": Relation(parent="ptx"),
        "pi": Relation(
            partitioned=True,
            partitions=("pi1",),
            indexed=MappingProxyType({"c": None}),
        ),
        "pi1": Relation(parent="pi", indexed=MappingProxyType({"c": None})),
        "d": Relation(
            indexed=MappingProxyType(
                {"at": None, "atz": None, "label": "default", "code": "C"}
            ),
            arrays=MappingProxyType({"tags": "character varying(10)[]"}),
        ),
    }
)
SCHEMA = Snapshot(
    {
        "d": IntrospectedTable(
            {
                "at": IntrospectedColumn("timestamp without time zone", True, False),
                "atz": IntrospectedColumn("timestamp with time zone", True, False),
                "label": IntrospectedColumn("text", True, False),
                "code": IntrospectedColumn("character varying(100)", True, False),
                "note": IntrospectedColumn("character varying(100)", True, False),
                "tags": IntrospectedColumn("ARRAY", True, False),
            },
            name="d",
        ),
        "pi": IntrospectedTable(
            {"c": IntrospectedColumn("integer", True, False)},
            indexes={"pi_c": IntrospectedIndex(("c",), False, name="pi_c")},
            name="pi",
        ),
        "f": IntrospectedTable(
            {"r_id": IntrospectedColumn("integer", True, False)},
            foreign_keys={
                "f_r": IntrospectedForeignKey(("r_id",), "r", ("id",), name="f_r"),
                "f_f": IntrospectedForeignKey(("r_id",), "f", ("id",), name="f_f"),
            },
            checks={"ck": "r_id > 0"},
            check_names={"ck": "ck"},
            name="f",
        ),
    }
)
READ = frozenset(
    {"version", "settings", "schema", "partitions", "indexes", "arrays", "types"}
)


def context(version=(18,), read=READ, **changes):
    fields = dict(
        settings={"TimeZone": "UTC"},
        schema=SCHEMA,
        read=read,
        relations=RELATIONS,
        types=MappingProxyType(
            {"positive": True, "public.positive": True, "plain": False}
        ),
    )
    fields.update(changes)
    return EngineContext("postgres", version, **fields)


def impact(sql, found=None, transactional=True):
    statement = MigrationStatement(sql, "m1", transactional=transactional)
    (result,) = analyze([statement], PG, found or context()).statements
    return result


def locks(statement):
    return {(t.table, t.lock, t.work) for t in statement.tables}


def dangers(statement):
    return [f for f in statement.findings if f.severity is Severity.DANGER]


class ArrayTestCase(unittest.TestCase):
    def work(self, old, new):
        return type_change(old, new, {})[:2]

    def test_an_element_change_rewrites(self):
        for old, new in [
            ("varchar(10)[]", "varchar(20)[]"),
            ("text[]", "varchar[]"),
            ("varchar[]", "text[]"),
            ("varchar(10)[]", "text[]"),
        ]:
            with self.subTest(old=old, new=new):
                self.assertEqual(self.work(old, new), (Work.REWRITE, Confidence.KNOWN))

    def test_dropping_the_element_length_is_catalog(self):
        self.assertEqual(
            self.work("character varying(10)[]", "varchar[]"),
            (Work.CATALOG, Confidence.KNOWN),
        )
        self.assertEqual(self.work("text[]", "text[]")[0], Work.CATALOG)

    def test_an_array_to_or_from_a_scalar_rewrites(self):
        self.assertEqual(self.work("text", "text[]")[0], Work.REWRITE)
        self.assertEqual(self.work("mood[]", "text")[1], Confidence.LIKELY)

    def test_the_array_read_gives_the_current_type(self):
        sql = "ALTER TABLE d ALTER COLUMN tags TYPE varchar[]"
        (found,) = impact(sql).tables
        self.assertEqual(found.rule, "pg.alter_column_type.binary_coercible")
        # The schema read alone says ARRAY, which gives no element type.
        statement = impact(sql, context(read=READ - {"arrays"}, relations={}))
        self.assertEqual(statement.tables[0].work, Work.REWRITE)
        self.assertEqual(statement.confidence, Confidence.LIKELY)


class IndexRebuildTestCase(unittest.TestCase):
    def test_a_time_zone_swap_rebuilds_the_indexes_on_the_column(self):
        for sql in [
            "ALTER TABLE d ALTER COLUMN at TYPE timestamptz",
            "ALTER TABLE d ALTER COLUMN atz TYPE timestamp",
        ]:
            with self.subTest(sql=sql):
                statement = impact(sql)
                (found,) = statement.tables
                self.assertEqual(
                    (found.lock, found.work, found.rule),
                    (
                        "ACCESS EXCLUSIVE",
                        Work.INDEX_BUILD,
                        "pg.alter_column_type.index_rebuild",
                    ),
                )
                self.assertEqual(statement.confidence, Confidence.KNOWN)
                self.assertIn("operator classes", statement.findings[0].message)

    def test_a_time_zone_swap_outside_utc_rewrites(self):
        paris = context(settings={"TimeZone": "Europe/Paris"})
        found = impact("ALTER TABLE d ALTER COLUMN atz TYPE timestamp", paris)
        self.assertEqual(found.tables[0].work, Work.REWRITE)

    def test_an_unindexed_column_changes_the_catalog(self):
        found = impact('ALTER TABLE d ALTER COLUMN note TYPE varchar(200) COLLATE "C"')
        self.assertEqual(found.tables[0].rule, "pg.alter_column_type.binary_coercible")

    def test_a_collation_change_rebuilds_the_index(self):
        for sql in [
            'ALTER TABLE d ALTER COLUMN label TYPE text COLLATE "C"',
            'ALTER TABLE d ALTER COLUMN label TYPE text COLLATE pg_catalog."C"',
            # Without COLLATE the column takes the default collation.
            "ALTER TABLE d ALTER COLUMN code TYPE varchar(200)",
        ]:
            with self.subTest(sql=sql):
                statement = impact(sql)
                self.assertEqual(statement.tables[0].work, Work.INDEX_BUILD)
                self.assertIn("the collation changes", statement.findings[0].message)

    def test_the_same_collation_rebuilds_nothing(self):
        for sql in [
            'ALTER TABLE d ALTER COLUMN code TYPE varchar(200) COLLATE "C"',
            'ALTER TABLE d ALTER COLUMN label TYPE text COLLATE "default"',
            "ALTER TABLE d ALTER COLUMN label TYPE text",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(impact(sql).tables[0].work, Work.CATALOG)

    def test_without_the_index_read_a_rebuild_is_likely(self):
        unread = context(read=READ - {"indexes"}, relations={})
        statement = impact(
            'ALTER TABLE d ALTER COLUMN label TYPE text COLLATE "C"', unread
        )
        self.assertEqual(statement.tables[0].work, Work.INDEX_BUILD)
        self.assertEqual(statement.confidence, Confidence.LIKELY)
        self.assertIn("was not read", statement.findings[0].message)
        statement = impact("ALTER TABLE d ALTER COLUMN at TYPE timestamptz", unread)
        self.assertEqual(statement.tables[0].work, Work.INDEX_BUILD)
        self.assertEqual(statement.confidence, Confidence.LIKELY)
        # Without COLLATE, no rebuild is assumed.
        statement = impact("ALTER TABLE d ALTER COLUMN label TYPE text", unread)
        self.assertEqual(statement.tables[0].work, Work.CATALOG)

    def test_the_schema_read_proves_an_index(self):
        unread = context(read=READ - {"indexes"}, relations={})
        sql = with_intent(
            "ALTER TABLE pi ALTER COLUMN c TYPE timestamptz",
            "alter_column_type",
            "pi",
            "c",
            from_type="timestamp",
            to_type="timestamptz",
        )
        (statement,) = analyze([sql], PG, unread).statements
        self.assertEqual(statement.tables[0].work, Work.INDEX_BUILD)
        self.assertEqual(statement.confidence, Confidence.KNOWN)


class DomainTestCase(unittest.TestCase):
    def added(self, type_text, found=None):
        statement = impact(f"ALTER TABLE t ADD COLUMN x {type_text}", found)
        (table,) = statement.tables
        return table.work, statement.confidence

    def test_a_domain_with_a_constraint_rewrites(self):
        for type_text in ["positive", "public.positive", '"positive" DEFAULT 3']:
            with self.subTest(type_text=type_text):
                self.assertEqual(
                    self.added(type_text), (Work.REWRITE, Confidence.KNOWN)
                )
        statement = impact("ALTER TABLE t ADD COLUMN x positive")
        self.assertIn("is a domain with a constraint", statement.findings[0].message)

    def test_a_type_that_checks_nothing_changes_the_catalog(self):
        for type_text in [
            "plain",
            "positive[]",
            "integer",
            "character varying(20)",
            "timestamp(3) with time zone",
            "interval day to second",
            "inet",
            "bit varying(4)",
        ]:
            with self.subTest(type_text=type_text):
                self.assertEqual(
                    self.added(type_text), (Work.CATALOG, Confidence.KNOWN)
                )

    def test_a_type_the_read_did_not_find_is_a_likely_rewrite(self):
        self.assertEqual(self.added("citext"), (Work.REWRITE, Confidence.LIKELY))
        statement = impact("ALTER TABLE t ADD COLUMN x citext")
        self.assertIn("did not find the type citext", statement.findings[0].message)

    def test_without_the_type_read_a_domain_is_a_likely_rewrite(self):
        unread = context(read=READ - {"types"}, types={})
        self.assertEqual(self.added("plain", unread), (Work.REWRITE, Confidence.LIKELY))
        self.assertEqual(
            self.added("integer", unread), (Work.CATALOG, Confidence.KNOWN)
        )

    def test_the_type_read_keys_each_domain_through_its_base(self):
        # oid, schema, name, visible, base, own constraint
        rows = [
            (1, "public", "positive", True, 23, True),
            (2, "public", "small", True, 1, False),
            (3, "app", "plain", False, 23, False),
            (4, "public", "mood", True, None, False),
        ]
        types = _types(rows)
        self.assertEqual(
            dict(types),
            {
                "public.positive": True,
                "positive": True,
                "public.small": True,
                "small": True,
                "app.plain": False,
                "public.mood": False,
                "mood": False,
            },
        )


class PartitionTestCase(unittest.TestCase):
    def test_create_index_builds_on_every_partition(self):
        statement = impact("CREATE INDEX ix ON pt (id)")
        self.assertEqual(
            locks(statement),
            {
                (name, "SHARE", Work.INDEX_BUILD)
                for name in ("pt", "pt1", "ptd", "ptx", "ptx1")
            },
        )
        finding = next(f for f in statement.findings if f.rule == "pg.create_index")
        self.assertEqual(finding.remedy, ())
        self.assertIn("ATTACH PARTITION", finding.message)

    def test_create_index_on_only_the_parent_builds_nothing(self):
        statement = impact("CREATE INDEX ix ON ONLY pt (id)")
        self.assertEqual(locks(statement), {("pt", "SHARE", Work.CATALOG)})
        # ONLY on a plain table builds the index as usual.
        statement = impact("CREATE INDEX ix ON ONLY t (id)")
        self.assertEqual(locks(statement), {("t", "SHARE", Work.INDEX_BUILD)})

    def test_create_index_concurrently(self):
        statement = impact(
            "CREATE INDEX CONCURRENTLY ix ON pt (id)", transactional=False
        )
        (finding,) = dangers(statement)
        self.assertEqual(finding.rule, "pg.create_index.concurrently")
        self.assertIn("partitioned table", finding.message)
        statement = impact("CREATE INDEX CONCURRENTLY ix ON t (id)")
        (finding,) = dangers(statement)
        self.assertIn("inside a transaction block", finding.message)
        statement = impact(
            "CREATE INDEX CONCURRENTLY ix ON t (id)", transactional=False
        )
        self.assertEqual([f.severity for f in statement.findings], [Severity.INFO])

    def test_drop_index_on_a_partitioned_table(self):
        statement = impact("DROP INDEX pi_c")
        self.assertEqual(
            locks(statement),
            {
                ("pi", "ACCESS EXCLUSIVE", Work.CATALOG),
                ("pi1", "ACCESS EXCLUSIVE", Work.CATALOG),
            },
        )
        finding = next(f for f in statement.findings if f.rule == "pg.drop_index")
        self.assertEqual(finding.remedy, ())
        statement = impact("DROP INDEX CONCURRENTLY pi_c", transactional=False)
        (finding,) = dangers(statement)
        self.assertIn("partitioned table", finding.message)
        statement = impact("DROP INDEX CONCURRENTLY ix")
        (finding,) = dangers(statement)
        self.assertIn("inside a transaction block", finding.message)
        self.assertEqual(
            dangers(impact("DROP INDEX CONCURRENTLY ix", transactional=False)), []
        )

    def test_drop_table(self):
        exclusive = "ACCESS EXCLUSIVE"
        cases = {
            "DROP TABLE pt1": {"pt1", "pt", "ptd"},
            "DROP TABLE ptd": {"ptd", "pt"},
            "DROP TABLE ptx": {"ptx", "ptx1", "pt", "ptd"},
            "DROP TABLE ptx1": {"ptx1", "ptx"},
            "DROP TABLE pt": {"pt", "pt1", "ptd", "ptx", "ptx1"},
            "DROP TABLE pt, pt1": {"pt", "pt1", "ptd", "ptx", "ptx1"},
            "TRUNCATE pt": {"pt", "pt1", "ptd", "ptx", "ptx1"},
            "TRUNCATE pt1": {"pt1"},
        }
        for sql, tables in cases.items():
            with self.subTest(sql=sql):
                statement = impact(sql)
                self.assertEqual(
                    locks(statement), {(t, exclusive, Work.CATALOG) for t in tables}
                )
        statement = impact("DROP TABLE pt1")
        note = next(f for f in statement.findings if "a partition of pt" in f.message)
        self.assertEqual(note.severity, Severity.INFO)

    def test_create_table_partition_of_scans_the_default(self):
        statement = impact("CREATE TABLE pt3 PARTITION OF pt FOR VALUES IN (7)")
        self.assertEqual(
            locks(statement),
            {
                ("pt", "ACCESS EXCLUSIVE", Work.CATALOG),
                ("ptd", "ACCESS EXCLUSIVE", Work.SCAN),
            },
        )
        self.assertEqual(statement.confidence, Confidence.LIKELY)
        statement = impact("CREATE TABLE ptx2 PARTITION OF ptx FOR VALUES IN (8)")
        self.assertEqual(locks(statement), {("ptx", "ACCESS EXCLUSIVE", Work.CATALOG)})

    def test_attach_partition(self):
        statement = impact("ALTER TABLE pt ATTACH PARTITION ptx FOR VALUES IN (2)")
        self.assertEqual(
            locks(statement),
            {
                ("pt", "SHARE UPDATE EXCLUSIVE", Work.CATALOG),
                ("ptx", "ACCESS EXCLUSIVE", Work.SCAN),
                ("ptx1", "ACCESS EXCLUSIVE", Work.SCAN),
                ("ptd", "ACCESS EXCLUSIVE", Work.SCAN),
            },
        )
        statement = impact(
            "ALTER TABLE pi ATTACH PARTITION p FOR VALUES FROM (1) TO (2)"
        )
        self.assertIn(("p", "ACCESS EXCLUSIVE", Work.INDEX_BUILD), locks(statement))
        # The schema read proves pi has an index when the index read failed.
        unread = context(read=READ - {"indexes"})
        statement = impact(
            "ALTER TABLE pi ATTACH PARTITION p FOR VALUES FROM (1) TO (2)", unread
        )
        self.assertIn(("p", "ACCESS EXCLUSIVE", Work.INDEX_BUILD), locks(statement))

    def test_detach_partition(self):
        statement = impact("ALTER TABLE pt DETACH PARTITION ptx")
        self.assertEqual(
            {t for t, _, _ in locks(statement)}, {"pt", "ptx", "ptx1", "ptd"}
        )
        statement = impact("ALTER TABLE pt DETACH PARTITION ptd")
        self.assertEqual({t for t, _, _ in locks(statement)}, {"pt", "ptd"})

    def test_detach_concurrently_is_refused_beside_a_default(self):
        statement = impact(
            "ALTER TABLE pt DETACH PARTITION pt1 CONCURRENTLY", transactional=False
        )
        (finding,) = dangers(statement)
        self.assertIn("DEFAULT partition, ptd", finding.message)
        statement = impact("ALTER TABLE pi DETACH PARTITION pi1 CONCURRENTLY")
        (finding,) = dangers(statement)
        self.assertIn("inside a transaction block", finding.message)
        statement = impact(
            "ALTER TABLE pi DETACH PARTITION pi1 CONCURRENTLY", transactional=False
        )
        self.assertEqual(statement.findings, ())

    def test_an_alter_on_the_parent_locks_each_partition(self):
        all_tables = {"pt", "pt1", "ptd", "ptx", "ptx1"}
        cases = {
            "ALTER TABLE pt ADD COLUMN d integer": ("ACCESS EXCLUSIVE", Work.CATALOG),
            "ALTER TABLE pt ADD CONSTRAINT ck CHECK (id > 0)": (
                "ACCESS EXCLUSIVE",
                Work.SCAN,
            ),
            "ALTER TABLE pt ALTER COLUMN id SET STATISTICS 10": (
                "SHARE UPDATE EXCLUSIVE",
                Work.CATALOG,
            ),
            "ALTER TABLE pt DISABLE TRIGGER tr": ("SHARE ROW EXCLUSIVE", Work.CATALOG),
        }
        for sql, (lock, work) in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(
                    locks(impact(sql)), {(t, lock, work) for t in all_tables}
                )

    def test_an_alter_of_the_parent_alone(self):
        for sql in [
            "ALTER TABLE ONLY pt ALTER COLUMN id SET DEFAULT 1",
            "ALTER TABLE pt OWNER TO someone",
            "ALTER TABLE pt RENAME TO pu",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual({t.table for t in impact(sql).tables}, {"pt"})
        # A partitioned table has no file to move.
        statement = impact("ALTER TABLE pt SET TABLESPACE fast")
        self.assertEqual(locks(statement), {("pt", "ACCESS EXCLUSIVE", Work.CATALOG)})

    def test_a_unique_key_locks_the_partitions_by_version(self):
        sql = "ALTER TABLE pi ADD CONSTRAINT pi_key UNIQUE (id)"
        self.assertIn(
            ("pi1", "SHARE", Work.INDEX_BUILD), locks(impact(sql, context((14,))))
        )
        self.assertIn(("pi1", "ACCESS EXCLUSIVE", Work.INDEX_BUILD), locks(impact(sql)))
        sql = "ALTER TABLE pi ADD PRIMARY KEY (id)"
        self.assertIn(
            ("pi1", "ACCESS EXCLUSIVE", Work.INDEX_BUILD),
            locks(impact(sql, context((14,)))),
        )

    def test_a_not_valid_foreign_key_on_a_parent_needs_18(self):
        sql = "ALTER TABLE pt ADD CONSTRAINT fk FOREIGN KEY (id) REFERENCES r (id) NOT VALID"
        (finding,) = dangers(impact(sql, context((17,))))
        self.assertEqual(finding.rule, "pg.add_foreign_key.not_valid")
        self.assertEqual(dangers(impact(sql)), [])
        self.assertIn(("ptx1", "SHARE ROW EXCLUSIVE", Work.CATALOG), locks(impact(sql)))

    def test_an_exclusion_on_a_parent_needs_17(self):
        sql = "ALTER TABLE pt ADD CONSTRAINT ex EXCLUDE USING btree (id WITH =)"
        (finding,) = dangers(impact(sql, context((16,))))
        self.assertIn("before 17", finding.message)
        self.assertIn(
            ("pt1", "SHARE", Work.INDEX_BUILD), locks(impact(sql, context((16,))))
        )
        self.assertEqual(dangers(impact(sql)), [])

    def test_other_statements_on_the_parent(self):
        statement = impact("ANALYZE pt")
        self.assertIn(("ptx1", "SHARE UPDATE EXCLUSIVE", Work.SCAN), locks(statement))
        statement = impact(
            "CREATE TRIGGER tr BEFORE UPDATE ON pt FOR EACH ROW EXECUTE FUNCTION f()"
        )
        self.assertIn(("ptx1", "SHARE ROW EXCLUSIVE", Work.CATALOG), locks(statement))
        statement = impact("LOCK TABLE pt IN SHARE MODE NOWAIT")
        self.assertIn(("ptx1", "SHARE", Work.CATALOG), locks(statement))
        self.assertNotIn("pg.lock_timeout", [f.rule for f in statement.findings])
        statement = impact("LOCK TABLE ONLY pt IN SHARE MODE")
        self.assertEqual(locks(statement), {("pt", "SHARE", Work.CATALOG)})

    def test_vacuum_is_refused_in_a_transaction(self):
        (finding,) = dangers(impact("VACUUM pt"))
        self.assertEqual(finding.rule, "pg.vacuum")
        self.assertIn(
            ("ptx1", "SHARE UPDATE EXCLUSIVE", Work.SCAN),
            locks(impact("VACUUM pt", transactional=False)),
        )
        self.assertEqual(dangers(impact("VACUUM FULL t"))[0].rule, "pg.vacuum_full")
        self.assertEqual(dangers(impact("ANALYZE t")), [])
        self.assertEqual(dangers(impact("CLUSTER"))[0].rule, "pg.vacuum_full")

    def test_reindex(self):
        statement = impact("REINDEX TABLE pt")
        self.assertIn("inside a transaction block", dangers(statement)[0].message)
        statement = impact("REINDEX TABLE pt", transactional=False)
        self.assertEqual(dangers(statement), [])
        self.assertIn(("ptx1", "SHARE", Work.INDEX_BUILD), locks(statement))
        statement = impact("REINDEX INDEX pi_c")
        self.assertTrue(dangers(statement))
        statement = impact("REINDEX TABLE CONCURRENTLY pt")
        self.assertEqual(dangers(statement)[0].rule, "pg.reindex.concurrently")
        self.assertIn(
            ("ptx1", "SHARE UPDATE EXCLUSIVE", Work.INDEX_BUILD), locks(statement)
        )
        self.assertEqual(dangers(impact("REINDEX SCHEMA app"))[0].rule, "pg.reindex")
        self.assertEqual(dangers(impact("REINDEX TABLE t")), [])


def unread_findings(statement):
    return [f for f in statement.findings if f.rule == "pg.partitions_unread"]


class UnreadPartitionsTestCase(unittest.TestCase):
    """A statement whose answer depends on partitions that were not read."""

    UNREAD = READ - {"partitions"}

    def unread(self, sql, found=None, transactional=True):
        return impact(
            sql,
            found or context(read=self.UNREAD, relations={}),
            transactional,
        )

    def test_without_a_context_the_partitions_are_unread(self):
        (statement,) = analyze(["CREATE INDEX ix ON pt (id)"], PG).statements
        self.assertEqual(locks(statement), {("pt", "SHARE", Work.INDEX_BUILD)})
        self.assertEqual(statement.confidence, Confidence.LIKELY)
        (finding,) = unread_findings(statement)
        self.assertEqual(finding.severity, Severity.INFO)
        self.assertEqual(
            finding.message,
            "the partitions were not read, so it is not known whether pt is a "
            "partitioned table or a partition; if pt is a partitioned "
            "table, each partition below it is also locked SHARE while the index "
            "is built on it, and the server refuses CREATE INDEX CONCURRENTLY on it",
        )
        # The finding follows the one about the index build.
        self.assertEqual(statement.findings[0].rule, "pg.create_index")

    def test_a_failed_partition_read_names_what_else_is_locked(self):
        cases = [
            ("ALTER TABLE pt ADD COLUMN d integer", ["if pt is a partitioned table"]),
            (
                "CREATE TABLE p2 PARTITION OF pt FOR VALUES IN (5)",
                ["if pt has a DEFAULT partition, it is also locked ACCESS EXCLUSIVE "],
            ),
            (
                "ALTER TABLE pt ATTACH PARTITION ptx FOR VALUES IN (2)",
                [
                    "if ptx is a partitioned table, each partition below it is "
                    "also locked ACCESS EXCLUSIVE",
                    "if pt has a DEFAULT partition, it is also locked ACCESS "
                    "EXCLUSIVE while each of its rows is checked against the bound "
                    "of ptx",
                ],
            ),
            (
                "ALTER TABLE pt DETACH PARTITION ptx",
                ["if ptx is a partitioned table", "if pt has a DEFAULT partition"],
            ),
            (
                "DROP TABLE pt1",
                [
                    "if pt1 is a partitioned table",
                    "if pt1 is a partition, its partitioned table and that "
                    "table's DEFAULT partition are also locked ACCESS EXCLUSIVE",
                ],
            ),
            ("TRUNCATE pt", ["if pt is a partitioned table"]),
            ("DROP INDEX ix", ["if the table of index ix is a partitioned table"]),
            (
                "DROP INDEX CONCURRENTLY pi_c",
                [
                    "if pi is a partitioned table, the server refuses DROP INDEX "
                    "CONCURRENTLY of pi_c"
                ],
            ),
            ("REINDEX TABLE pt", ["refuses the REINDEX inside a transaction block"]),
            ("CREATE TRIGGER tr AFTER INSERT ON pt EXECUTE FUNCTION f()", ["pt"]),
            (
                "LOCK TABLE pt IN SHARE MODE",
                ["each partition below it is also locked SHARE"],
            ),
        ]
        for sql, parts in cases:
            with self.subTest(sql=sql):
                statement = self.unread(sql)
                self.assertEqual(statement.confidence, Confidence.LIKELY)
                (finding,) = unread_findings(statement)
                for part in parts:
                    self.assertIn(part, finding.message)
        statement = self.unread("VACUUM pt", transactional=False)
        (finding,) = unread_findings(statement)
        self.assertIn("also locked SHARE UPDATE EXCLUSIVE", finding.message)
        statement = self.unread(
            "CREATE INDEX CONCURRENTLY ix ON pt (id)", transactional=False
        )
        (finding,) = unread_findings(statement)
        self.assertIn("refuses CREATE INDEX CONCURRENTLY on it", finding.message)
        # The version decides whether a refusal depends on the partitions.
        for version, sql in [
            ((16,), "ALTER TABLE pt ADD CONSTRAINT x EXCLUDE USING gist (id WITH =)"),
            (
                (17,),
                "ALTER TABLE pt ADD CONSTRAINT fk FOREIGN KEY (a) "
                "REFERENCES r (id) NOT VALID",
            ),
        ]:
            with self.subTest(version=version):
                found = context(version, read=self.UNREAD, relations={})
                (finding,) = unread_findings(self.unread(sql, found))
                self.assertIn(
                    f"PostgreSQL before {version[0] + 1} refuses", finding.message
                )

    def test_a_statement_that_touches_no_partition_has_no_finding(self):
        for sql in [
            "CREATE TABLE n (id integer)",
            "CREATE INDEX ix ON ONLY pt (id)",
            "ALTER TABLE pt RENAME TO pu",
            "ALTER TABLE ONLY pt ALTER COLUMN id SET STATISTICS 100",
            "LOCK TABLE ONLY pt IN SHARE MODE",
            "UPDATE pt SET id = 1",
            "ALTER TABLE pt DETACH PARTITION ptx CONCURRENTLY",
        ]:
            with self.subTest(sql=sql):
                statement = self.unread(sql, transactional=False)
                self.assertEqual(
                    [f.rule for f in unread_findings(statement)],
                    ["pg.partitions_unread"] if "DETACH" in sql else [],
                )

    def test_a_table_the_run_created_has_no_finding(self):
        statements = [
            MigrationStatement(sql, "m1")
            for sql in [
                "CREATE TABLE n (id integer)",
                "CREATE INDEX ix ON n (id)",
                "ALTER TABLE n ADD COLUMN d integer",
                "CREATE TABLE n1 PARTITION OF n FOR VALUES IN (1)",
                "DROP TABLE n",
            ]
        ]
        found = context(read=self.UNREAD, relations={})
        for statement in analyze(statements, PG, found).statements:
            with self.subTest(sql=statement.statement):
                self.assertEqual(unread_findings(statement), [])
                self.assertEqual(statement.confidence, Confidence.KNOWN)

    def test_a_table_the_read_found_unpartitioned_has_no_finding(self):
        for sql in [
            "CREATE INDEX ix ON d (at)",
            "ALTER TABLE d ADD COLUMN e integer",
            "DROP TABLE d",
            "ALTER TABLE d ATTACH PARTITION p FOR VALUES IN (1)",
        ]:
            with self.subTest(sql=sql):
                statement = impact(sql)
                self.assertEqual(unread_findings(statement), [])
        self.assertEqual(
            impact("CREATE INDEX ix ON d (at)").confidence, Confidence.KNOWN
        )

    def test_a_partitioned_table_keeps_its_partitions(self):
        statement = impact("CREATE INDEX ix ON pt (id)")
        self.assertEqual(
            {t for t, _, _ in locks(statement)}, {"pt", "pt1", "ptd", "ptx", "ptx1"}
        )
        self.assertEqual(unread_findings(statement), [])
        self.assertEqual(statement.confidence, Confidence.KNOWN)

    def test_the_statement_records_the_unread_partitions(self):
        statement = self.unread("CREATE INDEX ix ON pt (id)")
        self.assertTrue(statement.partitions_unread)
        # The partitions below pt are smaller than pt, which is named.
        self.assertEqual(statement.unnamed_locks, ())
        self.assertFalse(impact("CREATE INDEX ix ON pt (id)").partitions_unread)
        self.assertFalse(self.unread("CREATE TABLE n (id integer)").partitions_unread)
        # A DEFAULT partition, or a partitioned table, the read did not
        # name is no row of the report.
        for sql, lock in [
            (
                "ALTER TABLE pt ATTACH PARTITION p FOR VALUES IN (9)",
                UnnamedLock("ACCESS EXCLUSIVE", Blocks.READS_AND_WRITES, Work.SCAN),
            ),
            (
                "DROP TABLE pt1",
                UnnamedLock("ACCESS EXCLUSIVE", Blocks.READS_AND_WRITES, Work.CATALOG),
            ),
        ]:
            with self.subTest(sql=sql):
                statement = self.unread(sql)
                self.assertEqual(statement.unnamed_locks, (lock,))
                self.assertTrue(all("(" not in t for t, _, _ in locks(statement)))
                data = statement_data(statement)
                self.assertTrue(data["partitions_unread"])
                self.assertEqual(
                    data["unnamed_locks"],
                    [
                        {
                            "lock": "ACCESS EXCLUSIVE",
                            "blocks": "reads_and_writes",
                            "work": str(lock.work),
                        }
                    ],
                )
        self.assertEqual(impact("DROP TABLE pt1").unnamed_locks, ())

    def test_the_guards_count_an_unnamed_lock_as_a_table_of_unknown_size(self):
        tables = {
            name: TableStats(10, 8192) for name in ("pt", "pt1", "pt9", "d", "ix")
        }
        unread = context(read=self.UNREAD | {"sizes"}, relations={}, tables=tables)
        # With the partitions read and no relations, no table is
        # partitioned, so pt has no DEFAULT partition.
        read = context(read=READ | {"sizes"}, relations={}, tables=tables)

        def blocked(guard, sql, found):
            statements = attach_impact([sql], PG, found)
            return [v.rule for v in guard(statements, PG)]

        attach = "ALTER TABLE pt ATTACH PARTITION pt9 FOR VALUES IN (9)"
        self.assertEqual(
            blocked(max_blocking("ddl", over_rows=1000), attach, unread),
            ["max_blocking(ddl, over_rows=1000)"],
        )
        self.assertEqual(
            blocked(
                max_blocking("ddl", over_rows=1000, assume_small=True), attach, unread
            ),
            [],
        )
        self.assertEqual(blocked(max_blocking("ddl", over_rows=1000), attach, read), [])
        drop = "DROP TABLE pt1"
        self.assertEqual(
            blocked(max_blocking("writes", over_rows=1000), drop, unread),
            ["max_blocking(writes, over_rows=1000)"],
        )
        self.assertEqual(
            blocked(
                max_blocking("writes", over_rows=1000, assume_small=True), drop, unread
            ),
            [],
        )
        self.assertEqual(
            blocked(max_blocking("writes", over_rows=1000), "DROP TABLE d", read), []
        )
        # The partitions below a table are smaller than the table, so a
        # statement that locks only those draws no block for them.
        self.assertEqual(
            blocked(
                max_blocking("nothing", over_rows=1000),
                "CREATE INDEX ix ON pt (id)",
                unread,
            ),
            [],
        )
        # No unnamed lock rewrites a table.
        self.assertEqual(blocked(no_rewrite(over_rows=1000), attach, unread), [])


class ValidateTestCase(unittest.TestCase):
    def test_a_foreign_key_takes_row_share_on_its_target(self):
        statement = impact("ALTER TABLE f VALIDATE CONSTRAINT f_r")
        self.assertEqual(
            locks(statement),
            {
                ("f", "SHARE UPDATE EXCLUSIVE", Work.SCAN),
                ("r", "ROW SHARE", Work.CATALOG),
            },
        )
        self.assertEqual(statement.confidence, Confidence.KNOWN)

    def test_a_key_on_the_same_table_locks_it_once(self):
        statement = impact("ALTER TABLE f VALIDATE CONSTRAINT f_f")
        self.assertEqual(locks(statement), {("f", "SHARE UPDATE EXCLUSIVE", Work.SCAN)})

    def test_a_check_is_known(self):
        statement = impact("ALTER TABLE f VALIDATE CONSTRAINT ck")
        self.assertEqual(statement.confidence, Confidence.KNOWN)

    def test_an_unknown_constraint_is_likely(self):
        for sql, found in [
            ("ALTER TABLE f VALIDATE CONSTRAINT other", None),
            ("ALTER TABLE u VALIDATE CONSTRAINT ck", None),
            ("ALTER TABLE f VALIDATE CONSTRAINT f_r", EngineContext("postgres", (18,))),
        ]:
            with self.subTest(sql=sql):
                statement = impact(sql, found)
                self.assertEqual(statement.confidence, Confidence.LIKELY)
                self.assertEqual(len(statement.tables), 1)


class ReadTestCase(unittest.TestCase):
    PARTITIONS = [
        # oid, schema, name, visible, partitioned, parent, default
        (10, "public", "pt", True, True, None, 12),
        (11, "public", "pt1", True, False, 10, None),
        (12, "public", "ptd", True, False, 10, None),
        (13, "audit", "pa", False, False, 10, None),
    ]
    INDEXED = [
        # schema, name, visible, column, collation
        ("public", "pt", True, "id", None),
        ("public", "d", True, "Label", "C"),
    ]
    ARRAYS = [("public", "d", True, "tags", "text[]")]

    def test_reads_the_partitions_columns_and_types(self):
        asked, found = drive(
            context_plan(),
            [
                [SETTINGS_ROW],
                [("0", "1s")],
                SIZE_ROWS,
                [("0",)],
                self.PARTITIONS,
                self.INDEXED,
                self.ARRAYS,
                [(1, "public", "positive", True, 23, True)],
            ],
        )
        self.assertEqual(len(asked), 8)
        self.assertLessEqual({"partitions", "indexes", "arrays", "types"}, found.read)
        pt = found.relations["public.pt"]
        self.assertTrue(pt.partitioned)
        self.assertEqual(pt.partitions, ("audit.pa", "pt1", "ptd"))
        self.assertEqual(pt.default, "ptd")
        self.assertEqual(dict(pt.indexed), {"id": None})
        self.assertEqual(found.relations["audit.pa"].parent, "pt")
        self.assertNotIn("pa", found.relations)
        self.assertEqual(dict(found.relations["d"].indexed), {"label": "C"})
        self.assertEqual(dict(found.relations["d"].arrays), {"tags": "text[]"})
        self.assertIs(found.types["positive"], True)

    def test_a_failed_read_leaves_its_facts_out(self):
        _, found = drive(
            context_plan(),
            [
                [SETTINGS_ROW],
                [("0", "1s")],
                SIZE_ROWS,
                [("0",)],
                RuntimeError("denied"),
                [],
                [],
                RuntimeError("x"),
            ],
        )
        self.assertEqual(found.read & {"partitions", "types"}, set())
        self.assertLessEqual({"indexes", "arrays"}, found.read)


class QuotingTestCase(unittest.TestCase):
    def remedy(self, sql, rule):
        statement = impact(sql, EngineContext("postgres", (18,)))
        return next(f for f in statement.findings if f.rule == rule).remedy

    def test_a_reserved_word_keeps_its_quotes(self):
        remedy = self.remedy(
            'ALTER TABLE "user"."select" ALTER COLUMN "order" SET NOT NULL',
            "pg.set_not_null",
        )
        self.assertEqual(
            remedy[0],
            'ALTER TABLE "user"."select" ADD CONSTRAINT select_order_not_null '
            'CHECK ("order" IS NOT NULL) NOT VALID',
        )

    def test_a_quoted_part_with_a_dot_stays_one_part(self):
        remedy = self.remedy(
            'ALTER TABLE "a.b".c ADD CONSTRAINT k CHECK (x > 0)', "pg.add_check"
        )
        self.assertEqual(remedy[1], 'ALTER TABLE "a.b".c VALIDATE CONSTRAINT k')

    def test_a_bare_name_is_kept_as_written(self):
        remedy = self.remedy("ALTER TABLE Orders ADD UNIQUE (id)", "pg.add_key")
        self.assertEqual(
            remedy,
            (
                'CREATE UNIQUE INDEX CONCURRENTLY "Orders_key_idx" ON Orders (id)',
                'ALTER TABLE Orders ADD CONSTRAINT "Orders_key" UNIQUE USING INDEX '
                '"Orders_key_idx"',
            ),
        )

    def test_a_literal_doubles_quotes_and_backslashes(self):
        self.assertEqual(_literal("it's"), "'it''s'")
        self.assertEqual(_literal("a\\b'"), "E'a\\\\b'''")


class RecognizerTestCase(unittest.TestCase):
    def test_lock_table_only(self):
        self.assertTrue(recognize("LOCK TABLE ONLY pt", PG).options["only"])
        self.assertFalse(recognize("LOCK TABLE pt", PG).options["only"])


class RowWriteTestCase(unittest.TestCase):
    def test_a_delete_is_advised_to_delete_in_batches(self):
        statement = impact("DELETE FROM t WHERE id < 10")
        self.assertIn("delete in batches", statement.findings[0].message)
        statement = impact("UPDATE t SET c = 0")
        self.assertIn("backfill in batches", statement.findings[0].message)


if __name__ == "__main__":
    unittest.main()


class RunPartitionTestCase(unittest.TestCase):
    """Partitioned tables and partitions the run creates and links."""

    def last(self, statements, found):
        run = [MigrationStatement(sql, "m1") for sql in statements]
        return analyze(run, PG, found).statements[-1]

    def test_an_index_on_a_table_the_run_attached_a_large_table_to(self):
        found = context(
            tables={"orders": TableStats(5_000_000, 10**9)},
            read=READ | {"sizes"},
        )
        statement = self.last(
            [
                "CREATE TABLE orders_new (id int, created_at date, customer_id int) "
                "PARTITION BY RANGE (created_at)",
                "ALTER TABLE orders_new ATTACH PARTITION orders "
                "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
                "CREATE INDEX ON orders_new (customer_id)",
            ],
            found,
        )
        tables = {t.table: t for t in statement.tables}
        self.assertEqual(set(tables), {"orders_new", "orders"})
        for table in tables.values():
            self.assertEqual(
                (table.lock, table.work, table.blocks, table.rows),
                ("SHARE", Work.INDEX_BUILD, Blocks.WRITES, 5_000_000),
            )
        self.assertEqual(statement.confidence, Confidence.KNOWN)
        self.assertEqual(unread_findings(statement), [])

    def test_the_run_links_are_known_without_the_partitions_read(self):
        found = context(
            read=READ - {"partitions"} | {"sizes"},
            relations={},
            tables={"orders": TableStats(5_000_000, 10**9)},
        )
        statement = self.last(
            [
                "CREATE TABLE n (id int) PARTITION BY RANGE (id)",
                "ALTER TABLE n ATTACH PARTITION orders FOR VALUES FROM (1) TO (9)",
                "CREATE INDEX ON n (id)",
            ],
            found,
        )
        self.assertEqual(
            {(t.table, t.work) for t in statement.tables},
            {("n", Work.INDEX_BUILD), ("orders", Work.INDEX_BUILD)},
        )
        # What is below orders was not read.
        self.assertEqual(statement.confidence, Confidence.LIKELY)
        self.assertTrue(statement.partitions_unread)
        (finding,) = unread_findings(statement)
        self.assertIn(
            "it is not known whether orders is a partitioned table or a "
            "partition; if orders is a partitioned table, each partition below "
            "it is also locked SHARE",
            finding.message,
        )

    def test_a_partition_the_run_created_is_locked_with_its_parent(self):
        statement = self.last(
            [
                "CREATE TABLE pt2 PARTITION OF pt FOR VALUES IN (2)",
                "CREATE INDEX ix ON pt (id)",
            ],
            context(),
        )
        # The new partition is empty, so the build there blocks nothing.
        tables = {t.table: t for t in statement.tables}
        self.assertEqual(
            (tables["pt2"].lock, tables["pt2"].blocks), ("SHARE", Blocks.NOTHING)
        )
        statement = self.last(
            [
                "ALTER TABLE pt DETACH PARTITION ptd",
                "CREATE TABLE pt2 PARTITION OF pt DEFAULT",
                "CREATE TABLE pt3 PARTITION OF pt FOR VALUES IN (3)",
            ],
            context(),
        )
        tables = {t.table for t in statement.tables}
        self.assertIn("pt2", tables)
        self.assertNotIn("ptd", tables)

    def test_a_detached_partition_is_not_locked_with_its_parent(self):
        statement = self.last(
            ["ALTER TABLE pt DETACH PARTITION pt1", "CREATE INDEX ix ON pt (id)"],
            context(),
        )
        self.assertNotIn("pt1", {t.table for t in statement.tables})


class RunDomainTestCase(unittest.TestCase):
    """Domains the run creates and drops, read before the types read."""

    def added(self, statements, found=None):
        run = [MigrationStatement(sql, "m1") for sql in statements]
        run.append(MigrationStatement("ALTER TABLE t ADD COLUMN x d", "m1"))
        statement = analyze(run, PG, found or context()).statements[-1]
        (table,) = statement.tables
        return table.work, statement.confidence

    def test_a_domain_the_run_created_with_a_check_rewrites(self):
        for sql in (
            "CREATE DOMAIN d AS int CHECK (VALUE > 0)",
            "CREATE DOMAIN d AS int NOT NULL",
        ):
            with self.subTest(sql):
                self.assertEqual(self.added([sql]), (Work.REWRITE, Confidence.KNOWN))
        # Without the types read, the run's facts still answer.
        unread = context(read=READ - {"types"}, types={})
        self.assertEqual(
            self.added(["CREATE DOMAIN d AS int CHECK (VALUE > 0)"], unread),
            (Work.REWRITE, Confidence.KNOWN),
        )

    def test_a_domain_the_run_created_without_a_constraint_checks_nothing(self):
        unread = context(read=READ - {"types"}, types={})
        for found in (None, unread):
            with self.subTest(found=found):
                self.assertEqual(
                    self.added(["CREATE DOMAIN d AS int"], found),
                    (Work.CATALOG, Confidence.KNOWN),
                )

    def test_a_domain_over_a_constrained_domain_rewrites(self):
        self.assertEqual(
            self.added(["CREATE DOMAIN d AS positive"]),
            (Work.REWRITE, Confidence.KNOWN),
        )
        self.assertEqual(
            self.added(
                ["CREATE DOMAIN e AS int CHECK (VALUE > 0)", "CREATE DOMAIN d AS e"]
            ),
            (Work.REWRITE, Confidence.KNOWN),
        )
        self.assertEqual(
            self.added(["CREATE DOMAIN d AS plain"]), (Work.CATALOG, Confidence.KNOWN)
        )
        self.assertEqual(
            self.added(["CREATE DOMAIN d AS citext"]),
            (Work.REWRITE, Confidence.LIKELY),
        )

    def test_a_domain_created_again_replaces_the_read(self):
        found = context(types=MappingProxyType({"d": False}))
        self.assertEqual(self.added([], found), (Work.CATALOG, Confidence.KNOWN))
        self.assertEqual(
            self.added(
                ["DROP DOMAIN d", "CREATE DOMAIN d AS int CHECK (VALUE > 0)"], found
            ),
            (Work.REWRITE, Confidence.KNOWN),
        )

    def test_a_domain_the_run_dropped_is_not_found(self):
        found = context(types=MappingProxyType({"d": False}))
        self.assertEqual(
            self.added(["DROP DOMAIN d"], found), (Work.REWRITE, Confidence.LIKELY)
        )
