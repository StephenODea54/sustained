"""
The facts the SQL Server handlers share: whether the database reads
committed rows from row versions, what the edition allows, the WITH
options a statement spells, and the effect and message for an online
operation.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional, Tuple

from sustained.impact.model import Blocks, Confidence, Finding, Severity, Work
from sustained.impact.rules import Effect, Facts, Rule
from sustained.impact.rules.mssql.catalog import EDITIONS_SOURCE, ONLINE_EDITION
from sustained.impact.rules.mssql.locks import SCH_M, X, enterprise

ENTERPRISE_EDITIONS = "Enterprise, Developer, and Azure SQL"

# The WAIT_AT_LOW_PRIORITY the remedies offer: wait a minute beside the
# lock queue, then give up.
LOW_PRIORITY = (
    "WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)"
)


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


def is_resumable(options: Mapping[str, object]) -> bool:
    """Whether the statement spells WITH (RESUMABLE = ON ...)."""
    return str(with_options(options).get("RESUMABLE", "OFF")).upper().startswith("ON")


def abort_after_wait(options: Mapping[str, object]) -> Optional[str]:
    """
    The ABORT_AFTER_WAIT of a WAIT_AT_LOW_PRIORITY the statement spells,
    inside its ONLINE = ON value or as an option of its own, such as
    `SELF`, or None without one.
    """
    found = with_options(options)
    for text in (found.get("WAIT_AT_LOW_PRIORITY"), found.get("ONLINE")):
        match = re.search(r"(?i)ABORT_AFTER_WAIT\s*=\s*(\w+)", str(text or ""))
        if match is not None:
            return match.group(1).upper()
    return None


def queues(options: Mapping[str, object]) -> bool:
    """
    Whether other sessions queue behind the statement while it waits for
    its lock. A WAIT_AT_LOW_PRIORITY request waits beside the queue, and
    with ABORT_AFTER_WAIT = SELF or BLOCKERS it never joins it; with
    NONE it joins the queue once MAX_DURATION has passed.
    """
    return abort_after_wait(options) not in ("SELF", "BLOCKERS")


def low_priority(statement: str) -> str:
    """The statement with WAIT_AT_LOW_PRIORITY added to its ONLINE = ON."""
    return re.sub(
        r"(?i)\bONLINE\s*=\s*ON\b",
        f"ONLINE = ON ({LOW_PRIORITY})",
        statement,
        count=1,
    )


def resumable_findings(
    facts: Facts, rule: Rule, options: Mapping[str, object]
) -> Tuple[Finding, ...]:
    """
    The `danger` finding for RESUMABLE = ON where the server refuses it:
    inside a transaction (error 574), and without ONLINE = ON (error
    11438).
    """
    if not is_resumable(options):
        return ()
    if facts.transactional:
        message = (
            "RESUMABLE = ON cannot run inside a transaction, so this migration "
            "fails; run it in a migration with transactional=False"
        )
    elif not is_online(options):
        message = "RESUMABLE = ON needs ONLINE = ON, so the statement fails"
    else:
        return ()
    return (rule.finding(Severity.DANGER, message),)


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
    options: Mapping[str, object],
    low_priority_from: Optional[Tuple[int, ...]] = None,
) -> Effect:
    """
    The effect of an operation with ONLINE = ON. It takes a lock on the
    table when it starts, which waits for the open transactions that
    write the table, works while holding Sch-S, so reads and writes go
    on, and takes `final` on the table once its work is done, which
    waits for the transactions that conflict with it. Inside a
    transaction that lock is held until the migration commits.

    Outside a transaction the effect names `final`, so the lock-timeout
    finding and the preflight check the sessions its locks wait for, and
    blocks only DDL, since other sessions wait only while those locks are
    waited for. `low_priority_from` is the first version on which the
    statement takes WAIT_AT_LOW_PRIORITY, which the remedy offers, or
    None when no version does.
    """
    waits = queues(options)
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
            waits=waits,
            at_end=True,
        )
    if final == SCH_M:
        ends = (
            f"for every open transaction and query on {table} for the Sch-M lock "
            "it takes when it ends"
        )
    else:
        ends = f"for them again for the {final} lock it takes when it ends"
    remedy: Tuple[str, ...] = ()
    if (
        abort_after_wait(options) is None
        and low_priority_from is not None
        and facts.context.version >= low_priority_from
    ):
        remedy = (low_priority(facts.statement),)
    note = rule.finding(
        Severity.INFO,
        f"{what} runs online, but waits for the open transactions that write "
        f"{table} for the lock it takes when it starts, and {ends}",
        remedy,
    )
    return Effect(
        rule, table, final, work, notes=(note,), blocks=Blocks.DDL, waits=waits
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
