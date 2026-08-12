"""Semantic IR to native KiCad PCB compilation through an isolated pcbnew worker."""

from __future__ import annotations

import math
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compatibility import assert_supported_kicad_version
from .errors import PcbAgentError, ValidationError
from .io import atomic_write_json, load_json_limited
from .ir import Component, Design
from .kicad_schematic import stable_kicad_uuid
from .parts import PartGraph, PartRecord
from .placement import (
    GroupConstraint,
    NearConstraint,
    PlacementItem,
    PlacementResult,
    optimize_placement,
)
from .process import printable_first_line, run_command
from .project import sha256_file
from .routing import GridRouter, RouteSegment, RouteVia, RoutingPad, RoutingResult
from .scope import assert_supported

WORKER_RESULT_LIMIT = 32 * 1024 * 1024
WORKER_OUTPUT_LIMIT = 512 * 1024
PLACEMENT_COURTYARD_MARGIN_MM = 0.25
ROUTING_GEOMETRY_MARGIN_MM = 0.03
ROUTING_GRID_MM = 0.1


@dataclass(frozen=True, order=True)
class InspectedPad:
    index: int
    number: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    layers: tuple[int, ...]


@dataclass(frozen=True)
class FootprintInspection:
    component_id: str
    footprint: str
    bbox_x_mm: float
    bbox_y_mm: float
    width_mm: float
    height_mm: float
    pads: tuple[InspectedPad, ...]


@dataclass(frozen=True)
class PcbGeneration:
    path: Path
    sha256: str
    project_path: Path
    project_sha256: str
    worker_receipt: Path
    worker_receipt_sha256: str
    kicad_version: str
    placements: dict[str, dict[str, float | str | bool]]
    placement_state: str
    placement_objective: float
    placement_iterations: int
    placement_diagnostics: tuple[str, ...]
    routing: RoutingResult
    reference_planes: tuple[dict[str, Any], ...]
    constraint_metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "project_path": str(self.project_path),
            "project_sha256": self.project_sha256,
            "worker_receipt": str(self.worker_receipt),
            "worker_receipt_sha256": self.worker_receipt_sha256,
            "kicad_version": self.kicad_version,
            "placement": {
                "state": self.placement_state,
                "objective": self.placement_objective,
                "iterations": self.placement_iterations,
                "diagnostics": list(self.placement_diagnostics),
                "components": dict(sorted(self.placements.items())),
            },
            "routing": {
                "state": self.routing.state,
                "unrouted": list(self.routing.unrouted),
                "expanded_nodes": self.routing.expanded_nodes,
                "segment_count": len(self.routing.segments),
                "via_count": len(self.routing.vias),
                "length_mm_by_net": _routing_lengths(self.routing),
                "length_mm_by_net_and_width": _routing_lengths_by_width(self.routing),
                "via_count_by_net": _routing_vias(self.routing),
                "width_range_mm_by_net": _routing_width_ranges(self.routing),
                "reference_planes": [dict(item) for item in self.reference_planes],
                "diagnostics": list(self.routing.diagnostics),
            },
            "constraint_metrics": self.constraint_metrics,
        }


def inspect_native_board(
    design: Design,
    board_path: str | Path,
    *,
    system_python: str | Path | None = None,
) -> dict[str, Any]:
    """Return a bounded semantic snapshot of an existing native KiCad board."""
    source = Path(board_path).resolve(strict=True)
    job = {
        "schema": "pcb-agent-pcbnew-job",
        "version": 1,
        "mode": "inspect_board",
        "design_id": design.design_id,
        "board_path": str(source),
    }
    with tempfile.TemporaryDirectory(prefix="pcb-agent-board-inspect-") as temporary:
        result_path = Path(temporary) / "board-inspection.json"
        result = _run_worker(
            "inspect_board",
            job,
            result_path,
            system_python=system_python,
            timeout=30.0,
        )
    if result.get("mode") != "inspect_board":
        raise PcbAgentError("pcbnew worker returned a malformed board inspection")
    assert_supported_kicad_version(str(result.get("kicad_version", "")))
    return result


def _routing_lengths(routing: RoutingResult) -> dict[str, float]:
    values: dict[str, float] = {}
    for segment in routing.segments:
        values[segment.net] = values.get(segment.net, 0.0) + math.hypot(
            segment.x2_mm - segment.x1_mm, segment.y2_mm - segment.y1_mm
        )
    return {name: round(length, 6) for name, length in sorted(values.items())}


def _routing_vias(routing: RoutingResult) -> dict[str, int]:
    values: dict[str, int] = {}
    for via in routing.vias:
        values[via.net] = values.get(via.net, 0) + 1
    return dict(sorted(values.items()))


def _routing_lengths_by_width(
    routing: RoutingResult,
) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = {}
    for segment in routing.segments:
        widths = values.setdefault(segment.net, {})
        key = f"{segment.width_mm:.9f}".rstrip("0").rstrip(".")
        widths[key] = widths.get(key, 0.0) + math.hypot(
            segment.x2_mm - segment.x1_mm, segment.y2_mm - segment.y1_mm
        )
    return {
        name: {width: round(length, 6) for width, length in sorted(widths.items())}
        for name, widths in sorted(values.items())
    }


