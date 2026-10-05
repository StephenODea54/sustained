"""
The lexical layer shared by the textual scan and the impact recognizer.

Two consumers read statement text here. `sustained.analysis` scans the
words of a statement for drops and keeps literals and comments out of
the scan; it takes `lex()`, which keeps whitespace and comments as
tokens, and `scan_readings()`. The impact recognizer reads a statement
as a token stream; it takes `tokenize()`, which leaves them out.

Both read the same literal and comment rules, so the scan and the
recognizer cannot disagree about where a literal ends:

- a string literal is `'...'`, with `''` as an escaped quote
- a quoted identifier is `"..."`, with `""` as an escaped quote, or a
  MySQL `` `...` ``
- a comment is `-- ...` to the end of the line, or `/* ... */`
- a Postgres dollar-quoted string is `$$...$$` or `$tag$...$tag$`

A dollar tag follows the identifier rules Postgres gives it: letters,
digits, and underscores, not starting with a digit, and not glued to the
end of a word. So `$1` is a parameter and `a$b$c` is one word.

The dialect decides what differs between engines, as each server reads
it:

- Postgres, DuckDB, and SQL Server nest block comments, so
  `/* a /* b */ c */` is one comment. MySQL, MariaDB, and SQLite end a
  block comment at the first `*/`.
- Postgres, DuckDB, and SQL Server end a `--` comment at a carriage
  return or a newline, and MySQL, MariaDB, and SQLite at a newline only.
- MySQL and MariaDB read `#` as a comment to the end of the line, and
  `--` as a comment only when whitespace, a control character, or the
  end of the text follows it: `1--1` is `1 - -1`. They run the body of a
  `/*! ... */` comment, and MariaDB of a `/*M! ... */` comment, as SQL,
  after the five or six digit version that may open it. The body is read
  as SQL on the MYSQL dialect, which covers both servers, and the `*/`
  that ends it is left out like a comment.
- MySQL reads a backslash inside a literal as an escape, and a
  double-quoted string; SQL Server and SQLite read `[...]` identifiers.
- Each server has its own whitespace. Postgres and SQLite read only
  ASCII whitespace, and a character such as a no-break space is part of
  the word around it, so `CONCURRENTLY<no-break space>idx` is one name.
  SQL Server also reads the control characters and the Unicode spaces
  as whitespace, and DuckDB reads the no-break space and most of the
  Unicode spaces.

A keyword is matched by its ASCII upper case form, as every server
matches keywords, so a word such as `lımıt` is a name and not LIMIT.

Every reading is linear in the length of the text: a quote or comment
that never closes ends the reading at once with an ERROR token whose
text is the rest of the text.
"""

from __future__ import annotations

import re
import string
from typing import TYPE_CHECKING, Dict, List, NamedTuple, Optional, Tuple

if TYPE_CHECKING:
    from sustained.dialects import Dialects

# Token kinds. A keyword is a WORD; the recognizer compares its upper
# case form. An unterminated literal, quoted identifier, dollar quote, or
# block comment yields one ERROR token for the rest of the statement, so
# nothing reads text whose quoting is in doubt. SPACE and COMMENT come
# only from `lex()`: a COMMENT is a comment, or the opening or closing
# marker of a MySQL executable comment.
WORD = "word"
IDENT = "ident"
STRING = "string"
NUMBER = "number"
PARAM = "param"
PUNCT = "punct"
OP = "op"
ERROR = "error"
SPACE = "space"
COMMENT = "comment"


class Token(NamedTuple):
    """
    One lexical token. `text` is the token as the statement spells it.
    `value` is what it means: the ASCII upper case form of a word, the
    name inside the quotes of a quoted identifier, the contents of a
    string literal with its escapes undone, and the text itself for the
    rest. `start` is the offset of the token in the statement.
    """

    kind: str
    text: str
    value: str
    start: int

    def is_word(self, *words: str) -> bool:
        """Whether this is a bare word, and when words are given, one of them."""
        return self.kind == WORD and (not words or self.value in words)

    def is_punct(self, *chars: str) -> bool:
        """Whether this is punctuation, and when chars are given, one of them."""
        return self.kind == PUNCT and (not chars or self.text in chars)

    @property
    def name(self) -> Optional[str]:
        """The identifier this token names, bare or quoted, or None."""
        if self.kind == IDENT:
            return self.value
        if self.kind == WORD:
            return self.text
        return None


