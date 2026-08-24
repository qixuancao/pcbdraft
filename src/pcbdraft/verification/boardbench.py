"""Strict, versioned contracts for the BoardBench evidence chain.

This module owns data validation and private atomic storage only.  It does not
run a model, inspect a board, score a result, or manufacture hardware.  Those
steps bind their evidence to these immutable, source-hashed records.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, ClassVar, Self, TypeVar
from urllib.parse import urlsplit

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, make_directory, read_text_limited
from pcbdraft.core.locking import ResourceLock

ARTIFACT_VERSION = 1
REVIEW_ARTIFACT_VERSION = 3
ARTIFACT_FILE_LIMIT = 8 * 1024 * 1024
CORPUS_FILE_LIMIT = 16 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_SHORT_TEXT_BYTES = 8 * 1024
MAX_ARRAY_ITEMS = 4_096
MAX_INVENTORY_FILES = 10_000
MAX_INVENTORY_DIRECTORIES = 10_000
MAX_INVENTORY_FILE_BYTES = 512 * 1024 * 1024
MAX_INVENTORY_BYTES = 4 * 1024 * 1024 * 1024
BOARD_BENCH_LOCK_DIR = ".pcbdraft-boardbench-locks"

CORPUS_SCHEMA = "pcbdraft-boardbench-corpus"
CAMPAIGN_SCHEMA = "pcbdraft-boardbench-campaign"
RUN_SCHEMA = "pcbdraft-boardbench-run"
SCORE_SCHEMA = "pcbdraft-boardbench-score"
REVIEW_SCHEMA = "pcbdraft-boardbench-review"
CORRECTION_SCHEMA = "pcbdraft-boardbench-correction"
HARDWARE_SCHEMA = "pcbdraft-boardbench-hardware"
SELECTION_SCHEMA = "pcbdraft-boardbench-selection"
REPORT_SCHEMA = "pcbdraft-boardbench-report"

BOARD_CATEGORIES = (
    "mcu_minimum_system",
    "sensor",
    "power",
    "driver",
    "adapter_communication",
)
SEALED_HOLDOUT_BASELINE_COHORT = "sealed_holdout_baseline"
PUBLIC_CORPUS_RERUN_COHORT = "public_corpus_rerun"
AI_REVIEWED_PILOT_COHORT = "ai_reviewed_pilot"
COHORTS = frozenset(
    {
        SEALED_HOLDOUT_BASELINE_COHORT,
        PUBLIC_CORPUS_RERUN_COHORT,
        AI_REVIEWED_PILOT_COHORT,
    }
)
METRIC_STATES = frozenset({"pass", "fail", "unknown", "not_applicable"})
PHYSICAL_STATES = frozenset({"pass", "fail", "not_tested", "not_applicable"})
RAIL_STATES = frozenset({"pass", "fail", "not_tested"})
AUTOMATIC_METRICS = (
    "complete_project",
    "library_resolution",
    "reference_topology",
    "erc",
    "drc",
    "support_circuits",
    "ratings",
    "false_completion",
)
FAILURE_STAGES = (
    "requirements_understanding",
    "component_knowledge",
    "circuit_design",
    "kicad_materialization",
    "layout",
    "routing",
    "validation",
)
FAILURE_STAGE_VALUES = frozenset((*FAILURE_STAGES, "unclassified"))
FAILURE_CAUSES = frozenset(
    {
        "model_reasoning",
        "component_knowledge_gap",
        "deterministic_tool_defect",
        "environment_infrastructure",
        "insufficient_evidence",
        "human_process",
        "unclassified",
    }
)
FAILURE_OWNERS = frozenset(
    {
        "model",
        "knowledge_base",
        "pcbdraft",
        "kicad",
        "environment",
        "human",
        "unclassified",
    }
)
RUN_STATUSES = frozenset(
    {
        "planned",
        "running",
        "completed",
        "failed",
        "timed_out",
        "interrupted",
        "configuration_drift",
    }
)
TERMINAL_RUN_STATUSES = frozenset(RUN_STATUSES - {"planned", "running"})
COST_STATUSES = frozenset({"actual", "estimated", "subscription_included", "unknown"})
TOKEN_STATUSES = frozenset({"reported", "derived", "partial", "unknown"})
TOKEN_SOURCES = frozenset(
    {"provider_usage", "trace_reduction", "provider_and_trace", "unavailable"}
)
TOOL_CALL_STATUSES = frozenset(
    {"completed", "failed", "interrupted", "denied", "unknown"}
)
AUTOMATIC_OUTCOMES = frozenset({"pass", "fail", "unknown"})
REPORT_SCOPES = frozenset({"overall", "category", "case"})
PHYSICAL_METRICS = (
    "fabricator_accepted",
    "solderability",
    "first_power_no_short",
    "power_rails",
    "firmware_download",
    "core_function",
)
NET_RULE_KINDS = frozenset(
    {"same_net", "different_net", "required_endpoint", "forbidden_endpoint"}
)
REFERENCE_REQUIREMENT_KINDS = frozenset(
    {
        "decoupling",
        "pull_up",
        "protection",
        "power_source",
        "power_return",
        "reset",
        "boot",
        "debug",
        "interface",
        "forbidden_part",
        "forbidden_connection",
        "forbidden_rating",
        "forbidden_unmatched_bom_component",
    }
)
FORBIDDEN_REQUIREMENT_KINDS = frozenset(
    {
        "forbidden_part",
        "forbidden_connection",
        "forbidden_rating",
        "forbidden_unmatched_bom_component",
    }
)
SUPPORT_SLOT_REQUIREMENT_KINDS = frozenset(
    {"decoupling", "pull_up", "protection", "reset", "boot"}
)
ENDPOINT_PAIR_REQUIREMENT_KINDS = frozenset({"power_source", "power_return"})
ENDPOINT_PAIRS_REQUIREMENT_KINDS = frozenset(
    {"debug", "interface", "forbidden_connection"}
)
RATING_QUANTITIES = frozenset({"voltage", "current", "power"})
RATING_UNITS = frozenset({"V", "A", "W"})
RATING_SOURCES = frozenset({"operating", "part_rating"})
MANUFACTURING_KINDS = frozenset(
    {
        "min_trace_width_mm",
        "min_clearance_mm",
        "min_drill_mm",
        "max_board_width_mm",
        "max_board_height_mm",
        "max_component_height_mm",
    }
)
REVIEW_OUTCOMES = frozenset(
    {
        "not_reviewed",
        "pass_without_schematic_change",
        "pass_after_changes",
        "fail",
        "not_applicable",
    }
)
REVIEW_CHECKLIST_KINDS = frozenset({"review_rubric", "assembly_constraint"})
REVIEW_CHECKLIST_DISPOSITIONS = frozenset(
    {"not_reviewed", "pass", "fail", "not_applicable"}
)
ORDERABILITY_EVIDENCE_STATUSES = frozenset({"orderable", "not_orderable", "unknown"})
ORDERABILITY_SOURCE_KINDS = frozenset(
    {"manufacturer", "authorized_distributor", "distributor", "other"}
)
CHANGE_TYPES = frozenset(
    {"functional", "presentation", "manufacturing", "firmware", "documentation"}
)
FINDING_CATEGORIES = frozenset(
    {
        "erc_drc_missed",
        "model_reasoning",
        "component_knowledge",
        "compiler",
        "layout",
        "router",
        "other",
    }
)
DIFF_AREAS = frozenset(
    {
        "components",
        "identities",
        "footprints",
        "nets",
        "power_rules",
        "board_geometry",
        "placement",
        "routes",
        "files",
    }
)
DIFF_OPERATIONS = frozenset({"added", "removed", "changed"})

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CANONICAL_PART_ID = re.compile(r"[a-z][a-z0-9._-]{0,127}")
_ENDPOINT_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_ENDPOINT = re.compile(
    r"(?P<slot>[A-Za-z0-9][A-Za-z0-9_-]{0,127})\."
    r"(?P<pin>[A-Za-z0-9][A-Za-z0-9_-]{0,127})"
)
_RATING_FACT_KEY = re.compile(r"[a-z][a-z0-9_]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{7,64}")
_PCB_TOOL_NAME = re.compile(r"pcb_[a-z0-9_]{1,123}")
_EVIDENCE_UNKNOWN = "unknown"
_EVIDENCE_UNAVAILABLE = "unavailable"
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
_ARTIFACT_SCHEMAS = frozenset(
    {
        CORPUS_SCHEMA,
        CAMPAIGN_SCHEMA,
        RUN_SCHEMA,
        SCORE_SCHEMA,
        REVIEW_SCHEMA,
        CORRECTION_SCHEMA,
        HARDWARE_SCHEMA,
        SELECTION_SCHEMA,
        REPORT_SCHEMA,
    }
)


def _closed(value: object, fields: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"{path} has unexpected fields")
    return value


def _schema(
    value: object,
    *,
    schema: str,
    fields: set[str],
    path: str,
    version: int = ARTIFACT_VERSION,
) -> dict[str, Any]:
    item = _closed(value, {"schema", "version", *fields}, path)
    if (
        item["schema"] != schema
        or type(item["version"]) is not int
        or item["version"] != version
    ):
        raise ValidationError(f"unsupported {schema} schema/version at {path}")
    return item


def _text(
    value: object,
    path: str,
    *,
    limit: int = MAX_SHORT_TEXT_BYTES,
    empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{path} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{path} is invalid") from exc
    if (not empty and not value.strip()) or "\x00" in value or len(encoded) > limit:
        raise ValidationError(f"{path} is invalid")
    return value


def _optional_text(
    value: object, path: str, *, limit: int = MAX_SHORT_TEXT_BYTES
) -> str | None:
    return None if value is None else _text(value, path, limit=limit)


def _identity(value: object, path: str) -> str:
    text = _text(value, path, limit=128)
    if _IDENTITY.fullmatch(text) is None:
        raise ValidationError(f"{path} is not a path-safe identity")
    return text


def _slot_id(value: object, path: str) -> str:
    text = _text(value, path, limit=128)
    if _ENDPOINT_SEGMENT.fullmatch(text) is None:
        raise ValidationError(f"{path} is not a path-safe slot identity")
    return text


def _canonical_part_id(value: object, path: str) -> str:
    text = _text(value, path, limit=128)
    if _CANONICAL_PART_ID.fullmatch(text) is None:
        raise ValidationError(f"{path} is not a canonical part identity")
    return text


def _endpoint(value: object, path: str) -> tuple[str, str]:
    text = _text(value, path, limit=257)
    match = _ENDPOINT.fullmatch(text)
    if match is None:
        raise ValidationError(f"{path} must be a path-safe slot.pin endpoint")
    return match.group("slot"), match.group("pin")


def _rating_fact(value: object, path: str) -> str:
    text = _text(value, path, limit=128)
    if _RATING_FACT_KEY.fullmatch(text) is None:
        raise ValidationError(f"{path} must be an exact safe ratings fact key")
    return text


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValidationError(f"{path} must be a lowercase SHA-256")
    return value


def _timestamp(value: object, path: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValidationError(f"{path} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{path} must be a UTC timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != datetime.min.replace(tzinfo=UTC).utcoffset()
    ):
        raise ValidationError(f"{path} must be a UTC timestamp")
    return value


def _optional_timestamp(value: object, path: str) -> str | None:
    return None if value is None else _timestamp(value, path)


def _https_url(value: object, path: str) -> str:
    text = _text(value, path, limit=2_048)
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValidationError(f"{path} must be an attributed HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in text)
    ):
        raise ValidationError(f"{path} must be an attributed HTTPS URL")
    return text


def _integer(
    value: object, path: str, *, minimum: int = 0, maximum: int = 1_000_000_000
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValidationError(f"{path} must be an integer in [{minimum}, {maximum}]")
    return value


def _number(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{path} must be a finite number")
    if minimum is not None and result < minimum:
        raise ValidationError(f"{path} is below {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationError(f"{path} exceeds {maximum}")
    return result


def _optional_number(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    return _number(value, path, minimum=minimum, maximum=maximum)


def _choice(value: object, choices: Iterable[str], path: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValidationError(f"{path} is invalid")
    return value


T = TypeVar("T")


def _array(
    value: object,
    path: str,
    parser: Callable[[object, str], T],
    *,
    minimum: int = 0,
    maximum: int = MAX_ARRAY_ITEMS,
) -> tuple[T, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValidationError(f"{path} must contain {minimum}..{maximum} items")
    return tuple(parser(item, f"{path}[{index}]") for index, item in enumerate(value))


def _text_array(
    value: object,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_ARRAY_ITEMS,
    choices: Iterable[str] | None = None,
) -> tuple[str, ...]:
    items = _array(
        value,
        path,
        lambda item, item_path: _text(item, item_path),
        minimum=minimum,
        maximum=maximum,
    )
    if len(set(items)) != len(items):
        raise ValidationError(f"{path} contains duplicates")
    allowed = frozenset(choices) if choices is not None else None
    if allowed is not None and not set(items) <= allowed:
        raise ValidationError(f"{path} contains an unsupported value")
    return items


def _unique(items: Iterable[T], key: Callable[[T], str], path: str) -> None:
    values = [key(item) for item in items]
    if len(values) != len(set(values)):
        raise ValidationError(f"{path} contains duplicate identities")


def _relative_path(value: object, path: str) -> str:
    text = _text(value, path, limit=1_024)
    candidate = PurePosixPath(text)
    windows_candidate = PureWindowsPath(text)
    if (
        candidate.is_absolute()
        or bool(windows_candidate.drive)
        or text != candidate.as_posix()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "\\" in text
    ):
        raise ValidationError(f"{path} must be a normalized relative POSIX path")
    return text


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return deterministic JSON bytes suitable for artifact hashing."""
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("BoardBench artifact contains a non-JSON value") from exc


def artifact_sha256(artifact: BoardBenchArtifact | Mapping[str, Any]) -> str:
    """Hash one canonical artifact document, independent of file formatting."""
    value = artifact.to_dict() if hasattr(artifact, "to_dict") else artifact
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class ComponentAlternative:
    part_id: str
    symbol: str
    footprints: tuple[str, ...]

    def __post_init__(self) -> None:
        _canonical_part_id(self.part_id, "component alternative part_id")
        _text(self.symbol, "component alternative symbol", limit=512)
        if not 1 <= len(self.footprints) <= 16 or len(set(self.footprints)) != len(
            self.footprints
        ):
            raise ValidationError("component alternative footprints are invalid")
        for footprint in self.footprints:
            _text(footprint, "component alternative footprint", limit=512)

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_id": self.part_id,
            "symbol": self.symbol,
            "footprints": list(self.footprints),
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"part_id", "symbol", "footprints"}, path)
        return cls(
            part_id=_canonical_part_id(item["part_id"], f"{path}.part_id"),
            symbol=_text(item["symbol"], f"{path}.symbol", limit=512),
            footprints=_text_array(
                item["footprints"], f"{path}.footprints", minimum=1, maximum=16
            ),
        )


@dataclass(frozen=True)
class ComponentSlot:
    id: str
    alternatives: tuple[ComponentAlternative, ...]

    def __post_init__(self) -> None:
        _slot_id(self.id, "component slot id")
        if not 1 <= len(self.alternatives) <= 16:
            raise ValidationError("component slot alternatives are invalid")
        _unique(self.alternatives, lambda item: item.part_id, "component alternatives")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "alternatives": [item.to_dict() for item in self.alternatives],
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"id", "alternatives"}, path)
        return cls(
            id=_slot_id(item["id"], f"{path}.id"),
            alternatives=_array(
                item["alternatives"],
                f"{path}.alternatives",
                ComponentAlternative.from_dict,
                minimum=1,
                maximum=16,
            ),
        )


@dataclass(frozen=True)
class NetRule:
    id: str
    kind: str
    endpoints: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        _identity(self.id, "net rule id")
        _choice(self.kind, NET_RULE_KINDS, "net rule kind")
        if not 1 <= len(self.endpoints) <= 16 or len(set(self.endpoints)) != len(
            self.endpoints
        ):
            raise ValidationError("net rule endpoints are invalid")
        for endpoint in self.endpoints:
            _endpoint(endpoint, "net rule endpoint")
        if self.kind in {"same_net", "different_net"} and len(self.endpoints) < 2:
            raise ValidationError(
                "same/different-net rules need at least two endpoints"
            )
        if (
            self.kind in {"required_endpoint", "forbidden_endpoint"}
            and len(self.endpoints) != 1
        ):
            raise ValidationError(
                "required/forbidden-endpoint rules need exactly one endpoint"
            )
        _text(self.description, "net rule description")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "endpoints": list(self.endpoints),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"id", "kind", "endpoints", "description"}, path)
        return cls(
            id=_identity(item["id"], f"{path}.id"),
            kind=_choice(item["kind"], NET_RULE_KINDS, f"{path}.kind"),
            endpoints=_array(
                item["endpoints"],
                f"{path}.endpoints",
                lambda entry, entry_path: _text(entry, entry_path, limit=257),
                minimum=1,
                maximum=16,
            ),
            description=_text(item["description"], f"{path}.description"),
        )


@dataclass(frozen=True)
class ReferenceRequirement:
    """One closed evaluator predicate with kind-defined positional subjects.

    Endpoint pairs are consecutive entries and use same-net evaluation (which a
    ``forbidden_connection`` negates). Descriptions are reviewer-facing context
    only; evaluators must use ``kind`` and ``subjects``.
    """

    id: str
    kind: str
    subjects: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        _identity(self.id, "reference requirement id")
        _choice(self.kind, REFERENCE_REQUIREMENT_KINDS, "reference requirement kind")
        if not 1 <= len(self.subjects) <= 32 or len(set(self.subjects)) != len(
            self.subjects
        ):
            raise ValidationError("reference requirement subjects are invalid")
        if self.kind in SUPPORT_SLOT_REQUIREMENT_KINDS:
            if len(self.subjects) != 3:
                raise ValidationError(
                    f"{self.kind} requirement needs target endpoint, support slot, "
                    "and reference endpoint"
                )
            _endpoint(self.subjects[0], "reference requirement target endpoint")
            _slot_id(self.subjects[1], "reference requirement support slot")
            _endpoint(self.subjects[2], "reference requirement reference endpoint")
        elif self.kind in ENDPOINT_PAIR_REQUIREMENT_KINDS:
            if len(self.subjects) != 2:
                raise ValidationError(
                    f"{self.kind} requirement needs one endpoint pair"
                )
            for subject in self.subjects:
                _endpoint(subject, "reference requirement endpoint")
        elif self.kind in ENDPOINT_PAIRS_REQUIREMENT_KINDS:
            if len(self.subjects) % 2:
                raise ValidationError(
                    f"{self.kind} requirement needs one or more endpoint pairs"
                )
            for subject in self.subjects:
                _endpoint(subject, "reference requirement endpoint")
        elif self.kind == "forbidden_part":
            for subject in self.subjects:
                _canonical_part_id(subject, "forbidden-part canonical part id")
        elif self.kind == "forbidden_unmatched_bom_component":
            for subject in self.subjects:
                _slot_id(subject, "allowed BOM component slot")
        elif self.kind == "forbidden_rating":
            for subject in self.subjects:
                _identity(subject, "forbidden-rating bound id")
        _text(self.description, "reference requirement description")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subjects": list(self.subjects),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"id", "kind", "subjects", "description"}, path)
        return cls(
            id=_identity(item["id"], f"{path}.id"),
            kind=_choice(item["kind"], REFERENCE_REQUIREMENT_KINDS, f"{path}.kind"),
            subjects=_text_array(
                item["subjects"], f"{path}.subjects", minimum=1, maximum=32
            ),
            description=_text(item["description"], f"{path}.description"),
        )


