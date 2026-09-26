"""Tests for the ALGORITHM and LOCK clauses assert_algorithm writes."""

import unittest

from sustained.analysis import MigrationStatement, with_intent
from sustained.dialects import Dialects
from sustained.impact.rules.mysql import asserted_statements
from sustained.migrations import Migration
from sustained.migrations.planning import asserted_migration
from tests.test_impact_mysql import MARIADB, MYSQL

MY = Dialects.MYSQL
COMPILER = Dialects.get_compiler(MY)


def asserted(sql, ctx=MYSQL):
    (statement,) = asserted_statements([sql], ctx)
    return statement


class AssertedStatementsTestCase(unittest.TestCase):
    def test_an_instant_change_asserts_the_algorithm_alone(self):
        self.assertEqual(
            asserted("ALTER TABLE t ADD COLUMN x int"),
            "ALTER TABLE t ADD COLUMN x int, ALGORITHM=INSTANT",
        )

    def test_an_index_build_asserts_inplace_without_a_lock(self):
        self.assertEqual(
            asserted("CREATE INDEX ix2 ON t (c)"),
            "CREATE INDEX ix2 ON t (c) ALGORITHM=INPLACE LOCK=NONE",
        )
        self.assertEqual(
            asserted("DROP INDEX r_ix ON t"),
            "DROP INDEX r_ix ON t ALGORITHM=INPLACE LOCK=NONE",
        )

    def test_mariadb_asserts_nocopy_and_alters_to_drop_an_index(self):
        self.assertEqual(
            asserted("CREATE INDEX ix2 ON t (c)", MARIADB),
            "CREATE INDEX ix2 ON t (c) ALGORITHM=NOCOPY LOCK=NONE",
        )
        self.assertEqual(
            asserted("DROP INDEX r_ix ON t", MARIADB),
            "ALTER TABLE t DROP INDEX r_ix, ALGORITHM=NOCOPY, LOCK=NONE",
        )

    def test_a_copy_or_a_shared_lock_is_left(self):
        for sql in (
            "ALTER TABLE t MODIFY c bigint",
            "ALTER TABLE t ADD CONSTRAINT fk2 FOREIGN KEY (c) REFERENCES r (id)",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(asserted(sql), sql)
        # MariaDB copies with LOCK=NONE, which is no online form either.
        self.assertEqual(
            asserted("ALTER TABLE t MODIFY c bigint", MARIADB),
            "ALTER TABLE t MODIFY c bigint",
        )

    def test_a_statement_that_spells_a_clause_is_left(self):
        for sql in (
            "ALTER TABLE t ADD COLUMN y int, ALGORITHM=INPLACE",
            "ALTER TABLE t ADD COLUMN y int, LOCK=NONE",
            "CREATE INDEX ix2 ON t (c) ALGORITHM=INPLACE",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(asserted(sql), sql)

    def test_a_table_the_statements_created_is_left(self):
        statements = [
            "CREATE TABLE n (id int)",
            "CREATE INDEX nx ON n (id)",
            "ALTER TABLE n RENAME TO m",
            "ALTER TABLE m ADD COLUMN x int",
        ]
        self.assertEqual(asserted_statements(statements, MYSQL), statements)

    def test_other_statements_are_left(self):
        for sql in (
            "UPDATE t SET c = 1",
            "DROP TABLE p",
            "SET SESSION foreign_key_checks = 0",
            "SELECT 1 FROM",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(asserted(sql), sql)

    def test_a_prediction_short_of_known_is_left(self):
        # Without the instant row versions read, an instant ADD COLUMN
        # may have run out of them.
        tables = dict(MYSQL.tables)
        tables["t"] = tables["t"]._replace(row_versions=None)
        ctx = MYSQL._replace(tables=tables)
        statement = "ALTER TABLE t ADD COLUMN x int"
        self.assertEqual(asserted(statement, ctx), statement)

    def test_without_the_version_nothing_is_asserted(self):
        ctx = MYSQL._replace(read=frozenset())
        self.assertEqual(
            asserted("ALTER TABLE t ADD COLUMN x int", ctx),
            "ALTER TABLE t ADD COLUMN x int",
        )

    def test_a_migration_statement_keeps_what_it_holds(self):
        generated = with_intent(
            "ALTER TABLE t ADD COLUMN x int", "add_column", "t", "x"
        )
        statement = MigrationStatement(generated, "m1", False, True)
        (result,) = asserted_statements([statement], MYSQL)
        self.assertIsInstance(result, MigrationStatement)
        self.assertEqual(result, "ALTER TABLE t ADD COLUMN x int, ALGORITHM=INSTANT")
        self.assertEqual(result.migration_id, "m1")
        self.assertFalse(result.transactional)
        self.assertTrue(result.destructive)
        self.assertEqual(result.intent, generated.intent)


class AssertedMigrationTestCase(unittest.TestCase):
    def test_the_up_step_takes_the_clauses(self):
        migration = Migration(
            "gen",
            up=["ALTER TABLE t ADD COLUMN x int", "UPDATE t SET x = 1"],
            down=["ALTER TABLE t DROP COLUMN x"],
            transactional=False,
        )
        result = asserted_migration(migration, MY, COMPILER, MYSQL)
        self.assertEqual(result.id, "gen")
        self.assertEqual(
            result.up,
            ["ALTER TABLE t ADD COLUMN x int, ALGORITHM=INSTANT", "UPDATE t SET x = 1"],
        )
        self.assertEqual(result.down, ["ALTER TABLE t DROP COLUMN x"])
        self.assertFalse(result.transactional)

    def test_a_migration_without_a_change_is_returned_as_it_is(self):
        migration = Migration("gen", up=["ALTER TABLE t MODIFY c bigint"], down=None)
        self.assertIs(asserted_migration(migration, MY, COMPILER, MYSQL), migration)

    def test_another_dialect_is_left(self):
        migration = Migration("gen", up=["CREATE INDEX ix2 ON t (c)"], down=None)
        postgres = Dialects.POSTGRES
        self.assertIs(
            asserted_migration(
                migration, postgres, Dialects.get_compiler(postgres), MYSQL
            ),
            migration,
        )

    def test_a_callable_step_is_left(self):
        migration = Migration("gen", up=lambda connection: None, down=None)
        self.assertIs(asserted_migration(migration, MY, COMPILER, MYSQL), migration)


if __name__ == "__main__":
    unittest.main()
