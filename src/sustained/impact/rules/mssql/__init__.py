"""
The SQL Server rules, for 2012 and later.

Each statement kind, and each ALTER TABLE action, has a handler that
names the lock the statement has on each table when it ends and the
work it does there. SQL Server's DDL is transactional, so inside a
migration's transaction every lock is held until the commit.

The lock names are the table lock modes `sys.dm_tran_locks` reports,
ordered weakest first in `LOCKS`. What each blocks follows the lock
compatibility matrix, under locking READ COMMITTED:

- Sch-S, IS, and IX conflict with no reads or writes of other rows, only
  with schema changes (`ddl`)
- S and SIX conflict with the IX a write takes, so INSERT, UPDATE, and
  DELETE wait (`writes`)
- X and Sch-M conflict with the IS or Sch-S a read takes (`reads_and_writes`)

A database with READ_COMMITTED_SNAPSHOT on reads committed rows from row
versions, taking only Sch-S, so there X blocks writes and Sch-M still
blocks reads.

Each statement's lock and work:

- every ALTER TABLE takes Sch-M on the table, and a foreign key takes
  Sch-M on the table it points at too
- `ADD` a column changes only the catalog when it is nullable, or has a
  default that fills nothing. A NOT NULL column with a runtime constant
  default, or a default `WITH VALUES`, changes only the catalog on the
  Enterprise, Developer, and Azure SQL editions, and writes every row on
  the others, which is the case assumed when the edition was not read.
  A per-row default such as `NEWID()`, an identity, a persisted
  computed column, a `rowversion` column, and a default of a large value
  type (`nvarchar(max)`, `varchar(max)`, `varbinary(max)`), `xml`,
  `text`, `ntext`, `image`, `hierarchyid`, a spatial type, or `json`
  write every row everywhere (`rewrite`). A type that is not a system
  type may be a CLR type, which writes every row too, and is likely a
  `rewrite`
- `ALTER COLUMN` changes only the catalog for a longer variable length
  of the same type, or for dropping NOT NULL. Adding NOT NULL reads
  every row of a fixed-length column (`scan`) and writes every row of a
  variable-length one. Any other change writes every row (`rewrite`)
- `ADD CONSTRAINT` of a CHECK or FOREIGN KEY reads every row (`scan`),
  or changes only the catalog `WITH NOCHECK`, which leaves the
  constraint untrusted. `WITH CHECK CHECK CONSTRAINT` reads every row
- a PRIMARY KEY or UNIQUE constraint builds its index (`index_build`),
  or copies a heap into its clustered index (`rewrite`)
- `CREATE INDEX` takes S (`writes`) for a nonclustered index and Sch-M
  for a clustered one, which copies a heap (`rewrite`)
- `DROP INDEX` changes the catalog, except for a clustered index, which
  copies the table into a heap
- `ALTER INDEX ... REBUILD` and `ALTER TABLE ... REBUILD` hold Sch-M
  and build the index again, or copy the table for the clustered index
- `ALTER INDEX ... REORGANIZE` reads the index's pages (`scan`), and on
  the clustered index moves rows in proportion to its fragmentation, up
  to every row (`rewrite`); inside a transaction its locks add up to X
  on the table
- `UPDATE`, `DELETE`, and `INSERT` hold IX and X row locks (`rows`); a
  write to every row of a table of 5,000 rows or more escalates to X on
  the table, and an `INSERT ... SELECT`, whose rows are not counted,
  gets a note that it can escalate
- `UPDATE STATISTICS` reads the table holding Sch-S
- `sp_rename`, `TRUNCATE TABLE`, `DROP TABLE`, `SWITCH`, triggers, and
  defaults change only the catalog under Sch-M

`WITH (ONLINE = ON)` runs an index operation or an `ALTER COLUMN` in
three steps: it takes S on the table when it starts, works while holding
Sch-S, and takes S or Sch-M on the table when it ends. The locks at the
start and the end wait for the open transactions that conflict with
them, and new queries on the table wait behind them. Inside a
transaction the lock at the end is held until the migration commits.
Outside one the table names that lock with `ddl` blocking, so the
lock-timeout finding and the preflight apply, and a note offers
`WAIT_AT_LOW_PRIORITY` where the statement takes it: `ALTER INDEX ...
REBUILD` and `ALTER TABLE ... REBUILD` from 2014, `CREATE INDEX` from
2022, and neither `ADD CONSTRAINT` nor `ALTER COLUMN`. A
`WAIT_AT_LOW_PRIORITY` with `ABORT_AFTER_WAIT = SELF` or `BLOCKERS`
waits beside the lock queue, so later queries do not wait behind it and
it needs no lock timeout. `RESUMABLE = ON` fails inside a transaction
(error 574) and without `ONLINE = ON` (error 11438), and gets a `danger`
finding. `ONLINE = ON` runs only on the Enterprise, Developer, and
Azure SQL editions, and fails elsewhere.

`context_plan()` reads `SERVERPROPERTY('ProductVersion')`, the
`EngineEdition` and `Edition` properties, `@@LOCK_TIMEOUT`,
`is_read_committed_snapshot_on` from `sys.databases`, and each table's
rows from `sys.partitions`, its bytes from `sys.allocation_units`, and
its clustered index from `sys.indexes`.

A traced rehearsal, which SQL Server runs on a scratch database, reads
the locks, partitions, and log around each statement (`trace.py`).
`preflight_plan()` reads the other sessions' table locks and user
transactions for the live preflight (`preflight.py`), including a
transaction with work in this database from a session whose current
database is another one.
"""

from __future__ import annotations

from sustained.impact.rules import Facts, Outcome, Profile, Trace, common
from sustained.impact.rules.mssql.catalog import DOCS, FIXTURE_SCHEMA, RULES
from sustained.impact.rules.mssql.context import context_plan
from sustained.impact.rules.mssql.locks import (
    blocks,
    bounded,
    lock_rank,
    release,
    timeout_statement,
)
from sustained.impact.rules.mssql.preflight import preflight_plan
from sustained.impact.rules.mssql.statements import STATEMENTS
from sustained.impact.rules.mssql.trace import (
    sighting_plan,
    tables_plan,
    with_observations,
)


def effects(facts: Facts) -> Outcome:
    """What the statement does on SQL Server, table by table."""
    return common.dispatch(facts, STATEMENTS)


PROFILE = Profile(
    name="mssql",
    title="SQL Server",
    prefix="mssql",
    effects=effects,
    blocks=blocks,
    lock_rank=lock_rank,
    timeout_setting="lock_timeout",
    timeout_statement=timeout_statement,
    transactional_ddl=True,
    rules=RULES,
    timeout_source=DOCS + "t-sql/statements/set-lock-timeout-transact-sql",
    context_plan=context_plan,
    fixture_schema=FIXTURE_SCHEMA,
    bounded=bounded,
    local_scope=False,
    release=release,
    trace=Trace(tables_plan, with_observations, sighting=sighting_plan),
    preflight=preflight_plan,
)

__all__ = [
    "PROFILE",
]
