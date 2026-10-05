"""Tests for the recognizer that reads statement text into a ParsedStatement."""

import unittest

from sustained.dialects import Dialects
from sustained.impact.model import UNKNOWN_KIND
from sustained.impact.recognizer import (
    ACTION_KINDS,
    STATEMENT_KINDS,
    classify_default,
    recognize,
)
from sustained.impact.recognizer.cursor import (
    Cursor,
    Unrecognized,
    closing,
    depths,
    read_name,
)
from sustained.impact.recognizer.sources import tables_read
from sustained.impact.tokens import tokenize

PG = Dialects.POSTGRES
MYSQL = Dialects.MYSQL
MSSQL = Dialects.MSSQL
SQLITE = Dialects.DEFAULT


def action(sql, dialect=PG):
    """The single ALTER TABLE action a statement holds."""
    parsed = recognize(sql, dialect)
    assert parsed.kind == "alter_table", (parsed, sql)
    assert len(parsed.actions) == 1, parsed.actions
    return parsed.actions[0]


class RecognizerTestCase(unittest.TestCase):
    def assertUnknown(self, sql, dialect=PG, table=None):
        parsed = recognize(sql, dialect)
        self.assertEqual(parsed.kind, UNKNOWN_KIND, (sql, parsed))
        self.assertFalse(parsed.known)
        self.assertTrue(parsed.options["reason"])
        self.assertEqual(parsed.table, table)
        return parsed


class CreateIndexTestCase(RecognizerTestCase):
    def test_plain_index(self):
        parsed = recognize('CREATE INDEX "ix_a" ON "app"."items" ("a", "b")', PG)
        self.assertEqual(parsed.kind, "create_index")
        self.assertEqual(parsed.table, "app.items")
        self.assertEqual(parsed.options["name"], "ix_a")
        self.assertEqual(parsed.options["columns"], 2)
        self.assertFalse(parsed.options["unique"])
        self.assertFalse(parsed.options["concurrently"])
        self.assertFalse(parsed.options["partial"])

    def test_every_postgres_option(self):
        parsed = recognize(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix ON ONLY t "
            "USING btree (lower(a), b DESC NULLS LAST) INCLUDE (c) "
            "NULLS NOT DISTINCT WITH (fillfactor = 70) TABLESPACE fast "
            "WHERE deleted_at IS NULL",
            PG,
        )
        options = parsed.options
        self.assertTrue(options["unique"])
        self.assertTrue(options["concurrently"])
        self.assertTrue(options["if_not_exists"])
        self.assertTrue(options["only"])
        self.assertEqual(options["using"], "btree")
        self.assertEqual(options["columns"], 2)
        self.assertEqual(options["with"], {"FILLFACTOR": "70"})
        self.assertTrue(options["partial"])

    def test_an_unnamed_index(self):
        parsed = recognize("CREATE INDEX ON t (a)", PG)
        self.assertIsNone(parsed.options["name"])
        self.assertEqual(parsed.table, "t")

    def test_mssql_online_and_filegroup(self):
        parsed = recognize(
            "CREATE NONCLUSTERED INDEX [ix] ON [dbo].[t] ([a]) "
            "WITH (ONLINE = ON, RESUMABLE = ON) ON [PRIMARY]",
            MSSQL,
        )
        self.assertEqual(parsed.table, "dbo.t")
        self.assertEqual(parsed.options["with"], {"ONLINE": "ON", "RESUMABLE": "ON"})

    def test_mysql_algorithm_and_lock(self):
        parsed = recognize(
            "CREATE FULLTEXT INDEX ix ON t (body) ALGORITHM = INPLACE LOCK=NONE", MYSQL
        )
        self.assertTrue(parsed.options["fulltext"])
        self.assertEqual(parsed.options["algorithm"], "INPLACE")
        self.assertEqual(parsed.options["lock"], "NONE")

    def test_an_unread_option_is_unknown(self):
        self.assertUnknown("CREATE INDEX ix ON t (a) FROBNICATE", table="t")

    def test_unique_without_index_is_unknown(self):
        self.assertUnknown("CREATE UNIQUE TABLE t (a int)")


class DropIndexTestCase(RecognizerTestCase):
    def test_postgres_names_no_table(self):
        parsed = recognize("DROP INDEX CONCURRENTLY IF EXISTS app.ix_a, ix_b CASCADE")
        self.assertEqual(parsed.kind, "drop_index")
        self.assertIsNone(parsed.table)
        self.assertEqual(parsed.options["name"], "app.ix_a")
        self.assertEqual(parsed.options["names"], ("app.ix_a", "ix_b"))
        self.assertTrue(parsed.options["concurrently"])
        self.assertTrue(parsed.options["if_exists"])

    def test_mysql_on_table(self):
        parsed = recognize("DROP INDEX `ix` ON `app`.`t` ALGORITHM=INPLACE", MYSQL)
        self.assertEqual(parsed.table, "app.t")
        self.assertEqual(parsed.options["algorithm"], "INPLACE")

    def test_mssql_forms(self):
        self.assertEqual(recognize("DROP INDEX [ix] ON [t]", MSSQL).table, "t")
        parsed = recognize("DROP INDEX t.ix", MSSQL)
        self.assertEqual(parsed.table, "t")
        self.assertEqual(parsed.options["name"], "ix")
        # Postgres reads the same text as schema.index.
        self.assertIsNone(recognize("DROP INDEX t.ix", PG).table)

    def test_unread_tail_is_unknown(self):
        self.assertUnknown("DROP INDEX ix ON t EXTRA", MYSQL, table="t")


