"""Application-level validation and generated-project preview workflows.

The host application remains the project repository and record authority. It
supplies adapters for native project inspection, validation and preview
execution, receipt loading, locking, run IDs, and timestamps so historical
patch points remain available without a reverse import.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.core.project import sha256_file
from pcbdraft.domain.task_contract import evaluate_task_coverage


def _latest_drc_baseline(
    project: Any,
    *,
    load_record: Callable[[Path, int], Any],
    file_limit: int,
    record_error: type[BaseException],
) -> tuple[Path, int, str] | None:
    """Locate the newest complete application validation as a DRC baseline."""

    root = project.root / "validation"
    if not root.is_dir() or root.is_symlink():
        return None
    for directory in sorted(root.iterdir(), reverse=True)[:100]:
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            receipt = load_record(directory / "receipt.json", file_limit)
        except record_error:
            continue
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("schema") != "pcbdraft-validation-receipt"
            or receipt.get("status") != "complete"
            or not isinstance(receipt.get("source_design_revision"), int)
            or not isinstance(receipt.get("design_content_hash"), str)
            or not isinstance(receipt.get("complete_rule_evidence"), Mapping)
        ):
            continue
        name = receipt["complete_rule_evidence"].get("drc")
        if not isinstance(name, str) or Path(name).name != name:
            continue
        return (
            directory / name,
            int(receipt["source_design_revision"]),
            str(receipt["design_content_hash"]),
        )
    return None


class ApplicationValidationMixin:
    """Run aggregate validation and generate browser-safe project previews."""

    @staticmethod
    def _validation_open_managed_project(design_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _validation_validate_managed_project(
        managed: Any,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError

    @staticmethod
    def _validation_generate_previews(
        managed: Any,
        output: Path,
        *,
        timeout: float,
    ) -> Any:
        raise NotImplementedError

    @staticmethod
    def _validation_load_json_limited(path: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _validation_new_run_id() -> str:
        raise NotImplementedError

    @staticmethod
    def _validation_resource_lock(root: Path, locks_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _validation_timestamp() -> str:
        raise NotImplementedError

    def validate_project(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Run the real layered runtime validation and attach its evidence."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="validation"
        )
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "released",
            "interrupted",
            "release_failed",
        }:
            raise ValidationError("project must be generated before validation")
        managed = self._validation_open_managed_project(project.design_root)
        managed.assert_synchronized()
        baseline = self._latest_drc_baseline(project)
        source_design_revision = int(project.state["design_revision"])
        run_id = self._validation_new_run_id()
        output = project.root / "validation" / run_id
        with self._validation_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before validation")
            state = current.state
            state["status"] = "validating"
            state["revision"] += 1
            state["updated_at"] = self._validation_timestamp()
            self._event(
                state, current.root, "validation.started", "Running configured checks"
            )
            self._write_records(current.root, state, current.conversation)
            expected_revision = state["revision"]
        try:
            result = self._validation_validate_managed_project(
                managed,
                output=output,
                timeout=timeout,
                canonical_revision=expected_revision,
                design_revision=source_design_revision,
                baseline_drc_evidence=baseline[0] if baseline is not None else None,
                expected_baseline_design_revision=(
                    baseline[1] if baseline is not None else None
                ),
                expected_baseline_content_hash=(
                    baseline[2] if baseline is not None else None
                ),
            )
            self._bind_aggregate_validation_revision(
                output,
                managed.design.content_hash(),
                int(project.state["design_revision"]),
            )
            report = self._validation_load_json_limited(result.report_path)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "validation_failed",
                "validation.failed",
                str(exc),
            )
            raise
        relative_report = result.report_path.relative_to(project.root).as_posix()
        summary = {
            "run_id": run_id,
            "report": relative_report,
            "report_sha256": result.report_sha256,
            "candidate_ready": result.candidate_ready,
            "production_evidence_complete": result.production_evidence_complete,
            "production_ready": result.production_ready,
            "production_claimed": False,
            "source_design_revision": source_design_revision,
            "source_content_hash": managed.design.content_hash(),
            "design_content_hash": managed.design.content_hash(),
            "completed_at": self._validation_timestamp(),
            "erc_evidence": result.erc_evidence_path.relative_to(
                project.root
            ).as_posix(),
            "drc_evidence": result.drc_evidence_path.relative_to(
                project.root
            ).as_posix(),
            "drc_delta": result.drc_delta_path.relative_to(project.root).as_posix(),
            "assurance": str(managed.design.metadata.get("assurance", "verified")),
            "levels": report["levels"],
        }
        if hasattr(managed.design, "requirements"):
            task_coverage = evaluate_task_coverage(
                managed.design,
                summary,
                design_revision=source_design_revision,
            )
            task_coverage_path = output / "task-coverage.json"
            atomic_write_json(task_coverage_path, task_coverage)
            summary["task_coverage"] = {
                **task_coverage,
                "artifact": task_coverage_path.relative_to(project.root).as_posix(),
                "artifact_sha256": sha256_file(
                    task_coverage_path, max_bytes=4 * 1024 * 1024
                ),
            }
        with self._validation_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while validation was running")
            state = current.state
            conversation = current.conversation
            state["last_validation"] = summary
            provisional = summary["assurance"] == "provisional"
            state["status"] = (
                "validated"
                if result.candidate_ready
                else "generated"
                if provisional
                else "validation_failed"
            )
            state["revision"] += 1
            state["updated_at"] = self._validation_timestamp()
            text = (
                "The configured KiCad and PCBDraft checks passed. This does not "
                "establish electrical, regulatory, or manufacturing fitness."
                if result.candidate_ready
                else (
                    "KiCad and PCBDraft checks completed and the generated files were retained. Review the reported findings; no electrical, regulatory, or manufacturing validation is implied."
                    if provisional
                    else "Checks found issues; the generated files and results were retained for review."
                )
            )
            self._append_message(
                conversation,
                "assistant",
                "validation",
                text,
                data={
                    "candidate_ready": result.candidate_ready,
                    "production_evidence_complete": (
                        result.production_evidence_complete
                    ),
                    "production_ready": result.production_ready,
                },
            )
            self._event(
                state,
                current.root,
                "validation.complete",
                text,
                level="info" if result.candidate_ready or provisional else "error",
            )
            self._write_records(current.root, state, conversation)
        return self.open_project(project_id)

    def generate_project_previews(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Generate browser-safe links to real KiCad exports and a 3D render."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="preview generation"
        )
        if not project.design_root.is_dir():
            raise ValidationError("project must be generated before preview export")
        managed = self._validation_open_managed_project(project.design_root)
        managed.assert_synchronized()
        output = project.root / "previews" / self._validation_new_run_id()
        bundle = self._validation_generate_previews(
            managed,
            output,
            timeout=timeout,
        )
        preview = {
            "root": bundle.root.relative_to(project.root).as_posix(),
            "receipt": bundle.receipt_path.relative_to(project.root).as_posix(),
            "design_content_hash": bundle.design_content_hash,
            "files": {
                key: path.relative_to(project.root).as_posix()
                for key, path in bundle.files.items()
            },
        }
        with self._validation_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while previews were generated")
            if (
                self._validation_open_managed_project(
                    current.design_root
                ).design.content_hash()
                != bundle.design_content_hash
            ):
                raise ValidationError("design changed while previews were generated")
            current.state["last_preview"] = preview
            current.state["revision"] += 1
            current.state["updated_at"] = self._validation_timestamp()
            self._event(
                current.state,
                current.root,
                "preview.complete",
                "Schematic, PCB, PDF, and 3D render previews generated",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)
