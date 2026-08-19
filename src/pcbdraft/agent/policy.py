"""Legacy deterministic next-tool policy for one durable PCB agent turn.

This producer implements the historical fixed plan/generate/validate/repair
workflow.  It is retained as an explicit legacy mode: the TUI/JobRunner
durable-turn path and compatibility macros still use it, but it is not the
controller of the default Hermes agent.  In the default Hermes Goal Mode the
model re-selects the next tool itself after every result, and project status
reports engineering facts only.

This module intentionally contains no persistence, permission, provider, or
application-service implementation details.  It translates retained turn state
and a public project snapshot into the next bounded tool intent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pcbdraft.agent.repair import (
    MAX_AUTOMATIC_REPAIRS,
    generation_feedback,
    normalize_repair_feedback,
    validation_feedback,
)
from pcbdraft.agent.tooling import ToolSource, project_status_and_revision
from pcbdraft.agent.turns import ToolRunStatus, TurnRecord
from pcbdraft.core.errors import PCBDraftError


@dataclass(frozen=True)
class ProposedToolCall:
    """One producer decision before local authority is evaluated."""

    name: str
    arguments: Mapping[str, Any]
    source: ToolSource = "runtime_policy"
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ConversationStep:
    """One conversational model step: a durable reply with an optional intent."""

    reply: str | None = None
    proposal: ProposedToolCall | None = None


class PCBCallProducer(Protocol):
    """Transport-neutral source of the next strict, revision-bound tool intent."""

    def conversation_step(
        self,
        record: TurnRecord,
        view: Mapping[str, Any],
        *,
        timeout: float,
    ) -> ConversationStep | None: ...

    def next_call(
        self,
        record: TurnRecord,
        view: Mapping[str, Any],
        *,
        timeout: float,
    ) -> ProposedToolCall | None: ...


class DeterministicPCBCallProducer:
    """Legacy workflow policy: one bounded PCB tool from durable state.

    Kept as the compatibility/legacy controller for explicit shortcut turns
    (slash commands) and the durable TUI job path.  It must not be used to
    override a model's autonomous tool selection in the default Hermes Goal
    Mode.
    """

    def conversation_step(
        self,
        record: TurnRecord,
        view: Mapping[str, Any],
        *,
        timeout: float,
    ) -> ConversationStep | None:
        """Deterministic policy never speaks for the model; it only routes calls."""

        del record, view, timeout
        return None

    def next_call(
        self,
        record: TurnRecord,
        view: Mapping[str, Any],
        *,
        timeout: float,
    ) -> ProposedToolCall | None:
        del timeout
        completed = [
            tool for tool in record.tool_runs if tool.status is ToolRunStatus.COMPLETED
        ]
        # An MCP tools/call is an exact single-operation request. Its durable
        # initial proposal is written before this producer is consulted, and no
        # workflow continuation may be inferred after that call completes.
        if any(tool.source == "mcp" for tool in record.tool_runs):
            return None
        if record.user_message.startswith("/pcb_"):
            status, _revision = project_status_and_revision(view)
            requested_tool = record.user_message.removeprefix("/")
            if not any(tool.tool_name == requested_tool for tool in completed):
                return ProposedToolCall(requested_tool, {}, source="user")
            if (
                completed
                and completed[-1].source == "user"
                and completed[-1].tool_name == "pcb_generate_candidate"
                and status == "generated"
            ):
                return ProposedToolCall("validate", {})
            return None
        # A model-selected first call is already the interpreted user intent.
        # Do not feed the same natural-language message back through plan_request
        # after a direct validate/preview/release/change operation. Calls that
        # start with pcb_plan_request still enter the normal generate -> validate
        # workflow below, while state-driven safety follow-ups (validation and
        # bounded repair) remain local for every model-selected write.
        model_direct = next(
            (
                tool
                for tool in record.tool_runs
                if tool.source == "model" and tool.tool_name != "pcb_plan_request"
            ),
            None,
        )
        if (
            model_direct is not None
            and model_direct.status is not ToolRunStatus.COMPLETED
        ):
            # A failed/denied direct intent must never be replaced by a different
            # state-derived action (for example discard -> apply on change_ready).
            return None
        if model_direct is None and not any(
            tool.tool_name == "pcb_plan_request" for tool in completed
        ):
            return ProposedToolCall("plan_request", {"message": record.user_message})

        status, _revision = project_status_and_revision(view)
        repairs_used = sum(
            tool.tool_name == "pcb_repair_candidate" for tool in record.tool_runs
        )
        if status == "awaiting_confirmation":
            return ProposedToolCall("generate_candidate", {})
        if status == "change_ready":
            return ProposedToolCall("apply_candidate", {})
        if status == "generation_failed":
            if repairs_used >= MAX_AUTOMATIC_REPAIRS:
                return None
            error = self._latest_error(record) or "native PCB generation failed"
            return ProposedToolCall(
                "repair_candidate",
                {
                    "feedback": generation_feedback(
                        view,
                        PCBDraftError(error),
                        attempt=repairs_used + 1,
                    )
                },
            )
        if status == "repair_failed":
            return self._retry_repair(record, repairs_used)
        if status == "generated":
            last_completed = completed[-1].tool_name if completed else None
            if last_completed != "pcb_validate":
                return ProposedToolCall("validate", {})
        if (
            status in {"generated", "validated", "validation_failed"}
            and repairs_used < MAX_AUTOMATIC_REPAIRS
        ):
            feedback = validation_feedback(view, attempt=repairs_used + 1)
            if feedback is not None:
                return ProposedToolCall("repair_candidate", {"feedback": feedback})
        return None

    @staticmethod
    def _latest_error(record: TurnRecord) -> str | None:
        return next(
            (
                tool.error
                for tool in reversed(record.tool_runs)
                if tool.error is not None
            ),
            None,
        )

    @staticmethod
    def _retry_repair(record: TurnRecord, repairs_used: int) -> ProposedToolCall | None:
        if repairs_used >= MAX_AUTOMATIC_REPAIRS:
            return None
        previous = next(
            (
                tool
                for tool in reversed(record.tool_runs)
                if tool.tool_name == "pcb_repair_candidate"
                and isinstance(tool.arguments.get("feedback"), Mapping)
            ),
            None,
        )
        if previous is None:
            return None
        feedback = dict(previous.arguments["feedback"])
        feedback["attempt"] = repairs_used + 1
        return ProposedToolCall(
            "repair_candidate", {"feedback": normalize_repair_feedback(feedback)}
        )
