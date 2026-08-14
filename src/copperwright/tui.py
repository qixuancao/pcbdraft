"""Full-screen terminal interface for the CopperWright application service.

The TUI intentionally owns no engineering state and does not write KiCad files
itself.  It is a small interaction shell over :class:`ApplicationService`, just
like the browser client, so conversation, confirmation, attempts, validation,
and recovery keep one authoritative implementation.
"""

from __future__ import annotations

import curses
import os
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .application import ApplicationService
from .errors import CopperWrightError, ValidationError

_MAX_INPUT_CHARS = 8_192
_MIN_HEIGHT = 16
_MIN_WIDTH = 72


@dataclass
class TuiController:
    """Testable controller for a full-screen CopperWright terminal session."""

    service: ApplicationService
    timeout: float = 420.0
    project_id: str | None = None
    view: dict[str, Any] | None = None
    mode: str = "message"
    new_name: str = ""
    notice: str = ""
    error: str = ""
    projects: list[dict[str, Any]] = field(default_factory=list)
    picker_index: int = 0

    def __post_init__(self) -> None:
        if self.project_id:
            self.refresh()
        else:
            self.projects = self.service.list_projects()
            self.notice = (
                "Ctrl+N creates a board project; Ctrl+P opens an existing one."
            )

    @property
    def provider_name(self) -> str:
        provider = getattr(self.service, "provider", None)
        value = getattr(provider, "provider_id", None)
        return value if isinstance(value, str) else "configured provider"

    @property
    def input_label(self) -> str:
        if self.mode == "new_name":
            return "Project name"
        if self.mode == "new_request":
            return "What should this board do?"
        return "Message"

    def refresh(self) -> None:
        self.error = ""
        if self.project_id is None:
            self.projects = self.service.list_projects()
            self.notice = "No project selected. Ctrl+N creates one; Ctrl+P opens one."
            return
        try:
            self._set_view(self.service.open_project(self.project_id))
            self.notice = "Project refreshed."
        except CopperWrightError as exc:
            self.error = str(exc)

    def begin_new(self, name: str | None = None) -> None:
        self.error = ""
        self.new_name = (name or "").strip()
        self.mode = "new_request" if self.new_name else "new_name"
        self.notice = (
            "Describe the board request next."
            if self.new_name
            else "Enter a short project name."
        )

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
            self.notice = "No local projects yet. Press Ctrl+N to create one."

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
        value = selected.get("id")
        if not isinstance(value, str):
            self.error = "Selected project record is malformed."
            return
        self.open_project(value)

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
        except CopperWrightError as exc:
            self.error = str(exc)

    def cancel_overlay(self) -> None:
        if self.mode in {"project_picker", "help"}:
            self.mode = "message"
            self.notice = ""
        elif self.mode in {"new_name", "new_request"}:
            self.mode = "message"
            self.new_name = ""
            self.notice = "New project cancelled."

    def submit(self, text: str) -> str:
        """Handle a completed input line and return ``continue`` or ``quit``."""

        if len(text) > _MAX_INPUT_CHARS:
            self.error = f"Input is limited to {_MAX_INPUT_CHARS} characters."
            return "continue"
        clean = text.strip()
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
        if clean.startswith("/"):
            return self._command(clean)
        if not self._has_project():
            return "continue"
        self._call(
            "Planning the requested board",
            lambda: self.service.send_message(
                self.project_id or "", clean, timeout=self.timeout
            ),
        )
        return "continue"

    def action(self, name: str) -> str:
        """Run a deliberate UI action without exposing raw engineering writes."""

        if name == "new":
            self.begin_new()
            return "continue"
        if name == "projects":
            self.show_projects()
            return "continue"
        if name == "refresh":
            self.refresh()
            return "continue"
        if name == "help":
            self.mode = "help"
            self.error = ""
            return "continue"
        if name == "quit":
            return "quit"
        if not self._has_project():
            return "continue"
        if name == "confirm":
            status = self._project_status()
            if status == "awaiting_confirmation":
                self._call(
                    "Generating and validating the reviewed KiCad attempt",
                    lambda: self.service.confirm_project(
                        self.project_id or "", timeout=self.timeout
                    ),
                )
            elif status == "change_ready":
                self._call(
                    "Applying the reviewed semantic change",
                    lambda: self.service.apply_modification(self.project_id or ""),
                )
            else:
                self.error = "Nothing is awaiting confirmation."
            return "continue"
        if name == "validate":
            self._call(
                "Running L0–L7 validation",
                lambda: self.service.validate_project(
                    self.project_id or "", timeout=min(self.timeout, 3_600.0)
                ),
            )
            return "continue"
        if name == "undo":
            self._call(
                "Undoing the last semantic change",
                lambda: self.service.undo_last_modification(self.project_id or ""),
            )
            return "continue"
        if name == "discard":
            self._call(
                "Discarding the staged semantic change",
                lambda: self.service.discard_modification(self.project_id or ""),
            )
            return "continue"
        if name == "release":
            self._call(
                "Building manufacturing-candidate evidence",
                lambda: self.service.build_release(
                    self.project_id or "", timeout=min(self.timeout, 3_600.0)
                ),
            )
            return "continue"
        self.error = f"Unknown TUI action: {name}"
        return "continue"

    def _create_project(self, request: str) -> str:
        name = self.new_name
        if not name:
            self.error = "Project name is missing."
            self.mode = "new_name"
            return "continue"
        self.error = ""
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
            self.mode = "message"
            self.new_name = ""
            self.notice = (
                "Project created. Review the plan before confirming generation."
            )
        except CopperWrightError as exc:
            self.error = str(exc)
        return "continue"

    def _command(self, text: str) -> str:
        command, _, argument = text[1:].partition(" ")
        command = command.casefold()
        argument = argument.strip()
        if command in {"q", "quit", "exit"}:
            return self.action("quit")
        if command in {"new", "n"}:
            self.begin_new(argument or None)
            return "continue"
        if command in {"projects", "project", "p"}:
            self.show_projects()
            return "continue"
        if command == "open":
            self.open_project(argument)
            return "continue"
        if command in {"refresh", "status"}:
            return self.action("refresh")
        if command in {"confirm", "apply"}:
            return self.action("confirm")
        if command == "validate":
            return self.action("validate")
        if command == "undo":
            return self.action("undo")
        if command == "discard":
            return self.action("discard")
        if command == "release":
            return self.action("release")
        if command in {"help", "?"}:
            return self.action("help")
        self.error = "Unknown command. Type /help for the command list."
        return "continue"

    def _call(self, label: str, operation: Any) -> None:
        self.error = ""
        self.notice = label + "…"
        try:
            value = operation()
            if not isinstance(value, dict):
                raise ValidationError("application returned an invalid project view")
            self._set_view(value)
            self.notice = label + " complete."
        except CopperWrightError as exc:
            self.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - keep the terminal usable
            self.error = f"Unexpected UI action failure: {type(exc).__name__}"

    def _set_view(self, value: dict[str, Any]) -> None:
        project = value.get("project")
        if not isinstance(project, dict) or not isinstance(project.get("id"), str):
            raise ValidationError("application view has no valid project identity")
        self.view = value
        self.project_id = project["id"]
        self.projects = self.service.list_projects()

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


