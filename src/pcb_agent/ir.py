"""Semantic, model-independent circuit and PCB intermediate representation.

The IR is deliberately smaller than KiCad's file formats.  It records electrical
identity and engineering intent; rendering details remain a backend concern.  All
collections that are semantically sets are sorted during serialization so the same
design always has the same bytes and content hash.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io import atomic_write_bytes, read_bytes_limited

IR_SCHEMA = "pcb-agent-ir"
IR_VERSION = 1
IR_FILE_LIMIT = 32 * 1024 * 1024
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REF_RE = re.compile(r"^(?:#?[A-Z][A-Z0-9]*[0-9]+)$")
_NET_RE = re.compile(r"^[A-Za-z0-9_+./~{}-]{1,128}$")
_JSON_SCALARS = (str, int, float, bool, type(None))
MAX_JSON_DEPTH = 64


@dataclass(frozen=True, order=True)
class IRIssue:
    """A deterministic structural or semantic IR diagnostic."""

    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _json_value(value: Any, path: str = "$", *, _depth: int = 0) -> Any:
    """Validate and copy JSON data while rejecting ambiguous Python values."""
    if _depth > MAX_JSON_DEPTH:
        raise ValidationError(f"JSON nesting exceeds {MAX_JSON_DEPTH} levels at {path}")
    if isinstance(value, _JSON_SCALARS):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError(f"non-finite number at {path}")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValidationError(f"non-string JSON key at {path}")
            result[key] = _json_value(value[key], f"{path}.{key}", _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, f"{path}[{index}]", _depth=_depth + 1)
            for index, item in enumerate(value)
        ]
    raise ValidationError(f"non-JSON value at {path}: {type(value).__name__}")


def _strict_mapping(
    value: Any, path: str, *, required: set[str], optional: set[str]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} must be an object")
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise ValidationError(f"{path} is missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ValidationError(f"{path} has unknown fields: {', '.join(sorted(extra))}")
    return dict(value)


def _string(value: Any, path: str, *, nonempty: bool = True, limit: int = 4096) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValidationError(
            f"{path} must be {'a non-empty ' if nonempty else ''}string"
        )
    if len(value.encode("utf-8")) > limit:
        raise ValidationError(f"{path} exceeds {limit} UTF-8 bytes")
    return value


def _identifier(value: Any, path: str) -> str:
    result = _string(value, path, limit=128)
    if not _ID_RE.fullmatch(result):
        raise ValidationError(f"{path} is not a valid stable identifier: {result!r}")
    return result


def _strings(value: Any, path: str, *, identifiers: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be an array")
    items = tuple(
        _identifier(item, f"{path}[{index}]")
        if identifiers
        else _string(item, f"{path}[{index}]")
        for index, item in enumerate(value)
    )
    if len(set(items)) != len(items):
        raise ValidationError(f"{path} must not contain duplicates")
    return items


@dataclass(frozen=True)
class Provenance:
    id: str
    kind: str
    source: str
    locator: str
    acquired_at: str | None
    method: str
    confidence: float
    sha256: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Provenance:
        item = _strict_mapping(
            value,
            path,
            required={"id", "kind", "source", "locator", "method", "confidence"},
            optional={"acquired_at", "sha256", "notes"},
        )
        confidence = item["confidence"]
        if not _is_number(confidence) or not 0 <= float(confidence) <= 1:
            raise ValidationError(f"{path}.confidence must be between 0 and 1")
        sha256 = item.get("sha256")
        if sha256 is not None and (
            not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            raise ValidationError(f"{path}.sha256 must be a lowercase SHA-256 digest")
        acquired_at = item.get("acquired_at")
        if acquired_at is not None:
            acquired_at = _string(acquired_at, f"{path}.acquired_at", limit=64)
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            kind=_identifier(item["kind"], f"{path}.kind"),
            source=_string(item["source"], f"{path}.source", limit=256),
            locator=_string(item["locator"], f"{path}.locator", limit=2048),
            acquired_at=acquired_at,
            method=_identifier(item["method"], f"{path}.method"),
            confidence=float(confidence),
            sha256=sha256,
            notes=_string(
                item.get("notes", ""), f"{path}.notes", nonempty=False, limit=4096
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "locator": self.locator,
            "method": self.method,
            "confidence": self.confidence,
        }
        if self.acquired_at is not None:
            result["acquired_at"] = self.acquired_at
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        if self.notes:
            result["notes"] = self.notes
        return result


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    acceptance: tuple[str, ...]
    risk: str
    provenance: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Requirement:
        item = _strict_mapping(
            value,
            path,
            required={"id", "text", "acceptance", "risk", "provenance"},
            optional=set(),
        )
        risk = _identifier(item["risk"], f"{path}.risk")
        if risk not in {"low", "medium", "high", "critical", "unknown"}:
            raise ValidationError(f"{path}.risk has an unsupported value")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            text=_string(item["text"], f"{path}.text"),
            acceptance=tuple(
                sorted(_strings(item["acceptance"], f"{path}.acceptance"))
            ),
            risk=risk,
            provenance=tuple(
                sorted(
                    _strings(item["provenance"], f"{path}.provenance", identifiers=True)
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "acceptance": sorted(self.acceptance),
            "risk": self.risk,
            "provenance": sorted(self.provenance),
        }


@dataclass(frozen=True)
class Scope:
    domains: tuple[str, ...]
    max_voltage_v: float
    max_current_a: float
    max_power_w: float
    layers: int
    intended_use: str
    risk_class: str

    @classmethod
    def from_dict(cls, value: Any, path: str = "$.scope") -> Scope:
        item = _strict_mapping(
            value,
            path,
            required={
                "domains",
                "max_voltage_v",
                "max_current_a",
                "max_power_w",
                "layers",
                "intended_use",
                "risk_class",
            },
            optional=set(),
        )
        for name in ("max_voltage_v", "max_current_a", "max_power_w"):
            if not _is_number(item[name]) or float(item[name]) <= 0:
                raise ValidationError(f"{path}.{name} must be a positive finite number")
        layers = item["layers"]
        if (
            isinstance(layers, bool)
            or not isinstance(layers, int)
            or layers < 1
            or layers > 64
        ):
            raise ValidationError(f"{path}.layers must be an integer from 1 to 64")
        return cls(
            domains=tuple(
                sorted(_strings(item["domains"], f"{path}.domains", identifiers=True))
            ),
            max_voltage_v=float(item["max_voltage_v"]),
            max_current_a=float(item["max_current_a"]),
            max_power_w=float(item["max_power_w"]),
            layers=layers,
            intended_use=_string(
                item["intended_use"], f"{path}.intended_use", limit=1024
            ),
            risk_class=_identifier(item["risk_class"], f"{path}.risk_class"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domains": sorted(self.domains),
            "max_voltage_v": self.max_voltage_v,
            "max_current_a": self.max_current_a,
            "max_power_w": self.max_power_w,
            "layers": self.layers,
            "intended_use": self.intended_use,
            "risk_class": self.risk_class,
        }


@dataclass(frozen=True)
class Placement:
    x_mm: float
    y_mm: float
    rotation_deg: float = 0.0
    side: str = "front"
    fixed: bool = False

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Placement:
        item = _strict_mapping(
            value,
            path,
            required={"x_mm", "y_mm"},
            optional={"rotation_deg", "side", "fixed"},
        )
        for name in ("x_mm", "y_mm", "rotation_deg"):
            current = item.get(name, 0.0)
            if not _is_number(current):
                raise ValidationError(f"{path}.{name} must be finite")
        side = item.get("side", "front")
        if side not in {"front", "back"}:
            raise ValidationError(f"{path}.side must be front or back")
        fixed = item.get("fixed", False)
        if not isinstance(fixed, bool):
            raise ValidationError(f"{path}.fixed must be boolean")
        return cls(
            x_mm=float(item["x_mm"]),
            y_mm=float(item["y_mm"]),
            rotation_deg=float(item.get("rotation_deg", 0.0)) % 360,
            side=side,
            fixed=fixed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "rotation_deg": self.rotation_deg,
            "side": self.side,
            "fixed": self.fixed,
        }


@dataclass(frozen=True)
class Component:
    id: str
    reference: str
    part_id: str
    value: str
    block_id: str
    placement: Placement | None
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Component:
        item = _strict_mapping(
            value,
            path,
            required={"id", "reference", "part_id", "value", "block_id"},
            optional={"placement", "attributes"},
        )
        reference = _string(item["reference"], f"{path}.reference", limit=64)
        if not _REF_RE.fullmatch(reference):
            raise ValidationError(
                f"{path}.reference is not a valid schematic reference"
            )
        placement_value = item.get("placement")
        attributes = item.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise ValidationError(f"{path}.attributes must be an object")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            reference=reference,
            part_id=_identifier(item["part_id"], f"{path}.part_id"),
            value=_string(item["value"], f"{path}.value", limit=256),
            block_id=_identifier(item["block_id"], f"{path}.block_id"),
            placement=(
                Placement.from_dict(placement_value, f"{path}.placement")
                if placement_value is not None
                else None
            ),
            attributes=_json_value(attributes, f"{path}.attributes"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "reference": self.reference,
            "part_id": self.part_id,
            "value": self.value,
            "block_id": self.block_id,
            "attributes": _json_value(self.attributes),
        }
        if self.placement is not None:
            result["placement"] = self.placement.to_dict()
        return result


@dataclass(frozen=True, order=True)
class Endpoint:
    component: str
    pin: str
    role: str = "signal"

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Endpoint:
        item = _strict_mapping(
            value, path, required={"component", "pin"}, optional={"role"}
        )
        return cls(
            component=_identifier(item["component"], f"{path}.component"),
            pin=_string(item["pin"], f"{path}.pin", limit=64),
            role=_identifier(item.get("role", "signal"), f"{path}.role"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"component": self.component, "pin": self.pin, "role": self.role}


@dataclass(frozen=True)
class Net:
    id: str
    name: str
    endpoints: tuple[Endpoint, ...]
    net_class: str
    power_domain: str | None
    interface: str | None
    intent: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Net:
        item = _strict_mapping(
            value,
            path,
            required={"id", "name", "endpoints", "net_class", "intent"},
            optional={"power_domain", "interface"},
        )
        name = _string(item["name"], f"{path}.name", limit=128)
        if not _NET_RE.fullmatch(name):
            raise ValidationError(f"{path}.name contains unsupported characters")
        endpoints_value = item["endpoints"]
        if not isinstance(endpoints_value, list):
            raise ValidationError(f"{path}.endpoints must be an array")
        endpoints = tuple(
            Endpoint.from_dict(endpoint, f"{path}.endpoints[{index}]")
            for index, endpoint in enumerate(endpoints_value)
        )
        if len(set(endpoints)) != len(endpoints):
            raise ValidationError(f"{path}.endpoints contains duplicates")
        power_domain = item.get("power_domain")
        interface = item.get("interface")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            name=name,
            endpoints=tuple(sorted(endpoints)),
            net_class=_identifier(item["net_class"], f"{path}.net_class"),
            power_domain=(
                _identifier(power_domain, f"{path}.power_domain")
                if power_domain is not None
                else None
            ),
            interface=(
                _identifier(interface, f"{path}.interface")
                if interface is not None
                else None
            ),
            intent=_string(item["intent"], f"{path}.intent", limit=1024),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "endpoints": [endpoint.to_dict() for endpoint in sorted(self.endpoints)],
            "net_class": self.net_class,
            "intent": self.intent,
        }
        if self.power_domain is not None:
            result["power_domain"] = self.power_domain
        if self.interface is not None:
            result["interface"] = self.interface
        return result


@dataclass(frozen=True)
class FunctionalBlock:
    id: str
    kind: str
    name: str
    version: str
    intent: str
    components: tuple[str, ...]
    provenance: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, path: str) -> FunctionalBlock:
        item = _strict_mapping(
            value,
            path,
            required={
                "id",
                "kind",
                "name",
                "version",
                "intent",
                "components",
                "provenance",
            },
            optional=set(),
        )
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            kind=_identifier(item["kind"], f"{path}.kind"),
            name=_string(item["name"], f"{path}.name", limit=256),
            version=_string(item["version"], f"{path}.version", limit=64),
            intent=_string(item["intent"], f"{path}.intent", limit=2048),
            components=tuple(
                sorted(
                    _strings(item["components"], f"{path}.components", identifiers=True)
                )
            ),
            provenance=tuple(
                sorted(
                    _strings(item["provenance"], f"{path}.provenance", identifiers=True)
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "intent": self.intent,
            "components": sorted(self.components),
            "provenance": sorted(self.provenance),
        }


@dataclass(frozen=True)
class PowerDomain:
    id: str
    nominal_v: float
    min_v: float
    max_v: float
    max_current_a: float
    source: Endpoint
    intent: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PowerDomain:
        item = _strict_mapping(
            value,
            path,
            required={
                "id",
                "nominal_v",
                "min_v",
                "max_v",
                "max_current_a",
                "source",
                "intent",
            },
            optional=set(),
        )
        for name in ("nominal_v", "min_v", "max_v", "max_current_a"):
            if not _is_number(item[name]) or float(item[name]) < 0:
                raise ValidationError(
                    f"{path}.{name} must be a non-negative finite number"
                )
        if not float(item["min_v"]) <= float(item["nominal_v"]) <= float(item["max_v"]):
            raise ValidationError(f"{path} voltage range does not contain nominal_v")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            nominal_v=float(item["nominal_v"]),
            min_v=float(item["min_v"]),
            max_v=float(item["max_v"]),
            max_current_a=float(item["max_current_a"]),
            source=Endpoint.from_dict(item["source"], f"{path}.source"),
            intent=_string(item["intent"], f"{path}.intent", limit=1024),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "nominal_v": self.nominal_v,
            "min_v": self.min_v,
            "max_v": self.max_v,
            "max_current_a": self.max_current_a,
            "source": self.source.to_dict(),
            "intent": self.intent,
        }


@dataclass(frozen=True)
class Interface:
    id: str
    kind: str
    power_domain: str
    members: tuple[Endpoint, ...]
    controller: Endpoint | None
    params: dict[str, Any]
    intent: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Interface:
        item = _strict_mapping(
            value,
            path,
            required={"id", "kind", "power_domain", "members", "params", "intent"},
            optional={"controller"},
        )
        members_value = item["members"]
        if not isinstance(members_value, list):
            raise ValidationError(f"{path}.members must be an array")
        params = item["params"]
        if not isinstance(params, Mapping):
            raise ValidationError(f"{path}.params must be an object")
        controller_value = item.get("controller")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            kind=_identifier(item["kind"], f"{path}.kind"),
            power_domain=_identifier(item["power_domain"], f"{path}.power_domain"),
            members=tuple(
                sorted(
                    Endpoint.from_dict(member, f"{path}.members[{index}]")
                    for index, member in enumerate(members_value)
                )
            ),
            controller=(
                Endpoint.from_dict(controller_value, f"{path}.controller")
                if controller_value
                else None
            ),
            params=_json_value(params, f"{path}.params"),
            intent=_string(item["intent"], f"{path}.intent", limit=1024),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "power_domain": self.power_domain,
            "members": [member.to_dict() for member in sorted(self.members)],
            "params": _json_value(self.params),
            "intent": self.intent,
        }
        if self.controller is not None:
            result["controller"] = self.controller.to_dict()
        return result


@dataclass(frozen=True)
class Constraint:
    id: str
    kind: str
    targets: tuple[str, ...]
    params: dict[str, Any]
    severity: str
    rationale: str
    provenance: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Constraint:
        item = _strict_mapping(
            value,
            path,
            required={
                "id",
                "kind",
                "targets",
                "params",
                "severity",
                "rationale",
                "provenance",
            },
            optional=set(),
        )
        params = item["params"]
        if not isinstance(params, Mapping):
            raise ValidationError(f"{path}.params must be an object")
        severity = _identifier(item["severity"], f"{path}.severity")
        if severity not in {"advisory", "required", "release_blocking"}:
            raise ValidationError(f"{path}.severity is unsupported")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            kind=_identifier(item["kind"], f"{path}.kind"),
            targets=tuple(sorted(_strings(item["targets"], f"{path}.targets"))),
            params=_json_value(params, f"{path}.params"),
            severity=severity,
            rationale=_string(item["rationale"], f"{path}.rationale", limit=2048),
            provenance=tuple(
                sorted(
                    _strings(item["provenance"], f"{path}.provenance", identifiers=True)
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "targets": sorted(self.targets),
            "params": _json_value(self.params),
            "severity": self.severity,
            "rationale": self.rationale,
            "provenance": sorted(self.provenance),
        }


@dataclass(frozen=True)
class BoardSpec:
    width_mm: float
    height_mm: float
    layers: int
    thickness_mm: float
    edge_clearance_mm: float
    min_track_mm: float
    min_clearance_mm: float
    min_drill_mm: float
    finish: str

    @classmethod
    def from_dict(cls, value: Any, path: str = "$.board") -> BoardSpec:
        item = _strict_mapping(
            value,
            path,
            required={
                "width_mm",
                "height_mm",
                "layers",
                "thickness_mm",
                "edge_clearance_mm",
                "min_track_mm",
                "min_clearance_mm",
                "min_drill_mm",
                "finish",
            },
            optional=set(),
        )
        for name in (
            "width_mm",
            "height_mm",
            "thickness_mm",
            "edge_clearance_mm",
            "min_track_mm",
            "min_clearance_mm",
            "min_drill_mm",
        ):
            if not _is_number(item[name]) or float(item[name]) <= 0:
                raise ValidationError(f"{path}.{name} must be a positive finite number")
        if isinstance(item["layers"], bool) or not isinstance(item["layers"], int):
            raise ValidationError(f"{path}.layers must be an integer")
        return cls(
            width_mm=float(item["width_mm"]),
            height_mm=float(item["height_mm"]),
            layers=item["layers"],
            thickness_mm=float(item["thickness_mm"]),
            edge_clearance_mm=float(item["edge_clearance_mm"]),
            min_track_mm=float(item["min_track_mm"]),
            min_clearance_mm=float(item["min_clearance_mm"]),
            min_drill_mm=float(item["min_drill_mm"]),
            finish=_identifier(item["finish"], f"{path}.finish"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "layers": self.layers,
            "thickness_mm": self.thickness_mm,
            "edge_clearance_mm": self.edge_clearance_mm,
            "min_track_mm": self.min_track_mm,
            "min_clearance_mm": self.min_clearance_mm,
            "min_drill_mm": self.min_drill_mm,
            "finish": self.finish,
        }


@dataclass(frozen=True)
class Design:
    design_id: str
    name: str
    revision: str
    scope: Scope
    requirements: tuple[Requirement, ...]
    provenance: tuple[Provenance, ...]
    blocks: tuple[FunctionalBlock, ...]
    power_domains: tuple[PowerDomain, ...]
    interfaces: tuple[Interface, ...]
    components: tuple[Component, ...]
    nets: tuple[Net, ...]
    constraints: tuple[Constraint, ...]
    board: BoardSpec
    analyses: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    schema: str = IR_SCHEMA
    version: int = IR_VERSION

    @classmethod
    def from_dict(cls, value: Any, *, validate: bool = True) -> Design:
        item = _strict_mapping(
            value,
            "$",
            required={
                "schema",
                "version",
                "design_id",
                "name",
                "revision",
                "scope",
                "requirements",
                "provenance",
                "blocks",
                "power_domains",
                "interfaces",
                "components",
                "nets",
                "constraints",
                "board",
                "analyses",
                "metadata",
            },
            optional=set(),
        )
        if item["schema"] != IR_SCHEMA or item["version"] != IR_VERSION:
            raise ValidationError(
                f"unsupported IR schema/version: {item.get('schema')!r}/{item.get('version')!r}"
            )

        def parse_array(name: str, parser: Any) -> tuple[Any, ...]:
            raw = item[name]
            if not isinstance(raw, list):
                raise ValidationError(f"$.{name} must be an array")
            parsed = tuple(
                parser(entry, f"$.{name}[{index}]") for index, entry in enumerate(raw)
            )
            return tuple(sorted(parsed, key=lambda entry: entry.id))

        analyses = item["analyses"]
        if not isinstance(analyses, list) or not all(
            isinstance(entry, Mapping) for entry in analyses
        ):
            raise ValidationError("$.analyses must be an array of objects")
        metadata = item["metadata"]
        if not isinstance(metadata, Mapping):
            raise ValidationError("$.metadata must be an object")
        design = cls(
            design_id=_identifier(item["design_id"], "$.design_id"),
            name=_string(item["name"], "$.name", limit=256),
            revision=_string(item["revision"], "$.revision", limit=64),
            scope=Scope.from_dict(item["scope"]),
            requirements=parse_array("requirements", Requirement.from_dict),
            provenance=parse_array("provenance", Provenance.from_dict),
            blocks=parse_array("blocks", FunctionalBlock.from_dict),
            power_domains=parse_array("power_domains", PowerDomain.from_dict),
            interfaces=parse_array("interfaces", Interface.from_dict),
            components=parse_array("components", Component.from_dict),
            nets=parse_array("nets", Net.from_dict),
            constraints=parse_array("constraints", Constraint.from_dict),
            board=BoardSpec.from_dict(item["board"]),
            analyses=tuple(
                sorted(
                    (
                        _json_value(entry, f"$.analyses[{index}]")
                        for index, entry in enumerate(analyses)
                    ),
                    key=_canonical_sort_key,
                )
            ),
            metadata=_json_value(metadata, "$.metadata"),
        )
        if validate:
            design.assert_valid()
        return design

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "design_id": self.design_id,
            "name": self.name,
            "revision": self.revision,
            "scope": self.scope.to_dict(),
            "requirements": [
                entry.to_dict()
                for entry in sorted(self.requirements, key=lambda entry: entry.id)
            ],
            "provenance": [
                entry.to_dict()
                for entry in sorted(self.provenance, key=lambda entry: entry.id)
            ],
            "blocks": [
                entry.to_dict()
                for entry in sorted(self.blocks, key=lambda entry: entry.id)
            ],
            "power_domains": [
                entry.to_dict()
                for entry in sorted(self.power_domains, key=lambda entry: entry.id)
            ],
            "interfaces": [
                entry.to_dict()
                for entry in sorted(self.interfaces, key=lambda entry: entry.id)
            ],
            "components": [
                entry.to_dict()
                for entry in sorted(self.components, key=lambda entry: entry.id)
            ],
            "nets": [
                entry.to_dict()
                for entry in sorted(self.nets, key=lambda entry: entry.id)
            ],
            "constraints": [
                entry.to_dict()
                for entry in sorted(self.constraints, key=lambda entry: entry.id)
            ],
            "board": self.board.to_dict(),
            "analyses": sorted(
                (_json_value(entry) for entry in self.analyses), key=_canonical_sort_key
            ),
            "metadata": _json_value(self.metadata),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def clone(self) -> Design:
        return Design.from_dict(copy.deepcopy(self.to_dict()))

    def issues(self) -> list[IRIssue]:
        issues: list[IRIssue] = []

        def unique(entries: Iterable[Any], attribute: str, path: str) -> set[str]:
            seen: set[str] = set()
            for index, entry in enumerate(entries):
                value = getattr(entry, attribute)
                if value in seen:
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.duplicate",
                            f"$.{path}[{index}].{attribute}",
                            f"duplicate {attribute}: {value}",
                        )
                    )
                seen.add(value)
            return seen

        provenance = unique(self.provenance, "id", "provenance")
        requirements = unique(self.requirements, "id", "requirements")
        blocks = unique(self.blocks, "id", "blocks")
        component_ids = unique(self.components, "id", "components")
        references = unique(self.components, "reference", "components")
        del references
        net_ids = unique(self.nets, "id", "nets")
        net_names = unique(self.nets, "name", "nets")
        del net_names
        power_domains = unique(self.power_domains, "id", "power_domains")
        interfaces = unique(self.interfaces, "id", "interfaces")
        constraint_ids = unique(self.constraints, "id", "constraints")
        del requirements, constraint_ids

        for index, requirement in enumerate(self.requirements):
            for source in requirement.provenance:
                if source not in provenance:
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.missing_provenance",
                            f"$.requirements[{index}].provenance",
                            f"unknown provenance id: {source}",
                        )
                    )
        for index, block in enumerate(self.blocks):
            for component in block.components:
                if component not in component_ids:
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.missing_component",
                            f"$.blocks[{index}].components",
                            f"unknown component id: {component}",
                        )
                    )
            for source in block.provenance:
                if source not in provenance:
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.missing_provenance",
                            f"$.blocks[{index}].provenance",
                            f"unknown provenance id: {source}",
                        )
                    )
        for index, component in enumerate(self.components):
            if component.block_id not in blocks:
                issues.append(
                    IRIssue(
                        "error",
                        "ir.missing_block",
                        f"$.components[{index}].block_id",
                        f"unknown block id: {component.block_id}",
                    )
                )
            if component.placement and not (
                0 <= component.placement.x_mm <= self.board.width_mm
                and 0 <= component.placement.y_mm <= self.board.height_mm
            ):
                issues.append(
                    IRIssue(
                        "error",
                        "ir.placement_outside_board",
                        f"$.components[{index}].placement",
                        "placement origin is outside the board outline",
                    )
                )
        used_endpoints: dict[tuple[str, str], str] = {}
        for index, net in enumerate(self.nets):
            if len(net.endpoints) < 1:
                issues.append(
                    IRIssue(
                        "warning",
                        "ir.empty_net",
                        f"$.nets[{index}]",
                        "net has no endpoints",
                    )
                )
            if net.power_domain and net.power_domain not in power_domains:
                issues.append(
                    IRIssue(
                        "error",
                        "ir.missing_power_domain",
                        f"$.nets[{index}].power_domain",
                        f"unknown power domain: {net.power_domain}",
                    )
                )
            if net.interface and net.interface not in interfaces:
                issues.append(
                    IRIssue(
                        "error",
                        "ir.missing_interface",
                        f"$.nets[{index}].interface",
                        f"unknown interface: {net.interface}",
                    )
                )
            for endpoint_index, endpoint in enumerate(net.endpoints):
                if endpoint.component not in component_ids:
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.missing_component",
                            f"$.nets[{index}].endpoints[{endpoint_index}]",
                            f"unknown component id: {endpoint.component}",
                        )
                    )
                key = (endpoint.component, endpoint.pin)
                previous = used_endpoints.get(key)
                if previous and previous != net.id:
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.pin_on_multiple_nets",
                            f"$.nets[{index}].endpoints[{endpoint_index}]",
                            f"pin already belongs to net {previous}",
                        )
                    )
                used_endpoints[key] = net.id
        for index, domain in enumerate(self.power_domains):
            if domain.source.component not in component_ids:
                issues.append(
                    IRIssue(
                        "error",
                        "ir.missing_component",
                        f"$.power_domains[{index}].source",
                        f"unknown source component: {domain.source.component}",
                    )
                )
            if domain.max_v > self.scope.max_voltage_v:
                issues.append(
                    IRIssue(
                        "error",
                        "scope.voltage_exceeded",
                        f"$.power_domains[{index}].max_v",
                        "power-domain voltage exceeds declared design scope",
                    )
                )
        for index, interface in enumerate(self.interfaces):
            if interface.power_domain not in power_domains:
                issues.append(
                    IRIssue(
                        "error",
                        "ir.missing_power_domain",
                        f"$.interfaces[{index}].power_domain",
                        f"unknown power domain: {interface.power_domain}",
                    )
                )
            for endpoint in interface.members + (
                (interface.controller,) if interface.controller else ()
            ):
                if endpoint is not None and endpoint.component not in component_ids:
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.missing_component",
                            f"$.interfaces[{index}].members",
                            f"unknown interface component: {endpoint.component}",
                        )
                    )
        target_ids = component_ids | net_ids | blocks | power_domains | interfaces
        for index, constraint in enumerate(self.constraints):
            for target in constraint.targets:
                if target not in target_ids and not target.startswith("board"):
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.missing_constraint_target",
                            f"$.constraints[{index}].targets",
                            f"unknown constraint target: {target}",
                        )
                    )
            for source in constraint.provenance:
                if source not in provenance:
                    issues.append(
                        IRIssue(
                            "error",
                            "ir.missing_provenance",
                            f"$.constraints[{index}].provenance",
                            f"unknown provenance id: {source}",
                        )
                    )
        if self.scope.layers != self.board.layers:
            issues.append(
                IRIssue(
                    "error",
                    "ir.layer_mismatch",
                    "$.board.layers",
                    "board layer count differs from declared scope",
                )
            )
        return sorted(set(issues))

    def assert_valid(self) -> None:
        errors = [issue for issue in self.issues() if issue.severity == "error"]
        if errors:
            first = errors[0]
            raise ValidationError(
                f"invalid semantic IR ({len(errors)} errors): {first.code} at {first.path}: {first.message}"
            )


def _canonical_sort_key(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _json_value(value)
    return (
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def load_design(path: str | Path) -> Design:
    source = Path(path)
    try:
        raw = read_bytes_limited(source, IR_FILE_LIMIT)
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load semantic IR {source}: {exc}") from exc
    return Design.from_dict(value)


def save_design(path: str | Path, design: Design) -> None:
    design.assert_valid()
    atomic_write_bytes(Path(path), design.canonical_bytes())


def ir_json_schema() -> dict[str, Any]:
    """Return the stable public envelope schema.

    Runtime validation is intentionally stricter and performs graph checks that JSON
    Schema cannot express.  The envelope schema is useful for agent tool contracts.
    """
    arrays = {
        name: {"type": "array", "items": {"type": "object"}}
        for name in (
            "requirements",
            "provenance",
            "blocks",
            "power_domains",
            "interfaces",
            "components",
            "nets",
            "constraints",
            "analyses",
        )
    }
    properties: dict[str, Any] = {
        "schema": {"const": IR_SCHEMA},
        "version": {"const": IR_VERSION},
        "design_id": {"type": "string", "pattern": _ID_RE.pattern},
        "name": {"type": "string", "minLength": 1},
        "revision": {"type": "string", "minLength": 1},
        "scope": {"type": "object"},
        "board": {"type": "object"},
        "metadata": {"type": "object"},
        **arrays,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pcb-agent-runtime.invalid/schema/ir-v1.json",
        "title": "CopperWright semantic design IR v1",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": properties,
    }