@dataclass(frozen=True)
class RatingBound:
    """A numeric bound over an operating endpoint or one exact part rating fact."""

    id: str
    source: str
    subject: str
    fact_key: str | None
    quantity: str
    unit: str
    minimum: float | None
    maximum: float | None
    description: str

    def __post_init__(self) -> None:
        _identity(self.id, "rating bound id")
        _choice(self.source, RATING_SOURCES, "rating bound source")
        if self.source == "operating":
            _endpoint(self.subject, "operating rating bound subject")
            if self.fact_key is not None:
                raise ValidationError(
                    "operating rating bound must have a null fact_key"
                )
        else:
            _slot_id(self.subject, "part-rating bound subject")
            if self.fact_key is None:
                raise ValidationError("part-rating bound needs a ratings fact_key")
            _rating_fact(self.fact_key, "part-rating bound fact_key")
        _choice(self.quantity, RATING_QUANTITIES, "rating bound quantity")
        _choice(self.unit, RATING_UNITS, "rating bound unit")
        expected_unit = {"voltage": "V", "current": "A", "power": "W"}[self.quantity]
        if self.unit != expected_unit:
            raise ValidationError("rating bound unit does not match its quantity")
        if self.minimum is None and self.maximum is None:
            raise ValidationError("rating bound needs a minimum or maximum")
        if self.minimum is not None:
            _number(self.minimum, "rating bound minimum")
        if self.maximum is not None:
            _number(self.maximum, "rating bound maximum")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValidationError("rating bound minimum exceeds maximum")
        _text(self.description, "rating bound description")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "subject": self.subject,
            "fact_key": self.fact_key,
            "quantity": self.quantity,
            "unit": self.unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "id",
                "source",
                "subject",
                "fact_key",
                "quantity",
                "unit",
                "minimum",
                "maximum",
                "description",
            },
            path,
        )
        source = _choice(item["source"], RATING_SOURCES, f"{path}.source")
        fact_key = item["fact_key"]
        return cls(
            id=_identity(item["id"], f"{path}.id"),
            source=source,
            subject=_text(item["subject"], f"{path}.subject", limit=257),
            fact_key=(
                None if fact_key is None else _rating_fact(fact_key, f"{path}.fact_key")
            ),
            quantity=_choice(item["quantity"], RATING_QUANTITIES, f"{path}.quantity"),
            unit=_choice(item["unit"], RATING_UNITS, f"{path}.unit"),
            minimum=_optional_number(item["minimum"], f"{path}.minimum"),
            maximum=_optional_number(item["maximum"], f"{path}.maximum"),
            description=_text(item["description"], f"{path}.description"),
        )


@dataclass(frozen=True)
class ManufacturingConstraint:
    id: str
    kind: str
    value_mm: float
    description: str

    def __post_init__(self) -> None:
        _identity(self.id, "manufacturing constraint id")
        _choice(self.kind, MANUFACTURING_KINDS, "manufacturing constraint kind")
        _number(self.value_mm, "manufacturing constraint value", minimum=0.0)
        if self.value_mm == 0.0:
            raise ValidationError("manufacturing constraint value must be positive")
        _text(self.description, "manufacturing constraint description")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "value_mm": self.value_mm,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"id", "kind", "value_mm", "description"}, path)
        return cls(
            id=_identity(item["id"], f"{path}.id"),
            kind=_choice(item["kind"], MANUFACTURING_KINDS, f"{path}.kind"),
            value_mm=_number(item["value_mm"], f"{path}.value_mm", minimum=0.0),
            description=_text(item["description"], f"{path}.description"),
        )


@dataclass(frozen=True)
class BoardBenchCase:
    id: str
    category: str
    prompt: str
    applicable_metrics: tuple[str, ...]
    review_rubric: tuple[str, ...]
    component_slots: tuple[ComponentSlot, ...]
    net_rules: tuple[NetRule, ...]
    support_requirements: tuple[ReferenceRequirement, ...]
    forbidden_conditions: tuple[ReferenceRequirement, ...]
    rating_bounds: tuple[RatingBound, ...]
    manufacturing_constraints: tuple[ManufacturingConstraint, ...]
    assembly_constraints: tuple[str, ...]

    def __post_init__(self) -> None:
        _identity(self.id, "BoardBench case id")
        _choice(self.category, BOARD_CATEGORIES, "BoardBench case category")
        _text(self.prompt, "BoardBench case prompt", limit=MAX_TEXT_BYTES)
        if (
            not self.applicable_metrics
            or not set(self.applicable_metrics) <= set(AUTOMATIC_METRICS)
            or len(set(self.applicable_metrics)) != len(self.applicable_metrics)
        ):
            raise ValidationError("BoardBench applicable metrics are invalid")
        if not 1 <= len(self.review_rubric) <= 128:
            raise ValidationError("BoardBench review rubric is invalid")
        for rubric in self.review_rubric:
            _text(rubric, "BoardBench review rubric item")
        if not 1 <= len(self.component_slots) <= 128:
            raise ValidationError("BoardBench component slots are invalid")
        if any(
            len(collection) > 256
            for collection in (
                self.net_rules,
                self.support_requirements,
                self.forbidden_conditions,
                self.rating_bounds,
                self.manufacturing_constraints,
            )
        ):
            raise ValidationError("BoardBench case reference collection is oversized")
        _unique(
            self.component_slots, lambda item: item.id, "BoardBench component slots"
        )
        _unique(self.net_rules, lambda item: item.id, "BoardBench net rules")
        _unique(
            self.support_requirements,
            lambda item: item.id,
            "BoardBench support requirements",
        )
        _unique(
            self.forbidden_conditions,
            lambda item: item.id,
            "BoardBench forbidden conditions",
        )
        if any(
            item.kind in FORBIDDEN_REQUIREMENT_KINDS
            for item in self.support_requirements
        ):
            raise ValidationError(
                "BoardBench support requirements cannot contain forbidden conditions"
            )
        if any(
            item.kind not in FORBIDDEN_REQUIREMENT_KINDS
            for item in self.forbidden_conditions
        ):
            raise ValidationError(
                "BoardBench forbidden conditions need a forbidden requirement kind"
            )
        if (
            any(
                item.kind == "forbidden_unmatched_bom_component"
                for item in self.forbidden_conditions
            )
            and "support_circuits" not in self.applicable_metrics
        ):
            raise ValidationError(
                "exact BOM cardinality requires the support_circuits metric"
            )
        _unique(self.rating_bounds, lambda item: item.id, "BoardBench rating bounds")
        _unique(
            self.manufacturing_constraints,
            lambda item: item.id,
            "BoardBench manufacturing constraints",
        )
        if not 1 <= len(self.assembly_constraints) <= 128 or len(
            set(self.assembly_constraints)
        ) != len(self.assembly_constraints):
            raise ValidationError("BoardBench assembly constraints are invalid")
        for constraint in self.assembly_constraints:
            _text(constraint, "BoardBench assembly constraint")

        slot_ids = {item.id for item in self.component_slots}
        rating_ids = {item.id for item in self.rating_bounds}

        def require_endpoint(endpoint: str, path: str) -> None:
            slot_id, _pin = _endpoint(endpoint, path)
            if slot_id not in slot_ids:
                raise ValidationError(f"{path} references unknown slot {slot_id}")

        for rule in self.net_rules:
            for index, endpoint in enumerate(rule.endpoints):
                require_endpoint(endpoint, f"net rule {rule.id} endpoints[{index}]")

        for requirement in (
            *self.support_requirements,
            *self.forbidden_conditions,
        ):
            if requirement.kind in SUPPORT_SLOT_REQUIREMENT_KINDS:
                require_endpoint(
                    requirement.subjects[0],
                    f"reference requirement {requirement.id} target endpoint",
                )
                support_slot = requirement.subjects[1]
                if support_slot not in slot_ids:
                    raise ValidationError(
                        f"reference requirement {requirement.id} references unknown "
                        f"support slot {support_slot}"
                    )
                require_endpoint(
                    requirement.subjects[2],
                    f"reference requirement {requirement.id} reference endpoint",
                )
            elif requirement.kind in (
                ENDPOINT_PAIR_REQUIREMENT_KINDS | ENDPOINT_PAIRS_REQUIREMENT_KINDS
            ):
                for index, endpoint in enumerate(requirement.subjects):
                    require_endpoint(
                        endpoint,
                        f"reference requirement {requirement.id} subjects[{index}]",
                    )
            elif requirement.kind == "forbidden_rating":
                unknown = set(requirement.subjects) - rating_ids
                if unknown:
                    raise ValidationError(
                        f"reference requirement {requirement.id} references unknown "
                        f"rating bound {min(unknown)}"
                    )
            elif requirement.kind == "forbidden_unmatched_bom_component":
                unknown = set(requirement.subjects) - slot_ids
                if unknown:
                    raise ValidationError(
                        f"reference requirement {requirement.id} references unknown "
                        f"allowed BOM component slot {min(unknown)}"
                    )

        for bound in self.rating_bounds:
            if bound.source == "operating":
                require_endpoint(
                    bound.subject, f"rating bound {bound.id} operating subject"
                )
            elif bound.subject not in slot_ids:
                raise ValidationError(
                    f"rating bound {bound.id} references unknown slot {bound.subject}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "prompt": self.prompt,
            "applicable_metrics": list(self.applicable_metrics),
            "review_rubric": list(self.review_rubric),
            "component_slots": [item.to_dict() for item in self.component_slots],
            "net_rules": [item.to_dict() for item in self.net_rules],
            "support_requirements": [
                item.to_dict() for item in self.support_requirements
            ],
            "forbidden_conditions": [
                item.to_dict() for item in self.forbidden_conditions
            ],
            "rating_bounds": [item.to_dict() for item in self.rating_bounds],
            "manufacturing_constraints": [
                item.to_dict() for item in self.manufacturing_constraints
            ],
            "assembly_constraints": list(self.assembly_constraints),
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        fields = {
            "id",
            "category",
            "prompt",
            "applicable_metrics",
            "review_rubric",
            "component_slots",
            "net_rules",
            "support_requirements",
            "forbidden_conditions",
            "rating_bounds",
            "manufacturing_constraints",
            "assembly_constraints",
        }
        item = _closed(value, fields, path)
        return cls(
            id=_identity(item["id"], f"{path}.id"),
            category=_choice(item["category"], BOARD_CATEGORIES, f"{path}.category"),
            prompt=_text(item["prompt"], f"{path}.prompt", limit=MAX_TEXT_BYTES),
            applicable_metrics=_text_array(
                item["applicable_metrics"],
                f"{path}.applicable_metrics",
                minimum=1,
                maximum=len(AUTOMATIC_METRICS),
                choices=AUTOMATIC_METRICS,
            ),
            review_rubric=_text_array(
                item["review_rubric"], f"{path}.review_rubric", minimum=1, maximum=128
            ),
            component_slots=_array(
                item["component_slots"],
                f"{path}.component_slots",
                ComponentSlot.from_dict,
                minimum=1,
                maximum=128,
            ),
            net_rules=_array(
                item["net_rules"],
                f"{path}.net_rules",
                NetRule.from_dict,
                maximum=256,
            ),
            support_requirements=_array(
                item["support_requirements"],
                f"{path}.support_requirements",
                ReferenceRequirement.from_dict,
                maximum=256,
            ),
            forbidden_conditions=_array(
                item["forbidden_conditions"],
                f"{path}.forbidden_conditions",
                ReferenceRequirement.from_dict,
                maximum=256,
            ),
            rating_bounds=_array(
                item["rating_bounds"],
                f"{path}.rating_bounds",
                RatingBound.from_dict,
                maximum=256,
            ),
            manufacturing_constraints=_array(
                item["manufacturing_constraints"],
                f"{path}.manufacturing_constraints",
                ManufacturingConstraint.from_dict,
                maximum=256,
            ),
            assembly_constraints=_text_array(
                item["assembly_constraints"],
                f"{path}.assembly_constraints",
                minimum=1,
                maximum=128,
            ),
        )


@dataclass(frozen=True)
class BoardBenchCorpus:
    schema: ClassVar[str] = CORPUS_SCHEMA

    corpus_id: str
    corpus_version: int
    license: str
    methodology: str
    cohort: str
    cases: tuple[BoardBenchCase, ...]

    def __post_init__(self) -> None:
        _identity(self.corpus_id, "BoardBench corpus id")
        _integer(self.corpus_version, "BoardBench corpus version", minimum=1)
        if self.license != "CC0-1.0":
            raise ValidationError("BoardBench corpus license must be CC0-1.0")
        _text(self.methodology, "BoardBench methodology", limit=MAX_TEXT_BYTES)
        _choice(self.cohort, COHORTS, "BoardBench cohort")
        if len(self.cases) != 20:
            raise ValidationError("BoardBench corpus must contain exactly 20 cases")
        _unique(self.cases, lambda item: item.id, "BoardBench cases")
        counts = Counter(case.category for case in self.cases)
        if counts != Counter({category: 4 for category in BOARD_CATEGORIES}):
            raise ValidationError(
                "BoardBench corpus must contain four cases per category"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": ARTIFACT_VERSION,
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "license": self.license,
            "methodology": self.methodology,
            "cohort": self.cohort,
            "cases": [case.to_dict() for case in self.cases],
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        item = _schema(
            value,
            schema=cls.schema,
            fields={
                "corpus_id",
                "corpus_version",
                "license",
                "methodology",
                "cohort",
                "cases",
            },
            path=path,
        )
        return cls(
            corpus_id=_identity(item["corpus_id"], f"{path}.corpus_id"),
            corpus_version=_integer(
                item["corpus_version"], f"{path}.corpus_version", minimum=1
            ),
            license=_text(item["license"], f"{path}.license", limit=64),
            methodology=_text(
                item["methodology"], f"{path}.methodology", limit=MAX_TEXT_BYTES
            ),
            cohort=_choice(item["cohort"], COHORTS, f"{path}.cohort"),
            cases=_array(
                item["cases"],
                f"{path}.cases",
                BoardBenchCase.from_dict,
                minimum=20,
                maximum=20,
            ),
        )


@dataclass(frozen=True)
class CampaignRunPlan:
    case_id: str
    repetition: int
    run_id: str

    def __post_init__(self) -> None:
        _identity(self.case_id, "campaign case id")
        _integer(self.repetition, "campaign repetition", minimum=1, maximum=3)
        _identity(self.run_id, "campaign run id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "repetition": self.repetition,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"case_id", "repetition", "run_id"}, path)
        return cls(
            case_id=_identity(item["case_id"], f"{path}.case_id"),
            repetition=_integer(
                item["repetition"], f"{path}.repetition", minimum=1, maximum=3
            ),
            run_id=_identity(item["run_id"], f"{path}.run_id"),
        )


@dataclass(frozen=True)
class BoardBenchCampaign:
    schema: ClassVar[str] = CAMPAIGN_SCHEMA

    campaign_id: str
    cohort: str
    corpus_id: str
    corpus_sha256: str
    created_at: str
    pcbdraft_commit: str
    dirty_state_sha256: str
    provider: str
    model: str
    configuration_sha256: str
    kicad_version: str
    python_version: str
    platform: str
    tool_registry_sha256: str
    wall_timeout_seconds: float
    tool_call_budget: int
    repetitions: int
    evaluator_version: str
    runs: tuple[CampaignRunPlan, ...]

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "campaign id")
        _choice(self.cohort, COHORTS, "campaign cohort")
        _identity(self.corpus_id, "campaign corpus id")
        _sha256(self.corpus_sha256, "campaign corpus hash")
        _timestamp(self.created_at, "campaign created_at")
        if _COMMIT.fullmatch(self.pcbdraft_commit) is None:
            raise ValidationError("campaign PCBDraft commit is invalid")
        for value, label in (
            (self.dirty_state_sha256, "dirty state hash"),
            (self.configuration_sha256, "configuration hash"),
            (self.tool_registry_sha256, "tool registry hash"),
        ):
            _sha256(value, f"campaign {label}")
        for value, label in (
            (self.provider, "provider"),
            (self.model, "model"),
            (self.kicad_version, "KiCad version"),
            (self.python_version, "Python version"),
            (self.platform, "platform"),
            (self.evaluator_version, "evaluator version"),
        ):
            _text(value, f"campaign {label}", limit=512)
        _number(
            self.wall_timeout_seconds,
            "campaign wall timeout",
            minimum=1.0,
            maximum=86_400.0,
        )
        _integer(
            self.tool_call_budget,
            "campaign tool-call budget",
            minimum=1,
            maximum=100_000,
        )
        if self.repetitions != 3 or len(self.runs) != 60:
            raise ValidationError("BoardBench campaign must plan exactly 20 x 3 runs")
        _unique(self.runs, lambda item: item.run_id, "campaign run ids")
        combinations = {(item.case_id, item.repetition) for item in self.runs}
        if len(combinations) != 60:
            raise ValidationError("campaign case/repetition pairs must be unique")
        cases: dict[str, set[int]] = {}
        for run in self.runs:
            cases.setdefault(run.case_id, set()).add(run.repetition)
        if len(cases) != 20 or any(values != {1, 2, 3} for values in cases.values()):
            raise ValidationError("campaign must contain repetitions 1..3 for 20 cases")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": ARTIFACT_VERSION,
            "campaign_id": self.campaign_id,
            "cohort": self.cohort,
            "corpus_id": self.corpus_id,
            "corpus_sha256": self.corpus_sha256,
            "created_at": self.created_at,
            "pcbdraft_commit": self.pcbdraft_commit,
            "dirty_state_sha256": self.dirty_state_sha256,
            "provider": self.provider,
            "model": self.model,
            "configuration_sha256": self.configuration_sha256,
            "kicad_version": self.kicad_version,
            "python_version": self.python_version,
            "platform": self.platform,
            "tool_registry_sha256": self.tool_registry_sha256,
            "wall_timeout_seconds": self.wall_timeout_seconds,
            "tool_call_budget": self.tool_call_budget,
            "repetitions": self.repetitions,
            "evaluator_version": self.evaluator_version,
            "runs": [run.to_dict() for run in self.runs],
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "campaign_id",
            "cohort",
            "corpus_id",
            "corpus_sha256",
            "created_at",
            "pcbdraft_commit",
            "dirty_state_sha256",
            "provider",
            "model",
            "configuration_sha256",
            "kicad_version",
            "python_version",
            "platform",
            "tool_registry_sha256",
            "wall_timeout_seconds",
            "tool_call_budget",
            "repetitions",
            "evaluator_version",
            "runs",
        }
        item = _schema(value, schema=cls.schema, fields=fields, path=path)
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            cohort=_choice(item["cohort"], COHORTS, f"{path}.cohort"),
            corpus_id=_identity(item["corpus_id"], f"{path}.corpus_id"),
            corpus_sha256=_sha256(item["corpus_sha256"], f"{path}.corpus_sha256"),
            created_at=_timestamp(item["created_at"], f"{path}.created_at"),
            pcbdraft_commit=_text(
                item["pcbdraft_commit"], f"{path}.pcbdraft_commit", limit=64
            ),
            dirty_state_sha256=_sha256(
                item["dirty_state_sha256"], f"{path}.dirty_state_sha256"
            ),
            provider=_text(item["provider"], f"{path}.provider", limit=512),
            model=_text(item["model"], f"{path}.model", limit=512),
            configuration_sha256=_sha256(
                item["configuration_sha256"], f"{path}.configuration_sha256"
            ),
            kicad_version=_text(
                item["kicad_version"], f"{path}.kicad_version", limit=512
            ),
            python_version=_text(
                item["python_version"], f"{path}.python_version", limit=512
            ),
            platform=_text(item["platform"], f"{path}.platform", limit=512),
            tool_registry_sha256=_sha256(
                item["tool_registry_sha256"], f"{path}.tool_registry_sha256"
            ),
            wall_timeout_seconds=_number(
                item["wall_timeout_seconds"],
                f"{path}.wall_timeout_seconds",
                minimum=1.0,
                maximum=86_400.0,
            ),
            tool_call_budget=_integer(
                item["tool_call_budget"],
                f"{path}.tool_call_budget",
                minimum=1,
                maximum=100_000,
            ),
            repetitions=_integer(
                item["repetitions"], f"{path}.repetitions", minimum=3, maximum=3
            ),
            evaluator_version=_text(
                item["evaluator_version"], f"{path}.evaluator_version", limit=512
            ),
            runs=_array(
                item["runs"],
                f"{path}.runs",
                CampaignRunPlan.from_dict,
                minimum=60,
                maximum=60,
            ),
        )


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _relative_path(self.path, "inventory path")
        _integer(
            self.size_bytes,
            "inventory size",
            maximum=MAX_INVENTORY_FILE_BYTES,
        )
        _sha256(self.sha256, "inventory hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"path", "size_bytes", "sha256"}, path)
        return cls(
            path=_relative_path(item["path"], f"{path}.path"),
            size_bytes=_integer(
                item["size_bytes"],
                f"{path}.size_bytes",
                maximum=MAX_INVENTORY_FILE_BYTES,
            ),
            sha256=_sha256(item["sha256"], f"{path}.sha256"),
        )


@dataclass(frozen=True)
class BoardBenchRun:
    schema: ClassVar[str] = RUN_SCHEMA

    campaign_id: str
    run_id: str
    case_id: str
    repetition: int
    prompt_sha256: str
    status: str
    started_at: str | None
    completed_at: str | None
    termination_reason: str | None
    final_response: str | None
    inventory: tuple[InventoryEntry, ...]

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "run campaign id")
        _identity(self.run_id, "run id")
        _identity(self.case_id, "run case id")
        _integer(self.repetition, "run repetition", minimum=1, maximum=3)
        _sha256(self.prompt_sha256, "run prompt hash")
        _choice(self.status, RUN_STATUSES, "run status")
        _optional_timestamp(self.started_at, "run started_at")
        _optional_timestamp(self.completed_at, "run completed_at")
        _optional_text(self.termination_reason, "run termination reason")
        _optional_text(self.final_response, "run final response", limit=MAX_TEXT_BYTES)
        if len(self.inventory) > MAX_INVENTORY_FILES:
            raise ValidationError("run inventory is oversized")
        _unique(self.inventory, lambda item: item.path, "run inventory")
        if sum(item.size_bytes for item in self.inventory) > MAX_INVENTORY_BYTES:
            raise ValidationError("run inventory exceeds the total size limit")
        if self.status == "planned":
            if (
                any(
                    value is not None
                    for value in (
                        self.started_at,
                        self.completed_at,
                        self.termination_reason,
                        self.final_response,
                    )
                )
                or self.inventory
            ):
                raise ValidationError("planned run already contains execution evidence")
        elif self.status == "running":
            if (
                self.started_at is None
                or self.completed_at is not None
                or self.termination_reason is not None
                or self.final_response is not None
            ):
                raise ValidationError("running run timestamps are malformed")
        elif (
            self.started_at is None
            or self.completed_at is None
            or self.termination_reason is None
        ):
            raise ValidationError("terminal run needs timestamps and a reason")
        elif not self.inventory:
            raise ValidationError("terminal run needs retained artifact inventory")
        if self.status == "completed" and self.final_response is None:
            raise ValidationError("completed run needs the final Agent response")
        if self.started_at is not None and self.completed_at is not None:
            started = datetime.fromisoformat(self.started_at)
            completed = datetime.fromisoformat(self.completed_at)
            if completed < started:
                raise ValidationError("run completed_at precedes started_at")

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": ARTIFACT_VERSION,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "repetition": self.repetition,
            "prompt_sha256": self.prompt_sha256,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "termination_reason": self.termination_reason,
            "final_response": self.final_response,
            "inventory": [item.to_dict() for item in self.inventory],
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        item = _schema(
            value,
            schema=cls.schema,
            fields={
                "campaign_id",
                "run_id",
                "case_id",
                "repetition",
                "prompt_sha256",
                "status",
                "started_at",
                "completed_at",
                "termination_reason",
                "final_response",
                "inventory",
            },
            path=path,
        )
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            run_id=_identity(item["run_id"], f"{path}.run_id"),
            case_id=_identity(item["case_id"], f"{path}.case_id"),
            repetition=_integer(
                item["repetition"], f"{path}.repetition", minimum=1, maximum=3
            ),
            prompt_sha256=_sha256(item["prompt_sha256"], f"{path}.prompt_sha256"),
            status=_choice(item["status"], RUN_STATUSES, f"{path}.status"),
            started_at=_optional_timestamp(item["started_at"], f"{path}.started_at"),
            completed_at=_optional_timestamp(
                item["completed_at"], f"{path}.completed_at"
            ),
            termination_reason=_optional_text(
                item["termination_reason"], f"{path}.termination_reason"
            ),
            final_response=_optional_text(
                item["final_response"], f"{path}.final_response", limit=MAX_TEXT_BYTES
            ),
            inventory=_array(
                item["inventory"],
                f"{path}.inventory",
                InventoryEntry.from_dict,
                maximum=MAX_INVENTORY_FILES,
            ),
        )


