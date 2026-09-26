---
layout: default
title: Impact reference
description: "Reference for sustained.impact: analyze(), the ImpactReport model, EngineContext, thresholds, and the report's text and JSON forms."
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

## `Migrator.impact()`

```python
Migrator.impact(models=None) -> ImpactReport
await AsyncMigrator.impact(models=None) -> ImpactReport
```
{: .sig #migrator-impact}

The impact of the run `up()` would make: every pending migration, then the migration the models generate when `models` is given. The generated migration is diffed against the schema as it is now, before the pending migrations run, as `plan()` diffs it. A callable step renders no SQL and is left out. Nothing is written.

The analysis is static: the rules assume the support floor and read no table sizes. Both raise `DialectError` on a dialect the analysis does not cover, before any statement runs.

## `ImpactReport`

```python
ImpactReport(profile, version, evidence, migrations)
```
{: .sig}

`profile` is the rule profile, such as `'postgres'`. `version` is the server version the rules assumed, as a tuple of ints. `evidence` is what the report rests on. `migrations` is a tuple of `MigrationImpact`, in run order.

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
| `pg.lock_timeout` | `warn` | A lock that blocks writes or more waits with no `lock_timeout` in scope. |
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

The server facts the rules read: the profile, the version as a tuple of ints, the edition, settings such as `TimeZone`, a mapping of lower case table name to `TableStats`, the schema `Snapshot`, and `read`, the names of the facts that came from a server. With `read` empty, the report's evidence is `static`, and `catalog` otherwise. Sustained does not yet read a context from a connection, so a context passed to `analyze()` is one you build.

`TableStats(rows=None, bytes=None)` holds one table's size estimates.

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

The report as plain data that `json.dumps` accepts, with the keys `profile`, `version` (a string such as `"12"`), `evidence`, `migrations`, and `counts`. Each migration has `id`, `transactional`, `statements`, `locks`, `windows`, and `findings`. Each statement is `{"sql": ...}` merged with `statement_data()`.

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
