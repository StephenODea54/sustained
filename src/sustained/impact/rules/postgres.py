"""
The PostgreSQL rules, for 12 and later.

Each statement kind, and each ALTER TABLE action, has a handler that
names the lock the statement takes on each table and the work it does
there. The analyzer rates that work against the table's size and the
run so far; a handler only says what the engine does.

The lock names are the ones `pg_locks.mode` reports, without the `Lock`
suffix, ordered weakest first in `LOCKS`. What each blocks follows the
conflict table in the documentation's "Explicit Locking" chapter:

- ACCESS SHARE up to SHARE UPDATE EXCLUSIVE conflict with no reads or
  writes, only with other DDL (`ddl`)
- SHARE, SHARE ROW EXCLUSIVE, and EXCLUSIVE conflict with ROW EXCLUSIVE,
  so INSERT, UPDATE, and DELETE wait (`writes`)
- ACCESS EXCLUSIVE conflicts with every lock, reads included
  (`reads_and_writes`)

A statement spelled in another engine's syntax, such as MySQL's MODIFY,
is unknown here.

`context_plan()` reads the server facts the rules use: the version, the
`TimeZone` and `lock_timeout` settings, and each table's size from
`pg_class.reltuples` and `pg_total_relation_size()`. A partitioned
table's size is the sum of its leaf partitions.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from sustained.impact.context import FLOORS, ContextPlan, EngineContext, TableStats
from sustained.impact.model import (
    Action,
    Blocks,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, Profile, Rule
from sustained.impact.tokens import PUNCT, Token, tokenize

_DOCS = "https://www.postgresql.org/docs/current/"
_ALTER_TABLE = _DOCS + "sql-altertable.html"

ACCESS_SHARE = "ACCESS SHARE"
ROW_SHARE = "ROW SHARE"
ROW_EXCLUSIVE = "ROW EXCLUSIVE"
SHARE_UPDATE_EXCLUSIVE = "SHARE UPDATE EXCLUSIVE"
SHARE = "SHARE"
SHARE_ROW_EXCLUSIVE = "SHARE ROW EXCLUSIVE"
EXCLUSIVE = "EXCLUSIVE"
ACCESS_EXCLUSIVE = "ACCESS EXCLUSIVE"

LOCKS = (
    ACCESS_SHARE,
    ROW_SHARE,
    ROW_EXCLUSIVE,
    SHARE_UPDATE_EXCLUSIVE,
    SHARE,
    SHARE_ROW_EXCLUSIVE,
    EXCLUSIVE,
    ACCESS_EXCLUSIVE,
)
_BLOCKS: Mapping[str, Blocks] = {
    ACCESS_SHARE: Blocks.DDL,
    ROW_SHARE: Blocks.DDL,
    ROW_EXCLUSIVE: Blocks.DDL,
    SHARE_UPDATE_EXCLUSIVE: Blocks.DDL,
    SHARE: Blocks.WRITES,
    SHARE_ROW_EXCLUSIVE: Blocks.WRITES,
    EXCLUSIVE: Blocks.WRITES,
    ACCESS_EXCLUSIVE: Blocks.READS_AND_WRITES,
}


def blocks(lock: Optional[str]) -> Blocks:
    """What a Postgres table lock blocks; an unnamed lock blocks nothing."""
    if lock is None:
        return Blocks.NOTHING
    return _BLOCKS[lock]


def lock_rank(lock: Optional[str]) -> int:
    """A lock's strength: its place in `LOCKS`, or -1 for no lock."""
    return -1 if lock is None else LOCKS.index(lock)


def timeout_statement(transactional: bool) -> str:
    """The statement that bounds how long the next locks may queue."""
    scope = "LOCAL " if transactional else ""
    return f"SET {scope}lock_timeout = '5s'"


