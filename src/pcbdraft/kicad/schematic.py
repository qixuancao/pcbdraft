"""Deterministic semantic IR to native KiCad schematic compilation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from kicad_sch_api import Schematic
from kicad_sch_api.core.pin_utils import get_component_pin_info, list_component_pins

from pcbdraft import __version__
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.domain.ir import Component, Design
from pcbdraft.domain.parts import PartGraph, PartRecord
from pcbdraft.domain.scope import assert_supported

KICAD_NAMESPACE = uuid.UUID("68ff00d5-68cd-53c0-a336-f25343c70161")


@dataclass(frozen=True)
class SchematicGeneration:
    path: Path
    sha256: str
    root_uuid: str
    component_uuids: dict[str, str]
    label_count: int
    no_connect_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "root_uuid": self.root_uuid,
            "component_uuids": dict(sorted(self.component_uuids.items())),
            "label_count": self.label_count,
            "no_connect_count": self.no_connect_count,
        }


def stable_kicad_uuid(design_id: str, kind: str, identifier: str) -> str:
    """Return a version-stable UUID for one generated KiCad object."""
    return str(uuid.uuid5(KICAD_NAMESPACE, f"{design_id}/{kind}/{identifier}"))


def inspect_native_schematic(
    path: str | Path,
    *,
    include_connectivity: bool = False,
) -> dict[str, object]:
    """Return a bounded semantic snapshot of a native KiCad schematic."""
    source = Path(path)
    if source.suffix != ".kicad_sch" or source.is_symlink():
        raise ValidationError("schematic inspection requires a non-symlink .kicad_sch")
    try:
        resolved = source.resolve(strict=True)
        if resolved.stat().st_size > 128 * 1024 * 1024:
            raise ValidationError("schematic exceeds the inspection size limit")
        schematic = Schematic.load(resolved)
    except ValidationError:
        raise
    except Exception as exc:
        raise PCBDraftError(f"KiCad schematic inspection failed: {exc}") from exc
    fields = ("Manufacturer", "MPN", "Part_ID", "Lifecycle", "Trust")
    components = []
    for component in schematic.components:
        properties = {}
        for name in fields:
            raw = component.properties.get(name)
            if isinstance(raw, dict) and isinstance(raw.get("value"), str):
                properties[name] = raw["value"]
        components.append(
            {
                "reference": component.reference,
                "value": component.value,
                "symbol": component.lib_id,
                "footprint": component.footprint or "",
                "uuid": component.uuid,
                "position_mm": [
                    round(float(component.position.x), 6),
                    round(float(component.position.y), 6),
                ],
                "rotation_deg": round(float(component.rotation) % 360, 6),
                "properties": dict(sorted(properties.items())),
            }
        )
    labels = sorted(
        {
            str(label.text)
            for label in schematic.labels
            if isinstance(label.text, str) and label.text
        }
    )
    snapshot: dict[str, object] = {
        "schema": "pcbdraft-schematic-snapshot",
        "version": 1,
        "root_uuid": schematic.uuid,
        "components": sorted(components, key=lambda item: item["reference"]),
        "label_names": labels,
        "label_count": len(list(schematic.labels)),
        "no_connect_count": len(list(schematic.no_connects)),
    }
    if include_connectivity:
        snapshot["connectivity"] = _schematic_connectivity_snapshot(schematic)
    return snapshot


class _DisjointSets:
    def __init__(self) -> None:
        self._parents: dict[tuple[str, ...], tuple[str, ...]] = {}

    def add(self, item: tuple[str, ...]) -> None:
        self._parents.setdefault(item, item)

    def find(self, item: tuple[str, ...]) -> tuple[str, ...]:
        parent = self._parents[item]
        while parent != self._parents[parent]:
            parent = self._parents[parent]
        while item != parent:
            previous = self._parents[item]
            self._parents[item] = parent
            item = previous
        return parent

    def union(self, first: tuple[str, ...], second: tuple[str, ...]) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self._parents[max(first_root, second_root)] = min(first_root, second_root)


def _schematic_connectivity_snapshot(schematic: Schematic) -> dict[str, object]:
    """Project native pin connectivity without relying on generated IR."""

    sets = _DisjointSets()
    points: dict[tuple[str, ...], tuple[float, float]] = {}
    endpoint_nodes: dict[tuple[str, str], tuple[str, ...]] = {}
    label_nodes: dict[tuple[str, ...], str] = {}
    unmapped_components: list[str] = []

    for component in schematic.components:
        pins = sorted(list_component_pins(component), key=lambda item: item[0])
        if not pins:
            unmapped_components.append(str(component.reference))
        for pin_number, point in pins:
            endpoint = (str(component.reference), str(pin_number))
            endpoint_node = ("endpoint", *endpoint)
            sets.add(endpoint_node)
            points[endpoint_node] = _point_key(point.x, point.y)
            endpoint_nodes[endpoint] = endpoint_node

    for label in schematic.labels:
        label_node = ("label", str(label.uuid))
        sets.add(label_node)
        points[label_node] = _point_key(label.position.x, label.position.y)
        label_nodes[label_node] = str(label.text)

    wire_segments: list[
        tuple[tuple[str, ...], tuple[tuple[float, float], tuple[float, float]]]
    ] = []
    for wire in schematic.wires:
        wire_node = ("wire", str(wire.uuid))
        sets.add(wire_node)
        wire_points = [_point_key(point.x, point.y) for point in wire.points]
        for index, point in enumerate(wire_points):
            point_node = ("wire_point", str(wire.uuid), str(index))
            sets.add(point_node)
            points[point_node] = point
            sets.union(wire_node, point_node)
        for start, end in pairwise(wire_points):
            wire_segments.append((wire_node, (start, end)))

    _union_coincident_nodes(sets, points)
    _union_nodes_on_wires(sets, points, wire_segments)
    _union_junctions(sets, schematic, wire_segments)
    _union_equal_labels(sets, label_nodes)

    no_connects: set[tuple[str, str]] = set()
    unmapped_no_connects: list[list[float]] = []
    for no_connect in schematic.no_connects:
        point = _point_key(no_connect.position.x, no_connect.position.y)
        matches = sorted(
            endpoint
            for endpoint, node in endpoint_nodes.items()
            if points[node] == point
        )
        if matches:
            no_connects.update(matches)
        else:
            unmapped_no_connects.append([point[0], point[1]])

    endpoints_by_root: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    labels_by_root: dict[tuple[str, ...], set[str]] = {}
    for endpoint, lookup_node in endpoint_nodes.items():
        endpoints_by_root.setdefault(sets.find(lookup_node), []).append(endpoint)
    for lookup_node, name in label_nodes.items():
        labels_by_root.setdefault(sets.find(lookup_node), set()).add(name)

    partition_rows = sorted(
        (
            tuple(sorted(endpoints)),
            tuple(sorted(labels_by_root.get(root, set()))),
        )
        for root, endpoints in endpoints_by_root.items()
    )
    partition_by_endpoint: dict[tuple[str, str], str] = {}
    partitions: list[dict[str, object]] = []
    for index, (endpoints, names) in enumerate(partition_rows, start=1):
        identifier = f"schematic-partition-{index:04d}"
        for endpoint in endpoints:
            partition_by_endpoint[endpoint] = identifier
        partitions.append(
            {
                "id": identifier,
                "labels": list(names),
                "endpoints": [
                    {"reference": reference, "pin": pin} for reference, pin in endpoints
                ],
            }
        )

    connected_roots = set(endpoints_by_root)
    unmapped_labels = sorted(
        {
            name
            for node, name in label_nodes.items()
            if sets.find(node) not in connected_roots
        }
    )
    return {
        "schema": "pcbdraft-schematic-connectivity",
        "version": 1,
        "status": "evaluated",
        "endpoints": [
            {
                "reference": reference,
                "pin": pin,
                "partition": partition_by_endpoint[(reference, pin)],
                "no_connect": (reference, pin) in no_connects,
            }
            for reference, pin in sorted(endpoint_nodes)
        ],
        "partitions": partitions,
        "unmapped_components": sorted(unmapped_components),
        "unmapped_labels": unmapped_labels,
        "unmapped_no_connects": sorted(unmapped_no_connects),
    }


def _point_key(x_value: float, y_value: float) -> tuple[float, float]:
    return round(float(x_value), 6), round(float(y_value), 6)


def _union_coincident_nodes(
    sets: _DisjointSets,
    points: dict[tuple[str, ...], tuple[float, float]],
) -> None:
    nodes_by_point: dict[tuple[float, float], list[tuple[str, ...]]] = {}
    for node, point in points.items():
        nodes_by_point.setdefault(point, []).append(node)
    for nodes in nodes_by_point.values():
        for node in nodes[1:]:
            sets.union(nodes[0], node)


def _union_nodes_on_wires(
    sets: _DisjointSets,
    points: dict[tuple[str, ...], tuple[float, float]],
    wire_segments: list[
        tuple[tuple[str, ...], tuple[tuple[float, float], tuple[float, float]]]
    ],
) -> None:
    for node, point in points.items():
        for wire_node, (start, end) in wire_segments:
            if _point_on_segment(point, start, end):
                sets.union(node, wire_node)


def _union_junctions(
    sets: _DisjointSets,
    schematic: Schematic,
    wire_segments: list[
        tuple[tuple[str, ...], tuple[tuple[float, float], tuple[float, float]]]
    ],
) -> None:
    for junction in schematic.junctions:
        point = _point_key(junction.position.x, junction.position.y)
        matching_wires = [
            wire_node
            for wire_node, (start, end) in wire_segments
            if _point_on_segment(point, start, end)
        ]
        for wire_node in matching_wires[1:]:
            sets.union(matching_wires[0], wire_node)


def _union_equal_labels(
    sets: _DisjointSets,
    label_nodes: dict[tuple[str, ...], str],
) -> None:
    nodes_by_name: dict[str, list[tuple[str, ...]]] = {}
    for node, name in label_nodes.items():
        nodes_by_name.setdefault(name, []).append(node)
    for nodes in nodes_by_name.values():
        for node in nodes[1:]:
            sets.union(nodes[0], node)


def _point_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> bool:
    cross = (point[0] - start[0]) * (end[1] - start[1]) - (point[1] - start[1]) * (
        end[0] - start[0]
    )
    if abs(cross) > 1e-6:
        return False
    return (
        min(start[0], end[0]) - 1e-6 <= point[0] <= max(start[0], end[0]) + 1e-6
        and min(start[1], end[1]) - 1e-6 <= point[1] <= max(start[1], end[1]) + 1e-6
    )


def generate_schematic(
    design: Design,
    output: str | Path,
    *,
    graph: PartGraph | None = None,
    allow_incomplete: bool = False,
) -> SchematicGeneration:
    """Compile a validated IR into a self-contained modern ``.kicad_sch`` file."""
    resolved_graph = graph or PartGraph.bundled()
    assert_supported(design)
    resolved_graph.assert_design(
        design,
        check_libraries=True,
        allow_provisional=design.metadata.get("assurance") == "provisional",
        allow_incomplete=allow_incomplete,
    )
    target = Path(output).resolve(strict=False)
    if target.suffix != ".kicad_sch":
        raise ValidationError("schematic output must end in .kicad_sch")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValidationError("refusing to replace a schematic symlink")

    root_uuid = stable_kicad_uuid(design.design_id, "schematic", "root")
    schematic = Schematic.create(
        name=target.stem,
        generator="pcbdraft",
        generator_version=__version__,
        uuid=root_uuid,
    )
    # The pinned backend exposes no public title-block setter.
    schematic._data["title_block"] = {
        "title": design.name,
        "rev": design.revision,
        "company": "Generated by PCBDraft",
        "comment1": f"Semantic IR: {design.content_hash()}",
        "comment2": "Generated output; review the circuit and layout before fabrication",
    }
    component_uuids: dict[str, str] = {}
    wrappers: dict[str, object] = {}
    try:
        for index, component in enumerate(
            sorted(design.components, key=lambda entry: entry.reference)
        ):
            part = resolved_graph.get(component.part_id)
            component_uuid = stable_kicad_uuid(
                design.design_id, "component", component.id
            )
            component_uuids[component.id] = component_uuid
            x_mm, y_mm = _schematic_position(component, index=index)
            wrapper = schematic.components.add(
                part.symbol,
                reference=component.reference,
                value=component.value,
                position=(x_mm, y_mm),
                footprint=part.footprint,
                rotation=0,
                component_uuid=component_uuid,
                grid_units=False,
            )
            wrapper.add_properties(
                {
                    "Manufacturer": part.manufacturer,
                    "MPN": part.mpn,
                    "Part_ID": part.id,
                    "Lifecycle": str(part.lifecycle.get("status", "unknown")),
                    "Trust": part.trust,
                    "Datasheet": _datasheet(part),
                    "Description": part.description,
                },
                hidden=True,
            )
            if not hasattr(wrapper, "pin_uuids"):
                raise ValidationError(
                    f"multi-unit symbols are not supported by this compiler: {part.symbol}"
                )
            for pin in part.pins:
                if wrapper.get_pin(pin.number) is None:
                    raise ValidationError(
                        f"symbol {part.symbol} has no contracted pin {pin.number}"
                    )
                wrapper.pin_uuids[pin.number] = stable_kicad_uuid(
                    design.design_id,
                    "symbol_pin",
                    f"{component.id}/{pin.number}",
                )
            wrappers[component.id] = wrapper

        connected: set[tuple[str, str]] = set()
        label_count = 0
        for net in sorted(design.nets, key=lambda entry: entry.id):
            for endpoint in sorted(net.endpoints):
                component = next(
                    entry
                    for entry in design.components
                    if entry.id == endpoint.component
                )
                label_uuid = stable_kicad_uuid(
                    design.design_id,
                    "net_label",
                    f"{net.id}/{endpoint.component}/{endpoint.pin}",
                )
                schematic.add_label(
                    net.name,
                    pin=(component.reference, endpoint.pin),
                    uuid=label_uuid,
                )
                connected.add((endpoint.component, endpoint.pin))
                label_count += 1

        no_connect_count = 0
        for component in sorted(design.components, key=lambda entry: entry.reference):
            wrapper = wrappers[component.id]
            part = resolved_graph.get(component.part_id)
            for pin in part.pins:
                if (component.id, pin.number) in connected:
                    continue
                pin_info = get_component_pin_info(wrapper, pin.number)
                if pin_info is None:
                    raise ValidationError(
                        f"cannot locate {component.reference} pin {pin.number}"
                    )
                schematic.no_connects.add(
                    pin_info[0],
                    no_connect_uuid=stable_kicad_uuid(
                        design.design_id,
                        "no_connect",
                        f"{component.id}/{pin.number}",
                    ),
                )
                no_connect_count += 1

        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(temporary_fd)
        temporary = Path(temporary_name)
        try:
            schematic.save(temporary, preserve_format=False)
            _repair_malformed_private_library_properties(temporary)
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except ValidationError:
        raise
    except Exception as exc:
        raise PCBDraftError(f"KiCad schematic generation failed: {exc}") from exc

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return SchematicGeneration(
        path=target,
        sha256=digest,
        root_uuid=root_uuid,
        component_uuids=component_uuids,
        label_count=label_count,
        no_connect_count=no_connect_count,
    )


def _repair_malformed_private_library_properties(path: Path) -> None:
    """Work around kicad-sch-api 0.5.6's KiCad 10 private-property bug.

    KiCad symbol libraries may contain ``(property private ...)`` KLC annotation
    records. The pinned backend shifts those fields when embedding a symbol and
    emits invalid schematic syntax beginning ``(property "private" ...)``. These
    annotations have no electrical or visual meaning, but preserving them avoids
    KiCad's library-symbol mismatch warning. Repair only the shifted first line
    while preserving the rest of each property block.
    """

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    result: list[str] = []
    repaired = 0
    pattern = re.compile(
        r'^(?P<indent>\s*)\(property "private" "(?P<name>(?:\\.|[^"])*)" '
        r"(?P<description>.*?)(?P<newline>\r?\n)?$"
    )
    for line in lines:
        if '(property "private"' not in line:
            result.append(line)
            continue
        match = pattern.fullmatch(line)
        if match is None:
            raise ValidationError(
                "malformed private symbol property has an unsafe layout"
            )
        result.append(
            f"{match.group('indent')}(property private "
            f"{json.dumps(match.group('name'), ensure_ascii=False)} "
            f"{json.dumps(match.group('description'), ensure_ascii=False)}"
            f"{match.group('newline') or ''}"
        )
        repaired += 1
    if repaired:
        path.write_text("".join(result), encoding="utf-8", newline="")


def _schematic_position(component: Component, *, index: int) -> tuple[float, float]:
    placement = component.placement
    if placement is None:
        # Schematic sheet coordinates are presentation-only and do not establish
        # a semantic/PCB footprint pose. Keep unplaced components readable while
        # the model remains responsible for an explicit placement tool call.
        return 25.4 + (index % 5) * 30.48, 25.4 + (index // 5) * 25.4
    # Expand physical board coordinates into a readable A4 schematic while retaining
    # semantic grouping.  The library snaps these values to KiCad's 1.27 mm grid.
    return 20 + placement.x_mm * 2.54, 20 + placement.y_mm * 2.54


def _datasheet(part: PartRecord) -> str:
    evidence = next(
        (
            entry.locator
            for entry in part.evidence
            if entry.kind in {"datasheet", "manufacturer_record"}
        ),
        "",
    )
    return evidence