class CreateAndDropTestCase(RecognizerTestCase):
    def test_create_table_records_its_references(self):
        parsed = recognize(
            'CREATE TABLE IF NOT EXISTS "kids" ("id" int PRIMARY KEY, '
            '"parent_id" int REFERENCES "app"."parents" ("id"), '
            "FOREIGN KEY (a) REFERENCES others (id)) WITH (fillfactor = 70)",
            PG,
        )
        self.assertEqual(parsed.kind, "create_table")
        self.assertEqual(parsed.table, "kids")
        self.assertTrue(parsed.options["if_not_exists"])
        self.assertEqual(parsed.options["references"], ("app.parents", "others"))
        self.assertFalse(parsed.options["as_select"])

    def test_create_table_forms(self):
        self.assertTrue(recognize("CREATE TEMP TABLE t (a int)").options["temporary"])
        self.assertTrue(
            recognize("CREATE TABLE t AS SELECT * FROM u").options["as_select"]
        )
        self.assertTrue(
            recognize("CREATE TABLE t (a int) SELECT a FROM u", MYSQL).options[
                "as_select"
            ]
        )
        self.assertEqual(
            recognize("CREATE TABLE t AS SELECT * FROM u JOIN v USING (i)").options[
                "reads"
            ],
            ("u", "v"),
        )
        self.assertEqual(
            recognize("CREATE TABLE t AS TABLE u WITH NO DATA").options["reads"],
            ("u",),
        )
        self.assertNotIn("reads", recognize("CREATE TABLE t (a int)").options)
        partition = recognize(
            "CREATE TABLE p_2026 PARTITION OF p FOR VALUES FROM (1) TO (2)"
        )
        self.assertEqual(partition.options["partition_of"], "p")
        self.assertFalse(partition.options["default_partition"])
        self.assertFalse(partition.options["partitioned"])
        for sql in (
            "CREATE TABLE p_d PARTITION OF p DEFAULT",
            "CREATE TABLE p_d PARTITION OF p (a NOT NULL) DEFAULT PARTITION BY LIST (a)",
        ):
            with self.subTest(sql):
                self.assertTrue(recognize(sql).options["default_partition"])
        self.assertTrue(
            recognize("CREATE TABLE p (a int) PARTITION BY RANGE (a)").options[
                "partitioned"
            ]
        )
        self.assertTrue(
            recognize(
                "CREATE TABLE p_1 PARTITION OF p FOR VALUES IN (1) PARTITION BY LIST (b)"
            ).options["partitioned"]
        )
        self.assertFalse(
            recognize(
                "CREATE TABLE t AS SELECT row_number() OVER (PARTITION BY a) FROM u"
            ).options["partitioned"]
        )

    def test_mssql_guarded_create_table(self):
        parsed = recognize(
            "IF OBJECT_ID(N'[t]', 'U') IS NULL CREATE TABLE [t] ([id] INT)", MSSQL
        )
        self.assertEqual((parsed.kind, parsed.table), ("create_table", "t"))

    def test_drops_of_tables_views_and_types(self):
        parsed = recognize("DROP TABLE IF EXISTS a, app.b CASCADE")
        self.assertEqual(parsed.kind, "drop_table")
        self.assertEqual(parsed.options["tables"], ("a", "app.b"))
        self.assertTrue(parsed.options["if_exists"])
        view = recognize("DROP MATERIALIZED VIEW v")
        self.assertEqual(view.kind, "drop_view")
        self.assertTrue(view.options["materialized"])
        dropped = recognize('DROP TYPE IF EXISTS "mood" CASCADE')
        self.assertEqual(dropped.kind, "drop_type")
        self.assertIsNone(dropped.table)
        self.assertEqual(dropped.options["names"], ("mood",))

    def test_views_triggers_and_types(self):
        view = recognize("CREATE OR REPLACE MATERIALIZED VIEW v AS SELECT 1")
        self.assertEqual(view.kind, "create_view")
        self.assertTrue(view.options["materialized"])
        trigger = recognize(
            "CREATE TRIGGER trg BEFORE INSERT OR UPDATE OF a ON app.t "
            "FOR EACH ROW EXECUTE FUNCTION f()"
        )
        self.assertEqual((trigger.kind, trigger.table), ("create_trigger", "app.t"))
        self.assertEqual(trigger.options["name"], "trg")
        sqlite_trigger = recognize(
            "CREATE TRIGGER rb AFTER INSERT ON rb_items "
            "BEGIN INSERT INTO rb_log VALUES (new.label); END",
            SQLITE,
        )
        self.assertEqual(sqlite_trigger.table, "rb_items")
        mysql_trigger = recognize(
            "CREATE DEFINER=`app`@`%` TRIGGER trg BEFORE INSERT ON t "
            "FOR EACH ROW SET NEW.a = 1",
            MYSQL,
        )
        self.assertEqual(mysql_trigger.table, "t")
        constraint_trigger = recognize(
            "CREATE CONSTRAINT TRIGGER trg AFTER INSERT ON t FOR EACH ROW "
            "EXECUTE FUNCTION f()"
        )
        self.assertEqual(constraint_trigger.table, "t")
        dropped = recognize("DROP TRIGGER IF EXISTS trg ON t CASCADE")
        self.assertEqual((dropped.kind, dropped.table), ("drop_trigger", "t"))
        self.assertIsNone(recognize("DROP TRIGGER trg", SQLITE).table)
        self.assertTrue(recognize("CREATE TYPE m AS ENUM ('a')").options["enum"])
        self.assertFalse(recognize("CREATE TYPE p AS (x int)").options["enum"])

    def test_a_trigger_with_no_table_is_unknown(self):
        self.assertUnknown("CREATE TRIGGER trg AFTER INSERT")

    def test_other_objects(self):
        for sql, kind, obj in (
            ("CREATE SCHEMA IF NOT EXISTS app", "create_object", "schema"),
            ("CREATE SEQUENCE s START 5", "create_object", "sequence"),
            (
                "CREATE OR REPLACE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ "
                "LANGUAGE sql",
                "create_object",
                "function",
            ),
            ("CREATE EXTENSION IF NOT EXISTS pgcrypto", "create_object", "extension"),
            ("DROP SEQUENCE s", "drop_object", "sequence"),
            ("DROP FUNCTION f(int)", "drop_object", "function"),
        ):
            with self.subTest(sql):
                parsed = recognize(sql, PG)
                self.assertEqual((parsed.kind, parsed.options["object"]), (kind, obj))

    def test_domains(self):
        for sql, name, type_text, constrained in (
            ("CREATE DOMAIN d AS int", "d", "int", False),
            (
                'CREATE DOMAIN app."D" varchar(10) COLLATE "C"',
                "app.D",
                "varchar(10)",
                False,
            ),
            ("CREATE DOMAIN d AS int DEFAULT 0 NOT NULL", "d", "int", True),
            ("CREATE DOMAIN d AS int NULL", "d", "int", False),
            (
                "CREATE DOMAIN d AS timestamp with time zone "
                "CONSTRAINT c CHECK (VALUE > now())",
                "d",
                "timestamp with time zone",
                True,
            ),
            ("CREATE DOMAIN d2 AS d", "d2", "d", False),
        ):
            with self.subTest(sql):
                parsed = recognize(sql, PG)
                self.assertEqual(parsed.kind, "create_object")
                self.assertEqual(
                    dict(parsed.options),
                    {
                        "object": "domain",
                        "name": name,
                        "type": type_text,
                        "constrained": constrained,
                    },
                )
        dropped = recognize("DROP DOMAIN IF EXISTS d, app.e CASCADE", PG)
        self.assertEqual(
            (dropped.kind, dict(dropped.options)),
            ("drop_object", {"object": "domain", "names": ("d", "app.e")}),
        )
        self.assertUnknown("ALTER DOMAIN d ADD CHECK (VALUE > 0)")
        self.assertUnknown("CREATE DOMAIN d NOT NULL")

    def test_unread_create_and_drop_are_unknown(self):
        for sql in (
            "CREATE ROLE app",
            "DROP ROLE app",
            "CREATE POLICY p ON t USING (true)",
        ):
            with self.subTest(sql):
                self.assertUnknown(sql)

    def test_cascade(self):
        self.assertTrue(recognize("DROP TABLE t CASCADE").options["cascade"])
        self.assertFalse(recognize("DROP TABLE t RESTRICT").options["cascade"])
        self.assertTrue(recognize("TRUNCATE r CASCADE").options["cascade"])
        self.assertFalse(recognize("TRUNCATE r").options["cascade"])
        (drop,) = recognize("ALTER TABLE t DROP CONSTRAINT k CASCADE").actions
        self.assertTrue(drop.options["cascade"])
        (drop,) = recognize("ALTER TABLE t DROP COLUMN c").actions
        self.assertFalse(drop.options["cascade"])


