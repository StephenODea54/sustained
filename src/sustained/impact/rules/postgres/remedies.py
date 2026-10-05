"""
The text rewrites the PostgreSQL remedies are built from.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from sustained.impact.tokens import PUNCT, Token, tokenize

_SIMPLE_NAME_RE = re.compile(r"[a-z_][a-z0-9_$]*")

# The words `pg_get_keywords()` lists as reserved, or as allowed only as
# a column name or only as a function or type name, on PostgreSQL 14
# and 18. quote_ident() quotes each of them.
_KEYWORDS = frozenset("""
    all analyse analyze and any array as asc asymmetric authorization between
    bigint binary bit boolean both case cast char character check coalesce
    collate collation column concurrently constraint create cross
    current_catalog current_date current_role current_schema current_time
    current_timestamp current_user dec decimal default deferrable desc distinct
    do else end except exists extract false fetch float for foreign freeze from
    full grant greatest group grouping having ilike in initially inner inout int
    integer intersect interval into is isnull join json json_array
    json_arrayagg json_exists json_object json_objectagg json_query json_scalar
    json_serialize json_table json_value lateral leading least left like limit
    localtime localtimestamp merge_action national natural nchar none normalize
    not notnull null nullif numeric offset on only or order out outer overlaps
    overlay placing position precision primary real references returning right
    row select session_user setof similar smallint some substring symmetric
    system_user table tablesample then time timestamp to trailing treat trim
    true union unique user using values varchar variadic verbose when where
    window with xmlattributes xmlconcat xmlelement xmlexists xmlforest
    xmlnamespaces xmlparse xmlpi xmlroot xmlserialize xmltable
    """.split())


def quoted(part: str) -> str:
    """
    One identifier as Postgres reads it: bare when it is lower case and
    no keyword, as quote_ident() writes it, and quoted otherwise.
    """
    if _SIMPLE_NAME_RE.fullmatch(part) and part not in _KEYWORDS:
        return part
    return '"' + part.replace('"', '""') + '"'


def _name_tokens(statement: str, name: str) -> Optional[List[Token]]:
    """
    The tokens of the first dotted name in the statement whose parts,
    joined with dots, are the name, as the recognizer joins them.
    """
    tokens = _tokens(statement)
    for start, token in enumerate(tokens):
        if token.name is None:
            continue
        if start and tokens[start - 1].kind == PUNCT and tokens[start - 1].text == ".":
            continue
        end = start
        while (
            end + 2 < len(tokens)
            and tokens[end + 1].kind == PUNCT
            and tokens[end + 1].text == "."
            and tokens[end + 2].name is not None
        ):
            end += 2
        span = tokens[start : end + 1 : 2]
        if ".".join(str(t.name) for t in span) == name:
            return span
    return None


def spelled(statement: str, name: str) -> str:
    """
    A name the statement contains, as the statement spells it, so each
    part keeps its quotes and a quoted part with a dot in it stays one
    part. A name the statement does not contain is quoted part by part.
    """
    span = _name_tokens(statement, name)
    if span is None:
        return ident(name)
    return ".".join(t.text for t in span)


def ident(name: str) -> str:
    """A dotted name as Postgres reads it, quoting the parts that need it."""
    return ".".join(quoted(part) for part in name.split("."))


def insert_after(statement: str, word: str, text: str) -> Optional[str]:
    """The statement with `text` inserted after its first bare `word`."""
    for token in _tokens(statement):
        if token.is_word(word):
            end = token.start + len(token.text)
            return statement[:end] + " " + text + statement[end:]
    return None


def _tokens(statement: str) -> List[Token]:
    from sustained.dialects import Dialects

    return tokenize(statement, Dialects.POSTGRES)


def concurrently_remedy(statement: str, word: str) -> Tuple[str, ...]:
    """
    The remedy that runs the statement with CONCURRENTLY after its first
    bare `word`, or no remedy when the statement has no such word.
    """
    concurrent = insert_after(statement, word, "CONCURRENTLY")
    return (trimmed(concurrent),) if concurrent else ()


def trimmed(statement: str) -> str:
    return statement.strip().rstrip(";").rstrip()


def key_columns(statement: str) -> Optional[str]:
    """The column list of the first PRIMARY KEY or UNIQUE in a statement."""
    tokens = _tokens(statement)
    for index, token in enumerate(tokens):
        if not token.is_word("KEY", "UNIQUE"):
            continue
        if token.value == "KEY" and not (
            index and tokens[index - 1].is_word("PRIMARY")
        ):
            continue
        rest = tokens[index + 1 :]
        if not rest or rest[0].kind != PUNCT or rest[0].text != "(":
            continue
        depth = 0
        for closing in rest:
            if closing.kind == PUNCT and closing.text == "(":
                depth += 1
            elif closing.kind == PUNCT and closing.text == ")":
                depth -= 1
                if depth == 0:
                    return statement[rest[0].start : closing.start + 1]
    return None


def last_part(name: str, statement: Optional[str] = None) -> str:
    """
    The last part of a dotted name: the one the statement spells, when
    it contains the name, and the text after the last dot otherwise.
    """
    span = None if statement is None else _name_tokens(statement, name)
    if span:
        return str(span[-1].name)
    return name.rsplit(".", 1)[-1]
