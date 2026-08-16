"""Trusted component identity graph and design-to-part contract validation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import read_bytes_limited
from pcbdraft.core.resources import data_path
from pcbdraft.domain.ir import (
    Design,
    IRIssue,
    Provenance,
    _identifier,
    _json_value,
    _strict_mapping,
    _string,
)

PART_CATALOG_SCHEMA = "pcbdraft-part-catalog"
PART_CATALOG_VERSION = 1
PART_CATALOG_LIMIT = 32 * 1024 * 1024
LIBRARY_FILE_LIMIT = 128 * 1024 * 1024
TRUST_STATES = {
    "unverified",
    "extracted",
    "rule_validated",
    "human_verified",
    "production_verified",
}
LIFECYCLE_STATES = {"active", "nrnd", "obsolete", "unknown"}


@dataclass(frozen=True)
class PinDefinition:
    number: str
    name: str
    electrical_type: str
    functions: tuple[str, ...]
    required: bool
    footprint_pad: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PinDefinition:
        item = _strict_mapping(
            value,
            path,
            required={
                "number",
                "name",
                "electrical_type",
                "functions",
                "required",
                "footprint_pad",
            },
            optional=set(),
        )
        functions = item["functions"]
        if not isinstance(functions, list) or not all(
            isinstance(entry, str) and entry for entry in functions
        ):
            raise ValidationError(
                f"{path}.functions must be an array of non-empty strings"
            )
        if len(functions) != len(set(functions)):
            raise ValidationError(f"{path}.functions contains duplicates")
        if not isinstance(item["required"], bool):
            raise ValidationError(f"{path}.required must be boolean")
        return cls(
            number=_string(item["number"], f"{path}.number", limit=64),
            name=_string(item["name"], f"{path}.name", nonempty=False, limit=128),
            electrical_type=_identifier(
                item["electrical_type"], f"{path}.electrical_type"
            ),
            functions=tuple(sorted(functions)),
            required=item["required"],
            footprint_pad=_string(
                item["footprint_pad"], f"{path}.footprint_pad", limit=64
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "electrical_type": self.electrical_type,
            "functions": list(self.functions),
            "required": self.required,
            "footprint_pad": self.footprint_pad,
        }


@dataclass(frozen=True)
class PartRecord:
    id: str
    manufacturer: str
    mpn: str
    description: str
    kind: str
    symbol: str
    footprint: str | None
    pins: tuple[PinDefinition, ...]
    ratings: dict[str, Any]
    lifecycle: dict[str, Any]
    sourcing: dict[str, Any]
    manufacturing: dict[str, Any]
    models: dict[str, Any]
    bom: bool
    trust: str
    evidence: tuple[Provenance, ...]
    alternates: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PartRecord:
        item = _strict_mapping(
            value,
            path,
            required={
                "id",
                "manufacturer",
                "mpn",
                "description",
                "kind",
                "symbol",
                "footprint",
                "pins",
                "ratings",
                "lifecycle",
                "sourcing",
                "manufacturing",
                "models",
                "bom",
                "trust",
                "evidence",
                "alternates",
            },
            optional=set(),
        )
        footprint = item["footprint"]
        if footprint is not None:
            footprint = _string(footprint, f"{path}.footprint", limit=256)
            if ":" not in footprint:
                raise ValidationError(f"{path}.footprint must be a KiCad library id")
        symbol = _string(item["symbol"], f"{path}.symbol", limit=256)
        if ":" not in symbol:
            raise ValidationError(f"{path}.symbol must be a KiCad library id")
        pins_value = item["pins"]
        if not isinstance(pins_value, list) or not pins_value:
            raise ValidationError(f"{path}.pins must be a non-empty array")
        pins = tuple(
            PinDefinition.from_dict(pin, f"{path}.pins[{index}]")
            for index, pin in enumerate(pins_value)
        )
        numbers = [pin.number for pin in pins]
        pads = [pin.footprint_pad for pin in pins]
        if len(numbers) != len(set(numbers)):
            raise ValidationError(f"{path}.pins contains duplicate symbol pin numbers")
        if footprint is not None and len(pads) != len(set(pads)):
            raise ValidationError(f"{path}.pins contains duplicate footprint pads")
        for name in ("ratings", "lifecycle", "sourcing", "manufacturing", "models"):
            if not isinstance(item[name], Mapping):
                raise ValidationError(f"{path}.{name} must be an object")
        lifecycle_status = item["lifecycle"].get("status")
        if lifecycle_status not in LIFECYCLE_STATES:
            raise ValidationError(f"{path}.lifecycle.status is unsupported")
        if not isinstance(item["bom"], bool):
            raise ValidationError(f"{path}.bom must be boolean")
        if item["trust"] not in TRUST_STATES:
            raise ValidationError(f"{path}.trust is unsupported")
        evidence_value = item["evidence"]
        if not isinstance(evidence_value, list) or not evidence_value:
            raise ValidationError(
                f"{path}.evidence must contain at least one source record"
            )
        evidence = tuple(
            Provenance.from_dict(entry, f"{path}.evidence[{index}]")
            for index, entry in enumerate(evidence_value)
        )
        evidence_ids = [entry.id for entry in evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValidationError(f"{path}.evidence contains duplicate ids")
        alternates = item["alternates"]
        if not isinstance(alternates, list):
            raise ValidationError(f"{path}.alternates must be an array")
        alternate_ids = tuple(
            _identifier(entry, f"{path}.alternates[{index}]")
            for index, entry in enumerate(alternates)
        )
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            manufacturer=_string(
                item["manufacturer"], f"{path}.manufacturer", limit=256
            ),
            mpn=_string(item["mpn"], f"{path}.mpn", limit=256),
            description=_string(item["description"], f"{path}.description", limit=2048),
            kind=_identifier(item["kind"], f"{path}.kind"),
            symbol=symbol,
            footprint=footprint,
            pins=pins,
            ratings=_json_value(item["ratings"], f"{path}.ratings"),
            lifecycle=_json_value(item["lifecycle"], f"{path}.lifecycle"),
            sourcing=_json_value(item["sourcing"], f"{path}.sourcing"),
            manufacturing=_json_value(item["manufacturing"], f"{path}.manufacturing"),
            models=_json_value(item["models"], f"{path}.models"),
            bom=item["bom"],
            trust=item["trust"],
            evidence=evidence,
            alternates=alternate_ids,
        )

    def pin(self, number: str) -> PinDefinition | None:
        return next((pin for pin in self.pins if pin.number == number), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "manufacturer": self.manufacturer,
            "mpn": self.mpn,
            "description": self.description,
            "kind": self.kind,
            "symbol": self.symbol,
            "footprint": self.footprint,
            "pins": [
                pin.to_dict()
                for pin in sorted(self.pins, key=lambda pin: _natural_key(pin.number))
            ],
            "ratings": _json_value(self.ratings),
            "lifecycle": _json_value(self.lifecycle),
            "sourcing": _json_value(self.sourcing),
            "manufacturing": _json_value(self.manufacturing),
            "models": _json_value(self.models),
            "bom": self.bom,
            "trust": self.trust,
            "evidence": [
                entry.to_dict()
                for entry in sorted(self.evidence, key=lambda entry: entry.id)
            ],
            "alternates": sorted(self.alternates),
        }


def _natural_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)
    )


@dataclass(frozen=True)
class LibraryResolution:
    available: bool
    path: str | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"available": self.available, "path": self.path, "reason": self.reason}


class PartGraph:
    """Immutable canonical part records keyed by a stable digital identity."""

    def __init__(self, parts: Iterable[PartRecord], *, license_id: str, source: str):
        records = tuple(parts)
        ids = [part.id for part in records]
        if len(ids) != len(set(ids)):
            raise ValidationError("part catalog contains duplicate canonical ids")
        self._parts = {part.id: part for part in records}
        self.license_id = license_id
        self.source = source
        for part in records:
            for alternate in part.alternates:
                if alternate not in self._parts:
                    raise ValidationError(
                        f"part {part.id} references missing alternate {alternate}"
                    )

    @classmethod
    def from_dict(cls, value: Any, *, source: str = "<memory>") -> PartGraph:
        item = _strict_mapping(
            value,
            "$",
            required={"schema", "version", "license", "catalog_id", "parts"},
            optional={"notes"},
        )
        if (
            item["schema"] != PART_CATALOG_SCHEMA
            or item["version"] != PART_CATALOG_VERSION
        ):
            raise ValidationError("unsupported part catalog schema/version")
        parts = item["parts"]
        if not isinstance(parts, list):
            raise ValidationError("$.parts must be an array")
        return cls(
            (
                PartRecord.from_dict(part, f"$.parts[{index}]")
                for index, part in enumerate(parts)
            ),
            license_id=_string(item["license"], "$.license", limit=128),
            source=source,
        )

    @classmethod
    def load(cls, path: str | Path) -> PartGraph:
        source = Path(path)
        try:
            value = json.loads(read_bytes_limited(source, PART_CATALOG_LIMIT))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot load part catalog {source}: {exc}") from exc
        return cls.from_dict(value, source=str(source.resolve()))

    @classmethod
    def bundled(cls) -> PartGraph:
        return cls.load(data_path("parts", "catalog.json"))

    def get(self, part_id: str) -> PartRecord:
        try:
            return self._parts[part_id]
        except KeyError as exc:
            raise ValidationError(f"unknown canonical part id: {part_id}") from exc

    def merged(
        self,
        parts: Iterable[PartRecord],
        *,
        source: str | None = None,
    ) -> PartGraph:
        """Return an immutable graph extended with project-local part records.

        A project may carry parts extracted from the exact KiCad libraries used to
        generate it.  Never let a later record silently redefine an existing
        identity: reproducibility depends on the full record being identical.
        """

        records = dict(self._parts)
        for part in parts:
            existing = records.get(part.id)
            if existing is not None and existing.to_dict() != part.to_dict():
                raise ValidationError(
                    f"part catalog merge would redefine canonical part id: {part.id}"
                )
            records[part.id] = part
        return PartGraph(
            records.values(),
            license_id=self.license_id,
            source=source or self.source,
        )

    def find(
        self,
        *,
        kind: str | None = None,
        function: str | None = None,
        min_voltage_v: float | None = None,
        active_only: bool = True,
        trusted_only: bool = True,
    ) -> list[PartRecord]:
        result: list[PartRecord] = []
        for part in self._parts.values():
            if kind is not None and part.kind != kind:
                continue
            if function is not None and not any(
                function in pin.functions for pin in part.pins
            ):
                continue
            if active_only and part.lifecycle.get("status") != "active":
                continue
            if trusted_only and part.trust not in {
                "rule_validated",
                "human_verified",
                "production_verified",
            }:
                continue
            if min_voltage_v is not None:
                limit = part.ratings.get("absolute_max_voltage_v")
                if (
                    not isinstance(limit, (int, float))
                    or isinstance(limit, bool)
                    or limit < min_voltage_v
                ):
                    continue
            result.append(part)
        return sorted(result, key=lambda part: part.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PART_CATALOG_SCHEMA,
            "version": PART_CATALOG_VERSION,
            "license": self.license_id,
            "catalog_id": "runtime",
            "parts": [
                part.to_dict()
                for part in sorted(self._parts.values(), key=lambda part: part.id)
            ],
        }

    def resolve_libraries(
        self,
        part: PartRecord,
        *,
        symbol_root: str | Path | None = None,
        footprint_root: str | Path | None = None,
    ) -> dict[str, LibraryResolution]:
        symbol_base = Path(
            symbol_root
            or os.environ.get("KICAD_SYMBOL_DIR", "/usr/share/kicad/symbols")
        )
        footprint_base = Path(
            footprint_root
            or os.environ.get("KICAD_FOOTPRINT_DIR", "/usr/share/kicad/footprints")
        )
        symbol_lib, symbol_name = part.symbol.split(":", 1)
        symbol_path = symbol_base / f"{symbol_lib}.kicad_sym"
        symbol_resolution = _resolve_symbol(symbol_path, symbol_name)
        if part.footprint is None:
            footprint_resolution = LibraryResolution(
                True, None, "virtual/non-board part"
            )
        else:
            footprint_lib, footprint_name = part.footprint.split(":", 1)
            footprint_path = (
                footprint_base
                / f"{footprint_lib}.pretty"
                / f"{footprint_name}.kicad_mod"
            )
            footprint_resolution = (
                LibraryResolution(True, str(footprint_path.resolve()), None)
                if footprint_path.is_file() and not footprint_path.is_symlink()
                else LibraryResolution(
                    False,
                    str(footprint_path),
                    "footprint file not found or is a symlink",
                )
            )
        return {"symbol": symbol_resolution, "footprint": footprint_resolution}

    def validate_design(
        self,
        design: Design,
        *,
        check_libraries: bool = False,
        allow_provisional: bool = False,
    ) -> list[IRIssue]:
        issues: list[IRIssue] = []
        components = {component.id: component for component in design.components}
        connected: set[tuple[str, str]] = {
            (endpoint.component, endpoint.pin)
            for net in design.nets
            for endpoint in net.endpoints
        }
        domains = {domain.id: domain for domain in design.power_domains}
        for index, component in enumerate(design.components):
            part = self._parts.get(component.part_id)
            if part is None:
                issues.append(
                    IRIssue(
                        "error",
                        "part.unknown",
                        f"$.components[{index}].part_id",
                        f"canonical part does not exist: {component.part_id}",
                    )
                )
                continue
            if part.trust == "unverified" or (
                part.trust == "extracted" and not allow_provisional
            ):
                issues.append(
                    IRIssue(
                        "error",
                        "part.untrusted",
                        f"$.components[{index}].part_id",
                        (
                            f"part trust state is insufficient for generation: {part.trust}"
                            if not allow_provisional
                            else f"part trust state is insufficient even for a provisional attempt: {part.trust}"
                        ),
                    )
                )
            if (
                part.lifecycle.get("status") != "active"
                and part.bom
                and not (
                    allow_provisional
                    and part.trust == "extracted"
                    and part.lifecycle.get("status") == "unknown"
                )
            ):
                issues.append(
                    IRIssue(
                        "error",
                        "part.lifecycle",
                        f"$.components[{index}].part_id",
                        f"part lifecycle is {part.lifecycle.get('status')!r}",
                    )
                )
            allowed_unconnected = component.attributes.get("allow_unconnected_pins", [])
            if not isinstance(allowed_unconnected, list):
                issues.append(
                    IRIssue(
                        "error",
                        "part.invalid_unconnected_policy",
                        f"$.components[{index}].attributes",
                        "allow_unconnected_pins must be an array",
                    )
                )
                allowed_unconnected = []
            for pin in part.pins:
                if (
                    pin.required
                    and (component.id, pin.number) not in connected
                    and pin.number not in allowed_unconnected
                ):
                    issues.append(
                        IRIssue(
                            "error",
                            "part.required_pin_unconnected",
                            f"$.components[{index}]",
                            f"required pin {pin.number} ({pin.name}) is not assigned to a net",
                        )
                    )
            if part.bom and part.footprint is None:
                issues.append(
                    IRIssue(
                        "error",
                        "part.footprint_missing",
                        f"$.components[{index}]",
                        "BOM part has no footprint contract",
                    )
                )
            minimum_pad_gap = part.manufacturing.get("minimum_pad_gap_mm")
            if (
                isinstance(minimum_pad_gap, (int, float))
                and not isinstance(minimum_pad_gap, bool)
                and design.board.min_clearance_mm > float(minimum_pad_gap) + 1e-9
            ):
                issues.append(
                    IRIssue(
                        "error",
                        "part.footprint_clearance_incompatible",
                        f"$.components[{index}]",
                        f"board clearance {design.board.min_clearance_mm:g} mm exceeds the {part.id} footprint's {float(minimum_pad_gap):g} mm minimum pad gap",
                    )
                )
            if check_libraries:
                for library_kind, resolution in self.resolve_libraries(part).items():
                    if not resolution.available:
                        issues.append(
                            IRIssue(
                                "error",
                                f"part.{library_kind}_unavailable",
                                f"$.components[{index}]",
                                resolution.reason or "library unavailable",
                            )
                        )

        for net_index, net in enumerate(design.nets):
            domain = domains.get(net.power_domain) if net.power_domain else None
            for endpoint_index, endpoint in enumerate(net.endpoints):
                endpoint_component = components.get(endpoint.component)
                if endpoint_component is None:
                    continue
                endpoint_part = self._parts.get(endpoint_component.part_id)
                if endpoint_part is None:
                    continue
                endpoint_pin = endpoint_part.pin(endpoint.pin)
                if endpoint_pin is None:
                    issues.append(
                        IRIssue(
                            "error",
                            "part.pin_missing",
                            f"$.nets[{net_index}].endpoints[{endpoint_index}]",
                            f"part {endpoint_part.id} has no symbol pin {endpoint.pin}",
                        )
                    )
                    continue
                if not endpoint_pin.footprint_pad:
                    issues.append(
                        IRIssue(
                            "error",
                            "part.pad_mapping_missing",
                            f"$.nets[{net_index}].endpoints[{endpoint_index}]",
                            f"pin {endpoint.pin} lacks a footprint pad mapping",
                        )
                    )
                if domain is not None and endpoint_pin.electrical_type == "power_in":
                    supply = endpoint_part.ratings.get("supply_voltage_v")
                    if isinstance(supply, Mapping):
                        minimum = supply.get("min")
                        maximum = supply.get("max")
                        if isinstance(minimum, (int, float)) and domain.min_v < minimum:
                            issues.append(
                                IRIssue(
                                    "error",
                                    "part.undervoltage",
                                    f"$.nets[{net_index}]",
                                    f"{endpoint_part.id} minimum supply is {minimum} V",
                                )
                            )
                        if isinstance(maximum, (int, float)) and domain.max_v > maximum:
                            issues.append(
                                IRIssue(
                                    "error",
                                    "part.overvoltage",
                                    f"$.nets[{net_index}]",
                                    f"{endpoint_part.id} maximum supply is {maximum} V",
                                )
                            )
        return sorted(set(issues))

    def assert_design(
        self,
        design: Design,
        *,
        check_libraries: bool = False,
        allow_provisional: bool = False,
    ) -> None:
        errors = [
            issue
            for issue in self.validate_design(
                design,
                check_libraries=check_libraries,
                allow_provisional=allow_provisional,
            )
            if issue.severity == "error"
        ]
        if errors:
            first = errors[0]
            raise ValidationError(
                f"component contract validation failed ({len(errors)} errors): {first.code}: {first.message}"
            )


def _resolve_symbol(path: Path, symbol_name: str) -> LibraryResolution:
    if not path.is_file() or path.is_symlink():
        return LibraryResolution(
            False, str(path), "symbol library not found or is a symlink"
        )
    try:
        data = read_bytes_limited(path, LIBRARY_FILE_LIMIT)
    except (OSError, ValidationError) as exc:
        return LibraryResolution(False, str(path), f"cannot read symbol library: {exc}")
    pattern = rb'\(symbol\s+"' + re.escape(symbol_name.encode("utf-8")) + rb'"'
    if re.search(pattern, data) is None:
        return LibraryResolution(
            False, str(path), f"symbol {symbol_name!r} not found in library"
        )
    return LibraryResolution(True, str(path.resolve()), None)
