"""Focused tests for AIAgent session persistence."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.session_persistence import SessionPersistenceMixin
from pcbdraft.services.session_db import CompressionSessionClosedError


class SessionPersistenceCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_agent_inherits_all_extracted_methods(self) -> None:
        self.assertTrue(issubclass(AIAgent, SessionPersistenceMixin))
        for name in (
            "_build_memory_write_metadata",
            "_apply_persist_user_message_override",
            "_persist_session",
            "_drop_trailing_empty_response_scaffolding",
            "_repair_message_sequence",
            "_flush_messages_to_session_db",
            "_flush_messages_to_session_db_unlocked",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name), getattr(SessionPersistenceMixin, name)
                )

    def test_shared_helper_patch_paths_remain_dynamic(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        with patch(
            "pcbdraft.agent.background_review.build_memory_write_metadata",
            return_value={"origin": "test"},
        ) as metadata:
            self.assertEqual(
                self.agent._build_memory_write_metadata(write_origin="tool"),
                {"origin": "test"},
            )
        metadata.assert_called_once_with(
            self.agent,
            write_origin="tool",
            execution_context=None,
            task_id=None,
            tool_call_id=None,
        )

        with patch(
            "pcbdraft.agent.agent_runtime_helpers.repair_message_sequence",
            return_value=2,
        ) as repair:
            self.assertEqual(self.agent._repair_message_sequence(messages), 2)
        repair.assert_called_once_with(self.agent, messages)

    def test_legacy_summary_key_patch_controls_override_guard(self) -> None:
        self.agent._persist_user_message_idx = 0
        self.agent._persist_user_message_override = "clean"
        self.agent._persist_user_message_timestamp = "2026-09-17T00:00:00Z"
        messages = [
            {
                "role": "user",
                "content": "wire",
                "legacy_summary": True,
            }
        ]

        with patch.object(loop, "COMPRESSED_SUMMARY_METADATA_KEY", "legacy_summary"):
            self.agent._apply_persist_user_message_override(messages)

        self.assertEqual(messages[0]["content"], "wire")
        self.assertEqual(messages[0]["timestamp"], "2026-09-17T00:00:00Z")


class SessionPersistenceBehaviorTests(unittest.TestCase):
    def _agent_with_db(self) -> tuple[AIAgent, MagicMock]:
        agent = object.__new__(AIAgent)
        database = MagicMock()
        agent._persist_disabled = False
        agent._session_db = database
        agent._session_db_created = True
        agent.session_id = "session-old"
        agent._last_flushed_db_idx = 0
        agent._flushed_db_message_ids = set()
        agent._flushed_db_message_session_id = None
        agent._db_flush_scan_prefix = None
        agent._persist_user_message_idx = None
        agent._persist_user_message_override = None
        agent._persist_user_message_timestamp = None
        agent._pending_cli_user_message = None
        agent._session_persist_lock = None
        return agent, database

    def test_persist_session_serializes_snapshot_flush_and_token_drain(self) -> None:
        agent, database = self._agent_with_db()
        agent._session_persist_lock = threading.RLock()
        agent._drop_trailing_empty_response_scaffolding = MagicMock()
        agent._save_session_log = MagicMock()
        agent._flush_messages_to_session_db = MagicMock(return_value=True)
        messages = [{"role": "user", "content": "hello"}]
        history = [{"role": "assistant", "content": "prior"}]

        with patch(
            "pcbdraft.agent.agent_runtime_helpers.note_turn_persisted"
        ) as note_turn:
            agent._persist_session(messages, history)

        agent._drop_trailing_empty_response_scaffolding.assert_called_once_with(
            messages
        )
        agent._save_session_log.assert_called_once_with(messages)
        agent._flush_messages_to_session_db.assert_called_once_with(messages, history)
        database.flush_token_counts.assert_called_once_with()
        note_turn.assert_called_once_with(agent)
        self.assertIs(agent._session_messages, messages)

    def test_trailing_empty_scaffolding_rewinds_orphaned_tool_pair(self) -> None:
        agent, _database = self._agent_with_db()
        messages = [
            {"role": "user", "content": "request"},
            {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            {"role": "assistant", "_empty_terminal_sentinel": True},
        ]

        agent._drop_trailing_empty_response_scaffolding(messages)

        self.assertEqual(messages, [{"role": "user", "content": "request"}])

    def test_flush_writes_override_sidecar_once_and_marks_live_message(self) -> None:
        agent, database = self._agent_with_db()
        agent._persist_user_message_idx = 0
        agent._persist_user_message_override = "clean transcript"
        agent._persist_user_message_timestamp = "event-time"
        message = {"role": "user", "content": "wire prompt"}
        messages = [message]

        self.assertTrue(agent._flush_messages_to_session_db(messages))

        row = database.append_messages_batch.call_args.kwargs["messages"][0]
        self.assertEqual(row["content"], "clean transcript")
        self.assertEqual(row["api_content"], "wire prompt")
        self.assertEqual(row["timestamp"], "event-time")
        self.assertEqual(message["content"], "wire prompt")
        self.assertTrue(message[loop._DB_PERSISTED_MARKER])

        self.assertTrue(agent._flush_messages_to_session_db(messages))
        database.append_messages_batch.assert_called_once()

    def test_legacy_scaffolding_multimodal_and_marker_patches_control_flush(
        self,
    ) -> None:
        agent, database = self._agent_with_db()
        synthetic = {
            "role": "user",
            "content": "hidden",
            "legacy_ephemeral": True,
        }
        tool_result = {"role": "tool", "content": "multimodal"}
        messages = [synthetic, tool_result]

        with (
            patch.object(
                loop,
                "_is_ephemeral_scaffolding",
                side_effect=lambda message: bool(message.get("legacy_ephemeral")),
            ),
            patch.object(loop, "_DB_PERSISTED_MARKER", "_legacy_persisted"),
            patch.object(
                loop,
                "_is_multimodal_tool_result",
                side_effect=lambda content: content == "multimodal",
            ),
            patch.object(
                loop, "_multimodal_text_summary", return_value="text summary"
            ) as summarize,
        ):
            self.assertTrue(agent._flush_messages_to_session_db(messages))

        rows = database.append_messages_batch.call_args.kwargs["messages"]
        self.assertEqual([row["content"] for row in rows], ["text summary"])
        self.assertNotIn("_legacy_persisted", synthetic)
        self.assertTrue(tool_result["_legacy_persisted"])
        summarize.assert_called_once_with("multimodal")

    def test_compression_closed_write_adopts_live_tip_and_retries_once(self) -> None:
        agent, database = self._agent_with_db()
        database.append_messages_batch.side_effect = [
            CompressionSessionClosedError("session-old"),
            None,
        ]
        database.get_compression_tip.return_value = "session-new"
        database.get_session.return_value = {"ended_at": None}
        messages = [{"role": "assistant", "content": "answer"}]

        self.assertTrue(agent._flush_messages_to_session_db(messages))

        self.assertEqual(agent.session_id, "session-new")
        self.assertFalse(agent._compression_adoption_failed)
        self.assertEqual(database.append_messages_batch.call_count, 2)
        self.assertEqual(
            [
                call.kwargs["session_id"]
                for call in database.append_messages_batch.call_args_list
            ],
            ["session-old", "session-new"],
        )
        self.assertTrue(messages[0][loop._DB_PERSISTED_MARKER])

    def test_disabled_persistence_never_touches_session_db(self) -> None:
        agent, database = self._agent_with_db()
        agent._persist_disabled = True

        self.assertIsNone(
            agent._flush_messages_to_session_db(
                [{"role": "user", "content": "isolated"}]
            )
        )
        database.assert_not_called()
        database.append_messages_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
