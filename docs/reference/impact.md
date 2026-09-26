---
layout: default
title: Impact reference
description: "Reference for sustained.impact: analyze(), read_context(), rehearse(trace=True), the ImpactReport model, EngineContext, thresholds, and the report's text and JSON forms."
---

These names live in `sustained.impact`, except where a section names another module.

Guide: [Statement impact](/impact).

## `analyze()`

```python
analyze(statements, dialect, context=None, thresholds=Thresholds()) -> ImpactReport
```
{: .sig #analyze}

The impact of the statements a run would apply, in run order. `statements` is the same `Sequence[str]` guards receive. A `MigrationStatement` names its migration and its transaction flag, and consecutive statements with the same id and flag form one migration. A plain `str` reads as a statement of an unnamed migration inside a transaction.

Without a `context`, the rules assume the dialect's support floor, and the report's evidence is `static`. `analyze()` connects to no database. It raises `ValueError` for a dialect without impact rules.

```python
supported(dialect) -> bool
```
{: .sig #supported}

Whether the analysis has rules for the dialect. Only `Dialects.POSTGRES` has rules.

## `read_context()`

```python
read_context(connection, dialect) -> EngineContext
await async_read_context(adapter, dialect) -> EngineContext
```
{: .sig #read_context}

The server facts the dialect's rules read, from a blocking connection or an async adapter. On PostgreSQL that is the version from `server_version_num`, the `TimeZone` and `lock_timeout` settings, each table's estimated rows and total bytes, and the schema of the connection's own schema. Nothing is written.

Each statement runs inside a savepoint. A statement that fails leaves its facts out of `read`, and the read goes on. Both raise `ValueError` for a dialect without impact rules.

## `Migrator.impact()`

```python
Migrator.impact(models=None) -> ImpactReport
await AsyncMigrator.impact(models=None) -> ImpactReport
```
{: .sig #migrator-impact}

The impact of the run `up()` would make: every pending migration, then the migration the models generate when `models` is given. The generated migration is diffed against the schema as it is now, before the pending migrations run, as `plan()` diffs it. A callable step renders no SQL and is left out. Nothing is written.

The context comes from `read_context()` on the migrator's connection, or `async_read_context()` on its adapter. Both raise `DialectError` on a dialect the analysis does not cover, before any statement runs.

## `Migrator.rehearse(trace=True)`

```python
Migrator.rehearse(..., trace=True) -> Rehearsal
await AsyncMigrator.rehearse(..., trace=True) -> Rehearsal
```
{: .sig #rehearse-trace}

Rehearses the run with each statement observed; see [Observed impact](/impact#observed-impact). The result's `impact` is the run's `ImpactReport`, with the context read at the start of the rehearsal and each observed statement's lock and work in place of the prediction. It covers every migration the up sweep reached, in run order, including the ones the rehearsal left out, which keep their prediction. `impact` is `None` for a rehearsal without `trace`, and for one with nothing pending. `trace=True` raises `DialectError` on any dialect other than `POSTGRES`, before any statement runs.

These names live in `sustained.impact.trace`:

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

## `ImpactReport`

```python
ImpactReport(profile, version, evidence, migrations, read=frozenset())
```
{: .sig}

`profile` is the rule profile, such as `'postgres'`. `version` is the server version the rules assumed, as a tuple of ints. `evidence` is what the report rests on. `migrations` is a tuple of `MigrationImpact`, in run order. `read` is the context's `read`: the facts that came from the server.

| Member | Returns |
| --- | --- |
| `statements` | Every `StatementImpact`, in run order |
| `findings` | Every `Finding`, statement findings first within each migration |
| `count(severity)` | How many findings carry that severity |

## `MigrationImpact`

```python
MigrationImpact(migration_id, transactional, statements, locks=(), windows=(), findings=())
```
{: .sig}

One migration: its id, or `None` for statements with no migration, its transaction flag, a tuple of `StatementImpact`, the `Lock`s it holds, the `Window`s those locks make, and findings about the migration as a whole, such as `window.held` and `window.lock_order`.

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

What the statement does to one table: the engine's lock name, or `None` for no lock, what it blocks, the work, how long it is held, the row and byte estimates when known, and the id of the rule that gave the answer.

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
| `impact.mismatch` | `warn` | A traced rehearsal saw the server take another lock than the rules predicted, copy a file the rules did not predict, or copy none where they predicted a rewrite or an index build. |
| `pg.lock_timeout` | `warn` | A lock that blocks writes or more waits with no `lock_timeout` in scope, from a `SET` earlier in the run or from the connection's settings. |
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

The server facts the rules read: the profile, the version as a tuple of ints, the edition, settings such as `TimeZone` and `lock_timeout`, a mapping of lower case table name to `TableStats`, the schema `Snapshot`, and `read`, the names of the facts that came from a server: `version`, `settings`, `sizes`, and `schema`. With `read` empty, the report's evidence is `static`, and `catalog` otherwise. `read_context()` builds one from a connection.

`read_context()` keys each table as `schema.table`, and also by its bare name when the search path finds it under that name. `stats(table)` returns a table's `TableStats`, or unknown stats for a table the read did not see.

These methods read the schema, and return `None` or an empty tuple when the schema was not read or does not hold what they look for. A dotted name finds a table by its last part, since the read covers one schema.

| Method | Returns |
| --- | --- |
| `table(name)` | The `IntrospectedTable` |
| `column_type(table, column)` | The column's current type |
| `index_table(index)` | The name of the table the index is on |
| `references(table, columns=None)` | The tables the table's foreign keys point at; with `columns`, only the keys that use one of them |
| `referenced_by(table, columns=None)` | The other tables whose foreign keys point at the table; with `columns`, only the keys that point at one of them |
| `foreign_key_target(table, name)` | The table the named foreign key points at |

`TableStats(rows=None, bytes=None)` holds one table's size estimates. On PostgreSQL, `rows` is `None` for a table that was never vacuumed or analyzed.

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

The report as plain data that `json.dumps` accepts, with the keys `profile`, `version` (a string such as `"12"`), `evidence`, `read` (a sorted list), `migrations`, and `counts`. Each migration has `id`, `transactional`, `statements`, `locks`, `windows`, and `findings`. Each statement is `{"sql": ...}` merged with `statement_data()`.

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
