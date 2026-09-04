#!/usr/bin/env python3
"""Deterministic local OpenAI-compatible planner for product E2E tests only."""

from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import ClassVar

E2E_API_KEY = "pcbdraft-local-e2e-key"
MAX_REQUEST_BYTES = 2 * 1024 * 1024

_HERMES_AGENT_STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("pcb_create_project", {"name": "Hermes KiCad release smoke"}),
    (
        "pcb_add_block",
        {
            "value": {
                "id": "power_entry",
                "kind": "power_entry",
                "name": "Power entry",
                "version": "1",
                "intent": "Receive the two-wire smoke-test supply.",
            }
        },
    ),
    (
        "pcb_add_component",
        {
            "value": {
                "id": "input",
                "reference": "J1",
                "part_id": "samtec.tsw-102-07-g-s",
                "value": "POWER",
                "block_id": "power_entry",
            }
        },
    ),
    (
        "pcb_add_component",
        {
            "value": {
                "id": "bypass",
                "reference": "C1",
                "part_id": "murata.grm188r71c104ka01d",
                "value": "100n",
                "block_id": "power_entry",
            }
        },
    ),
    (
        "pcb_add_net",
        {
            "value": {
                "id": "v3v3",
                "name": "3V3",
                "net_class": "power",
                "power_domain": None,
                "interface": None,
                "intent": "External regulated smoke-test supply.",
            }
        },
    ),
    (
        "pcb_connect_pin",
        {
            "net_id": "v3v3",
            "component_id": "input",
            "pin": "1",
            "role": "source",
        },
    ),
    (
        "pcb_connect_pin",
        {
            "net_id": "v3v3",
            "component_id": "bypass",
            "pin": "1",
            "role": "load",
        },
    ),
    (
        "pcb_connect_pin",
        {
            "net_id": "gnd",
            "component_id": "bypass",
            "pin": "2",
            "role": "return",
        },
    ),
    (
        "pcb_connect_pin",
        {
            "net_id": "gnd",
            "component_id": "input",
            "pin": "2",
            "role": "return",
        },
    ),
    ("pcb_set_board_outline", {"width_mm": 30.0, "height_mm": 20.0}),
    (
        "pcb_place_footprint",
        {
            "component_id": "input",
            "x_mm": 10.0,
            "y_mm": 10.0,
            "rotation_deg": 0.0,
            "side": "front",
        },
    ),
    (
        "pcb_place_footprint",
        {
            "component_id": "bypass",
            "x_mm": 15.0,
            "y_mm": 10.0,
            "rotation_deg": 0.0,
            "side": "front",
        },
    ),
    ("pcb_route_net", {"net_id": "v3v3"}),
    ("pcb_route_net", {"net_id": "gnd"}),
    (
        "pcb_add_via",
        {
            "via_id": "gnd_stitch_1",
            "net_id": "gnd",
            "x_mm": 13.0,
            "y_mm": 10.9,
            "diameter_mm": 0.6,
            "drill_mm": 0.3,
            "from_layer": 0,
            "to_layer": 1,
        },
    ),
    ("pcb_run_erc", {}),
    ("pcb_run_drc", {}),
)


def _json_after(prompt: str, marker: str) -> Any:
    start = prompt.find(marker)
    if start < 0:
        raise ValueError(f"prompt marker is absent: {marker}")
    value, _end = json.JSONDecoder().raw_decode(prompt[start + len(marker) :].lstrip())
    return value


def _intent(prompt: str) -> dict[str, Any]:
    request = _json_after(prompt, "User request (quoted JSON string): ")
    if not isinstance(request, str):
        raise TypeError("quoted user request is not text")
    project_line = next(
        (
            line.removeprefix("Project name: ")
            for line in prompt.splitlines()
            if line.startswith("Project name: ")
        ),
        "Open PCB prototype",
    )
    return {
        "request_summary": request,
        "design_name": project_line,
        "layers": None,
        "board": {"width_mm": None, "height_mm": None},
        "assumptions": ["The external source is regulated to 3.3 V."],
        "requested_parts": [],
        "functions": ["status indication"],
        "power": {
            "nominal_v": 3.3,
            "max_voltage_v": 3.3,
            "max_current_a": 0.02,
            "max_power_w": 0.066,
        },
        "missing_fields": [],
    }