class MssqlRenameTestCase(RecognizerTestCase):
    def test_column_rename(self):
        parsed = recognize("EXEC sp_rename N'dbo.t.old', N'new', 'COLUMN'", MSSQL)
        self.assertEqual((parsed.kind, parsed.table), ("alter_table", "dbo.t"))
        self.assertEqual(parsed.actions[0].kind, "rename_column")
        self.assertEqual(parsed.actions[0].options["new"], "new")

    def test_table_rename(self):
        parsed = recognize("EXECUTE sp_rename 'dbo.t', 'u'", MSSQL)
        self.assertEqual((parsed.kind, parsed.table), ("rename_table", "dbo.t"))
        self.assertEqual(parsed.options["new"], "u")

    def test_index_rename(self):
        parsed = recognize("EXEC sp_rename 'dbo.t.ix', 'iy', 'INDEX'", MSSQL)
        self.assertEqual((parsed.kind, parsed.table), ("alter_table", "dbo.t"))
        (action,) = parsed.actions
        self.assertEqual(action.kind, "rename_index")
        self.assertEqual((action.options["old"], action.options["new"]), ("ix", "iy"))

    def test_other_procedures_and_kinds_are_unknown(self):
        self.assertUnknown("EXEC sp_who", MSSQL)
        self.assertUnknown("EXEC sp_rename 't.ix', 'iy', 'USERDATATYPE'", MSSQL)
        self.assertUnknown("EXEC sp_rename 'ix', 'iy', 'INDEX'", MSSQL)
        self.assertUnknown("EXEC sp_rename 't'", MSSQL)


