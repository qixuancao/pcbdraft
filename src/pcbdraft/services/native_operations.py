"""Native KiCad operation projections and postcondition helpers.

This module owns the pure and adapter-facing checks used by
``ApplicationService`` when it stages a native KiCad mutation.  Keeping the
checks together makes the service's transaction orchestration easier to read
without creating a second publication or project authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.domain.ir import Design
from pcbdraft.domain.operations import parse_place_group
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.consistency import (
    NativeBoardProjection,
    NativeConsistencyReport,
    NativeMismatch,
    NativeOperationDeltaReport,
    NativeSchematicProjection,
)
from pcbdraft.kicad.pcb import inspect_native_board
from pcbdraft.kicad.routing import RoutingFailure, RoutingFailureError
from pcbdraft.kicad.schematic import inspect_native_schematic


class _PCBOperationPostconditionError(ValidationError):
    """Internal expected failure carrying a stable transaction error code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        routing_failure: RoutingFailure | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.routing_failure = routing_failure


def _bind_transaction_failure(exc: BaseException, transaction_id: str) -> None:
    """Attach only an opaque retained receipt identity to an expected failure."""

    if isinstance(exc, PCBDraftError):
        exc.transaction_id = transaction_id  # type: ignore[attr-defined]


def _native_postconditions(
    tool_name: str, report: NativeConsistencyReport
) -> list[dict[str, Any]]:
    codes = {item.code for item in report.mismatches}
    conditions: list[dict[str, Any]] = [
        {
            "name": "native_consistency",
            "passed": report.consistency_passed,
            "mismatch_count": len(report.mismatches),
        }
    ]
    if tool_name == "route_net":
        conditions.extend(
            (
                {
                    "name": "native_nonzero_copper",
                    "passed": "native_zero_copper" not in codes,
                },
                {
                    "name": "native_endpoint_connectivity",
                    "passed": "native_connectivity_failed" not in codes,
                },
                {
                    "name": "native_net_isolation",
                    "passed": not codes
                    & {"unintended_net_merge", "unintended_board_net_merge"},
                },
            )
        )
    return conditions


def _routing_failure_context(design: Design, net_id: str | None) -> tuple[str, ...]:
    net = next((item for item in design.nets if item.id == net_id), None)
    if net is None:
        return (f"layers=board:{design.board.layers}", "order=unknown")
    components = {item.id: item for item in design.components}
    placements: list[str] = []
    for component_id in sorted({item.component for item in net.endpoints})[:8]:
        component = components.get(component_id)
        placement = component.placement if component is not None else None
        placements.append(
            f"{component_id}@"
            + (
                f"{placement.x_mm:.9g},{placement.y_mm:.9g}/{placement.rotation_deg:.9g}/{placement.side}"
                if placement is not None
                else "unplaced"
            )
        )
    order = tuple(item.id for item in sorted(design.nets, key=lambda item: item.id))
    return (
        *("placement=" + item for item in placements),
        f"layers=board:{design.board.layers}",
        f"order={order.index(net.id)}/{len(order)}",
    )


def _consistency_rejection(
    tool_name: str,
    net_id: str | None,
    report: NativeConsistencyReport,
    *,
    design: Design | None = None,
    state_revision: int = 0,
) -> _PCBOperationPostconditionError:
    codes = {item.code for item in report.mismatches}
    shown = ", ".join(sorted(codes)[:4]) or "unknown mismatch"
    if tool_name != "route_net":
        return _PCBOperationPostconditionError(
            "native_consistency_failed",
            f"native KiCad consistency postcondition failed: {shown}",
        )
    if codes & {"unintended_net_merge", "unintended_board_net_merge"}:
        error_code = "unintended_net_merge"
    elif codes & {"native_connectivity_failed", "native_zero_copper"}:
        error_code = "native_connectivity_failed"
    else:
        error_code = "native_commit_failed"
    failure = RoutingFailure(
        code=error_code,
        net=net_id or "unknown",
        expanded_nodes=0,
        blocking_summary=f"native postcondition mismatch: {shown}",
        recommendations=("inspect_native_artifact",),
        nearest_obstacle_class="native_artifact",
        state_revision=state_revision,
        state_context=(
            _routing_failure_context(design, net_id) if design is not None else ()
        ),
    )
    return _PCBOperationPostconditionError(
        error_code,
        failure.diagnostic,
        routing_failure=failure,
    )


