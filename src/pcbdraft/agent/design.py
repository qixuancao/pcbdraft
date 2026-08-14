"""Generic agent-to-KiCad planning runtime.

This module is the product path for conversational generation.  It deliberately
does not contain a catalogue of board "profiles" or a hidden BOM.  A model may
propose a bounded, reviewable circuit plan, but it may not emit KiCad text,
coordinates, or executable code.  PCBDraft resolves every selected symbol
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
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from kicad_sch_api import Schematic

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import read_bytes_limited
from pcbdraft.domain.assertions import ASSERTION_KINDS, evaluate_assertion
from pcbdraft.domain.component_qualification import (
    COMPONENT_QUALIFICATION_SCHEMA,
    ComponentQualificationReport,
    qualify_components,
)
from pcbdraft.domain.ir import (
    BoardSpec,
    Component,
    Constraint,
    Design,
    Endpoint,
    FunctionalBlock,
    Interface,
    Net,
    Placement,
    PowerDomain,
    Provenance,
    Requirement,
    Scope,
    _identifier,
    _json_value,
    _strict_mapping,
    _string,
    canonical_json_bytes,
)
from pcbdraft.domain.parts import (
    PART_CATALOG_SCHEMA,
    PartGraph,
    PartRecord,
    PinDefinition,
)
from pcbdraft.domain.scope import assert_scope_supported
from pcbdraft.domain.spatial_contracts import (
    BOARD_REGIONS,
    COPPER_LAYER_SCOPES,
    anchored_rectangle,
)

AGENT_REQUEST_SCHEMA = "pcbdraft-agent-design-request"
AGENT_REQUEST_VERSION = 1
AGENT_PLAN_SCHEMA = "pcbdraft-circuit-plan"
AGENT_PLAN_VERSION = 2
AGENT_PLAN_LEGACY_VERSION = 1
PLAN_REVIEW_SCHEMA = "pcbdraft-agent-plan-review"
PLAN_REVIEW_VERSION = 2
SYMBOL_FILE_LIMIT = 128 * 1024 * 1024
MAX_PLAN_COMPONENTS = 128
MAX_PLAN_NETS = 512
MAX_PLAN_BLOCKS = 128
MAX_PLAN_POWER_DOMAINS = 32
MAX_PLAN_INTERFACES = 64
MAX_PLAN_CONSTRAINTS = 256
MAX_PLAN_ASSERTIONS = 128
MAX_PLAN_PARAMETERS = 128
_SYMBOL_ID = re.compile(r"^[A-Za-z0-9_.+-]+:[A-Za-z0-9_.+~{}/-]+$")
_PHYSICAL_PIN_NUMBER = re.compile(r"^[A-Za-z0-9_+./~{}-]{1,64}$")
_PLAN_CONSTRAINT_KINDS = {
    "board_keepout",
    "connector_pinout",
    "current_limit",
    "decoupling",
    "differential_pair",
    "edge_placement",
    "functional_group",
    "i2c_electrical_budget",
    "interface_pullups",
    "ldo_regulation_budget",
    "net_label",
    "placement_region",
    "power_budget",
    "routing",
    "source_ownership",
    "spi_electrical_budget",
    "uart_electrical_budget",
}


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


def _parameter_map(
    value: Any,
    path: str,
    *,
    allow_physical_pin_names: bool = False,
) -> dict[str, Any]:
    """Parse a strict, schema-friendly list of named scalar parameters."""

    if not isinstance(value, list) or len(value) > MAX_PLAN_PARAMETERS:
        raise ValidationError(
            f"{path} must contain at most {MAX_PLAN_PARAMETERS} named parameters"
        )
    result: dict[str, Any] = {}
    for index, entry in enumerate(value):
        item_path = f"{path}[{index}]"
        item = _strict_mapping(
            entry,
            item_path,
            required={"name", "value"},
            optional=set(),
        )
        raw_name = item["name"]
        if (
            allow_physical_pin_names
            and isinstance(raw_name, str)
            and raw_name.startswith("pin.")
        ):
            pin_number = raw_name.removeprefix("pin.")
            if not _PHYSICAL_PIN_NUMBER.fullmatch(pin_number):
                raise ValidationError(
                    f"{item_path}.name contains an invalid physical pin number"
                )
            name = raw_name
        else:
            name = _identifier(raw_name, f"{item_path}.name")
        if name in result:
            raise ValidationError(f"{path} contains duplicate parameter {name}")
        parameter = item["value"]
        if isinstance(parameter, str):
            parameter = _string(
                parameter, f"{item_path}.value", nonempty=False, limit=512
            )
        elif isinstance(parameter, float):
            if not math.isfinite(parameter):
                raise ValidationError(f"{item_path}.value must be finite")
        elif parameter is not None and not isinstance(parameter, (int, bool)):
            raise ValidationError(f"{item_path}.value must be a JSON scalar")
        result[name] = parameter
    return result


def _parameter_list(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"name": name, "value": _json_value(value[name])} for name in sorted(value)]


def _positive_number(value: Any, path: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValidationError(f"{path} must be a positive finite number")
    return float(value)


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
class PlanBlock:
    """One reusable functional grouping in a version-2 circuit plan."""

    id: str
    kind: str
    name: str
    intent: str
    parent: str | None
    components: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PlanBlock:
        item = _strict_mapping(
            value,
            path,
            required={"id", "kind", "name", "intent", "parent", "components"},
            optional=set(),
        )
        parent = item["parent"]
        if parent is not None:
            parent = _identifier(parent, f"{path}.parent")
        components = item["components"]
        if not isinstance(components, list) or len(components) > MAX_PLAN_COMPONENTS:
            raise ValidationError(
                f"{path}.components must contain at most {MAX_PLAN_COMPONENTS} ids"
            )
        parsed_components = tuple(
            sorted(
                _identifier(component, f"{path}.components[{index}]")
                for index, component in enumerate(components)
            )
        )
        if len(set(parsed_components)) != len(parsed_components):
            raise ValidationError(f"{path}.components contains duplicates")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            kind=_identifier(item["kind"], f"{path}.kind"),
            name=_string(item["name"], f"{path}.name", limit=256),
            intent=_string(item["intent"], f"{path}.intent", limit=2048),
            parent=parent,
            components=parsed_components,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "intent": self.intent,
            "parent": self.parent,
            "components": list(self.components),
        }


@dataclass(frozen=True)
class PlanPowerDomain:
    id: str
    nominal_v: float
    min_v: float
    max_v: float
    max_current_a: float
    source: Endpoint
    intent: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PlanPowerDomain:
        parsed = PowerDomain.from_dict(value, path)
        return cls(
            parsed.id,
            parsed.nominal_v,
            parsed.min_v,
            parsed.max_v,
            parsed.max_current_a,
            parsed.source,
            parsed.intent,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_ir().to_dict()

    def to_ir(self) -> PowerDomain:
        return PowerDomain(
            self.id,
            self.nominal_v,
            self.min_v,
            self.max_v,
            self.max_current_a,
            self.source,
            self.intent,
        )


@dataclass(frozen=True)
class PlanInterface:
    id: str
    kind: str
    power_domain: str
    members: tuple[Endpoint, ...]
    controller: Endpoint | None
    parameters: dict[str, Any]
    intent: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PlanInterface:
        item = _strict_mapping(
            value,
            path,
            required={
                "id",
                "kind",
                "power_domain",
                "members",
                "controller",
                "parameters",
                "intent",
            },
            optional=set(),
        )
        members = item["members"]
        if not isinstance(members, list) or not members or len(members) > 128:
            raise ValidationError(f"{path}.members must contain 1 to 128 endpoints")
        parsed_members = tuple(
            sorted(
                Endpoint.from_dict(member, f"{path}.members[{index}]")
                for index, member in enumerate(members)
            )
        )
        if len(set(parsed_members)) != len(parsed_members):
            raise ValidationError(f"{path}.members contains duplicates")
        controller_value = item["controller"]
        controller = (
            None
            if controller_value is None
            else Endpoint.from_dict(controller_value, f"{path}.controller")
        )
        if controller is not None and controller not in parsed_members:
            raise ValidationError(f"{path}.controller must also appear in members")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            kind=_identifier(item["kind"], f"{path}.kind"),
            power_domain=_identifier(item["power_domain"], f"{path}.power_domain"),
            members=parsed_members,
            controller=controller,
            parameters=_parameter_map(item["parameters"], f"{path}.parameters"),
            intent=_string(item["intent"], f"{path}.intent", limit=1024),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "power_domain": self.power_domain,
            "members": [member.to_dict() for member in self.members],
            "controller": self.controller.to_dict() if self.controller else None,
            "parameters": _parameter_list(self.parameters),
            "intent": self.intent,
        }

    def to_ir(self) -> Interface:
        return Interface(
            id=self.id,
            kind=self.kind,
            power_domain=self.power_domain,
            members=self.members,
            controller=self.controller,
            params=_json_value(self.parameters),
            intent=self.intent,
        )


@dataclass(frozen=True)
class PlanConstraint:
    id: str
    kind: str
    targets: tuple[str, ...]
    parameters: dict[str, Any]
    severity: str
    rationale: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PlanConstraint:
        item = _strict_mapping(
            value,
            path,
            required={
                "id",
                "kind",
                "targets",
                "parameters",
                "severity",
                "rationale",
            },
            optional=set(),
        )
        kind = _identifier(item["kind"], f"{path}.kind")
        if kind not in _PLAN_CONSTRAINT_KINDS:
            raise ValidationError(f"{path}.kind is not a supported plan constraint")
        targets = item["targets"]
        if not isinstance(targets, list) or not targets or len(targets) > 128:
            raise ValidationError(f"{path}.targets must contain 1 to 128 ids")
        parsed_targets = tuple(
            sorted(
                _identifier(target, f"{path}.targets[{index}]")
                for index, target in enumerate(targets)
            )
        )
        if len(set(parsed_targets)) != len(parsed_targets):
            raise ValidationError(f"{path}.targets contains duplicates")
        severity = _identifier(item["severity"], f"{path}.severity")
        if severity not in {"advisory", "required", "release_blocking"}:
            raise ValidationError(f"{path}.severity is unsupported")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            kind=kind,
            targets=parsed_targets,
            parameters=_parameter_map(
                item["parameters"],
                f"{path}.parameters",
                allow_physical_pin_names=kind == "connector_pinout",
            ),
            severity=severity,
            rationale=_string(item["rationale"], f"{path}.rationale", limit=2048),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "targets": list(self.targets),
            "parameters": _parameter_list(self.parameters),
            "severity": self.severity,
            "rationale": self.rationale,
        }

    def to_ir(self) -> Constraint:
        return Constraint(
            id=self.id,
            kind=self.kind,
            targets=self.targets,
            params=_json_value(self.parameters),
            severity=self.severity,
            rationale=self.rationale,
            provenance=("agent_plan",),
        )


@dataclass(frozen=True)
class PlanAssertion:
    id: str
    kind: str
    targets: tuple[str, ...]
    minimum: int | None
    maximum: int | None
    severity: str
    rationale: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> PlanAssertion:
        item = _strict_mapping(
            value,
            path,
            required={
                "id",
                "kind",
                "targets",
                "minimum",
                "maximum",
                "severity",
                "rationale",
            },
            optional=set(),
        )
        kind = _identifier(item["kind"], f"{path}.kind")
        if kind not in ASSERTION_KINDS:
            raise ValidationError(f"{path}.kind is not a supported assertion")
        targets = item["targets"]
        if not isinstance(targets, list) or not targets or len(targets) > 128:
            raise ValidationError(f"{path}.targets must contain 1 to 128 ids")
        parsed_targets = tuple(
            sorted(
                _identifier(target, f"{path}.targets[{index}]")
                for index, target in enumerate(targets)
            )
        )
        if len(set(parsed_targets)) != len(parsed_targets):
            raise ValidationError(f"{path}.targets contains duplicates")
        minimum = item["minimum"]
        maximum = item["maximum"]
        if kind in {"net_endpoint_count", "interface_net_count"}:
            if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
                raise ValidationError(f"{path}.minimum must be a non-negative integer")
            if maximum is not None and (
                not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or maximum < minimum
            ):
                raise ValidationError(
                    f"{path}.maximum must be null or at least minimum"
                )
        elif minimum is not None or maximum is not None:
            raise ValidationError(
                f"{path}.minimum/maximum apply only to count assertions"
            )
        severity = _identifier(item["severity"], f"{path}.severity")
        if severity not in {"required", "release_blocking"}:
            raise ValidationError(
                f"{path}.severity must be required or release_blocking"
            )
        assertion_id = _identifier(item["id"], f"{path}.id")
        if len(f"assert.{assertion_id}".encode()) > 128:
            raise ValidationError(f"{path}.id is too long for the IR assertion id")
        return cls(
            id=assertion_id,
            kind=kind,
            targets=parsed_targets,
            minimum=minimum,
            maximum=maximum,
            severity=severity,
            rationale=_string(item["rationale"], f"{path}.rationale", limit=2048),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "targets": list(self.targets),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "severity": self.severity,
            "rationale": self.rationale,
        }

    def to_ir(self) -> Constraint:
        return Constraint(
            id=f"assert.{self.id}",
            kind="assertion",
            targets=self.targets,
            params={
                "predicate": self.kind,
                "minimum": self.minimum,
                "maximum": self.maximum,
            },
            severity=self.severity,
            rationale=self.rationale,
            provenance=("agent_plan",),
        )


@dataclass(frozen=True)
class PlanNet:
    id: str
    name: str
    endpoints: tuple[Endpoint, ...]
    net_class: str
    intent: str
    power_domain: str | None = None
    interface: str | None = None

    @classmethod
    def from_dict(
        cls, value: Any, path: str, *, version: int = AGENT_PLAN_LEGACY_VERSION
    ) -> PlanNet:
        required = {"id", "name", "endpoints", "net_class", "intent"}
        if version == AGENT_PLAN_VERSION:
            required.update({"power_domain", "interface"})
        item = _strict_mapping(
            value,
            path,
            required=required,
            optional={"power_domain", "interface"} - required,
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
        power_domain = item.get("power_domain")
        interface = item.get("interface")
        # Net performs KiCad-safe name validation and shares the same rules as IR.
        net = Net(
            id=_identifier(item["id"], f"{path}.id"),
            name=_string(item["name"], f"{path}.name", limit=128),
            endpoints=tuple(sorted(parsed)),
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
            intent=_string(item["intent"], f"{path}.intent", limit=2048),
        )
        # ``Design`` will validate references; this creates a local net-name check.
        Net.from_dict(net.to_dict(), path)
        return cls(
            net.id,
            net.name,
            net.endpoints,
            net.net_class,
            net.intent,
            net.power_domain,
            net.interface,
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
class CircuitPlan:
    """Versioned, non-geometric circuit plan accepted from an untrusted planner."""

    design_id: str
    summary: str
    assumptions: tuple[str, ...]
    components: tuple[PlanComponent, ...]
    nets: tuple[PlanNet, ...]
    notes: tuple[str, ...]
    blocks: tuple[PlanBlock, ...] = ()
    power_domains: tuple[PlanPowerDomain, ...] = ()
    interfaces: tuple[PlanInterface, ...] = ()
    constraints: tuple[PlanConstraint, ...] = ()
    assertions: tuple[PlanAssertion, ...] = ()
    version: int = AGENT_PLAN_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> CircuitPlan:
        if not isinstance(value, Mapping):
            raise ValidationError("$ must be an object")
        version = value.get("version")
        if (
            value.get("schema") != AGENT_PLAN_SCHEMA
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version not in {AGENT_PLAN_LEGACY_VERSION, AGENT_PLAN_VERSION}
        ):
            raise ValidationError("unsupported circuit plan schema/version")
        required = {
            "schema",
            "version",
            "design_id",
            "summary",
            "assumptions",
            "components",
            "nets",
            "notes",
        }
        if version == AGENT_PLAN_VERSION:
            required.update(
                {
                    "blocks",
                    "power_domains",
                    "interfaces",
                    "constraints",
                    "assertions",
                }
            )
        item = _strict_mapping(
            value,
            "$",
            required=required,
            optional=set(),
        )
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
            PlanNet.from_dict(entry, f"$.nets[{index}]", version=version)
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

        def parse_v2_array(
            name: str,
            limit: int,
            parser: Any,
        ) -> tuple[Any, ...]:
            if version == AGENT_PLAN_LEGACY_VERSION:
                return ()
            raw = item[name]
            if not isinstance(raw, list) or len(raw) > limit:
                raise ValidationError(f"$.{name} must contain at most {limit} items")
            return tuple(
                sorted(
                    (
                        parser(entry, f"$.{name}[{index}]")
                        for index, entry in enumerate(raw)
                    ),
                    key=lambda entry: entry.id,
                )
            )

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
            blocks=parse_v2_array("blocks", MAX_PLAN_BLOCKS, PlanBlock.from_dict),
            power_domains=parse_v2_array(
                "power_domains", MAX_PLAN_POWER_DOMAINS, PlanPowerDomain.from_dict
            ),
            interfaces=parse_v2_array(
                "interfaces", MAX_PLAN_INTERFACES, PlanInterface.from_dict
            ),
            constraints=parse_v2_array(
                "constraints", MAX_PLAN_CONSTRAINTS, PlanConstraint.from_dict
            ),
            assertions=parse_v2_array(
                "assertions", MAX_PLAN_ASSERTIONS, PlanAssertion.from_dict
            ),
            version=version,
        )
        _validate_plan_references(plan)
        return plan

    def to_dict(self) -> dict[str, Any]:
        nets = [entry.to_dict() for entry in self.nets]
        if self.version == AGENT_PLAN_VERSION:
            for net, parsed in zip(nets, self.nets, strict=True):
                net["power_domain"] = parsed.power_domain
                net["interface"] = parsed.interface
        result: dict[str, Any] = {
            "schema": AGENT_PLAN_SCHEMA,
            "version": self.version,
            "design_id": self.design_id,
            "summary": self.summary,
            "assumptions": sorted(self.assumptions),
            "components": [entry.to_dict() for entry in self.components],
            "nets": nets,
            "notes": sorted(self.notes),
        }
        if self.version == AGENT_PLAN_VERSION:
            result.update(
                {
                    "blocks": [entry.to_dict() for entry in self.blocks],
                    "power_domains": [entry.to_dict() for entry in self.power_domains],
                    "interfaces": [entry.to_dict() for entry in self.interfaces],
                    "constraints": [entry.to_dict() for entry in self.constraints],
                    "assertions": [entry.to_dict() for entry in self.assertions],
                }
            )
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def _validate_plan_references(plan: CircuitPlan) -> None:
    component_ids = {entry.id for entry in plan.components}
    net_ids = {entry.id for entry in plan.nets}
    seen_endpoints: set[tuple[str, str]] = set()
    endpoint_records: set[Endpoint] = set()
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
            endpoint_records.add(endpoint)
    if not any(net.name.lstrip("/").casefold() == "gnd" for net in plan.nets):
        raise ValidationError("circuit plan must declare a GND reference net")
    if plan.version == AGENT_PLAN_LEGACY_VERSION:
        return

    def unique_ids(entries: Iterable[Any], label: str) -> set[str]:
        identifiers = [entry.id for entry in entries]
        if len(set(identifiers)) != len(identifiers):
            raise ValidationError(f"circuit plan {label} ids must be unique")
        return set(identifiers)

    block_ids = unique_ids(plan.blocks, "block")
    power_domain_ids = unique_ids(plan.power_domains, "power-domain")
    interface_ids = unique_ids(plan.interfaces, "interface")
    constraint_ids = unique_ids(plan.constraints, "constraint")
    assertion_ids = unique_ids(plan.assertions, "assertion")

    semantic_groups = {
        "component": component_ids,
        "net": net_ids,
        "block": block_ids,
        "power-domain": power_domain_ids,
        "interface": interface_ids,
    }
    seen_semantic: dict[str, str] = {}
    for label, identifiers in semantic_groups.items():
        for identifier in identifiers:
            previous = seen_semantic.get(identifier)
            if previous is not None:
                raise ValidationError(
                    f"circuit plan id {identifier!r} is ambiguous between {previous} and {label}"
                )
            seen_semantic[identifier] = label

    assigned_components = [
        component for block in plan.blocks for component in block.components
    ]
    if len(assigned_components) != len(set(assigned_components)):
        raise ValidationError("circuit plan assigns a component to multiple blocks")
    if set(assigned_components) != component_ids:
        missing = component_ids - set(assigned_components)
        unknown = set(assigned_components) - component_ids
        detail = [
            *(f"missing {item}" for item in sorted(missing)),
            *(f"unknown {item}" for item in sorted(unknown)),
        ]
        raise ValidationError(
            "circuit plan blocks must cover every component exactly once: "
            + ", ".join(detail)
        )
    parents = {block.id: block.parent for block in plan.blocks}
    for block in plan.blocks:
        if block.parent is not None and block.parent not in block_ids:
            raise ValidationError(
                f"circuit plan block {block.id} references unknown parent {block.parent}"
            )
        cursor = block.parent
        visited = {block.id}
        while cursor is not None:
            if cursor in visited:
                raise ValidationError("circuit plan block hierarchy contains a cycle")
            visited.add(cursor)
            cursor = parents.get(cursor)

    net_by_endpoint = {endpoint: net for net in plan.nets for endpoint in net.endpoints}
    for domain in plan.power_domains:
        if domain.source.component not in component_ids:
            raise ValidationError(
                f"power domain {domain.id} references unknown source component"
            )
        source_net = net_by_endpoint.get(domain.source)
        if source_net is None or source_net.power_domain != domain.id:
            raise ValidationError(
                f"power domain {domain.id} source must occur on a net assigned to that domain"
            )
    for net in plan.nets:
        if net.power_domain is not None and net.power_domain not in power_domain_ids:
            raise ValidationError(
                f"circuit plan net {net.id} references unknown power domain {net.power_domain}"
            )
        if net.interface is not None and net.interface not in interface_ids:
            raise ValidationError(
                f"circuit plan net {net.id} references unknown interface {net.interface}"
            )
    for interface in plan.interfaces:
        if interface.power_domain not in power_domain_ids:
            raise ValidationError(
                f"interface {interface.id} references unknown power domain"
            )
        bound_endpoints = {
            endpoint
            for net in plan.nets
            if net.interface == interface.id
            for endpoint in net.endpoints
        }
        if not bound_endpoints:
            raise ValidationError(
                f"interface {interface.id} must be assigned to at least one net"
            )
        if not set(interface.members) <= bound_endpoints:
            raise ValidationError(
                f"interface {interface.id} contains members outside its assigned nets"
            )

    target_ids = set(seen_semantic) | {"board"}
    if constraint_ids & set(seen_semantic):
        raise ValidationError("circuit plan constraint ids must be globally unique")
    if assertion_ids & set(seen_semantic):
        raise ValidationError("circuit plan assertion ids must be globally unique")
    reserved_constraints = {"agent_manufacturing_rules", "agent_routing"}
    if constraint_ids & reserved_constraints:
        raise ValidationError("circuit plan uses a reserved runtime constraint id")
    if constraint_ids & {f"assert.{item}" for item in assertion_ids}:
        raise ValidationError("circuit plan assertion and constraint ids collide")
    for constraint in plan.constraints:
        unknown = set(constraint.targets) - target_ids
        if unknown:
            raise ValidationError(
                f"constraint {constraint.id} references unknown targets: "
                + ", ".join(sorted(unknown))
            )
        _validate_plan_constraint(
            constraint,
            component_ids,
            net_ids,
            {net.id: net for net in plan.nets},
        )
    for assertion in plan.assertions:
        if assertion.kind in {
            "all_power_inputs_connected",
            "components_share_net",
        }:
            expected = component_ids
        elif assertion.kind == "net_endpoint_count":
            expected = net_ids
        else:
            expected = interface_ids
        unknown = set(assertion.targets) - expected
        if unknown:
            raise ValidationError(
                f"assertion {assertion.id} has invalid targets: "
                + ", ".join(sorted(unknown))
            )
        if (
            assertion.kind in {"net_endpoint_count", "interface_net_count"}
            and len(assertion.targets) != 1
        ):
            raise ValidationError(
                f"assertion {assertion.id} requires exactly one target"
            )


def _validate_plan_constraint(
    constraint: PlanConstraint,
    component_ids: set[str],
    net_ids: set[str],
    nets_by_id: Mapping[str, PlanNet],
) -> None:
    if constraint.kind == "functional_group":
        if len(constraint.targets) < 2 or not set(constraint.targets) <= component_ids:
            raise ValidationError(
                f"constraint {constraint.id} requires at least two component targets"
            )
        if set(constraint.parameters) != {"max_diameter_mm"}:
            raise ValidationError(
                f"constraint {constraint.id} requires max_diameter_mm only"
            )
        _positive_number(
            constraint.parameters["max_diameter_mm"],
            f"constraint {constraint.id}.max_diameter_mm",
        )
    elif constraint.kind == "edge_placement":
        if len(constraint.targets) != 1 or constraint.targets[0] not in component_ids:
            raise ValidationError(
                f"constraint {constraint.id} requires one component target"
            )
        if set(constraint.parameters) != {"edge", "max_edge_distance_mm"}:
            raise ValidationError(
                f"constraint {constraint.id} requires edge and max_edge_distance_mm"
            )
        if constraint.parameters["edge"] not in {"left", "right", "top", "bottom"}:
            raise ValidationError(f"constraint {constraint.id} has an invalid edge")
        _positive_number(
            constraint.parameters["max_edge_distance_mm"],
            f"constraint {constraint.id}.max_edge_distance_mm",
        )
    elif constraint.kind == "routing":
        if not set(constraint.targets) <= net_ids:
            raise ValidationError(
                f"constraint {constraint.id} routing targets must be nets"
            )
        if set(constraint.parameters) != {"width_mm"}:
            raise ValidationError(f"constraint {constraint.id} requires width_mm only")
        _positive_number(
            constraint.parameters["width_mm"],
            f"constraint {constraint.id}.width_mm",
        )
    elif constraint.kind == "connector_pinout":
        if len(constraint.targets) != 1 or constraint.targets[0] not in component_ids:
            raise ValidationError(
                f"constraint {constraint.id} requires one component target"
            )
        pin_parameters = {
            name: value
            for name, value in constraint.parameters.items()
            if name.startswith("pin.")
        }
        if (
            constraint.parameters.get("require_complete") is not True
            or not pin_parameters
            or set(constraint.parameters) != {"require_complete", *pin_parameters}
        ):
            raise ValidationError(
                f"constraint {constraint.id} requires require_complete=true and pin.* mappings only"
            )
        for name, value in pin_parameters.items():
            pin = name.removeprefix("pin.")
            if not _PHYSICAL_PIN_NUMBER.fullmatch(pin):
                raise ValidationError(
                    f"constraint {constraint.id} contains an invalid pin mapping"
                )
            if value is not None and value not in net_ids:
                raise ValidationError(
                    f"constraint {constraint.id} pin {pin} references an unknown net"
                )
    elif constraint.kind == "net_label":
        if len(constraint.targets) != 1 or constraint.targets[0] not in net_ids:
            raise ValidationError(f"constraint {constraint.id} requires one net target")
        if set(constraint.parameters) != {"label"} or not isinstance(
            constraint.parameters["label"], str
        ):
            raise ValidationError(f"constraint {constraint.id} requires label only")
        if constraint.parameters["label"] != nets_by_id[constraint.targets[0]].name:
            raise ValidationError(
                f"constraint {constraint.id} label disagrees with its target net"
            )
    elif constraint.kind == "placement_region":
        if not set(constraint.targets) <= component_ids:
            raise ValidationError(
                f"constraint {constraint.id} placement targets must be components"
            )
        if (
            set(constraint.parameters) != {"region"}
            or constraint.parameters["region"] not in BOARD_REGIONS
        ):
            raise ValidationError(
                f"constraint {constraint.id} requires one supported named region"
            )
    elif constraint.kind == "board_keepout":
        if constraint.targets != ("board",):
            raise ValidationError(
                f"constraint {constraint.id} keepout target must be board"
            )
        expected = {"anchor", "height_mm", "layers", "width_mm"}
        if set(constraint.parameters) != expected:
            raise ValidationError(
                f"constraint {constraint.id} requires anchor, width_mm, height_mm, and layers"
            )
        if constraint.parameters["anchor"] not in BOARD_REGIONS:
            raise ValidationError(f"constraint {constraint.id} has an invalid anchor")
        if constraint.parameters["layers"] not in COPPER_LAYER_SCOPES:
            raise ValidationError(f"constraint {constraint.id} has invalid layers")
        for name in ("width_mm", "height_mm"):
            _positive_number(
                constraint.parameters[name],
                f"constraint {constraint.id}.{name}",
            )
    elif constraint.kind == "differential_pair":
        if len(constraint.targets) != 2 or not set(constraint.targets) <= net_ids:
            raise ValidationError(
                f"constraint {constraint.id} requires exactly two net targets"
            )
        expected = {
            "gap_mm",
            "gap_tolerance_mm",
            "max_length_mismatch_mm",
            "min_coupled_length_ratio",
            "width_mm",
        }
        if set(constraint.parameters) != expected:
            raise ValidationError(
                f"constraint {constraint.id} has an incomplete differential-pair contract"
            )
        for name in ("width_mm", "gap_mm", "gap_tolerance_mm"):
            _positive_number(
                constraint.parameters[name],
                f"constraint {constraint.id}.{name}",
            )
        mismatch = constraint.parameters["max_length_mismatch_mm"]
        if (
            isinstance(mismatch, bool)
            or not isinstance(mismatch, (int, float))
            or not math.isfinite(float(mismatch))
            or float(mismatch) < 0
        ):
            raise ValidationError(
                f"constraint {constraint.id}.max_length_mismatch_mm must be non-negative"
            )
        ratio = constraint.parameters["min_coupled_length_ratio"]
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(float(ratio))
            or not 0 < float(ratio) <= 1
        ):
            raise ValidationError(
                f"constraint {constraint.id}.min_coupled_length_ratio must be in (0, 1]"
            )


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
        evidence = [
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
            )
        ]
        if candidate.datasheet:
            evidence.append(
                Provenance(
                    id="local_kicad_datasheet_reference",
                    kind="datasheet",
                    source="KiCad installed symbol metadata",
                    locator=candidate.datasheet,
                    acquired_at=None,
                    method="local_library_reference",
                    confidence=0.4,
                    notes=(
                        "The locator was copied from the installed symbol. PCBDraft "
                        "did not fetch, authenticate, or review the referenced document."
                    ),
                )
            )
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
            sourcing={
                "status": "not_checked",
                "manufacturer_identity": (
                    "planner_claim_unverified"
                    if component.manufacturer is not None
                    else "not_specified"
                ),
                "mpn_identity": (
                    "planner_claim_unverified"
                    if component.mpn is not None
                    else "not_specified"
                ),
            },
            manufacturing={},
            models={},
            bom=component.on_board,
            trust="extracted",
            evidence=tuple(evidence),
            alternates=(),
        )


def _library_component(symbol: str) -> Any:
    schematic = Schematic.create(name="pcbdraft-symbol-probe")
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
    qualification: ComponentQualificationReport
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
                "Preflight checks topology, local library availability, and exact footprint pad numbers.",
                "It does not establish electrical, regulatory, RF, thermal, layout, or manufacturing correctness.",
            ],
            "component_qualification": self.qualification.to_dict(),
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
                "min_reference_stitching_vias": 0,
                "reference_connection_policy": "ensure_connected",
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


def _is_ground_net(net: Net) -> bool:
    return _normal_key(net.name) in {
        "0v",
        "agnd",
        "dgnd",
        "gnd",
        "ground",
        "pgnd",
        "vss",
        "vssa",
        "vssd",
    }


def _power_pin_expectation(name: str) -> str | None:
    key = _normal_key(name)
    if key in {
        "agnd",
        "dgnd",
        "epad",
        "gnd",
        "ground",
        "pgnd",
        "vss",
        "vssa",
        "vssd",
    }:
        return "ground"
    if key in {
        "avcc",
        "dvcc",
        "vbat",
        "vcc",
        "vcca",
        "vccd",
        "vdd",
        "vdda",
        "vddd",
        "vin",
        "vsupply",
    }:
        return "supply"
    return None


def _plausible_power_source(component: Component, pin: PinDefinition) -> bool:
    if pin.electrical_type == "power_out":
        return True
    role = str(component.attributes.get("planner_role", "")).casefold()
    return any(
        token in role
        for token in (
            "battery",
            "connector",
            "converter",
            "power_input",
            "regulator",
            "supply",
        )
    )


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

    qualification = qualify_components(design, graph)
    findings: list[PlanReviewFinding] = []
    mapping_failures = qualification.pad_mapping_failures
    findings.append(
        PlanReviewFinding(
            id="parts.footprint_pad_mapping",
            severity="error" if mapping_failures else "info",
            outcome="fail" if mapping_failures else "pass",
            summary=(
                "One or more selected symbols cannot be mapped to actual pads in the selected local footprint."
                if mapping_failures
                else "Every on-board symbol pin maps to an actual pad number in the selected local footprint."
            ),
            evidence=mapping_failures
            or ("all selected local symbol/footprint mappings",),
            action="Select a compatible installed footprint or provide a reviewed pin-to-pad contract.",
            requires_human=bool(mapping_failures),
        )
    )
    provisional = qualification.provisional_references
    findings.append(
        PlanReviewFinding(
            id="parts.datasheet_and_identity_qualification",
            severity="warning" if provisional else "info",
            outcome="unknown" if provisional else "pass",
            summary=(
                "Stock-library parts remain provisional: their datasheet locators and procurement identities have not been qualified."
                if provisional
                else "Every selected part has at least a rule-attributed component record; production qualification remains a separate gate."
            ),
            evidence=provisional or ("no local-library-only parts",),
            action="Review the exact manufacturer part, package, pin map, ratings, lifecycle, and datasheet before fabrication.",
            requires_human=True,
        )
    )
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

    polarity_issues: list[str] = []
    classified_power_pins = 0
    for component_id, component in sorted(components.items()):
        if component.attributes.get("exclude_from_board", False):
            continue
        for pin in graph.get(component.part_id).pins:
            expectation = _power_pin_expectation(pin.name)
            if expectation is None or pin.electrical_type != "power_in":
                continue
            net = endpoint_nets.get((component_id, pin.number))
            if net is None:
                continue
            classified_power_pins += 1
            correct = (
                _is_ground_net(net)
                if expectation == "ground"
                else (net.net_class == "power" and not _is_ground_net(net))
            )
            if not correct:
                polarity_issues.append(
                    f"{component.reference}.{pin.number} ({pin.name}) -> {net.name}; expected {expectation}"
                )
    findings.append(
        PlanReviewFinding(
            id="power.pin_polarity",
            severity="error" if polarity_issues else "info",
            outcome="fail" if polarity_issues else "pass",
            summary=(
                "A locally identified supply or ground pin is connected to the wrong power class."
                if polarity_issues
                else "Locally identifiable supply and ground pins have consistent power-net polarity."
            ),
            evidence=tuple(polarity_issues)
            or (f"{classified_power_pins} classified power pins checked",),
            action="Reconnect VSS/GND pins to ground and VDD/VCC/VBAT pins to a non-ground supply, then verify the datasheet.",
            requires_human=True,
        )
    )

    unsourced_rails: list[str] = []
    implausible_sources: list[str] = []
    for net in design.nets:
        if net.net_class != "power" or _normal_key(net.name) in {
            "gnd",
            "ground",
            "vss",
        }:
            continue
        has_source = False
        for endpoint in net.endpoints:
            component = components.get(endpoint.component)
            if component is not None:
                pin = graph.get(component.part_id).pin(endpoint.pin)
                if pin is not None and pin.electrical_type == "power_out":
                    has_source = True
                    break
                if endpoint.role == "source" and pin is not None:
                    if _plausible_power_source(component, pin):
                        has_source = True
                        break
                    implausible_sources.append(
                        f"{net.name}: {component.reference}.{pin.number} ({pin.name})"
                    )
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
    findings.append(
        PlanReviewFinding(
            id="power.source_role_evidence",
            severity="error" if implausible_sources else "info",
            outcome="fail" if implausible_sources else "pass",
            summary=(
                "A planner-labelled power source is not a local power-output pin or a component with a physical source role."
                if implausible_sources
                else "Every planner-labelled rail source has a plausible local pin type or physical source role."
            ),
            evidence=tuple(sorted(set(implausible_sources)))
            or ("all explicit source roles",),
            action="Use a connector, battery, regulator, converter, or local power-output pin as the declared rail source.",
            requires_human=True,
        )
    )

    contention: list[str] = []
    for net in design.nets:
        drivers: list[str] = []
        for endpoint in net.endpoints:
            component = components[endpoint.component]
            pin = graph.get(component.part_id).pin(endpoint.pin)
            if pin is not None and pin.electrical_type in {"output", "power_out"}:
                drivers.append(f"{component.reference}.{pin.number}")
        if len(drivers) > 1:
            contention.append(f"{net.name}: {', '.join(sorted(drivers))}")
    findings.append(
        PlanReviewFinding(
            id="electrical.output_contention",
            severity="error" if contention else "info",
            outcome="fail" if contention else "pass",
            summary=(
                "A net contains multiple push-pull or power-output pins."
                if contention
                else "No net contains multiple locally identified push-pull or power-output pins."
            ),
            evidence=tuple(contention) or ("declared net drivers",),
            action="Resolve output contention or use an explicitly open-drain/open-collector topology.",
            requires_human=bool(contention),
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
        i2c_by_id = {net.id: net for net in i2c_nets}
        pullups_by_net: dict[str, list[str]] = {net.id: [] for net in i2c_nets}
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
                net.id: net
                for net in design.nets
                if any(endpoint.component == component_id for endpoint in net.endpoints)
            }
            i2c_targets = set(component_nets) & set(i2c_by_id)
            has_non_ground_power = any(
                net.net_class == "power" and not _is_ground_net(net)
                for net in component_nets.values()
            )
            if has_non_ground_power:
                for net_id in i2c_targets:
                    pullups_by_net[net_id].append(component.reference)
        missing_pullups = [
            i2c_by_id[net_id].name
            for net_id, references in pullups_by_net.items()
            if not references
        ]
        pullup_evidence = tuple(
            f"{i2c_by_id[net_id].name}: {', '.join(sorted(references))}"
            for net_id, references in sorted(pullups_by_net.items())
            if references
        )
        findings.append(
            PlanReviewFinding(
                id="interface.i2c_pullup_evidence",
                severity="error" if missing_pullups else "warning",
                outcome="fail" if missing_pullups else "unknown",
                summary=(
                    "One or more I2C signal nets lack an explicit resistor path to a non-ground power rail."
                    if missing_pullups
                    else "Every I2C signal has an explicit pull-up path, but resistance and bus-budget evidence still require review."
                ),
                evidence=tuple(f"missing: {name}" for name in missing_pullups)
                + pullup_evidence,
                action="Verify pull-up topology, resistance, voltage, bus capacitance, and any internal pull-ups against the selected devices' datasheets.",
                requires_human=True,
            )
        )

    passive_shorts: list[str] = []
    polarized_leds = 0
    led_polarity_issues: list[str] = []
    for component_id, component in sorted(components.items()):
        part = graph.get(component.part_id)
        pins = tuple(part.pins)
        if len(pins) == 2 and part.symbol in {
            "Device:C",
            "Device:D",
            "Device:Fuse",
            "Device:L",
            "Device:LED",
            "Device:R",
        }:
            assigned = [endpoint_nets.get((component_id, pin.number)) for pin in pins]
            if assigned[0] is not None and assigned[0] is assigned[1]:
                passive_shorts.append(
                    f"{component.reference}: both terminals -> {assigned[0].name}"
                )
        if not part.symbol.endswith(":LED"):
            continue
        cathode = next(
            (pin for pin in pins if _normal_key(pin.name) in {"cathode", "k"}),
            None,
        )
        anode = next(
            (pin for pin in pins if _normal_key(pin.name) in {"a", "anode"}),
            None,
        )
        if cathode is None or anode is None:
            continue
        cathode_net = endpoint_nets.get((component_id, cathode.number))
        anode_net = endpoint_nets.get((component_id, anode.number))
        if cathode_net is None or anode_net is None:
            continue
        if _is_ground_net(cathode_net) or _is_ground_net(anode_net):
            polarized_leds += 1
            if not _is_ground_net(cathode_net) or _is_ground_net(anode_net):
                led_polarity_issues.append(
                    f"{component.reference}: K->{cathode_net.name}, A->{anode_net.name}"
                )
    findings.append(
        PlanReviewFinding(
            id="electrical.two_terminal_short",
            severity="error" if passive_shorts else "info",
            outcome="fail" if passive_shorts else "pass",
            summary=(
                "A two-terminal passive or diode has both pins assigned to the same net."
                if passive_shorts
                else "No selected two-terminal passive or diode is shorted by its net assignments."
            ),
            evidence=tuple(passive_shorts) or ("two-terminal component connectivity",),
            action="Connect the two terminals to their intended distinct nets or remove the component.",
            requires_human=bool(passive_shorts),
        )
    )
    findings.append(
        PlanReviewFinding(
            id="electrical.led_ground_polarity",
            severity="error" if led_polarity_issues else "info",
            outcome="fail" if led_polarity_issues else "pass",
            summary=(
                "A ground-referenced LED has its anode/cathode orientation reversed."
                if led_polarity_issues
                else "Ground-referenced LEDs with locally named A/K pins have consistent polarity."
            ),
            evidence=tuple(led_polarity_issues)
            or (f"{polarized_leds} ground-referenced LEDs checked",),
            action="Connect the cathode (K) toward ground and the anode (A) toward the current-limited positive path.",
            requires_human=bool(led_polarity_issues),
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
                severity="error" if not capacitors else "warning",
                outcome="fail" if not capacitors else "unknown",
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

    for assertion in plan.assertions:
        failure = evaluate_assertion(design, graph, assertion.to_ir())
        findings.append(
            PlanReviewFinding(
                id=f"assertion.{assertion.id}",
                severity="error" if failure else "info",
                outcome="fail" if failure else "pass",
                summary=(
                    failure
                    if failure
                    else f"Deterministic assertion {assertion.kind} passed."
                ),
                evidence=assertion.targets,
                action=(
                    "Revise the semantic circuit plan so the declared assertion is true."
                    if failure
                    else assertion.rationale
                ),
                requires_human=False,
            )
        )

    return AgentPlanReview(
        design_id=design.design_id,
        request_hash=hashlib.sha256(request.canonical_bytes()).hexdigest(),
        plan_hash=hashlib.sha256(plan.canonical_bytes()).hexdigest(),
        qualification=qualification,
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
    placements = _seed_placements(plan.components, request.board)
    if plan.version == AGENT_PLAN_VERSION:
        component_blocks = {
            component_id: block.id
            for block in plan.blocks
            for component_id in block.components
        }
        ir_blocks = tuple(
            FunctionalBlock(
                id=block.id,
                kind=block.kind,
                name=block.name,
                version="agent_plan_v2",
                intent=block.intent,
                components=block.components,
                provenance=("agent_plan",),
            )
            for block in plan.blocks
        )
    else:
        component_blocks = {
            component.id: f"block.{component.id}" for component in plan.components
        }
        ir_blocks = tuple(
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
        )
    ir_components = tuple(
        Component(
            id=item.id,
            reference=item.reference,
            part_id=parts_by_component[item.id].id,
            value=item.value,
            block_id=component_blocks[item.id],
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
            source="PCBDraft conversation",
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
        blocks=ir_blocks,
        power_domains=tuple(domain.to_ir() for domain in plan.power_domains),
        interfaces=tuple(interface.to_ir() for interface in plan.interfaces),
        components=ir_components,
        nets=tuple(
            Net(
                id=item.id,
                name=item.name,
                endpoints=item.endpoints,
                net_class=item.net_class,
                power_domain=item.power_domain,
                interface=item.interface,
                intent=item.intent,
            )
            for item in plan.nets
        ),
        constraints=(
            *_baseline_agent_constraints(request, plan),
            *(constraint.to_ir() for constraint in plan.constraints),
            *(assertion.to_ir() for assertion in plan.assertions),
        ),
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
            "generator": f"agent_plan_v{plan.version}",
            "assurance": "provisional",
            "requirements_hash": hashlib.sha256(request.canonical_bytes()).hexdigest(),
            "plan_hash": hashlib.sha256(plan.canonical_bytes()).hexdigest(),
            "requested_parts": list(request.requested_parts),
            "planner_assumptions": sorted({*request.assumptions, *plan.assumptions}),
            "part_catalog_schema": PART_CATALOG_SCHEMA,
            **(
                {
                    "block_hierarchy": [
                        {"id": block.id, "parent": block.parent}
                        for block in plan.blocks
                    ]
                }
                if plan.version == AGENT_PLAN_VERSION
                else {}
            ),
        },
    )
    routed_widths: dict[str, tuple[float, str]] = {}
    differential_membership: dict[str, str] = {}
    for constraint in plan.constraints:
        if constraint.kind in {"routing", "differential_pair"}:
            width = float(constraint.parameters["width_mm"])
            for target in constraint.targets:
                previous = routed_widths.get(target)
                if previous is not None and not math.isclose(
                    previous[0], width, rel_tol=0, abs_tol=1e-12
                ):
                    raise ValidationError(
                        f"net {target} has conflicting route widths in constraints "
                        f"{previous[1]} and {constraint.id}"
                    )
                routed_widths[target] = (width, constraint.id)
        if constraint.kind == "differential_pair":
            for target in constraint.targets:
                previous_pair = differential_membership.get(target)
                if previous_pair is not None:
                    raise ValidationError(
                        f"net {target} belongs to multiple differential pairs: "
                        f"{previous_pair} and {constraint.id}"
                    )
                differential_membership[target] = constraint.id
        if (
            constraint.kind in {"routing", "differential_pair"}
            and float(constraint.parameters["width_mm"]) < request.board.min_track_mm
        ):
            raise ValidationError(
                f"routing constraint {constraint.id} width is below the board minimum"
            )
        if (
            constraint.kind == "differential_pair"
            and float(constraint.parameters["gap_mm"]) < request.board.min_clearance_mm
        ):
            raise ValidationError(
                f"differential-pair constraint {constraint.id} gap is below the board minimum"
            )
        if constraint.kind == "board_keepout":
            try:
                anchored_rectangle(
                    str(constraint.parameters["anchor"]),
                    float(constraint.parameters["width_mm"]),
                    float(constraint.parameters["height_mm"]),
                    request.board.width_mm,
                    request.board.height_mm,
                    request.board.edge_clearance_mm,
                )
            except ValueError as exc:
                raise ValidationError(
                    f"board keepout {constraint.id} does not fit the board"
                ) from exc
    design.assert_valid()
    graph.assert_design(design, check_libraries=True, allow_provisional=True)
    qualification = qualify_components(design, graph)
    if qualification.pad_mapping_failures:
        raise ValidationError(
            "component qualification failed: part.footprint_pad_mapping: "
            + ", ".join(qualification.pad_mapping_failures)
        )
    design = replace(
        design,
        metadata={
            **design.metadata,
            "component_qualification_schema": COMPONENT_QUALIFICATION_SCHEMA,
            "component_qualification_hash": qualification.sha256(),
        },
    )
    design.assert_valid()
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
    """Strict version-2 schema for hierarchical, non-geometric PCB intent."""
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
    nullable_identifier = {
        "anyOf": [
            {"type": "string", "maxLength": 128},
            {"type": "null"},
        ]
    }
    parameter = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "value"],
        "properties": {
            "name": {"type": "string", "maxLength": 128},
            "value": {
                "anyOf": [
                    {"type": "string", "maxLength": 512},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                ]
            },
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
            "blocks",
            "power_domains",
            "interfaces",
            "constraints",
            "assertions",
            "notes",
        ],
        "properties": {
            "schema": {"type": "string", "const": AGENT_PLAN_SCHEMA},
            "version": {"type": "integer", "const": AGENT_PLAN_VERSION},
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
                    "required": [
                        "id",
                        "name",
                        "endpoints",
                        "net_class",
                        "power_domain",
                        "interface",
                        "intent",
                    ],
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
                        "power_domain": nullable_identifier,
                        "interface": nullable_identifier,
                        "intent": {"type": "string", "maxLength": 2048},
                    },
                },
            },
            "blocks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PLAN_BLOCKS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "kind",
                        "name",
                        "intent",
                        "parent",
                        "components",
                    ],
                    "properties": {
                        "id": {"type": "string", "maxLength": 128},
                        "kind": {"type": "string", "maxLength": 128},
                        "name": {"type": "string", "maxLength": 256},
                        "intent": {"type": "string", "maxLength": 2048},
                        "parent": nullable_identifier,
                        "components": {
                            "type": "array",
                            "maxItems": MAX_PLAN_COMPONENTS,
                            "items": {"type": "string", "maxLength": 128},
                        },
                    },
                },
            },
            "power_domains": {
                "type": "array",
                "maxItems": MAX_PLAN_POWER_DOMAINS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "nominal_v",
                        "min_v",
                        "max_v",
                        "max_current_a",
                        "source",
                        "intent",
                    ],
                    "properties": {
                        "id": {"type": "string", "maxLength": 128},
                        "nominal_v": {"type": "number", "minimum": 0},
                        "min_v": {"type": "number", "minimum": 0},
                        "max_v": {"type": "number", "minimum": 0},
                        "max_current_a": {"type": "number", "minimum": 0},
                        "source": endpoint,
                        "intent": {"type": "string", "maxLength": 1024},
                    },
                },
            },
            "interfaces": {
                "type": "array",
                "maxItems": MAX_PLAN_INTERFACES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "kind",
                        "power_domain",
                        "members",
                        "controller",
                        "parameters",
                        "intent",
                    ],
                    "properties": {
                        "id": {"type": "string", "maxLength": 128},
                        "kind": {"type": "string", "maxLength": 128},
                        "power_domain": {"type": "string", "maxLength": 128},
                        "members": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 128,
                            "items": endpoint,
                        },
                        "controller": {"anyOf": [endpoint, {"type": "null"}]},
                        "parameters": {
                            "type": "array",
                            "maxItems": MAX_PLAN_PARAMETERS,
                            "items": parameter,
                        },
                        "intent": {"type": "string", "maxLength": 1024},
                    },
                },
            },
            "constraints": {
                "type": "array",
                "maxItems": MAX_PLAN_CONSTRAINTS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "kind",
                        "targets",
                        "parameters",
                        "severity",
                        "rationale",
                    ],
                    "properties": {
                        "id": {"type": "string", "maxLength": 128},
                        "kind": {
                            "type": "string",
                            "enum": sorted(_PLAN_CONSTRAINT_KINDS),
                        },
                        "targets": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 128,
                            "items": {"type": "string", "maxLength": 128},
                        },
                        "parameters": {
                            "type": "array",
                            "maxItems": MAX_PLAN_PARAMETERS,
                            "items": parameter,
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["advisory", "required", "release_blocking"],
                        },
                        "rationale": {"type": "string", "maxLength": 2048},
                    },
                },
            },
            "assertions": {
                "type": "array",
                "maxItems": MAX_PLAN_ASSERTIONS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "kind",
                        "targets",
                        "minimum",
                        "maximum",
                        "severity",
                        "rationale",
                    ],
                    "properties": {
                        "id": {"type": "string", "maxLength": 120},
                        "kind": {"type": "string", "enum": sorted(ASSERTION_KINDS)},
                        "targets": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 128,
                            "items": {"type": "string", "maxLength": 128},
                        },
                        "minimum": {
                            "anyOf": [
                                {"type": "integer", "minimum": 0},
                                {"type": "null"},
                            ]
                        },
                        "maximum": {
                            "anyOf": [
                                {"type": "integer", "minimum": 0},
                                {"type": "null"},
                            ]
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["required", "release_blocking"],
                        },
                        "rationale": {"type": "string", "maxLength": 2048},
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
