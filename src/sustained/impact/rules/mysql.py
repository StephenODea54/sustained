"""
The MySQL and MariaDB rules, for InnoDB tables on MySQL 8.0.19 and
later and MariaDB 10.6 and later.

Both servers run an ALTER TABLE with one of the algorithms InnoDB
offers, and let other sessions read and write the table as far as the
LOCK level allows:

- `INSTANT` changes only the data dictionary
- `NOCOPY` (MariaDB) changes the table in place without rebuilding it
- `INPLACE` changes the table in place, and may rebuild it in place
- `COPY` copies every row into a new table

The engine's lock name in a report is the clause the server accepts for
the statement, such as `INSTANT`, `INPLACE, LOCK=NONE`, or `COPY,
LOCK=SHARED`. What each blocks while the statement's work runs:

- `LOCK=NONE`: reads and writes go on; other DDL waits (`ddl`)
- `LOCK=SHARED`: reads go on; writes wait (`writes`)
- `LOCK=EXCLUSIVE`: everything waits (`reads_and_writes`)
- `INSTANT`: only an exclusive metadata lock, taken and released in a
  moment, during which everything waits (`reads_and_writes`)

Every ALTER TABLE, and DROP TABLE, TRUNCATE, RENAME TABLE, and CREATE
TRIGGER, takes an exclusive metadata lock (MDL) at least briefly. It
queues behind every open transaction that has read the table, and every
later query on the table queues behind it, until `lock_wait_timeout`
runs out. So each of these statements draws the lock-timeout finding
unless a timeout is in scope, whatever its LOCK level. DROP TABLE,
TRUNCATE, RENAME TABLE, and CREATE TRIGGER report the lock as `MDL
EXCLUSIVE`. INSERT, UPDATE, and DELETE hold an intention lock on the
table, reported as `IX`, and lock the rows they change.

A statement the rules leave unknown here includes ANALYZE TABLE, LOCK
TABLES, and anything in another engine's syntax.

MySQL commits each DDL statement on its own, so every statement is a
transaction window of its own.

`context_plan()` reads what the rules use: `VERSION()`, which also names
the server MySQL or MariaDB, `foreign_key_checks` and
`lock_wait_timeout`, each table's size, row format, and FULLTEXT indexes
from `information_schema`, and on MySQL 8.0.29 and later the instant row
versions each table has used from `INNODB_TABLES.TOTAL_ROW_VERSIONS`.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import (
    Callable,
    Dict,
    Generator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from sustained.impact.context import FLOORS, ContextPlan, EngineContext, TableStats
from sustained.impact.model import (
    Action,
    Blocks,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, Profile, Rule, common
from sustained.types import RowValue

_MYSQL_DOCS = "https://dev.mysql.com/doc/refman/8.0/en/"
_ONLINE = _MYSQL_DOCS + "innodb-online-ddl-operations.html"
_MARIADB_DOCS = "https://mariadb.com/kb/en/"
_MARIADB_ONLINE = _MARIADB_DOCS + "innodb-online-ddl-overview/"

INSTANT = "INSTANT"
NOCOPY_NONE = "NOCOPY, LOCK=NONE"
INPLACE_NONE = "INPLACE, LOCK=NONE"
COPY_NONE = "COPY, LOCK=NONE"
INPLACE_SHARED = "INPLACE, LOCK=SHARED"
COPY_SHARED = "COPY, LOCK=SHARED"
INPLACE_EXCLUSIVE = "INPLACE, LOCK=EXCLUSIVE"
COPY_EXCLUSIVE = "COPY, LOCK=EXCLUSIVE"
MDL_EXCLUSIVE = "MDL EXCLUSIVE"
ROW_LOCKS = "IX"

LOCKS = (
    ROW_LOCKS,
    INSTANT,
    NOCOPY_NONE,
    INPLACE_NONE,
    COPY_NONE,
    INPLACE_SHARED,
    COPY_SHARED,
    INPLACE_EXCLUSIVE,
    COPY_EXCLUSIVE,
    MDL_EXCLUSIVE,
)

ALGORITHMS = ("INSTANT", "NOCOPY", "INPLACE", "COPY")
LEVELS = ("NONE", "SHARED", "EXCLUSIVE")

# lock_wait_timeout is in seconds. MySQL's default is a year and
# MariaDB's a day, so a value of a day or more bounds nothing.
_UNBOUNDED_SECONDS = 86400


class Online(NamedTuple):
    """
    How the server runs an ALTER TABLE: the algorithm, and the LOCK
    level, which is None for INSTANT.
    """

    algorithm: str
    level: Optional[str] = None

    @property
    def label(self) -> str:
        """The clause the server accepts, such as `INPLACE, LOCK=NONE`."""
        if self.level is None:
            return self.algorithm
        return f"{self.algorithm}, LOCK={self.level}"

    def combined(self, other: "Online") -> "Online":
        """What the server does for both changes in one statement."""
        algorithm = max(self.algorithm, other.algorithm, key=ALGORITHMS.index)
        if algorithm == "INSTANT":
            return Online(algorithm)
        level = max(self.level or "NONE", other.level or "NONE", key=LEVELS.index)
        return Online(algorithm, level)


def parse_label(label: str) -> Optional[Online]:
    """A lock label read back as an `Online`, or None for another lock."""
    parts = [p.strip() for p in label.split(",")]
    if not parts or parts[0] not in ALGORITHMS:
        return None
    if len(parts) == 1:
        return Online(parts[0]) if parts[0] == "INSTANT" else None
    level = parts[1].replace(" ", "")
    if not level.startswith("LOCK=") or level[5:] not in LEVELS:
        return None
    return Online(parts[0], level[5:])


def blocks(lock: Optional[str]) -> Blocks:
    """What a lock label blocks while the statement's work runs."""
    if lock is None:
        return Blocks.NOTHING
    if lock == ROW_LOCKS:
        return Blocks.DDL
    if lock in (INSTANT, MDL_EXCLUSIVE):
        return Blocks.READS_AND_WRITES
    online = parse_label(lock)
    level = online.level if online is not None else "EXCLUSIVE"
    return {
        "NONE": Blocks.DDL,
        "SHARED": Blocks.WRITES,
    }.get(level or "", Blocks.READS_AND_WRITES)


def lock_rank(lock: Optional[str]) -> int:
    """A lock's strength: its place in `LOCKS`, or -1 for no lock."""
    if lock is None:
        return -1
    return LOCKS.index(lock) if lock in LOCKS else len(LOCKS)


def queues(lock: Optional[str]) -> bool:
    """
    Whether waiting for the lock queues other sessions: every lock but
    a DML statement's needs the exclusive metadata lock for a moment.
    """
    return lock is not None and lock != ROW_LOCKS


def bounded(value: str) -> bool:
    """Whether a `lock_wait_timeout` value bounds the wait."""
    try:
        seconds = float(value)
    except ValueError:
        return False
    return 0 < seconds < _UNBOUNDED_SECONDS


