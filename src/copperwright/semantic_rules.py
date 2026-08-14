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
    footprint_inspections: dict[str, Any] | None = None,
    routing: dict[str, Any] | None = None,
    approximate_geometry: bool = True,
    allow_provisional: bool = False,
) -> tuple[RuleFinding, ...]:
    """Return stable findings for locally provable semantic contract failures."""
    findings: list[RuleFinding] = []
    for issue in design.issues():
        findings.append(
            RuleFinding(issue.code, issue.severity, issue.path, issue.message)
        )
    for issue in graph.validate_design(
        design,
        check_libraries=False,
        allow_provisional=allow_provisional,
    ):
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
        try:
            if constraint.kind == "decoupling":
                finding = _decoupling_finding(
                    design,
                    graph,
                    constraint,
                    position_map,
                    footprint_inspections=footprint_inspections,
                    approximate_geometry=approximate_geometry,
                )
            elif constraint.kind == "interface_pullups":
                finding = _pullup_finding(design, graph, constraint)
            elif constraint.kind == "i2c_electrical_budget":
                finding = _i2c_electrical_budget_finding(design, constraint)
            elif constraint.kind == "spi_electrical_budget":
                finding = _spi_electrical_budget_finding(design, graph, constraint)
            elif constraint.kind == "uart_electrical_budget":
                finding = _uart_electrical_budget_finding(design, constraint)
            elif constraint.kind == "ldo_regulation_budget":
                finding = _ldo_regulation_budget_finding(design, graph, constraint)
            elif constraint.kind == "source_ownership":
                finding = _source_ownership_finding(design, constraint)
            elif constraint.kind == "current_limit":
                finding = _current_limit_finding(design, graph, constraint)
            elif constraint.kind == "functional_group":
                finding = _group_finding(constraint, position_map)
            elif constraint.kind == "edge_placement":
                finding = _edge_finding(
                    design, constraint, position_map, footprint_bounds
                )
            elif constraint.kind == "manufacturing_rules":
                finding = _manufacturing_finding(design, constraint)
            elif constraint.kind == "routing":
                finding = _routing_finding(design, constraint, routing)
            elif constraint.kind == "power_budget":
                finding = _power_budget_finding(design, constraint)
            else:
                finding = None
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            finding = _finding(
                "intent.invalid_constraint_params",
                constraint.id,
                f"{constraint.kind} constraint parameters are invalid: {type(exc).__name__}",
                constraint_kind=constraint.kind,
            )
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
        required["i2c_electrical_budget"] = "i2c"
    if any(interface.kind == "spi" for interface in design.interfaces):
        required["spi_electrical_budget"] = "spi"
    if any(interface.kind == "uart" for interface in design.interfaces):
        required["uart_electrical_budget"] = "uart"
    if any(
        graph.get(component.part_id).kind == "ldo_regulator"
        for component in design.components
        if component.part_id in {part["id"] for part in graph.to_dict()["parts"]}
    ):
        required["ldo_regulation_budget"] = "ldo_regulator"
    if any(
        endpoint.role == "voltage_sense"
        for net in design.nets
        for endpoint in net.endpoints
    ):
        required["source_ownership"] = "voltage_sense"
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
                    f"{interface.kind.upper()} nets lack a routing constraint",
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
    footprint_inspections: dict[str, Any] | None,
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
    if (
        constraint.params.get("distance_metric")
        != "minimum_relevant_copper_pad_edge_gap"
    ):
        reasons.append("distance metric is missing or unsupported")
    if constraint.params.get("geometry_evidence") != "native_footprint_pad_rectangles":
        reasons.append("native geometry evidence contract is missing or unsupported")
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
            footprint_inspections is not None
            and cap_position is not None
            and device_position is not None
        ):
            distances = _native_decoupling_pad_distances(
                design,
                graph,
                capacitor,
                device,
                nets,
                cap_position,
                device_position,
                footprint_inspections,
            )
            if len(distances) != len(nets):
                reasons.append("native supply-pad geometry is incomplete")
            elif any(distance > maximum + 1e-9 for distance in distances.values()):
                reasons.append("native supply-pad distance exceeds the declared bound")
        elif (
            approximate_geometry
            and not (
                capacitor.placement is not None
                and capacitor.placement.fixed
                and device.placement is not None
                and device.placement.fixed
            )
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


def _native_decoupling_pad_distances(
    design: Design,
    graph: PartGraph,
    capacitor: Any,
    device: Any,
    nets: list[Any],
    capacitor_position: dict[str, Any],
    device_position: dict[str, Any],
    inspections: dict[str, Any],
) -> dict[str, float]:
    """Measure declared rail gaps from native KiCad footprint pad rectangles."""

    positions: dict[tuple[str, str], tuple[float, float, float, float]] = {}
    for component, origin in (
        (capacitor, capacitor_position),
        (device, device_position),
    ):
        inspection = inspections.get(component.id)
        if inspection is None:
            continue
        part = graph.get(component.part_id)
        for pin in part.pins:
            matching = [
                pad for pad in inspection.pads if pad.number == pin.footprint_pad
            ]
            if len(matching) != 1:
                continue
            pad = matching[0]
            positions[(component.id, pin.number)] = (
                float(origin["x_mm"]) + pad.x_mm,
                float(origin["y_mm"]) + pad.y_mm,
                pad.width_mm / 2,
                pad.height_mm / 2,
            )

    distances: dict[str, float] = {}
    for net in nets:
        capacitor_pads = [
            positions[(endpoint.component, endpoint.pin)]
            for endpoint in net.endpoints
            if endpoint.component == capacitor.id
            and (endpoint.component, endpoint.pin) in positions
        ]
        device_pads = [
            positions[(endpoint.component, endpoint.pin)]
            for endpoint in net.endpoints
            if endpoint.component == device.id
            and (endpoint.component, endpoint.pin) in positions
        ]
        if capacitor_pads and device_pads:
            distances[net.id] = min(
                _rectangle_gap(first, second)
                for first in capacitor_pads
                for second in device_pads
            )
    return distances


def _rectangle_gap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    delta_x = max(0.0, abs(first[0] - second[0]) - first[2] - second[2])
    delta_y = max(0.0, abs(first[1] - second[1]) - first[3] - second[3])
    return math.hypot(delta_x, delta_y)


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


def _i2c_electrical_budget_finding(
    design: Design, constraint: Any
) -> RuleFinding | None:
    interfaces = [
        interface
        for interface in design.interfaces
        if interface.kind == "i2c" and interface.id in constraint.targets
    ]
    reasons: list[str] = []
    if len(interfaces) != 1:
        reasons.append("target set does not identify exactly one I2C interface")
        interface = None
    else:
        interface = interfaces[0]
    try:
        speed_hz = int(constraint.params["speed_hz"])
        pullup_ohm = float(constraint.params["pullup_ohm"])
        capacitance_pf = float(constraint.params["bus_capacitance_pf_max"])
        rise_limit_ns = float(constraint.params["rise_time_limit_ns"])
        declared_rise_ns = float(constraint.params["calculated_rise_time_ns"])
        sink_limit_ma = float(constraint.params["sink_current_limit_ma"])
        declared_sink_ma = float(constraint.params["calculated_sink_current_ma"])
    except (KeyError, TypeError, ValueError):
        return _finding(
            "intent.i2c_electrical_budget",
            constraint.id,
            "I2C electrical budget parameters are incomplete or invalid",
        )
    values = (
        float(speed_hz),
        pullup_ohm,
        capacitance_pf,
        rise_limit_ns,
        declared_rise_ns,
        sink_limit_ma,
        declared_sink_ma,
    )
    if not all(math.isfinite(value) and value > 0 for value in values):
        reasons.append("electrical budget values must be positive and finite")
    expected_limit_ns = 1000.0 if speed_hz <= 100_000 else 300.0
    calculated_rise_ns = 0.8473 * pullup_ohm * capacitance_pf * 1e-3
    power_domain = next(
        (
            domain
            for domain in design.power_domains
            if interface is not None and domain.id == interface.power_domain
        ),
        None,
    )
    calculated_sink_ma = (
        power_domain.max_v / pullup_ohm * 1000
        if power_domain is not None and pullup_ohm > 0
        else math.inf
    )
    if interface is not None:
        expected_params = {
            "speed_hz": speed_hz,
            "pullup_ohm": pullup_ohm,
            "bus_capacitance_pf_max": capacitance_pf,
            "rise_time_limit_ns": rise_limit_ns,
            "external_pullups": constraint.params.get("external_pullups"),
        }
        for name, expected in expected_params.items():
            actual = interface.params.get(name)
            if actual != expected:
                reasons.append(f"interface {name} disagrees with the budget")
    if rise_limit_ns != expected_limit_ns:
        reasons.append("rise-time limit does not match the declared I2C speed")
    if abs(declared_rise_ns - calculated_rise_ns) > 1e-6:
        reasons.append("recorded rise time does not match the RC calculation")
    if calculated_rise_ns > rise_limit_ns + 1e-9:
        reasons.append("calculated RC rise time exceeds the protocol limit")
    if abs(declared_sink_ma - calculated_sink_ma) > 1e-6:
        reasons.append(
            "recorded sink current does not match the worst-case rail calculation"
        )
    if calculated_sink_ma > sink_limit_ma + 1e-9:
        reasons.append("worst-case pull-up current exceeds the declared sink limit")
    if constraint.params.get("external_pullups") != "forbidden":
        reasons.append("external pull-ups are not forbidden by the bounded profile")
    if not reasons:
        return None
    return _finding(
        "intent.i2c_electrical_budget",
        constraint.id,
        "; ".join(sorted(set(reasons))),
        calculated_rise_time_ns=round(calculated_rise_ns, 6),
        calculated_sink_current_ma=round(calculated_sink_ma, 6),
    )


def _spi_electrical_budget_finding(
    design: Design, graph: PartGraph, constraint: Any
) -> RuleFinding | None:
    interfaces = [
        interface
        for interface in design.interfaces
        if interface.kind == "spi" and interface.id in constraint.targets
    ]
    reasons: list[str] = []
    if len(interfaces) != 1:
        reasons.append("target set does not identify exactly one SPI interface")
        interface = None
    else:
        interface = interfaces[0]
    clock = int(constraint.params["clock_hz"])
    clock_limit = int(constraint.params["sensor_clock_limit_hz"])
    mode = int(constraint.params["mode"])
    voltage = float(constraint.params["voltage_v"])
    pullup = float(constraint.params["cs_pullup_ohm"])
    if clock <= 0 or clock > clock_limit:
        reasons.append("SPI clock exceeds the declared sensor limit")
    if mode != 0:
        reasons.append("SPI mode is not the verified mode 0")
    if constraint.params.get("topology") != "four_wire_single_peripheral":
        reasons.append(
            "SPI topology is outside the verified single-peripheral contract"
        )
    sensor = next(
        (
            component
            for component in _target_components(design, constraint.targets)
            if graph.get(component.part_id).kind == "environmental_sensor"
        ),
        None,
    )
    resistor = next(
        (
            component
            for component in _target_components(design, constraint.targets)
            if "resistance_ohm" in graph.get(component.part_id).ratings
        ),
        None,
    )
    if sensor is None:
        reasons.append("SPI sensor target is absent")
    elif clock > int(graph.get(sensor.part_id).ratings.get("max_spi_clock_hz", 0)):
        reasons.append("SPI clock exceeds the trusted sensor rating")
    if resistor is None:
        reasons.append("chip-select pull-up is absent")
    else:
        actual_pullup = float(graph.get(resistor.part_id).ratings["resistance_ohm"])
        resistor_nets = _component_net_ids(design, resistor.id)
        if abs(actual_pullup - pullup) > 1e-9:
            reasons.append("chip-select pull-up value differs from the contract")
        if not {"net_3v3", "net_spi_cs"} <= resistor_nets:
            reasons.append("chip-select pull-up is not connected between 3V3 and CS")
    if interface is not None:
        expected = {
            "clock_hz": clock,
            "mode": mode,
            "voltage_v": voltage,
            "topology": "four_wire_single_peripheral",
            "external_connector": False,
        }
        for key, expected_value in expected.items():
            if interface.params.get(key) != expected_value:
                reasons.append(f"interface {key} disagrees with the SPI budget")
        domain = next(
            (
                domain
                for domain in design.power_domains
                if domain.id == interface.power_domain
            ),
            None,
        )
        if domain is None or not domain.min_v <= voltage <= domain.max_v:
            reasons.append("SPI logic voltage is outside its power domain")
    if not reasons:
        return None
    return _finding(
        "intent.spi_electrical_budget",
        constraint.id,
        "; ".join(sorted(set(reasons))),
        clock_hz=clock,
        sensor_clock_limit_hz=clock_limit,
    )


def _uart_electrical_budget_finding(
    design: Design, constraint: Any
) -> RuleFinding | None:
    interfaces = [
        interface
        for interface in design.interfaces
        if interface.kind == "uart" and interface.id in constraint.targets
    ]
    reasons: list[str] = []
    if len(interfaces) != 1:
        reasons.append("target set does not identify exactly one UART interface")
        interface = None
    else:
        interface = interfaces[0]
    expected = {
        "baud": int(constraint.params["baud"]),
        "data_bits": int(constraint.params["data_bits"]),
        "parity": constraint.params["parity"],
        "stop_bits": int(constraint.params["stop_bits"]),
        "voltage_v": float(constraint.params["voltage_v"]),
        "logic": constraint.params["logic"],
    }
    if expected["baud"] not in {9600, 19200, 38400, 57600, 115200}:
        reasons.append("UART baud is outside the verified set")
    if (
        expected["data_bits"],
        expected["parity"],
        expected["stop_bits"],
    ) != (8, "none", 1):
        reasons.append("UART framing is not 8-N-1")
    if expected["logic"] != "single_ended_cmos_not_rs232":
        reasons.append("UART voltage-level contract is not 3.3 V CMOS-only")
    if interface is not None:
        for key, expected_value in expected.items():
            if interface.params.get(key) != expected_value:
                reasons.append(f"interface {key} disagrees with the UART budget")
        domain = next(
            (
                domain
                for domain in design.power_domains
                if domain.id == interface.power_domain
            ),
            None,
        )
        if domain is None or not domain.min_v <= expected["voltage_v"] <= domain.max_v:
            reasons.append("UART logic voltage is outside its power domain")
    if not reasons:
        return None
    return _finding(
        "intent.uart_electrical_budget",
        constraint.id,
        "; ".join(sorted(set(reasons))),
        baud=expected["baud"],
        logic=expected["logic"],
    )


def _ldo_regulation_budget_finding(
    design: Design, graph: PartGraph, constraint: Any
) -> RuleFinding | None:
    targets = _target_components(design, constraint.targets)
    regulators = [
        component
        for component in targets
        if graph.get(component.part_id).kind == "ldo_regulator"
    ]
    capacitors = [
        component
        for component in targets
        if graph.get(component.part_id).kind == "capacitor"
    ]
    reasons: list[str] = []
    if len(regulators) != 1 or len(capacitors) != 2:
        reasons.append("LDO target set must contain one regulator and two capacitors")
        regulator = None
    else:
        regulator = regulators[0]
    vin = next((domain for domain in design.power_domains if domain.id == "vin5"), None)
    vout = next(
        (domain for domain in design.power_domains if domain.id == "v3v3"), None
    )
    if vin is None or vout is None:
        reasons.append("LDO input or output power domain is absent")
    if regulator is not None:
        ratings = graph.get(regulator.part_id).ratings
        input_rating = ratings.get("input_voltage_v", {})
        if (
            vin is None
            or vin.min_v < float(input_rating.get("min", math.inf))
            or vin.max_v > float(input_rating.get("max", -math.inf))
        ):
            reasons.append("declared input domain exceeds the trusted LDO rating")
        if (
            vout is None
            or abs(vout.nominal_v - float(ratings.get("output_voltage_v", math.inf)))
            > 1e-9
        ):
            reasons.append("declared output domain disagrees with the fixed LDO rating")
        if float(constraint.params["load_limit_a"]) > float(
            ratings.get("max_output_current_a", 0)
        ):
            reasons.append("declared load exceeds the trusted LDO current rating")
        pin_nets = {
            endpoint.pin: net.id
            for net in design.nets
            for endpoint in net.endpoints
            if endpoint.component == regulator.id
        }
        if pin_nets.get("1") != "net_vin" or pin_nets.get("3") != "net_vin":
            reasons.append("LDO enable is not tied to its input rail")
        if pin_nets.get("5") != "net_3v3" or pin_nets.get("2") != "net_gnd":
            reasons.append("LDO input/output/return topology is inconsistent")
    required_caps = {
        "net_vin": float(constraint.params["input_capacitance_f"]),
        "net_3v3": float(constraint.params["output_capacitance_f"]),
    }
    for rail, minimum in required_caps.items():
        matches = [
            capacitor
            for capacitor in capacitors
            if {rail, "net_gnd"} <= _component_net_ids(design, capacitor.id)
        ]
        if (
            len(matches) != 1
            or float(graph.get(matches[0].part_id).ratings.get("capacitance_f", 0))
            < minimum
        ):
            reasons.append(f"{rail} stability/bypass capacitor contract failed")
    if constraint.params.get("enable_policy") != "tied_to_vin":
        reasons.append("LDO enable policy is not tied_to_vin")
    if not reasons:
        return None
    return _finding(
        "intent.ldo_regulation_budget",
        constraint.id,
        "; ".join(sorted(set(reasons))),
        input_min_v=constraint.params.get("input_min_v"),
        input_max_v=constraint.params.get("input_max_v"),
        output_v=constraint.params.get("output_v"),
    )


def _source_ownership_finding(design: Design, constraint: Any) -> RuleFinding | None:
    reasons: list[str] = []
    net = next(
        (
            net
            for net in design.nets
            if net.id in constraint.targets and net.name == "3V3"
        ),
        None,
    )
    if net is None:
        return _finding(
            "intent.source_ownership",
            constraint.id,
            "source-ownership target does not identify the 3V3 rail",
        )
    component_by_id = {component.id: component for component in design.components}
    physical_sources = sorted(
        (endpoint.component, endpoint.pin)
        for endpoint in net.endpoints
        if endpoint.role == "source"
        and endpoint.component in component_by_id
        and not component_by_id[endpoint.component].attributes.get(
            "exclude_from_board", False
        )
    )
    expected_source = constraint.params.get("physical_source_component")
    if [component for component, _pin in physical_sources] != [expected_source]:
        reasons.append("the populated rail does not have exactly one declared source")
    sense_component = constraint.params.get("sense_component")
    sense_pin = constraint.params.get("sense_pin")
    sense_role = constraint.params.get("sense_role")
    sense_matches = [
        endpoint
        for endpoint in net.endpoints
        if endpoint.component == sense_component and endpoint.pin == sense_pin
    ]
    if len(sense_matches) != 1 or sense_matches[0].role != sense_role:
        reasons.append(
            "the UPDI voltage-reference pin is not a sense-only rail endpoint"
        )
    if constraint.params.get("simultaneous_external_power_sources") != "forbidden":
        reasons.append("simultaneous external sources are not explicitly forbidden")
    if not reasons:
        return None
    return _finding(
        "intent.source_ownership",
        constraint.id,
        "; ".join(reasons),
        physical_sources=",".join(
            f"{component}:{pin}" for component, pin in physical_sources
        ),
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
    reference_net_id = constraint.params.get("continuous_reference_net")
    raw_stitching = constraint.params.get("min_reference_stitching_vias")
    stitching_contract_valid = (
        isinstance(raw_stitching, int)
        and not isinstance(raw_stitching, bool)
        and raw_stitching >= 1
    )
    if reference_net_id is not None and not stitching_contract_valid:
        reasons.append(
            "continuous reference plane lacks a positive integer stitching-via contract"
        )
    elif reference_net_id is None and raw_stitching is not None:
        reasons.append("stitching-via contract lacks a reference-net identity")
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
        if reference_net_id is not None:
            reference_net = net_by_id.get(str(reference_net_id))
            planes = routing.get("reference_planes", [])
            if reference_net is None or not any(
                isinstance(plane, dict)
                and str(plane.get("net", "")).lstrip("/") == reference_net.name
                and plane.get("filled") is True
                and _positive_finite(plane.get("area_mm2"))
                for plane in planes
            ):
                reasons.append(
                    "declared continuous reference plane lacks fill evidence"
                )
            minimum_stitching = raw_stitching if stitching_contract_valid else 0
            via_counts = routing.get("via_count_by_net", {})
            actual_stitching = (
                int(via_counts.get(reference_net.name, 0))
                if reference_net is not None and isinstance(via_counts, dict)
                else 0
            )
            if actual_stitching < minimum_stitching:
                reasons.append(
                    "continuous reference plane has fewer stitching vias than declared"
                )
    if not reasons:
        return None
    return _finding(
        "intent.routing",
        constraint.id,
        "; ".join(sorted(set(reasons))),
    )


def _positive_finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _power_budget_finding(design: Design, constraint: Any) -> RuleFinding | None:
    try:
        declared_current = float(constraint.params.get("max_current_a", -1))
        declared_power = float(constraint.params.get("max_power_w", -1))
        voltage_basis = float(constraint.params.get("voltage_basis_v", -1))
    except (TypeError, ValueError, OverflowError):
        return _finding(
            "intent.power_budget",
            constraint.id,
            "power-budget constraint contains non-numeric envelope values",
        )
    expected_power = design.scope.max_voltage_v * design.scope.max_current_a
    if (
        math.isclose(
            declared_current, design.scope.max_current_a, rel_tol=0, abs_tol=1e-12
        )
        and math.isclose(
            declared_power, design.scope.max_power_w, rel_tol=0, abs_tol=1e-12
        )
        and math.isclose(
            design.scope.max_power_w, expected_power, rel_tol=0, abs_tol=1e-12
        )
        and math.isclose(
            voltage_basis,
            design.scope.max_voltage_v,
            rel_tol=0,
            abs_tol=1e-12,
        )
        and constraint.params.get("envelope") == "simultaneous_declared_scope_maxima"
    ):
        return None
    return _finding(
        "intent.power_budget",
        constraint.id,
        "power-budget constraint is inconsistent with the declared scope envelope",
        declared_current_a=declared_current,
        declared_power_w=declared_power,
        expected_power_w=expected_power,
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
