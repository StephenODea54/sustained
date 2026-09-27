---
layout: default
title: Statement impact
description: "Read what each migration statement does to a live PostgreSQL, MySQL, MariaDB, SQL Server, SQLite, or DuckDB database while it runs: the locks it takes, what they block, whether it rewrites the table, and the safer form."
---

A migration can be valid, reversible, and free of drops, and still take the application down while it runs. A `CREATE INDEX` on a large table stops every write to it until the build finishes. An `ALTER TABLE` that needs `ACCESS EXCLUSIVE` waits behind the longest open transaction, and every query on the table waits behind the `ALTER TABLE`.

The impact analysis reads the statements a run would apply and reports, for each one:

- the tables it locks, the engine's name for each lock, and what that lock blocks: other schema changes, writes, or reads and writes
- the work it does on each table: a catalog change, a scan, an index build, a rewrite, or row changes
- how long each lock lasts: a moment, the whole statement, or until the migration commits
- a safer form of the statement, when the engine has one

The analysis covers PostgreSQL 12 and later, InnoDB tables on MySQL 8.0.19 and later and MariaDB 10.6 and later, SQL Server 2012 and later, SQLite 3.35 and later, and DuckDB 1.0 and later. It reads the statement text, the intent Sustained attaches to the statements it generates, and, when it has a connection, the server's version, settings, and table sizes.

## Running it

`sustained impact` prints the report for the run `migrate` would make: every pending migration, then the migration the config module's `models` generate. `migrate` diffs the models after the pending migrations apply, so while migrations are pending `impact` diffs them where `rehearse` would: on the scratch database `get_rehearsal_connection()` returns, after a rehearsal there applies the pending migrations and takes them back. Without a scratch database it leaves the models' migration out and prints `models not diffed` under the report, since only applying the pending migrations shows the schema the diff would read.

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
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  transaction  ~41.2M rows, 12.4 GB  [pg.add_column]
    warn    no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on orders queues behind it, for as long as the longest open transaction runs
    fix     SET LOCAL lock_timeout = '5s'
  window  orders: SHARE from statement 1, ACCESS EXCLUSIVE from statement 2, held to commit

2 statements, 1 danger, 2 warn. Evidence: catalog (PostgreSQL 16.4)
```

On MySQL and MariaDB the same report names the algorithm and lock level the server runs each ALTER TABLE with; see [MySQL and MariaDB](#mysql-and-mariadb). On SQL Server it names the table lock mode, and what the edition runs online; see [SQL Server](#sql-server). On SQLite it names the lock every write takes on the whole database; see [SQLite](#sqlite). On DuckDB, which takes no locks, it names the conflict each statement opens with other transactions; see [DuckDB](#duckdb).

`sustained impact` exits 0 when it prints the report and 1 on a failure, including a dialect the analysis does not cover. It never blocks a run. `--json` prints the report as one object; see [JSON output](/reference/cli#json-output). `--live` adds the sessions each statement would wait behind now; see [Live preflight](#live-preflight).

From Python, `Migrator.impact(models=None)` returns the same report as an `ImpactReport`, and `await AsyncMigrator.impact(models=None)` does the same on an async adapter. It takes the diff options `up()` takes, such as `allow_drops` and `renames`, and diffs the models against the schema as it is now. `sustained.impact.analyze(statements, dialect)` analyzes any list of statements, with no connection at all:

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

A statement appears there when it has a `warn` or `danger` finding, or when the analysis could not read it. `plan` reads the same server facts as `impact`. The section leaves `plan`'s exit codes unchanged. In `plan --json`, every statement object has an `impact` key, which is `null` on a dialect the analysis does not cover.

## Reading a report

The report lists each migration by id, with `transaction` or `no transaction` beside it. Under each statement come its tables, then its findings.

A table line reads, in order: the table, the engine's lock name, what the lock blocks, the work, how long the lock is held, the table's size when it is known, and the id of the rule that gave the answer.

A finding line starts with its severity. The lines under it that start with `fix`, and the unlabelled lines after those, are the safer statements, in the order to run them. A remedy is advice. Sustained never rewrites a statement.

The `window` line closes a migration that runs inside a transaction. It names each blocked table and every lock the migration takes on it, each of which lasts until the commit. In such a migration, the hold of every lock that blocks something is `transaction`, on the last statement too.

The last line counts the statements and findings and says what the answer rests on.

## Vocabulary

**Blocks**, ordered from least to most:

| Value | Means |
| --- | --- |
| `nothing` | Other sessions do not wait. |
| `ddl` | Other schema changes wait. Reads and writes proceed. |
| `writes` | INSERT, UPDATE, and DELETE wait. Reads proceed. |
| `reads_and_writes` | Every query on the table waits. |

**Lock** is the engine's own name for the lock. On PostgreSQL it is the mode `pg_locks.mode` reports, without the `Lock` suffix, such as `ACCESS EXCLUSIVE`, `SHARE`, or `SHARE UPDATE EXCLUSIVE`. On MySQL and MariaDB it is the `ALGORITHM` and `LOCK` clause the server accepts for the statement, such as `INSTANT` or `INPLACE, LOCK=NONE`, or `MDL EXCLUSIVE` and `IX` for statements that take no such clause. On SQL Server it is the table lock mode `sys.dm_tran_locks.request_mode` reports, such as `Sch-M`, `S`, or `X`. On SQLite it is `database write lock`, which every write takes on the whole database file. DuckDB takes no locks, so there it is the conflict a statement opens on the table, `altered table`, `changed rows`, `dropped table`, or `catalog entry`, and another transaction that conflicts with it aborts instead of waiting.

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

On PostgreSQL, a lock that blocks writes or more, with no lock timeout in scope, draws a `pg.lock_timeout` finding whatever the table's size. The statement waits for its lock behind the longest open transaction on the table, and every query that conflicts with the lock waits behind the statement. The remedy is `SET LOCAL lock_timeout` inside a transaction, or `SET lock_timeout` outside one. A `LOCK TABLE ... NOWAIT` never waits, so it draws no timeout finding. A `lock_timeout` the connection already has, from the role, the database, or the connection string, covers the whole run. MySQL and MariaDB draw the same finding, as `mysql.lock_timeout` or `mariadb.lock_timeout`, for every statement that takes the exclusive metadata lock; see [Lock timeouts on MySQL and MariaDB](#lock-timeouts-on-mysql-and-mariadb). SQL Server draws it as `mssql.lock_timeout` for every table lock of `S` or stronger, with `SET LOCK_TIMEOUT 5000` as the remedy; see [Lock timeouts on SQL Server](#lock-timeouts-on-sql-server). SQLite draws none, because a write waiting for the lock does not make other connections queue behind it. DuckDB draws none either, because a conflicting transaction aborts instead of waiting.

## Server facts

`Migrator.impact()`, `sustained impact`, and `sustained plan` read these facts from the connection before the analysis runs:

| Fact | Read from | Used for |
| --- | --- | --- |
| Version | `server_version_num` | Rules that depend on the version, such as `DETACH PARTITION ... CONCURRENTLY` on 14 and later |
| `TimeZone` | `current_setting()` | Whether a change between `timestamp` and `timestamptz` rewrites the table |
| `lock_timeout` | `current_setting()` | Whether a lock timeout covers the run before any `SET` |
| Table sizes | `pg_class.reltuples` and `pg_total_relation_size()` | The severity of blocking work |
| Partitions (`partitions`) | `pg_inherits` and `pg_partitioned_table` | Which tables are partitioned, the partitions a statement on one also locks, and the DEFAULT partition an added, attached, detached, or dropped partition locks |
| Indexed columns (`indexes`) | `pg_index`, `pg_depend`, and `pg_attribute` | Whether a type change rebuilds an index on the column, including an expression index, and the collation the column has before the change |
| Array columns (`arrays`) | `format_type()` of each array column | The current type of an array column, which the schema read reports as `ARRAY` |
| Types (`types`) | `pg_type` and `pg_constraint` | Whether a column added with a type outside the system schemas is a domain with a NOT NULL or a CHECK |
| Schema | the schema read `plan()` uses | The current type of a column a hand-written type change names, the table an index to drop is on, and the tables at the other end of a foreign key a statement drops or re-creates |

On MySQL and MariaDB the read is this:

| Fact | Read from | Used for |
| --- | --- | --- |
| Version and engine | `VERSION()` | Whether the MySQL or the MariaDB rules apply, and rules that depend on the version, such as instant `DROP COLUMN` on MySQL 8.0.29 and later |
| `foreign_key_checks` | `@@foreign_key_checks` | Whether `ADD FOREIGN KEY` copies the table |
| `lock_wait_timeout` | `@@lock_wait_timeout` | Whether a timeout covers the run before any `SET` |
| Table sizes, row format, and default collation | `information_schema.TABLES` | The severity of blocking work, whether a `COMPRESSED` table rules out an instant change, and the collation a restated text column takes when the statement names none |
| FULLTEXT indexes | `information_schema.STATISTICS` | Whether `ADD COLUMN` copies or rebuilds the table, and whether a FULLTEXT index is the table's first |
| Instant row versions | `information_schema.INNODB_TABLES.TOTAL_ROW_VERSIONS`, MySQL 8.0.29 and later | Whether the table has instant changes left. `INNODB_TABLES` names a table as its file is named, such as `a@002db` for `a-b`, and names each partition on its own; the rules read a partitioned table as the partition that has used the most. A name with a character MySQL writes from a table of its own, such as `ö`, reads as unknown. |
| Schema | the schema read `plan()` uses | A column's current definition, which columns are indexed, and the table at the other end of a foreign key |

On SQL Server the read is this:

| Fact | Read from | Used for |
| --- | --- | --- |
| Version | `SERVERPROPERTY('ProductVersion')` | Rules that depend on the version, such as `ALTER COLUMN ... WITH (ONLINE = ON)` on 2016 and later, and the release the report names, such as `2022 (16.0.4135.4)` |
| Edition | `SERVERPROPERTY('EngineEdition')` and `SERVERPROPERTY('Edition')` | Whether `ONLINE = ON` runs, and whether a NOT NULL column with a default changes only the catalog |
| `LOCK_TIMEOUT` | `@@LOCK_TIMEOUT` | Whether a timeout covers the run before any `SET LOCK_TIMEOUT` |
| `READ_COMMITTED_SNAPSHOT` | `sys.databases.is_read_committed_snapshot_on` | Whether reads wait for an `X` lock |
| Table rows and sizes | `sys.partitions` and `sys.allocation_units` | The severity of blocking work |
| Clustered index | `sys.indexes` | Whether the table is a heap, and whether an index a statement drops or rebuilds is the clustered one |
| Schema | the schema read `plan()` uses | A column's current type and nullability, and the table an index to drop is on |

On SQLite the read is this:

| Fact | Read from | Used for |
| --- | --- | --- |
| Version | `sqlite_version()` | The version the report names |
| Journal mode | `PRAGMA journal_mode` | Whether reads wait for the write lock |
| Table rows | `sqlite_stat1`, which exists once `ANALYZE` has run, and with `exact_counts=True` a `SELECT COUNT(*)` of each table it has no row count for | The severity of blocking work |
| Table sizes | with `exact_counts=True`, the `dbstat` virtual table, when SQLite was built with it; otherwise the database size times the table's share of the rows `sqlite_stat1` counts | The severity of blocking work |
| Database size | `PRAGMA page_count` and `page_size` | The severity of `VACUUM` and of a `REINDEX` of every index |
| Schema | the schema read `plan()` uses | The table an index to drop or reindex is on |

On DuckDB the read is this:

| Fact | Read from | Used for |
| --- | --- | --- |
| Version | `version()` | The version the report names |
| Table rows | `duckdb_tables().estimated_size` | The severity of blocking work |
| Schema | the schema read `plan()` uses | The table an index to drop is on |

The row count is the planner's estimate, which `VACUUM` and `ANALYZE` keep current. On MySQL and MariaDB it is InnoDB's estimate, which `ANALYZE TABLE` refreshes, and which InnoDB also refreshes on its own after a tenth of the rows change. On SQL Server it is the approximate count `sys.partitions` keeps for the heap or clustered index, and the size is the table's used pages across all its indexes. On SQLite it is the count `ANALYZE` recorded, which nothing refreshes until `ANALYZE` runs again. `ANALYZE` records no count for a table that was empty when it ran, or created after it, and the read counts no rows itself unless `exact_counts=True` asks it to count those tables. Each count reads every page of the table. `exact_counts=True` also reads each table's bytes from `dbstat`, which reads every page of the table and its indexes; without it, a table's bytes are the database file's bytes in proportion to its share of the rows `sqlite_stat1` counts. `read_context()`, `async_read_context()`, `impact()`, `up()`, and `script()` on either migrator take `exact_counts`; on the command line, `plan`, `impact`, `migrate`, and `script` take `--exact-counts`, or the config module sets `exact_counts = True`. On DuckDB it is the row count DuckDB keeps for each table, and DuckDB reports no size in bytes for a single table. The other engines read the estimates the server keeps either way. A table that was never vacuumed or analyzed has no estimate, so only its size in bytes is known. The size in bytes includes the table's indexes and TOAST data. A partitioned table's figures are the sums over its leaf partitions.

A statement that fails, for example for lack of a privilege, leaves its facts out, and the rules fall back to the support floor or the worst case for them. Each statement runs inside a savepoint, so a failure does not abort the connection's open transaction. The report's `read` lists the facts that came from the server, and the last line of the text report says `assumed` before the version when the version was not read.

On PostgreSQL, without the partitions read it is not known whether a table is a partitioned table or a partition, and a statement whose answer depends on that has confidence `likely` and a `pg.partitions_unread` finding; see [Partitioned tables](#partitioned-tables). Without the types read, a column added with a type outside the built-in types reads as a rewrite with confidence `likely`. Without the indexes read, a type change with `COLLATE` reads as an index rebuild with confidence `likely`, and so does a change between `timestamp` and `timestamptz` on a column the schema read does not show in an index. The schema read leaves out expression indexes, so it cannot show that a column has no index. `ATTACH PARTITION` reads as an index build with confidence `likely` unless the indexes read shows the partitioned table has no index.

`up()` reads the sizes of the tables its statements name, and of no other table. The names come from the analysis of the statements with the schema read, so a statement that drops an index or a foreign key also names the table the schema puts it on. `impact()`, `plan`, `script`, and `rehearse --trace` read the sizes of every table. On PostgreSQL, `pg_total_relation_size()` takes `ACCESS SHARE` on each table it sizes, which waits behind a session that holds `ACCESS EXCLUSIVE` on the table. So the size read sets `lock_timeout` to `1s` with `set_config()`, and afterwards sets the session's own value back. When the read of the named tables runs out of time, each table is read in a statement of its own, and only a table whose lock was not granted in time has an unknown size. An unknown size counts as over a guard's threshold.

`read_context(connection, dialect, exact_counts=False, statements=None)` returns these facts as an `EngineContext`, and `await async_read_context(adapter, dialect)` reads them through an async adapter. With `statements`, the sizes are read only for the tables `named_tables(statements, dialect, schema)` returns. Pass the context to `analyze()` to rate any list of statements against the live server:

```python
from sustained.impact import analyze, read_context