class MssqlStatementsTestCase(RecognizerTestCase):
    def test_clustered_indexes(self):
        clustered = recognize("CREATE UNIQUE CLUSTERED INDEX cx ON t (a)", MSSQL)
        self.assertTrue(clustered.options["clustered"])
        self.assertTrue(clustered.options["unique"])
        plain = recognize("CREATE NONCLUSTERED INDEX ix ON t (a)", MSSQL)
        self.assertFalse(plain.options["clustered"])
        self.assertNotIn("clustered", recognize("CREATE INDEX ix ON t (a)").options)
        (key,) = recognize(
            "ALTER TABLE t ADD CONSTRAINT pk PRIMARY KEY NONCLUSTERED (id)", MSSQL
        ).actions
        self.assertFalse(key.options["clustered"])

    def test_a_filtered_index_reads_the_with_after_its_predicate(self):
        parsed = recognize(
            "CREATE INDEX ix ON t (a) INCLUDE (b) WHERE a > 0 AND b IN (1, 2) "
            "WITH (ONLINE = ON, RESUMABLE = ON) ON [PRIMARY]",
            MSSQL,
        )
        self.assertTrue(parsed.options["partial"])
        self.assertEqual(
            dict(parsed.options["with"]), {"ONLINE": "ON", "RESUMABLE": "ON"}
        )

    def test_drop_index_with_options(self):
        parsed = recognize("DROP INDEX ix ON t WITH (ONLINE = ON)", MSSQL)
        self.assertEqual(parsed.table, "t")
        self.assertEqual(dict(parsed.options["with"]), {"ONLINE": "ON"})
        self.assertUnknown("DROP INDEX ix ON t WITH (ONLINE = ON)", table="t")

    def test_alter_index(self):
        parsed = recognize(
            "ALTER INDEX ALL ON dbo.t REBUILD PARTITION = 2 WITH (ONLINE = ON "
            "(WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)))",
            MSSQL,
        )
        self.assertEqual((parsed.kind, parsed.table), ("alter_index", "dbo.t"))
        self.assertIsNone(parsed.options["name"])
        self.assertEqual(parsed.options["operation"], "rebuild")
        self.assertTrue(parsed.options["partition"])
        self.assertTrue(parsed.options["with"]["ONLINE"].startswith("ON (WAIT_AT"))
        reorganize = recognize("ALTER INDEX ix ON t REORGANIZE", MSSQL)
        self.assertEqual(reorganize.options["name"], "ix")
        self.assertEqual(reorganize.options["operation"], "reorganize")
        settings = recognize("ALTER INDEX ix ON t SET (ALLOW_PAGE_LOCKS = OFF)", MSSQL)
        self.assertEqual(dict(settings.options["with"]), {"ALLOW_PAGE_LOCKS": "OFF"})
        self.assertUnknown("ALTER INDEX ix ON t REBUILD EXTRA", MSSQL, table="t")
        self.assertUnknown("ALTER INDEX ix ON t REBUILD")

    def test_attach_index_on_postgres(self):
        parsed = recognize('ALTER INDEX "app"."ix" ATTACH PARTITION "app"."ix_a"', PG)
        self.assertEqual(parsed.kind, "attach_index")
        self.assertEqual(
            dict(parsed.options), {"name": "app.ix", "partition": "app.ix_a"}
        )
        self.assertUnknown("ALTER INDEX ix RENAME TO iy", PG)
        self.assertUnknown("ALTER INDEX ix ATTACH PARTITION ix_a EXTRA", PG)
        self.assertUnknown("ALTER INDEX ix ATTACH PARTITION ix_a", MYSQL)
        self.assertUnknown("ALTER INDEX ix ATTACH PARTITION ix_a", SQLITE)

    def test_rebuild_and_switch(self):
        (rebuild,) = recognize(
            "ALTER TABLE t REBUILD PARTITION = ALL WITH (ONLINE = ON)", MSSQL
        ).actions
        self.assertEqual(rebuild.kind, "rebuild")
        self.assertTrue(rebuild.options["partition"])
        self.assertEqual(dict(rebuild.options["with"]), {"ONLINE": "ON"})
        (switch,) = recognize(
            "ALTER TABLE t SWITCH PARTITION 1 TO dbo.u PARTITION 1 WITH "
            "(WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = NONE))",
            MSSQL,
        ).actions
        self.assertEqual((switch.kind, switch.options["target"]), ("switch", "dbo.u"))
        self.assertIn("WAIT_AT_LOW_PRIORITY", switch.options["with"])
        self.assertUnknown("ALTER TABLE t SWITCH TO u", table="t")
        self.assertUnknown("ALTER TABLE t REBUILD", table="t")

    def test_update_statistics(self):
        parsed = recognize("UPDATE STATISTICS dbo.t (ix, ix2) WITH FULLSCAN", MSSQL)
        self.assertEqual((parsed.kind, parsed.table), ("update_statistics", "dbo.t"))
        self.assertTrue(parsed.options["fullscan"])
        sampled = recognize("UPDATE STATISTICS t ix WITH SAMPLE 10 PERCENT", MSSQL)
        self.assertFalse(sampled.options["fullscan"])
        self.assertFalse(recognize("UPDATE STATISTICS t", MSSQL).options["fullscan"])


