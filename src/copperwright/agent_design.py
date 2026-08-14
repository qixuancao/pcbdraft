"""Generic agent-to-KiCad planning runtime.

This module is the product path for conversational generation.  It deliberately
does not contain a catalogue of board "profiles" or a hidden BOM.  A model may
propose a bounded, reviewable circuit plan, but it may not emit KiCad text,
coordinates, or executable code.  CopperWright resolves every selected symbol
against the installed KiCad libraries, records a project-local part graph, then
lowers the plan into the stable semantic IR used by the transactional KiCad
backend.

The local-library extraction is intentionally provisional: a KiCad symbol and
footprint are useful executable data, not proof of ratings or layout
suitability. Generated results state that limitation rather than hiding behind
a fixed demo profile.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from kicad_sch_api import Schematic

from .errors import ValidationError
from .io import read_bytes_limited
from .ir import (
    BoardSpec,
    Component,
    Constraint,
    Design,
    Endpoint,
    FunctionalBlock,
    Net,
    Placement,
    Provenance,
    Requirement,
    Scope,
    _identifier,
    _json_value,
    _strict_mapping,
    _string,
    canonical_json_bytes,
)
from .parts import PART_CATALOG_SCHEMA, PartGraph, PartRecord, PinDefinition
from .scope import assert_scope_supported

AGENT_REQUEST_SCHEMA = "copperwright-agent-design-request"
AGENT_REQUEST_VERSION = 1
AGENT_PLAN_SCHEMA = "copperwright-circuit-plan"
AGENT_PLAN_VERSION = 1
PLAN_REVIEW_SCHEMA = "copperwright-agent-plan-review"
PLAN_REVIEW_VERSION = 1
SYMBOL_FILE_LIMIT = 128 * 1024 * 1024
MAX_PLAN_COMPONENTS = 128
MAX_PLAN_NETS = 512
_SYMBOL_ID = re.compile(r"^[A-Za-z0-9_.+-]+:[A-Za-z0-9_.+~{}/-]+$")


def _bounded_strings(
    value: Any,
    path: str,
    *,
    limit: int,
    item_limit: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValidationError(f"{path} must contain at most {limit} strings")
    result = tuple(
        _string(item, f"{path}[{index}]", limit=item_limit)
        for index, item in enumerate(value)
    )
    if len(set(result)) != len(result):
        raise ValidationError(f"{path} contains duplicate values")
    return result


def _normal_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _part_id(symbol: str, footprint: str | None) -> str:
    library, name = symbol.split(":", 1)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{library}-{name}".casefold()).strip("-")
    digest = hashlib.sha256(f"{symbol}\x00{footprint or ''}".encode()).hexdigest()[:12]
    return f"kicad.{slug[:88].rstrip('-')}.{digest}"


@lru_cache(maxsize=8)
def _installed_symbol_index(symbol_root: str) -> tuple[str, ...]:
    """Index public symbol IDs once per installed KiCad library directory.

    This is deliberately a byte-level index rather than model knowledge.  It lets
    an agent propose a symbol from the local KiCad installation without trusting
    an unverified static list or a hard-coded component profile.
    """

    root = Path(symbol_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("installed KiCad symbol library directory is unavailable")
    result: set[str] = set()
    for path in sorted(root.glob("*.kicad_sym")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = read_bytes_limited(path, SYMBOL_FILE_LIMIT)
        except (OSError, ValidationError):
            continue
        library = path.stem
        for match in re.finditer(rb'\(symbol\s+"([^"\\]+)"', raw):
            try:
                name = match.group(1).decode("utf-8")
            except UnicodeDecodeError:
                continue
            # KiCad's nested graphic/unit symbols are not public library IDs.
            if re.search(r"_[0-9]+_[0-9]+$", name):
                continue
            candidate = f"{library}:{name}"
            if _SYMBOL_ID.fullmatch(candidate):
                result.add(candidate)
    return tuple(sorted(result))


@dataclass(frozen=True)
class AgentDesignRequest:
    """A durable, model-independent statement of the user's board request."""

    design_id: str
    name: str
    revision: str
    request_summary: str
    scope: Scope
    board: BoardSpec
    assumptions: tuple[str, ...]
    requested_parts: tuple[str, ...]
    functions: tuple[str, ...]
    power: dict[str, Any]
    source: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> AgentDesignRequest:
        item = _strict_mapping(
            value,
            "$",
            required={
                "schema",
                "version",
                "design_id",
                "name",
                "revision",
                "request_summary",
                "scope",
                "board",
                "assumptions",
                "requested_parts",
                "functions",
                "power",
                "source",
            },
            optional=set(),
        )
        if (
            item["schema"] != AGENT_REQUEST_SCHEMA
            or item["version"] != AGENT_REQUEST_VERSION
        ):
            raise ValidationError("unsupported agent design request schema/version")
        if not isinstance(item["power"], Mapping) or not isinstance(
            item["source"], Mapping
        ):
            raise ValidationError("agent design request power/source must be objects")
        request = cls(
            design_id=_identifier(item["design_id"], "$.design_id"),
            name=_string(item["name"], "$.name", limit=256),
            revision=_string(item["revision"], "$.revision", limit=64),
            request_summary=_string(
                item["request_summary"], "$.request_summary", limit=4096
            ),
            scope=Scope.from_dict(item["scope"]),
            board=BoardSpec.from_dict(item["board"]),
            assumptions=tuple(
                sorted(
                    _bounded_strings(
                        item["assumptions"], "$.assumptions", limit=32, item_limit=512
                    )
                )
            ),
            requested_parts=tuple(
                sorted(
                    _bounded_strings(
                        item["requested_parts"],
                        "$.requested_parts",
                        limit=64,
                        item_limit=256,
                    ),
                    key=str.casefold,
                )
            ),
            functions=tuple(
                sorted(
                    _bounded_strings(
                        item["functions"], "$.functions", limit=64, item_limit=256
                    )
                )
            ),
            power=_json_value(item["power"], "$.power"),
            source=_json_value(item["source"], "$.source"),
        )
        if request.scope.layers != request.board.layers:
            raise ValidationError("agent design request scope and board layers differ")
        assert_scope_supported(request.scope)
        return request

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": AGENT_REQUEST_SCHEMA,
            "version": AGENT_REQUEST_VERSION,
            "design_id": self.design_id,
            "name": self.name,
            "revision": self.revision,
            "request_summary": self.request_summary,
            "scope": self.scope.to_dict(),
            "board": self.board.to_dict(),
            "assumptions": sorted(self.assumptions),
            "requested_parts": sorted(self.requested_parts, key=str.casefold),
            "functions": sorted(self.functions),
            "power": _json_value(self.power),
            "source": _json_value(self.source),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class PlanComponent:
    id: str
    reference: str
    symbol: str
    value: str
    role: str
    footprint: str | None
    on_board: bool
    exact_name: str | None
    # Retained only for backward compatibility with older plan files. Stock
    # KiCad generation does not require procurement identity.
    manufacturer: str | None = None
    mpn: str | None = None

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PlanComponent:
        item = _strict_mapping(
            value,
            path,
            required={
                "id",
                "reference",
                "symbol",
                "value",
                "role",
                "footprint",
                "on_board",
                "exact_name",
            },
            optional={"manufacturer", "mpn"},
        )
        symbol = _string(item["symbol"], f"{path}.symbol", limit=256)
        if not _SYMBOL_ID.fullmatch(symbol):
            raise ValidationError(f"{path}.symbol must be a KiCad library id")
        footprint = item["footprint"]
        if footprint is not None:
            footprint = _string(footprint, f"{path}.footprint", limit=256)
            if ":" not in footprint:
                raise ValidationError(f"{path}.footprint must be a KiCad library id")
        optional_text: dict[str, str | None] = {}
        for field in ("exact_name", "manufacturer", "mpn"):
            current = item.get(field)
            optional_text[field] = (
                None
                if current is None
                else _string(current, f"{path}.{field}", limit=256)
            )
        if not isinstance(item["on_board"], bool):
            raise ValidationError(f"{path}.on_board must be boolean")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            reference=_string(item["reference"], f"{path}.reference", limit=64),
            symbol=symbol,
            value=_string(item["value"], f"{path}.value", limit=256),
            role=_identifier(item["role"], f"{path}.role"),
            footprint=footprint,
            on_board=item["on_board"],
            exact_name=optional_text["exact_name"],
            manufacturer=optional_text["manufacturer"],
            mpn=optional_text["mpn"],
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "reference": self.reference,
            "symbol": self.symbol,
            "value": self.value,
            "role": self.role,
            "footprint": self.footprint,
            "on_board": self.on_board,
            "exact_name": self.exact_name,
        }
        if self.manufacturer is not None:
            result["manufacturer"] = self.manufacturer
        if self.mpn is not None:
            result["mpn"] = self.mpn
        return result