def _routing_width_ranges(routing: RoutingResult) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {}
    for segment in routing.segments:
        widths = values.setdefault(segment.net, [])
        widths.append(segment.width_mm)
    return {
        name: [round(min(widths), 6), round(max(widths), 6)]
        for name, widths in sorted(values.items())
    }


def generate_pcb(
    design: Design,
    output: str | Path,
    *,
    graph: PartGraph | None = None,
    system_python: str | Path | None = None,
    require_routed: bool = True,
) -> PcbGeneration:
    """Place, route, and materialize a board; never report an unrouted board complete."""
    resolved_graph = graph or PartGraph.bundled()
    assert_supported(design)
    resolved_graph.assert_design(design, check_libraries=True)
    target = Path(output).resolve(strict=False)
    if target.suffix != ".kicad_pcb":
        raise ValidationError("PCB output must end in .kicad_pcb")
    if target.is_symlink():
        raise ValidationError("refusing to replace a PCB symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    board_components = tuple(
        component
        for component in design.components
        if not component.attributes.get("exclude_from_board", False)
        and resolved_graph.get(component.part_id).footprint is not None
    )
    if not board_components:
        raise ValidationError("design contains no board components")

    inspection, kicad_version = inspect_footprints(
        design,
        board_components,
        resolved_graph,
        system_python=system_python,
    )
    assert_supported_kicad_version(kicad_version)
    placement_result, placements = _place(design, board_components, inspection)
    if placement_result.state != "completed":
        raise ValidationError(
            "placement constraints remain unsatisfied: "
            + "; ".join(placement_result.diagnostics)
        )
    routing = _route(
        design,
        board_components,
        resolved_graph,
        inspection,
        placements,
    )
    if require_routed and routing.unrouted:
        raise ValidationError(
            "bounded router left nets unrouted: " + ", ".join(routing.unrouted)
        )
    constraint_metrics = _native_constraint_metrics(
        design,
        board_components,
        resolved_graph,
        inspection,
        placements,
    )

    job = _build_job(
        design,
        board_components,
        resolved_graph,
        placements,
        routing.segments,
        routing.vias,
    )
    result = _run_worker(
        "build", job, target, system_python=system_python, timeout=60.0
    )
    if result.get("mode") != "build" or not isinstance(result.get("sha256"), str):
        raise PcbAgentError("pcbnew worker returned a malformed build receipt")
    reference_planes = _parse_reference_planes(result.get("reference_planes"))
    assert_supported_kicad_version(str(result.get("kicad_version", "")))
    actual_hash = sha256_file(target, max_bytes=128 * 1024 * 1024)
    if actual_hash != result["sha256"]:
        raise PcbAgentError("pcbnew worker board hash does not match its receipt")
    receipt = target.with_suffix(".worker-result.json")
    project_path = target.with_suffix(".kicad_pro")
    project_hash = sha256_file(project_path, max_bytes=16 * 1024 * 1024)
    if project_hash != result.get("project_sha256"):
        raise PcbAgentError("pcbnew worker project hash does not match its receipt")
    return PcbGeneration(
        path=target,
        sha256=actual_hash,
        project_path=project_path,
        project_sha256=project_hash,
        worker_receipt=receipt,
        worker_receipt_sha256=sha256_file(receipt, max_bytes=WORKER_RESULT_LIMIT),
        kicad_version=str(result.get("kicad_version", kicad_version)),
        placements=placements,
        placement_state=placement_result.state,
        placement_objective=placement_result.objective,
        placement_iterations=placement_result.iterations,
        placement_diagnostics=placement_result.diagnostics,
        routing=routing,
        reference_planes=reference_planes,
        constraint_metrics=constraint_metrics,
    )


def _parse_reference_planes(value: Any) -> tuple[dict[str, Any], ...]:
    required = {"net", "layer", "filled", "area_mm2", "pad_connection"}
    if not isinstance(value, list) or len(value) != 1:
        raise PcbAgentError("pcbnew worker did not return one reference plane")
    plane = value[0]
    if not isinstance(plane, dict) or set(plane) != required:
        raise PcbAgentError("pcbnew worker returned a malformed reference plane")
    area = plane.get("area_mm2")
    if (
        not isinstance(plane.get("net"), str)
        or plane["net"].lstrip("/") != "GND"
        or plane.get("layer") not in {"B.Cu", "In1.Cu"}
        or plane.get("filled") is not True
        or isinstance(area, bool)
        or not isinstance(area, (int, float))
        or not math.isfinite(float(area))
        or float(area) <= 0
        or plane.get("pad_connection") != "thermal_relief"
    ):
        raise PcbAgentError("pcbnew worker reference-plane evidence is invalid")
    return (dict(plane),)