class TablesReadTestCase(unittest.TestCase):
    def reads(self, sql):
        return tables_read(list(tokenize(sql, PG)))

    def test_from_lists_joins_and_subqueries(self):
        self.assertEqual(
            self.reads(
                "SELECT x FROM a JOIN b ON a.i = b.i, c AS cc, ONLY d dd "
                "WHERE y IN (SELECT z FROM e) GROUP BY a, b"
            ),
            ("a", "b", "c", "d", "e"),
        )
        self.assertEqual(self.reads("SELECT * FROM (SELECT * FROM a) s, b"), ("a", "b"))
        self.assertEqual(self.reads("SELECT (SELECT max(i) FROM b) FROM a"), ("b", "a"))
        self.assertEqual(self.reads("TABLE app.a"), ("app.a",))

    def test_a_cte_is_not_a_table(self):
        self.assertEqual(
            self.reads("WITH w (i) AS (SELECT i FROM a) SELECT * FROM w"), ("a",)
        )

    def test_the_from_of_a_function_names_no_table(self):
        self.assertEqual(
            self.reads("SELECT extract(year FROM c), substring(n FROM 2) FROM a"),
            ("a",),
        )

    def test_no_table(self):
        self.assertEqual(self.reads("SELECT 1"), ())
        self.assertEqual(self.reads("VALUES (1)"), ())

    def test_rows_from_something_else(self):
        for sql in (
            "SELECT * FROM generate_series(1, 3)",
            "SELECT * FROM (VALUES (1)) v",
            "SELECT * FROM a, LATERAL unnest(a.x)",
            "SELECT * FROM (a JOIN b ON true)",
            "SELECT * FROM",
            "TABLE",
        ):
            with self.subTest(sql):
                self.assertIsNone(self.reads(sql))


class UnknownTestCase(RecognizerTestCase):
    def test_statements_no_rule_reads(self):
        for sql in (
            "SELECT 1",
            "GRANT SELECT ON t TO app",
            "DO $$ BEGIN PERFORM 1; END $$",
            "MERGE INTO t USING u ON true WHEN MATCHED THEN DELETE",
            "BEGIN",
            "(SELECT 1)",
            "",
            "  ;  ",
        ):
            with self.subTest(sql):
                self.assertUnknown(sql)

    def test_several_statements_are_unknown(self):
        parsed = self.assertUnknown("ALTER TABLE t DROP COLUMN a; DROP TABLE t")
        self.assertIn("more than one", parsed.options["reason"])

    def test_a_trailing_semicolon_is_fine(self):
        self.assertEqual(recognize("DROP TABLE t;;").kind, "drop_table")

    def test_an_unclosed_quote_is_unknown(self):
        self.assertUnknown("ALTER TABLE t ADD COLUMN c text DEFAULT 'x")

    def test_a_comment_is_ignored(self):
        parsed = recognize("-- add it\nALTER TABLE t /* note */ ADD COLUMN c int")
        self.assertEqual(parsed.actions[0].kind, "add_column")

    def test_statement_ends_early(self):
        self.assertUnknown("ALTER TABLE t ADD CONSTRAINT", table="t")
        self.assertUnknown("CREATE INDEX ix ON t (a", table="t")

    def test_every_kind_is_declared(self):
        self.assertIn(UNKNOWN_KIND, STATEMENT_KINDS)
        self.assertIn("alter_column_type", ACTION_KINDS)


