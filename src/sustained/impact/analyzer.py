"""
`analyze()`: statements in, an `ImpactReport` out, with no database.

For each statement, in run order, the analyzer:

1. recognizes the text, and checks it against the intent of a generated
   statement; when they disagree, the text wins and the disagreement is
   a finding. When the text cannot be read, the statement is read from
   its intent, and a fact the intent does not give takes its worst case
   and lowers the confidence to `likely`
2. asks the profile's rules for the lock and the work on each table
3. rates blocking work against the table's size: `danger` past the
   thresholds, `info` below them, and `warn` when the size is unknown
4. adds a lock-timeout finding when a lock that blocks reads or writes
   has no timeout in scope; a timeout the context's settings show on
   the connection covers the whole run
5. takes the statement's changes into the run state

Then it groups the statements by migration and reads each migration's
transaction window (`sustained.impact.window`).

A table the run created earlier and left empty is unseen by other
sessions, so work on it blocks nothing and draws no findings. A table
the run filled from a query has the size of the tables the query read
(`sustained.impact.state`).
"""

from __future__ import annotations

from types import MappingProxyType
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
    Action,
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
    from sustained.analysis import MigrationStatement
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
    "validate_constraint": frozenset({("alter_table", "validate_constraint")}),
    "create_index": frozenset({("create_index", None), ("alter_table", "add_index")}),
    "drop_index": frozenset({("drop_index", None), ("alter_table", "drop_index")}),
    "attach_index": frozenset({("attach_index", None)}),
    "create_enum_type": frozenset({("create_type", None)}),
    "drop_enum_type": frozenset({("drop_type", None)}),
    "add_enum_value": frozenset({("alter_type_add_value", None)}),
    "session_setting": frozenset({("set", None)}),
}

# The id of the lock-timeout finding, after the profile's prefix.
_LOCK_TIMEOUT = "lock_timeout"

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
        from sustained.impact.rules import engine

        raise ValueError(f"Impact analysis does not cover {engine(dialect)} yet.")
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


def attach_impact(
    statements: Sequence[str],
    dialect: "Dialects",
    context: Optional[EngineContext] = None,
) -> List["MigrationStatement"]:
    """
    The statements with each one's `StatementImpact` from `analyze()` on
    its `impact` attribute. A plain `str` becomes a statement of an
    unnamed migration inside a transaction; a `MigrationStatement` keeps
    its migration, its transaction flag, its destructive mark, and its
    intent. Raises ValueError for a dialect that has no impact rules yet.
    """
    from sustained.analysis import MigrationStatement

    report = analyze(statements, dialect, context)
    return [
        MigrationStatement(statement, impact=impact)
        for statement, impact in zip(statements, report.statements)
    ]


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


def parsed_from_intent(intent: Intent) -> Optional[ParsedStatement]:
    """
    What a generated statement the recognizer cannot read does, from its
    intent alone: the one statement kind, and ALTER TABLE action, the
    intent reads as, with the options its details give. A fact the
    details leave out takes its worst case, and `intent_unread()` names
    it. None for an intent that reads as more than one statement kind.
    """
    forms = _INTENT_FORMS.get(intent.kind)
    if forms is None or len(forms) != 1 or intent.table is None:
        return None
    ((kind, action),) = forms
    options, action_options, _ = _intent_reading(intent)
    actions = (
        ()
        if action is None
        else (Action(action, intent.column, MappingProxyType(action_options)),)
    )
    return ParsedStatement(kind, intent.table, actions, MappingProxyType(options))


def intent_unread(intent: Intent) -> Tuple[str, ...]:
    """
    The facts `parsed_from_intent()` needs and the intent's details do
    not give, which it takes at their worst case.
    """
    return _intent_reading(intent)[2]


def _intent_reading(
    intent: Intent,
) -> Tuple[Dict[str, object], Dict[str, object], Tuple[str, ...]]:
    """
    The statement options, the action options, and the unread facts of
    an intent read without its statement text.
    """
    kind = intent.kind
    statement: Dict[str, object] = {}
    action: Dict[str, object] = {}
    unread: List[str] = []
    name = intent.get("name")
    if kind == "drop_table":
        statement["tables"] = (intent.table,)
    elif kind == "create_table":
        statement.update(
            temporary=False,
            if_not_exists=False,
            references=(),
            partition_of=None,
            as_select=False,
        )
        unread.append("the tables its foreign keys reference")
    elif kind == "add_column":
        nullable = intent.get("nullable")
        action.update(_UNREAD_COLUMN)
        action["not_null"] = nullable is not True
        if nullable is None:
            unread.append("whether the column is nullable")
        if intent.get("has_default") is False:
            action["default"] = None
        else:
            # The default's expression is not in the intent, so it
            # counts as one that gives each row a new value.
            action.update(_UNREAD_DEFAULT)
            unread.append("the default")
        unread.append("the column's type and constraints")
    elif kind == "rename_column":
        action.update(old=intent.column, new=intent.get("new"))
        if intent.get("new") is None:
            unread.append("the new name")
    elif kind in ("add_foreign_key", "add_check"):
        action.update(name=name, not_valid=False, using_index=None)
        if kind == "add_foreign_key":
            action.update(constraint="foreign_key", references=intent.get("references"))
            unread.append("whether the key is NOT VALID")
        else:
            action["constraint"] = "check"
            unread.append("the check's expression, and whether it is NOT VALID")
    elif kind in ("drop_foreign_key", "drop_constraint", "validate_constraint"):
        action["name"] = name
        if kind != "validate_constraint":
            action.update(if_exists=False, cascade=False)
    return statement, action, tuple(unread)


