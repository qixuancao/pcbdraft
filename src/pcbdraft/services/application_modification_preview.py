"""Transactional preview entrypoint for agent-planned project revisions.

The host application remains the project revision, record, and agent-runtime
authority. It supplies adapters for managed-project inspection, locking,
timestamps, and repair-feedback construction so historical patch points remain
available without a reverse import.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError


class ApplicationModificationPreviewMixin:
    """Stage a requested agent-plan revision for review."""

    @staticmethod
    def _modification_preview_open_managed_project(design_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _modification_preview_resource_lock(root: Path, locks_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _modification_preview_timestamp() -> str:
        raise NotImplementedError

    @staticmethod
    def _modification_preview_feedback(request: str) -> dict[str, Any]:
        raise NotImplementedError

    def preview_modification(
        self,
        project_id: str,
        request: str,
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Turn a follow-up message into a validated, staged replacement design.

        The planning provider receives the retained semantic plan plus a bounded
        user revision request. It never edits native KiCad files: the replacement
        is generated and checked inside a transaction before the runtime policy or
        user can atomically apply it.
        """

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="revision staging"
        )
        managed = self._modification_preview_open_managed_project(project.design_root)
        managed.assert_synchronized()
        if managed.design.metadata.get("generator") != "agent_plan_v1":
            raise ValidationError(
                "this project was not generated from a retained agent circuit plan; use the semantic patch workflow"
            )
        if project.state["active_transaction"] is not None:
            raise ValidationError(
                "review, apply, or discard the staged PCB change before requesting another revision"
            )
        if project.state["status"] not in {
            "generated",
            "validated",
            "validation_failed",
            "repair_failed",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise ValidationError("the current project state cannot accept a revision")
        with self._modification_preview_resource_lock(
            project.root,
            self.locks_root,
        ):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before the revision was staged")
            self._append_message(current.conversation, "user", "revision", request)
            current.state["revision"] += 1
            current.state["updated_at"] = self._modification_preview_timestamp()
            self._event(
                current.state,
                current.root,
                "repair.requested",
                "Preparing a transactional PCB revision from the follow-up request",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = int(current.state["revision"])
        return self.prepare_agent_repair(
            project_id,
            self._modification_preview_feedback(request),
            timeout=timeout,
            expected_revision=expected_revision,
        )
