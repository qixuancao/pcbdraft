"""Focused coverage for the SessionDB conversation/lineage mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_conversation


class SessionConversationMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

        self.db.create_session("root", "cli")
        self.root_user_id = self.db.append_message(
            "root", "user", "root question", timestamp=10
        )
        self.db.append_message("root", "assistant", "root answer", timestamp=11)
        self.db.end_session("root", "compression")

        self.db.create_session("tip", "cli", parent_session_id="root")
        self.tip_user_id = self.db.append_message(
            "tip", "user", "tip question", timestamp=12
        )
        self.db.append_message("tip", "assistant", "tip answer", timestamp=13)

    def test_session_db_inherits_conversation_api_without_reverse_import(self) -> None:
        method_names = (
            "resolve_resume_session_id",
            "get_messages_as_conversation",
            "_rows_to_conversation",
            "get_resume_conversations",
            "get_resume_message_count",
            "assert_resume_safe",
            "assert_export_safe",
            "get_ancestor_display_prefix",
            "_is_explicit_branch_session",
            "get_conversation_root",
            "_session_lineage_root_to_tip",
        )
        mixin = session_db_conversation.SessionConversationMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )
        self.assertIs(session_db_conversation.logger, session_db.logger)

        tree = ast.parse(
            Path(session_db_conversation.__file__).read_text(encoding="utf-8")
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

    def test_compression_lineage_restores_model_and_display_histories(self) -> None:
        self.assertEqual(self.db.resolve_resume_session_id("root"), "tip")
        self.assertEqual(self.db.get_conversation_root("tip"), "root")
        self.assertEqual(self.db._session_lineage_root_to_tip("tip"), ["root", "tip"])

        model_history, display_history = self.db.get_resume_conversations("tip")
        self.assertEqual(
            [message["content"] for message in model_history],
            ["tip question", "tip answer"],
        )
        self.assertEqual(
            [message["content"] for message in display_history],
            ["root question", "root answer", "tip question", "tip answer"],
        )
        self.assertEqual(model_history[0]["_row_id"], self.tip_user_id)

        with_ancestors = self.db.get_messages_as_conversation(
            "tip", include_ancestors=True, include_row_ids=True
        )
        self.assertEqual(with_ancestors, display_history)
        prefix = self.db.get_ancestor_display_prefix("tip")
        self.assertEqual(
            [message["content"] for message in prefix],
            ["root question", "root answer"],
        )
        self.assertEqual(self.db.get_resume_message_count("tip"), 4)

    def test_explicit_branch_owns_its_copied_history(self) -> None:
        self.db.create_session(
            "branch",
            "cli",
            parent_session_id="root",
            model_config={"_branched_from": "root"},
        )
        self.db.append_message("branch", "user", "branch copy")

        self.assertTrue(self.db._is_explicit_branch_session("branch"))
        self.assertEqual(self.db.get_ancestor_display_prefix("branch"), [])
        self.assertEqual(
            [
                message["content"]
                for message in self.db.get_messages_as_conversation(
                    "branch", include_ancestors=True
                )
            ],
            ["branch copy"],
        )
        self.assertEqual(self.db.get_conversation_root("branch"), "root")

    def test_guards_and_legacy_monkeypatch_paths_remain_effective(self) -> None:
        self.assertEqual(self.db.assert_resume_safe("tip", max_messages=4), 4)
        with self.assertRaises(session_db.SessionResumeTooLargeError):
            self.db.assert_resume_safe("tip", max_messages=3)
        self.assertEqual(self.db.assert_export_safe("tip", max_messages=2), 2)
        with self.assertRaises(session_db.SessionExportTooLargeError):
            self.db.assert_export_safe("tip", max_messages=1)

        with patch.object(
            session_db, "resolved_max_resume_messages", return_value=0
        ) as resolver:
            self.assertEqual(self.db.assert_resume_safe("tip"), 0)
        resolver.assert_called_once_with()

        def keep_last(messages):
            return messages[-1:]

        with patch.object(
            session_db,
            "_strip_background_review_harness",
            side_effect=keep_last,
        ) as strip_harness:
            history = self.db.get_messages_as_conversation("tip")
        self.assertEqual([message["content"] for message in history], ["tip answer"])
        strip_harness.assert_called_once()


if __name__ == "__main__":
    unittest.main()