def timeout_statement(transactional: bool) -> str:
    """The statement that bounds how long the next metadata locks queue."""
    return "SET SESSION lock_wait_timeout = 5"


# --- the rules ---------------------------------------------------------


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
_MDL = _MYSQL_DOCS + "metadata-locking.html"
_MARIADB_INSTANT = _MARIADB_DOCS + (
    "innodb-online-ddl-operations-with-the-instant-alter-algorithm/"
)
_MARIADB_NOCOPY = _MARIADB_DOCS + (
    "innodb-online-ddl-operations-with-the-nocopy-alter-algorithm/"
)
_MARIADB_INPLACE = _MARIADB_DOCS + (
    "innodb-online-ddl-operations-with-the-inplace-alter-algorithm/"
)
_MARIADB_ALTER = _MARIADB_DOCS + "alter-table/"
_MARIADB_MDL = _MARIADB_DOCS + "metadata-locking/"

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
        ),
        (
            "ALTER TABLE t ADD COLUMN d int UNIQUE",
            "ALTER TABLE p ADD COLUMN d int AUTO_INCREMENT PRIMARY KEY",
            "ALTER TABLE cz ADD COLUMN d int",
            "ALTER TABLE ft ADD COLUMN d int",
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
        ),
        (
            "ALTER TABLE t ADD COLUMN d varchar(36) DEFAULT (uuid())",
            "ALTER TABLE t ADD COLUMN d int GENERATED ALWAYS AS (c * 2) STORED",
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
        ("ALTER TABLE t DROP COLUMN c",),
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
        ),
    ),
    _Spec(
        "modify_column.inplace",
        _COLUMN_OPS,
        _MARIADB_INPLACE,
        (
            "ALTER TABLE t MODIFY COLUMN name varchar(200)",
            "ALTER TABLE t MODIFY COLUMN small varchar(63)",
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
        ),
        (
            "ALTER TABLE t MODIFY COLUMN c bigint",
            "ALTER TABLE t MODIFY COLUMN name varchar(50)",
            "ALTER TABLE t MODIFY COLUMN e enum('b','a')",
            "ALTER TABLE t CHANGE COLUMN name label text",
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
        ),
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
        _MYSQL_DOCS + "create-table-check-constraints.html",
        _MARIADB_ALTER,
        ("ALTER TABLE t ADD CONSTRAINT ck2 CHECK (c > -1)",),
    ),
    _Spec(
        "drop_check",
        _MYSQL_DOCS + "alter-table.html",
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
        _MYSQL_DOCS + "innodb-locks-set.html",
        _MARIADB_DOCS + "innodb-lock-modes/",
        ("UPDATE t SET name = 'x' WHERE id < 3", "DELETE FROM t WHERE id > 18"),
    ),
    _Spec(
        "insert",
        _MYSQL_DOCS + "innodb-locks-set.html",
        _MARIADB_DOCS + "innodb-lock-modes/",
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
        ("ALTER TABLE t MODIFY COLUMN c bigint, ALGORITHM=INPLACE",),
    ),
)


class _Rules:
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


_RULES: Mapping[str, _Rules] = {"mysql": _Rules("mysql"), "mariadb": _Rules("mariadb")}

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
    "CREATE TABLE ft (id int PRIMARY KEY, body text, FULLTEXT KEY ft_body (body))",
    "INSERT INTO ft VALUES (1, 'a b c')",
    "CREATE TABLE cz (id int PRIMARY KEY, v int) ROW_FORMAT=COMPRESSED",
    "INSERT INTO cz VALUES (1, 1)",
    "CREATE VIEW v AS SELECT id FROM p",
)

# --- facts the rules read ----------------------------------------------


def _mariadb(facts: Facts) -> bool:
    return facts.context.profile == "mariadb"


def _rules(facts: Facts) -> _Rules:
    return _RULES[facts.context.profile]


def _version(facts: Facts) -> Tuple[int, ...]:
    return facts.context.version


def _copy(facts: Facts) -> Online:
    """COPY as the server runs it: MariaDB 11.2 and later allow writes."""
    if _mariadb(facts) and _version(facts) >= (11, 2):
        return Online("COPY", "NONE")
    return Online("COPY", "SHARED")


def _nocopy(facts: Facts) -> Online:
    """An in-place change that rebuilds nothing, as each server names it."""
    return Online("NOCOPY", "NONE") if _mariadb(facts) else Online("INPLACE", "NONE")


def _row_version_limit(version: Tuple[int, ...]) -> int:
    return 255 if version >= (9, 1) else 64


def _off(value: Optional[str]) -> bool:
    return value is not None and value.strip().upper() in ("0", "OFF", "FALSE")


def _foreign_key_checks_off(facts: Facts) -> bool:
    setting = facts.state.settings.get("foreign_key_checks")
    if setting is None:
        setting = facts.context.settings.get("foreign_key_checks")
    return _off(setting)


def _stats(facts: Facts, table: str) -> TableStats:
    if facts.state.is_new(table):
        return TableStats(0, 0, "DYNAMIC", 0, False)
    return facts.context.stats(facts.state.original(table))


class _Change(NamedTuple):
    """What the server does for one ALTER TABLE action."""

    online: Online
    work: Work
    rule: str
    reason: str
    confidence: Confidence = Confidence.KNOWN
    notes: Tuple[str, ...] = ()


# --- ADD COLUMN and DROP COLUMN ----------------------------------------


def _storage_limits(facts: Facts, table: str) -> Tuple[Optional[str], Confidence]:
    """
    Why an instant column change would not be instant on this table, or
    None when nothing stops it, with how sure the answer is.
    """
    stats = _stats(facts, table)
    unread: List[str] = []
    if stats.fulltext:
        return "the table has a FULLTEXT index", Confidence.KNOWN
    if stats.row_format == "COMPRESSED":
        return "the table's ROW_FORMAT is COMPRESSED", Confidence.KNOWN
    if stats.fulltext is None:
        unread.append("its FULLTEXT indexes")
    if stats.row_format is None:
        unread.append("its row format")
    if not _mariadb(facts) and _version(facts) >= (8, 0, 29):
        limit = _row_version_limit(_version(facts))
        if stats.row_versions is not None and stats.row_versions >= limit:
            return (
                f"the table has used all {limit} instant row versions",
                Confidence.KNOWN,
            )
        if stats.row_versions is None:
            unread.append("its instant row versions")
    if unread:
        return None, Confidence.LIKELY
    return None, Confidence.KNOWN


def _unread_note(facts: Facts, table: str) -> str:
    stats = _stats(facts, table)
    missing = []
    if stats.fulltext is None:
        missing.append("a FULLTEXT index")
    if stats.row_format is None:
        missing.append("ROW_FORMAT=COMPRESSED")
    if (
        not _mariadb(facts)
        and _version(facts) >= (8, 0, 29)
        and stats.row_versions is None
    ):
        missing.append("used-up instant row versions")
    return (
        "instant unless the table has "
        + " or ".join(missing)
        + ", which the rules did not read"
    )