@dataclass(frozen=True)
class MetricResult:
    name: str
    state: str
    reason: str

    def __post_init__(self) -> None:
        _choice(self.name, AUTOMATIC_METRICS, "metric name")
        _choice(self.state, METRIC_STATES, "metric state")
        _text(self.reason, "metric reason")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "reason": self.reason}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"name", "state", "reason"}, path)
        return cls(
            name=_choice(item["name"], AUTOMATIC_METRICS, f"{path}.name"),
            state=_choice(item["state"], METRIC_STATES, f"{path}.state"),
            reason=_text(item["reason"], f"{path}.reason"),
        )


@dataclass(frozen=True)
class FailureClassification:
    stage: str
    causes: tuple[str, ...]
    owners: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _choice(self.stage, FAILURE_STAGE_VALUES, "failure stage")
        if (
            not 1 <= len(self.causes) <= len(FAILURE_CAUSES)
            or len(set(self.causes)) != len(self.causes)
            or not set(self.causes) <= FAILURE_CAUSES
        ):
            raise ValidationError("failure causes are invalid")
        if (
            not 1 <= len(self.owners) <= len(FAILURE_OWNERS)
            or len(set(self.owners)) != len(self.owners)
            or not set(self.owners) <= FAILURE_OWNERS
        ):
            raise ValidationError("failure owners are invalid")
        if "unclassified" in self.causes and len(self.causes) != 1:
            raise ValidationError("unclassified failure cause must stand alone")
        if "unclassified" in self.owners and len(self.owners) != 1:
            raise ValidationError("unclassified failure owner must stand alone")
        _text(self.reason, "failure classification reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "causes": list(self.causes),
            "owners": list(self.owners),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"stage", "causes", "owners", "reason"}, path)
        return cls(
            stage=_choice(item["stage"], FAILURE_STAGE_VALUES, f"{path}.stage"),
            causes=_text_array(
                item["causes"],
                f"{path}.causes",
                minimum=1,
                maximum=len(FAILURE_CAUSES),
                choices=FAILURE_CAUSES,
            ),
            owners=_text_array(
                item["owners"],
                f"{path}.owners",
                minimum=1,
                maximum=len(FAILURE_OWNERS),
                choices=FAILURE_OWNERS,
            ),
            reason=_text(item["reason"], f"{path}.reason"),
        )


@dataclass(frozen=True)
class ToolCallCount:
    name: str
    status: str
    count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _PCB_TOOL_NAME.fullmatch(self.name) is None
        ):
            raise ValidationError("PCB tool-call name is invalid")
        _choice(self.status, TOOL_CALL_STATUSES, "PCB tool-call status")
        _integer(self.count, "PCB tool-call count", minimum=1, maximum=1_000_000)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "count": self.count}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"name", "status", "count"}, path)
        name = _text(item["name"], f"{path}.name", limit=128)
        if _PCB_TOOL_NAME.fullmatch(name) is None:
            raise ValidationError(f"{path}.name is not a PCB tool name")
        return cls(
            name=name,
            status=_choice(item["status"], TOOL_CALL_STATUSES, f"{path}.status"),
            count=_integer(
                item["count"], f"{path}.count", minimum=1, maximum=1_000_000
            ),
        )


@dataclass(frozen=True)
class EfficiencyMetrics:
    model_requests: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    token_status: str
    token_source: str
    cost_amount: float | None
    cost_currency: str | None
    cost_status: str
    cost_source: str
    pcb_tool_calls: int | None
    tool_call_counts: tuple[ToolCallCount, ...]
    provider_retries: int | None
    provider_errors: int | None
    tool_seconds: float | None
    api_seconds: float | None
    wall_seconds: float
    failure_reason: str | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.model_requests, "model requests"),
            (self.input_tokens, "input tokens"),
            (self.output_tokens, "output tokens"),
            (self.cache_read_tokens, "cache-read tokens"),
            (self.cache_write_tokens, "cache-write tokens"),
            (self.reasoning_tokens, "reasoning tokens"),
            (self.total_tokens, "total tokens"),
            (self.pcb_tool_calls, "PCB tool calls"),
            (self.provider_retries, "provider retries"),
            (self.provider_errors, "provider errors"),
        ):
            if value is not None:
                _integer(value, f"efficiency {label}")
        _choice(self.token_status, TOKEN_STATUSES, "efficiency token status")
        _choice(self.token_source, TOKEN_SOURCES, "efficiency token source")
        token_values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
            self.total_tokens,
        )
        if self.token_status == _EVIDENCE_UNKNOWN:
            if any(value is not None for value in token_values):
                raise ValidationError(
                    "unknown token evidence cannot contain token counts"
                )
            if self.token_source != _EVIDENCE_UNAVAILABLE:
                raise ValidationError(
                    "unknown token evidence source must be unavailable"
                )
        elif self.token_status in {"reported", "derived"}:
            if any(value is None for value in token_values):
                raise ValidationError("complete token evidence needs all token counts")
            if self.token_source == _EVIDENCE_UNAVAILABLE:
                raise ValidationError(
                    "complete token evidence needs an available source"
                )
        else:
            if not any(value is not None for value in token_values):
                raise ValidationError(
                    "partial token evidence needs at least one token count"
                )
            if all(value is not None for value in token_values):
                raise ValidationError(
                    "partial token evidence must have at least one missing token count"
                )
            if self.token_source == _EVIDENCE_UNAVAILABLE:
                raise ValidationError(
                    "partial token evidence needs an available source"
                )
        if self.cost_amount is not None:
            _number(self.cost_amount, "efficiency cost amount", minimum=0.0)
        if self.cost_currency is not None:
            _text(self.cost_currency, "efficiency cost currency", limit=16)
        _choice(self.cost_status, COST_STATUSES, "efficiency cost status")
        _text(self.cost_source, "efficiency cost source", limit=512)
        if self.cost_status in {"actual", "estimated"}:
            if self.cost_amount is None or self.cost_currency is None:
                raise ValidationError(
                    "priced cost needs an amount, currency, and source"
                )
        elif self.cost_amount is not None or self.cost_currency is not None:
            raise ValidationError(
                "unpriced cost status cannot contain an amount/currency"
            )
        if len(self.tool_call_counts) > 512:
            raise ValidationError("PCB tool-call breakdown is oversized")
        pairs = [(item.name, item.status) for item in self.tool_call_counts]
        if len(pairs) != len(set(pairs)):
            raise ValidationError("PCB tool-call breakdown contains duplicate pairs")
        breakdown_total = sum(item.count for item in self.tool_call_counts)
        if self.pcb_tool_calls is None:
            if self.tool_call_counts:
                raise ValidationError(
                    "unknown PCB tool-call total cannot have a breakdown"
                )
        elif breakdown_total != self.pcb_tool_calls:
            raise ValidationError("PCB tool-call breakdown does not match its total")
        for seconds_value, label in (
            (self.tool_seconds, "tool seconds"),
            (self.api_seconds, "API seconds"),
        ):
            if seconds_value is not None:
                _number(seconds_value, f"efficiency {label}", minimum=0.0)
        _number(self.wall_seconds, "efficiency wall seconds", minimum=0.0)
        _optional_text(self.failure_reason, "efficiency failure reason")
        if all(value is not None for value in token_values) and (
            sum(
                value
                for value in (
                    self.input_tokens,
                    self.cache_read_tokens,
                    self.cache_write_tokens,
                    self.output_tokens,
                )
                if value is not None
            )
            != self.total_tokens
        ):
            raise ValidationError("efficiency token totals are inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_requests": self.model_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "token_status": self.token_status,
            "token_source": self.token_source,
            "cost_amount": self.cost_amount,
            "cost_currency": self.cost_currency,
            "cost_status": self.cost_status,
            "cost_source": self.cost_source,
            "pcb_tool_calls": self.pcb_tool_calls,
            "tool_call_counts": [item.to_dict() for item in self.tool_call_counts],
            "provider_retries": self.provider_retries,
            "provider_errors": self.provider_errors,
            "tool_seconds": self.tool_seconds,
            "api_seconds": self.api_seconds,
            "wall_seconds": self.wall_seconds,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        fields = {
            "model_requests",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
            "token_status",
            "token_source",
            "cost_amount",
            "cost_currency",
            "cost_status",
            "cost_source",
            "pcb_tool_calls",
            "tool_call_counts",
            "provider_retries",
            "provider_errors",
            "tool_seconds",
            "api_seconds",
            "wall_seconds",
            "failure_reason",
        }
        item = _closed(value, fields, path)

        def optional_integer(name: str) -> int | None:
            raw = item[name]
            return None if raw is None else _integer(raw, f"{path}.{name}")

        return cls(
            model_requests=optional_integer("model_requests"),
            input_tokens=optional_integer("input_tokens"),
            output_tokens=optional_integer("output_tokens"),
            cache_read_tokens=optional_integer("cache_read_tokens"),
            cache_write_tokens=optional_integer("cache_write_tokens"),
            reasoning_tokens=optional_integer("reasoning_tokens"),
            total_tokens=optional_integer("total_tokens"),
            token_status=_choice(
                item["token_status"], TOKEN_STATUSES, f"{path}.token_status"
            ),
            token_source=_choice(
                item["token_source"], TOKEN_SOURCES, f"{path}.token_source"
            ),
            cost_amount=_optional_number(
                item["cost_amount"], f"{path}.cost_amount", minimum=0.0
            ),
            cost_currency=_optional_text(
                item["cost_currency"], f"{path}.cost_currency", limit=16
            ),
            cost_status=_choice(
                item["cost_status"], COST_STATUSES, f"{path}.cost_status"
            ),
            cost_source=_text(item["cost_source"], f"{path}.cost_source", limit=512),
            pcb_tool_calls=optional_integer("pcb_tool_calls"),
            tool_call_counts=_array(
                item["tool_call_counts"],
                f"{path}.tool_call_counts",
                ToolCallCount.from_dict,
                maximum=512,
            ),
            provider_retries=optional_integer("provider_retries"),
            provider_errors=optional_integer("provider_errors"),
            tool_seconds=_optional_number(
                item["tool_seconds"], f"{path}.tool_seconds", minimum=0.0
            ),
            api_seconds=_optional_number(
                item["api_seconds"], f"{path}.api_seconds", minimum=0.0
            ),
            wall_seconds=_number(
                item["wall_seconds"], f"{path}.wall_seconds", minimum=0.0
            ),
            failure_reason=_optional_text(
                item["failure_reason"], f"{path}.failure_reason"
            ),
        )


@dataclass(frozen=True)
class BoardBenchScore:
    schema: ClassVar[str] = SCORE_SCHEMA

    campaign_id: str
    run_id: str
    source_campaign_sha256: str
    source_case_sha256: str
    source_run_sha256: str
    evaluator_version: str
    scored_at: str
    overall_state: str
    metrics: tuple[MetricResult, ...]
    efficiency: EfficiencyMetrics
    failure_suggestion: FailureClassification | None

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "score campaign id")
        _identity(self.run_id, "score run id")
        _sha256(self.source_campaign_sha256, "score source campaign hash")
        _sha256(self.source_case_sha256, "score source case hash")
        _sha256(self.source_run_sha256, "score source run hash")
        _text(self.evaluator_version, "score evaluator version", limit=512)
        _timestamp(self.scored_at, "score scored_at")
        _choice(self.overall_state, {"pass", "fail", "unknown"}, "score overall state")
        if len(self.metrics) != len(AUTOMATIC_METRICS):
            raise ValidationError("score must contain every automatic metric")
        _unique(self.metrics, lambda item: item.name, "score metrics")
        if {item.name for item in self.metrics} != set(AUTOMATIC_METRICS):
            raise ValidationError("score metric set is incomplete")
        states = {item.state for item in self.metrics if item.state != "not_applicable"}
        if not states:
            raise ValidationError(
                "score cannot mark every automatic metric not applicable"
            )
        expected = (
            "fail" if "fail" in states else "unknown" if "unknown" in states else "pass"
        )
        if self.overall_state != expected:
            raise ValidationError("score overall state does not match metric states")
        if self.overall_state == "pass" and self.failure_suggestion is not None:
            raise ValidationError("passing score cannot suggest a failure")
        if self.overall_state != "pass" and self.failure_suggestion is None:
            raise ValidationError("non-passing score needs a failure suggestion")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": ARTIFACT_VERSION,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "source_campaign_sha256": self.source_campaign_sha256,
            "source_case_sha256": self.source_case_sha256,
            "source_run_sha256": self.source_run_sha256,
            "evaluator_version": self.evaluator_version,
            "scored_at": self.scored_at,
            "overall_state": self.overall_state,
            "metrics": [item.to_dict() for item in self.metrics],
            "efficiency": self.efficiency.to_dict(),
            "failure_suggestion": (
                self.failure_suggestion.to_dict()
                if self.failure_suggestion is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        item = _schema(
            value,
            schema=cls.schema,
            fields={
                "campaign_id",
                "run_id",
                "source_campaign_sha256",
                "source_case_sha256",
                "source_run_sha256",
                "evaluator_version",
                "scored_at",
                "overall_state",
                "metrics",
                "efficiency",
                "failure_suggestion",
            },
            path=path,
        )
        suggestion = item["failure_suggestion"]
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            run_id=_identity(item["run_id"], f"{path}.run_id"),
            source_campaign_sha256=_sha256(
                item["source_campaign_sha256"],
                f"{path}.source_campaign_sha256",
            ),
            source_case_sha256=_sha256(
                item["source_case_sha256"], f"{path}.source_case_sha256"
            ),
            source_run_sha256=_sha256(
                item["source_run_sha256"], f"{path}.source_run_sha256"
            ),
            evaluator_version=_text(
                item["evaluator_version"], f"{path}.evaluator_version", limit=512
            ),
            scored_at=_timestamp(item["scored_at"], f"{path}.scored_at"),
            overall_state=_choice(
                item["overall_state"],
                {"pass", "fail", "unknown"},
                f"{path}.overall_state",
            ),
            metrics=_array(
                item["metrics"],
                f"{path}.metrics",
                MetricResult.from_dict,
                minimum=len(AUTOMATIC_METRICS),
                maximum=len(AUTOMATIC_METRICS),
            ),
            efficiency=EfficiencyMetrics.from_dict(
                item["efficiency"], f"{path}.efficiency"
            ),
            failure_suggestion=(
                None
                if suggestion is None
                else FailureClassification.from_dict(
                    suggestion, f"{path}.failure_suggestion"
                )
            ),
        )


