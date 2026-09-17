from __future__ import annotations

import ast
import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.tools import mcp_runtime_loop, mcp_tool


class MCPRuntimeLoopCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_symbols_remain(self):
        source = Path(mcp_runtime_loop.__file__).read_text(encoding="utf-8")
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

        names = (
            "_LockCookie",
            "_acquire_lock_on_fh",
            "_try_acquire_mcp_discovery_lock",
            "_snapshot_child_pids",
            "_filter_mcp_children",
            "_mcp_loop_exception_handler",
            "_ensure_mcp_loop",
            "_wrap_with_home_override",
            "_wrap_with_dashboard_oauth_flow",
            "_run_on_mcp_loop",
            "_interrupted_call_result",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(mcp_tool, name)))
        self.assertIs(mcp_tool._LockCookie, mcp_runtime_loop._LockCookie)

    def test_legacy_discovery_lock_wrapper_resolves_patched_hooks(self):
        class PatchedCookie:
            def __init__(self, fh):
                self.fh = fh

        with tempfile.TemporaryDirectory() as temporary:
            lock_path = str(Path(temporary) / "discovery.lock")
            with (
                patch.object(mcp_tool, "_MCP_DISCOVERY_LOCK_PATH", lock_path),
                patch.object(
                    mcp_tool,
                    "_acquire_lock_on_fh",
                    return_value=True,
                ) as acquire,
                patch.object(mcp_tool, "_LockCookie", PatchedCookie),
            ):
                cookie = mcp_tool._try_acquire_mcp_discovery_lock()

            self.assertIsInstance(cookie, PatchedCookie)
            acquire.assert_called_once_with(cookie.fh)
            self.assertFalse(cookie.fh.closed)
            cookie.fh.close()

    def test_legacy_ensure_loop_uses_patched_factories_and_handler(self):
        loop = Mock()
        loop.is_running.return_value = False
        thread = Mock()
        handler = Mock()

        with (
            patch.object(mcp_tool, "_mcp_loop", None),
            patch.object(mcp_tool, "_mcp_thread", None),
            patch.object(mcp_tool, "_mcp_loop_exception_handler", handler),
            patch.object(mcp_tool.asyncio, "new_event_loop", return_value=loop),
            patch.object(mcp_tool.threading, "Thread", return_value=thread) as factory,
        ):
            mcp_tool._ensure_mcp_loop()
            self.assertIs(mcp_tool._mcp_loop, loop)
            self.assertIs(mcp_tool._mcp_thread, thread)

        loop.set_exception_handler.assert_called_once_with(handler)
        factory.assert_called_once_with(
            target=loop.run_forever,
            name="mcp-event-loop",
            daemon=True,
        )
        thread.start.assert_called_once_with()

    def test_legacy_run_loop_uses_patched_context_wrappers(self):
        async def operation():
            return "unused"

        loop = Mock()
        loop.is_running.return_value = True
        future: concurrent.futures.Future[str] = concurrent.futures.Future()
        future.set_result("complete")
        scheduled = []

        def schedule(coro, scheduled_loop, **kwargs):
            scheduled.append((coro, scheduled_loop, kwargs))
            coro.close()
            return future

        coro = operation()
        with (
            patch.object(mcp_tool, "_mcp_loop", loop),
            patch.object(
                mcp_tool,
                "_wrap_with_home_override",
                side_effect=lambda value: value,
            ) as home_wrapper,
            patch.object(
                mcp_tool,
                "_wrap_with_dashboard_oauth_flow",
                side_effect=lambda value: value,
            ) as oauth_wrapper,
            patch(
                "pcbdraft.agent.async_utils.safe_schedule_threadsafe",
                side_effect=schedule,
            ),
            patch("pcbdraft.tools.interrupt.is_interrupted", return_value=False),
        ):
            self.assertEqual(mcp_tool._run_on_mcp_loop(coro), "complete")

        home_wrapper.assert_called_once_with(coro)
        oauth_wrapper.assert_called_once_with(coro)
        self.assertIs(scheduled[0][1], loop)
        self.assertIs(scheduled[0][2]["logger"], mcp_tool.logger)

    def test_legacy_interrupted_result_uses_patched_error_factory(self):
        with patch.object(
            mcp_tool,
            "tool_error",
            side_effect=lambda message: f"patched:{message}",
        ) as error_factory:
            result = mcp_tool._interrupted_call_result()

        self.assertEqual(
            result,
            "patched:MCP call interrupted: user sent a new message",
        )
        error_factory.assert_called_once_with(
            "MCP call interrupted: user sent a new message"
        )


class MCPRuntimeLoopTests(unittest.TestCase):
    def test_child_filter_excludes_markers_and_raced_processes(self):
        processes = {
            10: ["python", "-m", "tui_gateway.slash_worker"],
            20: ["node", "mcp-server.js"],
        }

        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        def process(pid):
            if pid == 30:
                raise NoSuchProcess
            return SimpleNamespace(cmdline=lambda: processes[pid])

        fake_psutil = SimpleNamespace(
            Process=process,
            NoSuchProcess=NoSuchProcess,
            AccessDenied=AccessDenied,
        )
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            self.assertEqual(
                mcp_runtime_loop._filter_mcp_children({10, 20, 30}),
                {20},
            )

    def test_loop_exception_handler_only_suppresses_closed_loop_race(self):
        loop = Mock()
        mcp_runtime_loop._mcp_loop_exception_handler(
            loop,
            {"exception": RuntimeError("Event loop is closed")},
        )
        loop.default_exception_handler.assert_not_called()

        context = {"exception": RuntimeError("other failure")}
        mcp_runtime_loop._mcp_loop_exception_handler(loop, context)
        loop.default_exception_handler.assert_called_once_with(context)

    def test_unavailable_loop_closes_prebuilt_coroutine(self):
        async def operation():
            return None

        coro = operation()
        with self.assertRaisesRegex(RuntimeError, "event loop is not running"):
            mcp_runtime_loop._run_on_mcp_loop(
                coro,
                loop=None,
                schedule_threadsafe=Mock(),
                is_interrupted=lambda: False,
            )
        self.assertIsNone(coro.cr_frame)


if __name__ == "__main__":
    unittest.main()
