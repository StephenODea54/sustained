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


class AlterTableTestCase(RecognizerTestCase):
    def test_table_options(self):
        parsed = recognize("ALTER TABLE IF EXISTS ONLY app.t DROP COLUMN c")
        self.assertEqual(parsed.table, "app.t")
        self.assertTrue(parsed.options["if_exists"])
        self.assertTrue(parsed.options["only"])

    def test_several_actions_in_order(self):
        parsed = recognize(
            "ALTER TABLE t ADD COLUMN a int, DROP COLUMN b, ALTER COLUMN c SET NOT NULL"
        )
        self.assertEqual(
            [(a.kind, a.column) for a in parsed.actions],
            [("add_column", "a"), ("drop_column", "b"), ("set_not_null", "c")],
        )

    def test_add_column_definition(self):
        added = action(
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS c varchar(20) NOT NULL "
            "DEFAULT 'x' REFERENCES u (id) ON DELETE SET NULL UNIQUE CHECK (c <> '')"
        )
        self.assertEqual(added.kind, "add_column")
        self.assertEqual(added.column, "c")
        options = added.options
        self.assertEqual(options["type"], "varchar(20)")
        self.assertTrue(options["not_null"])
        self.assertEqual(options["default"], "'x'")
        self.assertEqual(options["default_volatility"], "constant")
        self.assertEqual(options["references"], "u")
        self.assertTrue(options["unique"])
        self.assertTrue(options["check"])
        self.assertTrue(options["if_not_exists"])

    def test_add_column_without_the_column_keyword(self):
        self.assertEqual(action("ALTER TABLE t ADD c int").column, "c")

    def test_multi_word_and_array_types(self):
        cases = {
            "timestamp with time zone": "timestamp with time zone",
            "double precision NOT NULL": "double precision",
            "character varying(10)": "character varying(10)",
            "int[] DEFAULT '{}'": "int[]",
            "numeric(10, 2)": "numeric(10, 2)",
            '"my type"': '"my type"',
        }
        for definition, type_sql in cases.items():
            with self.subTest(definition):
                added = action(f"ALTER TABLE t ADD COLUMN c {definition}")
                self.assertEqual(added.options["type"], type_sql)

    def test_serial_identity_and_generated_columns(self):
        self.assertTrue(action("ALTER TABLE t ADD c bigserial").options["serial"])
        identity = action(
            "ALTER TABLE t ADD c int GENERATED BY DEFAULT AS IDENTITY (START 5)"
        ).options
        self.assertEqual(identity["generated"], "identity")
        self.assertTrue(identity["identity"])
        stored = action(
            "ALTER TABLE t ADD c int GENERATED ALWAYS AS (a + b) STORED"
        ).options
        self.assertEqual(stored["generated"], "stored")
        virtual = action("ALTER TABLE t ADD c int AS (a + b) VIRTUAL", MYSQL).options
        self.assertEqual(virtual["generated"], "virtual")
        self.assertTrue(
            action("ALTER TABLE t ADD c INT IDENTITY(1,1)", MSSQL).options["identity"]
        )

    def test_more_column_forms(self):
        keyed = action("ALTER TABLE t ADD c int PRIMARY KEY", PG).options
        self.assertTrue(keyed["primary_key"])
        self.assertEqual(action("ALTER TABLE t ADD c int[3]").options["type"], "int[3]")
        self.assertTrue(
            action("ALTER TABLE t ADD c int AUTO_INCREMENT", MYSQL).options["identity"]
        )
        self.assertEqual(
            action("ALTER TABLE t ALTER COLUMN c SET INVISIBLE", MYSQL).kind,
            "set_storage",
        )
        self.assertUnknown("ALTER TABLE t ADD c int NOT DEFERRABLE", table="t")
        self.assertUnknown("ALTER TABLE t ADD c (int)", table="t")

    def test_malformed_clauses_are_unknown(self):
        for sql, dialect in (
            ("ALTER TABLE t WITH SOMETHING ADD c int", MSSQL),
            ("ALTER TABLE t ADD CONSTRAINT c NOTHING", PG),
            ("ALTER TABLE t ADD FOREIGN KEY (a) REFERENCES u ON DELETE EXPLODE", PG),
            ("ALTER TABLE t CONVERT TO utf8", MYSQL),
            ("ALTER TABLE t,", PG),
            ("ALTER TABLE t ADD c int DEFAULT", PG),
            ("CREATE INDEX ix ON t (a) WITH (= 1)", PG),
        ):
            with self.subTest(sql):
                self.assertUnknown(sql, dialect, table="t")
        self.assertUnknown("ALTER SEQUENCE s RESTART")

    def test_mysql_column_attributes(self):
        added = action(
            "ALTER TABLE t ADD COLUMN c VARCHAR(10) CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_bin NULL COMMENT 'x' AFTER b",
            MYSQL,
        )
        self.assertFalse(added.options["not_null"])
        self.assertEqual(added.options["position"], "after b")
        self.assertEqual(
            action("ALTER TABLE t ADD c INT FIRST", MYSQL).options["position"], "first"
        )
        updated = action(
            "ALTER TABLE t ADD c TIMESTAMP DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP",
            MYSQL,
        )
        self.assertEqual(updated.options["default"], "CURRENT_TIMESTAMP")

    def test_mssql_not_null_default_with_values(self):
        added = action(
            "ALTER TABLE t ADD c INT NOT NULL CONSTRAINT df DEFAULT 0 WITH VALUES",
            MSSQL,
        )
        self.assertTrue(added.options["not_null"])
        self.assertEqual(added.options["default"], "0")
        self.assertTrue(added.options["with_values"])

    def test_mssql_lists_more_columns_after_one_add_or_drop(self):
        parsed = recognize("ALTER TABLE t ADD a INT, b INT NULL", MSSQL)
        self.assertEqual([a.column for a in parsed.actions], ["a", "b"])
        parsed = recognize("ALTER TABLE t DROP COLUMN a, b", MSSQL)
        self.assertEqual(
            [(a.kind, a.column) for a in parsed.actions],
            [("drop_column", "a"), ("drop_column", "b")],
        )
        self.assertUnknown("ALTER TABLE t ADD a INT, b INT", PG, table="t")

    def test_constraints(self):
        cases = [
            ("ADD CONSTRAINT pk PRIMARY KEY (id)", "primary_key", {"name": "pk"}),
            (
                "ADD CONSTRAINT uq UNIQUE USING INDEX ix_uq",
                "unique",
                {"using_index": "ix_uq"},
            ),
            ("ADD UNIQUE NULLS NOT DISTINCT (a, b)", "unique", {"name": None}),
            (
                "ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES app.u (id) "
                "ON DELETE CASCADE ON UPDATE NO ACTION MATCH FULL "
                "DEFERRABLE INITIALLY DEFERRED NOT VALID",
                "foreign_key",
                {"references": "app.u", "not_valid": True},
            ),
            ("ADD CHECK (a > 0) NOT VALID", "check", {"not_valid": True}),
            (
                "ADD CONSTRAINT ex EXCLUDE USING gist (r WITH &&)",
                "exclude",
                {"name": "ex"},
            ),
        ]
        for clause, constraint, expected in cases:
            with self.subTest(clause):
                added = action(f"ALTER TABLE t {clause}")
                self.assertEqual(added.kind, "add_constraint")
                self.assertEqual(added.options["constraint"], constraint)
                for key, value in expected.items():
                    self.assertEqual(added.options[key], value)

    def test_mysql_keys_and_indexes(self):
        unique = action("ALTER TABLE t ADD UNIQUE KEY uq_a (a)", MYSQL)
        self.assertEqual(unique.options["name"], "uq_a")
        index = action("ALTER TABLE t ADD INDEX ix_a USING BTREE (a)", MYSQL)
        self.assertEqual(index.kind, "add_index")
        self.assertEqual(index.options["name"], "ix_a")
        fulltext = action("ALTER TABLE t ADD FULLTEXT KEY ft (body)", MYSQL)
        self.assertTrue(fulltext.options["fulltext"])
        fk = action("ALTER TABLE t ADD FOREIGN KEY fk_a (a) REFERENCES u (id)", MYSQL)
        self.assertEqual(fk.options["name"], "fk_a")

    def test_mssql_check_forms(self):
        parsed = recognize(
            "ALTER TABLE t WITH NOCHECK ADD CONSTRAINT ck CHECK (a > 0)", MSSQL
        )
        self.assertTrue(parsed.options["nocheck"])
        self.assertEqual(parsed.actions[0].options["constraint"], "check")
        parsed = recognize("ALTER TABLE t WITH CHECK CHECK CONSTRAINT ck", MSSQL)
        self.assertFalse(parsed.options["nocheck"])
        self.assertEqual(parsed.actions[0].kind, "enable_constraint")
        self.assertEqual(
            action("ALTER TABLE t NOCHECK CONSTRAINT ck", MSSQL).kind,
            "disable_constraint",
        )
        default = action("ALTER TABLE [t] ADD DEFAULT (N'raw') FOR [grade]", MSSQL)
        self.assertEqual((default.kind, default.column), ("set_default", "grade"))

    def test_drops(self):
        cases = [
            ("DROP CONSTRAINT IF EXISTS ck CASCADE", "drop_constraint", None),
            ("DROP FOREIGN KEY fk", "drop_constraint", None),
            ("DROP PRIMARY KEY", "drop_constraint", None),
            ("DROP CHECK ck", "drop_constraint", None),
            ("DROP INDEX ix", "drop_index", None),
            ("DROP KEY ix", "drop_index", None),
            ("DROP COLUMN IF EXISTS c RESTRICT", "drop_column", "c"),
            ("DROP c", "drop_column", "c"),
        ]
        for clause, kind, column in cases:
            with self.subTest(clause):
                dropped = action(f"ALTER TABLE t {clause}", MYSQL)
                self.assertEqual((dropped.kind, dropped.column), (kind, column))
        self.assertUnknown("ALTER TABLE t DROP PARTITION p1", MYSQL, table="t")

    def test_alter_column_forms(self):
        cases = [
            ("ALTER COLUMN c TYPE bigint", "alter_column_type"),
            ("ALTER c SET DATA TYPE text", "alter_column_type"),
            ("ALTER COLUMN c SET NOT NULL", "set_not_null"),
            ("ALTER COLUMN c DROP NOT NULL", "drop_not_null"),
            ("ALTER COLUMN c SET DEFAULT now()", "set_default"),
            ("ALTER COLUMN c DROP DEFAULT", "drop_default"),
            ("ALTER COLUMN c SET STATISTICS 500", "set_statistics"),
            ("ALTER COLUMN c SET STORAGE EXTERNAL", "set_storage"),
        ]
        for clause, kind in cases:
            with self.subTest(clause):
                altered = action(f"ALTER TABLE t {clause}")
                self.assertEqual((altered.kind, altered.column), (kind, "c"))

    def test_type_change_details(self):
        altered = action(
            'ALTER TABLE t ALTER COLUMN "c" TYPE varchar(20) COLLATE "C" '
            "USING c::varchar(20)"
        )
        self.assertEqual(altered.options["type"], "varchar(20)")
        self.assertEqual(altered.options["collate"], "C")
        self.assertEqual(altered.options["using"], "c::varchar(20)")
        self.assertIsNone(action("ALTER TABLE t ALTER c TYPE int").options["using"])

    def test_set_default_is_classified(self):
        altered = action("ALTER TABLE t ALTER COLUMN c SET DEFAULT gen_random_uuid()")
        self.assertEqual(altered.options["default_volatility"], "volatile")

    def test_mssql_alter_column_restates_the_column(self):
        altered = action(
            "ALTER TABLE [t] ALTER COLUMN [c] NVARCHAR(20) COLLATE Latin1_General_CI_AS "
            "NOT NULL WITH (ONLINE = ON)",
            MSSQL,
        )
        self.assertEqual(altered.kind, "alter_column")
        self.assertEqual(altered.options["type"], "NVARCHAR(20)")
        self.assertTrue(altered.options["not_null"])
        self.assertEqual(altered.options["with"], {"ONLINE": "ON"})

    def test_unread_alter_column_is_unknown(self):
        for clause in (
            "ALTER COLUMN c SET COMPRESSION lz4",
            "ALTER COLUMN c DROP IDENTITY",
            "ALTER CONSTRAINT fk DEFERRABLE",
        ):
            with self.subTest(clause):
                self.assertUnknown(f"ALTER TABLE t {clause}", table="t")

    def test_mysql_modify_and_change(self):
        modified = action(
            "ALTER TABLE t MODIFY COLUMN c VARCHAR(50) NOT NULL AFTER b", MYSQL
        )
        self.assertEqual(modified.kind, "modify_column")
        self.assertEqual(modified.options["type"], "VARCHAR(50)")
        changed = action("ALTER TABLE t CHANGE old new INT NULL", MYSQL)
        self.assertEqual((changed.kind, changed.column), ("change_column", "old"))
        self.assertEqual(changed.options["new"], "new")

    def test_renames(self):
        cases = [
            ("RENAME COLUMN a TO b", "rename_column", "a"),
            ("RENAME a TO b", "rename_column", "a"),
            ("RENAME TO u", "rename_to", None),
            ("RENAME AS u", "rename_to", None),
            ("RENAME CONSTRAINT a TO b", "rename_constraint", None),
            ("RENAME INDEX a TO b", "rename_index", None),
        ]
        for clause, kind, column in cases:
            with self.subTest(clause):
                renamed = action(f"ALTER TABLE t {clause}", MYSQL)
                self.assertEqual((renamed.kind, renamed.column), (kind, column))
                self.assertIn(renamed.options["new"], ("b", "u"))

    def test_partitions(self):
        attached = action(
            "ALTER TABLE p ATTACH PARTITION p_2026 FOR VALUES FROM ('2026-01-01') "
            "TO ('2027-01-01')"
        )
        self.assertEqual(attached.kind, "attach_partition")
        self.assertEqual(attached.options["partition"], "p_2026")
        self.assertEqual(
            action("ALTER TABLE p ATTACH PARTITION d DEFAULT").kind, "attach_partition"
        )
        detached = action("ALTER TABLE p DETACH PARTITION p_2026 CONCURRENTLY")
        self.assertTrue(detached.options["concurrently"])
        self.assertTrue(
            action("ALTER TABLE p DETACH PARTITION x FINALIZE").options["finalize"]
        )
        self.assertUnknown("ALTER TABLE p ATTACH PARTITION x", table="p")

    def test_table_level_actions(self):
        cases = [
            ("VALIDATE CONSTRAINT fk", "validate_constraint"),
            ("SET TABLESPACE fast", "set_tablespace"),
            ("SET LOGGED", "set_logged"),
            ("SET UNLOGGED", "set_unlogged"),
            ("SET SCHEMA archive", "set_schema"),
            ("SET (fillfactor = 70)", "set_parameters"),
            ("OWNER TO app", "owner_to"),
            ("ENABLE TRIGGER trg", "enable_trigger"),
            ("DISABLE TRIGGER ALL", "disable_trigger"),
            ("ENABLE ALWAYS TRIGGER trg", "enable_trigger"),
            ("ENABLE ROW LEVEL SECURITY", "row_security"),
        ]
        for clause, kind in cases:
            with self.subTest(clause):
                self.assertEqual(action(f"ALTER TABLE t {clause}").kind, kind)
        for clause in ("SET WITHOUT CLUSTER", "ENABLE RULE r"):
            with self.subTest(clause):
                self.assertUnknown(f"ALTER TABLE t {clause}", table="t")

    def test_mysql_table_options(self):
        parsed = recognize(
            "ALTER TABLE t ADD COLUMN c INT, ALGORITHM=INSTANT, LOCK = NONE", MYSQL
        )
        self.assertEqual(parsed.options["algorithm"], "INSTANT")
        self.assertEqual(parsed.options["lock"], "NONE")
        self.assertEqual([a.kind for a in parsed.actions], ["add_column"])
        self.assertEqual(action("ALTER TABLE t ENGINE = InnoDB", MYSQL).kind, "engine")
        converted = action(
            "ALTER TABLE t CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_bin", MYSQL
        )
        self.assertEqual(converted.options["charset"], "utf8mb4")
        self.assertEqual(action("ALTER TABLE t FORCE", MYSQL).kind, "force")

    def test_an_alter_with_only_table_options_is_unknown(self):
        self.assertUnknown("ALTER TABLE t ALGORITHM=INPLACE", MYSQL, table="t")

    def test_unknown_action_keeps_the_table(self):
        parsed = self.assertUnknown(
            "ALTER TABLE app.t ADD COLUMN a int, CLUSTER ON ix", table="app.t"
        )
        self.assertIn("CLUSTER", parsed.options["reason"])

    def test_athena_add_columns_is_unknown(self):
        self.assertUnknown(
            "ALTER TABLE `t` ADD COLUMNS (`note` STRING)", Dialects.ATHENA, table="t"
        )


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
        partition = recognize(
            "CREATE TABLE p_2026 PARTITION OF p FOR VALUES FROM (1) TO (2)"
        )
        self.assertEqual(partition.options["partition_of"], "p")

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