def _add_column(facts: Facts, action: Action) -> _Change:
    options = action.options
    table = common.table(facts)
    mariadb = _mariadb(facts)
    default = str(options.get("default") or "")
    if options.get("generated") == "stored":
        return _Change(
            _copy(facts),
            Work.REWRITE,
            "add_column.copy",
            "a stored generated column is computed for every row",
        )
    if options.get("check"):
        return _Change(
            _copy(facts),
            Work.REWRITE,
            "add_column.copy",
            "a column with a CHECK clause has every row checked",
        )
    if options.get("references") and not _foreign_key_checks_off(facts):
        return _Change(
            _copy(facts),
            Work.REWRITE,
            "add_column.copy",
            "with foreign_key_checks on, a column with a REFERENCES clause is "
            "added by copying the table, which checks every row",
        )
    if default.startswith("("):
        volatile = options.get("default_volatility") == "volatile"
        if not mariadb or volatile:
            what = (
                "an expression default"
                if not mariadb
                else f"the default calls {options.get('default_function')}()"
            )
            return _Change(
                _copy(facts),
                Work.REWRITE,
                "add_column.copy",
                f"{what} is computed for every row",
            )
    if options.get("identity"):
        return _Change(
            Online("INPLACE", "SHARED"),
            Work.REWRITE,
            "add_column.rebuild",
            "an AUTO_INCREMENT column is filled for every row",
        )
    if options.get("primary_key") or options.get("unique"):
        return _Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "add_column.rebuild",
            "a column with a key is added by rebuilding the table",
        )
    reason, confidence = _storage_limits(facts, table)
    if reason is not None and "FULLTEXT" in reason and not mariadb:
        return _Change(
            _copy(facts),
            Work.REWRITE,
            "add_column.copy",
            f"{reason}, so the column is added by copying the table",
        )
    if reason is not None and "FULLTEXT" in reason:
        return _Change(
            Online("INPLACE", "SHARED"),
            Work.REWRITE,
            "add_column.rebuild",
            f"{reason}, so the column is added by rebuilding the table",
        )
    if reason is not None:
        return _Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "add_column.rebuild",
            f"{reason}, so the column is added by rebuilding the table",
        )
    if options.get("position") and not mariadb and _version(facts) < (8, 0, 29):
        return _Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "add_column.rebuild",
            "before MySQL 8.0.29 only a column added last is instant",
        )
    note = _unread_note(facts, table) if confidence is Confidence.LIKELY else ""
    return _Change(
        Online("INSTANT"),
        Work.CATALOG,
        "add_column.instant",
        note or "the column is added in the data dictionary",
        confidence,
    )


def _indexed(facts: Facts, table: str, column: str) -> Optional[bool]:
    """Whether a column is part of any index, or None when not read."""
    found = facts.context.table(facts.state.original(table))
    if found is None:
        return None
    wanted = column.lower()
    if wanted in (c.lower() for c in found.primary_key):
        return True
    return any(
        wanted in (c.lower() for c in index.columns) for index in found.indexes.values()
    )


def _drop_column(facts: Facts, action: Action) -> _Change:
    table = common.table(facts)
    column = action.column or "?"
    mariadb = _mariadb(facts)
    indexed = _indexed(facts, table, column)
    if indexed:
        return _Change(
            _nocopy(facts) if mariadb else Online("INPLACE", "NONE"),
            Work.CATALOG if mariadb else Work.REWRITE,
            "drop_column.rebuild",
            f"{column} is part of an index",
        )
    if not mariadb and _version(facts) < (8, 0, 29):
        return _Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "drop_column.rebuild",
            "before MySQL 8.0.29 a column is dropped by rebuilding the table",
        )
    reason, confidence = _storage_limits(facts, table)
    if reason is not None:
        return _Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "drop_column.rebuild",
            f"{reason}, so the column is dropped by rebuilding the table",
        )
    if indexed is None:
        confidence = Confidence.LIKELY
    note = ""
    if confidence is Confidence.LIKELY:
        note = f"instant unless {column} is part of an index" + (
            "" if indexed is not None else ", which the schema read would show"
        )
    return _Change(
        Online("INSTANT"),
        Work.CATALOG,
        "drop_column.instant",
        note or "the column is dropped in the data dictionary",
        confidence,
    )


# --- MODIFY and CHANGE -------------------------------------------------

_DISPLAY_WIDTH_RE = re.compile(
    r"^(tinyint|smallint|mediumint|int|bigint)\(\d+\)(.*)$", re.IGNORECASE
)
_TYPE_ALIASES = {"integer": "int", "bool": "tinyint(1)", "boolean": "tinyint(1)"}
_LENGTH_RE = re.compile(r"^(varchar|varbinary)\((\d+)\)$")
_MEMBERS_RE = re.compile(r"^(enum|set)\((.*)\)$", re.DOTALL)
# The most bytes a character takes in each character set.
_CHARSET_BYTES = {
    "utf8mb4": 4,
    "utf8mb3": 3,
    "utf8": 3,
    "ucs2": 2,
    "utf16": 4,
    "utf16le": 4,
    "utf32": 4,
    "latin1": 1,
    "ascii": 1,
    "binary": 1,
}


def _normal_type(text: str) -> str:
    """
    A column type in one spelling: lower case, and without an integer
    display width, which changes nothing stored. `tinyint(1)` keeps its
    width, since it is how MySQL spells a boolean.
    """
    lowered = " ".join(text.lower().replace("`", "").split())
    lowered = _TYPE_ALIASES.get(lowered, lowered)
    match = _DISPLAY_WIDTH_RE.match(lowered)
    if match and not lowered.startswith("tinyint(1)"):
        lowered = match.group(1) + match.group(2)
    return lowered


def _members(body: str) -> Tuple[str, ...]:
    return tuple(part.strip() for part in body.split(","))


def _length_bytes(
    old: int, new: int, collation: Optional[str]
) -> Tuple[bool, Confidence]:
    """
    Whether a VARCHAR keeps its length prefix: one byte while its
    longest value takes at most 255 bytes, two above that.
    """
    charset = (collation or "").split("_", 1)[0].lower()
    width = _CHARSET_BYTES.get(charset)
    if width is not None:
        return (old * width > 255) == (new * width > 255), Confidence.KNOWN
    one = (old > 255) == (new > 255)
    four = (old * 4 > 255) == (new * 4 > 255)
    if one == four:
        return one, Confidence.KNOWN
    # The character set was not read; utf8mb4 is the default.
    return four, Confidence.LIKELY


