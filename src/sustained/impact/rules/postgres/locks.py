"""
The PostgreSQL table locks, weakest first, what each blocks, and the
statement that bounds how long a lock may queue.
"""

from __future__ import annotations

import re

from sustained.impact.model import Blocks
from sustained.impact.rules import LockOrder

ACCESS_SHARE = "ACCESS SHARE"
ROW_SHARE = "ROW SHARE"
ROW_EXCLUSIVE = "ROW EXCLUSIVE"
SHARE_UPDATE_EXCLUSIVE = "SHARE UPDATE EXCLUSIVE"
SHARE = "SHARE"
SHARE_ROW_EXCLUSIVE = "SHARE ROW EXCLUSIVE"
EXCLUSIVE = "EXCLUSIVE"
ACCESS_EXCLUSIVE = "ACCESS EXCLUSIVE"

ORDER = LockOrder(
    (
        (ACCESS_SHARE, Blocks.DDL),
        (ROW_SHARE, Blocks.DDL),
        (ROW_EXCLUSIVE, Blocks.DDL),
        (SHARE_UPDATE_EXCLUSIVE, Blocks.DDL),
        (SHARE, Blocks.WRITES),
        (SHARE_ROW_EXCLUSIVE, Blocks.WRITES),
        (EXCLUSIVE, Blocks.WRITES),
        (ACCESS_EXCLUSIVE, Blocks.READS_AND_WRITES),
    )
)
LOCKS = ORDER.names
blocks = ORDER.blocks
lock_rank = ORDER.rank


def timeout_statement(transactional: bool) -> str:
    """The statement that bounds how long the next locks may queue."""
    scope = "LOCAL " if transactional else ""
    return f"SET {scope}lock_timeout = '5s'"


def lock_name(mode: str) -> str:
    """A `pg_locks.mode` value as the rules name it: `ShareLock` is `SHARE`."""
    if mode.endswith("Lock"):
        mode = mode[: -len("Lock")]
    return " ".join(re.findall(r"[A-Z][a-z]*", mode)).upper()