def inspect_footprints(
    design: Design,
    components: tuple[Component, ...],
    graph: PartGraph,
    *,
    system_python: str | Path | None = None,
) -> tuple[dict[str, FootprintInspection], str]:
    job = {
        "schema": "pcb-agent-pcbnew-job",
        "version": 1,
        "mode": "inspect",
        "design_id": design.design_id,
        "board": {"layers": design.board.layers},
        "components": [
            {
                "id": component.id,
                "footprint": graph.get(component.part_id).footprint,
                "rotation_deg": _placement(component).rotation_deg,
                "side": _placement(component).side,
            }
            for component in sorted(components, key=lambda entry: entry.id)
        ],
    }
    with tempfile.TemporaryDirectory(prefix="pcb-agent-inspect-") as temporary:
        result_path = Path(temporary) / "inspection.json"
        result = _run_worker(
            "inspect",
            job,
            result_path,
            system_python=system_python,
            timeout=30.0,
        )
    if result.get("mode") != "inspect" or not isinstance(
        result.get("components"), list
    ):
        raise PcbAgentError("pcbnew worker returned a malformed inspection result")
    inspections: dict[str, FootprintInspection] = {}
    try:
        for entry in result["components"]:
            if not isinstance(entry, dict) or set(entry) != {
                "id",
                "footprint",
                "bbox",
                "pads",
            }:
                raise TypeError
            bbox = entry["bbox"]
            if not isinstance(bbox, dict) or set(bbox) != {
                "x_mm",
                "y_mm",
                "width_mm",
                "height_mm",
            }:
                raise TypeError
            pads = tuple(_parse_pad(pad) for pad in entry["pads"])
            inspection = FootprintInspection(
                component_id=str(entry["id"]),
                footprint=str(entry["footprint"]),
                bbox_x_mm=float(bbox["x_mm"]),
                bbox_y_mm=float(bbox["y_mm"]),
                width_mm=float(bbox["width_mm"]),
                height_mm=float(bbox["height_mm"]),
                pads=tuple(sorted(pads)),
            )
            if inspection.component_id in inspections:
                raise ValueError
            inspections[inspection.component_id] = inspection
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PcbAgentError(
            "pcbnew worker returned invalid footprint geometry"
        ) from exc
    expected = {component.id for component in components}
    if set(inspections) != expected:
        raise PcbAgentError("pcbnew inspection omitted or added components")
    version = str(result.get("kicad_version", "unknown"))
    assert_supported_kicad_version(version)
    return inspections, version


def _parse_pad(value: Any) -> InspectedPad:
    if not isinstance(value, dict) or set(value) != {
        "index",
        "number",
        "x_mm",
        "y_mm",
        "width_mm",
        "height_mm",
        "layers",
    }:
        raise TypeError
    layers = value["layers"]
    if not isinstance(layers, list) or not all(
        isinstance(layer, int) and not isinstance(layer, bool) for layer in layers
    ):
        raise TypeError
    return InspectedPad(
        index=int(value["index"]),
        number=str(value["number"]),
        x_mm=float(value["x_mm"]),
        y_mm=float(value["y_mm"]),
        width_mm=float(value["width_mm"]),
        height_mm=float(value["height_mm"]),
        layers=tuple(layers),
    )


def _place(
    design: Design,
    components: tuple[Component, ...],
    inspections: dict[str, FootprintInspection],
) -> tuple[PlacementResult, dict[str, dict[str, float | str | bool]]]:
    items: list[PlacementItem] = []
    offsets: dict[str, tuple[float, float]] = {}
    board_ids = {component.id for component in components}
    for component in components:
        placement = _placement(component)
        info = inspections[component.id]
        offset_x = info.bbox_x_mm + info.width_mm / 2
        offset_y = info.bbox_y_mm + info.height_mm / 2
        offsets[component.id] = (offset_x, offset_y)
        items.append(
            PlacementItem(
                id=component.id,
                x_mm=placement.x_mm + offset_x,
                y_mm=placement.y_mm + offset_y,
                width_mm=info.width_mm + PLACEMENT_COURTYARD_MARGIN_MM,
                height_mm=info.height_mm + PLACEMENT_COURTYARD_MARGIN_MM,
                fixed=placement.fixed,
            )
        )
    nets = tuple(
        tuple(
            sorted(
                {
                    endpoint.component
                    for endpoint in net.endpoints
                    if endpoint.component in board_ids
                }
            )
        )
        for net in design.nets
    )
    near: list[NearConstraint] = []
    groups: list[GroupConstraint] = []
    item_by_id = {item.id: item for item in items}
    for constraint in design.constraints:
        if constraint.kind == "decoupling":
            targets = [target for target in constraint.targets if target in board_ids]
            maximum = constraint.params.get("max_distance_mm")
            if len(targets) >= 2 and isinstance(maximum, (int, float)):
                first = item_by_id[targets[0]]
                second = item_by_id[targets[1]]
                # Component-level centers cannot be closer than their physical
                # rectangles without overlap.  Preserve the electrical bound in
                # the IR; this coarse placer uses the nearest feasible grid bound,
                # while the geometric validator measures the actual supply pads.
                feasible = min(
                    (first.width_mm + second.width_mm) / 2,
                    (first.height_mm + second.height_mm) / 2,
                )
                placement_bound = max(float(maximum), math.ceil(feasible / 0.25) * 0.25)
                near.append(
                    NearConstraint(targets[0], targets[1], placement_bound, 100.0)
                )
        elif constraint.kind == "functional_group":
            targets = tuple(
                target for target in constraint.targets if target in board_ids
            )
            maximum = constraint.params.get("max_diameter_mm")
            if len(targets) >= 2 and isinstance(maximum, (int, float)):
                groups.append(GroupConstraint(targets, float(maximum), 20.0))
    result = optimize_placement(
        items,
        board_width_mm=design.board.width_mm,
        board_height_mm=design.board.height_mm,
        edge_clearance_mm=design.board.edge_clearance_mm,
        nets=nets,
        near=near,
        groups=groups,
        grid_mm=0.25,
        max_iterations=60,
    )
    centers = result.by_id()
    placements: dict[str, dict[str, float | str | bool]] = {}
    for component in components:
        center = centers[component.id]
        offset_x, offset_y = offsets[component.id]
        seed = _placement(component)
        placements[component.id] = {
            "x_mm": round(center.x_mm - offset_x, 9),
            "y_mm": round(center.y_mm - offset_y, 9),
            "rotation_deg": seed.rotation_deg,
            "side": seed.side,
            "fixed": seed.fixed,
        }
    return result, placements