def _unavailable_consistency_report(candidate_revision: int) -> NativeConsistencyReport:
    return NativeConsistencyReport(
        candidate_revision,
        "unknown",
        "unknown",
        "not_evaluated",
        (
            NativeMismatch(
                "board_projection_unknown",
                "board",
                "connectivity",
                "evaluated",
                "native inspection failed",
            ),
            NativeMismatch(
                "schematic_projection_unknown",
                "schematic",
                "connectivity",
                "evaluated",
                "native inspection failed",
            ),
        ),
    )


def _native_delta_postconditions(
    report: NativeOperationDeltaReport,
) -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "passed": item.passed,
            "expected": item.expected,
            "observed": item.observed,
        }
        for item in report.checks
    ]


def _native_board_projection(
    managed: Any,
    *,
    inspector: Callable[..., Mapping[str, Any]] | None = None,
    is_complete: Callable[[Any, NativeBoardProjection], bool] | None = None,
) -> NativeBoardProjection:
    snapshots = managed.manifest.get("native_snapshots")
    if not isinstance(snapshots, dict) or "board" not in snapshots:
        raise ValidationError("managed project lacks a native board snapshot")
    snapshot = snapshots["board"]
    projection = NativeBoardProjection.from_snapshot(snapshot)
    completeness_check = is_complete or _native_board_projection_complete
    if completeness_check(snapshot, projection):
        return projection
    refreshed_snapshot = (inspector or inspect_native_board)(
        managed.design,
        managed.board_path,
        include_connectivity=True,
    )
    refreshed = NativeBoardProjection.from_snapshot(refreshed_snapshot)
    if not completeness_check(refreshed_snapshot, refreshed):
        raise ValidationError(
            "native board reinspection lacks operation-delta evidence"
        )
    return refreshed


def _native_board_projection_complete(
    snapshot: Any,
    projection: NativeBoardProjection,
) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    pose_references = {item.reference for item in projection.footprint_poses}
    complete_poses = len(projection.footprint_poses) == len(
        projection.components
    ) and pose_references == set(projection.components)
    reference_text_references = {
        item.reference for item in projection.reference_text_poses
    }
    complete_reference_text_poses = len(projection.reference_text_poses) == len(
        projection.components
    ) and reference_text_references == set(projection.components)
    required_board_rules = {
        "layers",
        "thickness_mm",
        "min_clearance_mm",
        "min_track_mm",
        "min_drill_mm",
        "edge_clearance_mm",
    }
    complete_board_rules = required_board_rules <= {
        key for key, _value in projection.board_rules
    }
    detailed_copper = all(
        item.geometry for item in projection.copper if item.kind in {"segment", "via"}
    )
    complete_components = (
        len(projection.component_artifacts) == len(projection.components)
        and {item.reference for item in projection.component_artifacts}
        == set(projection.components)
        and all(item.part_id is not None for item in projection.component_artifacts)
    )
    complete_nets = isinstance(snapshot.get("nets"), list)
    tracks = snapshot.get("tracks")
    complete_tracks = isinstance(tracks, list) and all(
        isinstance(item, Mapping)
        and (
            (
                item.get("kind") == "segment"
                and "width_mm" in item
                and ("layer_index" in item or "layer" in item)
            )
            or (
                item.get("kind") == "via"
                and {
                    "x_mm",
                    "y_mm",
                    "width_mm",
                    "drill_mm",
                    "from_layer",
                    "to_layer",
                }
                <= set(item)
            )
        )
        for item in tracks
    )
    zones = snapshot.get("zones")
    complete_zones = isinstance(zones, list) and all(
        isinstance(item, Mapping) and isinstance(item.get("pad_connection"), str)
        for item in zones
    )
    return (
        projection.status == "evaluated"
        and complete_poses
        and complete_reference_text_poses
        and complete_board_rules
        and complete_components
        and complete_nets
        and complete_tracks
        and complete_zones
        and len(projection.outline) == 4
        and detailed_copper
    )


def _native_schematic_projection(
    managed: Any,
    *,
    inspector: Callable[..., Mapping[str, Any]] | None = None,
    is_complete: Callable[[NativeSchematicProjection], bool] | None = None,
) -> NativeSchematicProjection:
    snapshots = managed.manifest.get("native_snapshots")
    if not isinstance(snapshots, dict) or "schematic" not in snapshots:
        raise ValidationError("managed project lacks a native schematic snapshot")
    projection = NativeSchematicProjection.from_snapshot(snapshots["schematic"])
    completeness_check = is_complete or _native_schematic_projection_complete
    if completeness_check(projection):
        return projection
    refreshed = NativeSchematicProjection.from_snapshot(
        (inspector or inspect_native_schematic)(
            managed.schematic_path,
            include_connectivity=True,
        )
    )
    if not completeness_check(refreshed):
        raise ValidationError(
            "native schematic reinspection lacks operation-delta evidence"
        )
    return refreshed


