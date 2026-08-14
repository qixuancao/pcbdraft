"""Deterministic evaluators for model-declared circuit-plan assertions.

Assertions are deliberately small predicates over the semantic IR and trusted
local part graph.  They preserve useful design intent without allowing a model
to provide executable expressions or claim that an unevaluated statement
passed.
"""

from __future__ import annotations

from typing import Any

from pcbdraft.domain.ir import Constraint, Design
from pcbdraft.domain.parts import PartGraph

ASSERTION_KINDS = {
    "all_power_inputs_connected",
    "components_share_net",
    "interface_net_count",
    "net_endpoint_count",
}


def evaluate_assertion(
    design: Design,
    graph: PartGraph,
    constraint: Constraint,
) -> str | None:
    """Return a stable failure reason, or ``None`` when an assertion passes."""

    if constraint.kind != "assertion":
        raise ValueError("constraint is not an assertion")
    predicate = constraint.params.get("predicate")
    if predicate not in ASSERTION_KINDS:
        return "assertion predicate is missing or unsupported"

    components = {component.id: component for component in design.components}
    nets = {net.id: net for net in design.nets}
    interfaces = {interface.id: interface for interface in design.interfaces}

    if predicate == "components_share_net":
        targets = set(constraint.targets)
        if len(targets) < 2 or not targets <= set(components):
            return "components_share_net requires at least two component targets"
        if not any(
            targets <= {endpoint.component for endpoint in net.endpoints}
            for net in design.nets
        ):
            return "declared components do not share one net"
        return None

    if predicate == "net_endpoint_count":
        if len(constraint.targets) != 1 or constraint.targets[0] not in nets:
            return "net_endpoint_count requires exactly one net target"
        return _count_failure(
            len(nets[constraint.targets[0]].endpoints), constraint.params
        )

    if predicate == "interface_net_count":
        if len(constraint.targets) != 1 or constraint.targets[0] not in interfaces:
            return "interface_net_count requires exactly one interface target"
        count = sum(net.interface == constraint.targets[0] for net in design.nets)
        return _count_failure(count, constraint.params)

    target_ids = set(constraint.targets)
    if not target_ids or not target_ids <= set(components):
        return "all_power_inputs_connected requires component targets"
    endpoint_index = {
        (endpoint.component, endpoint.pin)
        for net in design.nets
        for endpoint in net.endpoints
    }
    missing: list[str] = []
    for component_id in sorted(target_ids):
        component = components[component_id]
        part = graph.get(component.part_id)
        for pin in part.pins:
            if (
                pin.electrical_type == "power_in"
                and (
                    component_id,
                    pin.number,
                )
                not in endpoint_index
            ):
                missing.append(f"{component.reference}.{pin.number}")
    if missing:
        return "unconnected power-input pins: " + ", ".join(missing)
    return None


def _count_failure(count: int, params: dict[str, Any]) -> str | None:
    minimum = params.get("minimum")
    maximum = params.get("maximum")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
        return "assertion minimum must be a non-negative integer"
    if maximum is not None and (
        not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < minimum
    ):
        return "assertion maximum must be null or an integer at least minimum"
    if count < minimum:
        return f"observed count {count} is below minimum {minimum}"
    if maximum is not None and count > maximum:
        return f"observed count {count} exceeds maximum {maximum}"
    return None
