---
layout: default
title: Statement impact
description: "Read what each migration statement does to a live PostgreSQL database while it runs: the locks it takes, what they block, whether it rewrites the table, and the safer form."
---

A migration can be valid, reversible, and free of drops, and still take the application down while it runs. A `CREATE INDEX` on a large table stops every write to it until the build finishes. An `ALTER TABLE` that needs `ACCESS EXCLUSIVE` waits behind the longest open transaction, and every query on the table waits behind the `ALTER TABLE`.

The impact analysis reads the statements a run would apply and reports, for each one:

- the tables it locks, the engine's name for each lock, and what that lock blocks: other schema changes, writes, or reads and writes
- the work it does on each table: a catalog change, a scan, an index build, a rewrite, or row changes
- how long it holds each lock: a moment, the whole statement, or until the migration commits
- a safer form of the statement, when the engine has one

The analysis covers PostgreSQL 12 and later. It reads the statement text, the intent Sustained attaches to the statements it generates, and, when it has a connection, the server's version, settings, and table sizes.

## Running it

`sustained impact` prints the report for the run `migrate` would make: every pending migration, then the migration the config module's `models` generate.

```console
$ sustained impact
20260926_orders  transaction
  CREATE INDEX ix_orders_customer ON orders (customer_id)
    orders  SHARE  blocks writes  index_build  transaction  ~41.2M rows, 12.4 GB  [pg.create_index]
    danger  writes to orders wait for the whole index build; build it CONCURRENTLY in a migration with transactional=False
    fix     CREATE INDEX CONCURRENTLY ix_orders_customer ON orders (customer_id)
    warn    no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on orders queues behind it, for as long as the longest open transaction runs
    fix     SET LOCAL lock_timeout = '5s'
  ALTER TABLE orders ADD COLUMN note text
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  brief  ~41.2M rows, 12.4 GB  [pg.add_column]
    warn    no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on orders queues behind it, for as long as the longest open transaction runs
    fix     SET LOCAL lock_timeout = '5s'
  window  orders: SHARE from statement 1, ACCESS EXCLUSIVE from statement 2, held to commit

2 statements, 1 danger, 2 warn. Evidence: catalog (PostgreSQL 16.4)
```

