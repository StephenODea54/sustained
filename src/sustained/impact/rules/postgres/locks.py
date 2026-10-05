"""
The PostgreSQL table locks, weakest first, what each blocks, and the
statement that bounds how long a lock may queue.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional

from sustained.impact.model import (
    Blocks,
)

ACCESS_SHARE = "ACCESS SHARE"
ROW_SHARE = "ROW SHARE"
ROW_EXCLUSIVE = "ROW EXCLUSIVE"
SHARE_UPDATE_EXCLUSIVE = "SHARE UPDATE EXCLUSIVE"
SHARE = "SHARE"
SHARE_ROW_EXCLUSIVE = "SHARE ROW EXCLUSIVE"
EXCLUSIVE = "EXCLUSIVE"
ACCESS_EXCLUSIVE = "ACCESS EXCLUSIVE"

LOCKS = (
    ACCESS_SHARE,
    ROW_SHARE,
    ROW_EXCLUSIVE,
    SHARE_UPDATE_EXCLUSIVE,
    SHARE,
    SHARE_ROW_EXCLUSIVE,
    EXCLUSIVE,
    ACCESS_EXCLUSIVE,
)
_BLOCKS: Mapping[str, Blocks] = {
    ACCESS_SHARE: Blocks.DDL,
    ROW_SHARE: Blocks.DDL,
    ROW_EXCLUSIVE: Blocks.DDL,
    SHARE_UPDATE_EXCLUSIVE: Blocks.DDL,
    SHARE: Blocks.WRITES,
    SHARE_ROW_EXCLUSIVE: Blocks.WRITES,
    EXCLUSIVE: Blocks.WRITES,
    ACCESS_EXCLUSIVE: Blocks.READS_AND_WRITES,
}


def blocks(lock: Optional[str]) -> Blocks:
    """What a Postgres table lock blocks; an unnamed lock blocks nothing."""
    if lock is None:
        return Blocks.NOTHING
    return _BLOCKS[lock]


def lock_rank(lock: Optional[str]) -> int:
    """A lock's strength: its place in `LOCKS`, or -1 for no lock."""
    return -1 if lock is None else LOCKS.index(lock)


def timeout_statement(transactional: bool) -> str:
    """The statement that bounds how long the next locks may queue."""
    scope = "LOCAL " if transactional else ""
    return f"SET {scope}lock_timeout = '5s'"


def lock_name(mode: str) -> str:
    """A `pg_locks.mode` value as the rules name it: `ShareLock` is `SHARE`."""
    if mode.endswith("Lock"):
        mode = mode[: -len("Lock")]
    return " ".join(re.findall(r"[A-Z][a-z]*", mode)).upper()
