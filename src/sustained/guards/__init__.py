"""
Rules that read the statements an up run would apply.

A guard takes the statement list and the dialect, and returns a verdict
for each statement it objects to. A `block` verdict stops the run before
any statement executes. A `warn` verdict prints and lets the run go on.

Guards are given to the migrator (`Migrator(..., guards=[...])`) or named
in the config module (`guards = [...]`) for the CLI. They run over every
statement an up run would apply: file migrations, Python migrations with
SQL steps, and the diff against the models. A callable step renders no
SQL, so guards cannot read it, the same limit the destructive labels
carry.

Down runs are not checked. A down undoes work that already passed the
rules, so a rule like `no_drops()` would block every rollback of a
create.

The built-in rules are factories, so every one reads the same at the call
site:

    guards = [no_drops(), max_statements(50)]

Each statement a run hands a guard is a `MigrationStatement`: a string
that also names the migration it came from and says whether that
migration runs inside a transaction. A rule reads it as a plain string,
so a guard written against `Sequence[str]` keeps working, and a rule
about a per-transaction setting can tell one migration from the next.

The textual rules (`no_drops()`, `index_must_be_concurrent()`,
`no_table_rewrite()`, `no_lock_without_timeout()`, `max_statements()`)
scan like the destructive labels: a rule matches on the
words in the statement and never parses SQL. Comments and the text
inside quotes are kept out of the scan, so a rule reads neither a
commented-out drop nor a drop named in a string literal. The verdict
prints the statement with its literals intact.

The impact rules (`max_blocking()`, `no_rewrite()`,
`lock_timeout_required()`, `no_unknown_impact()`) read each statement's
`StatementImpact` instead of its text; see sustained.impact. The
migrator attaches it to each statement before the guards run, analyzed
with the server facts it read from the connection. A statement without
one, such as a plain `str`, is analyzed on the spot with no context,
so a table's size is unknown there. A size threshold whose size is
unknown counts as exceeded, unless the rule is given
`assume_small=True`. The impact rules are silent on a dialect the
analysis does not cover.

A guard with a true `reads_impact` attribute counts as an impact rule.
When no configured guard is one, `up()` prints each `danger` finding
on stderr, since no rule reads them.
"""

from sustained.guards.core import (
    BLOCK,
    WARN,
    Guard,
    Verdict,
    blocking,
    run_guards,
    warnings_only,
)
from sustained.guards.impact import (
    lock_timeout_required,
    max_blocking,
    no_rewrite,
    no_unknown_impact,
    reads_impact,
    statement_impacts,
)
from sustained.guards.text import (
    index_must_be_concurrent,
    max_statements,
    no_drops,
    no_lock_without_timeout,
    no_table_rewrite,
)

__all__ = [
    "BLOCK",
    "WARN",
    "Guard",
    "Verdict",
    "blocking",
    "index_must_be_concurrent",
    "lock_timeout_required",
    "max_blocking",
    "max_statements",
    "no_drops",
    "no_lock_without_timeout",
    "no_rewrite",
    "no_table_rewrite",
    "no_unknown_impact",
    "reads_impact",
    "run_guards",
    "statement_impacts",
    "warnings_only",
]