def _plan(prompt: str, *, repaired: bool) -> dict[str, Any]:
    request = _json_after(prompt, "Approved request (JSON): ")
    if not isinstance(request, dict) or not isinstance(request.get("design_id"), str):
        raise TypeError("approved request is malformed")
    notes = ["LED current and resistor value require human review."]
    if repaired:
        feedback = _json_after(prompt, "Deterministic repair feedback (JSON): ")
        notes.append(
            f"Replacement plan produced from bounded repair attempt {feedback['attempt']}."
        )
    return {
        "schema": "pcbdraft-circuit-plan",
        "version": 2,
        "design_id": request["design_id"],
        "summary": "A generic connector, bypass capacitor, resistor, and LED topology.",
        "assumptions": ["The external source is regulated to 3.3 V."],
        "notes": notes,
        "components": [
            {
                "id": "capacitor",
                "reference": "C1",
                "symbol": "Device:C",
                "value": "100n",
                "role": "supply_bypass",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
                "on_board": True,
                "exact_name": None,
            },
            {
                "id": "input",
                "reference": "J1",
                "symbol": "Connector_Generic:Conn_01x02",
                "value": "POWER",
                "role": "power_input_connector",
                "footprint": "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
                "on_board": True,
                "exact_name": None,
            },
            {
                "id": "resistor",
                "reference": "R1",
                "symbol": "Device:R",
                "value": "1k",
                "role": "led_current_limit",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "on_board": True,
                "exact_name": None,
            },
            {
                "id": "led",
                "reference": "D1",
                "symbol": "Device:LED",
                "value": "LED",
                "role": "indicator",
                "footprint": "LED_SMD:LED_0603_1608Metric",
                "on_board": True,
                "exact_name": None,
            },
        ],
        "nets": [
            {
                "id": "gnd",
                "name": "GND",
                "net_class": "power",
                "power_domain": "logic_3v3",
                "interface": None,
                "intent": "Common return.",
                "endpoints": [
                    {"component": "capacitor", "pin": "2", "role": "return"},
                    {"component": "input", "pin": "2", "role": "return"},
                    {"component": "led", "pin": "1", "role": "return"},
                ],
            },
            {
                "id": "v3v3",
                "name": "3V3",
                "net_class": "power",
                "power_domain": "logic_3v3",
                "interface": None,
                "intent": "External regulated source.",
                "endpoints": [
                    {"component": "capacitor", "pin": "1", "role": "load"},
                    {"component": "input", "pin": "1", "role": "source"},
                    {"component": "resistor", "pin": "1", "role": "load"},
                ],
            },
            {
                "id": "led_a",
                "name": "LED_A",
                "net_class": "signal",
                "power_domain": None,
                "interface": "status_led",
                "intent": "Current-limited LED anode.",
                "endpoints": [
                    {"component": "resistor", "pin": "2", "role": "load"},
                    {"component": "led", "pin": "2", "role": "load"},
                ],
            },
        ],
        "blocks": [
            {
                "id": "power_entry",
                "kind": "power_entry",
                "name": "Power entry",
                "intent": "Accept and bypass the regulated external supply.",
                "parent": None,
                "components": ["capacitor", "input"],
            },
            {
                "id": "status_indicator",
                "kind": "status_indicator",
                "name": "Status indicator",
                "intent": "Limit current and emit visible status.",
                "parent": None,
                "components": ["led", "resistor"],
            },
        ],
        "power_domains": [
            {
                "id": "logic_3v3",
                "nominal_v": 3.3,
                "min_v": 3.3,
                "max_v": 3.3,
                "max_current_a": 0.02,
                "source": {"component": "input", "pin": "1", "role": "source"},
                "intent": "Regulated external logic and indicator supply.",
            }
        ],
        "interfaces": [
            {
                "id": "status_led",
                "kind": "status_indicator",
                "power_domain": "logic_3v3",
                "members": [
                    {"component": "resistor", "pin": "2", "role": "load"},
                    {"component": "led", "pin": "2", "role": "load"},
                ],
                "controller": None,
                "parameters": [],
                "intent": "Current-limited visible indicator path.",
            }
        ],
        "constraints": [
            {
                "id": "indicator_group",
                "kind": "functional_group",
                "targets": ["led", "resistor"],
                "parameters": [{"name": "max_diameter_mm", "value": 15.0}],
                "severity": "required",
                "rationale": "Keep the series element near the indicator.",
            },
            {
                "id": "indicator_route_width",
                "kind": "routing",
                "targets": ["led_a"],
                "parameters": [{"name": "width_mm", "value": 0.25}],
                "severity": "required",
                "rationale": "Retain a normal prototype signal width.",
            },
            {
                "id": "input_pinout",
                "kind": "connector_pinout",
                "targets": ["input"],
                "parameters": [
                    {"name": "pin.1", "value": "v3v3"},
                    {"name": "pin.2", "value": "gnd"},
                    {"name": "require_complete", "value": True},
                ],
                "severity": "required",
                "rationale": "Preserve the reviewed external power pinout.",
            },
            {
                "id": "status_net_label",
                "kind": "net_label",
                "targets": ["led_a"],
                "parameters": [{"name": "label", "value": "LED_A"}],
                "severity": "required",
                "rationale": "Preserve the reviewed status-net identity.",
            },
            {
                "id": "indicator_region",
                "kind": "placement_region",
                "targets": ["led", "resistor"],
                "parameters": [{"name": "region", "value": "right"}],
                "severity": "required",
                "rationale": "Place the visible indicator in the right board third.",
            },
            {
                "id": "center_reserved",
                "kind": "board_keepout",
                "targets": ["board"],
                "parameters": [
                    {"name": "anchor", "value": "center"},
                    {"name": "height_mm", "value": 2.0},
                    {"name": "layers", "value": "all"},
                    {"name": "width_mm", "value": 2.0},
                ],
                "severity": "required",
                "rationale": "Reserve a small central placement and routing area.",
            },
        ],
        "assertions": [
            {
                "id": "indicator_series_path",
                "kind": "components_share_net",
                "targets": ["led", "resistor"],
                "minimum": None,
                "maximum": None,
                "severity": "required",
                "rationale": "The resistor and LED must share their series net.",
            }
        ],
    }


