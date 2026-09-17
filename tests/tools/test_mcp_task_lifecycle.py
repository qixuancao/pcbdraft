from __future__ import annotations

import inspect
import unittest
from contextlib import ExitStack
from pathlib import Path
from threading import RLock
from unittest.mock import AsyncMock, MagicMock, patch

from pcbdraft.tools import mcp_task_lifecycle, mcp_tool


class _AsyncContext:
    def __init__(self, value):
        self.value = value
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class _FakeSession:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True

    async def initialize(self):
        return object()


class MCPTaskLifecycleContractTests(unittest.TestCase):
    def test_extracted_module_does_not_reverse_import_compatibility_module(self):
        source = Path(mcp_task_lifecycle.__file__).read_text(encoding="utf-8")

        self.assertNotIn("import mcp_tool", source)
        self.assertNotIn("from pcbdraft.tools.mcp_tool", source)

    def test_public_task_identity_and_method_ownership_are_preserved(self):
        task = mcp_tool.MCPServerTask("identity")

        self.assertIs(type(task), mcp_tool.MCPServerTask)
        self.assertIs(
            mcp_tool.MCPServerTask.start,
            mcp_task_lifecycle.MCPTaskLifecycleMixin.start,
        )
        self.assertIs(
            mcp_tool.MCPServerTask._run_stdio,
            mcp_task_lifecycle.MCPTaskLifecycleMixin._run_stdio,
        )
        self.assertIs(
            mcp_tool.MCPServerTask._run_http,
            mcp_task_lifecycle.MCPTaskLifecycleMixin._run_http,
        )
        self.assertEqual(inspect.getmodule(task.start), mcp_task_lifecycle)
        self.assertEqual(inspect.getmodule(task.run), mcp_tool)
        self.assertEqual(inspect.getmodule(task._discover_tools), mcp_tool)

    def test_constructor_reads_legacy_module_patch_path_late(self):
        with patch.object(mcp_tool, "_DEFAULT_TOOL_TIMEOUT", 17.0):
            task = mcp_tool.MCPServerTask("patched")

        self.assertEqual(task.tool_timeout, 17.0)


class MCPTaskLifecycleAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_shutdown_drive_a_real_background_lifecycle(self):
        class MinimalTask(mcp_tool.MCPServerTask):
            __slots__ = ()

            async def run(self, config):
                self._config = config
                self._ready.set()
                await self._shutdown_event.wait()

        task = MinimalTask("minimal")

        await task.start({"command": "unused"})
        self.assertIsNotNone(task._task)
        self.assertFalse(task._task.done())

        await task.shutdown()
        self.assertTrue(task._shutdown_event.is_set())
        self.assertTrue(task._task.done())
        self.assertIsNone(task.session)

    async def test_stdio_transport_uses_legacy_patch_paths_and_closes(self):
        task = mcp_tool.MCPServerTask("stdio")
        transport = _AsyncContext((object(), object()))
        stdio_client = MagicMock(return_value=transport)
        sessions: list[_FakeSession] = []
        stdio_pids: dict[int, str] = {}
        stdio_pgids: dict[int, int] = {}
        orphan_pids: set[int] = set()
        orphan_servers: dict[int, str] = {}

        def session_factory(*args, **kwargs):
            session = _FakeSession(*args, **kwargs)
            sessions.append(session)
            return session

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "pcbdraft.tools.osv_check.check_package_for_malware",
                    return_value=None,
                )
            )
            stack.enter_context(
                patch.object(mcp_tool, "_ensure_mcp_sdk", return_value=True)
            )
            stack.enter_context(
                patch.object(mcp_tool, "_build_safe_env", return_value={"PATH": "/bin"})
            )
            stack.enter_context(
                patch.object(
                    mcp_tool,
                    "_resolve_stdio_command",
                    return_value=("python", {"PATH": "/bin"}),
                )
            )
            stack.enter_context(
                patch.object(
                    mcp_tool,
                    "_wrap_command_with_watchdog",
                    side_effect=lambda command, args: (command, args),
                )
            )
            for name, value in (
                ("StdioServerParameters", MagicMock()),
                ("_MCP_NOTIFICATION_TYPES", False),
                ("_MCP_MESSAGE_HANDLER_SUPPORTED", False),
                ("_MCP_LOGGING_CALLBACK_SUPPORTED", False),
                ("_kill_orphaned_mcp_children", MagicMock()),
                ("_write_stderr_log_header", MagicMock()),
                ("_get_mcp_stderr_log", MagicMock(return_value=object())),
                ("stdio_client", stdio_client),
                ("ClientSession", session_factory),
                ("_pcbdraft_mcp_client_info", MagicMock(return_value=object())),
                ("_reset_server_error", MagicMock()),
                ("_lock", RLock()),
                ("_stdio_pids", stdio_pids),
                ("_stdio_pgids", stdio_pgids),
                ("_orphan_stdio_pids", orphan_pids),
                ("_orphan_stdio_pid_servers", orphan_servers),
            ):
                stack.enter_context(patch.object(mcp_tool, name, value, create=True))
            stack.enter_context(
                patch.object(
                    mcp_tool,
                    "_snapshot_child_pids",
                    side_effect=[set(), {321}],
                )
            )
            stack.enter_context(
                patch.object(
                    mcp_tool, "_filter_mcp_children", side_effect=lambda pids: pids
                )
            )
            stack.enter_context(patch.object(mcp_tool.os, "getpgid", return_value=9321))
            stack.enter_context(
                patch("pcbdraft.core.runtime_process._pid_exists", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    mcp_tool.MCPServerTask,
                    "_discover_tools",
                    new=AsyncMock(),
                )
            )
            stack.enter_context(
                patch.object(
                    mcp_tool.MCPServerTask,
                    "_wait_for_lifecycle_event",
                    new=AsyncMock(return_value="shutdown"),
                )
            )
            reason = await task._run_stdio(
                {"command": "python", "args": ["server.py"], "connect_timeout": 1}
            )

        self.assertEqual(reason, "shutdown")
        self.assertTrue(transport.entered)
        self.assertTrue(transport.exited)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].entered)
        self.assertTrue(sessions[0].exited)
        self.assertNotIn(321, stdio_pids)
        self.assertEqual(stdio_pgids[321], 9321)
        self.assertEqual(orphan_pids, {321})
        self.assertEqual(orphan_servers, {321: "stdio"})
        stdio_client.assert_called_once()

    async def test_sse_transport_uses_legacy_patch_paths_and_closes(self):
        task = mcp_tool.MCPServerTask("sse")
        task._auth_type = ""
        transport = _AsyncContext((object(), object()))
        sse_client = MagicMock(return_value=transport)
        sessions: list[_FakeSession] = []

        def session_factory(*args, **kwargs):
            session = _FakeSession(*args, **kwargs)
            sessions.append(session)
            return session

        with (
            patch.object(mcp_tool, "_ensure_mcp_sdk", return_value=True),
            patch.object(mcp_tool, "_MCP_HTTP_AVAILABLE", True),
            patch.object(
                mcp_tool,
                "_apply_identity_header",
                side_effect=lambda _name, _config, headers: headers,
            ),
            patch.object(mcp_tool, "LATEST_HANDSHAKE_VERSION", "2025-test"),
            patch.object(mcp_tool, "_resolve_client_cert", return_value=None),
            patch.object(mcp_tool, "_MCP_NOTIFICATION_TYPES", False),
            patch.object(mcp_tool, "_MCP_MESSAGE_HANDLER_SUPPORTED", False),
            patch.object(mcp_tool, "_MCP_LOGGING_CALLBACK_SUPPORTED", False),
            patch.object(mcp_tool, "sse_client", sse_client, create=True),
            patch.object(mcp_tool, "ClientSession", session_factory, create=True),
            patch.object(mcp_tool, "_pcbdraft_mcp_client_info", return_value=object()),
            patch.object(mcp_tool, "_reset_server_error", MagicMock()),
            patch.object(
                mcp_tool.MCPServerTask,
                "_discover_tools",
                new=AsyncMock(),
            ),
            patch.object(
                mcp_tool.MCPServerTask,
                "_wait_for_lifecycle_event",
                new=AsyncMock(return_value="shutdown"),
            ),
        ):
            reason = await task._run_http(
                {
                    "url": "https://example.test/sse",
                    "transport": "sse",
                    "connect_timeout": 1,
                }
            )

        self.assertEqual(reason, "shutdown")
        self.assertTrue(transport.entered)
        self.assertTrue(transport.exited)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].entered)
        self.assertTrue(sessions[0].exited)
        sse_client.assert_called_once()


if __name__ == "__main__":
    unittest.main()
