"""Regression coverage for the native agent's late-bound runtime namespace."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.agent import loop
from pcbdraft.agent.agent_runtime_helpers import invoke_tool
from pcbdraft.agent.loop import AIAgent


def _tool_definition(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class RuntimeDynamicReexportTests(unittest.TestCase):
    def _agent(self, *, tool_name: str = "tool_search") -> AIAgent:
        runtime_home = tempfile.TemporaryDirectory(prefix="pcbdraft-reexport-")
        self.addCleanup(runtime_home.cleanup)
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "PCBDRAFT_RUNTIME_HOME": runtime_home.name,
                    "PCBDRAFT_DEBUG_TRACE": "0",
                },
            )
        )
        with patch.object(
            loop,
            "get_tool_definitions",
            return_value=[_tool_definition(tool_name)],
        ):
            agent = AIAgent(
                base_url="http://127.0.0.1:1/v1",
                api_key="test-only",
                provider="custom",
                api_mode="chat_completions",
                model="native-test",
                max_iterations=1,
                enabled_toolsets=["pcbdraft"],
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                skip_background_review=True,
                session_id="dynamic-reexport-test",
            )
        self.addCleanup(agent.close)
        return agent

    def test_agent_initialization_uses_loop_tool_definition_reexport(self) -> None:
        definition = _tool_definition("tool_search")
        with tempfile.TemporaryDirectory(prefix="pcbdraft-init-") as runtime_home:
            with (
                patch.dict(
                    os.environ,
                    {
                        "PCBDRAFT_RUNTIME_HOME": runtime_home,
                        "PCBDRAFT_DEBUG_TRACE": "0",
                    },
                ),
                patch.object(
                    loop, "get_tool_definitions", return_value=[definition]
                ) as get_defs,
            ):
                agent = AIAgent(
                    base_url="http://127.0.0.1:1/v1",
                    api_key="test-only",
                    provider="custom",
                    api_mode="chat_completions",
                    model="native-test",
                    max_iterations=1,
                    enabled_toolsets=["pcbdraft"],
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                    skip_background_review=True,
                    session_id="dynamic-init-test",
                )
                self.addCleanup(agent.close)

            self.assertEqual(agent.valid_tool_names, {"tool_search"})
            get_defs.assert_called_once_with(
                enabled_toolsets=["pcbdraft"],
                disabled_toolsets=None,
                quiet_mode=True,
            )

    def test_system_prompt_resolves_deferred_tools_through_loop_namespace(self) -> None:
        agent = self._agent()
        agent.valid_tool_names = {"tool_search", "tool_call"}
        with patch.object(
            loop,
            "get_tool_definitions",
            return_value=[_tool_definition("pcb_render_board")],
        ) as get_defs:
            prompt = agent._build_system_prompt("inspect this board")

        get_defs.assert_called_once_with(
            enabled_toolsets=["pcbdraft"],
            disabled_toolsets=None,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        self.assertIn("pcb_render_board", prompt)

    def test_tool_dispatch_resolves_handle_function_call_through_loop_namespace(
        self,
    ) -> None:
        agent = SimpleNamespace(
            session_id="dispatch-session",
            valid_tool_names={"custom_tool"},
            _current_turn_id="turn-1",
            _current_api_request_id="request-1",
            _memory_manager=None,
            enabled_toolsets=["pcbdraft"],
            disabled_toolsets=None,
        )
        with patch.object(
            loop, "handle_function_call", return_value="handled"
        ) as dispatch:
            result = invoke_tool(
                agent,
                "custom_tool",
                {"value": 1},
                "task-1",
                tool_call_id="call-1",
                pre_tool_block_checked=True,
                skip_tool_request_middleware=True,
                skip_tool_execution_middleware=True,
            )

        self.assertEqual(result, "handled")
        dispatch.assert_called_once_with(
            "custom_tool",
            {"value": 1},
            "task-1",
            tool_call_id="call-1",
            session_id="dispatch-session",
            turn_id="turn-1",
            api_request_id="request-1",
            enabled_tools=["custom_tool"],
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            enabled_toolsets=["pcbdraft"],
            disabled_toolsets=None,
            tool_request_middleware_trace=[],
            skip_tool_execution_middleware=True,
        )


if __name__ == "__main__":
    unittest.main()
