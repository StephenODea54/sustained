"""
The `--json` payloads: the keys each command prints, and the printer.
"""

from __future__ import annotations

import json
from typing import (
    Dict,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

JsonValue = Union[
    str, int, float, bool, None, Sequence["JsonValue"], Mapping[str, "JsonValue"]
]
"""Anything --json prints: what json.dumps accepts, and nothing else."""


_JSON_KEYS: Dict[str, Tuple[str, ...]] = {
    "status": ("migrations",),
    "plan": ("pending", "problems", "drift"),
    "impact": ("profile", "version", "evidence", "read", "migrations", "counts"),
    "rehearse": ("rehearsed", "scratch", "key", "recorded", "ok", "impact"),
    "validate": ("ok", "problems"),
}
"""
The top-level keys each --json command prints besides `error`. A failed
run prints the same keys, all null, so a caller reads one set of keys
whatever the outcome.
"""


def _print_json(payload: Mapping[str, JsonValue], error: Optional[str] = None) -> None:
    print(json.dumps({**payload, "error": error}, indent=2))


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
