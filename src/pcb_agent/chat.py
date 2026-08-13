"""Interactive and scriptable terminal conversation for CopperWright."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .application import ApplicationService
from .errors import PcbAgentError, ValidationError


def _print_project(view: dict[str, Any], stream: TextIO) -> None:
    project = view["project"]
    print(
        f"\n{project['name']} [{project['id']}] — {project['status']}",
        file=stream,
    )
    proposal = view["conversation"].get("proposal")
    if isinstance(proposal, dict):
        scope = proposal.get("scope", {})
        print(f"Scope: {scope.get('decision', 'unknown')}", file=stream)
        if proposal.get("clarifications"):
            print(proposal["clarifications"][0]["question"], file=stream)
        brief = proposal.get("brief")
        if isinstance(brief, dict):
            board = brief["board"]
            print(
                f"Board: {board['width_mm']} × {board['height_mm']} mm, "
                f"{board['layers']} layers",
                file=stream,
            )
            print(
                f"BOM: {sum(item['quantity'] for item in brief['bom'])} placements / "
                f"{len(brief['bom'])} lines; constraints: {len(brief['constraints'])}",
                file=stream,
            )
            print("Assumptions:", file=stream)
            for assumption in brief["assumptions"]:
                print(f"  - {assumption}", file=stream)
            print(
                "External gates: human engineering review, physical build/test, "
                "and production sign-off.",
                file=stream,
            )
    if view.get("active_change"):
        change = view["active_change"]
        summary = change["diff"]["summary"]
        print(
            "Semantic change ready: "
            f"{summary['objects_added']} added, {summary['objects_removed']} removed, "
            f"{summary['objects_modified']} modified.",
            file=stream,
        )
    design = view.get("design")
    if isinstance(design, dict):
        print(f"KiCad project: {design['root']}", file=stream)
        for key in ("schematic", "board", "kicad_project"):
            print(f"  {key}: {design['files'][key]}", file=stream)
    validation = view.get("artifacts", {}).get("validation")
    if isinstance(validation, dict):
        print(
            "Validation: "
            f"candidate={'yes' if validation['candidate_ready'] else 'no'}, "
            f"production={'yes' if validation['production_ready'] else 'no'} "
            "(production is never self-claimed)",
            file=stream,
        )
        print(
            "  "
            + ", ".join(
                f"{level['level']}={level['state']}:{level['outcome']}"
                for level in validation.get("levels", [])
            ),
            file=stream,
        )
    preview = view.get("artifacts", {}).get("previews")
    if isinstance(preview, dict):
        print("Previews:", file=stream)
        for key, path in sorted(preview["files"].items()):
            print(f"  {key}: {path}", file=stream)
    release = view.get("artifacts", {}).get("release")
    if isinstance(release, dict):
        print(f"Release archive: {release['archive']}", file=stream)
        print(
            f"Offline verification: {release['offline_verification']['verified']}",
            file=stream,
        )


def run_chat_command(
    *,
    workspace: str | Path | None,
    provider: str,
    project_id: str | None,
    new_name: str | None,
    message: str | None,
    assume_yes: bool,
    undo: bool,
    validate: bool,
    release: bool,
    list_only: bool,
    as_json: bool,
    timeout: float,
) -> int:
    service = ApplicationService(workspace, provider_name=provider)
    if list_only:
        value: Any = {"projects": service.list_projects()}
    else:
        if new_name:
            if not message:
                raise ValidationError("--new requires --message")
            value = service.create_project(new_name, message)
            project_id = value["project"]["id"]
        elif not project_id:
            if sys.stdin.isatty():
                return interactive_chat(service)
            raise ValidationError("noninteractive chat requires --new or --project")
        else:
            value = service.open_project(project_id)
            if message:
                value = service.send_message(project_id, message, timeout=timeout)
        assert project_id is not None
        if undo:
            value = service.undo_last_modification(project_id)
        if validate:
            value = service.validate_project(project_id, timeout=timeout)
        if release:
            value = service.build_release(project_id, timeout=timeout)
        if assume_yes:
            status = value["project"]["status"]
            if status == "awaiting_confirmation":
                value = service.confirm_project(project_id, timeout=timeout)
            elif status == "change_ready":
                value = service.apply_modification(project_id)
            elif not any((undo, validate, release)):
                raise ValidationError(
                    "--yes was supplied, but no generation or change confirmation is pending"
                )
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif list_only:
        for item in value["projects"]:
            print(f"{item['id']}\t{item['status']}\t{item['name']}")
    else:
        _print_project(value, sys.stdout)
    return 0


def interactive_chat(
    service: ApplicationService,
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> int:
    """Run a small command-aware REPL suitable for SSH terminals."""

    print("CopperWright — local conversational PCB design", file=output_stream)
    diagnostic = service.diagnostics()
    provider = diagnostic["provider"]
    print(
        f"Provider: {provider['id']} ({'available' if provider['available'] else 'unavailable'})",
        file=output_stream,
    )
    print(
        "Type /help for commands. Credentials are never accepted here.",
        file=output_stream,
    )
    current_id: str | None = None
    while True:
        prompt = f"copperwright:{current_id or 'no-project'}> "
        output_stream.write(prompt)
        output_stream.flush()
        line = input_stream.readline()
        if not line:
            print(file=output_stream)
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            if line in {"/quit", "/exit"}:
                return 0
            if line == "/help":
                print(
                    "/new NAME, /open ID, /projects, /status, /confirm, /discard, "
                    "/validate, /undo, /release, /quit",
                    file=output_stream,
                )
                continue
            if line == "/projects":
                for item in service.list_projects():
                    print(
                        f"{item['id']}\t{item['status']}\t{item['name']}",
                        file=output_stream,
                    )
                continue
            if line.startswith("/new "):
                name = line[5:].strip()
                output_stream.write("What should the board do? ")
                output_stream.flush()
                request = input_stream.readline().strip()
                view = service.create_project(name, request)
                current_id = view["project"]["id"]
                _print_project(view, output_stream)
                continue
            if line.startswith("/open "):
                current_id = line[6:].strip()
                _print_project(service.open_project(current_id), output_stream)
                continue
            if current_id is None:
                print("Create or open a project first.", file=output_stream)
                continue
            if line == "/status":
                view = service.open_project(current_id)
            elif line == "/confirm":
                current = service.open_project(current_id)
                if current["project"]["status"] == "awaiting_confirmation":
                    view = service.confirm_project(current_id)
                elif current["project"]["status"] == "change_ready":
                    view = service.apply_modification(current_id)
                else:
                    raise ValidationError("nothing is awaiting confirmation")
            elif line == "/discard":
                view = service.discard_modification(current_id)
            elif line == "/validate":
                view = service.validate_project(current_id)
            elif line == "/undo":
                view = service.undo_last_modification(current_id)
            elif line == "/release":
                view = service.build_release(current_id)
            elif line.startswith("/"):
                raise ValidationError("unknown chat command; type /help")
            else:
                view = service.send_message(current_id, line)
            _print_project(view, output_stream)
        except PcbAgentError as exc:
            print(f"Error: {exc}", file=output_stream)