`sustained impact` exits 0 when it prints the report and 1 on a failure, including a dialect the analysis does not cover. It never blocks a run. `--json` prints the report as one object; see [JSON output](/reference/cli#json-output).

From Python, `Migrator.impact(models=None)` returns the same report as an `ImpactReport`, and `await AsyncMigrator.impact(models=None)` does the same on an async adapter. `sustained.impact.analyze(statements, dialect)` analyzes any list of statements, with no connection at all:

```python
from sustained.dialects import Dialects
from sustained.impact import analyze

report = analyze(
    ["CREATE INDEX ix_orders_customer ON orders (customer_id)"],
    Dialects.POSTGRES,
)
for statement in report.statements:
    for table in statement.tables:
        print(table.table, table.lock, table.blocks, table.work)
```

`sustained plan` lists the statements that merit a look in an `impact` section, one line each, with the worst severity and the rules that fired:

```console
impact
  danger  CREATE INDEX ix_orders_customer ON orders (customer_id)  [pg.create_index, pg.lock_timeout]
  info    GRANT SELECT ON orders TO reporting  [impact.unknown]
```

A statement appears there when it has a `warn` or `danger` finding, or when the analysis could not read it. `plan` reads the same server facts as `impact`. The section leaves `plan`'s exit codes unchanged. In `plan --json`, every statement object carries an `impact` key, which is `null` on a dialect the analysis does not cover.

## Reading a report

The report lists each migration by id, with `transaction` or `no transaction` beside it. Under each statement come its tables, then its findings.

A table line reads, in order: the table, the engine's lock name, what the lock blocks, the work, how long the lock is held, the table's size when it is known, and the id of the rule that gave the answer.

A finding line starts with its severity. The lines under it that start with `fix`, and the unlabelled lines after those, are the safer statements, in the order to run them. A remedy is advice. Sustained never rewrites a statement.

The `window` line closes a migration that runs inside a transaction. It names each blocked table and every lock the migration takes on it, all held until the commit.

The last line counts the statements and findings and says what the answer rests on.

## Vocabulary

**Blocks**, ordered from least to most:

| Value | Means |
| --- | --- |
| `nothing` | Other sessions do not wait. |
| `ddl` | Other schema changes wait. Reads and writes proceed. |
| `writes` | INSERT, UPDATE, and DELETE wait. Reads proceed. |
| `reads_and_writes` | Every query on the table waits. |

**Lock** is the engine's own name for the lock, as `pg_locks.mode` reports it without the `Lock` suffix, such as `ACCESS EXCLUSIVE`, `SHARE`, or `SHARE UPDATE EXCLUSIVE`.

**Work**, ordered from lightest to heaviest:

| Value | Means |
| --- | --- |
| `catalog` | Only the system catalog changes. |
| `scan` | Every row is read once. |
| `rows` | The rows a DML statement touches are changed and locked. |
| `index_build` | An index is built from every row. |
| `rewrite` | The table is copied and its indexes rebuilt. |
| `unknown` | The work is not known, and counts as the heaviest. |

**Hold**: `brief` for a lock taken and released within the statement's catalog change, `statement` for a lock held while the statement's work runs, and `transaction` for a lock held until the migration commits.

**Evidence**: `static` when the answer rests on the rule alone, `catalog` when it also rests on facts read from the server, and `observed` when the server was seen to do it. `sustained impact`, `sustained plan`, and `Migrator.impact()` read the server, so their reports are `catalog`. A report from `analyze()` without a context is `static`.

**Confidence**: `known`, `likely` when the answer depends on a fact that was not read, which the finding names, and `unknown` for a statement the analysis could not read.

**Severity**: `info`, `warn`, or `danger`.

## Severity and table size

Work that blocks writes, or reads and writes, is rated against the table's size:

- `danger` when the table has more than 1,000,000 estimated rows or more than 1 GiB
- `info` when the size is known and below both
- `warn` when the size is unknown, and the message says so

A static report reads no sizes, so blocking work is `warn`. `analyze()` takes a `Thresholds(rows, bytes)` to move the limits.

A lock that blocks writes or more, with no lock timeout in scope, draws a `pg.lock_timeout` finding whatever the table's size. The statement waits for its lock behind the longest open transaction on the table, and every query that conflicts with the lock waits behind the statement. The remedy is `SET LOCAL lock_timeout` inside a transaction, or `SET lock_timeout` outside one. A `LOCK TABLE ... NOWAIT` never waits, so it draws no timeout finding. A `lock_timeout` the connection already has, from the role, the database, or the connection string, covers the whole run.

## Server facts

`Migrator.impact()`, `sustained impact`, and `sustained plan` read these facts from the connection before the analysis runs:

| Fact | Read from | Used for |
| --- | --- | --- |
| Version | `server_version_num` | Rules that depend on the version, such as `DETACH PARTITION ... CONCURRENTLY` on 14 and later |
| `TimeZone` | `current_setting()` | Whether `timestamp` to `timestamptz` rewrites the table |
| `lock_timeout` | `current_setting()` | Whether a lock timeout covers the run before any `SET` |
| Table sizes | `pg_class.reltuples` and `pg_total_relation_size()` | The severity of blocking work |
| Schema | the schema read `plan()` uses | The current type of a column a hand-written type change names, the table an index to drop is on, and the tables at the other end of a foreign key a statement drops or re-creates |

The row count is the planner's estimate, which `VACUUM` and `ANALYZE` keep current. A table that was never vacuumed or analyzed has no estimate, so only its size in bytes is known. The size in bytes includes the table's indexes and TOAST data. A partitioned table's figures are the sums over its leaf partitions.

A statement that fails, for example for lack of a privilege, leaves its facts out, and the rules fall back to the support floor or the worst case for them. Each statement runs inside a savepoint, so a failure does not abort the connection's open transaction. The report's `read` lists the facts that came from the server, and the last line of the text report says `assumed` before the version when the version was not read.

`read_context(connection, dialect)` returns these facts as an `EngineContext`, and `await async_read_context(adapter, dialect)` reads them through an async adapter. Pass the context to `analyze()` to rate any list of statements against the live server:

```python
from sustained.impact import analyze, read_context

context = read_context(connection, Dialects.POSTGRES)
report = analyze(statements, Dialects.POSTGRES, context)
```

## Transaction windows

Inside a transaction, PostgreSQL holds every lock until the commit. A brief `ACCESS EXCLUSIVE` from the first statement, followed by a backfill in the second, keeps the table unreadable for the whole backfill. The report reads each migration's statements together:

- `locks` lists every lock that blocks something, with the position of the statement that took it
- `windows` gives, for each table blocked for writes or more, the heaviest work that runs while the lock is held
- a `window.held` finding names a table that stays blocked across heavier work from a later statement
- a `window.lock_order` finding names a migration that blocks reads and writes on more than one table at once, which can deadlock against application transactions that lock the same tables in another order

The NOT NULL flow the diff generates is one such case: it adds the column, backfills it with `UPDATE`, and then sets `NOT NULL`, all in one transaction.

```console
20260926_orders_region  transaction
  ALTER TABLE orders ADD COLUMN region text
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  transaction  [pg.add_column]
  UPDATE orders SET region = 'us' WHERE region IS NULL
    orders  ROW EXCLUSIVE  blocks writes  rows  transaction  [pg.write_rows]
  ALTER TABLE orders ALTER COLUMN region SET NOT NULL
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  scan  statement  [pg.set_not_null]
  window  orders: ACCESS EXCLUSIVE from statement 1, ROW EXCLUSIVE from statement 2, ACCESS EXCLUSIVE from statement 3, held to commit
  warn    orders stays blocked for reads_and_writes from statement 1 until the migration commits, across the rows work of statement 2; move that work to a migration of its own
```

A migration with `transactional=False` releases each lock when its statement ends, so each statement is a window of its own and the report prints no `window` line.

## What the analysis carries through a run

The analysis reads the run in order, and each statement changes what it knows about the next:

- **A table the run created is empty.** No other session can see it yet, so work on it blocks nothing and draws no findings. A plain `CREATE INDEX` on a table created earlier in the run is not flagged.
- **A renamed table keeps its identity.** A later statement that names the new name reads the size of the original table.
- **An index the run created is known.** A `DROP INDEX` names the table the run created the index on. For an index that already exists, the schema read names its table.
- **A lock timeout stays in scope** for as long as PostgreSQL keeps it: `SET LOCAL` until the migration commits, and `SET` for the rest of the session. A timeout the connection already has is in scope from the first statement. `no_lock_without_timeout()` reads the same scope from the statements alone.

## Generated statements

A statement the diff or a `DdlStep` generated carries an intent: what the statement is meant to do, the table and column, and facts only the generator knew, such as the column's type before a type change. The analysis reads the intent first, and reads the text to check that the two agree. When they disagree, the text wins and an `impact.intent_mismatch` finding reports it.

## Statements the analysis does not read

The analysis recognizes the DDL and DML statements its rules cover. Any other statement, such as `GRANT`, a `DO` block, or a statement written in another engine's syntax, has confidence `unknown` and an `impact.unknown` finding that gives the reason. An unknown statement never counts as safe. A migration string that holds more than one statement is also unknown, so write one statement per list entry or per line-ending semicolon in a SQL file.

A default the rules do not recognize as stable counts as volatile, so `ADD COLUMN ... DEFAULT some_function()` reads as a rewrite, with confidence `likely` and a finding that names the function.

## Annotated scripts

`sustained script --annotate` prints the SQL a run would execute, as `sustained script` does, with each statement's impact above it as SQL comments, for a DBA who reads the script before running it by hand:

```console
$ sustained script --annotate
-- impact: 2 statements, 1 danger, 1 warn. Evidence: catalog (PostgreSQL 16.4)
-- up: 20260926_orders
-- impact: orders  SHARE  blocks writes  index_build  transaction  ~41.2M rows, 12.4 GB  [pg.create_index]
-- impact: danger  writes to orders wait for the whole index build; build it CONCURRENTLY in a migration with transactional=False
-- impact: fix     CREATE INDEX CONCURRENTLY ix_orders_customer ON orders (customer_id)
CREATE INDEX ix_orders_customer ON orders (customer_id);
-- impact: locks no table
SET LOCAL lock_timeout = '5s';
-- impact: window  orders: SHARE from statement 1, held to commit
INSERT INTO "sustained_migrations" (...) VALUES (...);
```

The first line is the report's summary. Each statement's tables and findings come above it, and each migration's windows and findings follow its last statement. The tracking bookkeeping is not analyzed. `Migrator.script('up', annotate=True)` and `await AsyncMigrator.script('up', annotate=True)` return the same text. The analysis reads the server facts that `impact` reads, and raises `DialectError`, exit 1 from the shell, on a dialect it does not cover.

## Guards over impact

Four rules in `sustained.guards` read each statement's impact instead of its text, and block a run the way the other [guards](/schema#guards) do:

```python
from sustained.guards import (
    lock_timeout_required,
    max_blocking,
    no_rewrite,
    no_unknown_impact,
)

guards = [
    max_blocking("writes", over_rows=100_000),  # nothing worse than a write lock on a big table
    no_rewrite(over_bytes=1 << 30),             # no rewrite of a table past 1 GiB
    lock_timeout_required(),                    # no queueing lock without a lock_timeout
    no_unknown_impact(),                        # no statement the analysis cannot read
]
```

| Rule | Blocks |
| --- | --- |
| `max_blocking(limit, over_rows=None, over_bytes=None, assume_small=False)` | A statement that blocks more than `limit` on a table past the thresholds. `limit` is `nothing`, `ddl`, `writes`, or `reads_and_writes`. |
| `no_rewrite(over_rows=None, over_bytes=None, assume_small=False)` | A statement whose work on a table past the thresholds is `rewrite`, or `unknown`, which ranks above it |
| `lock_timeout_required()` | A statement with a `pg.lock_timeout` finding: a lock that would queue reads or writes, with no timeout in scope |
| `no_unknown_impact()` | A statement with confidence `unknown` |

With neither `over_rows` nor `over_bytes`, every table counts. With either, a table counts when its estimated rows or bytes pass one of them. A table whose size the threshold needs is not known counts as past it, the worst case, unless the rule is given `assume_small=True`. A table the run created earlier blocks nothing and is never rewritten, so it never counts. Only `no_unknown_impact()` blocks a statement the analysis cannot read; the other three pass it, since the analysis names no table for it.

Before the guards run, `up()` reads the server facts that `Migrator.impact()` reads, analyzes the run, and puts each statement's `StatementImpact` on the statement's `impact` attribute. A guard of your own can read it there. `plan` does the same before it runs the guards. A statement that reaches a rule with no `impact`, such as a plain string passed to `run_guards()`, is analyzed on the spot with no server facts, so its sizes are unknown.

When no configured guard reads impact, `up()` prints each `danger` finding on stderr and the run goes on, as it prints `warn` verdicts:

```console
danger: pg.create_index  CREATE INDEX ix_orders_customer ON orders (customer_id): writes to orders wait for the whole index build; build it CONCURRENTLY in a migration with transactional=False
```

`sustained migrate`, `Migrator.up()`, and `AsyncMigrator.up()` print the same lines. A guard counts as reading impact when it has a true `reads_impact` attribute, which the four rules set. On a dialect the analysis does not cover, `up()` reads no server facts, the four rules are silent, and nothing prints.

`no_table_rewrite()` and `index_must_be_concurrent()` read the statement text, and keep their verdicts in 2.x. `no_rewrite()` and `max_blocking("ddl")` answer the same questions from the analysis, with the server version and the table sizes.

## Observed impact

`sustained rehearse --trace` runs the rehearsal and records what the server did for each statement, and prints the impact report with those facts in place of the prediction. `Migrator.rehearse(trace=True)` puts the report on the result's `impact` attribute, and `await AsyncMigrator.rehearse(trace=True)` does the same.

The rehearsal runs each statement of each up step on its own. Before and after each statement it reads two things inside the rehearsal transaction:

- the table locks the transaction holds, from `pg_locks` for its own backend. Locks are held until the rollback, so a lock the statement took is one held after it and not before.
- the file of each table the statement names and of each of the table's indexes, from `pg_relation_filenode()` and `pg_relation_size()`. A table whose file changed and still holds data was rewritten. An index that is new, or whose file changed, was built.

The observed lock and work replace the predicted ones, and the statement's evidence becomes `observed`. Each difference from the prediction is an `impact.mismatch` finding with severity `warn`:

```console
  ALTER TABLE orders ALTER COLUMN note TYPE short_text
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  scan  statement  8.0 KB  [pg.alter_column_type]
    info    character varying(10) to short_text is not binary coercible as far as the rules know, so orders and its indexes are rewritten while reads and writes wait; ...
    warn    the rules predicted a rewrite on orders, and the server copied no file
```

A lock of `SHARE UPDATE EXCLUSIVE` or stronger on a table no rule named is a mismatch too, and the table joins the statement's tables. The windows are read again from the observed facts.

What the observation can and cannot show:

- It cannot tell a scan from a catalog change, because neither changes a file. A predicted scan stands unless a copy was seen, and a predicted rewrite or index build that copied no file falls to `scan`.
- A table file that changed but is empty afterwards, as after `TRUNCATE` or a rewrite of an empty table, proves nothing either way.
- A table the run created earlier is left as predicted, as the analysis leaves it.
- A migration with `transactional=False` is left out of every rehearsal, so a `CONCURRENTLY` statement keeps its prediction. So does a callable step, whose statements are not known.
- Each read runs inside a savepoint. A read that fails leaves its statement's facts as predicted, and the rehearsal goes on.

A mismatch does not change the exit code of `rehearse`. `--trace` needs PostgreSQL, and `rehearse(trace=True)` raises `DialectError` on any other dialect.

## PostgreSQL

The rules follow the PostgreSQL documentation for 12 and later. Each rule id links to the page it relies on through the finding's `source`.

| Statement | Lock | Work | Rule |
| --- | --- | --- | --- |
| `ADD COLUMN`, nullable or with a stable default | `ACCESS EXCLUSIVE` | catalog | `pg.add_column` |
| `ADD COLUMN` with a volatile default, `serial`, identity, or a stored generated column | `ACCESS EXCLUSIVE` | rewrite | `pg.add_column.rewrite` |
| `ADD COLUMN ... UNIQUE` or `PRIMARY KEY` | `ACCESS EXCLUSIVE` | index build | `pg.add_column.key` |
| `ADD COLUMN ... CHECK` or `REFERENCES` | `ACCESS EXCLUSIVE`, plus `SHARE ROW EXCLUSIVE` on the referenced table | scan | `pg.add_column.checked` |
| `DROP COLUMN` | `ACCESS EXCLUSIVE` | catalog | `pg.drop_column` |
| `ALTER COLUMN ... TYPE` | `ACCESS EXCLUSIVE` | rewrite | `pg.alter_column_type` |
| `ALTER COLUMN ... TYPE`, binary coercible | `ACCESS EXCLUSIVE` | catalog | `pg.alter_column_type.binary_coercible` |
| `SET NOT NULL` | `ACCESS EXCLUSIVE` | scan | `pg.set_not_null` |
| `DROP NOT NULL`, `SET DEFAULT`, `DROP DEFAULT`, `SET STORAGE` | `ACCESS EXCLUSIVE` | catalog | `pg.alter_column.catalog` |
| `SET STATISTICS` | `SHARE UPDATE EXCLUSIVE` | catalog | `pg.set_statistics` |
| `ADD CHECK` | `ACCESS EXCLUSIVE` | scan | `pg.add_check` |
| `ADD CHECK ... NOT VALID` | `ACCESS EXCLUSIVE` | catalog | `pg.add_check.not_valid` |
| `ADD FOREIGN KEY` | `SHARE ROW EXCLUSIVE` on both tables | scan | `pg.add_foreign_key` |
| `ADD FOREIGN KEY ... NOT VALID` | `SHARE ROW EXCLUSIVE` on both tables | catalog | `pg.add_foreign_key.not_valid` |
| `ADD PRIMARY KEY`, `ADD UNIQUE` | `ACCESS EXCLUSIVE` | index build | `pg.add_key` |
| `ADD CONSTRAINT ... USING INDEX` | `ACCESS EXCLUSIVE` | catalog | `pg.add_key.using_index` |
| `ADD EXCLUDE` | `ACCESS EXCLUSIVE` | index build | `pg.add_exclusion` |
| `DROP CONSTRAINT` | `ACCESS EXCLUSIVE` | catalog | `pg.drop_constraint` |
| A statement that drops a foreign key: `DROP TABLE`, `DROP CONSTRAINT`, or `DROP COLUMN` on the table that holds the key, or with `CASCADE` on the table it points at. A type change of a column a key uses or points at re-creates the key. | `ACCESS EXCLUSIVE` on the table at the key's other end | catalog, or a scan of the table that holds a re-created key | `pg.drop_foreign_key` |
| `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE` | scan | `pg.validate_constraint` |
| `RENAME`, `RENAME COLUMN`, `RENAME CONSTRAINT` | `ACCESS EXCLUSIVE` | catalog | `pg.rename` |
| `ATTACH PARTITION` | `SHARE UPDATE EXCLUSIVE` on the parent, `ACCESS EXCLUSIVE` on the partition | scan of the partition | `pg.attach_partition` |
| `DETACH PARTITION` | `ACCESS EXCLUSIVE` on both | catalog | `pg.detach_partition` |
| `DETACH PARTITION ... CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` on both | catalog | `pg.detach_partition.concurrently` |
| `SET TABLESPACE`, `SET LOGGED`, `SET UNLOGGED` | `ACCESS EXCLUSIVE` | rewrite | `pg.table_rewrite` |
| `SET SCHEMA`, `OWNER TO`, row level security, other storage parameters | `ACCESS EXCLUSIVE` | catalog | `pg.alter_table.catalog` |
| `SET (fillfactor = ...)` and the other parameters that take the weaker lock | `SHARE UPDATE EXCLUSIVE` | catalog | `pg.set_parameters` |
| `ENABLE TRIGGER`, `DISABLE TRIGGER` | `SHARE ROW EXCLUSIVE` | catalog | `pg.alter_trigger` |
| `CREATE INDEX` | `SHARE` | index build | `pg.create_index` |
| `CREATE INDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` | index build | `pg.create_index.concurrently` |
| `DROP INDEX` | `ACCESS EXCLUSIVE` on the table | catalog | `pg.drop_index` |
| `DROP INDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` on the table | catalog | `pg.drop_index.concurrently` |
| `CREATE TABLE ... REFERENCES`, `CREATE TABLE ... PARTITION OF` | `SHARE ROW EXCLUSIVE` on the referenced table, `ACCESS EXCLUSIVE` on the parent | catalog | `pg.create_table` |
| `DROP TABLE`, `TRUNCATE` | `ACCESS EXCLUSIVE`, and with `TRUNCATE ... CASCADE` on every table it empties through a foreign key | catalog | `pg.drop_table` |
| `UPDATE`, `DELETE` | `ROW EXCLUSIVE`, plus row locks on the rows changed | rows | `pg.write_rows` |
| `INSERT` | `ROW EXCLUSIVE` | rows | `pg.insert` |
| `REINDEX` | `SHARE` | index build | `pg.reindex` |
| `REINDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` | index build | `pg.reindex.concurrently` |
| `VACUUM`, `ANALYZE` | `SHARE UPDATE EXCLUSIVE` | scan | `pg.vacuum` |
| `VACUUM FULL`, `CLUSTER` | `ACCESS EXCLUSIVE` | rewrite | `pg.vacuum_full` |
| `REFRESH MATERIALIZED VIEW` | `ACCESS EXCLUSIVE` | rewrite | `pg.refresh_materialized_view` |
| `REFRESH MATERIALIZED VIEW CONCURRENTLY` | `EXCLUSIVE` | rows | `pg.refresh_materialized_view.concurrently` |
| `CREATE TRIGGER` | `SHARE ROW EXCLUSIVE` | catalog | `pg.trigger` |
| `DROP TRIGGER` | `ACCESS EXCLUSIVE` | catalog | `pg.trigger` |
| `COMMENT ON` | `SHARE UPDATE EXCLUSIVE` | catalog | `pg.comment` |
| `DROP VIEW` | `ACCESS EXCLUSIVE` | catalog | `pg.drop_view` |
| `LOCK TABLE` | the mode it names | catalog | `pg.lock_table` |
| `DROP SCHEMA ... CASCADE` | `ACCESS EXCLUSIVE` on every table in the schema, which the statement does not name, so the report lists no tables | catalog | `pg.drop_schema` |

A type change is binary coercible when PostgreSQL skips the rewrite: `varchar(n)` to a longer `varchar` or to `text`, `numeric(p,s)` to a wider precision at the same scale, and `timestamp` to `timestamptz` when the `TimeZone` setting is UTC. The rule needs the column's current type, which a generated statement's intent gives, and which the schema read gives for a hand-written statement. Without either, the change reads as a rewrite with confidence `likely`. Without the `TimeZone` setting, `timestamp` to `timestamptz` also reads as a rewrite with confidence `likely`.

A `SET NOT NULL` skips its scan when a valid check constraint already proves the column holds no NULL. The remedy for a scan on a populated table is that route: add the check `NOT VALID`, validate it, set `NOT NULL`, and drop the check.

The tables at the other end of a foreign key come from the schema read. Without it, a `DROP TABLE` or a `DROP CONSTRAINT` reports only the table it names, and a `DROP INDEX` reports its table as `(table of index ix)`.

The integration suite checks every rule against PostgreSQL 14 and 18. It creates the tables the rules' fixture statements name, runs each fixture alone in a transaction under the same reads as `rehearse --trace`, and fails on any `impact.mismatch`. A fixture that PostgreSQL refuses inside a transaction block, such as `CREATE INDEX CONCURRENTLY` or `VACUUM`, is not observed. Neither is `SET TABLESPACE`, because the test servers have no second tablespace to move a table to.

### Remedies

| Statement | Remedy |
| --- | --- |
| `CREATE INDEX` | `CREATE INDEX CONCURRENTLY` in a migration with `transactional=False`. A failed concurrent build leaves an invalid index behind, which has to be dropped. |
| `DROP INDEX` | `DROP INDEX CONCURRENTLY` in a migration with `transactional=False` |
| `ADD FOREIGN KEY` | `ADD ... NOT VALID`, then `VALIDATE CONSTRAINT` in a later migration |
| `ADD CHECK` | `ADD ... NOT VALID`, then `VALIDATE CONSTRAINT` |
| `SET NOT NULL` | `ADD CHECK (c IS NOT NULL) NOT VALID`, `VALIDATE CONSTRAINT`, `SET NOT NULL`, then drop the check |
| `ADD PRIMARY KEY`, `ADD UNIQUE` | `CREATE UNIQUE INDEX CONCURRENTLY`, then `ADD CONSTRAINT ... USING INDEX` |
| `ADD COLUMN` with a volatile default | Add the column without a default, set the default, then backfill in batches |
| A type change that rewrites | Add a new column, write to both, backfill, and swap. The finding names the steps and generates none of them. |
| `REINDEX` | `REINDEX CONCURRENTLY` |
| `REFRESH MATERIALIZED VIEW` | `REFRESH MATERIALIZED VIEW CONCURRENTLY`, which needs a unique index on the view |
| `UPDATE`, `DELETE` over a large table | A batched backfill outside the DDL migration |
| A lock that blocks writes or more, with no timeout | `SET LOCAL lock_timeout = '5s'` before it |

`CONCURRENTLY` needs a migration of its own with `transactional=False`, because PostgreSQL refuses that form inside a transaction block. See [Migrations without a transaction](/schema#migrations-without-a-transaction).
