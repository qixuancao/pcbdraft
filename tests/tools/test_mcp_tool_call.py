"""Focused tests for the generic MCP tool-call transaction boundary."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pcbdraft.tools import mcp_tool, mcp_tool_call


class _AsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


def _run_coroutine_factory(factory, *, timeout):
    del timeout
    return asyncio.run(factory())


def _server_with_result(result):
    return SimpleNamespace(
        session=SimpleNamespace(call_tool=AsyncMock(return_value=result)),
        _rpc_lock=_AsyncLock(),
        _pending_call_context=None,
        _mark_session_proven=Mock(),
    )


def _error_json(message: str) -> str:
    return json.dumps({"error": message})


class MCPToolCallCompatibilityTests(unittest.TestCase):
    def test_module_has_no_reverse_import_and_legacy_identity_remains(self) -> None:
        source = Path(mcp_tool_call.__file__).read_text(encoding="utf-8")
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
        self.assertIs(mcp_tool._make_tool_handler, mcp_tool_call._make_tool_handler)
        for host_symbol in (
            "_get_connected_server_for_call",
            "_handle_auth_error_and_retry",
            "_handle_session_expired_and_retry",
            "_bump_server_error",
            "_reset_server_error",
        ):
            with self.subTest(host_symbol=host_symbol):
                self.assertIs(
                    inspect.getmodule(getattr(mcp_tool, host_symbol)), mcp_tool
                )
        self.assertFalse(hasattr(mcp_tool_call, "_server_error_counts"))
        self.assertFalse(hasattr(mcp_tool_call, "_servers"))

    def test_legacy_trust_and_breaker_patch_paths_short_circuit_transport(self) -> None:
        connected = Mock()
        with (
            patch.object(mcp_tool, "_trust_gate_check", return_value="blocked") as gate,
            patch.object(mcp_tool, "_get_connected_server_for_call", connected),
        ):
            result = mcp_tool._make_tool_handler("docs", "write", 7)({"x": 1})

        self.assertEqual(result, "blocked")
        gate.assert_called_once_with("docs", "write")
        connected.assert_not_called()

        connected.reset_mock()
        with (
            patch.object(mcp_tool, "_trust_gate_check", return_value=None),
            patch.object(mcp_tool, "_server_error_counts", {"docs": 3}),
            patch.object(mcp_tool, "_server_breaker_opened_at", {"docs": 95.0}),
            patch.object(mcp_tool, "_CIRCUIT_BREAKER_THRESHOLD", 3),
            patch.object(mcp_tool, "_CIRCUIT_BREAKER_COOLDOWN_SEC", 10.0),
            patch.object(mcp_tool.time, "monotonic", return_value=100.0),
            patch.object(mcp_tool, "tool_error", side_effect=_error_json),
            patch.object(mcp_tool, "_get_connected_server_for_call", connected),
        ):
            result = mcp_tool._make_tool_handler("docs", "read", 7)({})

        self.assertIn("Auto-retry available in ~5s", json.loads(result)["error"])
        connected.assert_not_called()


class MCPToolCallTransactionTests(unittest.TestCase):
    def test_successful_rpc_projects_content_metadata_and_resets_breaker(self) -> None:
        text = SimpleNamespace(type="text", text="hello")
        image = SimpleNamespace(type="image", data="image")
        audio = SimpleNamespace(type="audio", data="audio")
        resource = SimpleNamespace(type="resource", resource=object())
        result = SimpleNamespace(
            is_error=False,
            content=[text, image, audio, resource],
            structured_content={"count": 1},
            meta={"plain": 2},
        )
        server = _server_with_result(result)

        with (
            patch.object(mcp_tool, "_trust_gate_check", return_value=None),
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(mcp_tool, "_mark_server_call_started") as mark_started,
            patch.object(
                mcp_tool,
                "_run_on_mcp_loop",
                side_effect=_run_coroutine_factory,
            ),
            patch.object(
                mcp_tool,
                "strip_unicode_tags",
                side_effect=lambda value: f"clean:{value}",
            ),
            patch.object(
                mcp_tool,
                "_cache_mcp_image_block",
                side_effect=lambda block: "MEDIA:image" if block is image else "",
            ),
            patch.object(
                mcp_tool,
                "_cache_mcp_audio_block",
                side_effect=lambda block: "MEDIA:audio" if block is audio else "",
            ),
            patch.object(
                mcp_tool,
                "_render_mcp_resource_block",
                side_effect=lambda block, _server: (
                    "resource" if block is resource else ""
                ),
            ),
            patch.object(
                mcp_tool,
                "_strip_reserved_meta_keys",
                return_value={"visible": 2},
            ) as strip_meta,
            patch.object(mcp_tool, "_reset_server_error") as reset,
            patch.object(mcp_tool, "_bump_server_error") as bump,
        ):
            rendered = mcp_tool._make_tool_handler("docs", "search", 9)(
                {"query": "pcb"},
                ignored=True,
            )

        self.assertEqual(
            json.loads(rendered),
            {
                "result": "clean:hello\nMEDIA:image\nMEDIA:audio\nresource",
                "structuredContent": {"count": 1},
                "_meta": {"visible": 2},
            },
        )
        server.session.call_tool.assert_awaited_once_with(
            "search", arguments={"query": "pcb"}
        )
        mark_started.assert_called_once_with(server)
        server._mark_session_proven.assert_called_once_with()
        self.assertIsNone(server._pending_call_context)
        strip_meta.assert_called_once_with({"plain": 2})
        reset.assert_called_once_with("docs")
        bump.assert_not_called()

    def test_tool_error_is_sanitized_and_counts_as_server_failure(self) -> None:
        result = SimpleNamespace(
            isError=True,
            content=[
                SimpleNamespace(text="secret"),
                SimpleNamespace(text=None, resource=SimpleNamespace(text=" detail")),
            ],
        )
        server = _server_with_result(result)

        with (
            patch.object(mcp_tool, "_trust_gate_check", return_value=None),
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(
                mcp_tool,
                "_run_on_mcp_loop",
                side_effect=_run_coroutine_factory,
            ),
            patch.object(
                mcp_tool,
                "_sanitize_error",
                side_effect=lambda value: f"safe:{value}",
            ) as sanitize,
            patch.object(mcp_tool, "tool_error", side_effect=_error_json),
            patch.object(mcp_tool, "_reset_server_error") as reset,
            patch.object(mcp_tool, "_bump_server_error") as bump,
        ):
            rendered = mcp_tool._make_tool_handler("docs", "search", 9)({})

        self.assertEqual(json.loads(rendered), {"error": "safe:secret detail"})
        sanitize.assert_called_once_with("secret detail")
        bump.assert_called_once_with("docs")
        reset.assert_not_called()

    def test_disconnected_server_requests_reconnect_through_host_hooks(self) -> None:
        server = SimpleNamespace(session=None)
        with (
            patch.object(mcp_tool, "_trust_gate_check", return_value=None),
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(
                mcp_tool,
                "_wait_for_server_session_ready",
                return_value=False,
            ) as wait_ready,
            patch.object(mcp_tool, "_signal_reconnect", return_value=True) as signal,
            patch.object(mcp_tool, "_bump_server_error") as bump,
            patch.object(mcp_tool, "tool_error", side_effect=_error_json),
        ):
            rendered = mcp_tool._make_tool_handler("docs", "search", 3)({})

        self.assertIn("reconnect requested", json.loads(rendered)["error"])
        wait_ready.assert_called_once_with(server, timeout=3.0)
        signal.assert_called_once_with(server)
        bump.assert_called_once_with("docs")

    def test_interrupt_and_recovery_paths_use_legacy_host_hooks(self) -> None:
        server = _server_with_result(None)
        handler = mcp_tool._make_tool_handler("docs", "search", 9)

        with (
            patch.object(mcp_tool, "_trust_gate_check", return_value=None),
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(
                mcp_tool,
                "_run_on_mcp_loop",
                side_effect=InterruptedError,
            ),
            patch.object(
                mcp_tool,
                "_interrupted_call_result",
                return_value="interrupted",
            ) as interrupted,
        ):
            self.assertEqual(handler({}), "interrupted")
        interrupted.assert_called_once_with()

        failure = RuntimeError("expired")
        with (
            patch.object(mcp_tool, "_trust_gate_check", return_value=None),
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(mcp_tool, "_run_on_mcp_loop", side_effect=failure),
            patch.object(
                mcp_tool,
                "_handle_auth_error_and_retry",
                return_value=None,
            ) as auth,
            patch.object(
                mcp_tool,
                "_handle_session_expired_and_retry",
                return_value="session-recovered",
            ) as expired,
            patch.object(mcp_tool, "_bump_server_error") as bump,
        ):
            self.assertEqual(handler({}), "session-recovered")

        auth.assert_called_once()
        expired.assert_called_once()
        self.assertIs(auth.call_args.args[1], failure)
        self.assertIs(expired.call_args.args[1], failure)
        bump.assert_not_called()

    def test_generic_failure_is_redacted_after_recovery_declines(self) -> None:
        server = _server_with_result(None)
        failure = RuntimeError("raw secret")
        with (
            patch.object(mcp_tool, "_trust_gate_check", return_value=None),
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(mcp_tool, "_run_on_mcp_loop", side_effect=failure),
            patch.object(
                mcp_tool,
                "_handle_auth_error_and_retry",
                return_value=None,
            ),
            patch.object(
                mcp_tool,
                "_handle_session_expired_and_retry",
                return_value=None,
            ),
            patch.object(mcp_tool, "_exc_str", return_value="rendered") as exc_str,
            patch.object(
                mcp_tool,
                "_sanitize_error",
                return_value="safe failure",
            ) as sanitize,
            patch.object(mcp_tool, "tool_error", side_effect=_error_json),
            patch.object(mcp_tool, "_bump_server_error") as bump,
            patch.object(mcp_tool.logger, "error") as log_error,
        ):
            rendered = mcp_tool._make_tool_handler("docs", "search", 9)({})

        self.assertEqual(json.loads(rendered), {"error": "safe failure"})
        exc_str.assert_called_once_with(failure)
        sanitize.assert_called_once_with("MCP call failed: RuntimeError: rendered")
        bump.assert_called_once_with("docs")
        log_error.assert_called_once_with(
            "MCP tool %s/%s call failed: %s",
            "docs",
            "search",
            failure,
        )


if __name__ == "__main__":
    unittest.main()
