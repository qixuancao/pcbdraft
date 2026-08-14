"""Compact full-screen terminal conversation for CopperWright.

The terminal is deliberately a thin client over :class:`ApplicationService`.
It owns no engineering state: project creation, planning, confirmation,
validation, recovery, and release actions remain in the application service.
"""

from __future__ import annotations

import curses
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .application import ApplicationService
from .errors import CopperWrightError, ValidationError
from .providers import resolve_provider

_MAX_INPUT_CHARS = 8_192
_MIN_HEIGHT = 16
_MIN_WIDTH = 72
_PROVIDER_NAMES = ("auto", "codex", "openai-compatible", "builtin")


def _cell_width(text: str) -> int:
    """Return terminal cells occupied by ordinary printable text."""

    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def _tail_to_cell_width(text: str, width: int) -> str:
    """Keep the newest input suffix that fits a terminal row."""

    result: list[str] = []
    used = 0
    for character in reversed(text):
        character_width = _cell_width(character)
        if character_width and used + character_width > width:
            break
        result.append(character)
        used += character_width
    return "".join(reversed(result))


def _split_to_cell_width(text: str, width: int) -> list[str]:
    """Split text into terminal rows without separating combining marks."""

    result: list[str] = []
    current: list[str] = []
    used = 0
    for character in text:
        character_width = _cell_width(character)
        if current and character_width and used + character_width > width:
            result.append("".join(current))
            current = []
            used = 0
        current.append(character)
        used += character_width
    if current:
        result.append("".join(current))
    return result or [""]


@dataclass(frozen=True)
class SlashCommand:
    """A visible, user-facing terminal command."""

    name: str
    usage: str
    description: str
    accepts_argument: bool = False
    requires_argument: bool = False


SLASH_COMMANDS = (
    SlashCommand("/help", "/help", "show command help"),
    SlashCommand("/new", "/new [name]", "start a project", True),
    SlashCommand("/projects", "/projects", "list local projects"),
    SlashCommand("/open", "/open ID", "open a project", True, True),
    SlashCommand("/status", "/status", "refresh this project"),
    SlashCommand(
        "/model",
        "/model [provider]",
        "show or switch planner",
        True,
    ),
    SlashCommand("/confirm", "/confirm", "confirm ready work"),
    SlashCommand("/validate", "/validate", "run validation"),
    SlashCommand("/undo", "/undo", "undo last change"),
    SlashCommand("/discard", "/discard", "discard staged change"),
    SlashCommand("/release", "/release", "build release evidence"),
    SlashCommand("/quit", "/quit", "quit CopperWright"),
)


def command_suggestions(text: str) -> tuple[SlashCommand, ...]:
    """Return commands matching the slash-command name currently being typed."""

    if not text.startswith("/"):
        return ()
    parts = text[1:].split(maxsplit=1)
    name = parts[0].casefold() if parts else ""
    return tuple(
        command
        for command in SLASH_COMMANDS
        if command.name[1:].casefold().startswith(name)
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
    """Testable controller for one full-screen CopperWright session."""

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
            self.notice = "Describe a board to begin, or type / for commands."

    @property
    def provider_name(self) -> str:
        provider = getattr(self.service, "provider", None)
        provider_id = getattr(provider, "provider_id", None)
        provider_id = provider_id if isinstance(provider_id, str) else "configured"
        diagnostic = getattr(provider, "diagnostic", None)
        details: dict[str, Any] = {}
        if callable(diagnostic):
            try:
                value = diagnostic()
            except Exception:  # noqa: BLE001 - header rendering must stay usable
                value = None
            if isinstance(value, dict):
                details = value
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
    def input_label(self) -> str:
        if self.mode == "new_name":
            return "Project name"
        if self.mode == "new_request":
            return "Board request"
        return "Message"

    def refresh(self) -> None:
        self.error = ""
        if self.project_id is None:
            self.projects = self.service.list_projects()
            self.notice = "No project selected. Describe a board or use /new."
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
        except CopperWrightError as exc:
            self.error = str(exc)

    def cancel_overlay(self) -> None:
        if self.mode == "project_picker":
            self.mode = "message"
            self.notice = ""
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
        if self.project_id is None:
            self.begin_new(_project_name_from_request(clean))
            return self._create_project(clean)
        self._call(
            "Planning the requested board",
            lambda: self.service.send_message(
                self.project_id or "", clean, timeout=self.timeout
            ),
        )
        return "continue"

    def action(self, name: str) -> str:
        """Run a deliberate UI action through the application service."""

        if name == "new":
            self.begin_new()
            return "continue"
        if name == "projects":
            self.show_projects()
            return "continue"
        if name == "status":
            self.refresh()
            return "continue"
        if name == "help":
            self.error = ""
            self.notice = "Type / for commands. ↑/↓ select · Tab complete · Enter run · Esc dismiss."
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
        self.error = f"Unknown terminal action: {name}"
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
        if command == "model":
            return self._set_provider(argument)
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
        if not argument:
            self.error = ""
            self.notice = (
                f"Planning provider: {self.provider_name}. "
                "Use /model [auto|codex|openai-compatible|builtin]."
            )
            return "continue"
        provider_name = argument.casefold()
        if provider_name not in _PROVIDER_NAMES or " " in provider_name:
            self.error = (
                "Supported providers: auto, codex, openai-compatible, or builtin."
            )
            return "continue"
        try:
            provider = resolve_provider(provider_name)
        except CopperWrightError as exc:
            self.error = str(exc)
            return "continue"
        self.service.provider = provider
        self.error = ""
        self.notice = (
            f"Planning provider switched for this session: {self.provider_name}."
        )
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
            self.error = f"Unexpected terminal action failure: {type(exc).__name__}"

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
        self.palette_index = 0
        self.palette_dismissed = False
        self._input_only_redraw = False
        self._init_terminal()

    def run(self) -> int:
        self._render()
        while True:
            key = self.screen.get_wch()
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
            return "quit"
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
            lambda: self.controller.submit(text), "Working with CopperWright"
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
            "CopperWright needs a terminal at least "
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
        left = f" CopperWright · {project_name} · {status} "
        right = f" {self.controller.provider_name} "
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
            or "Review plans before /confirm; type /quit to exit."
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
                ("Welcome to CopperWright.", self._style(2, curses.A_BOLD)),
                ("Describe the board you want to build to create a local project.", 0),
                ("Use /new to name it first, or /projects to reopen one.", 0),
                (
                    "A reviewable plan is required before /confirm creates KiCad files.",
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
                    "assistant": ("CopperWright", self._style(3, curses.A_BOLD)),
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
        if self.controller.mode == "new_name":
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
    controller = TuiController(service=service, timeout=timeout, project_id=project_id)
    try:
        return curses.wrapper(lambda screen: CopperWrightTui(screen, controller).run())
    except curses.error as exc:
        raise CopperWrightError("unable to initialize the terminal interface") from exc
