"""
The ALGORITHM and LOCK a statement runs with: the clause the server
would pick, the clause the statement spells, whether the server refuses
the pair, and the clause that asserts the pick.
"""

from __future__ import annotations

from typing import (
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
)


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
    asserts the algorithm alone. None when the statement cannot be read
    back into the ALTER TABLE form.
    """
    text = statement.strip().rstrip(";").rstrip()
    parts = [f"ALGORITHM={online.algorithm}"]
    if online.level is not None:
        parts.append(f"LOCK={online.level}")
    if kind == "drop_index" and mariadb:
        altered = _drop_index_as_alter(text)
        if altered is None:
            return None
        return f"{altered}, {', '.join(parts)}"
    if kind == "alter_table":
        return f"{text}, {', '.join(parts)}"
    return f"{text} {' '.join(parts)}"


def _drop_index_as_alter(statement: str) -> Optional[str]:
    """`DROP INDEX ix ON t` as `ALTER TABLE t DROP INDEX ix`."""
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
    """The ALGORITHM and LOCK the statement spells, DEFAULT read as none."""
    options = facts.parsed.options
    algorithm = options.get("algorithm")
    lock = options.get("lock")
    algorithm = None if algorithm in (None, "DEFAULT") else str(algorithm).upper()
    lock = None if lock in (None, "DEFAULT") else str(lock).upper()
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
    notes = [
        Finding(
            rules[change.rule].id, Severity.INFO, note, source=rules[change.rule].source
        )
        for note in change.notes
    ]
    if refusal is not None:
        rule = rules["refused"]
        finding = Finding(
            rule.id,
            Severity.WARN,
            f"the server refuses this statement: {refusal}; {change.reason}",
            source=rules[change.rule].source,
        )
        return Outcome(
            (Effect(rule, table, None, Work.CATALOG, change.confidence),),
            (finding,),
            change.confidence,
        )
    work = change.work
    confidence = change.confidence
    if requested_algorithm is not None and ALGORITHMS.index(
        requested_algorithm
    ) > ALGORITHMS.index(online.algorithm):
        online = Online(requested_algorithm, online.level or "NONE")
        if requested_algorithm == "COPY":
            work = Work.REWRITE
        else:
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
    rule = rules[change.rule]
    findings: List[Finding] = list(notes)
    message = blocking_message(facts, table, online, work, change.reason)
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
                Finding(
                    rule.id,
                    Severity.INFO,
                    f"{change.reason}; assert {online.label} so the server refuses "
                    "the statement instead of running it with a slower algorithm or "
                    "a stronger lock",
                    (asserted,),
                    rule.source,
                )
            )
    elif (
        message is None
        and confidence is not Confidence.KNOWN
        and not facts.state.is_new(table)
    ):
        findings.append(
            Finding(rule.id, Severity.INFO, change.reason, source=rule.source)
        )
    effect = Effect(rule, table, online.label, work, confidence, message=message)
    effects = [effect] + _parent_effects(facts)
    return Outcome(tuple(effects), tuple(findings), confidence)


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
    if lock is not None and lock in LEVELS and online.level is not None:
        if LEVELS.index(lock) < LEVELS.index(online.level):
            return f"LOCK={lock} cannot run it, which needs LOCK={online.level}"
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
