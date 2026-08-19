"""Typed, policy-independent execution boundary for PCB agent tools.

PCBDraft exposes two tool layers to models, both defined here or derived from
this registry: the high-level macro tools (``pcb_plan_request`` …
``pcb_build_release``), kept as compatibility macros and shortcuts for simple
projects, and the domain router tools (``pcb_project``, ``pcb_library``, …)
backed by :mod:`pcbdraft.agent.capability_registry`.  The agent selects among
them freely — this module only guarantees execution integrity.

The executor keeps the authority that should remain local: a closed tool
registry, strict arguments, status preconditions, optimistic revision checks,
and fixed service-method dispatch.  Providers can therefore propose an action
without gaining arbitrary Python, filesystem, or application-service access.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, cast

from pcbdraft.agent.ports import PCBToolServicePort
from pcbdraft.agent.repair import (
    MAX_AUTOMATIC_REPAIRS,
    MAX_REPAIR_FINDINGS,
    REPAIR_FEEDBACK_SCHEMA,
    REPAIR_FEEDBACK_VERSION,
    normalize_repair_feedback,
)
from pcbdraft.core.errors import ValidationError
from pcbdraft.core.redaction import sanitize_user_text

ToolSource = Literal["runtime_policy", "model", "mcp", "user"]
ToolEffect = Literal[
    "conversation_write",
    "candidate_write",
    "evidence_write",
    "staged_write",
    "authoritative_write",
]
ToolRisk = Literal["low", "medium", "high"]

_TOOL_SOURCES = frozenset({"runtime_policy", "model", "mcp", "user"})
_TOOL_EFFECTS = frozenset(
    {
        "conversation_write",
        "candidate_write",
        "evidence_write",
        "staged_write",
        "authoritative_write",
    }
)
_TOOL_RISKS = frozenset({"low", "medium", "high"})
# OpenAI function names and MCP tool names share this deliberately conservative
# protocol subset.  OpenAI currently caps function names at 64 characters.
_PROTOCOL_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _freeze_json(value: Any) -> Any:
    """Return an immutable JSON-shaped value for static protocol contracts."""

    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _schema_json(value: Mapping[str, Any]) -> str:
    """Canonicalize a schema and reject values that cannot cross JSON protocols."""

    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("PCB tool schemas must contain only JSON values") from exc
    if not isinstance(decoded, dict):
        raise TypeError("PCB tool schemas must be JSON objects")
    return encoded


def _arguments_json(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Return a detached JSON object and its canonical transport encoding."""

    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValidationError("PCB tool arguments must be JSON values") from exc
    if not isinstance(decoded, dict):
        raise ValidationError("PCB tool arguments must be a JSON object")
    return cast(dict[str, Any], decoded), encoded


def _assert_closed_object_schemas(value: Any, *, path: str = "input") -> None:
    """Require strict-mode object schemas at every nesting level."""

    if isinstance(value, Mapping):
        schema_type = value.get("type")
        if schema_type == "object":
            properties = value.get("properties")
            required = value.get("required")
            if not isinstance(properties, Mapping):
                raise ValueError(f"{path} object schema must declare properties")
            if value.get("additionalProperties") is not False:
                raise ValueError(
                    f"{path} object schema must set additionalProperties=false"
                )
            if (
                not isinstance(required, (list, tuple))
                or not all(isinstance(name, str) for name in required)
                or set(required) != set(properties)
                or len(required) != len(properties)
            ):
                raise ValueError(
                    f"{path} object schema must require every declared property"
                )
        for key, item in value.items():
            _assert_closed_object_schemas(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_closed_object_schemas(item, path=f"{path}[{index}]")


@dataclass(frozen=True)
class MCPToolAnnotations:
    """Explicit MCP safety hints; never infer authority from a coarse effect."""

    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("read_only", self.read_only),
            ("destructive", self.destructive),
            ("idempotent", self.idempotent),
            ("open_world", self.open_world),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"MCP tool annotation {name} must be boolean")

    def to_dict(self) -> dict[str, bool]:
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }


@dataclass(frozen=True)
class ToolArgumentSpec:
    """One required argument in a closed, strictly typed tool schema."""

    name: str
    value_type: type
    description: str
    schema: Mapping[str, Any] = field(default_factory=dict)
    _schema_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("PCB tool argument names must be non-empty text")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("PCB tool arguments must have a description")
        if self.value_type is str:
            inferred_type = "string"
        elif self.value_type is dict:
            inferred_type = "object"
        else:
            raise ValueError(f"unsupported PCB tool argument type: {self.value_type!r}")
        raw_schema = (
            dict(self.schema)
            if self.schema
            else {"type": inferred_type, "description": self.description}
        )
        raw_schema.setdefault("description", self.description)
        if raw_schema.get("type") != inferred_type:
            raise ValueError(
                f"PCB tool argument {self.name} schema does not match its Python type"
            )
        encoded = _schema_json(raw_schema)
        decoded = cast(dict[str, Any], json.loads(encoded))
        _assert_closed_object_schemas(decoded, path=f"argument.{self.name}")
        object.__setattr__(self, "schema", _freeze_json(decoded))
        object.__setattr__(self, "_schema_json", encoded)

    def schema_copy(self) -> dict[str, Any]:
        """Return a fresh transport-safe schema that callers may freely mutate."""

        return cast(dict[str, Any], json.loads(self._schema_json))


@dataclass(frozen=True)
class ToolSpec:
    """Static authority and input contract for one local PCB operation."""

    name: str
    external_name: str
    description: str
    result_description: str
    error_description: str
    effect: ToolEffect
    risk: ToolRisk
    annotations: MCPToolAnnotations
    allowed_statuses: frozenset[str]
    arguments: tuple[ToolArgumentSpec, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _PROTOCOL_TOOL_NAME.fullmatch(self.name) is None
        ):
            raise ValueError(
                "PCB tool names must contain 1-64 protocol-safe characters"
            )
        if (
            not isinstance(self.external_name, str)
            or not self.external_name.startswith("pcb_")
            or _PROTOCOL_TOOL_NAME.fullmatch(self.external_name) is None
        ):
            raise ValueError(
                "external PCB tool names must use the stable pcb_ protocol namespace"
            )
        if not isinstance(self.effect, str) or self.effect not in _TOOL_EFFECTS:
            raise ValueError(f"PCB tool {self.name} has an invalid effect")
        if not isinstance(self.risk, str) or self.risk not in _TOOL_RISKS:
            raise ValueError(f"PCB tool {self.name} has an invalid risk")
        if not isinstance(self.annotations, MCPToolAnnotations):
            raise TypeError(f"PCB tool {self.name} must declare MCP annotations")
        if (
            self.effect == "authoritative_write" or self.risk == "high"
        ) and not self.annotations.destructive:
            raise ValueError(f"PCB tool {self.name} cannot hide destructive authority")
        if self.annotations.read_only and self.effect in _TOOL_EFFECTS:
            raise ValueError(
                f"PCB tool {self.name} writes state and cannot be marked read-only"
            )
        if (
            not isinstance(self.allowed_statuses, frozenset)
            or not self.allowed_statuses
            or not all(
                isinstance(status, str) and bool(status)
                for status in self.allowed_statuses
            )
        ):
            raise ValueError(f"PCB tool {self.name} must declare allowed statuses")
        for label, value in (
            ("description", self.description),
            ("result description", self.result_description),
            ("error description", self.error_description),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"PCB tool {self.name} must have a {label}")
        names = [argument.name for argument in self.arguments]
        if len(names) != len(set(names)):
            raise ValueError(f"PCB tool {self.name} has duplicate argument names")
        _assert_closed_object_schemas(self.input_schema)

    @property
    def input_schema(self) -> dict[str, Any]:
        """Strict JSON Schema shared by model-native and MCP transports."""

        properties = {
            argument.name: argument.schema_copy() for argument in self.arguments
        }
        return {
            "type": "object",
            "properties": properties,
            "required": [argument.name for argument in self.arguments],
            "additionalProperties": False,
        }

    @property
    def protocol_description(self) -> str:
        """Describe the operation, successful receipt, and bounded failures."""

        return (
            f"{self.description.rstrip('. ')}. "
            f"Returns: {self.result_description.rstrip('. ')}. "
            f"Errors: {self.error_description.rstrip('. ')}."
        )

    @property
    def mcp_annotations(self) -> dict[str, bool]:
        """Return this operation's explicit transport-facing safety hints."""

        return self.annotations.to_dict()

    def to_openai_responses_tool(self) -> dict[str, Any]:
        """Export an OpenAI Responses API function-tool declaration."""

        return {
            "type": "function",
            "name": self.external_name,
            "description": self.protocol_description,
            "parameters": self.input_schema,
            "strict": True,
        }

    def to_mcp_tool(self) -> dict[str, Any]:
        """Export an SDK-independent MCP Tool descriptor."""

        return {
            "name": self.external_name,
            "description": self.protocol_description,
            "inputSchema": self.input_schema,
            "annotations": self.mcp_annotations,
        }