@dataclass(frozen=True)
class ModificationDecision:
    id: str
    change_type: str
    description: str
    finding_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identity(self.id, "modification decision id")
        _choice(self.change_type, CHANGE_TYPES, "modification decision type")
        _text(self.description, "modification decision description")
        if len(self.finding_ids) > 64 or len(set(self.finding_ids)) != len(
            self.finding_ids
        ):
            raise ValidationError("modification decision finding ids are invalid")
        for finding_id in self.finding_ids:
            _identity(finding_id, "modification decision finding id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "change_type": self.change_type,
            "description": self.description,
            "finding_ids": list(self.finding_ids),
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"id", "change_type", "description", "finding_ids"}, path)
        return cls(
            id=_identity(item["id"], f"{path}.id"),
            change_type=_choice(
                item["change_type"], CHANGE_TYPES, f"{path}.change_type"
            ),
            description=_text(item["description"], f"{path}.description"),
            finding_ids=tuple(
                _identity(entry, f"{path}.finding_ids[{index}]")
                for index, entry in enumerate(
                    _array(
                        item["finding_ids"],
                        f"{path}.finding_ids",
                        lambda raw, item_path: _identity(raw, item_path),
                        maximum=64,
                    )
                )
            ),
        )


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    category: str
    description: str

    def __post_init__(self) -> None:
        _identity(self.id, "review finding id")
        _choice(self.category, FINDING_CATEGORIES, "review finding category")
        _text(self.description, "review finding description")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"id", "category", "description"}, path)
        return cls(
            id=_identity(item["id"], f"{path}.id"),
            category=_choice(item["category"], FINDING_CATEGORIES, f"{path}.category"),
            description=_text(item["description"], f"{path}.description"),
        )


@dataclass(frozen=True)
class ReviewChecklistItem:
    """One case-authored human-review obligation and its explicit disposition."""

    id: str
    kind: str
    requirement: str
    disposition: str
    evidence_note: str | None

    def __post_init__(self) -> None:
        _identity(self.id, "review checklist item id")
        _choice(self.kind, REVIEW_CHECKLIST_KINDS, "review checklist item kind")
        _text(self.requirement, "review checklist requirement")
        _choice(
            self.disposition,
            REVIEW_CHECKLIST_DISPOSITIONS,
            "review checklist disposition",
        )
        _optional_text(self.evidence_note, "review checklist evidence note")
        if self.disposition == "not_reviewed":
            if self.evidence_note is not None:
                raise ValidationError(
                    "not-reviewed checklist item cannot contain an evidence note"
                )
        elif self.evidence_note is None:
            raise ValidationError(
                "completed checklist item needs a nonempty evidence note"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "requirement": self.requirement,
            "disposition": self.disposition,
            "evidence_note": self.evidence_note,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {"id", "kind", "requirement", "disposition", "evidence_note"},
            path,
        )
        return cls(
            id=_identity(item["id"], f"{path}.id"),
            kind=_choice(item["kind"], REVIEW_CHECKLIST_KINDS, f"{path}.kind"),
            requirement=_text(item["requirement"], f"{path}.requirement"),
            disposition=_choice(
                item["disposition"],
                REVIEW_CHECKLIST_DISPOSITIONS,
                f"{path}.disposition",
            ),
            evidence_note=_optional_text(
                item["evidence_note"], f"{path}.evidence_note"
            ),
        )


@dataclass(frozen=True)
class OrderabilityEvidence:
    """One dated, attributed sourcing observation for reviewed BOM slots.

    This is reviewer-supplied evidence, not a live inventory query.  The source
    and observation time stay explicit so a later reader can judge staleness.
    """

    slot_ids: tuple[str, ...]
    manufacturer_part_number: str
    status: str
    as_of: str
    source_kind: str
    source_name: str
    source_url: str
    note: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.slot_ids) <= 128 or len(set(self.slot_ids)) != len(
            self.slot_ids
        ):
            raise ValidationError("orderability evidence slot ids are invalid")
        for slot_id in self.slot_ids:
            _slot_id(slot_id, "orderability evidence slot id")
        _text(
            self.manufacturer_part_number,
            "orderability evidence manufacturer part number",
            limit=512,
        )
        _choice(
            self.status,
            ORDERABILITY_EVIDENCE_STATUSES,
            "orderability evidence status",
        )
        _timestamp(self.as_of, "orderability evidence as_of")
        _choice(
            self.source_kind,
            ORDERABILITY_SOURCE_KINDS,
            "orderability evidence source kind",
        )
        _text(self.source_name, "orderability evidence source name", limit=512)
        _https_url(self.source_url, "orderability evidence source URL")
        _text(self.note, "orderability evidence note")

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_ids": list(self.slot_ids),
            "manufacturer_part_number": self.manufacturer_part_number,
            "status": self.status,
            "as_of": self.as_of,
            "source_kind": self.source_kind,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "slot_ids",
                "manufacturer_part_number",
                "status",
                "as_of",
                "source_kind",
                "source_name",
                "source_url",
                "note",
            },
            path,
        )
        return cls(
            slot_ids=_array(
                item["slot_ids"],
                f"{path}.slot_ids",
                lambda raw, item_path: _slot_id(raw, item_path),
                minimum=1,
                maximum=128,
            ),
            manufacturer_part_number=_text(
                item["manufacturer_part_number"],
                f"{path}.manufacturer_part_number",
                limit=512,
            ),
            status=_choice(
                item["status"],
                ORDERABILITY_EVIDENCE_STATUSES,
                f"{path}.status",
            ),
            as_of=_timestamp(item["as_of"], f"{path}.as_of"),
            source_kind=_choice(
                item["source_kind"],
                ORDERABILITY_SOURCE_KINDS,
                f"{path}.source_kind",
            ),
            source_name=_text(item["source_name"], f"{path}.source_name", limit=512),
            source_url=_https_url(item["source_url"], f"{path}.source_url"),
            note=_text(item["note"], f"{path}.note"),
        )


def _review_checklist_id(
    case: BoardBenchCase, kind: str, index: int, requirement: str
) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "case_id": case.id,
                "kind": kind,
                "requirement": requirement,
            }
        )
    ).hexdigest()
    prefix = "review-rubric" if kind == "review_rubric" else "assembly-constraint"
    return f"{prefix}-{index:03d}-{digest}"


def review_checklist_for_case(case: BoardBenchCase) -> tuple[ReviewChecklistItem, ...]:
    """Materialize stable source items without inventing reviewer evidence."""

    return (
        *(
            ReviewChecklistItem(
                id=_review_checklist_id(case, "review_rubric", index, requirement),
                kind="review_rubric",
                requirement=requirement,
                disposition="not_reviewed",
                evidence_note=None,
            )
            for index, requirement in enumerate(case.review_rubric, start=1)
        ),
        *(
            ReviewChecklistItem(
                id=_review_checklist_id(
                    case, "assembly_constraint", index, requirement
                ),
                kind="assembly_constraint",
                requirement=requirement,
                disposition="not_reviewed",
                evidence_note=None,
            )
            for index, requirement in enumerate(case.assembly_constraints, start=1)
        ),
    )


@dataclass(frozen=True)
class BoardBenchReview:
    schema: ClassVar[str] = REVIEW_SCHEMA

    campaign_id: str
    run_id: str
    source_campaign_sha256: str
    source_corpus_sha256: str
    source_case_sha256: str
    source_run_sha256: str
    source_score_sha256: str | None
    reviewer: str | None
    reviewed_at: str | None
    outcome: str
    functional_correctness: str
    orderable_state: str
    orderability_evidence: tuple[OrderabilityEvidence, ...]
    active_engineer_minutes: float | None
    not_applicable_reason: str | None
    checklist: tuple[ReviewChecklistItem, ...]
    findings: tuple[ReviewFinding, ...]
    modifications: tuple[ModificationDecision, ...]
    final_failure: FailureClassification | None

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "review campaign id")
        _identity(self.run_id, "review run id")
        _sha256(self.source_campaign_sha256, "review source campaign hash")
        _sha256(self.source_corpus_sha256, "review source corpus hash")
        _sha256(self.source_case_sha256, "review source case hash")
        _sha256(self.source_run_sha256, "review source run hash")
        if self.source_score_sha256 is not None:
            _sha256(self.source_score_sha256, "review source score hash")
        _optional_text(self.reviewer, "reviewer attribution", limit=512)
        _optional_timestamp(self.reviewed_at, "review reviewed_at")
        _choice(self.outcome, REVIEW_OUTCOMES, "review outcome")
        _choice(
            self.functional_correctness, METRIC_STATES, "review functional correctness"
        )
        _choice(self.orderable_state, METRIC_STATES, "review orderable state")
        if len(self.orderability_evidence) > 256:
            raise ValidationError("review orderability evidence is oversized")
        orderability_slots = [
            slot_id
            for evidence in self.orderability_evidence
            for slot_id in evidence.slot_ids
        ]
        if len(orderability_slots) != len(set(orderability_slots)):
            raise ValidationError(
                "review orderability evidence repeats a component slot"
            )
        if self.active_engineer_minutes is not None:
            _number(
                self.active_engineer_minutes,
                "review active engineer minutes",
                minimum=0.0,
                maximum=100_000.0,
            )
        _optional_text(self.not_applicable_reason, "review not-applicable reason")
        if not 2 <= len(self.checklist) <= 256:
            raise ValidationError("review checklist is incomplete or oversized")
        _unique(self.checklist, lambda item: item.id, "review checklist items")
        if {item.kind for item in self.checklist} != REVIEW_CHECKLIST_KINDS:
            raise ValidationError(
                "review checklist needs rubric and assembly source items"
            )
        if len(self.findings) > 256 or len(self.modifications) > 256:
            raise ValidationError("review findings or modifications are oversized")
        _unique(self.findings, lambda item: item.id, "review findings")
        _unique(self.modifications, lambda item: item.id, "review modifications")
        finding_ids = {item.id for item in self.findings}
        if any(not set(item.finding_ids) <= finding_ids for item in self.modifications):
            raise ValidationError("review modification references an unknown finding")
        if self.outcome == "not_reviewed":
            if (
                any(
                    value is not None
                    for value in (
                        self.reviewer,
                        self.reviewed_at,
                        self.active_engineer_minutes,
                    )
                )
                or self.findings
                or self.modifications
                or self.final_failure is not None
                or self.orderability_evidence
            ):
                raise ValidationError("not-reviewed record contains review evidence")
            if (
                self.functional_correctness != "unknown"
                or self.orderable_state != "unknown"
            ):
                raise ValidationError("not-reviewed metrics must remain unknown")
            if any(item.disposition != "not_reviewed" for item in self.checklist):
                raise ValidationError(
                    "not-reviewed checklist dispositions must remain not_reviewed"
                )
        else:
            if (
                self.reviewer is None
                or self.reviewed_at is None
                or self.active_engineer_minutes is None
            ):
                raise ValidationError(
                    "completed review needs reviewer, time, and active minutes"
                )
            if any(item.disposition == "not_reviewed" for item in self.checklist):
                raise ValidationError(
                    "completed review needs every checklist item disposition"
                )
        if self.outcome == "not_applicable":
            if not self.not_applicable_reason:
                raise ValidationError("not-applicable review needs a reason")
            if self.functional_correctness != "not_applicable":
                raise ValidationError(
                    "not-applicable review has an invalid functional result"
                )
            if self.orderable_state != "not_applicable":
                raise ValidationError(
                    "not-applicable review has an invalid orderable result"
                )
            if self.modifications:
                raise ValidationError(
                    "not-applicable review cannot contain modification decisions"
                )
            if self.orderability_evidence:
                raise ValidationError(
                    "not-applicable review cannot contain orderability evidence"
                )
            if self.final_failure is None:
                raise ValidationError(
                    "not-applicable review needs a final failure classification"
                )
            if any(item.disposition != "not_applicable" for item in self.checklist):
                raise ValidationError(
                    "not-applicable review needs every checklist item not_applicable"
                )
        elif self.not_applicable_reason is not None:
            raise ValidationError(
                "applicable review cannot have a not-applicable reason"
            )
        elif any(item.disposition == "not_applicable" for item in self.checklist):
            raise ValidationError(
                "applicable review cannot skip a case-authored checklist item"
            )
        if self.outcome not in {"not_reviewed", "not_applicable"}:
            if not self.orderability_evidence:
                raise ValidationError(
                    "completed applicable review needs orderability evidence"
                )
            if self.reviewed_at is not None and any(
                datetime.fromisoformat(item.as_of)
                > datetime.fromisoformat(self.reviewed_at)
                for item in self.orderability_evidence
            ):
                raise ValidationError(
                    "orderability evidence as_of cannot follow the review time"
                )
            statuses = {item.status for item in self.orderability_evidence}
            expected_orderable_state = (
                "fail"
                if "not_orderable" in statuses
                else "unknown"
                if "unknown" in statuses
                else "pass"
            )
            if self.orderable_state != expected_orderable_state:
                raise ValidationError(
                    "review orderable state contradicts orderability evidence"
                )
        if self.outcome in {
            "pass_without_schematic_change",
            "pass_after_changes",
        }:
            if self.functional_correctness != "pass":
                raise ValidationError(
                    "passing review must mark functional correctness pass"
                )
            if self.orderable_state != "pass":
                raise ValidationError("passing review must mark orderable state pass")
            if any(item.disposition != "pass" for item in self.checklist):
                raise ValidationError(
                    "passing review needs every case-authored checklist item to pass"
                )
        if self.outcome == "pass_without_schematic_change" and self.modifications:
            raise ValidationError(
                "no-change pass cannot contain modification decisions"
            )
        if self.outcome == "pass_after_changes" and not self.modifications:
            raise ValidationError("changed pass needs modification decisions")
        if self.outcome == "pass_after_changes" and self.final_failure is None:
            raise ValidationError(
                "changed pass needs the generated result's failure classification"
            )
        if self.outcome == "fail" and self.final_failure is None:
            raise ValidationError("failed review needs a final failure classification")

    @property
    def modification_count(self) -> int:
        return len(self.modifications)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": REVIEW_ARTIFACT_VERSION,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "source_campaign_sha256": self.source_campaign_sha256,
            "source_corpus_sha256": self.source_corpus_sha256,
            "source_case_sha256": self.source_case_sha256,
            "source_run_sha256": self.source_run_sha256,
            "source_score_sha256": self.source_score_sha256,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "outcome": self.outcome,
            "functional_correctness": self.functional_correctness,
            "orderable_state": self.orderable_state,
            "orderability_evidence": [
                item.to_dict() for item in self.orderability_evidence
            ],
            "active_engineer_minutes": self.active_engineer_minutes,
            "not_applicable_reason": self.not_applicable_reason,
            "checklist": [item.to_dict() for item in self.checklist],
            "findings": [item.to_dict() for item in self.findings],
            "modifications": [item.to_dict() for item in self.modifications],
            "final_failure": (
                self.final_failure.to_dict() if self.final_failure is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "campaign_id",
            "run_id",
            "source_campaign_sha256",
            "source_corpus_sha256",
            "source_case_sha256",
            "source_run_sha256",
            "source_score_sha256",
            "reviewer",
            "reviewed_at",
            "outcome",
            "functional_correctness",
            "orderable_state",
            "orderability_evidence",
            "active_engineer_minutes",
            "not_applicable_reason",
            "checklist",
            "findings",
            "modifications",
            "final_failure",
        }
        item = _schema(
            value,
            schema=cls.schema,
            fields=fields,
            path=path,
            version=REVIEW_ARTIFACT_VERSION,
        )
        final_failure = item["final_failure"]
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            run_id=_identity(item["run_id"], f"{path}.run_id"),
            source_campaign_sha256=_sha256(
                item["source_campaign_sha256"],
                f"{path}.source_campaign_sha256",
            ),
            source_corpus_sha256=_sha256(
                item["source_corpus_sha256"], f"{path}.source_corpus_sha256"
            ),
            source_case_sha256=_sha256(
                item["source_case_sha256"], f"{path}.source_case_sha256"
            ),
            source_run_sha256=_sha256(
                item["source_run_sha256"], f"{path}.source_run_sha256"
            ),
            source_score_sha256=(
                None
                if item["source_score_sha256"] is None
                else _sha256(item["source_score_sha256"], f"{path}.source_score_sha256")
            ),
            reviewer=_optional_text(item["reviewer"], f"{path}.reviewer", limit=512),
            reviewed_at=_optional_timestamp(item["reviewed_at"], f"{path}.reviewed_at"),
            outcome=_choice(item["outcome"], REVIEW_OUTCOMES, f"{path}.outcome"),
            functional_correctness=_choice(
                item["functional_correctness"],
                METRIC_STATES,
                f"{path}.functional_correctness",
            ),
            orderable_state=_choice(
                item["orderable_state"], METRIC_STATES, f"{path}.orderable_state"
            ),
            orderability_evidence=_array(
                item["orderability_evidence"],
                f"{path}.orderability_evidence",
                OrderabilityEvidence.from_dict,
                maximum=256,
            ),
            active_engineer_minutes=_optional_number(
                item["active_engineer_minutes"],
                f"{path}.active_engineer_minutes",
                minimum=0.0,
                maximum=100_000.0,
            ),
            not_applicable_reason=_optional_text(
                item["not_applicable_reason"], f"{path}.not_applicable_reason"
            ),
            checklist=_array(
                item["checklist"],
                f"{path}.checklist",
                ReviewChecklistItem.from_dict,
                minimum=2,
                maximum=256,
            ),
            findings=_array(
                item["findings"],
                f"{path}.findings",
                ReviewFinding.from_dict,
                maximum=256,
            ),
            modifications=_array(
                item["modifications"],
                f"{path}.modifications",
                ModificationDecision.from_dict,
                maximum=256,
            ),
            final_failure=(
                None
                if final_failure is None
                else FailureClassification.from_dict(
                    final_failure, f"{path}.final_failure"
                )
            ),
        )


