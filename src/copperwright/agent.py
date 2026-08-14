"""Compact Pi-style terminal conversation for CopperWright.

The full-screen TUI remains available for inspecting project evidence. This
module is intentionally smaller: a first request creates a local project, and
subsequent plain lines continue that project. Slash commands are reserved for
project and engineering actions, mirroring the lightweight interaction model
of terminal coding agents.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .application import ApplicationService
from .errors import CopperWrightError, ValidationError

START_QUESTION = "What would you like to build?"


def project_name_from_request(request: str) -> str:
    """Derive a short local project label without a second model request."""

    text = " ".join(request.split())
    text = re.sub(
        r"^(?:please\s+)?(?:build|create|make|design)\s+(?:(?:an?|the)\s+)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip(" .,:;")
    return text[:64].rstrip() or "Untitled board"


@dataclass(frozen=True)
class AgentResult:
    """One result for the stream-oriented terminal renderer."""

    view: dict[str, Any] | None = None
    lines: tuple[str, ...] = ()
    quit: bool = False


@dataclass
class AgentController:
    """State and service routing for the compact terminal agent."""

    service: ApplicationService
    timeout: float = 420.0
    project_id: str | None = None
    pending_name: str | None = None

    @property
    def provider_name(self) -> str:
        provider = getattr(self.service, "provider", None)
        value = getattr(provider, "provider_id", None)
        return value if isinstance(value, str) else "configured provider"

    def open_initial_project(self) -> AgentResult:
        if self.project_id is None:
            return AgentResult()
        view = self.service.open_project(self.project_id)
        self.project_id = _project_id(view)
        return AgentResult(view=view)

    def submit(self, text: str) -> AgentResult:
        clean = text.strip()
        if not clean:
            return AgentResult()
        if self.project_id is None:
            name = self.pending_name or project_name_from_request(clean)
            draft = self.service.create_draft(name)
            self.project_id = _project_id(draft)
            self.pending_name = None
            view = self.service.send_message(
                self.project_id,
                clean,
                timeout=self.timeout,
            )
            self.project_id = _project_id(view)
            return AgentResult(view=view)
        view = self.service.send_message(
            self.project_id,
            clean,
            timeout=self.timeout,
        )
        self.project_id = _project_id(view)
        return AgentResult(view=view)

    def command(self, text: str) -> AgentResult:
        command, _, argument = text[1:].partition(" ")
        command = command.casefold()
        argument = argument.strip()
        if command in {"q", "quit", "exit"}:
            return AgentResult(quit=True)
        if command in {"help", "?"}:
            return AgentResult(lines=_help_lines())
        if command in {"new", "n"}:
            self.project_id = None
            self.pending_name = argument or None
            question = START_QUESTION
            if self.pending_name:
                question = f"{question} Project: {self.pending_name}"
            return AgentResult(lines=(question,))
        if command in {"projects", "project", "p"}:
            if argument:
                return self._open(argument)
            return AgentResult(lines=_project_lines(self.service.list_projects()))
        if command in {"open", "resume"}:
            if not argument:
                raise ValidationError("/open requires a project id")
            return self._open(argument)
        if command in {"status", "s"}:
            return AgentResult(lines=_status_lines(self._current_view()))
        if command in {"confirm", "apply"}:
            current = self._current_view()
            status = _project(current).get("status")
            if status == "change_ready":
                view = self.service.apply_modification(self._require_project())
            else:
                view = self.service.confirm_project(
                    self._require_project(),
                    timeout=min(self.timeout, 3_600.0),
                )
            return AgentResult(view=view)
        if command == "validate":
            view = self.service.validate_project(
                self._require_project(),
                timeout=min(self.timeout, 3_600.0),
            )
            return AgentResult(view=view)
        if command == "undo":
            return AgentResult(
                view=self.service.undo_last_modification(self._require_project())
            )
        if command == "discard":
            return AgentResult(
                view=self.service.discard_modification(self._require_project())
            )
        if command == "release":
            view = self.service.build_release(
                self._require_project(),
                timeout=min(self.timeout, 3_600.0),
            )
            return AgentResult(view=view)
        raise ValidationError("unknown command; type /help")

    def _open(self, project_id: str) -> AgentResult:
        view = self.service.open_project(project_id)
        self.project_id = _project_id(view)
        self.pending_name = None
        return AgentResult(view=view)

    def _current_view(self) -> dict[str, Any]:
        return self.service.open_project(self._require_project())

    def _require_project(self) -> str:
        if self.project_id is None:
            raise ValidationError("start a board conversation first")
        return self.project_id


def interactive_agent(
    service: ApplicationService,
    *,
    project_id: str | None = None,
    initial_message: str | None = None,
    timeout: float = 420.0,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> int:
    """Run the small terminal agent against the authoritative application service."""

    controller = AgentController(service, timeout=timeout, project_id=project_id)
    try:
        initial = controller.open_initial_project()
    except CopperWrightError as exc:
        print(f"Error: {exc}", file=output_stream)
        return exc.exit_code
    if initial.view is not None:
        _write_result(initial, output_stream)

    if initial_message and initial_message.strip():
        result = _run_submission(controller, initial_message)
        _write_result(result, output_stream)
        if result.quit:
            return 0
    elif controller.project_id is None:
        print(START_QUESTION, file=output_stream)

    while True:
        output_stream.write(_prompt(controller.project_id))
        output_stream.flush()
        line = input_stream.readline()
        if not line:
            print(file=output_stream)
            return 0
        clean = line.strip()
        if not clean:
            continue
        result = _run_submission(controller, clean)
        _write_result(result, output_stream)
        if result.quit:
            return 0


def run_agent_command(
    *,
    workspace: str | Path | None,
    provider: str,
    project_id: str | None,
    initial_message: str | None,
    timeout: float,
) -> int:
    """Launch the Pi-style terminal entry point on an interactive terminal."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValidationError(
            "agent requires an interactive terminal; use `chat --json` for scripting"
        )
    service = ApplicationService(workspace, provider_name=provider)
    return interactive_agent(
        service,
        project_id=project_id,
        initial_message=initial_message,
        timeout=timeout,
    )