def _route(
    design: Design,
    components: tuple[Component, ...],
    graph: PartGraph,
    inspections: dict[str, FootprintInspection],
    placements: dict[str, dict[str, float | str | bool]],
) -> RoutingResult:
    endpoint_nets: dict[tuple[str, str], str] = {}
    component_by_id = {component.id: component for component in components}
    for net in design.nets:
        for endpoint in net.endpoints:
            component = component_by_id.get(endpoint.component)
            if component is None:
                continue
            contracted_pin = graph.get(component.part_id).pin(endpoint.pin)
            if contracted_pin is None:
                raise ValidationError(
                    f"part mapping disappeared for {component.id}/{endpoint.pin}"
                )
            key = (component.id, contracted_pin.footprint_pad)
            if key in endpoint_nets:
                raise ValidationError(
                    f"footprint pad is assigned more than once: {component.id}/{contracted_pin.footprint_pad}"
                )
            endpoint_nets[key] = net.name

    pads: list[RoutingPad] = []
    escape_segments: list[RouteSegment] = []
    for component in components:
        origin = placements[component.id]
        info = inspections[component.id]
        for pad in inspections[component.id].pads:
            if not pad.layers:
                continue
            net_name = endpoint_nets.get((component.id, pad.number))
            if net_name is None:
                net_name = f"__obstacle__{component.id}_{pad.index}"
            pads.append(
                RoutingPad(
                    id=f"{component.id}/{pad.index}/{pad.number}",
                    net=net_name,
                    x_mm=float(origin["x_mm"]) + pad.x_mm,
                    y_mm=float(origin["y_mm"]) + pad.y_mm,
                    width_mm=pad.width_mm,
                    height_mm=pad.height_mm,
                    layers=pad.layers,
                )
            )
            if (
                not net_name.startswith("__obstacle__")
                and min(pad.width_mm, pad.height_mm) < 0.5
            ):
                escape_segments.append(
                    _escape_segment(
                        net_name,
                        pad,
                        info,
                        float(origin["x_mm"]),
                        float(origin["y_mm"]),
                        design.board.min_track_mm,
                        design.board.min_clearance_mm + ROUTING_GEOMETRY_MARGIN_MM,
                    )
                )
    widths = {net.name: max(design.board.min_track_mm, 0.25) for net in design.nets}
    net_name_by_id = {net.id: net.name for net in design.nets}
    for constraint in design.constraints:
        if constraint.kind != "routing":
            continue
        width = constraint.params.get("width_mm")
        if not isinstance(width, (int, float)) or isinstance(width, bool):
            continue
        for target in constraint.targets:
            if target in net_name_by_id:
                widths[net_name_by_id[target]] = float(width)
    router = GridRouter(
        board_width_mm=design.board.width_mm,
        board_height_mm=design.board.height_mm,
        layers=design.board.layers,
        clearance_mm=design.board.min_clearance_mm + ROUTING_GEOMETRY_MARGIN_MM,
        min_track_mm=design.board.min_track_mm,
        min_drill_mm=design.board.min_drill_mm,
        edge_clearance_mm=design.board.edge_clearance_mm,
        grid_mm=ROUTING_GRID_MM,
        max_expansions=750_000,
    )
    routed = router.route(
        pads,
        widths=widths,
        power_nets=(net.name for net in design.nets if net.net_class == "power"),
        seed_segments=escape_segments,
    )
    return _add_reference_stitching_vias(
        design,
        components,
        graph,
        tuple(pads),
        routed,
    )