context = read_context(connection, Dialects.POSTGRES)
report = analyze(statements, Dialects.POSTGRES, context)
```

## Transaction windows

Inside a transaction, PostgreSQL keeps every lock until the commit. A brief `ACCESS EXCLUSIVE` from the first statement, followed by a backfill in the second, keeps the table unreadable for the whole backfill. The report reads each migration's statements together:

- `locks` lists every lock that blocks something, with the position of the statement that took it
- `windows` gives, for each table blocked for writes or more, the heaviest work that runs while the lock is held
- a `window.held` finding names a table that stays blocked across heavier work from a later statement, with each level the table is blocked for and the first statement to block it that far
- a `window.lock_order` finding names a migration that blocks reads and writes on more than one table at once, which can deadlock against application transactions that lock the same tables in another order

The NOT NULL flow the diff generates is one such case: it adds the column, backfills it with `UPDATE`, and then sets `NOT NULL`, all in one transaction.

```console
20260926_orders_region  transaction
  ALTER TABLE orders ADD COLUMN region text
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  transaction  [pg.add_column]
    info    the partitions were not read, so it is not known whether orders is a partitioned table or a partition; if orders is a partitioned table, each partition below it is also locked ACCESS EXCLUSIVE
    warn    no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on orders queues behind it, for as long as the longest open transaction runs
    fix     SET LOCAL lock_timeout = '5s'
  UPDATE orders SET region = 'us' WHERE region IS NULL
    orders  ROW EXCLUSIVE  blocks writes  rows  transaction  [pg.write_rows]
    warn    writes to the rows the backfill changes on orders wait until the migration commits; on a large table, backfill in batches outside the DDL migration; the size of orders is unknown
  ALTER TABLE orders ALTER COLUMN region SET NOT NULL
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  scan  transaction  [pg.set_not_null]
    warn    reads and writes on orders wait while every row is checked for NULL, unless a valid CHECK (region IS NOT NULL) already proves it; add that check NOT VALID, validate it, then SET NOT NULL skips the scan; the size of orders is unknown
    fix     ALTER TABLE orders ADD CONSTRAINT orders_region_not_null CHECK (region IS NOT NULL) NOT VALID
            ALTER TABLE orders VALIDATE CONSTRAINT orders_region_not_null
            ALTER TABLE orders ALTER COLUMN region SET NOT NULL
            ALTER TABLE orders DROP CONSTRAINT orders_region_not_null
    info    the partitions were not read, so it is not known whether orders is a partitioned table or a partition; if orders is a partitioned table, each partition below it is also locked ACCESS EXCLUSIVE
    warn    no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on orders queues behind it, for as long as the longest open transaction runs
    fix     SET LOCAL lock_timeout = '5s'
  window  orders: ACCESS EXCLUSIVE from statement 1, ROW EXCLUSIVE from statement 2, ACCESS EXCLUSIVE from statement 3, held to commit
  warn    orders stays blocked for reads_and_writes from statement 1 until the migration commits, across the rows work of statement 2; move that work to a migration of its own
```

A migration with `transactional=False` releases each lock when its statement ends, so each statement is a window of its own and the report prints no `window` line. MySQL and MariaDB commit each DDL statement on its own, so there every DDL statement is a window of its own too. Inside a transaction, the row locks of `INSERT`, `UPDATE`, and `DELETE` last until the next DDL statement commits them, or until the migration commits, so each run of those statements is one window, and a table an earlier statement of the run locked is reported `transaction` and draws `window.held` across heavier work later in the run. The finding then says the table stays blocked until the implicit commit before the DDL statement that ends the run, or until the migration commits when no DDL statement follows. A statement the rules do not read counts as one that commits nothing. `held_to_commit` is false there, since the DDL commits. On SQLite a write locks the whole database, so a migration inside a transaction is one window, named `(database)`, whatever tables it writes; see [SQLite](#sqlite). SQL Server DDL is transactional, so there every lock is held until the migration commits, as on PostgreSQL. DuckDB DDL is transactional, so there a conflict lasts until the migration commits, as a lock does on PostgreSQL. `MigrationImpact.held_to_commit`, and the `held_to_commit` key in the JSON output, say whether a migration's locks last until its commit.

## Online migrations

On PostgreSQL, the diff can generate the remedies in place of the direct statements. `sustained.autogenerate.autogenerate_migrations(..., online=True)` splits the migration the models generate into two:

- The migration named with the generated id runs in one transaction and changes only the catalog. A new column goes in nullable and without its `UNIQUE` or `REFERENCES` clause. A new `NOT NULL` column whose backfill is a value goes in with that value as its default, which PostgreSQL stores in the catalog without writing a row, and `ALTER COLUMN ... DROP DEFAULT` in the same migration takes the default off again. A new `NOT NULL` column with a default and no backfill goes in with its default, as in the direct form. A new foreign key or check goes in `NOT VALID`, and so does the foreign key a new column's `REFERENCES` declares.
- The migration named `<id>_online` has `transactional=False`, so each of its statements commits on its own and releases its locks when it ends. It runs, in this order: the backfills, as one `UPDATE ... WHERE c IS NULL` each; `CREATE INDEX CONCURRENTLY IF NOT EXISTS` for each new or changed index, after `DROP INDEX CONCURRENTLY IF EXISTS` of the same name, and for a new column's `UNIQUE` a unique index built the same way and attached with `ADD CONSTRAINT ... UNIQUE USING INDEX`; a foreign key `NOT VALID` that points at a key built in the same migration, which cannot go in before the key exists, and a foreign key on a partitioned table; `VALIDATE CONSTRAINT` for each constraint added `NOT VALID`; `SET NOT NULL` through a check, as `DROP CONSTRAINT IF EXISTS <table>_<column>_not_null_check`, `ADD CONSTRAINT <table>_<column>_not_null_check CHECK (c IS NOT NULL) NOT VALID`, the backfill again, `VALIDATE CONSTRAINT`, and `SET NOT NULL`; the drops `allow_drops` generates, each with `IF EXISTS`, and with `DROP INDEX CONCURRENTLY` for an index; and last, `DROP CONSTRAINT IF EXISTS` for each check the `SET NOT NULL` route added.

A migration with no statement is left out, so a run that only adds an index generates `<id>_online` alone. A constraint or index name the diff generates is the name PostgreSQL's `makeObjectName()` gives it, such as `orders_code_key` or `orders_customer_id_fkey`, so the schema reads the same as after the direct form. When the table name, the column name, and the suffix are longer than 63 bytes together, the longer of the two names is shortened and the suffix is kept, so the `_key`, `_fkey`, and `_not_null_check` names of one column stay distinct. The down step of `<id>_online` undoes its statements in the reverse order, with `DROP INDEX CONCURRENTLY` for an index, and leaves the schema the first migration made. It has no statement when `<id>_online` only validates or backfills, and it is missing when `<id>_online` drops a column or a table. The tracking row of a generated migration without a transaction stores `"transactional": false` beside its statements, so a later `down()` runs its down step outside a transaction too.

The NOT NULL example from [Transaction windows](#transaction-windows) adds a column with a value backfill, so it becomes catalog work in `<id>` alone:

```console
20260926_orders_region  transaction
  ALTER TABLE "orders" ADD COLUMN "region" TEXT NOT NULL DEFAULT 'us'
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  transaction  [pg.add_column]
  ALTER TABLE "orders" ALTER COLUMN "region" DROP DEFAULT
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  brief  [pg.alter_column.catalog]
  window  orders: ACCESS EXCLUSIVE from statement 1, ACCESS EXCLUSIVE from statement 2, held to commit
