"""
The handler helpers every rule profile uses.

- `table()` and `tables()` name the tables a statement acts on, and
  `index_table()` and `dropped_indexes()` the tables of its indexes.
- `dispatch()` hands a statement to the handler for its kind, and
  `unknown()` is the outcome for a statement or ALTER TABLE action no
  handler reads. `each_action()` joins the outcomes of an ALTER TABLE
  statement's actions, and `by_kind()` reads each with the handler for
  its kind.
- `row_write_message()` words the finding for an UPDATE or DELETE, and
  `rename_note()` the one for a rename.
- `dotted_version()` reads the version number from a server's version
  text.
- `keys()` are the names a table is found under: `schema.table`, and the
  bare name when an unqualified name finds the table. `add_stats()`
  keys a table's stats by them.
- `with_observations()` puts a traced rehearsal's observations in place
  of a report's predictions, and `mismatch()` is the finding for a
  difference between the two. `settled_work()` picks the work to report
  from a prediction and an observation, and `work_mismatch()` words the
  difference.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from sustained.impact.context import TableStats
from sustained.impact.model import (
    Action,
    Blocks,
    Confidence,
    Evidence,
    Finding,
    Hold,
    ImpactReport,
    MigrationImpact,
    Severity,
    StatementImpact,
    TableImpact,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, Profile, Rule, title
from sustained.impact.window import aggregate, row_scopes

UNNAMED_TABLE = "(unnamed table)"

Handler = Callable[[Facts], Outcome]
ActionHandler = Callable[[Facts, Action], Outcome]
# One ALTER TABLE action's outcome, or None for an action no rule reads.
ActionReader = Callable[[Facts, Action], Optional[Outcome]]


def table(facts: Facts) -> str:
    """The table the statement names, or a placeholder when it names none."""
    return facts.parsed.table or UNNAMED_TABLE


def tables(facts: Facts) -> List[str]:
    """Every table a statement that may name several acts on."""
    named = facts.parsed.items("tables")
    if named:
        return [str(t) for t in named]
    return [facts.parsed.table] if facts.parsed.table else []


def index_table(facts: Facts, name: str) -> Optional[str]:
    """The table an index is on, as the run or the context knows it."""
    return facts.state.index_table(name) or facts.context.index_table(name)


def index_label(name: str, table: Optional[str]) -> str:
    """The table, or a placeholder that names the index when none was found."""
    return table if table else f"(table of index {name})"


def dropped_indexes(facts: Facts) -> List[Tuple[str, Optional[str]]]:
    """
    Each index a DROP INDEX names, with the table it is on, or None. The
    intent's table stands for an index no read found only when the
    statement names one index, since the intent cannot say which of
    several it means.
    """
    names = [str(name) for name in facts.parsed.items("names")]
    found = []
    for name in names:
        table = index_table(facts, name)
        if table is None and facts.intent is not None and len(names) == 1:
            table = facts.intent.table
        found.append((name, table))
    return found


def nothing(facts: Facts) -> Outcome:
    """The outcome of a statement that locks no table."""
    return Outcome()


def unknown(facts: Facts, what: str) -> Outcome:
    """The outcome of a statement or action no rule of the profile reads."""
    return Outcome(
        findings=(
            Finding(
                "impact.unknown",
                Severity.INFO,
                f"no {title(facts.context.profile)} rule reads {what}",
            ),
        ),
        confidence=Confidence.UNKNOWN,
    )


def dispatch(facts: Facts, handlers: Mapping[str, Handler]) -> Outcome:
    """The outcome of the handler for the statement's kind."""
    handler = handlers.get(facts.parsed.kind)
    if handler is None:
        return unknown(facts, f"a {facts.parsed.kind} statement")
    return handler(facts)


def fixed_action(rule: Rule, lock: str, work: Work) -> ActionHandler:
    """The handler of an ALTER TABLE action that always takes one lock and work."""

    def handler(facts: Facts, action: Action) -> Outcome:
        return Outcome.of(Effect(rule, table(facts), lock, work))

    return handler


def fixed(
    rule: Rule,
    lock: Optional[str],
    work: Work,
    many: bool = False,
    default: Optional[str] = None,
) -> Handler:
    """
    The handler of a statement that always takes one lock and work on
    the table it names, or with `many` on each table it names. `default`
    stands for the table when the statement names none.
    """

    def handler(facts: Facts) -> Outcome:
        if many:
            named = tables(facts) or ([default] if default else [])
        else:
            named = [facts.parsed.table or default or UNNAMED_TABLE]
        return Outcome(tuple(Effect(rule, t, lock, work) for t in named))

    return handler