def _review() -> dict[str, Any]:
    return {
        "summary": "Fake structured heuristic review.",
        "risk": "unknown",
        "modules": ["fixture module"],
        "interfaces": [],
        "power_domains": [],
        "missing_constraints": ["functional intent is not encoded"],
        "findings": [
            {
                "severity": "info",
                "category": "test",
                "title": "Fixture finding",
                "evidence": ["demo.kicad_pcb: OLD"],
                "rationale": "Exercises the structured review path.",
                "proposed_action": "Human review.",
                "confidence": 0.5,
                "requires_human": True,
            }
        ],
        "unsupported_checks": ["SI", "PI", "thermal", "EMI", "functional correctness"],
    }


def _patch(prompt: str) -> dict[str, Any]:
    old_text = "OLD"
    new_text = "NEW"
    if "CAUSE_REGRESSION" in prompt:
        old_text = "DRC_ERROR=0\nDRC_WARNING=2\nOLD"
        new_text = "DRC_ERROR=1\nDRC_WARNING=2\nNEW"
    return {
        "summary": "Replace the unique fixture marker.",
        "operations": [
            {
                "op": "replace_text",
                "relative_path": "demo.kicad_pcb",
                "old_text": old_text,
                "new_text": new_text,
                "reason": "Offline end-to-end fixture change.",
            }
        ],
        "unsupported_checks": ["functional correctness"],
    }


