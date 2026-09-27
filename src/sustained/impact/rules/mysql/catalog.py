"""
The MySQL and MariaDB rules, as the `_Spec`s both profiles state, and
the objects the fixtures name.
"""

from __future__ import annotations

from typing import (
    Dict,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
)

from sustained.impact.rules import Rule

MYSQL_DOCS = "https://dev.mysql.com/doc/refman/8.0/en/"
_ONLINE = MYSQL_DOCS + "innodb-online-ddl-operations.html"
MARIADB_DOCS = "https://mariadb.com/kb/en/"
_MARIADB_ONLINE = MARIADB_DOCS + "innodb-online-ddl-overview/"


class _Spec(NamedTuple):
    """One rule as both profiles state it."""

    name: str
    mysql_source: str
    mariadb_source: str
    fixtures: Tuple[str, ...]
    mariadb_fixtures: Optional[Tuple[str, ...]] = None
    engines: Tuple[str, ...] = ("mysql", "mariadb")


_COLUMN_OPS = _ONLINE + "#online-ddl-column-operations"
_INDEX_OPS = _ONLINE + "#online-ddl-index-operations"
_KEY_OPS = _ONLINE + "#online-ddl-primary-key-operations"
_FK_OPS = _ONLINE + "#online-ddl-foreign-key-operations"
_TABLE_OPS = _ONLINE + "#online-ddl-table-operations"
_GENERATED_OPS = _ONLINE + "#online-ddl-generated-column-operations"
_MDL = MYSQL_DOCS + "metadata-locking.html"
_MARIADB_INSTANT = MARIADB_DOCS + (
    "innodb-online-ddl-operations-with-the-instant-alter-algorithm/"
)
_MARIADB_NOCOPY = MARIADB_DOCS + (
    "innodb-online-ddl-operations-with-the-nocopy-alter-algorithm/"
)
_MARIADB_INPLACE = MARIADB_DOCS + (
    "innodb-online-ddl-operations-with-the-inplace-alter-algorithm/"
)
_MARIADB_ALTER = MARIADB_DOCS + "alter-table/"
_MARIADB_MDL = MARIADB_DOCS + "metadata-locking/"