def by_kind(handlers: Mapping[str, ActionHandler]) -> ActionReader:
    """Reads each ALTER TABLE action with the handler for its kind."""

    def read(facts: Facts, action: Action) -> Optional[Outcome]:
        handler = handlers.get(action.kind)
        return None if handler is None else handler(facts, action)

    return read


def each_action(
    facts: Facts,
    read: ActionReader,
    adjust: Optional[Callable[[Action, Outcome], Outcome]] = None,
) -> Outcome:
    """
    The outcomes of an ALTER TABLE statement's actions, together. An
    action `read()` returns None for, or whose outcome is UNKNOWN, makes
    the whole statement's outcome. `adjust(action, outcome)` changes an
    action's outcome before it joins the others.
    """
    effects: List[Effect] = []
    findings: List[Finding] = []
    unnamed: List[Effect] = []
    confidence = Confidence.KNOWN
    for action in facts.parsed.actions:
        outcome = read(facts, action)
        if outcome is None:
            return unknown(facts, f"the ALTER TABLE action {action.kind}")
        if outcome.confidence is Confidence.UNKNOWN:
            return outcome
        if adjust is not None:
            outcome = adjust(action, outcome)
        effects.extend(outcome.effects)
        findings.extend(outcome.findings)
        unnamed.extend(outcome.unnamed)
        confidence = min(confidence, outcome.confidence)
    return Outcome(tuple(effects), tuple(findings), confidence, unnamed=tuple(unnamed))


def volatile_default(options: Mapping[str, object]) -> Tuple[str, Confidence]:
    """
    Why a new column's volatile default gives each row its own value,
    and how sure that is: LIKELY when the recognizer could not tell the
    default's function was volatile and counted it as volatile.
    """
    function = options.get("default_function")
    if options.get("default_certain", True):
        reason = f"the default calls {function}(), which gives each row a new value"
        return reason, Confidence.KNOWN
    if function:
        reason = (
            f"the default calls {function}(), which no rule knows, so it counts "
            "as volatile: a new value for each row"
        )
    else:
        reason = (
            "the default was not read, so it counts as volatile: a new value "
            "for each row"
        )
    return reason, Confidence.LIKELY


def row_write_message(facts: Facts, table: str, detail: str = "") -> str:
    """
    The finding for an UPDATE or DELETE: writes to the rows it changes
    wait until it ends, or until the migration commits inside a
    transaction. `detail` adds the engine's own reason after that. A
    DELETE is advised to delete in batches, and an UPDATE to backfill
    in batches.
    """
    if facts.intent is not None and facts.intent.kind == "backfill":
        what = "the backfill"
    else:
        what = f"the {facts.parsed.kind.upper()}"
    until = "the migration commits" if facts.transactional else "it ends"
    batches = "delete" if facts.parsed.kind == "delete" else "backfill"
    return (
        f"writes to the rows {what} changes on {table} wait until {until}"
        f"{detail}; on a large table, {batches} in batches outside the DDL "
        "migration"
    )


def rename_text(what: str, name: object, when: str = "commits") -> str:
    """
    The note for a rename: running code that names the old `what`, such
    as `column` or `table`, fails once the rename `when`, such as
    `commits` or `runs`.
    """
    return (
        f"running application code that names the {what} {name} fails once the "
        f"rename {when}"
    )


def rename_note(rule: Rule, what: str, name: object) -> Finding:
    """The `info` finding of `rename_text()`, for a rename that commits."""
    return rule.finding(Severity.INFO, rename_text(what, name))


def dotted_version(
    text: str, floor: Tuple[int, ...], prefix: str = ""
) -> Tuple[int, ...]:
    """
    The dotted number at the start of a server's version text, such as
    (8, 0, 19) for `8.0.19-log`, after an optional `prefix` such as `v`.
    Text that starts with no number gives `floor`.
    """
    match = re.match(rf"(?:{prefix})?(\d+(?:\.\d+)*)", text.strip())
    if match is None:
        return floor
    return tuple(int(part) for part in match.group(1).split("."))


def quoted(value: str) -> str:
    """A string literal of the value, with each quote doubled."""
    return "'" + value.replace("'", "''") + "'"