def validate_review_against_case(
    review: BoardBenchReview, case: BoardBenchCase
) -> None:
    """Require the exact source-authored rubric and assembly checklist set."""

    expected = {item.id: item for item in review_checklist_for_case(case)}
    actual = {item.id: item for item in review.checklist}
    if set(actual) != set(expected):
        raise ValidationError(
            "BoardBench review checklist item set differs from source case"
        )
    for item_id, expected_item in expected.items():
        actual_item = actual[item_id]
        if (
            actual_item.kind != expected_item.kind
            or actual_item.requirement != expected_item.requirement
        ):
            raise ValidationError(
                "BoardBench review checklist item differs from source case: " + item_id
            )
    if review.outcome not in {"not_reviewed", "not_applicable"}:
        expected_slots = {item.id for item in case.component_slots}
        observed_slots = [
            slot_id
            for evidence in review.orderability_evidence
            for slot_id in evidence.slot_ids
        ]
        if (
            len(observed_slots) != len(set(observed_slots))
            or set(observed_slots) != expected_slots
        ):
            raise ValidationError(
                "BoardBench review orderability evidence does not cover the "
                "source case component slots exactly"
            )


def case_sha256(case: BoardBenchCase) -> str:
    """Hash the complete hidden case contract, not just its prompt/checklist."""

    return hashlib.sha256(canonical_json_bytes(case.to_dict())).hexdigest()


def run_has_inspectable_schematic(run: BoardBenchRun) -> bool:
    """Return whether retained terminal inventory contains a nonempty schematic."""

    return any(
        entry.size_bytes > 0 and entry.path.endswith(".kicad_sch")
        for entry in run.inventory
    )


def validate_review_sources(
    review: BoardBenchReview,
    *,
    campaign: BoardBenchCampaign,
    corpus: BoardBenchCorpus,
    case: BoardBenchCase,
    run: BoardBenchRun,
    score: BoardBenchScore | None,
) -> None:
    """Validate one portable review against every canonical source artifact."""

    corpus_hash = artifact_sha256(corpus)
    campaign_hash = artifact_sha256(campaign)
    run_hash = artifact_sha256(run)
    expected_prompt_hash = hashlib.sha256(case.prompt.encode("utf-8")).hexdigest()
    if (
        campaign.corpus_id != corpus.corpus_id
        or campaign.cohort != corpus.cohort
        or campaign.corpus_sha256 != corpus_hash
        or run.campaign_id != campaign.campaign_id
        or run.case_id != case.id
        or run.prompt_sha256 != expected_prompt_hash
        or not run.terminal
        or review.campaign_id != campaign.campaign_id
        or review.run_id != run.run_id
        or review.source_campaign_sha256 != campaign_hash
        or review.source_corpus_sha256 != corpus_hash
        or review.source_case_sha256 != case_sha256(case)
        or review.source_run_sha256 != run_hash
    ):
        raise ValidationError("BoardBench review source hash or identity differs")
    validate_review_against_case(review, case)

    if review.outcome != "not_reviewed":
        has_schematic = run_has_inspectable_schematic(run)
        if review.outcome == "not_applicable" and has_schematic:
            raise ValidationError(
                "BoardBench not-applicable review has an inspectable schematic"
            )
        if review.outcome != "not_applicable" and not has_schematic:
            raise ValidationError(
                "BoardBench applicable review has no inspectable schematic"
            )

    if score is None:
        if review.source_score_sha256 is not None:
            raise ValidationError("BoardBench review cites an unavailable score")
        return

    if (
        score.campaign_id != campaign.campaign_id
        or score.run_id != run.run_id
        or score.source_campaign_sha256 != campaign_hash
        or score.source_case_sha256 != case_sha256(case)
        or score.source_run_sha256 != run_hash
        or score.evaluator_version != campaign.evaluator_version
        or review.source_score_sha256 != artifact_sha256(score)
    ):
        raise ValidationError("BoardBench review source score hash differs")
    if review.reviewed_at is not None and datetime.fromisoformat(
        review.reviewed_at
    ) < datetime.fromisoformat(score.scored_at):
        raise ValidationError("BoardBench review predates its cited automatic score")
    if (
        review.outcome != "not_reviewed"
        and score.overall_state != "pass"
        and review.final_failure is None
    ):
        raise ValidationError(
            "BoardBench non-passing automatic score needs reviewer final failure "
            "classification"
        )
    if (
        score.overall_state == "pass"
        and review.outcome == "pass_without_schematic_change"
        and review.final_failure is not None
    ):
        raise ValidationError(
            "BoardBench review failure classification contradicts passing "
            "automatic and engineering evidence"
        )


@dataclass(frozen=True)
class StructuralDiffEntry:
    area: str
    operation: str
    identity: str
    before_sha256: str | None
    after_sha256: str | None
    description: str

    def __post_init__(self) -> None:
        _choice(self.area, DIFF_AREAS, "structural diff area")
        _choice(self.operation, DIFF_OPERATIONS, "structural diff operation")
        _text(self.identity, "structural diff identity", limit=1_024)
        if self.before_sha256 is not None:
            _sha256(self.before_sha256, "structural diff before hash")
        if self.after_sha256 is not None:
            _sha256(self.after_sha256, "structural diff after hash")
        if self.operation == "added" and (
            self.before_sha256 is not None or self.after_sha256 is None
        ):
            raise ValidationError("added diff entry has invalid before/after hashes")
        if self.operation == "removed" and (
            self.before_sha256 is None or self.after_sha256 is not None
        ):
            raise ValidationError("removed diff entry has invalid before/after hashes")
        if self.operation == "changed" and (
            self.before_sha256 is None
            or self.after_sha256 is None
            or self.before_sha256 == self.after_sha256
        ):
            raise ValidationError("changed diff entry has invalid before/after hashes")
        _text(self.description, "structural diff description")

    def to_dict(self) -> dict[str, Any]:
        return {
            "area": self.area,
            "operation": self.operation,
            "identity": self.identity,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "area",
                "operation",
                "identity",
                "before_sha256",
                "after_sha256",
                "description",
            },
            path,
        )
        before = item["before_sha256"]
        after = item["after_sha256"]
        return cls(
            area=_choice(item["area"], DIFF_AREAS, f"{path}.area"),
            operation=_choice(item["operation"], DIFF_OPERATIONS, f"{path}.operation"),
            identity=_text(item["identity"], f"{path}.identity", limit=1_024),
            before_sha256=(
                None if before is None else _sha256(before, f"{path}.before_sha256")
            ),
            after_sha256=(
                None if after is None else _sha256(after, f"{path}.after_sha256")
            ),
            description=_text(item["description"], f"{path}.description"),
        )


@dataclass(frozen=True)
class BoardBenchCorrection:
    schema: ClassVar[str] = CORRECTION_SCHEMA

    campaign_id: str
    run_id: str
    source_run_sha256: str
    source_review_sha256: str
    created_at: str
    generated_snapshot_sha256: str
    corrected_snapshot_sha256: str
    manufacturing_candidate_sha256: str | None
    decision_ids: tuple[str, ...]
    changes: tuple[StructuralDiffEntry, ...]
    inventory: tuple[InventoryEntry, ...]

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "correction campaign id")
        _identity(self.run_id, "correction run id")
        for value, label in (
            (self.source_run_sha256, "source run"),
            (self.source_review_sha256, "source review"),
            (self.generated_snapshot_sha256, "generated snapshot"),
            (self.corrected_snapshot_sha256, "corrected snapshot"),
        ):
            _sha256(value, f"correction {label} hash")
        if self.manufacturing_candidate_sha256 is not None:
            _sha256(
                self.manufacturing_candidate_sha256,
                "correction manufacturing candidate hash",
            )
        _timestamp(self.created_at, "correction created_at")
        if not 1 <= len(self.decision_ids) <= 256 or len(set(self.decision_ids)) != len(
            self.decision_ids
        ):
            raise ValidationError("correction decision ids are invalid")
        for decision_id in self.decision_ids:
            _identity(decision_id, "correction decision id")
        if not 1 <= len(self.changes) <= 2_048:
            raise ValidationError("correction changes are invalid")
        if self.generated_snapshot_sha256 == self.corrected_snapshot_sha256:
            raise ValidationError(
                "correction must bind distinct generated/corrected snapshots"
            )
        if len(self.inventory) > MAX_INVENTORY_FILES:
            raise ValidationError("correction inventory is oversized")
        _unique(self.inventory, lambda item: item.path, "correction inventory")
        if sum(item.size_bytes for item in self.inventory) > MAX_INVENTORY_BYTES:
            raise ValidationError("correction inventory exceeds the total size limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": ARTIFACT_VERSION,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "source_run_sha256": self.source_run_sha256,
            "source_review_sha256": self.source_review_sha256,
            "created_at": self.created_at,
            "generated_snapshot_sha256": self.generated_snapshot_sha256,
            "corrected_snapshot_sha256": self.corrected_snapshot_sha256,
            "manufacturing_candidate_sha256": self.manufacturing_candidate_sha256,
            "decision_ids": list(self.decision_ids),
            "changes": [item.to_dict() for item in self.changes],
            "inventory": [item.to_dict() for item in self.inventory],
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "campaign_id",
            "run_id",
            "source_run_sha256",
            "source_review_sha256",
            "created_at",
            "generated_snapshot_sha256",
            "corrected_snapshot_sha256",
            "manufacturing_candidate_sha256",
            "decision_ids",
            "changes",
            "inventory",
        }
        item = _schema(value, schema=cls.schema, fields=fields, path=path)
        candidate_hash = item["manufacturing_candidate_sha256"]
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            run_id=_identity(item["run_id"], f"{path}.run_id"),
            source_run_sha256=_sha256(
                item["source_run_sha256"], f"{path}.source_run_sha256"
            ),
            source_review_sha256=_sha256(
                item["source_review_sha256"], f"{path}.source_review_sha256"
            ),
            created_at=_timestamp(item["created_at"], f"{path}.created_at"),
            generated_snapshot_sha256=_sha256(
                item["generated_snapshot_sha256"],
                f"{path}.generated_snapshot_sha256",
            ),
            corrected_snapshot_sha256=_sha256(
                item["corrected_snapshot_sha256"],
                f"{path}.corrected_snapshot_sha256",
            ),
            manufacturing_candidate_sha256=(
                None
                if candidate_hash is None
                else _sha256(candidate_hash, f"{path}.manufacturing_candidate_sha256")
            ),
            decision_ids=tuple(
                _identity(entry, f"{path}.decision_ids[{index}]")
                for index, entry in enumerate(
                    _array(
                        item["decision_ids"],
                        f"{path}.decision_ids",
                        lambda raw, item_path: _identity(raw, item_path),
                        minimum=1,
                        maximum=256,
                    )
                )
            ),
            changes=_array(
                item["changes"],
                f"{path}.changes",
                StructuralDiffEntry.from_dict,
                minimum=1,
                maximum=2_048,
            ),
            inventory=_array(
                item["inventory"],
                f"{path}.inventory",
                InventoryEntry.from_dict,
                maximum=MAX_INVENTORY_FILES,
            ),
        )


@dataclass(frozen=True)
class RailMeasurement:
    name: str
    unit: str
    expected_minimum: float
    expected_maximum: float
    measured: float | None
    state: str

    def __post_init__(self) -> None:
        _text(self.name, "rail measurement name", limit=128)
        _choice(self.unit, {"V", "A"}, "rail measurement unit")
        _number(self.expected_minimum, "rail expected minimum")
        _number(self.expected_maximum, "rail expected maximum")
        if self.expected_minimum > self.expected_maximum:
            raise ValidationError("rail expected minimum exceeds maximum")
        _choice(self.state, RAIL_STATES, "rail measurement state")
        if self.state == "not_tested":
            if self.measured is not None:
                raise ValidationError("untested rail cannot contain a measurement")
        elif self.measured is None:
            raise ValidationError("tested rail needs a measurement")
        else:
            _number(self.measured, "rail measured value")
        if (
            self.state == "pass"
            and self.measured is not None
            and not self.expected_minimum <= self.measured <= self.expected_maximum
        ):
            raise ValidationError(
                "passing rail measurement is outside its expected range"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "expected_minimum": self.expected_minimum,
            "expected_maximum": self.expected_maximum,
            "measured": self.measured,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "name",
                "unit",
                "expected_minimum",
                "expected_maximum",
                "measured",
                "state",
            },
            path,
        )
        return cls(
            name=_text(item["name"], f"{path}.name", limit=128),
            unit=_choice(item["unit"], {"V", "A"}, f"{path}.unit"),
            expected_minimum=_number(
                item["expected_minimum"], f"{path}.expected_minimum"
            ),
            expected_maximum=_number(
                item["expected_maximum"], f"{path}.expected_maximum"
            ),
            measured=_optional_number(item["measured"], f"{path}.measured"),
            state=_choice(item["state"], RAIL_STATES, f"{path}.state"),
        )


@dataclass(frozen=True)
class BoardBenchHardware:
    schema: ClassVar[str] = HARDWARE_SCHEMA

    campaign_id: str
    run_id: str
    source_kind: str
    source_artifact_sha256: str
    category: str
    board_revision: str
    board_serial: str
    revision_count: int
    fabricator: str
    operator: str
    observed_at: str
    fabricator_accepted: str
    solderability: str
    first_power_no_short: str
    firmware_download: str
    core_function: str
    rails: tuple[RailMeasurement, ...]
    notes: str
    attachments: tuple[InventoryEntry, ...]

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "hardware campaign id")
        _identity(self.run_id, "hardware run id")
        _choice(self.source_kind, {"correction", "release"}, "hardware source kind")
        _sha256(self.source_artifact_sha256, "hardware source artifact hash")
        _choice(self.category, BOARD_CATEGORIES, "hardware category")
        for value, label, limit in (
            (self.board_revision, "board revision", 128),
            (self.board_serial, "board serial", 128),
            (self.fabricator, "fabricator", 512),
            (self.operator, "operator", 512),
        ):
            _text(value, f"hardware {label}", limit=limit)
        _integer(
            self.revision_count, "hardware revision count", minimum=1, maximum=1_000
        )
        _timestamp(self.observed_at, "hardware observed_at")
        for value, label in (
            (self.fabricator_accepted, "fabricator acceptance"),
            (self.solderability, "solderability"),
            (self.first_power_no_short, "first-power short result"),
            (self.firmware_download, "firmware download result"),
            (self.core_function, "core-function result"),
        ):
            _choice(value, PHYSICAL_STATES, f"hardware {label}")
        if not 1 <= len(self.rails) <= 64:
            raise ValidationError("hardware rail measurements are oversized")
        _unique(self.rails, lambda item: item.name, "hardware rail measurements")
        _text(self.notes, "hardware notes", limit=MAX_TEXT_BYTES, empty=True)
        if not 1 <= len(self.attachments) <= 256:
            raise ValidationError("hardware record needs 1..256 hashed attachments")
        _unique(self.attachments, lambda item: item.path, "hardware attachments")
        if any(item.size_bytes == 0 for item in self.attachments):
            raise ValidationError("hardware attachments must not be empty")
        if sum(item.size_bytes for item in self.attachments) > MAX_INVENTORY_BYTES:
            raise ValidationError("hardware attachments exceed the total size limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": ARTIFACT_VERSION,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "source_kind": self.source_kind,
            "source_artifact_sha256": self.source_artifact_sha256,
            "category": self.category,
            "board_revision": self.board_revision,
            "board_serial": self.board_serial,
            "revision_count": self.revision_count,
            "fabricator": self.fabricator,
            "operator": self.operator,
            "observed_at": self.observed_at,
            "fabricator_accepted": self.fabricator_accepted,
            "solderability": self.solderability,
            "first_power_no_short": self.first_power_no_short,
            "firmware_download": self.firmware_download,
            "core_function": self.core_function,
            "rails": [item.to_dict() for item in self.rails],
            "notes": self.notes,
            "attachments": [item.to_dict() for item in self.attachments],
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "campaign_id",
            "run_id",
            "source_kind",
            "source_artifact_sha256",
            "category",
            "board_revision",
            "board_serial",
            "revision_count",
            "fabricator",
            "operator",
            "observed_at",
            "fabricator_accepted",
            "solderability",
            "first_power_no_short",
            "firmware_download",
            "core_function",
            "rails",
            "notes",
            "attachments",
        }
        item = _schema(value, schema=cls.schema, fields=fields, path=path)
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            run_id=_identity(item["run_id"], f"{path}.run_id"),
            source_kind=_choice(
                item["source_kind"], {"correction", "release"}, f"{path}.source_kind"
            ),
            source_artifact_sha256=_sha256(
                item["source_artifact_sha256"], f"{path}.source_artifact_sha256"
            ),
            category=_choice(item["category"], BOARD_CATEGORIES, f"{path}.category"),
            board_revision=_text(
                item["board_revision"], f"{path}.board_revision", limit=128
            ),
            board_serial=_text(item["board_serial"], f"{path}.board_serial", limit=128),
            revision_count=_integer(
                item["revision_count"],
                f"{path}.revision_count",
                minimum=1,
                maximum=1_000,
            ),
            fabricator=_text(item["fabricator"], f"{path}.fabricator", limit=512),
            operator=_text(item["operator"], f"{path}.operator", limit=512),
            observed_at=_timestamp(item["observed_at"], f"{path}.observed_at"),
            fabricator_accepted=_choice(
                item["fabricator_accepted"],
                PHYSICAL_STATES,
                f"{path}.fabricator_accepted",
            ),
            solderability=_choice(
                item["solderability"], PHYSICAL_STATES, f"{path}.solderability"
            ),
            first_power_no_short=_choice(
                item["first_power_no_short"],
                PHYSICAL_STATES,
                f"{path}.first_power_no_short",
            ),
            firmware_download=_choice(
                item["firmware_download"],
                PHYSICAL_STATES,
                f"{path}.firmware_download",
            ),
            core_function=_choice(
                item["core_function"], PHYSICAL_STATES, f"{path}.core_function"
            ),
            rails=_array(
                item["rails"],
                f"{path}.rails",
                RailMeasurement.from_dict,
                minimum=1,
                maximum=64,
            ),
            notes=_text(
                item["notes"], f"{path}.notes", limit=MAX_TEXT_BYTES, empty=True
            ),
            attachments=_array(
                item["attachments"],
                f"{path}.attachments",
                InventoryEntry.from_dict,
                minimum=1,
                maximum=256,
            ),
        )