```

When `region` already exists as a nullable column, or its backfill is an `Expression`, the column is set `NOT NULL` through the check in `<id>_online`:

```console
20260926_orders_region_online  no transaction
  UPDATE "orders" SET "region" = 'us' WHERE "region" IS NULL
    orders  ROW EXCLUSIVE  blocks writes  rows  statement  [pg.write_rows]
  ALTER TABLE "orders" DROP CONSTRAINT IF EXISTS "orders_region_not_null_check"
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  brief  [pg.drop_constraint]
  ALTER TABLE "orders" ADD CONSTRAINT "orders_region_not_null_check" CHECK ("region" IS NOT NULL) NOT VALID
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  brief  [pg.add_check.not_valid]
  UPDATE "orders" SET "region" = 'us' WHERE "region" IS NULL
    orders  ROW EXCLUSIVE  blocks writes  rows  statement  [pg.write_rows]
  ALTER TABLE "orders" VALIDATE CONSTRAINT "orders_region_not_null_check"
    orders  SHARE UPDATE EXCLUSIVE  blocks ddl  scan  statement  [pg.validate_constraint]
  ALTER TABLE "orders" ALTER COLUMN "region" SET NOT NULL
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  brief  [pg.set_not_null.proven]
  ALTER TABLE "orders" DROP CONSTRAINT IF EXISTS "orders_region_not_null_check"
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  brief  [pg.drop_constraint]
```

The check goes in after the first backfill: PostgreSQL applies a `NOT VALID` check to every row written after it goes in, so an `UPDATE` of a row whose column is still NULL would fail against it. A row the application writes with a NULL between the first backfill and the check is filled by the second backfill, so `VALIDATE CONSTRAINT` finds no NULL.

Each backfill is one `UPDATE`, which keeps its row locks until it ends. On a large table its `pg.write_rows` finding stays `danger`, and a batched backfill is still a migration you write.

A statement of `<id>_online` that fails leaves the statements before it committed, and the migration's failed row stops the next `up()` until `repair()`. A run after `repair()` starts again from the first statement of `<id>_online`, so each statement runs again over what the failed run left: `DROP INDEX CONCURRENTLY IF EXISTS` drops the invalid index a failed `CREATE INDEX CONCURRENTLY` leaves, before `CREATE INDEX CONCURRENTLY IF NOT EXISTS` builds it again; the `SET NOT NULL` route drops its check before it adds it; and every drop has `IF EXISTS`. The `pg.create_index.concurrently` finding names the invalid index a failed build leaves. `ADD CONSTRAINT ... UNIQUE USING INDEX` and `ADD CONSTRAINT ... FOREIGN KEY` have no `IF NOT EXISTS` form, so a run again after one of them committed fails on the constraint it added. A diff generated after the failure reads what the failed run left: it builds an invalid index again, validates a constraint the catalog marks as not validated, and drops a leftover `SET NOT NULL` check; see [Invalid indexes and constraints not validated](/schema#invalid-indexes-and-constraints-not-validated).

On a partitioned table, PostgreSQL refuses `CREATE INDEX CONCURRENTLY` and `DROP INDEX CONCURRENTLY`. The schema read lists the partitions of each partitioned table, and `<id>_online` builds a new or changed index there in steps: `CREATE INDEX IF NOT EXISTS ... ON ONLY` the partitioned table, which reads no rows and leaves the index invalid; `CREATE INDEX CONCURRENTLY IF NOT EXISTS` on each partition, with the name PostgreSQL gives the index it builds on a partition, such as `orders_a_email_idx`; and `ALTER INDEX ... ATTACH PARTITION` for each, which reads only the catalog. The index on the partitioned table turns valid once an index of every partition is attached. A partition that is partitioned in turn gets an index `ON ONLY` of its own, with the indexes of its partitions attached to it first. An index on a partitioned table is dropped with `DROP INDEX IF EXISTS`. A new column's `UNIQUE` there stays the attached unique index, since PostgreSQL refuses `ADD CONSTRAINT ... USING INDEX` on a partitioned table. A foreign key on a partitioned table goes in validated, in the `<id>_online` step for foreign keys, since PostgreSQL before 18 refuses `NOT VALID` there. A partition index a failed build left invalid is not dropped on a run again, since an index attached to the partitioned table's index cannot be dropped, so the run again fails on the `ATTACH PARTITION` of that index and names it.

`up()`, `rehearse()`, `impact()`, and `preflight()` on either migrator take `online=True` with `models`, and `plan_migrations()` returns the list of migrations the models generate. `plan(online=True)` returns the one migration the split generates, and raises `ValueError` when the split generates two. `autogenerate()` returns one migration and takes no `online`. For the command line, pass `--online` to `plan`, `impact`, `rehearse`, or `migrate`, or set `online = True` in the config module. The flag exits 1 on a dialect other than PostgreSQL, MySQL, and MariaDB.

```python
migrations = migrator.plan_migrations([Order], online=True)
migrator.rehearse(models=[Order], online=True)
applied = migrator.up(models=[Order], online=True)
```

`up()` applies `<id>`, then `<id>_online`, and adds each to the migrator's list after it applies. The guards and the preflight read both before either runs. A rehearsal runs the statements of `<id>_online` inside its transaction with `CONCURRENTLY` removed, since `CREATE INDEX CONCURRENTLY` refuses a transaction block, and its row covers the run `up()` makes with the same models. A traced rehearsal does not trace those statements, so their impact stays static. The drop of the check the `SET NOT NULL` route adds is not labelled by `destructive_statements()`, since the check exists only for that route.

On MySQL and MariaDB, `online=True` does what `assert_algorithm=True` does; see [Asserting the algorithm](#asserting-the-algorithm). Other dialects ignore it.

## Run state

The analysis reads the run in order, and each statement changes what it knows about the next:

- **A table the run created is empty.** No other session can see it yet, so work on it blocks nothing and draws no findings. A plain `CREATE INDEX` on a table created earlier in the run is not flagged. `CREATE TABLE IF NOT EXISTS` creates a table only when the context reads no table of that name, or the run dropped it. Without a schema or size read, the table may exist, so later statements read it as a table of unknown size.
- **A table the run filled from a query has the rows it copied.** After `CREATE TABLE ... AS SELECT`, or an `INSERT ... SELECT` into a table the run created, the table's size is the sum of the sizes of the tables the query reads, so a later `CREATE INDEX` on it is an index build over those rows. The size is unknown when the query reads rows from a function or a VALUES list. A query that reads no table, such as `SELECT 1`, leaves the table empty.
- **A renamed table keeps its identity.** A later statement that names the new name reads the size of the original table. A table the run filled keeps its size under the new name, so after a table swap (`CREATE TABLE big2`, `INSERT INTO big2 SELECT ... FROM big`, `DROP TABLE big`, `ALTER TABLE big2 RENAME TO big`) `big` has the size of the rows copied into it, and a later rewrite of `big` is reported against that size.
- **An index the run created is known.** A `DROP INDEX` names the table the run created the index on. For an index that already exists, the schema read names its table.
- **The run's partitions are known.** On PostgreSQL, a table created with `PARTITION BY` is a partitioned table, and a table created with `PARTITION OF`, or attached with `ATTACH PARTITION`, is a partition of its partitioned table, and its DEFAULT partition when the bound is `DEFAULT`, until `DETACH PARTITION`. A later statement on the partitioned table locks these partitions as it locks the ones the partitions read found, with or without that read. A partitioned table the run created has the size of the partitions attached to it, so after `CREATE TABLE orders_new (...) PARTITION BY RANGE (created_at)` and `ALTER TABLE orders_new ATTACH PARTITION orders ...`, a `CREATE INDEX ON orders_new (customer_id)` is an index build on `orders` that blocks writes. An `INSERT ... SELECT` into a partitioned table counts as filling each partition below it that the run created. `DROP TABLE` of a partitioned table drops the partitions below it, and a rename keeps a table's place among the partitions.
- **A lock timeout stays in scope** for as long as PostgreSQL keeps it: `SET LOCAL` until the migration commits, and `SET` for the rest of the session. A timeout the connection already has is in scope from the first statement. `no_lock_without_timeout()` reads the same scope from the statements alone. On MySQL and MariaDB, `SET`, `SET SESSION`, and `SET LOCAL` all set the session's value for the rest of the run. On SQL Server, `SET LOCK_TIMEOUT` sets it for the rest of the session, inside a transaction or not. A session `SET` inside a transaction replaces a `SET LOCAL` of the same setting. `RESET lock_timeout`, `RESET ALL`, and `DISCARD ALL` end the timeout, and `SELECT set_config('lock_timeout', ...)` sets it as `SET LOCAL` does when its third argument is true and as `SET` does when it is false. On PostgreSQL a `ROLLBACK` undoes a timeout set in the migration, so after it only a session timeout set before the migration began stays in scope.
- **A check can prove a column has no NULL.** After a check whose expression is `c IS NOT NULL` is added without `NOT VALID`, or validated with `VALIDATE CONSTRAINT`, a PostgreSQL `SET NOT NULL` on the column reads as catalog work, until the run drops the check or the column. A valid check the schema read reports proves the same. The run state follows `RENAME CONSTRAINT`, `RENAME COLUMN`, `DROP CONSTRAINT`, and `DROP COLUMN` for both kinds of check, so a check the run dropped proves nothing, and a check proves the column it tests after the run's renames. A table the run created has none of the schema's checks.
- **A table's storage changes what later statements on it can do.** On MySQL, each ALTER TABLE that adds or drops a column instantly uses one of the table's instant row versions, and a rebuild, such as `FORCE`, `ROW_FORMAT=`, or `OPTIMIZE TABLE`, gives them back, so an `ADD COLUMN` after the run has used the last one reads as a rebuild. After `ADD FULLTEXT INDEX`, a later `ADD COLUMN` copies the table on MySQL and rebuilds it on MariaDB, also after the index is dropped, since the hidden `FTS_DOC_ID` column stays. After `ROW_FORMAT=COMPRESSED`, `ADD COLUMN` rebuilds the table. A statement the rules predict the server refuses changes none of these.
- **A ROLLBACK undoes the migration's statements.** On PostgreSQL, SQL Server, SQLite, and DuckDB, a `ROLLBACK` or `ROLLBACK TO SAVEPOINT` in a migration that runs in a transaction undoes what the migration's statements before it did, so the statements after it read the tables as the earlier migrations left them, or as the context read them. After `ALTER TABLE big RENAME TO old`, `CREATE TABLE big (id int)`, and `ROLLBACK`, a `CREATE INDEX` on `big` is an index build over the rows of the table that was there. In a migration with `transactional=False` a `ROLLBACK` undoes nothing, and on MySQL and MariaDB each DDL statement commits, so a `ROLLBACK` there leaves what the analysis knows as it was. A table an `INSERT ... SELECT` filled on MySQL then stays filled, which reports more work than the run does.
- **Session settings change later statements.** After `SET foreign_key_checks = 0`, a MySQL or MariaDB `ADD FOREIGN KEY` is read as the in-place form that checks no rows. `SET GLOBAL` and `SET PERSIST` leave the session's own value unchanged, so they change nothing the analysis reads.

## Generated statements

A statement the diff or a `DdlStep` generated has an intent: what the statement is meant to do, the table and column, and facts only the generator knew, such as the column's type before a type change. The analysis reads the intent first, and reads the text to check that the two agree. When they disagree, the text wins and an `impact.intent_mismatch` finding reports it. When the text cannot be read, as with the batch the SQL Server diff generates to drop a column default whose constraint name it looks up at run time, the analysis follows the intent alone and adds an `impact.from_intent` finding. The intent's details give what they can: for `add_column`, whether the column is nullable and whether it has a default. A fact the details do not give takes its worst case, the finding names it, and the statement's confidence is at most `likely`. An added column's default counts as volatile, so on PostgreSQL, SQL Server, MySQL, and MariaDB a generated `ADD COLUMN ... DEFAULT ...` the analysis cannot read is a rewrite. A foreign key or check counts as added without `NOT VALID`, so it scans the table. A created table's foreign keys are not in the intent, so no lock on the tables they reference is reported.

## Statements the analysis does not read

The analysis recognizes the DDL and DML statements its rules cover. Any other statement, such as `GRANT`, a `DO` block, or a statement written in another engine's syntax, has confidence `unknown` and an `impact.unknown` finding that gives the reason. An unknown statement never counts as safe: every [impact guard](#guards-over-impact) blocks it, and `up(preflight="refuse")` refuses it. A migration string that has more than one statement is also unknown, so write one statement per list entry or per line-ending semicolon in a SQL file.

The analysis reads a statement's text as the engine's server reads it. On MySQL and MariaDB, `#` starts a line comment, `--` starts one only before a space or a control character, and the body of a `/*! ... */` or `/*M! ... */` comment is read as SQL, since the server runs it. Postgres, SQL Server, and DuckDB nest block comments. Each engine's own whitespace separates words: on SQL Server a no-break space separates words, and on Postgres it is part of the word.

A `;` inside a statement makes it unknown, except in the body of the trigger, function, or procedure a `CREATE` statement creates. The body is a dollar-quoted string or `BEGIN ATOMIC ... END` on Postgres and DuckDB, a `BEGIN ... END` block that ends the text on MySQL and SQLite, and on SQL Server everything after the first `AS`, which the server stores as the body. A table or column named `trigger` does not open a body, so `CREATE TABLE t (trigger int); ALTER TABLE big ...` is unknown.

SQL Server runs two statements with no `;` between them. A statement that a word such as `ALTER`, `UPDATE`, or `DROP` follows where its grammar ends, as in `UPDATE t SET a = 1 WHERE id = 1 ALTER TABLE big ...`, is unknown.

A MySQL `UPDATE` that joins or lists several tables, or a `DELETE` that names several tables to delete from, is unknown, since it may write any of them. A `DELETE` whose target is an alias, such as `DELETE a FROM items a JOIN ...`, reads as a delete from `items`. `limited` is set by SQL Server's `TOP`, or on MySQL and SQLite by a `LIMIT` with a number or placeholder at the end of the statement, so a table alias named `limit` does not set it.

A default the rules do not recognize as stable counts as volatile, so `ADD COLUMN ... DEFAULT some_function()` reads as a rewrite, with confidence `likely` and a finding that names the function. A quoted name before `(`, such as `"gen_random_uuid"()`, is a call. A call in a schema other than `pg_catalog`, such as `app.now()`, is a function the rules do not know.

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
| `max_blocking(limit, over_rows=None, over_bytes=None, assume_small=False)` | A statement that blocks more than `limit` on a table past the thresholds. `limit` is `nothing`, `ddl`, `writes`, or `reads_and_writes`. Also a statement with confidence `unknown` |
| `no_rewrite(over_rows=None, over_bytes=None, assume_small=False)` | A statement whose work on a table past the thresholds is `rewrite`, or `unknown`, which ranks above it. Also a statement with confidence `unknown` |
| `lock_timeout_required()` | A statement with a `pg.lock_timeout`, `mysql.lock_timeout`, or `mariadb.lock_timeout` finding: a lock that would queue other sessions, with no timeout in scope. Also a statement with confidence `unknown` |
| `no_unknown_impact()` | A statement with confidence `unknown` |

With neither `over_rows` nor `over_bytes`, every table counts. With either, a table counts when its estimated rows or bytes pass one of them. A table whose size the threshold needs is not known counts as past it, the worst case, unless the rule is given `assume_small=True`, and so does a lock in a statement's `unnamed_locks`, on a table the unread partitions would name; see [Partitioned tables](#partitioned-tables). A table the run created earlier and left empty blocks nothing and is never rewritten, so it never counts. A statement the analysis cannot read, such as a `DO` block, a data-modifying CTE, or a string with two statements, names no table, and may lock or rewrite any table, so all four rules block it, whatever the thresholds. `no_unknown_impact()` blocks only such statements.

Before the guards run, `up()` reads the server facts that `Migrator.impact()` reads, with the sizes of the run's tables only, analyzes the run, and puts each statement's `StatementImpact` on the statement's `impact` attribute. With `models`, the generated migration is analyzed again on its own once the registered migrations have applied, on facts read at that point, so a table they created or renamed has a size. A guard of your own can read it there. `plan` does the same before it runs the guards. A statement that reaches a rule with no `impact`, such as a plain string passed to `run_guards()`, is analyzed on the spot with no server facts, so its sizes are unknown.