@dataclass(frozen=True)
class PlanNet:
    id: str
    name: str
    endpoints: tuple[Endpoint, ...]
    net_class: str
    intent: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PlanNet:
        item = _strict_mapping(
            value,
            path,
            required={"id", "name", "endpoints", "net_class", "intent"},
            optional=set(),
        )
        endpoints = item["endpoints"]
        if not isinstance(endpoints, list) or not endpoints or len(endpoints) > 128:
            raise ValidationError(f"{path}.endpoints must contain 1 to 128 endpoints")
        parsed = tuple(
            Endpoint.from_dict(entry, f"{path}.endpoints[{index}]")
            for index, entry in enumerate(endpoints)
        )
        if len(set(parsed)) != len(parsed):
            raise ValidationError(f"{path}.endpoints contains duplicates")
        # Net performs KiCad-safe name validation and shares the same rules as IR.
        net = Net(
            id=_identifier(item["id"], f"{path}.id"),
            name=_string(item["name"], f"{path}.name", limit=128),
            endpoints=tuple(sorted(parsed)),
            net_class=_identifier(item["net_class"], f"{path}.net_class"),
            power_domain=None,
            interface=None,
            intent=_string(item["intent"], f"{path}.intent", limit=2048),
        )
        # ``Design`` will validate references; this creates a local net-name check.
        Net.from_dict(net.to_dict(), path)
        return cls(net.id, net.name, net.endpoints, net.net_class, net.intent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "endpoints": [endpoint.to_dict() for endpoint in sorted(self.endpoints)],
            "net_class": self.net_class,
            "intent": self.intent,
        }


