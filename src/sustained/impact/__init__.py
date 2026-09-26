"""
Statement impact analysis: what a migration statement does to a live
database while it runs, such as the locks it takes, what those locks
block, and whether the table is rewritten.

`analyze()` reads the statements a run would apply and returns an
`ImpactReport`, with no database. The pieces:

- `sustained.impact.tokens`: the tokenizer the textual scan shares
- `sustained.impact.recognizer`: statement text to a `ParsedStatement`
- `sustained.impact.model`: the report and its vocabulary
- `sustained.impact.context`: the server facts the rules read
- `sustained.impact.state`: what a run carries between statements
- `sustained.impact.rules`: the rule profiles, one per engine
- `sustained.impact.window`: locks held across a migration
"""

from sustained.impact.analyzer import analyze
from sustained.impact.context import EngineContext, TableStats
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
    Window,
    Work,
)
from sustained.impact.rules import supported

__all__ = [
    "analyze",
    "supported",
    "Blocks",
    "Confidence",
    "EngineContext",
    "Evidence",
    "Finding",
    "Hold",
    "ImpactReport",
    "Lock",
    "MigrationImpact",
    "Severity",
    "StatementImpact",
    "TableImpact",
    "TableStats",
    "Thresholds",
    "Window",
    "Work",
]
