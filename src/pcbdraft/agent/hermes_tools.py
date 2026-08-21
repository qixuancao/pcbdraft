"""Register the flat, PCBDraft-only PCB toolbox into vendored Hermes."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    "reset_session_project_context",
    "set_current_project_id",
)

_PCB_TOOLSET = "pcbdraft"

#: Timeout shared by the concrete Hermes-facing flat PCB handlers.
DEFAULT_PCB_TOOL_TIMEOUT = 600.0

#: Tool result JSON is bounded so model context stays reviewable.
_MODEL_SUMMARY_EVENT_LIMIT = 8

_GLOBAL_LIBRARY_TOOLS = frozenset(
    {
        "search_symbols",
        "describe_symbol",
        "search_footprints",
        "describe_footprint",
    }
)

_service_cache: Any = None
_permission_mode: PermissionMode = "workspace"


@dataclass(frozen=True)
class _SessionProjectBinding:
    epoch: int
    project_id: str


class _ProjectContextStore:
    """Bind model sessions to trusted human project selections."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._epoch = 0
        self._trusted_project_id: str | None = None
        self._sessions: dict[str, _SessionProjectBinding] = {}

    def trusted_project_id(self) -> str | None:
        with self._lock:
            return self._trusted_project_id

    def select_trusted(self, project_id: str | None) -> None:
        with self._lock:
            self._epoch += 1
            self._trusted_project_id = project_id
            self._sessions.clear()

    def bound_project(self, session_id: str) -> str | None:
        if not session_id:
            raise PCBDraftError("PCB project access requires a trusted Hermes session")
        with self._lock:
            binding = self._sessions.get(session_id)
            if binding is not None and binding.epoch == self._epoch:
                return binding.project_id
            if self._trusted_project_id is None:
                return None
            self._sessions[session_id] = _SessionProjectBinding(
                self._epoch, self._trusted_project_id
            )
            return self._trusted_project_id

    def create_and_bind(
        self,
        session_id: str,
        create: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Serialize model creation against trusted human selection changes."""

        if not session_id:
            raise PCBDraftError(
                "PCB project creation requires a trusted Hermes session"
            )
        with self._lock:
            current = self._sessions.get(session_id)
            if current is not None and current.epoch == self._epoch:
                raise PCBDraftError(
                    "this Hermes session is already bound to a PCB project"
                )
            if self._trusted_project_id is not None:
                raise PCBDraftError(
                    "this Hermes session already has a user-selected PCB project"
                )
            view = create()
            project = view.get("project")
            project_id = project.get("id") if isinstance(project, Mapping) else None
            if not isinstance(project_id, str) or not project_id:
                raise PCBDraftError("PCB project creation returned no project identity")
            self._epoch += 1
            self._trusted_project_id = project_id
            self._sessions.clear()
            self._sessions[session_id] = _SessionProjectBinding(self._epoch, project_id)
            return view

    def reset_session(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)


_project_context = _ProjectContextStore()


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
    """Return the current trusted human PCB project selection, if any."""

    return _project_context.trusted_project_id()


def set_current_project_id(value: str | None) -> None:
    """Set or clear the process-scoped current PCB project id.

    This is a convenience cursor for the interactive surface only; durable
    state always lives in the project records under the repository.
    """

    _project_context.select_trusted(value)


def reset_session_project_context(session_id: str) -> None:
    """Forget one ended Hermes session without changing human selection."""

    _project_context.reset_session(session_id)


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


def _execute_tool(
    spec: ToolSpec, arguments: dict[str, Any], *, session_id: str
) -> dict[str, Any]:
    """Execute one concrete PCB tool through permissions and the executor."""

    service = _service()
    registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY
    arguments = registry.normalize_arguments(spec.name, arguments)
    executor = PCBToolExecutor(service, registry=registry)
    permissions = PermissionBroker(_permission_mode)

    if spec.name in _GLOBAL_LIBRARY_TOOLS:
        return {
            "tool": spec.external_name,
            "success": True,
            "project_id": None,
            "result": service.inspect_installed_library(spec.name, arguments),
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
        view = _project_context.create_and_bind(
            session_id,
            lambda: service.create_empty_project(arguments["name"]),
        )
        return _model_summary(spec, view)

    current_project_id = _project_context.bound_project(session_id)
    if not current_project_id:
        raise PCBDraftError(
            f"{spec.external_name} requires a current project; create one or select one with a trusted user command first"
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
    return _model_summary(spec, result.view)


def _handler(spec: ToolSpec) -> Callable[[dict[str, Any]], str]:
    def handle(args: dict[str, Any], **kwargs: Any) -> str:
        session_id = str(kwargs.get("session_id") or "")
        try:
            summary = _execute_tool(spec, dict(args or {}), session_id=session_id)
            return json.dumps(summary, ensure_ascii=False)
        except PCBDraftError as exc:
            payload: dict[str, Any] = {
                "tool": spec.external_name,
                "success": False,
                "error": str(exc),
            }
            if spec.name not in _GLOBAL_LIBRARY_TOOLS:
                try:
                    project_id = _project_context.bound_project(session_id)
                    view = _service().open_project(str(project_id or ""))
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
