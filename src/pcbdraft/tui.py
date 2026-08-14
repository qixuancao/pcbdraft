"""Compact full-screen terminal conversation for PCBDraft.

The terminal is deliberately a thin client over :class:`ApplicationService`.
It owns no engineering state: project creation, planning, confirmation,
validation, recovery, and release actions remain in the application service.
"""

from __future__ import annotations

import curses
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_events import AgentActivity, AgentUpdate
from .agent_runtime import AgentRuntime
from .application import ApplicationService
from .errors import PCBDraftError, ValidationError
from .providers import resolve_provider
from .terminal_text import cell_width as _cell_width
from .terminal_text import split_to_cell_width as _split_to_cell_width
from .terminal_text import tail_to_cell_width as _tail_to_cell_width
from .tui_commands import SlashCommand, command_suggestions
from .tui_review import review_sections
from .tui_session import TuiSessionStore

_MAX_INPUT_CHARS = 8_192
_MIN_HEIGHT = 16
_MIN_WIDTH = 72
_PROVIDER_NAMES = (
    "auto",
    "codex",
    "deepseek-harness",
    "openai-compatible",
    "builtin",
)


def _project_name_from_request(request: str) -> str:
    """Make a short local draft name without asking a provider a second time."""

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
    _provider_details: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._refresh_provider_diagnostic()
        resumed = False
        self.projects = self.service.list_projects()
        if self.project_id is None and self.session_store is not None:
            recovered = self.session_store.load_project_id(
                {
                    str(item["id"])
                    for item in self.projects
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
            )
            if recovered is not None:
                self.project_id = recovered
                resumed = True
        if self.project_id:
            self._restore_project(resumed=resumed)
        else:
            self.notice = "Describe a board to begin, or type / for commands."

    @property
    def provider_name(self) -> str:
        provider = getattr(self.service, "provider", None)
        provider_id = getattr(provider, "provider_id", None)
        provider_id = provider_id if isinstance(provider_id, str) else "configured"
        details = self._provider_details
        model = details.get("model")
        reasoning_effort = details.get("reasoning_effort")
        if isinstance(model, str) and model:
            if isinstance(reasoning_effort, str) and reasoning_effort:
                return f"{provider_id} / {model} · {reasoning_effort}"
            return f"{provider_id} / {model}"
        if model is False:
            return f"{provider_id} / no planning model"
        if provider_id == "builtin":
            return "builtin / offline"
        if provider_id == "codex":
            return "codex / model managed by local CLI"
        return f"{provider_id} / model not reported"

    @property
    def provider_status(self) -> str:
        if self._provider_details.get("available") is False:
            return "setup required"
        provider = getattr(self.service, "provider", None)
        if not getattr(provider, "supports_planning", True):
            return "requirements only"
        return "ready"

    @property
    def input_label(self) -> str:
        if self.mode == "new_name":
            return "Project name"
        if self.mode == "new_request":
            return "Board request"
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
            self.notice = "No project selected. Describe a board or use /new."
            return
        try:
            self._set_view(self.service.open_project(self.project_id))
            self.notice = "Project refreshed."
        except PCBDraftError as exc:
            self.error = str(exc)

    def begin_new(self, name: str | None = None) -> None:
        self.error = ""
        self.new_name = (name or "").strip()
        self.mode = "new_request"
        self.notice = "Describe the board to build."

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
            self.mode = "project_picker"
        else:
            self.notice = "No local projects yet. Describe a board or use /new."

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
        elif self.mode in {"new_name", "new_request"}:
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
        if self.mode == "new_name":
            if not clean:
                self.error = "Project name cannot be empty."
                return "continue"
            self.new_name = clean
            self.mode = "new_request"
            self.error = ""
            self.notice = "Now describe the board, parts, interfaces, and constraints."
            return "continue"
        if self.mode == "new_request":
            if not clean:
                self.error = "A board request cannot be empty."
                return "continue"
            return self._create_project(clean)
        if not clean:
            return "continue"
        if clean.casefold() in {"quit", "exit"}:
            return "quit"
        if clean.startswith("/"):
            return self._command(clean)
        if self.project_id is None:
            self.begin_new(_project_name_from_request(clean))
            return self._create_project(clean)
        if self.runtime is not None:
            try:
                self._start_update(
                    self.runtime.submit_message(
                        self.project_id or "", clean, timeout=self.timeout
                    )
                )
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

    def _create_project(self, request: str) -> str:
        name = self.new_name or _project_name_from_request(request)
        self.error = ""
        if self.runtime is not None:
            try:
                update = self.runtime.start_project(name, request, timeout=self.timeout)
                self._start_update(update)
                self.mode = "message"
                self.new_name = ""
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
                self.notice = "Project created; generation and checks completed."
            else:
                self.notice = "Project created."
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

    def _set_provider(self, argument: str) -> str:
        if self.is_busy:
            self.error = "Stop the active turn before switching providers."
            return "continue"
        if not argument:
            self.error = ""
            self.notice = (
                f"Planning provider: {self.provider_name} ({self.provider_status}). "
                "Use /model [auto|codex|deepseek-harness|openai-compatible|builtin]."
            )
            return "continue"
        provider_name = argument.casefold()
        if provider_name not in _PROVIDER_NAMES or " " in provider_name:
            self.error = (
                "Supported providers: auto, codex, deepseek-harness, "
                "openai-compatible, or builtin."
            )
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
        if update.activities:
            self.activities.extend(update.activities)
            self.activities = self.activities[-80:]
        if update.view is not None:
            try:
                self._set_view(update.view)
            except PCBDraftError as exc:
                self.error = str(exc)
        if not update.terminal:
            return bool(update.activities)
        if update.status == "failed":
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
        if update.view is not None:
            self._set_view(update.view)
        else:
            self.project_id = update.project_id
        self.activities = []
        self.active_job_status = update.status
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

    def _set_view(self, value: dict[str, Any]) -> None:
        project = value.get("project")
        if not isinstance(project, dict) or not isinstance(project.get("id"), str):
            raise ValidationError("application view has no valid project identity")
        self.view = value
        self.project_id = project["id"]
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


class PCBDraftTui:
    """Curses renderer and keyboard loop for :class:`TuiController`."""

    def __init__(self, screen: Any, controller: TuiController) -> None:
        self.screen = screen
        self.controller = controller
        self.input_text = ""
        self.scroll = 0
        self.busy = ""
        self.height = 0
        self.width = 0
        self.palette_index = 0
        self.palette_dismissed = False
        self.review_scroll = 0
        self._input_only_redraw = False
        self._init_terminal()

    def run(self) -> int:
        self._render()
        while True:
            if self.controller.poll():
                self._render()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            self._input_only_redraw = False
            try:
                result = self._handle_key(key)
            except _QuitTui:
                return 0
            if result == "quit":
                return 0
            if self._input_only_redraw:
                self._render_input_only()
            else:
                self._render()

    def _init_terminal(self) -> None:
        self.screen.keypad(True)
        timeout = getattr(self.screen, "timeout", None)
        if self.controller.runtime is not None and callable(timeout):
            timeout(100)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            has_colors = curses.has_colors()
        except curses.error:
            has_colors = False
        if not has_colors:
            return
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        for number, foreground in (
            (1, curses.COLOR_BLACK),
            (2, curses.COLOR_CYAN),
            (3, curses.COLOR_GREEN),
            (4, curses.COLOR_YELLOW),
            (5, curses.COLOR_RED),
            (6, curses.COLOR_MAGENTA),
        ):
            try:
                curses.init_pair(number, foreground, -1)
            except curses.error:
                continue

    def _handle_key(self, key: str | int) -> str:
        if key == "\x11":  # Ctrl+Q
            if self.controller.is_busy:
                return self.controller.stop_active()
            return "quit"
        if self.controller.mode == "review":
            return self._handle_review_key(key)
        if key == "\x1b" and self.controller.is_busy:
            return self.controller.stop_active()
        if self.controller.mode == "project_picker":
            return self._handle_picker_key(key)
        if self._palette_visible():
            if key in (curses.KEY_UP,):
                self._move_palette(-1)
                return "continue"
            if key in (curses.KEY_DOWN,):
                self._move_palette(1)
                return "continue"
            if key in ("\t",):
                self._complete_selected_command()
                return "continue"
            if key == "\x1b":
                self.palette_dismissed = True
                return "continue"
        if key in ("\x0e", curses.KEY_F2):  # Ctrl+N / F2
            self._run_action("new")
            return "continue"
        if key in ("\x10", curses.KEY_F3):  # Ctrl+P / F3
            self._run_action("projects")
            return "continue"
        if key in ("\x12", curses.KEY_F5):  # Ctrl+R / F5
            self._run_action("status")
            return "continue"
        if key == curses.KEY_F4:
            self.review_scroll = 0
            self._run_action("review")
            return "continue"
        if key == "\x0c":  # Ctrl+L
            self._run_action("logs")
            return "continue"
        if key in ("\x19", curses.KEY_F7):  # Ctrl+Y / F7
            self._run_action("confirm")
            return "continue"
        if key in ("\x16", curses.KEY_F6):  # Ctrl+V / F6
            self._run_action("validate")
            return "continue"
        if key == "\x1a":  # Ctrl+Z
            self._run_action("undo")
            return "continue"
        if key == "\x04":  # Ctrl+D
            self._run_action("discard")
            return "continue"
        if key == curses.KEY_PPAGE:
            self.scroll += max(1, self.height // 3)
            return "continue"
        if key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - max(1, self.height // 3))
            return "continue"
        if key == curses.KEY_UP:
            self.scroll += 1
            return "continue"
        if key == curses.KEY_DOWN:
            self.scroll = max(0, self.scroll - 1)
            return "continue"
        if key == "\x1b":
            self.controller.cancel_overlay()
            self.input_text = ""
            self.palette_dismissed = False
            return "continue"
        if key in ("\n", "\r", curses.KEY_ENTER):
            return self._submit()
        if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
            had_palette = self._palette_visible()
            self.input_text = self.input_text[:-1]
            self._input_changed()
            if not had_palette and not self._palette_visible():
                self._input_only_redraw = True
            return "continue"
        if isinstance(key, str) and key.isprintable():
            if len(self.input_text) >= _MAX_INPUT_CHARS:
                self.controller.error = (
                    f"Input is limited to {_MAX_INPUT_CHARS} characters."
                )
            else:
                had_palette = self._palette_visible()
                self.input_text += key
                self._input_changed()
                if not had_palette and not self._palette_visible():
                    self._input_only_redraw = True
        return "continue"

    def _handle_picker_key(self, key: str | int) -> str:
        if key in ("\x1b", "q", "Q"):
            self.controller.cancel_overlay()
        elif key in (curses.KEY_UP, "k"):
            self.controller.move_picker(-1)
        elif key in (curses.KEY_DOWN, "j"):
            self.controller.move_picker(1)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self._run_controller(self.controller.select_picker, "Opening project")
        return "continue"

    def _handle_review_key(self, key: str | int) -> str:
        if key in ("\x1b", "q", "Q", "\n", "\r", curses.KEY_ENTER):
            self.controller.cancel_overlay()
            self.review_scroll = 0
        elif key in (curses.KEY_UP, "k"):
            self.review_scroll = max(0, self.review_scroll - 1)
        elif key in (curses.KEY_DOWN, "j"):
            self.review_scroll += 1
        elif key == curses.KEY_PPAGE:
            self.review_scroll = max(0, self.review_scroll - max(1, self.height // 2))
        elif key == curses.KEY_NPAGE:
            self.review_scroll += max(1, self.height // 2)
        return "continue"

    def _input_changed(self) -> None:
        self.palette_index = 0
        self.palette_dismissed = False

    def _palette_visible(self) -> bool:
        return (
            self.controller.mode == "message"
            and self.input_text.startswith("/")
            and not self.palette_dismissed
        )

    def _palette_commands(self) -> tuple[SlashCommand, ...]:
        return command_suggestions(self.input_text) if self._palette_visible() else ()

    def _move_palette(self, delta: int) -> None:
        commands = self._palette_commands()
        if not commands:
            return
        self.palette_index = (self.palette_index + delta) % len(commands)

    def _selected_command(self) -> SlashCommand | None:
        commands = self._palette_commands()
        if not commands:
            return None
        self.palette_index = max(0, min(self.palette_index, len(commands) - 1))
        return commands[self.palette_index]

    def _complete_selected_command(self) -> bool:
        command = self._selected_command()
        if command is None:
            return False
        self.input_text = command.name + (" " if command.accepts_argument else "")
        self.palette_index = 0
        self.palette_dismissed = False
        return True

    def _submit(self) -> str:
        if self._palette_visible():
            command = self._selected_command()
            if command is not None and self._command_needs_completion(command):
                self._complete_selected_command()
                return "continue"
        text = self.input_text
        if not text.strip() and self.controller.mode == "message":
            return "continue"
        self.input_text = ""
        self.palette_index = 0
        self.palette_dismissed = False
        self.scroll = 0
        result = self._run_controller(
            lambda: self.controller.submit(text), "Working with PCBDraft"
        )
        return result if result == "quit" else "continue"

    def _command_needs_completion(self, command: SlashCommand) -> bool:
        typed = self.input_text[1:]
        name, separator, _argument = typed.partition(" ")
        if name.casefold() != command.name[1:].casefold():
            return True
        return command.requires_argument and not separator

    def _run_action(self, action: str) -> None:
        result = self._run_controller(
            lambda: self.controller.action(action), self._action_label(action)
        )
        if result == "quit":
            raise _QuitTui

    def _run_controller(self, operation: Any, label: str) -> str:
        self.busy = label + "…"
        self._render()
        self.screen.refresh()
        try:
            result = operation()
            return result if isinstance(result, str) else "continue"
        finally:
            self.busy = ""

    @staticmethod
    def _action_label(action: str) -> str:
        return {
            "new": "Starting a project",
            "projects": "Loading projects",
            "status": "Refreshing project",
            "review": "Opening engineering review",
            "logs": "Changing activity detail",
            "retry": "Retrying the last job",
            "confirm": "Confirming generation",
            "validate": "Running validation",
            "undo": "Undoing change",
            "discard": "Discarding change",
            "release": "Building release evidence",
        }.get(action, "Working")

    def _render(self) -> None:
        self.height, self.width = self.screen.getmaxyx()
        self.screen.erase()
        if self.height < _MIN_HEIGHT or self.width < _MIN_WIDTH:
            self._render_too_small()
            self.screen.refresh()
            return
        self._render_header()
        palette_height = self._palette_height()
        input_top = self.height - 2
        palette_top = input_top - palette_height
        content_top = 3
        content_height = max(1, palette_top - content_top)
        self._render_conversation(content_top, content_height, self.width - 2)
        if palette_height:
            self._render_palette(palette_top)
        self._render_input(input_top)
        if self.controller.mode == "project_picker":
            self._render_project_picker()
        elif self.controller.mode == "review":
            self._render_review()
        self.screen.refresh()

    def _render_input_only(self) -> None:
        """Update a normal text edit without reflowing the whole transcript."""

        if self.screen.getmaxyx() != (self.height, self.width):
            self._render()
            return
        input_top = self.height - 2
        self._add(input_top + 1, 0, " " * max(0, self.width - 1))
        self._render_input(input_top)
        self.screen.refresh()

    def _render_too_small(self) -> None:
        self._add(
            0,
            0,
            "PCBDraft needs a terminal at least "
            f"{_MIN_WIDTH}×{_MIN_HEIGHT}; resize to continue.",
            curses.A_BOLD,
        )
        self._add(2, 0, "Resize, then type /quit to exit.")

    def _render_header(self) -> None:
        project_name = "No project"
        status = "ready"
        if isinstance(self.controller.view, dict):
            project = self.controller.view.get("project")
            if isinstance(project, dict):
                project_name = str(project.get("name", project_name))
                status = str(project.get("status", status)).replace("_", " ")
        left = f" PCBDraft · {project_name} · {status} "
        right = f" {self.controller.provider_name} · {self.controller.provider_status} "
        self._add(0, 0, " " * max(0, self.width - 1), self._style(1, curses.A_BOLD))
        self._add(0, 0, left, self._style(1, curses.A_BOLD))
        if len(left) + len(right) + 2 < self.width:
            self._add(
                0,
                self.width - len(right) - 1,
                right,
                self._style(1, curses.A_BOLD),
            )
        subtitle = (
            self.busy
            or self.controller.error
            or self.controller.notice
            or self.controller.activity_label
            or "Describe a board; PCBDraft chooses implementation details automatically."
        )
        tone = self._style(5) if self.controller.error else self._style(4)
        self._add(1, 1, subtitle, tone)
        self._add(2, 0, "─" * max(0, self.width - 1), self._style(4))

    def _render_conversation(self, y: int, height: int, width: int) -> None:
        lines = self._conversation_lines(width)
        maximum_scroll = max(0, len(lines) - height)
        self.scroll = max(0, min(self.scroll, maximum_scroll))
        start = max(0, len(lines) - height - self.scroll)
        for offset, (text, style) in enumerate(lines[start : start + height]):
            self._add(y + offset, 1, text, style)
        if self.scroll:
            self._add(y, max(1, self.width - 10), "↑ older", self._style(4))
        if start + height < len(lines):
            self._add(
                y + height - 1, max(1, self.width - 10), "↓ newer", self._style(4)
            )

    def _conversation_lines(self, width: int) -> list[tuple[str, int]]:
        if not isinstance(self.controller.view, dict):
            return [
                ("Welcome to PCBDraft.", self._style(2, curses.A_BOLD)),
                ("Describe the board you want to build to create a local project.", 0),
                ("Use /new to name it first, or /projects to reopen one.", 0),
                (
                    "The agent chooses layers, plans the circuit, generates KiCad, and runs checks.",
                    0,
                ),
            ]
        conversation = self.controller.view.get("conversation")
        messages = (
            conversation.get("messages") if isinstance(conversation, dict) else []
        )
        result: list[tuple[str, int]] = []
        if isinstance(messages, list):
            for message in messages[-300:]:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role", "system"))
                text = str(message.get("text", "")).strip()
                label, style = {
                    "user": ("You", self._style(2, curses.A_BOLD)),
                    "assistant": ("PCBDraft", self._style(3, curses.A_BOLD)),
                }.get(role, ("System", self._style(4, curses.A_BOLD)))
                result.append((label, style))
                if text:
                    result.extend(
                        (line, 0) for line in self._wrap(text, max(12, width - 1))
                    )
                result.append(("", 0))
        if not result:
            result.extend(
                [
                    ("No messages yet.", self._style(4)),
                    ("Describe a board or a requested change below.", 0),
                ]
            )
        if self.controller.activities:
            result.append(("", 0))
            detail_hint = (
                "expanded · /logs off"
                if self.controller.logs_expanded
                else "/logs to expand"
            )
            result.append(
                (
                    f"Agent activity ({detail_hint})",
                    self._style(6, curses.A_BOLD),
                )
            )
            activity_limit = 40 if self.controller.logs_expanded else 12
            for activity in self.controller.activities[-activity_limit:]:
                marker = {
                    "queued": "○",
                    "running": "●",
                    "completed": "✓",
                    "failed": "×",
                    "info": "·",
                }[activity.state]
                style = (
                    self._style(5)
                    if activity.state == "failed"
                    else self._style(3)
                    if activity.state == "completed"
                    else self._style(4)
                )
                result.append((f"{marker} {activity.tool} — {activity.label}", style))
                if self.controller.logs_expanded and activity.message:
                    detail = f"  {activity.created_at} · {activity.message}"
                    result.extend(
                        (line, self._style(4))
                        for line in self._wrap(detail, max(12, width - 3))
                    )
        status = self.controller._project_status()
        if status == "awaiting_confirmation":
            result.extend(
                [
                    ("", 0),
                    (
                        "Plan ready for review. Use /confirm when you are ready.",
                        self._style(4),
                    ),
                ]
            )
        elif status == "change_ready":
            result.extend(
                [
                    ("", 0),
                    (
                        "Semantic change ready. Use /confirm to apply or /discard.",
                        self._style(4),
                    ),
                ]
            )
        return result

    def _palette_height(self) -> int:
        commands = self._palette_commands()
        if not commands:
            return 0
        return 2 + (len(commands) + 1) // 2

    def _render_palette(self, y: int) -> None:
        commands = self._palette_commands()
        if not commands:
            return
        rows = (len(commands) + 1) // 2
        self._add(
            y,
            1,
            "Commands — filter by name; all commands are shown for /",
            self._style(4),
        )
        column_width = max(30, (self.width - 4) // 2)
        for index, command in enumerate(commands):
            column = index % 2
            row = index // 2
            marker = "› " if index == self.palette_index else "  "
            text = f"{marker}{command.usage} — {command.description}"
            style = self._style(1, curses.A_BOLD) if index == self.palette_index else 0
            self._add(y + 1 + row, 1 + column * column_width, text, style)
        self._add(
            y + rows + 1,
            1,
            "↑/↓ choose · Tab complete · Enter complete/run · Esc dismiss",
            self._style(4),
        )

    def _render_input(self, y: int) -> None:
        hint = "Enter sends · / commands · /quit exits"
        if self.controller.is_busy:
            hint = "Agent is working · Esc or /stop requests cancellation"
        elif self.controller.mode == "new_name":
            hint = "Enter continues · Esc cancels"
        elif self.controller.mode == "new_request":
            hint = "Enter creates the project · Esc cancels"
        self._add(y, 1, hint, self._style(4))
        prompt = self.controller.input_label + " › "
        self._add(y + 1, 1, prompt, self._style(2, curses.A_BOLD))
        if self.controller.mode != "project_picker":
            prompt_width = _cell_width(prompt)
            available = max(1, self.width - prompt_width - 3)
            visible = _tail_to_cell_width(self.input_text, available)
            self._add(y + 1, 1 + prompt_width, visible)
            try:
                self.screen.move(
                    y + 1,
                    min(self.width - 2, 1 + prompt_width + _cell_width(visible)),
                )
            except curses.error:
                pass

    def _render_project_picker(self) -> None:
        box_width = min(max(48, self.width * 3 // 5), self.width - 8)
        box_height = min(max(8, len(self.controller.projects) + 6), self.height - 6)
        x = max(2, (self.width - box_width) // 2)
        y = max(2, (self.height - box_height) // 2)
        self._box(y, x, box_height, box_width, "Open local project")
        if not self.controller.projects:
            self._add(y + 2, x + 2, "No local projects. Esc closes this picker.")
            return
        visible = box_height - 4
        first = max(
            0,
            min(
                self.controller.picker_index - visible + 1,
                len(self.controller.projects) - visible,
            ),
        )
        for offset, item in enumerate(
            self.controller.projects[first : first + visible]
        ):
            selected = first + offset == self.controller.picker_index
            name = str(item.get("name", "Unnamed project"))
            status = str(item.get("status", "unknown"))
            project_id = str(item.get("id", ""))
            text = f"{'› ' if selected else '  '}{name}  [{status}]  {project_id}"
            self._add(
                y + 2 + offset,
                x + 2,
                text,
                self._style(1, curses.A_BOLD) if selected else 0,
            )
        self._add(
            y + box_height - 2,
            x + 2,
            "↑/↓ or j/k select · Enter open · Esc cancel",
            self._style(4),
        )

    def _render_review(self) -> None:
        box_width = max(48, self.width - 8)
        box_height = max(10, self.height - 6)
        x = max(2, (self.width - box_width) // 2)
        y = max(2, (self.height - box_height) // 2)
        self._box(y, x, box_height, box_width, "Plan and change review")
        sections = (
            review_sections(self.controller.view)
            if isinstance(self.controller.view, dict)
            else ()
        )
        lines: list[tuple[str, int]] = []
        content_width = max(12, box_width - 6)
        for section in sections:
            if lines:
                lines.append(("", 0))
            lines.append((section.title, self._style(2, curses.A_BOLD)))
            for item in section.lines:
                lines.extend(
                    (line, 0) for line in self._wrap(f"• {item}", content_width)
                )
        visible = max(1, box_height - 4)
        maximum_scroll = max(0, len(lines) - visible)
        self.review_scroll = max(0, min(self.review_scroll, maximum_scroll))
        start = self.review_scroll
        for offset, (text, style) in enumerate(lines[start : start + visible]):
            self._add(y + 2 + offset, x + 3, text, style)
        footer = "↑/↓ or PgUp/PgDn scroll · Enter/Esc close"
        if self.review_scroll:
            footer += " · ↑ earlier"
        if self.review_scroll < maximum_scroll:
            footer += " · ↓ later"
        self._add(y + box_height - 2, x + 3, footer, self._style(4))

    def _box(self, y: int, x: int, height: int, width: int, title: str) -> None:
        if height < 3 or width < 5:
            return
        horizontal = "-" * max(0, width - 2)
        self._add(y, x, "+" + horizontal + "+", self._style(4))
        self._add(y + height - 1, x, "+" + horizontal + "+", self._style(4))
        for row in range(y + 1, y + height - 1):
            self._add(row, x, "|", self._style(4))
            self._add(row, x + width - 1, "|", self._style(4))
        self._add(y, x + 2, f" {title} ", self._style(2, curses.A_BOLD))

    def _add(self, y: int, x: int, text: str, style: int = 0) -> None:
        if y < 0 or x < 0 or y >= self.height or x >= self.width - 1:
            return
        try:
            self.screen.addnstr(
                y, x, text.replace("\n", " "), self.width - x - 1, style
            )
        except curses.error:
            return

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        clean = " ".join(text.replace("\n", " ").split())
        if not clean:
            return [""]
        line_width = max(1, width)
        result: list[str] = []
        current = ""
        for word in clean.split(" "):
            candidate = word if not current else f"{current} {word}"
            if _cell_width(candidate) <= line_width:
                current = candidate
                continue
            if current:
                result.append(current)
            chunks = _split_to_cell_width(word, line_width)
            result.extend(chunks[:-1])
            current = chunks[-1]
        if current:
            result.append(current)
        return result

    @staticmethod
    def _style(pair: int, attributes: int = 0) -> int:
        try:
            return curses.color_pair(pair) | attributes
        except curses.error:
            return attributes


class _QuitTui(Exception):
    """Internal control-flow marker used after a shortcut requests exit."""


def run_tui_command(
    *,
    workspace: str | Path | None,
    provider: str,
    project_id: str | None,
    timeout: float,
) -> int:
    """Launch the default full-screen terminal interface."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValidationError(
            "the full-screen terminal interface requires an interactive terminal; "
            "use `chat --json` for scripting"
        )
    if os.environ.get("TERM", "").casefold() in {"", "dumb"}:
        raise ValidationError(
            "the full-screen terminal interface requires cursor-addressing support"
        )
    service = ApplicationService(workspace, provider_name=provider)
    runtime = AgentRuntime(service)
    session_store = TuiSessionStore(service.root)
    controller = TuiController(
        service=service,
        runtime=runtime,
        session_store=session_store,
        timeout=timeout,
        project_id=project_id,
    )
    try:
        return curses.wrapper(lambda screen: PCBDraftTui(screen, controller).run())
    except curses.error as exc:
        raise PCBDraftError("unable to initialize the terminal interface") from exc
    finally:
        runtime.shutdown()