A_REWRITE = {
    PG: "ALTER TABLE big ALTER COLUMN a TYPE bigint",
    Dialects.DUCKDB: "ALTER TABLE big ALTER COLUMN a TYPE bigint",
    MYSQL: "ALTER TABLE big MODIFY a bigint",
    MSSQL: "ALTER TABLE big ALTER COLUMN a bigint NOT NULL",
    SQLITE: "ALTER TABLE big DROP COLUMN a",
    None: "ALTER TABLE big ALTER COLUMN a TYPE bigint",
}


class StatementJoinsTestCase(RecognizerTestCase):
    """
    A readable statement joined to a second statement reads as unknown
    whenever the dialect's lexer sees the second statement's words.
    """

    READABLE = (
        "CREATE TABLE t (trigger int)",
        "CREATE TABLE t (a int, procedure int)",
        "UPDATE t SET a = 1 WHERE id = 1",
        "DELETE FROM t WHERE id = 1",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 'it''s' WHERE id = 1",
    )
    JOINS = (
        "{0}; {1}",
        "{0} -- x\n; {1}",
        "{0} -- x\r; {1}",
        "{0} # x\n; {1}",
        "{0} /* x */; {1}",
        "{0} /* /* x */ */; {1}",
        "{0} /* /* */ '; {1} -- '",
        "{0} /*! ; {1} */",
        "{0} --x\n; {1}",
        "{0} {1}",
    )

    def test_a_second_statement_the_lexer_reads_makes_the_text_unknown(self):
        for dialect, second in A_REWRITE.items():
            for first in self.READABLE:
                for join in self.JOINS:
                    sql = join.format(first, second)
                    words = [t.value for t in tokenize(sql, dialect)]
                    if "BIG" not in words:
                        continue
                    with self.subTest(dialect=dialect, sql=sql):
                        parsed = recognize(sql, dialect)
                        if dialect is MSSQL or ";" in words:
                            self.assertEqual(parsed.kind, UNKNOWN_KIND)

    def test_sql_server_needs_no_separator(self):
        second = A_REWRITE[MSSQL]
        for first in self.READABLE + (
            "SET LOCK_TIMEOUT 5000",
            "CREATE TABLE t (a int)",
            "ALTER TABLE t ADD c int DEFAULT 1",
            "INSERT INTO t SELECT 1",
        ):
            with self.subTest(first):
                parsed = recognize(f"{first} {second}", MSSQL)
                self.assertEqual(parsed.kind, UNKNOWN_KIND)


class RoutineBodyTestCase(RecognizerTestCase):
    """A `;` is read only inside the body of the routine a statement creates."""

    def test_text_the_body_check_cannot_place_is_unknown(self):
        for sql, dialect in (
            ("CREATE TABLE t (trigger int); " + A_REWRITE[PG], PG),
            ("CREATE TABLE function (a int); " + A_REWRITE[PG], PG),
            (
                "CREATE TRIGGER tr2 BEFORE INSERT ON begin FOR EACH ROW "
                "SET NEW.a = 1; " + A_REWRITE[MYSQL] + "; END",
                MYSQL,
            ),
            (
                "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN "
                "SET NEW.a = 1; END; " + A_REWRITE[MYSQL],
                MYSQL,
            ),
            (
                "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET NEW.a = "
                "begin + 1; " + A_REWRITE[MYSQL] + "; END",
                MYSQL,
            ),
            (
                "CREATE PROCEDURE p() BEGIN SELECT 1; END lbl; " + A_REWRITE[MYSQL],
                MYSQL,
            ),
            (
                "CREATE PROCEDURE p() BEGIN SELECT begin FROM t; END",
                MYSQL,
            ),
            (
                "CREATE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC "
                "SELECT 1; END; " + A_REWRITE[PG],
                PG,
            ),
            (
                "CREATE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC "
                "SELECT 1; BEGIN SELECT 2; END; END",
                PG,
            ),
            ("CREATE FUNCTION f() RETURNS int AS 'x'; " + A_REWRITE[PG], PG),
            ("CREATE PROCEDURE p; " + A_REWRITE[MSSQL], MSSQL),
            ("CREATE OR DROP TRIGGER t; " + A_REWRITE[PG], PG),
            ("CREATE DEFINER = 1 TRIGGER t; " + A_REWRITE[MYSQL], MYSQL),
            ("CREATE DEFINER => x TRIGGER t; " + A_REWRITE[MYSQL], MYSQL),
            ("CREATE DEFINER = a@1 TRIGGER t; " + A_REWRITE[MYSQL], MYSQL),
            ("CREATE DEFINER = CURRENT_USER(1) TRIGGER t; END", MYSQL),
            ("CREATE TRIGGER t BEGIN; END", MYSQL),
        ):
            with self.subTest(sql):
                parsed = recognize(sql, dialect)
                self.assertEqual(parsed.kind, UNKNOWN_KIND)

    def test_a_routine_body_may_contain_semicolons(self):
        for sql, dialect, kind in (
            (
                "CREATE TRIGGER rb AFTER INSERT ON rb_items BEGIN "
                "INSERT INTO rb_log VALUES (new.label); "
                "SELECT CASE WHEN 1 THEN 2 END; END",
                SQLITE,
                "create_trigger",
            ),
            (
                "CREATE DEFINER = 'app'@'%' TRIGGER tr BEFORE INSERT ON t "
                "FOR EACH ROW BEGIN IF NEW.a > 1 THEN SET NEW.a = 1; END IF; "
                "CASE NEW.b WHEN 1 THEN SET NEW.c = 1; END CASE; END",
                MYSQL,
                "create_trigger",
            ),
            (
                "CREATE DEFINER = CURRENT_USER() PROCEDURE p() BEGIN SELECT 1; "
                "lbl: BEGIN SELECT 2; END lbl; WHILE 0 DO BEGIN SELECT 3; END; "
                "END WHILE; END",
                MYSQL,
                "create_object",
            ),
            (
                "CREATE DEFINER = CURRENT_USER PROCEDURE p() BEGIN SELECT 1; END",
                MYSQL,
                "create_object",
            ),
            (
                "CREATE OR REPLACE FUNCTION f() RETURNS int LANGUAGE sql "
                "BEGIN ATOMIC SELECT 1; SELECT CASE WHEN true THEN 1 END; END",
                PG,
                "create_object",
            ),
            (
                "CREATE TRIGGER tr ON t AFTER INSERT AS BEGIN SELECT 1; END",
                MSSQL,
                "create_trigger",
            ),
            ("CREATE PROCEDURE p AS SELECT 1; SELECT 2", MSSQL, "create_object"),
        ):
            with self.subTest(sql):
                self.assertEqual(recognize(sql, dialect).kind, kind)


