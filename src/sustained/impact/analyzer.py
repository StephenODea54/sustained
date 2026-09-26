"""
`analyze()`: statements in, an `ImpactReport` out, with no database.

For each statement, in run order, the analyzer:

1. recognizes the text, and checks it against the intent a generated
   statement carries; when they disagree, the text wins and the
   disagreement is a finding
2. asks the profile's rules for the lock and the work on each table
3. rates blocking work against the table's size: `danger` past the
   thresholds, `info` below them, and `warn` when the size is unknown
4. adds a lock-timeout finding when a lock that blocks reads or writes
   has no timeout in scope; a timeout the context's settings show on
   the connection covers the whole run
5. takes the statement's changes into the run state

Then it groups the statements by migration and reads each migration's
transaction window (`sustained.impact.window`).

A table the run created earlier is empty and unseen by other sessions,
so work on it blocks nothing and draws no findings.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.context import EngineContext, assumed
from sustained.impact.model import (
    Blocks,
    Confidence,
    Evidence,
    Finding,
    Hold,
    ImpactReport,
    Intent,
    MigrationImpact,
    ParsedStatement,
    Severity,
    StatementImpact,
    TableImpact,
    Thresholds,
    Work,
)
from sustained.impact.recognizer import recognize
from sustained.impact.rules import Effect, Facts, Profile, profile_for, profiles_for
from sustained.impact.state import RunState
from sustained.impact.window import aggregate

if TYPE_CHECKING:
    from sustained.dialects import Dialects

# The statement kinds, and ALTER TABLE actions, each intent kind may
# read as. A None action means the statement kind alone must match.
_INTENT_FORMS: Mapping[str, FrozenSet[Tuple[str, Optional[str]]]] = {
    "create_table": frozenset({("create_table", None)}),
    "drop_table": frozenset({("drop_table", None)}),
    "rename_table": frozenset({("alter_table", "rename_to"), ("rename_table", None)}),
    "add_column": frozenset({("alter_table", "add_column")}),
    "drop_column": frozenset({("alter_table", "drop_column")}),
    "rename_column": frozenset({("alter_table", "rename_column")}),
    "alter_column_type": frozenset(
        {
            ("alter_table", "alter_column_type"),
            ("alter_table", "modify_column"),
            ("alter_table", "alter_column"),
        }
    ),
    "set_not_null": frozenset(
        {
            ("alter_table", "set_not_null"),
            ("alter_table", "modify_column"),
            ("alter_table", "alter_column"),
        }
    ),
    "drop_not_null": frozenset(
        {
            ("alter_table", "drop_not_null"),
            ("alter_table", "modify_column"),
            ("alter_table", "alter_column"),
        }
    ),
    "set_column_default": frozenset({("alter_table", "set_default")}),
    "drop_column_default": frozenset({("alter_table", "drop_default")}),
    "set_column_comment": frozenset(
        {("comment_on", None), ("alter_table", "modify_column")}
    ),
    # DuckDB backfills through the type change's USING clause.
    "backfill": frozenset({("update", None), ("alter_table", "alter_column_type")}),
    "add_foreign_key": frozenset({("alter_table", "add_constraint")}),
    "drop_foreign_key": frozenset({("alter_table", "drop_constraint")}),
    "add_check": frozenset({("alter_table", "add_constraint")}),
    "add_unique": frozenset(
        {("alter_table", "add_constraint"), ("create_index", None)}
    ),
    "drop_constraint": frozenset({("alter_table", "drop_constraint")}),
    "create_index": frozenset({("create_index", None), ("alter_table", "add_index")}),
    "drop_index": frozenset({("drop_index", None), ("alter_table", "drop_index")}),
    "create_enum_type": frozenset({("create_type", None)}),
    "drop_enum_type": frozenset({("drop_type", None)}),
    "add_enum_value": frozenset({("alter_type_add_value", None)}),
    "session_setting": frozenset({("set", None)}),
}

_WORK_WORDS: Mapping[Work, str] = {
    Work.SCAN: "while every row is read",
    Work.ROWS: "while the rows are written",
    Work.INDEX_BUILD: "for the whole index build",
    Work.REWRITE: "while the table and its indexes are rewritten",
    Work.UNKNOWN: "for work the rules do not know",
}


def analyze(
    statements: Sequence[str],
    dialect: "Dialects",
    context: Optional[EngineContext] = None,
    thresholds: Thresholds = Thresholds(),
) -> ImpactReport:
    """
    The impact of the statements a run would apply, in run order. A
    `MigrationStatement` names its migration and whether it runs inside
    a transaction; a plain `str` reads as a statement of an unnamed
    migration inside a transaction.

    Without a context, the rules assume the dialect's support floor and
    say so in the report's evidence. The context's profile picks between
    the profiles of a dialect that has more than one, such as MySQL and
    MariaDB; without a context the first is assumed, and the first
    migration gets an `impact.assumed_profile` finding that says so.
    Raises ValueError for a dialect that has no impact rules yet.
    """
    profile = profile_for(dialect, context.profile if context else None)
    if profile is None:
        raise ValueError(f"Impact analysis does not cover {dialect.name} yet.")
    guessed = context is None and len(profiles_for(dialect)) > 1
    if context is None:
        context = assumed(profile.name)
    evidence = Evidence.CATALOG if context.read else Evidence.STATIC
    run = _Run(profile, dialect, context, thresholds, evidence)
    migrations = tuple(
        run.migration(migration_id, transactional, group)
        for migration_id, transactional, group in _groups(statements)
    )
    if guessed and migrations:
        first, *rest = migrations
        note = _assumed_profile(profile, profiles_for(dialect))
        migrations = (first._replace(findings=first.findings + (note,)), *rest)
    return ImpactReport(
        profile.name, context.version, evidence, migrations, context.read
    )


def _groups(
    statements: Sequence[str],
) -> List[Tuple[Optional[str], bool, List[str]]]:
    """The statements split into runs of one migration each."""
    from sustained.analysis import statement_scope

    groups: List[Tuple[Optional[str], bool, List[str]]] = []
    for statement in statements:
        migration_id, transactional = statement_scope(statement)
        if groups and groups[-1][0] == migration_id and groups[-1][1] == transactional:
            groups[-1][2].append(statement)
        else:
            groups.append((migration_id, transactional, [statement]))
    return groups


def intent_agrees(intent: Intent, parsed: ParsedStatement) -> bool:
    """Whether a statement's text reads as the intent its generator gave."""
    forms = _INTENT_FORMS.get(intent.kind)
    if forms is None:
        return True
    actions = {a.kind for a in parsed.actions}
    kind_matches = any(
        parsed.kind == kind and (action is None or action in actions)
        for kind, action in forms
    )
    return kind_matches and _same_table(intent.table, parsed.table)


