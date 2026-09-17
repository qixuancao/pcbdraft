"""Focused tests for API hook payloads and debug observability."""

from __future__ import annotations

import ast
import inspect
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.agent import api_hook_observability, loop
from pcbdraft.agent.api_hook_observability import ApiHookObservabilityMixin
from pcbdraft.agent.loop import AIAgent


@dataclass
class _NormalizedUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw_usage: object


class ApiHookObservabilityCompatibilityTests(unittest.TestCase):
    def test_module_does_not_import_legacy_loop(self) -> None:
        module_path = Path(api_hook_observability.__file__)
        tree = ast.parse(module_path.read_text())
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("pcbdraft.agent.loop", imported_modules)

    def test_agent_inherits_extracted_methods_without_wrappers(self) -> None:
        self.assertTrue(issubclass(AIAgent, ApiHookObservabilityMixin))
        for name in (
            "_usage_summary_for_api_request_hook",
            "_hook_payload_max_chars",
            "_is_sensitive_hook_key",
            "_hook_jsonable",
            "_sanitize_hook_payload",
            "_api_request_payload_for_hook",
            "_api_response_payload_for_hook",
            "_invoke_api_request_error_hook",
            "_dump_api_request_debug",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    inspect.getattr_static(AIAgent, name),
                    inspect.getattr_static(ApiHookObservabilityMixin, name),
                )


class ApiHookObservabilityBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)
        self.agent.session_id = "session-1"
        self.agent.platform = "cli"
        self.agent.model = "model-1"
        self.agent.provider = "provider-1"
        self.agent.api_mode = "chat_completions"
        self.agent._base_url = "https://example.invalid/v1"

    def test_request_payload_redacts_nested_secrets_and_bounds_values(self) -> None:
        class ToolCall:
            def model_dump(self, **_kwargs):
                return {"id": "call-1", "vendor_api_key": "nested-secret"}

        with patch.object(loop.os, "getenv", return_value="50000") as env_get:
            payload = self.agent._api_request_payload_for_hook(
                {
                    "api_key": "top-secret",
                    "timeout": 30,
                    "http_client": object(),
                    "headers": {
                        "Authorization": "Bearer secret",
                        "proxy-authorization": "Proxy secret",
                        "safe": "visible",
                    },
                    "tool": ToolCall(),
                    "binary": b"secret bytes",
                    "long": "x" * 9000,
                }
            )

        env_get.assert_called_once_with(
            "PCBDRAFT_RUNTIME_PLUGIN_PAYLOAD_MAX_CHARS", "50000"
        )
        body = payload["body"]
        self.assertEqual(body["api_key"], "<redacted>")
        self.assertEqual(body["headers"]["Authorization"], "<redacted>")
        self.assertEqual(body["headers"]["proxy-authorization"], "<redacted>")
        self.assertEqual(body["headers"]["safe"], "visible")
        self.assertEqual(body["tool"]["vendor_api_key"], "<redacted>")
        self.assertEqual(body["binary"], "<12 bytes>")
        self.assertIn("[truncated 1000 chars]", body["long"])
        self.assertNotIn("timeout", body)
        self.assertNotIn("http_client", body)

    def test_usage_summary_preserves_legacy_normalizer_patch_path(self) -> None:
        normalized = _NormalizedUsage(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            raw_usage={"private": "provider payload"},
        )
        response = SimpleNamespace(usage={"input_tokens": 11})

        with patch.object(
            loop, "normalize_usage", return_value=normalized
        ) as normalize:
            summary = self.agent._usage_summary_for_api_request_hook(response)

        normalize.assert_called_once_with(
            response.usage,
            provider="provider-1",
            api_mode="chat_completions",
        )
        self.assertEqual(
            summary,
            {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        )

    def test_error_hook_callback_is_sanitized_patchable_and_best_effort(self) -> None:
        from pcbdraft.interfaces.tui import lifecycle

        with (
            patch.object(lifecycle, "has_hook", return_value=True) as has_hook,
            patch.object(lifecycle, "invoke_hook") as invoke_hook,
            patch.object(loop.time, "time", return_value=25.0) as now,
        ):
            self.agent._invoke_api_request_error_hook(
                task_id="task-1",
                turn_id="turn-1",
                api_request_id="request-1",
                api_call_count=3,
                api_start_time=20.0,
                api_kwargs={"api_key": "private", "messages": ["hello"]},
                error_type="timeout",
                error_message="provider timed out",
                status_code=504,
                retryable=True,
                reason="stale",
            )

            invoke_hook.side_effect = RuntimeError("plugin failed")
            self.agent._invoke_api_request_error_hook(
                task_id="task-1",
                turn_id="turn-2",
                api_request_id="request-2",
                api_call_count=4,
                api_start_time=20.0,
                api_kwargs={"api_key": "still-private"},
                error_type="plugin_error",
                error_message="ignored",
            )

        self.assertEqual(has_hook.call_count, 2)
        self.assertEqual(now.call_count, 2)
        first_call = invoke_hook.call_args_list[0]
        self.assertEqual(first_call.args, ("api_request_error",))
        self.assertEqual(first_call.kwargs["api_duration"], 5.0)
        self.assertEqual(first_call.kwargs["request"]["body"]["api_key"], "<redacted>")
        self.assertEqual(
            first_call.kwargs["error"],
            {"type": "timeout", "message": "provider timed out"},
        )

    def test_debug_dump_keeps_shared_helper_patch_path(self) -> None:
        expected = Path("/tmp/request-debug.json")
        error = RuntimeError("failed")
        with patch(
            "pcbdraft.agent.agent_runtime_helpers.dump_api_request_debug",
            return_value=expected,
        ) as dump:
            result = self.agent._dump_api_request_debug(
                {"messages": ["hello"]},
                reason="preflight",
                error=error,
            )

        self.assertEqual(result, expected)
        dump.assert_called_once_with(
            self.agent,
            {"messages": ["hello"]},
            reason="preflight",
            error=error,
        )


if __name__ == "__main__":
    unittest.main()
