"""
The InnoDB lock labels, what each blocks and how they rank, and the
`lock_wait_timeout` statement and values.
"""

from __future__ import annotations

from typing import (
    NamedTuple,
    Optional,
)

from sustained.impact.model import (
    Blocks,
)

INSTANT = "INSTANT"
NOCOPY_NONE = "NOCOPY, LOCK=NONE"
INPLACE_NONE = "INPLACE, LOCK=NONE"
COPY_NONE = "COPY, LOCK=NONE"
INPLACE_SHARED = "INPLACE, LOCK=SHARED"
COPY_SHARED = "COPY, LOCK=SHARED"
INPLACE_EXCLUSIVE = "INPLACE, LOCK=EXCLUSIVE"
COPY_EXCLUSIVE = "COPY, LOCK=EXCLUSIVE"
MDL_EXCLUSIVE = "MDL EXCLUSIVE"
ROW_LOCKS = "IX"

LOCKS = (
    ROW_LOCKS,
    INSTANT,
    NOCOPY_NONE,
    INPLACE_NONE,
    COPY_NONE,
    INPLACE_SHARED,
    COPY_SHARED,
    INPLACE_EXCLUSIVE,
    COPY_EXCLUSIVE,
    MDL_EXCLUSIVE,
)

ALGORITHMS = ("INSTANT", "NOCOPY", "INPLACE", "COPY")
LEVELS = ("NONE", "SHARED", "EXCLUSIVE")

# lock_wait_timeout is in seconds. MySQL's default is a year and
# MariaDB's a day, so a value of a day or more bounds nothing.
UNBOUNDED_SECONDS = 86400


class Online(NamedTuple):
    """
    How the server runs an ALTER TABLE: the algorithm, and the LOCK
    level, which is None for INSTANT.
    """

    algorithm: str
    level: Optional[str] = None

    @property
    def label(self) -> str:
        """The clause the server accepts, such as `INPLACE, LOCK=NONE`."""
        if self.level is None:
            return self.algorithm
        return f"{self.algorithm}, LOCK={self.level}"

    def combined(self, other: "Online") -> "Online":
        """What the server does for both changes in one statement."""
        algorithm = max(self.algorithm, other.algorithm, key=ALGORITHMS.index)
        if algorithm == "INSTANT":
            return Online(algorithm)
        level = max(self.level or "NONE", other.level or "NONE", key=LEVELS.index)
        return Online(algorithm, level)


def parse_label(label: str) -> Optional[Online]:
    """A lock label read back as an `Online`, or None for another lock."""
    parts = [p.strip() for p in label.split(",")]
    if not parts or parts[0] not in ALGORITHMS:
        return None
    if len(parts) == 1:
        return Online(parts[0]) if parts[0] == "INSTANT" else None
    level = parts[1].replace(" ", "")
    if not level.startswith("LOCK=") or level[5:] not in LEVELS:
        return None
    return Online(parts[0], level[5:])


def blocks(lock: Optional[str]) -> Blocks:
    """What a lock label blocks while the statement's work runs."""
    if lock is None:
        return Blocks.NOTHING
    if lock == ROW_LOCKS:
        return Blocks.DDL
    if lock in (INSTANT, MDL_EXCLUSIVE):
        return Blocks.READS_AND_WRITES
    online = parse_label(lock)
    level = online.level if online is not None else "EXCLUSIVE"
    return {
        "NONE": Blocks.DDL,
        "SHARED": Blocks.WRITES,
    }.get(level or "", Blocks.READS_AND_WRITES)


def lock_rank(lock: Optional[str]) -> int:
    """
    A lock's strength, -1 for no lock. The row locks rank lowest and the
    exclusive metadata lock highest. Between them a label ranks by its
    LOCK level, then by its algorithm, which orders `LOCKS` and places a
    label outside it, such as `NOCOPY, LOCK=SHARED`, among the others.
    A label the rules cannot read ranks above every other.
    """
    if lock is None:
        return -1
    if lock == ROW_LOCKS:
        return 0
    if lock == MDL_EXCLUSIVE:
        return 1 + len(ALGORITHMS) * len(LEVELS) + 1
    online = parse_label(lock)
    if online is None:
        return 1 + len(ALGORITHMS) * len(LEVELS) + 2
    level = LEVELS.index(online.level) if online.level is not None else 0
    return 1 + level * len(ALGORITHMS) + ALGORITHMS.index(online.algorithm)


def queues(lock: Optional[str]) -> bool:
    """
    Whether waiting for the lock queues other sessions: every lock but
    a DML statement's needs the exclusive metadata lock for a moment.
    """
    return lock is not None and lock != ROW_LOCKS


def bounded(value: str) -> bool:
    """Whether a `lock_wait_timeout` value bounds the wait."""
    try:
        seconds = float(value)
    except ValueError:
        return False
    return 0 < seconds < UNBOUNDED_SECONDS


def timeout_statement(transactional: bool) -> str:
    """The statement that bounds how long the next metadata locks queue."""
    return "SET SESSION lock_wait_timeout = 5"
