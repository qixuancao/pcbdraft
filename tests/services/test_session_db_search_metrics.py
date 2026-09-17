"""Focused coverage for the SessionDB search/metrics mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_search_metrics


class SessionSearchMetricsMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_search_metrics_without_reverse_import(self) -> None:
        mixin = session_db_search_metrics.SessionSearchMetricsMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "search_sessions",
            "session_count",
            "session_count_ge",
            "session_count_by_source",
            "message_count",
            "has_platform_message_id",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(
            Path(session_db_search_metrics.__file__).read_text(encoding="utf-8")
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

    def test_search_sessions_scopes_workspace_and_uses_legacy_helpers(self) -> None:
        self.db.create_session("older", "cli", cwd="/repo/subdir")
        self.db.append_message("older", "user", "older", timestamp=10)
        self.db.create_session("newer", "cli", git_repo_root="/repo")
        self.db.append_message("newer", "user", "newer", timestamp=20)
        self.db.create_session("other-source", "cron", git_repo_root="/repo")
        self.db.append_message("other-source", "user", "other", timestamp=30)
        self.db.create_session("other-workspace", "cli", cwd="/elsewhere")
        self.db.append_message("other-workspace", "user", "elsewhere", timestamp=40)

        last_active = session_db._sql_session_last_active
        workspace_clause = session_db._workspace_key_clause
        with (
            patch.object(
                session_db, "_sql_session_last_active", wraps=last_active
            ) as active_sql,
            patch.object(
                session_db, "_workspace_key_clause", wraps=workspace_clause
            ) as workspace_sql,
        ):
            rows = self.db.search_sessions(source="cli", workspace_key="/repo")

        self.assertEqual([row["id"] for row in rows], ["newer", "older"])
        self.assertGreater(rows[0]["last_active"], rows[1]["last_active"])
        active_sql.assert_called_once_with("s")
        workspace_sql.assert_called_once_with("/repo")

    def test_session_counts_apply_visibility_filters_and_legacy_hooks(self) -> None:
        self.db.create_session("active", "cli", cwd="/repo")
        self.db.append_message("active", "user", "active")
        self.db.create_session("archived", "cron", cwd="/other")
        self.db.append_message("archived", "user", "archived")
        self.db.set_session_archived("archived", True)
        self.db.create_session("child", "tool", parent_session_id="active")
        self.db.append_message("child", "user", "child")

        self.assertEqual(self.db.session_count(), 2)
        self.assertEqual(self.db.session_count(exclude_children=True), 1)
        self.assertEqual(
            self.db.session_count(include_archived=True, exclude_children=True), 2
        )
        self.assertEqual(self.db.session_count_by_source(), {"cli": 1, "tool": 1})
        self.assertEqual(
            self.db.session_count_by_source(exclude_children=True), {"cli": 1}
        )
        self.assertEqual(
            self.db.session_count_by_source(
                include_archived=True, exclude_children=True
            ),
            {"cli": 1, "cron": 1},
        )
        self.assertTrue(self.db.session_count_ge(3))
        self.assertFalse(self.db.session_count_ge(4))

        cwd_clause = session_db._cwd_prefix_clause
        with patch.object(
            session_db, "_cwd_prefix_clause", wraps=cwd_clause
        ) as cwd_sql:
            self.assertEqual(self.db.session_count(cwd_prefix="/repo"), 2)
        cwd_sql.assert_called_once_with("/repo")

        with patch.object(session_db, "_LISTABLE_CHILD_SQL", "1 = 1"):
            self.assertEqual(self.db.session_count(exclude_children=True), 2)

    def test_message_count_and_platform_message_lookup(self) -> None:
        self.db.create_session("one", "cli")
        self.db.create_session("two", "cli")
        self.db.append_message("one", "user", "first", platform_message_id="platform-1")
        self.db.append_message("one", "assistant", "reply")
        self.db.append_message(
            "two", "user", "second", platform_message_id="platform-2"
        )

        self.assertEqual(self.db.message_count(), 3)
        self.assertEqual(self.db.message_count("one"), 2)
        self.assertEqual(self.db.message_count("missing"), 0)
        self.assertTrue(self.db.has_platform_message_id("one", "platform-1"))
        self.assertFalse(self.db.has_platform_message_id("two", "platform-1"))
        self.assertFalse(self.db.has_platform_message_id("one", "missing"))


if __name__ == "__main__":
    unittest.main()