class CopperWrightTui:
    """Curses renderer and keyboard loop for :class:`TuiController`."""

    def __init__(self, screen: Any, controller: TuiController) -> None:
        self.screen = screen
        self.controller = controller
        self.input_text = ""
        self.scroll = 0
        self.busy = ""
        self.height = 0
        self.width = 0
        self._init_terminal()

    def run(self) -> int:
        while True:
            self._render()
            key = self.screen.get_wch()
            try:
                result = self._handle_key(key)
            except _QuitTui:
                return 0
            if result == "quit":
                return 0

    def _init_terminal(self) -> None:
        self.screen.keypad(True)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            try:
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
        if self.controller.mode == "project_picker":
            return self._handle_picker_key(key)
        if self.controller.mode == "help":
            if key in ("\x1b", "\n", "\r", curses.KEY_ENTER, "q", "Q"):
                self.controller.cancel_overlay()
            return "continue"
        if key in ("\x11",):  # Ctrl+Q
            return "quit"
        if key in ("\x0e", curses.KEY_F2):  # Ctrl+N / F2
            self._run_action("new")
            return "continue"
        if key in ("\x10", curses.KEY_F3):  # Ctrl+P / F3
            self._run_action("projects")
            return "continue"
        if key in ("\x12", curses.KEY_F5):  # Ctrl+R / F5
            self._run_action("refresh")
            return "continue"
        if key in ("\x19", curses.KEY_F7):  # Ctrl+Y / F7
            self._run_action("confirm")
            return "continue"
        if key in ("\x16", curses.KEY_F6):  # Ctrl+V / F6
            self._run_action("validate")
            return "continue"
        if key in ("\x1a",):  # Ctrl+Z
            self._run_action("undo")
            return "continue"
        if key in ("\x04",):  # Ctrl+D
            self._run_action("discard")
            return "continue"
        if key in (curses.KEY_PPAGE,):
            self.scroll += max(1, self.height // 3)
            return "continue"
        if key in (curses.KEY_NPAGE,):
            self.scroll = max(0, self.scroll - max(1, self.height // 3))
            return "continue"
        if key in ("\x1b",):
            self.controller.cancel_overlay()
            self.input_text = ""
            return "continue"
        if key in ("\n", "\r", curses.KEY_ENTER):
            return self._submit()
        if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
            self.input_text = self.input_text[:-1]
            return "continue"
        if isinstance(key, str) and key.isprintable():
            if len(self.input_text) >= _MAX_INPUT_CHARS:
                self.controller.error = (
                    f"Input is limited to {_MAX_INPUT_CHARS} characters."
                )
            else:
                self.input_text += key
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
        elif key in ("\x11",):
            return "quit"
        return "continue"

    def _submit(self) -> str:
        text = self.input_text
        if not text.strip() and self.controller.mode == "message":
            return "continue"
        self.input_text = ""
        self.scroll = 0
        result = self._run_controller(
            lambda: self.controller.submit(text), "Working with CopperWright"
        )
        return result if result == "quit" else "continue"

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
            "refresh": "Refreshing project",
            "confirm": "Confirming generation",
            "validate": "Running validation",
            "undo": "Undoing change",
            "discard": "Discarding change",
            "release": "Building release evidence",
            "help": "Opening help",
        }.get(action, "Working")

    def _render(self) -> None:
        self.height, self.width = self.screen.getmaxyx()
        self.screen.erase()
        if self.height < _MIN_HEIGHT or self.width < _MIN_WIDTH:
            self._render_too_small()
            self.screen.refresh()
            return
        self._render_header()
        content_top = 2
        input_top = self.height - 3
        content_height = input_top - content_top - 1
        left_width = max(42, min(self.width - 30, (self.width * 3) // 5))
        right_x = left_width + 1
        right_width = self.width - right_x
        self._box(content_top, 0, content_height, left_width, "Conversation")
        self._box(content_top, right_x, content_height, right_width, "Design evidence")
        self._render_conversation(
            content_top + 1, 1, content_height - 2, left_width - 2
        )
        self._render_evidence(
            content_top + 1, right_x + 1, content_height - 2, right_width - 2
        )
        self._render_input(input_top)
        if self.controller.mode == "project_picker":
            self._render_project_picker()
        elif self.controller.mode == "help":
            self._render_help()
        self.screen.refresh()

    def _render_too_small(self) -> None:
        self._add(
            0,
            0,
            f"CopperWright TUI needs at least {_MIN_WIDTH}×{_MIN_HEIGHT}; resize this terminal.",
            curses.A_BOLD,
        )
        self._add(2, 0, "Ctrl+Q quits after resizing.")

    def _render_header(self) -> None:
        project_name = "No project"
        status = "ready"
        if isinstance(self.controller.view, dict):
            project = self.controller.view.get("project")
            if isinstance(project, dict):
                project_name = str(project.get("name", project_name))
                status = str(project.get("status", status))
        title = f" CopperWright TUI  |  {project_name} "
        right = f"provider: {self.controller.provider_name}  |  {status} "
        self._add(0, 0, " " * max(0, self.width - 1), self._style(1, curses.A_BOLD))
        self._add(0, 0, title, self._style(1, curses.A_BOLD))
        self._add(
            0,
            max(0, self.width - len(right) - 1),
            right,
            self._style(1, curses.A_BOLD),
        )
        subtitle = (
            self.busy
            or self.controller.error
            or self.controller.notice
            or "Review intent first; native KiCad generation always needs confirmation."
        )
        tone = self._style(5) if self.controller.error else self._style(2)
        self._add(1, 1, subtitle, tone)

    def _render_conversation(self, y: int, x: int, height: int, width: int) -> None:
        lines = self._conversation_lines(width)
        if not lines:
            lines = [
                ("Welcome. Press Ctrl+N to create a PCB project.", self._style(2)),
                (
                    "The AI will propose a reviewable circuit plan before it can generate KiCad files.",
                    0,
                ),
            ]
        maximum_scroll = max(0, len(lines) - height)
        self.scroll = max(0, min(self.scroll, maximum_scroll))
        start = max(0, len(lines) - height - self.scroll)
        for offset, (text, style) in enumerate(lines[start : start + height]):
            self._add(y + offset, x, text, style)
        if self.scroll:
            self._add(y, x + max(0, width - 8), "↑ scroll", self._style(4))
        if start + height < len(lines):
            self._add(y + height - 1, x + max(0, width - 8), "↓ more", self._style(4))

    def _conversation_lines(self, width: int) -> list[tuple[str, int]]:
        if not isinstance(self.controller.view, dict):
            return []
        conversation = self.controller.view.get("conversation")
        messages = (
            conversation.get("messages") if isinstance(conversation, dict) else []
        )
        if not isinstance(messages, list):
            return []
        result: list[tuple[str, int]] = []
        for message in messages[-300:]:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "system"))
            kind = str(message.get("kind", "message"))
            text = str(message.get("text", ""))
            label, style = {
                "user": ("You", self._style(2, curses.A_BOLD)),
                "assistant": ("CopperWright", self._style(3, curses.A_BOLD)),
            }.get(role, ("System", self._style(4, curses.A_BOLD)))
            result.append((f"{label} · {kind}", style))
            wrapped = self._wrap(text, max(12, width - 2))
            result.extend((f"  {line}", 0) for line in wrapped)
            result.append(("", 0))
        return result

    def _render_evidence(self, y: int, x: int, height: int, width: int) -> None:
        lines = self._evidence_lines(width)
        for offset, (text, style) in enumerate(lines[:height]):
            self._add(y + offset, x, text, style)

    def _evidence_lines(self, width: int) -> list[tuple[str, int]]:
        if not isinstance(self.controller.view, dict):
            return [
                ("No project selected", self._style(4, curses.A_BOLD)),
                ("", 0),
                ("Ctrl+N  New project", self._style(2)),
                ("Ctrl+P  Open project", self._style(2)),
                ("", 0),
                (
                    "A normal part request is planned and attempted; it is not rejected simply because there is no fixed template.",
                    0,
                ),
            ]
        project = self.controller.view.get("project")
        proposal = self.controller.view.get("conversation", {}).get("proposal")
        result: list[tuple[str, int]] = []
        if isinstance(project, dict):
            result.append(
                (str(project.get("name", "Project")), self._style(2, curses.A_BOLD))
            )
            result.append(
                (f"Status: {project.get('status', 'unknown')}", self._style(4))
            )
            result.append((f"ID: {project.get('id', '')}", 0))
        if isinstance(proposal, dict):
            scope = proposal.get("scope", {})
            if isinstance(scope, dict):
                result.append(("", 0))
                result.append(("Request", self._style(2, curses.A_BOLD)))
                result.extend(
                    (line, 0)
                    for line in self._wrap(
                        f"Generation: {scope.get('decision', 'unknown')}", width
                    )
                )
                for warning in scope.get("warnings", []):
                    result.extend(
                        (f"! {line}", self._style(5))
                        for line in self._wrap(str(warning), max(12, width - 2))
                    )
            brief = proposal.get("brief")
            if isinstance(brief, dict):
                result.append(("", 0))
                result.append(("Reviewed plan", self._style(2, curses.A_BOLD)))
                result.extend(
                    (line, 0)
                    for line in self._wrap(str(brief.get("purpose", "")), width)
                )
                board = brief.get("board")
                if isinstance(board, dict):
                    result.append(
                        (
                            f"Board: {board.get('width_mm')} × {board.get('height_mm')} mm · {board.get('layers')} layers",
                            0,
                        )
                    )
                bom = brief.get("bom")
                if isinstance(bom, list):
                    placements = sum(
                        item.get("quantity", 0)
                        for item in bom
                        if isinstance(item, dict)
                        and isinstance(item.get("quantity"), int)
                    )
                    result.append(
                        (f"Parts: {placements} placements / {len(bom)} types", 0)
                    )
                review = brief.get("plan_review")
                if isinstance(review, dict):
                    summary = review.get("summary", {})
                    if isinstance(summary, dict):
                        attention = summary.get("attention_required", 0)
                        failed = summary.get("failed", 0)
                    else:
                        attention = 0
                        failed = 0
                    if isinstance(attention, int) and attention > 0:
                        result.append(("", 0))
                        result.append(
                            ("Topology warnings", self._style(5, curses.A_BOLD))
                        )
                        result.append(
                            (
                                f"{attention} need attention · {failed} structural failures · generation available",
                                self._style(5),
                            )
                        )
                    findings = review.get("findings")
                    if (
                        isinstance(findings, list)
                        and isinstance(attention, int)
                        and attention > 0
                    ):
                        for finding in findings:
                            if (
                                not isinstance(finding, dict)
                                or finding.get("outcome") == "pass"
                            ):
                                continue
                            title = str(finding.get("summary", "Topology finding"))
                            result.extend(
                                (f"• {line}", self._style(5))
                                for line in self._wrap(title, max(12, width - 2))
                            )
        attempts = self.controller.view.get("attempts")
        if isinstance(attempts, list) and attempts:
            latest = attempts[0]
            if isinstance(latest, dict):
                result.append(("", 0))
                result.append(("Latest attempt", self._style(2, curses.A_BOLD)))
                result.append(
                    (
                        f"{latest.get('status')} · {latest.get('phase')}",
                        self._style(5)
                        if latest.get("status") == "failed"
                        else self._style(4),
                    )
                )
        artifacts = self.controller.view.get("artifacts")
        validation = (
            artifacts.get("validation") if isinstance(artifacts, dict) else None
        )
        if isinstance(validation, dict):
            result.append(("", 0))
            result.append(("Checks", self._style(2, curses.A_BOLD)))
            result.append(
                (
                    "Result: "
                    + (
                        "passed"
                        if validation.get("candidate_ready")
                        else "findings remain"
                    ),
                    self._style(3)
                    if validation.get("candidate_ready")
                    else self._style(5),
                )
            )
        return result

    def _render_input(self, y: int) -> None:
        if self.height < 4:
            return
        prompt = self.controller.input_label + " › "
        if self.controller.mode in {"project_picker", "help"}:
            prompt = "Overlay › "
        message = self.controller.error or self.controller.notice
        style = self._style(5) if self.controller.error else self._style(4)
        self._add(y, 1, message, style)
        footer = (
            "Ctrl+N New · Ctrl+P Projects · F5 Refresh · F7 Confirm · "
            "F6 Validate · Ctrl+Q Quit · /help"
        )
        self._add(y + 1, 1, footer, self._style(4))
        self._add(y + 2, 1, prompt, self._style(2, curses.A_BOLD))
        if self.controller.mode not in {"project_picker", "help"}:
            available = max(1, self.width - len(prompt) - 3)
            visible = self.input_text[-available:]
            self._add(y + 2, 1 + len(prompt), visible)
            try:
                self.screen.move(
                    y + 2, min(self.width - 2, 1 + len(prompt) + len(visible))
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
            text = f"{'> ' if selected else '  '}{name}  [{status}]  {project_id}"
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

    def _render_help(self) -> None:
        lines = [
            "CopperWright TUI keyboard guide",
            "",
            "Enter          send message / advance the new-project form",
            "Ctrl+N         create a new project",
            "Ctrl+P         list and open local projects",
            "F5 / Ctrl+R    refresh current project",
            "F7 / Ctrl+Y    confirm a reviewed generation or staged change",
            "F6 / Ctrl+V    run L0–L7 validation",
            "Ctrl+Z         undo the last applied semantic change",
            "Ctrl+D         discard a staged semantic change",
            "PageUp/Down    scroll the conversation",
            "Ctrl+Q         quit",
            "",
            "Commands: /new [name], /projects, /open ID, /confirm, /validate,",
            "/undo, /discard, /release, /refresh, /quit",
            "",
            "The planner can propose a general circuit topology, but the right panel",
            "shows deterministic preflight evidence before KiCad generation is allowed.",
            "",
            "Press Esc, Enter, or q to close this help.",
        ]
        box_width = min(max(56, self.width * 4 // 5), self.width - 6)
        box_height = min(len(lines) + 4, self.height - 4)
        x = max(2, (self.width - box_width) // 2)
        y = max(1, (self.height - box_height) // 2)
        self._box(y, x, box_height, box_width, "Help")
        for offset, line in enumerate(lines[: box_height - 2]):
            style = self._style(2, curses.A_BOLD) if offset == 0 else 0
            self._add(y + 1 + offset, x + 2, line, style)

    def _box(self, y: int, x: int, height: int, width: int, title: str) -> None:
        if height < 3 or width < 5:
            return
        horizontal = "-" * max(0, width - 2)
        self._add(y, x, "+" + horizontal + "+", self._style(4))
        self._add(y + height - 1, x, "+" + horizontal + "+", self._style(4))
        for row in range(y + 1, y + height - 1):
            self._add(row, x, "|", self._style(4))
            self._add(row, x + width - 1, "|", self._style(4))
        label = f" {title} "
        self._add(y, x + 2, label, self._style(2, curses.A_BOLD))

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
        return textwrap.wrap(
            clean,
            width=max(1, width),
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]

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
    """Launch the full-screen terminal UI on an interactive local terminal."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValidationError(
            "tui requires an interactive terminal; use `chat --json` for scripting"
        )
    if os.environ.get("TERM", "").casefold() in {"", "dumb"}:
        raise ValidationError("tui requires a terminal with cursor-addressing support")
    service = ApplicationService(workspace, provider_name=provider)
    controller = TuiController(service=service, timeout=timeout, project_id=project_id)
    try:
        return curses.wrapper(lambda screen: CopperWrightTui(screen, controller).run())
    except curses.error as exc:
        raise CopperWrightError("unable to initialize the terminal interface") from exc
