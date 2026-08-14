"""Persistent background runtime for PCBDraft interactive agent turns.

The runtime is intentionally independent of curses.  It turns synchronous
application operations into durable jobs and exposes only incremental events,
which lets terminal, web, and future desktop surfaces share one PCB-agent core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent_events import AgentActivity, AgentUpdate
from .application import ApplicationService
from .errors import ValidationError
from .jobs import JobRunner

_ACTIVE_JOB_STATES = {"queued", "running", "cancel_requested"}
_RETRYABLE_JOB_STATES = {"failed", "interrupted", "cancelled"}
_TUI_ACTIONS = {
    "confirm": "confirm",
    "validate": "validate",
    "apply_change": "apply_change",
    "discard_change": "discard_change",
    "undo": "undo",
    "release": "release",
    "previews": "previews",
}


@dataclass
class _ActiveTurn:
    project_id: str
    job_id: str
    action: str
    event_cursor: int


class AgentRuntime:
    """Coordinate one interactive session over the durable application jobs."""

    def __init__(
        self,
        service: ApplicationService,
        *,
        runner: JobRunner | None = None,
        workers: int = 1,
    ) -> None:
        self.service = service
        self.runner = runner or JobRunner(service, workers=workers)
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

        job_action = _TUI_ACTIONS.get(action)
        if job_action is None:
            raise ValidationError(f"unsupported agent action: {action}")
        return self._submit(project_id, job_action, {"timeout": timeout})

    def poll(self) -> AgentUpdate | None:
        """Return newly persisted activity and settle a finished turn."""

        active = self._active
        if active is None:
            return None
        job = self.runner.get(active.project_id, active.job_id)
        raw_events = self.service.events(active.project_id, after=active.event_cursor)
        activities = tuple(
            AgentActivity.from_project_event(event)
            for event in raw_events
            if isinstance(event, dict)
        )
        if activities:
            active.event_cursor = max(
                active.event_cursor, *(activity.sequence for activity in activities)
            )
        status = str(job.get("status", "failed"))
        terminal = status not in _ACTIVE_JOB_STATES
        view = self.service.open_project(active.project_id) if terminal else None
        error = job.get("error")
        update = AgentUpdate(
            project_id=active.project_id,
            job_id=active.job_id,
            action=active.action,
            status=status,
            activities=activities,
            view=view,
            error=error if isinstance(error, str) and error else None,
        )
        if terminal:
            self._active = None
        return update

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
        )

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
        )
        return AgentUpdate(
            project_id=project_id,
            job_id=job_id,
            action=action,
            status=str(job.get("status", "queued")),
            view=view,
        )

    def _assert_idle(self) -> None:
        if self._active is not None:
            raise ValidationError(
                "an agent turn is already running; press Esc or use /stop first"
            )
