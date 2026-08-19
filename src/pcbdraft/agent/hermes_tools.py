"""Register the PCBDraft PCB tools into the vendored Hermes tool registry.

Two layers are exposed to the Hermes agent:

* the existing high-level macro tools (:class:`~pcbdraft.agent.tooling.ToolSpec`
  entries such as ``pcb_validate``), kept as compatibility macros and
  shortcuts for simple projects; and
* the domain router tools (``pcb_project``, ``pcb_library``, ``pcb_design``,
  ``pcb_board``, ``pcb_inspect``, ``pcb_verify``, ``pcb_export``,
  ``pcb_analysis``) backed by the canonical capability registry in
  :mod:`pcbdraft.agent.capability_registry`.

Every handler executes through the authoritative
:class:`~pcbdraft.agent.tooling.PCBToolExecutor` /
:func:`~pcbdraft.agent.capability_registry.execute_capability` boundary.
The Hermes agent decides when to call which tool in any order; results
report facts only and never prescribe the next tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from pcbdraft.agent.capability_registry import (
    CAPABILITY_DOMAINS,
    DEFAULT_CAPABILITY_REGISTRY,
    PCBCapabilityRegistry,
    execute_capability,
    router_tool_schema,
)
from pcbdraft.agent.repair import (
    MAX_AUTOMATIC_REPAIRS,
    REPAIR_FEEDBACK_SCHEMA,
    REPAIR_FEEDBACK_VERSION,
    normalize_repair_feedback,
)
from pcbdraft.agent.tooling import (
    DEFAULT_PCB_TOOL_REGISTRY,
    PCBToolExecutor,
    PCBToolRegistry,
    ToolResult,
    ToolSpec,
    call_from_view,
)
from pcbdraft.core.errors import PCBDraftError

__all__ = (
    "get_current_project_id",
    "get_service",
    "register_all_pcb_tools",
    "set_current_project_id",
)

_PCB_TOOLSET = "hermes-cli"

#: Timeout shared by the Hermes-facing macro and router handlers.
DEFAULT_PCB_TOOL_TIMEOUT = 600.0

#: Tool result JSON is bounded so model context stays reviewable.
_MODEL_SUMMARY_EVENT_LIMIT = 8

#: Keep a process-scoped default project so the agent does not need to echo
#: the project id on every call.  Handlers still honour an explicit id.
_current_project_id: str | None = None
_service_cache: Any = None


def _service() -> Any:
    """Return one authoritative ApplicationService for this process."""

    global _service_cache
    if _service_cache is None:
        from pcbdraft.services.application import ApplicationService

        _service_cache = ApplicationService()
    return _service_cache


def _set_service(service: Any) -> None:
    """Allow tests and the launcher to pin an isolated service workspace."""

    global _service_cache
    _service_cache = service


def get_service() -> Any:
    """Return the authoritative ApplicationService for this process."""

    return _service()


def get_current_project_id() -> str | None:
    """Return the process-scoped current PCB project id, if any."""

    return _current_project_id


def set_current_project_id(value: str | None) -> None:
    """Set or clear the process-scoped current PCB project id.

    This is a convenience cursor for the interactive surface only; durable
    state always lives in the project records under the repository.
    """

    global _current_project_id
    _current_project_id = value


def _project_slug(message: str) -> str:
    text = " ".join(message.replace("\x00", "").split())
    if not text:
        return "pcbdraft"
    return text[:48]


def _readiness(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    for key in ("artifacts", "checks", "command", "log", "output"):
        result.pop(key, None)
    return result


def _event_summary(event: Any) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        return {"kind": str(event)}
    return {
        key: event.get(key)
        for key in ("kind", "level", "message", "at")
        if event.get(key) is not None
    }


def _model_summary(spec: ToolSpec, view: Mapping[str, Any]) -> dict[str, Any]:
    """Bound the public view into a compact, fact-only model-readable result.

    The result states what happened, what changed, and what evidence exists.
    It does not instruct the agent which tool to call next.
    """

    project = view.get("project") or {}
    state = view.get("state") or {}
    artifacts = view.get("artifacts") or {}
    conversation = view.get("conversation") or {}
    result: dict[str, Any] = {
        "tool": spec.external_name,
        "success": True,
        "project_id": project.get("id"),
        "project_name": project.get("name"),
        "status": project.get("status"),
        "revision": state.get("revision"),
        "design_revision": project.get("design_revision"),
    }
    proposal = conversation.get("proposal")
    if isinstance(proposal, Mapping):
        planning = proposal.get("planning")
        if isinstance(planning, Mapping):
            result["planning"] = {
                "state": planning.get("state"),
                "message": planning.get("message"),
            }
        brief = proposal.get("brief")
        if isinstance(brief, Mapping):
            result["plan"] = {
                key: brief.get(key)
                for key in (
                    "purpose",
                    "architecture",
                    "assumptions",
                    "power",
                    "interfaces",
                    "board",
                    "identity",
                    "bom",
                    "net_count",
                    "constraints",
                    "plan_review",
                    "semantic_content_hash",
                )
                if brief.get(key) is not None
            }
    design = view.get("design")
    if isinstance(design, Mapping):
        result["design"] = {
            "root": design.get("root"),
            "files": design.get("files"),
        }
    result["validation"] = _readiness(artifacts.get("validation"))
    result["release"] = _readiness(artifacts.get("release"))
    events = view.get("events")
    if isinstance(events, list) and events:
        result["events"] = [
            _event_summary(item) for item in events[-_MODEL_SUMMARY_EVENT_LIMIT:]
        ]
    return result


def _normalize_repair_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Accept a friendly repair request and normalize it to the strict schema."""

    feedback = dict(arguments.get("feedback") or {})
    if not feedback:
        raise PCBDraftError(
            "pcb_repair_candidate requires a feedback object with summary and findings"
        )
    attempt = feedback.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        attempt = 1
    attempt = min(attempt, MAX_AUTOMATIC_REPAIRS)
    phase = (
        feedback.get("phase")
        if isinstance(feedback.get("phase"), str)
        else "validation"
    )
    if phase not in {"generation", "validation", "user_request"}:
        phase = "validation"
    normalized = normalize_repair_feedback(
        {
            "schema": REPAIR_FEEDBACK_SCHEMA,
            "version": REPAIR_FEEDBACK_VERSION,
            "phase": phase,
            "attempt": attempt,
            "summary": feedback.get("summary"),
            "findings": feedback.get("findings") or [],
        }
    )
    return {"feedback": normalized}