@dataclass(frozen=True)
class CircuitPlan:
    """Small, non-geometric circuit plan accepted from an untrusted planner."""

    design_id: str
    summary: str
    assumptions: tuple[str, ...]
    components: tuple[PlanComponent, ...]
    nets: tuple[PlanNet, ...]
    notes: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any) -> CircuitPlan:
        item = _strict_mapping(
            value,
            "$",
            required={
                "schema",
                "version",
                "design_id",
                "summary",
                "assumptions",
                "components",
                "nets",
                "notes",
            },
            optional=set(),
        )
        if item["schema"] != AGENT_PLAN_SCHEMA or item["version"] != AGENT_PLAN_VERSION:
            raise ValidationError("unsupported circuit plan schema/version")
        components = item["components"]
        nets = item["nets"]
        if (
            not isinstance(components, list)
            or not components
            or len(components) > MAX_PLAN_COMPONENTS
        ):
            raise ValidationError("circuit plan must contain 1 to 128 components")
        if not isinstance(nets, list) or not nets or len(nets) > MAX_PLAN_NETS:
            raise ValidationError("circuit plan must contain 1 to 512 nets")
        parsed_components = tuple(
            PlanComponent.from_dict(entry, f"$.components[{index}]")
            for index, entry in enumerate(components)
        )
        parsed_nets = tuple(
            PlanNet.from_dict(entry, f"$.nets[{index}]")
            for index, entry in enumerate(nets)
        )
        if len({entry.id for entry in parsed_components}) != len(parsed_components):
            raise ValidationError("circuit plan component ids must be unique")
        if len({entry.reference for entry in parsed_components}) != len(
            parsed_components
        ):
            raise ValidationError("circuit plan references must be unique")
        if len({entry.id for entry in parsed_nets}) != len(parsed_nets):
            raise ValidationError("circuit plan net ids must be unique")
        if len({entry.name for entry in parsed_nets}) != len(parsed_nets):
            raise ValidationError("circuit plan net names must be unique")
        plan = cls(
            design_id=_identifier(item["design_id"], "$.design_id"),
            summary=_string(item["summary"], "$.summary", limit=4096),
            assumptions=tuple(
                sorted(
                    _bounded_strings(
                        item["assumptions"], "$.assumptions", limit=32, item_limit=512
                    )
                )
            ),
            components=tuple(sorted(parsed_components, key=lambda entry: entry.id)),
            nets=tuple(sorted(parsed_nets, key=lambda entry: entry.id)),
            notes=tuple(
                sorted(
                    _bounded_strings(
                        item["notes"], "$.notes", limit=32, item_limit=1024
                    )
                )
            ),
        )
        component_ids = {entry.id for entry in plan.components}
        seen_endpoints: set[tuple[str, str]] = set()
        for net in plan.nets:
            for endpoint in net.endpoints:
                if endpoint.component not in component_ids:
                    raise ValidationError(
                        f"circuit plan net {net.id} references unknown component {endpoint.component}"
                    )
                identity = (endpoint.component, endpoint.pin)
                if identity in seen_endpoints:
                    raise ValidationError(
                        f"circuit plan puts {endpoint.component}/{endpoint.pin} on more than one net"
                    )
                seen_endpoints.add(identity)
        if not any(net.name.lstrip("/").casefold() == "gnd" for net in plan.nets):
            raise ValidationError("circuit plan must declare a GND reference net")
        return plan

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": AGENT_PLAN_SCHEMA,
            "version": AGENT_PLAN_VERSION,
            "design_id": self.design_id,
            "summary": self.summary,
            "assumptions": sorted(self.assumptions),
            "components": [entry.to_dict() for entry in self.components],
            "nets": [entry.to_dict() for entry in self.nets],
            "notes": sorted(self.notes),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class SymbolCandidate:
    symbol: str
    footprint: str | None
    description: str
    datasheet: str | None
    pins: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "footprint": self.footprint,
            "description": self.description,
            "datasheet": self.datasheet,
            "pins": [dict(pin) for pin in self.pins],
        }


