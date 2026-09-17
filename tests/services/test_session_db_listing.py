"""Focused coverage for the SessionDB listing projection boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_listing


class SessionListingMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_listing_api_without_reverse_import(self) -> None:
        mixin = session_db_listing.SessionListingMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in (
            "usage_totals",
            "list_sessions_rich",
            "session_lifecycle_statuses",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(Path(session_db_listing.__file__).read_text(encoding="utf-8"))
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

    def test_usage_totals_filters_children_and_archived_roots(self) -> None:
        self.db.create_session("active", "cli")
        self.db.append_message("active", "user", "active")
        self.db.update_token_counts(
            "active", input_tokens=3, output_tokens=2, estimated_cost_usd=0.25
        )

        self.db.create_session("archived", "cli")
        self.db.append_message("archived", "user", "archived")
        self.db.update_token_counts(
            "archived",
            input_tokens=7,
            output_tokens=11,
            estimated_cost_usd=0.5,
            actual_cost_usd=0.4,
        )
        self.db.set_session_archived("archived", True)

        self.db.create_session("child", "tool", parent_session_id="active")
        self.db.append_message("child", "user", "child")
        self.db.update_token_counts(
            "child", input_tokens=100, output_tokens=200, estimated_cost_usd=9.0
        )

        self.assertEqual(self.db.usage_totals(), {"tokens": 5, "cost_usd": 0.25})
        totals = self.db.usage_totals(include_archived=True)
        self.assertEqual(totals["tokens"], 23)
        self.assertAlmostEqual(totals["cost_usd"], 0.65)

    def test_rich_listing_projects_compression_tip(self) -> None:
        self.db.create_session("root", "cli", cwd="/repo")
        self.db.append_message("root", "user", "root preview", timestamp=2)
        self.db.end_session("root", "compression")
        self.db.create_session("tip", "cli", parent_session_id="root", cwd="/repo")
        self.db.append_message("tip", "user", "tip preview", timestamp=4)
        self.db.set_session_title("tip", "Visible tip")

        rows = self.db.list_sessions_rich(order_by_last_active=True)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "tip")
        self.assertEqual(rows[0]["_lineage_root_id"], "root")
        self.assertEqual(rows[0]["title"], "Visible tip")
        self.assertEqual(rows[0]["preview"], "tip preview")

    def test_listing_uses_legacy_projection_patch_paths(self) -> None:
        self.db.create_session("visible", "cli", cwd="/repo/project")
        self.db.append_message("visible", "user", "raw preview", timestamp=10)
        last_active = session_db._sql_session_last_active
        cwd_clause = session_db._cwd_prefix_clause
        with (
            patch.object(
                session_db,
                "_shape_preview",
                side_effect=lambda value: f"legacy:{value}",
            ) as shape_preview,
            patch.object(
                session_db,
                "_sql_session_last_active",
                wraps=last_active,
            ) as active_sql,
            patch.object(
                session_db,
                "_cwd_prefix_clause",
                wraps=cwd_clause,
            ) as prefix_clause,
            patch.object(
                session_db.SessionDB,
                "session_unread",
                return_value=True,
            ) as unread,
        ):
            rows = self.db.list_sessions_rich(include_children=True, cwd_prefix="/repo")

        self.assertEqual(rows[0]["preview"], "legacy:raw preview")
        self.assertTrue(rows[0]["unread"])
        shape_preview.assert_called_once_with("raw preview")
        active_sql.assert_called_once_with("s")
        prefix_clause.assert_called_once_with("/repo")
        unread.assert_called_once()

    def test_lifecycle_statuses_use_legacy_classifier_path(self) -> None:
        for session_id in ("empty", "user", "complete", "tool-call", "error"):
            self.db.create_session(session_id, "cli")
        self.db.append_message("user", "user", "unfinished")
        self.db.append_message("complete", "assistant", "done", finish_reason="stop")
        self.db.append_message(
            "tool-call",
            "assistant",
            tool_calls=[{"id": "call-1", "type": "function"}],
            finish_reason="tool_calls",
        )
        self.db.append_message(
            "error", "assistant", "failed", finish_reason="agent_error"
        )

        classifier = session_db.classify_session_status
        with patch.object(
            session_db, "classify_session_status", wraps=classifier
        ) as classify:
            statuses = self.db.session_lifecycle_statuses(
                ["empty", "user", "complete", "tool-call", "error"]
            )

        self.assertEqual(
            statuses,
            {
                "empty": session_db.SESSION_STATUS_EMPTY,
                "user": session_db.SESSION_STATUS_INTERRUPTED,
                "complete": session_db.SESSION_STATUS_COMPLETE,
                "tool-call": session_db.SESSION_STATUS_INTERRUPTED,
                "error": session_db.SESSION_STATUS_ERROR,
            },
        )
        self.assertEqual(classify.call_count, 4)


if __name__ == "__main__":
    unittest.main()
