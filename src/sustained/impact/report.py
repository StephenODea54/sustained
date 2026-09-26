"""
An `ImpactReport` as the CLI prints it: plain lines, or plain data that
`json.dumps` accepts.

The text form reads one migration at a time:

    20260926_orders_customer_idx  transaction
      CREATE INDEX ix_orders_customer ON orders (customer_id)
        orders  SHARE  blocks writes  index_build  statement  [pg.create_index]
        warn    writes to orders wait for the whole index build; ...
        fix     CREATE INDEX CONCURRENTLY ix_orders_customer ON orders (customer_id)
      window  orders: SHARE from statement 1, held to commit

    1 statement, 0 danger, 2 warn. Evidence: static (assumed PostgreSQL 12)

A report read with `live=True` ends with its preflight:

    preflight
      ALTER TABLE orders ADD COLUMN note text would queue behind pid 4121
      (idle in transaction for 42m, user=billing, app=billing-worker,
      has ACCESS SHARE on orders)
        last statement: SELECT * FROM orders WHERE id = 1
      pid 5003 has had a transaction open for 12m (active, user=report)
      1 blocker, 1 transaction open 60s or longer. Read: locks, transactions

Each blocker and transaction is one line; the first is wrapped here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Sequence, Union

from sustained.impact.context import version_text
from sustained.impact.model import (
    Blocks,
    Confidence,
    Finding,
    ImpactReport,
    MigrationImpact,
    Severity,
    StatementImpact,
    TableImpact,
)
from sustained.impact.rules import release, title
from sustained.impact.window import DATABASE

if TYPE_CHECKING:
    from sustained.impact.preflight import Blocker, LiveSession, Preflight

JsonValue = Union[
    str, int, float, bool, None, Sequence["JsonValue"], Mapping[str, "JsonValue"]
]
"""What the data functions return: what json.dumps accepts."""


def statement_data(impact: StatementImpact) -> Dict[str, JsonValue]:
    """One statement's impact as plain data."""
    severity = impact.severity
    return {
        "kind": impact.parsed.kind if impact.parsed is not None else None,
        "severity": str(severity) if severity is not None else None,
        "confidence": str(impact.confidence),
        "evidence": str(impact.evidence),
        "tables": [_table_data(t) for t in impact.tables],
        "findings": [finding_data(f) for f in impact.findings],
    }


def _table_data(table: TableImpact) -> Dict[str, JsonValue]:
    return {
        "table": table.table,
        "lock": table.lock,
        "blocks": str(table.blocks),
        "work": str(table.work),
        "hold": str(table.hold),
        "rows": table.rows,
        "bytes": table.bytes,
        "rule": table.rule,
    }


def finding_data(finding: Finding) -> Dict[str, JsonValue]:
    """One finding as plain data."""
    return {
        "rule": finding.rule,
        "severity": str(finding.severity),
        "message": finding.message,
        "remedy": list(finding.remedy),
        "source": finding.source,
    }


def report_data(report: ImpactReport) -> Dict[str, JsonValue]:
    """The whole report as plain data."""
    return {
        "profile": report.profile,
        "version": version_text(report.version),
        "evidence": str(report.evidence),
        "read": sorted(report.read),
        "migrations": [_migration_data(m) for m in report.migrations],
        "counts": {str(s): report.count(s) for s in Severity},
        "preflight": (
            preflight_data(report.preflight) if report.preflight is not None else None
        ),
    }


def preflight_data(preflight: "Preflight") -> Dict[str, JsonValue]:
    """A preflight as plain data."""
    return {
        "profile": preflight.profile,
        "older_than": preflight.older_than,
        "read": sorted(preflight.read),
        "blockers": [
            {
                "statement": blocker.statement,
                "table": blocker.table,
                "lock": blocker.lock,
                "held": blocker.held,
                "granted": blocker.granted,
                "session": _session_data(blocker.session),
            }
            for blocker in preflight.blockers
        ],
        "transactions": [_session_data(s) for s in preflight.transactions],
    }


def _session_data(session: "LiveSession") -> Dict[str, JsonValue]:
    return {
        "id": session.id,
        "label": session.label,
        "user": session.user,
        "application": session.application,
        "state": session.state,
        "transaction_seconds": session.transaction_seconds,
        "query": session.query,
    }