def _type_change(
    facts: Facts, old: str, new: str, collation: Optional[str]
) -> Optional[_Change]:
    """The change a new column type makes, or None when it is the same."""
    a, b = _normal_type(old), _normal_type(new)
    if a == b:
        return None
    mariadb = _mariadb(facts)
    lengths = _LENGTH_RE.match(a), _LENGTH_RE.match(b)
    if lengths[0] and lengths[1] and lengths[0].group(1) == lengths[1].group(1):
        before, after = int(lengths[0].group(2)), int(lengths[1].group(2))
        if after >= before:
            if mariadb:
                return _Change(
                    Online("INSTANT"),
                    Work.CATALOG,
                    "modify_column.instant",
                    f"{old} to {new} widens the column in the data dictionary",
                )
            same, confidence = _length_bytes(before, after, collation)
            if same:
                return _Change(
                    Online("INPLACE", "NONE"),
                    Work.CATALOG,
                    "modify_column.inplace",
                    f"{old} to {new} keeps the length prefix, so only the data "
                    "dictionary changes",
                    confidence,
                )
            return _Change(
                _copy(facts),
                Work.REWRITE,
                "modify_column.copy",
                f"{old} to {new} needs a longer length prefix on every row",
                confidence,
            )
    members = _MEMBERS_RE.match(a), _MEMBERS_RE.match(b)
    if members[0] and members[1] and members[0].group(1) == members[1].group(1):
        before_members = _members(members[0].group(2))
        after_members = _members(members[1].group(2))
        kind = members[0].group(1)
        appended = after_members[: len(before_members)] == before_members
        if kind == "enum":
            same_size = (len(before_members) > 255) == (len(after_members) > 255)
        else:
            same_size = (len(before_members) + 7) // 8 == (len(after_members) + 7) // 8
        if appended and same_size:
            return _Change(
                Online("INSTANT"),
                Work.CATALOG,
                "modify_column.instant",
                f"new {kind.upper()} members are added at the end",
            )
    return _Change(
        _copy(facts),
        Work.REWRITE,
        "modify_column.copy",
        f"{old} to {new} converts every row",
    )


def _modify_column(facts: Facts, action: Action) -> _Change:
    table = common.table(facts)
    column = action.column or "?"
    options = action.options
    mariadb = _mariadb(facts)
    changes: List[_Change] = []
    new_name = options.get("new")
    if new_name and str(new_name).lower() != column.lower():
        changes.append(_rename_column(facts))
    if options.get("position"):
        if mariadb:
            changes.append(
                _Change(
                    Online("INSTANT"),
                    Work.CATALOG,
                    "modify_column.instant",
                    "MariaDB reorders columns in the data dictionary",
                )
            )
        else:
            changes.append(
                _Change(
                    Online("INPLACE", "NONE"),
                    Work.REWRITE,
                    "modify_column.rebuild",
                    "a column is moved by rebuilding the table",
                )
            )
    intent = facts.intent
    found = facts.context.table(facts.state.original(table))
    spec = None
    if found is not None:
        for name, candidate in found.columns.items():
            if name.lower() == column.lower():
                spec = candidate
    new_type = str(options.get("type"))
    new_nullable = not options.get("not_null") and not options.get("primary_key")
    if intent is not None and intent.kind == "alter_column_type":
        change = _type_change(
            facts,
            str(intent.get("from_type")),
            str(intent.get("to_type") or new_type),
            spec.collation if spec is not None else None,
        )
        if change is not None:
            changes.append(change)
    elif intent is not None and intent.kind in ("set_not_null", "drop_not_null"):
        changes.append(_nullability(intent.kind == "drop_not_null"))
    elif intent is not None and intent.kind == "set_column_comment":
        pass
    elif spec is not None:
        change = _type_change(facts, spec.raw_type, new_type, spec.collation)
        if change is not None:
            changes.append(change)
        if spec.nullable != new_nullable:
            changes.append(_nullability(new_nullable))
        if bool(options.get("identity")) != spec.autoincrement:
            changes.append(
                _Change(
                    _copy(facts),
                    Work.REWRITE,
                    "modify_column.copy",
                    "a change to AUTO_INCREMENT fills every row",
                )
            )
    else:
        changes.append(
            _Change(
                _copy(facts),
                Work.REWRITE,
                "modify_column.copy",
                f"the current definition of {column} is not known, so the change "
                "counts as a type change, which copies the table",
                Confidence.LIKELY,
            )
        )
    if options.get("generated"):
        changes.append(
            _Change(
                _copy(facts),
                Work.REWRITE,
                "modify_column.copy",
                "a generated column is computed again for every row",
                Confidence.LIKELY,
            )
        )
    if not changes:
        return _Change(
            Online("INSTANT"),
            Work.CATALOG,
            "modify_column.instant",
            "only the column's default or comment changes",
        )
    return _heaviest(changes)


def _nullability(nullable: bool) -> _Change:
    what = "NOT NULL to NULL" if nullable else "NULL to NOT NULL"
    return _Change(
        Online("INPLACE", "NONE"),
        Work.REWRITE,
        "modify_column.rebuild",
        f"a column changed from {what} is rebuilt in place",
    )


def _heaviest(changes: Sequence[_Change]) -> _Change:
    """The changes of one statement as the server runs them together."""
    online = changes[0].online
    for change in changes[1:]:
        online = online.combined(change.online)
    worst = max(
        changes,
        key=lambda c: (ALGORITHMS.index(c.online.algorithm), c.work),
    )
    confidence = min(c.confidence for c in changes)
    notes = tuple(n for c in changes for n in c.notes)
    return _Change(
        online,
        max(c.work for c in changes),
        worst.rule,
        worst.reason,
        confidence,
        notes,
    )


# --- the other ALTER TABLE actions -------------------------------------


def _rename_note(what: str, old: str) -> str:
    return f"running application code that names the {what} {old} fails once the rename runs"


def _rename_column(facts: Facts, action: Optional[Action] = None) -> _Change:
    old = action.column if action is not None else None
    notes = (_rename_note("column", old),) if old else ()
    if not _mariadb(facts) and _version(facts) < (8, 0, 28):
        return _Change(
            Online("INPLACE", "NONE"),
            Work.CATALOG,
            "rename",
            "before MySQL 8.0.28 a column is renamed in place",
            notes=notes,
        )
    return _Change(
        Online("INSTANT"),
        Work.CATALOG,
        "rename",
        "the column is renamed in the data dictionary",
        notes=notes,
    )


def _change_column(facts: Facts, action: Action) -> _Change:
    change = _modify_column(facts, action)
    new = action.options.get("new")
    if new and action.column and str(new).lower() != action.column.lower():
        note = _rename_note("column", action.column)
        change = change._replace(notes=change.notes + (note,))
    return change


def _fixed(
    online: Callable[[Facts], Online], work: Work, rule: str, reason: str
) -> Callable[[Facts, Action], _Change]:
    def handler(facts: Facts, action: Action) -> _Change:
        return _Change(online(facts), work, rule, reason)

    return handler


def _instant(facts: Facts) -> Online:
    return Online("INSTANT")


def _inplace(facts: Facts) -> Online:
    return Online("INPLACE", "NONE")


def _instant_on_mariadb(facts: Facts) -> Online:
    return Online("INSTANT") if _mariadb(facts) else Online("INPLACE", "NONE")


