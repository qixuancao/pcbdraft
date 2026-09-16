"""Focused coverage for the SessionDB rewind mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_rewind


class SessionRewindMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.db.create_session("session-1", "cli")
        self.first_user_id = self.db.append_message(
            "session-1", "user", "first question", timestamp=10
        )
        self.first_assistant_id = self.db.append_message(
            "session-1", "assistant", "first answer", timestamp=11
        )
        self.second_content = [{"type": "text", "text": "second question"}]
        self.second_user_id = self.db.append_message(
            "session-1", "user", self.second_content, timestamp=12
        )
        self.second_assistant_id = self.db.append_message(
            "session-1", "assistant", "second answer", timestamp=13
        )

    def test_session_db_inherits_rewind_api_without_reverse_import(self) -> None:
        method_names = (
            "_is_duplicate_replayed_user_message",
            "rewind_to_message",
            "restore_rewound",
        )
        mixin = session_db_rewind.SessionRewindMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )

        tree = ast.parse(Path(session_db_rewind.__file__).read_text(encoding="utf-8"))
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

    def test_rewind_and_restore_round_trip_real_sqlite_rows(self) -> None:
        result = self.db.rewind_to_message("session-1", self.second_user_id)

        self.assertEqual(result["rewound_count"], 2)
        self.assertEqual(result["target_message"]["content"], self.second_content)
        self.assertEqual(result["new_head_id"], self.first_assistant_id)
        self.assertEqual(
            [row["id"] for row in self.db.get_messages("session-1")],
            [self.first_user_id, self.first_assistant_id],
        )
        all_rows = self.db.get_messages("session-1", include_inactive=True)
        self.assertEqual([row["active"] for row in all_rows], [1, 1, 0, 0])
        self.assertEqual(self.db.get_session("session-1")["rewind_count"], 1)

        repeated = self.db.rewind_to_message("session-1", self.second_user_id)
        self.assertEqual(repeated["rewound_count"], 0)
        self.assertEqual(self.db.get_session("session-1")["rewind_count"], 2)

        self.assertEqual(self.db.restore_rewound("session-1", self.second_user_id), 2)
        self.assertEqual(
            [row["id"] for row in self.db.get_messages("session-1")],
            [
                self.first_user_id,
                self.first_assistant_id,
                self.second_user_id,
                self.second_assistant_id,
            ],
        )

    def test_rewind_rejects_missing_and_non_user_targets(self) -> None:
        with self.assertRaisesRegex(ValueError, "not found"):
            self.db.rewind_to_message("session-1", 999_999)
        with self.assertRaisesRegex(ValueError, "must be a 'user' message"):
            self.db.rewind_to_message("session-1", self.first_assistant_id)

    def test_conversation_duplicate_dependency_and_patch_path_remain_live(self) -> None:
        self.db.create_session("root", "cli")
        self.db.append_message("root", "user", "replayed prompt")
        self.db.create_session("tip", "cli", parent_session_id="root")
        self.db.append_message("tip", "user", "replayed prompt")

        history = self.db.get_messages_as_conversation("tip", include_ancestors=True)
        self.assertEqual(
            [message["content"] for message in history], ["replayed prompt"]
        )

        with patch.object(
            session_db.SessionDB,
            "_is_duplicate_replayed_user_message",
            return_value=False,
        ) as duplicate_check:
            history = self.db.get_messages_as_conversation(
                "tip", include_ancestors=True
            )
        self.assertEqual(
            [message["content"] for message in history],
            ["replayed prompt", "replayed prompt"],
        )
        duplicate_check.assert_called()


if __name__ == "__main__":
    unittest.main()