class DmlTestCase(RecognizerTestCase):
    def test_update(self):
        parsed = recognize('UPDATE "t" SET "a" = 1 WHERE "a" IS NULL')
        self.assertEqual((parsed.kind, parsed.table), ("update", "t"))
        self.assertTrue(parsed.options["where"])
        self.assertFalse(parsed.options["limited"])
        every = recognize("UPDATE ONLY t AS x SET a = (SELECT 1 WHERE true)")
        self.assertFalse(every.options["where"])

    def test_capped_writes(self):
        self.assertTrue(
            recognize("UPDATE TOP (100) t SET a = 1", MSSQL).options["limited"]
        )
        self.assertTrue(
            recognize("DELETE FROM t WHERE a < 5 LIMIT 1000", MYSQL).options["limited"]
        )

    def test_delete_forms(self):
        parsed = recognize("DELETE FROM ONLY t USING u WHERE t.a = u.a")
        self.assertEqual((parsed.kind, parsed.table), ("delete", "t"))
        self.assertTrue(parsed.options["where"])
        self.assertEqual(recognize("DELETE t WHERE a = 1", MSSQL).table, "t")
        self.assertEqual(
            recognize("DELETE TOP (10) FROM t", MSSQL).options["limited"], True
        )
        self.assertFalse(recognize("DELETE FROM t").options["where"])

    def test_insert_forms(self):
        values = recognize("INSERT INTO t (a, b) VALUES (1, 2), (3, 4) RETURNING a")
        self.assertEqual((values.kind, values.table), ("insert", "t"))
        self.assertEqual(values.options["source"], "values")
        self.assertEqual(values.options["rows"], 2)
        select = recognize('INSERT INTO "new" ("id") SELECT "id" FROM "old"')
        self.assertEqual(select.options["source"], "select")
        self.assertIsNone(select.options["rows"])
        self.assertEqual(
            recognize("INSERT INTO t DEFAULT VALUES").options["source"], "default"
        )
        self.assertEqual(
            recognize("INSERT IGNORE INTO t (a) VALUES (1)", MYSQL).table, "t"
        )
        self.assertEqual(
            recognize("INSERT INTO t (SELECT * FROM u)").options["source"], "select"
        )
        self.assertUnknown("INSERT INTO t SET a = 1", MYSQL, table="t")

    def test_with_prefixed_writes(self):
        parsed = recognize(
            "WITH RECURSIVE ids (id) AS NOT MATERIALIZED (SELECT 1), "
            "more AS (SELECT 2) UPDATE t SET a = 1 FROM ids WHERE t.id = ids.id"
        )
        self.assertEqual((parsed.kind, parsed.table), ("update", "t"))

    def test_a_writing_cte_is_unknown(self):
        self.assertUnknown(
            "WITH gone AS (DELETE FROM t RETURNING *) INSERT INTO u SELECT * FROM gone"
        )
        self.assertUnknown("WITH x AS (SELECT 1) SELECT * FROM x")

    def test_update_without_set_is_unknown(self):
        self.assertUnknown("UPDATE t", table="t")


