"""PCBDraft slash command surface for the interactive terminal.

The bare ``pcbdraft`` launch runs the vendored Hermes ``prompt_toolkit``
terminal.  That runtime ships ~50 built-in slash commands for messaging,
voice, kanban, billing, and other non-PCB concerns; this module owns the
pruned surface PCBDraft actually delivers:

* :data:`KEPT_HERMES_COMMANDS` — the Hermes built-ins PCBDraft keeps;
* :data:`PCBDRAFT_COMMANDS` / :data:`HANDLERS` — PCBDraft-owned commands
  (``/new``, ``/projects``, ``/project``, ``/open`` and the PCB workflow
  commands) backed by :class:`~pcbdraft.services.application.ApplicationService`
  and the repository authority;
* :func:`apply_command_surface` — rebuilds the vendored
  ``hermes_cli.commands`` registry in place so help, autocomplete, and
  dispatch expose only this surface.  Vendor files are never edited by hand.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pcbdraft.agent.hermes_tools import (
    get_current_project_id,
    get_service,
    set_current_project_id,
)
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.repository import current_repository
from pcbdraft.model.config import load_model_config

__all__ = (
    "HANDLERS",
    "KEPT_HERMES_COMMANDS",
    "PCBDRAFT_CATEGORY",
    "PCBDRAFT_COMMANDS",
    "apply_command_surface",
)

#: Hermes built-ins retained in the PCBDraft terminal surface.
KEPT_HERMES_COMMANDS = frozenset(
    {"status", "model", "goal", "stop", "retry", "undo", "quit", "help", "clear"}
)

#: Category label used by the merged /help display.
PCBDRAFT_CATEGORY = "PCB Project"

_REVIEW_LOG_LIMIT = 20


def handle_new(raw_args: str) -> str:
    """Create a new PCB project inside the configured repository.

    ``/new <name>`` is the PCB project creation entry: it creates the project
    record under ``<repository>/projects/`` and makes it the current project
    context for the PCB tools.  A bare ``/new`` shows usage and creates
    nothing.
    """

    name = raw_args.strip()
    if not name:
        return "Usage: /new <name> — create a new PCB project in the repository"
    service = get_service()
    view = service.create_draft(name)
    project = view["project"]
    set_current_project_id(str(project["id"]))
    return (
        f'✓ Created PCB project "{project["name"]}" ({project["id"]})\n'
        f"  location: {service.projects_root / str(project['id'])}\n"
        "  next step: describe the board you want (requirements, parts, size)."
    )


def handle_projects(raw_args: str) -> str:
    """List the PCB projects below the configured repository."""

    service = get_service()
    projects = service.list_projects()
    if not projects:
        return (
            f"No projects yet in {service.projects_root}.\n"
            "Use /new <name> to create your first PCB project."
        )
    lines = [f"PCB projects ({len(projects)}) in {service.projects_root}:"]
    for project in projects:
        lines.append(
            f"  • {project['id']}  [{project.get('status', '')}]  "
            f"{project.get('name', '')}"
        )
    lines.append("Use /open <id> to select one.")
    return "\n".join(lines)


def handle_project(raw_args: str) -> str:
    """Show the current PCB project repository or switch to a new one.

    With no argument the persisted repository is shown.  With a directory the
    repository pointer is updated and the current project context is cleared;
    the Hermes home stays under the PCBDraft config directory and never moves
    into the repository.
    """

    directory = raw_args.strip()
    if not directory:
        repository = current_repository()
        return (
            f"PCB project repository: {repository.root} ({repository.source})\n"
            f"  projects live in {repository.projects_root}"
        )
    service = get_service()
    repository = service.set_repository(directory)
    set_current_project_id(None)
    return (
        f"✓ PCB project repository switched to {repository.root}\n"
        f"  new projects will be created in {repository.projects_root}"
    )


def handle_open(raw_args: str) -> str:
    """Open an existing PCB project by id and make it the current context."""

    project_id = raw_args.strip()
    if not project_id:
        return "Usage: /open <id> — open an existing PCB project"
    view = get_service().open_project(project_id)
    project = view["project"]
    set_current_project_id(str(project["id"]))
    return (
        f'✓ Opened "{project["name"]}" ({project["id"]}) — '
        f"status: {project['status']}\n"
        "  next step: describe the change you want, or /review for a summary."
    )


def handle_connect(raw_args: str) -> str:
    """Show the active model provider connection."""

    config = load_model_config()
    provider = config.active
    if provider is None:
        return (
            "No model provider connected.\n"
            f"Edit the PCBDraft config file at {config.path} to register an "
            "OpenAI-compatible service, then rerun /connect."
        )
    model = config.active_model or (provider.models[0] if provider.models else "")
    lines = [
        f"Active model: {provider.name} ({provider.id})",
        f"  model: {model}",
        f"  base_url: {provider.base_url}",
    ]
    if provider.docs_url:
        lines.append(f"  docs: {provider.docs_url}")
    return "\n".join(lines)


def handle_review(raw_args: str) -> str:
    """Summarize the current project's engineering state."""

    project_id = _resolve_project_id(raw_args)
    view = get_service().open_project(project_id)
    project = view["project"]
    state = view.get("state") or {}
    lines = [
        f"{project['name']} ({project['id']}) — status: {project['status']}",
        (
            f"  revision: {state.get('revision', 0)}   "
            f"design revision: {state.get('design_revision', 0)}"
        ),
    ]
    validation = state.get("last_validation")
    if isinstance(validation, dict):
        verdict = "passing" if validation.get("candidate_ready") else "not passing"
        lines.append(f"  last validation: {verdict}")
    else:
        lines.append("  last validation: none")
    lines.append(
        f"  last preview: {'present' if isinstance(state.get('last_preview'), dict) else 'none'}"
    )
    lines.append(
        f"  last release: {'present' if isinstance(state.get('last_release'), dict) else 'none'}"
    )
    return "\n".join(lines)