def _execute_tool(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one PCB macro tool through the authoritative executor."""

    global _current_project_id
    service = _service()
    registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY
    executor = PCBToolExecutor(service, registry=registry)

    if spec.name == "plan_request":
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            raise PCBDraftError("pcb_plan_request requires a non-empty message")
        project_id = arguments.get("project_id") or _current_project_id
        if not project_id:
            view = service.create_project(_project_slug(message), message)
            project_id = str(view.get("project", {}).get("id"))
            _current_project_id = project_id
            return _model_summary(spec, view)
        call = call_from_view(
            spec.name,
            project_id,
            source="model",
            arguments={"message": message},
            view=service.open_project(project_id),
        )
    else:
        project_id = arguments.get("project_id") or _current_project_id
        if not project_id:
            raise PCBDraftError(
                f"{spec.external_name} requires a project_id; call pcb_plan_request first"
            )
        tool_arguments = {
            key: value for key, value in arguments.items() if key != "project_id"
        }
        if spec.name == "repair_candidate":
            tool_arguments = _normalize_repair_arguments(tool_arguments)
        call = call_from_view(
            spec.name,
            project_id,
            source="model",
            arguments=tool_arguments,
            view=service.open_project(project_id),
        )

    result: ToolResult = executor.execute(call, timeout=DEFAULT_PCB_TOOL_TIMEOUT)
    _current_project_id = project_id
    return _model_summary(spec, result.view)


def _handler(spec: ToolSpec) -> Callable[[dict[str, Any]], str]:
    def handle(args: dict[str, Any], **_kwargs: Any) -> str:
        try:
            summary = _execute_tool(spec, dict(args or {}))
            return json.dumps(summary, ensure_ascii=False)
        except PCBDraftError as exc:
            payload: dict[str, Any] = {
                "tool": spec.external_name,
                "success": False,
                "error": str(exc),
            }
            try:
                view = _service().open_project(
                    str((args or {}).get("project_id") or _current_project_id or "")
                )
                project = view.get("project") or {}
                payload["project_id"] = project.get("id")
                payload["status"] = project.get("status")
            except PCBDraftError:
                pass
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - defensive Hermes boundary
            return json.dumps(
                {
                    "tool": spec.external_name,
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )

    return handle


def _router_description(
    domain: str,
    *,
    registry: PCBCapabilityRegistry = DEFAULT_CAPABILITY_REGISTRY,
) -> str:
    """Describe one domain router honestly, including what it cannot do."""

    supported = [
        spec.operation for spec in registry.capabilities_for(domain) if spec.supported
    ]
    unsupported = [
        spec.operation
        for spec in registry.capabilities_for(domain)
        if not spec.supported
    ]
    lines = [
        (
            f"PCB {domain.removeprefix('pcb_')} domain router. Invoke one "
            f"capability per call via operation and arguments. Currently "
            f"supported operations: {', '.join(supported)}."
        ),
        (
            "Results report facts only (what ran, what changed, what evidence "
            "exists); the next action is always your decision."
        ),
    ]
    if unsupported:
        lines.append(
            f"Declared but not yet implemented (they will honestly return "
            f"supported=false): {', '.join(unsupported)}."
        )
    lines.append(
        'Call operation="capabilities" to list every operation with its '
        "argument schema and limitations."
    )
    return " ".join(lines)


def _router_handler(
    domain: str,
    *,
    registry: PCBCapabilityRegistry = DEFAULT_CAPABILITY_REGISTRY,
) -> Callable[[dict[str, Any]], str]:
    def handle(args: dict[str, Any], **_kwargs: Any) -> str:
        global _current_project_id
        arguments = dict(args or {})
        operation = arguments.get("operation")
        payload: dict[str, Any] = {"tool": domain, "operation": operation}
        try:
            if not isinstance(operation, str) or not operation:
                raise PCBDraftError(
                    f'{domain} requires operation="..." (use operation="capabilities" '
                    "to list supported operations)"
                )
            operation_arguments = arguments.get("arguments") or {}
            if not isinstance(operation_arguments, Mapping):
                raise PCBDraftError(f"{domain} arguments must be a JSON object")
            project_id = arguments.get("project_id") or _current_project_id
            if project_id is not None:
                payload["project_id"] = project_id
            result = execute_capability(
                domain,
                operation,
                service=_service(),
                arguments=operation_arguments,
                project_id=project_id,
                registry=DEFAULT_PCB_TOOL_REGISTRY,
                capabilities=registry,
                timeout=DEFAULT_PCB_TOOL_TIMEOUT,
            )
            observed = result.get("project_id")
            if isinstance(observed, str) and observed:
                _current_project_id = observed
            return json.dumps(result, ensure_ascii=False)
        except PCBDraftError as exc:
            payload["success"] = False
            payload["error"] = str(exc)
            try:
                view = _service().open_project(str(_current_project_id or ""))
                project = view.get("project") or {}
                payload["project_id"] = project.get("id")
                payload["status"] = project.get("status")
            except PCBDraftError:
                pass
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - defensive Hermes boundary
            payload["success"] = False
            payload["error"] = f"{type(exc).__name__}: {exc}"
            return json.dumps(payload, ensure_ascii=False)

    return handle


def register_all_pcb_tools() -> None:
    """Register the macro tools and domain routers into Hermes' registry."""

    from tools.registry import registry as hermes_registry

    for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
        hermes_registry.register(
            name=spec.external_name,
            toolset=_PCB_TOOLSET,
            schema={
                "name": spec.external_name,
                "description": (
                    f"{spec.protocol_description} (high-level macro; domain "
                    "routers offer finer-grained capabilities)"
                ),
                "parameters": spec.input_schema,
            },
            handler=_handler(spec),
            description=spec.protocol_description,
            emoji="🔌",
            max_result_size_chars=64 * 1024,
        )
    for domain in CAPABILITY_DOMAINS:
        hermes_registry.register(
            name=domain,
            toolset=_PCB_TOOLSET,
            schema={
                "name": domain,
                "description": _router_description(domain),
                "parameters": router_tool_schema(domain),
            },
            handler=_router_handler(domain),
            description=_router_description(domain),
            emoji="🔌",
            max_result_size_chars=64 * 1024,
        )
