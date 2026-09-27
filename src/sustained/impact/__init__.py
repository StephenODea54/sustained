"""
Statement impact analysis: what a migration statement does to a live
database while it runs, such as the locks it takes, what those locks
block, and whether the table is rewritten.

`analyze()` reads the statements a run would apply and returns an
`ImpactReport`, with no database. `attach_impact()` puts each
statement's part of that report on the statement. `read_context()` and
`async_read_context()` read the server facts it takes as its context.
The pieces:

- `sustained.impact.tokens`: the tokenizer the textual scan shares
- `sustained.impact.recognizer`: statement text to a `ParsedStatement`
- `sustained.impact.model`: the report and its vocabulary
- `sustained.impact.context`: the server facts the rules read
- `sustained.impact.state`: what a run carries between statements
- `sustained.impact.rules`: the rule profiles, one package per engine,
  each with the catalog read and, on Postgres, the traced rehearsal's
  reads
- `sustained.impact.window`: locks held across a migration
- `sustained.impact.preflight`: what a run would wait behind on a live
  server

`preflight()` and `async_preflight()` read the sessions a run's
statements would wait behind.
"""

from sustained.impact.analyzer import analyze, attach_impact
from sustained.impact.context import (
    EngineContext,
    Relation,
    TableStats,
    async_read_context,
    named_tables,
    read_context,
)
from sustained.impact.model import (
    Blocks,
    Confidence,
    Evidence,
    Finding,
    Hold,
    ImpactReport,
    Lock,
    MigrationImpact,
    Severity,
    StatementImpact,
    TableImpact,
    Thresholds,
    UnnamedLock,
    Window,
    Work,
)
from sustained.impact.preflight import (
    Blocker,
    LiveSession,
    Preflight,
    async_preflight,
    preflight,
)
from sustained.impact.rules import supported

__all__ = [
    "analyze",
    "attach_impact",
    "async_preflight",
    "async_read_context",
    "named_tables",
    "preflight",
    "read_context",
    "supported",
    "Blocker",
    "Blocks",
    "Confidence",
    "EngineContext",
    "Evidence",
    "Finding",
    "Hold",
    "ImpactReport",
    "LiveSession",
    "Lock",
    "MigrationImpact",
    "Preflight",
    "Relation",
    "Severity",
    "StatementImpact",
    "TableImpact",
    "TableStats",
    "Thresholds",
    "UnnamedLock",
    "Window",
    "Work",
]
