"""
Static reading of migration SQL, for previews that touch no database.

`MigrationStatement` is a statement with the migration it came from,
which is what a guard reads. `destructive_statements()` finds the statements that remove data or
drop a constraint, so a preview can label them. `summarize()` reduces one migration to the count
and the labels the `plan` command prints.

The scan is textual: it reads the words in a statement and parses no
SQL. It knows string literals, comments, and Postgres dollar-quoted
bodies only well enough to keep them out of the scan, so a drop written
inside a literal, a comment, or a `$$` function body is not labelled. It
reads comments as each engine does, so a comment that one engine ends
early, or a MySQL `/*! ... */` body that MySQL runs, cannot hide a drop.
The label informs the operator, and the rehearsal gate in `migrate`
reads the same list.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from sustained.impact.model import INTENT_KINDS, Intent, StatementImpact
from sustained.impact.tokens import (
    COMMENT,
    IDENT,
    SPACE,
    STRING,
    WORD,
    Lexicon,
    lex,
    scan_readings,
)
from sustained.migrations import Migration, run_statements

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.dialects import Dialects

# The scan reads a statement with the lexer the impact recognizer uses,
# so the two read literals and comments alike. `scan_readings()` names
# the readings a scan takes: the dialect's, or with no dialect one for
# each engine, and each one also with the backslash rule reversed when
# the statement has a backslash.
_WHITESPACE_RE = re.compile(r"\s+")
# DROP DATABASE always takes the data with it. DROP SCHEMA needs CASCADE
# to do so, since a plain DROP SCHEMA refuses a schema that holds
# anything. A DELETE at the start of a statement, after a CTE, or in a
# MERGE branch removes rows without the FROM keyword on MSSQL and MySQL
# (`DELETE t WHERE ...`, `DELETE t1 FROM t1 JOIN ...`).
_DESTRUCTIVE_RE = re.compile(
    r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|\bDROP\s+TYPE\b|\bTRUNCATE\b"
    r"|\bDROP\s+CONSTRAINT\b|\bDROP\s+CHECK\b|\bDROP\s+FOREIGN\s+KEY\b"
    r"|\bDELETE\s+FROM\b|^DELETE\b|\)\s*DELETE\b|\bTHEN\s+DELETE\b"
    r"|\bDROP\s+(?:MATERIALIZED\s+)?VIEW\b"
    r"|\bDROP\s+DATABASE\b|\bDROP\s+SCHEMA\b[^;]*\bCASCADE\b",
    re.IGNORECASE,
)
# MySQL lets a column drop omit the COLUMN keyword. This matches
# `ALTER TABLE <name> DROP <identifier>` while it skips drops of other
# schema objects, such as a constraint, an index, or a key. The table
# name may follow IF EXISTS or ONLY, and the drop may be any action in a
# comma-separated list (`ALTER TABLE t ADD x int, DROP y`).
_ALTER_DROP_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?\S+\s+(?:[^;]*?,\s*)?"
    r"DROP\s+"
    r"(?!CONSTRAINT\b|INDEX\b|KEY\b|FOREIGN\b|PRIMARY\b|CHECK\b|PARTITION\b)"
    r"[A-Za-z_`\"\[]",
    re.IGNORECASE,
)


class MigrationStatement(str):
    """
    One statement with the migration it came from.

    It is a `str`, so a guard reads it as the statement text and a guard
    written against `Sequence[str]` needs no change. `migration_id` names
    the migration the statement belongs to, and `transactional` says
    whether that migration runs inside a transaction. A rule about a
    setting that dies at a commit, such as `SET LOCAL`, reads the two to
    tell one migration from the next.

    `migration_id` is None for a statement that reached a guard with no
    migration around it. Statements that carry the same id in a row
    belong to one migration, so None statements next to each other read
    as one group.

    `destructive` marks a statement that removes data although its text
    names no drop, such as a column type change that narrows the type.
    The diff against the models sets it, because only the diff knows the
    type the column has today. `destructive_statements()` labels such a
    statement whatever its text says. When `destructive` is not given, a
    statement wrapped again keeps the mark of the statement it wraps.

    `intent` is what the statement is meant to do, as the code that
    generated it knows it: the diff and `DdlStep` rendering set it, and
    hand-written SQL has none. The impact analysis reads it before the
    text. Like `destructive`, a statement wrapped again keeps the intent
    of the statement it wraps when none is given.

    `impact` is the statement's `StatementImpact`, which the migrator
    attaches before the guards run on a dialect the impact analysis
    covers. It is None everywhere else. A statement wrapped again keeps
    it when none is given, and keeps its migration id and its
    `transactional` flag the same way. None of `destructive`, `intent`, and `impact`
    takes part in equality or in a migration's checksum, which reads the
    statement text alone.
    """

    migration_id: Optional[str]
    transactional: bool
    destructive: bool
    intent: Optional[Intent]
    impact: Optional[StatementImpact]

    def __new__(
        cls,
        statement: str,
        migration_id: Optional[str] = None,
        transactional: Optional[bool] = None,
        destructive: Optional[bool] = None,
        intent: Optional[Intent] = None,
        impact: Optional[StatementImpact] = None,
    ) -> "MigrationStatement":
        instance = super().__new__(cls, statement)
        wrapped = statement if isinstance(statement, MigrationStatement) else None
        if migration_id is None and wrapped is not None:
            migration_id = wrapped.migration_id
        instance.migration_id = migration_id
        if transactional is None:
            transactional = wrapped.transactional if wrapped is not None else True
        instance.transactional = transactional
        if destructive is None:
            destructive = (
                isinstance(statement, MigrationStatement) and statement.destructive
            )
        instance.destructive = destructive
        if intent is None and isinstance(statement, MigrationStatement):
            intent = statement.intent
        instance.intent = intent
        if impact is None and isinstance(statement, MigrationStatement):
            impact = statement.impact
        instance.impact = impact
        return instance


def with_intent(
    statement: str,
    kind: str,
    table: Optional[str],
    column: Optional[str] = None,
    **details: object,
) -> MigrationStatement:
    """
    The statement with an `Intent` attached, keeping whatever else a
    MigrationStatement it wraps carries, such as the destructive mark.
    `kind` must be one of `sustained.impact.model.INTENT_KINDS`.
    """
    if kind not in INTENT_KINDS:
        raise ValueError(f"Unknown intent kind: {kind!r}.")
    intent = Intent(kind, table, column, MappingProxyType(details))
    return MigrationStatement(statement, intent=intent)


def statement_scope(statement: str) -> Tuple[Optional[str], bool]:
    """
    The migration a statement came from and whether that migration is
    transactional. A plain `str` carries neither, and reads as an
    unnamed statement inside a transaction.
    """
    if isinstance(statement, MigrationStatement):
        return statement.migration_id, statement.transactional
    return None, True


def _rewrite_tokens(statement: str, blank_literals: bool, rules: Lexicon) -> str:
    """
    Replaces each comment in a statement with a space, as the server
    reads it. When `blank_literals` is true, it also empties every string
    literal, quoted identifier, and dollar-quoted body, so words inside
    quotes cannot match a scan, and replaces each word with a character
    past ASCII with `_`: no keyword has one, and the scan's patterns
    would otherwise split the word where the server reads one name. A
    quote that never closes ends the reading, and the text from it on
    stays and reads as plain SQL.

    A `DO` block is the exception: Postgres runs its body as soon as the
    statement runs, so the body is scanned as SQL of its own. A function
    body runs only when something calls the function, and stays blank.
    """
    tokens = lex(statement, rules=rules)
    executes_body = False
    if blank_literals:
        leading = [t for t in tokens if t.kind not in (SPACE, COMMENT)]
        executes_body = bool(leading) and leading[0].is_word("DO")
    out: List[str] = []
    for token in tokens:
        text = token.text
        if token.kind in (SPACE, COMMENT):
            out.append(" ")
        elif not blank_literals:
            out.append(text)
        elif token.kind == STRING and text.startswith("$"):
            if executes_body:
                out.append(f" {_rewrite_tokens(token.value, True, rules)} ")
            else:
                out.append("$$")
        elif token.kind == STRING:
            out.append("''")
        elif token.kind == IDENT:
            out.append(text[0] + text[-1])
        elif token.kind == WORD and not text.isascii():
            out.append("_")
        else:
            out.append(text)
    return "".join(out)


def normalize_statement(statement: str) -> str:
    """
    One statement on one line: comments removed, whitespace collapsed,
    ends trimmed. This is the form a statement prints in, so string
    literals keep their text. A '--' inside a literal starts no comment.
    """
    rules = scan_readings(None, False)[0]
    return _WHITESPACE_RE.sub(" ", _rewrite_tokens(statement, False, rules)).strip()


def scannable_statement(statement: str, dialect: Optional["Dialects"] = None) -> str:
    """
    The form a textual scan reads: `normalize_statement()` with every
    string literal and quoted identifier emptied. A commented-out drop
    and a drop written inside quotes both match nothing. Print
    `normalize_statement()` instead; this form loses text.

    The statement is read as the dialect reads it, or with no dialect
    with standard literals and comments and Postgres dollar quotes.
    """
    rules = scan_readings(dialect, False)[0]
    return _WHITESPACE_RE.sub(" ", _rewrite_tokens(statement, True, rules)).strip()


def scannable_forms(
    statement: str, dialect: Optional["Dialects"] = None
) -> Tuple[str, ...]:
    """
    Every form a scan for a drop reads, one for each reading
    `scan_readings()` gives, with repeats left out. A drop found in any
    form counts, so a literal or a comment that one reading ends early
    cannot hide a drop from the scan.

    With a dialect, the form is the dialect's reading, and for a
    statement with a backslash also the reading with the backslash rule
    reversed, as MySQL with NO_BACKSLASH_ESCAPES reads it. With no
    dialect, the text could be for any engine, so the scan also takes
    each engine's comment rules: MySQL's `#` and `/*! ... */`, and the
    nested block comments of Postgres and SQL Server.
    """
    forms: List[str] = []
    for rules in scan_readings(dialect, "\\" in statement):
        form = _WHITESPACE_RE.sub(" ", _rewrite_tokens(statement, True, rules)).strip()
        if form not in forms:
            forms.append(form)
    return tuple(forms)


def _removes_data(statement: str, dialect: Optional["Dialects"] = None) -> bool:
    """
    Whether one statement removes something the schema cannot give back,
    by the rules `destructive_statements()` gives.
    """
    if isinstance(statement, MigrationStatement):
        if statement.destructive:
            return True
        intent = statement.intent
        if intent is not None and intent.kind == "drop_constraint":
            if intent.get("transient"):
                return False
    return any(
        _DESTRUCTIVE_RE.search(form) or _ALTER_DROP_RE.search(form)
        for form in scannable_forms(statement, dialect)
    )


def destructive_statements(
    statements: Union[str, Sequence[str]], dialect: Optional["Dialects"] = None
) -> List[str]:
    """
    Returns the statements that remove something the schema cannot give
    back: DROP TABLE, DROP COLUMN, DROP TYPE, DROP VIEW, DROP
    MATERIALIZED VIEW, DROP DATABASE, DROP SCHEMA ... CASCADE, TRUNCATE,
    DELETE (with or without FROM, and in a MERGE branch), a MySQL-style
    column drop that omits the COLUMN keyword (`ALTER TABLE t DROP col`),
    and constraint drops (DROP CONSTRAINT, DROP CHECK, DROP FOREIGN KEY).
    A dropped constraint removes no rows, but re-adding it needs the data
    to still satisfy it. A plain DROP SCHEMA refuses a schema that holds
    anything, so only the CASCADE form is labelled. Drops of indexes and
    keys are not labelled. A generated drop of a constraint the same
    migration added for its own use, such as the check the online route
    to SET NOT NULL adds and drops, is not labelled either: its intent is
    `drop_constraint` with `transient=True`.

    A MigrationStatement marked `destructive`, such as a narrowing type
    change the diff generated, is labelled whatever its text says.

    Comments are removed and whitespace is collapsed, so each statement
    comes back on one line and a commented-out drop is not labelled. Both
    `--` and `/* */` comments are handled. The scan reads no text inside
    quotes or inside a dollar-quoted body, so a statement that names a
    drop in a string literal or a `$$` function body is not labelled.

    The scan reads comments as the dialect does. With no dialect, it
    reads each statement as every engine would, and labels a drop that
    any of those readings finds: a MySQL `#` comment, a `/*! ... */`
    body, and a nested `/* */` comment each read differently elsewhere.
    """
    if isinstance(statements, str):
        statements = [statements]
    return [normalize_statement(s) for s in statements if _removes_data(s, dialect)]


class PendingSummary(NamedTuple):
    """
    What a preview says about one migration that has not run yet.

    `sql` holds the statements the up step would run, and is None for a
    callable step, which has no SQL to render or scan. Each one is a
    MigrationStatement, so a guard reading them can tell which migration
    they came from.
    """

    id: str
    state: str
    repeatable: bool
    sql: Optional[List[str]]
    destructive: List[str]


def summarize(
    migration: Migration, state: str, compiler: Optional["Compiler"] = None
) -> PendingSummary:
    """
    Reduces one migration to its id, its state ('pending' or, for a
    repeatable whose contents changed, 'changed'), the statements its up
    step would run, and the ones that remove data. Ddl steps render for
    the given compiler's dialect, or ANSI when none is given.
    """
    if callable(migration.up):
        return PendingSummary(migration.id, state, migration.repeatable, None, [])
    statements: List[str] = [*run_statements([migration], compiler)]
    return PendingSummary(
        migration.id,
        state,
        migration.repeatable,
        statements,
        destructive_statements(statements),
    )
