from __future__ import annotations

import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.tools import mcp_connection_recovery, mcp_tool


class MCPConnectionRecoveryCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_symbols_remain(self):
        source = Path(mcp_connection_recovery.__file__).read_text(encoding="utf-8")
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
            "_record_connect_failure",
            "_clear_connect_failure",
            "_connect_cooldown_active",
            "_normalize_server_trust",
            "_annotation_read_only_hint",
            "_record_tool_trust_metadata",
            "_trust_gate_check",
            "_bump_server_error",
            "_reset_server_error",
            "_signal_reconnect",
            "reconnect_mcp_server",
            "_wait_for_server_session_ready",
            "_signal_reconnect_and_wait",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(mcp_tool, name)))

    def test_legacy_cooldown_wrappers_use_live_state_constants_and_clock(self):
        with (
            patch.object(mcp_tool, "_server_connect_failures", {}),
            patch.object(mcp_tool, "_server_connect_retry_after", {}),
            patch.object(mcp_tool, "_CONNECT_RETRY_BASE_BACKOFF_SEC", 2.0),
            patch.object(mcp_tool, "_CONNECT_RETRY_MAX_BACKOFF_SEC", 5.0),
            patch.object(mcp_tool.time, "monotonic", return_value=100.0),
        ):
            mcp_tool._record_connect_failure("docs")
            mcp_tool._record_connect_failure("docs")
            self.assertEqual(mcp_tool._server_connect_failures, {"docs": 2})
            self.assertEqual(mcp_tool._server_connect_retry_after, {"docs": 104.0})
            self.assertTrue(mcp_tool._connect_cooldown_active("docs"))

            mcp_tool._clear_connect_failure("docs")
            self.assertEqual(mcp_tool._server_connect_failures, {})
            self.assertEqual(mcp_tool._server_connect_retry_after, {})

    def test_legacy_metadata_wrapper_resolves_classifier_patch_paths(self):
        tools = [SimpleNamespace(name="read"), SimpleNamespace(name="write")]
        with (
            patch.object(mcp_tool, "_server_trust_levels", {}),
            patch.object(mcp_tool, "_tool_read_only_hints", {}),
            patch.object(
                mcp_tool,
                "_normalize_server_trust",
                return_value="patched-trust",
            ) as normalize,
            patch.object(
                mcp_tool,
                "_annotation_read_only_hint",
                side_effect=[True, False],
            ) as annotation,
        ):
            mcp_tool._record_tool_trust_metadata(
                "docs",
                {"trust": "full"},
                tools,
            )
            self.assertEqual(
                mcp_tool._server_trust_levels,
                {"docs": "patched-trust"},
            )
            self.assertEqual(
                mcp_tool._tool_read_only_hints,
                {"docs": {"read": True, "write": False}},
            )

        normalize.assert_called_once_with("full")
        self.assertEqual(annotation.call_args_list[0].args, (tools[0],))
        self.assertEqual(annotation.call_args_list[1].args, (tools[1],))

    def test_legacy_trust_gate_uses_approval_and_error_patch_paths(self):
        with (
            patch.object(mcp_tool, "_server_trust_levels", {"docs": "untrusted"}),
            patch.object(
                mcp_tool,
                "_tool_read_only_hints",
                {"docs": {"write": False}},
            ),
            patch(
                "pcbdraft.tools.approval.request_elicitation_consent",
                return_value="deny",
            ) as consent,
            patch.object(
                mcp_tool,
                "tool_error",
                side_effect=lambda message: f"blocked:{message}",
            ) as error_factory,
        ):
            result = mcp_tool._trust_gate_check("docs", "write")

        self.assertIn("blocked:The user did not approve", result)
        self.assertEqual(consent.call_args.kwargs["surface"], "mcp-trust/docs")
        error_factory.assert_called_once()

    def test_legacy_breaker_wrappers_use_live_threshold_state_and_clock(self):
        with (
            patch.object(mcp_tool, "_server_error_counts", {}),
            patch.object(mcp_tool, "_server_breaker_opened_at", {}),
            patch.object(mcp_tool, "_CIRCUIT_BREAKER_THRESHOLD", 2),
            patch.object(mcp_tool.time, "monotonic", return_value=55.0),
        ):
            mcp_tool._bump_server_error("docs")
            self.assertEqual(mcp_tool._server_breaker_opened_at, {})
            mcp_tool._bump_server_error("docs")
            self.assertEqual(mcp_tool._server_error_counts, {"docs": 2})
            self.assertEqual(mcp_tool._server_breaker_opened_at, {"docs": 55.0})

            mcp_tool._reset_server_error("docs")
            self.assertEqual(mcp_tool._server_error_counts, {"docs": 0})
            self.assertEqual(mcp_tool._server_breaker_opened_at, {})

    def test_legacy_reconnect_lookup_uses_patched_signal(self):
        server = object()
        with (
            patch.object(mcp_tool, "_servers", {"docs": server}),
            patch.object(mcp_tool, "_signal_reconnect", return_value=True) as signal,
        ):
            self.assertTrue(mcp_tool.reconnect_mcp_server("docs"))
            self.assertFalse(mcp_tool.reconnect_mcp_server("missing"))
        signal.assert_called_once_with(server)

    def test_legacy_signal_routes_async_event_through_live_loop(self):
        event = asyncio.Event()
        loop = Mock()
        loop.is_running.return_value = True
        server = SimpleNamespace(_reconnect_event=event)
        with patch.object(mcp_tool, "_mcp_loop", loop):
            self.assertTrue(mcp_tool._signal_reconnect(server))
        loop.call_soon_threadsafe.assert_called_once_with(event.set)

    def test_legacy_waiter_uses_patched_sleep(self):
        old_session = object()
        new_session = object()
        server = SimpleNamespace(
            session=old_session,
            _ready=SimpleNamespace(is_set=lambda: True),
        )

        def advance(_interval):
            server.session = new_session

        with patch.object(mcp_tool.time, "sleep", side_effect=advance) as sleep:
            self.assertTrue(
                mcp_tool._wait_for_server_session_ready(
                    server,
                    old_session=old_session,
                    timeout=0.5,
                )
            )
        sleep.assert_called_once_with(0.25)

    def test_legacy_signal_and_wait_uses_patched_waiter(self):
        old_session = object()
        ready = Mock()
        reconnect_event = Mock()
        server = SimpleNamespace(
            session=old_session,
            _ready=ready,
            _reconnect_event=reconnect_event,
        )
        loop = Mock()
        loop.is_running.return_value = True
        loop.call_soon_threadsafe.side_effect = lambda callback: callback()

        with (
            patch.object(mcp_tool, "_mcp_loop", loop),
            patch.object(
                mcp_tool,
                "_wait_for_server_session_ready",
                return_value=True,
            ) as waiter,
        ):
            self.assertTrue(
                mcp_tool._signal_reconnect_and_wait(
                    "docs",
                    server,
                    op_description="test",
                    timeout=3.0,
                )
            )

        ready.clear.assert_called_once_with()
        reconnect_event.set.assert_called_once_with()
        waiter.assert_called_once_with(
            server,
            old_session=old_session,
            timeout=3.0,
        )


class MCPConnectionRecoveryTests(unittest.TestCase):
    def test_trust_normalization_and_read_only_hint_fail_closed(self):
        warning = Mock()
        self.assertEqual(
            mcp_connection_recovery._normalize_server_trust(
                None,
                warning=warning,
            ),
            "full",
        )
        self.assertEqual(
            mcp_connection_recovery._normalize_server_trust(
                " typo ",
                warning=warning,
            ),
            "untrusted",
        )
        warning.assert_called_once()

        self.assertTrue(
            mcp_connection_recovery._annotation_read_only_hint(
                SimpleNamespace(annotations={"readOnlyHint": True})
            )
        )
        self.assertFalse(
            mcp_connection_recovery._annotation_read_only_hint(
                SimpleNamespace(annotations={"readOnlyHint": 1})
            )
        )


if __name__ == "__main__":
    unittest.main()
