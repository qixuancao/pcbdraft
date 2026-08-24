"""Typed native KiCad projections and deterministic IR consistency checks."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import Design, Endpoint
from pcbdraft.domain.operations import parse_connect_group, parse_place_group
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.pcb import inspect_native_board
from pcbdraft.kicad.schematic import inspect_native_schematic

NATIVE_CONSISTENCY_SCHEMA = "native-consistency-v1"
NATIVE_DRC_STATUSES = frozenset({"failed", "not_evaluated", "passed"})
NATIVE_OPERATION_POLICIES: Mapping[str, str] = {
    "add_block": "semantic_only",
    "remove_block": "semantic_only",
    "add_power_domain": "semantic_only",
    "update_power_domain": "semantic_only",
    "remove_power_domain": "semantic_only",
    "add_interface": "semantic_only",
    "update_interface": "semantic_only",
    "remove_interface": "semantic_only",
    # Constraints are compiler inputs, but flat semantic edits intentionally
    # disable placement and routing. They must therefore leave the current
    # native projection unchanged until a later physical operation consumes
    # them; they are not semantic-only metadata.
    "add_constraint": "deferred_native_input",
    "update_constraint": "deferred_native_input",
    "remove_constraint": "deferred_native_input",
    "add_component": "component_add",
    "remove_component": "component_remove",
    "update_component": "component_update",
    "assign_footprint": "footprint_assignment",
    "add_net": "net_add",
    "remove_net": "net_remove",
    "rename_net": "net_rename",
    "connect_pin": "connectivity",
    "connect_group": "connectivity_group",
    "disconnect_pin": "connectivity",
    "update_board_rules": "board_rules",
    "set_board_outline": "board_outline",
    "place_footprint": "footprint_transform",
    "place_group": "footprint_transform_group",
    "move_footprint": "footprint_transform",
    "rotate_footprint": "footprint_transform",
    "unplace_footprint": "footprint_transform",
    "route_net": "routing",
    "unroute_net": "unrouting",
    "add_via": "via_add",
    "remove_via": "via_remove",
    "register_kicad_part": "catalog_native_rematerialization",
}
NATIVE_MISMATCH_CODES = frozenset(
    {
        "board_projection_unknown",
        "extra_native_component",
        "extra_native_endpoint",
        "extra_native_pad",
        "missing_native_component",
        "missing_native_endpoint",
        "missing_native_pad",
        "missing_no_connect",
        "native_board_net_split",
        "native_connectivity_failed",
        "native_net_name_mismatch",
        "native_net_split",
        "native_pad_net_mismatch",
        "native_zero_copper",
        "schematic_projection_unknown",
        "unexpected_native_connection",
        "unexpected_native_no_connect",
        "unintended_board_net_merge",
        "unintended_net_merge",
        "unmapped_native_component",
        "unmapped_native_label",
        "unmapped_native_no_connect",
    }
)


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be an array")
    return value


def _text(value: Any, path: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ValidationError(f"{path} must be a string")
    return value


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{path} must be a non-negative integer")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{path} must be a finite number")
    return result


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{path} must be boolean")
    return value


def _endpoint(row: Mapping[str, Any], path: str, *, pin_key: str = "pin") -> Endpoint:
    return Endpoint(
        _text(row.get("reference"), f"{path}.reference"),
        _text(row.get(pin_key), f"{path}.{pin_key}"),
    )


def _display(endpoint: Endpoint) -> str:
    return f"{endpoint.component}.{endpoint.pin}"


def _texts(value: Any, path: str) -> tuple[str, ...]:
    result = tuple(
        _text(item, f"{path}[{index}]")
        for index, item in enumerate(_array(value, path))
    )
    if len(result) != len(set(result)):
        raise ValidationError(f"{path} contains duplicates")
    return tuple(sorted(result))


@dataclass(frozen=True)
class NativePartition:
    id: str
    labels: tuple[str, ...]
    endpoints: tuple[Endpoint, ...]


@dataclass(frozen=True, order=True)
class NativeSchematicComponent:
    reference: str
    value: str
    symbol: str
    footprint: str
    part_id: str | None


@dataclass(frozen=True)
class NativeSchematicProjection:
    status: str
    components: tuple[str, ...]
    partitions: tuple[NativePartition, ...]
    no_connects: frozenset[Endpoint]
    unmapped_components: tuple[str, ...] = ()
    unmapped_labels: tuple[str, ...] = ()
    unmapped_no_connect_count: int = 0
    component_artifacts: tuple[NativeSchematicComponent, ...] = ()

    @classmethod
    def from_snapshot(cls, value: Any) -> NativeSchematicProjection:
        snapshot = _object(value, "$schematic")
        if (
            snapshot.get("schema") != "pcbdraft-schematic-snapshot"
            or snapshot.get("version") != 1
        ):
            raise ValidationError("unsupported native schematic snapshot")
        component_rows = [
            _object(row, f"$schematic.components[{index}]")
            for index, row in enumerate(
                _array(snapshot.get("components"), "$schematic.components")
            )
        ]
        components = tuple(
            sorted(
                _text(row.get("reference"), "$schematic.components.reference")
                for row in component_rows
            )
        )
        component_artifacts = tuple(
            sorted(
                NativeSchematicComponent(
                    _text(row.get("reference"), "$schematic.components.reference"),
                    _text(row.get("value"), "$schematic.components.value", empty=True),
                    _text(row.get("symbol"), "$schematic.components.symbol"),
                    (
                        ""
                        if row.get("footprint") is None
                        else _text(
                            row.get("footprint"),
                            "$schematic.components.footprint",
                            empty=True,
                        )
                    ),
                    (
                        _text(
                            _object(
                                row.get("properties"),
                                "$schematic.components.properties",
                            ).get("Part_ID"),
                            "$schematic.components.properties.Part_ID",
                        )
                        if isinstance(row.get("properties"), Mapping)
                        and "Part_ID" in row["properties"]
                        else None
                    ),
                )
                for row in component_rows
                if {"value", "symbol", "footprint"} <= set(row)
            )
        )
        raw_connectivity = snapshot.get("connectivity")
        if raw_connectivity is None:
            return cls(
                "unknown",
                components,
                (),
                frozenset(),
                component_artifacts=component_artifacts,
            )
        connectivity = _object(raw_connectivity, "$schematic.connectivity")
        if (
            connectivity.get("schema") != "pcbdraft-schematic-connectivity"
            or connectivity.get("version") != 1
            or connectivity.get("status") != "evaluated"
        ):
            raise ValidationError("unsupported native schematic connectivity")
        partitions = _schematic_partitions(connectivity)
        declared = {
            endpoint: partition.id
            for partition in partitions
            for endpoint in partition.endpoints
        }
        endpoint_rows = [
            _object(row, f"$schematic.connectivity.endpoints[{index}]")
            for index, row in enumerate(
                _array(
                    connectivity.get("endpoints"),
                    "$schematic.connectivity.endpoints",
                )
            )
        ]
        endpoints = [
            _endpoint(row, "$schematic.connectivity.endpoints") for row in endpoint_rows
        ]
        if len(endpoints) != len(set(endpoints)) or any(
            declared.get(endpoint)
            != _text(
                row.get("partition"), "$schematic.connectivity.endpoints.partition"
            )
            for endpoint, row in zip(endpoints, endpoint_rows, strict=True)
        ):
            raise ValidationError(
                "native schematic endpoint partitions are inconsistent"
            )
        no_connects = frozenset(
            endpoint
            for endpoint, row in zip(endpoints, endpoint_rows, strict=True)
            if _boolean(
                row.get("no_connect"), "$schematic.connectivity.endpoints.no_connect"
            )
        )
        unmapped = _array(
            connectivity.get("unmapped_no_connects"),
            "$schematic.connectivity.unmapped_no_connects",
        )
        return cls(
            "evaluated",
            components,
            partitions,
            no_connects,
            _texts(
                connectivity.get("unmapped_components"),
                "$schematic.connectivity.unmapped_components",
            ),
            _texts(
                connectivity.get("unmapped_labels"),
                "$schematic.connectivity.unmapped_labels",
            ),
            len(unmapped),
            component_artifacts,
        )

    @property
    def endpoints(self) -> frozenset[Endpoint]:
        return frozenset(
            endpoint
            for partition in self.partitions
            for endpoint in partition.endpoints
        )


def _schematic_partitions(
    connectivity: Mapping[str, Any],
) -> tuple[NativePartition, ...]:
    partitions: list[NativePartition] = []
    for index, value in enumerate(
        _array(connectivity.get("partitions"), "$schematic.connectivity.partitions")
    ):
        path = f"$schematic.connectivity.partitions[{index}]"
        row = _object(value, path)
        endpoints = tuple(
            sorted(
                _endpoint(_object(item, f"{path}.endpoints[{item_index}]"), path)
                for item_index, item in enumerate(
                    _array(row.get("endpoints"), f"{path}.endpoints")
                )
            )
        )
        partitions.append(
            NativePartition(
                _text(row.get("id"), f"{path}.id"),
                _texts(row.get("labels"), f"{path}.labels"),
                endpoints,
            )
        )
    result = tuple(sorted(partitions, key=lambda item: item.id))
    if len(result) != len({item.id for item in result}) or len(
        [endpoint for item in result for endpoint in item.endpoints]
    ) != len({endpoint for item in result for endpoint in item.endpoints}):
        raise ValidationError("native schematic partitions contain duplicates")
    return result


@dataclass(frozen=True)
class NativeBoardPad:
    endpoint: Endpoint
    net: str | None
    connectivity_component: str | None
    no_connect: bool


@dataclass(frozen=True, order=True)
class NativeFootprintPose:
    reference: str
    x_mm: float
    y_mm: float
    rotation_deg: float
    side: str


@dataclass(frozen=True, order=True)
class NativeBoardComponent:
    reference: str
    value: str
    footprint: str
    part_id: str | None


@dataclass(frozen=True, order=True)
class NativeOutlineSegment:
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float


@dataclass(frozen=True, order=True)
class NativeCopper:
    kind: str
    net: str | None
    measure: float
    geometry: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeBoardProjection:
    status: str
    components: tuple[str, ...]
    pads: tuple[NativeBoardPad, ...]
    copper: tuple[NativeCopper, ...]
    unconnected_count: int | None
    drc_status: str
    footprint_poses: tuple[NativeFootprintPose, ...] = ()
    outline: tuple[NativeOutlineSegment, ...] = ()
    board_rules: tuple[tuple[str, str], ...] = ()
    component_artifacts: tuple[NativeBoardComponent, ...] = ()
    nets: tuple[str, ...] = ()

    @classmethod
    def from_snapshot(cls, value: Any) -> NativeBoardProjection:
        snapshot = _object(value, "$board")
        if (
            snapshot.get("schema") != "pcbdraft-pcbnew-result"
            or snapshot.get("version") != 1
            or snapshot.get("mode") != "inspect_board"
        ):
            raise ValidationError("unsupported native board snapshot")
        status, unconnected, drc_status = _board_status(snapshot)
        components: list[str] = []
        pads: list[NativeBoardPad] = []
        footprint_poses: list[NativeFootprintPose] = []
        component_artifacts: list[NativeBoardComponent] = []
        for component_index, component_value in enumerate(
            _array(snapshot.get("components"), "$board.components")
        ):
            path = f"$board.components[{component_index}]"
            component = _object(component_value, path)
            reference = _text(component.get("reference"), f"{path}.reference")
            components.append(reference)
            if {"value", "footprint"} <= set(component):
                properties = component.get("properties")
                part_id = (
                    _text(
                        _object(properties, f"{path}.properties").get("Part_ID"),
                        f"{path}.properties.Part_ID",
                    )
                    if isinstance(properties, Mapping) and "Part_ID" in properties
                    else None
                )
                component_artifacts.append(
                    NativeBoardComponent(
                        reference,
                        _text(component.get("value"), f"{path}.value", empty=True),
                        _text(component.get("footprint"), f"{path}.footprint"),
                        part_id,
                    )
                )
            pose_fields = {"x_mm", "y_mm", "rotation_deg", "side"}
            present_pose_fields = pose_fields & set(component)
            if present_pose_fields and present_pose_fields != pose_fields:
                raise ValidationError(f"{path} contains an incomplete native pose")
            if present_pose_fields:
                side = _text(component.get("side"), f"{path}.side")
                if side not in {"front", "back"}:
                    raise ValidationError(f"{path}.side is unsupported")
                footprint_poses.append(
                    NativeFootprintPose(
                        reference,
                        _number(component.get("x_mm"), f"{path}.x_mm"),
                        _number(component.get("y_mm"), f"{path}.y_mm"),
                        _number(component.get("rotation_deg"), f"{path}.rotation_deg")
                        % 360,
                        side,
                    )
                )
            for pad_index, pad_value in enumerate(
                _array(component.get("pads"), f"{path}.pads")
            ):
                pad_path = f"{path}.pads[{pad_index}]"
                pad = _object(pad_value, pad_path)
                pads.append(
                    NativeBoardPad(
                        Endpoint(
                            reference, _text(pad.get("number"), f"{pad_path}.number")
                        ),
                        _normalize_net(
                            _text(pad.get("net"), f"{pad_path}.net", empty=True)
                        ),
                        (
                            _text(
                                pad.get("connectivity_component"),
                                f"{pad_path}.connectivity_component",
                            )
                            if status == "evaluated"
                            else None
                        ),
                        (
                            _boolean(pad.get("no_connect"), f"{pad_path}.no_connect")
                            if status == "evaluated"
                            else False
                        ),
                    )
                )
        return cls(
            status,
            tuple(sorted(components)),
            _logical_pads(pads),
            tuple(sorted(_copper(snapshot))),
            unconnected,
            drc_status,
            tuple(sorted(footprint_poses)),
            _outline(snapshot),
            _board_rules(snapshot),
            tuple(sorted(component_artifacts)),
            tuple(
                sorted(
                    net
                    for item in _array(snapshot.get("nets", []), "$board.nets")
                    if (net := _normalize_net(_text(item, "$board.nets[]", empty=True)))
                    is not None
                )
            ),
        )

    def has_nonzero_copper(self, net: str) -> bool:
        return any(item.net == net and item.measure > 0 for item in self.copper)


def _board_status(snapshot: Mapping[str, Any]) -> tuple[str, int | None, str]:
    raw = snapshot.get("connectivity")
    if raw is None:
        return "unknown", None, "not_evaluated"
    connectivity = _object(raw, "$board.connectivity")
    if (
        connectivity.get("schema") != "pcbdraft-pcb-connectivity"
        or connectivity.get("version") != 1
        or connectivity.get("status") != "evaluated"
    ):
        raise ValidationError("unsupported native board connectivity")
    drc = _object(connectivity.get("drc"), "$board.connectivity.drc")
    _array(drc.get("items"), "$board.connectivity.drc.items")
    drc_status = _text(drc.get("status"), "$board.connectivity.drc.status")
    if drc_status not in NATIVE_DRC_STATUSES:
        raise ValidationError("$board.connectivity.drc.status is unsupported")
    return (
        "evaluated",
        _integer(
            connectivity.get("unconnected_count"),
            "$board.connectivity.unconnected_count",
        ),
        drc_status,
    )


def _logical_pads(pads: list[NativeBoardPad]) -> tuple[NativeBoardPad, ...]:
    grouped: dict[Endpoint, list[NativeBoardPad]] = {}
    for pad in pads:
        grouped.setdefault(pad.endpoint, []).append(pad)
    result: list[NativeBoardPad] = []
    for endpoint, copies in sorted(grouped.items()):
        states = {
            (copy.net, copy.connectivity_component, copy.no_connect) for copy in copies
        }
        if len(states) != 1:
            raise ValidationError(
                f"native logical pad {_display(endpoint)} has inconsistent physical copies"
            )
        net, connectivity, no_connect = next(iter(states))
        result.append(NativeBoardPad(endpoint, net, connectivity, no_connect))
    return tuple(result)


def _copper(snapshot: Mapping[str, Any]) -> list[NativeCopper]:
    result: list[NativeCopper] = []
    for index, value in enumerate(_array(snapshot.get("tracks"), "$board.tracks")):
        path = f"$board.tracks[{index}]"
        row = _object(value, path)
        kind = _text(row.get("kind"), f"{path}.kind")
        net = _normalize_net(_text(row.get("net"), f"{path}.net", empty=True))
        geometry: tuple[str, ...]
        if kind == "segment":
            x1 = _number(row.get("x1_mm"), f"{path}.x1_mm")
            y1 = _number(row.get("y1_mm"), f"{path}.y1_mm")
            x2 = _number(row.get("x2_mm"), f"{path}.x2_mm")
            y2 = _number(row.get("y2_mm"), f"{path}.y2_mm")
            measure = math.hypot(
                x2 - x1,
                y2 - y1,
            )
            width_value = row.get("width_mm")
            layer = row.get("layer_index", row.get("layer"))
            if (
                width_value is None
                or isinstance(layer, bool)
                or not isinstance(layer, (int, str))
            ):
                geometry = ()
            else:
                geometry = _segment_geometry(
                    layer,
                    x1,
                    y1,
                    x2,
                    y2,
                    _number(width_value, f"{path}.width_mm"),
                )
        elif kind == "via":
            measure = _number(row.get("drill_mm"), f"{path}.drill_mm")
            geometry = _via_geometry(
                _number(row.get("x_mm"), f"{path}.x_mm"),
                _number(row.get("y_mm"), f"{path}.y_mm"),
                _number(row.get("width_mm"), f"{path}.width_mm"),
                measure,
                _integer(row.get("from_layer", 0), f"{path}.from_layer"),
                _integer(row.get("to_layer", 1), f"{path}.to_layer"),
            )
        else:
            raise ValidationError(f"{path}.kind is unsupported")
        result.append(NativeCopper(kind, net, round(measure, 9), geometry))
    for index, value in enumerate(_array(snapshot.get("zones"), "$board.zones")):
        path = f"$board.zones[{index}]"
        row = _object(value, path)
        area = _number(row.get("area_mm2"), f"{path}.area_mm2")
        if not _boolean(row.get("filled"), f"{path}.filled"):
            area = 0.0
        result.append(
            NativeCopper(
                "zone",
                _normalize_net(_text(row.get("net"), f"{path}.net", empty=True)),
                area,
                (
                    f"layer={_text(row.get('layer'), f'{path}.layer')}",
                    "connection="
                    + _text(
                        row.get("pad_connection", "unknown"),
                        f"{path}.pad_connection",
                    ),
                    f"area={_format_number(area)}",
                ),
            )
        )
    return result


def _format_number(value: float) -> str:
    return f"{round(float(value), 9):.9g}"


def _segment_geometry(
    layer: int | str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float,
) -> tuple[str, ...]:
    first, second = sorted(((x1, y1), (x2, y2)))
    return (
        f"layer={layer}",
        f"x1={_format_number(first[0])}",
        f"y1={_format_number(first[1])}",
        f"x2={_format_number(second[0])}",
        f"y2={_format_number(second[1])}",
        f"width={_format_number(width)}",
    )


def _via_geometry(
    x_mm: float,
    y_mm: float,
    diameter_mm: float,
    drill_mm: float,
    from_layer: int,
    to_layer: int,
) -> tuple[str, ...]:
    return (
        f"x={_format_number(x_mm)}",
        f"y={_format_number(y_mm)}",
        f"diameter={_format_number(diameter_mm)}",
        f"drill={_format_number(drill_mm)}",
        f"layers={from_layer}:{to_layer}",
    )


def _outline(snapshot: Mapping[str, Any]) -> tuple[NativeOutlineSegment, ...]:
    result: list[NativeOutlineSegment] = []
    for index, value in enumerate(
        _array(snapshot.get("outline", []), "$board.outline")
    ):
        path = f"$board.outline[{index}]"
        row = _object(value, path)
        first = (
            _number(row.get("x1_mm"), f"{path}.x1_mm"),
            _number(row.get("y1_mm"), f"{path}.y1_mm"),
        )
        second = (
            _number(row.get("x2_mm"), f"{path}.x2_mm"),
            _number(row.get("y2_mm"), f"{path}.y2_mm"),
        )
        first, second = sorted((first, second))
        result.append(NativeOutlineSegment(*first, *second))
    return tuple(sorted(result))


def _board_rules(snapshot: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = snapshot.get("board", {})
    board = _object(raw, "$board.board")
    result: list[tuple[str, str]] = []
    for key, value in sorted(board.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValidationError(f"$board.board.{key} is unsupported")
        result.append(
            (key, _format_number(float(value)) if not isinstance(value, str) else value)
        )
    return tuple(result)


def _normalize_net(value: str) -> str | None:
    normalized = value.lstrip("/")
    if not normalized or normalized.startswith("unconnected-("):
        return None
    return normalized


@dataclass(frozen=True, order=True)
class NativeMismatch:
    code: str
    scope: str
    subject: str
    expected: str
    observed: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> NativeMismatch:
        row = _object(value, path)
        code = _text(row.get("code"), f"{path}.code")
        if code not in NATIVE_MISMATCH_CODES:
            raise ValidationError(f"{path}.code is unsupported")
        return cls(
            code,
            _text(row.get("scope"), f"{path}.scope"),
            _text(row.get("subject"), f"{path}.subject"),
            _text(row.get("expected"), f"{path}.expected", empty=True),
            _text(row.get("observed"), f"{path}.observed", empty=True),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "scope": self.scope,
            "subject": self.subject,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class NativeConsistencyReport:
    candidate_revision: int
    schematic_status: str
    board_status: str
    drc_status: str
    mismatches: tuple[NativeMismatch, ...]

    @property
    def consistency_passed(self) -> bool:
        return not self.mismatches

    @property
    def verification_status(self) -> str:
        """Return fail-closed overall verification without hiding unknown DRC."""

        if not self.consistency_passed or self.drc_status == "failed":
            return "failed"
        if self.drc_status == "passed":
            return "passed"
        return "unknown"

    @property
    def passed(self) -> bool:
        return self.verification_status == "passed"

    @classmethod
    def from_dict(cls, value: Any) -> NativeConsistencyReport:
        row = _object(value, "$")
        if row.get("schema_version") != NATIVE_CONSISTENCY_SCHEMA:
            raise ValidationError("unsupported native consistency report")
        mismatches = tuple(
            sorted(
                NativeMismatch.from_dict(item, f"$.mismatches[{index}]")
                for index, item in enumerate(
                    _array(row.get("mismatches"), "$.mismatches")
                )
            )
        )
        drc_status = _text(row.get("drc_status"), "$.drc_status")
        if drc_status not in NATIVE_DRC_STATUSES:
            raise ValidationError("$.drc_status is unsupported")
        result = cls(
            _integer(row.get("candidate_revision"), "$.candidate_revision"),
            _text(row.get("schematic_status"), "$.schematic_status"),
            _text(row.get("board_status"), "$.board_status"),
            drc_status,
            mismatches,
        )
        if (
            row.get("consistency_passed") is not result.consistency_passed
            or row.get("verification_status") != result.verification_status
            or row.get("passed") is not result.passed
            or row.get("mismatch_count") != len(mismatches)
        ):
            raise ValidationError("native consistency report summary is inconsistent")
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": NATIVE_CONSISTENCY_SCHEMA,
            "candidate_revision": self.candidate_revision,
            "consistency_passed": self.consistency_passed,
            "verification_status": self.verification_status,
            "passed": self.passed,
            "mismatch_count": len(self.mismatches),
            "schematic_status": self.schematic_status,
            "board_status": self.board_status,
            "drc_status": self.drc_status,
            "mismatches": [item.to_dict() for item in self.mismatches],
        }


@dataclass(frozen=True)
class NativeDeltaCheck:
    name: str
    passed: bool
    expected: str
    observed: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class NativeOperationDeltaReport:
    operation: str
    checks: tuple[NativeDeltaCheck, ...]
    policy: str = "injected_or_legacy"

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(item.passed for item in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "native-operation-delta-v1",
            "operation": self.operation,
            "policy": self.policy,
            "passed": self.passed,
            "checks": [item.to_dict() for item in self.checks],
        }


def compare_native_operation_delta(
    operation: str,
    arguments: Mapping[str, Any],
    before_design: Design,
    candidate_design: Design,
    before: NativeBoardProjection,
    after: NativeBoardProjection,
    *,
    before_schematic: NativeSchematicProjection | None = None,
    after_schematic: NativeSchematicProjection | None = None,
    graph: PartGraph | None = None,
) -> NativeOperationDeltaReport:
    """Verify one flat operation's expected native board delta.

    The comparison deliberately uses the already persisted native snapshots from
    the before and staged managed projects. The common consistency gate still
    reopens the staged KiCad files with connectivity enabled before this report
    can permit publication.
    """

    policy = NATIVE_OPERATION_POLICIES.get(operation)
    if policy is None:
        raise ValidationError(
            f"native operation delta is unsupported for operation: {operation}"
        )
    checks: list[NativeDeltaCheck] = []
    ignored_references: set[str] = set()
    ignored_outline = False
    ignored_board_rules = False
    ignored_nets: set[str] = set()
    ignored_endpoints: set[Endpoint] = set()
    ignored_pads: set[Endpoint] = set()
    ignored_copper: NativeCopper | None = None
    allow_zone_refill = False
    allow_zone_layer_change = False

    if operation == "place_group":
        placement_entries = parse_place_group(arguments.get("placements"))
        components = {item.id: item for item in candidate_design.components}
        observed_poses = {item.reference: item for item in after.footprint_poses}
        mismatched: list[str] = []
        for entry in placement_entries:
            component = components.get(entry.component_id)
            if component is None:
                raise ValidationError(f"component is absent: {entry.component_id}")
            ignored_references.add(component.reference)
            expected_pose = _expected_native_pose(
                component.reference, component.placement
            )
            if observed_poses.get(component.reference) != expected_pose:
                mismatched.append(component.reference)
        allow_zone_refill = True
        checks.append(
            NativeDeltaCheck(
                "native_footprint_group_transform",
                not mismatched,
                f"{len(placement_entries)} target footprint poses",
                (
                    f"{len(placement_entries)} target poses matched"
                    if not mismatched
                    else "mismatched references: " + ",".join(sorted(mismatched))
                ),
            )
        )
    elif operation in {
        "place_footprint",
        "move_footprint",
        "rotate_footprint",
        "unplace_footprint",
    }:
        component_id = str(arguments.get("component_id", ""))
        component = next(
            (item for item in candidate_design.components if item.id == component_id),
            None,
        )
        if component is None:
            raise ValidationError(f"component is absent: {component_id}")
        ignored_references.add(component.reference)
        allow_zone_refill = True
        expected_pose = _expected_native_pose(component.reference, component.placement)
        actual_pose = next(
            (
                item
                for item in after.footprint_poses
                if item.reference == component.reference
            ),
            None,
        )
        checks.append(
            NativeDeltaCheck(
                "native_footprint_transform",
                actual_pose == expected_pose,
                _pose_text(expected_pose),
                _pose_text(actual_pose),
            )
        )
    elif operation == "set_board_outline":
        ignored_outline = True
        allow_zone_refill = True
        expected_outline = _expected_outline(candidate_design)
        checks.append(
            NativeDeltaCheck(
                "native_board_outline",
                after.outline == expected_outline,
                f"{len(expected_outline)} exact Edge.Cuts segments",
                f"{len(after.outline)} observed Edge.Cuts segments",
            )
        )
    elif operation == "update_board_rules":
        ignored_board_rules = True
        changed_fields = _argument_change_names(arguments)
        allow_zone_refill = bool(
            changed_fields & {"layers", "min_clearance_mm", "edge_clearance_mm"}
        )
        allow_zone_layer_change = "layers" in changed_fields
        expected_rules = _expected_board_rules(candidate_design)
        checks.append(
            NativeDeltaCheck(
                "native_board_rules",
                after.board_rules == expected_rules,
                ",".join(f"{key}={value}" for key, value in expected_rules),
                ",".join(f"{key}={value}" for key, value in after.board_rules),
            )
        )
        semantic_fields = _argument_change_names(arguments) & {"finish"}
        if semantic_fields:
            checks.append(
                NativeDeltaCheck(
                    "semantic_only_fields",
                    True,
                    "no native artifact delta",
                    ",".join(sorted(semantic_fields)),
                )
            )
    elif policy in {
        "component_add",
        "component_remove",
        "component_update",
        "footprint_assignment",
    }:
        if before_schematic is None or after_schematic is None:
            raise ValidationError(
                f"native schematic projection is required for operation: {operation}"
            )
        resolved_graph = graph or PartGraph.bundled().with_footprint_overrides(
            candidate_design
        )
        if operation == "add_component":
            raw_value = _object(arguments.get("value"), "$.arguments.value")
            component_id = _text(raw_value.get("id"), "$.arguments.value.id")
        else:
            component_id = _text(
                arguments.get("component_id"), "$.arguments.component_id"
            )
        before_component = next(
            (item for item in before_design.components if item.id == component_id),
            None,
        )
        candidate_component = next(
            (item for item in candidate_design.components if item.id == component_id),
            None,
        )
        ignored_references.update(
            item.reference
            for item in (before_component, candidate_component)
            if item is not None
        )
        allow_zone_refill = _component_geometry_changed(
            before_component,
            candidate_component,
            operation,
        )
        if operation == "remove_component":
            if before_component is None:
                raise ValidationError(f"component is absent: {component_id}")
            schematic_actual = _schematic_component(
                after_schematic, before_component.reference
            )
            board_actual = _board_component(after, before_component.reference)
            component_passed = schematic_actual is None and board_actual is None
            expected_text = "component absent from native schematic and board"
            observed_text = (
                f"schematic={'present' if schematic_actual else 'absent'},"
                f"board={'present' if board_actual else 'absent'}"
            )
        else:
            if candidate_component is None:
                raise ValidationError(f"component is absent: {component_id}")
            expected_schematic, expected_board = _expected_native_components(
                candidate_component, resolved_graph
            )
            schematic_actual = _schematic_component(
                after_schematic, candidate_component.reference
            )
            board_actual = _board_component(after, candidate_component.reference)
            component_passed = (
                schematic_actual == expected_schematic
                and board_actual == expected_board
            )
            expected_text = _component_artifact_text(expected_schematic, expected_board)
            observed_text = _component_artifact_text(schematic_actual, board_actual)
            if expected_board is not None:
                expected_pose = _expected_native_pose(
                    candidate_component.reference,
                    candidate_component.placement,
                )
                actual_pose = next(
                    (
                        item
                        for item in after.footprint_poses
                        if item.reference == candidate_component.reference
                    ),
                    None,
                )
                checks.append(
                    NativeDeltaCheck(
                        "native_component_pose",
                        actual_pose == expected_pose,
                        _pose_text(expected_pose),
                        _pose_text(actual_pose),
                    )
                )
        checks.append(
            NativeDeltaCheck(
                "native_component_projection",
                component_passed,
                expected_text,
                observed_text,
            )
        )
        if operation == "update_component":
            semantic_fields = _argument_change_names(arguments) & {"block_id"}
            if semantic_fields:
                checks.append(
                    NativeDeltaCheck(
                        "semantic_only_fields",
                        True,
                        "no native artifact delta",
                        ",".join(sorted(semantic_fields)),
                    )
                )
    elif operation == "connect_group":
        if before_schematic is None or after_schematic is None:
            raise ValidationError(
                "native schematic projection is required for operation: connect_group"
            )
        connection_entries = parse_connect_group(arguments.get("connections"))
        net_ids = frozenset(entry.net_id for entry in connection_entries)
        before_nets = {
            item.id: item for item in before_design.nets if item.id in net_ids
        }
        candidate_nets = {
            item.id: item for item in candidate_design.nets if item.id in net_ids
        }
        if set(candidate_nets) != set(net_ids):
            absent = sorted(net_ids - set(candidate_nets))
            raise ValidationError(f"nets are absent: {', '.join(absent)}")
        ignored_nets.update(
            item.name for item in (*before_nets.values(), *candidate_nets.values())
        )
        resolved_graph = graph or PartGraph.bundled().with_footprint_overrides(
            candidate_design
        )
        for net_id in net_ids:
            ignored_endpoints.update(
                _net_schematic_endpoints(before_design, net_id)
                | _net_schematic_endpoints(candidate_design, net_id)
            )
            ignored_pads.update(
                _net_board_endpoints(before_design, net_id, resolved_graph)
                | _net_board_endpoints(candidate_design, net_id, resolved_graph)
            )
        mismatched_nets = [
            net_id
            for net_id in sorted(net_ids)
            if not _native_net_matches(
                candidate_design,
                net_id,
                after_schematic,
                after,
                resolved_graph,
            )
        ]
        checks.append(
            NativeDeltaCheck(
                "native_group_net_projection",
                not mismatched_nets,
                f"{len(net_ids)} target native net projections",
                (
                    f"{len(net_ids)} target nets matched"
                    if not mismatched_nets
                    else "mismatched nets: " + ",".join(mismatched_nets)
                ),
            )
        )
        # Preserve copper per semantic net instead of comparing one aggregate
        # union.  An aggregate would accept unchanged geometry whose native net
        # identities were swapped between two group targets.
        before_group_copper = tuple(
            (
                net_id,
                _net_copper_geometry(before, frozenset({before_nets[net_id].name})),
            )
            for net_id in sorted(net_ids)
        )
        after_group_copper = tuple(
            (
                net_id,
                _net_copper_geometry(after, frozenset({candidate_nets[net_id].name})),
            )
            for net_id in sorted(net_ids)
        )
        before_target_copper_count = sum(
            len(items) for _net_id, items in before_group_copper
        )
        after_target_copper_count = sum(
            len(items) for _net_id, items in after_group_copper
        )
        checks.append(
            NativeDeltaCheck(
                "native_group_net_copper_preserved",
                before_group_copper == after_group_copper,
                f"{before_target_copper_count} target copper objects with unchanged net identity and geometry",
                f"{after_target_copper_count} target copper objects after operation",
            )
        )
        allow_zone_refill = True
    elif policy in {"net_add", "net_remove", "net_rename", "connectivity"}:
        if before_schematic is None or after_schematic is None:
            raise ValidationError(
                f"native schematic projection is required for operation: {operation}"
            )
        if operation == "add_net":
            raw_value = _object(arguments.get("value"), "$.arguments.value")
            net_id = _text(raw_value.get("id"), "$.arguments.value.id")
        else:
            net_id = _text(arguments.get("net_id"), "$.arguments.net_id")
        before_net = next(
            (item for item in before_design.nets if item.id == net_id), None
        )
        candidate_net = next(
            (item for item in candidate_design.nets if item.id == net_id), None
        )
        ignored_nets.update(
            item.name for item in (before_net, candidate_net) if item is not None
        )
        ignored_endpoints.update(
            _net_schematic_endpoints(before_design, net_id)
            | _net_schematic_endpoints(candidate_design, net_id)
        )
        resolved_graph = graph or PartGraph.bundled().with_footprint_overrides(
            candidate_design
        )
        ignored_pads.update(
            _net_board_endpoints(before_design, net_id, resolved_graph)
            | _net_board_endpoints(candidate_design, net_id, resolved_graph)
        )
        allow_zone_refill = policy == "connectivity"
        if operation == "remove_net":
            if before_net is None:
                raise ValidationError(f"net is absent: {net_id}")
            target_passed = not _native_net_present(
                before_net.name, after_schematic, after
            )
            expected_text = f"native net absent: {before_net.name}"
            observed_text = _native_net_text(before_net.name, after_schematic, after)
        else:
            if candidate_net is None:
                raise ValidationError(f"net is absent: {net_id}")
            expected_schematic_endpoints = _net_schematic_endpoints(
                candidate_design, net_id
            )
            expected_board_endpoints = _net_board_endpoints(
                candidate_design, net_id, resolved_graph
            )
            expected_copper = any(
                item.net == net_id for item in candidate_design.native_intent.routes
            ) or any(item.net == net_id for item in candidate_design.native_intent.vias)
            empty_net_add = (
                operation == "add_net"
                and not expected_schematic_endpoints
                and not expected_board_endpoints
                and not expected_copper
            )
            if empty_net_add:
                target_passed = _empty_native_net_projection_is_compatible(
                    candidate_net.name,
                    after_schematic,
                    after,
                )
                expected_text = (
                    "native_projection=not_applicable_empty_net,"
                    f"name={candidate_net.name}"
                )
            else:
                target_passed = _native_net_matches(
                    candidate_design,
                    net_id,
                    after_schematic,
                    after,
                    resolved_graph,
                )
                expected_text = _expected_net_text(
                    candidate_design, net_id, resolved_graph
                )
            observed_text = _native_net_text(candidate_net.name, after_schematic, after)
        checks.append(
            NativeDeltaCheck(
                "native_net_projection",
                target_passed,
                expected_text,
                observed_text,
            )
        )
        before_target_copper = _net_copper_geometry(
            before,
            frozenset({before_net.name}) if before_net is not None else frozenset(),
        )
        after_target_copper = _net_copper_geometry(
            after,
            (
                frozenset({candidate_net.name})
                if candidate_net is not None
                else frozenset()
            ),
        )
        checks.append(
            NativeDeltaCheck(
                "native_net_copper_preserved",
                before_target_copper == after_target_copper,
                f"{len(before_target_copper)} target copper objects with unchanged geometry",
                f"{len(after_target_copper)} target copper objects after operation",
            )
        )
    elif operation in {"add_via", "remove_via"}:
        allow_zone_refill = True
        via_id = str(arguments.get("via_id", ""))
        source_design = candidate_design if operation == "add_via" else before_design
        via = next(
            (item for item in source_design.native_intent.vias if item.id == via_id),
            None,
        )
        if via is None:
            raise ValidationError(f"via is absent: {via_id}")
        ignored_copper = _expected_via(source_design, via)
        count = after.copper.count(ignored_copper)
        expected_count = 1 if operation == "add_via" else 0
        checks.append(
            NativeDeltaCheck(
                "native_via_geometry",
                count == expected_count,
                f"target via count {expected_count}",
                f"target via count {count}",
            )
        )
    elif operation in {"route_net", "unroute_net"}:
        allow_zone_refill = True
        net_id = str(arguments.get("net_id", ""))
        net = next((item for item in candidate_design.nets if item.id == net_id), None)
        if net is None:
            raise ValidationError(f"net is absent: {net_id}")
        ignored_nets.add(net.name)
        before_target_zones = _net_zone_geometry(before, frozenset({net.name}))
        after_target_zones = _net_zone_geometry(after, frozenset({net.name}))
        target = tuple(
            item
            for item in after.copper
            if item.kind in {"segment", "via"} and item.net == net.name
        )
        checks.append(
            NativeDeltaCheck(
                "native_target_copper_delta",
                bool(target) if operation == "route_net" else not target,
                "non-zero target copper"
                if operation == "route_net"
                else "no target copper",
                f"{len(target)} target copper objects",
            )
        )
        checks.append(
            NativeDeltaCheck(
                "native_target_zones_preserved",
                before_target_zones == after_target_zones,
                f"{len(before_target_zones)} target zones with unchanged identity",
                f"{len(after_target_zones)} target zones after operation",
            )
        )
    elif policy in {
        "semantic_only",
        "deferred_native_input",
        "catalog_native_rematerialization",
    }:
        checks.append(
            NativeDeltaCheck(
                (
                    "deferred_native_input_projection"
                    if policy == "deferred_native_input"
                    else "semantic_only_native_projection"
                ),
                True,
                "no operation-scoped native delta",
                policy,
            )
        )

    before_unrelated = _unrelated_native_state(
        before,
        ignored_references=frozenset(ignored_references),
        ignored_outline=ignored_outline,
        ignored_board_rules=ignored_board_rules,
        ignored_nets=frozenset(ignored_nets),
        ignored_pads=frozenset(ignored_pads),
        ignored_copper=ignored_copper,
        allow_zone_refill=allow_zone_refill,
        allow_zone_layer_change=allow_zone_layer_change,
    )
    after_unrelated = _unrelated_native_state(
        after,
        ignored_references=frozenset(ignored_references),
        ignored_outline=ignored_outline,
        ignored_board_rules=ignored_board_rules,
        ignored_nets=frozenset(ignored_nets),
        ignored_pads=frozenset(ignored_pads),
        ignored_copper=ignored_copper,
        allow_zone_refill=allow_zone_refill,
        allow_zone_layer_change=allow_zone_layer_change,
    )
    if before_schematic is not None and after_schematic is not None:
        before_unrelated += _unrelated_schematic_state(
            before_schematic,
            ignored_references=frozenset(ignored_references),
            ignored_nets=frozenset(ignored_nets),
            ignored_endpoints=frozenset(ignored_endpoints),
        )
        after_unrelated += _unrelated_schematic_state(
            after_schematic,
            ignored_references=frozenset(ignored_references),
            ignored_nets=frozenset(ignored_nets),
            ignored_endpoints=frozenset(ignored_endpoints),
        )
    checks.append(
        NativeDeltaCheck(
            "no_unrelated_native_delta",
            before_unrelated == after_unrelated,
            "unrelated native projection unchanged",
            (
                "unchanged"
                if before_unrelated == after_unrelated
                else "unrelated native projection changed"
            ),
        )
    )
    return NativeOperationDeltaReport(operation, tuple(checks), policy)


def _argument_change_names(arguments: Mapping[str, Any]) -> set[str]:
    raw = arguments.get("changes")
    if not isinstance(raw, Mapping):
        return set()
    entries = raw.get("entries")
    if isinstance(entries, list):
        return {
            str(item["field"])
            for item in entries
            if isinstance(item, Mapping) and isinstance(item.get("field"), str)
        }
    return {str(key) for key in raw}


def _schematic_component(
    projection: NativeSchematicProjection, reference: str
) -> NativeSchematicComponent | None:
    return next(
        (
            item
            for item in projection.component_artifacts
            if item.reference == reference
        ),
        None,
    )


def _component_geometry_changed(
    before: Any | None,
    candidate: Any | None,
    operation: str,
) -> bool:
    if operation in {"add_component", "remove_component"}:
        return True
    if before is None or candidate is None:
        return True
    return (
        before.part_id != candidate.part_id
        or before.attributes.get("footprint") != candidate.attributes.get("footprint")
        or before.attributes.get("exclude_from_board", False)
        != candidate.attributes.get("exclude_from_board", False)
    )


def _board_component(
    projection: NativeBoardProjection, reference: str
) -> NativeBoardComponent | None:
    return next(
        (
            item
            for item in projection.component_artifacts
            if item.reference == reference
        ),
        None,
    )


def _expected_native_components(
    component: Any, graph: PartGraph
) -> tuple[NativeSchematicComponent, NativeBoardComponent | None]:
    part = graph.get(component.part_id)
    footprint = part.footprint or ""
    schematic = NativeSchematicComponent(
        component.reference,
        component.value,
        part.symbol,
        footprint,
        part.id,
    )
    board = (
        NativeBoardComponent(
            component.reference,
            component.value,
            footprint,
            part.id,
        )
        if footprint and not component.attributes.get("exclude_from_board", False)
        else None
    )
    return schematic, board


def _component_artifact_text(
    schematic: NativeSchematicComponent | None,
    board: NativeBoardComponent | None,
) -> str:
    return (
        "schematic="
        + (
            f"{schematic.reference}/{schematic.value}/{schematic.symbol}/"
            f"{schematic.footprint}/{schematic.part_id or 'unknown'}"
            if schematic is not None
            else "absent"
        )
        + ",board="
        + (
            f"{board.reference}/{board.value}/{board.footprint}/"
            f"{board.part_id or 'unknown'}"
            if board is not None
            else "absent"
        )
    )


def _net_schematic_endpoints(design: Design, net_id: str) -> set[Endpoint]:
    net = next((item for item in design.nets if item.id == net_id), None)
    if net is None:
        return set()
    references = {item.id: item.reference for item in design.components}
    return {
        Endpoint(references[item.component], item.pin)
        for item in net.endpoints
        if item.component in references
    }


def _net_board_endpoints(
    design: Design, net_id: str, graph: PartGraph
) -> set[Endpoint]:
    net = next((item for item in design.nets if item.id == net_id), None)
    if net is None:
        return set()
    components = {item.id: item for item in design.components}
    result: set[Endpoint] = set()
    for endpoint in net.endpoints:
        component = components.get(endpoint.component)
        if component is None or component.attributes.get("exclude_from_board", False):
            continue
        part = graph.get(component.part_id)
        if part.footprint is None:
            continue
        pin = part.pin(endpoint.pin)
        if pin is None:
            continue
        result.add(Endpoint(component.reference, pin.footprint_pad))
    return result


def _native_net_matches(
    design: Design,
    net_id: str,
    schematic: NativeSchematicProjection,
    board: NativeBoardProjection,
    graph: PartGraph,
) -> bool:
    net = next((item for item in design.nets if item.id == net_id), None)
    if net is None or net.name not in board.nets:
        return False
    expected_schematic = _net_schematic_endpoints(design, net_id)
    native_partitions = tuple(
        item for item in schematic.partitions if net.name in item.labels
    )
    schematic_matches = (
        len(native_partitions) == 1
        and frozenset(native_partitions[0].endpoints) == frozenset(expected_schematic)
        if expected_schematic
        else not native_partitions
    )
    expected_board = _net_board_endpoints(design, net_id, graph)
    actual_board = {item.endpoint for item in board.pads if item.net == net.name}
    retained_copper_expected = any(
        item.net == net_id for item in design.native_intent.routes
    ) or any(item.net == net_id for item in design.native_intent.vias)
    retained_copper_present = any(
        item.net == net.name and item.kind in {"segment", "via"} and item.measure > 0
        for item in board.copper
    )
    return (
        schematic_matches
        and actual_board == expected_board
        and (not retained_copper_expected or retained_copper_present)
    )


def _empty_native_net_projection_is_compatible(
    name: str,
    schematic: NativeSchematicProjection,
    board: NativeBoardProjection,
) -> bool:
    """Accept an absent/name-only projection, but reject electrical membership."""

    return (
        not any(name in item.labels for item in schematic.partitions)
        and not any(item.net == name for item in board.pads)
        and not any(item.net == name for item in board.copper)
    )


def _native_net_present(
    name: str,
    schematic: NativeSchematicProjection,
    board: NativeBoardProjection,
) -> bool:
    return (
        name in board.nets
        or any(name in item.labels for item in schematic.partitions)
        or any(item.net == name for item in board.pads)
        or any(item.net == name for item in board.copper)
    )


def _native_net_text(
    name: str,
    schematic: NativeSchematicProjection,
    board: NativeBoardProjection,
) -> str:
    return (
        f"name={name},board_net={name in board.nets},"
        f"schematic_partitions={sum(name in item.labels for item in schematic.partitions)},"
        f"pads={sum(item.net == name for item in board.pads)},"
        f"copper={sum(item.net == name for item in board.copper)}"
    )


def _net_copper_geometry(
    projection: NativeBoardProjection,
    names: frozenset[str],
) -> tuple[tuple[str, float, tuple[str, ...]], ...]:
    """Return target copper identity independent of a permitted net rename.

    Refill area is deliberately omitted for zones: rematerializing a board may
    recalculate filled polygon area, but it must not add/remove a zone or alter
    its layer or pad-connection policy.
    """

    return tuple(
        sorted(
            (
                item.kind,
                0.0 if item.kind == "zone" else item.measure,
                (
                    tuple(
                        entry
                        for entry in item.geometry
                        if not entry.startswith("area=")
                    )
                    if item.kind == "zone"
                    else item.geometry
                ),
            )
            for item in projection.copper
            if item.net in names
        )
    )


def _net_zone_geometry(
    projection: NativeBoardProjection,
    names: frozenset[str],
) -> tuple[tuple[str, float, tuple[str, ...]], ...]:
    return tuple(
        item for item in _net_copper_geometry(projection, names) if item[0] == "zone"
    )


def _expected_net_text(design: Design, net_id: str, graph: PartGraph) -> str:
    net = next(item for item in design.nets if item.id == net_id)
    return (
        f"name={net.name},schematic_endpoints="
        f"{len(_net_schematic_endpoints(design, net_id))},"
        f"pads={len(_net_board_endpoints(design, net_id, graph))}"
    )


def _expected_board_rules(design: Design) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            {
                "layers": _format_number(design.board.layers),
                "thickness_mm": _format_number(design.board.thickness_mm),
                "min_clearance_mm": _format_number(design.board.min_clearance_mm),
                "min_track_mm": _format_number(design.board.min_track_mm),
                "min_drill_mm": _format_number(design.board.min_drill_mm),
                "edge_clearance_mm": _format_number(design.board.edge_clearance_mm),
            }.items()
        )
    )


def _expected_native_pose(reference: str, placement: Any) -> NativeFootprintPose:
    return NativeFootprintPose(
        reference,
        placement.x_mm if placement is not None else 0.0,
        placement.y_mm if placement is not None else 0.0,
        (placement.rotation_deg if placement is not None else 0.0) % 360,
        placement.side if placement is not None else "front",
    )


def _pose_text(value: NativeFootprintPose | None) -> str:
    if value is None:
        return "missing"
    return (
        f"{value.reference}@{_format_number(value.x_mm)},"
        f"{_format_number(value.y_mm)}/{_format_number(value.rotation_deg)}/{value.side}"
    )


def _expected_outline(design: Design) -> tuple[NativeOutlineSegment, ...]:
    points = (
        (0.0, 0.0),
        (design.board.width_mm, 0.0),
        (design.board.width_mm, design.board.height_mm),
        (0.0, design.board.height_mm),
        (0.0, 0.0),
    )
    result: list[NativeOutlineSegment] = []
    for first, second in pairwise(points):
        first, second = sorted((first, second))
        result.append(NativeOutlineSegment(*first, *second))
    return tuple(sorted(result))


def _expected_via(design: Design, via: Any) -> NativeCopper:
    net = next((item for item in design.nets if item.id == via.net), None)
    if net is None:
        raise ValidationError(f"via references an absent net: {via.net}")
    return NativeCopper(
        "via",
        net.name,
        round(via.drill_mm, 9),
        _via_geometry(
            via.x_mm,
            via.y_mm,
            via.diameter_mm,
            via.drill_mm,
            via.from_layer,
            via.to_layer,
        ),
    )


def _unrelated_native_state(
    value: NativeBoardProjection,
    *,
    ignored_references: frozenset[str],
    ignored_outline: bool,
    ignored_board_rules: bool,
    ignored_nets: frozenset[str],
    ignored_pads: frozenset[Endpoint],
    ignored_copper: NativeCopper | None,
    allow_zone_refill: bool,
    allow_zone_layer_change: bool,
) -> tuple[object, ...]:
    copper = [
        item
        for item in value.copper
        if item.kind in {"segment", "via"} and item.net not in ignored_nets
    ]
    if ignored_copper is not None and ignored_copper in copper:
        copper.remove(ignored_copper)
    pads = tuple(
        sorted(
            (
                item.endpoint,
                item.net,
                item.no_connect,
            )
            for item in value.pads
            if item.endpoint.component not in ignored_references
            and item.endpoint not in ignored_pads
            and item.net not in ignored_nets
        )
    )
    zones = tuple(item for item in value.copper if item.kind == "zone")
    return (
        tuple(item for item in value.components if item not in ignored_references),
        tuple(
            item
            for item in value.component_artifacts
            if item.reference not in ignored_references
        ),
        pads,
        tuple(
            item
            for item in value.footprint_poses
            if item.reference not in ignored_references
        ),
        () if ignored_outline else value.outline,
        tuple(sorted(copper)),
        _zone_state(
            zones,
            ignored_nets=ignored_nets,
            ignore_area=allow_zone_refill,
            ignore_layer=allow_zone_layer_change,
        ),
        () if ignored_board_rules else value.board_rules,
        tuple(item for item in value.nets if item not in ignored_nets),
    )


def _zone_state(
    zones: tuple[NativeCopper, ...],
    *,
    ignored_nets: frozenset[str],
    ignore_area: bool,
    ignore_layer: bool,
) -> tuple[tuple[str | None, tuple[str, ...]], ...]:
    """Keep zone identity while allowing only deterministic refill variation."""

    result = (
        (
            item.net,
            tuple(
                entry
                for entry in item.geometry
                if not (ignore_area and entry.startswith("area="))
                and not (ignore_layer and entry.startswith("layer="))
            ),
        )
        for item in zones
        if item.net not in ignored_nets
    )
    return tuple(sorted(result, key=lambda item: (item[0] or "", item[1])))


def _unrelated_schematic_state(
    value: NativeSchematicProjection,
    *,
    ignored_references: frozenset[str],
    ignored_nets: frozenset[str],
    ignored_endpoints: frozenset[Endpoint],
) -> tuple[object, ...]:
    def ignored_partition(partition: NativePartition) -> bool:
        return bool(
            set(partition.labels) & ignored_nets
            or set(partition.endpoints) & ignored_endpoints
            or any(
                endpoint.component in ignored_references
                for endpoint in partition.endpoints
            )
        )

    partitions = tuple(
        sorted(
            (item.labels, item.endpoints)
            for item in value.partitions
            if not ignored_partition(item)
        )
    )
    return (
        tuple(item for item in value.components if item not in ignored_references),
        tuple(
            item
            for item in value.component_artifacts
            if item.reference not in ignored_references
        ),
        partitions,
        frozenset(
            item
            for item in value.no_connects
            if item.component not in ignored_references
            and item not in ignored_endpoints
        ),
        value.unmapped_components,
        value.unmapped_labels,
        value.unmapped_no_connect_count,
    )


@dataclass(frozen=True)
class _ExpectedState:
    schematic_components: frozenset[str]
    schematic_endpoints: frozenset[Endpoint]
    schematic_no_connects: frozenset[Endpoint]
    schematic_nets: dict[str, tuple[str, frozenset[Endpoint]]]
    board_components: frozenset[str]
    board_pads: frozenset[Endpoint]
    board_pad_nets: dict[Endpoint, str | None]
    board_nets: dict[str, tuple[str, frozenset[Endpoint]]]


def compare_native_consistency(
    design: Design,
    schematic: NativeSchematicProjection,
    board: NativeBoardProjection,
    *,
    candidate_revision: int,
    graph: PartGraph | None = None,
    require_routed_net_ids: frozenset[str] = frozenset(),
) -> NativeConsistencyReport:
    """Compare candidate IR with already-read native projections."""

    if candidate_revision < 0:
        raise ValidationError("candidate revision must be non-negative")
    expected = _expected_state(design, graph or PartGraph.bundled())
    mismatches: list[NativeMismatch] = []
    if schematic.status == "evaluated":
        mismatches.extend(_compare_schematic(expected, schematic))
    else:
        mismatches.append(
            NativeMismatch(
                "schematic_projection_unknown",
                "schematic",
                "connectivity",
                "evaluated",
                schematic.status,
            )
        )
    if board.status == "evaluated":
        mismatches.extend(_compare_board(expected, board, require_routed_net_ids))
    else:
        mismatches.append(
            NativeMismatch(
                "board_projection_unknown",
                "board",
                "connectivity",
                "evaluated",
                board.status,
            )
        )
    return NativeConsistencyReport(
        candidate_revision,
        schematic.status,
        board.status,
        board.drc_status,
        tuple(sorted(set(mismatches))),
    )


def inspect_native_consistency(
    design: Design,
    schematic_path: str | Path,
    board_path: str | Path,
    *,
    candidate_revision: int,
    graph: PartGraph | None = None,
    system_python: str | Path | None = None,
    require_routed_net_ids: frozenset[str] = frozenset(),
) -> NativeConsistencyReport:
    """Read staged native KiCad files explicitly and compare them with candidate IR."""

    schematic = NativeSchematicProjection.from_snapshot(
        inspect_native_schematic(schematic_path, include_connectivity=True)
    )
    board = NativeBoardProjection.from_snapshot(
        inspect_native_board(
            design,
            board_path,
            system_python=system_python,
            include_connectivity=True,
        )
    )
    return compare_native_consistency(
        design,
        schematic,
        board,
        candidate_revision=candidate_revision,
        graph=graph,
        require_routed_net_ids=require_routed_net_ids,
    )


def _expected_state(design: Design, graph: PartGraph) -> _ExpectedState:
    references = {component.id: component.reference for component in design.components}
    schematic_endpoints: set[Endpoint] = set()
    board_pads: set[Endpoint] = set()
    board_components: set[str] = set()
    logical_to_pad: dict[tuple[str, str], Endpoint] = {}
    for component in design.components:
        part = graph.get(component.part_id)
        schematic_endpoints.update(
            Endpoint(component.reference, pin.number) for pin in part.pins
        )
        if part.footprint is None or component.attributes.get(
            "exclude_from_board", False
        ):
            continue
        board_components.add(component.reference)
        for pin in part.pins:
            pad = Endpoint(component.reference, pin.footprint_pad)
            board_pads.add(pad)
            logical_to_pad[(component.id, pin.number)] = pad
    schematic_nets: dict[str, tuple[str, frozenset[Endpoint]]] = {}
    board_nets: dict[str, tuple[str, frozenset[Endpoint]]] = {}
    connected_schematic: set[Endpoint] = set()
    board_pad_nets: dict[Endpoint, str | None] = {pad: None for pad in board_pads}
    for net in design.nets:
        schematic_members = frozenset(
            Endpoint(references[item.component], item.pin) for item in net.endpoints
        )
        connected_schematic.update(schematic_members)
        schematic_nets[net.id] = (net.name, schematic_members)
        board_members = frozenset(
            logical_to_pad[(item.component, item.pin)]
            for item in net.endpoints
            if (item.component, item.pin) in logical_to_pad
        )
        board_nets[net.id] = (net.name, board_members)
        for pad in board_members:
            board_pad_nets[pad] = net.name
    return _ExpectedState(
        frozenset(references.values()),
        frozenset(schematic_endpoints),
        frozenset(schematic_endpoints - connected_schematic),
        schematic_nets,
        frozenset(board_components),
        frozenset(board_pads),
        board_pad_nets,
        board_nets,
    )


def _compare_schematic(
    expected: _ExpectedState,
    actual: NativeSchematicProjection,
) -> list[NativeMismatch]:
    mismatches = _compare_names(
        expected.schematic_components, set(actual.components), "schematic"
    )
    mismatches.extend(
        _compare_endpoints(
            expected.schematic_endpoints, set(actual.endpoints), "schematic"
        )
    )
    partition_by_endpoint = {
        endpoint: partition
        for partition in actual.partitions
        for endpoint in partition.endpoints
    }
    expected_net_by_endpoint = {
        endpoint: net_id
        for net_id, (_name, endpoints) in expected.schematic_nets.items()
        for endpoint in endpoints
    }
    for partition in actual.partitions:
        net_ids = {
            expected_net_by_endpoint[endpoint]
            for endpoint in partition.endpoints
            if endpoint in expected_net_by_endpoint
        }
        if len(net_ids) > 1:
            mismatches.append(
                NativeMismatch(
                    "unintended_net_merge",
                    "schematic",
                    partition.id,
                    "separate IR nets",
                    ", ".join(sorted(net_ids)),
                )
            )
    for net_id, (name, endpoints) in sorted(expected.schematic_nets.items()):
        partitions = {
            partition_by_endpoint[endpoint]
            for endpoint in endpoints & set(partition_by_endpoint)
        }
        if len(partitions) > 1:
            mismatches.append(
                NativeMismatch(
                    "native_net_split",
                    "schematic",
                    net_id,
                    "one partition",
                    ", ".join(sorted(item.id for item in partitions)),
                )
            )
        labels = {label for partition in partitions for label in partition.labels}
        if endpoints & set(partition_by_endpoint) and name not in labels:
            mismatches.append(
                NativeMismatch(
                    "native_net_name_mismatch",
                    "schematic",
                    net_id,
                    name,
                    ", ".join(sorted(labels)) or "unnamed",
                )
            )
    mismatches.extend(_compare_no_connects(expected, actual, partition_by_endpoint))
    mismatches.extend(
        NativeMismatch(
            "unmapped_native_component",
            "schematic",
            reference,
            "pin projection",
            "unmapped",
        )
        for reference in actual.unmapped_components
    )
    mismatches.extend(
        NativeMismatch(
            "unmapped_native_label",
            "schematic",
            label,
            "mapped partition",
            "unmapped",
        )
        for label in actual.unmapped_labels
    )
    if actual.unmapped_no_connect_count:
        mismatches.append(
            NativeMismatch(
                "unmapped_native_no_connect",
                "schematic",
                "no_connects",
                "all mapped",
                str(actual.unmapped_no_connect_count),
            )
        )
    return mismatches


def _compare_no_connects(
    expected: _ExpectedState,
    actual: NativeSchematicProjection,
    partitions: dict[Endpoint, NativePartition],
) -> list[NativeMismatch]:
    mismatches: list[NativeMismatch] = []
    for endpoint in sorted(expected.schematic_no_connects - actual.no_connects):
        mismatches.append(
            NativeMismatch(
                "missing_no_connect",
                "schematic",
                _display(endpoint),
                "no-connect marker",
                "absent",
            )
        )
    for endpoint in sorted(actual.no_connects - expected.schematic_no_connects):
        mismatches.append(
            NativeMismatch(
                "unexpected_native_no_connect",
                "schematic",
                _display(endpoint),
                "connected endpoint",
                "no-connect marker",
            )
        )
    for endpoint in sorted(expected.schematic_no_connects & set(partitions)):
        partition = partitions[endpoint]
        if len(partition.endpoints) > 1 or partition.labels:
            mismatches.append(
                NativeMismatch(
                    "unexpected_native_connection",
                    "schematic",
                    _display(endpoint),
                    "isolated no-connect",
                    partition.id,
                )
            )
    return mismatches


def _compare_board(
    expected: _ExpectedState,
    actual: NativeBoardProjection,
    required_routes: frozenset[str],
) -> list[NativeMismatch]:
    unknown_routes = required_routes - set(expected.board_nets)
    if unknown_routes:
        raise ValidationError(
            "required routed nets are unknown: " + ", ".join(sorted(unknown_routes))
        )
    mismatches = _compare_names(
        expected.board_components, set(actual.components), "board"
    )
    pads = {item.endpoint: item for item in actual.pads}
    electrical_pads = {
        endpoint
        for endpoint, pad in pads.items()
        if endpoint in expected.board_pads or pad.net is not None or pad.no_connect
    }
    mismatches.extend(_compare_endpoints(expected.board_pads, electrical_pads, "board"))
    for endpoint in sorted(expected.board_pads & set(pads)):
        expected_net = expected.board_pad_nets[endpoint]
        pad = pads[endpoint]
        if pad.net != expected_net:
            mismatches.append(
                NativeMismatch(
                    "native_pad_net_mismatch",
                    "board",
                    _display(endpoint),
                    expected_net or "no-connect",
                    pad.net or "no-connect",
                )
            )
        if expected_net is not None and pad.no_connect:
            mismatches.append(
                NativeMismatch(
                    "unexpected_native_no_connect",
                    "board",
                    _display(endpoint),
                    expected_net,
                    "no-connect pad",
                )
            )
    mismatches.extend(_compare_board_net_assignments(expected, pads))
    mismatches.extend(_compare_required_routes(expected, actual, pads, required_routes))
    return mismatches


def _compare_board_net_assignments(
    expected: _ExpectedState,
    pads: dict[Endpoint, NativeBoardPad],
) -> list[NativeMismatch]:
    mismatches: list[NativeMismatch] = []
    expected_net_by_pad = {
        endpoint: net_id
        for net_id, (_name, endpoints) in expected.board_nets.items()
        for endpoint in endpoints
    }
    native_net_members: dict[str, set[str]] = {}
    for endpoint, pad in pads.items():
        net_id = expected_net_by_pad.get(endpoint)
        if net_id is not None and pad.net is not None:
            native_net_members.setdefault(pad.net, set()).add(net_id)
    for native_net, net_ids in sorted(native_net_members.items()):
        if len(net_ids) > 1:
            mismatches.append(
                NativeMismatch(
                    "unintended_board_net_merge",
                    "board",
                    native_net,
                    "separate IR nets",
                    ", ".join(sorted(net_ids)),
                )
            )
    for net_id, (_name, endpoints) in sorted(expected.board_nets.items()):
        assignments = {pads[item].net for item in endpoints & set(pads)}
        if len(assignments) > 1:
            mismatches.append(
                NativeMismatch(
                    "native_board_net_split",
                    "board",
                    net_id,
                    "one native net",
                    ", ".join(sorted(item or "no-connect" for item in assignments)),
                )
            )
    return mismatches


def _compare_required_routes(
    expected: _ExpectedState,
    actual: NativeBoardProjection,
    pads: dict[Endpoint, NativeBoardPad],
    required_routes: frozenset[str],
) -> list[NativeMismatch]:
    mismatches: list[NativeMismatch] = []
    for net_id in sorted(required_routes):
        name, endpoints = expected.board_nets[net_id]
        present = endpoints & set(pads)
        connectivity = {pads[item].connectivity_component for item in present}
        if len(endpoints) > 1 and not actual.has_nonzero_copper(name):
            mismatches.append(
                NativeMismatch(
                    "native_zero_copper",
                    "board",
                    net_id,
                    "non-zero native copper",
                    "absent",
                )
            )
        if len(endpoints) > 1 and (
            len(present) != len(endpoints)
            or None in connectivity
            or len(connectivity) != 1
        ):
            mismatches.append(
                NativeMismatch(
                    "native_connectivity_failed",
                    "board",
                    net_id,
                    "one connectivity component",
                    ", ".join(sorted(item or "unknown" for item in connectivity))
                    or "missing endpoints",
                )
            )
    return mismatches


def _compare_names(
    expected: frozenset[str], actual: set[str], scope: str
) -> list[NativeMismatch]:
    return [
        *(
            NativeMismatch(
                "missing_native_component",
                scope,
                name,
                "native component",
                "absent",
            )
            for name in sorted(expected - actual)
        ),
        *(
            NativeMismatch(
                "extra_native_component",
                scope,
                name,
                "absent from candidate IR",
                "native component",
            )
            for name in sorted(actual - expected)
        ),
    ]


def _compare_endpoints(
    expected: frozenset[Endpoint], actual: set[Endpoint], scope: str
) -> list[NativeMismatch]:
    missing_code = (
        "missing_native_endpoint" if scope == "schematic" else "missing_native_pad"
    )
    extra_code = "extra_native_endpoint" if scope == "schematic" else "extra_native_pad"
    return [
        *(
            NativeMismatch(
                missing_code,
                scope,
                _display(endpoint),
                "native endpoint",
                "absent",
            )
            for endpoint in sorted(expected - actual)
        ),
        *(
            NativeMismatch(
                extra_code,
                scope,
                _display(endpoint),
                "absent from candidate IR",
                "native endpoint",
            )
            for endpoint in sorted(actual - expected)
        ),
    ]