def _rename_to(facts: Facts, action: Action) -> _Change:
    return _Change(
        Online("INSTANT"),
        Work.CATALOG,
        "rename",
        "the table is renamed in the data dictionary",
        notes=(_rename_note("table", common.table(facts)),),
    )


def _rename_action(facts: Facts, action: Action) -> _Change:
    return _rename_column(facts, action)


def _add_index(facts: Facts, action: Action) -> _Change:
    return _index_change(facts, bool(action.options.get("fulltext")))


def _index_change(facts: Facts, fulltext: bool) -> _Change:
    if not fulltext:
        return _Change(
            _nocopy(facts),
            Work.INDEX_BUILD,
            "add_index",
            "the index is built while reads and writes go on",
        )
    table = common.table(facts)
    existing = _stats(facts, table).fulltext
    if existing:
        return _Change(
            Online("INPLACE", "SHARED"),
            Work.INDEX_BUILD,
            "add_fulltext",
            "a FULLTEXT index is built while writes wait",
        )
    return _Change(
        Online("INPLACE", "SHARED"),
        Work.REWRITE,
        "add_fulltext",
        "the table's first FULLTEXT index adds a hidden FTS_DOC_ID column, "
        "which rebuilds the table while writes wait",
        Confidence.KNOWN if existing is not None else Confidence.LIKELY,
    )


def _add_constraint(facts: Facts, action: Action) -> _Change:
    options = action.options
    constraint = options.get("constraint")
    if constraint == "primary_key":
        return _Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "add_primary_key",
            "a primary key orders the rows, so the table is rebuilt in place",
        )
    if constraint == "unique":
        return _index_change(facts, False)
    if constraint == "foreign_key":
        return _add_foreign_key(facts, action)
    if constraint == "check":
        return _Change(
            _copy(facts),
            Work.REWRITE,
            "add_check",
            "the check is added by copying the table, which checks every row",
        )
    return _unread(action)


def _add_foreign_key(facts: Facts, action: Action) -> _Change:
    table = common.table(facts)
    if _foreign_key_checks_off(facts):
        return _Change(
            Online("INSTANT") if _mariadb(facts) else Online("INPLACE", "NONE"),
            Work.CATALOG,
            "add_foreign_key.unchecked",
            "foreign_key_checks is off, so the key is added without reading a row",
            notes=(
                f"with foreign_key_checks off, the rows already in {table} are not "
                "checked against the key, and a row that breaks it stays",
            ),
        )
    return _Change(
        _copy(facts),
        Work.REWRITE,
        "add_foreign_key",
        "with foreign_key_checks on, the key is added by copying the table, "
        "which checks every row; with foreign_key_checks = 0 it is added in "
        "place, but the rows already there go unchecked",
    )


def _drop_constraint(facts: Facts, action: Action) -> _Change:
    options = action.options
    constraint = options.get("constraint")
    name = str(options.get("name") or "")
    table = common.table(facts)
    live = facts.state.original(table)
    if constraint is None and name:
        constraint = _constraint_kind(facts, live, name)
    if constraint == "primary_key" or name.upper() == "PRIMARY":
        return _Change(
            _copy(facts),
            Work.REWRITE,
            "drop_primary_key",
            "without a primary key the rows are ordered again, by copying the table",
        )
    if constraint == "foreign_key":
        return _Change(
            _instant_on_mariadb(facts),
            Work.CATALOG,
            "drop_foreign_key",
            "the key is dropped in the data dictionary",
        )
    if constraint == "check":
        return _Change(
            Online("INSTANT"),
            Work.CATALOG,
            "drop_check",
            "the check is dropped in the data dictionary",
        )
    if constraint == "unique":
        return _drop_index_change(facts)
    return _Change(
        _nocopy(facts),
        Work.CATALOG,
        "drop_index",
        f"the rules do not know what kind of constraint {name} is",
        Confidence.LIKELY,
    )


def _constraint_kind(facts: Facts, table: str, name: str) -> Optional[str]:
    """What kind of constraint a name is on the table, from the schema read."""
    found = facts.context.table(table)
    if found is None:
        return None
    lowered = name.lower()
    if facts.context.foreign_key_target(table, name) is not None:
        return "foreign_key"
    if lowered in found.check_names or lowered in found.checks:
        return "check"
    if lowered in found.indexes:
        return "unique"
    return None


def _drop_index_change(facts: Facts) -> _Change:
    return _Change(
        _nocopy(facts),
        Work.CATALOG,
        "drop_index",
        "the index is dropped in the data dictionary",
    )


def _drop_index_action(facts: Facts, action: Action) -> _Change:
    if str(action.options.get("name") or "").upper() == "PRIMARY":
        return _drop_constraint(
            facts, Action("drop_constraint", None, {"name": "PRIMARY"})
        )
    return _drop_index_change(facts)


def _engine(facts: Facts, action: Action) -> _Change:
    engine = str(action.options.get("engine") or "").upper()
    if engine == "INNODB":
        return _Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "table_rebuild",
            "ENGINE=InnoDB on an InnoDB table rebuilds it in place",
        )
    return _Change(
        _copy(facts),
        Work.REWRITE,
        "table_copy",
        f"moving the table to {engine} copies it",
    )


def _table_option(facts: Facts, action: Action) -> _Change:
    name = str(action.options.get("name"))
    if name in ("row_format", "key_block_size"):
        return _Change(
            Online("INPLACE", "NONE"),
            Work.REWRITE,
            "table_rebuild",
            f"a new {name.upper()} rebuilds the table in place",
        )
    return _Change(
        _instant_on_mariadb(facts),
        Work.CATALOG,
        "table_option",
        f"{name.upper()} changes in the data dictionary",
    )


def _unread(action: Action) -> _Change:
    return _Change(
        Online("COPY", "EXCLUSIVE"),
        Work.UNKNOWN,
        "",
        f"no rule reads the ALTER TABLE action {action.kind}",
        Confidence.UNKNOWN,
    )


_ActionHandler = Callable[[Facts, Action], _Change]
_ACTIONS: Dict[str, _ActionHandler] = {
    "add_column": _add_column,
    "drop_column": _drop_column,
    "modify_column": _modify_column,
    "change_column": _change_column,
    "set_default": _fixed(
        _instant,
        Work.CATALOG,
        "column_default",
        "the default changes in the data dictionary",
    ),
    "drop_default": _fixed(
        _instant,
        Work.CATALOG,
        "column_default",
        "the default changes in the data dictionary",
    ),
    "set_storage": _fixed(
        _instant,
        Work.CATALOG,
        "column_default",
        "the column's visibility changes in the data dictionary",
    ),
    "rename_column": _rename_action,
    "rename_to": _rename_to,
    "rename_index": _fixed(
        _instant_on_mariadb,
        Work.CATALOG,
        "rename_index",
        "the index is renamed in the data dictionary",
    ),
    "add_index": _add_index,
    "drop_index": _drop_index_action,
    "add_constraint": _add_constraint,
    "drop_constraint": _drop_constraint,
    "convert_charset": _fixed(
        _copy,
        Work.REWRITE,
        "table_copy",
        "every text column is converted, by copying the table",
    ),
    "engine": _engine,
    "force": _fixed(
        _inplace, Work.REWRITE, "table_rebuild", "FORCE rebuilds the table in place"
    ),
    "table_option": _table_option,
}


