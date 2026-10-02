"""
ALTER TABLE and its actions, and ALTER TYPE.
"""

from __future__ import annotations

from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
)

from sustained.impact.model import Action, ParsedStatement
from sustained.impact.recognizer.cursor import (
    Options,
    Unrecognized,
    frozen,
)
from sustained.impact.recognizer.definitions import (
    Definitions,
)
from sustained.impact.tokens import (
    WORD,
)


class AlterTable(Definitions):
    """ALTER TABLE and its actions, and ALTER TYPE."""

    def alter(self) -> ParsedStatement:
        # MariaDB's ALTER ONLINE TABLE asks for LOCK=NONE, and its ALTER
        # IGNORE TABLE drops the rows a new unique key would refuse.
        online = self.accept("ONLINE")
        ignore = self.accept("IGNORE")
        if self.accept("TABLE"):
            parsed = self.alter_table()
            flags: Options = {}
            if online:
                flags["online"] = True
            if ignore:
                flags["ignore"] = True
            if not flags:
                return parsed
            return parsed._replace(options=frozen({**parsed.options, **flags}))
        if online or ignore:
            raise Unrecognized(f"expected TABLE {self.where()}")
        if self.accept("TYPE"):
            return self.alter_type()
        if self.mssql and self.accept("INDEX"):
            return self.alter_index()
        if self.postgres and self.accept("INDEX"):
            return self.attach_index()
        raise Unrecognized(f"no rule reads ALTER {self.where()}")

    def attach_index(self) -> ParsedStatement:
        """
        PostgreSQL's `ALTER INDEX name ATTACH PARTITION index`, which
        attaches the index of a partition to the index of its partitioned
        table. Every other ALTER INDEX is unknown.
        """
        name = self.name()
        self.expect("ATTACH", "PARTITION")
        partition = self.name()
        self.finish()
        return ParsedStatement(
            "attach_index", options=frozen({"name": name, "partition": partition})
        )

    def alter_index(self) -> ParsedStatement:
        """
        SQL Server's `ALTER INDEX { name | ALL } ON table operation`, where
        the operation is REBUILD, REORGANIZE, DISABLE, SET, RESUME, PAUSE,
        or ABORT, with its PARTITION and WITH options.
        """
        name = None if self.accept("ALL") else self.name()
        self.expect("ON")
        table = self.target()
        operation = self.expect_one_of_words(
            "REBUILD", "REORGANIZE", "DISABLE", "SET", "RESUME", "PAUSE", "ABORT"
        )
        options: Options = {"name": name, "operation": operation.lower()}
        options["partition"] = False
        options["with"] = {}
        if operation == "SET":
            options["with"] = self.with_options()
        while not self.at_end():
            if self.accept("PARTITION"):
                self.accept_op("=")
                self.value()
                options["partition"] = True
            elif self.accept("WITH"):
                options["with"] = self.with_options()
            else:
                raise Unrecognized(f"unread text {self.where()}")
        return ParsedStatement("alter_index", table, options=frozen(options))

    def alter_type(self) -> ParsedStatement:
        self.name()
        if self.accept("ADD", "VALUE"):
            self.accept("IF", "NOT", "EXISTS")
            value = self.value()
            if self.accept_any("BEFORE", "AFTER"):
                self.value()
            return ParsedStatement(
                "alter_type_add_value", options=frozen({"value": value})
            )
        if self.accept("RENAME", "VALUE"):
            self.value()
            self.expect("TO")
            self.value()
            return ParsedStatement("alter_type_rename_value")
        raise Unrecognized(f"no rule reads ALTER TYPE {self.where()}")

    def alter_table(self) -> ParsedStatement:
        options: Options = {}
        options["if_exists"] = self.accept("IF", "EXISTS")
        options["only"] = self.accept("ONLY")
        table = self.target()
        self.accept_op("*")
        wait = self.wait()
        if wait is not None:
            options["wait"] = wait
        actions: List[Action] = []
        while True:
            action = self.alter_action(options, actions)
            if action is not None:
                actions.append(action)
            if not self.accept_punct(","):
                break
        self.finish()
        if not actions:
            raise Unrecognized("the ALTER TABLE has no action")
        return ParsedStatement("alter_table", table, tuple(actions), frozen(options))

    def alter_action(
        self, options: Options, actions: Sequence[Action]
    ) -> Optional[Action]:
        """
        One ALTER TABLE action, or None for a MySQL table option such as
        ALGORITHM, which the statement's options record instead.
        """
        token = self.peek()
        if token is None:
            raise Unrecognized("the ALTER TABLE ends early")
        if token.is_word("ALGORITHM", "LOCK"):
            options[token.value.lower()] = self.mysql_option(token.value)
            return None
        if token.is_word("WITH") and self.peek(1) is not None:
            # SQL Server: WITH CHECK or WITH NOCHECK before the action.
            self.pos += 1
            checked = self.accept_any("CHECK", "NOCHECK")
            if checked is None:
                raise Unrecognized(f"expected CHECK or NOCHECK {self.where()}")
            options["nocheck"] = checked == "NOCHECK"
            return self.alter_action(options, actions)
        handler = _ACTIONS.get(token.value) if token.kind == WORD else None
        if handler is not None:
            self.pos += 1
            return handler(self)
        if self.mssql and actions and actions[-1].kind in ("add_column", "drop_column"):
            # SQL Server lists more columns after one ADD or DROP COLUMN.
            if actions[-1].kind == "drop_column":
                return Action("drop_column", self.name(), frozen({}))
            return self.column_action("add_column")
        raise Unrecognized(f"no rule reads the ALTER TABLE action {self.where()}")

    def action_add(self) -> Action:
        if self.accept("CONSTRAINT"):
            return self.constraint(self.name())
        if self.is_word("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "EXCLUDE", "DEFAULT"):
            return self.constraint(None)
        kind = self.accept_any("INDEX", "KEY", "FULLTEXT", "SPATIAL")
        if kind is not None:
            return self.add_index(kind)
        if self.is_word("COLUMNS"):
            raise Unrecognized("no rule reads ADD COLUMNS")
        self.accept("COLUMN")
        if_not_exists = self.accept("IF", "NOT", "EXISTS")
        action = self.column_action("add_column")
        if if_not_exists:
            action = action._replace(
                options=frozen({**action.options, "if_not_exists": True})
            )
        return action

    def add_index(self, kind: str) -> Action:
        fulltext = kind in ("FULLTEXT", "SPATIAL")
        if fulltext:
            self.accept_any("INDEX", "KEY")
        name = None if self.is_punct("(") or self.is_word("USING") else self.name()
        options: Options = {"name": name, "fulltext": fulltext}
        if self.accept("USING"):
            options["using"] = self.value().lower()
        self.group()
        if self.accept("USING"):
            options["using"] = self.value().lower()
        if kind == "SPATIAL":
            options["spatial"] = True
        return Action("add_index", None, frozen(options))

    def action_drop(self) -> Action:
        if self.accept("CONSTRAINT"):
            if_exists = self.accept("IF", "EXISTS")
            name = self.name()
            cascade = self.accept_any("CASCADE", "RESTRICT") == "CASCADE"
            options: Options = {
                "name": name,
                "if_exists": if_exists,
                "cascade": cascade,
            }
            return Action("drop_constraint", None, frozen(options))
        if self.accept("FOREIGN", "KEY"):
            return Action(
                "drop_constraint",
                None,
                frozen({"name": self.name(), "constraint": "foreign_key"}),
            )
        if self.accept("PRIMARY", "KEY"):
            return Action(
                "drop_constraint", None, frozen({"constraint": "primary_key"})
            )
        if self.accept("CHECK"):
            return Action(
                "drop_constraint",
                None,
                frozen({"name": self.name(), "constraint": "check"}),
            )
        if self.accept_any("INDEX", "KEY"):
            return Action("drop_index", None, frozen({"name": self.name()}))
        if self.is_word("PARTITION", "DEFAULT"):
            raise Unrecognized(f"no rule reads DROP {self.where()}")
        self.accept("COLUMN")
        if_exists = self.accept("IF", "EXISTS")
        column = self.name()
        cascade = self.accept_any("CASCADE", "RESTRICT") == "CASCADE"
        return Action(
            "drop_column",
            column,
            frozen({"if_exists": if_exists, "cascade": cascade}),
        )

    def action_alter(self) -> Action:
        if self.accept("INDEX"):
            return self.index_visibility()
        self.accept("COLUMN")
        if self.is_word("CONSTRAINT", "INDEX", "CHECK"):
            raise Unrecognized(f"no rule reads ALTER {self.where()}")
        column = self.name()
        if self.accept("TYPE") or self.accept("SET", "DATA", "TYPE"):
            return self.alter_column_type(column)
        simple = (
            (("SET", "NOT", "NULL"), "set_not_null"),
            (("DROP", "NOT", "NULL"), "drop_not_null"),
            (("DROP", "DEFAULT"), "drop_default"),
        )
        for words, kind in simple:
            if self.accept(*words):
                return Action(kind, column, frozen({}))
        if self.accept("SET", "DEFAULT"):
            default = self.expression(frozenset())
            return Action("set_default", column, frozen(self.default_options(default)))
        if self.accept("SET", "STATISTICS"):
            self.value()
            return Action("set_statistics", column, frozen({}))
        if self.accept("SET", "STORAGE"):
            self.value()
            return Action("set_storage", column, frozen({}))
        if self.accept("SET", "VISIBLE") or self.accept("SET", "INVISIBLE"):
            return Action("set_storage", column, frozen({}))
        if self.is_word("SET", "DROP", "ADD", "RESET", "OPTIONS"):
            raise Unrecognized(f"no rule reads ALTER COLUMN {self.where()}")
        # SQL Server restates the column: ALTER COLUMN c type [NULL].
        options = self.column_definition()
        if self.accept("WITH"):
            options["with"] = self.with_options()
        return Action("alter_column", column, frozen(options))

    def index_visibility(self) -> Action:
        """
        MySQL's `ALTER INDEX name VISIBLE | INVISIBLE`, and MariaDB's
        `ALTER INDEX name [NOT] IGNORED`: name, visible, which is False
        for INVISIBLE and IGNORED, and word, the keywords spelled, such
        as `NOT IGNORED`.
        """
        name = self.name()
        if self.accept("NOT", "IGNORED"):
            word = "NOT IGNORED"
        elif self.accept("IGNORED"):
            word = "IGNORED"
        else:
            word = self.expect_one_of_words("VISIBLE", "INVISIBLE")
        options = {"name": name, "visible": word in ("VISIBLE", "NOT IGNORED")}
        return Action("index_visibility", None, frozen({**options, "word": word}))

    def alter_column_type(self, column: str) -> Action:
        type_tokens = self.column_type()
        options: Options = {"type": self.text(type_tokens)}
        if self.accept("COLLATE"):
            options["collate"] = self.name()
        options["using"] = None
        if self.accept("USING"):
            options["using"] = self.text(self.expression(frozenset()))
        return Action("alter_column_type", column, frozen(options))

    def action_modify(self) -> Action:
        self.accept("COLUMN")
        return self.column_action("modify_column")

    def action_change(self) -> Action:
        self.accept("COLUMN")
        old = self.name()
        new = self.name()
        options = self.column_definition()
        options["new"] = new
        return Action("change_column", old, frozen(options))

    def action_rename(self) -> Action:
        if self.accept("COLUMN"):
            return self.rename_pair("rename_column")
        if self.accept_any("TO", "AS"):
            return Action("rename_to", None, frozen({"new": self.name()}))
        if self.accept("CONSTRAINT"):
            return self.rename_pair("rename_constraint")
        if self.accept_any("INDEX", "KEY"):
            return self.rename_pair("rename_index")
        return self.rename_pair("rename_column")

    def rename_pair(self, kind: str) -> Action:
        old = self.name()
        self.expect("TO")
        new = self.name()
        column = old if kind == "rename_column" else None
        return Action(kind, column, frozen({"old": old, "new": new}))

    def action_validate(self) -> Action:
        self.expect("CONSTRAINT")
        return Action("validate_constraint", None, frozen({"name": self.name()}))

    def action_check(self) -> Action:
        # SQL Server: [WITH CHECK] CHECK CONSTRAINT name, which enables
        # the constraint, and checks the rows when WITH CHECK leads.
        self.expect("CONSTRAINT")
        return Action("enable_constraint", None, frozen({"name": self.name()}))

    def action_nocheck(self) -> Action:
        self.expect("CONSTRAINT")
        return Action("disable_constraint", None, frozen({"name": self.name()}))

    def action_attach(self) -> Action:
        """`ATTACH PARTITION name`: partition, and default for a DEFAULT bound."""
        self.expect("PARTITION")
        partition = self.name()
        default = self.accept("DEFAULT")
        if not (default or self.accept("FOR", "VALUES")):
            raise Unrecognized(f"expected FOR VALUES or DEFAULT {self.where()}")
        self.item()
        return Action(
            "attach_partition",
            None,
            frozen({"partition": partition, "default": default}),
        )

    def action_detach(self) -> Action:
        self.expect("PARTITION")
        partition = self.name()
        mode = self.accept_any("CONCURRENTLY", "FINALIZE")
        options: Options = {
            "partition": partition,
            "concurrently": mode == "CONCURRENTLY",
            "finalize": mode == "FINALIZE",
        }
        return Action("detach_partition", None, frozen(options))

    def action_set(self) -> Action:
        if self.accept("TABLESPACE"):
            return Action("set_tablespace", None, frozen({"name": self.name()}))
        if self.accept("LOGGED"):
            return Action("set_logged", None, frozen({}))
        if self.accept("UNLOGGED"):
            return Action("set_unlogged", None, frozen({}))
        if self.accept("SCHEMA"):
            return Action("set_schema", None, frozen({"name": self.name()}))
        if self.is_punct("("):
            return Action("set_parameters", None, frozen(self.with_options()))
        raise Unrecognized(f"no rule reads SET {self.where()}")

    def action_owner(self) -> Action:
        self.expect("TO")
        return Action("owner_to", None, frozen({"name": self.name()}))

    def action_replica(self) -> Action:
        """
        PostgreSQL's `REPLICA IDENTITY {DEFAULT | FULL | NOTHING | USING
        INDEX name}`.
        """
        self.expect("IDENTITY")
        if self.accept("USING", "INDEX"):
            return Action("replica_identity", None, frozen({"index": self.name()}))
        mode = self.value()
        return Action("replica_identity", None, frozen({"mode": mode}))

    def action_enable(self) -> Action:
        return self.toggle("enable")

    def action_disable(self) -> Action:
        return self.toggle("disable")

    def toggle(self, verb: str) -> Action:
        self.accept_any("ALWAYS", "REPLICA")
        if self.accept("TRIGGER"):
            return Action(f"{verb}_trigger", None, frozen({"name": self.name()}))
        if self.accept("ROW", "LEVEL", "SECURITY"):
            return Action("row_security", None, frozen({"enabled": verb == "enable"}))
        raise Unrecognized(f"no rule reads {verb.upper()} {self.where()}")

    def action_engine(self) -> Action:
        self.accept_op("=")
        return Action("engine", None, frozen({"engine": self.value()}))

    def action_convert(self) -> Action:
        self.expect("TO")
        if not (self.accept("CHARACTER", "SET") or self.accept("CHARSET")):
            raise Unrecognized(f"expected CHARACTER SET {self.where()}")
        charset = self.value()
        if self.accept("COLLATE"):
            self.value()
        return Action("convert_charset", None, frozen({"charset": charset}))

    def action_force(self) -> Action:
        return Action("force", None, frozen({}))

    def action_rebuild(self) -> Action:
        """SQL Server's `REBUILD [PARTITION = n] [WITH (...)]`."""
        if not self.mssql:
            raise Unrecognized(f"no rule reads REBUILD {self.where()}")
        options: Options = {"partition": False, "with": {}}
        if self.accept("PARTITION"):
            self.accept_op("=")
            self.value()
            options["partition"] = True
        if self.accept("WITH"):
            options["with"] = self.with_options()
        return Action("rebuild", None, frozen(options))

    def action_switch(self) -> Action:
        """
        SQL Server's `SWITCH [PARTITION n] TO target [PARTITION n]
        [WITH (...)]`: `target` is the table the rows move to.
        """
        if not self.mssql:
            raise Unrecognized(f"no rule reads SWITCH {self.where()}")
        if self.accept("PARTITION"):
            self.value()
        self.expect("TO")
        target = self.name()
        if self.accept("PARTITION"):
            self.value()
        options: Options = {"target": target, "with": {}}
        if self.accept("WITH"):
            options["with"] = self.with_options()
        return Action("switch", None, frozen(options))

    def action_table_option(self) -> Action:
        """
        A MySQL table option, such as COMMENT = 'text' or AUTO_INCREMENT
        = 100: name, the option in lower case, and value.
        """
        name = self.tokens[self.pos - 1].value.lower()
        self.accept_op("=")
        return Action(
            "table_option", None, frozen({"name": name, "value": self.value()})
        )


_ActionHandler = Callable[[AlterTable], Action]
_ACTIONS: Dict[str, _ActionHandler] = {
    "ADD": AlterTable.action_add,
    "DROP": AlterTable.action_drop,
    "ALTER": AlterTable.action_alter,
    "MODIFY": AlterTable.action_modify,
    "CHANGE": AlterTable.action_change,
    "RENAME": AlterTable.action_rename,
    "VALIDATE": AlterTable.action_validate,
    "CHECK": AlterTable.action_check,
    "NOCHECK": AlterTable.action_nocheck,
    "ATTACH": AlterTable.action_attach,
    "DETACH": AlterTable.action_detach,
    "SET": AlterTable.action_set,
    "OWNER": AlterTable.action_owner,
    "REPLICA": AlterTable.action_replica,
    "ENABLE": AlterTable.action_enable,
    "DISABLE": AlterTable.action_disable,
    "ENGINE": AlterTable.action_engine,
    "CONVERT": AlterTable.action_convert,
    "COMMENT": AlterTable.action_table_option,
    "AUTO_INCREMENT": AlterTable.action_table_option,
    "ROW_FORMAT": AlterTable.action_table_option,
    "KEY_BLOCK_SIZE": AlterTable.action_table_option,
    "FORCE": AlterTable.action_force,
    "REBUILD": AlterTable.action_rebuild,
    "SWITCH": AlterTable.action_switch,
}
