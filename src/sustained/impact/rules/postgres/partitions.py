"""
Partitioned tables: the partitions a statement on a partitioned table
also locks, from the partitions the context read found.

A partitioned table stores no rows. A statement on it that reaches its
rows, such as `CREATE INDEX` or an `ALTER TABLE` action that PostgreSQL
applies to each partition, locks every partition below it, partitions
of partitions included. Adding, attaching, detaching, or dropping a
partition also locks the partitioned table it belongs to, and its
DEFAULT partition, whose rows PostgreSQL checks against the new bound.

A table the run created is not in the read, so a partition the run
added is not locked by a later statement on its parent.
"""

from __future__ import annotations

from typing import List, Optional

from sustained.impact.context import Relation
from sustained.impact.model import Confidence, Work
from sustained.impact.rules import Effect, Facts, Rule


def relation(facts: Facts, table: str) -> Optional[Relation]:
    """The catalog facts about a table, or None when the read has none."""
    return facts.context.relations.get(facts.state.original(table).lower())


def partitioned(facts: Facts, table: str) -> bool:
    """Whether the read found the table to be a partitioned table."""
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
    """The same lock and work on each partition below the table."""
    return [
        Effect(rule, name, lock, work, confidence) for name in descendants(facts, table)
    ]


def default_partition(facts: Facts, parent: str) -> Optional[str]:
    """The DEFAULT partition of a partitioned table, or None."""
    found = relation(facts, parent)
    return None if found is None else found.default


def parent_of(facts: Facts, table: str) -> Optional[str]:
    """The partitioned table a partition belongs to, or None."""
    found = relation(facts, table)
    return None if found is None else found.parent
