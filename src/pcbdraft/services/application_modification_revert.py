"""Discard and undo workflows for staged or applied project revisions.

The host application remains the project, record, and native-operation authority.
It supplies adapters for file mutation, managed-project inspection, progress
projection, locking, timestamps, error classification, and sanitization so
historical patch points remain available without a reverse import.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.services.application_project_store import APP_FILE_LIMIT
from pcbdraft.services.progress import EngineeringStage, ProgressVector, StageProjection


class ApplicationModificationRevertMixin:
    """Discard staged revisions and atomically undo applied revisions."""

    @staticmethod
    def _modification_revert_load_json(path: Path, limit: int) -> Any:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_atomic_write_json(path: Path, value: Any) -> None:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_resource_lock(root: Path, locks_root: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_timestamp() -> str:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_open_managed_project(path: Path) -> Any:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_replace(source: Path, destination: Path) -> None:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_operation_failure_code(
        error: BaseException,
        *,
        stage: str,
        tool_name: str,
    ) -> str:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_sanitize_secret_text(value: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_attach_progress(
        receipt: dict[str, Any],
        before: ProgressVector,
        after: ProgressVector,
        before_stage: StageProjection,
        after_stage: StageProjection,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_transaction_progress(
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _modification_revert_postcondition_error(
        code: str,
        message: str,
    ) -> BaseException:
        raise NotImplementedError

    def discard_modification(
        self, project_id: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="candidate discard"
        )
        transaction_id = project.state["active_transaction"]
        if project.state["status"] != "change_ready" or not isinstance(
            transaction_id, str
        ):
            raise ValidationError(
                "project has no semantic change awaiting confirmation"
            )
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = self._modification_revert_load_json(receipt_path, APP_FILE_LIMIT)
        with self._modification_revert_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise ValidationError("project changed before candidate discard")
            if current.state["active_transaction"] != transaction_id:
                raise ValidationError("active semantic transaction changed")
            receipt["status"] = "discarded"
            receipt["discarded_at"] = self._modification_revert_timestamp()
            self._modification_revert_atomic_write_json(receipt_path, receipt)
            current.state["active_transaction"] = None
            current.state["status"] = receipt.get("prior_status", "generated")
            current.state["revision"] += 1
            current.state["updated_at"] = self._modification_revert_timestamp()
            self._event(
                current.state,
                current.root,
                "change.discarded",
                "Staged semantic change discarded; authoritative design was untouched.",
            )
            self._write_records(current.root, current.state, current.conversation)
        return self.open_project(project_id)

    def undo_last_modification(
        self, project_id: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="last-change undo"
        )
        transaction_id = project.state["last_transaction"]
        if not isinstance(transaction_id, str):
            raise ValidationError("project has no applied semantic change to undo")
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = self._modification_revert_load_json(receipt_path, APP_FILE_LIMIT)
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "pcbdraft-agent-repair-transaction"
            or receipt.get("status") != "applied"
            or receipt.get("version") not in {1, 2}
        ):
            raise ValidationError("last semantic transaction is not undoable")
        before = transaction / "before"
        after = transaction / "after"
        baseline_progress, baseline_stage = self._current_progress_and_stage(project)
        with self._modification_revert_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            original_receipt = copy.deepcopy(receipt)
            moved_after = False
            moved_before = False
            event_path: Path | None = None
            before_progress = baseline_progress
            before_stage = baseline_stage
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError("project changed before last-change undo")
                if current.state["last_transaction"] != transaction_id:
                    raise ValidationError("last semantic transaction changed")
                if after.exists() or after.is_symlink():
                    raise ValidationError("undo backup already exists")
                managed = self._modification_revert_open_managed_project(
                    current.design_root
                )
                restored_managed = self._modification_revert_open_managed_project(
                    before
                )
                managed.assert_synchronized()
                restored_managed.assert_synchronized()
                if managed.design.content_hash() != receipt["after_hash"]:
                    raise ValidationError(
                        "authoritative design changed after the last transaction"
                    )
                if restored_managed.design.content_hash() != receipt["before_hash"]:
                    raise self._modification_revert_postcondition_error(
                        "native_delta_failed",
                        "undo target no longer matches the semantic replacement identity",
                    )
                semantic_delta = self._modification_revert_load_json(
                    transaction / "semantic-diff.json", APP_FILE_LIMIT
                )
                if (
                    not isinstance(semantic_delta, Mapping)
                    or semantic_delta.get("schema") != "pcbdraft-semantic-diff"
                    or semantic_delta.get("before_hash") != receipt["before_hash"]
                    or semantic_delta.get("after_hash") != receipt["after_hash"]
                ):
                    raise self._modification_revert_postcondition_error(
                        "native_delta_failed",
                        "undo semantic replacement identity is invalid",
                    )
                before_revision = int(current.state["design_revision"])
                after_revision = before_revision + 1
                before_progress, before_stage, before_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        managed,
                        before_revision,
                    )
                )
                prior_validation_root: Path | None = None
                prior_validation = receipt.get("prior_validation")
                if isinstance(prior_validation, Mapping) and isinstance(
                    prior_validation.get("report"), str
                ):
                    candidate = current.root / str(prior_validation["report"])
                    try:
                        candidate.relative_to(current.root)
                    except ValueError:
                        pass
                    else:
                        prior_validation_root = candidate.parent
                after_progress, after_stage, after_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        restored_managed,
                        after_revision,
                        validation_root=prior_validation_root,
                        include_routing_failures=False,
                    )
                )
                verified_before = self._require_current_native_consistency(
                    before_consistency,
                    before_revision,
                    label="applied source",
                )
                verified_after = self._require_current_native_consistency(
                    after_consistency,
                    after_revision,
                    label="undo target",
                )
                self._modification_revert_atomic_write_json(
                    transaction / "undo-native-before.json",
                    verified_before.to_dict(),
                )
                self._modification_revert_atomic_write_json(
                    transaction / "undo-native-after.json",
                    verified_after.to_dict(),
                )
                receipt["version"] = 2
                receipt.setdefault("artifact", {})
                receipt["artifact"].update(
                    {
                        "undo_native_before": "undo-native-before.json",
                        "undo_native_after": "undo-native-after.json",
                    }
                )
                receipt["postconditions"] = [
                    {
                        "name": "undo_source_native_consistency",
                        "passed": verified_before.consistency_passed,
                    },
                    {
                        "name": "undo_target_native_consistency",
                        "passed": verified_after.consistency_passed,
                    },
                    {
                        "name": "undo_semantic_replacement_identity",
                        "passed": True,
                    },
                ]
                self._modification_revert_attach_progress(
                    receipt,
                    before_progress,
                    after_progress,
                    before_stage,
                    after_stage,
                )
                receipt["convergence_classification"] = receipt["progress_delta"][
                    "classification"
                ]
                receipt["undo_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                receipt["undo"] = {
                    "status": "publishing",
                    "error_code": None,
                    "rollback": {
                        "state": "not_required",
                        "performed": False,
                        "live_unchanged": True,
                    },
                }
                self._modification_revert_atomic_write_json(receipt_path, receipt)
                self._modification_revert_replace(current.design_root, after)
                moved_after = True
                self._modification_revert_replace(before, current.design_root)
                moved_before = True
                current.state["status"] = receipt.get("prior_status", "generated")
                current.state["last_transaction"] = None
                current.state["last_validation"] = receipt.get("prior_validation")
                current.state["last_preview"] = receipt.get("prior_preview")
                current.state["last_release"] = receipt.get("prior_release")
                current.state["design_revision"] = after_revision
                current.state["revision"] += 1
                current.state["updated_at"] = self._modification_revert_timestamp()
                text = "Undo restored the exact previous authoritative managed project."
                self._append_message(
                    current.conversation,
                    "assistant",
                    "change_undone",
                    text,
                    data={"transaction_id": transaction_id},
                )
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(current.state, current.root, "change.undone", text)
                self._write_records(current.root, current.state, current.conversation)
                receipt["status"] = "undone"
                receipt["undone_at"] = self._modification_revert_timestamp()
                receipt["undo"] = {
                    "status": "committed",
                    "error_code": None,
                    "rollback": {
                        "state": "committed",
                        "performed": False,
                        "live_unchanged": False,
                    },
                }
                self._modification_revert_atomic_write_json(receipt_path, receipt)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_before:
                    try:
                        if before.exists() or before.is_symlink():
                            raise ValidationError(
                                "undo target rollback destination already exists"
                            )
                        self._modification_revert_replace(current.design_root, before)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                if moved_after:
                    try:
                        if (
                            current.design_root.exists()
                            or current.design_root.is_symlink()
                        ):
                            raise ValidationError(
                                "live undo rollback destination already exists"
                            )
                        self._modification_revert_replace(after, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                try:
                    self._modification_revert_atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    self._modification_revert_atomic_write_json(
                        current.root / "project.json", original_state
                    )
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                    rollback_failures.append(rollback_exc)
                receipt.pop("undone_at", None)
                receipt["status"] = (
                    "rollback_incomplete"
                    if rollback_failures
                    else str(original_receipt.get("status", "applied"))
                )
                receipt["undo"] = {
                    "status": "rollback_incomplete" if rollback_failures else "failed",
                    "error_code": self._modification_revert_operation_failure_code(
                        exc, stage="publication", tool_name="undo_modification"
                    ),
                    "failure": self._modification_revert_sanitize_secret_text(str(exc))[
                        :2048
                    ],
                    "rollback": {
                        "state": (
                            "incomplete"
                            if rollback_failures
                            else "restored"
                            if moved_after or moved_before
                            else "not_required"
                        ),
                        "performed": (moved_after or moved_before)
                        and not rollback_failures,
                        "live_unchanged": not rollback_failures,
                    },
                }
                if rollback_failures:
                    self._modification_revert_attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(before_progress.source_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                else:
                    self._modification_revert_attach_progress(
                        receipt,
                        before_progress,
                        before_progress,
                        before_stage,
                        before_stage,
                    )
                receipt["convergence_classification"] = receipt["progress_delta"][
                    "classification"
                ]
                receipt["undo_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                try:
                    self._modification_revert_atomic_write_json(receipt_path, receipt)
                except PCBDraftError:
                    pass
                if rollback_failures:
                    raise PCBDraftError(
                        "last-change undo failed and rollback was incomplete"
                    ) from exc
                raise
        result = self.open_project(project_id)
        result["transaction_progress"] = self._modification_revert_transaction_progress(
            receipt
        )
        return result