@dataclass(frozen=True)
class SelectionEntry:
    category: str
    run_id: str
    source_artifact_sha256: str
    rationale: str

    def __post_init__(self) -> None:
        _choice(self.category, BOARD_CATEGORIES, "selection category")
        _identity(self.run_id, "selection run id")
        _sha256(self.source_artifact_sha256, "selection source artifact hash")
        _text(self.rationale, "selection rationale")

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "run_id": self.run_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {"category", "run_id", "source_artifact_sha256", "rationale"},
            path,
        )
        return cls(
            category=_choice(item["category"], BOARD_CATEGORIES, f"{path}.category"),
            run_id=_identity(item["run_id"], f"{path}.run_id"),
            source_artifact_sha256=_sha256(
                item["source_artifact_sha256"], f"{path}.source_artifact_sha256"
            ),
            rationale=_text(item["rationale"], f"{path}.rationale"),
        )


@dataclass(frozen=True)
class BoardBenchSelection:
    schema: ClassVar[str] = SELECTION_SCHEMA

    campaign_id: str
    created_at: str
    selector: str
    selections: tuple[SelectionEntry, ...]

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "selection campaign id")
        _timestamp(self.created_at, "selection created_at")
        _text(self.selector, "selection selector", limit=512)
        if not 5 <= len(self.selections) <= 20:
            raise ValidationError("selection must contain 5..20 boards")
        _unique(self.selections, lambda item: item.run_id, "selected run ids")
        if {item.category for item in self.selections} != set(BOARD_CATEGORIES):
            raise ValidationError("selection must cover every BoardBench category")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": ARTIFACT_VERSION,
            "campaign_id": self.campaign_id,
            "created_at": self.created_at,
            "selector": self.selector,
            "selections": [item.to_dict() for item in self.selections],
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        item = _schema(
            value,
            schema=cls.schema,
            fields={"campaign_id", "created_at", "selector", "selections"},
            path=path,
        )
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            created_at=_timestamp(item["created_at"], f"{path}.created_at"),
            selector=_text(item["selector"], f"{path}.selector", limit=512),
            selections=_array(
                item["selections"],
                f"{path}.selections",
                SelectionEntry.from_dict,
                minimum=5,
                maximum=20,
            ),
        )


@dataclass(frozen=True)
class MetricAggregate:
    name: str
    total: int
    passed: int
    failed: int
    unknown: int
    not_applicable: int

    def __post_init__(self) -> None:
        _choice(self.name, AUTOMATIC_METRICS, "metric aggregate name")
        for value, label in (
            (self.total, "total"),
            (self.passed, "passed"),
            (self.failed, "failed"),
            (self.unknown, "unknown"),
            (self.not_applicable, "not applicable"),
        ):
            _integer(value, f"metric aggregate {label}", maximum=60)
        if self.passed + self.failed + self.unknown + self.not_applicable != self.total:
            raise ValidationError("metric aggregate counts do not sum to total")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "unknown": self.unknown,
            "not_applicable": self.not_applicable,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {"name", "total", "passed", "failed", "unknown", "not_applicable"},
            path,
        )
        return cls(
            name=_choice(item["name"], AUTOMATIC_METRICS, f"{path}.name"),
            total=_integer(item["total"], f"{path}.total", maximum=60),
            passed=_integer(item["passed"], f"{path}.passed", maximum=60),
            failed=_integer(item["failed"], f"{path}.failed", maximum=60),
            unknown=_integer(item["unknown"], f"{path}.unknown", maximum=60),
            not_applicable=_integer(
                item["not_applicable"], f"{path}.not_applicable", maximum=60
            ),
        )


@dataclass(frozen=True)
class FailureStageCount:
    stage: str
    count: int

    def __post_init__(self) -> None:
        _choice(self.stage, FAILURE_STAGE_VALUES, "failure-stage count stage")
        _integer(self.count, "failure-stage count", maximum=60)

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "count": self.count}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"stage", "count"}, path)
        return cls(
            stage=_choice(item["stage"], FAILURE_STAGE_VALUES, f"{path}.stage"),
            count=_integer(item["count"], f"{path}.count", maximum=60),
        )


@dataclass(frozen=True)
class NamedCount:
    value: str
    count: int

    def __post_init__(self) -> None:
        _text(self.value, "named-count value", limit=128)
        _integer(self.count, "named-count count", maximum=1_000_000)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "count": self.count}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"value", "count"}, path)
        return cls(
            value=_text(item["value"], f"{path}.value", limit=128),
            count=_integer(item["count"], f"{path}.count", maximum=1_000_000),
        )


@dataclass(frozen=True)
class EvidenceValueCount:
    """Count for a bounded, evaluator-provided evidence value."""

    value: str
    count: int

    def __post_init__(self) -> None:
        _text(self.value, "evidence-value count value")
        _integer(self.count, "evidence-value count", minimum=1, maximum=60)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "count": self.count}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"value", "count"}, path)
        return cls(
            value=_text(item["value"], f"{path}.value"),
            count=_integer(item["count"], f"{path}.count", minimum=1, maximum=60),
        )


def _validate_count_set(
    items: tuple[NamedCount, ...],
    choices: Iterable[str],
    denominator: int,
    path: str,
) -> dict[str, int]:
    expected = frozenset(choices)
    values = [item.value for item in items]
    if len(values) != len(set(values)) or set(values) != expected:
        raise ValidationError(f"{path} does not contain its complete fixed taxonomy")
    result = {item.value: item.count for item in items}
    if sum(result.values()) != denominator:
        raise ValidationError(f"{path} counts do not sum to their denominator")
    return result


def _validate_evidence_distribution(
    items: tuple[EvidenceValueCount, ...],
    denominator: int,
    missing_count: int,
    path: str,
) -> None:
    if len(items) > denominator:
        raise ValidationError(f"{path} has more values than its denominator")
    _unique(items, lambda item: item.value, path)
    if sum(item.count for item in items) + missing_count != denominator:
        raise ValidationError(f"{path} counts do not sum to their denominator")


@dataclass(frozen=True)
class NumericSummary:
    observed_count: int
    missing_count: int
    sum_value: float | None
    minimum: float | None
    maximum: float | None
    mean: float | None

    def __post_init__(self) -> None:
        _integer(self.observed_count, "numeric summary observed count", maximum=60)
        _integer(self.missing_count, "numeric summary missing count", maximum=60)
        values = (self.sum_value, self.minimum, self.maximum, self.mean)
        if self.observed_count == 0:
            if any(value is not None for value in values):
                raise ValidationError("empty numeric summary cannot contain values")
            return
        if (
            self.sum_value is None
            or self.minimum is None
            or self.maximum is None
            or self.mean is None
        ):
            raise ValidationError("observed numeric summary needs all values")
        for value, label in (
            (self.sum_value, "sum"),
            (self.minimum, "minimum"),
            (self.maximum, "maximum"),
            (self.mean, "mean"),
        ):
            _number(value, f"numeric summary {label}", minimum=0.0)
        if not self.minimum <= self.mean <= self.maximum:
            raise ValidationError("numeric summary mean is outside its range")
        if not math.isclose(
            self.sum_value,
            self.mean * self.observed_count,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValidationError("numeric summary mean does not match its sum")

    def validate_denominator(self, denominator: int, path: str) -> None:
        if self.observed_count + self.missing_count != denominator:
            raise ValidationError(f"{path} counts do not sum to their denominator")

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_count": self.observed_count,
            "missing_count": self.missing_count,
            "sum": self.sum_value,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {"observed_count", "missing_count", "sum", "minimum", "maximum", "mean"},
            path,
        )
        return cls(
            observed_count=_integer(
                item["observed_count"], f"{path}.observed_count", maximum=60
            ),
            missing_count=_integer(
                item["missing_count"], f"{path}.missing_count", maximum=60
            ),
            sum_value=_optional_number(item["sum"], f"{path}.sum", minimum=0.0),
            minimum=_optional_number(item["minimum"], f"{path}.minimum", minimum=0.0),
            maximum=_optional_number(item["maximum"], f"{path}.maximum", minimum=0.0),
            mean=_optional_number(item["mean"], f"{path}.mean", minimum=0.0),
        )


@dataclass(frozen=True)
class ReviewAggregate:
    denominator: int
    outcomes: tuple[NamedCount, ...]
    corrections_required: int
    corrections_present: int
    modifications: NumericSummary
    active_minutes: NumericSummary

    def __post_init__(self) -> None:
        _integer(self.denominator, "review denominator", maximum=60)
        outcomes = _validate_count_set(
            self.outcomes, REVIEW_OUTCOMES, self.denominator, "review outcomes"
        )
        reviewed = self.denominator - outcomes["not_reviewed"]
        _integer(
            self.corrections_required,
            "review corrections required",
            maximum=self.denominator,
        )
        _integer(
            self.corrections_present,
            "review corrections present",
            maximum=self.denominator,
        )
        if self.corrections_present > self.corrections_required:
            raise ValidationError(
                "review correction coverage exceeds required corrections"
            )
        if self.corrections_required > reviewed:
            raise ValidationError("unreviewed runs cannot require correction artifacts")
        self.modifications.validate_denominator(
            self.denominator, "review modifications"
        )
        self.active_minutes.validate_denominator(
            self.denominator, "review active minutes"
        )
        if (
            self.modifications.observed_count != reviewed
            or self.active_minutes.observed_count != reviewed
        ):
            raise ValidationError(
                "review numeric coverage does not match reviewed outcomes"
            )
        modification_total = self.modifications.sum_value or 0.0
        modification_extents = (
            modification_total,
            self.modifications.minimum or 0.0,
            self.modifications.maximum or 0.0,
        )
        if any(not value.is_integer() for value in modification_extents):
            raise ValidationError("review modification counts must be integers")
        if self.corrections_required < outcomes["pass_after_changes"]:
            raise ValidationError(
                "changed review outcomes require correction artifacts"
            )
        if self.corrections_required > modification_total:
            raise ValidationError(
                "correction coverage exceeds recorded modification decisions"
            )
        if modification_total and self.corrections_required == 0:
            raise ValidationError(
                "recorded modification decisions require correction coverage"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "denominator": self.denominator,
            "outcomes": [item.to_dict() for item in self.outcomes],
            "corrections_required": self.corrections_required,
            "corrections_present": self.corrections_present,
            "modifications": self.modifications.to_dict(),
            "active_minutes": self.active_minutes.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "denominator",
                "outcomes",
                "corrections_required",
                "corrections_present",
                "modifications",
                "active_minutes",
            },
            path,
        )
        return cls(
            denominator=_integer(
                item["denominator"], f"{path}.denominator", maximum=60
            ),
            outcomes=_array(
                item["outcomes"],
                f"{path}.outcomes",
                NamedCount.from_dict,
                minimum=len(REVIEW_OUTCOMES),
                maximum=len(REVIEW_OUTCOMES),
            ),
            corrections_required=_integer(
                item["corrections_required"],
                f"{path}.corrections_required",
                maximum=60,
            ),
            corrections_present=_integer(
                item["corrections_present"],
                f"{path}.corrections_present",
                maximum=60,
            ),
            modifications=NumericSummary.from_dict(
                item["modifications"], f"{path}.modifications"
            ),
            active_minutes=NumericSummary.from_dict(
                item["active_minutes"], f"{path}.active_minutes"
            ),
        )


@dataclass(frozen=True)
class FailureAggregate:
    failed_runs: int
    stages: tuple[FailureStageCount, ...]
    causes: tuple[NamedCount, ...]
    owners: tuple[NamedCount, ...]

    def __post_init__(self) -> None:
        _integer(self.failed_runs, "failure aggregate failed runs", maximum=60)
        stage_values = [item.stage for item in self.stages]
        if (
            len(stage_values) != len(set(stage_values))
            or set(stage_values) != FAILURE_STAGE_VALUES
        ):
            raise ValidationError("failure stages do not contain the fixed taxonomy")
        if sum(item.count for item in self.stages) != self.failed_runs:
            raise ValidationError("failure stage counts do not match failed runs")
        for items, choices, label in (
            (self.causes, FAILURE_CAUSES, "failure causes"),
            (self.owners, FAILURE_OWNERS, "failure owners"),
        ):
            values = [item.value for item in items]
            if len(values) != len(set(values)) or set(values) != choices:
                raise ValidationError(f"{label} do not contain the fixed taxonomy")
            total = sum(item.count for item in items)
            if self.failed_runs == 0 and total != 0:
                raise ValidationError(f"{label} exist without failed runs")
            if (
                self.failed_runs
                and not self.failed_runs <= total <= self.failed_runs * len(choices)
            ):
                raise ValidationError(f"{label} counts cannot cover failed runs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_runs": self.failed_runs,
            "stages": [item.to_dict() for item in self.stages],
            "causes": [item.to_dict() for item in self.causes],
            "owners": [item.to_dict() for item in self.owners],
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"failed_runs", "stages", "causes", "owners"}, path)
        return cls(
            failed_runs=_integer(
                item["failed_runs"], f"{path}.failed_runs", maximum=60
            ),
            stages=_array(
                item["stages"],
                f"{path}.stages",
                FailureStageCount.from_dict,
                minimum=len(FAILURE_STAGE_VALUES),
                maximum=len(FAILURE_STAGE_VALUES),
            ),
            causes=_array(
                item["causes"],
                f"{path}.causes",
                NamedCount.from_dict,
                minimum=len(FAILURE_CAUSES),
                maximum=len(FAILURE_CAUSES),
            ),
            owners=_array(
                item["owners"],
                f"{path}.owners",
                NamedCount.from_dict,
                minimum=len(FAILURE_OWNERS),
                maximum=len(FAILURE_OWNERS),
            ),
        )