def _run_submission(controller: AgentController, text: str) -> AgentResult:
    try:
        return (
            controller.command(text)
            if text.startswith("/")
            else controller.submit(text)
        )
    except CopperWrightError as exc:
        return AgentResult(lines=(f"Error: {exc}",))


def _project(view: dict[str, Any]) -> dict[str, Any]:
    value = view.get("project")
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise ValidationError("application returned an invalid project view")
    return value


def _project_id(view: dict[str, Any]) -> str:
    return _project(view)["id"]


def _latest_assistant_text(view: dict[str, Any]) -> str | None:
    conversation = view.get("conversation")
    messages = conversation.get("messages") if isinstance(conversation, dict) else None
    if not isinstance(messages, list):
        return None
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _reply_lines(view: dict[str, Any]) -> tuple[str, ...]:
    project = _project(view)
    status = str(project.get("status", "unknown")).replace("_", " ")
    name = str(project.get("name", "Unnamed project"))
    project_id = project["id"]
    message = _latest_assistant_text(view) or "Project is ready for your request."
    return (f"[{status}] {name} ({project_id})", message)


def _status_lines(view: dict[str, Any]) -> tuple[str, ...]:
    project = _project(view)
    lines = [
        f"{project.get('name', 'Unnamed project')} ({project['id']})",
        f"status: {str(project.get('status', 'unknown')).replace('_', ' ')}",
    ]
    conversation = view.get("conversation")
    proposal = conversation.get("proposal") if isinstance(conversation, dict) else None
    brief = proposal.get("brief") if isinstance(proposal, dict) else None
    if isinstance(brief, dict):
        board = brief.get("board")
        if isinstance(board, dict):
            lines.append(
                "board: "
                f"{board.get('width_mm')} x {board.get('height_mm')} mm, "
                f"{board.get('layers')} layers"
            )
        identity = brief.get("identity")
        requested = (
            identity.get("requested_parts") if isinstance(identity, dict) else None
        )
        if isinstance(requested, list) and requested:
            lines.append("parts: " + ", ".join(str(item) for item in requested))
    return tuple(lines)


def _project_lines(projects: list[dict[str, Any]]) -> tuple[str, ...]:
    if not projects:
        return ("No local projects. " + START_QUESTION,)
    lines = ["Projects:"]
    for project in projects:
        project_id = project.get("id", "")
        status = str(project.get("status", "unknown")).replace("_", " ")
        name = project.get("name", "Unnamed project")
        lines.append(f"{project_id}  {status}  {name}")
    return tuple(lines)


def _help_lines() -> tuple[str, ...]:
    return (
        "Plain text continues the current board conversation.",
        "/new [name]     start a new board conversation",
        "/projects        list local projects",
        "/open ID         open a project",
        "/status          show the current project",
        "/confirm         generate or apply a reviewed change",
        "/validate        run validation",
        "/undo | /discard | /release",
        "/quit",
    )


def _prompt(project_id: str | None) -> str:
    return f"copperwright:{project_id or 'new'}> "


def _write_result(result: AgentResult, stream: TextIO) -> None:
    lines = _reply_lines(result.view) if result.view is not None else result.lines
    if not lines:
        return
    print(file=stream)
    for line in lines:
        print(line, file=stream)
