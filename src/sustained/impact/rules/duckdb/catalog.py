"""
The DuckDB rules: each one's id, the documentation it relies on, and
its fixtures, with the objects the fixtures name.
"""

from __future__ import annotations

from sustained.impact.rules import Rule

DOCS = "https://duckdb.org/docs/current/"
_ALTER_TABLE = DOCS + "sql/statements/alter_table.html"
CONCURRENCY = DOCS + "connect/concurrency.html#optimistic-concurrency-control"
_CREATE_INDEX = DOCS + "sql/statements/create_index.html"

ADD_COLUMN = Rule(
    "duckdb.add_column",
    _ALTER_TABLE + "#add-column",
    (
        "ALTER TABLE t ADD COLUMN d integer",
        "ALTER TABLE t ADD COLUMN d integer DEFAULT 5",
        "ALTER TABLE t ADD COLUMN d timestamp DEFAULT now()",
    ),
)
ADD_COLUMN_VOLATILE = Rule(
    "duckdb.add_column.volatile",
    _ALTER_TABLE + "#add-column",
    (
        "ALTER TABLE t ADD COLUMN d double DEFAULT random()",
        "ALTER TABLE t ADD COLUMN d uuid DEFAULT gen_random_uuid()",
    ),
)
DROP_COLUMN = Rule(
    "duckdb.drop_column",
    _ALTER_TABLE + "#drop-column",
    ("ALTER TABLE t DROP COLUMN name",),
)
ALTER_COLUMN_TYPE = Rule(
    "duckdb.alter_column_type",
    _ALTER_TABLE + "#set-data-type",
    (
        "ALTER TABLE t ALTER COLUMN c TYPE bigint",
        "ALTER TABLE t ALTER COLUMN c SET DATA TYPE integer USING coalesce(c, 0)",
    ),
)
SET_NOT_NULL = Rule(
    "duckdb.set_not_null",
    _ALTER_TABLE,
    ("ALTER TABLE t ALTER COLUMN name SET NOT NULL",),
)
ALTER_COLUMN = Rule(
    "duckdb.alter_column",
    _ALTER_TABLE + "#set--drop-default",
    (
        "ALTER TABLE t ALTER COLUMN name DROP NOT NULL",
        "ALTER TABLE t ALTER COLUMN name SET DEFAULT 'x'",
        "ALTER TABLE t ALTER COLUMN name DROP DEFAULT",
    ),
)
RENAME = Rule(
    "duckdb.rename",
    _ALTER_TABLE + "#rename-column",
    ("ALTER TABLE t RENAME COLUMN name TO label", "ALTER TABLE t RENAME TO u"),
)
CREATE_INDEX = Rule(
    "duckdb.create_index",
    _CREATE_INDEX,
    ("CREATE INDEX ix2 ON t (name)", "CREATE UNIQUE INDEX ix2 ON t (c)"),
)
DROP_INDEX = Rule(
    "duckdb.drop_index", _CREATE_INDEX + "#drop-index", ("DROP INDEX ix",)
)
COMMENT = Rule(
    "duckdb.comment",
    DOCS + "sql/statements/comment_on.html",
    ("COMMENT ON COLUMN t.name IS 'x'", "COMMENT ON TABLE t IS 'x'"),
)
CREATE_TABLE = Rule(
    "duckdb.create_table",
    DOCS + "sql/statements/create_table.html",
    (
        "CREATE TABLE n (id integer)",
        "CREATE TABLE n (id integer, r_id integer REFERENCES r (id))",
    ),
)
SCHEMA_CHANGE = Rule(
    "duckdb.schema_change",
    CONCURRENCY,
    (
        "CREATE VIEW v2 AS SELECT id FROM t",
        "DROP VIEW v",
        "CREATE TYPE mood2 AS ENUM ('a')",
        "DROP TYPE mood",
        "CREATE SEQUENCE s",
    ),
)
DROP_TABLE = Rule(
    "duckdb.drop_table", DOCS + "sql/statements/drop.html", ("DROP TABLE p",)
)
WRITE_ROWS = Rule(
    "duckdb.write_rows",
    CONCURRENCY,
    (
        "UPDATE t SET name = 'x' WHERE id < 3",
        "DELETE FROM t WHERE id < 3",
        "TRUNCATE p",
        "INSERT INTO t (id, c) VALUES (50000, 50000)",
    ),
)
ANALYZE = Rule(
    "duckdb.analyze", DOCS + "sql/statements/analyze.html", ("ANALYZE t", "ANALYZE")
)

RULES = (
    ADD_COLUMN,
    ADD_COLUMN_VOLATILE,
    DROP_COLUMN,
    ALTER_COLUMN_TYPE,
    SET_NOT_NULL,
    ALTER_COLUMN,
    RENAME,
    CREATE_INDEX,
    DROP_INDEX,
    COMMENT,
    CREATE_TABLE,
    SCHEMA_CHANGE,
    DROP_TABLE,
    WRITE_ROWS,
    ANALYZE,
)

# The objects the fixtures above name, with rows in each table. t has no
# index, since DuckDB refuses to alter a table an index depends on, and
# enough rows to fill several column segments, so the ground-truth tests
# can tell a statement that writes a column again from one that changes
# only the catalog. Every value of c is distinct.
FIXTURE_SCHEMA = (
    "CREATE TABLE r (id integer PRIMARY KEY)",
    "INSERT INTO r VALUES (1), (2), (3)",
    "CREATE TABLE t (id integer, c integer, name varchar, r_id integer)",
    "INSERT INTO t SELECT i, (i * 7919) % 100003, 'name ' || i, 1 "
    "FROM range(10000) n (i)",
    "CREATE TABLE p (id integer, v integer)",
    "INSERT INTO p VALUES (1, 1), (2, 2)",
    "CREATE INDEX ix ON p (v)",
    "CREATE VIEW v AS SELECT id FROM p",
    "CREATE TYPE mood AS ENUM ('a', 'b')",
)