def handle_confirm(raw_args: str) -> str:
    """Approve the current engineering candidate for generation."""

    project_id = _resolve_project_id(raw_args)
    view = get_service().confirm_project(project_id, timeout=180.0)
    project = view["project"]
    return f"✓ Confirmed; project status: {project['status']}"


def handle_discard(raw_args: str) -> str:
    """Discard the staged semantic change without touching the design."""

    project_id = _resolve_project_id(raw_args)
    view = get_service().discard_modification(project_id)
    project = view["project"]
    return f"✓ Staged change discarded; project status: {project['status']}"


def handle_logs(raw_args: str) -> str:
    """Show the recent structured events of the current project."""

    project_id = _resolve_project_id(raw_args)
    events = get_service().events(project_id)
    if not events:
        return f"No events recorded yet for {project_id}."
    lines = [f"Recent events for {project_id}:"]
    for event in events[-_REVIEW_LOG_LIMIT:]:
        lines.append(
            f"  #{event.get('sequence', '?')} {event.get('kind', '?')}: "
            f"{event.get('message', '')}"
        )
    return "\n".join(lines)


def handle_validate(raw_args: str) -> str:
    """Run the real engineering validation on the current project."""

    project_id = _resolve_project_id(raw_args)
    view = get_service().validate_project(project_id, timeout=180.0)
    project = view["project"]
    validation = (view.get("artifacts") or {}).get("validation")
    candidate_ready = bool(
        isinstance(validation, dict) and validation.get("candidate_ready")
    )
    return (
        f"Validation complete: candidate_ready={candidate_ready}; "
        f"project status: {project['status']}"
    )


def handle_release(raw_args: str) -> str:
    """Build a manufacturing-candidate release of the current project."""

    project_id = _resolve_project_id(raw_args)
    view = get_service().build_release(project_id, timeout=300.0)
    project = view["project"]
    return (
        f"✓ Release built for {project['name']} ({project['id']})\n"
        f"  project status: {project['status']}"
    )


def _resolve_project_id(raw_args: str) -> str:
    """Resolve an explicit argument, else the current project context."""

    explicit = raw_args.strip()
    project_id = explicit or get_current_project_id()
    if not project_id:
        raise PCBDraftError("no project selected; use /new <name> or /open <id> first")
    return project_id


