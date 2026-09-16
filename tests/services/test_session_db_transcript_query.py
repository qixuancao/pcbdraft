"""Focused coverage for the SessionDB transcript-query mixin boundary."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path

from pcbdraft.core import runtime_environment
from pcbdraft.services import session_db, session_db_transcript_query


class SessionTranscriptQueryMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.db = self.enterContext(session_db.SessionDB(self.home / "state.db"))
        self.db.create_session("session-1", "cli")

    def test_session_db_inherits_query_api_without_reverse_import(self) -> None:
        method_names = (
            "_message_column_names",
            "set_latest_user_api_content",
            "get_messages",
            "find_pr_url_messages",
            "get_messages_around",
        )
        mixin = session_db_transcript_query.SessionTranscriptQueryMixin
        self.assertTrue(issubclass(session_db.SessionDB, mixin))
        for name in method_names:
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(session_db.SessionDB, name),
                    inspect.getattr_static(mixin, name),
                )
        self.assertIs(session_db_transcript_query.logger, session_db.logger)

        tree = ast.parse(
            Path(session_db_transcript_query.__file__).read_text(encoding="utf-8")
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

    def test_sidecar_update_and_decoded_query_use_real_sqlite(self) -> None:
        structured = [{"type": "text", "text": "current prompt"}]
        older_id = self.db.append_message("session-1", "user", "older prompt")
        current_id = self.db.append_message(
            "session-1",
            "user",
            structured,
            display_metadata={"source": "composer"},
        )

        self.assertEqual(
            self.db.set_latest_user_api_content(
                "session-1", structured, "current prompt\nplugin context"
            ),
            1,
        )
        self.assertEqual(
            self.db.set_latest_user_api_content(
                "session-1", "does not match", "must not land"
            ),
            0,
        )

        messages = self.db.get_messages("session-1")
        self.assertEqual([row["id"] for row in messages], [older_id, current_id])
        self.assertEqual(messages[1]["content"], structured)
        self.assertEqual(messages[1]["api_content"], "current prompt\nplugin context")
        self.assertEqual(messages[1]["display_metadata"], {"source": "composer"})

    def test_paging_keyset_and_around_queries_preserve_insertion_order(self) -> None:
        batch = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
            for index in range(5)
        ]
        self.assertEqual(self.db.append_messages_batch("session-1", batch), 5)
        ids = [message["_row_id"] for message in batch]

        self.assertEqual(
            [
                row["content"]
                for row in self.db.get_messages("session-1", limit=2, offset=1)
            ],
            ["m1", "m2"],
        )
        self.assertEqual(
            [
                row["content"]
                for row in self.db.get_messages("session-1", limit=2, latest=True)
            ],
            ["m3", "m4"],
        )
        self.assertEqual(
            [
                row["content"]
                for row in self.db.get_messages("session-1", after_id=ids[1])
            ],
            ["m2", "m3", "m4"],
        )
        with self.assertRaisesRegex(ValueError, "after_id is incompatible"):
            self.db.get_messages("session-1", after_id=ids[0], latest=True)

        around = self.db.get_messages_around("session-1", ids[2], window=1)
        self.assertEqual(
            [row["content"] for row in around["window"]], ["m1", "m2", "m3"]
        )
        self.assertEqual(around["messages_before"], 1)
        self.assertEqual(around["messages_after"], 1)
        self.assertEqual(
            self.db.get_messages_around("session-1", 999_999, window=1),
            {"window": [], "messages_before": 0, "messages_after": 0},
        )

    def test_pr_url_query_returns_only_tool_candidates(self) -> None:
        self.db.create_session("session-2", "cli")
        self.db.append_message(
            "session-1", "tool", "https://github.com/acme/board/pull/12"
        )
        self.db.append_message(
            "session-1", "assistant", "https://github.com/acme/board/pull/ignored"
        )
        self.db.append_message("session-2", "tool", "no pull request here")

        self.assertEqual(
            self.db.find_pr_url_messages(["", "session-1", "session-2"]),
            [
                {
                    "session_id": "session-1",
                    "content": "https://github.com/acme/board/pull/12",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
