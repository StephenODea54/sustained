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


def charset_of(collation: str) -> str:
    """The character set a collation belongs to, such as `utf8mb4`."""
    return _charset_name(collation.split("_", 1)[0])


def _charset_name(name: str) -> str:
    # The servers read utf8 as utf8mb3, and report it so.
    lowered = name.strip("`'\"").lower()
    return "utf8mb3" if lowered == "utf8" else lowered


def _collation_name(name: str) -> str:
    lowered = name.strip("`'\"").lower()
    if lowered.startswith("utf8_"):
        return "utf8mb3_" + lowered[5:]
    return lowered


# The default collation of a character set, where every supported
# server version has the same one. MariaDB 11.5 moved the Unicode
# character sets to the uca1400 collations, so they are MySQL's alone.
_DEFAULT_COLLATIONS = {
    "latin1": "latin1_swedish_ci",
    "ascii": "ascii_general_ci",
    "binary": "binary",
}
_MYSQL_DEFAULT_COLLATIONS = {
    "utf8mb3": "utf8mb3_general_ci",
    "utf8mb4": "utf8mb4_0900_ai_ci",
}


def _default_collation(facts: Facts, charset: str) -> Optional[str]:
    """The collation a column of `charset` takes, or None when not known."""
    found = _DEFAULT_COLLATIONS.get(charset)
    if found is None and not is_mariadb(facts):
        found = _MYSQL_DEFAULT_COLLATIONS.get(charset)
    return found


def collation_change(
    facts: Facts,
    old: Optional[str],
    charset: Optional[str],
    collate: Optional[str],
    table_collation: Optional[str],
    indexed: Optional[bool],
    raw_type: str,
) -> Optional[Change]:
    """
    The change a MODIFY or CHANGE makes to a column's collation, or None
    when it keeps it. `old` is the column's collation, None for a column
    without one, `charset` and `collate` what the statement names, and
    `table_collation` the table's default, which the column takes when
    the statement names neither. A character set named without COLLATE
    gives the column its default collation, which the rules know for
    some character sets only. `indexed` says whether the column is part
    of an index, None when that was not read.

    A new collation of the same character set, or utf8mb3 to utf8mb4,
    changes only the data dictionary, unless the column is part of an
    index: MySQL then copies the table, and MariaDB rebuilds the
    indexes in place. utf8mb3 to utf8mb4 on a VARCHAR whose longest
    value passes 255 bytes needs a two-byte length on every row, which
    copies the table, and so does any other new character set.
    """
    if old is None:
        return None
    before = _collation_name(old)
    if collate is not None:
        after: Optional[str] = _collation_name(collate)
        new_charset: Optional[str] = charset_of(after or "")
    elif charset is not None:
        new_charset = _charset_name(charset)
        after = _default_collation(facts, new_charset)
    elif table_collation is not None:
        after = _collation_name(table_collation)
        new_charset = charset_of(after)
    else:
        after, new_charset = None, None
    if after == before:
        return None
    old_charset = charset_of(before)
    if new_charset is None:
        return Change(
            copy_algorithm(facts),
            Work.REWRITE,
            "modify_column.copy",
            "the column takes the table's default collation, which the rules "
            "did not read, so the change counts as a new character set, which "
            "copies the table",
            Confidence.LIKELY,
        )
    widened = old_charset == "utf8mb3" and new_charset == "utf8mb4"
    if new_charset != old_charset and not widened:
        return Change(
            copy_algorithm(facts),
            Work.REWRITE,
            "modify_column.copy",
            f"{old_charset} to {new_charset} converts every row",
        )
    unsure = indexed is None
    if widened:
        length = _LENGTH_RE.match(_normal_type(raw_type))
        if length is None or length.group(1) != "varchar":
            unsure = True
        elif int(length.group(2)) * 3 <= 255 < int(length.group(2)) * 4:
            return Change(
                copy_algorithm(facts),
                Work.REWRITE,
                "modify_column.copy",
                "utf8mb3 to utf8mb4 needs a two-byte length on every row, which "
                "copies the table",
            )
    change = _collated(facts, indexed is not False, widened)
    # A charset named without COLLATE takes the charset's default
    # collation, which the rules may not know; an instant change costs
    # the same whether it changes or not.
    if after is None and change.online.algorithm != "INSTANT":
        unsure = True
    if unsure:
        change = change._replace(confidence=Confidence.LIKELY)
    return change


