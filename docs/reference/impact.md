---
layout: default
title: Impact reference
description: "Reference for sustained.impact: analyze(), read_context(), the rule profiles, the impact attached for guards, rehearse(trace=True), the live preflight, the ImpactReport model, EngineContext, thresholds, and the report's text and JSON forms."
---

These names live in `sustained.impact`, except where a section names another module.

Guide: [Statement impact](/impact).

## `analyze()`

```python
analyze(statements, dialect, context=None, thresholds=Thresholds()) -> ImpactReport
```
{: .sig #analyze}

The impact of the statements a run would apply, in run order. `statements` is the same `Sequence[str]` guards receive. A `MigrationStatement` names its migration and its transaction flag, and consecutive statements with the same id and flag form one migration. A plain `str` reads as a statement of an unnamed migration inside a transaction.

Without a `context`, the rules assume the dialect's support floor, and the report's evidence is `static`. The context's `profile` picks the rules on a dialect with more than one profile: `mysql` or `mariadb` on `Dialects.MYSQL`. Without a context, MySQL is assumed, and the first migration gets an `impact.assumed_profile` finding. `analyze()` connects to no database. It raises `ValueError` for a dialect without impact rules.

```python
attach_impact(statements, dialect, context=None) -> list[MigrationStatement]
```
{: .sig #attach_impact}

The statements with each one's `StatementImpact` from `analyze()` on its `impact` attribute, which the impact guards read. A plain `str` becomes a `MigrationStatement` of an unnamed migration inside a transaction. A `MigrationStatement` keeps its migration id, its transaction flag, its `destructive` mark, and its `intent`. It raises `ValueError` for a dialect without impact rules.

```python
supported(dialect) -> bool
```
{: .sig #supported}

Whether the analysis has rules for the dialect. `Dialects.POSTGRES`, `Dialects.MYSQL`, `Dialects.MSSQL`, `Dialects.DEFAULT`, and `Dialects.DUCKDB` have rules.

```python
profile_for(dialect, name=None) -> Profile | None
profiles_for(dialect) -> tuple[Profile, ...]
```
{: .sig #profile_for}

These live in `sustained.impact.rules`. `profile_for()` returns the dialect's rule profile named `name`, or its first profile when `name` is `None` or names none of them, and `None` for a dialect without rules. `profiles_for()` returns every profile of the dialect, the one assumed without a server first: `postgres` on `Dialects.POSTGRES`, `mysql` then `mariadb` on `Dialects.MYSQL`, `mssql` on `Dialects.MSSQL`, `sqlite` on `Dialects.DEFAULT`, which SQLite connections use, and `duckdb` on `Dialects.DUCKDB`.

## `read_context()`

```python
read_context(connection, dialect, exact_counts=False, statements=None) -> EngineContext
await async_read_context(adapter, dialect, exact_counts=False, statements=None) -> EngineContext
```
{: .sig #read_context}

The server facts the dialect's rules read, from a blocking connection or an async adapter. On PostgreSQL that is the version from `server_version_num`, the `TimeZone` and `lock_timeout` settings, each table's estimated rows and total bytes, read under a `lock_timeout` of `1s` that is set back to the session's own value after, and the schema of the connection's own schema. On MySQL and MariaDB it is `VERSION()`, which also sets the context's profile to `mysql` or `mariadb`, the `foreign_key_checks` and `lock_wait_timeout` settings, each table's estimated rows, bytes, row format, and default collation, which tables have a FULLTEXT index, on MySQL 8.0.29 and later each table's instant row versions, and the schema of the current database. On SQL Server it is `SERVERPROPERTY('ProductVersion')`, the edition from `SERVERPROPERTY('EngineEdition')` and `SERVERPROPERTY('Edition')`, `@@LOCK_TIMEOUT` as the `lock_timeout` setting, `is_read_committed_snapshot_on` from `sys.databases` as the `read_committed_snapshot` setting, each table's rows from `sys.partitions`, its bytes from `sys.allocation_units`, whether it is a heap and the name of its clustered index from `sys.indexes`, and the schema. On SQLite it is `sqlite_version()`, the `journal_mode` setting, each table's estimated rows from `sqlite_stat1`, the database file's bytes under the name `(database)`, each table's bytes as the file's bytes times its share of the rows `sqlite_stat1` counts, and the schema. On DuckDB it is `version()`, each table's estimated rows from `duckdb_tables()`, with no bytes, and the schema. With `exact_counts=True`, the SQLite read also runs `SELECT COUNT(*)` on each table `sqlite_stat1` has no row count for, leaving out virtual tables, reads each table's bytes from the `dbstat` virtual table, and adds `counts` to `read`; the other profiles ignore it. With `statements`, the sizes are read only for the tables `named_tables()` returns for them, and on SQLite the counts and the `dbstat` read cover only those tables. Nothing is written.

Each statement runs inside a savepoint. A statement that fails leaves its facts out of `read`, and the read goes on. On PostgreSQL, when the size read of the named tables fails, for example on a lock not granted within the timeout, each table is read in a statement of its own, and only the tables whose read failed have unknown sizes. Both raise `ValueError` for a dialect without impact rules.

```python
named_tables(statements, dialect, schema=None) -> frozenset
```
{: .sig #named_tables}

The tables the statements act on, as `analyze()` finds them with the schema `Snapshot` and no other server fact: each table a statement names, and each table the schema answers for it, such as the table of an index a statement drops, or the table at the other end of a foreign key. Each name is the last part of a dotted name, in lower case. Raises `ValueError` for a dialect without impact rules.

## `Migrator.impact()`

```python
Migrator.impact(models=None, assert_algorithm=False, exact_counts=False, live=False, older_than=60.0, online=False, *, allow_drops=False, ignore_changed_columns=False, migration_id=None, renames=None, table_renames=None, type_casts=None) -> ImpactReport
await AsyncMigrator.impact(models=None, assert_algorithm=False, exact_counts=False, live=False, older_than=60.0, online=False, *, allow_drops=False, ...) -> ImpactReport
```
{: .sig #migrator-impact}

The impact of the run `up()` would make: every pending migration, then the migration the models generate when `models` is given. The generated migration is diffed against the schema as it is now, before the pending migrations run, as `plan()` diffs it, and `assert_algorithm` writes the clauses `plan()` writes on it. The keyword-only arguments are the diff options `up()` takes, so a call with the options of the `up()` it precedes analyzes the migration that run generates. With `online=True`, the models generate the migrations `plan_migrations(models, online=True)` returns, and each is analyzed in turn. A callable step renders no SQL and is left out. Nothing is written.

The context comes from `read_context()` on the migrator's connection, or `async_read_context()` on its adapter, with `exact_counts` passed on. With `live=True`, the report's `preflight` is the [live preflight](#preflight) of the analyzed statements, with `older_than` passed on. Both raise `DialectError` on a dialect the analysis does not cover, and with `live=True` on a dialect without a preflight, before any statement runs. With `live=True`, an `older_than` that is negative, NaN, or not a number raises `ValueError`, also before any statement runs.

## Guards over impact

`up()` reads the context the way `Migrator.impact()` does, with its own `exact_counts` and with `statements` set to the run's statements, so it reads the sizes of the run's tables only. It analyzes the run before the guards run, and sets each `MigrationStatement`'s `impact` attribute to its `StatementImpact`. With `models`, the generated migration is read and analyzed again on its own after the registered migrations apply. The impact rules `max_blocking()`, `no_rewrite()`, `lock_timeout_required()`, and `no_unknown_impact()` live in `sustained.guards`; see [Guards](/reference/migrations#guards).

## `Migrator.rehearse(trace=True)`

```python
Migrator.rehearse(..., trace=True) -> Rehearsal
await AsyncMigrator.rehearse(..., trace=True) -> Rehearsal
```
{: .sig #rehearse-trace}

Rehearses the run with each statement observed; see [Observed impact](/impact#observed-impact). The result's `impact` is the run's `ImpactReport`, with the context read at the start of the rehearsal and each observed statement's lock and work in place of the prediction. It covers every migration the up sweep reached, in run order, including the ones the rehearsal left out, which keep their prediction. `impact` is `None` for a rehearsal without `trace`, and for one with nothing pending. `trace=True` raises `DialectError` on any dialect other than `POSTGRES`, `MYSQL`, and `MSSQL`, before any statement runs. On `MYSQL` and `MSSQL` it needs `scratch=True`, as every rehearsal there does.

The profile's `trace` attribute names how a rehearsal observes its statements, and is `None` for a profile the rehearsal cannot observe. It is a `Trace(tables, report, sighting=None, attempts=None, refused=...)`: `tables()` reads the tables that exist before the run, and `report(predicted, observations, existing, profile)` puts the observations in place of the prediction. A trace with `sighting` reads it before and after each statement, as on Postgres and SQL Server. A trace with `attempts` runs the statement with each clause `attempts(impact, profile)` returns, in order, until one runs without an error that `refused(error)` reads as a refusal, as on MySQL and MariaDB; the observation is a `Probe(accepted, refused)`, the accepted clause and each refused one with the server's reason.

The Postgres names live in `sustained.impact.rules.postgres.trace`:

```python
sighting_plan(tables) -> Sighting
tables_plan() -> frozenset[int] | None
```
{: .sig #sighting_plan}

Read plans, generators that yield SQL and take each statement's rows back, which `run_plan()` in `sustained.introspect.runner` drives. `sighting_plan()` reads the table locks the transaction has and the files of the named tables and their indexes. `tables_plan()` reads the oids of every table that exists.

`Sighting(locks, names, storage, read)` is one read: the lock modes granted on each table, by oid, in the rules' names, such as `SHARE`; the lower case names that find each table; each named table's `File(is_index, filenode, size)` records, by relation oid; and `read`, which names `locks` and `storage` for the parts that were read.

```python
observe(impact, before, after, existing, profile) -> StatementImpact
with_observations(report, observations, existing, profile) -> ImpactReport
```
{: .sig #observe}

`observe()` returns one statement's impact with the facts its two sightings show, and an `impact.mismatch` finding for each difference. `existing` is the set of oids of the tables that existed before the run, or `None` to compare every table. It returns the impact unchanged when the locks were not read both times. `with_observations()` applies `observe()` across a report, keyed by migration id and the statement's position in its migration, counting from 0, and reads each migration's locks and windows again.

The MySQL and MariaDB names live in `sustained.impact.rules.mysql.trace`:

```python
tables_plan() -> frozenset[str] | None
candidates(mariadb) -> tuple[Online, ...]
attempts(impact, profile) -> list[tuple[str, Online]]
refused(error) -> str | None
```
{: .sig #mysql-trace}

`tables_plan()` reads the names of every table that exists, lower case, as `schema.table` and as the bare name in the connection's own database. `candidates()` lists the clauses a probe tries, in the order the server picks one. `attempts()` writes the statement with each of them through `assertion()`, and is empty for a statement that is not an ALTER TABLE, CREATE INDEX, or DROP INDEX, or that spells its own `ALGORITHM` or `LOCK`. `refused()` returns the server's message for errors 1845, 1846, and 4092, read from `errno` and `msg` or from `args`, and `None` for any other error.

```python
observe(impact, probe, existing, profile) -> StatementImpact
with_observations(report, observations, existing, profile) -> ImpactReport
```
{: .sig #mysql-observe}

`observe()` returns one statement's impact with the accepted clause as the lock on the table the statement names, `catalog` work after `INSTANT` and `rewrite` after `COPY`, and an `impact.mismatch` finding for each difference. `existing` lists the names `tables_plan()` read, or `None` to compare the named table whatever it is. `with_observations()` applies `observe()` across a report, as the Postgres form does.

The SQL Server names live in `sustained.impact.rules.mssql.trace`:

```python
sighting_plan(tables) -> Sighting
tables_plan() -> frozenset[int] | None
```
{: .sig #mssql-sighting_plan}

`sighting_plan()` reads the table locks granted to the session from `sys.dm_tran_locks`, the partitions of the named tables and their indexes, and the log the transaction has written. `tables_plan()` reads the object ids of every table that exists.

`Sighting(locks, names, storage, log, read)` is one read: the lock modes held on each table, by object id, such as `Sch-M`; the lower case names that find each table, as `schema.table` and as the bare name in the default schema; each named table's `Partition(base, partition_id, pages)` records, by object id and then by index id and partition number, where `base` marks the heap or clustered index; the bytes of log the transaction has written; and `read`, which lists `locks`, `storage`, and `log` for the parts that were read.

```python
observe(impact, before, after, existing, profile) -> StatementImpact
with_observations(report, observations, existing, profile) -> ImpactReport
```
{: .sig #mssql-observe}

`observe()` returns one statement's impact with the facts its two sightings show, and an `impact.mismatch` finding for each difference, as the Postgres form does. A changed partition of the heap or clustered index that still has pages, or log of at least twice the bytes it held and at least 64 KB, is a rewrite, and a new or changed partition of another index is an index build. `existing` is the set of object ids of the tables that existed before the run, or `None` to compare every table. `with_observations()` applies `observe()` across a report.

## Live preflight

Guide: [Live preflight](/impact#live-preflight).

```python
preflight(connection, dialect, statements, older_than=60.0, context=None) -> Preflight
await async_preflight(adapter, dialect, statements, older_than=60.0, context=None) -> Preflight
```
{: .sig #preflight}

The sessions the statements would wait behind on the server now, and the other transactions open at least `older_than` seconds, from a blocking connection or an async adapter. A statement with `impact` attached is read from it. The others are analyzed with `context`, which is read from the connection when it is not given. The connection's own session is never listed, and no session is ended. Both raise `ValueError` for a dialect without a preflight: `Dialects.POSTGRES`, `Dialects.MYSQL`, and `Dialects.MSSQL` have one. Both also raise `ValueError` for an `older_than` that is negative, NaN, or not a number, and so does `preflight_plan()`.

```python
Migrator.preflight(models=None, older_than=60.0, exact_counts=False, online=False, *, allow_drops=False, ...) -> Preflight
await AsyncMigrator.preflight(models=None, older_than=60.0, exact_counts=False, online=False, *, allow_drops=False, ...) -> Preflight
```
{: .sig #migrator-preflight}

The preflight of the run `Migrator.impact()` analyzes, with `models`, `exact_counts`, `online`, and the diff options as there. Both raise `DialectError` on a dialect without a preflight, and `ValueError` for an `older_than` that is negative, NaN, or not a number.

```python
Migrator.up(..., preflight=None) -> list[str]
await AsyncMigrator.up(..., preflight=None) -> list[str]
PreflightCheck(mode, older_than=60.0)
```
{: .sig #up-preflight}

With `preflight="warn"` or `"refuse"`, or a `PreflightCheck` with one of them as its `mode`, `up()` reads the preflight of the run's statements after the guards pass and before any migration applies, and again for the generated migration with `models`. `warn` prints each blocker, each transaction open `older_than` seconds or longer, 60 for a plain mode, each read that failed, and each statement the preflight cannot check on stderr as `preflight: <line>`, once per run. `refuse` raises `PreflightBlocked` when the `Preflight` is not `clear`: when there is a blocker, when a read in `needs` failed, or when a statement is in `unread`. It prints the transaction lines otherwise. Any other mode raises `ValueError` before the run starts. `PreflightCheck` lives in `sustained.migrations`, and raises `ValueError` for an `older_than` that is negative, NaN, or not a number. On a dialect without a preflight, SQLite and DuckDB among them, `up()` with a preflight raises `DialectError` before the run starts, as `impact(live=True)` does.

`PreflightBlocked` lives in `sustained.exceptions` and in `sustained`. It is a `SustainedError` whose `preflight` attribute is the `Preflight` that stopped the run, and whose message lists each blocker's line, the reads in `missing`, and each unread statement's line.

```python
Preflight(profile, blockers, transactions, older_than, read=frozenset(), needs=frozenset(), unread=())
Preflight.missing -> tuple[str, ...]
Preflight.clear -> bool
```
{: .sig}

`profile` is the rule profile the server reads as, such as `'postgres'` or `'mariadb'`. `blockers` is a tuple of `Blocker`, in run order. `transactions` is a tuple of `LiveSession`: the other transactions open at least `older_than` seconds whose sessions are not blockers, oldest first. `read` lists `locks` and `transactions` for the reads that came from the server. `needs` names the reads the blockers of these statements come from: `locks` when a statement takes a table lock, and on PostgreSQL `transactions` as well when a statement is `CREATE INDEX CONCURRENTLY` or `REINDEX CONCURRENTLY`, which wait for every transaction with a snapshot. `unread` is a tuple of the statements whose impact has confidence `unknown`, in run order: they name no table, so their locks are not checked. `missing` is the reads in `needs` that are not in `read`, in name order. `clear` is true when there is no blocker, nothing is missing, and nothing is unread. `preflight_plan()` fills in `needs` and `unread`.

```python
Blocker(statement, table, lock, held, granted, session)
```
{: .sig}

One session a statement would wait behind: the statement, the table as the statement names it, the lock the statement takes on it, and the engine's name for the lock the session was granted, with `granted` true, or is waiting for, with `granted` false. `held` is `None` when the statement waits for the session's transaction to end instead of for a lock, as PostgreSQL's `CREATE INDEX CONCURRENTLY` waits for every transaction with a snapshot. A session is listed once per table and lock, beside the first statement that would wait for it.

```python
LiveSession(id, label, user=None, application=None, state=None, transaction_seconds=None, query=None)
```
{: .sig}

Another session. `id` is the backend pid on PostgreSQL, the connection id on MySQL and MariaDB, and the session id on SQL Server, and `None` for a PostgreSQL prepared transaction. `label` is how the report names it: `pid 4121`, `connection 12`, `session 57`, or `prepared transaction 'gid'`. `state` is the engine's word for what the session is doing: `pg_stat_activity.state`, the processlist command, such as `Sleep`, or `sys.dm_exec_sessions.status`. `transaction_seconds` is how long its transaction has been open, and `query` its current or most recent statement. Any field but `id` and `label` is `None` where the read did not give it.

The profile's `preflight` attribute is the read plan, `preflight(impacts, older_than)`, which yields SQL and returns a `Preflight`, or `None` for a profile without one. `preflight_plan(dialect, impacts, older_than=60.0)` and `covered(dialect)` live in `sustained.impact.preflight`. The plans live in `sustained.impact.rules.postgres.preflight`, `sustained.impact.rules.mysql.preflight`, and `sustained.impact.rules.mssql.preflight`, each with `conflicts(planned, mode)`, which says whether a planned lock waits for another session's lock mode.

## `ImpactReport`

```python
ImpactReport(profile, version, evidence, migrations, read=frozenset(), preflight=None)
```
{: .sig}

`profile` is the rule profile: `'postgres'`, `'mysql'`, `'mariadb'`, `'sqlite'`, or `'duckdb'`. `version` is the server version the rules assumed, as a tuple of ints. `evidence` is what the report rests on. `migrations` is a tuple of `MigrationImpact`, in run order. `read` is the context's `read`: the facts that came from the server. `preflight` is the `Preflight` of a report read with `live=True`, and `None` otherwise.

| Member | Returns |
| --- | --- |
| `statements` | Every `StatementImpact`, in run order |
| `findings` | Every `Finding`, statement findings first within each migration |
| `count(severity)` | How many findings have that severity |

## `MigrationImpact`

```python
MigrationImpact(migration_id, transactional, statements, locks=(), windows=(), findings=(), held_to_commit=False)
```
{: .sig}

One migration: its id, or `None` for statements with no migration, its transaction flag, a tuple of `StatementImpact`, the `Lock`s it takes, the `Window`s those locks make, and findings about the migration as a whole, such as `window.held` and `window.lock_order`. `held_to_commit` is true when the locks last until the migration commits: inside a transaction, on an engine whose DDL does not commit on its own. Otherwise each statement is a window of its own, except that on MySQL and MariaDB, inside a transaction, each run of `INSERT`, `UPDATE`, and `DELETE` statements between DDL statements is one window, since their row locks last until the next DDL statement commits them; `held_to_commit` is false there. On SQLite, whose writes lock the whole database, the migration's locks make one `Window` named `(database)`, and it draws no `window.held` finding.

`Lock(table, lock, blocks, statement)` is one lock that blocks something. `statement` is the position of the statement that took it, counting from 1 within the migration.

`Window(table, blocks, taken_by, heaviest, during)` is one table blocked for writes or more. `blocks` is the worst lock held on it, `taken_by` the position of the statement that first blocked it that far, and `heaviest` the heaviest `Work` that runs while the lock is held, done by the statement at position `during`.

## `StatementImpact`

```python
StatementImpact(statement, parsed, tables, findings, evidence, confidence)
```
{: .sig}

One statement. `parsed` is the recognizer's `ParsedStatement`, or `None` for a statement it could not read. `tables` is a tuple of `TableImpact`, and `findings` a tuple of `Finding`. `severity` is the worst severity among the findings, or `None` with none.

```python
TableImpact(table, lock, blocks, work, hold, rows=None, bytes=None, rule=None)
```
{: .sig}

What the statement does to one table: the engine's lock name, or `None` for no lock, what it blocks, the work, how long the lock lasts, the row and byte estimates when known, and the id of the rule that gave the answer. DuckDB takes no locks, so there the lock name is the conflict the statement opens on the table: `altered table`, `changed rows`, `dropped table`, or `catalog entry`; see [DuckDB](/impact#duckdb).

```python
Finding(rule, severity, message, remedy=(), source=None)
```
{: .sig}

One thing a rule has to say. `rule` is the rule id, such as `pg.create_index`. `remedy` is a tuple of safer statements, in the order to run them, and is empty when none exists. `source` is the URL of the documentation the rule relies on.

The analysis itself raises these findings:

| Rule | Severity | Raised when |
| --- | --- | --- |
| `impact.unknown` | `info` | The recognizer could not read the statement. The message gives the reason. |
| `impact.intent_mismatch` | `warn` | A generated statement's text reads as something other than its intent. The analysis follows the text. |
| `impact.from_intent` | `info` | The recognizer could not read a generated statement, so the analysis follows its intent. The message names each fact the intent does not give, which takes its worst case, and the statement's confidence is then at most `likely`. |
| `impact.mismatch` | `warn` | A traced rehearsal saw the server take another lock than the rules predicted, copy a file the rules did not predict, or copy none where they predicted a rewrite or an index build. On MySQL and MariaDB: the server accepted another clause than the rules predicted, or its clause copied the table where they predicted none, or nothing where they predicted a copy. |
| `impact.assumed_profile` | `info` | No context was given on a dialect with more than one profile, so the first was assumed. |
| `pg.lock_timeout` | `warn` | A lock that blocks writes or more waits with no `lock_timeout` in scope, from a `SET` earlier in the run or from the connection's settings. |
| `mysql.lock_timeout`, `mariadb.lock_timeout` | `warn` | A statement that takes the exclusive metadata lock runs with no `lock_wait_timeout` below a day in scope. |
| `mysql.refused`, `mariadb.refused` | `warn` | The statement spells an `ALGORITHM` or `LOCK` the change cannot run with, so the server refuses it. |
| `window.held` | `warn` | A table stays blocked until the commit across heavier work from a later statement. The message names each level the table is blocked for, the first statement to block it that far, and when the block ends: when the migration commits, or on MySQL and MariaDB at the implicit commit before the next DDL statement. |
| `window.lock_order` | `warn` | One migration blocks reads and writes on more than one table at once. |

## Enums

Each enum is a `str` enum, so a member compares, prints, and serializes as its value. `Blocks`, `Work`, `Hold`, `Evidence`, `Confidence`, and `Severity` order their members, so `max()` picks the worst.

| Enum | Members, in order |
| --- | --- |
| `Blocks` | `nothing`, `ddl`, `writes`, `reads_and_writes` |
| `Work` | `catalog`, `scan`, `rows`, `index_build`, `rewrite`, `unknown` |
| `Hold` | `brief`, `statement`, `transaction` |
| `Evidence` | `static`, `catalog`, `observed` |
| `Confidence` | `unknown`, `likely`, `known` |
| `Severity` | `info`, `warn`, `danger` |

## `Thresholds`

```python
Thresholds(rows=1_000_000, bytes=1 << 30)
```
{: .sig}

The table size past which blocking work is `danger`. Work on a table with more estimated rows or bytes than these is `danger`, work on a smaller table is `info`, and work on a table of unknown size is `warn`.

## `EngineContext`

```python
EngineContext(profile, version, edition=None, settings={}, tables={}, schema=None, read=frozenset(), relations={}, types={})
```
{: .sig}

The server facts the rules read: the profile, the version as a tuple of ints, the edition, settings such as `TimeZone` and `lock_timeout`, a mapping of lower case table name to `TableStats`, the schema `Snapshot`, `read`, the names of the facts that came from a server, `relations`, a mapping of lower case table name to `Relation`, and `types`, a mapping of lower case type name to whether the type is a domain with a NOT NULL or a CHECK, for the types outside the system schemas. The names in `read` are `version`, `settings`, `sizes`, and `schema`, on PostgreSQL also `partitions`, `indexes`, `arrays`, and `types`, on MySQL and MariaDB also `fulltext` and `row_versions`, on SQL Server also `edition` and `clustered`, and on SQLite `counts` when `exact_counts` read the table list. With `read` empty, the report's evidence is `static`, and `catalog` otherwise. `read_context()` builds one from a connection.

`read_context()` keys each table as `schema.table`, and also by its bare name when the search path finds it under that name, or on MySQL and MariaDB when it is in the current database. `relations` and `types` use the same keys. `stats(table)` returns a table's `TableStats`, or unknown stats for a table the read did not see.

These methods read the schema, and return `None` or an empty tuple when the schema was not read or does not have what they look for. A dotted name finds a table by its last part, since the read covers one schema.

| Method | Returns |
| --- | --- |
| `table(name)` | The `IntrospectedTable` |
| `column_type(table, column)` | The column's current type |
| `index_table(index)` | The name of the table the index is on |
| `references(table, columns=None)` | The tables the table's foreign keys point at; with `columns`, only the keys that use one of them |
| `referenced_by(table, columns=None)` | The other tables whose foreign keys point at the table; with `columns`, only the keys that point at one of them |
| `foreign_key_target(table, name)` | The table the named foreign key points at |

`TableStats(rows=None, bytes=None, row_format=None, row_versions=None, fulltext=None, heap=None, clustered=None, collation=None)` gives one table's size estimates and the storage facts the InnoDB rules read, each `None` where it was not read. On PostgreSQL, `rows` is `None` for a table that was never vacuumed or analyzed. `row_format` is the InnoDB row format in upper case, such as `DYNAMIC` or `COMPRESSED`. `row_versions` counts the instant column changes MySQL has recorded since the table was last rebuilt. `fulltext` says whether the table has a FULLTEXT index. On SQL Server, `heap` says whether the table has no clustered index, and `clustered` names the one it has. On MySQL and MariaDB, `collation` is the table's default collation in lower case, such as `utf8mb4_0900_ai_ci`, which a restated text column takes unless the statement names another.

`Relation(partitioned=False, parent=None, default=None, partitions=(), indexed={}, arrays={})` gives the PostgreSQL catalog facts about one table that its size does not give. `partitioned` says whether it is a partitioned table, `parent` names the partitioned table it is a partition of, `default` names its DEFAULT partition, and `partitions` names its partitions one level down. Each name is the bare name when the search path finds the table under it, and `schema.table` otherwise. `indexed` maps each column an index uses, as a key column or inside an expression or predicate, in lower case, to the name of the collation the column is declared with, or `None` for a column of a type without one. `arrays` maps each array column, in lower case, to its type as `format_type()` writes it, such as `character varying(10)[]`. The `partitions` read fills the first four, the `indexes` read `indexed`, and the `arrays` read `arrays`.

## Report forms

These names live in `sustained.impact.report`, and `sustained impact`, `sustained plan`, and `sustained script --annotate` print through them. The module's `__all__` lists them.

```python
render(report) -> str
summary(report) -> str
table_line(table) -> str
```
{: .sig #render}

`render()` returns the report as the lines `sustained impact` prints, with `render_preflight()` after the summary when the report has a preflight. Each statement prints on one line, with each run of whitespace in it, line breaks included, printed as one space. `summary()` returns the report's last line: the counts of statements and findings, and what the answer rests on. `table_line()` renders one `TableImpact` as a table line: the table, the lock, what it blocks, the work, the hold, the size when it is known, and the rule id.

```python
statement_annotation(impact) -> list[str]
migration_annotation(migration) -> list[str]
```
{: .sig #statement_annotation}

The comment lines `script(annotate=True)` prints, without the `-- impact: ` prefix. `statement_annotation()` gives a statement's table lines and then its finding lines, or `["locks no table"]` for a statement with neither. `migration_annotation()` gives a `MigrationImpact`'s `window` lines and then its findings, and is empty for a migration with neither.

```python
report_data(report) -> dict
```
{: .sig #report_data}

The report as plain data that `json.dumps` accepts, with the keys `profile`, `version` (a string such as `"12"`), `evidence`, `read` (a sorted list), `migrations`, `counts`, and `preflight`, which is `preflight_data()` of the report's preflight or `null`. Each migration has `id`, `transactional`, `held_to_commit`, `statements`, `locks`, `windows`, and `findings`. Each statement is `{"sql": ...}` merged with `statement_data()`.

```python
statement_data(impact) -> dict
```
{: .sig #statement_data}

One statement's impact as plain data: `kind` (`null` for an unknown statement), `severity` (`null` with no findings), `confidence`, `evidence`, `tables`, and `findings`. Each table has `table`, `lock`, `blocks`, `work`, `hold`, `rows`, `bytes`, and `rule`. Each finding has `rule`, `severity`, `message`, `remedy` as a list, and `source`.

```python
finding_data(finding) -> dict
```
{: .sig #finding_data}

One `Finding` as plain data, with the keys `rule`, `severity`, `message`, `remedy` as a list, and `source`. `statement_data()` and `report_data()` give each finding in this form.

```python
flagged(statements) -> list[StatementImpact]
flagged_line(impact) -> str
```
{: .sig #flagged}

`flagged()` keeps the statements `plan` lists, in the order given: those with a `warn` or `danger` finding, and those the analysis could not read. `flagged_line()` renders one of them as `plan` prints it: the worst severity, the statement on one line, as `render()` prints it, and the rules at `warn` or above. An unknown statement reads as `info` with the rule `impact.unknown`.

```python
render_preflight(preflight) -> str
blocker_line(blocker) -> str
transaction_line(session) -> str
unread_line(statement) -> str
preflight_summary(preflight) -> str
preflight_data(preflight) -> dict
```
{: .sig #render_preflight}

`render_preflight()` returns the preflight as `sustained impact --live` prints it: a `preflight` line, each blocker's line and each transaction's line, each followed by the session's statement when it is known, each unread statement's line, and the summary. `blocker_line()` renders one blocker as `<statement> would queue behind <label> (<state> for <age>, user=..., app=..., has <lock> on <table>)`, with `waits for` in place of `has` for a lock not yet granted, and `would wait for the transaction of <label> to end` for a blocker with no `held`. `transaction_line()` renders one transaction as `<label> has had a transaction open for <age> (<state>, user=..., app=...)`. `unread_line()` renders one statement of `unread` as `<statement> is not read, so the preflight cannot check the locks it takes`. `preflight_summary()` counts both, names what was read and what was not, and counts the unread statements after `Not checked:` when there are any. `preflight_data()` returns the preflight as plain data with the keys `profile`, `older_than`, `read`, `needs` (a sorted list), `unread` (a list of statements), `blockers`, and `transactions`; each blocker has `statement`, `table`, `lock`, `held`, `granted`, and `session`, and each session has `id`, `label`, `user`, `application`, `state`, `transaction_seconds`, and `query`.
