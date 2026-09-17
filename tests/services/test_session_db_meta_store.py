"""Focused coverage for the SessionDB state_meta mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_meta_store


class SessionMetaStoreMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_meta_api_without_reverse_import(self) -> None:
        mixin = session_db_meta_store.SessionMetaStoreMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "get_meta",
            "set_meta",
            "list_meta_prefix",
            "retag_kanban_worker_sessions",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(
            Path(session_db_meta_store.__file__).read_text(encoding="utf-8")
        )
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("pcbdraft.services.session_db", imports)

    def test_get_set_and_inline_cursor_writes_preserve_transactions(self) -> None:
        self.assertIsNone(self.db.get_meta("missing"))
        self.db.set_meta("plain", "one")
        self.db.set_meta("plain", "two")
        self.assertEqual(self.db.get_meta("plain"), "two")

        def write_inline(conn):
            self.db.set_meta("inline", "committed", cursor=conn.cursor())

        self.db._execute_write(write_inline)
        self.assertEqual(self.db.get_meta("inline"), "committed")

        def fail_after_inline_write(conn):
            self.db.set_meta("rollback", "value", cursor=conn.cursor())
            raise RuntimeError("rollback probe")

        with self.assertRaisesRegex(RuntimeError, "rollback probe"):
            self.db._execute_write(fail_after_inline_write)
        self.assertIsNone(self.db.get_meta("rollback"))

        version = self.db._conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(version, session_db.SCHEMA_VERSION)

    def test_list_meta_prefix_treats_wildcards_and_backslashes_literally(self) -> None:
        values = {
            "loop:one": "1",
            "loop:two": "2",
            "loop%literal:one": "percent",
            "loop_literal:one": "underscore",
            r"path\name:one": "backslash",
        }
        for key, value in values.items():
            self.db.set_meta(key, value)

        self.assertEqual(
            dict(self.db.list_meta_prefix("loop:")),
            {"loop:one": "1", "loop:two": "2"},
        )
        self.assertEqual(
            dict(self.db.list_meta_prefix("loop%literal:")),
            {"loop%literal:one": "percent"},
        )
        self.assertEqual(
            dict(self.db.list_meta_prefix("loop_literal:")),
            {"loop_literal:one": "underscore"},
        )
        self.assertEqual(
            dict(self.db.list_meta_prefix(r"path\name:")),
            {r"path\name:one": "backslash"},
        )
        self.assertEqual(self.db.list_meta_prefix(""), [])

    def test_kanban_retag_is_literal_scoped_and_gated_once(self) -> None:
        root = "/boards/work_spaces%"
        sessions = {
            "exact": ("cli", root),
            "nested": ("cli", f"{root}/task-1"),
            "lookalike": ("cli", "/boards/workXspacesY/task-1"),
            "sibling": ("cli", f"{root}-other/task-1"),
            "already": ("kanban", f"{root}/task-2"),
            "cron": ("cron", f"{root}/task-3"),
        }
        for session_id, (source, cwd) in sessions.items():
            self.db.create_session(session_id, source, cwd=cwd)

        escape_like = session_db._escape_like
        with patch.object(session_db, "_escape_like", wraps=escape_like) as escape:
            self.assertEqual(
                self.db.retag_kanban_worker_sessions(root + "/"),
                2,
            )
            self.assertEqual(self.db.retag_kanban_worker_sessions(root), 0)

        escape.assert_called_once_with(root)
        self.assertEqual(self.db.get_session("exact")["source"], "kanban")
        self.assertEqual(self.db.get_session("nested")["source"], "kanban")
        self.assertEqual(self.db.get_session("already")["source"], "kanban")
        self.assertEqual(self.db.get_session("lookalike")["source"], "cli")
        self.assertEqual(self.db.get_session("sibling")["source"], "cli")
        self.assertEqual(self.db.get_session("cron")["source"], "cron")
        self.assertEqual(self.db.get_meta(f"kanban_worker_source_retagged:{root}"), "1")
        self.assertEqual(self.db.retag_kanban_worker_sessions("///"), 0)


if __name__ == "__main__":
    unittest.main()