class LocalKiCadPartResolver:
    """Resolve selected components from the installed KiCad symbol libraries."""

    def __init__(self, symbol_root: str | Path | None = None) -> None:
        raw = symbol_root or os.environ.get(
            "KICAD_SYMBOL_DIR", "/usr/share/kicad/symbols"
        )
        self.symbol_root = Path(raw)

    def _symbol_path(self, symbol: str) -> Path:
        if not _SYMBOL_ID.fullmatch(symbol):
            raise ValidationError("symbol must be a KiCad library id")
        library, _name = symbol.split(":", 1)
        path = self.symbol_root / f"{library}.kicad_sym"
        if path.is_symlink() or not path.is_file():
            raise ValidationError(
                f"installed KiCad symbol library is unavailable: {library}"
            )
        return path

    @property
    def _symbol_index(self) -> tuple[str, ...]:
        return _installed_symbol_index(str(self.symbol_root))

    def find(self, query: str, *, limit: int = 12) -> tuple[SymbolCandidate, ...]:
        query = _string(query, "symbol query", limit=256)
        if limit < 1 or limit > 64:
            raise ValidationError("symbol candidate limit must be from 1 to 64")
        key = _normal_key(query)
        if not key:
            return ()

        def rank(symbol: str) -> tuple[int, int, str]:
            library, name = symbol.split(":", 1)
            name_key = _normal_key(name)
            library_key = _normal_key(library)
            if name_key == key:
                score = 0
            elif name_key.startswith(key):
                score = 1
            elif key in name_key:
                score = 2
            elif key in library_key:
                score = 3
            else:
                score = 99
            return (score, len(name_key), symbol)

        matches = [symbol for symbol in self._symbol_index if rank(symbol)[0] < 99]
        return tuple(
            self.describe(symbol) for symbol in sorted(matches, key=rank)[:limit]
        )

    def describe(self, symbol: str) -> SymbolCandidate:
        self._symbol_path(symbol)
        component = _library_component(symbol)
        pins = _pins(component)
        properties = _symbol_properties(component)
        footprint = properties.get("Footprint") or None
        return SymbolCandidate(
            symbol=symbol,
            footprint=footprint,
            description=properties.get("Description") or f"KiCad symbol {symbol}",
            datasheet=properties.get("Datasheet") or None,
            pins=tuple(
                {
                    "number": pin["number"],
                    "name": pin["name"],
                    "electrical_type": pin["electrical_type"],
                }
                for pin in pins
            ),
        )

    def resolve(self, component: PlanComponent) -> PartRecord:
        candidate = self.describe(component.symbol)
        footprint = component.footprint or candidate.footprint
        if component.on_board and footprint is None:
            raise ValidationError(
                f"plan component {component.id} is on_board but {component.symbol} has no footprint; provide an explicit footprint"
            )
        if footprint is not None and ":" not in footprint:
            raise ValidationError(
                f"plan component {component.id} has an invalid footprint"
            )
        part_id = _part_id(component.symbol, footprint)
        return PartRecord(
            id=part_id,
            manufacturer=component.manufacturer or "not specified",
            mpn=component.mpn or "not specified",
            description=candidate.description,
            kind="generic_component",
            symbol=component.symbol,
            footprint=footprint,
            pins=tuple(
                PinDefinition(
                    number=pin["number"],
                    name=pin["name"],
                    electrical_type=pin["electrical_type"],
                    functions=(),
                    required=False,
                    footprint_pad=pin["number"],
                )
                for pin in candidate.pins
            ),
            ratings={},
            lifecycle={"status": "unknown", "source": "local_kicad_library"},
            sourcing={"status": "not_checked"},
            manufacturing={},
            models={},
            bom=component.on_board,
            trust="extracted",
            evidence=(
                Provenance(
                    id="local_kicad_symbol",
                    kind="symbol_library",
                    source="KiCad installed libraries",
                    locator=component.symbol,
                    acquired_at=None,
                    method="local_library_extract",
                    confidence=0.7,
                    notes=(
                        "Pin names and the default footprint were read from the installed "
                        "stock KiCad libraries; electrical and layout suitability were not validated."
                    ),
                ),
            ),
            alternates=(),
        )


def _library_component(symbol: str) -> Any:
    schematic = Schematic.create(name="copperwright-symbol-probe")
    try:
        return schematic.components.add(
            symbol,
            reference="U1",
            value=symbol.split(":", 1)[1],
            position=(0, 0),
            grid_units=False,
        )
    except Exception as exc:  # backend errors are not an executable agent instruction
        raise ValidationError(
            f"cannot load installed KiCad symbol {symbol}: {exc}"
        ) from exc


def _pins(component: Any) -> tuple[dict[str, str], ...]:
    raw = component.list_pins()
    if not isinstance(raw, list):
        raise ValidationError("KiCad symbol probe returned malformed pin data")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValidationError("KiCad symbol probe returned malformed pin entry")
        number = entry.get("number")
        name = entry.get("name")
        electrical_type = entry.get("type")
        if not all(isinstance(item, str) for item in (number, name, electrical_type)):
            raise ValidationError("KiCad symbol probe returned invalid pin values")
        if not number or number in seen:
            # Hidden duplicate-unit pins do not represent a distinct footprint pad.
            continue
        seen.add(number)
        result.append(
            {
                "number": number,
                "name": name,
                "electrical_type": re.sub(
                    r"[^a-z0-9_]+", "_", electrical_type.casefold()
                ).strip("_")
                or "passive",
            }
        )
    if not result:
        raise ValidationError("KiCad symbol has no usable pins")
    return tuple(sorted(result, key=lambda entry: _pin_sort_key(entry["number"])))


def _pin_sort_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(item) if item.isdigit() else item for item in re.split(r"(\d+)", value)
    )


def _symbol_properties(component: Any) -> dict[str, str]:
    definition = component.get_symbol_definition()
    raw = getattr(definition, "raw_kicad_data", None)
    if not isinstance(raw, list):
        return {}
    result: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        if str(entry[0]) != "property" or not isinstance(entry[1], str):
            continue
        name = entry[1]
        value = entry[2]
        if isinstance(value, str) and name in {"Footprint", "Datasheet", "Description"}:
            result[name] = value
    return result


@dataclass(frozen=True)
class AgentCompilation:
    request: AgentDesignRequest
    plan: CircuitPlan
    design: Design
    graph: PartGraph
    review: AgentPlanReview


@dataclass(frozen=True, order=True)
class PlanReviewFinding:
    """One deterministic preflight observation about a generic circuit plan.

    These are deliberately narrower than electrical sign-off.  They describe
    facts derivable from the selected local symbols and the reviewed topology,
    while making residual engineering work explicit rather than fabricating a
    pass result.
    """

    id: str
    severity: str
    outcome: str
    summary: str
    evidence: tuple[str, ...]
    action: str
    requires_human: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "outcome": self.outcome,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "action": self.action,
            "requires_human": self.requires_human,
        }


