"""
The ALGORITHM and LOCK a statement runs with: the clause the server
would pick, the clause the statement spells, whether the server refuses
the pair, and the clause that asserts the pick. `asserted_statements()`
writes that clause into the statements of a generated migration.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    List,
    Optional,
    Sequence,
    Tuple,
)

from sustained.impact.model import (
    Blocks,
    Confidence,
    Finding,
    Severity,
    Work,
)
from sustained.impact.rules import Effect, Facts, Outcome, Rule, common
from sustained.impact.rules.mysql.facts import (
    Change,
    copy_algorithm,
    is_mariadb,
    rules_for,
)
from sustained.impact.rules.mysql.locks import (
    ALGORITHMS,
    INPLACE_NONE,
    INSTANT,
    LEVELS,
    MDL_EXCLUSIVE,
    NOCOPY_NONE,
    Online,
    blocks,
    parse_label,
)
from sustained.impact.state import RunState

if TYPE_CHECKING:
    from sustained.impact.context import EngineContext
    from sustained.impact.model import StatementImpact
    from sustained.impact.tokens import Token

# The clauses asserted: the ones that let reads and writes go on while
# the statement runs.
ONLINE = (INSTANT, NOCOPY_NONE, INPLACE_NONE)

_ASSERTED = ("alter_table", "create_index", "drop_index")

# A WAIT of a day or more bounds nothing, as lock_wait_timeout does not.
_UNBOUNDED_WAIT = 86400


def assertion(
    statement: str, kind: str, online: Online, mariadb: bool = False
) -> Optional[str]:
    """
    The statement with an ALGORITHM and LOCK clause that asserts how the
    server runs it, so the server refuses the statement instead of
    running it another way. A CREATE INDEX, and a DROP INDEX on MySQL,
    take the clause without a comma. MariaDB's DROP INDEX takes no
    clause, so there it becomes the ALTER TABLE ... DROP INDEX it stands
    for. MySQL refuses a LOCK clause beside ALGORITHM=INSTANT, so INSTANT
    asserts the algorithm alone. The clause goes after the statement's
    last token, before any trailing comment, which would otherwise hide
    it, and the trailing `;` is left off. None when the statement cannot
    be read back into the ALTER TABLE form.
    """
    ends = _ends(statement)
    if ends is None:
        return None
    text, rest = ends
    parts = [f"ALGORITHM={online.algorithm}"]
    if online.level is not None:
        parts.append(f"LOCK={online.level}")
    if kind == "drop_index" and mariadb:
        altered = _drop_index_as_alter(text)
        if altered is None:
            return None
        return f"{altered}, {', '.join(parts)}{rest}"
    if kind == "alter_table":
        return f"{text}, {', '.join(parts)}{rest}"
    return f"{text} {' '.join(parts)}{rest}"


def _ends(statement: str) -> Optional[Tuple[str, str]]:
    """
    The statement up to the end of its last token, and the comments
    after it without the `;`. None when the statement has no token, or
    ends in text the tokenizer could not close.
    """
    from sustained.dialects import Dialects
    from sustained.impact.tokens import ERROR, PUNCT, tokenize

    tokens = tokenize(statement, Dialects.MYSQL)
    semicolons: List[Token] = []
    while tokens and tokens[-1].kind == PUNCT and tokens[-1].text == ";":
        semicolons.insert(0, tokens.pop())
    if not tokens or tokens[-1].kind == ERROR:
        return None
    end = tokens[-1].start + len(tokens[-1].text)
    pieces = []
    at = end
    for semicolon in semicolons:
        pieces.append(statement[at : semicolon.start])
        at = semicolon.start + 1
    pieces.append(statement[at:])
    return statement[:end].lstrip(), "".join(pieces).rstrip()


def asserted_statements(
    statements: Sequence[str], context: "EngineContext"
) -> List[str]:
    """
    The statements, in order, with the ALGORITHM and LOCK clause that
    `assertion()` writes on each ALTER TABLE, CREATE INDEX, and DROP
    INDEX that spells neither, whose predicted clause is `INSTANT`,
    `NOCOPY, LOCK=NONE`, or `INPLACE, LOCK=NONE` with confidence
    `known`, and whose table existed before the statements. Every other
    statement is returned as it is, and so is every statement when the
    context holds no version read from the server. A MigrationStatement
    keeps its migration, its transaction flag, its destructive mark,
    and its intent.
    """
    from sustained.analysis import MigrationStatement
    from sustained.dialects import Dialects
    from sustained.impact.analyzer import analyze

    if "version" not in context.read:
        return list(statements)
    report = analyze(statements, Dialects.MYSQL, context)
    mariadb = report.profile == "mariadb"
    state = RunState()
    found: List[str] = []
    for statement, impact in zip(statements, report.statements):
        asserted = _asserted(impact, state, mariadb)
        if asserted is None:
            found.append(statement)
        elif isinstance(statement, MigrationStatement):
            found.append(
                MigrationStatement(
                    asserted,
                    statement.migration_id,
                    statement.transactional,
                    statement.destructive,
                    statement.intent,
                )
            )
        else:
            found.append(asserted)
        if impact.parsed is not None:
            state.record(impact.parsed, True)
    return found


def _asserted(
    impact: "StatementImpact", state: RunState, mariadb: bool
) -> Optional[str]:
    """The statement with its asserted clause, or None to leave it."""
    parsed = impact.parsed
    if parsed is None or parsed.kind not in _ASSERTED or parsed.table is None:
        return None
    if parsed.options.get("algorithm") or parsed.options.get("lock"):
        return None
    if impact.confidence is not Confidence.KNOWN or state.is_new(parsed.table):
        return None
    # The clause is about the table the statement names. A foreign
    # key's parent table gets the exclusive metadata lock, which is no
    # clause.
    clauses = [parse_label(t.lock) for t in impact.tables if t.lock is not None]
    online = [c for c in clauses if c is not None]
    if len(online) != 1 or online[0].label not in ONLINE:
        return None
    return assertion(impact.statement, parsed.kind, online[0], mariadb)


def _drop_index_as_alter(statement: str) -> Optional[str]:
    """
    `DROP INDEX ix ON t` as `ALTER TABLE t DROP INDEX ix`, from a
    statement with no trailing comment or `;`. MariaDB's `WAIT n` or
    `NOWAIT` after the table goes with it, where ALTER TABLE takes it.
    """
    from sustained.dialects import Dialects
    from sustained.impact.tokens import tokenize

    tokens = tokenize(statement, Dialects.MYSQL)
    words = [t for t in tokens if t.is_word("ON")]
    if len(tokens) < 5 or not tokens[1].is_word("INDEX") or len(words) != 1:
        return None
    on = words[0]
    index = statement[tokens[2].start : on.start].strip()
    table = statement[on.start + len(on.text) :].strip()
    return f"ALTER TABLE {table} DROP INDEX {index}"


def _requested(facts: Facts) -> Tuple[Optional[str], Optional[str]]:
    """
    The ALGORITHM and LOCK the statement spells, DEFAULT read as none.
    MariaDB's ALTER ONLINE TABLE asks for LOCK=NONE.
    """
    options = facts.parsed.options
    algorithm = options.get("algorithm")
    lock = options.get("lock")
    algorithm = None if algorithm in (None, "DEFAULT") else str(algorithm).upper()
    lock = None if lock in (None, "DEFAULT") else str(lock).upper()
    if lock is None and options.get("online"):
        lock = "NONE"
    return algorithm, lock


def online_outcome(facts: Facts, change: Change) -> Outcome:
    """
    The outcome of an ALTER TABLE, CREATE INDEX, or DROP INDEX the
    server runs as `change` says, after the ALGORITHM and LOCK the
    statement spells.
    """
    rules = rules_for(facts)
    table = common.table(facts)
    requested_algorithm, requested_lock = _requested(facts)
    online = change.online
    refusal = _refusal(facts, online, requested_algorithm, requested_lock)
    notes = [rules[change.rule].finding(Severity.INFO, note) for note in change.notes]
    if refusal is not None:
        rule = rules["refused"]
        finding = Finding(
            rule.id,
            Severity.WARN,
            f"the server refuses this statement: {refusal}; {change.reason}",
            source=rules[change.rule].source,
        )
        return Outcome.of(
            Effect(rule, table, None, Work.CATALOG, change.confidence),
            findings=(finding,),
            confidence=change.confidence,
        )
    work = change.work
    confidence = change.confidence
    reason = change.reason
    rule_name = change.rule
    if requested_algorithm in ALGORITHMS and ALGORITHMS.index(
        str(requested_algorithm)
    ) > ALGORITHMS.index(online.algorithm):
        if requested_algorithm == "COPY":
            online = _copied(facts, online)
            work = Work.REWRITE
            copied = change.rule.rsplit(".", 1)[0] + ".copy"
            if copied in rules.by_name:
                rule_name = copied
            reason = f"ALGORITHM=COPY copies the table; without it, {reason}"
        elif is_mariadb(facts):
            # MariaDB reads ALGORITHM as the slowest algorithm the
            # statement accepts, and runs a change a faster one can run
            # as it would without the clause.
            pass
        elif change.rebuild is not None:
            online = Online(str(requested_algorithm), online.level or "NONE")
            work = Work.REWRITE
            rule_name, what = change.rebuild
            reason = (
                f"{what} in place, as ALGORITHM={requested_algorithm} asks, "
                "which rebuilds the table"
            )
        else:
            online = Online(str(requested_algorithm), online.level or "NONE")
            confidence = min(confidence, Confidence.LIKELY)
    if requested_lock is not None and online.algorithm != "INSTANT":
        level = online.level or "NONE"
        if LEVELS.index(requested_lock) > LEVELS.index(level):
            online = Online(online.algorithm, requested_lock)
    elif requested_lock is not None and not is_mariadb(facts):
        # MySQL runs a LOCK clause without ALGORITHM in place: INSTANT
        # takes no LOCK clause.
        online = Online("INPLACE", requested_lock)
        confidence = min(confidence, Confidence.LIKELY)
    rule = rules[rule_name]
    findings: List[Finding] = list(notes)
    message = blocking_message(facts, table, online, work, reason)
    if (
        requested_algorithm is None
        and online.label in (INSTANT, NOCOPY_NONE, INPLACE_NONE)
        and not facts.state.is_new(table)
    ):
        asserted = assertion(
            facts.statement, facts.parsed.kind, online, is_mariadb(facts)
        )
        if asserted is not None:
            findings.append(
                rule.finding(
                    Severity.INFO,
                    f"{reason}; assert {online.label} so the server refuses "
                    "the statement instead of running it with a slower algorithm or "
                    "a stronger lock",
                    (asserted,),
                )
            )
    elif (
        message is None
        and confidence is not Confidence.KNOWN
        and not facts.state.is_new(table)
    ):
        findings.append(rule.finding(Severity.INFO, reason))
    effect = Effect(
        rule,
        table,
        online.label,
        work,
        confidence,
        message=message,
        waits=not _bounds_its_wait(facts),
    )
    effects = [effect] + _parent_effects(facts)
    return Outcome(tuple(effects), tuple(findings), confidence)


def _copied(facts: Facts, online: Online) -> Online:
    """A change run with ALGORITHM=COPY, at the lock COPY takes or stronger."""
    copy = copy_algorithm(facts)
    level = max(copy.level or "NONE", online.level or "NONE", key=LEVELS.index)
    return Online("COPY", level)


def _bounds_its_wait(facts: Facts) -> bool:
    """
    Whether MariaDB's `WAIT n` or `NOWAIT` on the statement bounds how
    long it queues for its metadata lock, as a lock_wait_timeout under a
    day does.
    """
    wait = facts.parsed.options.get("wait")
    if wait is None or not is_mariadb(facts):
        return False
    try:
        return float(str(wait)) < _UNBOUNDED_WAIT
    except ValueError:
        return False


def blocking_message(
    facts: Facts, table: str, online: Online, work: Work, reason: str
) -> Optional[str]:
    blocked = blocks(online.label)
    if blocked < Blocks.WRITES or work is Work.CATALOG:
        return None
    who = "reads and writes on" if blocked is Blocks.READS_AND_WRITES else "writes to"
    tool = ""
    if online.algorithm == "COPY":
        tool = (
            "; an online schema change tool such as gh-ost or pt-online-schema-change "
            "copies the table without blocking writes"
        )
    return f"{reason}, and {who} {table} wait until it finishes ({online.label}){tool}"


def _refusal(
    facts: Facts,
    online: Online,
    algorithm: Optional[str],
    lock: Optional[str],
) -> Optional[str]:
    """Why the server refuses the ALGORITHM and LOCK spelled, or None."""
    if algorithm is not None and algorithm not in ALGORITHMS:
        return None
    if algorithm == "NOCOPY" and not is_mariadb(facts):
        return "MySQL has no ALGORITHM=NOCOPY"
    if algorithm is not None and ALGORITHMS.index(algorithm) < ALGORITHMS.index(
        online.algorithm
    ):
        return f"ALGORITHM={algorithm} cannot run it, which needs {online.algorithm}"
    if (
        algorithm == "INSTANT"
        and lock is not None
        and lock != "DEFAULT"
        and not is_mariadb(facts)
    ):
        return "MySQL takes no LOCK clause beside ALGORITHM=INSTANT"
    asked = f"LOCK={lock}"
    if facts.parsed.options.get("online") and facts.parsed.options.get("lock") is None:
        asked = "ALTER ONLINE TABLE, which asks for LOCK=NONE,"
    if algorithm == "COPY" and lock is not None and lock in LEVELS:
        needed = _copied(facts, online).level or "NONE"
        if LEVELS.index(lock) < LEVELS.index(needed):
            return f"{asked} cannot run ALGORITHM=COPY, which needs LOCK={needed}"
    if lock is not None and lock in LEVELS and online.level is not None:
        if LEVELS.index(lock) < LEVELS.index(online.level):
            return f"{asked} cannot run it, which needs LOCK={online.level}"
    return None


def _parent_effects(facts: Facts) -> List[Effect]:
    """
    MySQL locks the table at the other end of a foreign key that a
    statement adds or drops: it takes the exclusive metadata lock on the
    parent table, as on the table itself. MariaDB does not.
    """
    if is_mariadb(facts):
        return []
    rule = rules_for(facts)["foreign_key_parent"]
    table = common.table(facts)
    live = facts.state.original(table)
    parents: List[str] = []
    for action in facts.parsed.actions:
        options = action.options
        if (
            action.kind == "add_constraint"
            and options.get("constraint") == "foreign_key"
        ):
            parents.append(str(options.get("references")))
        elif action.kind == "add_column" and options.get("references"):
            parents.append(str(options.get("references")))
        elif action.kind == "drop_constraint" and options.get("name"):
            target = facts.context.foreign_key_target(live, str(options["name"]))
            if target is not None:
                parents.append(target)
    return [
        parent_effect(rule, table, parent) for parent in unique_names(parents, table)
    ]


def unique_names(names: Sequence[str], exclude: str) -> List[str]:
    seen = {exclude.lower()}
    found: List[str] = []
    for name in names:
        if name.lower() not in seen:
            seen.add(name.lower())
            found.append(name)
    return found


def parent_effect(rule: Rule, child: str, parent: str) -> Effect:
    return Effect(
        rule,
        parent,
        MDL_EXCLUSIVE,
        Work.CATALOG,
        message=f"MySQL takes the exclusive metadata lock on {parent}, which a "
        f"foreign key of {child} points at; reads and writes on {parent} wait "
        "while it is held",
    )