# --- statements --------------------------------------------------------


def assertion(
    statement: str, kind: str, online: Online, mariadb: bool = False
) -> Optional[str]:
    """
    The statement with an ALGORITHM and LOCK clause that asserts how the
    server runs it, so the server refuses the statement instead of
    running it another way. A CREATE INDEX, and a DROP INDEX on MySQL,
    take the clause without a comma. MariaDB's DROP INDEX takes no
    clause, so there it becomes the ALTER TABLE ... DROP INDEX it stands
    for. MySQL refuses a LOCK clause beside ALGORITHM=INSTANT, so INSTANT
    asserts the algorithm alone. None when the statement cannot be read
    back into the ALTER TABLE form.
    """
    text = statement.strip().rstrip(";").rstrip()
    parts = [f"ALGORITHM={online.algorithm}"]
    if online.level is not None:
        parts.append(f"LOCK={online.level}")
    if kind == "drop_index" and mariadb:
        altered = _drop_index_as_alter(text)
        if altered is None:
            return None
        return f"{altered}, {', '.join(parts)}"
    if kind == "alter_table":
        return f"{text}, {', '.join(parts)}"
    return f"{text} {' '.join(parts)}"


def _drop_index_as_alter(statement: str) -> Optional[str]:
    """`DROP INDEX ix ON t` as `ALTER TABLE t DROP INDEX ix`."""
    from sustained.dialects import Dialects
    from sustained.impact.tokens import tokenize

    tokens = tokenize(statement, Dialects.MYSQL)
    words = [t for t in tokens if t.is_word("ON")]
    if len(tokens) < 5 or not tokens[1].is_word("INDEX") or len(words) != 1:
        return None
    on = words[0]
    index = statement[tokens[2].start : on.start].strip()
    table = statement[on.start + len(on.text) :].strip()
    return f"ALTER TABLE {table} DROP INDEX {index}"


def _requested(facts: Facts) -> Tuple[Optional[str], Optional[str]]:
    """The ALGORITHM and LOCK the statement spells, DEFAULT read as none."""
    options = facts.parsed.options
    algorithm = options.get("algorithm")
    lock = options.get("lock")
    algorithm = None if algorithm in (None, "DEFAULT") else str(algorithm).upper()
    lock = None if lock in (None, "DEFAULT") else str(lock).upper()
    return algorithm, lock


def _online_outcome(facts: Facts, change: _Change) -> Outcome:
    """
    The outcome of an ALTER TABLE, CREATE INDEX, or DROP INDEX the
    server runs as `change` says, after the ALGORITHM and LOCK the
    statement spells.
    """
    rules = _rules(facts)
    table = common.table(facts)
    requested_algorithm, requested_lock = _requested(facts)
    online = change.online
    refusal = _refusal(facts, online, requested_algorithm, requested_lock)
    notes = [
        Finding(
            rules[change.rule].id, Severity.INFO, note, source=rules[change.rule].source
        )
        for note in change.notes
    ]
    if refusal is not None:
        rule = rules["refused"]
        finding = Finding(
            rule.id,
            Severity.WARN,
            f"the server refuses this statement: {refusal}; {change.reason}",
            source=rules[change.rule].source,
        )
        return Outcome(
            (Effect(rule, table, None, Work.CATALOG, change.confidence),),
            (finding,),
            change.confidence,
        )
    work = change.work
    confidence = change.confidence
    if requested_algorithm is not None and ALGORITHMS.index(
        requested_algorithm
    ) > ALGORITHMS.index(online.algorithm):
        online = Online(requested_algorithm, online.level or "NONE")
        if requested_algorithm == "COPY":
            work = Work.REWRITE
        else:
            confidence = min(confidence, Confidence.LIKELY)
    if requested_lock is not None and online.algorithm != "INSTANT":
        level = online.level or "NONE"
        if LEVELS.index(requested_lock) > LEVELS.index(level):
            online = Online(online.algorithm, requested_lock)
    elif requested_lock is not None and not _mariadb(facts):
        # MySQL runs a LOCK clause without ALGORITHM in place: INSTANT
        # takes no LOCK clause.
        online = Online("INPLACE", requested_lock)
        confidence = min(confidence, Confidence.LIKELY)
    rule = rules[change.rule]
    findings: List[Finding] = list(notes)
    message = _message(facts, table, online, work, change.reason)
    if (
        requested_algorithm is None
        and online.label in (INSTANT, NOCOPY_NONE, INPLACE_NONE)
        and not facts.state.is_new(table)
    ):
        asserted = assertion(
            facts.statement, facts.parsed.kind, online, _mariadb(facts)
        )
        if asserted is not None:
            findings.append(
                Finding(
                    rule.id,
                    Severity.INFO,
                    f"{change.reason}; assert {online.label} so the server refuses "
                    "the statement instead of running it with a slower algorithm or "
                    "a stronger lock",
                    (asserted,),
                    rule.source,
                )
            )
    elif (
        message is None
        and confidence is not Confidence.KNOWN
        and not facts.state.is_new(table)
    ):
        findings.append(
            Finding(rule.id, Severity.INFO, change.reason, source=rule.source)
        )
    effect = Effect(rule, table, online.label, work, confidence, message=message)
    effects = [effect] + _parent_effects(facts)
    return Outcome(tuple(effects), tuple(findings), confidence)


def _message(
    facts: Facts, table: str, online: Online, work: Work, reason: str
) -> Optional[str]:
    blocked = blocks(online.label)
    if blocked < Blocks.WRITES or work is Work.CATALOG:
        return None
    who = "reads and writes on" if blocked is Blocks.READS_AND_WRITES else "writes to"
    tool = ""
    if online.algorithm == "COPY":
        tool = (
            "; an online schema change tool such as gh-ost or pt-online-schema-change "
            "copies the table without blocking writes"
        )
    return f"{reason}, and {who} {table} wait until it finishes ({online.label}){tool}"


def _refusal(
    facts: Facts,
    online: Online,
    algorithm: Optional[str],
    lock: Optional[str],
) -> Optional[str]:
    """Why the server refuses the ALGORITHM and LOCK spelled, or None."""
    if algorithm is not None and algorithm not in ALGORITHMS:
        return None
    if algorithm == "NOCOPY" and not _mariadb(facts):
        return "MySQL has no ALGORITHM=NOCOPY"
    if algorithm is not None and ALGORITHMS.index(algorithm) < ALGORITHMS.index(
        online.algorithm
    ):
        return f"ALGORITHM={algorithm} cannot run it, which needs {online.algorithm}"
    if (
        algorithm == "INSTANT"
        and lock is not None
        and lock != "DEFAULT"
        and not _mariadb(facts)
    ):
        return "MySQL takes no LOCK clause beside ALGORITHM=INSTANT"
    if lock is not None and lock in LEVELS and online.level is not None:
        if LEVELS.index(lock) < LEVELS.index(online.level):
            return f"LOCK={lock} cannot run it, which needs LOCK={online.level}"
    return None


