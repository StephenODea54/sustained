"""
The recognizer's kind vocabularies agree with what the recognizer emits.

The test reads the recognizer's source and collects every kind it passes
to `ParsedStatement(...)` or `Action(...)`. A kind given as a parameter
is read from the calls of that function, and a kind given as a loop
variable is read from the tuple the loop walks. A kind given as a local
variable is read from the strings assigned to it.
"""

import ast
import pathlib
import re
import unittest
from typing import Dict, List, Set, Tuple

import sustained.impact.recognizer as recognizer
from sustained.impact.analyzer import parsed_from_intent
from sustained.impact.model import UNKNOWN_KIND, Intent
from sustained.impact.recognizer import (
    ACTION_KINDS,
    COLUMN_DEFAULTS,
    CREATE_TABLE_DEFAULTS,
    STATEMENT_KINDS,
)

_SOURCES = sorted(pathlib.Path(recognizer.__file__).parent.glob("*.py"))
_BUILDERS = {"ParsedStatement": "statement", "Action": "action"}
_NAMED = {"UNKNOWN_KIND": UNKNOWN_KIND}


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _strings(node: ast.expr) -> List[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in _NAMED:
        return [_NAMED[node.id]]
    if isinstance(node, ast.IfExp):
        return _strings(node.body) + _strings(node.orelse)
    return []


class _Collector:
    def __init__(self) -> None:
        self.trees = [ast.parse(path.read_text()) for path in _SOURCES]
        self.calls: Dict[str, List[ast.Call]] = {}
        for tree in self.trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    self.calls.setdefault(_name(node.func), []).append(node)
        self.kinds: Dict[str, Set[str]] = {"statement": set(), "action": set()}
        self.unresolved: List[str] = []

    def collect(self) -> None:
        for tree in self.trees:
            for function in ast.walk(tree):
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._function(function)

    def _function(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parameters = [a.arg for a in function.args.args if a.arg != "self"]
        loops: Dict[str, Tuple[ast.expr, int]] = {}
        assigned: Dict[str, List[str]] = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                assigned.setdefault(node.targets[0].id, []).extend(_strings(node.value))
            if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple):
                for at, item in enumerate(node.target.elts):
                    if isinstance(item, ast.Name):
                        loops[item.id] = (node.iter, at)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or _name(node.func) not in _BUILDERS:
                continue
            found = self.kinds[_BUILDERS[_name(node.func)]]
            first = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg == "kind":
                    first = keyword.value
            assert first is not None
            if _strings(first):
                found.update(_strings(first))
            elif isinstance(first, ast.Name) and assigned.get(first.id):
                found.update(assigned[first.id])
            elif isinstance(first, ast.Name) and first.id in parameters:
                found.update(self._passed(function.name, parameters.index(first.id)))
            elif isinstance(first, ast.Name) and first.id in loops:
                found.update(self._looped(function, *loops[first.id]))
            else:
                self.unresolved.append(f"{function.name}: {ast.unparse(first)}")

    def _passed(self, function: str, at: int) -> Set[str]:
        passed: Set[str] = set()
        for call in self.calls.get(function, []):
            if at < len(call.args):
                passed.update(_strings(call.args[at]))
        return passed

    def _looped(self, function: ast.AST, iterable: ast.expr, at: int) -> Set[str]:
        values: List[ast.expr] = []
        if isinstance(iterable, ast.Name):
            for node in ast.walk(function):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == iterable.id
                    for t in node.targets
                ):
                    values.append(node.value)
        else:
            values.append(iterable)
        looped: Set[str] = set()
        for value in values:
            if isinstance(value, ast.Tuple):
                for row in value.elts:
                    if isinstance(row, ast.Tuple) and at < len(row.elts):
                        looped.update(_strings(row.elts[at]))
        return looped


def _documented() -> Set[str]:
    """The kinds the package docstring lists, before each bullet's colon."""
    doc = recognizer.__doc__ or ""
    start = doc.index("The statement kinds")
    end = doc.index("An ALTER TABLE action")
    kinds: Set[str] = set()
    for bullet in re.split(r"\n- ", doc[start:end])[1:]:
        kinds.update(re.findall(r"`(\w+)`", bullet.split(":")[0]))
    return kinds


class KindVocabularyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.collector = _Collector()
        cls.collector.collect()

    def test_every_kind_resolves_to_a_literal(self) -> None:
        self.assertEqual(self.collector.unresolved, [])

    def test_statement_kinds_match_what_the_recognizer_emits(self) -> None:
        self.assertEqual(self.collector.kinds["statement"], set(STATEMENT_KINDS))

    def test_action_kinds_match_what_the_recognizer_emits(self) -> None:
        self.assertEqual(self.collector.kinds["action"], set(ACTION_KINDS))

    def test_the_docstring_lists_every_statement_kind(self) -> None:
        self.assertEqual(_documented(), set(STATEMENT_KINDS) - {UNKNOWN_KIND})


class IntentDefaultsTestCase(unittest.TestCase):
    """An intent-built statement sets the options the recognizer sets."""

    def test_add_column_sets_every_column_option(self) -> None:
        parsed = recognizer.recognize("ALTER TABLE t ADD COLUMN c int")
        intent = Intent("add_column", "t", "c", {"has_default": False})
        built = parsed_from_intent(intent)
        assert built is not None
        self.assertEqual(set(built.actions[0].options), set(parsed.actions[0].options))
        self.assertEqual(set(COLUMN_DEFAULTS), set(parsed.actions[0].options))

    def test_create_table_sets_every_table_option(self) -> None:
        parsed = recognizer.recognize("CREATE TABLE t (a int)")
        built = parsed_from_intent(Intent("create_table", "t"))
        assert built is not None
        self.assertEqual(set(built.options), set(parsed.options))
        self.assertEqual(set(CREATE_TABLE_DEFAULTS), set(parsed.options))


if __name__ == "__main__":
    unittest.main()