def _add_reference_stitching_vias(
    design: Design,
    components: tuple[Component, ...],
    graph: PartGraph,
    pads: tuple[RoutingPad, ...],
    routing: RoutingResult,
) -> RoutingResult:
    """Add deterministic through-via ties from routed GND copper to its plane.

    Candidate centers are sampled on existing GND tracks and rejected against
    foreign pads, tracks, vias, and the board edge.  This keeps geometry in a
    deterministic solver instead of asking an LLM to guess stitching locations.
    """
    net_by_id = {net.id: net for net in design.nets}
    reference_contracts = [
        constraint
        for constraint in design.constraints
        if constraint.kind == "routing"
        and constraint.params.get("continuous_reference_net") is not None
    ]
    if not reference_contracts:
        return routing
    reference_ids = {
        str(constraint.params["continuous_reference_net"])
        for constraint in reference_contracts
    }
    if len(reference_ids) != 1 or not reference_ids <= set(net_by_id):
        raise ValidationError("routing constraints require one valid reference net")
    reference_net = net_by_id[next(iter(reference_ids))].name
    raw_minimums = [
        constraint.params.get("min_reference_stitching_vias")
        for constraint in reference_contracts
    ]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1
        for value in raw_minimums
    ):
        raise ValidationError(
            "reference-plane stitching-via minimum must be a positive integer"
        )
    minimum = max(raw_minimums)

    diameter = max(design.board.min_drill_mm + 0.3, 0.6)
    drill = max(design.board.min_drill_mm, 0.3)
    radius = diameter / 2
    clearance = design.board.min_clearance_mm + ROUTING_GEOMETRY_MARGIN_MM
    candidates: set[tuple[float, float]] = set()
    for segment in routing.segments:
        if segment.net != reference_net:
            continue
        length = math.hypot(
            segment.x2_mm - segment.x1_mm, segment.y2_mm - segment.y1_mm
        )
        steps = max(1, math.ceil(length / ROUTING_GRID_MM))
        for index in range(steps + 1):
            fraction = index / steps
            point = (
                round(segment.x1_mm + (segment.x2_mm - segment.x1_mm) * fraction, 6),
                round(segment.y1_mm + (segment.y2_mm - segment.y1_mm) * fraction, 6),
            )
            if _stitching_candidate_is_clear(
                point,
                reference_net=reference_net,
                radius=radius,
                clearance=clearance,
                design=design,
                pads=pads,
                routing=routing,
            ):
                candidates.add(point)

    targets = _reference_stitching_targets(
        design, components, graph, pads, reference_net
    )
    selected: list[tuple[float, float]] = []
    available = sorted(candidates)
    for target in targets:
        choices = sorted(
            available,
            key=lambda point: (math.dist(point, target), point[0], point[1]),
        )
        choice = next(
            (
                point
                for point in choices
                if math.dist(point, target) <= 6.0
                and all(math.dist(point, other) >= 2.0 for other in selected)
            ),
            None,
        )
        if choice is not None:
            selected.append(choice)
            available.remove(choice)
        if len(selected) >= minimum:
            break
    while len(selected) < minimum:
        choices = [
            point
            for point in available
            if all(math.dist(point, other) >= 2.0 for other in selected)
        ]
        if not choices:
            raise ValidationError(
                "could not place the declared number of safe reference-plane stitching vias"
            )
        choice = max(
            choices,
            key=lambda point: (
                min(
                    (math.dist(point, other) for other in selected),
                    default=math.inf,
                ),
                -point[0],
                -point[1],
            ),
        )
        selected.append(choice)
        available.remove(choice)

    stitching = tuple(
        RouteVia(
            net=reference_net,
            x_mm=point[0],
            y_mm=point[1],
            diameter_mm=diameter,
            drill_mm=drill,
            from_layer=0,
            to_layer=design.board.layers - 1,
        )
        for point in sorted(selected)
    )
    return RoutingResult(
        segments=routing.segments,
        vias=tuple(sorted((*routing.vias, *stitching))),
        unrouted=routing.unrouted,
        state=routing.state,
        expanded_nodes=routing.expanded_nodes,
        diagnostics=tuple(
            sorted(
                (
                    *routing.diagnostics,
                    f"added {len(stitching)} deterministic {reference_net} reference-plane stitching vias",
                )
            )
        ),
    )


def _stitching_candidate_is_clear(
    point: tuple[float, float],
    *,
    reference_net: str,
    radius: float,
    clearance: float,
    design: Design,
    pads: tuple[RoutingPad, ...],
    routing: RoutingResult,
) -> bool:
    x_mm, y_mm = point
    edge_limit = design.board.edge_clearance_mm + radius
    if not (
        edge_limit <= x_mm <= design.board.width_mm - edge_limit
        and edge_limit <= y_mm <= design.board.height_mm - edge_limit
    ):
        return False
    for pad in pads:
        gap = _point_rectangle_gap(point, pad)
        required = radius + (0.1 if pad.net == reference_net else clearance)
        if gap + 1e-9 < required:
            return False
    for segment in routing.segments:
        if segment.net == reference_net:
            continue
        gap = _point_segment_distance(point, segment)
        if gap + 1e-9 < radius + segment.width_mm / 2 + clearance:
            return False
    for via in routing.vias:
        required = radius + via.diameter_mm / 2
        if via.net != reference_net:
            required += clearance
        if math.dist(point, (via.x_mm, via.y_mm)) + 1e-9 < required:
            return False
    return True