# The column facts an add_column intent does not give, at the values the
# recognizer gives a column definition that does not spell them.
_UNREAD_COLUMN: Mapping[str, object] = MappingProxyType(
    {
        "type": None,
        "serial": False,
        "generated": None,
        "identity": False,
        "references": None,
        "unique": False,
        "primary_key": False,
        "check": False,
        "position": None,
    }
)

# A default whose expression is not read: an expression no rule knows,
# which counts as volatile. The parentheses make it an expression
# default on MySQL and MariaDB, the worst case there.
_UNREAD_DEFAULT: Mapping[str, object] = MappingProxyType(
    {
        "default": "(?)",
        "default_volatility": "volatile",
        "default_function": None,
        "default_certain": False,
    }
)


def _from_intent(intent: Intent, unread: Sequence[str]) -> Finding:
    message = (
        f"the statement text is not read, so the analysis follows the intent it "
        f"was generated with: {intent.kind} on {intent.table}"
    )
    if intent.kind == "create_table":
        # No worst case names the tables a key references.
        message += f"; the intent does not give {_listed(unread)}, so no lock on "
        message += "them is reported"
    elif unread:
        message += f"; the intent does not give {_listed(unread)}, so the analysis "
        message += "assumes the worst case" + (" for each" if len(unread) > 1 else "")
    return Finding("impact.from_intent", Severity.INFO, message)


def _listed(items: Sequence[str]) -> str:
    if len(items) < 3:
        return " or ".join(items)
    return ", ".join(items[:-1]) + f", or {items[-1]}"


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
            profile.timeout_setting, profile.bounded, profile.local_scope, context
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
            self.statement(text, migration_id, transactional, spans)
            for text in statements
        )
        locks, windows, findings = aggregate(
            impacts, spans, self.profile.locks_database
        )
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
        # A statement read from its intent alone is at best as sure as
        # the facts the intent gives.
        ceiling = Confidence.KNOWN
        if not parsed.known and intent is not None:
            from_intent = parsed_from_intent(intent)
            if from_intent is not None:
                unread = intent_unread(intent)
                findings.append(_from_intent(intent, unread))
                parsed = from_intent
                if unread:
                    ceiling = Confidence.LIKELY
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
            min(confidence, outcome.confidence, ceiling),
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
            stats = self.state.stats(self.context, table)
            rows, size = stats.rows, stats.bytes
        if held_to_commit and blocked > Blocks.NOTHING:
            hold = Hold.TRANSACTION
        else:
            hold = Hold.BRIEF if work is Work.CATALOG else Hold.STATEMENT
        return TableImpact(
            table, strongest.lock, blocked, work, hold, rows, size, worst.rule.id
        )

    def effect_findings(self, effect: Effect, impact: TableImpact) -> List[Finding]:
        # No running code names a table the run created, so the notes
        # about it, such as a rename breaking that code, do not apply.
        found = [] if self.state.created_in_run(effect.table) else list(effect.notes)
        blocked = self.blocks(effect)
        if blocked < Blocks.WRITES:
            return found
        if effect.work > Work.CATALOG and not effect.at_end:
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
            f"{self.profile.prefix}.{_LOCK_TIMEOUT}",
            Severity.WARN,
            f"no {setting} in scope: while this statement waits for its lock, "
            f"every query that conflicts with it on {', '.join(tables)} queues "
            "behind it, for as long as the longest open transaction runs",
            (self.profile.timeout_statement(transactional),),
            self.profile.timeout_source,
        )


def is_lock_timeout(finding: Finding) -> bool:
    """Whether the finding is a profile's lock-timeout finding."""
    return finding.rule.partition(".")[2] == _LOCK_TIMEOUT


def _assumed_profile(profile: Profile, profiles: Sequence[Profile]) -> Finding:
    from sustained.impact.context import FLOORS, version_text
    from sustained.impact.rules import title

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
