"""Deterministic semantic intent rules shared by validation and benchmarks.

These rules deliberately operate on the typed IR and trusted part graph.  They do
not try to replace KiCad ERC/DRC or physical analysis.  Optional footprint bounds
and router receipts allow callers with stronger geometric evidence to enable the
corresponding checks without turning missing evidence into a fabricated pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError
from .ir import Design
from .parts import PartGraph
from .scope import evaluate_scope


@dataclass(frozen=True, order=True)
class RuleFinding:
    code: str
    severity: str
    object_id: str
    message: str
    details: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "object_id": self.object_id,
            "message": self.message,
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


def evaluate_semantic_rules(
    design: Design,
    graph: PartGraph,
    *,
    placements: dict[str, dict[str, Any]] | None = None,
    footprint_bounds: dict[str, tuple[float, float, float, float]] | None = None,
    routing: dict[str, Any] | None = None,
    approximate_geometry: bool = True,
) -> tuple[RuleFinding, ...]:
    """Return stable findings for locally provable semantic contract failures."""
    findings: list[RuleFinding] = []
    for issue in design.issues():
        findings.append(
            RuleFinding(issue.code, issue.severity, issue.path, issue.message)
        )
    for issue in graph.validate_design(design, check_libraries=False):
        findings.append(
            RuleFinding(issue.code, issue.severity, issue.path, issue.message)
        )

    scope = evaluate_scope(design.scope)
    if not scope.accepted:
        findings.append(
            RuleFinding(
                "scope.unsupported",
                "error",
                design.design_id,
                "; ".join(scope.reasons),
                (("reason_count", len(scope.reasons)),),
            )
        )

    position_map = placements or {
        component.id: {
            "x_mm": component.placement.x_mm,
            "y_mm": component.placement.y_mm,
        }
        for component in design.components
        if component.placement is not None
    }
    constraints = {constraint.id: constraint for constraint in design.constraints}
    kinds = {constraint.kind for constraint in design.constraints}
    findings.extend(_coverage_findings(design, graph, kinds))
    for constraint in constraints.values():
        if constraint.kind == "decoupling":
            finding = _decoupling_finding(
                design,
                graph,
                constraint,
                position_map,
                approximate_geometry=approximate_geometry,
            )
        elif constraint.kind == "interface_pullups":
            finding = _pullup_finding(design, graph, constraint)
        elif constraint.kind == "current_limit":
            finding = _current_limit_finding(design, graph, constraint)
        elif constraint.kind == "functional_group":
            finding = _group_finding(constraint, position_map)
        elif constraint.kind == "edge_placement":
            finding = _edge_finding(design, constraint, position_map, footprint_bounds)
        elif constraint.kind == "manufacturing_rules":
            finding = _manufacturing_finding(design, constraint)
        elif constraint.kind == "routing":
            finding = _routing_finding(design, constraint, routing)
        elif constraint.kind == "power_budget":
            finding = _power_budget_finding(design, constraint)
        else:
            finding = None
        if finding is not None:
            findings.append(finding)

    findings.extend(_interface_findings(design))
    return tuple(sorted(set(findings)))


def _finding(code: str, object_id: str, message: str, **details: Any) -> RuleFinding:
    return RuleFinding(
        code,
        "error",
        object_id,
        message,
        tuple(sorted(details.items())),
    )


def _coverage_findings(
    design: Design, graph: PartGraph, kinds: set[str]
) -> list[RuleFinding]:
    required: dict[str, str] = {"manufacturing_rules": "board"}
    if any(interface.kind == "i2c" for interface in design.interfaces):
        required["interface_pullups"] = "i2c"
    known_parts = {part["id"] for part in graph.to_dict()["parts"]}
    if any(
        graph.get(component.part_id).kind == "led"
        for component in design.components
        if component.part_id in known_parts
    ):
        required["current_limit"] = "led"
    edge_parts = {
        component.id
        for component in design.components
        if component.part_id in known_parts
        and graph.get(component.part_id).manufacturing.get("edge_mount_intent") is True
    }
    if edge_parts:
        constrained = {
            target
            for constraint in design.constraints
            if constraint.kind == "edge_placement"
            for target in constraint.targets
        }
        for component_id in sorted(edge_parts - constrained):
            required[f"edge_placement:{component_id}"] = component_id
    for component in design.components:
        try:
            part = graph.get(component.part_id)
        except ValidationError:
            continue
        if part.kind != "capacitor":
            continue
        connected_roles = {
            endpoint.role
            for net in design.nets
            for endpoint in net.endpoints
            if endpoint.component == component.id
        }
        if "decoupling" in connected_roles and not any(
            constraint.kind == "decoupling" and component.id in constraint.targets
            for constraint in design.constraints
        ):
            required[f"decoupling:{component.id}"] = component.id

    result: list[RuleFinding] = []
    for requirement, object_id in sorted(required.items()):
        kind = requirement.split(":", 1)[0]
        if ":" not in requirement and kind in kinds:
            continue
        if ":" in requirement and any(
            constraint.kind == kind and object_id in constraint.targets
            for constraint in design.constraints
        ):
            continue
        result.append(
            _finding(
                "intent.required_constraint_missing",
                object_id,
                f"required {kind} constraint is absent",
                constraint_kind=kind,
            )
        )
    for interface in design.interfaces:
        if interface.kind != "i2c":
            continue
        interface_nets = {
            net.id for net in design.nets if net.interface == interface.id
        }
        routed_nets = {
            target
            for constraint in design.constraints
            if constraint.kind == "routing"
            for target in constraint.targets
        }
        missing_nets = interface_nets - routed_nets
        if missing_nets:
            result.append(
                _finding(
                    "intent.required_constraint_missing",
                    interface.id,
                    "I2C nets lack a routing constraint",
                    constraint_kind="routing",
                    nets=",".join(sorted(missing_nets)),
                )
            )
    return result


def _target_components(design: Design, targets: tuple[str, ...]) -> list[Any]:
    target_set = set(targets)
    return [component for component in design.components if component.id in target_set]


def _component_net_ids(design: Design, component_id: str) -> set[str]:
    return {
        net.id
        for net in design.nets
        if any(endpoint.component == component_id for endpoint in net.endpoints)
    }


def _decoupling_finding(
    design: Design,
    graph: PartGraph,
    constraint: Any,
    placements: dict[str, dict[str, Any]],
    *,
    approximate_geometry: bool,
) -> RuleFinding | None:
    components = _target_components(design, constraint.targets)
    capacitors = []
    devices = []
    for component in components:
        try:
            part = graph.get(component.part_id)
        except ValidationError:
            continue
        if "capacitance_f" in part.ratings:
            capacitors.append(component)
        else:
            devices.append(component)
    nets = [net for net in design.nets if net.id in constraint.targets]
    maximum = float(constraint.params.get("max_distance_mm", 0.0))
    minimum_capacitance = float(constraint.params.get("min_capacitance_f", 0.0))
    reasons: list[str] = []
    if len(capacitors) != 1 or len(devices) != 1 or len(nets) < 2:
        reasons.append(
            "target set does not identify one capacitor, one device, and two rails"
        )
    else:
        capacitor, device = capacitors[0], devices[0]
        capacitance = float(graph.get(capacitor.part_id).ratings["capacitance_f"])
        if capacitance + 1e-18 < minimum_capacitance:
            reasons.append("capacitance is below the declared minimum")
        cap_nets = _component_net_ids(design, capacitor.id)
        device_nets = _component_net_ids(design, device.id)
        required_nets = {net.id for net in nets}
        if not required_nets <= cap_nets or not required_nets <= device_nets:
            reasons.append("capacitor and device do not share every declared rail")
        cap_position = placements.get(capacitor.id)
        device_position = placements.get(device.id)
        if (
            approximate_geometry
            and cap_position is not None
            and device_position is not None
        ):
            distance = math.dist(
                (float(cap_position["x_mm"]), float(cap_position["y_mm"])),
                (float(device_position["x_mm"]), float(device_position["y_mm"])),
            )
            if distance > maximum + 1e-9:
                reasons.append("component-origin distance exceeds the declared bound")
    if not reasons:
        return None
    return _finding(
        "intent.decoupling",
        constraint.id,
        "; ".join(reasons),
        maximum_distance_mm=maximum,
    )


def _pullup_finding(
    design: Design, graph: PartGraph, constraint: Any
) -> RuleFinding | None:
    resistors = []
    for component in _target_components(design, constraint.targets):
        try:
            part = graph.get(component.part_id)
        except ValidationError:
            continue
        if "resistance_ohm" in part.ratings:
            resistors.append(component)
    signal_nets = {
        net.id
        for net in design.nets
        if net.id in constraint.targets and net.interface is not None
    }
    power_net = next((net.id for net in design.nets if net.name == "3V3"), None)
    expected = float(constraint.params.get("resistance_ohm", 0.0))
    covered: set[str] = set()
    reasons: list[str] = []
    if len(resistors) != int(constraint.params.get("count", 0)):
        reasons.append("pull-up count differs from the declared count")
    for component in resistors:
        nets = _component_net_ids(design, component.id)
        covered.update(nets & signal_nets)
        if power_net not in nets:
            reasons.append(f"{component.id} is not connected to 3V3")
        actual = float(graph.get(component.part_id).ratings["resistance_ohm"])
        if abs(actual - expected) > 1e-9:
            reasons.append(f"{component.id} value differs from the declared value")
    if covered != signal_nets:
        reasons.append("not every declared I2C signal has a pull-up")
    if not reasons:
        return None
    return _finding(
        "intent.i2c_pullups",
        constraint.id,
        "; ".join(sorted(set(reasons))),
        covered_nets=",".join(sorted(covered)),
    )


def _current_limit_finding(
    design: Design, graph: PartGraph, constraint: Any
) -> RuleFinding | None:
    resistor = None
    led = None
    for component in _target_components(design, constraint.targets):
        try:
            part = graph.get(component.part_id)
        except ValidationError:
            continue
        if "resistance_ohm" in part.ratings:
            resistor = component
        if part.kind == "led":
            led = component
    reasons: list[str] = []
    if resistor is None or led is None:
        reasons.append("current-limit target set is incomplete")
        current = math.inf
        power = math.inf
    else:
        resistor_part = graph.get(resistor.part_id)
        led_part = graph.get(led.part_id)
        resistance = float(resistor_part.ratings["resistance_ohm"])
        expected = float(constraint.params.get("resistance_ohm", resistance))
        if abs(resistance - expected) > 1e-9:
            reasons.append("installed resistance differs from the declared value")
        current = (
            float(constraint.params["supply_v"]) - float(constraint.params["forward_v"])
        ) / resistance
        power = current * current * resistance
        if current > float(constraint.params["max_current_a"]) + 1e-12:
            reasons.append("calculated current exceeds the intent limit")
        if current > float(led_part.ratings["max_current_a"]) + 1e-12:
            reasons.append("calculated current exceeds the LED rating")
        if power > float(resistor_part.ratings["power_w"]) + 1e-12:
            reasons.append("calculated dissipation exceeds the resistor rating")
        resistor_nets = _component_net_ids(design, resistor.id)
        led_nets = _component_net_ids(design, led.id)
        if not resistor_nets & led_nets:
            reasons.append("resistor and LED are not connected in series")
        declared_nets = {
            target for target in constraint.targets if target.startswith("net_")
        }
        if declared_nets and not declared_nets <= resistor_nets | led_nets:
            reasons.append("current-limit chain does not touch every declared net")
    if not reasons:
        return None
    return _finding(
        "intent.current_limit",
        constraint.id,
        "; ".join(reasons),
        current_a=round(current, 9),
        resistor_power_w=round(power, 9),
    )


def _group_finding(
    constraint: Any, placements: dict[str, dict[str, Any]]
) -> RuleFinding | None:
    points = {
        target: (
            float(placements[target]["x_mm"]),
            float(placements[target]["y_mm"]),
        )
        for target in constraint.targets
        if target in placements
    }
    if len(points) != len(constraint.targets):
        return _finding(
            "intent.functional_group",
            constraint.id,
            "one or more group placements are unavailable",
        )
    diameter = max(
        (
            math.dist(first, second)
            for first in points.values()
            for second in points.values()
        ),
        default=0.0,
    )
    maximum = float(constraint.params["max_diameter_mm"])
    if diameter <= maximum + 1e-9:
        return None
    return _finding(
        "intent.functional_group",
        constraint.id,
        "functional group diameter exceeds the declared bound",
        diameter_mm=round(diameter, 6),
        maximum_mm=maximum,
    )


def _edge_finding(
    design: Design,
    constraint: Any,
    placements: dict[str, dict[str, Any]],
    footprint_bounds: dict[str, tuple[float, float, float, float]] | None,
) -> RuleFinding | None:
    if footprint_bounds is None:
        return None
    component_id = next(
        (target for target in constraint.targets if target != "board"), None
    )
    if (
        component_id is None
        or component_id not in placements
        or component_id not in footprint_bounds
    ):
        return _finding(
            "intent.edge_placement",
            constraint.id,
            "edge-placement geometry is incomplete",
        )
    position = placements[component_id]
    bx1, by1, bx2, by2 = footprint_bounds[component_id]
    x1 = float(position["x_mm"]) + bx1
    y1 = float(position["y_mm"]) + by1
    x2 = float(position["x_mm"]) + bx2
    y2 = float(position["y_mm"]) + by2
    distances = {
        "left": x1,
        "right": design.board.width_mm - x2,
        "top": y1,
        "bottom": design.board.height_mm - y2,
    }
    edge = str(constraint.params["edge"])
    distance = distances[edge]
    maximum = float(constraint.params["max_edge_distance_mm"])
    if -1e-9 <= distance <= maximum + 1e-9:
        return None
    return _finding(
        "intent.edge_placement",
        constraint.id,
        "component violates its declared edge-access bound",
        distance_mm=round(distance, 6),
        maximum_mm=maximum,
    )


def _manufacturing_finding(design: Design, constraint: Any) -> RuleFinding | None:
    expected = {
        "min_track_mm": design.board.min_track_mm,
        "min_clearance_mm": design.board.min_clearance_mm,
        "min_drill_mm": design.board.min_drill_mm,
        "edge_clearance_mm": design.board.edge_clearance_mm,
    }
    mismatches = {
        key: (float(constraint.params.get(key, -1)), value)
        for key, value in expected.items()
        if abs(float(constraint.params.get(key, -1)) - value) > 1e-12
    }
    if not mismatches:
        return None
    return _finding(
        "intent.manufacturing_rules",
        constraint.id,
        "manufacturing constraint does not match the board contract",
        fields=",".join(sorted(mismatches)),
    )


def _routing_finding(
    design: Design, constraint: Any, routing: dict[str, Any] | None
) -> RuleFinding | None:
    nominal = float(constraint.params.get("width_mm", 0.0))
    neckdown = float(constraint.params.get("neckdown_width_mm", nominal))
    reasons: list[str] = []
    if nominal + 1e-12 < design.board.min_track_mm:
        reasons.append("nominal width is below the board minimum")
    if neckdown + 1e-12 < design.board.min_track_mm:
        reasons.append("neckdown width is below the board minimum")
    maximum = constraint.params.get("max_length_mm")
    if maximum is not None and float(maximum) <= 0:
        reasons.append("maximum route length is not positive")
    if routing is not None:
        if routing.get("state") != "completed" or routing.get("unrouted"):
            reasons.append("router receipt is incomplete")
        net_by_id = {net.id: net for net in design.nets}
        lengths = routing.get("length_mm_by_net", {})
        widths = routing.get("length_mm_by_net_and_width", {})
        for target in constraint.targets:
            net = net_by_id.get(target)
            if net is None:
                continue
            if (
                maximum is not None
                and float(lengths.get(net.name, math.inf)) > float(maximum) + 1e-9
            ):
                reasons.append(f"{net.name} exceeds its maximum length")
            if not widths.get(net.name):
                reasons.append(f"{net.name} lacks width evidence")
            elif any(float(width) + 1e-9 < neckdown for width in widths[net.name]):
                reasons.append(f"{net.name} is narrower than the neckdown limit")
    if not reasons:
        return None
    return _finding(
        "intent.routing",
        constraint.id,
        "; ".join(sorted(set(reasons))),
    )


def _power_budget_finding(design: Design, constraint: Any) -> RuleFinding | None:
    declared_current = float(constraint.params.get("max_current_a", -1))
    declared_power = float(constraint.params.get("max_power_w", -1))
    expected_power = design.scope.max_voltage_v * design.scope.max_current_a
    if (
        declared_current <= design.scope.max_current_a + 1e-12
        and declared_power <= expected_power + 1e-12
        and declared_current >= 0
        and declared_power >= 0
    ):
        return None
    return _finding(
        "intent.power_budget",
        constraint.id,
        "power-budget constraint exceeds the declared scope envelope",
        declared_current_a=declared_current,
        declared_power_w=declared_power,
    )


def _interface_findings(design: Design) -> list[RuleFinding]:
    connected = {
        (endpoint.component, endpoint.pin)
        for net in design.nets
        for endpoint in net.endpoints
        if net.interface is not None
    }
    result = []
    for interface in design.interfaces:
        missing = sorted(
            f"{member.component}:{member.pin}"
            for member in interface.members
            if (member.component, member.pin) not in connected
        )
        if missing:
            result.append(
                _finding(
                    "interface.member_unconnected",
                    interface.id,
                    "declared interface members are not connected to interface nets",
                    members=",".join(missing),
                )
            )
    return result