#: The PCBDraft-owned command table: (name, description, args_hint, handler).
PCBDRAFT_COMMANDS: tuple[tuple[str, str, str, Callable[[str], str]], ...] = (
    ("new", "Create a new PCB project in the repository", "<name>", handle_new),
    ("projects", "List PCB projects in the repository", "", handle_projects),
    (
        "project",
        "Show or switch the PCB project repository",
        "[directory]",
        handle_project,
    ),
    ("open", "Open an existing PCB project by id", "<id>", handle_open),
    ("connect", "Show the active model provider connection", "", handle_connect),
    ("review", "Summarize the current project state", "[id]", handle_review),
    ("confirm", "Approve the current candidate for generation", "[id]", handle_confirm),
    ("discard", "Discard the staged semantic change", "[id]", handle_discard),
    ("logs", "Show recent project events", "[id]", handle_logs),
    ("validate", "Run engineering validation", "[id]", handle_validate),
    ("release", "Build a manufacturing-candidate release", "[id]", handle_release),
)

#: Dispatch map used by the terminal's process_command wrapper.
HANDLERS: dict[str, Callable[[str], str]] = {
    name: handler for name, _description, _args_hint, handler in PCBDRAFT_COMMANDS
}


def apply_command_surface() -> None:
    """Prune the vendored Hermes command registry to the PCBDraft surface.

    Rebuilds ``COMMAND_REGISTRY`` (slice assignment), ``_COMMAND_LOOKUP``
    (rebind), and the derived dicts ``COMMANDS`` / ``COMMANDS_BY_CATEGORY`` /
    ``SUBCOMMANDS`` (in-place mutation so consumers holding from-import
    references see the same objects), then rebinds the derived frozensets.
    Mirrors the module-level derivation in ``vendor/hermes/hermes_cli/
    commands.py`` and never edits that file.
    """

    from hermes_cli import commands

    kept = [
        command
        for command in commands.COMMAND_REGISTRY
        if command.name in KEPT_HERMES_COMMANDS
    ]
    owned = [
        commands.CommandDef(
            name=name,
            description=description,
            category=PCBDRAFT_CATEGORY,
            args_hint=args_hint,
            busy_policy="dispatch",
        )
        for name, description, args_hint, _handler in PCBDRAFT_COMMANDS
    ]
    commands.COMMAND_REGISTRY[:] = [*kept, *owned]
    commands._COMMAND_LOOKUP = commands._build_command_lookup()
    _rebuild_derived_lookups(commands)


def _rebuild_derived_lookups(commands: Any) -> None:
    """Rebuild the derived command lookups exactly like the vendor module."""

    flat: dict[str, str] = {}
    for command in commands.COMMAND_REGISTRY:
        if not command.gateway_only:
            flat[f"/{command.name}"] = commands._build_description(command)
            for alias in command.aliases:
                flat[f"/{alias}"] = f"{command.description} (alias for /{command.name})"
    commands.COMMANDS.clear()
    commands.COMMANDS.update(flat)

    by_category: dict[str, dict[str, str]] = {}
    for command in commands.COMMAND_REGISTRY:
        if not command.gateway_only:
            category = by_category.setdefault(command.category, {})
            category[f"/{command.name}"] = flat[f"/{command.name}"]
            for alias in command.aliases:
                category[f"/{alias}"] = flat[f"/{alias}"]
    commands.COMMANDS_BY_CATEGORY.clear()
    commands.COMMANDS_BY_CATEGORY.update(by_category)

    subcommands: dict[str, list[str]] = {}
    for command in commands.COMMAND_REGISTRY:
        if command.subcommands:
            subcommands[f"/{command.name}"] = list(command.subcommands)
    pipe_subs = re.compile(r"[a-z]+(?:\|[a-z]+)+")
    for command in commands.COMMAND_REGISTRY:
        key = f"/{command.name}"
        if key in subcommands or not command.args_hint:
            continue
        match = pipe_subs.search(command.args_hint)
        if match:
            subcommands[key] = match.group(0).split("|")
    commands.SUBCOMMANDS.clear()
    commands.SUBCOMMANDS.update(subcommands)

    commands.GATEWAY_KNOWN_COMMANDS = frozenset(
        name
        for command in commands.COMMAND_REGISTRY
        if not command.cli_only or command.gateway_config_gate
        for name in (command.name, *command.aliases)
    )
    commands.ACTIVE_SESSION_BYPASS_COMMANDS = frozenset(
        command.name
        for command in commands.COMMAND_REGISTRY
        if command.busy_policy != "reject"
    )