@dataclass(frozen=True)
class AgentPlanReview:
    """Stable deterministic preflight evidence retained beside a circuit plan."""

    design_id: str
    request_hash: str
    plan_hash: str
    findings: tuple[PlanReviewFinding, ...]

    @property
    def failed_count(self) -> int:
        return sum(finding.outcome == "fail" for finding in self.findings)

    @property
    def attention_count(self) -> int:
        return sum(finding.outcome != "pass" for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_REVIEW_SCHEMA,
            "version": PLAN_REVIEW_VERSION,
            "design_id": self.design_id,
            "request_hash": self.request_hash,
            "plan_hash": self.plan_hash,
            "summary": {
                "passed": sum(finding.outcome == "pass" for finding in self.findings),
                "failed": self.failed_count,
                "attention_required": self.attention_count,
            },
            "limitations": [
                "Preflight checks topology against local KiCad symbol metadata only.",
                "It does not establish electrical, regulatory, RF, thermal, layout, or manufacturing correctness.",
            ],
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _baseline_agent_constraints(
    request: AgentDesignRequest, plan: CircuitPlan
) -> tuple[Constraint, ...]:
    """Carry the reviewed board manufacturing envelope into the semantic IR.

    This is generic project metadata derived from the user's accepted request,
    not a hidden electrical block or a model-authored geometry instruction.
    """

    constraints = [
        Constraint(
            id="agent_manufacturing_rules",
            kind="manufacturing_rules",
            targets=("board",),
            params={
                "process_profile": "stock_kicad_prototype",
                "min_track_mm": request.board.min_track_mm,
                "min_clearance_mm": request.board.min_clearance_mm,
                "min_drill_mm": request.board.min_drill_mm,
                "edge_clearance_mm": request.board.edge_clearance_mm,
            },
            severity="release_blocking",
            rationale=(
                "Preserve the approved board-manufacturing envelope for deterministic checks."
            ),
            provenance=("user_request",),
        ),
    ]
    ground = next(net for net in plan.nets if net.name.lstrip("/").casefold() == "gnd")
    routing_params: dict[str, Any] = {
        "width_mm": max(request.board.min_track_mm, 0.25),
        "auto_route": True,
        "neckdown_width_mm": request.board.min_track_mm,
        "neckdown_max_length_mm_per_pad": 2.25,
    }
    if len(ground.endpoints) >= 2:
        routing_params.update(
            {
                "continuous_reference_net": ground.id,
                "min_reference_stitching_vias": 2,
            }
        )
    constraints.append(
        Constraint(
            id="agent_routing",
            kind="routing",
            targets=tuple(sorted(net.id for net in plan.nets)),
            params=routing_params,
            severity="required",
            rationale="Route declared nets using the reviewed board rules.",
            provenance=("user_request",),
        )
    )
    return tuple(constraints)


def review_agent_plan(
    request: AgentDesignRequest,
    plan: CircuitPlan,
    design: Design,
    graph: PartGraph,
) -> AgentPlanReview:
    """Return deterministic, non-geometric preflight evidence for a plan.

    The compiler already rejects malformed symbols, pins, and duplicate net
    assignments.  This reviewer focuses on the important *missing* evidence
    that a syntactically valid topology can still have: power-pin coverage,
    declared rail sources, I2C pull-up evidence, and decoupling evidence.
    Findings are diagnostic and do not deny a generation attempt.
    """

    components = {component.id: component for component in design.components}
    endpoint_nets: dict[tuple[str, str], Net] = {}
    for net in design.nets:
        for endpoint in net.endpoints:
            endpoint_nets[(endpoint.component, endpoint.pin)] = net

    findings: list[PlanReviewFinding] = []
    findings.append(
        PlanReviewFinding(
            id="identity.requested_parts",
            severity="info",
            outcome="pass",
            summary="Every explicitly requested part name is represented by the reviewed plan.",
            evidence=tuple(request.requested_parts) or ("no exact part name supplied",),
            action="Review the selected stock symbol and footprint before generation.",
            requires_human=True,
        )
    )

    uncovered_power_inputs: list[str] = []
    non_power_power_inputs: list[str] = []
    power_input_count = 0
    for component_id, component in sorted(components.items()):
        if component.attributes.get("exclude_from_board", False):
            continue
        part = graph.get(component.part_id)
        for pin in part.pins:
            if pin.electrical_type != "power_in":
                continue
            power_input_count += 1
            label = f"{component.reference}.{pin.number} ({pin.name})"
            net = endpoint_nets.get((component_id, pin.number))
            if net is None:
                uncovered_power_inputs.append(label)
            elif net.net_class != "power":
                non_power_power_inputs.append(f"{label} -> {net.name}")
    power_issues = uncovered_power_inputs + non_power_power_inputs
    findings.append(
        PlanReviewFinding(
            id="power.input_pin_coverage",
            severity="error" if power_issues else "info",
            outcome="fail" if power_issues else "pass",
            summary=(
                "One or more local-symbol power-input pins are missing a power-net assignment or are assigned to a non-power net."
                if power_issues
                else (
                    "All local-symbol power-input pins are assigned to power-class nets."
                    if power_input_count
                    else "No selected local symbol exposes a power-input pin for this check."
                )
            ),
            evidence=tuple(power_issues)
            or (f"{power_input_count} power-input pins checked",),
            action="Connect every required supply/return pin to an explicit power-class net and verify the datasheet pin policy.",
            requires_human=True,
        )
    )

    unsourced_rails: list[str] = []
    for net in design.nets:
        if net.net_class != "power" or _normal_key(net.name) in {
            "gnd",
            "ground",
            "vss",
        }:
            continue
        has_source = False
        for endpoint in net.endpoints:
            if endpoint.role == "source":
                has_source = True
                break
            component = components.get(endpoint.component)
            if component is not None:
                pin = graph.get(component.part_id).pin(endpoint.pin)
                if pin is not None and pin.electrical_type == "power_out":
                    has_source = True
                    break
        if not has_source:
            unsourced_rails.append(net.name)
    findings.append(
        PlanReviewFinding(
            id="power.rail_source_evidence",
            severity="error" if unsourced_rails else "info",
            outcome="fail" if unsourced_rails else "pass",
            summary=(
                "A non-ground power rail has no explicit source endpoint or local power-output pin."
                if unsourced_rails
                else "Every non-ground power rail has an explicit source endpoint or local power-output pin."
            ),
            evidence=tuple(unsourced_rails) or ("all non-ground power rails",),
            action="Declare the connector, regulator, battery, or other physical source for each non-ground rail.",
            requires_human=True,
        )
    )

    i2c_requested = "i2c" in request.scope.domains or any(
        "i2c" in _normal_key(value)
        for value in (*request.functions, *(net.name for net in plan.nets))
    )
    i2c_nets = [
        net
        for net in design.nets
        if "i2c" in _normal_key(net.name) or _normal_key(net.name) in {"sda", "scl"}
    ]
    if not i2c_requested:
        findings.append(
            PlanReviewFinding(
                id="interface.i2c_pullup_evidence",
                severity="info",
                outcome="pass",
                summary="I2C pull-up review is not applicable to the declared request.",
                evidence=("no I2C domain or net declared",),
                action="No I2C-specific action is required by this preflight.",
                requires_human=False,
            )
        )
    elif not i2c_nets:
        findings.append(
            PlanReviewFinding(
                id="interface.i2c_pullup_evidence",
                severity="error",
                outcome="fail",
                summary="The request declares I2C but the plan exposes no SDA/SCL or I2C-labelled net.",
                evidence=("declared scope: i2c",),
                action="Add explicit I2C data and clock nets using selected symbol pins.",
                requires_human=True,
            )
        )
    else:
        i2c_ids = {net.id for net in i2c_nets}
        pullup_components: list[str] = []
        for component_id, component in sorted(components.items()):
            part = graph.get(component.part_id)
            looks_like_resistor = (
                part.symbol.endswith(":R")
                or "pullup"
                in str(component.attributes.get("planner_role", "")).casefold()
            )
            if not looks_like_resistor:
                continue
            component_nets = {
                net.id
                for net in design.nets
                if any(endpoint.component == component_id for endpoint in net.endpoints)
            }
            has_i2c = bool(component_nets & i2c_ids)
            has_non_ground_power = any(
                net.id in component_nets
                and net.net_class == "power"
                and _normal_key(net.name) not in {"gnd", "ground", "vss"}
                for net in design.nets
            )
            if has_i2c and has_non_ground_power:
                pullup_components.append(component.reference)
        findings.append(
            PlanReviewFinding(
                id="interface.i2c_pullup_evidence",
                severity="warning",
                outcome="unknown",
                summary=(
                    "No explicit resistor/pull-up component bridges an I2C net to a non-ground power rail."
                    if not pullup_components
                    else "The plan contains explicit resistor/pull-up connectivity from I2C to a power rail, but resistance and bus-budget evidence still require review."
                ),
                evidence=tuple(pullup_components)
                or tuple(sorted(net.name for net in i2c_nets)),
                action="Verify pull-up topology, resistance, voltage, bus capacitance, and any internal pull-ups against the selected devices' datasheets.",
                requires_human=True,
            )
        )

    active_device_ids = {
        component_id
        for component_id, component in components.items()
        if any(
            token in str(component.attributes.get("planner_role", "")).casefold()
            or token in graph.get(component.part_id).symbol.casefold()
            for token in ("mcu", "microcontroller", "sensor", "controller", "regulator")
        )
    }
    capacitors: list[str] = []
    for component_id, component in sorted(components.items()):
        part = graph.get(component.part_id)
        if not (
            part.symbol.endswith(":C")
            or "decoupl" in str(component.attributes.get("planner_role", "")).casefold()
        ):
            continue
        component_nets = {
            net
            for net in design.nets
            if any(endpoint.component == component_id for endpoint in net.endpoints)
        }
        if any(
            _normal_key(net.name) in {"gnd", "ground", "vss"} for net in component_nets
        ) and any(
            net.net_class == "power"
            and _normal_key(net.name) not in {"gnd", "ground", "vss"}
            for net in component_nets
        ):
            capacitors.append(component.reference)
    if not active_device_ids:
        findings.append(
            PlanReviewFinding(
                id="power.decoupling_evidence",
                severity="info",
                outcome="pass",
                summary="No MCU, sensor, controller, or regulator role triggers the generic decoupling preflight.",
                evidence=("no applicable planner role",),
                action="Review any device-specific bypass requirements if the plan evolves.",
                requires_human=True,
            )
        )
    else:
        findings.append(
            PlanReviewFinding(
                id="power.decoupling_evidence",
                severity="warning",
                outcome="unknown",
                summary=(
                    "No explicit capacitor connects a non-ground power rail to ground for the declared active devices."
                    if not capacitors
                    else "The plan contains explicit power-to-ground capacitors, but device-specific value, count, placement, and return-path evidence still require review."
                ),
                evidence=tuple(capacitors)
                or tuple(
                    sorted(components[item].reference for item in active_device_ids)
                ),
                action="Verify each selected device's decoupling network and layout against its datasheet before fabrication.",
                requires_human=True,
            )
        )

    has_manufacturing_contract = any(
        constraint.kind == "manufacturing_rules" for constraint in design.constraints
    )
    findings.append(
        PlanReviewFinding(
            id="constraints.board_manufacturing_envelope",
            severity="info" if has_manufacturing_contract else "error",
            outcome="pass" if has_manufacturing_contract else "fail",
            summary=(
                "The approved board manufacturing envelope is retained as a semantic constraint."
                if has_manufacturing_contract
                else "The semantic design lacks a manufacturing-envelope constraint."
            ),
            evidence=("agent_manufacturing_rules",)
            if has_manufacturing_contract
            else ("missing manufacturing_rules",),
            action="Review the board rules for the intended fabrication process.",
            requires_human=True,
        )
    )

    return AgentPlanReview(
        design_id=design.design_id,
        request_hash=hashlib.sha256(request.canonical_bytes()).hexdigest(),
        plan_hash=hashlib.sha256(plan.canonical_bytes()).hexdigest(),
        findings=tuple(sorted(findings)),
    )


def planner_symbol_context(
    request: AgentDesignRequest,
    *,
    resolver: LocalKiCadPartResolver | None = None,
    limit_per_query: int = 12,
) -> dict[str, list[dict[str, Any]]]:
    """Return actual local symbol candidates, grouped by user-facing query.

    The reserved ``_runtime_primitives`` group only contains universal KiCad
    primitives.  It is not a hidden design template: the planning provider still
    chooses whether they are needed and must declare every component and net.
    """
    resolved = resolver or LocalKiCadPartResolver()
    queries = tuple(dict.fromkeys((*request.requested_parts, *request.functions)))
    result: dict[str, list[dict[str, Any]]] = {}
    primitives: list[dict[str, Any]] = []
    for symbol, footprint in (
        ("Device:R", "Resistor_SMD:R_0603_1608Metric"),
        ("Device:C", "Capacitor_SMD:C_0603_1608Metric"),
        ("Device:LED", "LED_SMD:LED_0603_1608Metric"),
        ("power:GND", None),
        ("power:+3V3", None),
        (
            "Connector_Generic:Conn_01x02",
            "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
        ),
    ):
        try:
            primitive = resolved.describe(symbol).to_dict()
            if footprint is not None:
                primitive["footprint"] = footprint
            primitives.append(primitive)
        except ValidationError:
            # A partial KiCad installation should not turn a normal user request
            # into a fictional plan.  The planner receives the missing-library
            # evidence with the query-specific candidates below.
            continue
    result["_runtime_primitives"] = primitives
    for query in queries[:64]:
        try:
            candidates = resolved.find(query, limit=limit_per_query)
        except ValidationError as exc:
            result[query] = [{"error": str(exc)}]
            continue
        result[query] = [candidate.to_dict() for candidate in candidates]
    return result


def compile_agent_plan(
    request: AgentDesignRequest,
    plan_value: CircuitPlan | Mapping[str, Any],
    *,
    resolver: LocalKiCadPartResolver | None = None,
    base_graph: PartGraph | None = None,
) -> AgentCompilation:
    """Resolve a generic plan into semantic IR and a project-local part graph."""
    plan = (
        plan_value
        if isinstance(plan_value, CircuitPlan)
        else CircuitPlan.from_dict(plan_value)
    )
    if plan.design_id != request.design_id:
        raise ValidationError(
            "circuit plan design_id does not match the approved request"
        )
    resolved = resolver or LocalKiCadPartResolver()
    parts_by_component: dict[str, PartRecord] = {}
    for component in plan.components:
        parts_by_component[component.id] = resolved.resolve(component)
    _assert_requested_part_identity(request, plan)
    base = base_graph or PartGraph(
        (),
        license_id="local-stock-kicad-libraries",
        source="installed_stock_kicad",
    )
    graph = base.merged(parts_by_component.values(), source="project_local_kicad")
    component_by_id = {item.id: item for item in plan.components}
    placements = _seed_placements(plan.components, request.board)
    ir_components = tuple(
        Component(
            id=item.id,
            reference=item.reference,
            part_id=parts_by_component[item.id].id,
            value=item.value,
            block_id=f"block.{item.id}",
            placement=placements[item.id],
            attributes={
                "planner_role": item.role,
                "exclude_from_board": not item.on_board,
                "source_symbol": item.symbol,
            },
        )
        for item in plan.components
    )
    provenance = (
        Provenance(
            id="user_request",
            kind="user_requirement",
            source="CopperWright conversation",
            locator=str(request.source.get("locator", "conversation")),
            acquired_at=request.source.get("date")
            if isinstance(request.source.get("date"), str)
            else None,
            method="user_supplied",
            confidence=1.0,
            notes="Sanitized user request retained in the project record.",
        ),
        Provenance(
            id="agent_plan",
            kind="agent_plan",
            source="Configured planning provider",
            locator="circuit-plan.json",
            acquired_at=None,
            method="structured_output",
            confidence=0.5,
            notes="Untrusted plan constrained to local symbols and semantic topology.",
        ),
    )
    design = Design(
        design_id=request.design_id,
        name=request.name,
        revision=request.revision,
        scope=request.scope,
        requirements=(
            Requirement(
                id="user_board_request",
                text=request.request_summary,
                acceptance=(
                    "Generate a reviewable KiCad schematic and PCB attempt from the approved semantic plan.",
                ),
                risk="unknown",
                provenance=("user_request",),
            ),
        ),
        provenance=provenance,
        blocks=tuple(
            FunctionalBlock(
                id=f"block.{item.id}",
                kind="agent_component",
                name=item.reference,
                version="agent_plan_v1",
                intent=f"{item.role}: {item.value}",
                components=(item.id,),
                provenance=("agent_plan",),
            )
            for item in plan.components
        ),
        power_domains=(),
        interfaces=(),
        components=ir_components,
        nets=tuple(
            Net(
                id=item.id,
                name=item.name,
                endpoints=item.endpoints,
                net_class=item.net_class,
                power_domain=None,
                interface=None,
                intent=item.intent,
            )
            for item in plan.nets
        ),
        constraints=_baseline_agent_constraints(request, plan),
        board=request.board,
        analyses=(
            {
                "id": "agent_plan",
                "kind": "agent_plan",
                "state": "provisional",
                "required": False,
                "reason": "No functional simulation was performed for the generated circuit plan.",
                "summary": plan.summary,
                "notes": list(plan.notes),
            },
        ),
        metadata={
            "generator": "agent_plan_v1",
            "assurance": "provisional",
            "requirements_hash": hashlib.sha256(request.canonical_bytes()).hexdigest(),
            "plan_hash": hashlib.sha256(plan.canonical_bytes()).hexdigest(),
            "requested_parts": list(request.requested_parts),
            "planner_assumptions": sorted({*request.assumptions, *plan.assumptions}),
            "part_catalog_schema": PART_CATALOG_SCHEMA,
        },
    )
    del component_by_id
    design.assert_valid()
    graph.assert_design(design, check_libraries=True, allow_provisional=True)
    review = review_agent_plan(request, plan, design, graph)
    return AgentCompilation(
        request=request,
        plan=plan,
        design=design,
        graph=graph,
        review=review,
    )


def _assert_requested_part_identity(
    request: AgentDesignRequest, plan: CircuitPlan
) -> None:
    for requested in request.requested_parts:
        needle = _normal_key(requested)
        if not needle:
            continue
        candidates = (
            _normal_key(component.exact_name or "")
            + " "
            + _normal_key(component.symbol.split(":", 1)[1])
            + " "
            + _normal_key(component.mpn or "")
            for component in plan.components
        )
        if not any(needle in candidate for candidate in candidates):
            raise ValidationError(
                "circuit plan did not preserve explicitly requested part identity: "
                + requested
            )


def _seed_placements(
    components: Iterable[PlanComponent], board: BoardSpec
) -> dict[str, Placement]:
    entries = tuple(components)
    count = len(entries)
    columns = max(1, min(8, int(count**0.5 + 0.999)))
    rows = max(1, (count + columns - 1) // columns)
    result: dict[str, Placement] = {}
    for index, component in enumerate(entries):
        column = index % columns
        row = index // columns
        # These are only deterministic solver seeds, not LLM-authored geometry.
        x = board.width_mm * (column + 1) / (columns + 1)
        y = board.height_mm * (row + 1) / (rows + 1)
        result[component.id] = Placement(
            x_mm=round(x, 6),
            y_mm=round(y, 6),
            rotation_deg=0.0,
            side="front",
            fixed=False,
        )
    return result


def circuit_plan_schema() -> dict[str, Any]:
    """Strict JSON Schema passed to model providers for high-level topology only."""
    endpoint = {
        "type": "object",
        "additionalProperties": False,
        "required": ["component", "pin", "role"],
        "properties": {
            "component": {"type": "string", "maxLength": 128},
            "pin": {"type": "string", "maxLength": 64},
            "role": {"type": "string", "maxLength": 128},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "version",
            "design_id",
            "summary",
            "assumptions",
            "components",
            "nets",
            "notes",
        ],
        "properties": {
            "schema": {"const": AGENT_PLAN_SCHEMA},
            "version": {"const": AGENT_PLAN_VERSION},
            "design_id": {"type": "string", "maxLength": 128},
            "summary": {"type": "string", "maxLength": 4096},
            "assumptions": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "maxLength": 512},
            },
            "components": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PLAN_COMPONENTS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "reference",
                        "symbol",
                        "value",
                        "role",
                        "footprint",
                        "on_board",
                        "exact_name",
                    ],
                    "properties": {
                        "id": {"type": "string", "maxLength": 128},
                        "reference": {"type": "string", "maxLength": 64},
                        "symbol": {"type": "string", "maxLength": 256},
                        "value": {"type": "string", "maxLength": 256},
                        "role": {"type": "string", "maxLength": 128},
                        "footprint": {
                            "anyOf": [
                                {"type": "string", "maxLength": 256},
                                {"type": "null"},
                            ]
                        },
                        "on_board": {"type": "boolean"},
                        "exact_name": {
                            "anyOf": [
                                {"type": "string", "maxLength": 256},
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
            "nets": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PLAN_NETS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "name", "endpoints", "net_class", "intent"],
                    "properties": {
                        "id": {"type": "string", "maxLength": 128},
                        "name": {"type": "string", "maxLength": 128},
                        "endpoints": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 128,
                            "items": endpoint,
                        },
                        "net_class": {"type": "string", "maxLength": 128},
                        "intent": {"type": "string", "maxLength": 2048},
                    },
                },
            },
            "notes": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "maxLength": 1024},
            },
        },
    }
