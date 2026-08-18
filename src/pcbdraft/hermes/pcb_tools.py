"""Register the PCBDraft PCB tools into the vendored Hermes tool registry.

Each existing :class:`~pcbdraft.agent.tooling.ToolSpec` becomes one Hermes
tool whose handler executes through the authoritative
:class:`~pcbdraft.agent.tooling.PCBToolExecutor`.  The Hermes agent decides
when to call which tool; it is no longer driven by a hardcoded control flow.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

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
    ToolCall,
    ToolResult,
    ToolSpec,
    call_from_view,
)
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.hermes.bridge import DEFAULT_PCB_TOOL_TIMEOUT, _MODEL_SUMMARY_EVENT_LIMIT

__all__ = ("register_all_pcb_tools",)

_PCB_TOOLSET = "hermes-cli"

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


def _project_slug(message: str) -> str:
    text = " ".join(message.replace("\x00", "").split())
    if not text:
        return "pcbdraft"
    return text[:48]


def _status_next_step(status: Any) -> str:
    guidance: dict[str, str] = {
        "draft": "call pcb_plan_request with the user's request to produce a circuit plan",
        "needs_clarification": "ask the user for the missing information, then call pcb_plan_request again",
        "planning_required": "call pcb_plan_request again with a clearer request, or ask the user",
        "awaiting_confirmation": "call pcb_generate_candidate to materialize the plan as native KiCad files",
        "generated": "call pcb_validate to run connection checks, ERC, and DRC",
        "validation_failed": "review the validation findings and call pcb_repair_candidate with feedback, then pcb_validate",
        "validated": "call pcb_render_previews for previews, then pcb_build_release for the manufacturing bundle",
        "change_ready": "call pcb_apply_candidate to publish the staged change, or pcb_discard_candidate to reject it",
        "released": "call pcb_render_previews or pcb_build_release to (re)produce artifacts",
        "repair_failed": "call pcb_plan_request to revise the plan from the retained evidence",
        "generation_failed": "call pcb_repair_candidate with generation feedback, then pcb_generate_candidate",
        "interrupted": "continue by calling the next appropriate PCB tool",
        "release_failed": "review the release evidence and call pcb_build_release again",
        "provider_error": "ask the user to configure a model service, then call pcb_plan_request again",
    }
    return guidance.get(status, "continue with the next appropriate PCB tool")


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
    """Bound the public view into a compact, model-readable tool result."""

    project = view.get("project") or {}
    artifacts = view.get("artifacts") or {}
    conversation = view.get("conversation") or {}
    result: dict[str, Any] = {
        "tool": spec.external_name,
        "project_id": project.get("id"),
        "project_name": project.get("name"),
        "status": project.get("status"),
        "design_revision": project.get("design_revision"),
        "next_step": _status_next_step(project.get("status")),
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
    if attempt > MAX_AUTOMATIC_REPAIRS:
        attempt = MAX_AUTOMATIC_REPAIRS
    phase = feedback.get("phase") if isinstance(feedback.get("phase"), str) else "validation"
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
    """Execute one PCB tool through the authoritative executor and summarize."""

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
                "error": str(exc),
            }
            try:
                view = _service().open_project(
                    str((args or {}).get("project_id") or _current_project_id or "")
                )
                project = view.get("project") or {}
                payload["project_id"] = project.get("id")
                payload["status"] = project.get("status")
                payload["next_step"] = _status_next_step(project.get("status"))
            except Exception:
                payload["next_step"] = _status_next_step(None)
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:  # pragma: no cover - defensive Hermes boundary
            return json.dumps(
                {
                    "tool": spec.external_name,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )

    return handle


def register_all_pcb_tools() -> None:
    """Register every PCB tool spec into the Hermes tool registry."""

    from tools.registry import registry as hermes_registry

    for spec in DEFAULT_PCB_TOOL_REGISTRY.specs:
        hermes_registry.register(
            name=spec.external_name,
            toolset=_PCB_TOOLSET,
            schema={
                "name": spec.external_name,
                "description": spec.protocol_description,
                "parameters": spec.input_schema,
            },
            handler=_handler(spec),
            description=spec.protocol_description,
            emoji="🔌",
            max_result_size_chars=64 * 1024,
        )