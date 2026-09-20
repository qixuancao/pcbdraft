"""Flat semantic operation normalization for ``ApplicationService``.

This module owns strict argument normalization and grouped-operation
preflight checks. Project writes, native KiCad mutation, routing, and agent
orchestration remain in :mod:`pcbdraft.services.application`.
"""

from __future__ import annotations

import copy
import secrets
from collections.abc import Callable
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.constraint_support import validate_constraint_write
from pcbdraft.domain.ir import Design
from pcbdraft.domain.operations import (
    ConnectGroupEntry,
    PlaceGroupEntry,
    parse_connect_group,
    parse_place_group,
)
from pcbdraft.domain.parts import PartGraph


def _entry_mapping(value: Any, field: str) -> dict[str, Any]:
    """Convert one strict list-of-entries object into a unique mapping."""

    if not isinstance(value, dict) or set(value) != {"entries"}:
        raise ValidationError(f"{field} has an invalid object shape")
    entries = value["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValidationError(f"{field}.entries must be a non-empty array")
    result: dict[str, Any] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"field", "value"}:
            raise ValidationError(f"{field}.entries[{index}] is malformed")
        name = entry["field"]
        if not isinstance(name, str) or not name or name in result:
            raise ValidationError(f"{field} contains a duplicate or invalid field")
        result[name] = entry["value"]
    return result


def _parameter_mapping(value: Any, field: str) -> dict[str, Any]:
    """Convert strict named parameter entries into a domain mapping."""

    if not isinstance(value, list):
        raise ValidationError(f"{field} must be an array")
    result: dict[str, Any] = {}
    for index, entry in enumerate(value):
        if not isinstance(entry, dict) or set(entry) != {"name", "value"}:
            raise ValidationError(f"{field}[{index}] is malformed")
        name = entry["name"]
        if not isinstance(name, str) or not name or name in result:
            raise ValidationError(f"{field} contains a duplicate or invalid name")
        result[name] = entry["value"]
    return result


def _validate_connect_group(
    entries: tuple[ConnectGroupEntry, ...],
    design: Design,
    graph: PartGraph,
) -> None:
    """Resolve every group endpoint and conflict before creating a candidate."""

    components = {item.id: item for item in design.components}
    nets = {item.id: item for item in design.nets}
    connected = {
        (endpoint.component, endpoint.pin): (net.id, endpoint.role)
        for net in design.nets
        for endpoint in net.endpoints
    }
    for entry in entries:
        net = nets.get(entry.net_id)
        if net is None:
            raise ValidationError(
                "semantic_transaction_invalid_net: connect_group references "
                f"absent net {entry.net_id}"
            )
        component = components.get(entry.component_id)
        if component is None:
            raise ValidationError(
                "semantic_transaction_invalid_endpoint: connect_group references "
                f"absent component {entry.component_id}"
            )
        part = graph.get(component.part_id)
        if part.pin(entry.pin) is None:
            raise ValidationError(
                "semantic_transaction_invalid_endpoint: connect_group references "
                f"absent pin {entry.component_id}.{entry.pin}"
            )
        prior = connected.get((entry.component_id, entry.pin))
        if prior == (entry.net_id, entry.role):
            raise ValidationError(
                "semantic_transaction_duplicate: connect_group endpoint is already connected: "
                f"{entry.component_id}.{entry.pin}"
            )
        if prior is not None:
            raise ValidationError(
                "semantic_transaction_conflict: connect_group endpoint is already "
                f"connected to {prior[0]} as {prior[1]}: "
                f"{entry.component_id}.{entry.pin}"
            )


def _validate_place_group(
    entries: tuple[PlaceGroupEntry, ...],
    design: Design,
    graph: PartGraph,
    *,
    routed_component_nets: Callable[[Design, str], tuple[str, ...]] | None = None,
) -> None:
    """Resolve every absolute pose and board bound before creating a candidate."""

    components = {item.id: item for item in design.components}
    for entry in entries:
        component = components.get(entry.component_id)
        if component is None:
            raise ValidationError(
                "semantic_transaction_invalid_component: place_group references "
                f"absent component {entry.component_id}"
            )
        part = graph.get(component.part_id)
        if component.attributes.get("exclude_from_board", False) or (
            part.footprint is None
        ):
            raise ValidationError(
                "semantic_transaction_invalid_component: place_group component "
                f"has no board footprint: {entry.component_id}"
            )
        if not (
            0.0 <= entry.x_mm <= design.board.width_mm
            and 0.0 <= entry.y_mm <= design.board.height_mm
        ):
            raise ValidationError(
                "semantic_transaction_out_of_bounds: place_group pose lies outside "
                f"the board: {entry.component_id}"
            )
        existing = component.placement
        if existing is not None and (
            existing.x_mm,
            existing.y_mm,
            existing.rotation_deg,
            existing.side,
        ) == (
            entry.x_mm,
            entry.y_mm,
            entry.rotation_deg,
            entry.side,
        ):
            raise ValidationError(
                "semantic_transaction_duplicate: place_group pose is already applied: "
                f"{entry.component_id}"
            )
        if routed_component_nets is not None:
            routed_nets = routed_component_nets(design, entry.component_id)
            if routed_nets:
                raise ValidationError(
                    "semantic_transaction_conflict: place_group component retains routed "
                    f"copper on {', '.join(routed_nets)}: {entry.component_id}"
                )