def _native_schematic_projection_complete(
    projection: NativeSchematicProjection,
) -> bool:
    return (
        projection.status == "evaluated"
        and len(projection.component_artifacts) == len(projection.components)
        and {item.reference for item in projection.component_artifacts}
        == set(projection.components)
        and all(item.part_id is not None for item in projection.component_artifacts)
    )


def _routed_component_nets(design: Design, component_id: str) -> tuple[str, ...]:
    connected = {
        net.id
        for net in design.nets
        if any(endpoint.component == component_id for endpoint in net.endpoints)
    }
    retained = {route.net for route in design.native_intent.routes}
    retained.update(via.net for via in design.native_intent.vias)
    return tuple(sorted(connected & retained))


def _reject_stale_copper_transform(
    tool_name: str,
    arguments: Mapping[str, Any],
    before: Design,
    candidate: Design,
    *,
    before_graph: PartGraph,
    candidate_graph: PartGraph,
) -> None:
    transform_operations = {
        "place_footprint",
        "place_group",
        "move_footprint",
        "rotate_footprint",
        "unplace_footprint",
    }
    component_contract_operations = {"assign_footprint", "update_component"}
    if tool_name not in transform_operations | component_contract_operations:
        return
    component_ids = (
        tuple(
            entry.component_id for entry in parse_place_group(arguments["placements"])
        )
        if tool_name == "place_group"
        else (str(arguments["component_id"]),)
    )
    for component_id in component_ids:
        before_component = next(
            (item for item in before.components if item.id == component_id), None
        )
        after_component = next(
            (item for item in candidate.components if item.id == component_id), None
        )
        if before_component is None or after_component is None:
            continue
        if tool_name in transform_operations:
            changed = before_component.placement != after_component.placement
        else:
            changed = _component_footprint_contract(
                before_component,
                before_graph,
            ) != _component_footprint_contract(after_component, candidate_graph)
        if not changed:
            continue
        routed_nets = _routed_component_nets(before, component_id)
        if routed_nets:
            raise _PCBOperationPostconditionError(
                "routed_footprint_transform_unsupported",
                "footprint geometry change is blocked until associated retained copper is unrouted: "
                + ", ".join(routed_nets),
            )


def _component_footprint_contract(component: Any, graph: PartGraph) -> object:
    if component.attributes.get("exclude_from_board", False):
        return None
    part = graph.get(component.part_id)
    if part.footprint is None:
        return None
    return (
        part.footprint,
        tuple((pin.number, pin.footprint_pad) for pin in part.pins),
    )


def _operation_failure_code(exc: BaseException, *, stage: str, tool_name: str) -> str:
    if isinstance(exc, RoutingFailureError):
        return exc.failure.code
    if isinstance(exc, _PCBOperationPostconditionError):
        return exc.error_code
    if stage in {"routing_probe", "routing_commit", "native_consistency"} and (
        tool_name == "route_net"
    ):
        return "native_commit_failed"
    if stage == "native_consistency":
        return "native_verification_failed"
    if stage == "native_delta":
        return (
            "native_commit_failed"
            if tool_name == "route_net"
            else "native_delta_failed"
        )
    if stage == "drc":
        return "drc_unavailable"
    if stage == "publication":
        return "publication_failed"
    return "native_materialization_failed"


_FATAL_DRC_MARKERS = (
    "short",
    "clearance",
    "board_edge",
    "copper_edge",
    "hole",
    "courtyard_overlap",
)


def _fatal_drc_count(document: Any) -> int:
    """Count fatal DRC classes from the full native report, never its display cap."""

    total = 0

    def visit(value: Any) -> None:
        nonlocal total
        if isinstance(value, Mapping):
            severity = value.get("severity")
            violation_type = value.get("type")
            if (
                isinstance(severity, str)
                and severity.lower() == "error"
                and isinstance(violation_type, str)
                and any(
                    marker in violation_type.lower() for marker in _FATAL_DRC_MARKERS
                )
            ):
                total += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    return total
