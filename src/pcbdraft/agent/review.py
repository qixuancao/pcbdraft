"""Deterministic, non-geometric engineering preflight for circuit plans.

Findings are derived from the approved plan, semantic IR, and local part
graph. They are intentionally narrower than electrical or manufacturing
sign-off and do not invoke providers, KiCad, or application persistence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from pcbdraft.agent.plan import AgentDesignRequest, CircuitPlan, _normal_key
from pcbdraft.domain.assertions import evaluate_assertion
from pcbdraft.domain.component_qualification import (
    ComponentQualificationReport,
    qualify_components,
)
from pcbdraft.domain.ir import Component, Design, Net
from pcbdraft.domain.parts import PartGraph, PinDefinition

PLAN_REVIEW_SCHEMA = "pcbdraft-agent-plan-review"
PLAN_REVIEW_VERSION = 2


@dataclass(frozen=True, order=True)
class PlanReviewFinding:
    """One deterministic preflight observation about a generic circuit plan.

    These are deliberately narrower than electrical sign-off.  They describe
    facts derivable from the selected local symbols and the reviewed topology,
    while making residual engineering work explicit rather than fabricating a
    pass result.
    """

    id: str
    severity: str
    outcome: str
    summary: str
    evidence: tuple[str, ...]
    action: str
    requires_human: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "outcome": self.outcome,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "action": self.action,
            "requires_human": self.requires_human,
        }


@dataclass(frozen=True)
class AgentPlanReview:
    """Stable deterministic preflight evidence retained beside a circuit plan."""

    design_id: str
    request_hash: str
    plan_hash: str
    qualification: ComponentQualificationReport
    findings: tuple[PlanReviewFinding, ...]

    @property
    def failed_count(self) -> int:
        return sum(finding.outcome == "fail" for finding in self.findings)

    @property
    def attention_count(self) -> int:
        return sum(finding.outcome != "pass" for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_REVIEW_SCHEMA,
            "version": PLAN_REVIEW_VERSION,
            "design_id": self.design_id,
            "request_hash": self.request_hash,
            "plan_hash": self.plan_hash,
            "summary": {
                "passed": sum(finding.outcome == "pass" for finding in self.findings),
                "failed": self.failed_count,
                "attention_required": self.attention_count,
            },
            "limitations": [
                "Preflight checks topology, local library availability, and exact footprint pad numbers.",
                "It does not establish electrical, regulatory, RF, thermal, layout, or manufacturing correctness.",
            ],
            "component_qualification": self.qualification.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _is_ground_net(net: Net) -> bool:
    return _normal_key(net.name) in {
        "0v",
        "agnd",
        "dgnd",
        "gnd",
        "ground",
        "pgnd",
        "vss",
        "vssa",
        "vssd",
    }


def _power_pin_expectation(name: str) -> str | None:
    key = _normal_key(name)
    if key in {
        "agnd",
        "dgnd",
        "epad",
        "gnd",
        "ground",
        "pgnd",
        "vss",
        "vssa",
        "vssd",
    }:
        return "ground"
    if key in {
        "avcc",
        "dvcc",
        "vbat",
        "vcc",
        "vcca",
        "vccd",
        "vdd",
        "vdda",
        "vddd",
        "vin",
        "vsupply",
    }:
        return "supply"
    return None


def _plausible_power_source(component: Component, pin: PinDefinition) -> bool:
    if pin.electrical_type == "power_out":
        return True
    role = str(component.attributes.get("planner_role", "")).casefold()
    return any(
        token in role
        for token in (
            "battery",
            "connector",
            "converter",
            "power_input",
            "regulator",
            "supply",
        )
    )


def review_agent_plan(
    request: AgentDesignRequest,
    plan: CircuitPlan,
    design: Design,
    graph: PartGraph,
) -> AgentPlanReview:
    """Return deterministic, non-geometric preflight evidence for a plan.

    The compiler already rejects malformed symbols, pins, and duplicate net
    assignments.  This reviewer focuses on the important *missing* evidence
    that a syntactically valid topology can still have: power-pin coverage,
    declared rail sources, I2C pull-up evidence, and decoupling evidence.
    Findings are diagnostic and do not deny a generation attempt.
    """

    components = {component.id: component for component in design.components}
    endpoint_nets: dict[tuple[str, str], Net] = {}
    for net in design.nets:
        for endpoint in net.endpoints:
            endpoint_nets[(endpoint.component, endpoint.pin)] = net

    qualification = qualify_components(design, graph)
    findings: list[PlanReviewFinding] = []
    mapping_failures = qualification.pad_mapping_failures
    findings.append(
        PlanReviewFinding(
            id="parts.footprint_pad_mapping",
            severity="error" if mapping_failures else "info",
            outcome="fail" if mapping_failures else "pass",
            summary=(
                "One or more selected symbols cannot be mapped to actual pads in the selected local footprint."
                if mapping_failures
                else "Every on-board symbol pin maps to an actual pad number in the selected local footprint."
            ),
            evidence=mapping_failures
            or ("all selected local symbol/footprint mappings",),
            action="Select a compatible installed footprint or provide a reviewed pin-to-pad contract.",
            requires_human=bool(mapping_failures),
        )
    )
    provisional = qualification.provisional_references
    findings.append(
        PlanReviewFinding(
            id="parts.datasheet_and_identity_qualification",
            severity="warning" if provisional else "info",
            outcome="unknown" if provisional else "pass",
            summary=(
                "Stock-library parts remain provisional: their datasheet locators and procurement identities have not been qualified."
                if provisional
                else "Every selected part has at least a rule-attributed component record; production qualification remains a separate gate."
            ),
            evidence=provisional or ("no local-library-only parts",),
            action="Review the exact manufacturer part, package, pin map, ratings, lifecycle, and datasheet before fabrication.",
            requires_human=True,
        )
    )
    findings.append(
        PlanReviewFinding(
            id="identity.requested_parts",
            severity="info",
            outcome="pass",
            summary="Every explicitly requested part name is represented by the reviewed plan.",
            evidence=tuple(request.requested_parts) or ("no exact part name supplied",),
            action="Review the selected stock symbol and footprint before generation.",
            requires_human=True,
        )
    )

    uncovered_power_inputs: list[str] = []
    non_power_power_inputs: list[str] = []
    power_input_count = 0
    for component_id, component in sorted(components.items()):
        if component.attributes.get("exclude_from_board", False):
            continue
        part = graph.get(component.part_id)
        for pin in part.pins:
            if pin.electrical_type != "power_in":
                continue
            power_input_count += 1
            label = f"{component.reference}.{pin.number} ({pin.name})"
            power_input_net = endpoint_nets.get((component_id, pin.number))
            if power_input_net is None:
                uncovered_power_inputs.append(label)
            elif power_input_net.net_class != "power":
                non_power_power_inputs.append(f"{label} -> {power_input_net.name}")
    power_issues = uncovered_power_inputs + non_power_power_inputs
    findings.append(
        PlanReviewFinding(
            id="power.input_pin_coverage",
            severity="error" if power_issues else "info",
            outcome="fail" if power_issues else "pass",
            summary=(
                "One or more local-symbol power-input pins are missing a power-net assignment or are assigned to a non-power net."
                if power_issues
                else (
                    "All local-symbol power-input pins are assigned to power-class nets."
                    if power_input_count
                    else "No selected local symbol exposes a power-input pin for this check."
                )
            ),
            evidence=tuple(power_issues)
            or (f"{power_input_count} power-input pins checked",),
            action="Connect every required supply/return pin to an explicit power-class net and verify the datasheet pin policy.",
            requires_human=True,
        )
    )

    polarity_issues: list[str] = []
    classified_power_pins = 0
    for component_id, component in sorted(components.items()):
        if component.attributes.get("exclude_from_board", False):
            continue
        for pin in graph.get(component.part_id).pins:
            expectation = _power_pin_expectation(pin.name)
            if expectation is None or pin.electrical_type != "power_in":
                continue
            polarity_net = endpoint_nets.get((component_id, pin.number))
            if polarity_net is None:
                continue
            classified_power_pins += 1
            correct = (
                _is_ground_net(polarity_net)
                if expectation == "ground"
                else (
                    polarity_net.net_class == "power"
                    and not _is_ground_net(polarity_net)
                )
            )
            if not correct:
                polarity_issues.append(
                    f"{component.reference}.{pin.number} ({pin.name}) -> {polarity_net.name}; expected {expectation}"
                )
    findings.append(
        PlanReviewFinding(
            id="power.pin_polarity",
            severity="error" if polarity_issues else "info",
            outcome="fail" if polarity_issues else "pass",
            summary=(
                "A locally identified supply or ground pin is connected to the wrong power class."
                if polarity_issues
                else "Locally identifiable supply and ground pins have consistent power-net polarity."
            ),
            evidence=tuple(polarity_issues)
            or (f"{classified_power_pins} classified power pins checked",),
            action="Reconnect VSS/GND pins to ground and VDD/VCC/VBAT pins to a non-ground supply, then verify the datasheet.",
            requires_human=True,
        )
    )

    unsourced_rails: list[str] = []
    implausible_sources: list[str] = []
    for net in design.nets:
        if net.net_class != "power" or _normal_key(net.name) in {
            "gnd",
            "ground",
            "vss",
        }:
            continue
        has_source = False
        for endpoint in net.endpoints:
            source_component = components.get(endpoint.component)
            if source_component is not None:
                source_pin = graph.get(source_component.part_id).pin(endpoint.pin)
                if source_pin is not None and source_pin.electrical_type == "power_out":
                    has_source = True
                    break
                if endpoint.role == "source" and source_pin is not None:
                    if _plausible_power_source(source_component, source_pin):
                        has_source = True
                        break
                    implausible_sources.append(
                        f"{net.name}: {source_component.reference}.{source_pin.number} ({source_pin.name})"
                    )
        if not has_source:
            unsourced_rails.append(net.name)
    findings.append(
        PlanReviewFinding(
            id="power.rail_source_evidence",
            severity="error" if unsourced_rails else "info",
            outcome="fail" if unsourced_rails else "pass",
            summary=(
                "A non-ground power rail has no explicit source endpoint or local power-output pin."
                if unsourced_rails
                else "Every non-ground power rail has an explicit source endpoint or local power-output pin."
            ),
            evidence=tuple(unsourced_rails) or ("all non-ground power rails",),
            action="Declare the connector, regulator, battery, or other physical source for each non-ground rail.",
            requires_human=True,
        )
    )
    findings.append(
        PlanReviewFinding(
            id="power.source_role_evidence",
            severity="error" if implausible_sources else "info",
            outcome="fail" if implausible_sources else "pass",
            summary=(
                "A planner-labelled power source is not a local power-output pin or a component with a physical source role."
                if implausible_sources
                else "Every planner-labelled rail source has a plausible local pin type or physical source role."
            ),
            evidence=tuple(sorted(set(implausible_sources)))
            or ("all explicit source roles",),
            action="Use a connector, battery, regulator, converter, or local power-output pin as the declared rail source.",
            requires_human=True,
        )
    )

    contention: list[str] = []
    for net in design.nets:
        drivers: list[str] = []
        for endpoint in net.endpoints:
            driver_component = components[endpoint.component]
            driver_pin = graph.get(driver_component.part_id).pin(endpoint.pin)
            if driver_pin is not None and driver_pin.electrical_type in {
                "output",
                "power_out",
            }:
                drivers.append(f"{driver_component.reference}.{driver_pin.number}")
        if len(drivers) > 1:
            contention.append(f"{net.name}: {', '.join(sorted(drivers))}")
    findings.append(
        PlanReviewFinding(
            id="electrical.output_contention",
            severity="error" if contention else "info",
            outcome="fail" if contention else "pass",
            summary=(
                "A net contains multiple push-pull or power-output pins."
                if contention
                else "No net contains multiple locally identified push-pull or power-output pins."
            ),
            evidence=tuple(contention) or ("declared net drivers",),
            action="Resolve output contention or use an explicitly open-drain/open-collector topology.",
            requires_human=bool(contention),
        )
    )

    i2c_requested = "i2c" in request.scope.domains or any(
        "i2c" in _normal_key(value)
        for value in (*request.functions, *(net.name for net in plan.nets))
    )
    i2c_nets = [
        net
        for net in design.nets
        if "i2c" in _normal_key(net.name) or _normal_key(net.name) in {"sda", "scl"}
    ]
    if not i2c_requested:
        findings.append(
            PlanReviewFinding(
                id="interface.i2c_pullup_evidence",
                severity="info",
                outcome="pass",
                summary="I2C pull-up review is not applicable to the declared request.",
                evidence=("no I2C domain or net declared",),
                action="No I2C-specific action is required by this preflight.",
                requires_human=False,
            )
        )
    elif not i2c_nets:
        findings.append(
            PlanReviewFinding(
                id="interface.i2c_pullup_evidence",
                severity="error",
                outcome="fail",
                summary="The request declares I2C but the plan exposes no SDA/SCL or I2C-labelled net.",
                evidence=("declared scope: i2c",),
                action="Add explicit I2C data and clock nets using selected symbol pins.",
                requires_human=True,
            )
        )
    else:
        i2c_by_id = {net.id: net for net in i2c_nets}
        pullups_by_net: dict[str, list[str]] = {net.id: [] for net in i2c_nets}
        for component_id, component in sorted(components.items()):
            part = graph.get(component.part_id)
            looks_like_resistor = (
                part.symbol.endswith(":R")
                or "pullup"
                in str(component.attributes.get("planner_role", "")).casefold()
            )
            if not looks_like_resistor:
                continue
            component_nets = {
                net.id: net
                for net in design.nets
                if any(endpoint.component == component_id for endpoint in net.endpoints)
            }
            i2c_targets = set(component_nets) & set(i2c_by_id)
            has_non_ground_power = any(
                net.net_class == "power" and not _is_ground_net(net)
                for net in component_nets.values()
            )
            if has_non_ground_power:
                for net_id in i2c_targets:
                    pullups_by_net[net_id].append(component.reference)
        missing_pullups = [
            i2c_by_id[net_id].name
            for net_id, references in pullups_by_net.items()
            if not references
        ]
        pullup_evidence = tuple(
            f"{i2c_by_id[net_id].name}: {', '.join(sorted(references))}"
            for net_id, references in sorted(pullups_by_net.items())
            if references
        )
        findings.append(
            PlanReviewFinding(
                id="interface.i2c_pullup_evidence",
                severity="error" if missing_pullups else "warning",
                outcome="fail" if missing_pullups else "unknown",
                summary=(
                    "One or more I2C signal nets lack an explicit resistor path to a non-ground power rail."
                    if missing_pullups
                    else "Every I2C signal has an explicit pull-up path, but resistance and bus-budget evidence still require review."
                ),
                evidence=tuple(f"missing: {name}" for name in missing_pullups)
                + pullup_evidence,
                action="Verify pull-up topology, resistance, voltage, bus capacitance, and any internal pull-ups against the selected devices' datasheets.",
                requires_human=True,
            )
        )

    passive_shorts: list[str] = []
    polarized_leds = 0
    led_polarity_issues: list[str] = []
    for component_id, component in sorted(components.items()):
        part = graph.get(component.part_id)
        pins = tuple(part.pins)
        if len(pins) == 2 and part.symbol in {
            "Device:C",
            "Device:D",
            "Device:Fuse",
            "Device:L",
            "Device:LED",
            "Device:R",
        }:
            first_net, second_net = (
                endpoint_nets.get((component_id, pin.number)) for pin in pins
            )
            if (
                first_net is not None
                and second_net is not None
                and first_net is second_net
            ):
                passive_shorts.append(
                    f"{component.reference}: both terminals -> {first_net.name}"
                )
        if not part.symbol.endswith(":LED"):
            continue
        cathode = next(
            (pin for pin in pins if _normal_key(pin.name) in {"cathode", "k"}),
            None,
        )
        anode = next(
            (pin for pin in pins if _normal_key(pin.name) in {"a", "anode"}),
            None,
        )
        if cathode is None or anode is None:
            continue
        cathode_net = endpoint_nets.get((component_id, cathode.number))
        anode_net = endpoint_nets.get((component_id, anode.number))
        if cathode_net is None or anode_net is None:
            continue
        if _is_ground_net(cathode_net) or _is_ground_net(anode_net):
            polarized_leds += 1
            if not _is_ground_net(cathode_net) or _is_ground_net(anode_net):
                led_polarity_issues.append(
                    f"{component.reference}: K->{cathode_net.name}, A->{anode_net.name}"
                )
    findings.append(
        PlanReviewFinding(
            id="electrical.two_terminal_short",
            severity="error" if passive_shorts else "info",
            outcome="fail" if passive_shorts else "pass",
            summary=(
                "A two-terminal passive or diode has both pins assigned to the same net."
                if passive_shorts
                else "No selected two-terminal passive or diode is shorted by its net assignments."
            ),
            evidence=tuple(passive_shorts) or ("two-terminal component connectivity",),
            action="Connect the two terminals to their intended distinct nets or remove the component.",
            requires_human=bool(passive_shorts),
        )
    )
    findings.append(
        PlanReviewFinding(
            id="electrical.led_ground_polarity",
            severity="error" if led_polarity_issues else "info",
            outcome="fail" if led_polarity_issues else "pass",
            summary=(
                "A ground-referenced LED has its anode/cathode orientation reversed."
                if led_polarity_issues
                else "Ground-referenced LEDs with locally named A/K pins have consistent polarity."
            ),
            evidence=tuple(led_polarity_issues)
            or (f"{polarized_leds} ground-referenced LEDs checked",),
            action="Connect the cathode (K) toward ground and the anode (A) toward the current-limited positive path.",
            requires_human=bool(led_polarity_issues),
        )
    )

    active_device_ids = {
        component_id
        for component_id, component in components.items()
        if any(
            token in str(component.attributes.get("planner_role", "")).casefold()
            or token in graph.get(component.part_id).symbol.casefold()
            for token in ("mcu", "microcontroller", "sensor", "controller", "regulator")
        )
    }
    capacitors: list[str] = []
    for component_id, component in sorted(components.items()):
        part = graph.get(component.part_id)
        if not (
            part.symbol.endswith(":C")
            or "decoupl" in str(component.attributes.get("planner_role", "")).casefold()
        ):
            continue
        capacitor_nets = {
            net
            for net in design.nets
            if any(endpoint.component == component_id for endpoint in net.endpoints)
        }
        if any(
            _normal_key(net.name) in {"gnd", "ground", "vss"} for net in capacitor_nets
        ) and any(
            net.net_class == "power"
            and _normal_key(net.name) not in {"gnd", "ground", "vss"}
            for net in capacitor_nets
        ):
            capacitors.append(component.reference)
    if not active_device_ids:
        findings.append(
            PlanReviewFinding(
                id="power.decoupling_evidence",
                severity="info",
                outcome="pass",
                summary="No MCU, sensor, controller, or regulator role triggers the generic decoupling preflight.",
                evidence=("no applicable planner role",),
                action="Review any device-specific bypass requirements if the plan evolves.",
                requires_human=True,
            )
        )
    else:
        findings.append(
            PlanReviewFinding(
                id="power.decoupling_evidence",
                severity="error" if not capacitors else "warning",
                outcome="fail" if not capacitors else "unknown",
                summary=(
                    "No explicit capacitor connects a non-ground power rail to ground for the declared active devices."
                    if not capacitors
                    else "The plan contains explicit power-to-ground capacitors, but device-specific value, count, placement, and return-path evidence still require review."
                ),
                evidence=tuple(capacitors)
                or tuple(
                    sorted(components[item].reference for item in active_device_ids)
                ),
                action="Verify each selected device's decoupling network and layout against its datasheet before fabrication.",
                requires_human=True,
            )
        )

    has_manufacturing_contract = any(
        constraint.kind == "manufacturing_rules" for constraint in design.constraints
    )
    findings.append(
        PlanReviewFinding(
            id="constraints.board_manufacturing_envelope",
            severity="info" if has_manufacturing_contract else "error",
            outcome="pass" if has_manufacturing_contract else "fail",
            summary=(
                "The approved board manufacturing envelope is retained as a semantic constraint."
                if has_manufacturing_contract
                else "The semantic design lacks a manufacturing-envelope constraint."
            ),
            evidence=("agent_manufacturing_rules",)
            if has_manufacturing_contract
            else ("missing manufacturing_rules",),
            action="Review the board rules for the intended fabrication process.",
            requires_human=True,
        )
    )

    for assertion in plan.assertions:
        failure = evaluate_assertion(design, graph, assertion.to_ir())
        findings.append(
            PlanReviewFinding(
                id=f"assertion.{assertion.id}",
                severity="error" if failure else "info",
                outcome="fail" if failure else "pass",
                summary=(
                    failure
                    if failure
                    else f"Deterministic assertion {assertion.kind} passed."
                ),
                evidence=assertion.targets,
                action=(
                    "Revise the semantic circuit plan so the declared assertion is true."
                    if failure
                    else assertion.rationale
                ),
                requires_human=False,
            )
        )

    return AgentPlanReview(
        design_id=design.design_id,
        request_hash=hashlib.sha256(request.canonical_bytes()).hexdigest(),
        plan_hash=hashlib.sha256(plan.canonical_bytes()).hexdigest(),
        qualification=qualification,
        findings=tuple(sorted(findings)),
    )