class MaintenanceTestCase(RecognizerTestCase):
    def test_truncate_and_rename(self):
        truncated = recognize("TRUNCATE TABLE ONLY a, b RESTART IDENTITY CASCADE")
        self.assertEqual(truncated.options["tables"], ("a", "b"))
        renamed = recognize("RENAME TABLE a TO b, c TO d", MYSQL)
        self.assertEqual((renamed.kind, renamed.table), ("rename_table", "a"))
        self.assertEqual(renamed.options["renames"], (("a", "b"), ("c", "d")))

    def test_reindex(self):
        parsed = recognize("REINDEX (VERBOSE) TABLE CONCURRENTLY app.t")
        self.assertEqual((parsed.kind, parsed.table), ("reindex", "app.t"))
        self.assertTrue(parsed.options["concurrently"])
        index = recognize("REINDEX INDEX ix")
        self.assertIsNone(index.table)
        self.assertEqual(index.options["target"], "index")

    def test_vacuum_and_analyze(self):
        self.assertTrue(recognize("VACUUM FULL ANALYZE t").options["full"])
        self.assertTrue(recognize("VACUUM (FULL, VERBOSE) t (a), u").options["full"])
        self.assertEqual(recognize("VACUUM (FULL) t, u").options["tables"], ("t", "u"))
        self.assertFalse(recognize("VACUUM", SQLITE).options["full"])
        self.assertEqual(recognize("ANALYZE VERBOSE t").table, "t")
        self.assertIsNone(recognize("ANALYZE").table)

    def test_cluster_and_optimize(self):
        self.assertEqual(recognize("CLUSTER t USING ix").options["index"], "ix")
        self.assertEqual(recognize("CLUSTER ix ON t").table, "t")
        self.assertIsNone(recognize("CLUSTER").table)
        self.assertEqual(recognize("CLUSTER VERBOSE t").table, "t")
        parsed = recognize("OPTIMIZE LOCAL TABLE a, b", MYSQL)
        self.assertEqual(parsed.options["tables"], ("a", "b"))

    def test_refresh(self):
        parsed = recognize("REFRESH MATERIALIZED VIEW CONCURRENTLY v WITH DATA")
        self.assertTrue(parsed.options["concurrently"])
        self.assertTrue(parsed.options["with_data"])
        self.assertFalse(
            recognize("REFRESH MATERIALIZED VIEW v WITH NO DATA").options["with_data"]
        )

    def test_comments(self):
        column = recognize("COMMENT ON COLUMN app.t.c IS 'x'")
        self.assertEqual((column.table, column.options["column"]), ("app.t", "c"))
        self.assertEqual(recognize("COMMENT ON TABLE t IS NULL").table, "t")
        self.assertEqual(recognize("COMMENT ON CONSTRAINT ck ON t IS 'x'").table, "t")
        self.assertIsNone(recognize("COMMENT ON INDEX ix IS 'x'").table)
        self.assertIsNone(recognize("COMMENT ON FUNCTION f(int) IS 'x'").table)
        self.assertUnknown("COMMENT ON COLUMN c IS 'x'")
        self.assertUnknown("COMMENT ON ROLE r IS 'x'")

    def test_enum_values(self):
        added = recognize("ALTER TYPE mood ADD VALUE IF NOT EXISTS 'sad' AFTER 'ok'")
        self.assertEqual(added.kind, "alter_type_add_value")
        self.assertEqual(added.options["value"], "sad")
        self.assertEqual(
            recognize("ALTER TYPE mood RENAME VALUE 'a' TO 'b'").kind,
            "alter_type_rename_value",
        )
        self.assertUnknown("ALTER TYPE mood OWNER TO app")

    def test_lock_table(self):
        parsed = recognize("LOCK TABLE ONLY a, b IN SHARE ROW EXCLUSIVE MODE NOWAIT")
        self.assertEqual(parsed.options["mode"], "SHARE ROW EXCLUSIVE")
        self.assertTrue(parsed.options["nowait"])
        self.assertEqual(recognize("LOCK t").options["mode"], "ACCESS EXCLUSIVE")
        self.assertUnknown("LOCK TABLE t IN SOME MODE", table="t")
        self.assertUnknown("LOCK TABLES t WRITE", MYSQL)


