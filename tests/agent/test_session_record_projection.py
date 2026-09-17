"""Focused tests for pure session-record projections."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop, session_record_projection
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.session_record_projection import SessionRecordProjectionMixin


class SessionRecordProjectionCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_module_has_no_reverse_import_and_agent_inherits_methods(self) -> None:
        source = Path(session_record_projection.__file__).read_text(encoding="utf-8")
        imports = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        )

        self.assertNotIn("pcbdraft.agent.loop", imports)
        self.assertTrue(issubclass(AIAgent, SessionRecordProjectionMixin))
        for name in (
            "_get_messages_up_to_last_assistant",
            "_clean_session_content",
            "_redact_message_content",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name),
                    getattr(SessionRecordProjectionMixin, name),
                )
        self.assertIs(inspect.getmodule(AIAgent._save_session_log), loop)

    def test_legacy_transform_and_regex_patch_paths_remain_dynamic(self) -> None:
        fake_re = SimpleNamespace(sub=MagicMock(side_effect=lambda _p, _r, text: text))
        with (
            patch.object(
                loop,
                "convert_scratchpad_to_think",
                return_value=" projected ",
            ) as convert,
            patch.object(loop, "re", fake_re),
        ):
            self.assertEqual(self.agent._clean_session_content("raw"), "projected")

        convert.assert_called_once_with("raw")
        self.assertEqual(fake_re.sub.call_count, 2)

    def test_legacy_redaction_patch_path_controls_all_text_fields(self) -> None:
        content = [
            {"type": "text", "text": "secret"},
            {"type": "input_text", "content": "private"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            "opaque",
        ]
        original = [part.copy() if isinstance(part, dict) else part for part in content]

        with patch.object(
            loop,
            "redact_sensitive_text",
            side_effect=lambda text: f"redacted:{text}",
        ) as redact:
            projected = self.agent._redact_message_content(content)

        self.assertEqual(
            projected,
            [
                {"type": "text", "text": "redacted:secret"},
                {"type": "input_text", "content": "redacted:private"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,x"},
                },
                "opaque",
            ],
        )
        self.assertEqual(content, original)
        self.assertEqual(
            [call.args[0] for call in redact.call_args_list], ["secret", "private"]
        )


class SessionRecordProjectionBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_history_projection_stops_before_last_assistant(self) -> None:
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "incomplete"},
            {"role": "tool", "content": "trailing"},
        ]

        self.assertEqual(
            self.agent._get_messages_up_to_last_assistant(history),
            history[:3],
        )

        users_only = [{"role": "user", "content": "hello"}]
        projected = self.agent._get_messages_up_to_last_assistant(users_only)
        self.assertEqual(projected, users_only)
        self.assertIsNot(projected, users_only)

    def test_content_projection_handles_empty_and_plain_text(self) -> None:
        self.assertEqual(self.agent._clean_session_content(""), "")
        self.assertIsNone(self.agent._redact_message_content(None))
        with patch.object(
            loop,
            "redact_sensitive_text",
            return_value="safe",
        ) as redact:
            self.assertEqual(self.agent._redact_message_content("secret"), "safe")
        redact.assert_called_once_with("secret")


if __name__ == "__main__":
    unittest.main()
