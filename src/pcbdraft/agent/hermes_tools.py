"""Register the flat, PCBDraft-only PCB toolbox into vendored Hermes."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from pcbdraft.agent.permissions import (
    PCBToolGateway,
    PermissionBroker,
    PermissionMode,
    ToolPermissionError,
)
from pcbdraft.agent.tooling import (
    DEFAULT_PCB_TOOL_REGISTRY,
    PCBToolExecutor,
    PCBToolRegistry,
    ToolCall,
    ToolResult,
    ToolSpec,
    call_from_view,
)
from pcbdraft.core.errors import PCBDraftError

__all__ = (
    "get_current_project_id",
    "get_service",
    "refresh_service_provider",
    "register_all_pcb_tools",
    "set_current_project_id",
)

_PCB_TOOLSET = "pcbdraft"

#: Timeout shared by the concrete Hermes-facing flat PCB handlers.
DEFAULT_PCB_TOOL_TIMEOUT = 600.0

#: Tool result JSON is bounded so model context stays reviewable.
_MODEL_SUMMARY_EVENT_LIMIT = 8

#: Keep a process-scoped default project so the agent does not need to echo
#: the project id on every call.  Handlers still honour an explicit id.
_current_project_id: str | None = None
_service_cache: Any = None
_permission_mode: PermissionMode = "workspace"


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


def refresh_service_provider() -> None:
    """Refresh a cached service after the persistent model authority changes."""

    if _service_cache is None:
        return
    from pcbdraft.model.providers import resolve_provider

    _service_cache.provider = resolve_provider("auto")


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
    tool_result = view.get("tool_result")
    if isinstance(tool_result, Mapping):
        result["result"] = dict(tool_result)
    result["validation"] = _readiness(artifacts.get("validation"))
    result["release"] = _readiness(artifacts.get("release"))
    events = view.get("events")
    if isinstance(events, list) and events:
        result["events"] = [
            _event_summary(item) for item in events[-_MODEL_SUMMARY_EVENT_LIMIT:]
        ]
    return result


def _execute_tool(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one concrete PCB tool through permissions and the executor."""

    global _current_project_id
    service = _service()
    registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY
    executor = PCBToolExecutor(service, registry=registry)
    permissions = PermissionBroker(_permission_mode)

    if spec.name == "list_projects":
        return {
            "tool": spec.external_name,
            "success": True,
            "projects": service.list_projects(),
        }
    if spec.name == "create_project":
        call = ToolCall(
            name=spec.name,
            project_id="repository",
            source="model",
            arguments=arguments,
            baseline_revision=0,
        )
        verdict = permissions.decide(call, spec)
        if verdict.action != "allow":
            raise ToolPermissionError(verdict.reason)
        view = service.create_empty_project(arguments["name"])
        project_id = str(view.get("project", {}).get("id"))
        _current_project_id = project_id
        return _model_summary(spec, view)
    if spec.name == "open_project":
        project_id = str(arguments["project_id"])
        view = service.open_project(project_id)
        _current_project_id = project_id
        return _model_summary(spec, view)

    current_project_id = _current_project_id
    if not current_project_id:
        raise PCBDraftError(
            f"{spec.external_name} requires a current project; call pcb_open_project or pcb_create_project first"
        )
    call = call_from_view(
        spec.name,
        current_project_id,
        source="model",
        arguments=arguments,
        view=service.open_project(current_project_id),
    )
    gateway = PCBToolGateway(executor, permissions)
    result: ToolResult = gateway.execute(call, timeout=DEFAULT_PCB_TOOL_TIMEOUT)
    _current_project_id = current_project_id
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
            del exc
            return json.dumps(
                {
                    "tool": spec.external_name,
                    "success": False,
                    "error": "internal PCB tool failure",
                },
                ensure_ascii=False,
            )

    return handle


def register_all_pcb_tools(*, permission_mode: PermissionMode = "workspace") -> None:
    """Register only concrete flat tools under the PCBDraft toolset."""

    from tools.registry import registry as hermes_registry

    global _permission_mode
    _permission_mode = permission_mode

    for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
        hermes_registry.register(
            name=spec.external_name,
            toolset=_PCB_TOOLSET,
            schema={
                "name": spec.external_name,
                "description": (spec.protocol_description),
                "parameters": spec.input_schema,
            },
            handler=_handler(spec),
            description=spec.protocol_description,
            emoji="🔌",
            max_result_size_chars=64 * 1024,
        )