def _parent_effects(facts: Facts) -> List[Effect]:
    """
    MySQL locks the table at the other end of a foreign key that a
    statement adds or drops: it takes the exclusive metadata lock on the
    parent table, as on the table itself. MariaDB does not.
    """
    if _mariadb(facts):
        return []
    rule = _rules(facts)["foreign_key_parent"]
    table = common.table(facts)
    live = facts.state.original(table)
    parents: List[str] = []
    for action in facts.parsed.actions:
        options = action.options
        if (
            action.kind == "add_constraint"
            and options.get("constraint") == "foreign_key"
        ):
            parents.append(str(options.get("references")))
        elif action.kind == "add_column" and options.get("references"):
            parents.append(str(options.get("references")))
        elif action.kind == "drop_constraint" and options.get("name"):
            target = facts.context.foreign_key_target(live, str(options["name"]))
            if target is not None:
                parents.append(target)
    return [_parent_effect(rule, table, parent) for parent in _unique(parents, table)]


def _unique(names: Sequence[str], exclude: str) -> List[str]:
    seen = {exclude.lower()}
    found: List[str] = []
    for name in names:
        if name.lower() not in seen:
            seen.add(name.lower())
            found.append(name)
    return found


def _parent_effect(rule: Rule, child: str, parent: str) -> Effect:
    return Effect(
        rule,
        parent,
        MDL_EXCLUSIVE,
        Work.CATALOG,
        message=f"MySQL takes the exclusive metadata lock on {parent}, which a "
        f"foreign key of {child} points at; reads and writes on {parent} wait "
        "while it is held",
    )


def _alter_table(facts: Facts) -> Outcome:
    changes: List[_Change] = []
    actions = facts.parsed.actions
    kinds = {(a.kind, a.options.get("constraint")) for a in actions}
    swaps_primary_key = ("add_constraint", "primary_key") in kinds and (
        ("drop_constraint", "primary_key") in kinds
    )
    for action in actions:
        handler = _ACTIONS.get(action.kind)
        if handler is None:
            return common.unknown(facts, f"the ALTER TABLE action {action.kind}")
        if swaps_primary_key and action.kind == "drop_constraint":
            # Dropping the primary key and adding another in the same
            # statement rebuilds the table in place.
            continue
        changes.append(handler(facts, action))
    if not changes:
        return common.unknown(facts, f"the ALTER TABLE action {actions[0].kind}")
    return _online_outcome(facts, _heaviest(changes))


def _create_index(facts: Facts) -> Outcome:
    return _online_outcome(
        facts, _index_change(facts, bool(facts.parsed.options.get("fulltext")))
    )


def _drop_index(facts: Facts) -> Outcome:
    name = str(facts.parsed.options.get("name") or "")
    if name.upper() == "PRIMARY":
        change = _drop_constraint(
            facts, Action("drop_constraint", None, {"name": "PRIMARY"})
        )
    else:
        change = _drop_index_change(facts)
    return _online_outcome(facts, change)


def _metadata(facts: Facts, rule_name: str, tables: Sequence[str]) -> Outcome:
    rule = _rules(facts)[rule_name]
    return Outcome(
        tuple(Effect(rule, table, MDL_EXCLUSIVE, Work.CATALOG) for table in tables)
    )


def _drop_table(facts: Facts) -> Outcome:
    named = common.tables(facts)
    outcome = _metadata(facts, "drop_table", named)
    if _mariadb(facts) or facts.parsed.kind != "drop_table":
        return outcome
    rule = _rules(facts)["foreign_key_parent"]
    parents: List[Effect] = []
    seen = {t.lower() for t in named}
    for table in named:
        for parent in facts.context.references(facts.state.original(table)):
            if parent.lower() not in seen:
                seen.add(parent.lower())
                parents.append(_parent_effect(rule, table, parent))
    return outcome._replace(effects=outcome.effects + tuple(parents))


def _rename_table(facts: Facts) -> Outcome:
    rule = _rules(facts)["rename"]
    effects = []
    for old, _ in facts.parsed.items("renames"):
        note = Finding(
            rule.id,
            Severity.INFO,
            _rename_note("table", str(old)),
            source=rule.source,
        )
        effects.append(
            Effect(rule, str(old), MDL_EXCLUSIVE, Work.CATALOG, notes=(note,))
        )
    return Outcome(tuple(effects))


def _create_table(facts: Facts) -> Outcome:
    if _mariadb(facts):
        return Outcome()
    rule = _rules(facts)["foreign_key_parent"]
    table = common.table(facts)
    return Outcome(
        tuple(
            _parent_effect(rule, table, str(parent))
            for parent in _unique(
                [str(p) for p in facts.parsed.items("references")], table
            )
        )
    )


def _optimize(facts: Facts) -> Outcome:
    rule = _rules(facts)["table_rebuild"]
    effects = []
    for table in common.tables(facts):
        if _stats(facts, table).fulltext:
            online = _copy(facts)
        else:
            online = Online("INPLACE", "NONE")
        effects.append(
            Effect(
                rule,
                table,
                online.label,
                Work.REWRITE,
                message=_message(
                    facts,
                    table,
                    online,
                    Work.REWRITE,
                    "OPTIMIZE TABLE rebuilds an InnoDB table",
                ),
            )
        )
    return Outcome(tuple(effects))


def _write_rows(facts: Facts) -> Outcome:
    rule = _rules(facts)["write_rows"]
    table = common.table(facts)
    message = common.row_write_message(
        facts,
        table,
        ", and InnoDB locks every row the statement reads when no index narrows "
        "the WHERE clause",
    )
    return Outcome(
        (
            Effect(
                rule,
                table,
                ROW_LOCKS,
                Work.ROWS,
                message=message,
                blocks=Blocks.WRITES,
            ),
        )
    )


def _insert(facts: Facts) -> Outcome:
    rule = _rules(facts)["insert"]
    table = common.table(facts)
    return Outcome((Effect(rule, table, ROW_LOCKS, Work.ROWS),))


def _trigger(facts: Facts) -> Outcome:
    table = facts.parsed.table
    if table is None:
        # DROP TRIGGER names no table, and the schema read on MySQL
        # reads no triggers.
        return Outcome(confidence=Confidence.LIKELY)
    return _metadata(facts, "trigger", [table])


def _drop_view(facts: Facts) -> Outcome:
    return _metadata(facts, "drop_view", common.tables(facts))


