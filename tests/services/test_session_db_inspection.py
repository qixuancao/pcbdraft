"""Focused coverage for the SessionDB read-only inspection boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_inspection


class SessionInspectionMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_inspection_api_without_reverse_import(self) -> None:
        method_names = (
            "_session_row_dict",
            "get_session",
            "get_dominant_session_model_route",
            "resolve_session_id",
            "has_archived_messages",
        )
        mixin = session_db_inspection.SessionInspectionMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(
            Path(session_db_inspection.__file__).read_text(encoding="utf-8")
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

    def test_real_session_lookup_and_prefix_resolution(self) -> None:
        self.db.create_session(
            "exact-session",
            "cli",
            model="test-model",
            system_prompt="resolved prompt",
        )
        self.db.create_session("prefix-one", "cli")
        self.db.create_session("wild%target", "cli")
        self.db.create_session("wildXYZtarget", "cli")

        session = self.db.get_session("exact-session")
        self.assertEqual(session["model"], "test-model")
        self.assertEqual(session["system_prompt"], "resolved prompt")
        self.assertEqual(self.db.resolve_session_id("exact-session"), "exact-session")
        self.assertEqual(self.db.resolve_session_id("prefix-"), "prefix-one")
        self.assertEqual(self.db.resolve_session_id("wild%t"), "wild%target")

        self.db.create_session("prefix-two", "cli")
        self.assertIsNone(self.db.resolve_session_id("prefix-"))
        self.assertIsNone(self.db.resolve_session_id("missing"))

    def test_dominant_route_flushes_queued_usage_and_archived_probe(self) -> None:
        self.db.create_session("session-1", "cli", model="seed-model")
        self.db.queue_token_counts(
            "session-1",
            input_tokens=100,
            api_call_count=1,
            model="model-a",
            billing_provider="provider-a",
            billing_base_url="https://provider-a.example/v1",
            billing_mode="api",
        )
        self.db.queue_token_counts(
            "session-1",
            input_tokens=1,
            api_call_count=2,
            model="model-b",
            billing_provider="provider-b",
            billing_base_url="https://provider-b.example/v1",
            billing_mode="api",
        )

        route = self.db.get_dominant_session_model_route("session-1")
        self.assertEqual(route["model"], "model-b")
        self.assertEqual(route["billing_provider"], "provider-b")
        self.assertEqual(route["api_call_count"], 2)

        self.db.append_message("session-1", "user", "old message")
        self.assertFalse(self.db.has_archived_messages("session-1"))
        self.db.replace_messages(
            "session-1",
            [{"role": "user", "content": "new message"}],
            archive_dropped=True,
        )
        self.assertTrue(self.db.has_archived_messages("session-1"))

    def test_legacy_escape_like_patch_path_remains_live(self) -> None:
        self.db.create_session("prefix-only", "cli")
        legacy_escape = session_db._escape_like
        with patch.object(
            session_db, "_escape_like", wraps=legacy_escape
        ) as escape_like:
            self.assertEqual(self.db.resolve_session_id("prefix-"), "prefix-only")

        escape_like.assert_called_once_with("prefix-")


if __name__ == "__main__":
    unittest.main()