def _migration_data(migration: MigrationImpact) -> Dict[str, JsonValue]:
    return {
        "id": migration.migration_id,
        "transactional": migration.transactional,
        "held_to_commit": migration.held_to_commit,
        "statements": [
            {"sql": s.statement, **statement_data(s)} for s in migration.statements
        ],
        "locks": [
            {
                "table": lock.table,
                "lock": lock.lock,
                "blocks": str(lock.blocks),
                "statement": lock.statement,
            }
            for lock in migration.locks
        ],
        "windows": [
            {
                "table": window.table,
                "blocks": str(window.blocks),
                "taken_by": window.taken_by,
                "heaviest": str(window.heaviest),
                "during": window.during,
            }
            for window in migration.windows
        ],
        "findings": [finding_data(f) for f in migration.findings],
    }


# --- text --------------------------------------------------------------


def _size(table: TableImpact) -> Optional[str]:
    parts = []
    if table.rows is not None:
        parts.append(f"~{_scaled(table.rows, 1000, ('', 'K', 'M', 'B'))} rows")
    if table.bytes is not None:
        parts.append(_scaled(table.bytes, 1024, (" B", " KB", " MB", " GB", " TB")))
    return ", ".join(parts) if parts else None


def _scaled(value: int, step: int, units: tuple[str, ...]) -> str:
    number = float(value)
    for unit in units:
        if number < step or unit == units[-1]:
            if unit.strip() in ("", "B"):
                return f"{int(number)}{unit}"
            return f"{number:.1f}{unit}"
        number /= step
    raise AssertionError("unreachable")  # pragma: no cover


def table_line(table: TableImpact) -> str:
    """One table's impact on one line."""
    parts = [
        table.table,
        table.lock or "no lock",
        f"blocks {table.blocks}",
        str(table.work),
        str(table.hold),
    ]
    size = _size(table)
    if size:
        parts.append(size)
    if table.rule:
        parts.append(f"[{table.rule}]")
    return "  ".join(parts)


def _finding_lines(finding: Finding, indent: str) -> List[str]:
    lines = [f"{indent}{finding.severity!s:<7} {finding.message}"]
    for number, statement in enumerate(finding.remedy):
        label = "fix" if number == 0 else ""
        lines.append(f"{indent}{label:<7} {statement}")
    return lines


def _window_lines(migration: MigrationImpact, indent: str = "  ") -> List[str]:
    if not migration.held_to_commit:
        return []
    lines = []
    for window in migration.windows:
        if window.table == DATABASE:
            taken = _database_locks(migration)
        else:
            taken = [
                f"{lock.lock} from statement {lock.statement}"
                for lock in migration.locks
                if lock.table.lower() == window.table.lower()
            ]
        lines.append(
            f"{indent}window  {window.table}: {', '.join(taken)}, held to commit"
        )
    return lines


def _database_locks(migration: MigrationImpact) -> List[str]:
    """Each lock of a database-wide window, from the first statement to take it."""
    first: Dict[str, int] = {}
    for lock in migration.locks:
        if lock.blocks >= Blocks.WRITES:
            first.setdefault(str(lock.lock), lock.statement)
    return [f"{lock} from statement {position}" for lock, position in first.items()]


def statement_annotation(impact: StatementImpact) -> List[str]:
    """
    One statement's impact as the lines `script(annotate=True)` prints
    above it: its tables, then its findings.
    """
    lines = [table_line(table) for table in impact.tables]
    for finding in impact.findings:
        lines.extend(_finding_lines(finding, ""))
    return lines or ["locks no table"]


def migration_annotation(migration: MigrationImpact) -> List[str]:
    """
    One migration's windows and findings as the lines
    `script(annotate=True)` prints after its last statement.
    """
    lines = _window_lines(migration, "")
    for finding in migration.findings:
        lines.extend(_finding_lines(finding, ""))
    return lines


def render(report: ImpactReport) -> str:
    """The report as the lines `sustained impact` prints."""
    lines: List[str] = []
    for migration in report.migrations:
        if lines:
            lines.append("")
        scope = "transaction" if migration.transactional else "no transaction"
        lines.append(f"{migration.migration_id or '(no migration)'}  {scope}")
        for statement in migration.statements:
            lines.append(f"  {statement.statement}")
            for table in statement.tables:
                lines.append(f"    {table_line(table)}")
            for finding in statement.findings:
                lines.extend(_finding_lines(finding, "    "))
        lines.extend(_window_lines(migration))
        for finding in migration.findings:
            lines.extend(_finding_lines(finding, "  "))
    if lines:
        lines.append("")
    lines.append(summary(report))
    if report.preflight is not None:
        lines.extend(["", render_preflight(report.preflight)])
    return "\n".join(lines)