_ASCII_SPACE = " \t\n\r\f\v"
_POSTGRES_SPACE = " \t\n\r\f"
_SQLITE_SPACE = " \t\n\r\f"
_UNICODE_SPACES = (
    "\xa0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u200b\u202f\u205f\u3000"
)
_MSSQL_SPACE = (
    "".join(chr(c) for c in range(0x21)) + "\x85\u1680\u2028\u2029" + _UNICODE_SPACES
)
_DUCKDB_SPACE = _POSTGRES_SPACE + _UNICODE_SPACES + "\ufeff"


class Lexicon(NamedTuple):
    """
    The lexical rules of one reading of a statement. `lexicon()` gives
    the rules of a dialect, and `scan_readings()` the readings a textual
    scan takes.
    """

    backslash_escapes: bool
    double_quoted_strings: bool
    bracket_identifiers: bool
    dollar_quotes: bool
    nested_comments: bool
    # The characters that end a `--` comment.
    line_ends: str
    # MySQL's `#` comments, `--` comments that need a space after them,
    # and `/*! ... */` bodies read as SQL.
    mysql_comments: bool
    whitespace: str


_STANDARD = Lexicon(False, False, True, False, False, "\n\r", False, _ASCII_SPACE)
_POSTGRES = Lexicon(False, False, False, True, True, "\n\r", False, _POSTGRES_SPACE)
_DUCKDB = _POSTGRES._replace(whitespace=_DUCKDB_SPACE)
_MYSQL = Lexicon(True, True, False, False, False, "\n", True, _ASCII_SPACE)
_MSSQL = Lexicon(False, False, True, False, True, "\n\r", False, _MSSQL_SPACE)
_SQLITE = Lexicon(False, False, True, False, False, "\n", False, _SQLITE_SPACE)
_OTHER = Lexicon(False, False, False, False, False, "\n\r", False, _ASCII_SPACE)
# The reading the textual scan took before it knew a dialect: standard
# literals and comments, Postgres dollar quotes, and no `[...]`.
_GENERIC = _OTHER._replace(dollar_quotes=True)


def lexicon(dialect: Optional["Dialects"]) -> Lexicon:
    """
    The lexical rules of a dialect. With no dialect, the standard rules
    apply with `[...]` identifiers read as SQLite reads them. DEFAULT is
    the SQLite reading, which takes `[...]` and backtick identifiers.
    """
    if dialect is None:
        return _STANDARD
    return {
        "POSTGRES": _POSTGRES,
        "DUCKDB": _DUCKDB,
        "MYSQL": _MYSQL,
        "MSSQL": _MSSQL,
        "DEFAULT": _SQLITE,
    }.get(dialect.name, _OTHER)


def scan_readings(
    dialect: Optional["Dialects"], backslash: bool
) -> Tuple[Lexicon, ...]:
    """
    The readings a textual scan takes of a statement, most exact first.

    With a dialect, the scan takes the dialect's reading. With none, the
    text could be for any engine, so it takes the reading it took before
    it knew a dialect and each engine's reading, all of them with
    Postgres dollar quotes, so a drop that any engine would run shows in
    one of them. When the statement has a backslash, each reading is
    also taken with the backslash rule reversed: MySQL with
    NO_BACKSLASH_ESCAPES reads `'C:\\'` as a literal, and Postgres with
    standard_conforming_strings off reads `'it\\'s'` as one.
    """
    if dialect is not None:
        readings: Tuple[Lexicon, ...] = (lexicon(dialect),)
    else:
        readings = (_GENERIC,) + tuple(
            reading._replace(dollar_quotes=True)
            for reading in (_POSTGRES, _MYSQL, _MSSQL, _SQLITE, _DUCKDB)
        )
    if not backslash:
        return readings
    reversed_ = tuple(
        r._replace(backslash_escapes=not r.backslash_escapes) for r in readings
    )
    return tuple(r for pair in zip(readings, reversed_) for r in pair)


def _word_pattern(whitespace: str) -> "re.Pattern[str]":
    """
    A word: a letter, an underscore, or a character past ASCII, then
    those, digits, and `$`. A character the reading counts as whitespace
    ends the word.
    """
    wide = sorted(ord(c) for c in whitespace if ord(c) >= 0x80)
    ranges: List[str] = []
    low = 0x80
    for code in wide:
        if code > low:
            ranges.append(f"{chr(low)}-{chr(code - 1)}")
        low = code + 1
    ranges.append(f"{chr(low)}-\U0010ffff")
    past_ascii = "".join(ranges)
    return re.compile(f"[A-Za-z_{past_ascii}][A-Za-z_0-9${past_ascii}]*")


