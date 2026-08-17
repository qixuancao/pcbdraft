"""Controller and entry point for the PCBDraft terminal application.

The terminal is deliberately a thin client over :class:`ApplicationService`.
It owns no engineering state: project creation, planning, confirmation,
validation, recovery, and release actions remain in the application service.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pcbdraft.agent.events import AgentActivity, AgentUpdate
from pcbdraft.agent.permissions import PermissionMode
from pcbdraft.agent.runtime import AgentRuntime
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.interfaces.tui.commands import command_suggestions
from pcbdraft.interfaces.tui.review import review_sections
from pcbdraft.interfaces.tui.session import TuiSessionStore
from pcbdraft.model.config import connect_provider, select_model
from pcbdraft.model.providers import resolve_provider
from pcbdraft.services.application import ApplicationService

__all__ = ["TuiController", "command_suggestions", "run_tui_command"]

_MAX_INPUT_CHARS = 8_192
_PROVIDER_NAMES = (
    "auto",
    "openai-compatible",
)


def _project_name_from_request(request: str) -> str:
    """Derive a short local project label without another model round trip."""

    text = " ".join(request.split())
    text = re.sub(
        r"^(?:please\s+)?(?:build|create|make|design)\s+(?:(?:an?|the)\s+)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip(" .,:;")
    return text[:64].rstrip() or "Untitled board"


@dataclass
class TuiController:
    """Testable controller for one full-screen PCBDraft session."""

    service: ApplicationService
    runtime: AgentRuntime | None = None
    session_store: TuiSessionStore | None = None
    startup_project_picker: bool = False
    timeout: float = 420.0
    project_id: str | None = None
    view: dict[str, Any] | None = None
    mode: str = "message"
    new_name: str = ""
    notice: str = ""
    error: str = ""
    projects: list[dict[str, Any]] = field(default_factory=list)
    picker_index: int = 0
    activities: list[AgentActivity] = field(default_factory=list)
    active_job_status: str = ""
    logs_expanded: bool = False
    pending_user_text: str = ""
    pending_approval: dict[str, Any] | None = None
    pending_provider_id: str = ""
    _provider_details: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._refresh_provider_diagnostic()
        resumed = False
        recovered: str | None = None
        self.projects = self.service.list_projects()
        if self.project_id is None and self.session_store is not None:
            recovered = self.session_store.load_project_id(
                {
                    str(item["id"])
                    for item in self.projects
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
            )
            if recovered is not None and not self.startup_project_picker:
                self.project_id = recovered
                resumed = True
        if self.project_id:
            self._restore_project(resumed=resumed)
        elif self.startup_project_picker and self.projects:
            self.mode = "project_picker"
            self.picker_index = next(
                (
                    index
                    for index, item in enumerate(self.projects)
                    if item.get("id") == recovered
                ),
                0,
            )
            self.notice = "Choose a project to continue, or create a new project."
        else:
            root = getattr(self.service, "root", None)
            repository_notice = (
                f"PCB project repository: {root}. "
                if isinstance(root, Path)
                and getattr(self.service, "repository_configured_now", False)
                else ""
            )
            self.notice = (
                repository_notice + "Describe a board to begin, or type / for commands."
            )

    @property
    def provider_name(self) -> str:
        provider = getattr(self.service, "provider", None)
        if provider is None:
            return "no provider configured · run /connect"
        provider_id = getattr(provider, "provider_id", None)
        provider_id = provider_id if isinstance(provider_id, str) else "configured"
        details = self._provider_details
        model = details.get("model")
        display_name = details.get("name")
        provider_label = (
            display_name
            if isinstance(display_name, str) and display_name
            else provider_id
        )
        from pcbdraft.model.tool_calls import provider_agent_protocol

        router = (
            "Responses router"
            if provider_agent_protocol(provider) == "native-responses"
            else "local router"
        )
        if isinstance(model, str) and model:
            return f"{provider_label} / {model} · {router}"
        if model is False:
            return f"{provider_id} / no planning model · {router}"
        return f"{provider_id} / model not reported · {router}"

    @property
    def provider_status(self) -> str:
        provider = getattr(self.service, "provider", None)
        if provider is None:
            return "setup required"
        if self._provider_details.get("available") is False:
            return "setup required"
        if not getattr(provider, "supports_planning", True):
            return "requirements only"
        return "ready"

    @property
    def input_label(self) -> str:
        if self.mode == "new_name":
            return "Project name"
        return "Message"

    @property
    def is_busy(self) -> bool:
        """Whether an asynchronous agent turn is still running."""

        return self.runtime is not None and self.runtime.active

    @property
    def activity_label(self) -> str:
        """Short live status suitable for a terminal header."""

        if self.activities:
            latest = self.activities[-1]
            if latest.state == "failed":
                return f"{latest.label} failed"
            if latest.state == "completed":
                return f"{latest.label} complete"
            return latest.label
        if self.is_busy:
            return "Agent turn queued"
        return ""

    def refresh(self) -> None:
        self.error = ""
        if self.project_id is None:
            self.projects = self.service.list_projects()
            self.notice = "No project selected. Describe a board or use /projects."
            return
        try:
            self._set_view(self.service.open_project(self.project_id))
            self.notice = "Project refreshed."
        except PCBDraftError as exc:
            self.error = str(exc)

    def begin_new(self, name: str | None = None) -> None:
        """Open the lightweight project-folder creation flow.

        Project creation is intentionally separate from the first board request:
        it only creates a named local folder and conversation record.  The user
        can then describe the board in the normal composer after the project is
        open.
        """

        self.error = ""
        self.new_name = (name or "").strip()
        self.mode = "new_name"
        self.notice = "Choose a name for the new project folder."

    def show_projects(self) -> None:
        self.error = ""
        self.projects = self.service.list_projects()
        if self.projects:
            current = next(
                (
                    index
                    for index, item in enumerate(self.projects)
                    if item.get("id") == self.project_id
                ),
                0,
            )
            self.picker_index = max(0, min(current, len(self.projects) - 1))
        else:
            self.picker_index = 0
            self.notice = (
                "No projects yet. Close this list and describe a board to begin."
            )
        self.mode = "project_picker"

    def move_picker(self, delta: int) -> None:
        if self.mode != "project_picker" or not self.projects:
            return
        self.picker_index = max(
            0, min(self.picker_index + delta, len(self.projects) - 1)
        )

    def select_picker(self) -> None:
        if self.mode != "project_picker" or not self.projects:
            return
        selected = self.projects[self.picker_index]
        project_id = selected.get("id")
        if not isinstance(project_id, str):
            self.error = "Selected project record is malformed."
            return
        self.open_project(project_id)

    def open_project(self, project_id: str) -> None:
        clean = project_id.strip()
        if not clean:
            self.error = "A project id is required."
            return
        self.error = ""
        try:
            self._set_view(self.service.open_project(clean))
            self.mode = "message"
            self.notice = "Opened project."
        except PCBDraftError as exc:
            self.error = str(exc)

    def cancel_overlay(self) -> None:
        if self.mode == "project_picker":
            self.mode = "message"
            self.notice = ""
        elif self.mode == "review":
            self.mode = "message"
            self.notice = "Review closed."
        elif self.mode in {"provider_picker", "provider_form", "model_picker"}:
            self.mode = "message"
            self.pending_provider_id = ""
            self.notice = "Model setup closed."
        elif self.mode == "new_name":
            self.mode = "message"
            self.new_name = ""
            self.notice = "New project cancelled."

    def submit(self, text: str) -> str:
        """Handle one completed input line and return ``continue`` or ``quit``."""

        if len(text) > _MAX_INPUT_CHARS:
            self.error = f"Input is limited to {_MAX_INPUT_CHARS} characters."
            return "continue"
        clean = text.strip()
        if self.is_busy:
            lowered = clean.casefold()
            if lowered.startswith("/logs") or lowered == "/review":
                return self._command(clean)
            if clean.casefold() == "/stop":
                return self.action("stop")
            if clean.casefold() in {"/quit", "quit", "exit"}:
                return self.action("quit")
            if clean:
                self.error = (
                    "The current agent turn is still running. Press Esc or use /stop "
                    "before sending another request."
                )
            return "continue"
        if clean.casefold() in {"quit", "exit"}:
            return "quit"
        # A pending new-project prompt must not turn control commands into the
        # first board request. This previously made `/quit` create a project
        # literally named "/quit" after `/new`.
        if clean.startswith("/") and self.mode == "new_name":
            return self._command(clean)
        if self.mode == "new_name":
            if not clean:
                self.error = "Project name cannot be empty."
                return "continue"
            return self.create_named_project(clean)
        if not clean:
            return "continue"
        if clean.startswith("/"):
            return self._command(clean)
        if self.project_id is None:
            return self._create_project(clean)
        if self.runtime is not None:
            try:
                self._start_update(
                    self.runtime.submit_message(
                        self.project_id or "", clean, timeout=self.timeout
                    )
                )
                self.pending_user_text = clean
            except PCBDraftError as exc:
                self.error = str(exc)
        else:
            self._call(
                "Planning the requested board",
                lambda: self.service.send_message(
                    self.project_id or "", clean, timeout=self.timeout
                ),
            )
        return "continue"

    def action(self, name: str) -> str:
        """Run a deliberate UI action through the application service."""

        if self.is_busy and name == "quit":
            return self.stop_active()
        if self.is_busy and name not in {"help", "logs", "review", "stop"}:
            self.error = (
                "The current agent turn is still running. Press Esc or use /stop "
                "before starting another action."
            )
            return "continue"
        if name == "new":
            self.begin_new()
            return "continue"
        if name == "project":
            return self._project_repository("")
        if name == "projects":
            self.show_projects()
            return "continue"
        if name == "status":
            self.refresh()
            return "continue"
        if name == "logs":
            self.logs_expanded = not self.logs_expanded
            self.error = ""
            self.notice = (
                "Expanded activity details are visible."
                if self.logs_expanded
                else "Activity details collapsed."
            )
            return "continue"
        if name == "help":
            self.error = ""
            self.notice = "Type / for commands. ↑/↓ select · Tab complete · Enter run · Esc dismiss."
            return "continue"
        if name == "connect":
            self.show_provider_picker()
            return "continue"
        if name in {"models", "model-picker"}:
            self.show_model_picker()
            return "continue"
        if name == "stop":
            return self.stop_active()
        if name == "quit":
            return "quit"
        if not self._has_project():
            return "continue"
        if name == "review":
            if not isinstance(self.view, dict) or not review_sections(self.view):
                self.error = (
                    "This project has no plan, diff, or validation to review yet."
                )
                return "continue"
            self.mode = "review"
            self.error = ""
            self.notice = "Reviewing the retained circuit plan and semantic evidence."
            return "continue"
        if name == "retry":
            if self.runtime is None:
                self.error = "Job retry requires the interactive agent runtime."
                return "continue"
            try:
                self._start_update(self.runtime.retry_last(self.project_id or ""))
            except PCBDraftError as exc:
                self.error = str(exc)
            return "continue"
        if name == "confirm":
            if self.pending_approval is not None and self.runtime is not None:
                try:
                    self._start_update(
                        self.runtime.resolve_pending(
                            self.project_id or "",
                            checkpoint=self.pending_approval,
                            approve=True,
                            timeout=min(self.timeout, 1_800.0),
                        )
                    )
                    self.pending_approval = None
                    self.notice = (
                        "Approved this exact PCB tool call; resuming the turn."
                    )
                except PCBDraftError as exc:
                    self.error = str(exc)
                return "continue"
            status = self._project_status()
            if status == "awaiting_confirmation":
                self._run_project_action(
                    "confirm",
                    "Generating and validating the reviewed KiCad attempt",
                    lambda: self.service.confirm_project(
                        self.project_id or "", timeout=self.timeout
                    ),
                )
            elif status == "change_ready":
                self._run_project_action(
                    "apply_change",
                    "Applying the reviewed semantic change",
                    lambda: self.service.apply_modification(self.project_id or ""),
                )
            else:
                self.error = "Nothing is awaiting confirmation."
            return "continue"
        if name == "validate":
            self._run_project_action(
                "validate",
                "Running L0–L7 validation",
                lambda: self.service.validate_project(
                    self.project_id or "", timeout=min(self.timeout, 3_600.0)
                ),
            )
            return "continue"
        if name == "undo":
            self._run_project_action(
                "undo",
                "Undoing the last semantic change",
                lambda: self.service.undo_last_modification(self.project_id or ""),
            )
            return "continue"
        if name == "discard":
            if self.pending_approval is not None and self.runtime is not None:
                try:
                    update = self.runtime.resolve_pending(
                        self.project_id or "",
                        checkpoint=self.pending_approval,
                        approve=False,
                        timeout=min(self.timeout, 1_800.0),
                    )
                    if update.view is not None:
                        self._set_view(update.view)
                    self.pending_approval = None
                    self.active_job_status = update.status
                    self.error = ""
                    self.notice = (
                        "Rejected the pending PCB tool; no effect was applied."
                    )
                except PCBDraftError as exc:
                    self.error = str(exc)
                return "continue"
            self._run_project_action(
                "discard_change",
                "Discarding the staged semantic change",
                lambda: self.service.discard_modification(self.project_id or ""),
            )
            return "continue"
        if name == "release":
            self._run_project_action(
                "release",
                "Building manufacturing-candidate evidence",
                lambda: self.service.build_release(
                    self.project_id or "", timeout=min(self.timeout, 3_600.0)
                ),
            )
            return "continue"
        self.error = f"Unknown terminal action: {name}"
        return "continue"

    def create_named_project(self, name: str | None = None) -> str:
        """Create a draft project folder without sending an agent request."""

        clean_name = (name or self.new_name).strip()
        if not clean_name:
            self.error = "Project name cannot be empty."
            return "continue"
        self.error = ""
        try:
            draft = self.service.create_draft(clean_name)
            if not isinstance(draft, dict):
                raise ValidationError("application returned an invalid project draft")
            if isinstance(draft.get("conversation"), dict):
                self._set_view(draft)
            else:
                project = draft.get("project", {})
                project_id = project.get("id") if isinstance(project, dict) else None
                if not isinstance(project_id, str):
                    raise ValidationError("application did not return a project id")
                self._set_view(self.service.open_project(project_id))
            self.mode = "message"
            self.new_name = ""
            self.notice = (
                "Project folder created. Describe the board when you are ready."
            )
        except PCBDraftError as exc:
            self.error = str(exc)
        return "continue"

    def _create_project(self, request: str) -> str:
        """Start a project and its first agent turn from one natural-language message."""

        name = self.new_name or _project_name_from_request(request)
        self.error = ""
        if self.runtime is not None:
            try:
                update = self.runtime.start_project(name, request, timeout=self.timeout)
                self._start_update(update)
                self.pending_user_text = request
                self.mode = "message"
                self.new_name = ""
                self.notice = "Agent turn started."
            except PCBDraftError as exc:
                self.error = str(exc)
            return "continue"
        try:
            draft = self.service.create_draft(name)
            project = draft.get("project", {})
            project_id = project.get("id") if isinstance(project, dict) else None
            if not isinstance(project_id, str):
                raise ValidationError("application did not return a project id")
            self.project_id = project_id
            self._set_view(
                self.service.send_message(project_id, request, timeout=self.timeout)
            )
            if self._project_status() == "awaiting_confirmation":
                self._set_view(
                    self.service.confirm_project(project_id, timeout=self.timeout)
                )
            self.notice = "Agent turn completed."
            self.mode = "message"
            self.new_name = ""
        except PCBDraftError as exc:
            self.error = str(exc)
        return "continue"

    def _command(self, text: str) -> str:
        command, _, argument = text[1:].partition(" ")
        command = command.casefold()
        argument = argument.strip()
        if command == "help":
            return self.action("help")
        if command == "new":
            self.begin_new(argument or None)
            return "continue"
        if command == "project":
            return self._project_repository(argument)
        if command == "projects":
            return self.action("projects")
        if command == "open":
            self.open_project(argument)
            return "continue"
        if command == "status":
            return self.action("status")
        if command == "review":
            return self.action("review")
        if command == "logs":
            return self._set_logs(argument)
        if command == "connect":
            self.show_provider_picker()
            return "continue"
        if command in {"models", "model-picker"}:
            self.show_model_picker()
            return "continue"
        if command == "model":
            return self._set_provider(argument)
        if command == "stop":
            return self.action("stop")
        if command == "retry":
            return self.action("retry")
        if command == "confirm":
            return self.action("confirm")
        if command == "validate":
            return self.action("validate")
        if command == "undo":
            return self.action("undo")
        if command == "discard":
            return self.action("discard")
        if command == "release":
            return self.action("release")
        if command == "quit":
            return self.action("quit")
        self.error = "Unknown command. Type / for the command list."
        return "continue"

    def _project_repository(self, directory: str) -> str:
        """Show or switch the persistent repository used for future projects."""

        if self.is_busy:
            self.error = "Stop the active turn before changing the project repository."
            return "continue"
        if not directory:
            root = getattr(self.service, "root", None)
            projects_root = getattr(self.service, "projects_root", None)
            if not isinstance(root, Path) or not isinstance(projects_root, Path):
                self.error = (
                    "This terminal client cannot report its project repository."
                )
                return "continue"
            self.error = ""
            self.notice = (
                f"PCB project repository: {root} (projects: {projects_root}). "
                "Use /project /path/to/repository to change it."
            )
            return "continue"
        set_repository = getattr(self.service, "set_repository", None)
        if not callable(set_repository):
            self.error = "This terminal client cannot change its project repository."
            return "continue"
        try:
            repository = set_repository(directory)
            root = getattr(repository, "root", None)
            if not isinstance(root, Path):
                raise ValidationError("application did not return a project repository")
            self.project_id = None
            self.view = None
            self.projects = self.service.list_projects()
            self.picker_index = 0
            self.activities = []
            self.active_job_status = ""
            self.pending_user_text = ""
            self.pending_approval = None
            self.mode = "message"
            self.new_name = ""
            self.session_store = TuiSessionStore(root)
            self.error = ""
            self.notice = (
                f"PCB project repository changed to {root}. "
                "Existing projects were left in their original repository."
            )
        except PCBDraftError as exc:
            self.error = str(exc)
        return "continue"

    def show_provider_picker(self) -> None:
        if self.is_busy:
            self.error = "Stop the active turn before changing the model connection."
            return
        self.mode = "provider_picker"
        self.error = ""
        self.notice = "Choose a provider to connect."

    def begin_provider_form(self, provider_id: str) -> None:
        self.pending_provider_id = provider_id
        self.mode = "provider_form"

    def save_provider_connection(
        self,
        *,
        provider_id: str,
        api_key: str,
        base_url: str,
        model: str,
        name: str | None = None,
    ) -> None:
        if self.is_busy:
            self.error = "Stop the active turn before changing the model connection."
            return
        try:
            config = connect_provider(
                provider_id,
                api_key=api_key,
                base_url=base_url,
                model=model,
                name=name,
            )
            self.service.provider = resolve_provider("auto")
            self.pending_provider_id = ""
            self.mode = "message"
            self._refresh_provider_diagnostic()
            active = config.active
            self.error = ""
            self.notice = (
                f"Connected {active.name if active else provider_id}; "
                f"active model is {config.active_model}."
            )
        except (PCBDraftError, ValidationError) as exc:
            self.error = str(exc)
            self.mode = "message"

    def show_model_picker(self) -> None:
        if self.is_busy:
            self.error = "Stop the active turn before switching models."
            return
        self.mode = "model_picker"
        self.error = ""
        self.notice = "Choose the model for the next agent turn."

    def choose_model(self, provider_id: str, model: str) -> None:
        if self.is_busy:
            self.error = "Stop the active turn before switching models."
            return
        try:
            config = select_model(provider_id, model)
            self.service.provider = resolve_provider("auto")
            self.mode = "message"
            self._refresh_provider_diagnostic()
            self.error = ""
            self.notice = f"Model switched to {config.active_model}."
        except (PCBDraftError, ValidationError) as exc:
            self.error = str(exc)
            self.mode = "message"

    def _set_provider(self, argument: str) -> str:
        if self.is_busy:
            self.error = "Stop the active turn before switching providers."
            return "continue"
        if not argument:
            self.error = ""
            self.notice = (
                f"Planning provider: {self.provider_name} ({self.provider_status}). "
                "Use /connect to add a provider or /models to choose a model."
            )
            return "continue"
        provider_name = argument.casefold()
        if provider_name not in _PROVIDER_NAMES or " " in provider_name:
            self.error = "Supported providers: auto, or openai-compatible."
            return "continue"
        try:
            provider = resolve_provider(provider_name)
        except PCBDraftError as exc:
            self.error = str(exc)
            return "continue"
        self.service.provider = provider
        self._refresh_provider_diagnostic()
        self.error = ""
        self.notice = (
            "Planning provider switched for this session: "
            f"{self.provider_name} ({self.provider_status})."
        )
        return "continue"

    def _set_logs(self, argument: str) -> str:
        normalized = argument.casefold()
        if normalized not in {"", "on", "off"}:
            self.error = "Use /logs, /logs on, or /logs off."
            return "continue"
        self.logs_expanded = (
            not self.logs_expanded if not normalized else normalized == "on"
        )
        self.error = ""
        self.notice = (
            "Expanded activity details are visible."
            if self.logs_expanded
            else "Activity details collapsed."
        )
        return "continue"

    def poll(self) -> bool:
        """Consume one non-blocking runtime update; return whether UI state changed."""

        if self.runtime is None or not self.runtime.active:
            return False
        try:
            update = self.runtime.poll()
        except PCBDraftError as exc:
            self.error = str(exc)
            return True
        if update is None:
            return False
        self.active_job_status = update.status
        self.pending_approval = update.pending_approval
        if update.activities:
            self.activities.extend(update.activities)
            self.activities = self.activities[-80:]
        if update.view is not None:
            try:
                self._set_view(update.view, refresh_projects=update.terminal)
            except PCBDraftError as exc:
                self.error = str(exc)
        if not update.terminal:
            return bool(update.activities)
        self.pending_user_text = ""
        if update.turn_status == "waiting_approval":
            self.error = ""
            self.notice = (
                "This turn needs approval for one exact PCB write. "
                "Inspect /review, then use /confirm or /discard."
            )
        elif update.status == "failed":
            self.error = update.error or "The agent turn failed."
            self.notice = ""
        elif update.status == "cancelled":
            self.error = ""
            self.notice = (
                "Agent turn stopped. The last completed project state was kept."
            )
        elif update.status == "completed_after_cancel":
            self.error = ""
            self.notice = (
                "The current tool reached a safe boundary after stop was requested; "
                "its completed project state was kept."
            )
        elif update.status == "interrupted":
            self.error = update.error or "The agent turn was interrupted."
            self.notice = ""
        else:
            self.error = ""
            status = self._project_status()
            self.notice = (
                "Board generation and checks complete."
                if status in {"generated", "validated"}
                else "Agent turn complete."
            )
        return True

    def stop_active(self) -> str:
        """Request cancellation without blocking the terminal event loop."""

        if self.runtime is None or not self.runtime.active:
            self.error = "There is no active agent turn."
            return "continue"
        try:
            update = self.runtime.cancel()
        except PCBDraftError as exc:
            self.error = str(exc)
            return "continue"
        self.active_job_status = update.status
        self.error = ""
        self.notice = "Stop requested; waiting for the current PCB tool to settle."
        return "continue"

    def _start_update(self, update: AgentUpdate) -> None:
        previous_project_id = self.project_id
        if update.view is not None:
            self._set_view(update.view)
        else:
            self.project_id = update.project_id
        if previous_project_id != update.project_id:
            self.activities = []
        self.active_job_status = update.status
        self.pending_approval = update.pending_approval
        self.error = ""
        self.notice = (
            "Agent turn started. Unspecified implementation details are automatic."
        )

    def _run_project_action(
        self,
        action: str,
        label: str,
        synchronous_operation: Any,
    ) -> None:
        if self.runtime is None:
            self._call(label, synchronous_operation)
            return
        try:
            self._start_update(
                self.runtime.submit_action(
                    self.project_id or "",
                    action,
                    timeout=min(self.timeout, 1_800.0),
                )
            )
        except PCBDraftError as exc:
            self.error = str(exc)

    def _call(self, label: str, operation: Any) -> None:
        self.error = ""
        self.notice = label + "…"
        try:
            value = operation()
            if not isinstance(value, dict):
                raise ValidationError("application returned an invalid project view")
            self._set_view(value)
            self.notice = label + " complete."
        except PCBDraftError as exc:
            self.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - keep the terminal usable
            self.error = f"Unexpected terminal action failure: {type(exc).__name__}"

    def _set_view(
        self, value: dict[str, Any], *, refresh_projects: bool = True
    ) -> None:
        project = value.get("project")
        if not isinstance(project, dict) or not isinstance(project.get("id"), str):
            raise ValidationError("application view has no valid project identity")
        self.view = value
        self.project_id = project["id"]
        if refresh_projects:
            self.projects = self.service.list_projects()
        if self.session_store is not None:
            try:
                self.session_store.save_project_id(self.project_id)
            except PCBDraftError:
                pass

    def _restore_project(self, *, resumed: bool) -> None:
        try:
            if self.runtime is not None:
                update = self.runtime.restore_project(self.project_id or "")
                if update.view is not None:
                    self._set_view(update.view)
                self.activities = list(update.activities[-80:])
                self.active_job_status = update.status
                self.pending_approval = update.pending_approval
                if update.status in {"failed", "interrupted", "cancelled"}:
                    self.notice = (
                        "Recovered the previous session; its last job did not finish. "
                        "Inspect /logs and use /retry to run it explicitly."
                    )
                else:
                    self.notice = (
                        "Resumed the last terminal project."
                        if resumed
                        else "Opened project with its recent activity."
                    )
            else:
                self._set_view(self.service.open_project(self.project_id or ""))
                self.notice = (
                    "Resumed the last terminal project."
                    if resumed
                    else "Opened project."
                )
        except PCBDraftError as exc:
            self.error = str(exc)

    def _refresh_provider_diagnostic(self) -> None:
        provider = getattr(self.service, "provider", None)
        diagnostic = getattr(provider, "diagnostic", None)
        value: Any = None
        if callable(diagnostic):
            try:
                value = diagnostic()
            except Exception:  # noqa: BLE001 - provider status must not break the TUI
                value = None
        self._provider_details = value if isinstance(value, dict) else {}

    def _has_project(self) -> bool:
        if self.project_id is not None:
            return True
        self.error = "Create or open a project first."
        return False

    def _project_status(self) -> str | None:
        if not isinstance(self.view, dict):
            return None
        project = self.view.get("project")
        return project.get("status") if isinstance(project, dict) else None


def run_tui_command(
    *,
    workspace: str | Path | None,
    provider: str,
    project_id: str | None,
    timeout: float,
    approval_mode: PermissionMode = "workspace",
) -> int:
    """Launch the default full-screen Textual terminal interface."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValidationError(
            "the full-screen terminal interface requires an interactive terminal; "
            "use `chat --json` for scripting"
        )
    if os.environ.get("TERM", "").casefold() in {"", "dumb"}:
        raise ValidationError(
            "the full-screen terminal interface requires cursor-addressing support"
        )

    # Import the rendering layer lazily so controller-only consumers stay cheap and
    # no UI toolkit types leak into the application/runtime boundary.
    from pcbdraft.interfaces.tui.app import PCBDraftApp

    service = ApplicationService(workspace, provider_name=provider)
    runtime = AgentRuntime(service, permission_mode=approval_mode)
    session_store = TuiSessionStore(service.root)
    controller = TuiController(
        service=service,
        runtime=runtime,
        session_store=session_store,
        startup_project_picker=project_id is None,
        timeout=timeout,
        project_id=project_id,
    )
    try:
        result = PCBDraftApp(controller).run()
        return int(result) if isinstance(result, int) else 0
    finally:
        runtime.shutdown()