def summary(report: ImpactReport) -> str:
    """The report's last line: the counts and what the answer rests on."""
    count = len(report.statements)
    parts = [
        f"{count} statement" + ("" if count == 1 else "s"),
        f"{report.count(Severity.DANGER)} danger",
        f"{report.count(Severity.WARN)} warn",
    ]
    unknown = sum(1 for s in report.statements if s.confidence is Confidence.UNKNOWN)
    if unknown:
        parts.append(f"{unknown} unknown")
    engine = title(report.profile)
    version = release(report.profile, report.version)
    assumed = "" if "version" in report.read else "assumed "
    basis = f"{report.evidence} ({assumed}{engine} {version})"
    return f"{', '.join(parts)}. Evidence: {basis}"


def flagged(statements: Sequence[StatementImpact]) -> List[StatementImpact]:
    """
    The statements a plan lists, in the order given: those with a `warn`
    or `danger` finding, and those the analysis could not read.
    """
    return [
        s
        for s in statements
        if s.confidence is Confidence.UNKNOWN
        or (s.severity is not None and s.severity >= Severity.WARN)
    ]


def flagged_line(impact: StatementImpact) -> str:
    """One plan line: the worst severity, the statement, and its rules."""
    if impact.confidence is Confidence.UNKNOWN:
        severity = Severity.INFO
        rules = ["impact.unknown"]
    else:
        severity = impact.severity or Severity.INFO
        rules = [f.rule for f in impact.findings if f.severity >= Severity.WARN]
    unique = list(dict.fromkeys(rules))
    return f"{severity!s:<6}  {impact.statement}  [{', '.join(unique)}]"


# --- preflight ---------------------------------------------------------


def _duration(seconds: float) -> str:
    """A duration as people read it, such as `42s`, `12m`, or `3h 5m`."""
    whole = int(seconds)
    if whole < 120:
        return f"{whole}s"
    if whole < 7200:
        return f"{whole // 60}m"
    return f"{whole // 3600}h {whole % 3600 // 60}m"


def _one_line(sql: str, limit: Optional[int] = None) -> str:
    flat = " ".join(sql.split())
    if limit is not None and len(flat) > limit:
        return flat[: limit - 3] + "..."
    return flat


def _session_details(session: "LiveSession") -> List[str]:
    parts = []
    if session.state and session.transaction_seconds is not None:
        parts.append(f"{session.state} for {_duration(session.transaction_seconds)}")
    elif session.state:
        parts.append(session.state)
    elif session.transaction_seconds is not None:
        parts.append(f"transaction open for {_duration(session.transaction_seconds)}")
    if session.user:
        parts.append(f"user={session.user}")
    if session.application:
        parts.append(f"app={session.application}")
    return parts


def blocker_line(blocker: "Blocker") -> str:
    """One blocker on one line: the statement, the session, and the lock."""
    details = _session_details(blocker.session)
    statement = _one_line(blocker.statement)
    if blocker.held is None:
        what = f"would wait for the transaction of {blocker.session.label} to end"
    else:
        verb = "has" if blocker.granted else "waits for"
        details.append(f"{verb} {blocker.held} on {blocker.table}")
        what = f"would queue behind {blocker.session.label}"
    return f"{statement} {what} ({', '.join(details)})"


def transaction_line(session: "LiveSession") -> str:
    """One long transaction on one line."""
    age = _duration(session.transaction_seconds or 0.0)
    line = f"{session.label} has had a transaction open for {age}"
    details = _session_details(session._replace(transaction_seconds=None))
    return f"{line} ({', '.join(details)})" if details else line


def preflight_summary(preflight: "Preflight") -> str:
    """The preflight's last line: the counts and what was read."""
    blockers = len(preflight.blockers)
    transactions = len(preflight.transactions)
    parts = [
        f"{blockers} blocker" + ("" if blockers == 1 else "s"),
        f"{transactions} transaction"
        + ("" if transactions == 1 else "s")
        + f" open {_duration(preflight.older_than)} or longer",
    ]
    read = ", ".join(sorted(preflight.read)) or "nothing"
    line = f"{', '.join(parts)}. Read: {read}"
    missing = sorted({"locks", "transactions"} - preflight.read)
    if missing:
        line += f". Not read: {', '.join(missing)}"
    return line


def render_preflight(preflight: "Preflight") -> str:
    """The preflight as the lines `sustained impact --live` prints."""
    lines = ["preflight"]
    for blocker in preflight.blockers:
        lines.append(f"  {blocker_line(blocker)}")
        if blocker.session.query:
            lines.append(f"    last statement: {_one_line(blocker.session.query, 200)}")
    for session in preflight.transactions:
        lines.append(f"  {transaction_line(session)}")
        if session.query:
            lines.append(f"    last statement: {_one_line(session.query, 200)}")
    lines.append(f"  {preflight_summary(preflight)}")
    return "\n".join(lines)
