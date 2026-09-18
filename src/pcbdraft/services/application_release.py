"""Manufacturing-candidate release orchestration for the application service.

The host application remains the project and record authority. It supplies
adapters for managed-project inspection, release construction and verification,
locking, run IDs, and timestamps so historical patch points remain available
without a reverse import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError


class ApplicationReleaseMixin:
    """Build, verify, and retain one manufacturing-candidate release."""

    @staticmethod
    def _release_open_managed_project(design_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _release_new_run_id() -> str:
        raise NotImplementedError

    @staticmethod
    def _release_resource_lock(root: Path, locks_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _release_timestamp() -> str:
        raise NotImplementedError

    @staticmethod
    def _release_build_manufacturing(
        design_root: Path,
        output: Path,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError

    @staticmethod
    def _release_verify_manufacturing(root: Path) -> Any:
        raise NotImplementedError

    def build_release(
        self,
        project_id: str,
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="release build"
        )
        if project.state["status"] not in {
            "validated",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise ValidationError(
                "release requires a passing engineering-candidate validation"
            )
        managed = self._release_open_managed_project(project.design_root)
        managed.assert_synchronized()
        validation = self._require_current_candidate_validation(project, managed.design)
        baseline_relative = validation.get("drc_evidence")
        baseline_revision = validation.get("source_design_revision")
        baseline_hash = validation.get("source_content_hash")
        if (
            not isinstance(baseline_relative, str)
            or not isinstance(baseline_revision, int)
            or not isinstance(baseline_hash, str)
            or baseline_revision != project.state["design_revision"]
            or baseline_hash != managed.design.content_hash()
        ):
            raise ValidationError(
                "release requires complete DRC baseline evidence bound to the current design; run validation again"
            )
        baseline_path = project.root / baseline_relative
        try:
            baseline_path.resolve(strict=False).relative_to(project.root.resolve())
        except ValueError as exc:
            raise ValidationError(
                "release DRC baseline path is outside the project"
            ) from exc
        release_id = self._release_new_run_id()
        output = project.root / "releases" / release_id
        with self._release_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before release")
            current.state["status"] = "releasing"
            current.state["revision"] += 1
            current.state["updated_at"] = self._release_timestamp()
            self._event(
                current.state,
                current.root,
                "release.started",
                "Building manufacturing-candidate bundle",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]
        try:
            release = self._release_build_manufacturing(
                project.design_root,
                output,
                timeout=timeout,
                canonical_revision=expected_revision,
                design_revision=int(project.state["design_revision"]),
                baseline_drc_evidence=baseline_path,
                expected_baseline_design_revision=baseline_revision,
                expected_baseline_content_hash=baseline_hash,
            )
            verified = self._release_verify_manufacturing(release.root)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "release_failed",
                "release.failed",
                str(exc),
            )
            raise
        release_summary = {
            "id": release_id,
            "root": str(release.root),
            "manifest": str(release.manifest_path),
            "manifest_sha256": release.manifest_sha256,
            "archive": str(release.archive_path),
            "archive_sha256": release.archive_sha256,
            "candidate_ready": release.candidate_ready,
            "production_evidence_complete": release.production_evidence_complete,
            "production_ready": release.production_ready,
            "production_claimed": False,
            "source_revision": expected_revision,
            "source_design_revision": project.state["design_revision"],
            "source_content_hash": managed.design.content_hash(),
            "offline_verification": verified.to_dict(),
        }
        with self._release_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while release was running")
            current.state["status"] = "released"
            current.state["last_release"] = release_summary
            current.state["revision"] += 1
            current.state["updated_at"] = self._release_timestamp()
            text = (
                "Manufacturing-candidate bundle was built and verified offline; it is "
                "not a production or physical sign-off claim."
            )
            self._append_message(
                current.conversation,
                "assistant",
                "release",
                text,
                data={"release_id": release_id},
            )
            self._event(current.state, current.root, "release.complete", text)
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)
