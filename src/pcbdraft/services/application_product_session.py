"""Product-session terminal receipt recording.

The host application remains the project and progress authority. It supplies
adapters for receipt identity, locking, terminal classification, timestamps,
and immutable storage so historical patch points remain available without a
reverse import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.task_contract import evaluate_task_coverage
from pcbdraft.services.application_project_store import APP_FILE_LIMIT
from pcbdraft.services.managed import open_managed_project
from pcbdraft.services.progress import (
    ProcessStatus,
    ScopedTaskOutcome,
    scoped_task_status,
)


class ApplicationProductSessionMixin:
    """Record one immutable terminal outcome for a product session turn."""

    @staticmethod
    def _product_session_process_status(value: ProcessStatus | str) -> ProcessStatus:
        raise NotImplementedError

    @staticmethod
    def _product_session_validate_receipt_id(value: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _product_session_receipt_id(session_id: str, turn_id: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _product_session_resource_lock(root: Path, locks_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _product_session_load_json(path: Path, limit: int) -> Any:
        raise NotImplementedError

    @staticmethod
    def _product_session_parse_receipt(value: Any) -> Any:
        raise NotImplementedError

    @staticmethod
    def _product_session_stage_projection(
        stage: Any,
        release_gate_passed: bool,
        blockers: tuple[str, ...],
    ) -> Any:
        raise NotImplementedError

    @staticmethod
    def _product_session_terminal_outcome(
        *,
        process_status: ProcessStatus,
        requested_reason: str | None,
        stage: Any,
    ) -> tuple[Any, str]:
        raise NotImplementedError

    @staticmethod
    def _product_session_timestamp() -> str:
        raise NotImplementedError

    @staticmethod
    def _product_session_receipt(*args: Any) -> Any:
        raise NotImplementedError

    @staticmethod
    def _product_session_store(project_root: Path, receipt: Any) -> Path:
        raise NotImplementedError

    def record_product_session_terminal(
        self,
        project_id: str,
        *,
        session_id: str,
        turn_id: str,
        process_status: ProcessStatus | str,
        termination_reason: str | None = None,
        receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """Write the one PCB-level outcome shared by durable and Hermes sessions."""

        try:
            process = self._product_session_process_status(process_status)
        except ValueError as exc:
            raise ValidationError("product session process status is invalid") from exc
        resolved_receipt_id = (
            self._product_session_validate_receipt_id(receipt_id)
            if receipt_id is not None
            else self._product_session_receipt_id(session_id, turn_id)
        )
        project = self._open(project_id)
        with self._product_session_resource_lock(project.root, self.locks_root):
            project = self._open(project_id)
            existing_path = (
                project.root / "product-sessions" / f"{resolved_receipt_id}.json"
            )
            if existing_path.is_file() and not existing_path.is_symlink():
                existing = self._product_session_parse_receipt(
                    self._product_session_load_json(existing_path, APP_FILE_LIMIT)
                )
                if (
                    existing.project_id != project_id
                    or existing.session_id != session_id
                    or existing.turn_id != turn_id
                ):
                    raise ValidationError(
                        "product session receipt identity is already bound"
                    )
                retained_stage = self._product_session_stage_projection(
                    existing.stage_reached,
                    existing.release_gate_passed,
                    () if existing.release_gate_passed else ("retained_terminal",),
                )
                requested_release_outcome, requested_termination = (
                    self._product_session_terminal_outcome(
                        process_status=process,
                        requested_reason=termination_reason,
                        stage=retained_stage,
                    )
                )
                if (
                    existing.process_status is not process
                    or existing.release_outcome is not requested_release_outcome
                    or existing.termination_reason != requested_termination
                ):
                    raise ValidationError(
                        "product session terminal receipt facts conflict"
                    )
                result = existing.to_dict()
                result["artifact"] = existing_path.relative_to(project.root).as_posix()
                return result
            progress, stage = self._current_progress_and_stage(project)
            release_outcome, reason = self._product_session_terminal_outcome(
                process_status=process,
                requested_reason=termination_reason,
                stage=stage,
            )
            if project.design_root.is_dir() and not project.design_root.is_symlink():
                managed = open_managed_project(project.design_root)
                task_coverage = evaluate_task_coverage(
                    managed.design,
                    project.state.get("last_validation"),
                    design_revision=int(project.state["design_revision"]),
                )
                coverage_outcome = ScopedTaskOutcome(task_coverage["outcome"])
            else:
                coverage_outcome = ScopedTaskOutcome.INCOMPLETE
            scoped_outcome, scoped_evidence = scoped_task_status(
                process_status=process,
                release_outcome=release_outcome,
                termination_reason=reason,
                release_gate_passed=stage.release_gate_passed,
                task_coverage_outcome=coverage_outcome,
            )
            receipt = self._product_session_receipt(
                resolved_receipt_id,
                project_id,
                session_id,
                turn_id,
                self._product_session_timestamp(),
                process,
                release_outcome,
                reason,
                stage.stage,
                stage.release_gate_passed,
                progress.source_revision,
                progress,
                scoped_outcome,
                scoped_evidence,
            )
            path = self._product_session_store(project.root, receipt)
            result = receipt.to_dict()
            result["artifact"] = path.relative_to(project.root).as_posix()
            return result
