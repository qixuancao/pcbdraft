from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from pcbdraft.tools import (
    mcp_server_task,
    mcp_task_lifecycle,
    mcp_tool,
    mcp_tool_discovery,
)


class _ServerNotification:
    def __init__(self, root):
        self.root = root


class _ToolListChangedNotification:
    pass


class _PromptListChangedNotification:
    pass


class _ResourceListChangedNotification:
    pass


class MCPServerTaskContractTests(unittest.TestCase):
    def test_module_has_no_reverse_import_and_legacy_identity_remains(self):
        source = Path(mcp_server_task.__file__).read_text(encoding="utf-8")
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

        self.assertNotIn("pcbdraft.tools.mcp_tool", imports)
        self.assertIs(mcp_tool.MCPServerTask, mcp_server_task.MCPServerTask)
        self.assertIs(
            inspect.getmodule(mcp_tool.MCPServerTask.run),
            mcp_server_task,
        )
        self.assertIs(
            mcp_tool.MCPServerTask._refresh_tools,
            mcp_tool_discovery._refresh_tools,
        )

    def test_transport_lifecycle_stays_in_existing_mixin(self):
        self.assertIs(
            mcp_tool.MCPServerTask.__mro__[1],
            mcp_task_lifecycle.MCPTaskLifecycleMixin,
        )
        for name in ("start", "shutdown", "_run_stdio", "_run_http"):
            with self.subTest(name=name):
                self.assertNotIn(name, mcp_server_task.MCPServerTask.__dict__)
                self.assertIs(
                    getattr(mcp_server_task.MCPServerTask, name),
                    getattr(mcp_task_lifecycle.MCPTaskLifecycleMixin, name),
                )

    def test_constructor_reads_default_through_legacy_patch_path(self):
        with patch.object(mcp_tool, "_DEFAULT_TOOL_TIMEOUT", 17.0):
            task = mcp_tool.MCPServerTask("docs")

        self.assertEqual(task.name, "docs")
        self.assertEqual(task.tool_timeout, 17.0)
        self.assertEqual(task._mcp_task_lifecycle_runtime(), vars(mcp_tool))


class MCPServerTaskAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_callbacks_resolve_notification_and_logging_hooks_late(self):
        task = mcp_tool.MCPServerTask("docs")
        logging_callback = task._make_logging_callback()
        message_handler = task._make_message_handler()

        with (
            patch.object(mcp_tool, "_MCP_LOG_LEVEL_MAP", {"warning": 37}),
            patch.object(mcp_tool.logger, "log") as log,
        ):
            await logging_callback(
                SimpleNamespace(
                    level="warning",
                    data={"status": "degraded"},
                    logger="sdk",
                )
            )

        log.assert_called_once_with(
            37,
            "MCP server log [%s]: %s",
            "docs/sdk",
            '{"status": "degraded"}',
        )

        with (
            patch.object(mcp_tool, "_MCP_NOTIFICATION_TYPES", True),
            patch.object(mcp_tool, "ServerNotification", _ServerNotification),
            patch.object(
                mcp_tool,
                "ToolListChangedNotification",
                _ToolListChangedNotification,
            ),
            patch.object(
                mcp_tool,
                "PromptListChangedNotification",
                _PromptListChangedNotification,
            ),
            patch.object(
                mcp_tool,
                "ResourceListChangedNotification",
                _ResourceListChangedNotification,
            ),
            patch.object(
                mcp_tool.MCPServerTask,
                "_schedule_tools_refresh",
                return_value=Mock(),
            ) as schedule,
        ):
            await message_handler(_ServerNotification(_ToolListChangedNotification()))

        schedule.assert_called_once_with()

    async def test_run_uses_post_sdk_feature_flags_and_host_handlers(self):
        sampling_instances = []
        elicitation_instances = []

        class Sampling:
            def __init__(self, name, config):
                sampling_instances.append((name, config, self))

        class Elicitation:
            def __init__(self, name, config, *, owner):
                elicitation_instances.append((name, config, owner, self))

        task = mcp_tool.MCPServerTask("docs")

        def ensure_sdk():
            mcp_tool._MCP_SAMPLING_TYPES = True
            mcp_tool._MCP_ELICITATION_TYPES = True

        async def run_stdio(owner, config):
            owner._shutdown_event.set()
            return "shutdown"

        lifecycle_seconds = Mock(return_value=None)
        with (
            patch.object(mcp_tool, "_MCP_SAMPLING_TYPES", False),
            patch.object(mcp_tool, "_MCP_ELICITATION_TYPES", False),
            patch.object(mcp_tool, "_ensure_mcp_sdk", side_effect=ensure_sdk),
            patch.object(mcp_tool, "SamplingHandler", Sampling),
            patch.object(mcp_tool, "ElicitationHandler", Elicitation),
            patch.object(
                mcp_tool,
                "_get_lifecycle_seconds",
                lifecycle_seconds,
            ),
            patch.object(mcp_tool.MCPServerTask, "_run_stdio", new=run_stdio),
        ):
            await task.run(
                {
                    "command": "server",
                    "timeout": 23,
                    "sampling": {"enabled": True},
                    "elicitation": {"enabled": True},
                }
            )

        self.assertEqual(task.tool_timeout, 23)
        self.assertEqual(sampling_instances[0][:2], ("docs", {"enabled": True}))
        self.assertEqual(
            elicitation_instances[0][:3],
            ("docs", {"enabled": True}, task),
        )
        self.assertEqual(
            lifecycle_seconds.call_args_list,
            [
                call(
                    {
                        "command": "server",
                        "timeout": 23,
                        "sampling": {"enabled": True},
                        "elicitation": {"enabled": True},
                    },
                    "idle_timeout_seconds",
                ),
                call(
                    {
                        "command": "server",
                        "timeout": 23,
                        "sampling": {"enabled": True},
                        "elicitation": {"enabled": True},
                    },
                    "max_lifetime_seconds",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
