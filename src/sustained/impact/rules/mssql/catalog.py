"""
The SQL Server rules: each one's id, the documentation it relies on,
and its fixtures, with the objects the fixtures name.
"""

from __future__ import annotations

from sustained.impact.rules import Rule

DOCS = "https://learn.microsoft.com/en-us/sql/"
_STATEMENTS = DOCS + "t-sql/statements/"
_ALTER_TABLE = _STATEMENTS + "alter-table-transact-sql"
_LOCKING = (
    DOCS
    + "relational-databases/sql-server-transaction-locking-and-row-versioning-guide"
)
ONLINE_SOURCE = DOCS + "relational-databases/indexes/how-online-index-operations-work"
EDITIONS_SOURCE = DOCS + "sql-server/editions-and-components-of-sql-server-2022"

ADD_COLUMN = Rule(
    "mssql.add_column",
    _ALTER_TABLE + "#add",
    (
        "ALTER TABLE t ADD d int NULL",
        "ALTER TABLE t ADD d int NULL DEFAULT 0",
        "ALTER TABLE t ADD d AS (c * 2)",
    ),
)
ADD_COLUMN_DEFAULT = Rule(
    "mssql.add_column.default",
    _ALTER_TABLE + "#adding-not-null-columns-as-an-online-operation",
    (
        "ALTER TABLE t ADD d int NOT NULL DEFAULT 0",
        "ALTER TABLE t ADD d int NULL DEFAULT 0 WITH VALUES",
        "ALTER TABLE t ADD d datetime2 NOT NULL DEFAULT SYSDATETIME()",
    ),
)
ADD_COLUMN_REWRITE = Rule(
    "mssql.add_column.rewrite",
    _ALTER_TABLE + "#adding-not-null-columns-as-an-online-operation",
    (
        "ALTER TABLE t ADD d uniqueidentifier NOT NULL DEFAULT NEWID()",
        "ALTER TABLE t ADD d int IDENTITY",
        "ALTER TABLE t ADD d AS (c * 2) PERSISTED",
        "ALTER TABLE t ADD d nvarchar(max) NOT NULL DEFAULT N'x'",
        "ALTER TABLE t ADD d xml NOT NULL DEFAULT N'<a/>'",
        "ALTER TABLE t ADD d rowversion",
    ),
)
DROP_COLUMN = Rule(
    "mssql.drop_column",
    _ALTER_TABLE + "#drop-column-column_name",
    ("ALTER TABLE t DROP COLUMN v",),
)
ALTER_COLUMN = Rule(
    "mssql.alter_column",
    _ALTER_TABLE + "#alter-column",
    (
        "ALTER TABLE t ALTER COLUMN n bigint",
        "ALTER TABLE t ALTER COLUMN v varchar(5)",
        "ALTER TABLE t ALTER COLUMN v nvarchar(10)",
        "ALTER TABLE t ALTER COLUMN name nvarchar(max)",
    ),
)
ALTER_COLUMN_METADATA = Rule(
    "mssql.alter_column.metadata",
    _ALTER_TABLE + "#alter-column",
    (
        "ALTER TABLE t ALTER COLUMN name nvarchar(100)",
        "ALTER TABLE t ALTER COLUMN m int NULL",
    ),
)
SET_NOT_NULL = Rule(
    "mssql.set_not_null",
    _ALTER_TABLE + "#alter-column",
    (
        "ALTER TABLE t ALTER COLUMN n int NOT NULL",
        "ALTER TABLE t ALTER COLUMN v varchar(10) NOT NULL",
    ),
)
ALTER_COLUMN_ONLINE = Rule(
    "mssql.alter_column.online",
    _ALTER_TABLE + "#with--online--on--off-",
    ("ALTER TABLE t ALTER COLUMN n bigint WITH (ONLINE = ON)",),
    versions=lambda version: version >= (13,),
)
ADD_CHECK = Rule(
    "mssql.add_check",
    _ALTER_TABLE + "#with-check--with-nocheck",
    ("ALTER TABLE t ADD CONSTRAINT ck CHECK (c > 0)",),
)
ADD_CHECK_NOCHECK = Rule(
    "mssql.add_check.nocheck",
    _ALTER_TABLE + "#with-check--with-nocheck",
    ("ALTER TABLE t WITH NOCHECK ADD CONSTRAINT ck CHECK (c > 0)",),
)
ADD_FOREIGN_KEY = Rule(
    "mssql.add_foreign_key",
    _ALTER_TABLE + "#with-check--with-nocheck",
    ("ALTER TABLE t ADD CONSTRAINT fk2 FOREIGN KEY (r_id) REFERENCES r (id)",),
)
ADD_FOREIGN_KEY_NOCHECK = Rule(
    "mssql.add_foreign_key.nocheck",
    _ALTER_TABLE + "#with-check--with-nocheck",
    (
        "ALTER TABLE t WITH NOCHECK ADD CONSTRAINT fk2 FOREIGN KEY (r_id) "
        "REFERENCES r (id)",
    ),
)
CHECK_CONSTRAINT = Rule(
    "mssql.check_constraint",
    _ALTER_TABLE + "#with-check--with-nocheck",
    (
        "ALTER TABLE t WITH CHECK CHECK CONSTRAINT ck_t",
        "ALTER TABLE t WITH CHECK CHECK CONSTRAINT fk_t_r",
    ),
)
CONSTRAINT_STATE = Rule(
    "mssql.constraint_state",
    _ALTER_TABLE + "#check--nocheck-constraint",
    (
        "ALTER TABLE t NOCHECK CONSTRAINT ck_t",
        "ALTER TABLE t NOCHECK CONSTRAINT fk_t_r",
        "ALTER TABLE t CHECK CONSTRAINT ck_t",
    ),
)
ADD_KEY = Rule(
    "mssql.add_key",
    _ALTER_TABLE + "#primary-key",
    (
        "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name)",
        "ALTER TABLE h ADD CONSTRAINT pk_h PRIMARY KEY NONCLUSTERED (id)",
        "ALTER TABLE h ADD CONSTRAINT pk_h PRIMARY KEY (id)",
    ),
)
ADD_KEY_ONLINE = Rule(
    "mssql.add_key.online",
    _ALTER_TABLE + "#online--on--off-",
    ("ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name) WITH (ONLINE = ON)",),
)
DROP_CONSTRAINT = Rule(
    "mssql.drop_constraint",
    _ALTER_TABLE + "#drop-constraint",
    (
        "ALTER TABLE t DROP CONSTRAINT ck_t",
        "ALTER TABLE t DROP CONSTRAINT fk_t_r",
        "ALTER TABLE u DROP CONSTRAINT pk_u",
    ),
)
DEFAULT = Rule(
    "mssql.default",
    _ALTER_TABLE + "#default",
    (
        "ALTER TABLE t ADD DEFAULT 5 FOR n",
        "ALTER TABLE t ADD CONSTRAINT df_n DEFAULT 5 FOR n",
    ),
)
CREATE_INDEX = Rule(
    "mssql.create_index",
    _STATEMENTS + "create-index-transact-sql",
    (
        "CREATE INDEX ix2 ON t (name)",
        "CREATE UNIQUE INDEX ix2 ON t (name)",
        "CREATE INDEX ix2 ON t (name) INCLUDE (c) WHERE n > 0",
    ),
)
CREATE_INDEX_ONLINE = Rule(
    "mssql.create_index.online",
    ONLINE_SOURCE,
    ("CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON)",),
)
CREATE_CLUSTERED_INDEX = Rule(
    "mssql.create_index.clustered",
    _STATEMENTS + "create-index-transact-sql#clustered",
    (
        "CREATE CLUSTERED INDEX cx ON h (id)",
        "CREATE CLUSTERED INDEX cx ON h (id) WITH (ONLINE = ON)",
    ),
)
DROP_INDEX = Rule(
    "mssql.drop_index",
    _STATEMENTS + "drop-index-transact-sql",
    ("DROP INDEX ix ON t", "DROP INDEX t.ix"),
)
DROP_CLUSTERED_INDEX = Rule(
    "mssql.drop_index.clustered",
    _STATEMENTS + "drop-index-transact-sql#clustered-indexes",
    ("DROP INDEX cx_k ON k",),
)
REBUILD = Rule(
    "mssql.rebuild",
    _STATEMENTS
    + "alter-index-transact-sql#rebuild--with--rebuild_index_option------n---",
    (
        "ALTER INDEX ix ON t REBUILD",
        "ALTER INDEX ALL ON t REBUILD",
        "ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON)",
        "ALTER TABLE t REBUILD",
    ),
)
# REORGANIZE of a clustered index moves rows in proportion to its
# fragmentation, so what the server does depends on the index's state,
# and only the nonclustered form has a fixture.
REORGANIZE = Rule(
    "mssql.reorganize",
    _STATEMENTS + "alter-index-transact-sql#reorganize",
    ("ALTER INDEX ix ON t REORGANIZE",),
)
DISABLE_INDEX = Rule(
    "mssql.disable_index",
    _STATEMENTS + "alter-index-transact-sql#disable",
    ("ALTER INDEX ix ON t DISABLE",),
)
SWITCH = Rule(
    "mssql.switch",
    _ALTER_TABLE
    + "#switch--partition-source_partition_number_expression--to--schema_name--target_table--partition-target_partition_number_expression-",
    ("ALTER TABLE u SWITCH TO u2",),
)
RENAME = Rule(
    "mssql.rename",
    DOCS + "relational-databases/system-stored-procedures/sp-rename-transact-sql",
    (
        "EXEC sp_rename 't.name', 'label', 'COLUMN'",
        "EXEC sp_rename 't', 't9'",
        "EXEC sp_rename 't.ix', 'ix9', 'INDEX'",
    ),
)
TRUNCATE = Rule(
    "mssql.truncate",
    _STATEMENTS + "truncate-table-transact-sql",
    ("TRUNCATE TABLE t",),
)
DROP_TABLE = Rule(
    "mssql.drop_table",
    _STATEMENTS + "drop-table-transact-sql",
    ("DROP TABLE u2", "DROP TABLE t"),
)
TRIGGER = Rule(
    "mssql.trigger",
    _STATEMENTS + "create-trigger-transact-sql",
    (
        "CREATE TRIGGER tr2 ON p AFTER INSERT AS BEGIN SET NOCOUNT ON END",
        "ALTER TABLE p DISABLE TRIGGER tr",
        "ALTER TABLE p ENABLE TRIGGER ALL",
    ),
)
SCHEMA_CHANGE = Rule(
    "mssql.schema_change",
    _LOCKING,
    (
        "CREATE TABLE n (id int)",
        "CREATE TABLE n (id int, r_id int REFERENCES r (id))",
    ),
)
WRITE_ROWS = Rule(
    "mssql.write_rows",
    _LOCKING,
    (
        "UPDATE t SET name = N'x' WHERE id < 3",
        "DELETE FROM t WHERE id > 5990",
        "INSERT INTO t (id, c, r_id, m) VALUES (9000, 9000, 1, 0)",
        "UPDATE TOP (100) t SET name = N'x'",
    ),
)
LOCK_ESCALATION = Rule(
    "mssql.lock_escalation",
    _LOCKING + "#lock-escalation",
    ("UPDATE t SET name = N'x'", "DELETE FROM t", "INSERT INTO h SELECT id, c FROM t"),
)
RESUMABLE = Rule(
    "mssql.resumable",
    DOCS + "relational-databases/indexes/guidelines-for-online-index-operations",
    (
        "CREATE INDEX ix2 ON t (name) WITH (ONLINE = ON, RESUMABLE = ON)",
        "ALTER INDEX ix ON t REBUILD WITH (ONLINE = ON, RESUMABLE = ON)",
    ),
)
UPDATE_STATISTICS = Rule(
    "mssql.update_statistics",
    _STATEMENTS + "update-statistics-transact-sql",
    ("UPDATE STATISTICS t", "UPDATE STATISTICS t WITH FULLSCAN"),
)
ONLINE_EDITION = Rule("mssql.online.edition", EDITIONS_SOURCE)

