"""
The PostgreSQL rules: each one's id, the documentation it relies on,
and its fixtures, with the objects the fixtures name.
"""

from __future__ import annotations

from typing import Tuple

from sustained.impact.rules import Rule

DOCS = "https://www.postgresql.org/docs/current/"
_ALTER_TABLE = DOCS + "sql-altertable.html"

ADD_COLUMN = Rule(
    "pg.add_column",
    _ALTER_TABLE,
    (
        "ALTER TABLE t ADD COLUMN d integer",
        "ALTER TABLE t ADD COLUMN d integer NOT NULL DEFAULT 0",
        "ALTER TABLE t ADD COLUMN d timestamptz DEFAULT now()",
    ),
)
ADD_COLUMN_REWRITE = Rule(
    "pg.add_column.rewrite",
    _ALTER_TABLE,
    (
        "ALTER TABLE t ADD COLUMN d uuid DEFAULT gen_random_uuid()",
        "ALTER TABLE t ADD COLUMN d serial",
        "ALTER TABLE t ADD COLUMN d integer GENERATED ALWAYS AS (id * 2) STORED",
    ),
)
ADD_COLUMN_KEY = Rule(
    "pg.add_column.key",
    _ALTER_TABLE,
    ("ALTER TABLE t ADD COLUMN d integer UNIQUE",),
)
ADD_COLUMN_CHECKED = Rule(
    "pg.add_column.checked",
    _ALTER_TABLE,
    (
        "ALTER TABLE t ADD COLUMN d integer CHECK (d > 0)",
        "ALTER TABLE t ADD COLUMN d integer REFERENCES r (id)",
    ),
)
DROP_COLUMN = Rule("pg.drop_column", _ALTER_TABLE, ("ALTER TABLE t DROP COLUMN c",))
ALTER_TYPE = Rule(
    "pg.alter_column_type",
    _ALTER_TABLE,
    ("ALTER TABLE t ALTER COLUMN c TYPE bigint",),
)
ALTER_TYPE_COERCIBLE = Rule(
    "pg.alter_column_type.binary_coercible",
    _ALTER_TABLE,
    ("ALTER TABLE t ALTER COLUMN name TYPE varchar(200)",),
)
SET_NOT_NULL = Rule(
    "pg.set_not_null",
    _ALTER_TABLE,
    ("ALTER TABLE t ALTER COLUMN c SET NOT NULL",),
)
COLUMN_CATALOG = Rule(
    "pg.alter_column.catalog",
    _ALTER_TABLE,
    (
        "ALTER TABLE t ALTER COLUMN c DROP NOT NULL",
        "ALTER TABLE t ALTER COLUMN c SET DEFAULT 0",
        "ALTER TABLE t ALTER COLUMN c DROP DEFAULT",
        "ALTER TABLE t ALTER COLUMN name SET STORAGE EXTERNAL",
    ),
)
SET_STATISTICS = Rule(
    "pg.set_statistics",
    _ALTER_TABLE,
    ("ALTER TABLE t ALTER COLUMN c SET STATISTICS 500",),
)
ADD_CHECK = Rule(
    "pg.add_check",
    _ALTER_TABLE,
    ("ALTER TABLE t ADD CONSTRAINT ck2 CHECK (c > 0)",),
)
ADD_CHECK_NOT_VALID = Rule(
    "pg.add_check.not_valid",
    _ALTER_TABLE,
    ("ALTER TABLE t ADD CONSTRAINT ck2 CHECK (c > 0) NOT VALID",),
)
ADD_FOREIGN_KEY = Rule(
    "pg.add_foreign_key",
    _ALTER_TABLE,
    ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (r_id) REFERENCES r (id)",),
)
ADD_FOREIGN_KEY_NOT_VALID = Rule(
    "pg.add_foreign_key.not_valid",
    _ALTER_TABLE,
    (
        "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (r_id) REFERENCES r (id) "
        "NOT VALID",
    ),
)
ADD_KEY = Rule(
    "pg.add_key",
    _ALTER_TABLE,
    (
        "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (c)",
        "ALTER TABLE p ADD PRIMARY KEY (id)",
    ),
)
ADD_KEY_USING_INDEX = Rule(
    "pg.add_key.using_index",
    _ALTER_TABLE,
    ("ALTER TABLE t ADD CONSTRAINT uq UNIQUE USING INDEX ix",),
)
ADD_EXCLUSION = Rule(
    "pg.add_exclusion",
    _ALTER_TABLE,
    ("ALTER TABLE t ADD CONSTRAINT ex EXCLUDE (c WITH =)",),
)
DROP_CONSTRAINT = Rule(
    "pg.drop_constraint", _ALTER_TABLE, ("ALTER TABLE t DROP CONSTRAINT ck",)
)
DROP_FOREIGN_KEY = Rule(
    "pg.drop_foreign_key",
    _ALTER_TABLE,
    (
        "DROP TABLE t",
        "DROP TABLE r CASCADE",
        "ALTER TABLE t DROP CONSTRAINT t_r_id_fkey",
        "ALTER TABLE r DROP CONSTRAINT r_pkey CASCADE",
        "ALTER TABLE t DROP COLUMN r_id",
        "ALTER TABLE t ALTER COLUMN r_id TYPE bigint",
        "ALTER TABLE r ALTER COLUMN id TYPE bigint",
    ),
)
VALIDATE = Rule(
    "pg.validate_constraint",
    _ALTER_TABLE,
    ("ALTER TABLE t VALIDATE CONSTRAINT ck",),
)
RENAME = Rule(
    "pg.rename",
    _ALTER_TABLE,
    (
        "ALTER TABLE t RENAME COLUMN c TO d",
        "ALTER TABLE t RENAME TO u",
        "ALTER TABLE t RENAME CONSTRAINT ck TO ck2",
    ),
)
ATTACH_PARTITION = Rule(
    "pg.attach_partition",
    _ALTER_TABLE,
    ("ALTER TABLE pt ATTACH PARTITION p FOR VALUES IN (2)",),
)
DETACH_PARTITION = Rule(
    "pg.detach_partition",
    _ALTER_TABLE,
    ("ALTER TABLE pt DETACH PARTITION pt1",),
)
DETACH_PARTITION_CONCURRENTLY = Rule(
    "pg.detach_partition.concurrently",
    _ALTER_TABLE,
    ("ALTER TABLE pt DETACH PARTITION pt1 CONCURRENTLY",),
    lambda version: version >= (14,),
)
TABLE_REWRITE = Rule(
    "pg.table_rewrite",
    _ALTER_TABLE,
    (
        "ALTER TABLE t SET TABLESPACE pg_default",
        "ALTER TABLE ul SET LOGGED",
        "ALTER TABLE t SET UNLOGGED",
    ),
)
TABLE_CATALOG = Rule(
    "pg.alter_table.catalog",
    _ALTER_TABLE,
    (
        "ALTER TABLE t SET SCHEMA s",
        "ALTER TABLE t OWNER TO CURRENT_USER",
        "ALTER TABLE t ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE t SET (autovacuum_enabled = false, user_catalog_table = true)",
    ),
)
TABLE_PARAMETERS = Rule(
    "pg.set_parameters",
    _ALTER_TABLE,
    ("ALTER TABLE t SET (fillfactor = 70)",),
)
TRIGGER_STATE = Rule(
    "pg.alter_trigger",
    _ALTER_TABLE,
    ("ALTER TABLE t DISABLE TRIGGER tr", "ALTER TABLE t ENABLE TRIGGER tr"),
)
CREATE_INDEX = Rule(
    "pg.create_index",
    DOCS + "sql-createindex.html",
    ("CREATE INDEX ix2 ON t (c)", "CREATE UNIQUE INDEX ix2 ON t (c)"),
)
CREATE_INDEX_CONCURRENTLY = Rule(
    "pg.create_index.concurrently",
    DOCS + "sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY",
    ("CREATE INDEX CONCURRENTLY ix2 ON t (c)",),
)
DROP_INDEX = Rule("pg.drop_index", DOCS + "sql-dropindex.html", ("DROP INDEX ix",))
DROP_INDEX_CONCURRENTLY = Rule(
    "pg.drop_index.concurrently",
    DOCS + "sql-dropindex.html",
    ("DROP INDEX CONCURRENTLY ix",),
)
CREATE_TABLE = Rule(
    "pg.create_table",
    DOCS + "sql-createtable.html",
    (
        "CREATE TABLE n (id integer, r_id integer REFERENCES r (id))",
        "CREATE TABLE pt2 PARTITION OF pt FOR VALUES IN (3)",
    ),
)
DROP_TABLE = Rule(
    "pg.drop_table",
    DOCS + "sql-droptable.html",
    ("DROP TABLE t", "TRUNCATE t", "TRUNCATE r CASCADE"),
)
WRITE_ROWS = Rule(
    "pg.write_rows",
    DOCS + "sql-update.html",
    ("UPDATE t SET c = 0 WHERE c IS NULL", "DELETE FROM t WHERE c < 0"),
)
INSERT_ROWS = Rule(
    "pg.insert", DOCS + "sql-insert.html", ("INSERT INTO t (id, c) VALUES (100, 100)",)
)
REINDEX = Rule("pg.reindex", DOCS + "sql-reindex.html", ("REINDEX TABLE t",))
REINDEX_CONCURRENTLY = Rule(
    "pg.reindex.concurrently",
    DOCS + "sql-reindex.html#SQL-REINDEX-CONCURRENTLY",
    ("REINDEX TABLE CONCURRENTLY t",),
)
VACUUM = Rule("pg.vacuum", DOCS + "sql-vacuum.html", ("VACUUM t", "ANALYZE t"))
VACUUM_FULL = Rule(
    "pg.vacuum_full",
    DOCS + "sql-vacuum.html",
    ("VACUUM FULL t", "CLUSTER t USING t_pkey"),
)
REFRESH = Rule(
    "pg.refresh_materialized_view",
    DOCS + "sql-refreshmaterializedview.html",
    ("REFRESH MATERIALIZED VIEW mv",),
)
REFRESH_CONCURRENTLY = Rule(
    "pg.refresh_materialized_view.concurrently",
    DOCS + "sql-refreshmaterializedview.html",
    ("REFRESH MATERIALIZED VIEW CONCURRENTLY mv",),
)
TRIGGER = Rule(
    "pg.trigger",
    DOCS + "sql-createtrigger.html",
    (
        "CREATE TRIGGER tr2 BEFORE UPDATE ON t FOR EACH ROW EXECUTE FUNCTION f()",
        "DROP TRIGGER tr ON t",
    ),
)
COMMENT = Rule(
    "pg.comment", DOCS + "sql-comment.html", ("COMMENT ON COLUMN t.c IS 'note'",)
)
DROP_VIEW = Rule("pg.drop_view", DOCS + "sql-dropview.html", ("DROP VIEW v",))
LOCK_TABLE = Rule(
    "pg.lock_table",
    DOCS + "sql-lock.html",
    ("LOCK TABLE t IN SHARE MODE", "LOCK TABLE t IN ACCESS EXCLUSIVE MODE NOWAIT"),
)
DROP_SCHEMA = Rule(
    "pg.drop_schema", DOCS + "sql-dropschema.html", ("DROP SCHEMA s CASCADE",)
)

