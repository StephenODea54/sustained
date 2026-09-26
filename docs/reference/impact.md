---
layout: default
title: Impact reference
description: "Reference for sustained.impact: analyze(), read_context(), the rule profiles, the impact attached for guards, rehearse(trace=True), the ImpactReport model, EngineContext, thresholds, and the report's text and JSON forms."
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

Whether the analysis has rules for the dialect. `Dialects.POSTGRES`, `Dialects.MYSQL`, `Dialects.DEFAULT`, and `Dialects.DUCKDB` have rules.

```python
profile_for(dialect, name=None) -> Profile | None
profiles_for(dialect) -> tuple[Profile, ...]
```
{: .sig #profile_for}

These live in `sustained.impact.rules`. `profile_for()` returns the dialect's rule profile named `name`, or its first profile when `name` is `None` or names none of them, and `None` for a dialect without rules. `profiles_for()` returns every profile of the dialect, the one assumed without a server first: `postgres` on `Dialects.POSTGRES`, `mysql` then `mariadb` on `Dialects.MYSQL`, `sqlite` on `Dialects.DEFAULT`, which SQLite connections use, and `duckdb` on `Dialects.DUCKDB`.

## `read_context()`

```python
read_context(connection, dialect, exact_counts=False) -> EngineContext
await async_read_context(adapter, dialect, exact_counts=False) -> EngineContext
```
{: .sig #read_context}

The server facts the dialect's rules read, from a blocking connection or an async adapter. On PostgreSQL that is the version from `server_version_num`, the `TimeZone` and `lock_timeout` settings, each table's estimated rows and total bytes, and the schema of the connection's own schema. On MySQL and MariaDB it is `VERSION()`, which also sets the context's profile to `mysql` or `mariadb`, the `foreign_key_checks` and `lock_wait_timeout` settings, each table's estimated rows, bytes, and row format, which tables have a FULLTEXT index, on MySQL 8.0.29 and later each table's instant row versions, and the schema of the current database. On SQLite it is `sqlite_version()`, the `journal_mode` setting, each table's estimated rows from `sqlite_stat1` and bytes from the `dbstat` virtual table, the database file's bytes under the name `(database)`, and the schema. On DuckDB it is `version()`, each table's estimated rows from `duckdb_tables()`, with no bytes, and the schema. With `exact_counts=True`, the SQLite read also runs `SELECT COUNT(*)` on each table `sqlite_stat1` has no row count for, leaving out virtual tables, and adds `counts` to `read`; the other profiles ignore it. Nothing is written.

Each statement runs inside a savepoint. A statement that fails leaves its facts out of `read`, and the read goes on. Both raise `ValueError` for a dialect without impact rules.

## `Migrator.impact()`

```python
Migrator.impact(models=None, assert_algorithm=False, exact_counts=False) -> ImpactReport
await AsyncMigrator.impact(models=None, assert_algorithm=False, exact_counts=False) -> ImpactReport
```
{: .sig #migrator-impact}

The impact of the run `up()` would make: every pending migration, then the migration the models generate when `models` is given. The generated migration is diffed against the schema as it is now, before the pending migrations run, as `plan()` diffs it, and `assert_algorithm` writes the clauses `plan()` writes on it. A callable step renders no SQL and is left out. Nothing is written.

The context comes from `read_context()` on the migrator's connection, or `async_read_context()` on its adapter, with `exact_counts` passed on. Both raise `DialectError` on a dialect the analysis does not cover, before any statement runs.

## Guards over impact

`up()` reads the context the way `Migrator.impact()` does, with its own `exact_counts`, analyzes the run before the guards run, and sets each `MigrationStatement`'s `impact` attribute to its `StatementImpact`. The impact rules `max_blocking()`, `no_rewrite()`, `lock_timeout_required()`, and `no_unknown_impact()` live in `sustained.guards`; see [Guards](/reference/migrations#guards).

## `Migrator.rehearse(trace=True)`

```python
Migrator.rehearse(..., trace=True) -> Rehearsal
await AsyncMigrator.rehearse(..., trace=True) -> Rehearsal
```
{: .sig #rehearse-trace}

Rehearses the run with each statement observed; see [Observed impact](/impact#observed-impact). The result's `impact` is the run's `ImpactReport`, with the context read at the start of the rehearsal and each observed statement's lock and work in place of the prediction. It covers every migration the up sweep reached, in run order, including the ones the rehearsal left out, which keep their prediction. `impact` is `None` for a rehearsal without `trace`, and for one with nothing pending. `trace=True` raises `DialectError` on any dialect other than `POSTGRES` and `MYSQL`, before any statement runs. On `MYSQL` it needs `scratch=True`, as every rehearsal there does.

The profile's `trace` attribute names how a rehearsal observes its statements, and is `None` for a profile the rehearsal cannot observe. It is a `Trace(tables, report, sighting=None, attempts=None, refused=...)`: `tables()` reads the tables that exist before the run, and `report(predicted, observations, existing, profile)` puts the observations in place of the prediction. A trace with `sighting` reads it before and after each statement, as on Postgres. A trace with `attempts` runs the statement with each clause `attempts(impact, profile)` returns, in order, until one runs without an error that `refused(error)` reads as a refusal, as on MySQL and MariaDB; the observation is a `Probe(accepted, refused)`, the accepted clause and each refused one with the server's reason.

The Postgres names live in `sustained.impact.rules.postgres.trace`:

```python
sighting_plan(tables) -> Sighting
tables_plan() -> frozenset[int] | None
```
{: .sig #sighting_plan}

Read plans, generators that yield SQL and take each statement's rows back, which `run_plan()` in `sustained.introspect.runner` drives. `sighting_plan()` reads the table locks the transaction holds and the files of the named tables and their indexes. `tables_plan()` reads the oids of every table that exists.

`Sighting(locks, names, storage, read)` holds one read: the lock modes held on each table, by oid, in the rules' names, such as `SHARE`; the lower case names that find each table; each named table's `File(is_index, filenode, size)` records, by relation oid; and `read`, which holds `locks` and `storage` for the parts that were read.

```python
observe(impact, before, after, existing, profile) -> StatementImpact
with_observations(report, observations, existing, profile) -> ImpactReport
```
{: .sig #observe}

`observe()` returns one statement's impact with the facts its two sightings show, and an `impact.mismatch` finding for each difference. `existing` holds the oids of the tables that existed before the run, or `None` to compare every table. It returns the impact unchanged when the locks were not read both times. `with_observations()` applies `observe()` across a report, keyed by migration id and the statement's position in its migration, counting from 0, and reads each migration's locks and windows again.

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

## `ImpactReport`

```python
ImpactReport(profile, version, evidence, migrations, read=frozenset())
```
{: .sig}

`profile` is the rule profile: `'postgres'`, `'mysql'`, `'mariadb'`, `'sqlite'`, or `'duckdb'`. `version` is the server version the rules assumed, as a tuple of ints. `evidence` is what the report rests on. `migrations` is a tuple of `MigrationImpact`, in run order. `read` is the context's `read`: the facts that came from the server.

| Member | Returns |
| --- | --- |
| `statements` | Every `StatementImpact`, in run order |
| `findings` | Every `Finding`, statement findings first within each migration |
| `count(severity)` | How many findings carry that severity |

## `MigrationImpact`

```python
MigrationImpact(migration_id, transactional, statements, locks=(), windows=(), findings=(), held_to_commit=False)
```
{: .sig}

One migration: its id, or `None` for statements with no migration, its transaction flag, a tuple of `StatementImpact`, the `Lock`s it takes, the `Window`s those locks make, and findings about the migration as a whole, such as `window.held` and `window.lock_order`. `held_to_commit` is true when the locks last until the migration commits: inside a transaction, on an engine whose DDL does not commit on its own. Otherwise each statement is a window of its own. On SQLite, whose writes lock the whole database, the migration's locks make one `Window` named `(database)`, and it draws no `window.held` finding.

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

What the statement does to one table: the engine's lock name, or `None` for no lock, what it blocks, the work, how long it is held, the row and byte estimates when known, and the id of the rule that gave the answer. DuckDB takes no locks, so there the lock name is the conflict the statement opens on the table: `altered table`, `changed rows`, or `catalog entry`; see [DuckDB](/impact#duckdb).

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
| `impact.mismatch` | `warn` | A traced rehearsal saw the server take another lock than the rules predicted, copy a file the rules did not predict, or copy none where they predicted a rewrite or an index build. On MySQL and MariaDB: the server accepted another clause than the rules predicted, or its clause copied the table where they predicted none, or nothing where they predicted a copy. |
| `impact.assumed_profile` | `info` | No context was given on a dialect with more than one profile, so the first was assumed. |
| `pg.lock_timeout` | `warn` | A lock that blocks writes or more waits with no `lock_timeout` in scope, from a `SET` earlier in the run or from the connection's settings. |
| `mysql.lock_timeout`, `mariadb.lock_timeout` | `warn` | A statement that takes the exclusive metadata lock runs with no `lock_wait_timeout` below a day in scope. |
| `mysql.refused`, `mariadb.refused` | `warn` | The statement spells an `ALGORITHM` or `LOCK` the change cannot run with, so the server refuses it. |
| `window.held` | `warn` | A table stays blocked until the commit across heavier work from a later statement. |
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
EngineContext(profile, version, edition=None, settings={}, tables={}, schema=None, read=frozenset())
```
{: .sig}

The server facts the rules read: the profile, the version as a tuple of ints, the edition, settings such as `TimeZone` and `lock_timeout`, a mapping of lower case table name to `TableStats`, the schema `Snapshot`, and `read`, the names of the facts that came from a server: `version`, `settings`, `sizes`, and `schema`, on MySQL and MariaDB also `fulltext` and `row_versions`, and on SQLite `counts` when `exact_counts` read the table list. With `read` empty, the report's evidence is `static`, and `catalog` otherwise. `read_context()` builds one from a connection.

`read_context()` keys each table as `schema.table`, and also by its bare name when the search path finds it under that name, or on MySQL and MariaDB when it is in the current database. `stats(table)` returns a table's `TableStats`, or unknown stats for a table the read did not see.

These methods read the schema, and return `None` or an empty tuple when the schema was not read or does not hold what they look for. A dotted name finds a table by its last part, since the read covers one schema.

| Method | Returns |
| --- | --- |
| `table(name)` | The `IntrospectedTable` |
| `column_type(table, column)` | The column's current type |
| `index_table(index)` | The name of the table the index is on |
| `references(table, columns=None)` | The tables the table's foreign keys point at; with `columns`, only the keys that use one of them |
| `referenced_by(table, columns=None)` | The other tables whose foreign keys point at the table; with `columns`, only the keys that point at one of them |
| `foreign_key_target(table, name)` | The table the named foreign key points at |

`TableStats(rows=None, bytes=None, row_format=None, row_versions=None, fulltext=None)` gives one table's size estimates and the storage facts the InnoDB rules read, each `None` where it was not read. On PostgreSQL, `rows` is `None` for a table that was never vacuumed or analyzed. `row_format` is the InnoDB row format in upper case, such as `DYNAMIC` or `COMPRESSED`. `row_versions` counts the instant column changes MySQL has recorded since the table was last rebuilt. `fulltext` says whether the table has a FULLTEXT index.

## Report forms

These names live in `sustained.impact.report`, and `sustained impact` and `sustained plan` print through them.

```python
render(report) -> str
```
{: .sig #render}

The report as the lines `sustained impact` prints.

```python
report_data(report) -> dict
```
{: .sig #report_data}

The report as plain data that `json.dumps` accepts, with the keys `profile`, `version` (a string such as `"12"`), `evidence`, `read` (a sorted list), `migrations`, and `counts`. Each migration has `id`, `transactional`, `held_to_commit`, `statements`, `locks`, `windows`, and `findings`. Each statement is `{"sql": ...}` merged with `statement_data()`.

```python
statement_data(impact) -> dict
```
{: .sig #statement_data}

One statement's impact as plain data: `kind` (`null` for an unknown statement), `severity` (`null` with no findings), `confidence`, `evidence`, `tables`, and `findings`. Each table has `table`, `lock`, `blocks`, `work`, `hold`, `rows`, `bytes`, and `rule`. Each finding has `rule`, `severity`, `message`, `remedy` as a list, and `source`.

```python
flagged(statements) -> list[StatementImpact]
flagged_line(impact) -> str
```
{: .sig #flagged}

`flagged()` keeps the statements `plan` lists, in the order given: those with a `warn` or `danger` finding, and those the analysis could not read. `flagged_line()` renders one of them as `plan` prints it: the worst severity, the statement, and the rules at `warn` or above. An unknown statement reads as `info` with the rule `impact.unknown`.
