"""Persistent background runtime for PCBDraft interactive agent turns.

The runtime is intentionally independent of any UI toolkit.  It turns synchronous
application operations into durable jobs and exposes only incremental events,
which lets terminal, web, and future desktop surfaces share one PCB-agent core.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pcbdraft.agent.capabilities import agent_capability
from pcbdraft.agent.events import AgentActivity, AgentUpdate
from pcbdraft.agent.orchestrator import AgentOrchestrator
from pcbdraft.agent.permissions import PermissionBroker, PermissionMode
from pcbdraft.agent.turns import TurnRecord
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.jobs import JobRunner

_ACTIVE_JOB_STATES = {"queued", "running", "cancel_requested"}
_RETRYABLE_JOB_STATES = {"failed", "interrupted", "cancelled"}


@dataclass
class _ActiveTurn:
    project_id: str
    job_id: str
    action: str
    event_cursor: int
    turn_id: str | None = None
    tool_states: dict[str, str] = field(default_factory=dict)


class AgentRuntime:
    """Coordinate one interactive session over the durable application jobs."""

    def __init__(
        self,
        service: ApplicationService,
        *,
        runner: JobRunner | None = None,
        workers: int = 1,
        permission_mode: PermissionMode = "workspace",
    ) -> None:
        self.service = service
        owned_agent = AgentOrchestrator(
            service, permissions=PermissionBroker(permission_mode)
        )
        self.runner = runner or JobRunner(
            service, workers=workers, orchestrator=owned_agent
        )
        runner_agent = getattr(self.runner, "agent", None)
        self.agent = (
            runner_agent if isinstance(runner_agent, AgentOrchestrator) else owned_agent
        )
        self._owns_runner = runner is None
        self._active: _ActiveTurn | None = None

    @property
    def active(self) -> bool:
        """Whether this terminal session owns an unfinished turn."""

        return self._active is not None

    @property
    def active_project_id(self) -> str | None:
        return self._active.project_id if self._active is not None else None

    def start_project(
        self,
        name: str,
        message: str,
        *,
        timeout: float,
    ) -> AgentUpdate:
        """Create a durable draft, then enqueue its first autonomous turn."""

        self._assert_idle()
        draft = self.service.create_draft(name)
        project = draft.get("project")
        project_id = project.get("id") if isinstance(project, dict) else None
        if not isinstance(project_id, str):
            raise ValidationError("application did not return a project id")
        return self.submit_message(project_id, message, timeout=timeout)

    def submit_message(
        self,
        project_id: str,
        message: str,
        *,
        timeout: float,
    ) -> AgentUpdate:
        """Enqueue a complete request → plan → generate/check agent turn."""

        return self._submit(
            project_id,
            "agent_message",
            {"text": message, "timeout": timeout},
        )

    def submit_action(
        self,
        project_id: str,
        action: str,
        *,
        timeout: float,
    ) -> AgentUpdate:
        """Enqueue one explicit PCB project action."""

        capability = agent_capability(action)
        if capability is None:
            raise ValidationError(f"unsupported agent action: {action}")
        return self._submit(
            project_id,
            "agent_tool",
            {"tool": capability.tool_name, "timeout": timeout},
        )

    def poll(self) -> AgentUpdate | None:
        """Return newly persisted activity and settle a finished turn."""

        active = self._active
        if active is None:
            return None
        job = self.runner.get(active.project_id, active.job_id)
        raw_events = self.service.events(active.project_id, after=active.event_cursor)
        project_activities = tuple(
            AgentActivity.from_project_event(event)
            for event in raw_events
            if isinstance(event, dict)
        )
        if project_activities:
            active.event_cursor = max(
                active.event_cursor,
                *(activity.sequence for activity in project_activities),
            )
        turn = self._load_turn(active.project_id, active.turn_id)
        tool_activities: tuple[AgentActivity, ...] = ()
        if turn is not None:
            changed: list[AgentActivity] = []
            for tool in turn.tool_runs:
                status = tool.status.value
                if active.tool_states.get(tool.tool_call_id) == status:
                    continue
                active.tool_states[tool.tool_call_id] = status
                changed.append(AgentActivity.from_tool_run(tool))
            tool_activities = tuple(changed)
        activities = (*project_activities, *tool_activities)
        status = str(job.get("status", "failed"))
        terminal = status not in _ACTIVE_JOB_STATES
        # Surface each completed PCB tool boundary as part of one live turn.
        # Clients can therefore update the same conversation/status view while
        # the autonomous job continues, instead of appearing to jump from the
        # initial draft straight to the final result.
        view = (
            self._snapshot(active.project_id, terminal=terminal)
            if terminal or activities
            else None
        )
        error = job.get("error")
        snapshot_pending = terminal and view is None
        update = AgentUpdate(
            project_id=active.project_id,
            job_id=active.job_id,
            action=active.action,
            status="running" if snapshot_pending else status,
            activities=activities,
            view=view,
            error=error if isinstance(error, str) and error else None,
            turn_id=turn.turn_id if turn is not None else active.turn_id,
            turn_status=(
                turn.status.value if turn is not None else self._job_turn_status(job)
            ),
            pending_approval=(
                self.agent.approval_payload(turn)
                if turn is not None
                else self._job_pending_approval(job)
            ),
        )
        if terminal and not snapshot_pending:
            self._active = None
        return update

    def _snapshot(self, project_id: str, *, terminal: bool) -> dict[str, Any] | None:
        """Read only committed project state while a worker may still be writing."""

        snapshot = getattr(self.service, "try_open_project_snapshot", None)
        if callable(snapshot):
            return snapshot(project_id, timeout=1.0 if terminal else 0.0)
        # Small test doubles and older service adapters do not yet expose the
        # lock-aware reader.  Their in-memory views have no partial-write window.
        return self.service.open_project(project_id)

    def cancel(self) -> AgentUpdate:
        """Request cooperative cancellation at the next safe tool boundary."""

        active = self._active
        if active is None:
            raise ValidationError("there is no active agent turn")
        job = self.runner.cancel(active.project_id, active.job_id)
        return AgentUpdate(
            project_id=active.project_id,
            job_id=active.job_id,
            action=active.action,
            status=str(job["status"]),
        )

    def restore_project(self, project_id: str, *, limit: int = 80) -> AgentUpdate:
        """Restore recent durable activity without replaying any side effect."""

        self._assert_idle()
        if not 1 <= limit <= 200:
            raise ValidationError("restored activity limit must be from 1 to 200")
        view = self.service.open_project(project_id)
        state = view.get("state")
        sequence = state.get("event_sequence", 0) if isinstance(state, dict) else 0
        if not isinstance(sequence, int) or sequence < 0:
            sequence = 0
        events = self.service.events(project_id, after=max(0, sequence - limit))
        activities = tuple(
            AgentActivity.from_project_event(event)
            for event in events[-limit:]
            if isinstance(event, dict)
        )
        jobs = self.runner.list(project_id)
        latest = jobs[0] if jobs else None
        error = latest.get("error") if isinstance(latest, dict) else None
        try:
            turns = self.agent.store(project_id).list(thread_id="main", limit=20)
        except AttributeError:
            turns = []
        except PCBDraftError as exc:
            if "resource is locked by another runtime process" not in str(exc):
                raise
            turns = []
        turn = turns[0] if turns else None
        if turns:
            retained_tools = [
                tool
                for historical_turn in reversed(turns)
                for tool in historical_turn.tool_runs
            ][-60:]
            activities = (
                *activities,
                *tuple(AgentActivity.from_tool_run(tool) for tool in retained_tools),
            )
        return AgentUpdate(
            project_id=project_id,
            job_id=str(latest.get("id", "session-history"))
            if isinstance(latest, dict)
            else "session-history",
            action=str(latest.get("action", "restore"))
            if isinstance(latest, dict)
            else "restore",
            status=str(latest.get("status", "restored"))
            if isinstance(latest, dict)
            else "restored",
            activities=activities,
            view=view,
            error=error if isinstance(error, str) and error else None,
            turn_id=turn.turn_id if turn is not None else None,
            turn_status=turn.status.value if turn is not None else None,
            pending_approval=(
                self.agent.approval_payload(turn) if turn is not None else None
            ),
        )

    def pending_approval(self, project_id: str) -> dict[str, Any] | None:
        """Return the exact crash-durable approval currently blocking a turn."""

        record = self.agent.store(project_id).waiting_approval(thread_id="main")
        if record is None or record.pending_approval is None:
            return None
        return self.agent.approval_payload(record)

    def resolve_pending(
        self,
        project_id: str,
        *,
        checkpoint: Mapping[str, Any],
        approve: bool,
        timeout: float,
    ) -> AgentUpdate:
        """Approve once and resume the same turn, or reject it without dispatch."""

        self._assert_idle()
        fields = self._approval_binding(checkpoint)
        view = self.service.open_project(project_id)
        state = view.get("state")
        cursor = state.get("event_sequence", 0) if isinstance(state, dict) else 0
        if not isinstance(cursor, int) or cursor < 0:
            cursor = 0
        job, record = self.runner.resolve_approval(
            project_id,
            **fields,
            approve=approve,
            timeout=timeout,
            decision_source="tui",
        )
        if approve:
            if job is None:
                raise ValidationError("approval continuation job was not persisted")
            return self._activate_job(job, view=view, event_cursor=cursor)
        return AgentUpdate(
            project_id=project_id,
            job_id=f"approval:{record.turn_id}",
            action="agent_approval",
            status="cancelled",
            view=self.service.open_project(project_id),
            turn_id=record.turn_id,
            turn_status=record.status.value,
        )

    @staticmethod
    def _approval_binding(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(checkpoint, Mapping):
            raise ValidationError("approval checkpoint must be an object")
        required = {
            "turn_id",
            "checkpoint_id",
            "tool_call_id",
            "tool_name",
            "effect",
            "risk",
            "args_hash",
            "baseline_revision",
        }
        missing = required - set(checkpoint)
        if missing:
            raise ValidationError(
                "approval checkpoint is missing its exact call binding"
            )
        result = {name: checkpoint[name] for name in required}
        if not all(
            isinstance(result[name], str) for name in required - {"baseline_revision"}
        ):
            raise ValidationError("approval checkpoint call binding is malformed")
        revision = result["baseline_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValidationError("approval checkpoint revision is malformed")
        return result

    def retry_last(self, project_id: str) -> AgentUpdate:
        """Explicitly retry the newest failed/interrupted/cancelled durable job."""

        self._assert_idle()
        previous = next(
            (
                job
                for job in self.runner.list(project_id)
                if job.get("status") in _RETRYABLE_JOB_STATES
            ),
            None,
        )
        if previous is None:
            raise ValidationError("project has no failed or interrupted job to retry")
        view = self.service.open_project(project_id)
        state = view.get("state")
        cursor = state.get("event_sequence", 0) if isinstance(state, dict) else 0
        if not isinstance(cursor, int) or cursor < 0:
            cursor = 0
        job = self.runner.retry(project_id, str(previous["id"]))
        return self._activate_job(job, view=view, event_cursor=cursor)

    def shutdown(self) -> None:
        """Release worker resources owned by this runtime."""

        if self._owns_runner:
            self.runner.shutdown()

    def _submit(
        self,
        project_id: str,
        action: str,
        args: dict[str, Any],
    ) -> AgentUpdate:
        self._assert_idle()
        view = self.service.open_project(project_id)
        state = view.get("state")
        cursor = state.get("event_sequence", 0) if isinstance(state, dict) else 0
        if not isinstance(cursor, int) or cursor < 0:
            cursor = 0
        job = self.runner.submit(project_id, action, args)
        return self._activate_job(job, view=view, event_cursor=cursor)

    def _activate_job(
        self,
        job: dict[str, Any],
        *,
        view: dict[str, Any],
        event_cursor: int,
    ) -> AgentUpdate:
        job_id = job.get("id")
        project_id = job.get("project_id")
        action = job.get("action")
        if not isinstance(job_id, str):
            raise ValidationError("job runner did not return a job id")
        if not isinstance(project_id, str) or not isinstance(action, str):
            raise ValidationError("job runner returned an invalid job identity")
        self._active = _ActiveTurn(
            project_id=project_id,
            job_id=job_id,
            action=action,
            event_cursor=event_cursor,
            turn_id=self._job_turn_id(job),
        )
        return AgentUpdate(
            project_id=project_id,
            job_id=job_id,
            action=action,
            status=str(job.get("status", "queued")),
            view=view,
            turn_id=self._active.turn_id,
        )

    def _load_turn(self, project_id: str, turn_id: str | None) -> TurnRecord | None:
        if turn_id is None:
            return None
        try:
            try:
                store = self.agent.store(project_id, lock_timeout=0)
            except TypeError:
                # Compatibility for small in-memory orchestrator test doubles.
                store = self.agent.store(project_id)
            return store.load(turn_id)
        except PCBDraftError as exc:
            if "resource is locked by another runtime process" in str(exc):
                return None
            raise

    @staticmethod
    def _job_turn_id(job: dict[str, Any]) -> str | None:
        args = job.get("args")
        turn_id = args.get("turn_id") if isinstance(args, dict) else None
        if isinstance(turn_id, str):
            return turn_id
        result = job.get("result")
        turn_id = result.get("turn_id") if isinstance(result, dict) else None
        return turn_id if isinstance(turn_id, str) else None

    @staticmethod
    def _job_turn_status(job: dict[str, Any]) -> str | None:
        result = job.get("result")
        status = result.get("turn_status") if isinstance(result, dict) else None
        return status if isinstance(status, str) else None

    @staticmethod
    def _job_pending_approval(job: dict[str, Any]) -> dict[str, Any] | None:
        result = job.get("result")
        approval = result.get("pending_approval") if isinstance(result, dict) else None
        return approval if isinstance(approval, dict) else None

    def _assert_idle(self) -> None:
        if self._active is not None:
            raise ValidationError(
                "an agent turn is already running; press Esc or use /stop first"
            )