class SetTestCase(RecognizerTestCase):
    def settings(self, sql, dialect=PG):
        parsed = recognize(sql, dialect)
        self.assertEqual(parsed.kind, "set", parsed)
        return parsed.options["settings"]

    def test_postgres_scopes(self):
        self.assertEqual(
            self.settings("SET lock_timeout = '5s'"),
            (("session", "lock_timeout", "5s"),),
        )
        self.assertEqual(
            self.settings("SET LOCAL lock_timeout TO 0"),
            (("local", "lock_timeout", "0"),),
        )
        self.assertEqual(
            self.settings("SET search_path TO app, public"),
            (("session", "search_path", "app, public"),),
        )
        self.assertEqual(
            self.settings("SET TIME ZONE 'UTC'"), (("session", "timezone", "UTC"),)
        )

    def test_mysql_forms(self):
        self.assertEqual(
            self.settings("SET foreign_key_checks = 0, unique_checks = 0", MYSQL),
            (
                ("session", "foreign_key_checks", "0"),
                ("session", "unique_checks", "0"),
            ),
        )
        self.assertEqual(
            self.settings("SET @@GLOBAL.lock_wait_timeout = 5", MYSQL),
            (("global", "lock_wait_timeout", "5"),),
        )
        self.assertEqual(self.settings("SET @x = 1", MYSQL), (("user", "x", "1"),))

    def test_mssql_and_sqlite_forms(self):
        self.assertEqual(
            self.settings("SET LOCK_TIMEOUT 5000", MSSQL),
            (("session", "lock_timeout", "5000"),),
        )
        self.assertEqual(
            self.settings("PRAGMA foreign_keys = OFF", SQLITE),
            (("pragma", "foreign_keys", "OFF"),),
        )
        self.assertEqual(
            self.settings("PRAGMA main.journal_mode", SQLITE),
            (("pragma", "main.journal_mode", ""),),
        )
        self.assertEqual(
            self.settings("PRAGMA busy_timeout(500)", SQLITE),
            (("pragma", "busy_timeout", "500"),),
        )

    def test_a_setting_with_no_value_is_unknown(self):
        self.assertUnknown("SET lock_timeout =")


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

    def test_other_procedures_and_kinds_are_unknown(self):
        self.assertUnknown("EXEC sp_who", MSSQL)
        self.assertUnknown("EXEC sp_rename 't.ix', 'iy', 'INDEX'", MSSQL)
        self.assertUnknown("EXEC sp_rename 't'", MSSQL)


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


if __name__ == "__main__":
    unittest.main()