_STATEMENTS: Dict[str, common.Handler] = {
    "alter_table": _alter_table,
    "create_index": _create_index,
    "drop_index": _drop_index,
    "create_table": _create_table,
    "drop_table": _drop_table,
    "truncate": _drop_table,
    "rename_table": _rename_table,
    "optimize_table": _optimize,
    "update": _write_rows,
    "delete": _write_rows,
    "insert": _insert,
    "create_trigger": _trigger,
    "drop_trigger": _trigger,
    "drop_view": _drop_view,
    "create_view": common.nothing,
    "create_object": common.nothing,
    "drop_object": common.nothing,
    "set": common.nothing,
}


def effects(facts: Facts) -> Outcome:
    """What the statement does on MySQL or MariaDB, table by table."""
    return common.dispatch(facts, _STATEMENTS)


# --- the server facts --------------------------------------------------

_SETTINGS_SQL = "SELECT VERSION(), @@foreign_key_checks, @@lock_wait_timeout"

_SYSTEM_SCHEMAS = "('mysql', 'information_schema', 'performance_schema', 'sys')"

# One row per base table outside the system schemas: its schema, its
# name, whether it is in the current database, the estimated rows, the
# bytes of its data and indexes, and its row format. The hint reads the
# figures from the storage engine instead of the cache MySQL keeps for
# a day by default; MariaDB reads the hint as a comment.
_SIZES_SQL = (
    "SELECT /*+ SET_VAR(information_schema_stats_expiry = 0) */ "
    "TABLE_SCHEMA, TABLE_NAME, TABLE_SCHEMA = DATABASE(), TABLE_ROWS, "
    "COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0), UPPER(ROW_FORMAT) "
    "FROM information_schema.TABLES "
    f"WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"
)

_FULLTEXT_SQL = (
    "SELECT DISTINCT TABLE_SCHEMA, TABLE_NAME FROM information_schema.STATISTICS "
    f"WHERE INDEX_TYPE = 'FULLTEXT' AND TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"
)

# MySQL 8.0.29 and later count the instant column changes of each table.
_ROW_VERSIONS_SQL = (
    "SELECT NAME, TOTAL_ROW_VERSIONS FROM information_schema.INNODB_TABLES "
    "WHERE TOTAL_ROW_VERSIONS > 0"
)


def server_version(text: str) -> Tuple[str, Tuple[int, ...]]:
    """
    The profile and version a `VERSION()` value names, such as
    ('mariadb', (11, 4, 13)) for `11.4.13-MariaDB-ubu2404`.
    """
    profile = "mariadb" if "mariadb" in text.lower() else "mysql"
    match = re.match(r"(\d+(?:\.\d+)*)", text.strip())
    if match is None:
        return profile, FLOORS[profile]
    version = tuple(int(part) for part in match.group(1).split("."))
    return profile, version


def context_plan() -> ContextPlan:
    """
    Reads the version, the settings, the table sizes, and the storage
    facts. A statement that fails leaves its facts out of `read`, and
    the rules assume the floor or the worst case for them.
    """
    profile, version = "mysql", FLOORS["mysql"]
    settings: Dict[str, str] = {}
    tables: Dict[str, TableStats] = {}
    read: Set[str] = set()
    try:
        rows = yield _SETTINGS_SQL
    except Exception:
        rows = []
    if rows:
        text, checks, timeout = rows[0]
        profile, version = server_version(str(text))
        settings = {
            "foreign_key_checks": str(checks),
            "lock_wait_timeout": str(timeout),
        }
        read |= {"version", "settings"}
    try:
        sizes = yield _SIZES_SQL
    except Exception:
        sizes = None
    if sizes is not None:
        tables, current = _sizes(sizes)
        read.add("sizes")
        yield from _storage(tables, current, profile, version, read)
    return EngineContext(
        profile,
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(tables),
        read=frozenset(read),
    )


_StoragePlan = Generator[str, List[Sequence[RowValue]], None]


def _storage(
    tables: Dict[str, TableStats],
    current: Mapping[str, str],
    profile: str,
    version: Tuple[int, ...],
    read: Set[str],
) -> _StoragePlan:
    """
    Adds each table's FULLTEXT indexes and instant row versions to
    `tables`, whose `schema.table` keys `current` maps each bare key to.
    """
    try:
        fulltext = yield _FULLTEXT_SQL
    except Exception:
        fulltext = None
    if fulltext is not None:
        marked = {f"{schema}.{name}".lower() for schema, name in fulltext}
        _update(tables, current, lambda key, s: s._replace(fulltext=key in marked))
        read.add("fulltext")
    if profile != "mysql" or version < (8, 0, 29):
        return
    try:
        versions = yield _ROW_VERSIONS_SQL
    except Exception:
        return
    # INNODB_TABLES names a table `schema/table`.
    counts = {
        str(name).replace("/", ".", 1).lower(): int(str(count))
        for name, count in versions
    }
    _update(tables, current, lambda key, s: s._replace(row_versions=counts.get(key, 0)))
    read.add("row_versions")


def _update(
    tables: Dict[str, TableStats],
    current: Mapping[str, str],
    change: Callable[[str, TableStats], TableStats],
) -> None:
    """Replaces each table's stats, under its full key and its bare key."""
    for key in [k for k in tables if "." in k]:
        tables[key] = change(key, tables[key])
    for bare, key in current.items():
        tables[bare] = tables[key]


def _sizes(
    rows: Sequence[Sequence[object]],
) -> Tuple[Dict[str, TableStats], Dict[str, str]]:
    """
    Each table's stats, keyed `schema.table`, and also by the bare name
    when the table is in the current database, with the full key each
    bare key stands for.
    """
    tables: Dict[str, TableStats] = {}
    current: Dict[str, str] = {}
    for schema, name, here, count, size, row_format in rows:
        stats = TableStats(
            None if count is None else int(str(count)),
            int(str(size)),
            None if row_format is None else str(row_format),
        )
        bare = bool(here and int(str(here)))
        key = common.add_stats(tables, str(schema), str(name), bare, stats)
        if bare:
            current[str(name).lower()] = key
    return tables, current


def _profile(name: str) -> Profile:
    mariadb = name == "mariadb"
    return Profile(
        name=name,
        title="MariaDB" if mariadb else "MySQL",
        prefix=name,
        effects=effects,
        blocks=blocks,
        lock_rank=lock_rank,
        timeout_setting="lock_wait_timeout",
        timeout_statement=timeout_statement,
        transactional_ddl=False,
        rules=_RULES[name].all(),
        timeout_source=(
            _MARIADB_DOCS + "server-system-variables/#lock_wait_timeout"
            if mariadb
            else _MYSQL_DOCS + "server-system-variables.html#sysvar_lock_wait_timeout"
        ),
        context_plan=context_plan,
        fixture_schema=FIXTURE_SCHEMA,
        queues=queues,
        bounded=bounded,
        local_scope=False,
    )


MYSQL = _profile("mysql")
MARIADB = _profile("mariadb")
