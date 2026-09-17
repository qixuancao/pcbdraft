"""Focused tests for the extracted MCP elicitation callback boundary."""

from __future__ import annotations

import ast
import asyncio
import contextvars
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.tools import mcp_elicitation_handler, mcp_tool


class _ElicitResult:
    def __init__(self, *, action: str, content=None):
        self.action = action
        self.content = content


class MCPElicitationCompatibilityTests(unittest.TestCase):
    def test_module_has_no_reverse_import_and_legacy_identities_remain(self) -> None:
        source = Path(mcp_elicitation_handler.__file__).read_text(encoding="utf-8")
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
        self.assertIs(
            mcp_tool.ElicitationHandler,
            mcp_elicitation_handler.ElicitationHandler,
        )
        self.assertIs(
            mcp_tool._format_elicitation_schema_summary,
            mcp_elicitation_handler._format_elicitation_schema_summary,
        )
        self.assertIs(
            inspect.getmodule(mcp_tool.MCPServerTask.run),
            mcp_tool,
        )
        self.assertFalse(hasattr(mcp_elicitation_handler, "_servers"))

    def test_legacy_safe_numeric_patch_configures_handler(self) -> None:
        with patch.object(mcp_tool, "_safe_numeric", return_value=17.0) as numeric:
            handler = mcp_tool.ElicitationHandler("docs", {"timeout": "20"})

        numeric.assert_called_once_with("20", 300, float)
        self.assertEqual(handler.timeout, 17.0)
        self.assertIs(handler.session_kwargs()["elicitation_callback"], handler)


class MCPElicitationHandlerTests(unittest.TestCase):
    def test_schema_summary_projects_fields(self) -> None:
        rendered = mcp_elicitation_handler._format_elicitation_schema_summary(
            {
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Receipt address",
                    },
                    "remember": {"type": "boolean"},
                }
            },
            "payments",
        )

        self.assertEqual(
            rendered,
            "Fields requested by MCP server 'payments':\n"
            "  - email (string): Receipt address\n"
            "  - remember (boolean)",
        )

    def test_url_mode_declines_through_legacy_sdk_and_logger_paths(self) -> None:
        handler = mcp_tool.ElicitationHandler("payments", {})
        with (
            patch.object(mcp_tool, "ElicitResult", _ElicitResult),
            patch.object(mcp_tool.logger, "info") as log_info,
        ):
            result = asyncio.run(handler(None, SimpleNamespace(mode="url")))

        self.assertEqual(result.action, "decline")
        self.assertEqual(handler.metrics["requests"], 1)
        self.assertEqual(handler.metrics["declined"], 1)
        log_info.assert_called_once()

    def test_form_accept_replays_owner_context_and_legacy_helpers(self) -> None:
        route = contextvars.ContextVar("route", default="current")
        token = route.set("captured")
        captured = contextvars.copy_context()
        route.reset(token)
        owner = SimpleNamespace(_pending_call_context=captured)
        handler = mcp_tool.ElicitationHandler(
            "payments",
            {"timeout": 8},
            owner=owner,
        )
        consent_contexts: list[str] = []

        def approve(*args, **kwargs):
            consent_contexts.append(route.get())
            return "accept"

        format_schema = Mock(return_value="field summary")
        sanitize = Mock(return_value="safe message")
        params = SimpleNamespace(
            mode="form",
            message="card details",
            requested_schema={"properties": {"card": {"type": "string"}}},
        )
        with (
            patch.object(mcp_tool, "ElicitResult", _ElicitResult),
            patch.object(
                mcp_tool,
                "_format_elicitation_schema_summary",
                format_schema,
            ),
            patch.object(mcp_tool, "_sanitize_error", sanitize),
            patch(
                "pcbdraft.tools.approval.request_elicitation_consent",
                side_effect=approve,
            ) as consent,
        ):
            result = asyncio.run(handler(None, params))

        self.assertEqual(result.action, "accept")
        self.assertEqual(result.content, {})
        self.assertEqual(consent_contexts, ["captured"])
        self.assertEqual(
            handler.metrics,
            {
                "requests": 1,
                "accepted": 1,
                "declined": 0,
                "errors": 0,
            },
        )
        format_schema.assert_called_once_with(params.requested_schema, "payments")
        sanitize.assert_called_once_with("card details")
        consent.assert_called_once_with(
            "card details",
            "field summary",
            timeout_seconds=8,
            surface="mcp-elicitation/payments",
        )


if __name__ == "__main__":
    unittest.main()