# --- the rules ---------------------------------------------------------

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
    _DOCS + "sql-createindex.html",
    ("CREATE INDEX ix2 ON t (c)", "CREATE UNIQUE INDEX ix2 ON t (c)"),
)
CREATE_INDEX_CONCURRENTLY = Rule(
    "pg.create_index.concurrently",
    _DOCS + "sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY",
    ("CREATE INDEX CONCURRENTLY ix2 ON t (c)",),
)
DROP_INDEX = Rule("pg.drop_index", _DOCS + "sql-dropindex.html", ("DROP INDEX ix",))
DROP_INDEX_CONCURRENTLY = Rule(
    "pg.drop_index.concurrently",
    _DOCS + "sql-dropindex.html",
    ("DROP INDEX CONCURRENTLY ix",),
)
CREATE_TABLE = Rule(
    "pg.create_table",
    _DOCS + "sql-createtable.html",
    (
        "CREATE TABLE n (id integer, r_id integer REFERENCES r (id))",
        "CREATE TABLE pt2 PARTITION OF pt FOR VALUES IN (3)",
    ),
)
DROP_TABLE = Rule(
    "pg.drop_table",
    _DOCS + "sql-droptable.html",
    ("DROP TABLE t", "TRUNCATE t", "TRUNCATE r CASCADE"),
)
WRITE_ROWS = Rule(
    "pg.write_rows",
    _DOCS + "sql-update.html",
    ("UPDATE t SET c = 0 WHERE c IS NULL", "DELETE FROM t WHERE c < 0"),
)
INSERT_ROWS = Rule(
    "pg.insert", _DOCS + "sql-insert.html", ("INSERT INTO t (id, c) VALUES (100, 100)",)
)
REINDEX = Rule("pg.reindex", _DOCS + "sql-reindex.html", ("REINDEX TABLE t",))
REINDEX_CONCURRENTLY = Rule(
    "pg.reindex.concurrently",
    _DOCS + "sql-reindex.html#SQL-REINDEX-CONCURRENTLY",
    ("REINDEX TABLE CONCURRENTLY t",),
)
VACUUM = Rule("pg.vacuum", _DOCS + "sql-vacuum.html", ("VACUUM t", "ANALYZE t"))
VACUUM_FULL = Rule(
    "pg.vacuum_full",
    _DOCS + "sql-vacuum.html",
    ("VACUUM FULL t", "CLUSTER t USING t_pkey"),
)
REFRESH = Rule(
    "pg.refresh_materialized_view",
    _DOCS + "sql-refreshmaterializedview.html",
    ("REFRESH MATERIALIZED VIEW mv",),
)
REFRESH_CONCURRENTLY = Rule(
    "pg.refresh_materialized_view.concurrently",
    _DOCS + "sql-refreshmaterializedview.html",
    ("REFRESH MATERIALIZED VIEW CONCURRENTLY mv",),
)
TRIGGER = Rule(
    "pg.trigger",
    _DOCS + "sql-createtrigger.html",
    (
        "CREATE TRIGGER tr2 BEFORE UPDATE ON t FOR EACH ROW EXECUTE FUNCTION f()",
        "DROP TRIGGER tr ON t",
    ),
)
COMMENT = Rule(
    "pg.comment", _DOCS + "sql-comment.html", ("COMMENT ON COLUMN t.c IS 'note'",)
)
DROP_VIEW = Rule("pg.drop_view", _DOCS + "sql-dropview.html", ("DROP VIEW v",))
LOCK_TABLE = Rule(
    "pg.lock_table",
    _DOCS + "sql-lock.html",
    ("LOCK TABLE t IN SHARE MODE", "LOCK TABLE t IN ACCESS EXCLUSIVE MODE NOWAIT"),
)
DROP_SCHEMA = Rule(
    "pg.drop_schema", _DOCS + "sql-dropschema.html", ("DROP SCHEMA s CASCADE",)
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

# --- remedies ----------------------------------------------------------

_SIMPLE_NAME_RE = re.compile(r"[a-z_][a-z0-9_$]*")
_TRANSACTION_NOTE = "in a migration with transactional=False"


def _ident(name: str) -> str:
    """A dotted name as Postgres reads it, quoting the parts that need it."""
    return ".".join(
        part if _SIMPLE_NAME_RE.fullmatch(part) else '"' + part.replace('"', '""') + '"'
        for part in name.split(".")
    )


def _insert_after(statement: str, word: str, text: str) -> Optional[str]:
    """The statement with `text` inserted after its first bare `word`."""
    for token in _tokens(statement):
        if token.is_word(word):
            end = token.start + len(token.text)
            return statement[:end] + " " + text + statement[end:]
    return None


def _tokens(statement: str) -> List[Token]:
    from sustained.dialects import Dialects

    return tokenize(statement, Dialects.POSTGRES)


def _trimmed(statement: str) -> str:
    return statement.strip().rstrip(";").rstrip()


def _key_columns(statement: str) -> Optional[str]:
    """The column list of the first PRIMARY KEY or UNIQUE in a statement."""
    tokens = _tokens(statement)
    for index, token in enumerate(tokens):
        if not token.is_word("KEY", "UNIQUE"):
            continue
        if token.value == "KEY" and not (
            index and tokens[index - 1].is_word("PRIMARY")
        ):
            continue
        rest = tokens[index + 1 :]
        if not rest or rest[0].kind != PUNCT or rest[0].text != "(":
            continue
        depth = 0
        for closing in rest:
            if closing.kind == PUNCT and closing.text == "(":
                depth += 1
            elif closing.kind == PUNCT and closing.text == ")":
                depth -= 1
                if depth == 0:
                    return statement[rest[0].start : closing.start + 1]
    return None


def _last(name: str) -> str:
    return name.rsplit(".", 1)[-1]


# --- statement handlers ------------------------------------------------


def _table_label(index: str, table: Optional[str]) -> str:
    return table if table else f"(table of index {index})"


def _create_index(facts: Facts) -> Outcome:
    parsed = facts.parsed
    table = parsed.table or "(unnamed table)"
    if parsed.options.get("concurrently"):
        return Outcome(
            (
                Effect(
                    CREATE_INDEX_CONCURRENTLY,
                    table,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.INDEX_BUILD,
                ),
            ),
            (
                Finding(
                    CREATE_INDEX_CONCURRENTLY.id,
                    Severity.INFO,
                    "the build scans the table twice and waits for every "
                    "older transaction; a failed build leaves an invalid "
                    "index behind, which must be dropped before a retry",
                    source=CREATE_INDEX_CONCURRENTLY.source,
                ),
            ),
        )
    concurrent = _insert_after(facts.statement, "INDEX", "CONCURRENTLY")
    remedy = (_trimmed(concurrent),) if concurrent else ()
    return Outcome(
        (
            Effect(
                CREATE_INDEX,
                table,
                SHARE,
                Work.INDEX_BUILD,
                message=f"writes to {table} wait for the whole index build; "
                f"build it CONCURRENTLY {_TRANSACTION_NOTE}",
                remedy=remedy,
            ),
        )
    )


def _drop_index(facts: Facts) -> Outcome:
    options = facts.parsed.options
    names = [str(n) for n in facts.parsed.items("names")]
    concurrently = bool(options.get("concurrently"))
    effects = []
    for name in names:
        table = facts.state.index_table(name) or facts.context.index_table(name)
        if table is None and facts.intent is not None and len(names) == 1:
            table = facts.intent.table
        label = _table_label(name, table)
        if concurrently:
            effects.append(
                Effect(
                    DROP_INDEX_CONCURRENTLY,
                    label,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.CATALOG,
                )
            )
            continue
        concurrent = (
            _insert_after(facts.statement, "INDEX", "CONCURRENTLY")
            if len(names) == 1
            else None
        )
        effects.append(
            Effect(
                DROP_INDEX,
                label,
                ACCESS_EXCLUSIVE,
                Work.CATALOG,
                message=f"reads and writes on {label} wait until the drop commits; "
                f"drop it CONCURRENTLY {_TRANSACTION_NOTE}",
                remedy=(_trimmed(concurrent),) if concurrent else (),
            )
        )
    return Outcome(tuple(effects))


def _create_table(facts: Facts) -> Outcome:
    options = facts.parsed.options
    effects = [
        Effect(
            CREATE_TABLE,
            str(referenced),
            SHARE_ROW_EXCLUSIVE,
            Work.CATALOG,
            message=f"writes to {referenced} wait while the new table's "
            "foreign key is created",
        )
        for referenced in facts.parsed.items("references")
    ]
    parent = options.get("partition_of")
    if parent:
        effects.append(
            Effect(
                CREATE_TABLE,
                str(parent),
                ACCESS_EXCLUSIVE,
                Work.CATALOG,
                message=f"reads and writes on {parent} wait while the partition "
                "is created",
            )
        )
    return Outcome(tuple(effects))


def _tables(facts: Facts) -> List[str]:
    tables = facts.parsed.items("tables")
    if tables:
        return [str(t) for t in tables]
    return [facts.parsed.table] if facts.parsed.table else []


def _drop_table(facts: Facts) -> Outcome:
    """
    DROP TABLE and TRUNCATE lock each named table. A dropped table's
    foreign keys go with it, and so does the lock on each table they
    point at. With CASCADE, DROP also drops the keys that point at the
    table, and TRUNCATE also empties the tables those keys belong to,
    and the tables that point at those in turn.
    """
    named = _tables(facts)
    effects = [
        Effect(DROP_TABLE, table, ACCESS_EXCLUSIVE, Work.CATALOG) for table in named
    ]
    seen = {table.lower() for table in named}
    context = facts.context
    cascade = bool(facts.parsed.options.get("cascade"))
    if facts.parsed.kind == "truncate":
        pending = list(named) if cascade else []
        while pending:
            table = pending.pop(0)
            for other in context.referenced_by(facts.state.original(table)):
                if other.lower() in seen:
                    continue
                seen.add(other.lower())
                pending.append(other)
                effects.append(
                    Effect(
                        DROP_TABLE,
                        other,
                        ACCESS_EXCLUSIVE,
                        Work.CATALOG,
                        message=f"TRUNCATE ... CASCADE also empties {other}, whose "
                        f"foreign key points at {table}; reads and writes on "
                        f"{other} wait until it commits",
                    )
                )
        return Outcome(tuple(effects))
    for table in named:
        live = facts.state.original(table)
        for other in context.references(live):
            if other.lower() not in seen:
                seen.add(other.lower())
                effects.append(_foreign_key_effect(table, other, "dropped"))
        if cascade:
            for other in context.referenced_by(live):
                if other.lower() not in seen:
                    seen.add(other.lower())
                    effects.append(_foreign_key_effect(other, table, "dropped", other))
    return Outcome(tuple(effects))


def _foreign_key_effect(
    source: str,
    target: str,
    change: str,
    locked: Optional[str] = None,
    work: Work = Work.CATALOG,
    confidence: Confidence = Confidence.KNOWN,
) -> Effect:
    """
    The lock on one end of a foreign key from `source` to `target` that
    the statement drops or re-creates: on `target` unless `locked` names
    the other end. Postgres takes ACCESS EXCLUSIVE on both tables of a
    key it drops, to remove the key's triggers from each.
    """
    table = locked or target
    return Effect(
        DROP_FOREIGN_KEY,
        table,
        ACCESS_EXCLUSIVE,
        work,
        confidence,
        message=f"the foreign key from {source} to {target} is {change}, which "
        f"locks {table} ACCESS EXCLUSIVE: reads and writes on {table} wait until "
        "the statement commits",
    )


def _drop_view(facts: Facts) -> Outcome:
    return Outcome(
        tuple(
            Effect(DROP_VIEW, table, ACCESS_EXCLUSIVE, Work.CATALOG)
            for table in _tables(facts)
        )
    )


def _write_rows(facts: Facts) -> Outcome:
    table = facts.parsed.table or "(unnamed table)"
    verb = facts.parsed.kind.upper()
    if facts.intent is not None and facts.intent.kind == "backfill":
        what = "the backfill"
    else:
        what = f"the {verb}"
    until = "the migration commits" if facts.transactional else "it ends"
    message = (
        f"writes to the rows {what} changes on {table} wait until {until}; on a "
        "large table, backfill in batches outside the DDL migration"
    )
    return Outcome(
        (
            Effect(
                WRITE_ROWS,
                table,
                ROW_EXCLUSIVE,
                Work.ROWS,
                message=message,
                blocks=Blocks.WRITES,
            ),
        )
    )


def _insert(facts: Facts) -> Outcome:
    table = facts.parsed.table or "(unnamed table)"
    return Outcome((Effect(INSERT_ROWS, table, ROW_EXCLUSIVE, Work.ROWS),))


def _reindex(facts: Facts) -> Outcome:
    options = facts.parsed.options
    target = str(options.get("target"))
    name = str(options.get("name"))
    if target == "table":
        label = name
    elif target == "index":
        table = facts.state.index_table(name) or facts.context.index_table(name)
        label = _table_label(name, table)
    else:
        label = f"(every table in {target} {name})"
    if options.get("concurrently"):
        return Outcome(
            (
                Effect(
                    REINDEX_CONCURRENTLY,
                    label,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.INDEX_BUILD,
                ),
            )
        )
    concurrent = _insert_after(facts.statement, target.upper(), "CONCURRENTLY")
    return Outcome(
        (
            Effect(
                REINDEX,
                label,
                SHARE,
                Work.INDEX_BUILD,
                message=f"writes to {label} wait for the rebuild, and so do reads "
                "that would use an index being rebuilt, which is locked ACCESS "
                f"EXCLUSIVE; rebuild it CONCURRENTLY {_TRANSACTION_NOTE}",
                remedy=(_trimmed(concurrent),) if concurrent else (),
                blocks=Blocks.READS_AND_WRITES,
            ),
        )
    )


def _vacuum(facts: Facts) -> Outcome:
    tables = _tables(facts)
    if not tables:
        return Outcome(
            findings=(
                Finding(
                    VACUUM.id,
                    Severity.INFO,
                    f"{facts.parsed.kind.upper()} with no table reads every table "
                    "in the database",
                    source=VACUUM.source,
                ),
            ),
            confidence=Confidence.LIKELY,
        )
    if facts.parsed.options.get("full"):
        return Outcome(
            tuple(
                Effect(VACUUM_FULL, table, ACCESS_EXCLUSIVE, Work.REWRITE)
                for table in tables
            )
        )
    return Outcome(
        tuple(
            Effect(VACUUM, table, SHARE_UPDATE_EXCLUSIVE, Work.SCAN) for table in tables
        )
    )


def _cluster(facts: Facts) -> Outcome:
    if facts.parsed.table is None:
        return _vacuum(facts)
    return Outcome(
        (Effect(VACUUM_FULL, facts.parsed.table, ACCESS_EXCLUSIVE, Work.REWRITE),)
    )


def _refresh(facts: Facts) -> Outcome:
    view = facts.parsed.table or "(unnamed view)"
    options = facts.parsed.options
    work = Work.REWRITE if options.get("with_data", True) else Work.CATALOG
    if options.get("concurrently"):
        # The query runs in full, and only the rows that differ are
        # written into the view, whose file stays in place.
        return Outcome((Effect(REFRESH_CONCURRENTLY, view, EXCLUSIVE, Work.ROWS),))
    concurrent = _insert_after(facts.statement, "VIEW", "CONCURRENTLY")
    return Outcome(
        (
            Effect(
                REFRESH,
                view,
                ACCESS_EXCLUSIVE,
                work,
                message=f"reads of {view} wait for the whole refresh; refresh it "
                "CONCURRENTLY, which needs a unique index on the view",
                remedy=(_trimmed(concurrent),) if concurrent else (),
            ),
        )
    )


def _trigger(facts: Facts) -> Outcome:
    table = facts.parsed.table or "(unnamed table)"
    lock = (
        SHARE_ROW_EXCLUSIVE
        if facts.parsed.kind == "create_trigger"
        else ACCESS_EXCLUSIVE
    )
    return Outcome((Effect(TRIGGER, table, lock, Work.CATALOG),))


def _comment(facts: Facts) -> Outcome:
    if facts.parsed.options.get("object") not in ("table", "column"):
        return Outcome()
    table = facts.parsed.table or "(unnamed table)"
    return Outcome((Effect(COMMENT, table, SHARE_UPDATE_EXCLUSIVE, Work.CATALOG),))


def _lock_table(facts: Facts) -> Outcome:
    options = facts.parsed.options
    mode = str(options.get("mode"))
    waits = not options.get("nowait")
    return Outcome(
        tuple(
            Effect(LOCK_TABLE, table, mode, Work.CATALOG, waits=waits)
            for table in _tables(facts)
        )
    )


def _drop_object(facts: Facts) -> Outcome:
    if facts.parsed.options.get("object") != "schema":
        return Outcome()
    return Outcome(
        findings=(
            Finding(
                DROP_SCHEMA.id,
                Severity.INFO,
                "dropping a schema locks every table in it ACCESS EXCLUSIVE; "
                "the tables are not named in the statement",
                source=DROP_SCHEMA.source,
            ),
        ),
        confidence=Confidence.LIKELY,
    )


def _no_table(facts: Facts) -> Outcome:
    return Outcome()


# --- ALTER TABLE -------------------------------------------------------


def _alter_table(facts: Facts) -> Outcome:
    effects: List[Effect] = []
    findings: List[Finding] = []
    confidence = Confidence.KNOWN
    for action in facts.parsed.actions:
        handler = _ACTIONS.get(action.kind)
        if handler is None:
            return _unknown_action(facts, action)
        outcome = handler(facts, action)
        effects.extend(outcome.effects)
        findings.extend(outcome.findings)
        confidence = min(confidence, outcome.confidence)
    if len(facts.parsed.actions) > 1:
        # A remedy rewrites one action, so it cannot stand for a
        # statement that holds others.
        effects = [e._replace(remedy=()) for e in effects]
    return Outcome(tuple(effects), tuple(findings), confidence)


def _unknown_action(facts: Facts, action: Action) -> Outcome:
    return Outcome(
        findings=(
            Finding(
                "impact.unknown",
                Severity.INFO,
                f"no PostgreSQL rule reads the ALTER TABLE action {action.kind}",
            ),
        ),
        confidence=Confidence.UNKNOWN,
    )


def _table(facts: Facts) -> str:
    return facts.parsed.table or "(unnamed table)"


def _simple(rule: Rule, lock: str, work: Work) -> "_ActionHandler":
    def handler(facts: Facts, action: Action) -> Outcome:
        return Outcome((Effect(rule, _table(facts), lock, work),))

    return handler


def _add_column(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    options = action.options
    column = action.column or "?"
    volatility = options.get("default_volatility")
    rewrite_reason: Optional[str] = None
    confidence = Confidence.KNOWN
    if options.get("serial"):
        rewrite_reason = "a serial column fills every row from its sequence"
    elif options.get("identity"):
        rewrite_reason = "an identity column fills every row from its sequence"
    elif options.get("generated") == "stored":
        rewrite_reason = "a stored generated column is computed for every row"
    elif volatility == "volatile":
        function = options.get("default_function")
        rewrite_reason = (
            f"the default calls {function}(), which gives each row a new value"
        )
        if not options.get("default_certain", True):
            confidence = Confidence.LIKELY
            rewrite_reason = (
                f"the default calls {function}(), which no rule knows, so it "
                "counts as volatile: a new value for each row"
            )
    effects: List[Effect] = []
    if rewrite_reason is not None:
        remedy: Tuple[str, ...] = ()
        default = options.get("default")
        if volatility == "volatile" and default:
            remedy = (
                f"ALTER TABLE {_ident(table)} ADD COLUMN {_ident(column)} "
                f"{options.get('type')}",
                f"ALTER TABLE {_ident(table)} ALTER COLUMN {_ident(column)} "
                f"SET DEFAULT {default}",
            )
        effects.append(
            Effect(
                ADD_COLUMN_REWRITE,
                table,
                ACCESS_EXCLUSIVE,
                Work.REWRITE,
                confidence,
                message=f"{rewrite_reason}, so the table is rewritten while reads "
                "and writes wait"
                + (
                    "; add the column without a default, set the default, then "
                    "backfill in batches"
                    if remedy
                    else ""
                ),
                remedy=remedy,
            )
        )
    elif options.get("primary_key") or options.get("unique"):
        effects.append(
            Effect(ADD_COLUMN_KEY, table, ACCESS_EXCLUSIVE, Work.INDEX_BUILD)
        )
    elif options.get("check") or options.get("references"):
        effects.append(Effect(ADD_COLUMN_CHECKED, table, ACCESS_EXCLUSIVE, Work.SCAN))
    else:
        effects.append(Effect(ADD_COLUMN, table, ACCESS_EXCLUSIVE, Work.CATALOG))
    referenced = options.get("references")
    if referenced:
        effects.append(
            Effect(
                ADD_COLUMN_CHECKED, str(referenced), SHARE_ROW_EXCLUSIVE, Work.CATALOG
            )
        )
    return Outcome(tuple(effects), confidence=confidence)


def _drop_column(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    live = facts.state.original(table)
    column = [action.column] if action.column else []
    effects = [Effect(DROP_COLUMN, table, ACCESS_EXCLUSIVE, Work.CATALOG)]
    for other in facts.context.references(live, column):
        effects.append(_foreign_key_effect(table, other, "dropped with the column"))
    if action.options.get("cascade"):
        for other in facts.context.referenced_by(live, column):
            effects.append(
                _foreign_key_effect(other, table, "dropped with the column", other)
            )
    return Outcome(tuple(effects))


def _drop_constraint(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    live = facts.state.original(table)
    effects = [Effect(DROP_CONSTRAINT, table, ACCESS_EXCLUSIVE, Work.CATALOG)]
    name = action.options.get("name")
    if not name:
        return Outcome(tuple(effects))
    context = facts.context
    target = context.foreign_key_target(live, str(name))
    if target is not None:
        if target.lower() != table.lower():
            effects.append(_foreign_key_effect(table, target, "dropped"))
    elif action.options.get("cascade"):
        columns = _constraint_columns(context, live, str(name))
        if columns:
            for other in context.referenced_by(live, columns):
                effects.append(_foreign_key_effect(other, table, "dropped", other))
    return Outcome(tuple(effects))


def _constraint_columns(
    context: EngineContext, table: str, name: str
) -> Tuple[str, ...]:
    """
    The columns of a unique or primary key constraint, from the schema
    read. The read keeps the columns of a primary key but not its name,
    so a named constraint that is neither a unique constraint nor a
    check is taken for the primary key.
    """
    found = context.table(table)
    if found is None:
        return ()
    index = found.indexes.get(name.lower())
    if index is not None:
        return index.columns if index.constraint else ()
    if name.lower() in found.check_names or name.lower() in found.checks:
        return ()
    return found.primary_key


def _set_not_null(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    column = action.column or "?"
    check = f"{_last(table)}_{column}_not_null"[:63]
    t, c, k = _ident(table), _ident(column), _ident(check)
    remedy = (
        f"ALTER TABLE {t} ADD CONSTRAINT {k} CHECK ({c} IS NOT NULL) NOT VALID",
        f"ALTER TABLE {t} VALIDATE CONSTRAINT {k}",
        f"ALTER TABLE {t} ALTER COLUMN {c} SET NOT NULL",
        f"ALTER TABLE {t} DROP CONSTRAINT {k}",
    )
    return Outcome(
        (
            Effect(
                SET_NOT_NULL,
                table,
                ACCESS_EXCLUSIVE,
                Work.SCAN,
                Confidence.LIKELY,
                message=f"reads and writes on {table} wait while every row is "
                f"checked for NULL, unless a valid CHECK ({column} IS NOT NULL) "
                "already proves it; add that check NOT VALID, validate it, then "
                "SET NOT NULL skips the scan",
                remedy=remedy,
            ),
        ),
        confidence=Confidence.LIKELY,
    )


def _add_constraint(facts: Facts, action: Action) -> Outcome:
    constraint = action.options.get("constraint")
    if constraint == "check":
        return _add_check(facts, action)
    if constraint == "foreign_key":
        return _add_foreign_key(facts, action)
    if constraint in ("primary_key", "unique"):
        return _add_key(facts, action)
    return Outcome(
        (Effect(ADD_EXCLUSION, _table(facts), ACCESS_EXCLUSIVE, Work.INDEX_BUILD),)
    )


def _validate_later(facts: Facts, action: Action) -> Tuple[str, ...]:
    name = action.options.get("name")
    if not name:
        return ()
    return (
        _trimmed(facts.statement) + " NOT VALID",
        f"ALTER TABLE {_ident(_table(facts))} VALIDATE CONSTRAINT {_ident(str(name))}",
    )


def _add_check(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    if action.options.get("not_valid"):
        return Outcome(
            (Effect(ADD_CHECK_NOT_VALID, table, ACCESS_EXCLUSIVE, Work.CATALOG),)
        )
    return Outcome(
        (
            Effect(
                ADD_CHECK,
                table,
                ACCESS_EXCLUSIVE,
                Work.SCAN,
                message=f"reads and writes on {table} wait while every row is "
                "checked; add the check NOT VALID, then VALIDATE CONSTRAINT in a "
                "later migration, which lets reads and writes go on",
                remedy=_validate_later(facts, action),
            ),
        )
    )


def _add_foreign_key(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    referenced = str(action.options.get("references"))
    if action.options.get("not_valid"):
        return Outcome(
            tuple(
                Effect(
                    ADD_FOREIGN_KEY_NOT_VALID, name, SHARE_ROW_EXCLUSIVE, Work.CATALOG
                )
                for name in (table, referenced)
            )
        )
    return Outcome(
        (
            Effect(
                ADD_FOREIGN_KEY,
                table,
                SHARE_ROW_EXCLUSIVE,
                Work.SCAN,
                message=f"writes to {table} and {referenced} wait while every row "
                f"of {table} is checked; add the key NOT VALID, then VALIDATE "
                "CONSTRAINT in a later migration",
                remedy=_validate_later(facts, action),
            ),
            Effect(ADD_FOREIGN_KEY, referenced, SHARE_ROW_EXCLUSIVE, Work.CATALOG),
        )
    )


def _add_key(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    options = action.options
    if options.get("using_index"):
        return Outcome(
            (Effect(ADD_KEY_USING_INDEX, table, ACCESS_EXCLUSIVE, Work.CATALOG),)
        )
    primary = options.get("constraint") == "primary_key"
    columns = _key_columns(facts.statement)
    remedy: Tuple[str, ...] = ()
    if columns is not None:
        name = str(
            options.get("name") or f"{_last(table)}_{'pkey' if primary else 'key'}"
        )
        index = f"{name}_idx"[:63]
        kind = "PRIMARY KEY" if primary else "UNIQUE"
        remedy = (
            f"CREATE UNIQUE INDEX CONCURRENTLY {_ident(index)} ON {_ident(table)} "
            f"{columns}",
            f"ALTER TABLE {_ident(table)} ADD CONSTRAINT {_ident(name)} {kind} "
            f"USING INDEX {_ident(index)}",
        )
    return Outcome(
        (
            Effect(
                ADD_KEY,
                table,
                ACCESS_EXCLUSIVE,
                Work.INDEX_BUILD,
                message=f"reads and writes on {table} wait for the whole index "
                f"build; build a unique index CONCURRENTLY {_TRANSACTION_NOTE}, "
                "then add the constraint USING INDEX",
                remedy=remedy,
            ),
        )
    )


def _rename(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    if action.kind == "rename_constraint":
        return Outcome((Effect(RENAME, table, ACCESS_EXCLUSIVE, Work.CATALOG),))
    what = "column" if action.kind == "rename_column" else "table"
    old = action.column if action.kind == "rename_column" else table
    note = Finding(
        RENAME.id,
        Severity.INFO,
        f"running application code that names the {what} {old} fails once "
        "the rename commits",
        source=RENAME.source,
    )
    return Outcome(
        (Effect(RENAME, table, ACCESS_EXCLUSIVE, Work.CATALOG, notes=(note,)),)
    )


def _attach_partition(facts: Facts, action: Action) -> Outcome:
    partition = str(action.options.get("partition"))
    return Outcome(
        (
            Effect(
                ATTACH_PARTITION, _table(facts), SHARE_UPDATE_EXCLUSIVE, Work.CATALOG
            ),
            Effect(
                ATTACH_PARTITION,
                partition,
                ACCESS_EXCLUSIVE,
                Work.SCAN,
                Confidence.LIKELY,
                message=f"reads and writes on {partition} wait while every row is "
                "checked against the partition bound, unless a valid CHECK "
                "constraint already proves it; add one NOT VALID and validate "
                "it first",
            ),
        ),
        confidence=Confidence.LIKELY,
    )


def _detach_partition(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    partition = str(action.options.get("partition"))
    if action.options.get("concurrently"):
        findings: Tuple[Finding, ...] = ()
        if not DETACH_PARTITION_CONCURRENTLY.versions(facts.context.version):
            findings = (
                _needs_version(facts.context, DETACH_PARTITION_CONCURRENTLY, (14,)),
            )
        return Outcome(
            (
                Effect(
                    DETACH_PARTITION_CONCURRENTLY,
                    table,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.CATALOG,
                ),
                Effect(
                    DETACH_PARTITION_CONCURRENTLY,
                    partition,
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.CATALOG,
                ),
            ),
            findings,
        )
    return Outcome(
        (
            Effect(DETACH_PARTITION, table, ACCESS_EXCLUSIVE, Work.CATALOG),
            Effect(DETACH_PARTITION, partition, ACCESS_EXCLUSIVE, Work.CATALOG),
        )
    )


def _needs_version(
    context: EngineContext, rule: Rule, version: Tuple[int, ...]
) -> Finding:
    from sustained.impact.context import version_text

    assumed = "" if "version" in context.read else "assumed "
    return Finding(
        rule.id,
        Severity.WARN,
        f"this needs PostgreSQL {version_text(version)} or later; the {assumed}"
        f"server version is {version_text(context.version)}",
        source=rule.source,
    )


# The storage parameters Postgres changes under SHARE UPDATE EXCLUSIVE;
# any other parameter takes ACCESS EXCLUSIVE.
_LIGHT_PARAMETERS_RE = re.compile(
    r"(TOAST\.)?(AUTOVACUUM_.*|FILLFACTOR|TOAST_TUPLE_TARGET|PARALLEL_WORKERS"
    r"|LOG_AUTOVACUUM_MIN_DURATION|VACUUM_TRUNCATE|VACUUM_INDEX_CLEANUP)"
)


def _set_parameters(facts: Facts, action: Action) -> Outcome:
    light = all(_LIGHT_PARAMETERS_RE.fullmatch(key.upper()) for key in action.options)
    if light:
        return Outcome(
            (
                Effect(
                    TABLE_PARAMETERS,
                    _table(facts),
                    SHARE_UPDATE_EXCLUSIVE,
                    Work.CATALOG,
                ),
            )
        )
    return Outcome(
        (Effect(TABLE_CATALOG, _table(facts), ACCESS_EXCLUSIVE, Work.CATALOG),)
    )


# --- column type changes -----------------------------------------------

_TYPE_ALIASES: Mapping[str, str] = {
    "character varying": "varchar",
    "char varying": "varchar",
    "character": "char",
    "bpchar": "char",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
    "decimal": "numeric",
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "int2": "smallint",
    "bool": "boolean",
    "float8": "double precision",
    "float": "double precision",
    "float4": "real",
}
# Types whose conversions no rule treats as binary coercible unless
# listed below, so a change between two of them is a known rewrite.
_BUILTIN_TYPES = frozenset(
    {
        "smallint",
        "integer",
        "bigint",
        "numeric",
        "real",
        "double precision",
        "text",
        "varchar",
        "char",
        "boolean",
        "date",
        "time",
        "timetz",
        "timestamp",
        "timestamptz",
        "interval",
        "uuid",
        "json",
        "jsonb",
        "bytea",
    }
)
_TYPE_ARGS_RE = re.compile(r"\(([^)]*)\)")


def _pg_type(text: str) -> Tuple[str, Optional[Tuple[int, ...]], bool]:
    """
    A type's base name, its numeric arguments, and whether it is an
    array. The arguments are None when they are not all numbers.
    """
    lowered = " ".join(text.lower().replace('"', "").split())
    array = lowered.endswith("[]")
    lowered = lowered.rstrip("[] ")
    match = _TYPE_ARGS_RE.search(lowered)
    args: Optional[Tuple[int, ...]] = ()
    if match:
        try:
            args = tuple(int(a) for a in match.group(1).split(","))
        except ValueError:
            args = None
        lowered = " ".join((lowered[: match.start()] + lowered[match.end() :]).split())
    if lowered.startswith("pg_catalog."):
        lowered = lowered[len("pg_catalog.") :]
    return _TYPE_ALIASES.get(lowered, lowered), args, array


def type_change(
    from_type: str, to_type: str, settings: Mapping[str, str]
) -> Tuple[Work, Confidence, str]:
    """
    The work a column type change does: `catalog` for a binary-coercible
    change, `rewrite` otherwise, with how sure the answer is and why.
    """
    old, new = _pg_type(from_type), _pg_type(to_type)
    if old == new:
        return Work.CATALOG, Confidence.KNOWN, "the type does not change"
    (old_base, old_args, old_array), (new_base, new_args, new_array) = old, new
    readable = old_args is not None and new_args is not None
    if old_array == new_array and old_args is not None and new_args is not None:
        verdict = _coercible(old_base, old_args, new_base, new_args, settings)
        if verdict is not None:
            return verdict
    known = readable and old_base in _BUILTIN_TYPES and new_base in _BUILTIN_TYPES
    return (
        Work.REWRITE,
        Confidence.KNOWN if known else Confidence.LIKELY,
        f"{from_type} to {to_type} is not binary coercible"
        + ("" if known else " as far as the rules know"),
    )


def _coercible(
    old: str,
    old_args: Tuple[int, ...],
    new: str,
    new_args: Tuple[int, ...],
    settings: Mapping[str, str],
) -> Optional[Tuple[Work, Confidence, str]]:
    widened = "a widening change is binary coercible"
    if old in ("varchar", "text") and new == "text":
        return Work.CATALOG, Confidence.KNOWN, widened
    if old in ("varchar", "text") and new == "varchar" and not new_args:
        return Work.CATALOG, Confidence.KNOWN, widened
    if old == new == "varchar" and old_args and new_args[0] >= old_args[0]:
        return Work.CATALOG, Confidence.KNOWN, widened
    if old == new == "numeric" and old_args and not new_args:
        return Work.CATALOG, Confidence.KNOWN, widened
    if old == new == "numeric" and len(old_args) == len(new_args) and old_args:
        same_scale = old_args[1:] == new_args[1:]
        if same_scale and new_args[0] >= old_args[0]:
            return Work.CATALOG, Confidence.KNOWN, widened
    if old == "timestamp" and new == "timestamptz" and old_args == new_args:
        zone = settings.get("TimeZone")
        if zone is None:
            return (
                Work.REWRITE,
                Confidence.LIKELY,
                "timestamp to timestamptz only changes the catalog when the "
                "TimeZone setting is UTC, and the setting was not read",
            )
        if zone.upper() in ("UTC", "ETC/UTC", "GMT", "Z"):
            return Work.CATALOG, Confidence.KNOWN, "TimeZone is UTC"
    return None


_TRIVIAL_USING_RE = re.compile(r'\s*"?(?P<column>[^"\s:]+)"?\s*(::.*)?', re.DOTALL)


def _alter_column_type(facts: Facts, action: Action) -> Outcome:
    outcome = _column_type(facts, action)
    keys = _type_keys(facts, action)
    if not keys:
        return outcome
    confidence = min([outcome.confidence] + [e.confidence for e in keys])
    return outcome._replace(
        effects=outcome.effects + tuple(keys), confidence=confidence
    )


def _column_type(facts: Facts, action: Action) -> Outcome:
    table = _table(facts)
    column = action.column or "?"
    to_type = str(action.options.get("type"))
    using = action.options.get("using")
    intent = facts.intent
    from_type: Optional[str] = None
    if intent is not None and intent.get("from_type"):
        from_type = str(intent.get("from_type"))
    else:
        from_type = facts.context.column_type(facts.state.original(table), column)
    if using:
        match = _TRIVIAL_USING_RE.fullmatch(str(using))
        if not match or match.group("column").lower() != column.lower():
            return _type_rewrite(
                table,
                column,
                Confidence.KNOWN,
                "the USING clause computes a new value for every row",
            )
    if from_type is None:
        return _type_rewrite(
            table,
            column,
            Confidence.LIKELY,
            "the column's current type is not known; a binary-coercible change, "
            "such as widening a varchar, would only change the catalog",
        )
    work, confidence, reason = type_change(from_type, to_type, facts.context.settings)
    if work is Work.CATALOG:
        return Outcome(
            (Effect(ALTER_TYPE_COERCIBLE, table, ACCESS_EXCLUSIVE, Work.CATALOG),)
        )
    return _type_rewrite(table, column, confidence, reason)


def _type_keys(facts: Facts, action: Action) -> List[Effect]:
    """
    A column type change re-creates each foreign key that uses the
    column or points at it. The table at the key's other end is locked,
    and when that table holds the key, its rows are checked again
    unless the old and new types compare the same way.
    """
    table = _table(facts)
    live = facts.state.original(table)
    column = [action.column] if action.column else []
    effects = [
        _foreign_key_effect(table, other, "re-created")
        for other in facts.context.references(live, column)
    ]
    effects.extend(
        _foreign_key_effect(
            other, table, "re-created", other, Work.SCAN, Confidence.LIKELY
        )
        for other in facts.context.referenced_by(live, column)
    )
    return effects


def _type_rewrite(
    table: str, column: str, confidence: Confidence, reason: str
) -> Outcome:
    return Outcome(
        (
            Effect(
                ALTER_TYPE,
                table,
                ACCESS_EXCLUSIVE,
                Work.REWRITE,
                confidence,
                message=f"{reason}, so {table} and its indexes are rewritten while "
                "reads and writes wait; the online route takes four steps: add a "
                f"new column, write to both, backfill it, and swap it for {column}",
            ),
        ),
        confidence=confidence,
    )


# --- dispatch ----------------------------------------------------------

_ActionHandler = Callable[[Facts, Action], Outcome]
_ACTIONS: Dict[str, _ActionHandler] = {
    "add_column": _add_column,
    "drop_column": _drop_column,
    "alter_column_type": _alter_column_type,
    "set_not_null": _set_not_null,
    "drop_not_null": _simple(COLUMN_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_default": _simple(COLUMN_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "drop_default": _simple(COLUMN_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_storage": _simple(COLUMN_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_statistics": _simple(SET_STATISTICS, SHARE_UPDATE_EXCLUSIVE, Work.CATALOG),
    "add_constraint": _add_constraint,
    "drop_constraint": _drop_constraint,
    "validate_constraint": _simple(VALIDATE, SHARE_UPDATE_EXCLUSIVE, Work.SCAN),
    "rename_column": _rename,
    "rename_to": _rename,
    "rename_constraint": _rename,
    "attach_partition": _attach_partition,
    "detach_partition": _detach_partition,
    "set_tablespace": _simple(TABLE_REWRITE, ACCESS_EXCLUSIVE, Work.REWRITE),
    "set_logged": _simple(TABLE_REWRITE, ACCESS_EXCLUSIVE, Work.REWRITE),
    "set_unlogged": _simple(TABLE_REWRITE, ACCESS_EXCLUSIVE, Work.REWRITE),
    "set_schema": _simple(TABLE_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "owner_to": _simple(TABLE_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "row_security": _simple(TABLE_CATALOG, ACCESS_EXCLUSIVE, Work.CATALOG),
    "set_parameters": _set_parameters,
    "enable_trigger": _simple(TRIGGER_STATE, SHARE_ROW_EXCLUSIVE, Work.CATALOG),
    "disable_trigger": _simple(TRIGGER_STATE, SHARE_ROW_EXCLUSIVE, Work.CATALOG),
}

_Handler = Callable[[Facts], Outcome]
_STATEMENTS: Dict[str, _Handler] = {
    "alter_table": _alter_table,
    "create_index": _create_index,
    "drop_index": _drop_index,
    "create_table": _create_table,
    "drop_table": _drop_table,
    "truncate": _drop_table,
    "drop_view": _drop_view,
    "update": _write_rows,
    "delete": _write_rows,
    "insert": _insert,
    "reindex": _reindex,
    "vacuum": _vacuum,
    "analyze": _vacuum,
    "cluster": _cluster,
    "refresh_materialized_view": _refresh,
    "create_trigger": _trigger,
    "drop_trigger": _trigger,
    "comment_on": _comment,
    "lock_table": _lock_table,
    "drop_object": _drop_object,
    "create_object": _no_table,
    "create_type": _no_table,
    "drop_type": _no_table,
    "alter_type_add_value": _no_table,
    "alter_type_rename_value": _no_table,
    "create_view": _no_table,
    "set": _no_table,
}


def effects(facts: Facts) -> Outcome:
    """What the statement does on PostgreSQL, table by table."""
    handler = _STATEMENTS.get(facts.parsed.kind)
    if handler is None:
        return Outcome(
            findings=(
                Finding(
                    "impact.unknown",
                    Severity.INFO,
                    f"no PostgreSQL rule reads a {facts.parsed.kind} statement",
                ),
            ),
            confidence=Confidence.UNKNOWN,
        )
    return handler(facts)


# --- the server facts --------------------------------------------------

_SETTINGS_SQL = (
    "SELECT current_setting('server_version_num'), "
    "current_setting('TimeZone'), current_setting('lock_timeout')"
)

# One row per table, partitioned table, and materialized view outside
# the system schemas: its schema, its name, whether an unqualified name
# finds it on the search path, the estimated rows, the bytes of the
# table with its indexes and TOAST data, and whether any part of it has
# never been vacuumed or analyzed, which leaves the row estimate empty.
# A partitioned table holds no rows itself, so its figures sum its leaf
# partitions. pg_partition_tree() returns no rows for a table outside a
# partition tree, which then stands for itself. The statement holds no
# percent sign, which a driver could read as a placeholder.
_SIZES_SQL = """SELECT n.nspname, c.relname, pg_catalog.pg_table_is_visible(c.oid),
  s.rows, s.bytes, s.unread
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL (
  SELECT coalesce(sum(greatest(l.reltuples, 0)), 0)::bigint,
    coalesce(sum(pg_catalog.pg_total_relation_size(l.oid)), 0)::bigint,
    coalesce(bool_or(l.reltuples < 0 OR (l.reltuples = 0 AND l.relpages = 0)), false)
  FROM (
    SELECT c.oid WHERE c.relkind <> 'p'
    UNION ALL
    SELECT t.relid FROM pg_catalog.pg_partition_tree(c.oid) t
    WHERE c.relkind = 'p' AND t.isleaf
  ) leaf (oid)
  JOIN pg_catalog.pg_class l ON l.oid = leaf.oid
) s (rows, bytes, unread)
WHERE c.relkind IN ('r', 'p', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_(toast|temp_)'"""


def server_version(number: str) -> Tuple[int, ...]:
    """A `server_version_num` value as a version, such as (16, 4)."""
    value = int(number)
    return (value // 10000, value % 10000)


def context_plan() -> ContextPlan:
    """
    Reads the version, the settings, and the table sizes. A statement
    that fails leaves its facts out of `read`, and the rules assume the
    floor or the worst case for them.
    """
    version = FLOORS["postgres"]
    settings: Dict[str, str] = {}
    tables: Dict[str, TableStats] = {}
    read: Set[str] = set()
    try:
        rows = yield _SETTINGS_SQL
    except Exception:
        rows = []
    if rows:
        number, zone, timeout = rows[0]
        version = server_version(str(number))
        settings = {"TimeZone": str(zone), "lock_timeout": str(timeout)}
        read |= {"version", "settings"}
    try:
        sizes = yield _SIZES_SQL
    except Exception:
        pass
    else:
        tables = _sizes(sizes)
        read.add("sizes")
    return EngineContext(
        "postgres",
        version,
        settings=MappingProxyType(settings),
        tables=MappingProxyType(tables),
        read=frozenset(read),
    )


def _sizes(rows: Sequence[Sequence[object]]) -> Dict[str, TableStats]:
    """
    Each table's stats, keyed `schema.table`, and also by the bare name
    when the search path finds the table under it.
    """
    tables: Dict[str, TableStats] = {}
    for schema, name, visible, count, size, unread in rows:
        stats = TableStats(None if unread else int(str(count)), int(str(size)))
        tables[f"{schema}.{name}".lower()] = stats
        if visible:
            tables[str(name).lower()] = stats
    return tables


def _rules() -> Tuple[Rule, ...]:
    return tuple(value for value in globals().values() if isinstance(value, Rule))


PROFILE = Profile(
    name="postgres",
    prefix="pg",
    effects=effects,
    blocks=blocks,
    lock_rank=lock_rank,
    timeout_setting="lock_timeout",
    timeout_statement=timeout_statement,
    transactional_ddl=True,
    rules=_rules(),
    timeout_source=_DOCS + "runtime-config-client.html#GUC-LOCK-TIMEOUT",
    context_plan=context_plan,
    fixture_schema=FIXTURE_SCHEMA,
)
