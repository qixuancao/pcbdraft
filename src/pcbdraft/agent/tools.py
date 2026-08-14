"""PCB-agent tool orchestration over the deterministic application service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pcbdraft.agent.repair import (
    MAX_AUTOMATIC_REPAIRS,
    generation_feedback,
    normalize_repair_feedback,
    validation_feedback,
)
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.services.application import ApplicationService


def _stopped(
    service: ApplicationService, project_id: str, view: dict[str, Any], message: str
) -> dict[str, Any]:
    service.record_progress(
        project_id,
        "agent.stopped",
        message,
        level="warning",
    )
    return view


def _rejected_candidate_feedback(
    view: Mapping[str, Any], *, attempt: int
) -> dict[str, Any] | None:
    conversation = view.get("conversation")
    messages = (
        conversation.get("messages") if isinstance(conversation, Mapping) else None
    )
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, Mapping):
            continue
        data = message.get("data")
        feedback = data.get("repair_feedback") if isinstance(data, Mapping) else None
        if not isinstance(feedback, Mapping):
            continue
        revised = dict(feedback)
        revised["attempt"] = attempt
        return normalize_repair_feedback(revised)
    return None


def run_design_turn(
    service: ApplicationService,
    project_id: str,
    message: str,
    *,
    timeout: float,
    cancellation_requested: Callable[[], bool],
) -> dict[str, Any]:
    """Run one autonomous design turn and stop at consistent boundaries.

    Requirement interpretation and circuit planning form the first tool boundary.
    Native generation or change application starts only if that boundary completed
    and cancellation has not been requested. Application operations remain
    transactional; this function owns orchestration, not PCB state.
    """

    view = service.send_message(project_id, message, timeout=timeout)
    if cancellation_requested():
        return _stopped(
            service,
            project_id,
            view,
            "Stopped before the next PCB tool was started",
        )
    repairs_used = 0
    pending_feedback: dict[str, Any] | None = None
    while True:
        if pending_feedback is not None:
            if cancellation_requested():
                return _stopped(
                    service,
                    project_id,
                    view,
                    "Stopped before automatic PCB repair was started",
                )
            try:
                view = service.prepare_agent_repair(
                    project_id, pending_feedback, timeout=timeout
                )
            except PCBDraftError as exc:
                view = service.open_project(project_id)
                if repairs_used >= MAX_AUTOMATIC_REPAIRS:
                    raise
                repairs_used += 1
                pending_feedback = generation_feedback(view, exc, attempt=repairs_used)
                continue
            pending_feedback = None
            continue

        status = view["project"]["status"]
        if status == "awaiting_confirmation":
            try:
                view = service.confirm_project(
                    project_id, validate=False, timeout=timeout
                )
            except PCBDraftError as exc:
                view = service.open_project(project_id)
                if repairs_used >= MAX_AUTOMATIC_REPAIRS:
                    raise
                repairs_used += 1
                pending_feedback = generation_feedback(view, exc, attempt=repairs_used)
                continue
            if cancellation_requested():
                return _stopped(
                    service,
                    project_id,
                    view,
                    "Stopped before PCB validation was started",
                )
            view = service.validate_project(project_id, timeout=timeout)
            continue
        if status == "change_ready":
            if cancellation_requested():
                return _stopped(
                    service,
                    project_id,
                    view,
                    "Stopped before the staged PCB repair was applied",
                )
            return service.apply_modification(project_id)
        if status == "repair_failed":
            if repairs_used >= MAX_AUTOMATIC_REPAIRS:
                raise PCBDraftError(
                    "automatic PCB repair attempts were exhausted; retained evidence is available for review"
                )
            repairs_used += 1
            pending_feedback = _rejected_candidate_feedback(view, attempt=repairs_used)
            if pending_feedback is None:
                raise PCBDraftError(
                    "automatic PCB repair failed without reusable deterministic feedback"
                )
            continue
        if status in {"generated", "validated", "validation_failed"}:
            if repairs_used < MAX_AUTOMATIC_REPAIRS:
                feedback = validation_feedback(view, attempt=repairs_used + 1)
                if feedback is not None:
                    repairs_used += 1
                    pending_feedback = feedback
                    continue
            return view
        return view
