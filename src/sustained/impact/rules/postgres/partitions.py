"""
Partitioned tables: the partitions a statement on a partitioned table
also locks, from the partitions the context read found and the
partitions the run changed.

A partitioned table stores no rows. A statement on it that reaches its
rows, such as `CREATE INDEX` or an `ALTER TABLE` action that PostgreSQL
applies to each partition, locks every partition below it, partitions
of partitions included. Adding, attaching, detaching, or dropping a
partition also locks the partitioned table it belongs to, and its
DEFAULT partition, whose rows PostgreSQL checks against the new bound.

The run state adds what earlier statements of the run changed: a
partitioned table the run created with `PARTITION BY`, a partition it
created with `PARTITION OF` or attached, and a partition it detached,
with its renames and drops followed (`RunState.relation()`). These are
known with or without the partitions read.

Without the partitions read, whether a table is a partitioned table or
a partition is not known. A handler whose answer would change if a
table it names were one gives `unread()` findings for that table, which
`merge_unread()` joins into one `pg.partitions_unread` finding for the
statement, whose confidence is then at most `likely`. A lock on a table
no read names that may be larger than the tables the statement names,
the DEFAULT partition `ATTACH PARTITION` scans and the partitioned table
`DROP TABLE` of a partition locks, goes in the outcome's `unnamed`. A
table the run created is never in the read, so it draws none. A
partition the run attached below a table is known, and what is below it
is not, so `cascade()` gives each such partition the same finding and
confidence `likely`.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from sustained.impact.context import Relation
from sustained.impact.model import Confidence, Finding, Severity, Work
from sustained.impact.rules import Effect, Facts, Outcome, Rule, listed
from sustained.impact.rules.postgres.catalog import DOCS

# The id of the finding for a statement whose answer depends on the
# partitions, when they were not read.
UNREAD = "pg.partitions_unread"
_UNREAD_SOURCE = DOCS + "ddl-partitioning.html"


def relation(facts: Facts, table: str) -> Optional[Relation]:
    """
    The catalog facts about a table, from the read and the run, or None
    when neither has any.
    """
    return facts.state.relation(table, facts.context)


def partitioned(facts: Facts, table: str) -> bool:
    """Whether the read or the run makes the table a partitioned table."""
    found = relation(facts, table)
    return found is not None and found.partitioned


def descendants(facts: Facts, table: str) -> List[str]:
    """Every partition below a partitioned table, nearest first."""
    found: List[str] = []
    seen = {table.lower()}
    pending = [table]
    while pending:
        current = relation(facts, pending.pop(0))
        for name in current.partitions if current is not None else ():
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            found.append(name)
            pending.append(name)
    return found


def cascade(
    facts: Facts,
    rule: Rule,
    table: str,
    lock: Optional[str],
    work: Work,
    confidence: Confidence = Confidence.KNOWN,
) -> List[Effect]:
    """
    The same lock and work on each partition below the table. Without
    the partitions read, a partition the run did not create has what is
    below it unread, and a note that says so.
    """
    effects: List[Effect] = []
    for name in descendants(facts, table):
        effect = Effect(rule, name, lock, work, confidence)
        if "partitions" not in facts.context.read and not (
            facts.state.created_in_run(name)
        ):
            effect = effect._replace(
                confidence=min(confidence, Confidence.LIKELY),
                notes=(_unread_note([name], locked_below(name, lock)),),
            )
        effects.append(effect)
    return effects


def default_partition(facts: Facts, parent: str) -> Optional[str]:
    """The DEFAULT partition of a partitioned table, or None."""
    found = relation(facts, parent)
    return None if found is None else found.default


def parent_of(facts: Facts, table: str) -> Optional[str]:
    """The partitioned table a partition belongs to, or None."""
    found = relation(facts, table)
    return None if found is None else found.parent


def is_unread(facts: Facts, table: str) -> bool:
    """
    Whether the partitions were not read, so whether the table is a
    partitioned table or a partition is not known. A table the run
    created is never in the read, and its partitions are the run's.
    """
    return "partitions" not in facts.context.read and not (
        facts.state.created_in_run(table)
    )


def unread(facts: Facts, table: str, clause: str) -> Tuple[Finding, ...]:
    """
    A finding that says what the statement also does if the table is
    partitioned or a partition, in `clause`, such as `if t is a
    partitioned table, each partition below it is also locked SHARE`,
    when `is_unread()`; otherwise none. `merge_unread()` joins these,
    and reads the table's name from the remedy, where it is kept until
    then.
    """
    if not is_unread(facts, table):
        return ()
    return (Finding(UNREAD, Severity.INFO, clause, (table,), _UNREAD_SOURCE),)


def locked_below(table: str, lock: Optional[str]) -> str:
    """The clause for the partitions below a table the read did not cover."""
    return (
        f"if {table} is a partitioned table, each partition below it is also "
        f"locked {lock}"
    )


def merge_unread(facts: Facts, outcome: Outcome) -> Outcome:
    """
    The outcome with its `unread()` findings joined into one finding,
    with the clauses in order and each once, with `partitions_unread`
    set, and with its confidence at most `likely`. The analyzer gives a
    statement the lowest confidence of its outcome and its effects, so
    this lowers the statement's. The finding is a note on the last
    effect on a table the run did not create, so it follows the
    findings about the statement's work.
    """
    clauses: Dict[str, None] = {}
    tables: Dict[str, None] = {}
    others: List[Finding] = []
    for finding in outcome.findings:
        if finding.rule == UNREAD:
            clauses[finding.message] = None
            tables.update(dict.fromkeys(finding.remedy))
        else:
            others.append(finding)
    if not clauses:
        if any(n.rule == UNREAD for e in outcome.effects for n in e.notes):
            return outcome._replace(partitions_unread=True)
        return outcome
    note = _unread_note(list(tables), "; ".join(clauses))
    effects = list(outcome.effects)
    for i in reversed(range(len(effects))):
        if not facts.state.created_in_run(effects[i].table):
            effects[i] = effects[i]._replace(notes=effects[i].notes + (note,))
            break
    else:
        others.append(note)
    return outcome._replace(
        effects=tuple(effects),
        findings=tuple(others),
        confidence=min(outcome.confidence, Confidence.LIKELY),
        partitions_unread=True,
    )


def _unread_note(names: List[str], clauses: str) -> Finding:
    """The `pg.partitions_unread` finding for the tables and clauses."""
    unknown = (
        f"whether {names[0]} is a partitioned table or a partition"
        if len(names) == 1
        else f"whether {listed(names)} are partitioned tables or partitions"
    )
    return Finding(
        UNREAD,
        Severity.INFO,
        f"the partitions were not read, so it is not known {unknown}; " + clauses,
        source=_UNREAD_SOURCE,
    )
