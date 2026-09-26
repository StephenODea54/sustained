"""
The facts the SQL Server handlers share: whether the database reads
committed rows from row versions, what the edition allows, the WITH
options a statement spells, and the effect and message for an online
operation.
"""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

from sustained.impact.model import Blocks, Confidence, Finding, Severity, Work
from sustained.impact.rules import Effect, Facts, Rule
from sustained.impact.rules.mssql.catalog import EDITIONS_SOURCE, ONLINE_EDITION
from sustained.impact.rules.mssql.locks import SCH_S, X, enterprise

ENTERPRISE_EDITIONS = "Enterprise, Developer, and Azure SQL"


def row_versioning(facts: Facts) -> Optional[bool]:
    """
    Whether READ_COMMITTED_SNAPSHOT is on, so readers read row versions
    instead of waiting for X locks, or None when it was not read.
    """
    value = facts.context.settings.get("read_committed_snapshot")
    return None if value is None else value == "on"


def exclusive_blocks(facts: Facts) -> Blocks:
    """
    What an X lock blocks: writes only when readers read row versions,
    and reads too otherwise, which is the case assumed when the setting
    was not read.
    """
    return Blocks.WRITES if row_versioning(facts) else Blocks.READS_AND_WRITES


def with_options(options: Mapping[str, object]) -> Mapping[str, str]:
    """The WITH (...) options a statement or action spelled."""
    found = options.get("with")
    return found if isinstance(found, Mapping) else {}


def is_online(options: Mapping[str, object]) -> bool:
    """Whether the statement spells WITH (ONLINE = ON ...)."""
    return str(with_options(options).get("ONLINE", "OFF")).startswith("ON")


def online_findings(facts: Facts, rule: Rule) -> Tuple[Finding, ...]:
    """
    The finding for ONLINE = ON where the edition may lack it: `danger`
    on an edition without online operations, where the statement fails,
    and `info` when the edition was not read.
    """
    allowed = enterprise(facts.context)
    if allowed:
        return ()
    if allowed is None:
        return (
            Finding(
                ONLINE_EDITION.id,
                Severity.INFO,
                f"ONLINE = ON runs only on the {ENTERPRISE_EDITIONS} editions, "
                "and the statement fails on the others; the edition was not read",
                source=EDITIONS_SOURCE,
            ),
        )
    return (
        Finding(
            ONLINE_EDITION.id,
            Severity.DANGER,
            f"ONLINE = ON runs only on the {ENTERPRISE_EDITIONS} editions, so the "
            f"statement fails on {facts.context.edition}",
            source=EDITIONS_SOURCE,
        ),
    )


def online_effect(
    facts: Facts,
    rule: Rule,
    table: str,
    final: str,
    work: Work,
    what: str,
    notes: Tuple[Finding, ...] = (),
) -> Effect:
    """
    The effect of an operation with ONLINE = ON: it runs holding Sch-S,
    so reads and writes go on, and takes `final` on the table once its
    work is done. Inside a transaction that lock is held until the
    migration commits.
    """
    if facts.transactional:
        return Effect(
            rule,
            table,
            final,
            work,
            message=(
                f"{what} runs online, but the {final} lock it takes on {table} "
                "when it ends is held until the migration commits; run it in a "
                "migration with transactional=False, or last in its migration"
            ),
            notes=notes,
            at_end=True,
        )
    return Effect(
        rule,
        table,
        SCH_S,
        work,
        message=(
            f"{what} runs online, and takes {final} on {table} briefly when it "
            "ends, which waits for the queries running on the table"
        ),
        notes=notes,
    )


def unread_type(column: str, table: str) -> str:
    """The note for a column whose current type the schema read lacks."""
    return f"the current type of {table}.{column} was not read"


def escalation(facts: Facts, table: str) -> Tuple[Optional[str], Confidence]:
    """
    Whether a write to every row of the table escalates to an X lock:
    SQL Server escalates once one statement has 5,000 locks on a
    table. Returns X, or None when the table has fewer rows, with the
    confidence the row count gives.
    """
    rows = facts.context.stats(facts.state.original(table)).rows
    if rows is None:
        return X, Confidence.LIKELY
    return (X if rows >= ESCALATION_LOCKS else None), Confidence.KNOWN


ESCALATION_LOCKS = 5000