_WORD_PATTERNS: Dict[str, "re.Pattern[str]"] = {}
_NUMBER_RE = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_PARAM_RE = re.compile(r"\$\d+|%\(\w+\)s|%s|\?|:[A-Za-z_]\w*")
_DOLLAR_RE = re.compile(r"\$(?P<tag>[A-Za-z_][A-Za-z_0-9]*)?\$")
_OP_RE = re.compile(r"::|<>|!=|<=|>=|\|\||->>|->|[=<>+\-*/%|&^~!@#:]")
_PUNCT = "(),;.[]"
# A prefix that makes the literal after it a string of another kind:
# E'...' (escapes), N'...' (national), X'...' and B'...' (bits and
# bytes), U&'...' (unicode escapes).
_PREFIX_RE = re.compile(r"(?:[EeNnXxBb]|[Uu]&)'")
# What opens a MySQL executable comment, and the version after it.
_EXECUTABLE_RE = re.compile(r"/\*M?!(?:[0-9]{5,6})?")
_COMMENT_MARK_RE = re.compile(r"/\*|\*/")
_QUOTE_STOPS: Dict[Tuple[str, bool], "re.Pattern[str]"] = {}
_ASCII_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)


def _quoted(
    sql: str, start: int, quote: str, backslash: bool
) -> Optional[Tuple[str, int]]:
    """
    Reads a quoted run that opens at `start`, where `quote` closes it and
    a doubled `quote` escapes it. Returns the contents with the escapes
    undone and the offset after the closing quote, or None when the run
    never closes.
    """
    stops = _QUOTE_STOPS.get((quote, backslash))
    if stops is None:
        stops = re.compile(re.escape(quote) + (r"|\\" if backslash else ""))
        _QUOTE_STOPS[(quote, backslash)] = stops
    out: List[str] = []
    i = start + 1
    end = len(sql)
    while True:
        stop = stops.search(sql, i)
        if stop is None:
            return None
        at = stop.start()
        out.append(sql[i:at])
        if sql[at] == "\\":
            if at + 1 >= end:
                return None
            out.append(sql[at + 1])
            i = at + 2
            continue
        if sql.startswith(quote, at + 1):
            out.append(quote)
            i = at + 2
            continue
        return "".join(out), at + 1


def _block_comment_end(sql: str, start: int, nested: bool) -> int:
    """
    The offset after the `*/` that closes the block comment opening at
    `start`, or -1 when it never closes.
    """
    if not nested:
        close = sql.find("*/", start + 2)
        return -1 if close < 0 else close + 2
    depth = 1
    i = start + 2
    while depth:
        mark = _COMMENT_MARK_RE.search(sql, i)
        if mark is None:
            return -1
        depth += 1 if mark.group(0) == "/*" else -1
        i = mark.end()
    return i


def _line_end(sql: str, start: int, ends: str) -> int:
    """The offset after the line comment that opens at `start`."""
    close = len(sql)
    for char in ends:
        at = sql.find(char, start, close)
        if at >= 0:
            close = at
    return close if close == len(sql) else close + 1


def _literal(sql: str, i: int, rules: Lexicon) -> Optional[Tuple[str, int, str]]:
    """
    The string, quoted identifier, or dollar-quoted string that opens at
    `i`: its kind, the offset after it, and its value. The kind is ERROR
    when it never closes, and the result is None when none opens at `i`.
    """
    char = sql[i]
    unclosed = (ERROR, i, "")
    prefix = _PREFIX_RE.match(sql, i)
    if prefix and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
        escapes = rules.backslash_escapes or sql[i] in "Ee"
        read = _quoted(sql, prefix.end() - 1, "'", escapes)
        return unclosed if read is None else (STRING, read[1], read[0])
    if char == "'" or (char == '"' and rules.double_quoted_strings):
        read = _quoted(sql, i, char, rules.backslash_escapes)
        return unclosed if read is None else (STRING, read[1], read[0])
    if char in '"`':
        read = _quoted(sql, i, char, False)
        return unclosed if read is None else (IDENT, read[1], read[0])
    if char == "[" and rules.bracket_identifiers:
        close = sql.find("]", i + 1)
        # `]]` escapes a bracket inside the name.
        while close >= 0 and sql.startswith("]]", close):
            close = sql.find("]", close + 2)
        if close < 0:
            return unclosed
        return IDENT, close + 1, sql[i + 1 : close].replace("]]", "]")
    if char == "$" and rules.dollar_quotes:
        opening = _DOLLAR_RE.match(sql, i)
        glued = i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in "_$")
        if opening and not glued:
            delimiter = opening.group(0)
            close = sql.find(delimiter, opening.end())
            if close < 0:
                return unclosed
            return STRING, close + len(delimiter), sql[opening.end() : close]
    return None


