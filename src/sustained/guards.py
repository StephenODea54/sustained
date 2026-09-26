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

from __future__ import annotations

import re
from typing import Callable, List, NamedTuple, Optional, Sequence, Union

from sustained.analysis import (
    _ALTER_DROP_RE,
    MigrationStatement,
    normalize_statement,
    scannable_forms,
    scannable_statement,
    statement_scope,
)
from sustained.dialects import Dialects
from sustained.impact.model import (
    Blocks,
    Confidence,
    StatementImpact,
    TableImpact,
    Work,
)

# The two verdicts a rule can return. There is no third severity: a rule
# either stops the run or tells the operator about it.
BLOCK = "block"
WARN = "warn"


class Verdict(NamedTuple):
    """
    One rule's objection to one statement: the rule that objected, whether
    it blocks or warns, and the statement it read.
    """

    rule: str
    verdict: str
    statement: str


# A guard reads the statements an up run would apply and returns its
# verdicts. A MigrationStatement is a str that also names the migration
# it came from, so a guard written against Sequence[str] fits here and
# reads the statements as plain strings.
Guard = Callable[[Sequence[MigrationStatement], Dialects], List[Verdict]]


def run_guards(
    guards: Sequence[Guard], statements: Sequence[str], dialect: Dialects
) -> List[Verdict]:
    """
    Runs every guard over the statements and returns the verdicts, in
    guard order. An empty guard list returns an empty list, so a caller
    with no guards pays nothing.

    A plain string is wrapped in a MigrationStatement that names no
    migration, so every guard reads the same kind of value.
    """
    tagged = [
        s if isinstance(s, MigrationStatement) else MigrationStatement(s)
        for s in statements
    ]
    verdicts: List[Verdict] = []
    for guard in guards:
        verdicts.extend(guard(tagged, dialect))
    return verdicts


def blocking(verdicts: Sequence[Verdict]) -> List[Verdict]:
    """The verdicts that stop a run."""
    return [v for v in verdicts if v.verdict == BLOCK]


def warnings_only(verdicts: Sequence[Verdict]) -> List[Verdict]:
    """The verdicts that only tell the operator."""
    return [v for v in verdicts if v.verdict == WARN]


_DROP_RE = re.compile(
    r"\bDROP\s+(TABLE|COLUMN|MATERIALIZED\s+VIEW|VIEW|SCHEMA|DATABASE|TYPE"
    r"|CONSTRAINT|CHECK|FOREIGN\s+KEY)\b",
    re.IGNORECASE,
)
_CREATE_INDEX_RE = re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", re.IGNORECASE)
_CONCURRENTLY_RE = re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE)
_TYPE_CHANGE_RE = re.compile(
    r"\bALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b|\bMODIFY\s+(COLUMN\s+)?\S+\s",
    re.IGNORECASE,
)
_SET_NOT_NULL_RE = re.compile(r"\bSET\s+NOT\s+NULL\b", re.IGNORECASE)
_ADD_NOT_NULL_RE = re.compile(r"\bADD\s+(COLUMN\s+)?\S+.*\bNOT\s+NULL\b", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.IGNORECASE)
_LOCK_TAKING_RE = re.compile(r"\bALTER\s+TABLE\b|\bDROP\s+TABLE\b", re.IGNORECASE)
# Only a statement that starts with `SET lock_timeout` counts, so an
# `UPDATE t SET lock_timeout = 5` on a column of that name does not pass
# for a timeout. `SESSION` and `LOCAL` are the two words Postgres allows
# between the two.
_LOCK_TIMEOUT_RE = re.compile(
    r"^SET\s+(SESSION\s+|LOCAL\s+)?lock_timeout\b", re.IGNORECASE
)


def no_drops() -> Guard:
    """
    Blocks a statement that drops a table, a column, a view, a
    materialized view, a schema, a database, an enum type, or a
    constraint. A dropped constraint removes no rows, but putting it back
    needs the data to still satisfy it, so the drop is not freely
    reversible. Drops of indexes and keys pass.

    A column type change that narrows the type drops no object, so this
    rule passes it. The destructive label and the rehearsal gate in
    `migrate` still catch it.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement in statements:
            if any(
                _DROP_RE.search(form) or _ALTER_DROP_RE.search(form)
                for form in scannable_forms(statement)
            ):
                found.append(Verdict("no_drops", BLOCK, normalize_statement(statement)))
        return found

    return guard


def index_must_be_concurrent() -> Guard:
    """
    Blocks `CREATE INDEX` without `CONCURRENTLY` on Postgres, where a
    plain index build holds a write lock on the table for its whole
    duration. Silent on every other dialect, which has no such keyword.

    Postgres refuses CREATE INDEX CONCURRENTLY inside a transaction
    block, and the migrator wraps a migration in one. Put the index in a
    migration of its own with transactional=False, or in a SQL file that
    carries the '-- sustained: no transaction' marker. Such a migration
    that fails part way leaves an invalid index behind, which you drop by
    hand before you run it again.

    `max_blocking("ddl")` reads the impact analysis instead, which also
    passes an index on a table the same run created.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        if dialect is not Dialects.POSTGRES:
            return []
        found = []
        for statement in statements:
            scanned = scannable_statement(statement)
            if _CREATE_INDEX_RE.search(scanned) and not _CONCURRENTLY_RE.search(
                scanned
            ):
                found.append(
                    Verdict(
                        "index_must_be_concurrent",
                        BLOCK,
                        normalize_statement(statement),
                    )
                )
        return found

    return guard