def _hermes_agent_message(body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return one deterministic decision for the current Hermes conversation."""

    messages = body.get("messages")
    tools = body.get("tools")
    if not isinstance(messages, list) or not isinstance(tools, list):
        raise TypeError("Hermes agent request must contain messages and tools")
    available = {
        str(function.get("name"))
        for item in tools
        if isinstance(item, dict)
        and isinstance((function := item.get("function")), dict)
    }
    required = {name for name, _arguments in _HERMES_AGENT_STEPS}
    if not required <= available:
        raise ValueError(
            "Hermes request omitted PCBDraft tools: "
            + ", ".join(sorted(required - available))
        )

    prior_calls: list[str] = []
    failed_result = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    prior_calls.append(function["name"])
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict) and result.get("success") is False:
            failed_result = True

    expected_prefix = [name for name, _arguments in _HERMES_AGENT_STEPS]
    if prior_calls != expected_prefix[: len(prior_calls)]:
        raise ValueError("Hermes agent replay contains an unexpected PCB tool sequence")
    if failed_result:
        return (
            {
                "role": "assistant",
                "content": (
                    "The Hermes/KiCad smoke stopped because a PCB tool reported "
                    "failure; the board is incomplete and is not production ready."
                ),
            },
            "stop",
        )
    if len(prior_calls) < len(_HERMES_AGENT_STEPS):
        name, arguments = _HERMES_AGENT_STEPS[len(prior_calls)]
        return (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-pcbdraft-{len(prior_calls) + 1}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                arguments,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ],
            },
            "tool_calls",
        )
    return (
        {
            "role": "assistant",
            "content": (
                "Hermes/KiCad smoke completed with retained ERC and DRC evidence. "
                "These checks do not establish production readiness."
            ),
        },
        "stop",
    )


class _ProviderServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _ProviderHandler)
        self.request_count = 0


class _ProviderHandler(BaseHTTPRequestHandler):
    server: _ProviderServer
    protocol_version = "HTTP/1.1"
    server_version = "PCBDraftE2EProvider"
    sys_version = ""
    _SCHEMAS: ClassVar[set[str]] = {
        "pcbdraft_intent",
        "pcbdraft_circuit_plan",
        "pcbdraft_repair_plan",
        "pcbdraft_review",
        "pcbdraft_patch",
    }

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:
        if self.path != "/stats":
            self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._write(
            HTTPStatus.OK,
            {
                "schema": "pcbdraft-e2e-provider-stats",
                "requests": self.server.request_count,
            },
        )

    def do_POST(self) -> None:
        try:
            if self.path != "/v1/chat/completions":
                raise LookupError("not found")
            if self.headers.get("Authorization") != f"Bearer {E2E_API_KEY}":
                self._write(HTTPStatus.UNAUTHORIZED, {"error": "invalid key"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request length")
            body = json.loads(self.rfile.read(length))
            response_format = body.get("response_format")
            if isinstance(response_format, dict):
                schema_name = response_format["json_schema"]["name"]
                if schema_name not in self._SCHEMAS:
                    raise ValueError("unexpected response schema")
                prompt = body["messages"][0]["content"]
                if schema_name == "pcbdraft_intent":
                    content = _intent(prompt)
                elif schema_name == "pcbdraft_review":
                    content = _review()
                elif schema_name == "pcbdraft_patch":
                    content = _patch(prompt)
                else:
                    content = _plan(
                        prompt, repaired=schema_name == "pcbdraft_repair_plan"
                    )
                message = {"role": "assistant", "content": json.dumps(content)}
                finish_reason = "stop"
            else:
                message, finish_reason = _hermes_agent_message(body)
            self.server.request_count += 1
            prompt_tokens = max(1, len(json.dumps(body.get("messages", []))) // 4)
            completion_tokens = max(1, len(json.dumps(message)) // 4)
            response = {
                "id": f"e2e-{self.server.request_count}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": str(body.get("model") or "pcbdraft-e2e-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            if body.get("stream") is True:
                self._write_stream(response)
            else:
                self._write(HTTPStatus.OK, response)
        except LookupError:
            self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._write(HTTPStatus.BAD_REQUEST, {"error": str(exc)[:512]})

    def _write(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _write_stream(self, value: dict[str, Any]) -> None:
        choice = value["choices"][0]
        delta = dict(choice["message"])
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            delta["tool_calls"] = [
                {"index": index, **call}
                for index, call in enumerate(tool_calls)
                if isinstance(call, dict)
            ]
        chunk = {
            "id": value["id"],
            "object": "chat.completion.chunk",
            "created": value["created"],
            "model": value["model"],
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": choice["finish_reason"],
                }
            ],
            "usage": value["usage"],
        }
        payload = (
            "data: "
            + json.dumps(chunk, sort_keys=True, separators=(",", ":"))
            + "\n\ndata: [DONE]\n\n"
        ).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)


@dataclass
class RunningProvider:
    server: _ProviderServer
    thread: threading.Thread
    base_url: str

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_fake_provider() -> RunningProvider:
    server = _ProviderServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return RunningProvider(
        server=server,
        thread=thread,
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    running = start_fake_provider()
    args.ready_file.write_text(running.base_url + "\n", encoding="utf-8")
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        stopped.wait()
    finally:
        running.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
