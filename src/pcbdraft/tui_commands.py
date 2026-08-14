"""Command catalog for PCBDraft interactive terminal clients."""

from __future__ import annotations

from dataclasses import dataclass


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
    SlashCommand("/review", "/review", "review plan or staged diff"),
    SlashCommand("/logs", "/logs [on|off]", "expand activity details", True),
    SlashCommand("/model", "/model [provider]", "show or switch planner", True),
    SlashCommand("/stop", "/stop", "stop the active turn"),
    SlashCommand("/retry", "/retry", "retry last failed job"),
    SlashCommand("/confirm", "/confirm", "confirm ready work"),
    SlashCommand("/validate", "/validate", "run validation"),
    SlashCommand("/undo", "/undo", "undo last change"),
    SlashCommand("/discard", "/discard", "discard staged change"),
    SlashCommand("/release", "/release", "build release evidence"),
    SlashCommand("/quit", "/quit", "quit PCBDraft"),
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
