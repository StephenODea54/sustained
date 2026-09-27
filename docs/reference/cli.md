---
layout: default
title: Command line reference
description: "Reference for the sustained command line: plan, impact, status, rehearse, migrate, down, validate, repair, script, and baseline, with options and exit codes."
---

The `sustained` console script installs with the package, and you can also run it as `python -m sustained`.

```
sustained <command> [--config MODULE] [command options]
```

Every command imports a config module from the current directory, `sustained_config` by default. Pass `--config MODULE` to name a different module.

Guide: [Schema and Migrations](/schema#command-line).

## Commands

| Command | Options | Does |
| --- | --- | --- |
| `plan` | `--json`, `--exact-counts`, `--online`, `--assert-algorithm` | Shows the pending migrations, the problems, and the model drift. |
| `impact` | `--json`, `--exact-counts`, `--live`, `--older-than SECONDS`, `--online`, `--assert-algorithm` | Shows the locks, blocking, and work of each statement in the run. `--live` adds the sessions each statement would wait behind now. |
| `status` | `--json` | Shows every migration's state: applied, pending, or changed. |
| `rehearse` | `--json`, `--trace`, `--online`, `--assert-algorithm` | Runs the pending migrations up and back down, then rolls it all back. |
| `migrate` | `--target ID`, `--no-validate`, `--allow-out-of-order`, `--unrehearsed`, `--exact-counts`, `--preflight warn\|refuse`, `--online`, `--assert-algorithm` | Applies pending migrations in order. |
| `down` | `--steps N` (default 1) or `--to ID` | Reverts applied migrations, newest first. |
| `validate` | `--json` | Checks the tracking table against the migrations. |
| `repair` | | Fixes tracking rows after failures or intentional edits. |
| `script` | `up` or `down` (default `up`), `--annotate`, `--exact-counts` | Prints the SQL a run would execute, without running it. `--annotate` prints each statement's impact above it as `-- impact:` comments. |
| `baseline` | `TARGET` (required) | Records migrations as applied without running them. |

`--steps` and `--to` are mutually exclusive.

A flag that changes nothing is an error. `--older-than` needs `--live`, and `script --exact-counts` needs `--annotate`; without them `argparse` exits 2 with a usage error. `--older-than` also refuses an age below 0 or `nan`. `--online` and `--assert-algorithm` change the migration the config's `models` generate: `--online` on PostgreSQL, MySQL, and MariaDB, where MySQL and MariaDB read it as `--assert-algorithm`, and `--assert-algorithm` on MySQL and MariaDB, where it writes the predicted `ALGORITHM` and `LOCK` clauses on the generated statements; see [Asserting the algorithm](/impact#asserting-the-algorithm). On another dialect either flag exits 1 before the command runs. The config module's `online` and `assert_algorithm` attributes are not checked.

## Exit codes

| Code | Means |
| --- | --- |
| 0 | Success, or nothing to do. |
| 1 | A failure: config, connection, validation problems, or a migration error. Details on stderr. |
| 2 | `plan` only: work is waiting. |
| 3 | `plan` and `migrate`: a guard blocked a statement. |
| 4 | `migrate` only: the run removes data and no rehearsal proved it. |
| 5 | `migrate` only: with `preflight` set to `refuse`, another session has a lock a statement of the run would wait for. |

`plan` uses every code except 4 and 5. It exits 0 when the database is current, 2 when migrations are pending or the models have drifted, 3 when a guard blocked a statement, and 1 when validation found problems. Problems outrank a blocked statement, and a blocked statement outranks pending work.

`argparse` also exits 2 on a usage error. If your script treats 2 as "work is waiting", check stderr for an `error:` line first.

`rehearse` exits 1 when an up step or a down step failed, when the models did not land, or when the schema did not come back. A migration with no down step is not a failure, so `rehearse` exits 0 for it.

`rehearse --trace` observes the locks each statement takes and the tables it rewrites, and prints the impact report after the rehearsal's lines, with the observed facts in place of the prediction; see [Observed impact](/impact#observed-impact). An `impact.mismatch` finding in the report does not change the exit code. On MySQL and MariaDB it runs each ALTER TABLE, CREATE INDEX, and DROP INDEX on the scratch database with each ALGORITHM and LOCK clause until the server accepts one. On SQL Server it reads the locks, the partitions of the tables each statement names, and the log the transaction writes, on the scratch database. `--trace` needs PostgreSQL, MySQL, MariaDB, or SQL Server, and exits 1 on any other dialect.

`migrate` exits 4 when the run would remove data and no passing rehearsal covers those statements. The message names the statements and both ways forward, and repeats `--target` when the run had one.

A `migrate` that fails part way leaves the migrations it already applied in place. Their ids print on stdout as `applied  <id>` lines before the error, whatever stopped the run: a failing statement, a guard block, or a refusal on the migration generated from the models.

`validate` exits 1 when it finds problems, and 0 when it finds none. The exit codes are the same with and without `--json`.

`impact` exits 0 when it prints the report, whatever the report says, and 1 on a failure, including a dialect the analysis does not cover. Blocking a run on impact is the job of guards. `impact --live` also exits 1 on a dialect without a [live preflight](/impact#live-preflight), which SQLite and DuckDB lack.

`migrate --preflight refuse` reads the sessions the run would wait behind after the guards pass, and exits 5 when there is one, with each blocker on stderr. The first read comes before any migration applies. A run with `models` reads the generated migration a second time, once the registered migrations have applied, and a refusal on that read leaves them applied, with their ids on stdout as `applied  <id>` lines. `--preflight warn` prints the same lines on stderr as `preflight: ...` and the run goes on. The flag takes precedence over the config module's `preflight` attribute. On a dialect without a [live preflight](/impact#live-preflight), SQLite and DuckDB among them, `migrate` with either mode, from the flag or the config, exits 1 before the run starts.

## The config module

| Attribute | Required | Type | Default |
| --- | --- | --- | --- |
| `connection` | one of the two | A DB-API 2.0 connection | checked first |
| `get_connection` | one of the two | `() -> Connection` | used when `connection` is absent |
| `migrations` | no | `list[Migration]` | `[]` |
| `migrations_dir` | no | Path to `.up.sql` / `.down.sql` / `.repeat.sql` files | `None` |
| `placeholders` | no | `dict[str, str]` filling `${key}` markers | `None` |
| `models` | no | List of model classes | `None` |
| `dialect` | no | A `Dialects` member, or its name: `'postgres'`, `'MSSQL'` | `Dialects.DEFAULT` |
| `table` | no | Tracking table name | `'sustained_migrations'` |
| `rehearsal_table` | no | Rehearsal table name | `'sustained_rehearsals'` |
| `tracking_table_options` | no | `TableOptions` | `None` |
| `guards` | no | `list[Guard]` from `sustained.guards` | `[]` |
| `get_rehearsal_connection` | no | `() -> Connection`, a scratch database | `None` |
| `rehearsal_lock_timeout` | no | `float`, seconds; `rehearse`, and the rehearsal `impact` runs on the scratch database, pass it to `rehearse(lock_timeout=...)`, so a statement that waits longer for a lock fails the rehearsal. A value that is not a number above 0 exits 1 | `None` |
| `assert_algorithm` | no | `bool`; `plan`, `impact`, `migrate`, and `rehearse` pass it to the diff of `models`, as `--assert-algorithm` does | `False` |
| `online` | no | `bool`; `plan`, `impact`, `migrate`, and `rehearse` generate the online form of the migrations the models need, as `--online` does. See [Online migrations](/impact#online-migrations) | `False` |
| `exact_counts` | no | `bool`; `plan`, `impact`, `migrate`, and `script --annotate` count the rows of each SQLite table `sqlite_stat1` has no row count for, as `--exact-counts` does | `False` |
| `preflight` | no | `'warn'` or `'refuse'`; `migrate` reads the sessions the run would wait behind before it applies anything, as `--preflight` does | `None` |
| `preflight_older_than` | no | A number of seconds, 0 or more; the age from which the preflight lists an open transaction, in `migrate` with a preflight and `impact --live`, unless `--older-than` is given. Only those read it, and a value that is not a number fails them with a message that names the attribute | `60` |
| `before_migrate` | no | `(connection) -> None` | not called |
| `after_migrate` | no | `(connection, applied) -> None` | not called |
| `on_error` | no | `(connection, migration_id, error) -> None` | not called |

A config module that defines neither `connection` nor `get_connection` raises `ValueError`. An unknown dialect name raises `ValueError` listing the valid names.

Migrations from `migrations_dir` are appended after the ones in `migrations`, so you can use both sources together.

```python
# sustained_config.py
import psycopg

from models import Show, Venue


def get_connection():
    return psycopg.connect('postgresql://localhost/app')


migrations_dir = 'migrations'
models = [Venue, Show]
dialect = 'postgres'
```

### Guards

`plan` runs the guards over every statement it lists, the pending migrations and the drift together, and prints a `guards` section beside the other sections. With `--json`, each verdict appears on the statement object it flags, as `{"rule", "verdict"}`.

`migrate` refuses a blocked run before any statement executes and exits 3, with the rule and the statement on stderr. A warning verdict prints on stderr and the run continues. When no guard reads impact, `migrate` also prints each `danger` finding of the [impact analysis](/impact#guards-over-impact) on stderr, and the run continues. `rehearse` does not enforce guards.

No flag skips a guard for one run. Fix the statement, or take the rule out of the config module.

### Callbacks

Only `migrate` calls `before_migrate` and `after_migrate`. `on_error` also runs when `down` fails. `rehearse` calls none of them, because it rolls everything back. The CLI collects the callbacks into a `Callbacks` object and hands that object to the migrator, and the migrator makes the calls, so the same hooks are available through the API.

`before_migrate` runs before the run starts, ahead of validation and the advisory lock. `after_migrate` runs only when at least one migration applied, so a run with nothing to do calls nothing. `on_error` runs after a failure and before the failure reaches the shell. Its `migration_id` argument is `None` when the run failed before it reached a migration, as it does after a guard block or a validation problem.

The CLI skips a callback that is not callable. When `on_error` itself raises, its error prints to stderr, and the original migration error still decides the exit code.

### Rehearsal connection

When the config defines `get_rehearsal_connection()`, `rehearse` builds a second migrator on that connection and rehearses on the scratch database instead. The dialect check does not apply there, the changes may remain after the rollback, and the footer says so. The scratch connection closes when the command ends.

The rehearsal row goes on the real database rather than the scratch one, keyed against the real database's applied history and pending set. Sustained writes the row only when the scratch run applied every migration pending on the real database. Otherwise the output says the row was not recorded.

`impact` uses the same connection when the config names `models` and migrations are pending. It rehearses the pending migrations and the diff of the models on the scratch database, as `rehearse` does there, and analyzes the pending migrations with the migrations that diff generated, with the facts of the real database. Neither database keeps a change. A pending migration that fails on the scratch database exits 1, since the diff then never ran.

## Output

Output is plain text, one record per line, with no colour.

```console
$ sustained status
applied  001_create_venues
pending  002_create_shows
changed  upcoming_shows
```

```console
$ sustained plan
pending
  003_sessions  2 statements
  004_trim      1 statement
    destructive  ALTER TABLE users DROP COLUMN legacy
  vw_active     1 statement  repeat changed

drift
  ALTER TABLE users ADD COLUMN bio TEXT

2 pending migrations, 1 drift statement
run: sustained rehearse
```

The footer points at `rehearse` when a pending migration removes data, because `migrate` refuses that run without a rehearsal row. Otherwise the footer points at `migrate`. A blocked statement replaces the footer with `blocked: fix the statement, or take the rule out of the guard list to run it anyway`.

A `guards` section follows the other sections when the config names `guards`, with one line per verdict:

```console
guards
  block  no_drops          ALTER TABLE users DROP COLUMN legacy
  warn   no_table_rewrite  ALTER TABLE users ALTER COLUMN age TYPE BIGINT
```

An `impact` section follows the guards section, with one line per statement that has a `warn` or `danger` finding or that the analysis could not read. Each line gives the worst severity, the statement, and the rules at `warn` or above. The section appears only on a dialect the analysis covers, and it does not change the exit code. See [Statement impact](/impact).

```console
impact
  danger  CREATE INDEX ix_orders_customer ON orders (customer_id)  [pg.create_index, pg.lock_timeout]
  info    GRANT SELECT ON orders TO reporting  [impact.unknown]
```

`impact` prints the report for the run `migrate` would make: the pending migrations, then the migration the config's `models` generate. `migrate` diffs the models after the pending migrations apply, so while migrations are pending `impact` diffs them on the scratch database of `get_rehearsal_connection()`; see [Rehearsal connection](#rehearsal-connection). Without a scratch database it leaves the models' migration out and prints this line after the report:

```console
models not diffed: migrate diffs them after the pending migrations apply; define get_rehearsal_connection() in the config module to diff them on a scratch database
```

With nothing pending, `impact` diffs the models against the real database.

```console
$ sustained impact
20260926_orders  transaction
  CREATE INDEX ix_orders_customer ON orders (customer_id)
    orders  SHARE  blocks writes  index_build  transaction  ~41.2M rows, 12.4 GB  [pg.create_index]
    danger  writes to orders wait for the whole index build; build it CONCURRENTLY in a migration with transactional=False
    fix     CREATE INDEX CONCURRENTLY ix_orders_customer ON orders (customer_id)
    warn    no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on orders queues behind it, for as long as the longest open transaction runs
    fix     SET LOCAL lock_timeout = '5s'
  window  orders: SHARE from statement 1, held to commit

1 statement, 1 danger, 1 warn. Evidence: catalog (PostgreSQL 16.4)
```

`impact --live` adds a `preflight` section after the summary: one line per session a statement would wait behind, one line per other transaction open at least `--older-than` seconds, and a count. See [Live preflight](/impact#live-preflight).

```console
preflight
  ALTER TABLE orders ADD COLUMN note text would queue behind pid 4121 (idle in transaction for 42m, user=billing, app=billing-worker, has ACCESS SHARE on orders)
    last statement: SELECT * FROM orders WHERE id = 1
  1 blocker, 0 transactions open 60s or longer. Read: locks, transactions
```

The drift section appears only when the config names `models`. It reports every difference, drops included, even though `migrate` never generates a drop. A drift section that contains only drops says so instead of offering the command. The `run:` line prints only when validation found no problems.

```console
$ sustained rehearse
rehearsed 003_sessions  up ok, down ok, reversed
rehearsed 004_trim      up ok, down ok, reversed
rehearsed vw_active     up ok, no down step (repeatable)
rollback complete, database unchanged
rehearsal row recorded
```

`rehearsal row recorded` means Sustained wrote the row where `migrate` will read it. The words after the id are the checks that passed, in order: `up ok`, `landed` for the migration generated from the config's `models`, `down ok`, and `reversed`. A check that failed reads `not landed` or `not reversed`, with the objects listed underneath and `run: sustained plan` at the end.

A failure names the statement that failed, and the migrations under it that never ran:

```console
$ sustained rehearse
rehearsed 003_sessions  up ok, down not rehearsed: the run stopped
failed    004_trim      up: column "legacy" of relation "users" does not exist
rollback complete, database unchanged
run: sustained plan
```

When the config names `models`, `migrate` reads the schema back after a successful run. It prints `schema matches the models`, or one `drift    <gap>` line per remaining difference. This report does not change the exit code.

The other commands print `applied  <id>`, `reverted <id>`, `repaired <action>`, or `baselined <id>`, one per line. With nothing to do they print `Nothing to apply.`, `Nothing to revert.`, `Nothing to repair.`, `Nothing to baseline.`, or `Nothing to rehearse.` `validate` prints `OK`, or one `problem  <text>` line per problem.

Errors go to stderr as `error: <message>`, or as `error in '<migration id>': <message>` when the failure came from a known migration.

## JSON output

`status`, `validate`, `plan`, `impact`, and `rehearse` take `--json` and print one object to stdout.

```console
$ sustained plan --json
{
  "pending": [
    {
      "id": "004_trim",
      "state": "pending",
      "repeatable": false,
      "statements": [
        {
          "sql": "ALTER TABLE users DROP COLUMN legacy",
          "destructive": true,
          "guards": [{"rule": "no_drops", "verdict": "block"}],
          "impact": {
            "kind": "alter_table",
            "severity": "warn",
            "confidence": "known",
            "evidence": "static",
            "tables": [
              {
                "table": "users",
                "lock": "ACCESS EXCLUSIVE",
                "blocks": "reads_and_writes",
                "work": "catalog",
                "hold": "brief",
                "rows": null,
                "bytes": null,
                "rule": "pg.drop_column"
              }
            ],
            "findings": [
              {
                "rule": "pg.lock_timeout",
                "severity": "warn",
                "message": "no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on users queues behind it, for as long as the longest open transaction runs",
                "remedy": ["SET LOCAL lock_timeout = '5s'"],
                "source": "https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-LOCK-TIMEOUT"
              }
            ]
          }
        }
      ],
      "destructive": ["ALTER TABLE users DROP COLUMN legacy"]
    }
  ],
  "problems": [],
  "drift": null,
  "error": null
}
```

Every place a command reports SQL uses that statement object, including `drift`. When the config names no models, `drift` is `null` rather than `[]`, so a caller can tell "nothing was compared" from "compared and found no gap". `statements` is `null` for a callable step, which renders no SQL; before version 2.13.0 `statements` was a count. A guard verdict appears on the statement it flags, as `{"rule", "verdict"}`, and a statement no guard flagged has `[]`. The `guards` key is present from version 2.15.0 onward. `impact` is the statement's impact in the form [`statement_data()`](/reference/impact#statement_data) gives, and is `null` on a dialect the analysis does not cover.

`impact --json` prints the report in the form [`report_data()`](/reference/impact#report_data) gives, with the top-level keys `profile`, `version`, `evidence`, `read`, `migrations`, `counts`, `preflight`, and `error`, and the key `models_diffed`. `preflight` is `null` without `--live`, and otherwise has the keys [`preflight_data()`](/reference/impact#render_preflight) gives. `models_diffed` is `null` when the config names no `models`, `true` when the report includes the migrations they generate, and `false` when `impact` left them out, as the `models not diffed` line says. Each statement in a migration has `sql` and the keys of the plan's `impact` object.

`rehearse --json` prints:

```console
$ sustained rehearse --json
{
  "rehearsed": [
    {
      "id": "004_trim",
      "up_ok": true,
      "down_ok": true,
      "error": null,
      "landed": null,
      "reversed": []
    }
  ],
  "scratch": false,
  "key": "9c1f...",
  "recorded": true,
  "ok": true,
  "impact": null,
  "error": null
}
```

`landed` and `reversed` are `null` when the check did not run, `[]` when the check passed, and the lines naming the trouble when the check failed. `key` names the content the run covered. `recorded` says whether Sustained wrote the row where `migrate` will read it. `impact` is the traced report in the form `impact --json` prints, and is `null` without `--trace`.

`status --json` prints `{"migrations": [{"id": ..., "state": ...}], "error": null}`. `validate --json` prints `{"ok": ..., "problems": [...], "error": null}`.

A command that fails still prints one object. Every key is `null`, because nothing was evaluated, and `error` contains the message that also goes to stderr. The exit code is the same as without `--json`. For example, a config module that will not import gives:

```console
$ sustained validate --json --config missing
{
  "ok": null,
  "problems": null,
  "error": "No module named 'missing'"
}
```

The `error` key is present from version 2.25.0 onward. A usage error that `argparse` rejects prints no object, because it exits before the command runs.
