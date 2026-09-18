"""Thin Web projection over canonical application jobs and agent turns.

This adapter deliberately persists nothing. JobRunner, AgentTurnStore, and
ApplicationService remain the only owners of execution, conversation, revision,
permission, transaction, and completion state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pcbdraft.agent.turns import TurnRecord
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.services.assistant_preview import AssistantPreviewSink
from pcbdraft.services.gui_session_contract import (
    GuiActionResponse,
    GuiSessionMessage,
    GuiSessionResponse,
    action_response,
    active_turn,
    session_message,
    session_response,
    visible_job,
)
from pcbdraft.services.jobs import JobRunner
from pcbdraft.services.project_history import legacy_project_messages

MAX_PROMPT_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_MESSAGES = 60
MAX_VISIBLE_JOBS = 20
_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancel_requested"})


def _validated_message(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValidationError("GUI message must be non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("GUI message must be valid UTF-8 text") from exc
    if len(encoded) > MAX_PROMPT_BYTES:
        raise ValidationError("GUI message exceeds the size limit")
    return sanitize_user_text(value.strip())


def _bounded_text(value: str, limit: int = MAX_RESPONSE_BYTES) -> str:
    text = sanitize_user_text(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    suffix = "\n[text truncated]"
    room = max(0, limit - len(suffix.encode("utf-8")))
    return encoded[:room].decode("utf-8", errors="ignore") + suffix


class GuiSessionManager:
    """Translate Web actions to canonical jobs and build bounded view models."""

    def __init__(
        self,
        service: Any,
        cache_root: str | Path | None = None,
        *,
        jobs: Any | None = None,
        worker_command: object | None = None,
        stop_grace_seconds: float | None = None,
    ) -> None:
        del cache_root, stop_grace_seconds
        if worker_command is not None:
            raise ValidationError(
                "GUI subprocess workers were removed; use the canonical JobRunner"
            )
        self.service = service
        if jobs is None:
            from pcbdraft.agent.conversations import ConversationOrchestrator

            jobs = JobRunner(service, orchestrator=ConversationOrchestrator(service))
        self.jobs = jobs

    def set_assistant_preview_sink(self, sink: AssistantPreviewSink | None) -> bool:
        """Attach a transient preview sink when the canonical agent supports it."""

        setter = getattr(self.jobs.agent, "set_assistant_preview_sink", None)
        if not callable(setter):
            return False
        setter(sink)
        return True

    def start(self, project_id: str, text: object) -> GuiActionResponse:
        """Admit one canonical permission-bound agent job."""

        self.service.project_root(project_id)
        job = self.jobs.submit(
            project_id,
            "agent_message",
            {"text": _validated_message(text), "timeout": 420.0},
        )
        args = job.get("args")
        turn_id = args.get("turn_id") if isinstance(args, dict) else None
        return action_response(
            project_id=project_id,
            job_id=job["id"],
            turn_id=turn_id,
            status=job["status"],
        )

    def stop(self, project_id: str) -> GuiActionResponse:
        """Request cancellation through the canonical job/turn boundary."""

        active = next(
            (
                job
                for job in self.jobs.list(project_id)
                if job.get("status") in _ACTIVE_JOB_STATES
            ),
            None,
        )
        if active is None:
            return action_response(
                project_id=project_id,
                job_id=None,
                status="idle",
                include_turn_id=False,
            )
        cancelled = self.jobs.cancel(project_id, str(active["id"]))
        args = cancelled.get("args")
        return action_response(
            project_id=project_id,
            job_id=cancelled["id"],
            turn_id=args.get("turn_id") if isinstance(args, dict) else None,
            status=cancelled["status"],
        )

    def session(self, project_id: str) -> GuiSessionResponse:
        """Build reconnect state from canonical turns or verified legacy history."""

        view = self.service.open_project(project_id)
        jobs = self.jobs.list(project_id)
        turns = self.jobs.agent.store(project_id).list(limit=MAX_MESSAGES // 2)
        active = next(
            (job for job in jobs if job.get("status") in _ACTIVE_JOB_STATES), None
        )
        messages = self._messages(turns)
        legacy_session_id = None
        legacy = legacy_project_messages(self.service, project_id)
        if legacy is not None:
            legacy_session_id, legacy_history = legacy
            messages = (
                self._legacy_messages(legacy_history, legacy_session_id) + messages
            )[-MAX_MESSAGES:]
        active_args = active.get("args") if isinstance(active, dict) else None
        active_turn_id = (
            active_args.get("turn_id") if isinstance(active_args, dict) else None
        )
        pending = None
        if isinstance(active_turn_id, str):
            try:
                pending = self.jobs.agent.approval_payload(
                    self.jobs.agent.store(project_id).load(active_turn_id)
                )
            except PCBDraftError:
                pending = None
        state = view.get("state") if isinstance(view, dict) else None
        raw_product = view.get("product_status") if isinstance(view, dict) else None
        candidate_gate = (
            raw_product.get("candidate_gate")
            if isinstance(raw_product, dict)
            else None
        )
        task_coverage = (
            raw_product.get("task_coverage")
            if isinstance(raw_product, dict)
            else None
        )
        latest_turn = turns[0] if turns else None
        conversation_status = (
            str(active["status"])
            if active is not None
            else latest_turn.status.value
            if latest_turn is not None
            else "not_started"
        )
        return session_response(
            project_id=project_id,
            status=active["status"] if active is not None else "idle",
            active=(
                active_turn(active, turn_id=active_turn_id)
                if active is not None
                else None
            ),
            pending_approval=pending,
            messages=messages,
            legacy_session_id=legacy_session_id,
            jobs=[visible_job(job) for job in jobs[:MAX_VISIBLE_JOBS]],
            product_status={
                "conversation": conversation_status,
                "candidate_gate": dict(candidate_gate)
                if isinstance(candidate_gate, dict)
                else {"outcome": "incomplete", "passed": False},
                "task_coverage": dict(task_coverage)
                if isinstance(task_coverage, dict)
                else {"outcome": "incomplete", "complete": False},
            },
            canonical_revision=(
                state.get("revision") if isinstance(state, dict) else None
            ),
            design_revision=(
                state.get("design_revision") if isinstance(state, dict) else None
            ),
            content_hash=self._content_hash(view),
        )

    def events(self, project_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Compatibility view: lifecycle events are owned by ApplicationService."""

        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValidationError("GUI event sequence is invalid")
        self.service.project_root(project_id)
        return []

    def drain(self, project_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.events(project_id, after=after)

    def shutdown(self) -> list[dict[str, Any]]:
        self.jobs.shutdown()
        return []

    @staticmethod
    def _messages(turns: list[TurnRecord]) -> list[GuiSessionMessage]:
        messages: list[GuiSessionMessage] = []
        for turn in reversed(turns):
            messages.append(
                session_message(
                    message_id=f"{turn.turn_id}-user",
                    turn_id=turn.turn_id,
                    role="user",
                    text=_bounded_text(turn.user_message),
                    status=turn.status.value,
                    created_at=turn.created_at,
                )
            )
            for index, reply in enumerate(turn.assistant_texts):
                messages.append(
                    session_message(
                        message_id=f"{turn.turn_id}-assistant-{index}",
                        turn_id=turn.turn_id,
                        role="assistant",
                        text=_bounded_text(reply),
                        status=turn.status.value,
                        created_at=turn.updated_at,
                    )
                )
        return messages[-MAX_MESSAGES:]

    @staticmethod
    def _legacy_messages(
        history: list[dict[str, Any]], session_id: str
    ) -> list[GuiSessionMessage]:
        """Project a verified pre-GUI transcript into the public message shape."""

        messages: list[GuiSessionMessage] = []
        for index, message in enumerate(history):
            role = message.get("role")
            text = GuiSessionManager._legacy_text(message.get("content"))
            if role not in {"user", "assistant"} or not text:
                continue
            messages.append(
                session_message(
                    message_id=f"legacy-{session_id}-{index}",
                    turn_id=session_id,
                    role=role,
                    text=_bounded_text(text),
                    status="completed",
                    created_at="",
                )
            )
        return messages[-MAX_MESSAGES:]

    @staticmethod
    def _legacy_text(content: object) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        return ""

    @staticmethod
    def _content_hash(view: dict[str, Any]) -> str | None:
        design = view.get("design")
        value = design.get("content_hash") if isinstance(design, dict) else None
        return value if isinstance(value, str) else None