def _collated(facts: Facts, indexed: bool, widened: bool) -> Change:
    """A new collation of the same character set, or utf8mb3 to utf8mb4."""
    if not indexed:
        if is_mariadb(facts):
            return Change(
                Online("INSTANT"),
                Work.CATALOG,
                "modify_column.instant",
                "the new collation changes the data dictionary",
            )
        return Change(
            Online("INPLACE", "NONE"),
            Work.CATALOG,
            "modify_column.inplace",
            "the new collation changes only the data dictionary",
        )
    if is_mariadb(facts):
        # MariaDB 12.3 changes an indexed column from utf8mb3 to utf8mb4
        # instantly, and 11.4 rebuilds its indexes.
        return Change(
            Online("NOCOPY", "NONE"),
            Work.INDEX_BUILD,
            "modify_column.rebuild",
            "the indexes on the column are built again for the new collation",
            Confidence.LIKELY if widened else Confidence.KNOWN,
        )
    return Change(
        copy_algorithm(facts),
        Work.REWRITE,
        "modify_column.copy",
        "a new collation on a column in an index copies the table",
    )


def mariadb_widens_instantly(
    old: int, new: int, collation: Optional[str], row_format: Optional[str]
) -> Tuple[bool, Confidence]:
    """
    Whether MariaDB widens a VARCHAR from `old` to `new` characters in
    the data dictionary. On COMPACT, DYNAMIC, and COMPRESSED rows a
    value of 128 bytes or more takes a two-byte length once the longest
    value can pass 255 bytes, so a column whose longest value took 128
    to 255 bytes is copied when it widens past 255. REDUNDANT rows
    store every length the same way. `collation` None is read as
    unknown: one byte and four to a character are both tried.
    """
    width = _CHARSET_BYTES.get(charset_of(collation or ""))
    widths = (width,) if width is not None else (1, 4)
    crosses = {128 <= old * w <= 255 < new * w for w in widths}
    if crosses == {False}:
        return True, Confidence.KNOWN
    confidence = Confidence.KNOWN if len(crosses) == 1 else Confidence.LIKELY
    if row_format is None:
        return False, Confidence.LIKELY
    return row_format.upper() == "REDUNDANT", confidence


def type_change(
    facts: Facts,
    old: str,
    new: str,
    collation: Optional[str],
    row_format: Optional[str] = None,
) -> Optional[Change]:
    """
    The change a new column type makes, or None when it is the same.
    `collation` is the column's, and `row_format` the table's.
    """
    a, b = _normal_type(old), _normal_type(new)
    if a == b:
        return None
    mariadb = is_mariadb(facts)
    lengths = _LENGTH_RE.match(a), _LENGTH_RE.match(b)
    if lengths[0] and lengths[1] and lengths[0].group(1) == lengths[1].group(1):
        before, after = int(lengths[0].group(2)), int(lengths[1].group(2))
        if after >= before:
            if mariadb:
                binary = lengths[0].group(1) == "varbinary"
                instant, confidence = mariadb_widens_instantly(
                    before, after, "binary" if binary else collation, row_format
                )
                if not instant:
                    return Change(
                        copy_algorithm(facts),
                        Work.REWRITE,
                        "modify_column.copy",
                        f"{old} to {new} needs a two-byte length on the values of "
                        "128 bytes or more, which copies every row",
                        confidence,
                    )
                return Change(
                    Online("INSTANT"),
                    Work.CATALOG,
                    "modify_column.instant",
                    f"{old} to {new} widens the column in the data dictionary",
                    confidence,
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
