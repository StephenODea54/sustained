"""
Observed impact on MySQL and MariaDB: the ALGORITHM and LOCK the server
accepts for a statement, found by running it on a scratch database with
each clause in turn.

A statement that `assertion()` can write with an ALGORITHM and LOCK
clause, an ALTER TABLE, CREATE INDEX, or DROP INDEX that spells neither,
runs with each clause in the order the server itself picks one: the
cheapest algorithm first, and for each algorithm the weakest lock
first. `INSTANT` comes first, then MariaDB's `NOCOPY`, then `INPLACE`,
then `COPY`, each with `LOCK=NONE`, `SHARED`, and `EXCLUSIVE`. The
server refuses a clause it cannot run before it does any work, with
error 1845 (`ER_ALTER_OPERATION_NOT_SUPPORTED`), 1846
(`ER_ALTER_OPERATION_NOT_SUPPORTED_REASON`), or on MySQL 4092 when a
table has used every instant row version. The first clause it accepts
is the observation, and that run is the rehearsal's own run of the
statement. Any other error ends the attempts, and the statement runs
as written, so the rehearsal reports the server's own error.

The probe runs on the scratch table itself, so the table's foreign
keys, rows, row format, and instant row versions are the ones the
server reads.

`observe()` puts the accepted clause in place of the predicted lock on
the table the statement names, and the work it proves: `INSTANT` copies
nothing, and `COPY` copies the table. `NOCOPY` and `INPLACE` leave the
predicted work, since either may rebuild the table in place or build an
index. Each difference is an `impact.mismatch` finding, which quotes
the server's reason for refusing the predicted clause. The metadata
lock on a foreign key's parent table is left as predicted, and so is a
table the run created.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    FrozenSet,
    Generator,
    List,
    Mapping,
    Optional,
    Tuple,
)

from sustained.impact.context import Rows, attempt
from sustained.impact.model import (
    Evidence,
    Finding,
    Hold,
    ImpactReport,
    StatementImpact,
    TableImpact,
    Work,
)
from sustained.impact.rules import Probe, common
from sustained.impact.rules.mysql.locks import ALGORITHMS, LEVELS, Online
from sustained.impact.rules.mysql.online import assertion

if TYPE_CHECKING:
    from sustained.impact.rules import Profile


# The errors a server raises when it refuses an ALGORITHM or LOCK
# clause: ER_ALTER_OPERATION_NOT_SUPPORTED, its _REASON form, and
# MySQL's ER_INNODB_MAX_ROW_VERSION.
REFUSALS = frozenset({1845, 1846, 4092})

_PROBED = ("alter_table", "create_index", "drop_index")

# Every table outside the system schemas, and whether it is in the
# connection's own database.
_TABLES_SQL = """SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_SCHEMA = DATABASE()
FROM information_schema.TABLES
WHERE TABLE_TYPE = 'BASE TABLE'
  AND TABLE_SCHEMA NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')"""


def _key(name: str) -> str:
    return ".".join(part.strip("`") for part in name.split(".")).lower()


def tables_plan() -> Generator[str, Rows, Optional[FrozenSet[str]]]:
    """
    The names that find each table that exists, lower case, as
    `schema.table` and, in the connection's own database, as the bare
    name; or None when the read failed.
    """
    rows = yield from attempt(_TABLES_SQL)
    if rows is None:
        return None
    names = set()
    for schema, name, own in rows:
        names.add(f"{schema}.{name}".lower())
        if own is not None and int(str(own)):
            names.add(str(name).lower())
    return frozenset(names)


def candidates(mariadb: bool) -> Tuple[Online, ...]:
    """Each clause to try, in the order the server picks one."""
    found = [Online("INSTANT")]
    for algorithm in ALGORITHMS[1:]:
        if algorithm == "NOCOPY" and not mariadb:
            continue
        found.extend(Online(algorithm, level) for level in LEVELS)
    return tuple(found)