def name_filter(
    column: str, names: Iterable[str], literal: Callable[[str], str]
) -> str:
    """
    `column IN (...)` with each name spelled by `literal`, and with NULL,
    which matches no row, when there are no names.
    """
    listed = ", ".join(literal(name) for name in names)
    return f"{column} IN ({listed or 'NULL'})"


def keys(schema: str, name: str, bare: bool) -> Tuple[str, ...]:
    """
    The lower case keys a table is found under: `schema.table`, and the
    bare name when `bare` says an unqualified name finds it. The size
    read, the preflight, and the trace all match names by these keys.
    """
    full = f"{schema}.{name}".lower()
    return (full, name.lower()) if bare else (full,)


def add_stats(
    found: Dict[str, TableStats],
    schema: str,
    name: str,
    bare: bool,
    stats: TableStats,
) -> str:
    """
    Adds a table's stats under `schema.table`, and under the bare name
    when `bare` says an unqualified name finds it. Returns the full key.
    """
    found.update(dict.fromkeys(keys(schema, name, bare), stats))
    return keys(schema, name, False)[0]


def mismatch(message: str) -> Finding:
    """The finding for a difference between a prediction and an observation."""
    return Finding("impact.mismatch", Severity.WARN, message)


def settled_work(predicted: Work, observed: Optional[Work]) -> Optional[Work]:
    """
    The work to report, or None to keep the prediction. A copy that was
    seen stands. With no copy seen, a predicted scan, row write, or
    catalog change stands, since an observation cannot tell them apart,
    and a predicted copy falls to a scan, the heaviest work that copies
    nothing.
    """
    if observed is None or observed is Work.UNKNOWN:
        return None
    if observed in (Work.REWRITE, Work.INDEX_BUILD):
        return observed
    if predicted in (Work.REWRITE, Work.INDEX_BUILD):
        return Work.SCAN
    return None


def work_mismatch(table: TableImpact, observed: Optional[Work], nothing: str) -> str:
    """
    The mismatch message for observed work that differs from the
    prediction; `nothing` says what the server did when it copied
    nothing, such as `copied no file`.
    """
    if observed is Work.REWRITE:
        return f"the rules predicted {table.work} on {table.table}, and the server rewrote it"
    if observed is Work.INDEX_BUILD:
        return (
            f"the rules predicted {table.work} on {table.table}, and the server "
            "built an index on it"
        )
    what = "a rewrite" if table.work is Work.REWRITE else "an index build"
    return f"the rules predicted {what} on {table.table}, and the server {nothing}"


def _kept_to_commit(statement: StatementImpact) -> StatementImpact:
    """
    The statement with the hold of each lock that blocks something set
    to `transaction`, for a migration whose locks last until the commit,
    as the analysis sets it.
    """
    tables = tuple(
        t._replace(hold=Hold.TRANSACTION) if t.blocks > Blocks.NOTHING else t
        for t in statement.tables
    )
    return statement._replace(tables=tables)


def with_observations(
    report: ImpactReport,
    observations: Mapping[Tuple[Optional[str], int], Any],
    profile: Profile,
    observe: Callable[[StatementImpact, Any], StatementImpact],
) -> ImpactReport:
    """
    The report with each observed statement's facts in place of the
    predicted ones, and each migration's locks and windows read again
    from them. `observations` is keyed by each statement's migration id
    and its position in that migration, counting from 0, and
    `observe(statement, observation)` returns the observed impact. The
    report's evidence is `observed` when any statement was observed.
    """
    migrations: List[MigrationImpact] = []
    for migration in report.migrations:
        spans = migration.transactional and profile.transactional_ddl
        statements = []
        for index, statement in enumerate(migration.statements):
            key = (migration.migration_id, index)
            if key in observations:
                statement = observe(statement, observations[key])
                if spans:
                    statement = _kept_to_commit(statement)
            statements.append(statement)
        scopes = None
        if migration.transactional and not profile.transactional_ddl:
            scopes = row_scopes(statements)
        locks, windows, findings = aggregate(
            statements, spans, profile.locks_database, scopes
        )
        migrations.append(
            migration._replace(
                statements=tuple(statements),
                locks=locks,
                windows=windows,
                findings=findings,
            )
        )
    observed = any(
        s.evidence is Evidence.OBSERVED for m in migrations for s in m.statements
    )
    return report._replace(
        migrations=tuple(migrations),
        evidence=Evidence.OBSERVED if observed else report.evidence,
    )