def _same_table(intended: Optional[str], parsed: Optional[str]) -> bool:
    if intended is None or parsed is None:
        return True
    a, b = intended.lower(), parsed.lower()
    if "." in a and "." not in b or "." in b and "." not in a:
        a, b = a.rsplit(".", 1)[-1], b.rsplit(".", 1)[-1]
    return a == b


class _Run:
    """One analysis pass, holding the run state between statements."""

    def __init__(
        self,
        profile: Profile,
        dialect: "Dialects",
        context: EngineContext,
        thresholds: Thresholds,
        evidence: Evidence,
    ) -> None:
        self.profile = profile
        self.dialect = dialect
        self.context = context
        self.thresholds = thresholds
        self.evidence = evidence
        self.state = RunState(
            profile.timeout_setting, profile.bounded, profile.local_scope
        )
        # A timeout the connection already has, from the role, the
        # database, or the connection string, covers the whole run.
        configured = context.settings.get(profile.timeout_setting)
        if configured is not None and profile.bounded(configured):
            self.state.timeouts.session = True

    def migration(
        self, migration_id: Optional[str], transactional: bool, statements: List[str]
    ) -> MigrationImpact:
        spans = transactional and self.profile.transactional_ddl
        impacts = tuple(
            self.statement(
                text, migration_id, transactional, spans and i < len(statements)
            )
            for i, text in enumerate(statements, 1)
        )
        locks, windows, findings = aggregate(impacts, spans)
        return MigrationImpact(
            migration_id, transactional, impacts, locks, windows, findings, spans
        )

    def statement(
        self,
        text: str,
        migration_id: Optional[str],
        transactional: bool,
        held_to_commit: bool,
    ) -> StatementImpact:
        intent: Optional[Intent] = getattr(text, "intent", None)
        parsed = recognize(text, self.dialect)
        self.state.timeouts.enter(migration_id)
        findings: List[Finding] = []
        if intent is not None and parsed.known and not intent_agrees(intent, parsed):
            findings.append(_mismatch(intent, parsed))
            intent = None
        if not parsed.known:
            findings.append(
                Finding(
                    "impact.unknown",
                    Severity.INFO,
                    f"the statement is not understood: {parsed.options.get('reason')}",
                )
            )
            return StatementImpact(
                str(text), None, (), tuple(findings), self.evidence, Confidence.UNKNOWN
            )
        facts = Facts(
            str(text), parsed, intent, self.context, self.state, transactional
        )
        outcome = self.profile.effects(facts)
        tables, table_findings, confidence = self.tables(
            outcome.effects, held_to_commit, transactional
        )
        findings.extend(outcome.findings)
        findings.extend(table_findings)
        self.state.record(parsed, transactional)
        return StatementImpact(
            str(text),
            parsed,
            tables,
            tuple(findings),
            self.evidence,
            min(confidence, outcome.confidence),
        )

    def tables(
        self, effects: Sequence[Effect], held_to_commit: bool, transactional: bool
    ) -> Tuple[Tuple[TableImpact, ...], List[Finding], Confidence]:
        """The effects merged per table, and the findings they draw."""
        by_table: Dict[str, List[Effect]] = {}
        for effect in effects:
            by_table.setdefault(effect.table.lower(), []).append(effect)
        tables: List[TableImpact] = []
        findings: List[Finding] = []
        timeout_tables: List[str] = []
        confidence = min((e.confidence for e in effects), default=Confidence.KNOWN)
        for group in by_table.values():
            impact = self.merge(group, held_to_commit)
            tables.append(impact)
            if self.state.is_new(impact.table):
                continue
            for effect in group:
                findings.extend(self.effect_findings(effect, impact))
            if any(self.queues(effect) for effect in group):
                timeout_tables.append(impact.table)
        if timeout_tables and not self.state.timeouts.covered:
            findings.append(self.timeout_finding(timeout_tables, transactional))
        return tuple(tables), findings, confidence

    def blocks(self, effect: Effect) -> Blocks:
        if effect.blocks is not None:
            return effect.blocks
        return self.profile.blocks(effect.lock)

    def queues(self, effect: Effect) -> bool:
        """Whether waiting for the effect's lock would queue other sessions."""
        return effect.waits and self.profile.waits_in_queue(effect.lock)

    def merge(self, group: Sequence[Effect], held_to_commit: bool) -> TableImpact:
        rank = self.profile.lock_rank
        strongest = max(group, key=lambda e: rank(e.lock))
        worst = max(group, key=lambda e: (self.blocks(e), e.work))
        table = group[0].table
        blocked = max(self.blocks(e) for e in group)
        work = max(e.work for e in group)
        if self.state.is_new(table):
            blocked, work = Blocks.NOTHING, Work.CATALOG
            rows: Optional[int] = 0
            size: Optional[int] = 0
        else:
            stats = self.context.stats(self.state.original(table))
            rows, size = stats.rows, stats.bytes
        if held_to_commit and blocked > Blocks.NOTHING:
            hold = Hold.TRANSACTION
        else:
            hold = Hold.BRIEF if work is Work.CATALOG else Hold.STATEMENT
        return TableImpact(
            table, strongest.lock, blocked, work, hold, rows, size, worst.rule.id
        )

    def effect_findings(self, effect: Effect, impact: TableImpact) -> List[Finding]:
        found = list(effect.notes)
        blocked = self.blocks(effect)
        if blocked < Blocks.WRITES:
            return found
        if effect.work > Work.CATALOG:
            severity, size_note = self.rate(impact)
            message = effect.message or self.default_message(effect, blocked)
            found.insert(
                0,
                Finding(
                    effect.rule.id,
                    severity,
                    message + size_note,
                    effect.remedy,
                    effect.rule.source,
                ),
            )
        elif effect.message or effect.remedy:
            message = effect.message or self.default_message(effect, blocked)
            found.insert(
                0,
                Finding(
                    effect.rule.id,
                    Severity.INFO,
                    message,
                    effect.remedy,
                    effect.rule.source,
                ),
            )
        return found

    def default_message(self, effect: Effect, blocked: Blocks) -> str:
        who = (
            "reads and writes on" if blocked is Blocks.READS_AND_WRITES else "writes to"
        )
        what = _WORK_WORDS.get(effect.work, "while the catalog changes")
        return f"{who} {effect.table} wait {what}"

    def rate(self, impact: TableImpact) -> Tuple[Severity, str]:
        """The severity of blocking work on a table of this size."""
        if impact.rows is None and impact.bytes is None:
            return Severity.WARN, f"; the size of {impact.table} is unknown"
        over = (impact.rows is not None and impact.rows > self.thresholds.rows) or (
            impact.bytes is not None and impact.bytes > self.thresholds.bytes
        )
        return (Severity.DANGER if over else Severity.INFO), ""

    def timeout_finding(self, tables: Sequence[str], transactional: bool) -> Finding:
        setting = self.profile.timeout_setting
        return Finding(
            f"{self.profile.prefix}.lock_timeout",
            Severity.WARN,
            f"no {setting} in scope: while this statement waits for its lock, "
            f"every query that conflicts with it on {', '.join(tables)} queues "
            "behind it, for as long as the longest open transaction runs",
            (self.profile.timeout_statement(transactional),),
            self.profile.timeout_source,
        )


def _assumed_profile(profile: Profile, profiles: Sequence[Profile]) -> Finding:
    from sustained.impact.context import FLOORS, version_text
    from sustained.impact.report import title

    others = " or ".join(title(p.name) for p in profiles if p is not profile)
    return Finding(
        "impact.assumed_profile",
        Severity.INFO,
        f"no server was read, so the rules assume {title(profile.name)} "
        f"{version_text(FLOORS[profile.name])}; for a {others} server, pass a "
        "context read from it",
    )


def _mismatch(intent: Intent, parsed: ParsedStatement) -> Finding:
    actions = ", ".join(a.kind for a in parsed.actions)
    reads = parsed.kind + (f" ({actions})" if actions else "")
    return Finding(
        "impact.intent_mismatch",
        Severity.WARN,
        f"the statement was generated as {intent.kind} on {intent.table}, but its "
        f"text reads as {reads} on {parsed.table}; the analysis follows the text",
    )