RULES = (
    ADD_COLUMN,
    ADD_COLUMN_DEFAULT,
    ADD_COLUMN_REWRITE,
    DROP_COLUMN,
    ALTER_COLUMN,
    ALTER_COLUMN_METADATA,
    SET_NOT_NULL,
    ALTER_COLUMN_ONLINE,
    ADD_CHECK,
    ADD_CHECK_NOCHECK,
    ADD_FOREIGN_KEY,
    ADD_FOREIGN_KEY_NOCHECK,
    CHECK_CONSTRAINT,
    CONSTRAINT_STATE,
    ADD_KEY,
    ADD_KEY_ONLINE,
    DROP_CONSTRAINT,
    DEFAULT,
    CREATE_INDEX,
    CREATE_INDEX_ONLINE,
    CREATE_CLUSTERED_INDEX,
    DROP_INDEX,
    DROP_CLUSTERED_INDEX,
    REBUILD,
    REORGANIZE,
    DISABLE_INDEX,
    SWITCH,
    RENAME,
    TRUNCATE,
    DROP_TABLE,
    TRIGGER,
    SCHEMA_CHANGE,
    WRITE_ROWS,
    LOCK_ESCALATION,
    RESUMABLE,
    UPDATE_STATISTICS,
    ONLINE_EDITION,
)

