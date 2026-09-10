from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent.observability import (
    _clear_session_decisions,
    _context_quality_metrics,
    _cost_metrics,
    _project_pcb_tool_schemas,
    register,
)
from pcbdraft.core.debug_trace import (
    DebugTraceWriter,
    record_event,
    reset_trace_writer,
    trace_enabled,
    trace_path,
)
from pcbdraft.model.settings import write_runtime_config


class FakePluginContext:
    """Minimal stand-in for the PCBDraft ``PluginContext`` hook registry."""

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

    def test_structured_drc_pass_with_null_error_is_not_observed_as_error(
        self,
    ) -> None:
        from pcbdraft.tools.dispatch import _tool_result_observer_fields

        result = json.dumps(
            {
                "success": True,
                "error": None,
                "message": "DRC检查完成",
                "result": {"overall_status": "pass"},
            }
        )

        self.assertEqual(
            _tool_result_observer_fields("pcb_run_drc", result),
            ("ok", None, None),
        )
        self.assertEqual(
            _tool_result_observer_fields(
                "pcb_run_drc",
                json.dumps({"success": False, "error": "KiCad failed"}),
            ),
            ("error", "tool_error", "KiCad failed"),
        )
        runtime_failure = json.dumps({"ok": False, "message": "runtime failed"})
        self.assertEqual(
            _tool_result_observer_fields("pcb_run_drc", runtime_failure),
            ("error", "tool_error", "runtime failed"),
        )
        for terminal_status in ("blocked", "cancelled", "failed", "timeout"):
            with self.subTest(terminal_status=terminal_status):
                status, error_type, _message = _tool_result_observer_fields(
                    "pcb_run_drc",
                    json.dumps({"status": terminal_status}),
                )
                self.assertEqual(status, "error")
                self.assertEqual(error_type, "tool_error")

        from pcbdraft.agent.tool_guardrails import classify_tool_failure

        self.assertEqual(classify_tool_failure("pcb_run_drc", result), (False, ""))
        self.assertEqual(
            classify_tool_failure("pcb_run_drc", runtime_failure),
            (True, " [error]"),
        )

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
        self.assertEqual(set(context.middleware), {"llm_request", "tool_execution"})

    def test_stage_schema_projection_rebuilds_a_strict_registry_subset(self) -> None:
        from pcbdraft.agent.tool_bindings import ModelToolProjection
        from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY

        routing_specs = DEFAULT_PCB_TOOL_REGISTRY.projected_specs(
            "routing", project_bound=True
        )
        projection = ModelToolProjection("routing", "board", 7, 4, routing_specs)
        original = [
            {
                "type": "function",
                "function": {
                    "name": spec.external_name,
                    "description": "stale transport copy",
                    "parameters": {"type": "object"},
                },
            }
            for spec in DEFAULT_PCB_TOOL_REGISTRY.specs
        ]
        original.append({"type": "function", "function": {"name": "clock"}})
        original.append({"type": "function", "function": {"name": "pcb_unknown_tool"}})
        request = {"model": "board-model", "tools": original}

        with patch(
            "pcbdraft.agent.tool_bindings.model_tool_projection",
            return_value=projection,
        ):
            projected = _project_pcb_tool_schemas(request, "session-stage")["request"]

        names = {item["function"]["name"] for item in projected["tools"]}
        self.assertIn("clock", names)
        self.assertIn("pcb_route_net", names)
        self.assertIn("pcb_move_footprint", names)
        self.assertIn("pcb_connect_group", names)
        self.assertNotIn("pcb_export_gerbers", names)
        self.assertNotIn("pcb_register_kicad_part", names)
        self.assertNotIn("pcb_unknown_tool", names)
        self.assertEqual(
            names & {spec.external_name for spec in routing_specs},
            {spec.external_name for spec in routing_specs},
        )
        route = next(
            item
            for item in projected["tools"]
            if item["function"]["name"] == "pcb_route_net"
        )
        route_spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("route_net")
        self.assertEqual(route["function"]["parameters"], route_spec.input_schema)
        self.assertEqual(
            route["function"]["description"], route_spec.protocol_description
        )

    def test_stage_schema_projection_rebuilds_direct_provider_shapes(self) -> None:
        from pcbdraft.agent.tool_bindings import ModelToolProjection
        from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY

        routing_specs = DEFAULT_PCB_TOOL_REGISTRY.projected_specs(
            "routing", project_bound=True
        )
        projection = ModelToolProjection("routing", "board", 7, 4, routing_specs)
        route_spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("route_net")
        for original, schema_key in (
            (
                {
                    "type": "function",
                    "name": "pcb_route_net",
                    "description": "stale Responses copy",
                    "parameters": {"type": "object"},
                },
                "parameters",
            ),
            (
                {
                    "name": "pcb_route_net",
                    "description": "stale input-schema copy",
                    "input_schema": {"type": "object"},
                },
                "input_schema",
            ),
        ):
            with (
                self.subTest(schema_key=schema_key),
                patch(
                    "pcbdraft.agent.tool_bindings.model_tool_projection",
                    return_value=projection,
                ),
            ):
                projected = _project_pcb_tool_schemas(
                    {"model": "board-model", "tools": [original]},
                    "session-stage-direct",
                )["request"]["tools"]

            self.assertEqual(len(projected), 1)
            self.assertEqual(projected[0]["name"], "pcb_route_net")
            self.assertEqual(projected[0][schema_key], route_spec.input_schema)
            self.assertEqual(
                projected[0]["description"], route_spec.protocol_description
            )

    def test_stage_schema_projection_fails_to_a_bounded_corrective_subset(self) -> None:
        from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY

        original = DEFAULT_PCB_TOOL_REGISTRY.openai_responses_tools()
        with (
            patch(
                "pcbdraft.agent.tool_bindings.model_tool_projection",
                side_effect=RuntimeError("stage adapter unavailable"),
            ),
            patch(
                "pcbdraft.agent.tool_bindings.get_session_project_id",
                return_value="board",
            ),
        ):
            projected = _project_pcb_tool_schemas(
                {"model": "board-model", "tools": original},
                "session-stage-fallback",
            )["request"]["tools"]

        names = {item["name"] for item in projected}
        self.assertIn("pcb_inspect_design", names)
        self.assertIn("pcb_connect_group", names)
        self.assertIn("pcb_move_footprint", names)
        self.assertIn("pcb_route_net", names)
        self.assertIn("pcb_run_drc", names)
        self.assertNotIn("pcb_create_project", names)
        self.assertNotIn("pcb_export_gerbers", names)
        self.assertLessEqual(len(names), 30)
        self.assertLess(
            len(json.dumps(projected, separators=(",", ":")).encode("utf-8")),
            len(json.dumps(original, separators=(",", ":")).encode("utf-8")) * 0.6,
        )

    def test_cost_and_context_quality_metrics_remain_separate(self) -> None:
        unknown_cost = _cost_metrics(
            {
                "input_tokens": 100,
                "cache_read_tokens": 90,
                "output_tokens": 7,
            }
        )
        self.assertEqual(unknown_cost["uncached_input_tokens"], 10)
        self.assertEqual(unknown_cost["cache_read_tokens"], 90)
        self.assertIsNone(unknown_cost["actual_cost_value"])
        self.assertEqual(unknown_cost["actual_cost_status"], "unknown")
        estimated_only = _cost_metrics(
            {
                "estimated_cost_usd": 0.2,
                "actual_cost_usd": 0.0,
                "cost_status": "estimated",
            }
        )
        self.assertIsNone(estimated_only["actual_cost_value"])
        self.assertEqual(estimated_only["actual_cost_status"], "unknown")
        zero_default = _cost_metrics({"actual_cost_usd": 0.0})
        self.assertIsNone(zero_default["actual_cost_value"])
        self.assertEqual(zero_default["actual_cost_status"], "unknown")

        reported_cost = _cost_metrics(
            {
                "input_tokens": 100,
                "output_tokens": 7,
                "actual_cost_usd": 0.012,
                "cost_source": "provider_response",
            }
        )
        self.assertEqual(reported_cost["actual_cost_value"], 0.012)
        self.assertEqual(reported_cost["actual_cost_currency"], "USD")
        self.assertEqual(reported_cost["actual_cost_status"], "reported")
        self.assertEqual(reported_cost["cost_source"], "provider_response")
        alternate_reported_cost = _cost_metrics(
            {
                "prompt_tokens": 20,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 5},
                "cost_status": "actual",
                "cost_amount": 0.025,
                "cost_currency": "EUR",
            }
        )
        self.assertEqual(alternate_reported_cost["uncached_input_tokens"], 15)
        self.assertEqual(alternate_reported_cost["output_tokens"], 3)
        self.assertEqual(alternate_reported_cost["actual_cost_value"], 0.025)
        self.assertEqual(alternate_reported_cost["actual_cost_currency"], "EUR")

        first = _context_quality_metrics(
            session_id="metrics-session",
            turn_id="turn-1",
            active_tokens=100,
            active_bytes=400,
            message_count=2,
        )
        second = _context_quality_metrics(
            session_id="metrics-session",
            turn_id="turn-1",
            active_tokens=115,
            active_bytes=460,
            message_count=4,
        )
        self.assertEqual(first["newly_added_tokens_estimate"], 100)
        self.assertEqual(second["newly_added_tokens_estimate"], 15)
        self.assertEqual(second["repeated_content_token_estimate"], 100)
        self.assertNotIn("cache_read_tokens", second)
        self.assertEqual(second["active_context_token_estimate"], 115)
        empty = _context_quality_metrics(
            session_id="empty-metrics-session",
            turn_id="turn-1",
            active_tokens=0,
            active_bytes=0,
            message_count=0,
        )
        self.assertEqual(empty["repeated_content_ratio"], 0.0)
        other_session = _context_quality_metrics(
            session_id="other-metrics-session",
            turn_id="turn-2",
            active_tokens=10,
            active_bytes=40,
            message_count=1,
        )
        self.assertIsNone(other_session["previous_turn_id"])
        _clear_session_decisions("metrics-session")
        after_reset = _context_quality_metrics(
            session_id="metrics-session",
            turn_id="turn-2",
            active_tokens=12,
            active_bytes=48,
            message_count=1,
        )
        self.assertIsNone(after_reset["previous_turn_id"])
        self.assertEqual(after_reset["newly_added_tokens_estimate"], 12)

    def test_middleware_batches_reads_but_dispatches_only_one_write_per_response(
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
        first_read = middleware(
            tool_name="pcb_inspect_project",
            args={},
            next_call=dispatch,
            **common,
        )
        second_read = middleware(
            tool_name="pcb_search_parts",
            args={"query": "LED"},
            next_call=dispatch,
            **common,
        )
        batch_write = middleware(
            tool_name="pcb_connect_group",
            args={
                "connections": {
                    "entries": [
                        {
                            "net_id": "net_out",
                            "component_id": "load_r",
                            "pin": "2",
                            "role": "signal",
                        }
                    ]
                }
            },
            next_call=dispatch,
            **common,
        )
        second_write = middleware(
            tool_name="pcb_place_group",
            args={
                "placements": {
                    "entries": [
                        {
                            "component_id": "load_r",
                            "x_mm": 1.0,
                            "y_mm": 2.0,
                            "rotation_deg": 0.0,
                            "side": "front",
                        }
                    ]
                }
            },
            next_call=dispatch,
            **common,
        )
        trailing_read = middleware(
            tool_name="pcb_inspect_design",
            args={},
            next_call=dispatch,
            **common,
        )
        next_decision_write = middleware(
            tool_name="pcb_add_component",
            args={"value": {}},
            next_call=dispatch,
            **{**common, "api_request_id": "turn-1:api:2"},
        )

        self.assertEqual(first_read, "executed")
        self.assertEqual(second_read, "executed")
        self.assertEqual(batch_write, "executed")
        blocked = json.loads(second_write)
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["policy"], "one_pcb_write_per_model_decision")
        self.assertEqual(trailing_read, "executed")
        self.assertEqual(next_decision_write, "executed")
        self.assertEqual(
            calls,
            [
                {},
                {"query": "LED"},
                {
                    "connections": {
                        "entries": [
                            {
                                "net_id": "net_out",
                                "component_id": "load_r",
                                "pin": "2",
                                "role": "signal",
                            }
                        ]
                    }
                },
                {},
                {"value": {}},
            ],
        )

    def test_middleware_blocks_unknown_pcb_tool_before_dispatch(self) -> None:
        context = FakePluginContext()
        register(context)
        middleware = context.middleware["tool_execution"][0]
        calls: list[dict] = []
        common = {
            "session_id": "session-unknown-tool",
            "turn_id": "turn-1",
            "api_request_id": "turn-1:api:1",
        }

        blocked = middleware(
            tool_name="pcb_not_a_real_tool",
            args={},
            next_call=lambda args: calls.append(args),
            **common,
        )
        valid_write = middleware(
            tool_name="pcb_add_component",
            args={"value": {}},
            next_call=lambda args: calls.append(args) or "executed",
            **common,
        )

        payload = json.loads(blocked)
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["policy"], "closed_pcb_toolbox")
        self.assertEqual(valid_write, "executed")
        self.assertEqual(calls, [{"value": {}}])

    def test_middleware_enforces_session_pcb_tool_budget_before_dispatch(self) -> None:
        context = FakePluginContext()
        calls: list[dict] = []
        common = {
            "session_id": "session-tool-budget",
            "turn_id": "turn-1",
            "api_request_id": "turn-1:api:1",
        }

        with patch.dict(
            os.environ,
            {
                "PCBDRAFT_PCB_TOOL_CALL_LIMIT": "2",
                "PCBDRAFT_DEBUG_TRACE_PATH": str(self.trace_path),
            },
        ):
            reset_trace_writer()
            register(context)
            middleware = context.middleware["tool_execution"][0]
            for _ in range(2):
                self.assertEqual(
                    middleware(
                        tool_name="pcb_inspect_project",
                        args={},
                        next_call=lambda args: calls.append(args) or "executed",
                        **common,
                    ),
                    "executed",
                )
            blocked = middleware(
                tool_name="pcb_inspect_project",
                args={},
                next_call=lambda args: calls.append(args) or "executed",
                **common,
            )

        payload = json.loads(blocked)
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["policy"], "pcb_tool_call_budget")
        self.assertEqual(len(calls), 2)
        event = self._recorded_events()[-1]
        self.assertEqual(event["event"], "pcb_tool_budget_exhausted")
        self.assertEqual(event["data"]["consumed"], 2)

    def test_session_terminal_reports_enforced_pcb_tool_budget(self) -> None:
        class Service:
            def __init__(self) -> None:
                self.values: dict | None = None

            def record_product_session_terminal(self, project_id: str, **values):
                self.values = {"project_id": project_id, **values}
                return {
                    "process_status": "exited",
                    "task_outcome": "incomplete",
                    "termination_reason": values["termination_reason"],
                    "stage_reached": "routing",
                    "release_gate_passed": False,
                    "artifact": "product-sessions/receipt.json",
                }

        service = Service()
        context = FakePluginContext()
        register(context)
        middleware = context.middleware["tool_execution"][0]
        with (
            patch.dict(
                os.environ,
                {
                    "PCBDRAFT_DEBUG_TRACE_PATH": str(self.trace_path),
                    "PCBDRAFT_PCB_TOOL_CALL_LIMIT": "1",
                },
            ),
            patch(
                "pcbdraft.agent.tool_bindings.get_session_project_id",
                return_value="board-1",
            ),
            patch("pcbdraft.agent.tool_bindings.get_service", return_value=service),
        ):
            common = {
                "session_id": "session-terminal-budget",
                "turn_id": "turn-1",
                "api_request_id": "turn-1:api:1",
            }
            middleware(
                tool_name="pcb_inspect_project",
                args={},
                next_call=lambda _args: "executed",
                **common,
            )
            middleware(
                tool_name="pcb_inspect_project",
                args={},
                next_call=lambda _args: self.fail("budget overrun dispatched"),
                **common,
            )
            context.hooks["on_session_end"][0](
                session_id=common["session_id"],
                turn_id=common["turn_id"],
                completed=True,
                failed=False,
                interrupted=False,
                turn_exit_reason="completed",
                model="gpt-5.6-luna",
            )

        self.assertIsNotNone(service.values)
        self.assertEqual(
            service.values["termination_reason"],
            "budget_exhausted:pcb_tool_calls",
        )

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
                request_char_count=4000,
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
        self.assertEqual(
            request["data"]["context_quality"]["active_context_token_estimate"],
            1000,
        )
        response = next(event for event in events if event["event"] == "model_response")
        self.assertEqual(
            response["data"]["response"]["assistant_message"]["tool_calls"][0][
                "function"
            ]["name"],
            "pcb_plan_request",
        )
        self.assertEqual(
            response["data"]["cost_metrics"]["actual_cost_status"], "unknown"
        )
        error = next(event for event in events if event["event"] == "model_error")
        self.assertEqual(error["data"]["http_status"], 429)
        self.assertTrue(error["data"]["retryable"])
        tool = next(event for event in events if event["event"] == "tool_end")
        self.assertEqual(tool["data"]["tool_name"], "pcb_plan_request")
        self.assertEqual(tool["data"]["status"], "ok")

    def test_session_end_records_product_terminal_before_unbinding(self) -> None:
        class Service:
            def __init__(self) -> None:
                self.receipts: list[dict] = []

            def record_product_session_terminal(self, project_id: str, **values):
                self.receipts.append({"project_id": project_id, **values})
                return {
                    "process_status": "exited",
                    "release_outcome": "incomplete",
                    "scoped_task_outcome": "unknown",
                    "scoped_task_evidence": {
                        "kind": "unavailable",
                        "source_revision": 4,
                    },
                    "termination_reason": "agent_returned_before_gate",
                    "stage_reached": "routing",
                    "release_gate_passed": False,
                    "artifact": "product-sessions/receipt.json",
                }

        service = Service()
        with (
            patch.dict(os.environ, {"PCBDRAFT_DEBUG_TRACE_PATH": str(self.trace_path)}),
            patch(
                "pcbdraft.agent.tool_bindings.get_session_project_id",
                return_value="board-1",
            ),
            patch("pcbdraft.agent.tool_bindings.get_service", return_value=service),
        ):
            context = FakePluginContext()
            register(context)
            context.hooks["on_session_end"][0](
                session_id="session-1",
                turn_id="turn-1",
                completed=True,
                failed=False,
                interrupted=False,
                turn_exit_reason="completed",
                model="gpt-5.6-luna",
            )

        self.assertEqual(len(service.receipts), 1)
        self.assertEqual(service.receipts[0]["project_id"], "board-1")
        self.assertEqual(service.receipts[0]["process_status"], "exited")
        events = self._recorded_events()
        self.assertEqual(
            [event["event"] for event in events],
            ["plugin_loaded", "product_session_terminal", "session_end"],
        )
        terminal = events[1]["data"]
        self.assertEqual(terminal["release_outcome"], "incomplete")
        self.assertEqual(terminal["scoped_task_outcome"], "unknown")
        self.assertEqual(
            terminal["scoped_task_evidence"],
            {"kind": "unavailable", "source_revision": 4},
        )

    def test_session_end_maps_turn_failure_budget_and_strategy_truthfully(self) -> None:
        class Service:
            def __init__(self) -> None:
                self.receipts: list[dict] = []

            def record_product_session_terminal(self, project_id: str, **values):
                self.receipts.append({"project_id": project_id, **values})
                return {
                    "process_status": values["process_status"],
                    "task_outcome": "incomplete",
                    "termination_reason": values["termination_reason"],
                    "stage_reached": "routing",
                    "release_gate_passed": False,
                    "artifact": "product-sessions/receipt.json",
                }

        service = Service()
        cases = (
            (
                False,
                True,
                False,
                "all_retries_exhausted_no_response",
                "exited",
                "tool_failure",
            ),
            (
                False,
                False,
                False,
                "max_iterations_reached(90/90)",
                "exited",
                "budget_exhausted:model_turns",
            ),
            (False, False, True, "interrupted_by_user", "cancelled", "cancelled"),
            (
                True,
                False,
                False,
                "strategy_change_required",
                "exited",
                "human_intervention_required",
            ),
        )
        with (
            patch.dict(os.environ, {"PCBDRAFT_DEBUG_TRACE_PATH": str(self.trace_path)}),
            patch(
                "pcbdraft.agent.tool_bindings.get_session_project_id",
                return_value="board-1",
            ),
            patch("pcbdraft.agent.tool_bindings.get_service", return_value=service),
        ):
            context = FakePluginContext()
            register(context)
            hook = context.hooks["on_session_end"][0]
            for index, (
                completed,
                failed,
                interrupted,
                reason,
                expected_process,
                expected_reason,
            ) in enumerate(cases):
                hook(
                    session_id="session-1",
                    turn_id=f"turn-{index}",
                    completed=completed,
                    failed=failed,
                    interrupted=interrupted,
                    turn_exit_reason=reason,
                    model="gpt-5.6-luna",
                )
                self.assertEqual(
                    service.receipts[-1]["process_status"], expected_process
                )
                self.assertEqual(
                    service.receipts[-1]["termination_reason"], expected_reason
                )


