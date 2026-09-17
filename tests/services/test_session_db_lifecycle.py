"""Focused coverage for the SessionDB lifecycle mixin boundary."""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_lifecycle


class SessionLifecycleMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))

    def test_session_db_inherits_lifecycle_api_without_reverse_import(self) -> None:
        method_names = (
            "_store_system_prompt",
            "_delete_unreferenced_system_prompts",
            "_insert_session_row",
            "create_session",
            "find_live_compression_child",
            "reopen_orphaned_compression_session",
            "publish_compression_child",
            "end_session",
            "reopen_session",
            "promote_to_session_reset",
            "ensure_session",
            "replace_messages",
            "get_active_message_watermark",
            "archive_and_compact",
        )
        mixin = session_db_lifecycle.SessionLifecycleMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )
        self.assertEqual(
            session_db.SessionDB._NON_CONTINUATION_CHILD_FILTER_SQL,
            mixin._NON_CONTINUATION_CHILD_FILTER_SQL,
        )
        self.assertNotIn("_compression_lease_matches_on_conn", mixin.__dict__)
        self.assertNotIn("_reclaim_expired_compression_lease_on_conn", mixin.__dict__)

        tree = ast.parse(
            Path(session_db_lifecycle.__file__).read_text(encoding="utf-8")
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

    def test_real_create_replace_end_and_reopen_lifecycle(self) -> None:
        self.assertEqual(
            self.db.create_session(
                "session-1",
                "cli",
                model="test-model",
                model_config={"temperature": 0.25},
            ),
            "session-1",
        )
        self.db.append_message("session-1", "user", "old request")
        self.db.append_message("session-1", "assistant", "old response")

        replacement = [
            {"role": "user", "content": "new request", "timestamp": 10},
            {
                "role": "assistant",
                "content": "new response",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "probe", "arguments": "{}"},
                    }
                ],
                "timestamp": 11,
            },
        ]
        self.db.replace_messages("session-1", replacement)

        messages = self.db.get_messages("session-1")
        self.assertEqual(
            [(message["role"], message["content"]) for message in messages],
            [("user", "new request"), ("assistant", "new response")],
        )
        row = self.db.get_session("session-1")
        self.assertEqual(row["message_count"], 2)
        self.assertEqual(row["tool_call_count"], 1)

        self.db.end_session("session-1", "complete")
        self.db.end_session("session-1", "later-close-must-not-win")
        row = self.db.get_session("session-1")
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["end_reason"], "complete")

        self.db.reopen_session("session-1")
        row = self.db.get_session("session-1")
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["end_reason"])

    def test_real_compaction_gateway_uses_host_lease_api(self) -> None:
        self.db.create_session(
            "compact-session",
            "cli",
            model_config={"keep": "value"},
        )
        self.db.append_message("compact-session", "user", "old request")
        watermark = self.db.get_active_message_watermark("compact-session")
        self.db.append_message("compact-session", "user", "concurrent tail")
        self.assertTrue(
            self.db.try_acquire_compression_lock(
                "compact-session", "test-holder", ttl_seconds=60
            )
        )

        active_count = self.db.archive_and_compact(
            "compact-session",
            [{"role": "assistant", "content": "summary", "timestamp": 20}],
            model_config_patch={"compacted": True},
            watermark=watermark,
            lock_holder="test-holder",
        )

        self.assertEqual(active_count, 2)
        messages = self.db.get_messages("compact-session")
        self.assertEqual(
            [(message["role"], message["content"]) for message in messages],
            [("assistant", "summary"), ("user", "concurrent tail")],
        )
        row = self.db.get_session("compact-session")
        self.assertEqual(row["message_count"], 2)
        self.assertEqual(
            json.loads(row["model_config"]), {"keep": "value", "compacted": True}
        )

    def test_legacy_system_prompt_hash_patch_path_remains_live(self) -> None:
        with patch.object(
            session_db, "_system_prompt_hash", return_value="legacy-patched-hash"
        ) as prompt_hash:
            self.db.create_session(
                "patched-session",
                "cli",
                system_prompt="patched prompt",
            )

        prompt_hash.assert_called_once_with("patched prompt")
        row = self.db._conn.execute(
            "SELECT system_prompt_hash FROM sessions WHERE id = ?",
            ("patched-session",),
        ).fetchone()
        self.assertEqual(row["system_prompt_hash"], "legacy-patched-hash")
        prompt = self.db._conn.execute(
            "SELECT prompt FROM system_prompts WHERE hash = ?",
            ("legacy-patched-hash",),
        ).fetchone()
        self.assertEqual(prompt["prompt"], "patched prompt")


if __name__ == "__main__":
    unittest.main()
