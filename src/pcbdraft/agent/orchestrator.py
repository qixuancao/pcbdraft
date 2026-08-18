"""Crash-resumable orchestration for conversational PCB agent turns.

The orchestrator is deliberately transport-neutral. A capability-gated native
Responses producer may open a turn conversationally (durable prose reply and/or
one intent tool); deterministic local policy owns mandatory PCB follow-ups, and
MCP clients submit the same strict :class:`~pcbdraft.agent.tooling.ToolCall`
values. Tool intent is persisted before dispatch, permission is decided
centrally, and retries continue the existing turn instead of replaying the
user's message.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pcbdraft.agent.permissions import PermissionBroker
from pcbdraft.agent.policy import (
    DeterministicPCBCallProducer,
    PCBCallProducer,
    ProposedToolCall,
)
from pcbdraft.agent.ports import AgentServicePort
from pcbdraft.agent.repair import MAX_AUTOMATIC_REPAIRS
from pcbdraft.agent.tooling import (
    DEFAULT_PCB_TOOL_REGISTRY,
    PCBToolExecutor,
    PCBToolRegistry,
    ToolCall,
    ToolResult,
    call_from_view,
    project_status_and_revision,
)
from pcbdraft.agent.turns import (
    AgentTurnStore,
    ApprovalStatus,
    ToolRunRecord,
    ToolRunStatus,
    TurnRecord,
    TurnStatus,
)
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.runs import new_run_id

__all__ = (
    "DEFAULT_THREAD_ID",
    "MAX_TOOL_CALLS_PER_TURN",
    "AgentOrchestrator",
    "DeterministicPCBCallProducer",
    "PCBCallProducer",
    "ProposedToolCall",
)

DEFAULT_THREAD_ID = "main"
MAX_TOOL_CALLS_PER_TURN = 32


class AgentOrchestrator:
    """Persist, authorize, execute, and resume one PCB agent turn."""

    def __init__(
        self,
        service: AgentServicePort,
        *,
        registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY,
        permissions: PermissionBroker | None = None,
        producer: PCBCallProducer | None = None,
    ) -> None:
        self.service = service
        self.registry = registry
        self.permissions = permissions or PermissionBroker("workspace")
        if producer is None:
            # Imported lazily to keep the durable orchestration module independent
            # of any particular model transport at import time.
            from pcbdraft.model.tool_calls import ConfiguredPCBCallProducer

            producer = ConfiguredPCBCallProducer(service, registry=registry)
        self.producer = producer
        self.executor = PCBToolExecutor(service, registry=registry)

    def store(self, project_id: str, *, lock_timeout: float = 10.0) -> AgentTurnStore:
        """Return the project-scoped durable turn store."""

        return AgentTurnStore(
            self.service.project_root(project_id),
            self.service.locks_root,
            lock_timeout=lock_timeout,
        )

    def start_turn(
        self,
        project_id: str,
        message: str,
        *,
        thread_id: str = DEFAULT_THREAD_ID,
    ) -> TurnRecord:
        """Admit a user message durably before provider or tool work begins."""

        view = self.service.open_project(project_id)
        _status, revision = project_status_and_revision(view)
        return self.store(project_id).begin(
            project_id=project_id,
            thread_id=thread_id,
            user_message=sanitize_user_text(message),
            baseline_revision=revision,
        )

    def start_tool_turn(
        self,
        project_id: str,
        tool_name: str,
        *,
        thread_id: str = DEFAULT_THREAD_ID,
    ) -> TurnRecord:
        """Durably admit one explicit no-argument user tool before dispatch."""

        spec = self.registry.resolve(tool_name)
        if spec.arguments:
            raise ValidationError(
                "explicit agent tool submission requires a no-argument PCB tool"
            )
        view = self.service.open_project(project_id)
        turn = self.store(project_id).begin(
            project_id=project_id,
            thread_id=thread_id,
            user_message=f"/{spec.external_name}",
            baseline_revision=project_status_and_revision(view)[1],
        )
        return turn

    def start_external_tool_turn(
        self,
        project_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        thread_id: str = DEFAULT_THREAD_ID,
    ) -> TurnRecord:
        """Atomically admit one exact MCP tool call without granting user trust."""

        view = self.service.open_project(project_id)
        before_status, revision = project_status_and_revision(view)
        call = call_from_view(
            tool_name,
            project_id,
            source="mcp",
            arguments=arguments,
            view=view,
        )
        spec = self.registry.resolve(call.name)
        turn_id = f"turn-{new_run_id()}"
        tool = ToolRunRecord.proposed(
            project_id=project_id,
            thread_id=thread_id,
            turn_id=turn_id,
            tool_call_id=f"mcp-{new_run_id()}",
            tool_name=spec.external_name,
            source="mcp",
            effect=spec.effect,
            risk=spec.risk,
            arguments=call.arguments,
            args_hash=call.arguments_hash,
            baseline_revision=revision,
            before_status=before_status,
            before_revision=revision,
        )
        return self.store(project_id).begin_with_tool(
            project_id=project_id,
            thread_id=thread_id,
            user_message=f"/{spec.external_name}",
            baseline_revision=revision,
            tool_run=tool,
        )

    def run_turn(
        self,
        project_id: str,
        turn_id: str,
        *,
        timeout: float,
        cancellation_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        """Run or explicitly resume a durable turn at its next safe boundary."""

        store = self.store(project_id)
        record = store.load(turn_id)
        self._assert_turn_identity(record, project_id)
        if record.status is TurnStatus.COMPLETED:
            return self.service.open_project(project_id)
        if record.status is TurnStatus.WAITING_APPROVAL:
            return self.service.open_project(project_id)
        if record.status in {
            TurnStatus.FAILED,
            TurnStatus.INTERRUPTED,
            TurnStatus.CANCELLED,
        }:
            if self._has_non_replayable_interruption(record):
                raise PCBDraftError(
                    "an interrupted PCB tool may already have taken effect; "
                    "inspect the project and submit a new turn instead of retrying it"
                )
            if self._has_unfinished_model_direct_intent(record):
                raise PCBDraftError(
                    "the model-selected direct PCB operation did not complete; "
                    "submit a new turn so its original intent cannot be replaced"
                )
            record = store.resume(turn_id)
        elif record.status is TurnStatus.QUEUED:
            record = store.update(turn_id, TurnStatus.RUNNING)

        view = self.service.open_project(project_id)
        record, view = self._recover_active_call(store, record, view)
        for _ in range(MAX_TOOL_CALLS_PER_TURN):
            if cancellation_requested():
                store.cancel(
                    turn_id,
                    "stopped at a safe PCB tool boundary",
                )
                return view

            record = store.load(turn_id)
            if record.status is TurnStatus.WAITING_APPROVAL:
                return view
            active = self._active_tool(record)
            if active is None:
                step = self.producer.conversation_step(
                    record, view, timeout=timeout
                )
                proposal = step.proposal if step is not None else None
                delivered_reply = step is not None and step.reply is not None
                if delivered_reply:
                    record = self._deliver_reply(store, record, step.reply)
                if proposal is None and not delivered_reply:
                    proposal = self.producer.next_call(record, view, timeout=timeout)
                if proposal is None:
                    project_status, _revision = project_status_and_revision(view)
                    if delivered_reply:
                        store.update(
                            turn_id,
                            TurnStatus.COMPLETED,
                            stop_reason="the PCB agent answered conversationally",
                        )
                        return view
                    if project_status in {
                        "generation_failed",
                        "repair_failed",
                        "provider_error",
                    }:
                        error = (
                            self._latest_record_error(record)
                            or f"PCB turn stopped in {project_status}"
                        )
                        store.update(
                            turn_id,
                            TurnStatus.FAILED,
                            stop_reason="bounded automatic recovery was exhausted",
                            error=error,
                        )
                        raise PCBDraftError(error)
                    store.update(
                        turn_id,
                        TurnStatus.COMPLETED,
                        stop_reason="the PCB agent reached a stable local state",
                    )
                    return view
                call = call_from_view(
                    proposal.name,
                    project_id,
                    source=proposal.source,
                    arguments=proposal.arguments,
                    view=view,
                )
                record = self._persist_proposal(
                    store,
                    record,
                    call,
                    view,
                    tool_call_id=proposal.tool_call_id,
                )
                active = record.tool_runs[-1]

            spec = self.registry.resolve(active.tool_name)
            if active.effect != spec.effect or active.risk != spec.risk:
                raise ValidationError(
                    "durable tool authority metadata does not match the registry"
                )
            if active.status is ToolRunStatus.PROPOSED:
                call = self._tool_call(active)
                verdict = self.permissions.decide(
                    call,
                    spec,
                    trusted_user_action=self._trusted_user_action(
                        record, active, spec.external_name
                    ),
                )
                if verdict.action == "ask":
                    store.request_approval(turn_id, active.tool_call_id)
                    return view
                if verdict.action == "deny":
                    store.cancel(turn_id, verdict.reason, decision_source="policy")
                    return view
                record = store.update_tool_run(
                    turn_id, active.tool_call_id, ToolRunStatus.RUNNING
                )
                active = record.tool_run(active.tool_call_id)

            if active.dispatch_started_at is None:
                record = store.begin_dispatch(turn_id, active.tool_call_id)
                active = record.tool_run(active.tool_call_id)

            try:
                result = self.executor.execute(
                    self._tool_call(active), timeout=timeout, observed_view=view
                )
            except Exception as exc:  # noqa: BLE001 - persist dispatch ambiguity
                public_error = sanitize_user_text(str(exc))[:4096] or type(exc).__name__
                try:
                    observed = self.service.open_project(project_id)
                    observed_status, observed_revision = project_status_and_revision(
                        observed
                    )
                except Exception:  # noqa: BLE001 - preserve the primary tool failure
                    observed = view
                    observed_status = active.before_status
                    observed_revision = active.before_revision
                if self._effect_is_reconciled(active, observed):
                    store.update_tool_run(
                        turn_id,
                        active.tool_call_id,
                        ToolRunStatus.COMPLETED,
                        after_status=observed_status,
                        after_revision=observed_revision,
                        result=self._reconciled_receipt(active, observed),
                    )
                    view = observed
                    continue
                reason = (
                    "PCB tool reported an error after durable dispatch without an "
                    f"exact effect receipt; outcome is unknown and was not replayed: {public_error}"
                )
                self._fail_ambiguous_active(
                    store,
                    store.load(turn_id),
                    active,
                    observed_status,
                    observed_revision,
                    reason,
                )
            view = result.view
            store.update_tool_run(
                turn_id,
                active.tool_call_id,
                ToolRunStatus.COMPLETED,
                after_status=result.after_status,
                after_revision=result.after_revision,
                result=self._result_receipt(result),
            )

        error = f"agent turn exceeded {MAX_TOOL_CALLS_PER_TURN} PCB tool calls"
        store.update(
            turn_id,
            TurnStatus.FAILED,
            stop_reason="bounded tool-call limit reached",
            error=error,
        )
        raise PCBDraftError(error)

    def resolve_pending_approval(
        self,
        project_id: str,
        *,
        turn_id: str,
        checkpoint_id: str,
        tool_call_id: str,
        tool_name: str,
        effect: str,
        risk: str,
        args_hash: str,
        baseline_revision: int,
        approve: bool,
        decision_source: str = "user",
    ) -> TurnRecord:
        """Resolve the exact checkpoint shown to the user, never a newer one."""

        store = self.store(project_id)
        record = store.load(turn_id)
        self._assert_turn_identity(record, project_id)
        resolved = store.resolve_approval(
            turn_id,
            tool_call_id,
            ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED,
            tool_name=tool_name,
            effect=effect,
            risk=risk,
            args_hash=args_hash,
            baseline_revision=baseline_revision,
            current_revision_reader=lambda: project_status_and_revision(
                self.service.open_project(project_id)
            )[1],
            checkpoint_id=checkpoint_id,
            decision_source=decision_source,
            reason=("approved once" if approve else "rejected by user"),
            cancel_on_deny=True,
        )
        return resolved

    @staticmethod
    def approval_payload(record: TurnRecord) -> dict[str, Any] | None:
        """Return the UI-safe, fully bound payload for one pending checkpoint."""

        checkpoint = record.pending_approval
        if checkpoint is None:
            return None
        tool = record.tool_run(checkpoint.tool_call_id)
        if (
            checkpoint.tool_name != tool.tool_name
            or checkpoint.effect != tool.effect
            or checkpoint.risk != tool.risk
        ):
            raise ValidationError("pending approval authority binding is inconsistent")
        return checkpoint.to_dict()

    def latest_turn(self, project_id: str) -> TurnRecord | None:
        return self.store(project_id).latest(thread_id=DEFAULT_THREAD_ID)

    @staticmethod
    def _assert_turn_identity(record: TurnRecord, project_id: str) -> None:
        if record.project_id != project_id:
            raise ValidationError("agent turn belongs to a different project")

    def _deliver_reply(
        self,
        store: AgentTurnStore,
        record: TurnRecord,
        reply: str,
    ) -> TurnRecord:
        """Persist one model reply exactly once, durably and to the transcript.

        The conversation message is keyed by ``(turn_id, index)`` so a worker
        that crashed after delivering it appends neither a duplicate transcript
        message nor a duplicate durable turn entry on resume.
        """

        index = len(record.assistant_texts)
        if index and record.assistant_texts[-1] == reply:
            return record
        self.service.reply_message(
            record.project_id,
            reply,
            turn_id=record.turn_id,
            index=index,
        )
        return store.append_assistant_text(record.turn_id, reply)

    def _persist_proposal(
        self,
        store: AgentTurnStore,
        record: TurnRecord,
        call: ToolCall,
        view: Mapping[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> TurnRecord:
        before_status, before_revision = project_status_and_revision(view)
        spec = self.registry.resolve(call.name)
        call_id = tool_call_id or f"call-{new_run_id()}"
        if any(tool.tool_call_id == call_id for tool in record.tool_runs):
            raise ValidationError("producer returned a duplicate tool-call id")
        tool = ToolRunRecord.proposed(
            project_id=record.project_id,
            thread_id=record.thread_id,
            turn_id=record.turn_id,
            tool_call_id=call_id,
            tool_name=spec.external_name,
            source=call.source,
            effect=spec.effect,
            risk=spec.risk,
            arguments=call.arguments,
            args_hash=call.arguments_hash,
            baseline_revision=call.baseline_revision,
            before_status=before_status,
            before_revision=before_revision,
        )
        return store.append_tool_run(record.turn_id, tool)

    @staticmethod
    def _tool_call(tool: ToolRunRecord) -> ToolCall:
        call = ToolCall(
            name=tool.tool_name,
            project_id=tool.project_id,
            source=tool.source,  # type: ignore[arg-type]
            arguments=tool.arguments,
            baseline_revision=tool.baseline_revision,
        )
        if call.arguments_hash != tool.args_hash:
            raise ValidationError("durable tool arguments no longer match their hash")
        return call

    @staticmethod
    def _active_tool(record: TurnRecord) -> ToolRunRecord | None:
        return next(
            (
                tool
                for tool in reversed(record.tool_runs)
                if tool.status in {ToolRunStatus.PROPOSED, ToolRunStatus.RUNNING}
            ),
            None,
        )

    def _recover_active_call(
        self,
        store: AgentTurnStore,
        record: TurnRecord,
        view: dict[str, Any],
    ) -> tuple[TurnRecord, dict[str, Any]]:
        active = self._active_tool(record)
        if active is None or active.status is ToolRunStatus.PROPOSED:
            return record, view
        status, revision = project_status_and_revision(view)
        if active.dispatch_started_at is None and revision == active.baseline_revision:
            return record, view
        if revision < active.baseline_revision:
            return self._fail_ambiguous_active(
                store,
                record,
                active,
                status,
                revision,
                "project revision precedes its durable tool call",
            )
        if revision > active.baseline_revision and self._effect_is_reconciled(
            active, view
        ):
            receipt = self._reconciled_receipt(active, view)
            record = store.update_tool_run(
                record.turn_id,
                active.tool_call_id,
                ToolRunStatus.COMPLETED,
                after_status=status,
                after_revision=revision,
                result=receipt,
            )
            return record, view
        reason = (
            "tool dispatch was interrupted without a conclusive project receipt; "
            "the effect was not replayed"
            if revision == active.baseline_revision
            else "project state changed after tool dispatch but did not prove that "
            "exact effect; the effect was not replayed"
        )
        return self._fail_ambiguous_active(
            store, record, active, status, revision, reason
        )

    @staticmethod
    def _fail_ambiguous_active(
        store: AgentTurnStore,
        record: TurnRecord,
        active: ToolRunRecord,
        status: str,
        revision: int,
        reason: str,
    ) -> tuple[TurnRecord, dict[str, Any]]:
        observed: dict[str, Any] = {}
        if revision >= active.baseline_revision:
            observed = {"after_status": status, "after_revision": revision}
        store.update_tool_run(
            record.turn_id,
            active.tool_call_id,
            ToolRunStatus.INTERRUPTED,
            error=reason,
            **observed,
        )
        store.update(
            record.turn_id,
            TurnStatus.FAILED,
            stop_reason="an interrupted PCB effect requires inspection and a new turn",
            error=reason,
        )
        raise PCBDraftError(reason)

    @staticmethod
    def _effect_is_reconciled(active: ToolRunRecord, view: Mapping[str, Any]) -> bool:
        """Accept only a durable receipt bound to this exact call identity."""

        status, revision = project_status_and_revision(view)
        if revision <= active.baseline_revision:
            return False
        receipts = view.get("tool_receipts")
        receipt = (
            receipts.get(active.tool_call_id) if isinstance(receipts, Mapping) else None
        )
        return bool(
            isinstance(receipt, Mapping)
            and receipt.get("tool_call_id") == active.tool_call_id
            and receipt.get("tool_name") == active.tool_name
            and receipt.get("args_hash") == active.args_hash
            and receipt.get("baseline_revision") == active.baseline_revision
            and receipt.get("after_status") == status
            and receipt.get("after_revision") == revision
        )

    @staticmethod
    def _has_non_replayable_interruption(record: TurnRecord) -> bool:
        return any(
            tool.status is ToolRunStatus.INTERRUPTED
            and tool.dispatch_started_at is not None
            for tool in record.tool_runs
        )

    @staticmethod
    def _has_unfinished_model_direct_intent(record: TurnRecord) -> bool:
        return any(
            tool.source == "model"
            and tool.tool_name != "pcb_plan_request"
            and tool.status is not ToolRunStatus.COMPLETED
            for tool in record.tool_runs
        )

    @staticmethod
    def _reconciled_receipt(
        active: ToolRunRecord, view: Mapping[str, Any]
    ) -> dict[str, Any]:
        status, revision = project_status_and_revision(view)
        design = view.get("design")
        content_hash = (
            design.get("content_hash") if isinstance(design, Mapping) else None
        )
        return {
            "tool": active.tool_name,
            "before_status": active.before_status,
            "before_revision": active.before_revision,
            "after_status": status,
            "after_revision": revision,
            "design_content_hash": content_hash
            if isinstance(content_hash, str)
            else None,
            "reconciled_after_interruption": True,
        }

    @staticmethod
    def _trusted_user_action(
        record: TurnRecord, active: ToolRunRecord, external_name: str
    ) -> bool:
        """Derive explicit authority only from the durable slash-command entry."""

        return (
            active.source == "user"
            and record.user_message == f"/{external_name}"
            and not active.arguments
        )

    @staticmethod
    def _can_recover_automatically(record: TurnRecord, view: Mapping[str, Any]) -> bool:
        status, _revision = project_status_and_revision(view)
        repairs_used = sum(
            tool.tool_name == "pcb_repair_candidate" for tool in record.tool_runs
        )
        return status in {"generation_failed", "repair_failed"} and (
            repairs_used < MAX_AUTOMATIC_REPAIRS
        )

    @staticmethod
    def _latest_record_error(record: TurnRecord) -> str | None:
        return next(
            (
                tool.error
                for tool in reversed(record.tool_runs)
                if tool.error is not None
            ),
            None,
        )

    @staticmethod
    def _result_receipt(result: ToolResult) -> dict[str, Any]:
        design = result.view.get("design")
        content_hash = (
            design.get("content_hash") if isinstance(design, Mapping) else None
        )
        return {
            "tool": result.spec.external_name,
            "before_status": result.before_status,
            "before_revision": result.before_revision,
            "after_status": result.after_status,
            "after_revision": result.after_revision,
            "design_content_hash": content_hash
            if isinstance(content_hash, str)
            else None,
        }