class NativeLifecycleTests(unittest.TestCase):
    def test_guards_survive_plugin_unload_without_installing_a_shim(self) -> None:
        from pcbdraft.agent.extensions.manager import PluginManager

        with tempfile.TemporaryDirectory() as temporary:
            manager = PluginManager(scope_key=temporary)
            self.assertTrue(manager.has_middleware("tool_execution"))
            self.assertTrue(manager.has_hook("pre_api_request"))
            manager.unload()
            self.assertTrue(manager.has_middleware("tool_execution"))
            self.assertTrue(manager.has_hook("pre_api_request"))
            self.assertFalse((Path(temporary) / "plugins").exists())

    def test_settings_retire_disk_shim_and_preserve_provider(self) -> None:
        from pcbdraft.model.configuration import read_raw_config, save_config

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": temporary}),
        ):
            config = {
                "model": {
                    "provider": "custom",
                    "default": "board-model",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "api_key": "local",
                },
                "plugins": {"enabled": ["pcbdraft-debug"]},
            }
            save_config(config, strip_defaults=False)
            write_runtime_config()
            config_path = Path(temporary) / "config.yaml"
            before = config_path.stat()
            before_bytes = config_path.read_bytes()
            with patch(
                "pcbdraft.model.configuration.save_config", wraps=save_config
            ) as persist:
                write_runtime_config()
            persist.assert_not_called()
            after = config_path.stat()
            self.assertEqual(config_path.read_bytes(), before_bytes)
            self.assertEqual(after.st_ino, before.st_ino)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            configured = read_raw_config()
            self.assertNotIn("pcbdraft-debug", configured["plugins"]["enabled"])
            self.assertEqual(configured["model"]["default"], "board-model")
            self.assertEqual(configured["platform_toolsets"]["cli"], ["pcbdraft"])


if __name__ == "__main__":
    unittest.main()
