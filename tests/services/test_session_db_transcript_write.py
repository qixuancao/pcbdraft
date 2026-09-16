"""Focused coverage for the SessionDB transcript-write mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_transcript_write


class SessionTranscriptWriteMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.db.create_session("session-1", "cli")

    def test_session_db_inherits_write_api_without_reverse_import(self) -> None:
        method_names = (
            "_encode_content",
            "_decode_content",
            "_encode_display_metadata",
            "_check_transcript_write_guards",
            "_decode_display_metadata",
            "_reasoning_json_text",
            "append_message",
            "append_messages_batch",
            "set_latest_matching_message_display_kind",
            "set_message_reaction",
            "get_message_reactions",
            "take_unseen_reactions",
            "latest_message_row_id",
            "latest_user_message_row_id",
            "get_message_role",
            "_insert_message_rows",
        )
        mixin = session_db_transcript_write.SessionTranscriptWriteMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )
        self.assertIs(
            session_db._scrub_surrogates,
            session_db_transcript_write._scrub_surrogates,
        )
        self.assertIs(session_db_transcript_write.logger, session_db.logger)

        tree = ast.parse(
            Path(session_db_transcript_write.__file__).read_text(encoding="utf-8")
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

    def test_append_and_batch_round_trip_content_and_display_metadata(self) -> None:
        structured_content = [
            {"type": "text", "text": "inspect this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]
        first_id = self.db.append_message(
            "session-1",
            "user",
            structured_content,
            display_kind="attachment",
            display_metadata={"source": "picker", "count": 1},
            timestamp=10,
        )
        batch = [
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "probe", "arguments": "{}"},
                    }
                ],
                "display_metadata": '{"batch": true}',
                "timestamp": 11,
            },
            {
                "role": "tool",
                "content": "done",
                "tool_name": "probe",
                "tool_call_id": "call-1",
                "timestamp": 12,
            },
        ]

        self.assertEqual(self.db.append_messages_batch("session-1", batch), 2)
        self.assertTrue(
            all(isinstance(message.get("_row_id"), int) for message in batch)
        )

        messages = self.db.get_messages("session-1")
        self.assertEqual([message["id"] for message in messages][0], first_id)
        self.assertEqual(messages[0]["content"], structured_content)
        self.assertEqual(messages[0]["display_kind"], "attachment")
        self.assertEqual(
            messages[0]["display_metadata"], {"source": "picker", "count": 1}
        )
        self.assertEqual(messages[1]["display_metadata"], {"batch": True})
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "call-1")

        session = self.db.get_session("session-1")
        self.assertEqual(session["message_count"], 3)
        self.assertEqual(session["tool_call_count"], 1)

    def test_display_stamp_and_reaction_round_trip(self) -> None:
        user_id = self.db.append_message("session-1", "user", "plain input")
        self.assertTrue(
            self.db.set_latest_matching_message_display_kind(
                "session-1",
                role="user",
                content="plain input",
                display_kind="project-command",
                display_metadata={"project_id": "board-1"},
            )
        )
        self.assertEqual(self.db.latest_user_message_row_id("session-1"), user_id)
        self.assertEqual(self.db.get_message_role("session-1", user_id), "user")

        assistant_id = self.db.append_message(
            "session-1",
            "assistant",
            "finished",
            display_metadata={"origin": "agent"},
        )
        reactions = self.db.set_message_reaction(
            "session-1", assistant_id, "ok", author="user"
        )
        self.assertEqual(len(reactions), 1)
        self.assertEqual(
            self.db.get_message_reactions("session-1", assistant_id)[0]["emoji"], "ok"
        )

        pending = self.db.take_unseen_reactions("session-1")
        self.assertEqual(
            pending,
            [
                {
                    "row_id": assistant_id,
                    "role": "assistant",
                    "emoji": "ok",
                    "text": "finished",
                }
            ],
        )
        self.assertEqual(self.db.take_unseen_reactions("session-1"), [])

        messages = self.db.get_messages("session-1")
        self.assertEqual(messages[0]["display_metadata"], {"project_id": "board-1"})
        self.assertEqual(messages[0]["display_kind"], "project-command")
        self.assertEqual(messages[1]["display_metadata"]["origin"], "agent")
        self.assertTrue(messages[1]["display_metadata"]["reactions"][0]["seen"])


if __name__ == "__main__":
    unittest.main()
