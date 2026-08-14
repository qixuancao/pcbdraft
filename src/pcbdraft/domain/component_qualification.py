"""Generation-time component evidence without pretending it is sign-off.

The generic planner is allowed to select installed KiCad symbols and
footprints.  That is enough to make native files, but it is not the same thing
as choosing an orderable manufacturer part or verifying a datasheet.  This
module records those states separately and checks the one mapping fact the
runtime can establish locally: every symbol pin used by a part record maps to
an actual pad number in the selected footprint.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import read_bytes_limited
from pcbdraft.domain.ir import (
    Design,
    _identifier,
    _strict_mapping,
    _string,
    canonical_json_bytes,
)
from pcbdraft.domain.parts import TRUST_STATES, PartGraph, PartRecord

COMPONENT_QUALIFICATION_SCHEMA = "pcbdraft-component-qualification"
COMPONENT_QUALIFICATION_VERSION = 1
COMPONENT_QUALIFICATION_NAME = "component-qualification.json"
FOOTPRINT_FILE_LIMIT = 64 * 1024 * 1024
MAX_QUALIFIED_COMPONENTS = 2_048

_PAD = re.compile(rb'\(pad\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()]+))(?=\s)')
_DIGIT = re.compile(r"(\d+)")
_COMPONENT_FIELDS = {
    "id",
    "reference",
    "part_id",
    "trust",
    "overall_state",
    "identity",
    "symbol",
    "footprint",
    "datasheet",
    "electrical_metadata",
}
_OVERALL_STATES = {
    "human_verified",
    "invalid_local_mapping",
    "local_library_only",
    "production_verified",
    "rule_validated",
}


def _exact_object(value: Any, fields: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"{path} fields are malformed")
    return value


def _optional_text(value: Any, path: str, *, limit: int = 4096) -> str | None:
    if value is None:
        return None
    return _string(value, path, limit=limit)


def _string_list(value: Any, path: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 4_096
        or not all(
            isinstance(item, str) and len(item.encode("utf-8")) <= 256 for item in value
        )
        or len(value) != len(set(value))
    ):
        raise ValidationError(f"{path} must be a bounded unique string array")
    return value


def _state(value: Any, allowed: set[str], path: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{path} is invalid")
    return value


def _validate_component_records(component: dict[str, Any], path: str) -> None:
    if (
        not isinstance(component["trust"], str)
        or component["trust"] not in TRUST_STATES
    ):
        raise ValidationError(f"{path}.trust is invalid")
    if (
        not isinstance(component["overall_state"], str)
        or component["overall_state"] not in _OVERALL_STATES
    ):
        raise ValidationError(f"{path}.overall_state is invalid")

    identity = _exact_object(
        component["identity"],
        {"manufacturer", "mpn", "state", "orderable_identity_established"},
        f"{path}.identity",
    )
    _string(identity["manufacturer"], f"{path}.identity.manufacturer", limit=256)
    _string(identity["mpn"], f"{path}.identity.mpn", limit=256)
    identity_state = _state(
        identity["state"],
        {"attributed_catalog_record", "unspecified", "unverified_claim"},
        f"{path}.identity.state",
    )
    if not isinstance(identity["orderable_identity_established"], bool):
        raise ValidationError(f"{path}.identity state is invalid")
    if identity["orderable_identity_established"] != (
        identity_state == "attributed_catalog_record"
    ):
        raise ValidationError(f"{path}.identity derived state is inconsistent")

    symbol = _exact_object(
        component["symbol"],
        {"library_id", "state", "source", "reason"},
        f"{path}.symbol",
    )
    _string(symbol["library_id"], f"{path}.symbol.library_id", limit=256)
    _state(symbol["state"], {"available", "unavailable"}, f"{path}.symbol.state")
    _optional_text(symbol["source"], f"{path}.symbol.source")
    _optional_text(symbol["reason"], f"{path}.symbol.reason")

    footprint = _exact_object(
        component["footprint"],
        {
            "library_id",
            "state",
            "source",
            "mapped_symbol_pads",
            "native_pad_numbers",
            "missing_mapped_pads",
            "extra_native_pads",
            "reason",
        },
        f"{path}.footprint",
    )
    _optional_text(footprint["library_id"], f"{path}.footprint.library_id", limit=256)
    _state(
        footprint["state"],
        {"not_applicable", "pad_map_checked", "pad_mismatch", "unavailable"},
        f"{path}.footprint.state",
    )
    _optional_text(footprint["source"], f"{path}.footprint.source")
    _optional_text(footprint["reason"], f"{path}.footprint.reason")
    for name in (
        "mapped_symbol_pads",
        "native_pad_numbers",
        "missing_mapped_pads",
        "extra_native_pads",
    ):
        _string_list(footprint[name], f"{path}.footprint.{name}")
    mapped = set(footprint["mapped_symbol_pads"])
    native = set(footprint["native_pad_numbers"])
    if set(footprint["missing_mapped_pads"]) != mapped - native:
        raise ValidationError(f"{path}.footprint missing-pad evidence is inconsistent")
    if set(footprint["extra_native_pads"]) != native - mapped:
        raise ValidationError(f"{path}.footprint extra-pad evidence is inconsistent")

    datasheet = _exact_object(
        component["datasheet"],
        {"state", "locator", "source", "method", "verification"},
        f"{path}.datasheet",
    )
    _state(
        datasheet["state"],
        {"attributed_record", "missing", "reference_only"},
        f"{path}.datasheet.state",
    )
    for name in ("locator", "source", "method"):
        _optional_text(datasheet[name], f"{path}.datasheet.{name}")
    _string(datasheet["verification"], f"{path}.datasheet.verification", limit=512)

    electrical = _exact_object(
        component["electrical_metadata"],
        {"state", "ratings", "verification"},
        f"{path}.electrical_metadata",
    )
    _state(
        electrical["state"],
        {"missing", "recorded"},
        f"{path}.electrical_metadata.state",
    )
    _string_list(electrical["ratings"], f"{path}.electrical_metadata.ratings")
    _string(
        electrical["verification"],
        f"{path}.electrical_metadata.verification",
        limit=512,
    )


def _natural(value: str) -> tuple[Any, ...]:
    return tuple(int(item) if item.isdigit() else item for item in _DIGIT.split(value))


def _pad_numbers(path: Path) -> tuple[str, ...]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("selected footprint file is unavailable or unsafe")
    data = read_bytes_limited(path, FOOTPRINT_FILE_LIMIT)
    pads: set[str] = set()
    for match in _PAD.finditer(data):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if raw is None:
            continue
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                "selected footprint has a non-UTF-8 pad number"
            ) from exc
        value = value.replace(r"\"", '"').replace(r"\\", "\\")
        if value:
            pads.add(value)
    return tuple(sorted(pads, key=_natural))


def _datasheet(part: PartRecord) -> dict[str, Any]:
    records = sorted(
        (entry for entry in part.evidence if entry.kind == "datasheet"),
        key=lambda entry: entry.id,
    )
    if not records:
        return {
            "state": "missing",
            "locator": None,
            "source": None,
            "method": None,
            "verification": "no_datasheet_evidence_recorded",
        }
    record = records[0]
    state = (
        "reference_only"
        if record.method in {"local_library_extract", "local_library_reference"}
        else "attributed_record"
    )
    return {
        "state": state,
        "locator": record.locator,
        "source": record.source,
        "method": record.method,
        "verification": (
            "locator_copied_from_installed_kicad_metadata_not_fetched_or_authenticated"
            if state == "reference_only"
            else "record_attributed_but_document_truth_not_reverified_by_this_run"
        ),
    }


def _identity(part: PartRecord) -> dict[str, Any]:
    specified = part.manufacturer != "not specified" and part.mpn != "not specified"
    if not specified:
        state = "unspecified"
    elif part.trust in {"unverified", "extracted"}:
        state = "unverified_claim"
    else:
        state = "attributed_catalog_record"
    return {
        "manufacturer": part.manufacturer,
        "mpn": part.mpn,
        "state": state,
        "orderable_identity_established": state == "attributed_catalog_record",
    }


def _component_record(component: Any, graph: PartGraph) -> dict[str, Any]:
    part = graph.get(component.part_id)
    libraries = graph.resolve_libraries(part)
    symbol_resolution = libraries["symbol"]
    footprint_resolution = libraries["footprint"]
    mapped = tuple(
        sorted(
            {pin.footprint_pad for pin in part.pins if pin.footprint_pad}, key=_natural
        )
    )
    native: tuple[str, ...] = ()
    missing: tuple[str, ...] = mapped
    extra: tuple[str, ...] = ()
    footprint_state: str
    footprint_error: str | None = None
    if part.footprint is None:
        footprint_state = "not_applicable"
        missing = ()
    elif not footprint_resolution.available or footprint_resolution.path is None:
        footprint_state = "unavailable"
        footprint_error = footprint_resolution.reason or "footprint unavailable"
    else:
        try:
            native = _pad_numbers(Path(footprint_resolution.path))
        except (OSError, ValidationError) as exc:
            footprint_state = "unavailable"
            footprint_error = str(exc)
        else:
            missing = tuple(sorted(set(mapped) - set(native), key=_natural))
            extra = tuple(sorted(set(native) - set(mapped), key=_natural))
            footprint_state = "pad_mismatch" if missing else "pad_map_checked"

    identity = _identity(part)
    datasheet = _datasheet(part)
    if not symbol_resolution.available or footprint_state in {
        "unavailable",
        "pad_mismatch",
    }:
        overall = "invalid_local_mapping"
    elif part.trust == "production_verified":
        overall = "production_verified"
    elif part.trust == "human_verified":
        overall = "human_verified"
    elif part.trust == "rule_validated":
        overall = "rule_validated"
    else:
        overall = "local_library_only"
    return {
        "id": component.id,
        "reference": component.reference,
        "part_id": part.id,
        "trust": part.trust,
        "overall_state": overall,
        "identity": identity,
        "symbol": {
            "library_id": part.symbol,
            "state": "available" if symbol_resolution.available else "unavailable",
            "source": symbol_resolution.path,
            "reason": symbol_resolution.reason,
        },
        "footprint": {
            "library_id": part.footprint,
            "state": footprint_state,
            "source": footprint_resolution.path,
            "mapped_symbol_pads": list(mapped),
            "native_pad_numbers": list(native),
            "missing_mapped_pads": list(missing),
            "extra_native_pads": list(extra),
            "reason": footprint_error,
        },
        "datasheet": datasheet,
        "electrical_metadata": {
            "state": "recorded" if part.ratings else "missing",
            "ratings": sorted(part.ratings),
            "verification": "bounded_part_record_not_a_functional_validation",
        },
    }


@dataclass(frozen=True)
class ComponentQualificationReport:
    design_id: str
    part_catalog_hash: str
    components: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, value: Any) -> ComponentQualificationReport:
        item = _strict_mapping(
            value,
            "$",
            required={
                "schema",
                "version",
                "design_id",
                "part_catalog_hash",
                "summary",
                "limitations",
                "components",
            },
            optional=set(),
        )
        if (
            item["schema"] != COMPONENT_QUALIFICATION_SCHEMA
            or item["version"] != COMPONENT_QUALIFICATION_VERSION
        ):
            raise ValidationError("unsupported component qualification schema/version")
        digest = item["part_catalog_hash"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError(
                "component qualification part-catalog hash is invalid"
            )
        raw_components = item["components"]
        if (
            not isinstance(raw_components, list)
            or len(raw_components) > MAX_QUALIFIED_COMPONENTS
        ):
            raise ValidationError("component qualification components are invalid")
        components: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_components):
            component = _strict_mapping(
                raw,
                f"$.components[{index}]",
                required=_COMPONENT_FIELDS,
                optional=set(),
            )
            identifier = _identifier(component["id"], f"$.components[{index}].id")
            if identifier in seen:
                raise ValidationError("component qualification contains duplicate ids")
            seen.add(identifier)
            # The nested records are executable evidence, so accept only JSON
            # objects here and compare their complete canonical form to the
            # generation-time report when opening a managed project.
            normalized = dict(component)
            for name in (
                "identity",
                "symbol",
                "footprint",
                "datasheet",
                "electrical_metadata",
            ):
                if not isinstance(normalized[name], dict):
                    raise ValidationError(
                        f"$.components[{index}].{name} must be an object"
                    )
            _validate_component_records(normalized, f"$.components[{index}]")
            normalized["id"] = identifier
            normalized["reference"] = _string(
                component["reference"], f"$.components[{index}].reference", limit=64
            )
            normalized["part_id"] = _identifier(
                component["part_id"], f"$.components[{index}].part_id"
            )
            components.append(normalized)
        # Validate the derived fields by requiring an exact canonical round-trip.
        report = cls(
            design_id=_identifier(item["design_id"], "$.design_id"),
            part_catalog_hash=digest,
            components=tuple(components),
        )
        if report.to_dict() != value:
            raise ValidationError(
                "component qualification derived fields are inconsistent"
            )
        return report

    @property
    def pad_mapping_failures(self) -> tuple[str, ...]:
        return tuple(
            str(component["reference"])
            for component in self.components
            if component["footprint"].get("state") in {"unavailable", "pad_mismatch"}
            or component["symbol"].get("state") != "available"
        )

    @property
    def provisional_references(self) -> tuple[str, ...]:
        return tuple(
            str(component["reference"])
            for component in self.components
            if component["overall_state"] in {"local_library_only", "rule_validated"}
        )

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        datasheet_counts: dict[str, int] = {}
        identity_counts: dict[str, int] = {}
        for component in self.components:
            state = str(component["overall_state"])
            counts[state] = counts.get(state, 0) + 1
            datasheet_state = str(component["datasheet"].get("state", "missing"))
            datasheet_counts[datasheet_state] = (
                datasheet_counts.get(datasheet_state, 0) + 1
            )
            identity_state = str(component["identity"].get("state", "unspecified"))
            identity_counts[identity_state] = identity_counts.get(identity_state, 0) + 1
        return {
            "schema": COMPONENT_QUALIFICATION_SCHEMA,
            "version": COMPONENT_QUALIFICATION_VERSION,
            "design_id": self.design_id,
            "part_catalog_hash": self.part_catalog_hash,
            "summary": {
                "components": len(self.components),
                "states": dict(sorted(counts.items())),
                "datasheets": dict(sorted(datasheet_counts.items())),
                "procurement_identity": dict(sorted(identity_counts.items())),
                "pad_mapping_failures": len(self.pad_mapping_failures),
                "production_verified": counts.get("production_verified", 0),
            },
            "limitations": [
                "Local KiCad availability and pad-number mapping are deterministic runtime checks.",
                "A KiCad datasheet locator is a reference only unless separately attributed and reviewed.",
                "Manufacturer identity, ratings, lifecycle, sourcing, package suitability, and functional correctness require stronger evidence than a stock library entry.",
            ],
            "components": [copy.deepcopy(component) for component in self.components],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def qualify_components(
    design: Design, graph: PartGraph
) -> ComponentQualificationReport:
    """Build deterministic generation-time qualification evidence."""

    records = tuple(
        _component_record(component, graph)
        for component in sorted(design.components, key=lambda item: item.id)
    )
    return ComponentQualificationReport(
        design_id=design.design_id,
        part_catalog_hash=hashlib.sha256(
            canonical_json_bytes(graph.to_dict())
        ).hexdigest(),
        components=records,
    )