class ClassifyDefaultTestCase(unittest.TestCase):
    def classify(self, expression):
        return classify_default(tokenize(expression, PG))

    def test_constants(self):
        for expression in ("0", "'x'", "'x'::text", "-1.5", "NULL", "TRUE", "'{}'"):
            with self.subTest(expression):
                self.assertEqual(self.classify(expression), ("constant", None, True))

    def test_stable_values(self):
        for expression in (
            "now()",
            "CURRENT_TIMESTAMP",
            "current_setting('app.x')",
            "lower('A')",
        ):
            with self.subTest(expression):
                self.assertEqual(self.classify(expression), ("stable", None, True))

    def test_volatile_values(self):
        self.assertEqual(
            self.classify("gen_random_uuid()"), ("volatile", "gen_random_uuid", True)
        )
        self.assertEqual(
            self.classify("nextval('s'::regclass)"), ("volatile", "nextval", True)
        )
        self.assertEqual(
            self.classify("coalesce(clock_timestamp(), now())"),
            ("volatile", "clock_timestamp", True),
        )

    def test_an_unknown_function_is_likely_volatile(self):
        self.assertEqual(self.classify("my_func(1)"), ("volatile", "my_func", False))

    def test_a_cast_to_a_sized_type_is_not_a_call(self):
        self.assertEqual(self.classify("'x'::varchar(10)"), ("constant", None, True))
        self.assertEqual(
            self.classify("'x'::pg_catalog.varchar(10)"), ("constant", None, True)
        )

    def test_a_quoted_name_before_a_parenthesis_is_a_call(self):
        self.assertEqual(
            self.classify('"gen_random_uuid"()'), ("volatile", "gen_random_uuid", True)
        )
        self.assertEqual(self.classify('"NOW"()'), ("volatile", "NOW", False))

    def test_a_call_in_another_schema_is_a_user_function(self):
        self.assertEqual(self.classify("app.now()"), ("volatile", "app.now", False))
        self.assertEqual(self.classify("pg_catalog.now()"), ("stable", None, True))
        self.assertEqual(self.classify('"pg_catalog"."now"()'), ("stable", None, True))


if __name__ == "__main__":
    unittest.main()


