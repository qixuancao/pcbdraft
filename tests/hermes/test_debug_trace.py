from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core.debug_trace import (
    DebugTraceWriter,
    record_event,
    reset_trace_writer,
    trace_enabled,
    trace_path,
)
from pcbdraft.core.hermes_paths import DEBUG_PLUGIN_DIR_NAME, install_vendor_path
from pcbdraft.interfaces.hermes_cli import install_debug_plugin
from pcbdraft.interfaces.hermes_plugin import register
from pcbdraft.model.hermes_config import write_hermes_config


class FakePluginContext:
    """Minimal stand-in for the Hermes ``PluginContext`` hook registry."""

    def __init__(self) -> None:
        self.hooks: dict[str, list] = {}
        self.middleware: dict[str, list] = {}

    def register_hook(self, hook_name: str, callback) -> None:
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_middleware(self, kind: str, callback) -> None:
        self.middleware.setdefault(kind, []).append(callback)


def _read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class DebugTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_trace_writer()

    def tearDown(self) -> None:
        reset_trace_writer()

    def test_trace_is_enabled_by_default_and_can_be_disabled(self) -> None:
        self.assertTrue(trace_enabled())
        for value in ("0", "off", "false", "no", "OFF"):
            with patch.dict(os.environ, {"PCBDRAFT_DEBUG_TRACE": value}):
                self.assertFalse(trace_enabled())
        with patch.dict(os.environ, {"PCBDRAFT_DEBUG_TRACE": "1"}):
            self.assertTrue(trace_enabled())

    def test_trace_path_honors_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "trace.jsonl"
            with patch.dict(os.environ, {"PCBDRAFT_DEBUG_TRACE_PATH": str(target)}):
                self.assertEqual(trace_path(), target)

    def test_record_event_writes_bounded_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "trace.jsonl"
            with patch.dict(os.environ, {"PCBDRAFT_DEBUG_TRACE_PATH": str(target)}):
                record_event(
                    "model_request",
                    model="test-model",
                    api_key="sk-secret",
                    provider_error=(
                        "Bearer oauth-access-token-123456 refresh_token=refresh-secret-123"
                    ),
                    messages=["hello" * 4000],
                )
                record_event("model_response", reply="ok")
            events = _read_events(target)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["event"], "model_request")
            self.assertEqual(events[0]["data"]["model"], "test-model")
            self.assertEqual(events[0]["data"]["api_key"], "***redacted***")
            self.assertNotIn("oauth-access-token", json.dumps(events[0]))
            self.assertNotIn("refresh-secret", json.dumps(events[0]))
            self.assertIn("truncated", events[0]["data"]["messages"][0])
            self.assertEqual(events[1]["data"]["reply"], "ok")
            for event in events:
                self.assertIn("seq", event)
                self.assertIn("timestamp", event)
                self.assertIn("pid", event)

    def test_disabled_trace_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "trace.jsonl"
            with patch.dict(
                os.environ,
                {
                    "PCBDRAFT_DEBUG_TRACE": "off",
                    "PCBDRAFT_DEBUG_TRACE_PATH": str(target),
                },
            ):
                record_event("model_request", model="test-model")
            self.assertFalse(target.exists())

    def test_writer_rotation_keeps_bounded_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "trace.jsonl"
            writer = DebugTraceWriter(target, max_bytes=64 * 1024, backups=2)
            for index in range(400):
                writer.record("model_request", payload="x" * 1024, index=index)
            self.assertTrue(target.exists())
            self.assertTrue(target.with_name("trace.jsonl.1").exists())
            self.assertTrue(target.with_name("trace.jsonl.2").exists())
            live = _read_events(target)
            self.assertLess(
                sum(len(json.dumps(event)) for event in live), 64 * 1024 + 8192
            )
            rotated = _read_events(target.with_name("trace.jsonl.1"))
            self.assertLess(rotated[0]["data"]["index"], live[0]["data"]["index"])

    def test_writer_never_raises_for_unserializable_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "trace.jsonl"
            writer = DebugTraceWriter(target)

            class Weird:
                def __str__(self) -> str:
                    raise RuntimeError("nope")

            writer.record("model_request", payload=object())
            writer.record("model_response", ok=True)
            events = _read_events(target)
            self.assertEqual(events[-1]["event"], "model_response")

    def test_writer_assigns_unique_sequences_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "trace.jsonl"
            writer = DebugTraceWriter(target)
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(
                    executor.map(
                        lambda index: writer.record("step", index=index), range(100)
                    )
                )
            sequences = [event["seq"] for event in _read_events(target)]
            self.assertEqual(sequences, list(range(1, 101)))


class DebugPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_trace_writer()
        self._temporary = tempfile.TemporaryDirectory()
        self.trace_path = Path(self._temporary.name) / "trace.jsonl"

    def tearDown(self) -> None:
        reset_trace_writer()
        self._temporary.cleanup()

    def _recorded_events(self) -> list[dict]:
        return _read_events(self.trace_path)

    def test_register_subscribes_all_conversation_hooks(self) -> None:
        context = FakePluginContext()
        register(context)
        self.assertEqual(
            set(context.hooks),
            {
                "on_session_start",
                "on_session_end",
                "on_session_finalize",
                "on_session_reset",
                "pre_api_request",
                "post_api_request",
                "api_request_error",
                "pre_tool_call",
                "post_tool_call",
                "post_llm_call",
            },
        )
        self.assertEqual(set(context.middleware), {"tool_execution"})

    def test_middleware_dispatches_only_one_pcb_call_per_provider_response(
        self,
    ) -> None:
        context = FakePluginContext()
        register(context)
        middleware = context.middleware["tool_execution"][0]
        calls: list[dict] = []

        def dispatch(args: dict) -> str:
            calls.append(args)
            return "executed"

        common = {
            "session_id": "session-one-action",
            "turn_id": "turn-1",
            "api_request_id": "turn-1:api:1",
        }
        first = middleware(
            tool_name="pcb_inspect_project",
            args={},
            next_call=dispatch,
            **common,
        )
        second = middleware(
            tool_name="pcb_search_parts",
            args={"query": "LED"},
            next_call=dispatch,
            **common,
        )
        next_decision = middleware(
            tool_name="pcb_search_parts",
            args={"query": "LED"},
            next_call=dispatch,
            **{**common, "api_request_id": "turn-1:api:2"},
        )

        self.assertEqual(first, "executed")
        self.assertTrue(json.loads(second)["blocked"])
        self.assertEqual(next_decision, "executed")
        self.assertEqual(calls, [{}, {"query": "LED"}])

    def test_hooks_forward_full_conversation_step(self) -> None:
        import os as _os

        with patch.dict(
            _os.environ, {"PCBDRAFT_DEBUG_TRACE_PATH": str(self.trace_path)}
        ):
            context = FakePluginContext()
            register(context)
            context.hooks["on_session_start"][0](
                session_id="s1", model="mimo-v2.5", platform="cli"
            )
            context.hooks["pre_api_request"][0](
                turn_id="t1",
                api_request_id="t1:api:1",
                session_id="s1",
                api_call_count=1,
                model="mimo-v2.5",
                provider="custom",
                base_url="https://example.test/v1",
                message_count=2,
                tool_count=8,
                approx_input_tokens=1000,
                retry_count=0,
                request={"method": "POST", "body": {"model": "mimo-v2.5"}},
            )
            context.hooks["post_api_request"][0](
                turn_id="t1",
                api_request_id="t1:api:1",
                session_id="s1",
                api_call_count=1,
                model="mimo-v2.5",
                provider="custom",
                api_duration=0.25,
                finish_reason="tool_calls",
                response={
                    "model": "mimo-v2.5",
                    "assistant_message": {
                        "role": "assistant",
                        "content": "planning",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "pcb_plan_request",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "usage": {"prompt_tokens": 10, "total_tokens": 20},
                },
                usage={"prompt_tokens": 10, "total_tokens": 20},
            )
            context.hooks["post_tool_call"][0](
                tool_name="pcb_plan_request",
                args={"message": "make a board"},
                result='{"ok": true}',
                session_id="s1",
                tool_call_id="call-1",
                turn_id="t1",
                duration_ms=42,
                status="ok",
                error_type=None,
                error_message=None,
            )
            context.hooks["api_request_error"][0](
                turn_id="t1",
                api_request_id="t1:api:2",
                session_id="s1",
                api_call_count=2,
                model="mimo-v2.5",
                provider="custom",
                status_code=429,
                retry_count=1,
                max_retries=3,
                retryable=True,
                reason="rate_limit",
                error={"type": "RateLimitError", "message": "slow down"},
                api_duration=0.1,
            )
            context.hooks["post_llm_call"][0](
                session_id="s1",
                turn_id="t1",
                user_message="make a board",
                assistant_response="here is the plan",
                model="mimo-v2.5",
            )
            context.hooks["on_session_end"][0](
                session_id="s1",
                turn_id="t1",
                completed=True,
                failed=False,
                interrupted=False,
                turn_exit_reason="completed",
                model="mimo-v2.5",
            )
        events = self._recorded_events()
        kinds = [event["event"] for event in events]
        self.assertEqual(
            kinds,
            [
                "plugin_loaded",
                "session_start",
                "model_request",
                "model_response",
                "tool_end",
                "model_error",
                "turn_complete",
                "session_end",
            ],
        )
        request = next(event for event in events if event["event"] == "model_request")
        self.assertEqual(request["data"]["api_call_count"], 1)
        self.assertEqual(request["data"]["request"]["body"]["model"], "mimo-v2.5")
        response = next(event for event in events if event["event"] == "model_response")
        self.assertEqual(
            response["data"]["response"]["assistant_message"]["tool_calls"][0][
                "function"
            ]["name"],
            "pcb_plan_request",
        )
        error = next(event for event in events if event["event"] == "model_error")
        self.assertEqual(error["data"]["http_status"], 429)
        self.assertTrue(error["data"]["retryable"])
        tool = next(event for event in events if event["event"] == "tool_end")
        self.assertEqual(tool["data"]["tool_name"], "pcb_plan_request")
        self.assertEqual(tool["data"]["status"], "ok")


class DebugPluginInstallTests(unittest.TestCase):
    def test_install_debug_plugin_writes_idempotent_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.dict(os.environ, {"PCBDRAFT_HERMES_HOME": str(home)}):
                plugin_dir = install_debug_plugin()
                self.assertEqual(plugin_dir.name, DEBUG_PLUGIN_DIR_NAME)
                manifest = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
                init = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
                self.assertIn("name: pcbdraft-debug", manifest)
                self.assertIn(
                    "from pcbdraft.interfaces.hermes_plugin import register", init
                )
                before = manifest + init
                install_debug_plugin()
                after = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8") + (
                    plugin_dir / "__init__.py"
                ).read_text(encoding="utf-8")
                self.assertEqual(before, after)

    def test_write_hermes_config_enables_debug_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.dict(os.environ, {"PCBDRAFT_HERMES_HOME": str(home)}):
                install_vendor_path()
                os.environ["HERMES_HOME"] = str(home)
                path = write_hermes_config()
                no_provider = path.read_text(encoding="utf-8")
                self.assertIn("plugins:", no_provider)
                self.assertIn(f"- {DEBUG_PLUGIN_DIR_NAME}", no_provider)
                self.assertIn("platform_toolsets:", no_provider)
                self.assertIn("- pcbdraft", no_provider)
                self.assertNotIn("- hermes-cli", no_provider)
                from hermes_cli.config import read_raw_config, save_config

                config = read_raw_config()
                config["model"] = {
                    "provider": "custom",
                    "default": "board-model",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "api_key": "local",
                }
                save_config(config, strip_defaults=False)
                write_hermes_config()
                configured = path.read_text(encoding="utf-8")
            self.assertIn("plugins:", configured)
            self.assertIn(f"- {DEBUG_PLUGIN_DIR_NAME}", configured)
            self.assertIn("default: board-model", configured)


if __name__ == "__main__":
    unittest.main()
