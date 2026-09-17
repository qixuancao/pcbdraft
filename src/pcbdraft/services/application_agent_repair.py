"""Read-only agent proposal preparation and project event projection.

Repair execution, pending-artifact writes, project-state mutation, and failure
recording remain in the host application. It installs late-bound adapters for
event reads and proposal policy helpers so historical ``services.application``
patch points remain effective without a reverse import.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pcbdraft.agent.plan import AgentDesignRequest


def _unconfigured(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("application agent-repair hooks are not configured")


load_json_limited: Callable[..., Any] = _unconfigured
_validation_error: Callable[[str], Exception] = _unconfigured
_initial_stackup_layers: Callable[[str], int] = _unconfigured
_scope_from_dict: Callable[[dict[str, Any]], Any] = _unconfigured
evaluate_scope: Callable[[Any], Any] = _unconfigured
_board_spec_from_dict: Callable[[dict[str, Any]], Any] = _unconfigured
_slug: Callable[[str], str] = _unconfigured
_agent_design_request_from_dict: Callable[[dict[str, Any]], Any] = _unconfigured


def _configure_legacy_application_hooks(
    *,
    load_json_limited_hook: Callable[..., Any],
    validation_error_hook: Callable[[str], Exception],
    initial_stackup_layers_hook: Callable[[str], int],
    scope_from_dict_hook: Callable[[dict[str, Any]], Any],
    evaluate_scope_hook: Callable[[Any], Any],
    board_spec_from_dict_hook: Callable[[dict[str, Any]], Any],
    slug_hook: Callable[[str], str],
    agent_design_request_from_dict_hook: Callable[[dict[str, Any]], Any],
) -> None:
    """Install late-bound adapters owned by the application module."""

    global load_json_limited
    global _validation_error
    global _initial_stackup_layers
    global _scope_from_dict
    global evaluate_scope
    global _board_spec_from_dict
    global _slug
    global _agent_design_request_from_dict

    load_json_limited = load_json_limited_hook
    _validation_error = validation_error_hook
    _initial_stackup_layers = initial_stackup_layers_hook
    _scope_from_dict = scope_from_dict_hook
    evaluate_scope = evaluate_scope_hook
    _board_spec_from_dict = board_spec_from_dict_hook
    _slug = slug_hook
    _agent_design_request_from_dict = agent_design_request_from_dict_hook


class ApplicationAgentRepairMixin:
    """Prepare reviewable proposals and expose bounded project events."""

    def events(self, project_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        if after < 0:
            raise _validation_error("event cursor must be non-negative")
        project = self._open(project_id)
        result: list[dict[str, Any]] = []
        for path in sorted((project.root / "events").glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                sequence = int(path.stem)
            except ValueError:
                continue
            if sequence > after:
                event = load_json_limited(path, 64 * 1024)
                if isinstance(event, dict):
                    result.append(event)
            if len(result) >= 500:
                break
        return result

    def _prepare_proposal(
        self,
        project_id: str,
        created_at: str,
        value: dict[str, Any],
        prior: dict[str, Any],
        request: str,
    ) -> tuple[dict[str, Any], AgentDesignRequest | None]:
        """Turn generic intent into a reviewable request, never a fixed board type."""

        merged = dict(value)
        layer_reply = bool(
            re.fullmatch(
                r"\s*(?:use\s+)?\d+\s*(?:[- ]?layers?|层)?\s*",
                request,
                re.IGNORECASE,
            )
        )
        prior_layers = prior.get("layers")
        if (
            merged.get("layers") is None
            and isinstance(prior_layers, int)
            and not isinstance(prior_layers, bool)
            and prior_layers >= 1
        ):
            merged["layers"] = prior_layers
        prior_board_value = prior.get("board")
        old_board: dict[str, Any] = (
            prior_board_value if isinstance(prior_board_value, dict) else {}
        )
        raw_board_value = merged.get("board")
        raw_board: dict[str, Any] = (
            raw_board_value if isinstance(raw_board_value, dict) else {}
        )
        merged["board"] = {
            key: raw_board.get(key)
            if raw_board.get(key) is not None
            else old_board.get(key)
            for key in ("width_mm", "height_mm")
        }
        if layer_reply:
            for key in ("requested_parts", "functions"):
                if not merged.get(key) and isinstance(prior.get(key), list):
                    merged[key] = list(prior[key])
            if isinstance(prior.get("request_summary"), str):
                merged["request_summary"] = prior["request_summary"]
        requested_parts = tuple(
            sorted(
                {
                    item
                    for item in merged.get("requested_parts", [])
                    if isinstance(item, str) and item.strip()
                },
                key=str.casefold,
            )
        )
        functions = tuple(
            sorted(
                {
                    item
                    for item in merged.get("functions", [])
                    if isinstance(item, str) and item.strip()
                }
            )
        )
        assumptions = [
            item
            for item in merged.get("assumptions", [])
            if isinstance(item, str) and item.strip()
        ]
        clarifications: list[dict[str, Any]] = []
        layers = merged.get("layers")
        if not isinstance(layers, int) or isinstance(layers, bool) or layers < 1:
            layers = _initial_stackup_layers(request)
            assumptions.append(
                "No usable planner stackup was returned; PCBDraft inferred an initial "
                f"{layers}-layer stackup from the stated design complexity."
            )
        merged["layers"] = layers
        width = merged["board"].get("width_mm")
        height = merged["board"].get("height_mm")
        if width is None or height is None:
            width, height = 80.0, 50.0
            assumptions.append(
                "Board envelope is assumed as 80 mm × 50 mm until changed in the reviewed plan."
            )
        merged["board"] = {"width_mm": float(width), "height_mm": float(height)}
        power_raw_value = merged.get("power")
        power_raw: dict[str, Any] = (
            power_raw_value if isinstance(power_raw_value, dict) else {}
        )
        nominal = power_raw.get("nominal_v")
        if (
            not isinstance(nominal, (int, float))
            or isinstance(nominal, bool)
            or nominal <= 0
        ):
            nominal = 3.3
            assumptions.append(
                "3.3 V logic supply is assumed until the reviewed plan specifies otherwise."
            )
        max_voltage = power_raw.get("max_voltage_v")
        if not isinstance(max_voltage, (int, float)) or isinstance(max_voltage, bool):
            max_voltage = nominal
        max_current = power_raw.get("max_current_a")
        if (
            not isinstance(max_current, (int, float))
            or isinstance(max_current, bool)
            or max_current <= 0
        ):
            max_current = 0.5
        max_power = power_raw.get("max_power_w")
        if (
            not isinstance(max_power, (int, float))
            or isinstance(max_power, bool)
            or max_power <= 0
        ):
            max_power = float(nominal) * float(max_current)
        power = {
            "nominal_v": float(nominal),
            "max_voltage_v": max(float(nominal), float(max_voltage)),
            "max_current_a": float(max_current),
            "max_power_w": float(max_power),
        }
        domains = {"simple_control"}
        words = " ".join((request, *requested_parts, *functions)).casefold()
        for token, domain in (
            ("i2c", "i2c"),
            ("i²c", "i2c"),
            ("spi", "spi"),
            ("uart", "uart"),
            ("串口", "uart"),
            ("usb", "usb2_basic"),
            ("基础usb", "usb2_basic"),
            ("buck", "simple_buck"),
            ("降压", "simple_buck"),
            ("ldo", "ldo"),
            ("稳压", "ldo"),
        ):
            if token in words:
                domains.add(domain)
        if any(
            token in words
            for token in (
                "sensor",
                "temperature",
                "humidity",
                "pressure",
                "传感器",
                "温度",
                "湿度",
                "压力",
            )
        ):
            domains.add("sensor")
        if any(
            token in words
            for token in (
                "mcu",
                "controller",
                "microcontroller",
                "embedded control",
                "单片机",
                "微控制器",
                "控制器",
                "控制板",
            )
        ):
            domains.add("low_voltage_mcu")
        for token, domain in (
            ("ddr", "ddr"),
            ("pcie", "pcie"),
            ("serdes", "serdes"),
            ("高速串行", "serdes"),
            ("rf", "rf"),
            ("antenna", "rf"),
            ("射频", "rf"),
            ("天线", "rf"),
            ("mains", "mains"),
            ("市电", "mains"),
            ("交流电", "mains"),
            ("high voltage", "high_voltage"),
            ("高压", "high_voltage"),
            ("high power", "high_power"),
            ("high-power", "high_power"),
            ("大功率", "high_power"),
            ("高功率", "high_power"),
            ("medical", "medical"),
            ("医疗", "medical"),
            ("aviation", "aviation"),
            ("航空", "aviation"),
            ("safety-critical", "safety_critical"),
            ("安全关键", "safety_critical"),
        ):
            if token in words:
                domains.add(domain)
        scope = _scope_from_dict(
            {
                "domains": sorted(domains),
                "max_voltage_v": power["max_voltage_v"],
                "max_current_a": power["max_current_a"],
                "max_power_w": power["max_power_w"],
                "layers": layers,
                "intended_use": "User-requested PCB design; no domain validation is implied.",
                "risk_class": "unspecified",
            }
        )
        scope_decision = evaluate_scope(scope)
        if not scope_decision.accepted:
            return (
                {
                    **merged,
                    "requested_parts": list(requested_parts),
                    "functions": list(functions),
                    "assurance": "provisional",
                    "scope": {
                        "decision": "generation_unavailable",
                        "errors": list(scope_decision.reasons),
                        "warnings": list(scope_decision.warnings),
                    },
                    "clarifications": [],
                    "planning": {"state": "not_started", "message": None},
                    "brief": None,
                    "decisions": {},
                },
                None,
            )
        board = _board_spec_from_dict(
            {
                "width_mm": float(width),
                "height_mm": float(height),
                "layers": layers,
                "thickness_mm": 1.6,
                "edge_clearance_mm": 0.5,
                "min_track_mm": 0.2,
                "min_clearance_mm": 0.2,
                "min_drill_mm": 0.3,
                "finish": "enig",
            }
        )
        design_id = (
            f"{_slug(merged.get('design_name', 'board'))[:40]}-{project_id[-8:]}"
        )
        approved_request = _agent_design_request_from_dict(
            {
                "schema": "pcbdraft-agent-design-request",
                "version": 1,
                "design_id": design_id,
                "name": str(merged.get("design_name") or "PCBDraft board"),
                "revision": "A",
                "request_summary": str(merged.get("request_summary") or request),
                "scope": scope.to_dict(),
                "board": board.to_dict(),
                "assumptions": sorted(set(assumptions)),
                "requested_parts": list(requested_parts),
                "functions": list(functions),
                "power": power,
                "source": {
                    "locator": f"application/projects/{project_id}/conversation.json",
                    "date": created_at[:10],
                },
            }
        )
        proposal: dict[str, Any] = {
            **merged,
            "requested_parts": list(requested_parts),
            "functions": list(functions),
            "assumptions": list(approved_request.assumptions),
            "power": power,
            "assurance": "provisional",
            "scope": {
                "decision": "attempted",
                "warnings": list(scope_decision.warnings),
            },
            "clarifications": clarifications,
            "planning": {"state": "pending", "message": None},
            "brief": None,
            "decisions": {
                "runtime": "agent_plan_v1",
                "assurance": "provisional",
                "design_id": approved_request.design_id,
                "design_name": approved_request.name,
                "layers": approved_request.board.layers,
                "board": approved_request.board.to_dict(),
                "requested_parts": list(approved_request.requested_parts),
                "risk_class": approved_request.scope.risk_class,
            },
        }
        return proposal, None if clarifications else approved_request

    @staticmethod
    def _attach_plan(proposal: dict[str, Any], compilation: Any) -> dict[str, Any]:
        """Attach only reviewable plan/IR facts; native generation stays confirmed."""

        result = dict(proposal)
        design = compilation.design
        graph = compilation.graph
        counts: dict[tuple[str, str], int] = {}
        references: dict[tuple[str, str], list[str]] = {}
        for component in design.components:
            key = (component.part_id, component.value)
            counts[key] = counts.get(key, 0) + 1
            references.setdefault(key, []).append(component.reference)
        result["planning"] = {"state": "ready", "message": None}
        result["brief"] = {
            "purpose": compilation.request.request_summary,
            "architecture": [
                {"id": block.id, "kind": block.kind, "name": block.name}
                for block in design.blocks
            ],
            "assumptions": list(compilation.request.assumptions),
            "power": compilation.request.power,
            "interfaces": [],
            "board": compilation.request.board.to_dict(),
            "identity": {
                "requested_parts": list(compilation.request.requested_parts),
                "planned_symbols": [
                    {
                        "reference": component.reference,
                        "symbol": graph.get(component.part_id).symbol,
                        "part_id": component.part_id,
                    }
                    for component in design.components
                ],
                "preserved": True,
            },
            "bom": [
                {
                    "part_id": key[0],
                    "value": key[1],
                    "quantity": counts[key],
                    "references": sorted(references[key]),
                    "symbol": graph.get(key[0]).symbol,
                    "trust": graph.get(key[0]).trust,
                }
                for key in sorted(counts)
            ],
            "net_count": len(design.nets),
            "constraints": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "severity": item.severity,
                    "rationale": item.rationale,
                }
                for item in design.constraints
            ],
            "plan_review": compilation.review.to_dict(),
            "semantic_content_hash": design.content_hash(),
            "confirmation_required": True,
        }
        return result

    @staticmethod
    def _proposal_message(proposal: dict[str, Any]) -> str:
        decision = proposal["scope"]["decision"]
        if decision != "attempted":
            return "This request cannot reach the current KiCad backend: " + "; ".join(
                proposal["scope"].get("errors", [])
            )
        if proposal["clarifications"]:
            return proposal["clarifications"][0]["question"]
        planning = proposal.get("planning", {})
        if planning.get("state") != "ready":
            return (
                "Requirements were retained without substituting parts, but a circuit "
                "planning provider is needed before a reviewable topology can be generated: "
                + str(planning.get("message") or "planning is pending")
            )
        review = proposal.get("brief", {}).get("plan_review", {})
        summary = review.get("summary", {}) if isinstance(review, dict) else {}
        attention = summary.get("attention_required", 0)
        if isinstance(attention, int) and attention > 0:
            return (
                "The circuit plan and stock KiCad parts are ready for review. "
                f"{attention} deterministic preflight finding(s) need engineering attention; "
                "generation remains available according to the active client policy."
            )
        return (
            "The circuit plan, stock KiCad parts, and assumptions are ready. "
            "The active client policy controls whether generation continues automatically "
            "or waits for review."
        )