@dataclass(frozen=True)
class EfficiencyAggregate:
    denominator: int
    token_statuses: tuple[NamedCount, ...]
    token_sources: tuple[NamedCount, ...]
    cost_statuses: tuple[NamedCount, ...]
    cost_sources: tuple[EvidenceValueCount, ...]
    model_requests: NumericSummary
    total_tokens: NumericSummary
    cost_amount: NumericSummary
    cost_currency: str | None
    pcb_tool_calls: NumericSummary
    tool_call_counts: tuple[ToolCallCount, ...]
    provider_retries: NumericSummary
    provider_errors: NumericSummary
    tool_seconds: NumericSummary
    api_seconds: NumericSummary
    wall_seconds: NumericSummary
    failure_reasons: tuple[EvidenceValueCount, ...]
    missing_failure_reason_count: int

    def __post_init__(self) -> None:
        _integer(self.denominator, "efficiency denominator", maximum=60)
        token_statuses = _validate_count_set(
            self.token_statuses,
            TOKEN_STATUSES,
            self.denominator,
            "efficiency token statuses",
        )
        token_sources = _validate_count_set(
            self.token_sources,
            TOKEN_SOURCES,
            self.denominator,
            "efficiency token sources",
        )
        cost_statuses = _validate_count_set(
            self.cost_statuses,
            COST_STATUSES,
            self.denominator,
            "efficiency cost statuses",
        )
        _validate_evidence_distribution(
            self.cost_sources,
            self.denominator,
            0,
            "efficiency cost sources",
        )
        for source in self.cost_sources:
            _text(source.value, "efficiency cost source", limit=512)
        for summary, label in (
            (self.model_requests, "model requests"),
            (self.total_tokens, "total tokens"),
            (self.cost_amount, "cost amount"),
            (self.pcb_tool_calls, "PCB tool calls"),
            (self.provider_retries, "provider retries"),
            (self.provider_errors, "provider errors"),
            (self.tool_seconds, "tool seconds"),
            (self.api_seconds, "API seconds"),
            (self.wall_seconds, "wall seconds"),
        ):
            summary.validate_denominator(self.denominator, f"efficiency {label}")
        for summary, label in (
            (self.provider_retries, "provider retries"),
            (self.provider_errors, "provider errors"),
        ):
            if summary.observed_count and any(
                not value.is_integer()
                for value in (
                    summary.sum_value,
                    summary.minimum,
                    summary.maximum,
                )
                if value is not None
            ):
                raise ValidationError(f"efficiency {label} must contain integers")
        complete_tokens = token_statuses["reported"] + token_statuses["derived"]
        known_tokens = complete_tokens + token_statuses["partial"]
        if not complete_tokens <= self.total_tokens.observed_count <= known_tokens:
            raise ValidationError("total-token coverage contradicts token statuses")
        if token_sources[_EVIDENCE_UNAVAILABLE] != token_statuses[_EVIDENCE_UNKNOWN]:
            raise ValidationError("token-source coverage contradicts token statuses")
        priced = cost_statuses["actual"] + cost_statuses["estimated"]
        if self.cost_amount.observed_count != priced:
            raise ValidationError("cost amount coverage contradicts cost statuses")
        _optional_text(self.cost_currency, "efficiency aggregate currency", limit=16)
        if (priced == 0) != (self.cost_currency is None):
            raise ValidationError("cost currency presence contradicts priced runs")
        if len(self.tool_call_counts) > 512:
            raise ValidationError("aggregate PCB tool-call breakdown is oversized")
        pairs = [(item.name, item.status) for item in self.tool_call_counts]
        if len(pairs) != len(set(pairs)):
            raise ValidationError("aggregate PCB tool-call breakdown has duplicates")
        tool_sum = self.pcb_tool_calls.sum_value or 0.0
        if not math.isclose(
            float(sum(item.count for item in self.tool_call_counts)), tool_sum
        ):
            raise ValidationError(
                "aggregate PCB tool-call breakdown does not match sum"
            )
        _integer(
            self.missing_failure_reason_count,
            "missing failure-reason count",
            maximum=self.denominator,
        )
        _validate_evidence_distribution(
            self.failure_reasons,
            self.denominator,
            self.missing_failure_reason_count,
            "efficiency failure reasons",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "denominator": self.denominator,
            "token_statuses": [item.to_dict() for item in self.token_statuses],
            "token_sources": [item.to_dict() for item in self.token_sources],
            "cost_statuses": [item.to_dict() for item in self.cost_statuses],
            "cost_sources": [item.to_dict() for item in self.cost_sources],
            "model_requests": self.model_requests.to_dict(),
            "total_tokens": self.total_tokens.to_dict(),
            "cost_amount": self.cost_amount.to_dict(),
            "cost_currency": self.cost_currency,
            "pcb_tool_calls": self.pcb_tool_calls.to_dict(),
            "tool_call_counts": [item.to_dict() for item in self.tool_call_counts],
            "provider_retries": self.provider_retries.to_dict(),
            "provider_errors": self.provider_errors.to_dict(),
            "tool_seconds": self.tool_seconds.to_dict(),
            "api_seconds": self.api_seconds.to_dict(),
            "wall_seconds": self.wall_seconds.to_dict(),
            "failure_reasons": [item.to_dict() for item in self.failure_reasons],
            "missing_failure_reason_count": self.missing_failure_reason_count,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "denominator",
                "token_statuses",
                "token_sources",
                "cost_statuses",
                "cost_sources",
                "model_requests",
                "total_tokens",
                "cost_amount",
                "cost_currency",
                "pcb_tool_calls",
                "tool_call_counts",
                "provider_retries",
                "provider_errors",
                "tool_seconds",
                "api_seconds",
                "wall_seconds",
                "failure_reasons",
                "missing_failure_reason_count",
            },
            path,
        )
        return cls(
            denominator=_integer(
                item["denominator"], f"{path}.denominator", maximum=60
            ),
            token_statuses=_array(
                item["token_statuses"],
                f"{path}.token_statuses",
                NamedCount.from_dict,
                minimum=len(TOKEN_STATUSES),
                maximum=len(TOKEN_STATUSES),
            ),
            token_sources=_array(
                item["token_sources"],
                f"{path}.token_sources",
                NamedCount.from_dict,
                minimum=len(TOKEN_SOURCES),
                maximum=len(TOKEN_SOURCES),
            ),
            cost_statuses=_array(
                item["cost_statuses"],
                f"{path}.cost_statuses",
                NamedCount.from_dict,
                minimum=len(COST_STATUSES),
                maximum=len(COST_STATUSES),
            ),
            cost_sources=_array(
                item["cost_sources"],
                f"{path}.cost_sources",
                EvidenceValueCount.from_dict,
                minimum=1,
                maximum=60,
            ),
            model_requests=NumericSummary.from_dict(
                item["model_requests"], f"{path}.model_requests"
            ),
            total_tokens=NumericSummary.from_dict(
                item["total_tokens"], f"{path}.total_tokens"
            ),
            cost_amount=NumericSummary.from_dict(
                item["cost_amount"], f"{path}.cost_amount"
            ),
            cost_currency=_optional_text(
                item["cost_currency"], f"{path}.cost_currency", limit=16
            ),
            pcb_tool_calls=NumericSummary.from_dict(
                item["pcb_tool_calls"], f"{path}.pcb_tool_calls"
            ),
            tool_call_counts=_array(
                item["tool_call_counts"],
                f"{path}.tool_call_counts",
                ToolCallCount.from_dict,
                maximum=512,
            ),
            provider_retries=NumericSummary.from_dict(
                item["provider_retries"], f"{path}.provider_retries"
            ),
            provider_errors=NumericSummary.from_dict(
                item["provider_errors"], f"{path}.provider_errors"
            ),
            tool_seconds=NumericSummary.from_dict(
                item["tool_seconds"], f"{path}.tool_seconds"
            ),
            api_seconds=NumericSummary.from_dict(
                item["api_seconds"], f"{path}.api_seconds"
            ),
            wall_seconds=NumericSummary.from_dict(
                item["wall_seconds"], f"{path}.wall_seconds"
            ),
            failure_reasons=_array(
                item["failure_reasons"],
                f"{path}.failure_reasons",
                EvidenceValueCount.from_dict,
                maximum=60,
            ),
            missing_failure_reason_count=_integer(
                item["missing_failure_reason_count"],
                f"{path}.missing_failure_reason_count",
                maximum=60,
            ),
        )


@dataclass(frozen=True)
class PhysicalMetricAggregate:
    name: str
    outcomes: tuple[NamedCount, ...]

    def __post_init__(self) -> None:
        _choice(self.name, PHYSICAL_METRICS, "physical metric aggregate name")

    def validate_denominator(self, denominator: int, path: str) -> dict[str, int]:
        return _validate_count_set(self.outcomes, PHYSICAL_STATES, denominator, path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outcomes": [item.to_dict() for item in self.outcomes],
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"name", "outcomes"}, path)
        return cls(
            name=_choice(item["name"], PHYSICAL_METRICS, f"{path}.name"),
            outcomes=_array(
                item["outcomes"],
                f"{path}.outcomes",
                NamedCount.from_dict,
                minimum=len(PHYSICAL_STATES),
                maximum=len(PHYSICAL_STATES),
            ),
        )


@dataclass(frozen=True)
class PhysicalAggregate:
    records: int
    metrics: tuple[PhysicalMetricAggregate, ...]
    revision_counts: NumericSummary

    def __post_init__(self) -> None:
        _integer(self.records, "physical record count", maximum=60)
        if len(self.metrics) != len(PHYSICAL_METRICS):
            raise ValidationError("physical aggregate metric set is incomplete")
        _unique(self.metrics, lambda item: item.name, "physical aggregate metrics")
        if {item.name for item in self.metrics} != set(PHYSICAL_METRICS):
            raise ValidationError("physical aggregate metric set is incomplete")
        for metric in self.metrics:
            metric.validate_denominator(
                self.records, f"physical aggregate {metric.name} outcomes"
            )
        self.revision_counts.validate_denominator(
            self.records, "physical revision counts"
        )
        if self.revision_counts.observed_count != self.records:
            raise ValidationError("every physical record needs a revision count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "metrics": [item.to_dict() for item in self.metrics],
            "revision_counts": self.revision_counts.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"records", "metrics", "revision_counts"}, path)
        return cls(
            records=_integer(item["records"], f"{path}.records", maximum=60),
            metrics=_array(
                item["metrics"],
                f"{path}.metrics",
                PhysicalMetricAggregate.from_dict,
                minimum=len(PHYSICAL_METRICS),
                maximum=len(PHYSICAL_METRICS),
            ),
            revision_counts=NumericSummary.from_dict(
                item["revision_counts"], f"{path}.revision_counts"
            ),
        )


@dataclass(frozen=True)
class ReportSlice:
    scope: str
    id: str
    category: str | None
    planned_runs: int
    terminal_runs: int
    automatic_outcomes: tuple[NamedCount, ...]
    automatic_metrics: tuple[MetricAggregate, ...]
    reviews: ReviewAggregate
    failures: FailureAggregate
    efficiency: EfficiencyAggregate
    physical: PhysicalAggregate

    def __post_init__(self) -> None:
        _choice(self.scope, REPORT_SCOPES, "report slice scope")
        if self.scope == "overall":
            if (
                self.id != "overall"
                or self.category is not None
                or self.planned_runs != 60
            ):
                raise ValidationError("overall report slice identity is invalid")
        elif self.scope == "category":
            if (
                self.id not in BOARD_CATEGORIES
                or self.category != self.id
                or self.planned_runs != 12
            ):
                raise ValidationError("category report slice identity is invalid")
        else:
            _identity(self.id, "case report slice id")
            if self.category not in BOARD_CATEGORIES or self.planned_runs != 3:
                raise ValidationError("case report slice identity is invalid")
        _integer(
            self.terminal_runs, "report slice terminal runs", maximum=self.planned_runs
        )
        outcomes = _validate_count_set(
            self.automatic_outcomes,
            AUTOMATIC_OUTCOMES,
            self.planned_runs,
            "automatic outcomes",
        )
        if len(self.automatic_metrics) != len(AUTOMATIC_METRICS):
            raise ValidationError("report slice automatic metrics are incomplete")
        _unique(self.automatic_metrics, lambda item: item.name, "slice metrics")
        if {item.name for item in self.automatic_metrics} != set(AUTOMATIC_METRICS):
            raise ValidationError("report slice automatic metrics are incomplete")
        if any(item.total != self.planned_runs for item in self.automatic_metrics):
            raise ValidationError("report slice metric denominator is invalid")
        if self.reviews.denominator != self.planned_runs:
            raise ValidationError("report slice review denominator is invalid")
        reviewed = self.planned_runs - next(
            item.count for item in self.reviews.outcomes if item.value == "not_reviewed"
        )
        if reviewed > self.terminal_runs:
            raise ValidationError("report slice has reviews for nonterminal runs")
        review_outcomes = {item.value: item.count for item in self.reviews.outcomes}
        automatic_failures = outcomes["fail"] + outcomes["unknown"]
        review_failures = (
            review_outcomes["pass_after_changes"]
            + review_outcomes["fail"]
            + review_outcomes["not_applicable"]
        )
        minimum_failures = max(automatic_failures, review_failures)
        maximum_failures = min(self.planned_runs, automatic_failures + review_failures)
        if not minimum_failures <= self.failures.failed_runs <= maximum_failures:
            raise ValidationError("report slice failure coverage contradicts outcomes")
        if self.efficiency.denominator != self.planned_runs:
            raise ValidationError("report slice efficiency denominator is invalid")
        if self.physical.records > self.planned_runs:
            raise ValidationError("report slice physical records exceed planned runs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "id": self.id,
            "category": self.category,
            "planned_runs": self.planned_runs,
            "terminal_runs": self.terminal_runs,
            "automatic_outcomes": [item.to_dict() for item in self.automatic_outcomes],
            "automatic_metrics": [item.to_dict() for item in self.automatic_metrics],
            "reviews": self.reviews.to_dict(),
            "failures": self.failures.to_dict(),
            "efficiency": self.efficiency.to_dict(),
            "physical": self.physical.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "scope",
                "id",
                "category",
                "planned_runs",
                "terminal_runs",
                "automatic_outcomes",
                "automatic_metrics",
                "reviews",
                "failures",
                "efficiency",
                "physical",
            },
            path,
        )
        category = item["category"]
        return cls(
            scope=_choice(item["scope"], REPORT_SCOPES, f"{path}.scope"),
            id=_text(item["id"], f"{path}.id", limit=128),
            category=(
                None
                if category is None
                else _choice(category, BOARD_CATEGORIES, f"{path}.category")
            ),
            planned_runs=_integer(
                item["planned_runs"], f"{path}.planned_runs", minimum=1, maximum=60
            ),
            terminal_runs=_integer(
                item["terminal_runs"], f"{path}.terminal_runs", maximum=60
            ),
            automatic_outcomes=_array(
                item["automatic_outcomes"],
                f"{path}.automatic_outcomes",
                NamedCount.from_dict,
                minimum=len(AUTOMATIC_OUTCOMES),
                maximum=len(AUTOMATIC_OUTCOMES),
            ),
            automatic_metrics=_array(
                item["automatic_metrics"],
                f"{path}.automatic_metrics",
                MetricAggregate.from_dict,
                minimum=len(AUTOMATIC_METRICS),
                maximum=len(AUTOMATIC_METRICS),
            ),
            reviews=ReviewAggregate.from_dict(item["reviews"], f"{path}.reviews"),
            failures=FailureAggregate.from_dict(item["failures"], f"{path}.failures"),
            efficiency=EfficiencyAggregate.from_dict(
                item["efficiency"], f"{path}.efficiency"
            ),
            physical=PhysicalAggregate.from_dict(item["physical"], f"{path}.physical"),
        )


def _numeric_summaries(item: ReportSlice) -> dict[str, NumericSummary]:
    return {
        "reviews.modifications": item.reviews.modifications,
        "reviews.active_minutes": item.reviews.active_minutes,
        "efficiency.model_requests": item.efficiency.model_requests,
        "efficiency.total_tokens": item.efficiency.total_tokens,
        "efficiency.cost_amount": item.efficiency.cost_amount,
        "efficiency.pcb_tool_calls": item.efficiency.pcb_tool_calls,
        "efficiency.provider_retries": item.efficiency.provider_retries,
        "efficiency.provider_errors": item.efficiency.provider_errors,
        "efficiency.tool_seconds": item.efficiency.tool_seconds,
        "efficiency.api_seconds": item.efficiency.api_seconds,
        "efficiency.wall_seconds": item.efficiency.wall_seconds,
        "physical.revision_counts": item.physical.revision_counts,
    }


def _slice_additive_values(item: ReportSlice) -> dict[str, float]:
    result: dict[str, float] = {
        "planned_runs": float(item.planned_runs),
        "terminal_runs": float(item.terminal_runs),
        "reviews.corrections_required": float(item.reviews.corrections_required),
        "reviews.corrections_present": float(item.reviews.corrections_present),
        "failures.failed_runs": float(item.failures.failed_runs),
        "physical.records": float(item.physical.records),
        "efficiency.missing_failure_reason_count": float(
            item.efficiency.missing_failure_reason_count
        ),
    }
    for prefix, counts in (
        ("automatic_outcomes", item.automatic_outcomes),
        ("reviews.outcomes", item.reviews.outcomes),
        ("failures.causes", item.failures.causes),
        ("failures.owners", item.failures.owners),
        ("efficiency.token_statuses", item.efficiency.token_statuses),
        ("efficiency.token_sources", item.efficiency.token_sources),
        ("efficiency.cost_statuses", item.efficiency.cost_statuses),
    ):
        for named_count in counts:
            result[f"{prefix}.{named_count.value}"] = float(named_count.count)
    for prefix, evidence_counts in (
        ("efficiency.cost_sources", item.efficiency.cost_sources),
        ("efficiency.failure_reasons", item.efficiency.failure_reasons),
    ):
        for value_count in evidence_counts:
            result[f"{prefix}.{value_count.value}"] = float(value_count.count)
    for auto_metric in item.automatic_metrics:
        for state, state_count in (
            ("pass", auto_metric.passed),
            ("fail", auto_metric.failed),
            ("unknown", auto_metric.unknown),
            ("not_applicable", auto_metric.not_applicable),
        ):
            result[f"automatic_metrics.{auto_metric.name}.{state}"] = float(state_count)
    for stage_count in item.failures.stages:
        result[f"failures.stages.{stage_count.stage}"] = float(stage_count.count)
    for tool_count in item.efficiency.tool_call_counts:
        result[f"efficiency.tools.{tool_count.name}.{tool_count.status}"] = float(
            tool_count.count
        )
    for physical_metric in item.physical.metrics:
        for outcome_count in physical_metric.outcomes:
            result[f"physical.{physical_metric.name}.{outcome_count.value}"] = float(
                outcome_count.count
            )
    for name, summary in _numeric_summaries(item).items():
        result[f"{name}.observed_count"] = float(summary.observed_count)
        result[f"{name}.missing_count"] = float(summary.missing_count)
        result[f"{name}.sum"] = summary.sum_value or 0.0
    return result