def _flat_semantic_operation(
    tool_name: str,
    arguments: dict[str, Any],
    design: Design,
    *,
    token_hex: Callable[[int], str] = secrets.token_hex,
    deep_copy: Callable[[Any], Any] = copy.deepcopy,
    entry_mapping: Callable[[Any, str], dict[str, Any]] = _entry_mapping,
    parameter_mapping: Callable[[Any, str], dict[str, Any]] = _parameter_mapping,
) -> dict[str, Any]:
    """Normalize one concrete flat PCB tool into a semantic operation."""

    operation_id = f"op_{token_hex(6)}"
    args: dict[str, Any]
    expected: dict[str, Any] = {}
    op = tool_name
    if tool_name.startswith("add_") and tool_name in {
        "add_block",
        "add_component",
        "add_net",
    }:
        value = deep_copy(arguments["value"])
        if tool_name == "add_block":
            value["components"] = []
            value["provenance"] = []
        elif tool_name == "add_net":
            value["endpoints"] = []
        args = {"value": value}
    elif tool_name in {"remove_block", "remove_component", "remove_net"}:
        key = tool_name.removeprefix("remove_") + "_id"
        if tool_name == "remove_net":
            net = next(
                (item for item in design.nets if item.id == arguments[key]),
                None,
            )
            if net is not None and net.name.upper() in {"GND", "GROUND", "VSS"}:
                raise ValidationError(
                    "the required reference-plane net cannot be removed"
                )
        args = {"id": arguments[key]}
    elif tool_name == "update_component":
        args = {
            "component_id": arguments["component_id"],
            "changes": entry_mapping(arguments["changes"], "changes"),
        }
    elif tool_name in {"assign_footprint", "rename_net"}:
        args = dict(arguments)
    elif tool_name in {"connect_pin", "disconnect_pin"}:
        op = "connect" if tool_name == "connect_pin" else "disconnect"
        args = {
            "net_id": arguments["net_id"],
            "endpoint": {
                "component": arguments["component_id"],
                "pin": arguments["pin"],
                "role": arguments["role"],
            },
        }
    elif any(
        tool_name.endswith(suffix)
        for suffix in ("requirement", "power_domain", "interface", "constraint")
    ):
        collection = next(
            name
            for name in ("requirement", "power_domain", "interface", "constraint")
            if tool_name.endswith(name)
        )
        if tool_name.startswith("remove_"):
            op = f"remove_{collection}"
            args = {"id": arguments["id"]}
        else:
            op = (
                "upsert_constraint"
                if collection == "constraint"
                else "upsert_requirement"
                if collection == "requirement"
                else f"upsert_{collection}"
            )
            value = deep_copy(arguments["value"])
            if collection in {"interface", "constraint"}:
                value["params"] = parameter_mapping(
                    value["params"], f"{collection}.params"
                )
            if collection in {"constraint", "requirement"}:
                value["provenance"] = []
            entry_id = str(value["id"])
            exists = (
                any(item.id == entry_id for item in design.requirements)
                if collection == "requirement"
                else any(item.id == entry_id for item in design.power_domains)
                if collection == "power_domain"
                else any(item.id == entry_id for item in design.interfaces)
                if collection == "interface"
                else any(item.id == entry_id for item in design.constraints)
            )
            if tool_name.startswith("add_"):
                if exists:
                    raise ValidationError(
                        f"cannot add existing {collection}: {entry_id}"
                    )
                expected = {"absent": True}
            else:
                if not exists:
                    raise ValidationError(
                        f"cannot update absent {collection}: {entry_id}"
                    )
                expected = {"id": entry_id}
            args = {"value": value}
    elif tool_name == "update_board_rules":
        op = "update_board"
        args = {"changes": entry_mapping(arguments["changes"], "changes")}
    elif tool_name == "set_board_outline":
        args = {
            "width_mm": float(arguments["width_mm"]),
            "height_mm": float(arguments["height_mm"]),
        }
    elif tool_name in {"place_footprint", "move_footprint", "rotate_footprint"}:
        op = "place_footprint"
        component_id = str(arguments["component_id"])
        component = next(
            (item for item in design.components if item.id == component_id), None
        )
        if component is None:
            raise ValidationError(f"component is absent: {component_id}")
        existing = component.placement
        args = {
            "component_id": component_id,
            "x_mm": float(arguments.get("x_mm", existing.x_mm if existing else 0.0)),
            "y_mm": float(arguments.get("y_mm", existing.y_mm if existing else 0.0)),
            "rotation_deg": float(
                arguments.get(
                    "rotation_deg", existing.rotation_deg if existing else 0.0
                )
            ),
            "side": str(arguments.get("side", existing.side if existing else "front")),
            "fixed": True,
        }
    elif tool_name == "move_footprint_reference":
        component_id = str(arguments["component_id"])
        if not any(item.id == component_id for item in design.components):
            raise ValidationError(f"component is absent: {component_id}")
        args = {
            "component_id": component_id,
            "x_mm": float(arguments["x_mm"]),
            "y_mm": float(arguments["y_mm"]),
        }
    elif tool_name == "unplace_footprint":
        args = {"component_id": arguments["component_id"]}
    elif tool_name in {"route_net", "unroute_net"}:
        net = next(
            (item for item in design.nets if item.id == arguments["net_id"]),
            None,
        )
        if net is None:
            raise ValidationError(f"net is absent: {arguments['net_id']}")
        if tool_name == "unroute_net" and net.name.upper() in {
            "GND",
            "GROUND",
            "VSS",
        }:
            raise ValidationError("the required reference-plane net cannot be unrouted")
        args = {"net_id": arguments["net_id"]}
        if tool_name == "route_net":
            args.update({"segments": [], "vias": []})
    elif tool_name == "add_via":
        args = {
            "value": {
                "id": arguments["via_id"],
                "net": arguments["net_id"],
                "x_mm": float(arguments["x_mm"]),
                "y_mm": float(arguments["y_mm"]),
                "diameter_mm": float(arguments["diameter_mm"]),
                "drill_mm": float(arguments["drill_mm"]),
                "from_layer": int(arguments["from_layer"]),
                "to_layer": int(arguments["to_layer"]),
            }
        }
    elif tool_name == "remove_via":
        args = {"via_id": arguments["via_id"]}
    else:
        raise ValidationError(f"flat PCB write is not implemented: {tool_name}")
    return {
        "id": operation_id,
        "op": op,
        "args": args,
        "expected": expected,
        "reason": f"Concrete flat PCB tool {tool_name}.",
    }


