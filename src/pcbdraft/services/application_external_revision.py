"""External KiCad revision review and import workflow.

This mixin owns the policy for detecting native-file drift and explicitly
importing one reviewed placement revision. The host application remains the
repository and project-record authority and supplies adapters for native KiCad
operations, locking, timestamps, and sanitization.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError


class ApplicationExternalRevisionMixin:
    """Review and import externally edited KiCad placement revisions."""

    @staticmethod
    def _external_revision_open_managed_project(design_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _external_revision_preview_kicad_import(managed: Any) -> Any:
        raise NotImplementedError

    @staticmethod
    def _external_revision_apply_kicad_import(preview: Any, *, timeout: float) -> Path:
        raise NotImplementedError

    @staticmethod
    def _external_revision_sanitize_secret_text(value: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _external_revision_preview_token_is_valid(value: Any) -> bool:
        raise NotImplementedError

    @staticmethod
    def _external_revision_resource_lock(root: Path, locks_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _external_revision_timestamp() -> str:
        raise NotImplementedError

    def external_kicad_change_status(self, project_id: str) -> dict[str, Any]:
        """Detect native-file drift without treating desktop state as authoritative."""

        project = self._open(project_id)
        if project.design_root.is_symlink() or not project.design_root.is_dir():
            return {
                "state": "no_design",
                "requires_import": False,
                "canonical_revision": project.state["revision"],
                "design_revision": project.state["design_revision"],
                "content_hash": None,
            }
        managed = self._external_revision_open_managed_project(project.design_root)
        drift = managed.drift()
        binding = {
            "canonical_revision": project.state["revision"],
            "design_revision": project.state["design_revision"],
            "content_hash": managed.design.content_hash(),
        }
        if not drift:
            return {
                "state": "clean",
                "requires_import": False,
                "drift": [],
                **binding,
            }
        try:
            preview = self._external_revision_preview_kicad_import(managed)
        except PCBDraftError as exc:
            return {
                "state": "unsupported_external_change",
                "requires_import": True,
                "importable": False,
                "drift": list(drift),
                "limitation": self._external_revision_sanitize_secret_text(str(exc))[
                    :1024
                ],
                **binding,
            }
        if not preview.has_changes:
            return {
                "state": "unsupported_external_change",
                "requires_import": True,
                "importable": False,
                "drift": list(drift),
                "limitation": "native bytes changed without a supported semantic placement revision",
                **binding,
            }
        return {
            "state": "review_required",
            "requires_import": True,
            "importable": True,
            "drift": list(drift),
            "board_sha256": preview.board_sha256,
            "review_token": preview.review_token,
            "change_set_id": preview.change_set.id if preview.change_set else None,
            "native_changes": list(preview.native_changes[:1_000]),
            "semantic_diff": preview.diff,
            **binding,
        }

    def import_external_kicad_revision(
        self,
        project_id: str,
        *,
        expected_preview_token: str,
        expected_revision: int | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Explicitly import reviewed KiCad placement drift as a new revision."""

        if not self._external_revision_preview_token_is_valid(expected_preview_token):
            raise ValidationError(
                "external import requires a valid reviewed preview token"
            )
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="external KiCad import"
        )
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise ValidationError("project is not eligible for external KiCad import")
        if project.state["active_transaction"] is not None:
            raise ValidationError("project already has a staged semantic change")
        managed = self._external_revision_open_managed_project(project.design_root)
        preview = self._external_revision_preview_kicad_import(managed)
        if not preview.has_changes or preview.change_set is None:
            raise ValidationError("no supported external KiCad revision is available")
        source_design_revision = int(project.state["design_revision"])
        source_hash = managed.design.content_hash()
        with self._external_revision_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before external KiCad import")
            if preview.review_token != expected_preview_token:
                raise ValidationError(
                    "external KiCad files changed since review; refresh the preview"
                )
            current.state["status"] = "importing_external"
            current.state["revision"] += 1
            current.state["updated_at"] = self._external_revision_timestamp()
            self._event(
                current.state,
                current.root,
                "external_revision.import_started",
                "Importing a reviewed external KiCad placement revision",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = int(current.state["revision"])
        try:
            transaction = self._external_revision_apply_kicad_import(
                preview,
                timeout=timeout,
            )
            imported = self._external_revision_open_managed_project(project.design_root)
            imported.assert_synchronized()
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "interrupted",
                "external_revision.import_failed",
                str(exc),
            )
            raise
        with self._external_revision_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed while external KiCad import ran")
            current.state["status"] = "generated"
            current.state["revision"] += 1
            current.state["design_revision"] += 1
            current.state["updated_at"] = self._external_revision_timestamp()
            current.state["last_validation"] = None
            current.state["last_preview"] = None
            current.state["last_release"] = None
            message = (
                "Reviewed KiCad placement changes were imported as an explicit external "
                "revision. Run final validation before release."
            )
            self._append_message(
                current.conversation,
                "assistant",
                "external_revision",
                message,
                data={
                    "source_design_revision": source_design_revision,
                    "source_content_hash": source_hash,
                    "design_content_hash": imported.design.content_hash(),
                    "transaction": transaction.name,
                },
            )
            self._event(
                current.state,
                current.root,
                "external_revision.imported",
                message,
            )
            self._write_records(current.root, current.state, current.conversation)
        result = self.open_project(project_id)
        result["external_revision"] = {
            "state": "imported",
            "source_design_revision": source_design_revision,
            "design_revision": result["state"]["design_revision"],
            "content_hash": imported.design.content_hash(),
            "transaction": transaction.name,
        }
        return result