def _reference_stitching_targets(
    design: Design,
    components: tuple[Component, ...],
    graph: PartGraph,
    pads: tuple[RoutingPad, ...],
    reference_net: str,
) -> tuple[tuple[float, float], ...]:
    component_by_id = {component.id: component for component in components}
    pad_by_key: dict[tuple[str, str], RoutingPad] = {}
    for pad in pads:
        component_id, _index, number = pad.id.split("/", 2)
        pad_by_key[(component_id, number)] = pad
    targets: list[tuple[float, float]] = []
    for constraint in sorted(design.constraints, key=lambda item: item.id):
        if constraint.kind != "decoupling":
            continue
        capacitors = [
            component_by_id[target]
            for target in constraint.targets
            if target in component_by_id
            and "capacitance_f" in graph.get(component_by_id[target].part_id).ratings
        ]
        for capacitor in capacitors:
            reference_endpoints = [
                endpoint
                for net in design.nets
                if net.name == reference_net
                for endpoint in net.endpoints
                if endpoint.component == capacitor.id
            ]
            for endpoint in reference_endpoints:
                pin = graph.get(capacitor.part_id).pin(endpoint.pin)
                if pin is None:
                    continue
                pad = pad_by_key.get((capacitor.id, pin.footprint_pad))
                if pad is not None:
                    targets.append((pad.x_mm, pad.y_mm))
    if not targets:
        targets.extend((pad.x_mm, pad.y_mm) for pad in pads if pad.net == reference_net)
    return tuple(sorted(set(targets)))


def _point_rectangle_gap(point: tuple[float, float], pad: RoutingPad) -> float:
    delta_x = max(0.0, abs(point[0] - pad.x_mm) - pad.width_mm / 2)
    delta_y = max(0.0, abs(point[1] - pad.y_mm) - pad.height_mm / 2)
    return math.hypot(delta_x, delta_y)


def _point_segment_distance(point: tuple[float, float], segment: RouteSegment) -> float:
    start = (segment.x1_mm, segment.y1_mm)
    end = (segment.x2_mm, segment.y2_mm)
    vector = (end[0] - start[0], end[1] - start[1])
    squared = vector[0] ** 2 + vector[1] ** 2
    if squared <= 1e-18:
        return math.dist(point, start)
    projection = (
        (point[0] - start[0]) * vector[0] + (point[1] - start[1]) * vector[1]
    ) / squared
    bounded = max(0.0, min(1.0, projection))
    closest = (start[0] + bounded * vector[0], start[1] + bounded * vector[1])
    return math.dist(point, closest)


def _native_constraint_metrics(
    design: Design,
    components: tuple[Component, ...],
    graph: PartGraph,
    inspections: dict[str, FootprintInspection],
    placements: dict[str, dict[str, float | str | bool]],
) -> dict[str, Any]:
    """Record exact native pad-rectangle metrics used by L3 decoupling checks."""
    component_by_id = {component.id: component for component in components}
    pad_positions: dict[tuple[str, str], tuple[float, float, float, float]] = {}
    for component in components:
        origin = placements[component.id]
        part = graph.get(component.part_id)
        for pin in part.pins:
            matching = [
                pad
                for pad in inspections[component.id].pads
                if pad.number == pin.footprint_pad
            ]
            if len(matching) != 1:
                raise ValidationError(
                    f"cannot derive native pad metric for {component.id}/{pin.number}"
                )
            pad = matching[0]
            pad_positions[(component.id, pin.number)] = (
                float(origin["x_mm"]) + pad.x_mm,
                float(origin["y_mm"]) + pad.y_mm,
                pad.width_mm / 2,
                pad.height_mm / 2,
            )
    result: dict[str, Any] = {}
    for constraint in sorted(design.constraints, key=lambda item: item.id):
        if constraint.kind != "decoupling":
            continue
        targets = [
            component_by_id[target]
            for target in constraint.targets
            if target in component_by_id
        ]
        capacitors = [
            component
            for component in targets
            if "capacitance_f" in graph.get(component.part_id).ratings
        ]
        devices = [component for component in targets if component not in capacitors]
        distances: dict[str, float] = {}
        if len(capacitors) == 1 and len(devices) == 1:
            capacitor, device = capacitors[0], devices[0]
            for net in design.nets:
                if net.id not in constraint.targets:
                    continue
                cap_points = [
                    pad_positions[(endpoint.component, endpoint.pin)]
                    for endpoint in net.endpoints
                    if endpoint.component == capacitor.id
                ]
                device_points = [
                    pad_positions[(endpoint.component, endpoint.pin)]
                    for endpoint in net.endpoints
                    if endpoint.component == device.id
                ]
                if cap_points and device_points:
                    distances[net.name] = min(
                        _pad_rectangle_gap(first, second)
                        for first in cap_points
                        for second in device_points
                    )
        maximum = float(constraint.params["max_distance_mm"])
        metric_valid = (
            constraint.params.get("distance_metric")
            == "minimum_relevant_copper_pad_edge_gap"
            and constraint.params.get("geometry_evidence")
            == "native_footprint_pad_rectangles"
        )
        result[constraint.id] = {
            "kind": "decoupling",
            "metric": constraint.params.get("distance_metric"),
            "geometry_evidence": constraint.params.get("geometry_evidence"),
            "distance_mm_by_net": {
                name: round(distance, 6) for name, distance in sorted(distances.items())
            },
            "maximum_mm": maximum,
            "outcome": "pass"
            if metric_valid
            and len(distances) >= 2
            and all(distance <= maximum + 1e-9 for distance in distances.values())
            else "fail",
        }
    if any(metric["outcome"] != "pass" for metric in result.values()):
        raise ValidationError("native decoupling geometry violates its declared metric")
    return result


