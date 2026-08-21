"""Versioned semantic contracts for conversational PCB plans.

This module owns the strict non-geometric input boundary between a planner and
the deterministic PCB compiler. It has no dependency on installed KiCad
libraries, model providers, or application persistence.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.assertions import ASSERTION_KINDS
from pcbdraft.domain.ir import (
    BoardSpec,
    Constraint,
    Endpoint,
    Interface,
    Net,
    PowerDomain,
    Scope,
    _json_value,
    _string,
    canonical_json_bytes,
)
from pcbdraft.domain.ir import (
    _identifier as _strict_identifier,
)
from pcbdraft.domain.ir import (
    normalize_stable_identifier as _normalize_identifier,
)
from pcbdraft.domain.scope import assert_scope_supported
from pcbdraft.domain.spatial_contracts import BOARD_REGIONS, COPPER_LAYER_SCOPES


def _identifier(value: Any, path: str) -> str:
    """Identifier for model-authored plan fields: normalize, then validate.

    Planning models frequently return case or spacing variants (``Power``,
    ``current limiting resistor``) that the strict IR contract rejects.  The
    deterministic normalizer maps them onto the identifier space first; the
    strict validator still runs on the normalized value.
    """

    if isinstance(value, str):
        value = _normalize_identifier(value)
    return _strict_identifier(value, path)


def _model_mapping(
    value: Any, path: str, *, required: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    """Tolerant mapping for model-authored plans: ignore unknown fields.

    The deterministic compiler and review step are the authority on plan
    soundness.  Rejecting on unknown fields here only punishes harmless model
    verbosity, so extra keys are dropped instead.
    """

    del optional
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} must be an object")
    missing = set(required) - set(value)
    if missing:
        raise ValidationError(f"{path} is missing fields: {', '.join(sorted(missing))}")
    return dict(value)


def _model_bool(value: Any, path: str) -> bool:
    """Coerce a model-provided boolean field, accepting common string forms."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off", ""}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    raise ValidationError(f"{path} must be boolean")


def _model_number(value: Any, path: str) -> float:
    """Coerce a model-provided numeric field, accepting numeric strings."""

    if isinstance(value, bool):
        raise ValidationError(f"{path} must be a finite number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        stripped = value.strip().strip("vV")
        try:
            number = float(stripped)
        except ValueError:
            raise ValidationError(f"{path} must be a finite number") from None
    else:
        raise ValidationError(f"{path} must be a finite number")
    if not math.isfinite(number):
        raise ValidationError(f"{path} must be a finite number")
    return number


def _model_endpoint(value: Any, path: str) -> Endpoint:
    """Tolerant endpoint parse for model-authored nets."""

    item = _model_mapping(value, path, required={"component", "pin"})
    return Endpoint(
        component=_identifier(item["component"], f"{path}.component"),
        pin=_string(item["pin"], f"{path}.pin", limit=64),
        role=_identifier(item.get("role", "signal"), f"{path}.role"),
    )


def _model_power_domain(value: Any, path: str) -> PowerDomain:
    """Tolerant power-domain parse for model-authored plans."""

    item = _model_mapping(
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
    )
    nominal = _model_number(item["nominal_v"], f"{path}.nominal_v")
    minimum = _model_number(item["min_v"], f"{path}.min_v")
    maximum = _model_number(item["max_v"], f"{path}.max_v")
    current = _model_number(item["max_current_a"], f"{path}.max_current_a")
    if nominal < 0 or minimum < 0 or maximum < 0 or current < 0:
        raise ValidationError(f"{path} contains a negative electrical limit")
    if not minimum <= nominal <= maximum:
        raise ValidationError(f"{path} voltage range does not contain nominal_v")
    return PowerDomain(
        id=_identifier(item["id"], f"{path}.id"),
        nominal_v=nominal,
        min_v=minimum,
        max_v=maximum,
        max_current_a=current,
        source=_model_endpoint(item["source"], f"{path}.source"),
        intent=_string(item["intent"], f"{path}.intent", limit=1024),
    )


AGENT_REQUEST_SCHEMA = "pcbdraft-agent-design-request"
AGENT_REQUEST_VERSION = 1
AGENT_PLAN_SCHEMA = "pcbdraft-circuit-plan"
AGENT_PLAN_VERSION = 2
AGENT_PLAN_LEGACY_VERSION = 1
MAX_PLAN_COMPONENTS = 128
MAX_PLAN_NETS = 512
MAX_PLAN_BLOCKS = 128
MAX_PLAN_POWER_DOMAINS = 32
MAX_PLAN_INTERFACES = 64
MAX_PLAN_CONSTRAINTS = 256
MAX_PLAN_ASSERTIONS = 128
MAX_PLAN_PARAMETERS = 128
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
        item = _model_mapping(
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
        item = _model_mapping(
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
        item = _model_mapping(
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
        # Bare library symbols (``LED`` instead of ``Device:LED``) remain valid;
        # the local part resolver reports if no installed symbol can map them.
        footprint = item["footprint"]
        if footprint is not None:
            footprint = _string(footprint, f"{path}.footprint", limit=256)
        optional_text: dict[str, str | None] = {}
        for field in ("exact_name", "manufacturer", "mpn"):
            current = item.get(field)
            optional_text[field] = (
                None
                if current is None
                else _string(current, f"{path}.{field}", limit=256)
            )
        on_board = _model_bool(item["on_board"], f"{path}.on_board")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            reference=_string(item["reference"], f"{path}.reference", limit=64),
            symbol=symbol,
            value=_string(item["value"], f"{path}.value", limit=256),
            role=_identifier(item["role"], f"{path}.role"),
            footprint=footprint,
            on_board=on_board,
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
        item = _model_mapping(
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
        parsed = _model_power_domain(value, path)
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
        item = _model_mapping(
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
        item = _model_mapping(
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
        item = _model_mapping(
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
            # Models occasionally attach count bounds to non-count assertions.
            # They carry no meaning for those kinds; ignore them deterministically
            # instead of rejecting the whole plan.
            minimum = None
            maximum = None
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
        item = _model_mapping(
            value,
            path,
            required=required,
            optional={"power_domain", "interface"} - required,
        )
        endpoints = item["endpoints"]
        if not isinstance(endpoints, list) or not endpoints or len(endpoints) > 128:
            raise ValidationError(f"{path}.endpoints must contain 1 to 128 endpoints")
        parsed = tuple(
            _model_endpoint(entry, f"{path}.endpoints[{index}]")
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
        item = _model_mapping(
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

    net_by_physical_endpoint = {
        (endpoint.component, endpoint.pin): net
        for net in plan.nets
        for endpoint in net.endpoints
    }
    for domain in plan.power_domains:
        if domain.source.component not in component_ids:
            raise ValidationError(
                f"power domain {domain.id} references unknown source component"
            )
        source_net = net_by_physical_endpoint.get(
            (domain.source.component, domain.source.pin)
        )
        if source_net is None:
            raise ValidationError(
                f"power domain {domain.id} source component/pin is not connected to any net"
            )
        if source_net.power_domain != domain.id:
            raise ValidationError(
                f"power domain {domain.id} source component/pin is connected to net {source_net.id}, which is assigned to {source_net.power_domain!r}"
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
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
                            "maxLength": 128,
                        },
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
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
                            "maxLength": 128,
                        },
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
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
                            "maxLength": 128,
                        },
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
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
                            "maxLength": 128,
                        },
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
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
                            "maxLength": 128,
                        },
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
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
                            "maxLength": 128,
                        },
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
                        "id": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_.-]{0,127}$",
                            "maxLength": 120,
                        },
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