_SPECS = (
    _Spec(
        "add_column.instant",
        _COLUMN_OPS,
        _MARIADB_INSTANT,
        (
            "ALTER TABLE t ADD COLUMN d int",
            "ALTER TABLE t ADD COLUMN d int NOT NULL DEFAULT 0",
            "ALTER TABLE t ADD COLUMN d int AFTER c",
            "ALTER TABLE t ADD COLUMN d datetime DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE t ADD COLUMN d int GENERATED ALWAYS AS (c * 2) VIRTUAL",
        ),
        (
            "ALTER TABLE t ADD COLUMN d int",
            "ALTER TABLE t ADD COLUMN d int NOT NULL DEFAULT 0",
            "ALTER TABLE t ADD COLUMN d int AFTER c",
            "ALTER TABLE t ADD COLUMN d datetime DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE t ADD COLUMN d int DEFAULT (1 + 1)",
            "ALTER TABLE t ADD COLUMN d int GENERATED ALWAYS AS (c * 2) VIRTUAL",
            "ALTER TABLE t ADD COLUMN d int, ALGORITHM=INPLACE",
            "ALTER ONLINE TABLE t ADD COLUMN d int",
            "ALTER TABLE t WAIT 5 ADD COLUMN d int",
            "ALTER IGNORE TABLE t NOWAIT ADD COLUMN d int",
        ),
    ),
    _Spec(
        "add_column.rebuild",
        _COLUMN_OPS,
        _MARIADB_INPLACE,
        (
            "ALTER TABLE t ADD COLUMN d int UNIQUE",
            "ALTER TABLE p ADD COLUMN d int AUTO_INCREMENT PRIMARY KEY",
            "ALTER TABLE cz ADD COLUMN d int",
            "ALTER TABLE t ADD COLUMN d int, ADD INDEX ix2 (name)",
            "ALTER TABLE t ADD COLUMN d int, MODIFY COLUMN name varchar(200)",
            "ALTER TABLE t ADD COLUMN d int, ALGORITHM=INPLACE",
        ),
        (
            "ALTER TABLE t ADD COLUMN d int UNIQUE",
            "ALTER TABLE p ADD COLUMN d int AUTO_INCREMENT PRIMARY KEY",
            "ALTER TABLE cz ADD COLUMN d int",
            "ALTER TABLE ft ADD COLUMN d int",
            "ALTER TABLE t ADD COLUMN d int, ADD INDEX ix2 (name)",
        ),
    ),
    _Spec(
        "add_column.copy",
        _COLUMN_OPS,
        _MARIADB_ALTER,
        (
            "ALTER TABLE t ADD COLUMN d int DEFAULT (1 + 1)",
            "ALTER TABLE t ADD COLUMN d int GENERATED ALWAYS AS (c * 2) STORED",
            "ALTER TABLE ft ADD COLUMN d int",
            "ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY",
        ),
        (
            "ALTER TABLE t ADD COLUMN d varchar(36) DEFAULT (uuid())",
            "ALTER TABLE t ADD COLUMN d int GENERATED ALWAYS AS (c * 2) STORED",
            "ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY",
        ),
    ),
    _Spec(
        "drop_column.instant",
        _COLUMN_OPS,
        _MARIADB_INSTANT,
        ("ALTER TABLE t DROP COLUMN name",),
    ),
    _Spec(
        "drop_column.rebuild",
        _COLUMN_OPS,
        _MARIADB_NOCOPY,
        (
            "ALTER TABLE t DROP COLUMN c",
            "ALTER TABLE ft DROP COLUMN x",
            "ALTER TABLE t DROP COLUMN name, ADD INDEX ix2 (small)",
        ),
    ),
    _Spec(
        "modify_column.instant",
        _COLUMN_OPS,
        _MARIADB_INSTANT,
        (
            "ALTER TABLE t MODIFY COLUMN c int",
            "ALTER TABLE t MODIFY COLUMN c int COMMENT 'note'",
            "ALTER TABLE t MODIFY COLUMN nn int NOT NULL DEFAULT 5",
            "ALTER TABLE t MODIFY COLUMN e enum('a','b','c')",
        ),
        (
            "ALTER TABLE t MODIFY COLUMN c int",
            "ALTER TABLE t MODIFY COLUMN c int COMMENT 'note'",
            "ALTER TABLE t MODIFY COLUMN nn int NOT NULL DEFAULT 5",
            "ALTER TABLE t MODIFY COLUMN e enum('a','b','c')",
            "ALTER TABLE t MODIFY COLUMN name varchar(200)",
            "ALTER TABLE t MODIFY COLUMN small varchar(64)",
            "ALTER TABLE t MODIFY COLUMN c int FIRST",
            "ALTER TABLE ci MODIFY COLUMN name varchar(100) COLLATE utf8mb4_bin",
            "ALTER TABLE ci MODIFY COLUMN name varchar(200) COLLATE utf8mb4_bin",
            "ALTER TABLE ci MODIFY COLUMN s3 varchar(30) CHARACTER SET utf8mb4",
            "ALTER TABLE ci MODIFY COLUMN l varchar(100) CHARACTER SET latin1",
            "ALTER TABLE ci MODIFY COLUMN mid varchar(40)",
        ),
    ),
    _Spec(
        "modify_column.inplace",
        _COLUMN_OPS,
        _MARIADB_INPLACE,
        (
            "ALTER TABLE t MODIFY COLUMN name varchar(200)",
            "ALTER TABLE t MODIFY COLUMN small varchar(63)",
            "ALTER TABLE ci MODIFY COLUMN name varchar(100) COLLATE utf8mb4_bin",
            "ALTER TABLE ci MODIFY COLUMN name varchar(200) COLLATE utf8mb4_bin",
            "ALTER TABLE ci MODIFY COLUMN s3 varchar(30) CHARACTER SET utf8mb4",
        ),
        engines=("mysql",),
    ),
    _Spec(
        "modify_column.rebuild",
        _COLUMN_OPS,
        _MARIADB_INPLACE,
        (
            "ALTER TABLE t MODIFY COLUMN c int NOT NULL",
            "ALTER TABLE t MODIFY COLUMN nn int NULL DEFAULT 0",
            "ALTER TABLE t MODIFY COLUMN c int FIRST",
        ),
        (
            "ALTER TABLE t MODIFY COLUMN c int NOT NULL",
            "ALTER TABLE t MODIFY COLUMN nn int NULL DEFAULT 0",
            "ALTER TABLE t MODIFY COLUMN c int FIRST, ADD INDEX ix2 (name)",
            "ALTER TABLE ci MODIFY COLUMN code varchar(50) COLLATE utf8mb4_bin",
        ),
    ),
    _Spec(
        "modify_column.copy",
        _COLUMN_OPS,
        _MARIADB_ALTER,
        (
            "ALTER TABLE t MODIFY COLUMN c bigint",
            "ALTER TABLE t MODIFY COLUMN small varchar(64)",
            "ALTER TABLE t MODIFY COLUMN name varchar(50)",
            "ALTER TABLE t MODIFY COLUMN e enum('b','a')",
            "ALTER TABLE t CHANGE COLUMN name label text",
            "ALTER TABLE ci MODIFY COLUMN name varchar(100) CHARACTER SET latin1",
            "ALTER TABLE ci MODIFY COLUMN code varchar(50) COLLATE utf8mb4_bin",
            "ALTER TABLE ci MODIFY COLUMN l varchar(100)",
            "ALTER TABLE ci MODIFY COLUMN mid varchar(64)",
            "ALTER TABLE ci MODIFY COLUMN m3 varchar(80) CHARACTER SET utf8mb4",
        ),
        (
            "ALTER TABLE t MODIFY COLUMN c bigint",
            "ALTER TABLE t MODIFY COLUMN name varchar(50)",
            "ALTER TABLE t MODIFY COLUMN e enum('b','a')",
            "ALTER TABLE t CHANGE COLUMN name label text",
            "ALTER TABLE ci MODIFY COLUMN name varchar(100) CHARACTER SET latin1",
            "ALTER TABLE ci MODIFY COLUMN l varchar(100)",
            "ALTER TABLE ci MODIFY COLUMN mid varchar(64)",
            "ALTER TABLE ci MODIFY COLUMN m3 varchar(80) CHARACTER SET utf8mb4",
        ),
    ),
    _Spec(
        "column_default",
        _COLUMN_OPS,
        _MARIADB_INSTANT,
        (
            "ALTER TABLE t ALTER COLUMN c SET DEFAULT 5",
            "ALTER TABLE t ALTER COLUMN nn DROP DEFAULT",
            "ALTER TABLE t ALTER COLUMN name SET INVISIBLE",
        ),
        (
            "ALTER TABLE t ALTER COLUMN c SET DEFAULT 5",
            "ALTER TABLE t ALTER COLUMN nn DROP DEFAULT",
        ),
    ),
    _Spec(
        "rename",
        _COLUMN_OPS,
        _MARIADB_INSTANT,
        (
            "ALTER TABLE t RENAME COLUMN name TO label",
            "ALTER TABLE t CHANGE COLUMN name label varchar(100)",
            "ALTER TABLE t RENAME TO u",
            "RENAME TABLE t TO u",
        ),
    ),
    _Spec(
        "rename_index",
        _INDEX_OPS,
        _MARIADB_INSTANT,
        ("ALTER TABLE t RENAME INDEX ix TO ix2",),
    ),
    _Spec(
        "add_index",
        _INDEX_OPS,
        _MARIADB_NOCOPY,
        (
            "CREATE INDEX ix2 ON t (name)",
            "CREATE UNIQUE INDEX ix2 ON t (name)",
            "ALTER TABLE t ADD INDEX ix2 (name)",
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name)",
            "CREATE INDEX ix2 USING BTREE ON t (name)",
            "CREATE INDEX ix2 ON t (name) USING BTREE",
            "ALTER TABLE t ADD INDEX ix2 (name) USING BTREE",
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name) USING HASH",
            "CREATE UNIQUE INDEX uq ON t (name) USING HASH",
        ),
        (
            "CREATE INDEX ix2 ON t (name)",
            "CREATE UNIQUE INDEX ix2 ON t (name)",
            "ALTER TABLE t ADD INDEX ix2 (name)",
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name)",
            "CREATE INDEX ix2 USING BTREE ON t (name)",
            "CREATE INDEX ix2 ON t (name) USING BTREE",
            "ALTER TABLE t ADD INDEX ix2 (name) USING BTREE",
            "ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name) USING HASH",
            "CREATE UNIQUE INDEX uq ON t (name) USING HASH",
            "ALTER TABLE t ADD INDEX ix2 (name) USING HASH",
            "CREATE INDEX ix2 ON t (name) WAIT 3",
            "ALTER ONLINE TABLE t ADD INDEX ix2 (name)",
            "ALTER IGNORE TABLE t ADD CONSTRAINT uq UNIQUE (name)",
        ),
    ),
    _Spec(
        "add_spatial",
        _INDEX_OPS,
        _MARIADB_NOCOPY,
        ("ALTER TABLE g ADD SPATIAL INDEX sp (p)", "CREATE SPATIAL INDEX sp ON g (p)"),
    ),
    _Spec(
        "add_fulltext",
        _INDEX_OPS,
        _MARIADB_INPLACE,
        (
            "CREATE FULLTEXT INDEX ft2 ON t (name)",
            "ALTER TABLE ft ADD FULLTEXT INDEX ft2 (body)",
        ),
    ),
    _Spec(
        "drop_index",
        _INDEX_OPS,
        _MARIADB_NOCOPY,
        ("DROP INDEX ix ON t", "ALTER TABLE t DROP INDEX ix"),
        (
            "DROP INDEX ix ON t",
            "ALTER TABLE t DROP INDEX ix",
            "DROP INDEX ix ON t NOWAIT",
        ),
    ),
    _Spec(
        "index_visibility",
        MYSQL_DOCS + "invisible-indexes.html",
        MARIADB_DOCS + "ignored-indexes/",
        ("ALTER TABLE ci ALTER INDEX code_ix INVISIBLE",),
        ("ALTER TABLE ci ALTER INDEX code_ix IGNORED",),
    ),
    _Spec(
        "add_primary_key",
        _KEY_OPS,
        _MARIADB_INPLACE,
        (
            "ALTER TABLE p ADD PRIMARY KEY (id)",
            "ALTER TABLE t DROP PRIMARY KEY, ADD PRIMARY KEY (id, nn)",
        ),
    ),
    _Spec(
        "drop_primary_key",
        _KEY_OPS,
        _MARIADB_ALTER,
        ("ALTER TABLE t DROP PRIMARY KEY",),
    ),
    _Spec(
        "add_foreign_key",
        _FK_OPS,
        _MARIADB_ALTER,
        ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (r_id) REFERENCES r (id)",),
    ),
    _Spec(
        "add_foreign_key.unchecked",
        _FK_OPS,
        _MARIADB_INSTANT,
        (),
    ),
    _Spec(
        "drop_foreign_key",
        _FK_OPS,
        _MARIADB_INSTANT,
        ("ALTER TABLE t DROP FOREIGN KEY t_r_fk",),
    ),
    _Spec(
        "foreign_key_parent",
        _MDL,
        _MARIADB_MDL,
        (
            "CREATE TABLE n (id int, r_id int, FOREIGN KEY (r_id) REFERENCES r (id))",
            "DROP TABLE t",
        ),
        engines=("mysql",),
    ),
    _Spec(
        "add_check",
        MYSQL_DOCS + "create-table-check-constraints.html",
        _MARIADB_ALTER,
        ("ALTER TABLE t ADD CONSTRAINT ck2 CHECK (c > -1)",),
    ),
    _Spec(
        "drop_check",
        MYSQL_DOCS + "alter-table.html",
        _MARIADB_ALTER,
        ("ALTER TABLE t DROP CHECK ck",),
        ("ALTER TABLE t DROP CONSTRAINT ck",),
    ),
    _Spec(
        "table_copy",
        _TABLE_OPS,
        _MARIADB_ALTER,
        ("ALTER TABLE t CONVERT TO CHARACTER SET latin1",),
    ),
    _Spec(
        "table_rebuild",
        _TABLE_OPS,
        _MARIADB_INPLACE,
        (
            "ALTER TABLE t ENGINE=InnoDB",
            "ALTER TABLE t FORCE",
            "ALTER TABLE t ROW_FORMAT=COMPACT",
            "OPTIMIZE TABLE t",
        ),
    ),
    _Spec(
        "table_option",
        _TABLE_OPS,
        _MARIADB_INSTANT,
        ("ALTER TABLE t COMMENT = 'note'", "ALTER TABLE t AUTO_INCREMENT = 100"),
    ),
    _Spec(
        "drop_table",
        _MDL,
        _MARIADB_MDL,
        ("DROP TABLE p", "TRUNCATE TABLE p"),
    ),
    _Spec(
        "trigger",
        _MDL,
        _MARIADB_MDL,
        ("CREATE TRIGGER tr BEFORE UPDATE ON t FOR EACH ROW SET NEW.c = NEW.c",),
    ),
    _Spec(
        "write_rows",
        MYSQL_DOCS + "innodb-locks-set.html",
        MARIADB_DOCS + "innodb-lock-modes/",
        ("UPDATE t SET name = 'x' WHERE id < 3", "DELETE FROM t WHERE id > 18"),
    ),
    _Spec(
        "insert",
        MYSQL_DOCS + "innodb-locks-set.html",
        MARIADB_DOCS + "innodb-lock-modes/",
        ("INSERT INTO t (id, c, nn) VALUES (100, 100, 0)",),
    ),
    _Spec(
        "drop_view",
        _MDL,
        _MARIADB_MDL,
        ("DROP VIEW v",),
    ),
    _Spec(
        "refused",
        _ONLINE,
        _MARIADB_ONLINE,
        (
            "ALTER TABLE t MODIFY COLUMN c bigint, ALGORITHM=INPLACE",
            "ALTER TABLE t ADD COLUMN d int, ALGORITHM=COPY, LOCK=NONE",
        ),
        ("ALTER TABLE t MODIFY COLUMN c bigint, ALGORITHM=INPLACE",),
    ),
)