class BatchSeparatorTestCase(RecognizerTestCase):
    """A SQL Server `GO` line separates batches and is not a statement."""

    def test_trailing_go_is_dropped(self):
        parsed = recognize("CREATE TABLE t (id int)\nGO", MSSQL)
        self.assertEqual(parsed.kind, "create_table")
        self.assertEqual(parsed.table, "t")

    def test_go_with_a_count_lower_case_and_crlf(self):
        parsed = recognize("\r\n  go 3  \r\nDROP TABLE t\r\ngo\r\n", MSSQL)
        self.assertEqual(parsed.kind, "drop_table")

    def test_go_before_a_semicolon_only_batch(self):
        parsed = recognize("TRUNCATE TABLE t;\nGO\n;", MSSQL)
        self.assertEqual(parsed.kind, "truncate")

    def test_go_sharing_a_line_is_a_word(self):
        parsed = recognize("UPDATE t SET go = 1 WHERE id = 2", MSSQL)
        self.assertEqual(parsed.kind, "update")
        self.assertUnknown("DROP TABLE t GO", MSSQL, table="t")

    def test_two_batches_are_unknown(self):
        parsed = self.assertUnknown("DROP TABLE a\nGO\nDROP TABLE b", MSSQL)
        self.assertEqual(
            parsed.options["reason"], "the text contains more than one batch"
        )

    def test_only_separators_is_empty(self):
        parsed = self.assertUnknown("GO\nGO 2\n", MSSQL)
        self.assertEqual(parsed.options["reason"], "the statement is empty")

    def test_other_dialects_keep_go(self):
        self.assertUnknown("DROP TABLE t\nGO", PG, table="t")


class DepthsTestCase(unittest.TestCase):
    def test_a_bracket_counts_at_the_depth_outside_it(self):
        tokens = tokenize("a (b [c]) d", PG)
        self.assertEqual(
            [(t.text, d) for _, t, d in depths(tokens)],
            [("a", 0), ("(", 0), ("b", 1), ("[", 1), ("c", 2), ("]", 1)]
            + [(")", 0), ("d", 0)],
        )

    def test_start_skips_tokens_and_keeps_their_index(self):
        tokens = tokenize("a ( b")
        self.assertEqual([(i, d) for i, _, d in depths(tokens, 1)], [(1, 0), (2, 1)])

    def test_square_brackets_nest_in_every_caller(self):
        tokens = tokenize("x[1, 2], (y), z[a JOIN b]", PG)
        cursor = Cursor("", tokens, PG)
        self.assertEqual(len(Cursor.split_top(tokens)), 3)
        self.assertFalse(cursor.top_level_word(tokens, "JOIN"))
        self.assertTrue(cursor.top_level_word(tokenize("x[a] JOIN b", PG), "JOIN"))
        parsed = recognize("INSERT INTO t VALUES (ARRAY[1, 2]), (ARRAY[3])")
        self.assertEqual(parsed.options["rows"], 2)
        parsed = recognize("UPDATE t SET a = b[1] FROM u WHERE t.id = u.id")
        self.assertEqual(parsed.table, "t")

    def test_closing_finds_the_matching_bracket_or_the_end(self):
        tokens = tokenize("(a (b) [c, d]) e", PG)
        self.assertEqual(closing(tokens, 0), 10)
        self.assertEqual(closing(tokens, 2), 4)
        self.assertEqual(closing(tokenize("(a (b)", PG), 0), 5)

    def test_cursor_readers_skip_commas_and_words_in_square_brackets(self):
        def cursor(sql):
            return Cursor(sql, tokenize(sql, PG), PG)

        reader = cursor("(a[1], b) c")
        self.assertEqual(len(reader.group()), 6)
        self.assertTrue(reader.is_word("C"))
        self.assertEqual(len(cursor("a[1, 2], b").item()), 6)
        self.assertEqual(len(cursor("a[1, 2]) b").item()), 6)
        self.assertEqual(len(cursor("a[x WHERE] WHERE b").up_to_word("WHERE")), 5)
        self.assertEqual(len(cursor("a[1, 2], b").expression(frozenset())), 6)
        self.assertEqual(len(cursor("a) b").rest()), 3)

    def test_an_unclosed_group_is_unrecognized(self):
        with self.assertRaises(Unrecognized):
            Cursor("(a", tokenize("(a", PG), PG).group()

    def test_a_derived_table_with_brackets_is_skipped(self):
        parsed = recognize(
            "UPDATE t SET a = 1 FROM (SELECT x[1] AS y) AS d, u WHERE t.id = u.id", PG
        )
        self.assertEqual(parsed.table, "t")
        parsed = recognize("DELETE FROM t USING u) JOIN v WHERE t.id = 1", PG)
        self.assertEqual(parsed.table, "t")


class ReadNameTestCase(unittest.TestCase):
    def test_a_dotted_name_and_the_index_after_it(self):
        tokens = tokenize('a."B".c (x)', PG)
        self.assertEqual(read_name(tokens, 0), (["a", "B", "c"], 5))

    def test_no_name_reads_nothing(self):
        self.assertEqual(read_name(tokenize("( a", PG), 0), ([], 0))

    def test_a_trailing_dot_is_read(self):
        self.assertEqual(read_name(tokenize("a.", PG), 0), (["a"], 2))

    def test_the_cursor_rejects_a_trailing_dot(self):
        self.assertEqual(
            recognize("DROP TABLE a.").options["reason"][:15], "expected a name"
        )
