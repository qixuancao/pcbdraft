"""Installed KiCad part inspection and staged registration transaction.

ApplicationService remains the project record, revision, lock, and publication
authority. This mixin sequences one inspected part contract through isolated
native materialization and atomic publication using late-bound host adapters,
without importing the application coordinator back.
"""

from __future__ import annotations

import copy
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pcbdraft.services.progress import (
    EngineeringStage,
    EvidenceCheck,
    ProgressVector,
    StageProjection,
)


def _unconfigured(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("application part-registration hooks are not configured")


ValidationError: Callable[[str], BaseException] = _unconfigured
ResourceLock: Callable[..., Any] = _unconfigured
open_managed_project: Callable[..., Any] = _unconfigured
canonical_json_bytes: Callable[[Any], bytes] = _unconfigured
new_run_id: Callable[[], str] = _unconfigured
make_directory: Callable[[Path], Path] = _unconfigured
utc_timestamp: Callable[[], str] = _unconfigured
_attach_progress: Callable[..., Any] = _unconfigured
atomic_write_json: Callable[..., Any] = _unconfigured
load_generation_request: Callable[[Path], Any] = _unconfigured
materialize_managed_design: Callable[..., Any] = _unconfigured
inspect_native_consistency: Callable[..., Any] = _unconfigured
_unavailable_consistency_report: Callable[..., Any] = _unconfigured
_native_postconditions: Callable[..., Any] = _unconfigured
_native_board_projection: Callable[..., Any] = _unconfigured
_progress_vector: Callable[..., Any] = _unconfigured
derive_stage: Callable[..., Any] = _unconfigured
_progress_stage_evidence: Callable[..., Any] = _unconfigured
_consistency_rejection: Callable[..., Any] = _unconfigured
compare_native_operation_delta: Callable[..., Any] = _unconfigured
_native_schematic_projection: Callable[..., Any] = _unconfigured
_native_delta_postconditions: Callable[..., Any] = _unconfigured
_operation_failure_code: Callable[..., Any] = _unconfigured
_bind_transaction_failure: Callable[..., Any] = _unconfigured
_sanitize_secret_text: Callable[[str], str] = _unconfigured
_PCBOperationPostconditionError: Callable[..., BaseException] = _unconfigured
_design_from_dict: Callable[[dict[str, Any]], Any] = _unconfigured
_installed_kicad_part: Callable[..., Any] = _unconfigured
_pcbdraft_error_type: Callable[[], type[BaseException]] = _unconfigured


def _configure_legacy_application_hooks(
    *,
    validation_error_hook: Callable[[str], BaseException],
    resource_lock_hook: Callable[..., Any],
    open_managed_project_hook: Callable[..., Any],
    canonical_json_bytes_hook: Callable[[Any], bytes],
    new_run_id_hook: Callable[[], str],
    make_directory_hook: Callable[[Path], Path],
    utc_timestamp_hook: Callable[[], str],
    attach_progress_hook: Callable[..., Any],
    atomic_write_json_hook: Callable[..., Any],
    load_generation_request_hook: Callable[[Path], Any],
    materialize_managed_design_hook: Callable[..., Any],
    inspect_native_consistency_hook: Callable[..., Any],
    unavailable_consistency_report_hook: Callable[..., Any],
    native_postconditions_hook: Callable[..., Any],
    native_board_projection_hook: Callable[..., Any],
    progress_vector_hook: Callable[..., Any],
    derive_stage_hook: Callable[..., Any],
    progress_stage_evidence_hook: Callable[..., Any],
    consistency_rejection_hook: Callable[..., Any],
    compare_native_operation_delta_hook: Callable[..., Any],
    native_schematic_projection_hook: Callable[..., Any],
    native_delta_postconditions_hook: Callable[..., Any],
    operation_failure_code_hook: Callable[..., Any],
    bind_transaction_failure_hook: Callable[..., Any],
    sanitize_secret_text_hook: Callable[[str], str],
    pcb_operation_postcondition_error_hook: Callable[..., BaseException],
    design_from_dict_hook: Callable[[dict[str, Any]], Any],
    installed_kicad_part_hook: Callable[..., Any],
    pcbdraft_error_type_hook: Callable[[], type[BaseException]],
) -> None:
    """Install adapters that resolve historical application globals at call time."""

    global ValidationError
    global ResourceLock
    global open_managed_project
    global canonical_json_bytes
    global new_run_id
    global make_directory
    global utc_timestamp
    global _attach_progress
    global atomic_write_json
    global load_generation_request
    global materialize_managed_design
    global inspect_native_consistency
    global _unavailable_consistency_report
    global _native_postconditions
    global _native_board_projection
    global _progress_vector
    global derive_stage
    global _progress_stage_evidence
    global _consistency_rejection
    global compare_native_operation_delta
    global _native_schematic_projection
    global _native_delta_postconditions
    global _operation_failure_code
    global _bind_transaction_failure
    global _sanitize_secret_text
    global _PCBOperationPostconditionError
    global _design_from_dict
    global _installed_kicad_part
    global _pcbdraft_error_type

    ValidationError = validation_error_hook
    ResourceLock = resource_lock_hook
    open_managed_project = open_managed_project_hook
    canonical_json_bytes = canonical_json_bytes_hook
    new_run_id = new_run_id_hook
    make_directory = make_directory_hook
    utc_timestamp = utc_timestamp_hook
    _attach_progress = attach_progress_hook
    atomic_write_json = atomic_write_json_hook
    load_generation_request = load_generation_request_hook
    materialize_managed_design = materialize_managed_design_hook
    inspect_native_consistency = inspect_native_consistency_hook
    _unavailable_consistency_report = unavailable_consistency_report_hook
    _native_postconditions = native_postconditions_hook
    _native_board_projection = native_board_projection_hook
    _progress_vector = progress_vector_hook
    derive_stage = derive_stage_hook
    _progress_stage_evidence = progress_stage_evidence_hook
    _consistency_rejection = consistency_rejection_hook
    compare_native_operation_delta = compare_native_operation_delta_hook
    _native_schematic_projection = native_schematic_projection_hook
    _native_delta_postconditions = native_delta_postconditions_hook
    _operation_failure_code = operation_failure_code_hook
    _bind_transaction_failure = bind_transaction_failure_hook
    _sanitize_secret_text = sanitize_secret_text_hook
    _PCBOperationPostconditionError = pcb_operation_postcondition_error_hook
    _design_from_dict = design_from_dict_hook
    _installed_kicad_part = installed_kicad_part_hook
    _pcbdraft_error_type = pcbdraft_error_type_hook


class ApplicationPartRegistrationMixin:
    """Inspect and atomically register one installed KiCad part contract."""

    def register_kicad_part(
        self,
        project_id: str,
        value: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Atomically publish one inspected local KiCad part contract."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="register_kicad_part"
        )
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            raise ValidationError("project has no synchronized design to modify")
        authoritative = open_managed_project(project.design_root)
        authoritative.assert_synchronized()
        before_catalog_hash = hashlib.sha256(
            canonical_json_bytes(authoritative.graph.to_dict())
        ).hexdigest()
        baseline_design_revision = int(project.state["design_revision"])
        before_progress, before_stage = self._current_progress_and_stage(project)
        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        before = transaction / "before"
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-kicad-part-registration-receipt",
            "version": 2,
            "status": "preparing",
            "operation": "register_kicad_part",
            "native_scope": "catalog_plus_native_rematerialization",
            "created_at": utc_timestamp(),
            "baseline_revision": expected_revision,
            "baseline_design_revision": baseline_design_revision,
            "candidate_revision": None,
            "committed_revision": None,
            "committed_design_revision": None,
            "before_hash": authoritative.design.content_hash(),
            "before_catalog_hash": before_catalog_hash,
            "part_id": value.get("id"),
            "rollback_performed": False,
            "rollback": {
                "state": "not_required",
                "performed": False,
                "live_unchanged": True,
            },
            "artifact": {
                "transaction_id": transaction_id,
                "receipt": "receipt.json",
            },
        }
        _attach_progress(
            receipt,
            before_progress,
            before_progress,
            before_stage,
            before_stage,
        )
        atomic_write_json(receipt_path, receipt)
        stage = "inspection"
        try:
            from pcbdraft.agent.footprint_resolver import LocalKiCadFootprintResolver
            from pcbdraft.agent.part_resolver import LocalKiCadPartResolver

            symbol = LocalKiCadPartResolver().describe(str(value.get("symbol", "")))
            footprint = LocalKiCadFootprintResolver().describe(
                str(value.get("footprint", ""))
            )
            pins = value.get("pins")
            if not isinstance(pins, list):
                raise ValidationError("part pins must be an array")
            installed_pins = {item["number"]: item for item in symbol.pins}
            supplied_pins = {
                str(item.get("number")): item for item in pins if isinstance(item, dict)
            }
            if set(supplied_pins) != set(installed_pins):
                raise ValidationError(
                    "part pins must exactly cover the installed symbol pin numbers"
                )
            available_pads = set(footprint.pad_numbers)
            for number, installed in installed_pins.items():
                supplied = supplied_pins[number]
                if supplied.get("name") != installed["name"]:
                    raise ValidationError(
                        f"part pin {number} name differs from the installed symbol"
                    )
                if supplied.get("electrical_type") != installed["electrical_type"]:
                    raise ValidationError(
                        f"part pin {number} electrical type differs from the installed symbol"
                    )
                if supplied.get("footprint_pad") not in available_pads:
                    raise ValidationError(
                        f"part pin {number} maps to a missing footprint pad"
                    )
            part = _installed_kicad_part(value, footprint_sha256=footprint.sha256)
            existing = authoritative.graph.get_optional(part.id)
            if existing is not None:
                if existing.to_dict() != part.to_dict():
                    raise ValidationError(
                        f"part id already exists with different facts: {part.id}"
                    )
                with ResourceLock(project.root, self.locks_root):
                    current = self._open(project_id)
                    if current.state["revision"] != expected_revision:
                        raise ValidationError(
                            "project changed while part registration was inspected"
                        )
                    current_design = open_managed_project(current.design_root)
                    current_design.assert_synchronized()
                    current_catalog_hash = hashlib.sha256(
                        canonical_json_bytes(current_design.graph.to_dict())
                    ).hexdigest()
                    if current_design.design.content_hash() != receipt["before_hash"]:
                        raise ValidationError(
                            "authoritative design changed before part no-op"
                        )
                    if current_catalog_hash != receipt["before_catalog_hash"]:
                        raise ValidationError(
                            "authoritative part catalog changed before part no-op"
                        )
                receipt.update(
                    {
                        "status": "noop",
                        "native_scope": "catalog_noop_no_native_write",
                        "completed_at": utc_timestamp(),
                        "candidate_revision": baseline_design_revision,
                        "after_hash": authoritative.design.content_hash(),
                        "after_catalog_hash": before_catalog_hash,
                    }
                )
                atomic_write_json(receipt_path, receipt)
                return self._with_tool_result(
                    self.open_project(project_id),
                    {
                        "operation": "register_kicad_part",
                        "transaction_id": transaction_id,
                        "project_id": project_id,
                        "part": part.to_dict(),
                        "changed": False,
                        "catalog_count": len(authoritative.graph),
                    },
                )
            graph = authoritative.graph.merged([part], source=f"project:{project_id}")
            document = authoritative.design.to_dict()
            document["metadata"]["assurance"] = "provisional"
            candidate = _design_from_dict(document)
            after_catalog_hash = hashlib.sha256(
                canonical_json_bytes(graph.to_dict())
            ).hexdigest()
            receipt.update(
                {
                    "candidate_revision": baseline_design_revision + 1,
                    "after_hash": candidate.content_hash(),
                    "after_catalog_hash": after_catalog_hash,
                    "symbol": symbol.to_dict(),
                    "footprint": footprint.to_dict(),
                }
            )
            atomic_write_json(receipt_path, receipt)
            request = load_generation_request(authoritative.requirements_path)
            stage = "materialization"
            materialize_managed_design(
                request,
                candidate,
                staged,
                graph=graph,
                plan=authoritative.plan,
                retain_failed_attempt=transaction / "failed-native",
                lock_timeout=min(10.0, timeout),
                auto_place=False,
                route_net_ids=frozenset(),
                allow_incomplete=True,
            )
            staged_project = open_managed_project(staged)
            staged_project.assert_synchronized()
            if staged_project.design.content_hash() != candidate.content_hash():
                raise ValidationError("staged semantic design hash changed")
            if (
                hashlib.sha256(
                    canonical_json_bytes(staged_project.graph.to_dict())
                ).hexdigest()
                != after_catalog_hash
            ):
                raise ValidationError("staged part catalog hash changed")
            candidate_revision = baseline_design_revision + 1
            stage = "native_consistency"
            consistency_failure: BaseException | None = None
            try:
                consistency = inspect_native_consistency(
                    candidate,
                    staged_project.schematic_path,
                    staged_project.board_path,
                    candidate_revision=candidate_revision,
                    graph=graph,
                )
            except _pcbdraft_error_type() as exc:
                consistency_failure = exc
                consistency = _unavailable_consistency_report(candidate_revision)
            atomic_write_json(
                transaction / "native-consistency.json", consistency.to_dict()
            )
            receipt["consistency_passed"] = consistency.consistency_passed
            receipt["postconditions"] = _native_postconditions(
                "register_kicad_part", consistency
            )
            receipt["artifact"]["native_consistency"] = "native-consistency.json"
            try:
                candidate_board = _native_board_projection(staged_project)
            except _pcbdraft_error_type():
                candidate_board = None
            after_progress = _progress_vector(
                candidate,
                graph,
                candidate_revision,
                consistency=consistency,
                board=candidate_board,
                fatal_drc=before_progress.fatal_drc_count.for_revision(
                    candidate_revision
                ),
                error_drc=before_progress.error_drc_count.for_revision(
                    candidate_revision
                ),
                erc_error=before_progress.erc_error_count.for_revision(
                    candidate_revision
                ),
            )
            after_stage = derive_stage(
                after_progress,
                _progress_stage_evidence(
                    candidate,
                    candidate_revision,
                    requirements_frozen=bool(staged_project.design.requirements),
                    consistency=consistency,
                    progress=after_progress,
                    erc_check=EvidenceCheck.unknown(candidate_revision),
                    drc_check=EvidenceCheck.unknown(candidate_revision),
                ),
            )
            _attach_progress(
                receipt,
                before_progress,
                after_progress,
                before_stage,
                after_stage,
            )
            if consistency_failure is not None:
                raise _PCBOperationPostconditionError(
                    "native_verification_failed",
                    "native KiCad inspection failed",
                ) from consistency_failure
            if not consistency.consistency_passed:
                raise _consistency_rejection("register_kicad_part", None, consistency)
            stage = "native_delta"
            native_delta = compare_native_operation_delta(
                "register_kicad_part",
                {},
                authoritative.design,
                candidate,
                _native_board_projection(authoritative),
                _native_board_projection(staged_project),
                before_schematic=_native_schematic_projection(authoritative),
                after_schematic=_native_schematic_projection(staged_project),
                graph=graph,
            )
            atomic_write_json(
                transaction / "native-operation-delta.json", native_delta.to_dict()
            )
            receipt["artifact"]["native_delta"] = "native-operation-delta.json"
            receipt["postconditions"].extend(_native_delta_postconditions(native_delta))
            receipt["native_delta"] = {
                "operation_checked": True,
                "policy": native_delta.policy,
                "passed": native_delta.passed,
                "failed_checks": [
                    item.name for item in native_delta.checks if not item.passed
                ][:4],
            }
            atomic_write_json(receipt_path, receipt)
            if not native_delta.passed:
                raise _PCBOperationPostconditionError(
                    "native_delta_failed",
                    "part registration changed unrelated native project state",
                )
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            receipt["error_code"] = _operation_failure_code(
                exc, stage=stage, tool_name="register_kicad_part"
            )
            _attach_progress(
                receipt,
                before_progress,
                before_progress,
                before_stage,
                before_stage,
            )
            atomic_write_json(receipt_path, receipt)
            _bind_transaction_failure(exc, transaction_id)
            raise

        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            moved_before = False
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while part registration was staged"
                    )
                current_design = open_managed_project(current.design_root)
                current_design.assert_synchronized()
                current_catalog_hash = hashlib.sha256(
                    canonical_json_bytes(current_design.graph.to_dict())
                ).hexdigest()
                if current_design.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed before part publication"
                    )
                if current_catalog_hash != receipt["before_catalog_hash"]:
                    raise ValidationError(
                        "authoritative part catalog changed before publication"
                    )
                os.replace(current.design_root, before)
                moved_before = True
                os.replace(staged, current.design_root)
                published = open_managed_project(current.design_root)
                published.assert_synchronized()
                current.state["status"] = "generated"
                current.state["revision"] += 1
                current.state["design_revision"] += 1
                current.state["updated_at"] = utc_timestamp()
                current.state["last_validation"] = None
                current.state["last_preview"] = None
                current.state["last_release"] = None
                current.state["last_transaction"] = transaction_id
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(
                    current.state,
                    current.root,
                    "pcb.kicad_part_registered",
                    f"Registered installed KiCad part {part.id}",
                )
                self._write_records(current.root, current.state, current.conversation)
                # Publish success last.  A receipt write failure must still be
                # able to restore the live design, event, and project records
                # without leaving a durable applied claim for a rolled-back part.
                receipt.update(
                    {
                        "status": "applied",
                        "applied_at": utc_timestamp(),
                        "committed_revision": current.state["revision"],
                        "committed_design_revision": current.state["design_revision"],
                        "rollback": {
                            "state": "committed",
                            "performed": False,
                            "live_unchanged": False,
                        },
                    }
                )
                atomic_write_json(receipt_path, receipt)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if moved_before:
                    try:
                        failed_published = transaction / "failed-published"
                        if current.design_root.exists():
                            os.replace(current.design_root, failed_published)
                        if before.exists():
                            os.replace(before, current.design_root)
                    except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                        rollback_failures.append(rollback_exc)
                try:
                    atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - complete rollback audit
                    rollback_failures.append(rollback_exc)
                receipt["status"] = "failed"
                receipt["failed_at"] = utc_timestamp()
                receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
                receipt.pop("applied_at", None)
                receipt["committed_revision"] = None
                receipt["committed_design_revision"] = None
                receipt["error_code"] = _operation_failure_code(
                    exc, stage="publication", tool_name="register_kicad_part"
                )
                receipt["rollback_performed"] = moved_before and not rollback_failures
                receipt["rollback"] = {
                    "state": (
                        "restored"
                        if moved_before and not rollback_failures
                        else "incomplete"
                        if rollback_failures
                        else "not_required"
                    ),
                    "performed": moved_before and not rollback_failures,
                    "live_unchanged": not rollback_failures,
                }
                if not rollback_failures:
                    _attach_progress(
                        receipt,
                        before_progress,
                        before_progress,
                        before_stage,
                        before_stage,
                    )
                else:
                    _attach_progress(
                        receipt,
                        before_progress,
                        ProgressVector.unknown(baseline_design_revision),
                        before_stage,
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                try:
                    atomic_write_json(receipt_path, receipt)
                except _pcbdraft_error_type():
                    pass
                if rollback_failures:
                    failure = _pcbdraft_error_type()(
                        "part publication failed and rollback was incomplete"
                    )
                    _bind_transaction_failure(failure, transaction_id)
                    raise failure from exc
                _bind_transaction_failure(exc, transaction_id)
                raise
        return self._with_tool_result(
            self.open_project(project_id),
            {
                "operation": "register_kicad_part",
                "transaction_id": transaction_id,
                "project_id": project_id,
                "part": part.to_dict(),
                "symbol": symbol.to_dict(),
                "footprint": footprint.to_dict(),
                "changed": True,
                "catalog_count": len(graph),
                "before_catalog_hash": before_catalog_hash,
                "after_catalog_hash": after_catalog_hash,
                "revision": current.state["revision"],
                "design_revision": current.state["design_revision"],
                "progress_before": receipt["progress_before"],
                "progress_after": receipt["progress_after"],
                "progress_delta": receipt["progress_delta"],
                "stage": receipt["stage_after"],
            },
        )
