"""
The SQL Server lock modes, what each blocks and how they rank, the
editions that have online index operations, and the `SET LOCK_TIMEOUT`
statement and values.
"""

from __future__ import annotations

from typing import Optional, Tuple

from sustained.impact.context import EngineContext
from sustained.impact.model import Blocks

SCH_S = "Sch-S"
IS = "IS"
IX = "IX"
S = "S"
SIX = "SIX"
X = "X"
SCH_M = "Sch-M"

# Weakest first, by the conflicts in the lock compatibility matrix.
LOCKS = (SCH_S, IS, IX, S, SIX, X, SCH_M)

# SERVERPROPERTY('EngineEdition') values whose engine has online index
# operations and adds a NOT NULL column with a runtime constant default
# as a catalog change: Enterprise, Developer, and Evaluation (3), Azure
# SQL Database (5), and Azure SQL Managed Instance (8).
_ENTERPRISE_ENGINES = frozenset({"3", "5", "8"})

# The release year of each major version, which is how people name one.
_RELEASES = {
    11: "2012",
    12: "2014",
    13: "2016",
    14: "2017",
    15: "2019",
    16: "2022",
    17: "2025",
}


def blocks(lock: Optional[str]) -> Blocks:
    """
    What a table lock blocks under locking READ COMMITTED, the worst
    case: Sch-S and the intent locks only block other DDL, S and SIX
    block writes, and X and Sch-M block reads too. A handler lowers X
    to writes when the database reads under row versioning.
    """
    if lock is None:
        return Blocks.NOTHING
    if lock in (SCH_S, IS, IX):
        return Blocks.DDL
    if lock in (S, SIX):
        return Blocks.WRITES
    return Blocks.READS_AND_WRITES


def lock_rank(lock: Optional[str]) -> int:
    """A lock's strength, weakest first, and -1 for no lock."""
    if lock is None or lock not in LOCKS:
        return -1
    return LOCKS.index(lock)


def timeout_statement(transactional: bool) -> str:
    """The statement that bounds how long a lock waits, in milliseconds."""
    return "SET LOCK_TIMEOUT 5000"


def bounded(value: str) -> bool:
    """
    Whether a LOCK_TIMEOUT value bounds the wait: -1, the default, waits
    for ever, and 0 or more waits that many milliseconds.
    """
    try:
        return int(value.strip()) >= 0
    except ValueError:
        return False


def enterprise(context: EngineContext) -> Optional[bool]:
    """
    Whether the server's edition has online index operations, or None
    when the edition was not read.
    """
    engine = context.settings.get("EngineEdition")
    if engine is None:
        return None
    return engine in _ENTERPRISE_ENGINES


def release(version: Tuple[int, ...]) -> str:
    """A version as its release, such as `2022 (16.0.4135.4)`."""
    number = ".".join(str(part) for part in version)
    year = _RELEASES.get(version[0]) if version else None
    return f"{year} ({number})" if year else number