def no_table_rewrite() -> Guard:
    """
    Warns on a statement that may rewrite the whole table: a column type
    change, or a NOT NULL added with no default to fill the existing
    rows.

    This rule warns where the others block. Whether a given change
    rewrites depends on the engine, its version, and whether the two
    types coerce, so a block here would stop safe statements. Read the
    warning against your own engine.

    `no_rewrite()` reads the impact analysis instead, which knows the
    engine version, which type changes coerce, and the table's size.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement in statements:
            scanned = scannable_statement(statement)
            rewrites = bool(
                _TYPE_CHANGE_RE.search(scanned) or _SET_NOT_NULL_RE.search(scanned)
            )
            if not rewrites and _ADD_NOT_NULL_RE.search(scanned):
                rewrites = not _DEFAULT_RE.search(scanned)
            if rewrites:
                found.append(
                    Verdict("no_table_rewrite", WARN, normalize_statement(statement))
                )
        return found

    return guard


def no_lock_without_timeout() -> Guard:
    """
    Blocks a run that alters or drops a table without setting a lock
    timeout first, on Postgres, where a statement waiting behind a long
    transaction queues every other query on that table behind it. Silent
    on every other dialect, which has no such setting.

    The rule reads the statements in run order, and it reads how far each
    timeout reaches.

    `SET lock_timeout`, with or without SESSION, sets the timeout for the
    session. It covers every statement after it in the run, in its own
    migration and in the ones that follow, and none before it.

    `SET LOCAL lock_timeout` dies at the commit that ends its migration,
    so it covers only the statements after it in that same migration. The
    next migration starts uncovered.

    A migration that runs outside a transaction is the third case.
    Postgres has no transaction block to attach a LOCAL setting to there,
    so it ignores the `SET LOCAL` and the statements after it stay
    uncovered. Write the plain `SET lock_timeout` in a migration like
    that.

    The impact analysis reads timeout scopes the same way, through
    `sustained.impact.state.TimeoutScope`; its `pg.lock_timeout` finding
    covers every lock that blocks reads or writes, where this rule reads
    only ALTER TABLE and DROP TABLE.
    """

    def guard(
        statements: Sequence[MigrationStatement], dialect: Dialects
    ) -> List[Verdict]:
        from sustained.impact.state import TimeoutScope

        if dialect is not Dialects.POSTGRES:
            return []
        found = []
        timeouts = TimeoutScope()
        for statement in statements:
            migration_id, transactional = statement_scope(statement)
            timeouts.enter(migration_id)
            scanned = scannable_statement(statement)
            match = _LOCK_TIMEOUT_RE.search(scanned)
            if match:
                scope = (match.group(1) or "session").strip().lower()
                timeouts.set(scope, transactional)
            elif not timeouts.covered and _LOCK_TAKING_RE.search(scanned):
                found.append(
                    Verdict(
                        "no_lock_without_timeout",
                        BLOCK,
                        normalize_statement(statement),
                    )
                )
        return found

    return guard


def max_statements(limit: int) -> Guard:
    """
    Blocks a run longer than `limit` statements, which usually means
    several changes that should have been several deploys. The verdict
    names every statement past the limit.
    """
    if limit < 1:
        raise ValueError("max_statements needs a limit of at least 1.")
    rule = f"max_statements({limit})"

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        return [
            Verdict(rule, BLOCK, normalize_statement(statement))
            for statement in statements[limit:]
        ]

    return guard


def reads_impact(guard: Guard) -> bool:
    """Whether a guard is an impact rule, which reads `statement.impact`."""
    return bool(getattr(guard, "reads_impact", False))


def _impact_rule(guard: Guard) -> Guard:
    """Marks a guard as one that reads `statement.impact`."""
    setattr(guard, "reads_impact", True)
    return guard


def statement_impacts(
    statements: Sequence[str], dialect: Dialects
) -> List[Optional[StatementImpact]]:
    """
    Each statement's impact, in order: the `impact` a MigrationStatement
    carries, or else what `analyze()` gives with no context, reading the
    statements as one run. Every entry is None on a dialect the analysis
    does not cover.
    """
    from sustained.impact import analyze, supported

    if not supported(dialect):
        return [None] * len(statements)
    attached: List[Optional[StatementImpact]] = [
        getattr(s, "impact", None) for s in statements
    ]
    if all(impact is not None for impact in attached):
        return attached
    analyzed = analyze(statements, dialect).statements
    return [a if a is not None else b for a, b in zip(attached, analyzed)]


def _over(
    table: TableImpact,
    over_rows: Optional[int],
    over_bytes: Optional[int],
    assume_small: bool,
) -> bool:
    """
    Whether the table passes a size threshold. With neither threshold
    every table passes. A known size past either threshold passes; a
    size a threshold needs and the analysis does not know passes unless
    `assume_small` is set.
    """
    if over_rows is None and over_bytes is None:
        return True
    unknown = False
    for size, limit in ((table.rows, over_rows), (table.bytes, over_bytes)):
        if limit is None:
            continue
        if size is None:
            unknown = True
        elif size > limit:
            return True
    return unknown and not assume_small


def _threshold_rule(
    name: str, over_rows: Optional[int], over_bytes: Optional[int]
) -> str:
    """The rule name a verdict reports, with the thresholds given."""
    parts = [name] if name else []
    if over_rows is not None:
        parts.append(f"over_rows={over_rows}")
    if over_bytes is not None:
        parts.append(f"over_bytes={over_bytes}")
    return ", ".join(parts)


def max_blocking(
    limit: Union[Blocks, str],
    over_rows: Optional[int] = None,
    over_bytes: Optional[int] = None,
    assume_small: bool = False,
) -> Guard:
    """
    Blocks a statement that blocks more than `limit` on a table past the
    size thresholds. `limit` is a `Blocks` member or its name:
    `nothing`, `ddl`, `writes`, or `reads_and_writes`. So
    `max_blocking("writes")` passes a lock that stops writes and blocks
    one that stops reads as well.

    With neither `over_rows` nor `over_bytes`, every table counts. With
    either, a table counts when its estimated size passes one of them,
    and when the size the threshold reads is unknown, unless
    `assume_small=True`. A table the run created earlier blocks nothing
    in the analysis, so it never counts.
    """
    ceiling = Blocks(limit)
    if (
        over_rows is not None
        and over_rows < 0
        or over_bytes is not None
        and over_bytes < 0
    ):
        raise ValueError("max_blocking needs thresholds of 0 or more.")
    rule = f"max_blocking({_threshold_rule(str(ceiling), over_rows, over_bytes)})"

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement, impact in zip(
            statements, statement_impacts(statements, dialect)
        ):
            if impact is not None and any(
                table.blocks > ceiling
                and _over(table, over_rows, over_bytes, assume_small)
                for table in impact.tables
            ):
                found.append(Verdict(rule, BLOCK, normalize_statement(statement)))
        return found

    return _impact_rule(guard)


def no_rewrite(
    over_rows: Optional[int] = None,
    over_bytes: Optional[int] = None,
    assume_small: bool = False,
) -> Guard:
    """
    Blocks a statement that rewrites a table past the size thresholds:
    work `rewrite`, or `unknown`, which the analysis ranks above it. The
    thresholds read as they do for `max_blocking()`. A table the run
    created earlier is never rewritten in the analysis.
    """
    if (
        over_rows is not None
        and over_rows < 0
        or over_bytes is not None
        and over_bytes < 0
    ):
        raise ValueError("no_rewrite needs thresholds of 0 or more.")
    rule = f"no_rewrite({_threshold_rule('', over_rows, over_bytes)})"

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement, impact in zip(
            statements, statement_impacts(statements, dialect)
        ):
            if impact is not None and any(
                table.work >= Work.REWRITE
                and _over(table, over_rows, over_bytes, assume_small)
                for table in impact.tables
            ):
                found.append(Verdict(rule, BLOCK, normalize_statement(statement)))
        return found

    return _impact_rule(guard)


def lock_timeout_required() -> Guard:
    """
    Blocks a statement whose lock would queue reads or writes with no
    lock timeout in scope: the statements the analysis gives a
    `<profile>.lock_timeout` finding, such as `pg.lock_timeout`. It
    reads timeout scopes as `no_lock_without_timeout()` does, and covers
    every such lock where that rule reads only ALTER TABLE and DROP
    TABLE. A timeout the connection already has covers the whole run
    when the migrator read it.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement, impact in zip(
            statements, statement_impacts(statements, dialect)
        ):
            if impact is not None and any(
                f.rule.endswith(".lock_timeout") for f in impact.findings
            ):
                found.append(
                    Verdict(
                        "lock_timeout_required", BLOCK, normalize_statement(statement)
                    )
                )
        return found

    return _impact_rule(guard)


def no_unknown_impact() -> Guard:
    """
    Blocks a statement the impact analysis cannot read, the strict mode
    for hand-written SQL. The other impact rules pass such a statement,
    since the analysis names no table for it.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement, impact in zip(
            statements, statement_impacts(statements, dialect)
        ):
            if impact is not None and impact.confidence is Confidence.UNKNOWN:
                found.append(
                    Verdict("no_unknown_impact", BLOCK, normalize_statement(statement))
                )
        return found

    return _impact_rule(guard)
