"""Atomic publication of a staged semantic project modification.

The host application remains the project lock, revision, event, record, and
native-state authority. This mixin reuses the modification-revert adapters for
all shared I/O, locking, progress, and rollback operations without importing the
application coordinator back.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class ApplicationModificationApplyMixin:
    """Validate and atomically publish the current staged modification."""

    @staticmethod
    def _modification_apply_file_limit() -> int:
        raise NotImplementedError

    @staticmethod
    def _modification_apply_validation_error(message: str) -> BaseException:
        raise NotImplementedError

    @staticmethod
    def _modification_apply_error(message: str) -> BaseException:
        raise NotImplementedError

    @staticmethod
    def _modification_apply_error_type() -> type[BaseException]:
        raise NotImplementedError

    @staticmethod
    def _modification_apply_unknown_progress(revision: int) -> Any:
        raise NotImplementedError

    @staticmethod
    def _modification_apply_not_started_stage() -> Any:
        raise NotImplementedError

    @staticmethod
    def _modification_apply_stage_projection(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def apply_modification(
        self,
        project_id: str,
        *,
        timeout: float = 90.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Atomically publish the currently staged, confirmed semantic change."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="candidate application"
        )
        transaction_id = project.state["active_transaction"]
        if project.state["status"] != "change_ready" or not isinstance(
            transaction_id, str
        ):
            raise self._modification_apply_validation_error(
                "project has no semantic change awaiting confirmation"
            )
        transaction = project.root / "transactions" / transaction_id
        receipt_path = transaction / "receipt.json"
        receipt = self._modification_revert_load_json(
            receipt_path, self._modification_apply_file_limit()
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "pcbdraft-agent-repair-transaction"
            or receipt.get("status") != "ready"
            or receipt.get("version") not in {1, 2}
        ):
            raise self._modification_apply_validation_error(
                "semantic change receipt is not ready"
            )
        staged = transaction / "staged"
        before = transaction / "before"
        baseline_progress, baseline_stage = self._current_progress_and_stage(project)
        with self._modification_revert_resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            original_receipt = copy.deepcopy(receipt)
            moved_before = False
            moved_candidate = False
            event_path: Path | None = None
            before_progress = baseline_progress
            before_stage = baseline_stage
            try:
                if current.state["revision"] != expected_revision:
                    raise self._modification_apply_validation_error(
                        "project changed before candidate application"
                    )
                if current.state["active_transaction"] != transaction_id:
                    raise self._modification_apply_validation_error(
                        "active semantic transaction changed"
                    )
                if before.exists() or before.is_symlink():
                    raise self._modification_apply_validation_error(
                        "candidate application backup already exists"
                    )
                current_managed = self._modification_revert_open_managed_project(
                    current.design_root
                )
                staged_managed = self._modification_revert_open_managed_project(staged)
                current_managed.assert_synchronized()
                staged_managed.assert_synchronized()
                if current_managed.design.content_hash() != receipt["before_hash"]:
                    raise self._modification_apply_validation_error(
                        "authoritative design changed after semantic preview"
                    )
                if staged_managed.design.content_hash() != receipt["after_hash"]:
                    raise self._modification_apply_validation_error(
                        "staged design no longer matches the semantic receipt"
                    )
                semantic_delta = self._modification_revert_load_json(
                    transaction / "semantic-diff.json",
                    self._modification_apply_file_limit(),
                )
                if (
                    not isinstance(semantic_delta, Mapping)
                    or semantic_delta.get("schema") != "pcbdraft-semantic-diff"
                    or semantic_delta.get("before_hash") != receipt["before_hash"]
                    or semantic_delta.get("after_hash") != receipt["after_hash"]
                ):
                    raise self._modification_revert_postcondition_error(
                        "native_delta_failed",
                        "legacy modification semantic replacement identity is invalid",
                    )
                before_revision = int(current.state["design_revision"])
                after_revision = before_revision + 1
                before_progress, before_stage, before_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        current_managed,
                        before_revision,
                    )
                )
                after_progress, after_stage, after_consistency = (
                    self._managed_progress_and_stage(
                        current,
                        staged_managed,
                        after_revision,
                        validation_root=transaction / "validation",
                        include_routing_failures=False,
                    )
                )
                verified_before = self._require_current_native_consistency(
                    before_consistency,
                    before_revision,
                    label="authoritative source",
                )
                verified_after = self._require_current_native_consistency(
                    after_consistency,
                    after_revision,
                    label="staged candidate",
                )
                self._modification_revert_atomic_write_json(
                    transaction / "application-native-before.json",
                    verified_before.to_dict(),
                )
                self._modification_revert_atomic_write_json(
                    transaction / "application-native-after.json",
                    verified_after.to_dict(),
                )
                receipt["version"] = 2
                receipt["baseline_design_revision"] = before_revision
                receipt["candidate_revision"] = after_revision
                receipt.setdefault("artifact", {})
                receipt["artifact"].update(
                    {
                        "application_native_before": "application-native-before.json",
                        "application_native_after": "application-native-after.json",
                    }
                )
                receipt["postconditions"] = [
                    {
                        "name": "source_native_consistency",
                        "passed": verified_before.consistency_passed,
                    },
                    {
                        "name": "candidate_native_consistency",
                        "passed": verified_after.consistency_passed,
                    },
                    {
                        "name": "semantic_replacement_identity",
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
                receipt["application_progress"] = {
                    key: copy.deepcopy(receipt[key])
                    for key in (
                        "progress_before",
                        "progress_after",
                        "progress_delta",
                        "stage_before",
                        "stage_after",
                    )
                }
                receipt["application"] = {
                    "status": "publishing",
                    "error_code": None,
                    "rollback": {
                        "state": "not_required",
                        "performed": False,
                        "live_unchanged": True,
                    },
                }
                self._modification_revert_atomic_write_json(receipt_path, receipt)
                self._modification_revert_replace(current.design_root, before)
                moved_before = True
                self._modification_revert_replace(staged, current.design_root)
                moved_candidate = True
                current.state["status"] = receipt.get("result_status", "validated")
                current.state["active_transaction"] = None
                current.state["last_transaction"] = transaction_id
                current.state["last_validation"] = {
                    "run_id": f"transaction:{transaction_id}",
                    "report": (
                        Path("transactions")
                        / transaction_id
                        / receipt["validation"]["report"]
                    ).as_posix(),
                    **{
                        key: receipt["validation"][key]
                        for key in (
                            "report_sha256",
                            "candidate_ready",
                            "production_evidence_complete",
                            "production_ready",
                            "production_claimed",
                            "source_design_revision",
                        )
                    },
                    "assurance": receipt["validation"].get("assurance", "provisional"),
                    "levels": self._modification_revert_load_json(
                        transaction / receipt["validation"]["report"],
                        self._modification_apply_file_limit(),
                    )["levels"],
                }
                current.state["last_release"] = None
                current.state["last_preview"] = None
                current.state["design_revision"] = after_revision
                current.state["revision"] += 1
                current.state["updated_at"] = self._modification_revert_timestamp()
                text = (
                    "Applied the staged replacement atomically; undo remains available."
                    if receipt.get("schema") == "pcbdraft-agent-repair-transaction"
                    else "Applied the confirmed semantic change atomically; undo remains available."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "change_applied",
                    text,
                    data={"transaction_id": transaction_id},
                )
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(current.state, current.root, "change.applied", text)
                self._write_records(current.root, current.state, current.conversation)
                receipt["status"] = "applied"
                receipt["applied_at"] = self._modification_revert_timestamp()
                receipt["application"] = {
                    "status": "committed",
                    "error_code": None,
                    "rollback": {
                        "state": "committed",
                        "performed": False,
                        "live_unchanged": False,
                    },
                }
                self._modification_revert_atomic_write_json(receipt_path, receipt)
                expected_revision = int(current.state["revision"])
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_candidate:
                    try:
                        if staged.exists() or staged.is_symlink():
                            raise self._modification_apply_validation_error(
                                "staged rollback destination already exists"
                            )
                        self._modification_revert_replace(current.design_root, staged)
                    except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                        rollback_failures.append(rollback_exc)
                if moved_before:
                    try:
                        if (
                            current.design_root.exists()
                            or current.design_root.is_symlink()
                        ):
                            raise self._modification_apply_validation_error(
                                "live rollback destination already exists"
                            )
                        self._modification_revert_replace(before, current.design_root)
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
                receipt.pop("applied_at", None)
                receipt["status"] = (
                    "rollback_incomplete"
                    if rollback_failures
                    else str(original_receipt.get("status", "ready"))
                )
                receipt["application"] = {
                    "status": "rollback_incomplete" if rollback_failures else "failed",
                    "error_code": self._modification_revert_operation_failure_code(
                        exc, stage="publication", tool_name="repair_candidate"
                    ),
                    "failure": self._modification_revert_sanitize_secret_text(str(exc))[
                        :2048
                    ],
                    "rollback": {
                        "state": (
                            "incomplete"
                            if rollback_failures
                            else "restored"
                            if moved_before or moved_candidate
                            else "not_required"
                        ),
                        "performed": (moved_before or moved_candidate)
                        and not rollback_failures,
                        "live_unchanged": not rollback_failures,
                    },
                }
                if rollback_failures:
                    self._modification_revert_attach_progress(
                        receipt,
                        before_progress,
                        self._modification_apply_unknown_progress(
                            before_progress.source_revision
                        ),
                        before_stage,
                        self._modification_apply_stage_projection(
                            self._modification_apply_not_started_stage(),
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
                receipt["application_progress"] = {
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
                except self._modification_apply_error_type():
                    pass
                if rollback_failures:
                    raise self._modification_apply_error(
                        "candidate application failed and rollback was incomplete"
                    ) from exc
                raise
        result = self.generate_project_previews(
            project_id,
            timeout=timeout,
            expected_revision=expected_revision,
        )
        result["transaction_progress"] = self._modification_revert_transaction_progress(
            receipt
        )
        return result