def _space_or_comment(sql: str, i: int, rules: Lexicon) -> int:
    """
    The offset after the whitespace or the line comment that opens at
    `i`, or `i` when neither does.
    """
    end = len(sql)
    if sql[i] in rules.whitespace:
        stop = i + 1
        while stop < end and sql[stop] in rules.whitespace:
            stop += 1
        return stop
    if sql.startswith("--", i) and (
        not rules.mysql_comments
        or i + 2 >= end
        or sql[i + 2] in rules.whitespace
        or ord(sql[i + 2]) < 0x20
    ):
        return _line_end(sql, i, rules.line_ends)
    if sql[i] == "#" and rules.mysql_comments:
        return _line_end(sql, i, "\n")
    return i


def tokenize(sql: str, dialect: Optional["Dialects"] = None) -> List[Token]:
    """
    Splits one statement into tokens, with comments and whitespace left
    out. The dialect decides the lexical rules that differ between
    engines; with none given, the standard rules apply with `[...]`
    identifiers read as SQLite reads them.

    Text the lexer cannot close, such as a literal with no closing quote,
    ends the list with one ERROR token whose text is the rest of the
    statement.
    """
    return [t for t in lex(sql, dialect) if t.kind not in (SPACE, COMMENT)]


def lex(
    sql: str,
    dialect: Optional["Dialects"] = None,
    rules: Optional[Lexicon] = None,
) -> List[Token]:
    """
    Every token of one statement, whitespace and comments included, so
    the texts of the tokens join to the statement. `rules` replaces the
    dialect's rules when given.
    """
    rules = rules or lexicon(dialect)
    words = _WORD_PATTERNS.get(rules.whitespace)
    if words is None:
        words = _word_pattern(rules.whitespace)
        _WORD_PATTERNS[rules.whitespace] = words
    tokens: List[Token] = []
    i = 0
    end = len(sql)
    # Where the MySQL executable comment the reading is inside opened,
    # and the number of tokens before it.
    executable: Optional[Tuple[int, int]] = None

    def add(kind: str, stop: int, value: Optional[str] = None) -> None:
        text = sql[i:stop]
        tokens.append(Token(kind, text, text if value is None else value, i))

    def error(at: int) -> List[Token]:
        tokens.append(Token(ERROR, sql[at:], sql[at:], at))
        return tokens

    while i < end:
        char = sql[i]
        stop = _space_or_comment(sql, i, rules)
        if stop > i:
            add(SPACE if char in rules.whitespace else COMMENT, stop)
            i = stop
            continue
        if rules.mysql_comments and executable is not None and sql.startswith("*/", i):
            add(COMMENT, i + 2)
            i += 2
            executable = None
            continue
        if sql.startswith("/*", i):
            opener = _EXECUTABLE_RE.match(sql, i) if rules.mysql_comments else None
            if opener is not None:
                executable = (i, len(tokens))
                add(COMMENT, opener.end())
                i = opener.end()
                continue
            stop = _block_comment_end(sql, i, rules.nested_comments)
            if stop < 0:
                return error(i)
            add(COMMENT, stop)
            i = stop
            continue
        literal = _literal(sql, i, rules)
        if literal is not None:
            kind, stop, value = literal
            if kind == ERROR:
                return error(i)
            add(kind, stop, value)
            i = stop
            continue
        word = words.match(sql, i)
        if word:
            text = word.group(0)
            add(WORD, word.end(), text.translate(_ASCII_UPPER))
            i = word.end()
            continue
        number = _NUMBER_RE.match(sql, i)
        if number:
            add(NUMBER, number.end())
            i = number.end()
            continue
        param = _PARAM_RE.match(sql, i)
        if param and not sql.startswith("::", i):
            add(PARAM, param.end())
            i = param.end()
            continue
        if char in _PUNCT:
            add(PUNCT, i + 1)
            i += 1
            continue
        op = _OP_RE.match(sql, i)
        if op:
            add(OP, op.end())
            i = op.end()
            continue
        return error(i)
    if executable is not None:
        # An executable comment that never closes: the server refuses the
        # statement, and nothing reads the text after its opening.
        del tokens[executable[1] :]
        return error(executable[0])
    return tokens