# The objects the fixtures above name, with a few rows in each table.
# The ground-truth tests create them, then run each fixture alone inside
# a transaction that is rolled back. The views read `w`, so a fixture
# that drops `t` or changes a column of `r` does not fail on a view.
FIXTURE_SCHEMA = (
    "CREATE TABLE r (id integer PRIMARY KEY)",
    "INSERT INTO r VALUES (1), (2), (3)",
    "CREATE TABLE t (id integer PRIMARY KEY, c integer, name varchar(100), "
    "r_id integer REFERENCES r (id))",
    "INSERT INTO t SELECT g, g, 'n' || g, 1 FROM generate_series(1, 20) g",
    "CREATE UNIQUE INDEX ix ON t (c)",
    "ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0) NOT VALID",
    "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql "
    "AS 'BEGIN RETURN NEW; END'",
    "CREATE TRIGGER tr BEFORE UPDATE ON t FOR EACH ROW EXECUTE FUNCTION f()",
    "CREATE TABLE pt (id integer) PARTITION BY LIST (id)",
    "CREATE TABLE pt1 PARTITION OF pt FOR VALUES IN (1)",
    "INSERT INTO pt VALUES (1)",
    "CREATE TABLE p (id integer)",
    "INSERT INTO p VALUES (2)",
    "CREATE UNLOGGED TABLE ul (id integer)",
    "INSERT INTO ul VALUES (1)",
    "CREATE TABLE w (id integer)",
    "INSERT INTO w VALUES (1)",
    "CREATE MATERIALIZED VIEW mv AS SELECT id FROM w",
    "CREATE UNIQUE INDEX mv_id ON mv (id)",
    "CREATE VIEW v AS SELECT id FROM w",
    "CREATE SCHEMA s",
)


def all_rules() -> Tuple[Rule, ...]:
    """Every rule this module declares, in declaration order."""
    return tuple(value for value in globals().values() if isinstance(value, Rule))