def _pad_rectangle_gap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    delta_x = max(0.0, abs(first[0] - second[0]) - first[2] - second[2])
    delta_y = max(0.0, abs(first[1] - second[1]) - first[3] - second[3])
    return math.hypot(delta_x, delta_y)


def _escape_segment(
    net: str,
    pad: InspectedPad,
    footprint: FootprintInspection,
    origin_x: float,
    origin_y: float,
    width: float,
    clearance: float,
) -> RouteSegment:
    """Create a short deterministic fine-pitch neckdown to open routing space."""
    bbox_center_x = footprint.bbox_x_mm + footprint.width_mm / 2
    bbox_center_y = footprint.bbox_y_mm + footprint.height_mm / 2
    start_x = origin_x + pad.x_mm
    start_y = origin_y + pad.y_mm
    extension = clearance + 0.3
    if abs(pad.x_mm - bbox_center_x) >= abs(pad.y_mm - bbox_center_y):
        target_x = (
            origin_x + footprint.bbox_x_mm - extension
            if pad.x_mm < bbox_center_x
            else origin_x + footprint.bbox_x_mm + footprint.width_mm + extension
        )
        target_y = start_y
    else:
        target_x = start_x
        target_y = (
            origin_y + footprint.bbox_y_mm - extension
            if pad.y_mm < bbox_center_y
            else origin_y + footprint.bbox_y_mm + footprint.height_mm + extension
        )
    # The physical pad center need not lie on the routing grid.  Terminating at
    # an exact grid point prevents microscopic open ends when the main route is
    # materialized from integer cells; KiCad accepts the resulting shallow-angle
    # fan-out segment and independently checks its clearance.
    target_x = round(target_x / ROUTING_GRID_MM) * ROUTING_GRID_MM
    target_y = round(target_y / ROUTING_GRID_MM) * ROUTING_GRID_MM
    return RouteSegment(
        net=net,
        layer=pad.layers[0],
        x1_mm=start_x,
        y1_mm=start_y,
        x2_mm=target_x,
        y2_mm=target_y,
        width_mm=width,
    )


def _build_job(
    design: Design,
    components: tuple[Component, ...],
    graph: PartGraph,
    placements: dict[str, dict[str, float | str | bool]],
    segments: tuple[RouteSegment, ...],
    vias: tuple[RouteVia, ...],
) -> dict[str, Any]:
    board_ids = {component.id for component in components}
    nets = []
    connected: set[tuple[str, str]] = set()
    board_net_names: dict[str, str] = {}
    for net in sorted(design.nets, key=lambda entry: entry.name):
        board_net_name = f"/{net.name}"
        board_net_names[net.name] = board_net_name
        endpoints = []
        for endpoint in net.endpoints:
            if endpoint.component not in board_ids:
                continue
            component = next(
                entry for entry in components if entry.id == endpoint.component
            )
            pin = graph.get(component.part_id).pin(endpoint.pin)
            if pin is None:
                raise ValidationError(
                    f"missing build pad map for {component.id}/{endpoint.pin}"
                )
            endpoints.append(
                {"component": endpoint.component, "pad": pin.footprint_pad}
            )
            connected.add((endpoint.component, endpoint.pin))
        nets.append({"name": board_net_name, "endpoints": endpoints})
    for component in sorted(components, key=lambda entry: entry.reference):
        part = graph.get(component.part_id)
        for pin in part.pins:
            if (component.id, pin.number) in connected:
                continue
            pin_name = pin.name or pin.number
            nets.append(
                {
                    "name": (
                        f"unconnected-({component.reference}-{pin_name}-"
                        f"Pad{pin.footprint_pad})"
                    ),
                    "endpoints": [
                        {"component": component.id, "pad": pin.footprint_pad}
                    ],
                }
            )
    nets.sort(key=lambda entry: entry["name"])
    return {
        "schema": "pcb-agent-pcbnew-job",
        "version": 1,
        "mode": "build",
        "design_id": design.design_id,
        "title": {
            "name": design.name,
            "revision": design.revision,
            "ir_hash": design.content_hash(),
        },
        "board": {
            "width_mm": design.board.width_mm,
            "height_mm": design.board.height_mm,
            "layers": design.board.layers,
            "thickness_mm": design.board.thickness_mm,
            "min_clearance_mm": design.board.min_clearance_mm,
            "min_track_mm": design.board.min_track_mm,
            "min_drill_mm": design.board.min_drill_mm,
            "min_hole_clearance_mm": max(design.board.min_clearance_mm, 0.15),
            "min_hole_to_hole_mm": max(design.board.min_clearance_mm, 0.2),
            "edge_clearance_mm": design.board.edge_clearance_mm,
            "via_diameter_mm": max(design.board.min_drill_mm + 0.3, 0.6),
        },
        "components": [
            _component_job(design, component, graph.get(component.part_id), placements)
            for component in sorted(components, key=lambda entry: entry.reference)
        ],
        "nets": nets,
        "segments": [
            {
                "net": board_net_names[segment.net],
                "layer": segment.layer,
                "x1_mm": segment.x1_mm,
                "y1_mm": segment.y1_mm,
                "x2_mm": segment.x2_mm,
                "y2_mm": segment.y2_mm,
                "width_mm": segment.width_mm,
            }
            for segment in segments
        ],
        "vias": [
            {
                "net": board_net_names[via.net],
                "x_mm": via.x_mm,
                "y_mm": via.y_mm,
                "diameter_mm": via.diameter_mm,
                "drill_mm": via.drill_mm,
            }
            for via in vias
        ],
    }


