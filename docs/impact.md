---
layout: default
title: Statement impact
description: "Read what each migration statement does to a live PostgreSQL, MySQL, or MariaDB database while it runs: the locks it takes, what they block, whether it rewrites the table, and the safer form."
---

A migration can be valid, reversible, and free of drops, and still take the application down while it runs. A `CREATE INDEX` on a large table stops every write to it until the build finishes. An `ALTER TABLE` that needs `ACCESS EXCLUSIVE` waits behind the longest open transaction, and every query on the table waits behind the `ALTER TABLE`.

The impact analysis reads the statements a run would apply and reports, for each one:

- the tables it locks, the engine's name for each lock, and what that lock blocks: other schema changes, writes, or reads and writes
- the work it does on each table: a catalog change, a scan, an index build, a rewrite, or row changes
- how long it holds each lock: a moment, the whole statement, or until the migration commits
- a safer form of the statement, when the engine has one

The analysis covers PostgreSQL 12 and later, and InnoDB tables on MySQL 8.0.19 and later and MariaDB 10.6 and later. It reads the statement text, the intent Sustained attaches to the statements it generates, and, when it has a connection, the server's version, settings, and table sizes.

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

On MySQL and MariaDB the same report names the algorithm and lock level the server runs each ALTER TABLE with; see [MySQL and MariaDB](#mysql-and-mariadb).

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

**Lock** is the engine's own name for the lock. On PostgreSQL it is the mode `pg_locks.mode` reports, without the `Lock` suffix, such as `ACCESS EXCLUSIVE`, `SHARE`, or `SHARE UPDATE EXCLUSIVE`. On MySQL and MariaDB it is the `ALGORITHM` and `LOCK` clause the server accepts for the statement, such as `INSTANT` or `INPLACE, LOCK=NONE`, or `MDL EXCLUSIVE` and `IX` for statements that take no such clause.

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

On PostgreSQL, a lock that blocks writes or more, with no lock timeout in scope, draws a `pg.lock_timeout` finding whatever the table's size. The statement waits for its lock behind the longest open transaction on the table, and every query that conflicts with the lock waits behind the statement. The remedy is `SET LOCAL lock_timeout` inside a transaction, or `SET lock_timeout` outside one. A `LOCK TABLE ... NOWAIT` never waits, so it draws no timeout finding. A `lock_timeout` the connection already has, from the role, the database, or the connection string, covers the whole run. MySQL and MariaDB draw the same finding, as `mysql.lock_timeout` or `mariadb.lock_timeout`, for every statement that takes the exclusive metadata lock; see [Lock timeouts on MySQL and MariaDB](#lock-timeouts-on-mysql-and-mariadb).

## Server facts

`Migrator.impact()`, `sustained impact`, and `sustained plan` read these facts from the connection before the analysis runs:

| Fact | Read from | Used for |
| --- | --- | --- |
| Version | `server_version_num` | Rules that depend on the version, such as `DETACH PARTITION ... CONCURRENTLY` on 14 and later |
| `TimeZone` | `current_setting()` | Whether `timestamp` to `timestamptz` rewrites the table |
| `lock_timeout` | `current_setting()` | Whether a lock timeout covers the run before any `SET` |
| Table sizes | `pg_class.reltuples` and `pg_total_relation_size()` | The severity of blocking work |
| Schema | the schema read `plan()` uses | The current type of a column a hand-written type change names, the table an index to drop is on, and the tables at the other end of a foreign key a statement drops or re-creates |

On MySQL and MariaDB the read is this:

| Fact | Read from | Used for |
| --- | --- | --- |
| Version and engine | `VERSION()` | Whether the MySQL or the MariaDB rules apply, and rules that depend on the version, such as instant `DROP COLUMN` on MySQL 8.0.29 and later |
| `foreign_key_checks` | `@@foreign_key_checks` | Whether `ADD FOREIGN KEY` copies the table |
| `lock_wait_timeout` | `@@lock_wait_timeout` | Whether a timeout covers the run before any `SET` |
| Table sizes and row format | `information_schema.TABLES` | The severity of blocking work, and whether a `COMPRESSED` table rules out an instant change |
| FULLTEXT indexes | `information_schema.STATISTICS` | Whether `ADD COLUMN` copies or rebuilds the table, and whether a FULLTEXT index is the table's first |
| Instant row versions | `information_schema.INNODB_TABLES.TOTAL_ROW_VERSIONS`, MySQL 8.0.29 and later | Whether the table has instant changes left |
| Schema | the schema read `plan()` uses | A column's current definition, which columns are indexed, and the table at the other end of a foreign key |

The row count is the planner's estimate, which `VACUUM` and `ANALYZE` keep current. On MySQL and MariaDB it is InnoDB's estimate, which `ANALYZE TABLE` refreshes, and which InnoDB also refreshes on its own after a tenth of the rows change. A table that was never vacuumed or analyzed has no estimate, so only its size in bytes is known. The size in bytes includes the table's indexes and TOAST data. A partitioned table's figures are the sums over its leaf partitions.

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

A migration with `transactional=False` releases each lock when its statement ends, so each statement is a window of its own and the report prints no `window` line. MySQL and MariaDB commit each DDL statement on its own, so there every statement is a window of its own too. `MigrationImpact.held_to_commit`, and the `held_to_commit` key in the JSON output, say whether a migration's locks last until its commit.

## What the analysis carries through a run

The analysis reads the run in order, and each statement changes what it knows about the next:

- **A table the run created is empty.** No other session can see it yet, so work on it blocks nothing and draws no findings. A plain `CREATE INDEX` on a table created earlier in the run is not flagged.
- **A renamed table keeps its identity.** A later statement that names the new name reads the size of the original table.
- **An index the run created is known.** A `DROP INDEX` names the table the run created the index on. For an index that already exists, the schema read names its table.
- **A lock timeout stays in scope** for as long as PostgreSQL keeps it: `SET LOCAL` until the migration commits, and `SET` for the rest of the session. A timeout the connection already has is in scope from the first statement. `no_lock_without_timeout()` reads the same scope from the statements alone. On MySQL and MariaDB, `SET`, `SET SESSION`, and `SET LOCAL` all set the session's value for the rest of the run.
- **Session settings change later statements.** After `SET foreign_key_checks = 0`, a MySQL or MariaDB `ADD FOREIGN KEY` is read as the in-place form that checks no rows. `SET GLOBAL` and `SET PERSIST` leave the session's own value unchanged, so they change nothing the analysis reads.

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
| `lock_timeout_required()` | A statement with a `pg.lock_timeout`, `mysql.lock_timeout`, or `mariadb.lock_timeout` finding: a lock that would queue other sessions, with no timeout in scope |
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

`sustained rehearse --trace` runs the rehearsal and records what the server did for each statement, and prints the impact report with those facts in place of the prediction. `Migrator.rehearse(trace=True)` puts the report on the result's `impact` attribute, and `await AsyncMigrator.rehearse(trace=True)` does the same. Tracing works on PostgreSQL, MySQL, and MariaDB.

### On PostgreSQL

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

### On MySQL and MariaDB

MySQL and MariaDB rehearse on a scratch database, so a traced rehearsal needs `rehearse(scratch=True, trace=True)`, or a `get_rehearsal_connection()` in the config module for `sustained rehearse --trace`. The rehearsal runs each ALTER TABLE, CREATE INDEX, and DROP INDEX with each ALGORITHM and LOCK clause in turn, in the order the server picks one: `INSTANT`, then MariaDB's `NOCOPY`, then `INPLACE`, then `COPY`, and for each algorithm `LOCK=NONE`, then `SHARED`, then `EXCLUSIVE`. The server refuses a clause it cannot run before it does any work, with error 1845 or 1846, or on MySQL 4092 when the table has used every instant row version. The first clause the server accepts is the observed lock, and that run is the rehearsal's run of the statement. The statements run on the scratch tables themselves, so their foreign keys, rows, row format, and instant row versions are the ones the server reads.

The accepted clause replaces the predicted lock on the table the statement names. `INSTANT` shows that the statement copied nothing, and `COPY` that it copied the table; after `NOCOPY` or `INPLACE` the predicted work stands, since either can rebuild the table in place or build an index. Each difference is an `impact.mismatch` finding, and a mismatch quotes the reason the server gave for refusing the predicted clause:

```console
  ALTER TABLE orders ADD COLUMN extra int
    orders  INPLACE, LOCK=NONE  blocks ddl  catalog  brief  8.0 KB  [mysql.add_column.instant]
    warn    the rules predicted INSTANT on orders, and the server ran it with INPLACE, LOCK=NONE; it refused INSTANT: ALGORITHM=INSTANT is not supported. Reason: InnoDB presently supports one FULLTEXT index creation at a time. Try ALGORITHM=COPY/INPLACE.
```

What the probe can and cannot show:

- A statement that spells `ALGORITHM` or `LOCK` runs as written and keeps its prediction. So does every other kind of statement, such as `UPDATE` or `DROP TABLE`.
- An error that is not a refusal, such as a duplicate column, ends the attempts, and the statement runs as written, so the rehearsal fails with the server's own error.
- The metadata lock on a foreign key's parent table keeps its prediction, and so does a table the run created earlier.
- The probe reads the scratch tables. A scratch table without the real table's rows, FULLTEXT indexes, or instant row versions can accept a clause the real table would refuse.

### Mismatches

A mismatch does not change the exit code of `rehearse`. `--trace` needs PostgreSQL, MySQL, or MariaDB, and `rehearse(trace=True)` raises `DialectError` on any other dialect.

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

## MySQL and MariaDB

The rules cover InnoDB tables on MySQL 8.0.19 and later and MariaDB 10.6 and later. The two servers share the `MYSQL` dialect. The `VERSION()` string the server returns names which one it is, and the analysis applies the `mysql` or the `mariadb` rules to match. Rule ids start with `mysql.` or `mariadb.`. Without a server read, `analyze()` assumes MySQL 8.0.19, and the first migration gets an `impact.assumed_profile` finding that says to pass a context read from a MariaDB server.

### Algorithms and locks

InnoDB runs an ALTER TABLE with one of four algorithms:

| Algorithm | What the server does | Work |
| --- | --- | --- |
| `INSTANT` | Changes only the data dictionary | catalog |
| `NOCOPY` | MariaDB only: changes the table in place without rebuilding it | catalog or index build |
| `INPLACE` | Changes the table in place, and rebuilds it in place for some changes | catalog, index build, or rewrite |
| `COPY` | Copies every row into a new table | rewrite |

The report's lock is the clause the server accepts for the statement, and what it blocks while the work runs follows its LOCK level:

| Lock | Blocks |
| --- | --- |
| `..., LOCK=NONE` | `ddl` |
| `..., LOCK=SHARED` | `writes` |
| `..., LOCK=EXCLUSIVE` | `reads_and_writes` |
| `INSTANT` | `reads_and_writes`, for the moment the exclusive metadata lock is held |
| `MDL EXCLUSIVE` | `reads_and_writes`. `DROP TABLE`, `TRUNCATE`, `RENAME TABLE`, `CREATE TRIGGER`, and `DROP VIEW` take it. |
| `IX` | `ddl`. `INSERT`, `UPDATE`, and `DELETE` take it, and also lock the rows they change, so `UPDATE` and `DELETE` block writes. |

An ALTER TABLE with more than one action runs with the heaviest algorithm and the strongest LOCK level among its actions.

### Lock timeouts on MySQL and MariaDB

Every ALTER TABLE, and every statement reported with `MDL EXCLUSIVE`, takes the table's exclusive metadata lock, at least at its start and its end. The lock waits behind every open transaction that has read the table, and every later query on the table waits behind it. So each of these statements draws a `mysql.lock_timeout` or `mariadb.lock_timeout` finding whatever its LOCK level, unless a timeout is in scope. The remedy is `SET SESSION lock_wait_timeout = 5`, in seconds. A value of a day or more counts as no timeout: MySQL's default is a year, and MariaDB's global default is a day. `INSERT`, `UPDATE`, and `DELETE` wait for row locks, which `innodb_lock_wait_timeout` bounds, so they draw no timeout finding.

### Rules

The rules follow the MySQL 8.0 reference manual's [online DDL operations](https://dev.mysql.com/doc/refman/8.0/en/innodb-online-ddl-operations.html) and the MariaDB knowledge base's pages on each algorithm. Each rule id names its page through the finding's `source`.

| Statement | MySQL | MariaDB | Work | Rule |
| --- | --- | --- | --- | --- |
| `ADD COLUMN` | `INSTANT` | `INSTANT` | catalog | `add_column.instant` |
| `ADD COLUMN ... FIRST` or `AFTER`, before MySQL 8.0.29 | `INPLACE, LOCK=NONE` | `INSTANT` | rewrite on MySQL | `add_column.rebuild` |
| `ADD COLUMN` on a table that has used all its instant row versions: 64 before MySQL 9.1, 255 from 9.1 | `INPLACE, LOCK=NONE` | no limit | rewrite on MySQL | `add_column.rebuild` |
| `ADD COLUMN` on a `ROW_FORMAT=COMPRESSED` table | `INPLACE, LOCK=NONE` | `INPLACE, LOCK=NONE` | rewrite | `add_column.rebuild` |
| `ADD COLUMN` on a table with a FULLTEXT index | `COPY, LOCK=SHARED` | `INPLACE, LOCK=SHARED` | rewrite | `add_column.copy`, `add_column.rebuild` |
| `ADD COLUMN ... UNIQUE` or `PRIMARY KEY` | `INPLACE, LOCK=NONE` | `INPLACE, LOCK=NONE` | rewrite | `add_column.rebuild` |
| `ADD COLUMN ... AUTO_INCREMENT` | `INPLACE, LOCK=SHARED` | `INPLACE, LOCK=SHARED` | rewrite | `add_column.rebuild` |
| `ADD COLUMN` with an expression default in parentheses, such as `DEFAULT (1 + 1)` | `COPY, LOCK=SHARED` | `INSTANT`, or `COPY` for a volatile expression such as `(uuid())` | rewrite, or catalog when instant | `add_column.copy` |
| `ADD COLUMN ... STORED`, `... CHECK`, or `... REFERENCES` with `foreign_key_checks` on | `COPY` | `COPY` | rewrite | `add_column.copy` |
| `DROP COLUMN`, MySQL 8.0.29 and later | `INSTANT` | `INSTANT` | catalog | `drop_column.instant` |
| `DROP COLUMN` before MySQL 8.0.29, or of an indexed column | `INPLACE, LOCK=NONE` | `NOCOPY, LOCK=NONE` for an indexed column | rewrite on MySQL, catalog on MariaDB | `drop_column.rebuild` |
| `MODIFY` or `CHANGE` that keeps the type and the nullability, and changes the default, the comment, or adds ENUM or SET members at the end | `INSTANT` | `INSTANT` | catalog | `modify_column.instant` |
| `MODIFY` that widens a VARCHAR and keeps its length prefix | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `modify_column.inplace`, `modify_column.instant` |
| `MODIFY` that widens a VARCHAR past its length prefix | `COPY, LOCK=SHARED` | `INSTANT` | rewrite on MySQL | `modify_column.copy`, `modify_column.instant` |
| `MODIFY` that changes NULL to NOT NULL, or back | `INPLACE, LOCK=NONE` | `INPLACE, LOCK=NONE` | rewrite | `modify_column.rebuild` |
| `MODIFY ... FIRST` or `AFTER` | `INPLACE, LOCK=NONE` | `INSTANT` | rewrite on MySQL | `modify_column.rebuild`, `modify_column.instant` |
| `MODIFY` or `CHANGE` to another type, a shorter VARCHAR, reordered ENUM members, or AUTO_INCREMENT | `COPY, LOCK=SHARED` | `COPY` | rewrite | `modify_column.copy` |
| `ALTER COLUMN ... SET DEFAULT`, `DROP DEFAULT`, `SET VISIBLE`, `SET INVISIBLE` | `INSTANT` | `INSTANT` | catalog | `column_default` |
| `RENAME COLUMN`, `CHANGE` to a new name only | `INSTANT` from 8.0.28, `INPLACE, LOCK=NONE` before | `INSTANT` | catalog | `rename` |
| `RENAME TO`; `RENAME TABLE` | `INSTANT`; `MDL EXCLUSIVE` | `INSTANT`; `MDL EXCLUSIVE` | catalog | `rename` |
| `RENAME INDEX` | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `rename_index` |
| `CREATE INDEX`, `ADD INDEX`, `ADD UNIQUE` | `INPLACE, LOCK=NONE` | `NOCOPY, LOCK=NONE` | index build | `add_index` |
| A FULLTEXT index | `INPLACE, LOCK=SHARED` | `INPLACE, LOCK=SHARED` for the table's first, `NOCOPY, LOCK=SHARED` after | rewrite for the table's first, which adds a hidden `FTS_DOC_ID` column; index build after | `add_fulltext` |
| `DROP INDEX` | `INPLACE, LOCK=NONE` | `NOCOPY, LOCK=NONE` | catalog | `drop_index` |
| `ADD PRIMARY KEY`, or `DROP PRIMARY KEY` with `ADD PRIMARY KEY` | `INPLACE, LOCK=NONE` | `INPLACE, LOCK=NONE` | rewrite | `add_primary_key` |
| `DROP PRIMARY KEY` alone | `COPY, LOCK=SHARED` | `COPY` | rewrite | `drop_primary_key` |
| `ADD FOREIGN KEY` with `foreign_key_checks` on | `COPY, LOCK=SHARED` | `COPY` | rewrite | `add_foreign_key` |
| `ADD FOREIGN KEY` with `foreign_key_checks` off | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `add_foreign_key.unchecked` |
| `DROP FOREIGN KEY` | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `drop_foreign_key` |
| `ADD CHECK` | `COPY, LOCK=SHARED` | `COPY` | rewrite | `add_check` |
| `DROP CHECK`, or `DROP CONSTRAINT` of a check | `INSTANT` | `INSTANT` | catalog | `drop_check` |
| `CONVERT TO CHARACTER SET`, `ENGINE=` another engine | `COPY, LOCK=SHARED` | `COPY` | rewrite | `table_copy` |
| `ENGINE=InnoDB`, `FORCE`, `ROW_FORMAT=`, `KEY_BLOCK_SIZE=`, `OPTIMIZE TABLE` | `INPLACE, LOCK=NONE` | `INPLACE, LOCK=NONE` | rewrite | `table_rebuild` |
| `COMMENT =`, `AUTO_INCREMENT =` | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `table_option` |
| `DROP TABLE`, `TRUNCATE` | `MDL EXCLUSIVE` | `MDL EXCLUSIVE` | catalog | `drop_table` |
| `CREATE TRIGGER` | `MDL EXCLUSIVE` | `MDL EXCLUSIVE` | catalog | `trigger` |
| `DROP VIEW` | `MDL EXCLUSIVE` | `MDL EXCLUSIVE` | catalog | `drop_view` |
| `UPDATE`, `DELETE` | `IX`, plus row locks | `IX`, plus row locks | rows | `write_rows` |
| `INSERT` | `IX` | `IX` | rows | `insert` |

`COPY` runs with `LOCK=NONE` on MariaDB 11.2 and later, so writes go on while the table is copied. On MySQL, and on MariaDB before 11.2, it runs with `LOCK=SHARED`. `OPTIMIZE TABLE` on a table with a FULLTEXT index copies it.

MySQL also takes the exclusive metadata lock on the parent table of a foreign key the statement adds or drops: `ADD FOREIGN KEY`, `DROP FOREIGN KEY`, `CREATE TABLE ... REFERENCES`, and `DROP TABLE` of a table that has a foreign key. The report lists the parent with `MDL EXCLUSIVE` under the rule `mysql.foreign_key_parent`. The parent of a dropped key comes from the schema read. MariaDB takes no such lock.

A `MODIFY` or `CHANGE` restates the whole column, so the rules compare it with the column's current definition, from the intent of a statement the diff generated or from the schema read. Without either, the change counts as a type change: `COPY`, with confidence `likely`. A VARCHAR stores each value's length in one byte while its longest value fits in 255 bytes, and in two above that, so whether a widened VARCHAR keeps its length prefix depends on the character set. Without the column's collation, the rules assume utf8mb4, four bytes to a character.

Without a server read, the storage facts are unknown, so a change that is instant on most tables reads as `INSTANT` with confidence `likely`, and a finding names the facts that would rule it out: a FULLTEXT index, `ROW_FORMAT=COMPRESSED`, or used-up instant row versions. Without the schema read, `DROP COLUMN` reads as instant with confidence `likely`, since an indexed column is dropped in place. `DROP TRIGGER` names no table, and the schema read reads no triggers, so it reports no table, with confidence `likely`.

A statement the rules do not read, such as `ANALYZE TABLE`, `LOCK TABLES`, or MariaDB's `WAIT n` and `NOWAIT`, is unknown.

### Asserting the algorithm

For each ALTER TABLE, CREATE INDEX, or DROP INDEX the rules read as `INSTANT`, or as in place with `LOCK=NONE`, an `info` finding gives the reason and offers the statement with that clause added:

```console
  ALTER TABLE orders ADD COLUMN note text
    orders  INSTANT  blocks reads_and_writes  catalog  brief  ~41.2M rows, 12.4 GB  [mysql.add_column.instant]
    info    the column is added in the data dictionary; assert INSTANT so the server refuses the statement instead of running it with a slower algorithm or a stronger lock
    fix     ALTER TABLE orders ADD COLUMN note text, ALGORITHM=INSTANT
```

With the clause, the server refuses the statement when it cannot run it that way. Without it, the server falls back to a slower algorithm or a stronger lock, as when a table has used its instant row versions. MySQL refuses a LOCK clause beside `ALGORITHM=INSTANT`, so the instant form names the algorithm alone. MariaDB's DROP INDEX takes no clause, so there the finding offers the `ALTER TABLE ... DROP INDEX` form.

A statement that spells `ALGORITHM` or `LOCK` is read with them. A heavier algorithm or a stronger lock runs as asked. MySQL runs a `LOCK` clause without `ALGORITHM` in place, never instant. An algorithm or a LOCK level the change cannot run with draws a `mysql.refused` or `mariadb.refused` finding with severity `warn`, since the server refuses the statement, and the table is reported with no lock.

A statement that copies the table while writes wait names an online schema change tool, such as gh-ost or pt-online-schema-change, which copies the table without blocking writes.

The integration suite checks the rules against MySQL 8.4 and 26.7 and MariaDB 11.4 and 12.3. It checks the facts `read_context()` reads, and the parent-table locks, by running each foreign key statement while a second session reads the parent. It creates the tables the rules' fixture statements name in a database of their own, runs each fixture alone under the probe of `rehearse --trace`, and fails on any `impact.mismatch`. It creates the database again for each fixture, since MySQL schema changes do not roll back. A fixture that spells its own clause must run, or be refused when the rules predict a refusal.
