"""
The intent a generator gives a statement: whether the statement's text
reads as that intent, and what the statement does when the recognizer
cannot read its text.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from sustained.impact.model import Action, Finding, Intent, ParsedStatement, Severity
from sustained.impact.recognizer import COLUMN_DEFAULTS, CREATE_TABLE_DEFAULTS

# The statement kinds, and ALTER TABLE actions, each intent kind may
# read as. A None action means the statement kind alone must match.
INTENT_FORMS: Mapping[str, FrozenSet[Tuple[str, Optional[str]]]] = {
    "create_table": frozenset({("create_table", None)}),
    "drop_table": frozenset({("drop_table", None)}),
    "rename_table": frozenset({("alter_table", "rename_to"), ("rename_table", None)}),
    "add_column": frozenset({("alter_table", "add_column")}),
    "drop_column": frozenset({("alter_table", "drop_column")}),
    "rename_column": frozenset({("alter_table", "rename_column")}),
    "alter_column_type": frozenset(
        {
            ("alter_table", "alter_column_type"),
            ("alter_table", "modify_column"),
            ("alter_table", "alter_column"),
        }
    ),
    "set_not_null": frozenset(
        {
            ("alter_table", "set_not_null"),
            ("alter_table", "modify_column"),
            ("alter_table", "alter_column"),
        }
    ),
    "drop_not_null": frozenset(
        {
            ("alter_table", "drop_not_null"),
            ("alter_table", "modify_column"),
            ("alter_table", "alter_column"),
        }
    ),
    "set_column_default": frozenset({("alter_table", "set_default")}),
    "drop_column_default": frozenset({("alter_table", "drop_default")}),
    "set_column_comment": frozenset(
        {("comment_on", None), ("alter_table", "modify_column")}
    ),
    # DuckDB backfills through the type change's USING clause.
    "backfill": frozenset({("update", None), ("alter_table", "alter_column_type")}),
    "add_foreign_key": frozenset({("alter_table", "add_constraint")}),
    "drop_foreign_key": frozenset({("alter_table", "drop_constraint")}),
    "add_check": frozenset({("alter_table", "add_constraint")}),
    "add_unique": frozenset(
        {("alter_table", "add_constraint"), ("create_index", None)}
    ),
    "drop_constraint": frozenset({("alter_table", "drop_constraint")}),
    "validate_constraint": frozenset({("alter_table", "validate_constraint")}),
    "create_index": frozenset({("create_index", None), ("alter_table", "add_index")}),
    "drop_index": frozenset({("drop_index", None), ("alter_table", "drop_index")}),
    "attach_index": frozenset({("attach_index", None)}),
    "create_enum_type": frozenset({("create_type", None)}),
    "drop_enum_type": frozenset({("drop_type", None)}),
    "add_enum_value": frozenset({("alter_type_add_value", None)}),
    "session_setting": frozenset({("set", None)}),
}


def intent_agrees(intent: Intent, parsed: ParsedStatement) -> bool:
    """Whether a statement's text reads as the intent its generator gave."""
    forms = INTENT_FORMS.get(intent.kind)
    if forms is None:
        return True
    actions = {a.kind for a in parsed.actions}
    kind_matches = any(
        parsed.kind == kind and (action is None or action in actions)
        for kind, action in forms
    )
    return kind_matches and _same_table(intent.table, parsed.table)


def parsed_from_intent(
    intent: Intent,
) -> Optional[Tuple[ParsedStatement, Tuple[str, ...]]]:
    """
    What a generated statement the recognizer cannot read does, from its
    intent alone: the one statement kind, and ALTER TABLE action, the
    intent reads as, with the options its details give, and the facts
    the details do not give. Each of those facts takes its worst case.
    None for an intent that reads as more than one statement kind.
    """
    forms = INTENT_FORMS.get(intent.kind)
    if forms is None or len(forms) != 1 or intent.table is None:
        return None
    ((kind, action),) = forms
    options, action_options, unread = _intent_reading(intent)
    actions = (
        ()
        if action is None
        else (Action(action, intent.column, MappingProxyType(action_options)),)
    )
    parsed = ParsedStatement(kind, intent.table, actions, MappingProxyType(options))
    return parsed, unread


