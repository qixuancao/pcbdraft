"""Project confirmation and first native-generation workflow.

ApplicationService remains the project mutation and composition authority.
It provides a per-call runtime whose functions resolve historical application
patch points late, while this mixin owns confirmation sequencing and result
selection without importing the coordinator back.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConfirmationRuntime:
    """Late-bound host capabilities and storage contract for confirmation."""

    validation_error: Callable[[str], Exception]
    open_managed_project: Callable[..., Any]
    request_from_dict: Callable[[Any], Any]
    plan_from_dict: Callable[[Any], Any]
    design_from_dict: Callable[[Any], Any]
    graph_load: Callable[[Path], Any]
    load_json_limited: Callable[[Path, int], Any]
    resource_lock: Callable[..., Any]
    timestamp: Callable[[], str]
    new_run_id: Callable[[], str]
    make_directory: Callable[[Path], Path]
    atomic_write_json: Callable[[Path, Any], None]
    materialize_managed_design: Callable[..., Any]
    sanitize_secret_text: Callable[[str], str]
    app_file_limit: int
    pending_request_name: str
    pending_plan_name: str
    pending_design_name: str
    pending_parts_name: str
    attempt_schema: str
    attempt_version: int


class ApplicationConfirmationMixin:
    """Confirm one prepared design and project its generation outcome."""

    @staticmethod
    def _confirmation_runtime() -> ConfirmationRuntime:
        raise NotImplementedError

    def confirm_project(
        self,
        project_id: str,
        *,
        validate: bool = True,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        runtime = self._confirmation_runtime()
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="generation confirmation"
        )
        if project.state["status"] not in {
            "awaiting_confirmation",
            "generation_failed",
            "interrupted",
            "generated",
        }:
            raise runtime.validation_error(
                "project is not awaiting generation confirmation"
            )
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            runtime.open_managed_project(project.design_root).assert_synchronized()
            preview = self.generate_project_previews(
                project_id,
                timeout=timeout,
                expected_revision=expected_revision,
            )
            if validate:
                return self.validate_project(
                    project_id,
                    timeout=timeout,
                    expected_revision=int(preview["state"]["revision"]),
                )
            return preview
        request = runtime.request_from_dict(
            runtime.load_json_limited(
                project.root / runtime.pending_request_name, runtime.app_file_limit
            )
        )
        plan = runtime.plan_from_dict(
            runtime.load_json_limited(
                project.root / runtime.pending_plan_name, runtime.app_file_limit
            )
        )
        design = runtime.design_from_dict(
            runtime.load_json_limited(
                project.root / runtime.pending_design_name, runtime.app_file_limit
            )
        )
        graph = runtime.graph_load(project.root / runtime.pending_parts_name)
        if design.design_id != request.design_id or plan.design_id != request.design_id:
            raise runtime.validation_error(
                "pending request, plan, and semantic design identities differ"
            )
        graph.assert_design(
            design,
            check_libraries=True,
            allow_provisional=design.metadata.get("assurance") == "provisional",
        )
        with runtime.resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise runtime.validation_error("project changed before confirmation")
            if current.design_root.exists() or current.design_root.is_symlink():
                raise runtime.validation_error("confirmed project already has a design")
            state = current.state
            state["status"] = "generating"
            state["revision"] += 1
            state["updated_at"] = runtime.timestamp()
            self._event(
                state,
                current.root,
                "generation.started",
                "Generating native KiCad project",
            )
            self._write_records(current.root, state, current.conversation)
            expected_revision = state["revision"]
        attempt_dir: Path | None = None
        attempt_record: dict[str, Any] | None = None
        try:
            attempt_id = runtime.new_run_id()
            attempt_dir = runtime.make_directory(
                runtime.make_directory(project.root / "attempts") / attempt_id
            )
            attempt_record = {
                "schema": runtime.attempt_schema,
                "version": runtime.attempt_version,
                "id": attempt_id,
                "status": "running",
                "phase": "native_generation",
                "runtime": "agent_plan_v1",
                "assurance": "unknown",
                "started_at": runtime.timestamp(),
                "completed_at": None,
                "part_ids": [],
                "requested_parts": list(request.requested_parts),
                "files": {
                    "request": "request.json",
                    "plan": "circuit-plan.json",
                    "semantic_ir": "design.pcbir.json",
                    "part_catalog": "parts.pcbdraft.json",
                    "retained_native": None,
                },
                "error": None,
            }
            runtime.atomic_write_json(attempt_dir / "request.json", request.to_dict())
            runtime.atomic_write_json(attempt_dir / "circuit-plan.json", plan.to_dict())
            runtime.atomic_write_json(
                attempt_dir / "design.pcbir.json", design.to_dict()
            )
            runtime.atomic_write_json(
                attempt_dir / "parts.pcbdraft.json", graph.to_dict()
            )
            runtime.atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            attempt_record["assurance"] = str(
                design.metadata.get("assurance", "provisional")
            )
            attempt_record["part_ids"] = sorted(
                {component.part_id for component in design.components}
            )
            runtime.atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            generated = runtime.materialize_managed_design(
                request,
                design,
                project.design_root,
                graph=graph,
                plan=plan,
                retain_failed_attempt=attempt_dir / "native",
            )
        except BaseException as exc:
            if attempt_dir is not None and attempt_record is not None:
                attempt_record["status"] = "failed"
                attempt_record["phase"] = "failed"
                attempt_record["completed_at"] = runtime.timestamp()
                attempt_record["error"] = runtime.sanitize_secret_text(str(exc))[:2048]
                if (attempt_dir / "native").is_dir():
                    attempt_record["files"]["retained_native"] = "native"
                runtime.atomic_write_json(attempt_dir / "attempt.json", attempt_record)
            self._record_failure(
                project_id,
                expected_revision,
                "generation_failed",
                "generation.failed",
                str(exc),
            )
            raise
        if attempt_dir is not None and attempt_record is not None:
            attempt_record["status"] = "completed"
            attempt_record["phase"] = "completed"
            attempt_record["completed_at"] = runtime.timestamp()
            runtime.atomic_write_json(attempt_dir / "attempt.json", attempt_record)
        with runtime.resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise runtime.validation_error(
                    "project changed while generation was running"
                )
            state = current.state
            conversation = current.conversation
            state["status"] = "generated"
            state["design_revision"] = 1
            state["revision"] += 1
            state["updated_at"] = runtime.timestamp()
            self._append_message(
                conversation,
                "assistant",
                "generation",
                "Generated a native KiCad schematic and routed PCB. Validation results, when run, are reported separately.",
                data={
                    "design_content_hash": generated.project.design.content_hash(),
                    "routing_state": generated.pcb.routing.state,
                    "unrouted": list(generated.pcb.routing.unrouted),
                },
            )
            self._event(
                state,
                current.root,
                "generation.complete",
                "Native KiCad schematic and routed PCB generated",
            )
            self._write_records(current.root, state, conversation)
            expected_revision = int(state["revision"])
        preview = self.generate_project_previews(
            project_id,
            timeout=timeout,
            expected_revision=expected_revision,
        )
        if validate:
            return self.validate_project(
                project_id,
                timeout=timeout,
                expected_revision=int(preview["state"]["revision"]),
            )
        return preview