# The objects the fixtures above name, with rows in each table. t has
# more rows than the 5,000 row locks that escalate a statement's locks
# to the table, and enough pages that a statement which writes every
# row writes far more log than one that changes the catalog. h and k
# are heaps with a NOT NULL id, for the fixtures that give a table its
# clustered index, and u and u2 match, for SWITCH.
FIXTURE_SCHEMA = (
    "CREATE TABLE r (id int PRIMARY KEY)",
    "INSERT INTO r VALUES (1), (2), (3)",
    "CREATE TABLE t (id int PRIMARY KEY, c int, name nvarchar(50), "
    "r_id int CONSTRAINT fk_t_r REFERENCES r (id), v varchar(10), n int NULL, "
    "m int NOT NULL, CONSTRAINT ck_t CHECK (c > 0))",
    "CREATE UNIQUE INDEX ix ON t (c)",
    "WITH n AS (SELECT TOP 6000 ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS i "
    "FROM sys.all_objects a CROSS JOIN sys.all_objects b) "
    "INSERT INTO t (id, c, name, r_id, v, n, m) "
    "SELECT i, i, CONCAT(N'name ', i), 1, 'x', i, i FROM n",
    "CREATE TABLE h (id int NOT NULL, c int)",
    "INSERT INTO h SELECT id, c FROM t",
    "CREATE TABLE k (id int NOT NULL, c int)",
    "INSERT INTO k SELECT id, c FROM t",
    "CREATE CLUSTERED INDEX cx_k ON k (id)",
    "CREATE TABLE u (id int CONSTRAINT pk_u PRIMARY KEY, c int)",
    "INSERT INTO u SELECT id, c FROM t",
    "CREATE TABLE u2 (id int CONSTRAINT pk_u2 PRIMARY KEY, c int)",
    "CREATE TABLE p (id int PRIMARY KEY, v int)",
    "INSERT INTO p VALUES (1, 1), (2, 2)",
    "CREATE VIEW vw AS SELECT id FROM p",
    "CREATE TRIGGER tr ON p AFTER UPDATE AS BEGIN SET NOCOUNT ON END",
)