def attempts(impact: StatementImpact, profile: "Profile") -> List[Tuple[str, Online]]:
    """
    The statement written with each clause to try, beside the clause.
    Empty for a statement that is not an ALTER TABLE, CREATE INDEX, or
    DROP INDEX, that spells its own ALGORITHM or LOCK, or that cannot be
    written with one.
    """
    parsed = impact.parsed
    if parsed is None or parsed.kind not in _PROBED:
        return []
    if parsed.options.get("algorithm") or parsed.options.get("lock"):
        return []
    mariadb = profile.name == "mariadb"
    found: List[Tuple[str, Online]] = []
    for online in candidates(mariadb):
        sql = assertion(impact.statement, parsed.kind, online, mariadb)
        if sql is None:
            return []
        found.append((sql, online))
    return found


def refused(error: BaseException) -> Optional[str]:
    """
    The server's reason when the error refuses an ALGORITHM or LOCK
    clause, and None for any other error. PyMySQL, aiomysql, and asyncmy
    put the code first in `args`; mysql-connector and the MariaDB
    connector name it `errno`.
    """
    code = getattr(error, "errno", None)
    if code is None and error.args:
        code = error.args[0]
    if code not in REFUSALS:
        return None
    message = getattr(error, "msg", None)
    if message is None:
        message = error.args[1] if len(error.args) > 1 else str(error)
    return str(message)


def observe(
    impact: StatementImpact,
    probe: Probe,
    existing: Optional[FrozenSet[str]],
    profile: "Profile",
) -> StatementImpact:
    """
    The statement's impact with the clause the server accepted in place
    of the predicted lock on the table it names, and an
    `impact.mismatch` finding for each difference. `existing` lists the
    names of the tables that existed before the run; a table outside it
    was created by the run, and is left as predicted.
    """
    target = impact.parsed.table if impact.parsed is not None else None
    if target is None:
        return impact
    accepted: Online = probe.accepted
    reasons = {online.label: reason for online, reason in probe.refused}
    tables: List[TableImpact] = []
    findings: List[Finding] = []
    for table in impact.tables:
        key = _key(table.table)
        if key != _key(target) or (existing is not None and key not in existing):
            tables.append(table)
            continue
        updated, found = _compare(table, accepted, reasons, profile)
        tables.append(updated)
        findings.extend(found)
    return impact._replace(
        tables=tuple(tables),
        findings=impact.findings + tuple(findings),
        evidence=Evidence.OBSERVED,
    )


def _compare(
    table: TableImpact,
    accepted: Online,
    reasons: Mapping[str, str],
    profile: "Profile",
) -> Tuple[TableImpact, List[Finding]]:
    findings: List[Finding] = []
    updated = table
    if accepted.label != table.lock:
        message = (
            f"the rules predicted {table.lock or 'no lock'} on {table.table}, "
            f"and the server ran it with {accepted.label}"
        )
        reason = reasons.get(table.lock or "")
        if reason is not None:
            message += f"; it refused {table.lock}: {reason}"
        findings.append(common.mismatch(message))
        updated = updated._replace(
            lock=accepted.label, blocks=profile.blocks(accepted.label)
        )
    work = _work(accepted)
    if work is not None and work != table.work:
        copies = "copies the table" if work is Work.REWRITE else "copies nothing"
        findings.append(
            common.mismatch(
                f"the rules predicted {table.work} on {table.table}, and the "
                f"server ran it with {accepted.label}, which {copies}"
            )
        )
        hold = updated.hold
        if hold is not Hold.TRANSACTION:
            hold = Hold.BRIEF if work is Work.CATALOG else Hold.STATEMENT
        updated = updated._replace(work=work, hold=hold)
    return updated, findings


def _work(accepted: Online) -> Optional[Work]:
    """The work the accepted clause proves, or None when it proves none."""
    if accepted.algorithm == "INSTANT":
        return Work.CATALOG
    if accepted.algorithm == "COPY":
        return Work.REWRITE
    return None


def with_observations(
    report: ImpactReport,
    observations: Mapping[Tuple[Optional[str], int], Any],
    existing: Optional[FrozenSet[str]],
    profile: "Profile",
) -> ImpactReport:
    """
    The report with each probed statement's facts in place of the
    predicted ones. `observations` maps each statement's migration id
    and position to its `Probe`.
    """
    return common.with_observations(
        report,
        observations,
        profile,
        lambda statement, probe: observe(statement, probe, existing, profile),
    )
