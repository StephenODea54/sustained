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

MySQL commits each DDL statement on its own, so every DDL statement is
a transaction window of its own. Inside a transaction, the row locks of
INSERT, UPDATE, and DELETE last until the next DDL statement commits
them, so each run of those statements is one window, as
`sustained.impact.window.row_scopes()` makes it.

An earlier statement of the run can change how a table is stored: the
handlers record the instant row versions a statement used or gave back,
a FULLTEXT index, and a new ROW_FORMAT in the run state, and
`table_stats()` reads them over the context's figures.

`trace` probes each ALTER TABLE, CREATE INDEX, and DROP INDEX of a
traced rehearsal on a scratch database for the ALGORITHM and LOCK the
server accepts; `sustained.impact.rules.mysql.trace` describes it.

`context_plan()` reads what the rules use: `VERSION()`, which also names
the server MySQL or MariaDB, `foreign_key_checks` and
`lock_wait_timeout`, each table's size, row format, default collation,
and FULLTEXT indexes from `information_schema`, and on MySQL 8.0.29 and later the instant row
versions each table has used from `INNODB_TABLES.TOTAL_ROW_VERSIONS`.
`preflight_plan()` reads the other connections' metadata locks and
InnoDB transactions for the live preflight;
`sustained.impact.rules.mysql.preflight` describes it.
"""

from __future__ import annotations

from sustained.impact.rules import Facts, Outcome, Profile, Trace, common
from sustained.impact.rules.mysql.catalog import (
    FIXTURE_SCHEMA,
    MARIADB_DOCS,
    MYSQL_DOCS,
    RULE_SETS,
)
from sustained.impact.rules.mysql.context import context_plan
from sustained.impact.rules.mysql.locks import (
    blocks,
    bounded,
    lock_rank,
    queues,
    timeout_statement,
)
from sustained.impact.rules.mysql.online import asserted_statements
from sustained.impact.rules.mysql.preflight import preflight_plan
from sustained.impact.rules.mysql.statements import (
    STATEMENTS,
)
from sustained.impact.rules.mysql.trace import (
    attempts,
    refused,
    tables_plan,
    with_observations,
)


def effects(facts: Facts) -> Outcome:
    """What the statement does on MySQL or MariaDB, table by table."""
    return common.dispatch(facts, STATEMENTS)


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
        rules=RULE_SETS[name].all(),
        timeout_source=(
            MARIADB_DOCS + "server-system-variables/#lock_wait_timeout"
            if mariadb
            else MYSQL_DOCS + "server-system-variables.html#sysvar_lock_wait_timeout"
        ),
        context_plan=context_plan,
        fixture_schema=FIXTURE_SCHEMA,
        queues=queues,
        bounded=bounded,
        local_scope=False,
        trace=Trace(tables_plan, with_observations, attempts=attempts, refused=refused),
        preflight=preflight_plan,
    )


MYSQL = _profile("mysql")
MARIADB = _profile("mariadb")


__all__ = [
    "MARIADB",
    "MYSQL",
    "asserted_statements",
]
