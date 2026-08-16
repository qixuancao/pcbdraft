"""Deterministic policy for one PCB-agent turn.

This module currently chooses the next tool with ordinary Python control flow;
it is not model tool-calling.  Every chosen operation still crosses the typed
registry/executor boundary in :mod:`pcbdraft.agent.tooling`.  A future provider
or MCP call producer may replace this policy, but it must not bypass that local
executor or its revision, status, argument, effect, and risk contracts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pcbdraft.agent.permissions import (
    PCBToolGateway,
    PermissionBroker,
    ToolPermissionError,
)
from pcbdraft.agent.ports import LegacyRuntimeServicePort
from pcbdraft.agent.repair import (
    MAX_AUTOMATIC_REPAIRS,
    generation_feedback,
    normalize_repair_feedback,
    validation_feedback,
)
from pcbdraft.agent.tooling import (
    PCBToolExecutor,
    ToolCall,
    call_from_view,
)
from pcbdraft.core.errors import PCBDraftError


@dataclass(frozen=True)
class DeterministicRuntimePolicy:
    """Produce bounded calls from status transitions, without model autonomy."""

    project_id: str

    def call(
        self,
        name: str,
        view: Mapping[str, Any],
        arguments: Mapping[str, Any] | None = None,
    ) -> ToolCall:
        return call_from_view(
            name,
            self.project_id,
            source="runtime_policy",
            arguments=arguments or {},
            view=view,
        )


def _execute_policy_call(
    gateway: PCBToolGateway,
    policy: DeterministicRuntimePolicy,
    name: str,
    view: dict[str, Any],
    *,
    timeout: float,
    arguments: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    call = policy.call(name, view, arguments)
    return gateway.execute(call, timeout=timeout, observed_view=view).view


def _initial_view(
    service: LegacyRuntimeServicePort,
    executor: PCBToolExecutor,
    project_id: str,
) -> dict[str, Any]:
    """Load the initial baseline, retaining compatibility with narrow adapters."""

    opener = getattr(service, "open_project", None)
    if callable(opener):
        return executor.snapshot(project_id)
    # Some in-process service adapters expose only the operations they dispatch.
    # The real ApplicationService always takes the authoritative branch above.
    return {
        "project": {"id": project_id, "status": "draft"},
        "state": {"revision": 0},
    }


def _stopped(
    service: LegacyRuntimeServicePort,
    project_id: str,
    view: dict[str, Any],
    message: str,
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
    service: LegacyRuntimeServicePort,
    project_id: str,
    message: str,
    *,
    timeout: float,
    cancellation_requested: Callable[[], bool],
    permissions: PermissionBroker | None = None,
) -> dict[str, Any]:
    """Run one autonomous design turn and stop at consistent boundaries.

    Requirement interpretation and circuit planning form the first tool boundary.
    Native generation or change application starts only if that boundary completed
    and cancellation has not been requested.  This deterministic policy chooses
    calls; :class:`PCBToolGateway` owns authorization before the executor's fixed
    dispatch, while the application service remains the transactional PCB-state
    authority.
    """

    executor = PCBToolExecutor(service)
    gateway = PCBToolGateway(executor, permissions or PermissionBroker("workspace"))
    policy = DeterministicRuntimePolicy(project_id)
    view = _initial_view(service, executor, project_id)
    view = _execute_policy_call(
        gateway,
        policy,
        "plan_request",
        view,
        timeout=timeout,
        arguments={"message": message},
    )
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
                view = _execute_policy_call(
                    gateway,
                    policy,
                    "repair_candidate",
                    view,
                    timeout=timeout,
                    arguments={"feedback": pending_feedback},
                )
            except ToolPermissionError:
                raise
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
                view = _execute_policy_call(
                    gateway,
                    policy,
                    "generate_candidate",
                    view,
                    timeout=timeout,
                )
            except ToolPermissionError:
                raise
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
            view = _execute_policy_call(
                gateway,
                policy,
                "validate",
                view,
                timeout=timeout,
            )
            continue
        if status == "change_ready":
            if cancellation_requested():
                return _stopped(
                    service,
                    project_id,
                    view,
                    "Stopped before the staged PCB repair was applied",
                )
            return _execute_policy_call(
                gateway,
                policy,
                "apply_candidate",
                view,
                timeout=timeout,
            )
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