def _component_job(
    design: Design,
    component: Component,
    part: PartRecord,
    placements: dict[str, dict[str, float | str | bool]],
) -> dict[str, Any]:
    placement = placements[component.id]
    evidence = next(
        (
            item.locator
            for item in part.evidence
            if item.kind in {"datasheet", "manufacturer_record"}
        ),
        "",
    )
    return {
        "id": component.id,
        "reference": component.reference,
        "value": component.value,
        "footprint": part.footprint,
        "x_mm": placement["x_mm"],
        "y_mm": placement["y_mm"],
        "rotation_deg": placement["rotation_deg"],
        "side": placement["side"],
        "schematic_uuid": stable_kicad_uuid(
            design.design_id, "component", component.id
        ),
        "datasheet": evidence,
        "description": part.description,
        "properties": {
            "Manufacturer": part.manufacturer,
            "MPN": part.mpn,
            "Part_ID": part.id,
            "Lifecycle": str(part.lifecycle.get("status", "unknown")),
            "Trust": part.trust,
        },
    }


def _placement(component: Component):
    if component.placement is None:
        raise ValidationError(f"component {component.id} has no placement seed")
    return component.placement


def _run_worker(
    mode: str,
    job: dict[str, Any],
    output: Path,
    *,
    system_python: str | Path | None,
    timeout: float,
) -> dict[str, Any]:
    python = _system_python(system_python)
    worker = Path(__file__).resolve().with_name("pcbnew_worker.py")
    if not worker.is_file() or worker.is_symlink():
        raise PcbAgentError("trusted pcbnew worker is unavailable")
    with tempfile.TemporaryDirectory(prefix="pcb-agent-worker-") as temporary:
        job_path = Path(temporary) / "job.json"
        atomic_write_json(job_path, job)
        result_path = (
            output
            if mode in {"inspect", "inspect_board"}
            else output.with_suffix(".worker-result.json")
        )
        result = run_command(
            [str(python), "-I", str(worker), mode, str(job_path), str(output)],
            cwd=Path(temporary),
            timeout=timeout,
            max_output_bytes=WORKER_OUTPUT_LIMIT,
        )
        if result.timed_out:
            raise PcbAgentError("isolated pcbnew worker timed out")
        if result.output_limited:
            raise PcbAgentError("isolated pcbnew worker exceeded its output bound")
        if result.returncode != 0:
            lines = [
                line.strip()
                for line in result.stderr.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            detail = lines[-1] if lines else printable_first_line(result.stdout)
            raise PcbAgentError(
                f"isolated pcbnew worker failed (exit {result.returncode}): {detail[:512]}"
            )
        value = load_json_limited(result_path, WORKER_RESULT_LIMIT)
    if (
        not isinstance(value, dict)
        or value.get("schema") != "pcb-agent-pcbnew-result"
        or value.get("version") != 1
    ):
        raise PcbAgentError("isolated pcbnew worker result schema is invalid")
    return value


def _system_python(value: str | Path | None) -> Path:
    if value is None:
        candidate = Path("/usr/bin/python3")
    else:
        candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PcbAgentError("system Python for pcbnew is unavailable") from exc
    if (
        not resolved.is_file()
        or not resolved.is_absolute()
        or not shutil.which(str(resolved))
    ):
        raise PcbAgentError("system Python for pcbnew is not executable")
    return resolved
