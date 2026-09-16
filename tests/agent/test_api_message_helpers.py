"""Focused tests for the AIAgent API-message helper mixin."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.agent import loop
from pcbdraft.agent.api_message_helpers import ApiMessageHelpersMixin
from pcbdraft.agent.loop import AIAgent


def _tool_call(name: str, arguments: str = "{}", call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class ApiMessageHelperCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_agent_inherits_extracted_methods_without_wrappers(self) -> None:
        names = (
            "_build_system_prompt_parts",
            "_build_system_prompt",
            "_get_tool_call_id_static",
            "_get_tool_call_name_static",
            "_sanitize_api_messages",
            "_is_thinking_only_assistant",
            "_drop_thinking_only_and_merge_users",
            "_cap_delegate_task_calls",
            "_deduplicate_tool_calls",
            "_uniquify_tool_call_ids",
            "_repair_tool_call",
            "_deterministic_call_id",
            "_split_responses_tool_id",
            "_derive_responses_function_call_id",
        )

        self.assertTrue(issubclass(AIAgent, ApiMessageHelpersMixin))
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name), getattr(ApiMessageHelpersMixin, name)
                )

    def test_legacy_loop_patch_paths_remain_late_bound(self) -> None:
        calls = [_tool_call("todo")]
        with patch.object(
            loop, "_sanitize_coalesce_tool_call_id", return_value="patched-call"
        ):
            self.assertEqual(AIAgent._get_tool_call_id_static(calls[0]), "patched-call")
        with patch.object(
            loop, "_sanitize_uniquify_tool_call_ids", return_value="patched-unique"
        ):
            self.assertEqual(AIAgent._uniquify_tool_call_ids(calls), "patched-unique")
        with patch.object(
            loop, "_codex_deterministic_call_id", return_value="patched-id"
        ):
            self.assertEqual(AIAgent._deterministic_call_id("todo", "{}"), "patched-id")
        with patch.object(
            loop,
            "_codex_split_responses_tool_id",
            return_value=("patched-call", "patched-item"),
        ):
            self.assertEqual(
                AIAgent._split_responses_tool_id("raw"),
                ("patched-call", "patched-item"),
            )
        with patch.object(
            loop,
            "_codex_derive_responses_function_call_id",
            return_value="patched-item",
        ):
            self.assertEqual(
                self.agent._derive_responses_function_call_id("call_1"),
                "patched-item",
            )

    def test_system_prompt_forwarders_keep_policy_patch_paths(self) -> None:
        with patch(
            "pcbdraft.agent.system_prompt.build_system_prompt_parts",
            return_value={"identity": "patched"},
        ) as build_parts:
            self.assertEqual(
                self.agent._build_system_prompt_parts("system"),
                {"identity": "patched"},
            )
            build_parts.assert_called_once_with(self.agent, system_message="system")
        with patch(
            "pcbdraft.agent.system_prompt.build_system_prompt",
            return_value="patched prompt",
        ) as build_prompt:
            self.assertEqual(
                self.agent._build_system_prompt("system"), "patched prompt"
            )
            build_prompt.assert_called_once_with(self.agent, system_message="system")


class ApiMessageHelperBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_sanitize_and_thinking_cleanup_use_inherited_policy(self) -> None:
        messages = [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "private reasoning",
            },
            {"role": "user", "content": "second"},
            {"role": "invalid", "content": "drop me"},
        ]

        sanitized = self.agent._sanitize_api_messages(messages)
        self.assertNotIn("invalid", [message["role"] for message in sanitized])
        cleaned = self.agent._drop_thinking_only_and_merge_users(sanitized)
        self.assertEqual(cleaned, [{"role": "user", "content": "first\n\nsecond"}])
        self.assertEqual(messages[0]["content"], "first")

    def test_delegate_cap_and_dedupe_keep_first_calls(self) -> None:
        calls = [
            _tool_call("delegate_task", '{"b":2,"a":1}', "call_1"),
            _tool_call("delegate_task", '{"a":1,"b":2}', "call_2"),
            _tool_call("todo", "{}", "call_3"),
        ]

        with (
            patch(
                "pcbdraft.tools.delegate_tool._get_max_concurrent_children",
                return_value=1,
            ),
            patch.object(loop.logger, "warning") as warning,
        ):
            capped = self.agent._cap_delegate_task_calls(calls)
            self.assertEqual(capped, [calls[0], calls[2]])
            warning.assert_called_once()

        with patch.object(loop.logger, "warning") as warning:
            deduplicated = self.agent._deduplicate_tool_calls(calls[:2])
            self.assertEqual(deduplicated, [calls[0]])
            warning.assert_called_once_with(
                "Removed duplicate tool call: %s", "delegate_task"
            )

    def test_tool_name_and_response_id_helpers_preserve_behavior(self) -> None:
        self.agent.valid_tool_names = {"write_file", "todo"}
        call = _tool_call("todo", call_id="call_1")

        self.assertEqual(self.agent._get_tool_call_name_static(call), "todo")
        self.assertEqual(self.agent._repair_tool_call("WriteFile_tool"), "write_file")
        generated = self.agent._deterministic_call_id("todo", "{}")
        self.assertTrue(generated.startswith("call_"))
        self.assertEqual(
            self.agent._split_responses_tool_id("call_1|fc_1"),
            ("call_1", "fc_1"),
        )
        self.assertEqual(
            self.agent._derive_responses_function_call_id("call_1"), "fc_1"
        )


if __name__ == "__main__":
    unittest.main()