def _assert_slice_sum(
    parent: ReportSlice, children: tuple[ReportSlice, ...], label: str
) -> None:
    parent_values = _slice_additive_values(parent)
    child_values: dict[str, float] = {}
    for child in children:
        for key, value in _slice_additive_values(child).items():
            child_values[key] = child_values.get(key, 0.0) + value
    for key in parent_values.keys() | child_values.keys():
        if not math.isclose(
            parent_values.get(key, 0.0),
            child_values.get(key, 0.0),
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValidationError(f"report {label} slice does not sum at {key}")
    child_summaries = [_numeric_summaries(child) for child in children]
    for name, summary in _numeric_summaries(parent).items():
        observed = [
            values[name]
            for values in child_summaries
            if values[name].observed_count > 0
        ]
        expected_minimum = min(
            (value.minimum for value in observed if value.minimum is not None),
            default=None,
        )
        expected_maximum = max(
            (value.maximum for value in observed if value.maximum is not None),
            default=None,
        )
        if summary.minimum != expected_minimum or summary.maximum != expected_maximum:
            raise ValidationError(f"report {label} slice range does not sum at {name}")
    child_currencies = {
        child.efficiency.cost_currency
        for child in children
        if child.efficiency.cost_currency is not None
    }
    if len(child_currencies) > 1:
        raise ValidationError(f"report {label} slice mixes cost currencies")
    expected_currency = next(iter(child_currencies), None)
    if parent.efficiency.cost_currency != expected_currency:
        raise ValidationError(f"report {label} slice cost currency is inconsistent")


def _validate_sealed_report(
    overall: ReportSlice, categories: tuple[ReportSlice, ...]
) -> None:
    review_outcomes = {item.value: item.count for item in overall.reviews.outcomes}
    stages = {item.stage: item.count for item in overall.failures.stages}
    causes = {item.value: item.count for item in overall.failures.causes}
    owners = {item.value: item.count for item in overall.failures.owners}
    if overall.terminal_runs != 60 or review_outcomes["not_reviewed"] != 0:
        raise ValidationError("sealed report has missing terminal runs or reviews")
    if overall.reviews.corrections_required != overall.reviews.corrections_present:
        raise ValidationError("sealed report has missing correction artifacts")
    if (
        stages["unclassified"] != 0
        or causes["unclassified"] != 0
        or owners["unclassified"] != 0
    ):
        raise ValidationError("sealed report has unclassified failures")
    if overall.physical.records < 5 or any(
        category.physical.records < 1 for category in categories
    ):
        raise ValidationError("sealed report lacks five-category physical evidence")
    for slice_item in (overall, *categories):
        for metric in slice_item.physical.metrics:
            outcomes = {item.value: item.count for item in metric.outcomes}
            if outcomes["not_tested"]:
                raise ValidationError(
                    "sealed report contains untested physical outcomes"
                )
            if metric.name != "firmware_download" and outcomes["not_applicable"]:
                raise ValidationError("sealed report omits a required physical outcome")


@dataclass(frozen=True)
class BoardBenchReport:
    schema: ClassVar[str] = REPORT_SCHEMA

    campaign_id: str
    campaign_sha256: str
    cohort: str
    evaluator_version: str
    generated_at: str
    slices: tuple[ReportSlice, ...]
    sealed: bool

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "report campaign id")
        _sha256(self.campaign_sha256, "report campaign hash")
        _choice(self.cohort, COHORTS, "report cohort")
        _text(self.evaluator_version, "report evaluator version", limit=512)
        _timestamp(self.generated_at, "report generated_at")
        if not isinstance(self.sealed, bool):
            raise ValidationError("report sealed must be a boolean")
        if len(self.slices) != 26:
            raise ValidationError("BoardBench report requires 26 fixed slices")
        keys = [(item.scope, item.id) for item in self.slices]
        if len(keys) != len(set(keys)):
            raise ValidationError("BoardBench report contains duplicate slices")
        overall = [item for item in self.slices if item.scope == "overall"]
        categories = [item for item in self.slices if item.scope == "category"]
        cases = [item for item in self.slices if item.scope == "case"]
        if len(overall) != 1:
            raise ValidationError("BoardBench report requires one overall slice")
        if {item.id for item in categories} != set(BOARD_CATEGORIES):
            raise ValidationError("BoardBench report category slices are incomplete")
        if len(cases) != 20 or len({item.id for item in cases}) != 20:
            raise ValidationError("BoardBench report requires 20 unique case slices")
        case_categories = Counter(item.category for item in cases)
        if case_categories != Counter({category: 4 for category in BOARD_CATEGORIES}):
            raise ValidationError(
                "BoardBench report requires four case slices per category"
            )
        category_by_id = {item.id: item for item in categories}
        for category in BOARD_CATEGORIES:
            _assert_slice_sum(
                category_by_id[category],
                tuple(item for item in cases if item.category == category),
                f"category {category}",
            )
        _assert_slice_sum(overall[0], tuple(categories), "overall")
        if self.sealed:
            if self.cohort == AI_REVIEWED_PILOT_COHORT:
                raise ValidationError(
                    "AI-reviewed pilot reports cannot claim a sealed "
                    "BoardBench baseline"
                )
            _validate_sealed_report(overall[0], tuple(categories))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": ARTIFACT_VERSION,
            "campaign_id": self.campaign_id,
            "campaign_sha256": self.campaign_sha256,
            "cohort": self.cohort,
            "evaluator_version": self.evaluator_version,
            "generated_at": self.generated_at,
            "slices": [item.to_dict() for item in self.slices],
            "sealed": self.sealed,
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "campaign_id",
            "campaign_sha256",
            "cohort",
            "evaluator_version",
            "generated_at",
            "slices",
            "sealed",
        }
        item = _schema(value, schema=cls.schema, fields=fields, path=path)
        if not isinstance(item["sealed"], bool):
            raise ValidationError(f"{path}.sealed must be a boolean")
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            campaign_sha256=_sha256(item["campaign_sha256"], f"{path}.campaign_sha256"),
            cohort=_choice(item["cohort"], COHORTS, f"{path}.cohort"),
            evaluator_version=_text(
                item["evaluator_version"], f"{path}.evaluator_version", limit=512
            ),
            generated_at=_timestamp(item["generated_at"], f"{path}.generated_at"),
            slices=_array(
                item["slices"],
                f"{path}.slices",
                ReportSlice.from_dict,
                minimum=26,
                maximum=26,
            ),
            sealed=item["sealed"],
        )


BoardBenchArtifact = (
    BoardBenchCorpus
    | BoardBenchCampaign
    | BoardBenchRun
    | BoardBenchScore
    | BoardBenchReview
    | BoardBenchCorrection
    | BoardBenchHardware
    | BoardBenchSelection
    | BoardBenchReport
)

_ARTIFACT_TYPES: dict[str, type[BoardBenchArtifact]] = {
    CORPUS_SCHEMA: BoardBenchCorpus,
    CAMPAIGN_SCHEMA: BoardBenchCampaign,
    RUN_SCHEMA: BoardBenchRun,
    SCORE_SCHEMA: BoardBenchScore,
    REVIEW_SCHEMA: BoardBenchReview,
    CORRECTION_SCHEMA: BoardBenchCorrection,
    HARDWARE_SCHEMA: BoardBenchHardware,
    SELECTION_SCHEMA: BoardBenchSelection,
    REPORT_SCHEMA: BoardBenchReport,
}


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    absolute = path.expanduser().absolute()
    members = absolute.parents
    for parent in reversed(members):
        if parent.exists() and parent.is_symlink():
            raise ValidationError(f"BoardBench path traverses a symlink: {parent}")
    if include_leaf and absolute.is_symlink():
        raise ValidationError(f"BoardBench artifact must not be a symlink: {path}")


def _regular_file_identity(path: Path, label: str) -> tuple[int, int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValidationError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValidationError(f"{label} must be a single-link regular file: {path}")
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _json_object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_document(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    _reject_symlink_components(source)
    before_identity = _regular_file_identity(source, "BoardBench artifact")
    # The schema, not an operator-controlled filename, decides which of the two
    # bounds applies. The initial read remains capped at the larger one.
    try:
        text = read_text_limited(source, CORPUS_FILE_LIMIT)
    except PCBDraftError as exc:
        raise ValidationError(
            f"cannot load BoardBench artifact {source.name}: {exc}"
        ) from exc
    _reject_symlink_components(source)
    if _regular_file_identity(source, "BoardBench artifact") != before_identity:
        raise ValidationError("BoardBench artifact changed while it was being read")
    byte_length = len(text.encode("utf-8"))

    try:

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON number: {value}")

        value = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        if byte_length > ARTIFACT_FILE_LIMIT:
            raise ValidationError(
                f"BoardBench artifact exceeds {ARTIFACT_FILE_LIMIT} byte limit"
            ) from exc
        raise ValidationError(
            f"cannot load BoardBench artifact {source.name}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ValidationError("BoardBench artifact root must be a JSON object")
    if value.get("schema") != CORPUS_SCHEMA and byte_length > ARTIFACT_FILE_LIMIT:
        raise ValidationError(
            f"BoardBench artifact exceeds {ARTIFACT_FILE_LIMIT} byte limit"
        )
    return value


def load_artifact(path: str | Path) -> BoardBenchArtifact:
    """Load one recognized BoardBench artifact through a bounded strict reader."""
    document = _load_document(path)
    schema = document.get("schema")
    if schema == "pcbdraft-boardbench-run-v2":
        # New runs are stored only as v2.  Legacy consumers receive an explicit
        # in-memory projection; the v2 artifact itself is never rewritten.
        from pcbdraft.verification.boardbench_v2 import BoardBenchRunV2

        return BoardBenchRunV2.from_dict(document).to_legacy()
    if not isinstance(schema, str) or schema not in _ARTIFACT_SCHEMAS:
        raise ValidationError("unknown BoardBench artifact schema")
    artifact_type = _ARTIFACT_TYPES[schema]
    return artifact_type.from_dict(document)  # type: ignore[union-attr]


A = TypeVar("A", bound=BoardBenchArtifact)


def _load_expected(path: str | Path, artifact_type: type[A]) -> A:
    artifact = load_artifact(path)
    if not isinstance(artifact, artifact_type):
        raise ValidationError(f"expected {artifact_type.schema} artifact")
    return artifact


def load_corpus(path: str | Path) -> BoardBenchCorpus:
    return _load_expected(path, BoardBenchCorpus)


def load_campaign(path: str | Path) -> BoardBenchCampaign:
    return _load_expected(path, BoardBenchCampaign)


def load_run(path: str | Path) -> BoardBenchRun:
    artifact = load_artifact(path)
    if not isinstance(artifact, BoardBenchRun):
        raise ValidationError(f"expected {BoardBenchRun.schema} artifact")
    return artifact


def load_score(path: str | Path) -> BoardBenchScore:
    return _load_expected(path, BoardBenchScore)


def load_review(path: str | Path) -> BoardBenchReview:
    return _load_expected(path, BoardBenchReview)


def load_correction(path: str | Path) -> BoardBenchCorrection:
    return _load_expected(path, BoardBenchCorrection)


def load_hardware(path: str | Path) -> BoardBenchHardware:
    return _load_expected(path, BoardBenchHardware)


def load_selection(path: str | Path) -> BoardBenchSelection:
    return _load_expected(path, BoardBenchSelection)


def load_report(path: str | Path) -> BoardBenchReport:
    return _load_expected(path, BoardBenchReport)


def _prepare_artifact_target(path: str | Path) -> tuple[Path, tuple[int, int]]:
    raw = Path(path).expanduser()
    if raw.name in {"", ".", ".."}:
        raise ValidationError("BoardBench artifact filename is unsafe")
    _reject_symlink_components(raw)
    if raw.parent.exists():
        if not raw.parent.is_dir():
            raise ValidationError(
                f"BoardBench artifact parent is not a directory: {raw.parent}"
            )
    else:
        make_directory(raw.parent)
    _reject_symlink_components(raw)
    try:
        parent = raw.parent.resolve(strict=True)
        info = parent.stat()
    except OSError as exc:
        raise ValidationError("BoardBench artifact parent is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValidationError("BoardBench artifact parent is not a directory")
    return parent / raw.name, (info.st_dev, info.st_ino)


def _verify_artifact_parent(target: Path, identity: tuple[int, int]) -> None:
    _reject_symlink_components(target)
    try:
        parent = target.parent.resolve(strict=True)
        info = parent.stat()
    except OSError as exc:
        raise ValidationError(
            "BoardBench artifact parent changed during storage"
        ) from exc
    if parent != target.parent or (info.st_dev, info.st_ino) != identity:
        raise ValidationError("BoardBench artifact parent changed during storage")


def _artifact_lock_parent(target: Path) -> Path:
    return target.parent / BOARD_BENCH_LOCK_DIR


def _write_artifact_under_lock(
    target: Path, artifact: BoardBenchArtifact, *, overwrite: bool
) -> Path:
    _reject_symlink_components(target)
    if target.exists() or target.is_symlink():
        if not overwrite:
            raise ValidationError(f"BoardBench artifact already exists: {target.name}")
        _regular_file_identity(target, "BoardBench artifact target")
    atomic_write_json(target, artifact.to_dict(), mode=0o600)
    _regular_file_identity(target, "stored BoardBench artifact")
    return target


def _write_artifact(
    path: str | Path, artifact: BoardBenchArtifact, *, overwrite: bool
) -> Path:
    target, parent_identity = _prepare_artifact_target(path)
    lock_parent = _artifact_lock_parent(target)
    _reject_symlink_components(lock_parent)
    with ResourceLock(target, lock_parent, timeout=10.0):
        _verify_artifact_parent(target, parent_identity)
        result = _write_artifact_under_lock(target, artifact, overwrite=overwrite)
        _verify_artifact_parent(target, parent_identity)
        return result


def write_artifact(path: str | Path, artifact: BoardBenchArtifact) -> Path:
    """Atomically persist one private, write-once artifact."""
    return _write_artifact(path, artifact, overwrite=False)


def store_run(path: str | Path, run: BoardBenchRun) -> Path:
    """Legacy-only fixture/import writer; existing v1 receipts are read-only.

    New product runs use ``store_run_v2``.  The sole existing-v2 compatibility
    transition accepted here is planned-to-running for older in-memory callers.
    """
    target, parent_identity = _prepare_artifact_target(path)
    lock_parent = _artifact_lock_parent(target)
    _reject_symlink_components(lock_parent)
    with ResourceLock(target, lock_parent, timeout=10.0):
        _verify_artifact_parent(target, parent_identity)
        exists = target.exists() or target.is_symlink()
        if exists:
            document = _load_document(target)
            if document.get("schema") == "pcbdraft-boardbench-run-v2":
                # Compatibility-only transition used by legacy callers that
                # mark a newly planned v2 run as running.  Terminal v2 facts
                # require the authoritative runner normalizer, not inference
                # from a v1-shaped object.
                from pcbdraft.verification.boardbench_v2 import (
                    BoardBenchRunV2,
                    start_run_v2,
                )

                current_v2 = BoardBenchRunV2.from_dict(document)
                if (
                    run.campaign_id,
                    run.run_id,
                    run.case_id,
                    run.repetition,
                    run.prompt_sha256,
                ) != (
                    current_v2.campaign_id,
                    current_v2.run_id,
                    current_v2.case_id,
                    current_v2.repetition,
                    current_v2.prompt_sha256,
                ):
                    raise ValidationError(
                        "run v2 transition changes its immutable identity"
                    )
                if run.status != "running" or run.started_at is None:
                    raise ValidationError(
                        "terminal BoardBench run v2 needs authoritative outcome evidence"
                    )
                next_v2 = start_run_v2(current_v2, run.started_at)
                atomic_write_json(target, next_v2.to_dict(), mode=0o600)
                _verify_artifact_parent(target, parent_identity)
                return target
            raise ValidationError("legacy BoardBench v1 run is read-only")
        result = _write_artifact_under_lock(target, run, overwrite=exists)
        _verify_artifact_parent(target, parent_identity)
        return result


def allocate_campaign_directory(parent: str | Path, campaign_id: str) -> Path:
    """Allocate a fresh private campaign directory without following symlinks."""
    identifier = _identity(campaign_id, "campaign directory id")
    root = Path(parent).expanduser()
    _reject_symlink_components(root)
    if root.exists():
        if not root.is_dir():
            raise ValidationError(
                f"BoardBench campaign parent is not a directory: {root}"
            )
    else:
        make_directory(root)
    try:
        canonical_root = root.resolve(strict=True)
        root_info = canonical_root.stat()
    except OSError as exc:
        raise ValidationError("BoardBench campaign parent is unavailable") from exc
    target = canonical_root / identifier
    lock_parent = canonical_root / BOARD_BENCH_LOCK_DIR
    _reject_symlink_components(lock_parent)
    with ResourceLock(target, lock_parent, timeout=10.0):
        _reject_symlink_components(target)
        current = canonical_root.stat()
        if (current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino):
            raise ValidationError(
                "BoardBench campaign parent changed during allocation"
            )
        if target.exists() or target.is_symlink():
            raise ValidationError(
                f"BoardBench campaign directory already exists: {identifier}"
            )
        try:
            target.mkdir(mode=0o700)
            target.chmod(0o700)
        except OSError as exc:
            raise PCBDraftError(
                f"cannot allocate BoardBench campaign: {target}"
            ) from exc
        return target


def _hash_regular_file(
    path: Path, *, expected_size: int | None = None
) -> tuple[int, str]:
    _reject_symlink_components(path)
    before_identity = _regular_file_identity(path, "inventory member")
    size = before_identity[2]
    if size > MAX_INVENTORY_FILE_BYTES:
        raise ValidationError(f"inventory member exceeds size limit: {path.name}")
    if expected_size is not None and size != expected_size:
        raise ValidationError(f"inventory member changed while hashing: {path.name}")
    digest = hashlib.sha256()
    observed = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                observed += len(chunk)
                if observed > MAX_INVENTORY_FILE_BYTES:
                    raise ValidationError(
                        f"inventory member exceeds size limit: {path.name}"
                    )
                digest.update(chunk)
    except OSError as exc:
        raise PCBDraftError(f"cannot hash inventory member: {path}") from exc
    _reject_symlink_components(path)
    if (
        observed != size
        or _regular_file_identity(path, "inventory member") != before_identity
    ):
        raise ValidationError(f"inventory member changed while hashing: {path.name}")
    return observed, digest.hexdigest()


def build_inventory(root: str | Path) -> tuple[InventoryEntry, ...]:
    """Build a deterministic, relative inventory without following symlinks."""
    source = Path(root).expanduser()
    _reject_symlink_components(source)
    if not source.is_dir():
        raise ValidationError(f"inventory root is not a directory: {source}")
    entries: list[InventoryEntry] = []
    directory_count = 0
    total = 0
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        if BOARD_BENCH_LOCK_DIR in directories:
            lock_directory = current_path / BOARD_BENCH_LOCK_DIR
            if lock_directory.is_symlink() or not lock_directory.is_dir():
                raise ValidationError("inventory lock directory is unsafe")
        directories[:] = [
            directory for directory in directories if directory != BOARD_BENCH_LOCK_DIR
        ]
        directory_count += len(directories)
        if directory_count > MAX_INVENTORY_DIRECTORIES:
            raise ValidationError("inventory contains too many directories")
        for directory in directories:
            member = current_path / directory
            if member.is_symlink():
                raise ValidationError(f"inventory contains a symlink: {member}")
        for filename in files:
            member = current_path / filename
            if member.is_symlink():
                raise ValidationError(f"inventory contains a symlink: {member}")
            if len(entries) >= MAX_INVENTORY_FILES:
                raise ValidationError("inventory contains too many files")
            size, digest = _hash_regular_file(member)
            total += size
            if total > MAX_INVENTORY_BYTES:
                raise ValidationError("inventory exceeds the total size limit")
            relative = member.relative_to(source).as_posix()
            entries.append(InventoryEntry(relative, size, digest))
    return tuple(sorted(entries, key=lambda item: item.path))
