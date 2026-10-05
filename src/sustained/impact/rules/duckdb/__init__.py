"""
The DuckDB rules, for 1.0 and later.

DuckDB takes no locks. Its concurrency control is optimistic: a
transaction works on its own versions of the rows and the catalog, and a
transaction whose change conflicts with another's uncommitted change
aborts with a conflict error at once, instead of waiting for it. So
nothing queues, and DuckDB has no lock-timeout finding. Reads never
conflict: another transaction reads the table as it was before the
migration began, until the migration commits.

The report names each conflict a statement opens on a table, for as
long as its transaction runs, after the error the other transaction
gets:

- `altered table`: an ALTER TABLE that changes the table's storage,
  which is ADD COLUMN, DROP COLUMN, SET DATA TYPE, and SET NOT NULL.
  INSERT, UPDATE, DELETE, and schema changes on the table in other
  transactions abort, and a transaction that wrote to the table before
  it fails to commit (`writes`)
- `changed rows`: an UPDATE, DELETE, or TRUNCATE. Another transaction
  that updates the same columns of the same rows, or deletes the same
  rows, aborts (`writes`)
- `dropped table`: a DROP TABLE. Schema changes on the table in other
  transactions abort, and a transaction that wrote to the table before
  the DROP fails to commit; reads go on (`writes`)
- `catalog entry`: any other change to the table's entry in the
  catalog, such as a rename, a default, a comment, DROP INDEX, or a new
  table whose foreign key points at it. Only schema changes on the table
  abort; reads and writes go on (`ddl`)

CREATE INDEX, INSERT, ANALYZE, and creating or dropping a view, a type,
a sequence, or a schema open no conflict with other transactions
(`nothing`).

Each statement's work:

- SET DATA TYPE writes every value of the column again, and so does
  ADD COLUMN with a volatile default, such as `random()` (`rewrite`);
  the other columns keep their storage
- any other ADD COLUMN fills the column in every row group, in time
  that grows with the rows (`rows`)
- SET NOT NULL reads every row to check for NULLs (`scan`)
- CREATE INDEX builds the index from every row (`index_build`)
- ANALYZE reads every row (`scan`)
- UPDATE, DELETE, TRUNCATE, and INSERT write rows (`rows`)
- DROP COLUMN and every other statement change only the catalog

DuckDB refuses to alter a table that an index depends on, and refuses a
constraint on ADD COLUMN and ADD CONSTRAINT. The rules leave the first
to the rehearsal, and read the others as unknown.

`context_plan()` reads `version()` and each table's `estimated_size`
from `duckdb_tables()`. DuckDB reports no size in bytes for a single
table.
"""

from __future__ import annotations

from sustained.impact.rules import Facts, Outcome, Profile, common
from sustained.impact.rules.duckdb.catalog import CONCURRENCY, FIXTURE_SCHEMA, RULES
from sustained.impact.rules.duckdb.context import context_plan
from sustained.impact.rules.duckdb.statements import (
    STATEMENTS,
    blocks,
    lock_rank,
    timeout_statement,
)


def effects(facts: Facts) -> Outcome:
    """What the statement does on DuckDB, table by table."""
    return common.dispatch(facts, STATEMENTS)


def _never_queues(lock: object) -> bool:
    return False


PROFILE = Profile(
    name="duckdb",
    title="DuckDB",
    prefix="duckdb",
    effects=effects,
    blocks=blocks,
    lock_rank=lock_rank,
    # DuckDB has no lock timeout setting, and `queues` never draws the
    # finding that would name one.
    timeout_setting="",
    timeout_statement=timeout_statement,
    transactional_ddl=True,
    rules=RULES,
    timeout_source=CONCURRENCY,
    context_plan=context_plan,
    fixture_schema=FIXTURE_SCHEMA,
    queues=_never_queues,
)

__all__ = [
    "PROFILE",
]