When no configured guard reads impact, `up()` prints each `danger` finding on stderr and the run goes on, as it prints `warn` verdicts:

```console
danger: pg.create_index  CREATE INDEX ix_orders_customer ON orders (customer_id): writes to orders wait for the whole index build; build it CONCURRENTLY in a migration with transactional=False
```

`sustained migrate`, `Migrator.up()`, and `AsyncMigrator.up()` print the same lines. A guard counts as reading impact when it has a true `reads_impact` attribute, which the four rules set. On a dialect the analysis does not cover, `up()` reads no server facts, the four rules are silent, and nothing prints.

`no_table_rewrite()` and `index_must_be_concurrent()` read the statement text, and keep their verdicts in 2.x, except that `index_must_be_concurrent()` now passes `CREATE INDEX ... ON ONLY`, which it blocked before; see [The rules](/schema#the-rules). `no_rewrite()` and `max_blocking("ddl")` answer the same questions from the analysis, with the server version and the table sizes.

## Live preflight

A statement that needs a table lock another session already has waits until that session lets it go. On PostgreSQL, MySQL, and MariaDB every later query on the table then waits behind the statement, so one transaction left open by an application can stop every query on the table. The preflight reads, at the moment it runs, the sessions each statement of the run would wait behind. It never ends a session.

```console
$ sustained impact --live
20260926_orders  transaction
  ALTER TABLE orders ADD COLUMN note text
    orders  ACCESS EXCLUSIVE  blocks reads_and_writes  catalog  transaction  ~41.2M rows, 12.4 GB  [pg.add_column]
    ...

1 statement, 0 danger, 1 warn. Evidence: catalog (PostgreSQL 16.4)

preflight
  ALTER TABLE orders ADD COLUMN note text would queue behind pid 4121 (idle in transaction for 42m, user=billing, app=billing-worker, has ACCESS SHARE on orders)
    last statement: SELECT * FROM orders WHERE id = 1
  pid 5003 has had a transaction open for 12m (active, user=report, app=metabase)
  1 blocker, 1 transaction open 60s or longer. Read: locks, transactions
```

A blocker line names the first statement of the run that would wait for the session, the session, its state and how long its transaction has been open, its user and application, and the lock it was granted (`has`) or is waiting for (`waits for`) on the table. A session that is itself waiting for a conflicting lock counts, since the statement queues behind it. Each session appears once per table and lock. The `last statement` line gives the session's current or most recent statement, cut at 200 characters.

The transaction lines list the other transactions open at least `--older-than` seconds, 60 by default, that are not blockers. Such a transaction has no lock the run would wait for yet, but it may take one before the run finishes. The last line counts both and names what was read. A read that failed, for example for lack of a privilege, is named after `Not read:`, and none of its sessions are listed. A statement with confidence `unknown` names no table, so the preflight cannot check the locks it takes: it is listed on a line of its own, `... is not read, so the preflight cannot check the locks it takes`, and counted after `Not checked:`.

What each engine reads:

| Engine | Locks | Transactions | Privilege to see other users' sessions |
| --- | --- | --- | --- |
| PostgreSQL | `pg_locks`, with `pg_stat_activity` and `pg_prepared_xacts` | `pg_stat_activity` and `pg_prepared_xacts`, in the current database | `pg_read_all_stats` for another user's state, transaction age, and statement; without it, another user's transactions are not listed, and its blockers show no state. The locks need none |
| MySQL | `performance_schema.metadata_locks` | `information_schema.INNODB_TRX` and `PROCESSLIST` | `PROCESS`, and `SELECT` on `performance_schema` |
| MariaDB | `performance_schema.metadata_locks` when the Performance Schema is on, and otherwise `information_schema.METADATA_LOCK_INFO`, which the `metadata_lock_info` plugin adds and which lists granted locks only | the same as MySQL | the same as MySQL |
| SQL Server | `sys.dm_tran_locks`, in the current database | `sys.dm_tran_session_transactions` and `sys.dm_exec_sessions`, for sessions in the current database, and `sys.dm_tran_database_transactions`, for sessions whose transaction has work in the current database whatever their own current database | `VIEW SERVER STATE`, or `VIEW SERVER PERFORMANCE STATE` on 2022 and later; `VIEW DATABASE STATE` on Azure SQL Database |

The application is `application_name` on PostgreSQL, the `program_name` connection attribute on MySQL and MariaDB, which only some clients send, and `program_name` on SQL Server. MariaDB ships with the Performance Schema off, so there the locks are read only with `performance_schema = ON` in the server configuration or with the plugin installed.

A statement waits behind a lock that conflicts with its own:

- **PostgreSQL:** the table-level lock conflict table in the documentation. `CREATE INDEX CONCURRENTLY` also waits for every session that writes to the table, as a `SHARE` lock would, and then for every transaction with a snapshot, whatever table it reads, so every open transaction in the database is a blocker for it, named with `would wait for the transaction of pid ... to end`. `REINDEX CONCURRENTLY` waits for every session with a lock on the table and for every snapshot. `DROP INDEX CONCURRENTLY` and `DETACH PARTITION ... CONCURRENTLY` wait for every session with a lock on the table.
- **MySQL and MariaDB:** the metadata lock each statement needs. Every statement the rules name, other than INSERT, UPDATE, and DELETE, needs the `EXCLUSIVE` metadata lock at its start or its end, whatever its `ALGORITHM` and `LOCK`, so it waits for every other metadata lock on the table, including the `SHARED_READ` lock a transaction keeps after one `SELECT` until it commits. INSERT, UPDATE, and DELETE take `SHARED_WRITE`, which waits for `SHARED_NO_WRITE`, `SHARED_NO_READ_WRITE`, `SHARED_READ_ONLY`, and `EXCLUSIVE`.
- **SQL Server:** the lock compatibility matrix. The intent update modes `IU`, `SIU`, and `UIX`, which the matrix in the documentation leaves out, count as conflicting with `S`, `SIX`, and `X`.

The preflight reads table locks only. It reads no row locks, and a lock on a PostgreSQL partition is not matched to its partitioned table. SQLite and DuckDB have no preflight: `impact --live` exits 1 on them.

### Preflight before a run

`up(preflight="warn")` and `up(preflight="refuse")` read the preflight after the guards pass and before any migration applies. `warn` prints each blocker and each transaction open 60 seconds or longer on stderr, and the run goes on:

```console
preflight: ALTER TABLE orders ADD COLUMN note text would queue behind pid 4121 (idle in transaction for 42m, user=billing, app=billing-worker, has ACCESS SHARE on orders)
preflight: pid 5003 has had a transaction open for 12m (active, user=report, app=metabase)
```

`refuse` raises `PreflightBlocked` when there is a blocker, when a read the blockers come from failed, or when a statement is one the preflight cannot check, and prints the transaction lines otherwise. The blockers come from the locks read for every statement that takes a table lock, and on PostgreSQL from the transactions read as well for `CREATE INDEX CONCURRENTLY` and `REINDEX CONCURRENTLY`; `Preflight.needs` names the reads the run's statements need, and `Preflight.missing` the ones of those that failed. So `refuse` refuses on a MariaDB server with the Performance Schema off and no `metadata_lock_info` plugin, where no lock can be read. The error's `preflight` attribute is the whole read, and `sustained migrate` exits 5 for it. `up(preflight=PreflightCheck("warn", older_than=300))` sets another age; `PreflightCheck` lives in `sustained.migrations`. Under `warn`, a read that failed prints `preflight: could not read locks`, and a statement the preflight cannot check prints its `is not read` line, and the run goes on. With `models`, the generated migration is read again once the registered migrations have applied, as the guards read it. The read is a snapshot: a session may take a lock after it and before the statement runs, so `refuse` goes well with a `lock_timeout`. On a dialect without a preflight, SQLite and DuckDB among them, `up()` with a preflight raises `DialectError` before the run starts, as `impact(live=True)` does.

From the command line, `migrate --preflight refuse` or the config module's `preflight = "refuse"` does the same, and `preflight_older_than` sets the age, as `impact --older-than` does:

```python
# sustained_config.py
preflight = "refuse"
preflight_older_than = 300
```

From Python, `Migrator.preflight(models=None, older_than=60.0)` returns the read as a `Preflight`, and `Migrator.impact(live=True)` puts it on the report's `preflight`. `sustained.impact.preflight(connection, dialect, statements)` reads it for any list of statements, and `await async_preflight(adapter, dialect, statements)` through an async adapter:

```python
from sustained.impact import preflight

found = preflight(connection, Dialects.POSTGRES, ["ALTER TABLE orders ADD COLUMN note text"])
for blocker in found.blockers:
    print(blocker.session.label, blocker.held, blocker.session.transaction_seconds)
```

## Observed impact

`sustained rehearse --trace` runs the rehearsal and records what the server did for each statement, and prints the impact report with those facts in place of the prediction. `Migrator.rehearse(trace=True)` puts the report on the result's `impact` attribute, and `await AsyncMigrator.rehearse(trace=True)` does the same. Tracing works on PostgreSQL, MySQL, MariaDB, and SQL Server.

A rehearsal without `scratch=True` runs every pending migration in one transaction on the live database. Every lock a statement takes is kept until the rollback at the end of the rehearsal, the trace's reads included, so a rehearsal of a migration that takes `ACCESS EXCLUSIVE` on a table stops every query on that table for the whole rehearsal. A statement that waits for a lock waits without a limit, and the queries queued behind it wait with it. `rehearse(lock_timeout=seconds)`, or `rehearsal_lock_timeout` in the config module for `sustained rehearse`, sets the dialect's lock timeout for the rehearsal, and a statement that waits longer fails the rehearsal. On PostgreSQL that is `SET LOCAL lock_timeout`, which the rollback ends.

### On PostgreSQL

The rehearsal runs each statement of each up step on its own. Before and after each statement it reads two things inside the rehearsal transaction:

- the table locks the transaction has, from `pg_locks` for its own backend. Locks last until the rollback, so a lock the statement took is one held after it and not before.
- the file of each table the statement names and of each of the table's indexes, from `pg_relation_filenode()` and `pg_relation_size()`. A table whose file changed and still has data was rewritten. An index that is new, or whose file changed, was built.

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

### On SQL Server

SQL Server rehearses on a scratch database, so a traced rehearsal needs `rehearse(scratch=True, trace=True)`, or a `get_rehearsal_connection()` in the config module for `sustained rehearse --trace`. The rehearsal runs each statement of each up step on its own inside the rehearsal transaction, and before and after each statement reads three things for its own session:

- the table locks granted to the transaction, from `sys.dm_tran_locks`. Locks are held until the rollback, so a lock the statement took is one held after it and not before. A lock held only while the statement runs, such as the `Sch-S` of `UPDATE STATISTICS`, is gone by the second read, so a predicted lock that was not seen is not a mismatch.
- the partitions of each table the statement names, and of each of its indexes, with their used pages, from `sys.partitions` and `sys.allocation_units`. A heap or clustered index whose partition changed and still has pages was copied. An index whose partition is new or changed was built. A partition that moved from another named table, as `SWITCH` moves it, is no copy.
- the log the transaction has written, from `sys.dm_tran_database_transactions`. An `ALTER COLUMN` that updates every row in place keeps the table's partitions, so a statement that writes at least twice as much log as the table's heap or clustered index takes up, and at least 64 KB, counts as a rewrite. A write of rows logs every row it changes, so its log does not count as a copy.

The observed lock and work replace the predicted ones, and each difference is an `impact.mismatch` finding, as on PostgreSQL. A lock of `S` or stronger on a table no rule named is a mismatch too. The observation cannot tell a scan from a catalog change, so a predicted scan stands unless a copy was seen, and a predicted copy that copied nothing falls to `scan`. A table the run created earlier is left as predicted, and so is a migration the rehearsal leaves out, or a callable step. A read that fails leaves its statement's facts as predicted, and the rehearsal goes on.

### Mismatches

A mismatch does not change the exit code of `rehearse`. `--trace` needs PostgreSQL, MySQL, MariaDB, or SQL Server, and `rehearse(trace=True)` raises `DialectError` on any other dialect.

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
| `ALTER COLUMN ... TYPE` between `timestamp` and `timestamptz` under a UTC `TimeZone`, or a change of the collation of an indexed column | `ACCESS EXCLUSIVE` | index build of each index on the column | `pg.alter_column_type.index_rebuild` |
| `SET NOT NULL` | `ACCESS EXCLUSIVE` | scan | `pg.set_not_null` |
| `SET NOT NULL` on a column a valid `CHECK (c IS NOT NULL)` proves | `ACCESS EXCLUSIVE` | catalog | `pg.set_not_null.proven` |
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
| A statement that drops a foreign key: `DROP TABLE`, `DROP CONSTRAINT`, or `DROP COLUMN` on the table that has the key, or with `CASCADE` on the table it points at. A type change of a column a key uses or points at re-creates the key. | `ACCESS EXCLUSIVE` on the table at the key's other end | catalog, or a scan of the table that has a re-created key | `pg.drop_foreign_key` |
| `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE`, plus `ROW SHARE` on the table a foreign key points at | scan | `pg.validate_constraint` |
| `RENAME`, `RENAME COLUMN`, `RENAME CONSTRAINT` | `ACCESS EXCLUSIVE` | catalog | `pg.rename` |
| `ATTACH PARTITION` | `SHARE UPDATE EXCLUSIVE` on the parent, `ACCESS EXCLUSIVE` on the partition, its partitions, and the parent's DEFAULT partition | scan of the partition and the DEFAULT partition, and an index build on the partition when the parent has an index | `pg.attach_partition` |
| `DETACH PARTITION` | `ACCESS EXCLUSIVE` on both, the partition's partitions, and the parent's DEFAULT partition | catalog | `pg.detach_partition` |
| `DETACH PARTITION ... CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` on both | catalog | `pg.detach_partition.concurrently` |
| `SET TABLESPACE`, `SET LOGGED`, `SET UNLOGGED` | `ACCESS EXCLUSIVE` | rewrite, or catalog on a partitioned table, which has no file | `pg.table_rewrite` |
| `SET SCHEMA`, `OWNER TO`, row level security, other storage parameters | `ACCESS EXCLUSIVE` | catalog | `pg.alter_table.catalog` |
| `SET (fillfactor = ...)` and the other parameters that take the weaker lock | `SHARE UPDATE EXCLUSIVE` | catalog | `pg.set_parameters` |
| `ENABLE TRIGGER`, `DISABLE TRIGGER` | `SHARE ROW EXCLUSIVE` | catalog | `pg.alter_trigger` |
| `CREATE INDEX` | `SHARE` | index build | `pg.create_index` |
| `CREATE INDEX ... ON ONLY` a partitioned table | `SHARE` on the partitioned table | catalog | `pg.create_index` |
| `CREATE INDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` | index build | `pg.create_index.concurrently` |
| `DROP INDEX` | `ACCESS EXCLUSIVE` on the table | catalog | `pg.drop_index` |
| `DROP INDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` on the table | catalog | `pg.drop_index.concurrently` |
| `ALTER INDEX ... ATTACH PARTITION` | `ACCESS SHARE` on the partitioned table and on the partition, and `ACCESS EXCLUSIVE` on the partition's index, so reads and writes on the partition wait | catalog | `pg.attach_index` |
| `CREATE TABLE ... REFERENCES`, `CREATE TABLE ... PARTITION OF` | `SHARE ROW EXCLUSIVE` on the referenced table, `ACCESS EXCLUSIVE` on the parent and its DEFAULT partition | catalog, and a scan of the DEFAULT partition | `pg.create_table` |
| `DROP TABLE`, `TRUNCATE` | `ACCESS EXCLUSIVE`, with `TRUNCATE ... CASCADE` on every table it empties through a foreign key, and on the partitions of a partitioned table. Dropping a partition also locks its partitioned table and the DEFAULT partition. | catalog | `pg.drop_table` |
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

A type change is binary coercible when PostgreSQL skips the rewrite: `varchar(n)` to a longer `varchar` or to `text`, `numeric(p,s)` to a wider precision at the same scale, and an array such as `varchar(10)[]` to `varchar[]`, which drops the length of its elements. Any other change to or from an array type converts each element and rewrites the table, including `varchar(10)[]` to `varchar(20)[]` and `text[]` to `varchar[]`. The rule needs the column's current type, which a generated statement's intent gives, and which the schema read gives for a hand-written statement. The schema read reports an array column as `ARRAY`, so the current type of an array column comes from the `arrays` read. Without either, the change reads as a rewrite with confidence `likely`.

`timestamp` to `timestamptz`, and `timestamptz` to `timestamp`, leave the rows as they are when the `TimeZone` setting is UTC, and rebuild each index on the column, because the two types sort by different operator classes. Under another time zone the change rewrites the table. Without the `TimeZone` setting, it reads as a rewrite with confidence `likely`. A type change that gives an indexed column another collation also rebuilds each index on it, expression indexes included. A `text`, `varchar`, or `char` type without `COLLATE` gives the column the default collation, so `varchar(200)` on a column with `COLLATE "C"` rebuilds its indexes. The `indexes` read gives the columns each index uses and their collations. Both cases are `pg.alter_column_type.index_rebuild`.

`ADD COLUMN` of a domain with a NOT NULL or a CHECK, of its own or on the domain it is over, rewrites the table, because PostgreSQL checks the constraint against every row. The `types` read says which types are such domains. A type that read did not find, such as a domain an earlier statement of the same run creates, reads as a rewrite with confidence `likely`. A domain without a constraint, and an array of a domain, change only the catalog.

A `SET NOT NULL` skips its scan when a valid check constraint already proves the column has no NULL. The remedy for a scan on a populated table is that route: add the check `NOT VALID`, validate it, set `NOT NULL`, and drop the check. The analysis reads the route as catalog work: a check whose expression is `c IS NOT NULL`, with or without parentheses around it, proves the column once the run adds it without `NOT VALID` or validates it, until the run drops the check or the column. A check the schema read finds proves it too, unless PostgreSQL reports it `NOT VALID`.

The tables at the other end of a foreign key come from the schema read. Without it, a `DROP TABLE` or a `DROP CONSTRAINT` reports only the table it names, and a `DROP INDEX` reports its table as `(table of index ix)`. `VALIDATE CONSTRAINT` of a foreign key takes `ROW SHARE` on the table the key points at. A constraint the schema read does not list as a foreign key or a check reads with confidence `likely`.

### Partitioned tables

A partitioned table stores no rows, and PostgreSQL applies a statement that reaches rows to each partition below it, partitions of partitions included. The `partitions` read names them, and the report lists each partition with the lock and work the statement takes there, and a blocking-work finding for each:

- `CREATE INDEX` takes `SHARE` on every partition and builds the index on each. `CREATE INDEX ... ON ONLY` the partitioned table creates an invalid index on it alone, which is catalog work. The finding advises that route: create the index `ON ONLY` the partitioned table, build a matching index on each partition `CONCURRENTLY`, and attach each one with `ALTER INDEX ... ATTACH PARTITION`.
- An `ALTER TABLE` action takes its lock on every partition unless the statement says `ONLY`. A rename of the table, `SET SCHEMA`, `OWNER TO`, row level security, `SET TABLESPACE`, `SET LOGGED`, `SET UNLOGGED`, and storage parameters lock the partitioned table alone.
- `DROP TABLE`, `TRUNCATE`, `DROP INDEX` of a partitioned index, `REINDEX`, `VACUUM`, `ANALYZE`, `CREATE TRIGGER`, and `DROP TRIGGER` lock each partition. `LOCK TABLE` does too, unless it says `ONLY`.
- `CREATE TABLE ... PARTITION OF`, `ATTACH PARTITION`, and `DETACH PARTITION` lock the partitioned table's DEFAULT partition `ACCESS EXCLUSIVE`, and the first two scan it, because PostgreSQL checks its rows against the new bound. `DROP TABLE` of a partition locks its partitioned table and the DEFAULT partition.
- `ATTACH PARTITION` builds an index on the new partition for each index of the partitioned table the partition has no match for. The read does not compare indexes, so the report gives an index build with confidence `likely` whenever the partitioned table has an index.

A partition an earlier statement of the run creates or attaches is locked by a later statement on its partitioned table, and a partition it detaches is not; see [Run state](#run-state). A statement-level `CREATE TRIGGER` on a partitioned table locks the partitioned table alone, and the report still lists each partition.

Without the partitions read, as without a context, the report lists the tables the statement names and no partition. Each statement above has confidence `likely`, `partitions_unread` set, and one `info` finding, `pg.partitions_unread`, that says it is not known whether each table is a partitioned table or a partition, and names what the statement also locks or is refused if it is: each partition below it, with the lock the statement takes on the table, the DEFAULT partition and the scan of its rows, or the partitioned table and its DEFAULT partition. It also names the refusals the partitions decide: `CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`, `REINDEX` inside a transaction block, `DETACH PARTITION ... CONCURRENTLY` beside a DEFAULT partition, a `NOT VALID` foreign key before 18, and an exclusion constraint before 17. A statement that says `ONLY`, an `ALTER TABLE` of only the actions that lock the partitioned table alone, a write of rows, and a statement on a table the run created have no such finding. A partition the run attached below a table, and did not create, has the finding for the partitions below it, which the read did not cover.

A partition takes the lock the statement takes on its partitioned table, and a partitioned table's size is the sum of its partitions', so a guard's verdict on the table the statement names covers the partitions below it. The DEFAULT partition an `ATTACH PARTITION` scans, and the partitioned table a `DROP TABLE` of a partition locks, can be larger than the tables the statement names. The statement's `unnamed_locks` gives the lock, what it blocks, and the work on each, and `max_blocking()` and `no_rewrite()` count each one as a table of unknown size: past a threshold unless the rule is given `assume_small=True`. So `max_blocking("ddl", over_rows=1000)` blocks an `ATTACH PARTITION` of a small partition when the partitions were not read, and passes it when they were read and show no DEFAULT partition. `lock_timeout_required()` needs nothing more, since the tables the statement names take the same locks, and `no_unknown_impact()` does not block a statement for confidence `likely`, as with the other facts a failed read leaves out.

### Statements the server refuses

A statement PostgreSQL refuses to run is a `danger` finding under the statement's rule. These cannot run inside a transaction block, so they are `danger` in a migration that runs inside one: `CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`, `REINDEX CONCURRENTLY`, `DETACH PARTITION ... CONCURRENTLY`, `VACUUM`, `REINDEX SCHEMA`, `DATABASE`, or `SYSTEM`, `REINDEX` of a partitioned table or index, and `CLUSTER` without a table. The finding advises a migration with `transactional=False`. These are refused wherever they run:

- `CREATE INDEX CONCURRENTLY` and `DROP INDEX CONCURRENTLY` on a partitioned table or index
- `DETACH PARTITION ... CONCURRENTLY` from a partitioned table with a DEFAULT partition
- a `NOT VALID` foreign key on a partitioned table, before PostgreSQL 18
- an exclusion constraint on a partitioned table, before PostgreSQL 17

The integration suite checks every rule against PostgreSQL 14 and 18. It creates the tables the rules' fixture statements name, runs each fixture alone in a transaction under the same reads as `rehearse --trace`, and fails on any `impact.mismatch`. A fixture that PostgreSQL refuses inside a transaction block, such as `CREATE INDEX CONCURRENTLY` or `VACUUM`, is not observed. Neither is `SET TABLESPACE`, because the test servers have no second tablespace to move a table to. A fixture the server refuses on that version is run outside a transaction, and the suite checks that the server raises and that the report gives a `danger` finding under the fixture's rule. The suite also checks that each statement the rules call refused inside a transaction block raises there.

### Remedies

| Statement | Remedy |
| --- | --- |
| `CREATE INDEX` | `CREATE INDEX CONCURRENTLY` in a migration with `transactional=False`. A failed concurrent build leaves an invalid index behind, which has to be dropped. On a partitioned table, which refuses `CONCURRENTLY`, the finding names the `ON ONLY` route and the report generates no statement. |
| `DROP INDEX` | `DROP INDEX CONCURRENTLY` in a migration with `transactional=False` |
| `ADD FOREIGN KEY` | `ADD ... NOT VALID`, then `VALIDATE CONSTRAINT` in a later migration |
| `ADD CHECK` | `ADD ... NOT VALID`, then `VALIDATE CONSTRAINT` |
| `SET NOT NULL` | `ADD CHECK (c IS NOT NULL) NOT VALID`, `VALIDATE CONSTRAINT`, `SET NOT NULL`, then drop the check |
| `ADD PRIMARY KEY`, `ADD UNIQUE` | `CREATE UNIQUE INDEX CONCURRENTLY`, then `ADD CONSTRAINT ... USING INDEX` |
| `ADD COLUMN` with a volatile default | Add the column without a default, set the default, then backfill in batches |
| A type change that rewrites | Add a new column, write to both, backfill, and swap. The finding names the steps and generates none of them. |
| `REINDEX` | `REINDEX CONCURRENTLY` |
| `REFRESH MATERIALIZED VIEW` | `REFRESH MATERIALIZED VIEW CONCURRENTLY`, which needs a unique index on the view |
| `UPDATE`, `DELETE` over a large table | A batched backfill, or for a `DELETE` a batched delete, outside the DDL migration |
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

Every ALTER TABLE, and every statement reported with `MDL EXCLUSIVE`, takes the table's exclusive metadata lock, at least at its start and its end. The lock waits behind every open transaction that has read the table, and every later query on the table waits behind it. So each of these statements draws a `mysql.lock_timeout` or `mariadb.lock_timeout` finding whatever its LOCK level, unless a timeout is in scope. The remedy is `SET SESSION lock_wait_timeout = 5`, in seconds. A value of a day or more counts as no timeout: MySQL's default is a year, and MariaDB's global default is a day. `INSERT`, `UPDATE`, and `DELETE` wait for row locks, which `innodb_lock_wait_timeout` bounds, so they draw no timeout finding. On MariaDB, a statement with `WAIT n` under a day, or `NOWAIT`, bounds its own wait for the metadata lock, so it draws no finding either.

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
| `DROP COLUMN` on a table with a FULLTEXT index | `COPY, LOCK=SHARED` | `INPLACE, LOCK=SHARED` | rewrite | `drop_column.rebuild` |
| An instant `ADD COLUMN` or `DROP COLUMN`, or MariaDB's instant `MODIFY ... FIRST` or `AFTER`, in one ALTER TABLE with a change that is not instant, such as `ADD INDEX` | `INPLACE`, at the other change's LOCK level | `INPLACE`, at the other change's LOCK level | rewrite | `add_column.rebuild`, `drop_column.rebuild`, `modify_column.rebuild` |
| An instant `ADD COLUMN` or `DROP COLUMN` with `ALGORITHM=INPLACE` | `INPLACE, LOCK=NONE` | `INSTANT`, since MariaDB reads `ALGORITHM` as the slowest algorithm the statement accepts | rewrite on MySQL | `add_column.rebuild`, `drop_column.rebuild` |
| `MODIFY` or `CHANGE` that keeps the type and the nullability, and changes the default, the comment, or adds ENUM or SET members at the end | `INSTANT` | `INSTANT` | catalog | `modify_column.instant` |
| `MODIFY` that widens a VARCHAR and keeps its length prefix | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `modify_column.inplace`, `modify_column.instant` |
| `MODIFY` that widens a VARCHAR past its length prefix | `COPY, LOCK=SHARED` | `INSTANT`, or `COPY` when the longest value took 128 to 255 bytes and the table's rows are not `REDUNDANT` | rewrite, or catalog when instant | `modify_column.copy`, `modify_column.instant` |
| `MODIFY` or `CHANGE` to another collation of the same character set, or from utf8mb3 to utf8mb4 | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `modify_column.inplace`, `modify_column.instant` |
| The same on a column in an index | `COPY, LOCK=SHARED` | `NOCOPY, LOCK=NONE` | rewrite on MySQL, index build on MariaDB | `modify_column.copy`, `modify_column.rebuild` |
| `MODIFY` or `CHANGE` to another character set, or from utf8mb3 to utf8mb4 on a VARCHAR whose longest value passes 255 bytes | `COPY, LOCK=SHARED` | `COPY` | rewrite | `modify_column.copy` |
| `MODIFY` that changes NULL to NOT NULL, or back | `INPLACE, LOCK=NONE` | `INPLACE, LOCK=NONE` | rewrite | `modify_column.rebuild` |
| `MODIFY ... FIRST` or `AFTER` | `INPLACE, LOCK=NONE` | `INSTANT` | rewrite on MySQL | `modify_column.rebuild`, `modify_column.instant` |
| `MODIFY` or `CHANGE` to another type, a shorter VARCHAR, reordered ENUM members, or AUTO_INCREMENT | `COPY, LOCK=SHARED` | `COPY` | rewrite | `modify_column.copy` |
| `ALTER COLUMN ... SET DEFAULT`, `DROP DEFAULT`, `SET VISIBLE`, `SET INVISIBLE` | `INSTANT` | `INSTANT` | catalog | `column_default` |
| `RENAME COLUMN`, `CHANGE` to a new name only | `INSTANT` from 8.0.28, `INPLACE, LOCK=NONE` before | `INSTANT` | catalog | `rename` |
| `RENAME TO`; `RENAME TABLE` | `INSTANT`; `MDL EXCLUSIVE` | `INSTANT`; `MDL EXCLUSIVE` | catalog | `rename` |
| `RENAME INDEX` | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `rename_index` |
| `CREATE INDEX`, `ADD INDEX`, `ADD UNIQUE`, also with `USING BTREE`, or with `USING HASH` other than on a MariaDB unique key | `INPLACE, LOCK=NONE` | `NOCOPY, LOCK=NONE` | index build | `add_index` |
| `ADD UNIQUE` or `CREATE UNIQUE INDEX` with `USING HASH` | `INPLACE, LOCK=NONE` | `COPY`, since MariaDB keeps the key in a hidden generated column | index build on MySQL, rewrite on MariaDB | `add_index` |
| `ADD SPATIAL INDEX`, `CREATE SPATIAL INDEX` | `INPLACE, LOCK=SHARED` | `NOCOPY, LOCK=SHARED` | index build | `add_spatial` |
| A FULLTEXT index | `INPLACE, LOCK=SHARED` | `INPLACE, LOCK=SHARED` for the table's first, `NOCOPY, LOCK=SHARED` after | rewrite for the table's first, which adds a hidden `FTS_DOC_ID` column; index build after | `add_fulltext` |
| `DROP INDEX` | `INPLACE, LOCK=NONE` | `NOCOPY, LOCK=NONE` | catalog | `drop_index` |
| `ALTER INDEX ... VISIBLE` or `INVISIBLE` on MySQL, `ALTER INDEX ... IGNORED` or `NOT IGNORED` on MariaDB | `INPLACE, LOCK=NONE` | `INSTANT` | catalog | `index_visibility` |
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

`COPY` runs with `LOCK=NONE` on MariaDB 11.2 and later, so writes go on while the table is copied. On MySQL, on MariaDB before 11.2, and under MariaDB's `ALTER IGNORE TABLE`, it runs with `LOCK=SHARED`. `OPTIMIZE TABLE` on a table with a FULLTEXT index copies it.

MySQL also takes the exclusive metadata lock on the parent table of a foreign key the statement adds or drops: `ADD FOREIGN KEY`, `DROP FOREIGN KEY`, `CREATE TABLE ... REFERENCES`, and `DROP TABLE` of a table that has a foreign key. The report lists the parent with `MDL EXCLUSIVE` under the rule `mysql.foreign_key_parent`. The parent of a dropped key comes from the schema read. MariaDB takes no such lock.

A `MODIFY` or `CHANGE` restates the whole column, so the rules compare it with the column's current definition, from the intent of a statement the diff generated or from the schema read. That includes the column's collation: a restated text column that names no `CHARACTER SET` or `COLLATE` takes the table's default collation, so on a column of another character set it converts every row. A `CHARACTER SET` named without `COLLATE` takes the character set's default collation, which the rules know for latin1 and ascii, and on MySQL for utf8mb3 and utf8mb4; for another, a change that is not instant has confidence `likely`. Without the table's default collation, a restated column that names neither counts as a copy, with confidence `likely`. Without either, the change counts as a type change: `COPY`, with confidence `likely`. A VARCHAR stores each value's length in one byte while its longest value fits in 255 bytes, and in two above that, so whether a widened VARCHAR keeps its length prefix depends on the character set. Without the column's collation, the rules assume utf8mb4, four bytes to a character.

Without a server read, the storage facts are unknown, so a change that is instant on most tables reads as `INSTANT` with confidence `likely`, and a finding names the facts that would rule it out: a FULLTEXT index, `ROW_FORMAT=COMPRESSED`, or used-up instant row versions. Without the schema read, `DROP COLUMN` reads as instant with confidence `likely`, since an indexed column is dropped in place. `DROP TRIGGER` names no table, and the schema read reads no triggers, so it reports no table, with confidence `likely`.

A statement the rules do not read, such as `ANALYZE TABLE` or `LOCK TABLES`, is unknown.

The `mariadb` rules read MariaDB's own syntax. `ALTER ONLINE TABLE` asks for `LOCK=NONE`, so a change that needs a stronger lock, such as a FULLTEXT or SPATIAL index, draws `mariadb.refused`. Under `ALTER IGNORE TABLE`, a new unique key or primary key drops the rows that repeat it by copying the table. `WAIT n` and `NOWAIT` on ALTER TABLE, CREATE INDEX, and DROP INDEX are read with the statement. MySQL accepts none of these, so the `mysql` rules read a statement that spells one as unknown. Each server also refuses the other's `ALTER INDEX` form, so the `mysql` rules read `IGNORED` as unknown, and the `mariadb` rules read `VISIBLE` and `INVISIBLE` as unknown.

### Asserting the algorithm

For each ALTER TABLE, CREATE INDEX, or DROP INDEX the rules read as `INSTANT`, or as in place with `LOCK=NONE`, an `info` finding gives the reason and offers the statement with that clause added:

```console
  ALTER TABLE orders ADD COLUMN note text
    orders  INSTANT  blocks reads_and_writes  catalog  brief  ~41.2M rows, 12.4 GB  [mysql.add_column.instant]
    info    the column is added in the data dictionary; assert INSTANT so the server refuses the statement instead of running it with a slower algorithm or a stronger lock
    fix     ALTER TABLE orders ADD COLUMN note text, ALGORITHM=INSTANT
```

With the clause, the server refuses the statement when it cannot run it that way. Without it, the server falls back to a slower algorithm or a stronger lock, as when a table has used its instant row versions. MySQL refuses a LOCK clause beside `ALGORITHM=INSTANT`, so the instant form names the algorithm alone. The clause goes after the statement's last token, before any trailing comment, since a `-- comment` at the end would otherwise hide it. MariaDB's DROP INDEX takes no clause, so there the finding offers the `ALTER TABLE ... DROP INDEX` form.

`assert_algorithm=True` writes that clause on the migration the models generate, so the server refuses a statement it cannot run as predicted instead of falling back. `plan()`, `up()`, `rehearse()`, and `impact()` on either migrator take it, and so does `sustained.autogenerate.autogenerate()`. For the command line, pass `--assert-algorithm` to `plan`, `impact`, `migrate`, or `rehearse`, or set `assert_algorithm = True` in the config module. The flag exits 1 on a dialect other than MySQL and MariaDB. The rules read the server facts from the connection after the diff, as `impact()` reads them. A statement takes the clause when all of these are true:

- it is an ALTER TABLE, CREATE INDEX, or DROP INDEX that spells neither `ALGORITHM` nor `LOCK`
- the rules predict `INSTANT`, `NOCOPY, LOCK=NONE`, or `INPLACE, LOCK=NONE` for it, with confidence `known`
- its table existed before the migration

A prediction of `COPY` or of a `SHARED` or `EXCLUSIVE` lock is left without a clause, and so is every statement when the read could not get the server's version. The down step is left as generated. Other dialects ignore the option.

```python
migration = migrator.plan([Order], assert_algorithm=True)
applied = migrator.up(models=[Order], assert_algorithm=True)
```

The clause changes the SQL text, so a rehearsal row recorded without `assert_algorithm` does not cover a run with it, and a run with it needs a rehearsal with it. A scratch rehearsal predicts from the scratch tables, so a scratch table with a different row format, FULLTEXT indexes, or instant row versions from the real table can predict another clause, and its row then covers other statements.

A statement that spells `ALGORITHM` or `LOCK` is read with them. A heavier algorithm or a stronger lock runs as asked. `ALGORITHM=COPY` copies the table at the LOCK level COPY takes, so `ALGORITHM=COPY, LOCK=NONE` is refused on MySQL, on MariaDB before 11.2, and under `ALTER IGNORE TABLE`. On MySQL, `ALGORITHM=INPLACE` over an instant `ADD COLUMN` or `DROP COLUMN` rebuilds the table; MariaDB reads `ALGORITHM` as the slowest algorithm it accepts, and runs a change a faster algorithm can run as it would without the clause. MySQL runs a `LOCK` clause without `ALGORITHM` in place, never instant. An algorithm or a LOCK level the change cannot run with draws a `mysql.refused` or `mariadb.refused` finding with severity `warn`, since the server refuses the statement, and the table is reported with no lock.

A statement that copies the table while writes wait names an online schema change tool, such as gh-ost or pt-online-schema-change, which copies the table without blocking writes.

The integration suite checks the rules against MySQL 8.4 and 26.7 and MariaDB 11.4 and 12.3. It checks the facts `read_context()` reads, and the parent-table locks, by running each foreign key statement while a second session reads the parent. It creates the tables the rules' fixture statements name in a database of their own, named after the scratch database, runs each fixture alone under the probe of `rehearse --trace`, and fails on any `impact.mismatch`. It creates the database again for each fixture, since MySQL schema changes do not roll back. It also runs short lists of statements under the probe in order, each predicted from one read before the first: `ADD FULLTEXT INDEX` then `ADD COLUMN`, `ROW_FORMAT=COMPRESSED` then `ADD COLUMN`, and on MySQL two `ADD COLUMN` statements on a table one instant row version short of the limit. A fixture that spells its own clause must run, or be refused when the rules predict a refusal.

## SQL Server

The rules follow the SQL Server documentation for 2012 and later. Rule ids start with `mssql.`.

### Lock modes

Each table line names the table lock mode the statement has when it ends, as `sys.dm_tran_locks` reports it. What each mode blocks follows the lock compatibility matrix under the default locking READ COMMITTED isolation:

| Lock | Taken by | Blocks |
| --- | --- | --- |
| `Sch-S` | `UPDATE STATISTICS`, and an online index operation or `ALTER COLUMN` while it works | `ddl` |
| `IX` | `INSERT`, `UPDATE`, and `DELETE`, with an `X` lock on each row they change | `ddl` on the table. The table line reports `reads_and_writes` for the rows they change, or `writes` when the database reads with `READ_COMMITTED_SNAPSHOT` on |
| `S` | `CREATE INDEX` of a nonclustered index | `writes` |
| `X` | a write whose row locks escalate to the table, and `ALTER INDEX ... REORGANIZE` inside a transaction | `reads_and_writes`, or `writes` when the database reads with `READ_COMMITTED_SNAPSHOT` on |
| `Sch-M` | every `ALTER TABLE`, `CREATE CLUSTERED INDEX`, `DROP INDEX`, `ALTER INDEX ... REBUILD` and `DISABLE`, `sp_rename`, `TRUNCATE TABLE`, `DROP TABLE`, and triggers | `reads_and_writes` |

With `READ_COMMITTED_SNAPSHOT` on, readers read the last committed version of each row and take only `Sch-S`, so an `X` lock no longer blocks them, and `Sch-M` still does. Without a read of the setting, the rules assume it is off.

SQL Server DDL is transactional, so inside a migration's transaction every lock is held until the commit. A foreign key that a statement adds, drops, checks, or disables takes `Sch-M` on the table it points at as well, and so does `DROP TABLE` of a table whose foreign keys point at others.

SQL Server escalates the row locks of one statement to `X` on the whole table once the statement has 5,000 locks on it. An `UPDATE` or `DELETE` with no `WHERE` and no `TOP`, on a table of 5,000 rows or more, reports `X` under `mssql.lock_escalation`. Without the table's row count it reports `X` with confidence `likely`. Any other `UPDATE` or `DELETE` reports `IX`, and one without `TOP` gets an `info` finding that a write of 5,000 rows or more escalates. An `INSERT ... SELECT` reports `IX` with an `info` finding under `mssql.lock_escalation` that it can escalate, since the analysis does not count the rows the query gives. SQL Server 2022 and 2025 were observed to keep `IX` for an `INSERT ... SELECT` of 6,000 rows and to escalate at 6,500 rows. An `INSERT ... VALUES` gets no finding.

### Editions

The Enterprise, Developer, and Evaluation editions, Azure SQL Database, and Azure SQL Managed Instance run index operations and `ALTER COLUMN` with `ONLINE = ON`, and add a NOT NULL column with a runtime constant default as a catalog change. The Standard, Web, and Express editions write the default into every row, and refuse `ONLINE = ON`. Every edition writes into every row the default of a large value type (`nvarchar(max)`, `varchar(max)`, `varbinary(max)`), `xml`, `text`, `ntext`, `image`, `hierarchyid`, `geography`, `geometry`, `json`, or a CLR type, and the value of a new `rowversion` or `timestamp` column, with or without NULL. The rules read `SERVERPROPERTY('EngineEdition')`, and without it assume an edition without these features:

- `ADD` of a NOT NULL column with a constant default, or of a default `WITH VALUES`, is a `rewrite` with confidence `likely`, and the finding names both cases
- a statement with `ONLINE = ON` gets an `info` finding that it fails on the other editions; on an edition that was read to lack it, the finding is `danger`
- the remedy offers `ONLINE = ON` unless the edition was read to lack it

An operation with `ONLINE = ON` takes `S` on the table when it starts, works with `Sch-S`, so reads and writes go on, and takes `S`, or `Sch-M` for a clustered index, a key, a rebuild, or `ALTER COLUMN`, on the table when it ends. The locks at the start and at the end wait for the open transactions that conflict with them, and new queries on the table wait behind them. Inside a transaction, the lock at the end is held until the migration commits, so the table line names it with the work, and the finding is `info` whatever the table's size. The finding says to run the statement in a migration with `transactional=False`, or last in its migration. In a migration with `transactional=False`, the table line names the lock at the end, blocking `ddl`, so the statement draws the `mssql.lock_timeout` finding and the live preflight checks the sessions it would wait behind. An `info` finding says what the lock waits for, and offers `WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)` in the `ONLINE = ON` of `ALTER INDEX ... REBUILD` and `ALTER TABLE ... REBUILD` on 2014 and later and of `CREATE INDEX` on 2022 and later. SQL Server refuses it in `ADD CONSTRAINT` and `ALTER COLUMN`.

A lock request with `WAIT_AT_LOW_PRIORITY` waits beside the lock queue, so later queries on the table go ahead of it. With `ABORT_AFTER_WAIT = SELF` or `BLOCKERS` it never joins the queue, and the statement draws no `mssql.lock_timeout` finding, inside a transaction or not; `SET LOCK_TIMEOUT` does not end a low-priority wait, and `MAX_DURATION` does. With `ABORT_AFTER_WAIT = NONE` the request joins the queue once `MAX_DURATION` has passed, and the finding stays. `ALTER TABLE ... SWITCH` with `WAIT_AT_LOW_PRIORITY` follows the same rules.

`RESUMABLE = ON` fails inside a transaction, with error 574, and without `ONLINE = ON`, with error 11438. Either case gets a `danger` finding under `mssql.resumable`, and the first says to run the statement in a migration with `transactional=False`.

### Lock timeouts on SQL Server

A statement waiting for a lock queues every later lock request on the table that conflicts with it, so every statement whose table lock is `S`, `SIX`, `X`, or `Sch-M` draws an `mssql.lock_timeout` finding without a `LOCK_TIMEOUT` of 0 or more in scope. The `IX` of a write draws none. The remedy is `SET LOCK_TIMEOUT 5000`, in milliseconds. `SET LOCK_TIMEOUT` lasts for the rest of the session, inside a transaction or not. A `LOCK_TIMEOUT` the connection already has, read from `@@LOCK_TIMEOUT`, covers the whole run; the default, -1, waits without a limit. A statement that runs out of time fails with error 1222.

### Rules

| Statement | Lock | Work | Rule |
| --- | --- | --- | --- |
| `ADD` a nullable column, with or without a default, or a computed column | `Sch-M` | catalog | `mssql.add_column` |
| `ADD` a NOT NULL column with a runtime constant default, or a default `WITH VALUES` | `Sch-M` | catalog on the editions above, and rewrite on the others | `mssql.add_column.default` |
| `ADD` a column with a per-row default such as `NEWID()`, an identity, a `PERSISTED` computed column, or a `rowversion` or `timestamp` column; and a NOT NULL column, or a default `WITH VALUES`, of a large value type, `xml`, `text`, `ntext`, `image`, `hierarchyid`, a spatial type, or `json`, on every edition. A type that is not a system type may be a CLR type, which writes every row too, so it is a rewrite with confidence `likely` | `Sch-M` | rewrite | `mssql.add_column.rewrite` |
| `DROP COLUMN`, with a note that the space stays in each row until the table is rebuilt | `Sch-M` | catalog | `mssql.drop_column` |
| `ALTER COLUMN` to a longer length of the same variable-length type, to the same type, or to NULL | `Sch-M` | catalog | `mssql.alter_column.metadata` |
| `ALTER COLUMN ... NOT NULL` on a nullable column: a scan of a fixed-length column, and an update of every row of a variable-length one | `Sch-M` | scan or rewrite | `mssql.set_not_null` |
| Any other `ALTER COLUMN`, which updates every row | `Sch-M` | rewrite | `mssql.alter_column` |
| `ALTER COLUMN ... WITH (ONLINE = ON)`, 2016 and later | `Sch-M` when it ends | rewrite | `mssql.alter_column.online` |
| `ADD CONSTRAINT ... CHECK` | `Sch-M` | scan | `mssql.add_check` |
| `WITH NOCHECK ADD CONSTRAINT ... CHECK` | `Sch-M` | catalog | `mssql.add_check.nocheck` |
| `ADD CONSTRAINT ... FOREIGN KEY`, on both tables | `Sch-M` | scan | `mssql.add_foreign_key` |
| `WITH NOCHECK ADD CONSTRAINT ... FOREIGN KEY`, on both tables | `Sch-M` | catalog | `mssql.add_foreign_key.nocheck` |
| `WITH CHECK CHECK CONSTRAINT` | `Sch-M` | scan | `mssql.check_constraint` |
| `CHECK CONSTRAINT` and `NOCHECK CONSTRAINT` without `WITH CHECK` | `Sch-M` | catalog | `mssql.constraint_state` |
| `ADD CONSTRAINT ... PRIMARY KEY` or `UNIQUE`: an index build, or a rewrite for a clustered key on a heap. A primary key is clustered unless it says `NONCLUSTERED` or the table has a clustered index | `Sch-M` | index build or rewrite | `mssql.add_key` |
| The same `WITH (ONLINE = ON)` | `Sch-M` when it ends | index build or rewrite | `mssql.add_key.online` |
| `DROP CONSTRAINT`: a rewrite into a heap for the key behind the clustered index | `Sch-M` | catalog or rewrite | `mssql.drop_constraint` |
| `ADD DEFAULT ... FOR`, and the diff's default drop | `Sch-M` | catalog | `mssql.default` |
| `CREATE INDEX` of a nonclustered index | `S` | index build | `mssql.create_index` |
| `CREATE INDEX ... WITH (ONLINE = ON)` | `S` when it starts and when it ends | index build | `mssql.create_index.online` |
| `CREATE CLUSTERED INDEX`, which copies a heap into the index | `Sch-M` | rewrite | `mssql.create_index.clustered` |
| `DROP INDEX` | `Sch-M` | catalog | `mssql.drop_index` |
| `DROP INDEX` of the clustered index, which copies the table into a heap | `Sch-M` | rewrite | `mssql.drop_index.clustered` |
| `ALTER INDEX ... REBUILD`: a rewrite for the clustered index or `ALL`, and an index build otherwise; `ALTER TABLE ... REBUILD` | `Sch-M` | index build or rewrite | `mssql.rebuild` |
| `ALTER INDEX ... REORGANIZE`: a scan of a nonclustered index, and a rewrite with confidence `likely` of the clustered index or `ALL`, which moves rows in proportion to the fragmentation | `X` inside a transaction, `IX` outside one | scan or rewrite | `mssql.reorganize` |
| `ALTER INDEX ... DISABLE`, with a `danger` finding for the clustered index, which makes the table unreadable until it is rebuilt | `Sch-M` | catalog | `mssql.disable_index` |
| `ALTER TABLE ... SWITCH`, on both tables | `Sch-M` | catalog | `mssql.switch` |
| `sp_rename` of a table, column, or index, with a note that running code naming the old name fails | `Sch-M` | catalog | `mssql.rename` |
| `TRUNCATE TABLE` | `Sch-M` | catalog | `mssql.truncate` |
| `DROP TABLE` | `Sch-M` | catalog | `mssql.drop_table` |
| `CREATE TRIGGER`, `DROP TRIGGER`, `ENABLE TRIGGER`, `DISABLE TRIGGER` | `Sch-M` | catalog | `mssql.trigger` |
| `CREATE TABLE`, reported with each table its foreign keys reference; creating or dropping a view | `Sch-M` | catalog | `mssql.schema_change` |
| `INSERT`, `UPDATE`, `DELETE` | `IX` | rows | `mssql.write_rows` |
| `UPDATE` or `DELETE` of every row of a table of 5,000 rows or more | `X` | rows | `mssql.lock_escalation` |
| `RESUMABLE = ON` inside a transaction or without `ONLINE = ON`, which SQL Server refuses, with a `danger` finding | the statement's lock | the statement's work | `mssql.resumable` |
| `UPDATE STATISTICS` | `Sch-S` | scan | `mssql.update_statistics` |

The rules read the column's current type and nullability from the intent the diff attaches, or from the schema read. Without either, an `ALTER COLUMN` is a rewrite with confidence `likely`, and the finding says the current type was not read. A change of precision or scale within one type is a rewrite with confidence `likely`, since the new values may fit the same storage.

### Remedies

| Pattern | Remedy |
| --- | --- |
| `CREATE INDEX`, `ADD CONSTRAINT ... PRIMARY KEY` or `UNIQUE`, `ALTER TABLE ... REBUILD` | The statement `WITH (ONLINE = ON)`, and for `CREATE INDEX` in a migration with `transactional=False` on 2019 and later, `RESUMABLE = ON` as well |
| `ALTER INDEX ... REBUILD` | `WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)))` on 2014 and later, with `RESUMABLE = ON` in a migration with `transactional=False` on 2017 and later |
| `ALTER COLUMN` that updates every row | The statement `WITH (ONLINE = ON)`, on 2016 and later |
| `ALTER TABLE ... SWITCH` | `WITH (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF))`, on 2014 and later |
| An online `CREATE INDEX` on 2022 and later, or an online rebuild on 2014 and later, in a migration with `transactional=False` | `WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)` inside its `ONLINE = ON` |
| `ADD CONSTRAINT` of a CHECK or FOREIGN KEY | `WITH NOCHECK ADD CONSTRAINT`, with a note that the constraint stays untrusted, and the optimizer does not rely on it, until `WITH CHECK CHECK CONSTRAINT` checks every row later |
| `ADD` a NOT NULL column that writes every row, or a default of a type that writes every row | Add the column as NULL without a default, backfill it in batches, then add the default and make it NOT NULL |

A remedy that needs `ONLINE = ON` is left out on an edition that was read to lack it.

The integration suite checks the rules against SQL Server 2022 and 2025, on the Developer edition, which has the Enterprise features, so the rules for the other editions are checked by the unit tests alone. It checks the facts `read_context()` reads. It creates the tables the rules' fixture statements name on a scratch database, runs each fixture inside a transaction between two reads of the locks granted to the session, the partitions of the tables it names, and the log the transaction has written, rolls it back, and fails on any `impact.mismatch`. The `mssql.resumable` fixtures must fail inside the transaction with error 574. It checks that an online `CREATE INDEX` outside a transaction behind an open write fails with error 1222 under `SET LOCK_TIMEOUT`, that one with `WAIT_AT_LOW_PRIORITY` lets a later write go ahead, and that the live preflight names a session in `master` whose transaction writes a table of the scratch database. It also runs a traced rehearsal of an index build and a size-of-data `ALTER COLUMN`, and checks the observed lock and work of each.

## SQLite

The rules follow the SQLite documentation for 3.35 and later. SQLite connections use the `DEFAULT` dialect, whose rules are SQLite's, and rule ids start with `sqlite.`.

### The database write lock

SQLite locks the database file, not a table. The first write of a transaction takes the write lock, and keeps it until the transaction commits, so every other connection's writes wait for it on every table, for as long as their `busy_timeout` lets them. Each table line names the lock `database write lock`. What else waits depends on the journal mode:

| Journal mode | Blocks |
| --- | --- |
| `wal` | `writes`. Readers never wait for the writer. |
| any other, such as `delete` | `reads_and_writes`. Readers also wait while the changes are written to the database file, at the commit or when the page cache fills. |

Without a read of the journal mode, the rules assume a rollback journal, and a finding for blocking work says the journal mode was not read.

A migration inside a transaction keeps the lock from its first write to its commit, whatever tables its statements name, so the report reads it as one window, `(database)`:

```console
20260926_items  transaction
  ALTER TABLE items ADD COLUMN note text
    items  database write lock  blocks writes  catalog  transaction  ~2.0M rows, 1.4 GB  [sqlite.add_column]
  UPDATE items SET note = ''
    items  database write lock  blocks writes  rows  transaction  ~2.0M rows, 1.4 GB  [sqlite.write_rows]
    danger  the UPDATE writes rows of items; writes to every table in the database wait until the migration commits; on a large table, backfill in batches outside the DDL migration
  window  (database): database write lock from statement 1, held to commit
```

Every write blocks the same connections, so the work of a later statement blocks as much as the lock held across it, and the window draws no `window.held` finding. SQLite draws no lock-timeout finding: a write waiting for the lock does not make other connections queue behind it the way a server's lock queue does.

### Rules

| Statement | Work | Rule |
| --- | --- | --- |
| `ADD COLUMN` | catalog | `sqlite.add_column` |
| `ADD COLUMN` with a CHECK constraint, or a NOT NULL constraint on a generated column, which SQLite checks against every row | scan | `sqlite.add_column.checked` |
| `ADD COLUMN` that SQLite refuses, with a `danger` finding: a UNIQUE or PRIMARY KEY column always, and on a table that has rows a NOT NULL column without a default other than NULL, a default in parentheses such as `(random())`, `CURRENT_TIME`, `CURRENT_DATE`, or `CURRENT_TIMESTAMP`, and a `STORED` generated column. A table the run created, or one read as empty, gets no finding, and one whose row count was not read gets it with confidence `likely`. The remedy is to rebuild the table with the new column | catalog | `sqlite.add_column.refused` |
| `DROP COLUMN` | rewrite | `sqlite.drop_column` |
| `RENAME COLUMN`, `RENAME TO`, with a note that running code naming the old name fails | catalog | `sqlite.rename` |
| The diff's rebuild recipe, reported on the table it rebuilds | rewrite | `sqlite.rebuild` |
| `CREATE INDEX` | index build | `sqlite.create_index` |
| `DROP INDEX`, which visits every page of the index to free it | scan | `sqlite.drop_index` |
| `REINDEX` | index build | `sqlite.reindex` |
| `CREATE TABLE ... AS SELECT`, and `INSERT ... SELECT` into a table the run created, reported on each table the query reads, or on `(database)` with confidence `likely` when the query reads rows from a function or a VALUES list | rewrite | `sqlite.copy` |
| `CREATE TABLE`, and creating or dropping a view or trigger | catalog | `sqlite.schema_change` |
| `DROP TABLE`, which visits every page of the table to free it | scan | `sqlite.drop_table` |
| `INSERT`, `UPDATE`, `DELETE` | rows | `sqlite.write_rows` |
| `ANALYZE` | scan | `sqlite.analyze` |
| `VACUUM`, which copies the whole database into a new file | rewrite | `sqlite.vacuum` |

The diff changes a column's type or constraints on SQLite by rebuilding the table: it creates a new table, copies every row into it, drops the old table, renames the new one, and creates the indexes again. Each statement of the recipe has the `rebuild_table` intent, so the analysis reports the copy as a rewrite of the table being rebuilt, under `sqlite.rebuild`. A hand-written copy has no intent, and its `INSERT ... SELECT` into the new table is reported as a rewrite of the table it reads, under `sqlite.copy`. Either way the write lock lasts while every row is read and written again. After the copy, the new table has the size of the table it copied, so the rename and the `CREATE INDEX` statements after it are reported against that size. The rename draws no note about running code, since no running code names the new table.

`VACUUM` cannot run inside a transaction, so inside a transactional migration it also draws a `danger` finding that says to run it in a migration with `transactional=False`. `REINDEX` with no name reindexes every index in the database, and is reported on `(database)`. A `REINDEX` name that is neither a table nor an index the run or the schema read knows may be a collation, whose indexes span tables, so its confidence is `likely`.

A statement the rules do not read, such as an `ALTER TABLE` action other than a column add, drop, or rename, is unknown.

The integration suite checks each rule's fixtures on a WAL database file. The `sqlite.add_column.refused` fixtures must fail on the fixture table, which has rows. Each other fixture runs inside a transaction while a second connection tries to take the write lock and to read the database: the write must wait when the rules predict the write lock, and the read must go on. Each fixture then runs again with automatic checkpoints off, and the frames it leaves in the WAL count the pages it wrote. A statement the rules say rewrites a table or builds an index must write at least half as many pages as the table has, and one they say changes only the schema at most two.

## DuckDB

The rules follow the DuckDB documentation for 1.0 and later, and the integration suite checks them against the DuckDB release installed in the test environment. Rule ids start with `duckdb.`.

### Conflicts instead of locks

DuckDB takes no locks. Its [concurrency control](https://duckdb.org/docs/current/connect/concurrency.html#optimistic-concurrency-control) is optimistic: each transaction works on its own versions of the rows and the catalog, and a transaction whose change conflicts with another's uncommitted change aborts with a conflict error at once. It does not wait, so nothing queues behind a migration, and DuckDB draws no lock-timeout finding. Reads never conflict: another transaction reads the table as it was before the migration began, until the migration commits.

Each table line names the conflict the statement opens on the table, after the error the other transaction gets. DuckDB DDL is transactional, so a conflict lasts until the migration commits:

| Conflict | Opened by | Blocks |
| --- | --- | --- |
| `altered table` | `ADD COLUMN`, `DROP COLUMN`, `SET DATA TYPE`, `SET NOT NULL` | `writes`. `INSERT`, `UPDATE`, `DELETE`, and schema changes on the table in other transactions abort, and a transaction that wrote to the table before the statement fails to commit. |
| `changed rows` | `UPDATE`, `DELETE`, `TRUNCATE` | `writes`. Another transaction that updates the same columns of the same rows, or deletes the same rows, aborts. Other rows, and inserts, go on. |
| `dropped table` | `DROP TABLE` | `writes`. Schema changes on the table in other transactions abort, and a transaction that wrote to the table before the `DROP` fails to commit. Reads go on. |
| `catalog entry` | a rename, `SET DEFAULT`, `DROP DEFAULT`, `DROP NOT NULL`, `COMMENT ON`, `DROP INDEX`, and a new table whose foreign key points at the table | `ddl`. Schema changes on the table in other transactions abort. Reads and writes go on. |
| none | `CREATE INDEX`, `INSERT`, `ANALYZE`, and creating or dropping a view, a type, a sequence, or a schema | `nothing` |

The finding for a statement that blocks writes says which transactions abort:

```console
20260926_items  transaction
  ALTER TABLE items ADD COLUMN note varchar
    items  altered table  blocks writes  rows  transaction  ~2.0M rows  [duckdb.add_column]
    info    until the migration commits, INSERT, UPDATE, DELETE, and schema changes on items in other transactions abort with a conflict error instead of waiting, and a transaction that wrote to items before it fails to commit; reads go on
  ALTER TABLE items ALTER COLUMN price SET DATA TYPE decimal(12, 2)
    items  altered table  blocks writes  rewrite  transaction  ~2.0M rows  [duckdb.alter_column_type]
    danger  SET DATA TYPE writes every value of price again; until the migration commits, INSERT, UPDATE, DELETE, and schema changes on items in other transactions abort with a conflict error instead of waiting, and a transaction that wrote to items before it fails to commit; reads go on
  window  items: altered table from statement 1, altered table from statement 2, held to commit
  warn    items stays blocked for writes from statement 1 until the migration commits, across the rewrite work of statement 2; move that work to a migration of its own
```

A conflict works both ways. When another transaction changes the schema of a table first, the migration's own statement on that table aborts, and when another transaction alters a table the migration has written rows of, the migration fails to commit. DuckDB's documentation gives running the transaction again as the remedy.

### Rules

| Statement | Work | Rule |
| --- | --- | --- |
| `ADD COLUMN` with no default, a constant default, or a default such as `now()` that gives every row the same value, which DuckDB fills in every row group, in time that grows with the rows | rows | `duckdb.add_column` |
| `ADD COLUMN` with a volatile default, such as `random()` or `gen_random_uuid()`, which DuckDB writes for every row | rewrite | `duckdb.add_column.volatile` |
| `DROP COLUMN` | catalog | `duckdb.drop_column` |
| `SET DATA TYPE`, or `TYPE`, with or without `USING`, which writes every value of the column again | rewrite | `duckdb.alter_column_type` |
| `SET NOT NULL`, which reads every row to check for NULLs | scan | `duckdb.set_not_null` |
| `DROP NOT NULL`, `SET DEFAULT`, `DROP DEFAULT` | catalog | `duckdb.alter_column` |
| `RENAME COLUMN`, `RENAME TO`, with a note that running code naming the old name fails | catalog | `duckdb.rename` |
| `CREATE INDEX` | index build | `duckdb.create_index` |
| `DROP INDEX` | catalog | `duckdb.drop_index` |
| `COMMENT ON` | catalog | `duckdb.comment` |
| `CREATE TABLE`, reported with each table its foreign keys reference | catalog | `duckdb.create_table` |
| Creating or dropping a view, a type, a sequence, or a schema | catalog | `duckdb.schema_change` |
| `DROP TABLE`, which opens the `dropped table` conflict | catalog | `duckdb.drop_table` |
| `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE` | rows | `duckdb.write_rows` |
| `ANALYZE` | scan | `duckdb.analyze` |

A rewrite on DuckDB writes one column again. The other columns keep their storage, and DuckDB has no table copy for the analysis to report. The diff fills a column's NULLs before `SET NOT NULL` through `SET DATA TYPE ... USING coalesce(...)`, which DuckDB runs where an `UPDATE` followed by `SET NOT NULL` fails, so that step reads as a rewrite of the column.

DuckDB refuses to alter a table that an index depends on, with a dependency error. The rules leave that to the rehearsal, which runs the statement. DuckDB also refuses a constraint on `ADD COLUMN`, a generated column on `ADD COLUMN`, and `ADD CONSTRAINT` and `DROP CONSTRAINT`; the analysis reads those, and any other statement the rules do not read, as unknown.

The integration suite checks each rule's fixtures on a database file. Each fixture runs inside a transaction while a second connection to the same database reads the table, then inserts, updates, and deletes a row of it, and adds a column to it, each in a transaction of its own. The read must go on, and which of the others abort with a conflict must match what the rules say the statement blocks. On a fresh database for each write, a second connection's open transaction also inserts, updates, or deletes a row first, and the fixture then runs and commits; a fixture that aborts, or a commit of the other transaction that fails, counts as blocking writes. Each fixture then runs again on its own between two checkpoints. The column segments `pragma_storage_info()` shows in blocks the table did not use before count the rows the statement wrote: a statement the rules say rewrites a column must write every row of a column again, and one they say changes only the catalog, or scans, must write none and leave `pragma_database_size()` with no more used blocks. An index build must write no column and take more blocks.
