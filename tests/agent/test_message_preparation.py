"""Focused tests for message preparation extracted from the agent loop."""

from __future__ import annotations

import base64
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent.message_preparation import MessagePreparationMixin


class _MessageHarness(MessagePreparationMixin):
    provider = "test-provider"
    model = "test-model"
    _base_url_lower = "https://example.test/v1"

    def __init__(self) -> None:
        self._anthropic_image_fallback_cache: dict[str, str] = {}
        self._no_list_tool_content_models: set[tuple[str, str]] = set()


class MessagePreparationMixinTests(unittest.TestCase):
    def test_ai_agent_inherits_original_method_names(self) -> None:
        from pcbdraft.agent.loop import AIAgent

        self.assertTrue(issubclass(AIAgent, MessagePreparationMixin))
        self.assertIs(
            AIAgent._prepare_anthropic_messages_for_api,
            MessagePreparationMixin._prepare_anthropic_messages_for_api,
        )
        self.assertIs(
            AIAgent._qwen_prepare_chat_messages,
            MessagePreparationMixin._qwen_prepare_chat_messages,
        )

    def test_image_parts_are_detected_and_data_urls_are_materialized(self) -> None:
        harness = _MessageHarness()
        payload = b"small-image-payload"
        data_url = "data:image/png;base64," + base64.b64encode(payload).decode()

        path_text, path = harness._materialize_data_url_for_vision(data_url)
        try:
            self.assertIsNotNone(path)
            self.assertEqual(Path(path_text), path)
            self.assertEqual(path.suffix, ".png")
            self.assertEqual(path.read_bytes(), payload)
            self.assertTrue(
                harness._content_has_image_parts(
                    [{"type": "image_url", "image_url": {"url": data_url}}]
                )
            )
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def test_anthropic_nonvision_fallback_copies_and_rewrites_messages(self) -> None:
        harness = _MessageHarness()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.test/board.png"},
                    },
                ],
            }
        ]

        with (
            patch.object(harness, "_model_supports_vision", return_value=False),
            patch.object(
                harness,
                "_describe_image_for_anthropic_fallback",
                return_value="[board image description]",
            ),
        ):
            prepared = harness._prepare_anthropic_messages_for_api(messages)

        self.assertIsNot(prepared, messages)
        self.assertIsInstance(messages[0]["content"], list)
        self.assertEqual(
            prepared[0]["content"],
            "[board image description]\n\ninspect this",
        )

    def test_tool_message_image_downgrade_preserves_text_and_remembers_model(
        self,
    ) -> None:
        harness = _MessageHarness()
        messages = [
            {
                "role": "tool",
                "name": "computer_use",
                "content": [
                    {"type": "text", "text": "screen summary"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]

        changed = harness._try_strip_image_parts_from_tool_messages(messages)

        self.assertTrue(changed)
        self.assertEqual(messages[0]["content"], "screen summary")
        self.assertIn(
            ("test-provider", "test-model"),
            harness._no_list_tool_content_models,
        )

    def test_qwen_preparation_normalizes_parts_without_mutating_source(self) -> None:
        harness = _MessageHarness()
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": ["hello", {"type": "text", "text": "world"}]},
        ]

        prepared = harness._qwen_prepare_chat_messages(messages)

        self.assertEqual(messages[0]["content"], "system prompt")
        self.assertEqual(
            prepared[0]["content"],
            [
                {
                    "type": "text",
                    "text": "system prompt",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        )
        self.assertEqual(
            prepared[1]["content"],
            [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