def _intent_reading(
    intent: Intent,
) -> Tuple[Dict[str, object], Dict[str, object], Tuple[str, ...]]:
    """
    The statement options, the action options, and the unread facts of
    an intent read without its statement text.
    """
    kind = intent.kind
    statement: Dict[str, object] = {}
    action: Dict[str, object] = {}
    unread: List[str] = []
    name = intent.get("name")
    if kind == "drop_table":
        statement["tables"] = (intent.table,)
    elif kind == "create_table":
        statement.update(CREATE_TABLE_DEFAULTS)
        unread.append("the tables its foreign keys reference")
    elif kind == "add_column":
        nullable = intent.get("nullable")
        action.update(COLUMN_DEFAULTS)
        action["not_null"] = nullable is not True
        if nullable is None:
            unread.append("whether the column is nullable")
        if intent.get("has_default") is False:
            action["default"] = None
        else:
            # The default's expression is not in the intent, so it
            # counts as one that gives each row a new value.
            action.update(_UNREAD_DEFAULT)
            unread.append("the default")
        unread.append("the column's type and constraints")
    elif kind == "rename_column":
        action.update(old=intent.column, new=intent.get("new"))
        if intent.get("new") is None:
            unread.append("the new name")
    elif kind in ("add_foreign_key", "add_check"):
        action.update(name=name, not_valid=False, using_index=None)
        if kind == "add_foreign_key":
            action.update(constraint="foreign_key", references=intent.get("references"))
            unread.append("whether the key is NOT VALID")
        else:
            action["constraint"] = "check"
            unread.append("the check's expression, and whether it is NOT VALID")
    elif kind in ("drop_foreign_key", "drop_constraint", "validate_constraint"):
        action["name"] = name
        if kind != "validate_constraint":
            action.update(if_exists=False, cascade=False)
    return statement, action, tuple(unread)


# A default whose expression is not read: an expression no rule knows,
# which counts as volatile. The parentheses make it an expression
# default on MySQL and MariaDB, the worst case there.
_UNREAD_DEFAULT: Mapping[str, object] = MappingProxyType(
    {
        "default": "(?)",
        "default_volatility": "volatile",
        "default_function": None,
        "default_certain": False,
    }
)


def from_intent_finding(intent: Intent, unread: Sequence[str]) -> Finding:
    message = (
        f"the statement text is not read, so the analysis follows the intent it "
        f"was generated with: {intent.kind} on {intent.table}"
    )
    if intent.kind == "create_table":
        # No worst case names the tables a key references.
        message += f"; the intent does not give {_listed(unread)}, so no lock on "
        message += "them is reported"
    elif unread:
        message += f"; the intent does not give {_listed(unread)}, so the analysis "
        message += "assumes the worst case" + (" for each" if len(unread) > 1 else "")
    return Finding("impact.from_intent", Severity.INFO, message)


def _listed(items: Sequence[str]) -> str:
    if len(items) < 3:
        return " or ".join(items)
    return ", ".join(items[:-1]) + f", or {items[-1]}"


def _same_table(intended: Optional[str], parsed: Optional[str]) -> bool:
    if intended is None or parsed is None:
        return True
    a, b = intended.lower(), parsed.lower()
    if "." in a and "." not in b or "." in b and "." not in a:
        a, b = a.rsplit(".", 1)[-1], b.rsplit(".", 1)[-1]
    return a == b


def mismatch_finding(intent: Intent, parsed: ParsedStatement) -> Finding:
    actions = ", ".join(a.kind for a in parsed.actions)
    reads = parsed.kind + (f" ({actions})" if actions else "")
    return Finding(
        "impact.intent_mismatch",
        Severity.WARN,
        f"the statement was generated as {intent.kind} on {intent.table}, but its "
        f"text reads as {reads} on {parsed.table}; the analysis follows the text",
    )
