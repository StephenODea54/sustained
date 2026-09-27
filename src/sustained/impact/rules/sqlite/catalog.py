"""
The SQLite rules: each one's id, the documentation it relies on, and
its fixtures, with the objects the fixtures name.
"""

from __future__ import annotations

from sustained.impact.rules import Rule

DOCS = "https://www.sqlite.org/"
_ALTER_TABLE = DOCS + "lang_altertable.html"
_LOCKING = DOCS + "lockingv3.html"

ADD_COLUMN = Rule(
    "sqlite.add_column",
    _ALTER_TABLE + "#alter_table_add_column",
    (
        "ALTER TABLE t ADD COLUMN d integer",
        "ALTER TABLE t ADD COLUMN d integer NOT NULL DEFAULT 0",
        "ALTER TABLE t ADD COLUMN d integer REFERENCES r (id)",
    ),
)
ADD_COLUMN_CHECKED = Rule(
    "sqlite.add_column.checked",
    _ALTER_TABLE + "#alter_table_add_column",
    (
        "ALTER TABLE t ADD COLUMN d integer CHECK (d IS NULL OR d > 0)",
        "ALTER TABLE t ADD COLUMN d integer GENERATED ALWAYS AS (id * 2) "
        "VIRTUAL NOT NULL",
    ),
)
DROP_COLUMN = Rule(
    "sqlite.drop_column",
    _ALTER_TABLE + "#alter_table_drop_column",
    ("ALTER TABLE t DROP COLUMN name",),
)
RENAME = Rule(
    "sqlite.rename",
    _ALTER_TABLE + "#alter_table_rename",
    ("ALTER TABLE t RENAME COLUMN name TO label", "ALTER TABLE t RENAME TO u"),
)
REBUILD = Rule("sqlite.rebuild", _ALTER_TABLE + "#otheralter")
COPY = Rule(
    "sqlite.copy",
    DOCS + "lang_createtable.html#create_table_as_select_statements",
    ("CREATE TABLE n AS SELECT * FROM t",),
)
CREATE_INDEX = Rule(
    "sqlite.create_index",
    DOCS + "lang_createindex.html",
    ("CREATE INDEX ix2 ON t (name)", "CREATE UNIQUE INDEX ix2 ON t (name)"),
)
DROP_INDEX = Rule("sqlite.drop_index", DOCS + "lang_dropindex.html", ("DROP INDEX ix",))
REINDEX = Rule(
    "sqlite.reindex", DOCS + "lang_reindex.html", ("REINDEX t", "REINDEX ix")
)
SCHEMA_CHANGE = Rule(
    "sqlite.schema_change",
    _LOCKING,
    (
        "CREATE TABLE n (id integer)",
        "CREATE VIEW v2 AS SELECT id FROM t",
        "DROP VIEW v",
        "CREATE TRIGGER tr2 AFTER INSERT ON t BEGIN SELECT 1; END",
        "DROP TRIGGER tr",
    ),
)
DROP_TABLE = Rule("sqlite.drop_table", DOCS + "lang_droptable.html", ("DROP TABLE p",))
WRITE_ROWS = Rule(
    "sqlite.write_rows",
    _LOCKING,
    (
        "UPDATE t SET name = 'x' WHERE id < 3",
        "DELETE FROM t WHERE id > 18",
        "INSERT INTO t (id, c) VALUES (5000, 5000)",
    ),
)
ANALYZE = Rule("sqlite.analyze", DOCS + "lang_analyze.html", ("ANALYZE t",))
VACUUM = Rule("sqlite.vacuum", DOCS + "lang_vacuum.html", ("VACUUM",))

RULES = (
    ADD_COLUMN,
    ADD_COLUMN_CHECKED,
    DROP_COLUMN,
    RENAME,
    REBUILD,
    COPY,
    CREATE_INDEX,
    DROP_INDEX,
    REINDEX,
    SCHEMA_CHANGE,
    DROP_TABLE,
    WRITE_ROWS,
    ANALYZE,
    VACUUM,
)

# The objects the fixtures above name, with rows in each table. t holds
# enough rows to fill dozens of pages, so the ground-truth tests can
# tell a statement that copies it from one that changes a page or two.
FIXTURE_SCHEMA = (
    "CREATE TABLE r (id integer PRIMARY KEY)",
    "INSERT INTO r VALUES (1), (2), (3)",
    "CREATE TABLE t (id integer PRIMARY KEY, c integer, name text, r_id integer "
    "REFERENCES r (id))",
    "CREATE UNIQUE INDEX ix ON t (c)",
    "WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
    "WHERE i < 2000) INSERT INTO t (id, c, name, r_id) "
    "SELECT i, i, 'name ' || i, 1 FROM n",
    "CREATE TABLE p (id integer, v integer)",
    "INSERT INTO p VALUES (1, 1), (2, 2)",
    "CREATE VIEW v AS SELECT id FROM p",
    "CREATE TRIGGER tr AFTER UPDATE ON p BEGIN SELECT 1; END",
)
