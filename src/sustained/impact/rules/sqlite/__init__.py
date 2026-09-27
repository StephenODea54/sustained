"""
The SQLite rules, for 3.35 and later.

SQLite locks the whole database file, not a table. The first write of a
transaction takes the database write lock, and holds it until the
transaction commits, so every other connection's writes wait for it,
on every table, for as long as `busy_timeout` lets them. The report
names the lock `database write lock` on each table a statement writes,
and reads a migration's statements as one transaction window,
`(database)`. What else waits depends on the journal mode:

- `wal`: readers never wait for the writer (`writes`)
- any other mode: readers also wait while the changes are written to
  the database file, at the commit or when the page cache fills
  (`reads_and_writes`), which is the case assumed when the journal mode
  was not read

A writer waiting for the lock does not make later readers queue behind
it the way a server's lock queue does, so SQLite has no lock-timeout
finding.

Each statement's work:

- `ADD COLUMN` changes only the schema, unless the column has a CHECK
  constraint, or a NOT NULL constraint on a generated column, which
  SQLite checks against every row (`scan`). SQLite refuses a UNIQUE or
  PRIMARY KEY column, and on a table that has rows a NOT NULL column
  without a default other than NULL, a default in parentheses or of the
  current time, and a STORED generated column; each gets a `danger`
  finding
- `DROP COLUMN` rewrites every row (`rewrite`)
- the diff's rebuild recipe, known by its `rebuild_table` intent,
  copies every row into a new table and builds its indexes again
  (`rewrite`), reported on the table it rebuilds
- `CREATE INDEX` and `REINDEX` build indexes (`index_build`)
- `DROP TABLE` and `DROP INDEX` visit every page of the table or index
  to free it (`scan`)
- `ANALYZE` reads every index (`scan`)
- `VACUUM` copies the whole database into a new file (`rewrite`), and
  cannot run inside a transaction
- `UPDATE`, `DELETE`, and `INSERT` write rows (`rows`)
- a rename, creating a table, and creating or dropping a view or
  trigger change only the schema

`context_plan()` reads `sqlite_version()`, `PRAGMA journal_mode`, each
table's estimated rows from `sqlite_stat1`, which exists once ANALYZE
has run, its bytes with its indexes from the `dbstat` virtual table,
when SQLite was built with it, and the database file's size from
`PRAGMA page_count` and `page_size`. It counts rows only with
`exact_counts`, and then only in the tables `sqlite_stat1` has no row
count for. Both reads leave out the tables whose names start with
`sqlite_`, which SQLite keeps for itself.
"""

from __future__ import annotations

from sustained.impact.rules import Facts, Outcome, Profile, common
from sustained.impact.rules.sqlite.catalog import DOCS, FIXTURE_SCHEMA, RULES
from sustained.impact.rules.sqlite.context import context_plan, sqlite_version
from sustained.impact.rules.sqlite.statements import (
    STATEMENTS,
    WRITE_LOCK,
    blocks,
    lock_rank,
    timeout_statement,
)


def effects(facts: Facts) -> Outcome:
    """What the statement does on SQLite, table by table."""
    return common.dispatch(facts, STATEMENTS)


def _never_queues(lock: object) -> bool:
    return False


PROFILE = Profile(
    name="sqlite",
    title="SQLite",
    prefix="sqlite",
    effects=effects,
    blocks=blocks,
    lock_rank=lock_rank,
    timeout_setting="busy_timeout",
    timeout_statement=timeout_statement,
    transactional_ddl=True,
    rules=RULES,
    timeout_source=DOCS + "pragma.html#pragma_busy_timeout",
    context_plan=context_plan,
    fixture_schema=FIXTURE_SCHEMA,
    queues=_never_queues,
    locks_database=True,
)

__all__ = [
    "FIXTURE_SCHEMA",
    "PROFILE",
    "WRITE_LOCK",
    "blocks",
    "context_plan",
    "effects",
    "lock_rank",
    "sqlite_version",
    "timeout_statement",
]