def _normalize_arguments_for_binding(
    name: str, values: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalize known valid calls before their approval hash is created.

    Unknown calls remain constructible so the closed executor can reject their
    name. Known calls must satisfy and normalize through their strict contract
    before an approval/audit hash can exist. Module initialization constructs
    the registry after :class:`ToolCall` is defined, hence the guarded lookup.
    """

    copied, _encoded = _arguments_json(values)
    registry = globals().get("DEFAULT_PCB_TOOL_REGISTRY")
    if not isinstance(registry, PCBToolRegistry):
        return copied
    try:
        registry.resolve(name)
    except ValidationError:
        return copied
    return registry.normalize_arguments(name, copied)


@dataclass(frozen=True)
class ToolCall:
    """A proposed PCB operation bound to one observed project revision."""

    name: str
    project_id: str
    source: ToolSource
    arguments: Mapping[str, Any]
    baseline_revision: int
    arguments_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValidationError("PCB tool name must be non-empty text")
        if not isinstance(self.project_id, str) or not self.project_id:
            raise ValidationError("PCB tool project id must be non-empty text")
        if not isinstance(self.source, str) or self.source not in _TOOL_SOURCES:
            raise ValidationError(
                "PCB tool source must be runtime_policy, model, mcp, or user"
            )
        if not isinstance(self.arguments, Mapping):
            raise ValidationError("PCB tool arguments must be a mapping")
        if not all(isinstance(key, str) for key in self.arguments):
            raise ValidationError("PCB tool argument names must be text")
        if (
            isinstance(self.baseline_revision, bool)
            or not isinstance(self.baseline_revision, int)
            or self.baseline_revision < 0
        ):
            raise ValidationError("PCB tool baseline revision must be non-negative")
        copied = _normalize_arguments_for_binding(self.name, self.arguments)
        copied, canonical = _arguments_json(copied)
        object.__setattr__(self, "arguments", MappingProxyType(copied))
        object.__setattr__(
            self,
            "arguments_hash",
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True)
class ToolResult:
    """Successful execution receipt plus the resulting public project view."""

    call: ToolCall
    spec: ToolSpec
    before_status: str
    before_revision: int
    after_status: str
    after_revision: int
    view: dict[str, Any]


_PLAN_REQUEST_STATUSES = frozenset(
    {
        "draft",
        "needs_clarification",
        "planning_required",
        "awaiting_confirmation",
        "generation_unavailable",
        "provider_error",
        "generation_failed",
        "repair_failed",
        "validation_failed",
        "interrupted",
        "generated",
        "validated",
        "released",
        "release_failed",
    }
)

_REPAIR_FEEDBACK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Normalized deterministic repair feedback",
    "properties": {
        "schema": {
            "type": "string",
            "const": REPAIR_FEEDBACK_SCHEMA,
            "description": "Repair feedback schema identifier",
        },
        "version": {
            "type": "integer",
            "const": REPAIR_FEEDBACK_VERSION,
            "description": "Repair feedback schema version",
        },
        "phase": {
            "type": "string",
            "enum": ["generation", "validation", "user_request"],
            "description": "Source phase for the bounded evidence",
        },
        "attempt": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_AUTOMATIC_REPAIRS,
            "description": "One-based bounded repair attempt",
        },
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2048,
            "pattern": r"\S",
            "description": "Short explanation of the requested correction",
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2048,
                "pattern": r"\S",
            },
            "maxItems": MAX_REPAIR_FINDINGS,
            "description": "Bounded deterministic findings used for repair",
        },
    },
    "required": ["schema", "version", "phase", "attempt", "summary", "findings"],
    "additionalProperties": False,
}

PCB_TOOL_SPECS = (
    ToolSpec(
        name="plan_request",
        external_name="pcb_plan_request",
        description="Interpret a request and produce or revise a bounded circuit plan",
        result_description=(
            "updated public project view with the bounded plan and next status"
        ),
        error_description=(
            "invalid request, provider failure, stale revision, or disallowed state"
        ),
        effect="conversation_write",
        risk="low",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=False,
            idempotent=False,
            open_world=True,
        ),
        allowed_statuses=_PLAN_REQUEST_STATUSES,
        arguments=(
            ToolArgumentSpec(
                "message",
                str,
                "Natural-language PCB request",
                {"type": "string", "minLength": 1, "pattern": r"\S"},
            ),
        ),
    ),
    ToolSpec(
        name="generate_candidate",
        external_name="pcb_generate_candidate",
        description="Materialize and publish the initial semantic plan as native KiCad files",
        result_description="public project view after native KiCad generation is published",
        error_description=(
            "generation or routing failure, stale revision, or disallowed state"
        ),
        effect="authoritative_write",
        risk="high",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
        allowed_statuses=frozenset(
            {"awaiting_confirmation", "generation_failed", "interrupted"}
        ),
    ),
    ToolSpec(
        name="validate",
        external_name="pcb_validate",
        description="Run configured checks and retain validation evidence",
        result_description="public project view containing retained L1-L3 evidence",
        error_description=(
            "validation execution failure, stale revision, or disallowed state"
        ),
        effect="evidence_write",
        risk="low",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=False,
            idempotent=False,
            open_world=False,
        ),
        allowed_statuses=frozenset(
            {
                "generated",
                "validated",
                "validation_failed",
                "released",
                "interrupted",
                "release_failed",
            }
        ),
    ),
    ToolSpec(
        name="repair_candidate",
        external_name="pcb_repair_candidate",
        description="Revise a plan from bounded evidence and stage a checked candidate",
        result_description="public project view with the checked staged replacement",
        error_description=(
            "invalid feedback, repair failure, stale revision, or disallowed state"
        ),
        effect="staged_write",
        risk="medium",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=True,
            idempotent=False,
            open_world=True,
        ),
        allowed_statuses=frozenset(
            {
                "generation_failed",
                "generated",
                "validated",
                "validation_failed",
                "repair_failed",
                "released",
                "release_failed",
                "interrupted",
            }
        ),
        arguments=(
            ToolArgumentSpec(
                "feedback",
                dict,
                "Normalized deterministic repair feedback",
                _REPAIR_FEEDBACK_INPUT_SCHEMA,
            ),
        ),
    ),
    ToolSpec(
        name="apply_candidate",
        external_name="pcb_apply_candidate",
        description="Atomically publish the currently staged PCB candidate",
        result_description="public project view after atomic candidate publication",
        error_description=(
            "missing or stale candidate, revision conflict, apply failure, or disallowed state"
        ),
        effect="authoritative_write",
        risk="high",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
        allowed_statuses=frozenset({"change_ready"}),
    ),
    ToolSpec(
        name="discard_candidate",
        external_name="pcb_discard_candidate",
        description="Discard the staged PCB candidate without changing the authoritative design",
        result_description="public project view after the staged candidate is discarded",
        error_description="missing candidate, revision conflict, or disallowed state",
        effect="staged_write",
        risk="medium",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
        allowed_statuses=frozenset({"change_ready"}),
    ),
    ToolSpec(
        name="undo_last_change",
        external_name="pcb_undo_last_change",
        description="Restore the exact authoritative design saved before the last applied change",
        result_description="public project view after the previous design is restored",
        error_description=(
            "missing undo receipt, changed authoritative design, revision conflict, or disallowed state"
        ),
        effect="authoritative_write",
        risk="high",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
        allowed_statuses=frozenset(
            {
                "generated",
                "validated",
                "validation_failed",
                "released",
                "release_failed",
            }
        ),
    ),
    ToolSpec(
        name="render_previews",
        external_name="pcb_render_previews",
        description="Render browser-safe schematic, board, PDF, and 3D previews",
        result_description="public project view containing preview artifact references",
        error_description="rendering failure, stale revision, or disallowed state",
        effect="evidence_write",
        risk="low",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=False,
            idempotent=False,
            open_world=False,
        ),
        allowed_statuses=frozenset(
            {
                "generated",
                "validated",
                "validation_failed",
                "released",
                "release_failed",
                "interrupted",
            }
        ),
    ),
    ToolSpec(
        name="build_release",
        external_name="pcb_build_release",
        description="Build and verify a local manufacturing-candidate evidence bundle",
        result_description="public project view containing the retained release receipt",
        error_description=(
            "missing passing validation, bundle verification failure, stale revision, or disallowed state"
        ),
        effect="evidence_write",
        risk="medium",
        annotations=MCPToolAnnotations(
            read_only=False,
            destructive=False,
            idempotent=False,
            open_world=False,
        ),
        allowed_statuses=frozenset(
            {"validated", "released", "release_failed", "interrupted"}
        ),
    ),
)


ToolHandler = Callable[
    [PCBToolServicePort, str, dict[str, Any], float, int], dict[str, Any]
]


def _invoke_service_method(
    service: PCBToolServicePort,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Invoke the authoritative service contract with narrow-adapter fallback.

    Production services accept every supplied concurrency parameter. A few
    in-process tests and third-party read-through adapters predate
    ``expected_revision``; filtering only explicitly unsupported keyword
    arguments preserves compatibility without widening the tool contract.
    """

    method = getattr(service, method_name)
    try:
        parameters = tuple(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        parameters = ()
    if parameters and not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    ):
        accepted = {parameter.name for parameter in parameters}
        kwargs = {key: value for key, value in kwargs.items() if key in accepted}
    return cast(dict[str, Any], method(*args, **kwargs))


def _plan_request(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    return _invoke_service_method(
        service,
        "send_message",
        project_id,
        arguments["message"],
        timeout=timeout,
        expected_revision=expected_revision,
    )


def _generate_candidate(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    del arguments
    return _invoke_service_method(
        service,
        "confirm_project",
        project_id,
        validate=False,
        timeout=timeout,
        expected_revision=expected_revision,
    )


def _validate(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    del arguments
    return _invoke_service_method(
        service,
        "validate_project",
        project_id,
        timeout=timeout,
        expected_revision=expected_revision,
    )


def _repair_candidate(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    return _invoke_service_method(
        service,
        "prepare_agent_repair",
        project_id,
        arguments["feedback"],
        timeout=timeout,
        expected_revision=expected_revision,
    )


def _apply_candidate(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    del arguments
    return _invoke_service_method(
        service,
        "apply_modification",
        project_id,
        timeout=timeout,
        expected_revision=expected_revision,
    )


def _discard_candidate(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    del arguments, timeout
    return _invoke_service_method(
        service,
        "discard_modification",
        project_id,
        expected_revision=expected_revision,
    )


def _undo_last_change(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    del arguments, timeout
    return _invoke_service_method(
        service,
        "undo_last_modification",
        project_id,
        expected_revision=expected_revision,
    )


def _render_previews(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    del arguments
    return _invoke_service_method(
        service,
        "generate_project_previews",
        project_id,
        timeout=timeout,
        expected_revision=expected_revision,
    )


def _build_release(
    service: PCBToolServicePort,
    project_id: str,
    arguments: dict[str, Any],
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]:
    del arguments
    return _invoke_service_method(
        service,
        "build_release",
        project_id,
        timeout=timeout,
        expected_revision=expected_revision,
    )


_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "plan_request": _plan_request,
    "generate_candidate": _generate_candidate,
    "validate": _validate,
    "repair_candidate": _repair_candidate,
    "apply_candidate": _apply_candidate,
    "discard_candidate": _discard_candidate,
    "undo_last_change": _undo_last_change,
    "render_previews": _render_previews,
    "build_release": _build_release,
}


class PCBToolRegistry:
    """Closed catalog of operations a call producer is allowed to request."""

    def __init__(self, specs: tuple[ToolSpec, ...] = PCB_TOOL_SPECS) -> None:
        by_name = {spec.name: spec for spec in specs}
        by_external_name = {spec.external_name: spec for spec in specs}
        if len(by_name) != len(specs):
            raise ValueError("PCB tool specs contain duplicate names")
        if len(by_external_name) != len(specs):
            raise ValueError("PCB tool specs contain duplicate external names")
        if set(by_name) & set(by_external_name):
            raise ValueError("PCB tool internal and external names are ambiguous")
        if set(by_name) != set(_TOOL_HANDLERS):
            raise ValueError("PCB tool specs and handlers differ")
        canonical = {spec.name: spec for spec in PCB_TOOL_SPECS}
        for name, spec in by_name.items():
            if self._authority_contract(spec) != self._authority_contract(
                canonical[name]
            ):
                raise ValueError(
                    f"PCB tool {name} cannot change its fixed handler authority contract"
                )
        self._specs = MappingProxyType(by_name)
        self._external_specs = MappingProxyType(by_external_name)

    @staticmethod
    def _authority_contract(spec: ToolSpec) -> tuple[Any, ...]:
        """Metadata that must remain fixed for every built-in local handler."""

        return (
            spec.external_name,
            spec.effect,
            spec.risk,
            spec.annotations,
            spec.allowed_statuses,
            tuple(
                (argument.name, argument.value_type, argument._schema_json)
                for argument in spec.arguments
            ),
        )

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def resolve(self, name: str) -> ToolSpec:
        # Internal deterministic callers keep their established short names,
        # while provider/MCP adapters submit the stable protocol names.
        spec = self._specs.get(name) or self._external_specs.get(name)
        if spec is None:
            raise ValidationError(f"unknown PCB tool: {name}")
        return spec

    def internal_name(self, name: str) -> str:
        """Map either accepted name to the executor's fixed local handler key."""

        return self.resolve(name).name

    def normalize_arguments(
        self, name: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return the exact JSON-shaped arguments a handler will receive."""

        spec = self.resolve(name)
        normalized, _encoded = _arguments_json(values)
        expected = {argument.name for argument in spec.arguments}
        actual = set(normalized)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            details: list[str] = []
            if missing:
                details.append(f"missing: {', '.join(missing)}")
            if extra:
                details.append(f"unexpected: {', '.join(extra)}")
            raise ValidationError(
                f"PCB tool {spec.name} arguments do not match its strict schema"
                + (f" ({'; '.join(details)})" if details else "")
            )
        for argument in spec.arguments:
            value = normalized[argument.name]
            if not isinstance(value, argument.value_type):
                raise ValidationError(
                    f"PCB tool {spec.name} argument {argument.name} has an invalid type"
                )
        if spec.name == "plan_request":
            message = sanitize_user_text(
                normalized["message"].replace("\x00", "").strip()
            )
            if not message:
                raise ValidationError("PCB tool plan_request message must be non-empty")
            normalized["message"] = message
        if spec.name == "repair_candidate":
            normalized["feedback"] = normalize_repair_feedback(normalized["feedback"])
        return normalized

    def bind_arguments(
        self, name: str, values: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Normalize arguments and bind approval/audit to their executed shape."""

        normalized = self.normalize_arguments(name, values)
        normalized, canonical = _arguments_json(normalized)
        return normalized, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def openai_responses_tools(self) -> list[dict[str, Any]]:
        """Return fresh OpenAI Responses declarations for the closed registry."""

        return [spec.to_openai_responses_tool() for spec in self.specs]

    def mcp_tools(self) -> list[dict[str, Any]]:
        """Return fresh MCP Tool descriptors for the closed registry."""

        return [spec.to_mcp_tool() for spec in self.specs]

    def _handler(self, name: str) -> ToolHandler:
        """Resolve internal dispatch; call producers receive specs, not handlers."""

        spec = self.resolve(name)
        return _TOOL_HANDLERS[spec.name]


DEFAULT_PCB_TOOL_REGISTRY = PCBToolRegistry()


def project_status_and_revision(view: Mapping[str, Any]) -> tuple[str, int]:
    """Read the concurrency preconditions from a public application view."""

    project = view.get("project")
    state = view.get("state")
    status = project.get("status") if isinstance(project, Mapping) else None
    revision = state.get("revision") if isinstance(state, Mapping) else None
    if not isinstance(status, str) or not status:
        raise ValidationError("PCB tool snapshot has no project status")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValidationError("PCB tool snapshot has no valid project revision")
    return status, revision


def call_from_view(
    name: str,
    project_id: str,
    *,
    source: ToolSource,
    arguments: Mapping[str, Any],
    view: Mapping[str, Any],
) -> ToolCall:
    """Bind a producer's proposed operation to its observed project revision."""

    _status, revision = project_status_and_revision(view)
    return ToolCall(
        name=name,
        project_id=project_id,
        source=source,
        arguments=arguments,
        baseline_revision=revision,
    )


class PCBToolExecutor:
    """Validate and dispatch PCB calls without delegating local authority."""

    def __init__(
        self,
        service: PCBToolServicePort,
        *,
        registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY,
    ) -> None:
        self.service = service
        self.registry = registry

    def snapshot(self, project_id: str) -> dict[str, Any]:
        """Load the authoritative public view used for a call baseline."""

        return self.service.open_project(project_id)

    def execute(
        self,
        call: ToolCall,
        *,
        timeout: float,
        observed_view: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        """Execute one checked call and return a typed result receipt.

        ``observed_view`` exists for tightly scoped in-process adapters that
        already hold the latest returned service view.  Production execution
        reloads the project when ``open_project`` is available, ensuring that a
        call cannot silently cross a revision or status boundary.
        """

        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            or timeout > 1_800
        ):
            raise ValidationError("PCB tool timeout must be in (0, 1800] seconds")
        spec = self.registry.resolve(call.name)
        arguments, arguments_hash = self.registry.bind_arguments(
            call.name, call.arguments
        )
        if arguments_hash != call.arguments_hash:
            raise ValidationError(
                "PCB tool arguments changed after the revision-bound call was created"
            )
        opener = getattr(self.service, "open_project", None)
        current_view = (
            opener(call.project_id)
            if callable(opener)
            else dict(observed_view)
            if observed_view is not None
            else None
        )
        if current_view is None:
            raise ValidationError("PCB tool executor cannot load a project snapshot")
        self._assert_project_identity(call, current_view)
        status, revision = project_status_and_revision(current_view)
        if revision != call.baseline_revision:
            raise ValidationError(
                "PCB tool call has a stale baseline revision "
                f"({call.baseline_revision}; current {revision})"
            )
        if status not in spec.allowed_statuses:
            raise ValidationError(
                f"PCB tool {call.name} is not allowed while project status is {status}"
            )
        view = self.registry._handler(call.name)(
            self.service,
            call.project_id,
            arguments,
            timeout,
            call.baseline_revision,
        )
        if not isinstance(view, dict):
            raise ValidationError(f"PCB tool {call.name} returned an invalid view")
        self._assert_project_identity(call, view)
        after_status, after_revision = project_status_and_revision(view)
        return ToolResult(
            call=call,
            spec=spec,
            before_status=status,
            before_revision=revision,
            after_status=after_status,
            after_revision=after_revision,
            view=view,
        )

    @staticmethod
    def _assert_project_identity(call: ToolCall, view: Mapping[str, Any]) -> None:
        project = view.get("project")
        project_id = project.get("id") if isinstance(project, Mapping) else None
        if project_id != call.project_id:
            raise ValidationError("PCB tool snapshot belongs to a different project")