def _flat_semantic_operations(
    tool_name: str,
    arguments: dict[str, Any],
    design: Design,
    *,
    graph: PartGraph,
    connect_parser: Callable[[Any], tuple[ConnectGroupEntry, ...]] = (
        parse_connect_group
    ),
    place_parser: Callable[[Any], tuple[PlaceGroupEntry, ...]] = parse_place_group,
    validate_connect_group: Callable[
        [tuple[ConnectGroupEntry, ...], Design, PartGraph], None
    ] = _validate_connect_group,
    validate_place_group: Callable[
        [tuple[PlaceGroupEntry, ...], Design, PartGraph], None
    ] = _validate_place_group,
    operation_builder: Callable[
        [str, dict[str, Any], Design], dict[str, Any]
    ] = _flat_semantic_operation,
) -> list[dict[str, Any]]:
    """Normalize one concrete tool into one atomic semantic change set."""

    if tool_name == "connect_group":
        connection_entries = connect_parser(arguments["connections"])
        validate_connect_group(connection_entries, design, graph)
        return [
            operation_builder("connect_pin", entry.to_tool_arguments(), design)
            for entry in connection_entries
        ]
    if tool_name == "place_group":
        placement_entries = place_parser(arguments["placements"])
        validate_place_group(placement_entries, design, graph)
        return [
            operation_builder("place_footprint", entry.to_tool_arguments(), design)
            for entry in placement_entries
        ]
    operation = operation_builder(tool_name, arguments, design)
    if tool_name in {"add_constraint", "update_constraint"}:
        value = operation.get("args", {}).get("value")
        if not isinstance(value, dict):
            raise ValidationError("constraint write must contain an object value")
        kind = value.get("kind")
        validate_constraint_write(
            kind,
            value.get("params"),
        )
        if kind == "manufacturing_rules":
            expected = {
                "min_track_mm": design.board.min_track_mm,
                "min_clearance_mm": design.board.min_clearance_mm,
                "min_drill_mm": design.board.min_drill_mm,
                "edge_clearance_mm": design.board.edge_clearance_mm,
            }
            mismatches = sorted(
                name
                for name, board_value in expected.items()
                if abs(float(value["params"][name]) - board_value) > 1e-12
            )
            if mismatches:
                raise ValidationError(
                    "manufacturing_rules constraint does not match the board contract: "
                    + ", ".join(mismatches)
                )
    return [operation]