class RuleSet:
    """One profile's rules, found by the name the specs give them."""

    def __init__(self, prefix: str) -> None:
        mariadb = prefix == "mariadb"
        self.prefix = prefix
        self.by_name: Dict[str, Rule] = {}
        for spec in _SPECS:
            if prefix not in spec.engines:
                continue
            fixtures = spec.fixtures
            if mariadb and spec.mariadb_fixtures is not None:
                fixtures = spec.mariadb_fixtures
            source = spec.mariadb_source if mariadb else spec.mysql_source
            self.by_name[spec.name] = Rule(f"{prefix}.{spec.name}", source, fixtures)

    def __getitem__(self, name: str) -> Rule:
        return self.by_name[name]

    def all(self) -> Tuple[Rule, ...]:
        return tuple(self.by_name.values())


RULE_SETS: Mapping[str, RuleSet] = {
    "mysql": RuleSet("mysql"),
    "mariadb": RuleSet("mariadb"),
}

# The objects the fixtures above name, with a few rows in each table.
# The ground-truth tests create them, then probe each fixture on an
# empty copy of its table.
FIXTURE_SCHEMA = (
    "CREATE TABLE r (id int PRIMARY KEY)",
    "INSERT INTO r VALUES (1), (2), (3)",
    "CREATE TABLE t (id int NOT NULL PRIMARY KEY, c int, name varchar(100), "
    "small varchar(20), e enum('a','b'), r_id int, nn int NOT NULL DEFAULT 0, "
    "UNIQUE KEY ix (c), KEY r_ix (r_id), "
    "CONSTRAINT t_r_fk FOREIGN KEY (r_id) REFERENCES r (id), "
    "CONSTRAINT ck CHECK (c > 0)) "
    "DEFAULT CHARSET=utf8mb4",
    "INSERT INTO t (id, c, name, small, e, r_id) VALUES "
    + ", ".join(f"({i}, {i}, 'n{i}', 's{i}', 'a', 1)" for i in range(1, 21)),
    "CREATE TABLE p (id int NOT NULL, v int)",
    "INSERT INTO p VALUES (1, 1), (2, 2)",
    "CREATE TABLE ft (id int PRIMARY KEY, body text, x int, "
    "FULLTEXT KEY ft_body (body))",
    "INSERT INTO ft VALUES (1, 'a b c', 1)",
    "CREATE TABLE ci (id int PRIMARY KEY, name varchar(100), code varchar(50), "
    "mid varchar(40), l varchar(100) CHARACTER SET latin1, "
    "s3 varchar(30) CHARACTER SET utf8mb3, m3 varchar(80) CHARACTER SET utf8mb3, "
    "KEY code_ix (code)) DEFAULT CHARSET=utf8mb4",
    "INSERT INTO ci VALUES (1, 'a', 'b', 'c', 'd', 'e', 'f'), "
    "(2, 'g', 'h', 'i', 'j', 'k', 'l')",
    "CREATE TABLE g (id int PRIMARY KEY, p point NOT NULL)",
    "INSERT INTO g VALUES (1, POINT(1, 1))",
    "CREATE TABLE cz (id int PRIMARY KEY, v int) ROW_FORMAT=COMPRESSED",
    "INSERT INTO cz VALUES (1, 1)",
    "CREATE VIEW v AS SELECT id FROM p",
)
