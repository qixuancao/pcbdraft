"""Canonical PCB capability catalog behind the domain router tools.

This module is the single source of truth for what PCBDraft can actually do
through the ``pcb_*`` domain routers (``pcb_project``, ``pcb_library``,
``pcb_design``, ``pcb_board``, ``pcb_inspect``, ``pcb_verify``,
``pcb_export``, ``pcb_analysis``).  The same catalog is exported to the
Hermes tool registry, the MCP adapter, and future native model tools, so no
transport keeps a second hand-maintained schema list.

The catalog is a capability organization, not a workflow.  The agent may call
any router in any order; each result reports facts only and never prescribes
the next tool.

Authority model:

- read operations return observed application state and installed-library
  facts;
- write operations that exist today dispatch through the closed
  :class:`~pcbdraft.agent.tooling.PCBToolExecutor` exactly like the
  high-level macro tools, so strict schema validation, status preconditions,
  baseline revision checks, transactions, and the ApplicationService write
  authority all still apply;
- capabilities that are not implemented are reported honestly with
  ``supported: false`` and a concrete reason.  Nothing fakes success, silently
  substitutes a different operation, or treats unsupported as a pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pcbdraft.agent.tooling import (
    DEFAULT_PCB_TOOL_REGISTRY,
    PCBToolExecutor,
    PCBToolRegistry,
    ToolEffect,
    ToolRisk,
    call_from_view,
)
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import load_json_limited

__all__ = (
    "CAPABILITY_DOMAINS",
    "DEFAULT_CAPABILITY_REGISTRY",
    "UNSUPPORTED_ANALYSIS_MESSAGE",
    "CapabilitySpec",
    "PCBCapabilityRegistry",
    "execute_capability",
    "router_tool_schema",
)

#: Domain routers exposed to the agent.  Organizational, not sequential.
CAPABILITY_DOMAINS: tuple[str, ...] = (
    "pcb_project",
    "pcb_library",
    "pcb_design",
    "pcb_board",
    "pcb_inspect",
    "pcb_verify",
    "pcb_export",
    "pcb_analysis",
)

_PLAN_FILE_LIMIT = 4 * 1024 * 1024
_GRAPH_COMPONENT_LIMIT = 400
_GRAPH_NET_LIMIT = 600
_EVENT_LIMIT = 100

UNSUPPORTED_ANALYSIS_MESSAGE = (
    "engineering analysis is a declared future capability; no analysis "
    "engine is wired in this framework phase"
)
_UNSUPPORTED_GRAPH_WRITE = (
    "fine-grained semantic graph mutation is a declared extension point; "
    "semantic changes currently flow through the pcb_plan_request and "
    "pcb_repair_candidate macros"
)
_UNSUPPORTED_BOARD_WRITE = (
    "native board geometry operations are not yet exposed as per-operation "
    "capabilities; board work currently happens through the "
    "pcb_generate_candidate macro and its deterministic KiCad backend"
)


@dataclass(frozen=True)
class CapabilitySpec:
    """One addressable capability in one domain router."""

    domain: str
    operation: str
    description: str
    read_or_write: Literal["read", "write"]
    supported: bool = True
    reason: str = ""
    macro: str | None = None
    risk: ToolRisk = "low"
    effect: ToolEffect | None = None
    limitations: tuple[str, ...] = ()
    argument_properties: Mapping[str, Any] = field(default_factory=dict)
    required_arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.domain not in CAPABILITY_DOMAINS:
            raise ValueError(f"unknown capability domain: {self.domain}")
        if not self.operation or not self.operation.replace("_", "").isalnum():
            raise ValueError("capability operations must be snake_case identifiers")
        if self.read_or_write not in {"read", "write"}:
            raise ValueError("capability read_or_write must be read or write")
        if not self.supported and not self.reason.strip():
            raise ValueError(
                f"unsupported capability {self.domain}.{self.operation} must "
                "state why it is unavailable"
            )
        if self.macro is not None and self.read_or_write != "write":
            raise ValueError("macro-backed capabilities are writes")
        if self.macro is None and self.effect not in (None, "conversation_write"):
            raise ValueError("non-macro capabilities cannot claim write effects")
        unknown = set(self.required_arguments) - set(self.argument_properties)
        if unknown:
            raise ValueError("required capability arguments must be declared")

    @property
    def name(self) -> str:
        return f"{self.domain}.{self.operation}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "domain": self.domain,
            "operation": self.operation,
            "capability": self.name,
            "description": self.description,
            "read_or_write": self.read_or_write,
            "supported": self.supported,
            "risk": self.risk,
        }
        if self.macro is not None:
            payload["dispatches_macro"] = self.macro
        if not self.supported:
            payload["reason"] = self.reason
        if self.limitations:
            payload["limitations"] = list(self.limitations)
        if self.argument_properties:
            payload["arguments"] = {
                "type": "object",
                "properties": dict(self.argument_properties),
                "required": sorted(self.required_arguments),
                "additionalProperties": False,
            }
        return payload


def _unsupported(
    domain: str,
    operation: str,
    description: str,
    reason: str,
    *,
    limitations: tuple[str, ...] = (),
) -> CapabilitySpec:
    return CapabilitySpec(
        domain=domain,
        operation=operation,
        description=description,
        read_or_write="write",
        supported=False,
        reason=reason,
        limitations=limitations,
    )


_CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    # ---- pcb_project --------------------------------------------------
    CapabilitySpec(
        domain="pcb_project",
        operation="capabilities",
        description=(
            "List the capabilities this domain router actually supports, "
            "including honest unsupported entries"
        ),
        read_or_write="read",
    ),
    CapabilitySpec(
        domain="pcb_project",
        operation="list",
        description="List the projects in the local PCB repository",
        read_or_write="read",
    ),
    CapabilitySpec(
        domain="pcb_project",
        operation="open",
        description=(
            "Open one project and report its factual status, revision, "
            "design files, and retained evidence readiness"
        ),
        read_or_write="read",
    ),
    CapabilitySpec(
        domain="pcb_project",
        operation="create",
        description=(
            "Create a new draft project in the repository; no engineering "
            "files exist until planning and generation run"
        ),
        read_or_write="write",
        effect="conversation_write",
        argument_properties={
            "name": {
                "type": "string",
                "description": "Short human-readable project name",
                "minLength": 1,
                "maxLength": 512,
            },
        },
        required_arguments=("name",),
    ),
    # ---- pcb_library --------------------------------------------------
    CapabilitySpec(
        domain="pcb_library",
        operation="capabilities",
        description=(
            "List the capabilities this domain router actually supports, "
            "including honest unsupported entries"
        ),
        read_or_write="read",
    ),
    CapabilitySpec(
        domain="pcb_library",
        operation="search_symbols",
        description=(
            "Search the locally installed KiCad symbol libraries and return "
            "real candidates with pins and footprints"
        ),
        read_or_write="read",
        argument_properties={
            "query": {
                "type": "string",
                "description": "Symbol name or library fragment to search for",
                "minLength": 1,
                "maxLength": 256,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of candidates (1-64)",
                "minimum": 1,
                "maximum": 64,
            },
        },
        required_arguments=("query",),
        limitations=("installed libraries only; no manufacturer catalog access",),
    ),
    CapabilitySpec(
        domain="pcb_library",
        operation="describe_symbol",
        description=(
            "Describe one installed KiCad symbol: pins, electrical types, "
            "default footprint, and reference-only datasheet link"
        ),
        read_or_write="read",
        argument_properties={
            "symbol": {
                "type": "string",
                "description": "KiCad symbol id (Library:Name)",
                "minLength": 1,
                "maxLength": 256,
            },
        },
        required_arguments=("symbol",),
        limitations=("footprint availability is reported, not electrically qualified",),
    ),
    _unsupported(
        "pcb_library",
        "search_footprints",
        "Search installed KiCad footprint libraries",
        "footprint library search is a declared extension point; symbol "
        "records already carry their default footprint references",
    ),
    # ---- pcb_design ---------------------------------------------------
    CapabilitySpec(
        domain="pcb_design",
        operation="capabilities",
        description=(
            "List the capabilities this domain router actually supports, "
            "including honest unsupported entries"
        ),
        read_or_write="read",
    ),
    CapabilitySpec(
        domain="pcb_design",
        operation="inspect_graph",
        description=(
            "Inspect the current semantic design graph: components, "
            "functional blocks, nets, power domains, interfaces, and the "
            "published design IR summary"
        ),
        read_or_write="read",
        limitations=(
            (
                "returns the retained semantic plan and IR summary; it is a "
                "working representation, not proof of electrical correctness"
            ),
        ),
    ),
    CapabilitySpec(
        domain="pcb_design",
        operation="inspect_component",
        description="Inspect one component in the semantic design graph",
        read_or_write="read",
        argument_properties={
            "component": {
                "type": "string",
                "description": "Component id from the semantic graph",
                "minLength": 1,
                "maxLength": 256,
            },
        },
        required_arguments=("component",),
    ),
    CapabilitySpec(
        domain="pcb_design",
        operation="inspect_net",
        description="Inspect one net and its endpoints in the semantic design graph",
        read_or_write="read",
        argument_properties={
            "net": {
                "type": "string",
                "description": "Net id from the semantic graph",
                "minLength": 1,
                "maxLength": 256,
            },
        },
        required_arguments=("net",),
    ),
    _unsupported(
        "pcb_design",
        "add_component",
        "Add one component to the semantic design graph",
        _UNSUPPORTED_GRAPH_WRITE,
    ),
    _unsupported(
        "pcb_design",
        "remove_component",
        "Remove one component from the semantic design graph",
        _UNSUPPORTED_GRAPH_WRITE,
    ),
    _unsupported(
        "pcb_design",
        "connect",
        "Connect two endpoints with a new or existing net",
        _UNSUPPORTED_GRAPH_WRITE,
    ),
    _unsupported(
        "pcb_design",
        "disconnect",
        "Remove endpoints from a net",
        _UNSUPPORTED_GRAPH_WRITE,
    ),
    _unsupported(
        "pcb_design",
        "update_component",
        "Update one component in the semantic design graph",
        _UNSUPPORTED_GRAPH_WRITE,
    ),
    _unsupported(
        "pcb_design",
        "apply_semantic_patch",
        "Apply a bounded semantic patch to the design graph",
        _UNSUPPORTED_GRAPH_WRITE,
    ),
    # ---- pcb_board ----------------------------------------------------
    CapabilitySpec(
        domain="pcb_board",
        operation="capabilities",
        description=(
            "List the capabilities this domain router actually supports, "
            "including honest unsupported entries"
        ),
        read_or_write="read",
    ),
    _unsupported(
        "pcb_board",
        "board_summary",
        "Summarize the native board: outline, stackup, placements, routing",
        _UNSUPPORTED_BOARD_WRITE,
    ),
    _unsupported(
        "pcb_board",
        "move_footprint",
        "Move one footprint on the board",
        _UNSUPPORTED_BOARD_WRITE,
    ),
    _unsupported(
        "pcb_board",
        "set_board_outline",
        "Set or change the board outline",
        _UNSUPPORTED_BOARD_WRITE,
    ),
    _unsupported(
        "pcb_board",
        "route_net",
        "Route (or reroute) one net",
        _UNSUPPORTED_BOARD_WRITE,
    ),
    # ---- pcb_inspect --------------------------------------------------
    CapabilitySpec(
        domain="pcb_inspect",
        operation="capabilities",
        description=(
            "List the capabilities this domain router actually supports, "
            "including honest unsupported entries"
        ),
        read_or_write="read",
    ),
    CapabilitySpec(
        domain="pcb_inspect",
        operation="events",
        description=(
            "Read the durable project event log: engineering activity, "
            "findings, and tool receipts"
        ),
        read_or_write="read",
        argument_properties={
            "after": {
                "type": "integer",
                "description": "Return only events with a sequence above this cursor",
                "minimum": 0,
            },
        },
    ),
    CapabilitySpec(
        domain="pcb_inspect",
        operation="evidence",
        description=(
            "Read the retained validation and release evidence readiness for "
            "the current project"
        ),
        read_or_write="read",
        limitations=(
            "readiness records summarize evidence; they do not re-run checks",
        ),
    ),
    _unsupported(
        "pcb_inspect",
        "netlist",
        "Inspect the native schematic netlist",
        "native netlist extraction is a declared extension point; use "
        "pcb_design inspect operations for the semantic graph today",
    ),
    _unsupported(
        "pcb_inspect",
        "geometry",
        "Inspect native board geometry (tracks, zones, vias)",
        "native geometry inspection is a declared extension point; "
        "pcb_render_previews renders the real board instead",
    ),
    # ---- pcb_verify ---------------------------------------------------
    CapabilitySpec(
        domain="pcb_verify",
        operation="capabilities",
        description=(
            "List the capabilities this domain router actually supports, "
            "including honest unsupported entries"
        ),
        read_or_write="read",
    ),
    CapabilitySpec(
        domain="pcb_verify",
        operation="validate",
        description=(
            "Run the real layered validation (connectivity, ERC, DRC) and "
            "retain its evidence"
        ),
        read_or_write="write",
        macro="validate",
        risk="low",
        effect="evidence_write",
    ),
    _unsupported(
        "pcb_verify",
        "erc",
        "Run only the electrical rules check",
        "per-check selection is not implemented; the validate operation "
        "runs the full layered check, which includes ERC",
    ),
    _unsupported(
        "pcb_verify",
        "drc",
        "Run only the design rules check",
        "per-check selection is not implemented; the validate operation "
        "runs the full layered check, which includes DRC",
    ),
    # ---- pcb_export ---------------------------------------------------
    CapabilitySpec(
        domain="pcb_export",
        operation="capabilities",
        description=(
            "List the capabilities this domain router actually supports, "
            "including honest unsupported entries"
        ),
        read_or_write="read",
    ),
    CapabilitySpec(
        domain="pcb_export",
        operation="render_previews",
        description=(
            "Render browser-safe schematic, board, PDF, and 3D previews from "
            "the real project"
        ),
        read_or_write="write",
        macro="render_previews",
        risk="low",
        effect="evidence_write",
    ),
    CapabilitySpec(
        domain="pcb_export",
        operation="build_release",
        description=(
            "Build and verify a local manufacturing-candidate evidence bundle"
        ),
        read_or_write="write",
        macro="build_release",
        risk="medium",
        effect="evidence_write",
    ),
    _unsupported(
        "pcb_export",
        "gerbers",
        "Export Gerber files standalone",
        "standalone per-format export is not implemented; build_release "
        "produces and verifies the manufacturing bundle",
    ),
    _unsupported(
        "pcb_export",
        "step",
        "Export a STEP 3D model standalone",
        "standalone per-format export is not implemented; build_release "
        "produces and verifies the manufacturing bundle",
    ),
    # ---- pcb_analysis -------------------------------------------------
    CapabilitySpec(
        domain="pcb_analysis",
        operation="capabilities",
        description=(
            "List the analysis capabilities this domain router actually "
            "supports, including honest unsupported entries"
        ),
        read_or_write="read",
    ),
    _unsupported(
        "pcb_analysis",
        "thermal",
        "Thermal analysis of the board",
        UNSUPPORTED_ANALYSIS_MESSAGE,
    ),
    _unsupported(
        "pcb_analysis",
        "spice",
        "SPICE circuit simulation",
        UNSUPPORTED_ANALYSIS_MESSAGE,
    ),
    _unsupported(
        "pcb_analysis",
        "signal_integrity",
        "Signal integrity analysis",
        UNSUPPORTED_ANALYSIS_MESSAGE,
    ),
    _unsupported(
        "pcb_analysis",
        "power_integrity",
        "Power integrity analysis",
        UNSUPPORTED_ANALYSIS_MESSAGE,
    ),
)


class PCBCapabilityRegistry:
    """Closed catalog of domain-router capabilities and their dispatch facts."""

    def __init__(self, specs: tuple[CapabilitySpec, ...] = _CAPABILITY_SPECS) -> None:
        self._specs: dict[tuple[str, str], CapabilitySpec] = {}
        for spec in specs:
            key = (spec.domain, spec.operation)
            if key in self._specs:
                raise ValueError(f"duplicate capability: {spec.name}")
            self._specs[key] = spec
        missing = {(domain, "capabilities") for domain in CAPABILITY_DOMAINS} - set(
            self._specs
        )
        if missing:
            raise ValueError("every domain router must support operation=capabilities")

    def domains(self) -> tuple[str, ...]:
        return CAPABILITY_DOMAINS

    def resolve(self, domain: str, operation: str) -> CapabilitySpec:
        spec = self._specs.get((domain, operation))
        if spec is None:
            if domain not in CAPABILITY_DOMAINS:
                raise ValidationError(f"unknown PCB capability domain: {domain}")
            raise ValidationError(
                f"unknown operation for {domain}: {operation}; call "
                f'{domain}(operation="capabilities") to list supported operations'
            )
        return spec

    def capabilities_for(self, domain: str) -> tuple[CapabilitySpec, ...]:
        if domain not in CAPABILITY_DOMAINS:
            raise ValidationError(f"unknown PCB capability domain: {domain}")
        return tuple(spec for spec in self._specs.values() if spec.domain == domain)

    def describe(self, domain: str | None = None) -> dict[str, Any]:
        """Return the honest capability listing for one or all domains."""

        if domain is not None:
            return {
                "domain": domain,
                "capabilities": [
                    spec.to_dict() for spec in self.capabilities_for(domain)
                ],
            }
        return {
            "domains": [
                {
                    "domain": name,
                    "operations": sorted(
                        spec.operation for spec in self.capabilities_for(name)
                    ),
                }
                for name in CAPABILITY_DOMAINS
            ],
            "capabilities": [spec.to_dict() for spec in self._specs.values()],
        }

    def unsupported_report(self, spec: CapabilitySpec) -> dict[str, Any]:
        payload = spec.to_dict()
        return {
            "tool": spec.domain,
            "operation": spec.operation,
            "supported": False,
            "capability": spec.name,
            "reason": spec.reason,
            "limitations": list(spec.limitations),
            "description": payload["description"],
        }


DEFAULT_CAPABILITY_REGISTRY = PCBCapabilityRegistry()


def router_tool_schema(
    domain: str,
    *,
    registry: PCBCapabilityRegistry = DEFAULT_CAPABILITY_REGISTRY,
) -> dict[str, Any]:
    """Return the transport-facing JSON Schema for one domain router tool."""

    operations = sorted(spec.operation for spec in registry.capabilities_for(domain))
    return {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": operations,
                "description": (
                    "The capability to invoke in this domain; "
                    'operation="capabilities" lists what is really supported'
                ),
            },
            "arguments": {
                "type": "object",
                "description": (
                    "Operation-specific arguments; call operation=capabilities "
                    "for each operation's argument schema"
                ),
                "additionalProperties": True,
            },
            "project_id": {
                "type": "string",
                "description": (
                    "Project to operate on; omit to reuse the current project"
                ),
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    }


def _validate_arguments(
    spec: CapabilitySpec, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise ValidationError(f"{spec.name} arguments must be a JSON object")
    known = set(spec.argument_properties)
    actual = set(arguments)
    unknown = actual - known
    if unknown:
        raise ValidationError(
            f"{spec.name} does not accept arguments: {', '.join(sorted(unknown))}"
        )
    missing = [name for name in spec.required_arguments if name not in actual]
    if missing:
        raise ValidationError(
            f"{spec.name} is missing required arguments: {', '.join(missing)}"
        )
    normalized: dict[str, Any] = {}
    for name, value in arguments.items():
        declared = spec.argument_properties[name]
        expected = declared.get("type")
        if expected == "string":
            if not isinstance(value, str):
                raise ValidationError(f"{spec.name} argument {name} must be a string")
            minimum = declared.get("minLength", 0)
            maximum = declared.get("maxLength")
            if len(value) < minimum or (maximum is not None and len(value) > maximum):
                raise ValidationError(
                    f"{spec.name} argument {name} length is out of range"
                )
        elif expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"{spec.name} argument {name} must be an integer")
            minimum = declared.get("minimum")
            maximum = declared.get("maximum")
            if (minimum is not None and value < minimum) or (
                maximum is not None and value > maximum
            ):
                raise ValidationError(f"{spec.name} argument {name} is out of range")
        normalized[name] = value
    return normalized


def _view_facts(view: Mapping[str, Any]) -> dict[str, Any]:
    project = view.get("project") if isinstance(view, Mapping) else None
    project = project if isinstance(project, Mapping) else {}
    state = view.get("state") if isinstance(view, Mapping) else None
    state = state if isinstance(state, Mapping) else {}
    design = view.get("design") if isinstance(view, Mapping) else None
    design = design if isinstance(design, Mapping) else {}
    artifacts = view.get("artifacts") if isinstance(view, Mapping) else None
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    validation = artifacts.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    release = artifacts.get("release")
    release = release if isinstance(release, Mapping) else {}
    return {
        "project_id": project.get("id"),
        "project_name": project.get("name"),
        "status": project.get("status"),
        "revision": state.get("revision"),
        "design_revision": project.get("design_revision"),
        "design": {
            "root": design.get("root"),
            "files": design.get("files"),
            "content_hash": design.get("content_hash"),
        },
        "validation": {
            "candidate_ready": validation.get("candidate_ready"),
            "assurance": validation.get("assurance"),
        },
        "release": {"ready": release.get("ready")},
    }


def _require_project(project_id: str | None, spec: CapabilitySpec) -> str:
    if not project_id:
        raise ValidationError(
            f"{spec.name} requires a project_id; open or create a project first"
        )
    return project_id


def _readiness(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    for key in ("artifacts", "checks", "command", "log", "output"):
        result.pop(key, None)
    return result


def _load_semantic_plan(service: Any, project_id: str) -> dict[str, Any] | None:
    root = service.project_root(project_id)
    path = root / "pending-circuit-plan.json"
    if path.is_symlink() or not path.is_file():
        return None
    value = load_json_limited(path, _PLAN_FILE_LIMIT)
    return value if isinstance(value, dict) else None


def _graph_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    components = plan.get("components")
    components = components if isinstance(components, list) else []
    nets = plan.get("nets")
    nets = nets if isinstance(nets, list) else []
    blocks = plan.get("blocks")
    blocks = blocks if isinstance(blocks, list) else []
    domains = plan.get("power_domains")
    domains = domains if isinstance(domains, list) else []
    interfaces = plan.get("interfaces")
    interfaces = interfaces if isinstance(interfaces, list) else []
    truncated = len(components) > _GRAPH_COMPONENT_LIMIT or len(nets) > _GRAPH_NET_LIMIT
    return {
        "design_id": plan.get("design_id"),
        "version": plan.get("version"),
        "summary": plan.get("summary"),
        "assumptions": plan.get("assumptions"),
        "components": components[:_GRAPH_COMPONENT_LIMIT],
        "component_count": len(components),
        "nets": nets[:_GRAPH_NET_LIMIT],
        "net_count": len(nets),
        "blocks": [block.get("id") for block in blocks if isinstance(block, Mapping)],
        "power_domains": [
            domain.get("id") for domain in domains if isinstance(domain, Mapping)
        ],
        "interfaces": [
            interface.get("id")
            for interface in interfaces
            if isinstance(interface, Mapping)
        ],
        "constraints": plan.get("constraints"),
        "truncated": truncated,
    }


def _capabilities_result(
    spec: CapabilitySpec,
    registry: PCBCapabilityRegistry,
) -> dict[str, Any]:
    domain = None if spec.domain == "pcb_project" else spec.domain
    return {
        "tool": spec.domain,
        "operation": spec.operation,
        "supported": True,
        "success": True,
        "result": registry.describe(domain),
    }


def _macro_result(
    spec: CapabilitySpec,
    result: Any,
    registry: PCBToolRegistry,
) -> dict[str, Any]:
    """Turn one executor receipt into fact-only router output."""

    view = result.view
    artifacts = view.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    validation = _readiness(artifacts.get("validation"))
    release = _readiness(artifacts.get("release"))
    payload: dict[str, Any] = {
        "tool": spec.domain,
        "operation": spec.operation,
        "supported": True,
        "success": True,
        "dispatched_macro": registry.resolve(
            spec.macro or spec.operation
        ).external_name,
        "project_id": result.call.project_id,
        "status": result.after_status,
        "revision": result.after_revision,
        "changed": [
            {
                "status": [result.before_status, result.after_status],
                "revision": [result.before_revision, result.after_revision],
            }
        ],
        "evidence_refs": {
            "validation": validation,
            "release": release,
        },
    }
    if spec.limitations:
        payload["limitations"] = list(spec.limitations)
    return payload


def execute_capability(
    domain: str,
    operation: str,
    *,
    service: Any,
    arguments: Mapping[str, Any] | None = None,
    project_id: str | None = None,
    registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY,
    capabilities: PCBCapabilityRegistry = DEFAULT_CAPABILITY_REGISTRY,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Dispatch one domain-router operation and report facts only.

    Raises :class:`PCBDraftError` for invalid input; callers translate that
    into an honest error result.  Unsupported capabilities never raise: they
    return the explicit ``supported: false`` report.
    """

    spec = capabilities.resolve(domain, operation)
    if not spec.supported:
        return capabilities.unsupported_report(spec)
    arguments = _validate_arguments(spec, arguments or {})

    if operation == "capabilities":
        return _capabilities_result(spec, capabilities)

    if spec.macro is not None:
        project_id = _require_project(project_id, spec)
        view = service.open_project(project_id)
        call = call_from_view(
            spec.macro,
            project_id,
            source="model",
            arguments={},
            view=view,
        )
        executor = PCBToolExecutor(service, registry=registry)
        result = executor.execute(call, timeout=timeout)
        return _macro_result(spec, result, registry)

    if domain == "pcb_project":
        if operation == "list":
            return {
                "tool": domain,
                "operation": operation,
                "supported": True,
                "success": True,
                "result": {"projects": service.list_projects()},
            }
        if operation == "open":
            project_id = _require_project(project_id, spec)
            view = service.open_project(project_id)
            facts = _view_facts(view)
            facts.update(
                {
                    "tool": domain,
                    "operation": operation,
                    "supported": True,
                    "success": True,
                    "evidence_refs": {
                        "validation": _readiness(
                            (view.get("artifacts") or {}).get("validation")
                        ),
                        "release": _readiness(
                            (view.get("artifacts") or {}).get("release")
                        ),
                    },
                }
            )
            return facts
        if operation == "create":
            created = service.create_draft(arguments["name"])
            project = created.get("project") or {}
            return {
                "tool": domain,
                "operation": operation,
                "supported": True,
                "success": True,
                "project_id": project.get("id"),
                "project_name": project.get("name"),
                "status": project.get("status"),
                "changed": [{"project": [None, project.get("id")]}],
                "findings": [
                    (
                        "created a draft conversation project; no engineering "
                        "files exist yet"
                    )
                ],
            }

    if domain == "pcb_library":
        from pcbdraft.agent.part_resolver import LocalKiCadPartResolver

        resolver = LocalKiCadPartResolver()
        if operation == "search_symbols":
            limit = arguments.get("limit", 12)
            candidates = resolver.find(arguments["query"], limit=int(limit))
            return {
                "tool": domain,
                "operation": operation,
                "supported": True,
                "success": True,
                "result": {
                    "query": arguments["query"],
                    "candidates": [candidate.to_dict() for candidate in candidates],
                },
                "limitations": list(spec.limitations),
            }
        if operation == "describe_symbol":
            candidate = resolver.describe(arguments["symbol"])
            return {
                "tool": domain,
                "operation": operation,
                "supported": True,
                "success": True,
                "result": candidate.to_dict(),
                "limitations": list(spec.limitations),
            }

    if domain == "pcb_design":
        project_id = _require_project(project_id, spec)
        view = service.open_project(project_id)
        facts = _view_facts(view)
        plan = _load_semantic_plan(service, project_id)
        if operation == "inspect_graph":
            if plan is None:
                return {
                    "tool": domain,
                    "operation": operation,
                    "supported": True,
                    "success": True,
                    **facts,
                    "findings": [
                        (
                            "no semantic plan exists yet for this project; "
                            "the semantic graph is empty"
                        )
                    ],
                }
            return {
                "tool": domain,
                "operation": operation,
                "supported": True,
                "success": True,
                **facts,
                "result": {"graph": _graph_summary(plan)},
                "limitations": list(spec.limitations),
            }
        if operation in {"inspect_component", "inspect_net"}:
            if plan is None:
                raise PCBDraftError(
                    "no semantic plan exists yet for this project; the "
                    "semantic graph is empty"
                )
            key = "component" if operation == "inspect_component" else "net"
            collection = (
                plan.get("components") if key == "component" else plan.get("nets")
            )
            collection = collection if isinstance(collection, list) else []
            wanted = arguments[key]
            entry = next(
                (
                    item
                    for item in collection
                    if isinstance(item, Mapping) and item.get("id") == wanted
                ),
                None,
            )
            if entry is None:
                raise PCBDraftError(
                    f"{key} {wanted!r} was not found in the semantic design graph"
                )
            related: list[dict[str, Any]] = []
            if key == "component":
                nets = plan.get("nets")
                for net in nets if isinstance(nets, list) else []:
                    if not isinstance(net, Mapping):
                        continue
                    endpoints = net.get("endpoints")
                    if isinstance(endpoints, list) and any(
                        isinstance(endpoint, Mapping)
                        and endpoint.get("component") == wanted
                        for endpoint in endpoints
                    ):
                        related.append({"id": net.get("id"), "endpoints": endpoints})
            return {
                "tool": domain,
                "operation": operation,
                "supported": True,
                "success": True,
                **facts,
                "result": {key: dict(entry), "connected_nets": related},
            }

    if domain == "pcb_inspect":
        project_id = _require_project(project_id, spec)
        if operation == "events":
            after = arguments.get("after", 0)
            events = service.events(project_id, after=int(after))
            return {
                "tool": domain,
                "operation": operation,
                "supported": True,
                "success": True,
                "project_id": project_id,
                "result": {"events": events[-_EVENT_LIMIT:]},
                "findings": [f"returned {min(len(events), _EVENT_LIMIT)} events"],
            }
        if operation == "evidence":
            view = service.open_project(project_id)
            facts = _view_facts(view)
            attempts = view.get("attempts")
            return {
                "tool": domain,
                "operation": operation,
                "supported": True,
                "success": True,
                **facts,
                "evidence_refs": {
                    "validation": _readiness(
                        (view.get("artifacts") or {}).get("validation")
                    ),
                    "release": _readiness((view.get("artifacts") or {}).get("release")),
                },
                "result": {
                    "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
                },
                "limitations": list(spec.limitations),
            }

    raise ValidationError(
        f"capability {spec.name} is declared supported but has no handler"
    )
