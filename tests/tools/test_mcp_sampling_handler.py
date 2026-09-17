"""Focused tests for the extracted MCP sampling callback boundary."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.tools import mcp_sampling_handler, mcp_tool


def _namespace_result(**kwargs):
    return SimpleNamespace(**kwargs)


class MCPSamplingCompatibilityTests(unittest.TestCase):
    def test_module_has_no_reverse_import_and_legacy_identity_remains(self) -> None:
        source = Path(mcp_sampling_handler.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )

        self.assertNotIn("pcbdraft.tools.mcp_tool", imports)
        self.assertIs(mcp_tool.SamplingHandler, mcp_sampling_handler.SamplingHandler)
        self.assertIs(inspect.getmodule(mcp_tool._safe_numeric), mcp_tool)
        self.assertIs(inspect.getmodule(mcp_tool.MCPServerTask.run), mcp_tool)
        self.assertFalse(hasattr(mcp_sampling_handler, "_servers"))

    def test_constructor_and_rate_limit_use_legacy_patch_paths(self) -> None:
        with patch.object(
            mcp_tool,
            "_safe_numeric",
            side_effect=[2, 7.5, 1024, 3],
        ) as numeric:
            handler = mcp_tool.SamplingHandler(
                "docs",
                {
                    "max_rpm": "2",
                    "timeout": "7.5",
                    "max_tokens_cap": "1024",
                    "max_tool_rounds": "3",
                },
            )

        self.assertEqual(handler.max_rpm, 2)
        self.assertEqual(handler.timeout, 7.5)
        self.assertEqual(handler.max_tokens_cap, 1024)
        self.assertEqual(handler.max_tool_rounds, 3)
        self.assertEqual(numeric.call_count, 4)

        with patch.object(mcp_tool.time, "time", side_effect=[100.0, 101.0]):
            self.assertTrue(handler._check_rate_limit())
            self.assertTrue(handler._check_rate_limit())
        with patch.object(mcp_tool.time, "time", return_value=102.0):
            self.assertFalse(handler._check_rate_limit())


class MCPSamplingHandlerTests(unittest.TestCase):
    def test_message_conversion_preserves_tool_and_multimodal_blocks(self) -> None:
        handler = mcp_tool.SamplingHandler("docs", {})
        tool_result = SimpleNamespace(
            toolUseId="call_result",
            content=[SimpleNamespace(text="done")],
        )
        tool_use = SimpleNamespace(
            id="call_use",
            name="lookup",
            input={"query": "pcb"},
        )
        text = SimpleNamespace(text="working")
        image = SimpleNamespace(data="YWJj", mimeType="image/png")
        params = SimpleNamespace(
            messages=[
                SimpleNamespace(
                    role="assistant",
                    content=[tool_result, tool_use, text],
                ),
                SimpleNamespace(role="user", content=[image]),
            ]
        )

        with patch.object(
            mcp_tool,
            "mcp_field",
            wraps=mcp_tool.mcp_field,
        ) as field:
            converted = handler._convert_messages(params)

        self.assertEqual(
            converted,
            [
                {
                    "role": "tool",
                    "tool_call_id": "call_result",
                    "content": "done",
                },
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_use",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps(
                                    {"query": "pcb"}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                    "content": "working",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,YWJj",
                            },
                        }
                    ],
                },
            ],
        )
        self.assertGreater(field.call_count, 0)

    def test_sdk_result_builders_use_legacy_lazy_type_paths(self) -> None:
        handler = mcp_tool.SamplingHandler("docs", {"max_tool_rounds": 2})
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="lookup", arguments='{"x": 1}'),
        )
        tool_choice = SimpleNamespace(
            finish_reason="tool_calls",
            message=SimpleNamespace(tool_calls=[tool_call]),
        )
        response = SimpleNamespace(
            model="sample-model",
            usage=SimpleNamespace(total_tokens=4),
        )
        tool_content = Mock(side_effect=_namespace_result)
        tool_result = Mock(side_effect=_namespace_result)

        with (
            patch.object(mcp_tool, "ToolUseContent", tool_content),
            patch.object(
                mcp_tool,
                "CreateMessageResultWithTools",
                tool_result,
            ),
        ):
            rendered = handler._build_tool_use_result(tool_choice, response)

        self.assertEqual(rendered.stopReason, "toolUse")
        self.assertEqual(rendered.content[0].input, {"x": 1})
        tool_content.assert_called_once_with(
            type="tool_use",
            id="call_1",
            name="lookup",
            input={"x": 1},
        )
        tool_result.assert_called_once()

        tools_capability_value = SimpleNamespace()
        capability = Mock(side_effect=_namespace_result)
        tools_capability = Mock(return_value=tools_capability_value)
        with (
            patch.object(mcp_tool, "SamplingCapability", capability),
            patch.object(mcp_tool, "SamplingToolsCapability", tools_capability),
        ):
            kwargs = handler.session_kwargs()

        self.assertIs(kwargs["sampling_callback"], handler)
        self.assertIs(
            kwargs["sampling_capabilities"].tools,
            tools_capability_value,
        )

    def test_successful_callback_calls_auxiliary_router_and_builds_text_result(
        self,
    ) -> None:
        handler = mcp_tool.SamplingHandler(
            "docs",
            {
                "timeout": 4,
                "max_tokens_cap": 100,
                "allowed_models": ["sample-model"],
            },
        )
        params = SimpleNamespace(
            messages=[
                SimpleNamespace(
                    role="user",
                    content=SimpleNamespace(text="hello"),
                )
            ],
            model_preferences=SimpleNamespace(
                hints=[SimpleNamespace(name="sample-model")]
            ),
            system_prompt="system",
            max_tokens=200,
            temperature=0.2,
            tools=[
                SimpleNamespace(
                    name="lookup",
                    description="Lookup",
                    inputSchema={"type": "object"},
                )
            ],
        )
        choice = SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="provider text", tool_calls=None),
        )
        provider_response = SimpleNamespace(
            choices=[choice],
            model="sample-model",
            usage=SimpleNamespace(total_tokens=12),
        )
        call_llm = Mock(return_value=provider_response)
        normalize = Mock(return_value={"type": "object", "additionalProperties": True})
        sanitize = Mock(return_value="safe text")
        text_content = Mock(side_effect=_namespace_result)
        message_result = Mock(side_effect=_namespace_result)

        with (
            patch(
                "pcbdraft.model.auxiliary_client.call_llm",
                call_llm,
            ),
            patch.object(mcp_tool, "_normalize_mcp_input_schema", normalize),
            patch.object(mcp_tool, "_sanitize_error", sanitize),
            patch.object(mcp_tool, "TextContent", text_content),
            patch.object(mcp_tool, "CreateMessageResult", message_result),
        ):
            rendered = asyncio.run(handler(None, params))

        self.assertEqual(rendered.stopReason, "endTurn")
        self.assertEqual(rendered.content.text, "safe text")
        self.assertEqual(handler.metrics["requests"], 1)
        self.assertEqual(handler.metrics["tokens_used"], 12)
        normalize.assert_called_once_with({"type": "object"})
        sanitize.assert_called_once_with("provider text")
        call_llm.assert_called_once_with(
            task="mcp",
            model="sample-model",
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            temperature=0.2,
            max_tokens=100,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Lookup",
                        "parameters": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    },
                }
            ],
            timeout=4,
        )

    def test_error_result_uses_legacy_sdk_type_and_feature_flag(self) -> None:
        error_data = Mock(side_effect=_namespace_result)
        with (
            patch.object(mcp_tool, "_MCP_SAMPLING_TYPES", True),
            patch.object(mcp_tool, "ErrorData", error_data),
        ):
            result = mcp_tool.SamplingHandler._error("blocked", code=9)

        self.assertEqual(result.message, "blocked")
        self.assertEqual(result.code, 9)
        error_data.assert_called_once_with(code=9, message="blocked")

    def test_provider_failure_uses_legacy_error_projection_paths(self) -> None:
        handler = mcp_tool.SamplingHandler("docs", {"timeout": 2})
        params = SimpleNamespace(
            messages=[],
            model_preferences=None,
            system_prompt=None,
            max_tokens=8,
            temperature=None,
            tools=None,
        )
        failure = RuntimeError("provider secret")
        error_data = Mock(side_effect=_namespace_result)

        with (
            patch(
                "pcbdraft.model.auxiliary_client.call_llm",
                side_effect=failure,
            ),
            patch.object(mcp_tool, "_MCP_SAMPLING_TYPES", True),
            patch.object(mcp_tool, "ErrorData", error_data),
            patch.object(mcp_tool, "_exc_str", return_value="raw") as exc_str,
            patch.object(mcp_tool, "_sanitize_error", return_value="safe") as sanitize,
        ):
            result = asyncio.run(handler(None, params))

        self.assertEqual(result.message, "Sampling LLM call failed: safe")
        self.assertEqual(handler.metrics["errors"], 1)
        exc_str.assert_called_once_with(failure)
        sanitize.assert_called_once_with("raw")


if __name__ == "__main__":
    unittest.main()
