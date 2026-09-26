"""
Column type changes: whether a new type is the same type in another
spelling, and whether a longer VARCHAR keeps its length prefix.
"""

from __future__ import annotations

import re
from typing import (
    Optional,
    Tuple,
)

from sustained.impact.model import (
    Confidence,
    Work,
)
from sustained.impact.rules import Facts
from sustained.impact.rules.mysql.facts import (
    Change,
    copy_algorithm,
    is_mariadb,
)
from sustained.impact.rules.mysql.locks import (
    Online,
)

_DISPLAY_WIDTH_RE = re.compile(
    r"^(tinyint|smallint|mediumint|int|bigint)\(\d+\)(.*)$", re.IGNORECASE
)
_TYPE_ALIASES = {"integer": "int", "bool": "tinyint(1)", "boolean": "tinyint(1)"}
_LENGTH_RE = re.compile(r"^(varchar|varbinary)\((\d+)\)$")
_MEMBERS_RE = re.compile(r"^(enum|set)\((.*)\)$", re.DOTALL)
# The most bytes a character takes in each character set.
_CHARSET_BYTES = {
    "utf8mb4": 4,
    "utf8mb3": 3,
    "utf8": 3,
    "ucs2": 2,
    "utf16": 4,
    "utf16le": 4,
    "utf32": 4,
    "latin1": 1,
    "ascii": 1,
    "binary": 1,
}


def _normal_type(text: str) -> str:
    """
    A column type in one spelling: lower case, and without an integer
    display width, which changes nothing stored. `tinyint(1)` keeps its
    width, since it is how MySQL spells a boolean.
    """
    lowered = " ".join(text.lower().replace("`", "").split())
    lowered = _TYPE_ALIASES.get(lowered, lowered)
    match = _DISPLAY_WIDTH_RE.match(lowered)
    if match and not lowered.startswith("tinyint(1)"):
        lowered = match.group(1) + match.group(2)
    return lowered


def _members(body: str) -> Tuple[str, ...]:
    return tuple(part.strip() for part in body.split(","))


def length_bytes(
    old: int, new: int, collation: Optional[str]
) -> Tuple[bool, Confidence]:
    """
    Whether a VARCHAR keeps its length prefix: one byte while its
    longest value takes at most 255 bytes, two above that.
    """
    charset = (collation or "").split("_", 1)[0].lower()
    width = _CHARSET_BYTES.get(charset)
    if width is not None:
        return (old * width > 255) == (new * width > 255), Confidence.KNOWN
    one = (old > 255) == (new > 255)
    four = (old * 4 > 255) == (new * 4 > 255)
    if one == four:
        return one, Confidence.KNOWN
    # The character set was not read; utf8mb4 is the default.
    return four, Confidence.LIKELY


def type_change(
    facts: Facts, old: str, new: str, collation: Optional[str]
) -> Optional[Change]:
    """The change a new column type makes, or None when it is the same."""
    a, b = _normal_type(old), _normal_type(new)
    if a == b:
        return None
    mariadb = is_mariadb(facts)
    lengths = _LENGTH_RE.match(a), _LENGTH_RE.match(b)
    if lengths[0] and lengths[1] and lengths[0].group(1) == lengths[1].group(1):
        before, after = int(lengths[0].group(2)), int(lengths[1].group(2))
        if after >= before:
            if mariadb:
                return Change(
                    Online("INSTANT"),
                    Work.CATALOG,
                    "modify_column.instant",
                    f"{old} to {new} widens the column in the data dictionary",
                )
            same, confidence = length_bytes(before, after, collation)
            if same:
                return Change(
                    Online("INPLACE", "NONE"),
                    Work.CATALOG,
                    "modify_column.inplace",
                    f"{old} to {new} keeps the length prefix, so only the data "
                    "dictionary changes",
                    confidence,
                )
            return Change(
                copy_algorithm(facts),
                Work.REWRITE,
                "modify_column.copy",
                f"{old} to {new} needs a longer length prefix on every row",
                confidence,
            )
    members = _MEMBERS_RE.match(a), _MEMBERS_RE.match(b)
    if members[0] and members[1] and members[0].group(1) == members[1].group(1):
        before_members = _members(members[0].group(2))
        after_members = _members(members[1].group(2))
        kind = members[0].group(1)
        appended = after_members[: len(before_members)] == before_members
        if kind == "enum":
            same_size = (len(before_members) > 255) == (len(after_members) > 255)
        else:
            same_size = (len(before_members) + 7) // 8 == (len(after_members) + 7) // 8
        if appended and same_size:
            return Change(
                Online("INSTANT"),
                Work.CATALOG,
                "modify_column.instant",
                f"new {kind.upper()} members are added at the end",
            )
    return Change(
        copy_algorithm(facts),
        Work.REWRITE,
        "modify_column.copy",
        f"{old} to {new} converts every row",
    )
