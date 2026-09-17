"""Focused tests for the AIAgent response-cleanup mixin."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.response_cleanup import ResponseCleanupMixin


class ResponseCleanupCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_agent_inherits_extracted_methods_without_wrappers(self) -> None:
        self.assertTrue(issubclass(AIAgent, ResponseCleanupMixin))
        for name in (
            "_has_content_after_think_block",
            "_strip_think_blocks",
            "_has_natural_response_ending",
            "_is_ollama_glm_backend",
            "_should_treat_stop_as_truncated",
            "_looks_like_codex_intermediate_ack",
            "_extract_reasoning",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name), getattr(ResponseCleanupMixin, name)
                )

    def test_shared_helper_patch_paths_remain_dynamic(self) -> None:
        message = object()
        with patch(
            "pcbdraft.agent.agent_runtime_helpers.strip_think_blocks",
            return_value="visible",
        ) as strip:
            self.assertEqual(self.agent._strip_think_blocks("raw"), "visible")
            strip.assert_called_once_with(self.agent, "raw")
        with patch(
            "pcbdraft.agent.agent_runtime_helpers.looks_like_codex_intermediate_ack",
            return_value=True,
        ) as looks_like:
            self.assertTrue(
                self.agent._looks_like_codex_intermediate_ack(
                    "user", "assistant", [], False
                )
            )
            looks_like.assert_called_once_with(
                self.agent, "user", "assistant", [], False
            )
        with patch(
            "pcbdraft.agent.agent_runtime_helpers.extract_reasoning",
            return_value="reasoning",
        ) as extract:
            self.assertEqual(self.agent._extract_reasoning(message), "reasoning")
            extract.assert_called_once_with(self.agent, message)


class ResponseCleanupBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_think_blocks_are_removed_before_visible_content_check(self) -> None:
        self.assertEqual(
            self.agent._strip_think_blocks("<think>secret</think>Answer."),
            "Answer.",
        )
        self.assertFalse(
            self.agent._has_content_after_think_block("<thinking>secret</thinking>")
        )
        self.assertTrue(
            self.agent._has_content_after_think_block(
                "<reasoning>secret</reasoning>Answer."
            )
        )

    def test_natural_response_endings_cover_text_code_and_emoji(self) -> None:
        self.assertFalse(self.agent._has_natural_response_ending("unfinished"))
        self.assertTrue(self.agent._has_natural_response_ending("finished."))
        self.assertTrue(
            self.agent._has_natural_response_ending("```python\nx = 1\n```")
        )
        self.assertTrue(self.agent._has_natural_response_ending("done 😀"))

    def test_ollama_glm_detection_does_not_match_arbitrary_local_routes(self) -> None:
        self.agent.model = "glm-4.7"
        self.agent.provider = "custom"
        self.agent._base_url_lower = "http://127.0.0.1:11434/v1"
        self.assertTrue(self.agent._is_ollama_glm_backend())

        self.agent._base_url_lower = "http://127.0.0.1:8000/v1"
        self.assertFalse(self.agent._is_ollama_glm_backend())

        self.agent.provider = "ollama"
        self.assertTrue(self.agent._is_ollama_glm_backend())

    def test_stop_truncation_requires_tool_context_and_unnatural_ending(self) -> None:
        self.agent.model = "glm-4.7"
        self.agent.provider = "ollama"
        self.agent._base_url_lower = "http://localhost:11434/v1"
        self.agent.api_mode = "chat_completions"
        assistant = SimpleNamespace(
            content="This response is still continuing with more details",
            tool_calls=None,
        )
        tool_history = [{"role": "tool", "content": "result"}]

        self.assertTrue(
            self.agent._should_treat_stop_as_truncated("stop", assistant, tool_history)
        )
        assistant.content += "."
        self.assertFalse(
            self.agent._should_treat_stop_as_truncated("stop", assistant, tool_history)
        )
        assistant.content = "This response is still continuing with more details"
        self.assertFalse(
            self.agent._should_treat_stop_as_truncated("stop", assistant, [])
        )

    def test_legacy_loop_regex_patch_controls_truncation_whitespace_gate(self) -> None:
        self.agent.model = "glm-4.7"
        self.agent.provider = "ollama"
        self.agent._base_url_lower = "http://localhost:11434/v1"
        self.agent.api_mode = "chat_completions"
        assistant = SimpleNamespace(
            content="This response is still continuing with more details",
            tool_calls=None,
        )
        fake_re = SimpleNamespace(search=MagicMock(return_value=None))

        with patch.object(loop, "re", fake_re):
            self.assertFalse(
                self.agent._should_treat_stop_as_truncated(
                    "stop", assistant, [{"role": "tool"}]
                )
            )
        fake_re.search.assert_called_once_with(r"\s", assistant.content)

    def test_intermediate_ack_and_reasoning_formats(self) -> None:
        self.assertTrue(
            self.agent._looks_like_codex_intermediate_ack(
                "Please inspect the repository.",
                "I'll inspect the repository and report back.",
                [],
            )
        )

        structured = SimpleNamespace(
            reasoning="first",
            reasoning_content="first",
            reasoning_details=[{"summary": "second"}],
            content="answer",
        )
        self.assertEqual(self.agent._extract_reasoning(structured), "first\n\nsecond")

        inline = SimpleNamespace(
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
            content="<think>inline thought</think>answer",
        )
        self.assertEqual(self.agent._extract_reasoning(inline), "inline thought")


if __name__ == "__main__":
    unittest.main()
