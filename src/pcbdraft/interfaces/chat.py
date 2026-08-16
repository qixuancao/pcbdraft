"""Parameterized terminal conversation actions for PCBDraft."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from pcbdraft.core.errors import ValidationError
from pcbdraft.services.application import ApplicationService


def _print_project(view: dict[str, Any], stream: TextIO) -> None:
    project = view["project"]
    print(
        f"\n{project['name']} [{project['id']}] — {project['status']}",
        file=stream,
    )
    proposal = view["conversation"].get("proposal")
    if isinstance(proposal, dict):
        scope = proposal.get("scope", {})
        for warning in scope.get("warnings", []):
            print(f"Warning: {warning}", file=stream)
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
                f"Parts: {sum(item['quantity'] for item in brief['bom'])} placements / "
                f"{len(brief['bom'])} types; board rules: {len(brief['constraints'])}",
                file=stream,
            )
            plan_review = brief.get("plan_review")
            if isinstance(plan_review, dict):
                summary = plan_review.get("summary", {})
                attention = summary.get("attention_required", 0)
                if isinstance(attention, int) and attention > 0:
                    print(
                        f"Topology warnings: {attention}; generation remains available.",
                        file=stream,
                    )
                    for finding in plan_review.get("findings", []):
                        if (
                            isinstance(finding, dict)
                            and finding.get("outcome") != "pass"
                        ):
                            print(f"  - {finding.get('summary')}", file=stream)
            identity = brief.get("identity", {})
            requested_parts = identity.get("requested_parts", [])
            if requested_parts:
                print(
                    "Named parts: "
                    + ", ".join(requested_parts)
                    + " (preserved for planning)",
                    file=stream,
                )
            print("Assumptions:", file=stream)
            for assumption in brief["assumptions"]:
                print(f"  - {assumption}", file=stream)
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
    attempts = view.get("attempts", [])
    if attempts:
        latest = attempts[0]
        print(
            f"Latest generation attempt: {latest['status']} ({latest['phase']})",
            file=stream,
        )
        print(f"  retained files: {latest['root']}", file=stream)
        if latest.get("error"):
            print(f"  failure: {latest['error']}", file=stream)
    validation = view.get("artifacts", {}).get("validation")
    if isinstance(validation, dict):
        print(
            "Checks: "
            + ("passed" if validation["candidate_ready"] else "findings remain")
            + "; no electrical, regulatory, or manufacturing claim",
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
    if not list_only and not new_name and not project_id:
        raise ValidationError(
            "chat requires --new, --project, or --list; "
            "run `pcbdraft` for the terminal interface"
        )
    service = ApplicationService(workspace, provider_name=provider)
    if list_only:
        value: Any = {"projects": service.list_projects()}
    else:
        if new_name:
            if not message:
                raise ValidationError("--new requires --message")
            value = service.create_project(new_name, message)
            project_id = value["project"]["id"]
        else:
            if project_id is None:
                raise ValidationError("chat project id is required")
            value = service.open_project(project_id)
            if message:
                value = service.send_message(project_id, message, timeout=timeout)
        if project_id is None:
            raise ValidationError("chat project id is required")
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
