"""
The text rewrites the PostgreSQL remedies are built from.
"""

from __future__ import annotations

import re
from typing import List, Optional

from sustained.impact.tokens import PUNCT, Token, tokenize

_SIMPLE_NAME_RE = re.compile(r"[a-z_][a-z0-9_$]*")
TRANSACTION_NOTE = "in a migration with transactional=False"


def ident(name: str) -> str:
    """A dotted name as Postgres reads it, quoting the parts that need it."""
    return ".".join(
        part if _SIMPLE_NAME_RE.fullmatch(part) else '"' + part.replace('"', '""') + '"'
        for part in name.split(".")
    )


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


def last_part(name: str) -> str:
    return name.rsplit(".", 1)[-1]
