"""Atomic semantic/native PCB operation transaction workflow.

ApplicationService remains the composition root and project-record authority.
It supplies late-bound adapters for staging, native verification, locking,
publication, rollback, progress evidence, and legacy application patch points.
This module does not import the application coordinator back.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pcbdraft.agent.plan import AgentDesignRequest
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.domain.component_qualification import COMPONENT_QUALIFICATION_SCHEMA
from pcbdraft.domain.ir import Design
from pcbdraft.domain.operations import ChangeSet
from pcbdraft.kicad.consistency import NATIVE_OPERATION_POLICIES
from pcbdraft.kicad.routing import (
    ROUTING_FAILURE_CODES,
    RoutingFailure,
    RoutingFailureError,
)
from pcbdraft.services.native_operations import _PCBOperationPostconditionError
from pcbdraft.services.progress import (
    ConvergenceDecision,
    EngineeringStage,
    MetricValue,
    ProgressVector,
    StageProjection,
)

_NATIVE_DELTA_OPERATIONS = frozenset(NATIVE_OPERATION_POLICIES) - {
    "register_kicad_part"
}


def _unconfigured(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("application PCB-operation hooks are not configured")


apply_change_set: Callable[..., Any] = _unconfigured
qualify_components: Callable[..., Any] = _unconfigured
semantic_diff: Callable[..., Any] = _unconfigured
new_run_id: Callable[..., Any] = _unconfigured
make_directory: Callable[..., Any] = _unconfigured
load_generation_request: Callable[..., Any] = _unconfigured
utc_timestamp: Callable[..., Any] = _unconfigured
_route_state_key: Callable[..., Any] = _unconfigured
_route_state_record: Callable[..., Any] = _unconfigured
_progress_vector: Callable[..., Any] = _unconfigured
_progress_stage_evidence: Callable[..., Any] = _unconfigured
derive_stage: Callable[..., Any] = _unconfigured
_attach_progress: Callable[..., Any] = _unconfigured
atomic_write_json: Callable[..., Any] = _unconfigured
_reject_stale_copper_transform: Callable[..., Any] = _unconfigured
materialize_managed_design: Callable[..., Any] = _unconfigured
open_managed_project: Callable[..., Any] = _unconfigured
inspect_native_consistency: Callable[..., Any] = _unconfigured
_unavailable_consistency_report: Callable[..., Any] = _unconfigured
_native_postconditions: Callable[..., Any] = _unconfigured
_native_board_projection: Callable[..., Any] = _unconfigured
_consistency_rejection: Callable[..., Any] = _unconfigured
compare_native_operation_delta: Callable[..., Any] = _unconfigured
_native_schematic_projection: Callable[..., Any] = _unconfigured
_native_delta_postconditions: Callable[..., Any] = _unconfigured
_sanitize_secret_text: Callable[..., Any] = _unconfigured
_operation_failure_code: Callable[..., Any] = _unconfigured
_routing_failure_context: Callable[..., Any] = _unconfigured
_bind_transaction_failure: Callable[..., Any] = _unconfigured
ResourceLock: Callable[..., Any] = _unconfigured


def _configure_legacy_application_hooks(
    *,
    apply_change_set_hook: Callable[..., Any],
    qualify_components_hook: Callable[..., Any],
    semantic_diff_hook: Callable[..., Any],
    new_run_id_hook: Callable[..., Any],
    make_directory_hook: Callable[..., Any],
    load_generation_request_hook: Callable[..., Any],
    utc_timestamp_hook: Callable[..., Any],
    route_state_key_hook: Callable[..., Any],
    route_state_record_hook: Callable[..., Any],
    progress_vector_hook: Callable[..., Any],
    progress_stage_evidence_hook: Callable[..., Any],
    derive_stage_hook: Callable[..., Any],
    attach_progress_hook: Callable[..., Any],
    atomic_write_json_hook: Callable[..., Any],
    reject_stale_copper_transform_hook: Callable[..., Any],
    materialize_managed_design_hook: Callable[..., Any],
    open_managed_project_hook: Callable[..., Any],
    inspect_native_consistency_hook: Callable[..., Any],
    unavailable_consistency_report_hook: Callable[..., Any],
    native_postconditions_hook: Callable[..., Any],
    native_board_projection_hook: Callable[..., Any],
    consistency_rejection_hook: Callable[..., Any],
    compare_native_operation_delta_hook: Callable[..., Any],
    native_schematic_projection_hook: Callable[..., Any],
    native_delta_postconditions_hook: Callable[..., Any],
    sanitize_secret_text_hook: Callable[..., Any],
    operation_failure_code_hook: Callable[..., Any],
    routing_failure_context_hook: Callable[..., Any],
    bind_transaction_failure_hook: Callable[..., Any],
    resource_lock_hook: Callable[..., Any],
) -> None:
    """Install adapters that resolve historical application globals at call time."""

    global apply_change_set
    global qualify_components
    global semantic_diff
    global new_run_id
    global make_directory
    global load_generation_request
    global utc_timestamp
    global _route_state_key
    global _route_state_record
    global _progress_vector
    global _progress_stage_evidence
    global derive_stage
    global _attach_progress
    global atomic_write_json
    global _reject_stale_copper_transform
    global materialize_managed_design
    global open_managed_project
    global inspect_native_consistency
    global _unavailable_consistency_report
    global _native_postconditions
    global _native_board_projection
    global _consistency_rejection
    global compare_native_operation_delta
    global _native_schematic_projection
    global _native_delta_postconditions
    global _sanitize_secret_text
    global _operation_failure_code
    global _routing_failure_context
    global _bind_transaction_failure
    global ResourceLock

    apply_change_set = apply_change_set_hook
    qualify_components = qualify_components_hook
    semantic_diff = semantic_diff_hook
    new_run_id = new_run_id_hook
    make_directory = make_directory_hook
    load_generation_request = load_generation_request_hook
    utc_timestamp = utc_timestamp_hook
    _route_state_key = route_state_key_hook
    _route_state_record = route_state_record_hook
    _progress_vector = progress_vector_hook
    _progress_stage_evidence = progress_stage_evidence_hook
    derive_stage = derive_stage_hook
    _attach_progress = attach_progress_hook
    atomic_write_json = atomic_write_json_hook
    _reject_stale_copper_transform = reject_stale_copper_transform_hook
    materialize_managed_design = materialize_managed_design_hook
    open_managed_project = open_managed_project_hook
    inspect_native_consistency = inspect_native_consistency_hook
    _unavailable_consistency_report = unavailable_consistency_report_hook
    _native_postconditions = native_postconditions_hook
    _native_board_projection = native_board_projection_hook
    _consistency_rejection = consistency_rejection_hook
    compare_native_operation_delta = compare_native_operation_delta_hook
    _native_schematic_projection = native_schematic_projection_hook
    _native_delta_postconditions = native_delta_postconditions_hook
    _sanitize_secret_text = sanitize_secret_text_hook
    _operation_failure_code = operation_failure_code_hook
    _routing_failure_context = routing_failure_context_hook
    _bind_transaction_failure = bind_transaction_failure_hook
    ResourceLock = resource_lock_hook


class ApplicationPCBOperationsMixin:
    """Stage, verify, publish, and roll back one concrete PCB operation."""

    def apply_pcb_operation(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Stage, materialize, verify, and publish exactly one typed operation."""

        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation=tool_name
        )
        if not project.design_root.is_dir() or project.design_root.is_symlink():
            raise ValidationError("project has no synchronized design to modify")
        authoritative = open_managed_project(project.design_root)
        authoritative.assert_synchronized()
        before_graph = authoritative.graph.with_footprint_overrides(
            authoritative.design
        )
        operations = self._flat_semantic_operations(
            tool_name,
            arguments,
            authoritative.design,
            graph=before_graph,
        )
        change_set = ChangeSet.from_dict(
            {
                "schema": "pcbdraft-change-set",
                "version": 1,
                "id": f"flat_{secrets.token_hex(6)}",
                "base_hash": authoritative.design.content_hash(),
                "intent": f"Apply concrete PCB operation {tool_name}.",
                "actor": "flat-pcb-toolbox",
                "operations": operations,
                "provenance": [f"tool:{tool_name}"],
            }
        )
        candidate = apply_change_set(authoritative.design, change_set)
        request = load_generation_request(authoritative.requirements_path)
        graph = authoritative.graph.with_footprint_overrides(candidate)
        if isinstance(request, AgentDesignRequest):
            qualification = qualify_components(candidate, graph)
            if qualification.pad_mapping_failures:
                raise ValidationError(
                    "component qualification contains invalid local pad mappings"
                )
            document = candidate.to_dict()
            document["metadata"]["component_qualification_schema"] = (
                COMPONENT_QUALIFICATION_SCHEMA
            )
            document["metadata"]["component_qualification_hash"] = (
                qualification.sha256()
            )
            candidate = Design.from_dict(document)
        candidate_diff = semantic_diff(authoritative.design, candidate)
        transaction_id = new_run_id()
        transaction = make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        before = transaction / "before"
        receipt_path = transaction / "receipt.json"
        route_net_id = str(arguments["net_id"]) if tool_name == "route_net" else None
        baseline_design_revision = int(project.state["design_revision"])
        candidate_design_revision = baseline_design_revision + 1
        convergence_state_key = (
            _route_state_key(
                authoritative.design,
                route_net_id,
                baseline_design_revision,
            )
            if route_net_id is not None
            else f"revision={baseline_design_revision}"
        )
        convergence_state = (
            _route_state_record(
                authoritative.design,
                route_net_id,
                baseline_design_revision,
            )
            if route_net_id is not None
            else None
        )
        before_consistency, before_board = self._native_progress_sources(
            authoritative, baseline_design_revision, before_graph
        )
        before_erc, _unused_fatal, before_erc_check = self._retained_check_progress(
            project,
            authoritative.design.content_hash(),
            "run_erc",
            baseline_design_revision,
        )
        before_drc_metric, before_fatal_drc, before_drc_check = (
            self._retained_check_progress(
                project,
                authoritative.design.content_hash(),
                "run_drc",
                baseline_design_revision,
            )
        )
        before_progress = _progress_vector(
            authoritative.design,
            before_graph,
            baseline_design_revision,
            consistency=before_consistency,
            board=before_board,
            fatal_drc=before_fatal_drc,
            error_drc=before_drc_metric,
            erc_error=before_erc,
            routing_failure_count=self._routing_failure_count(
                project, baseline_design_revision, convergence_state_key
            ),
        )
        before_stage_evidence = _progress_stage_evidence(
            authoritative.design,
            baseline_design_revision,
            requirements_frozen=bool(authoritative.design.requirements),
            consistency=before_consistency,
            progress=before_progress,
            erc_check=before_erc_check,
            drc_check=before_drc_check,
        )
        before_stage = derive_stage(before_progress, before_stage_evidence)
        convergence = (
            self._route_convergence_decision(project, state_key=convergence_state_key)
            if route_net_id is not None
            else ConvergenceDecision(True, "continue", None, 0, 0)
        )
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-flat-operation-receipt",
            "version": 2,
            "status": "preparing",
            "operation": tool_name,
            "created_at": utc_timestamp(),
            "before_hash": authoritative.design.content_hash(),
            "after_hash": candidate.content_hash(),
            "baseline_revision": expected_revision,
            "baseline_design_revision": baseline_design_revision,
            "candidate_revision": candidate_design_revision,
            "committed_revision": None,
            "committed_design_revision": None,
            "consistency_passed": False,
            "postconditions": [],
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
            "convergence_state_key": convergence_state_key,
            "convergence_state": convergence_state,
            "convergence": convergence.to_dict(),
            "transaction_scope": {
                "kind": tool_name,
                "entry_count": len(operations),
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
        atomic_write_json(transaction / "semantic-diff.json", candidate_diff)
        stage = "materialization"
        try:
            if not convergence.allowed:
                raise _PCBOperationPostconditionError(
                    convergence.action,
                    "route retry stopped until placement, layer, order, or revision state changes",
                )
            _reject_stale_copper_transform(
                tool_name,
                arguments,
                authoritative.design,
                candidate,
                before_graph=before_graph,
                candidate_graph=graph,
            )
            generated = materialize_managed_design(
                request,
                candidate,
                staged,
                graph=graph,
                plan=authoritative.plan,
                retain_failed_attempt=transaction / "failed-native",
                lock_timeout=min(10.0, timeout),
                auto_place=False,
                route_net_ids=(
                    frozenset({route_net_id})
                    if route_net_id is not None
                    else frozenset()
                ),
                allow_incomplete=True,
            )
            if tool_name == "route_net":
                stage = "routing_probe"
                candidate = self._retain_generated_route(
                    candidate,
                    route_net_id or "",
                    generated.pcb.routing,
                )
                candidate_diff = semantic_diff(authoritative.design, candidate)
                receipt["after_hash"] = candidate.content_hash()
                atomic_write_json(receipt_path, receipt)
                atomic_write_json(transaction / "semantic-diff.json", candidate_diff)
                os.replace(staged, transaction / "routing-probe")
                stage = "routing_commit"
                materialize_managed_design(
                    request,
                    candidate,
                    staged,
                    graph=graph,
                    plan=authoritative.plan,
                    retain_failed_attempt=transaction / "failed-native-final",
                    lock_timeout=min(10.0, timeout),
                    auto_place=False,
                    route_net_ids=frozenset(),
                    allow_incomplete=True,
                )
            staged_project = open_managed_project(staged)
            staged_project.assert_synchronized()
            if staged_project.design.content_hash() != candidate.content_hash():
                raise ValidationError("staged semantic design hash changed")
            stage = "native_consistency"
            consistency_failure: PCBDraftError | None = None
            try:
                consistency = inspect_native_consistency(
                    candidate,
                    staged_project.schematic_path,
                    staged_project.board_path,
                    candidate_revision=candidate_design_revision,
                    graph=graph,
                    require_routed_net_ids=(
                        frozenset({route_net_id})
                        if route_net_id is not None
                        else frozenset()
                    ),
                )
            except PCBDraftError as exc:
                consistency_failure = exc
                consistency = _unavailable_consistency_report(candidate_design_revision)
            atomic_write_json(
                transaction / "native-consistency.json", consistency.to_dict()
            )
            receipt["artifact"]["native_consistency"] = "native-consistency.json"
            receipt["consistency_passed"] = consistency.consistency_passed
            receipt["postconditions"] = _native_postconditions(tool_name, consistency)
            receipt["native_delta"] = {
                "mismatch_count": len(consistency.mismatches),
                "required_routed_net": route_net_id,
            }
            receipt["intended_delta"] = candidate_diff["summary"]
            try:
                candidate_board = _native_board_projection(staged_project)
            except PCBDraftError:
                candidate_board = None
            after_progress = _progress_vector(
                candidate,
                graph,
                candidate_design_revision,
                consistency=consistency,
                board=candidate_board,
                fatal_drc=before_progress.fatal_drc_count.for_revision(
                    candidate_design_revision
                ),
                error_drc=before_progress.error_drc_count.for_revision(
                    candidate_design_revision
                ),
                erc_error=before_progress.erc_error_count.for_revision(
                    candidate_design_revision
                ),
                routing_failure_count=0,
            )
            after_stage_evidence = _progress_stage_evidence(
                candidate,
                candidate_design_revision,
                requirements_frozen=bool(staged_project.design.requirements),
                consistency=consistency,
                progress=after_progress,
                erc_check=before_erc_check.for_revision(candidate_design_revision),
                drc_check=before_drc_check.for_revision(candidate_design_revision),
            )
            after_stage = derive_stage(after_progress, after_stage_evidence)
            _attach_progress(
                receipt,
                before_progress,
                after_progress,
                before_stage,
                after_stage,
            )
            atomic_write_json(receipt_path, receipt)
            if consistency_failure is not None:
                if tool_name == "route_net":
                    failure = RoutingFailure(
                        code="native_commit_failed",
                        net=route_net_id or "unknown",
                        blocking_summary="native KiCad inspection failed",
                        recommendations=("inspect_native_artifact",),
                        nearest_obstacle_class="native_artifact",
                        state_revision=candidate_design_revision,
                        state_context=_routing_failure_context(candidate, route_net_id),
                    )
                    raise _PCBOperationPostconditionError(
                        failure.code,
                        failure.diagnostic,
                        routing_failure=failure,
                    ) from consistency_failure
                raise _PCBOperationPostconditionError(
                    "native_verification_failed",
                    "native KiCad inspection failed",
                ) from consistency_failure
            if not consistency.consistency_passed:
                raise _consistency_rejection(
                    tool_name,
                    route_net_id,
                    consistency,
                    design=candidate,
                    state_revision=candidate_design_revision,
                )
            if tool_name in _NATIVE_DELTA_OPERATIONS:
                stage = "native_delta"
                native_operation_delta = compare_native_operation_delta(
                    tool_name,
                    arguments,
                    authoritative.design,
                    candidate,
                    before_board or _native_board_projection(authoritative),
                    candidate_board or _native_board_projection(staged_project),
                    before_schematic=_native_schematic_projection(authoritative),
                    after_schematic=_native_schematic_projection(staged_project),
                    graph=graph,
                )
                atomic_write_json(
                    transaction / "native-operation-delta.json",
                    native_operation_delta.to_dict(),
                )
                receipt["artifact"]["native_delta"] = "native-operation-delta.json"
                receipt["native_delta"].update(
                    {
                        "operation_checked": True,
                        "policy": native_operation_delta.policy,
                        "passed": native_operation_delta.passed,
                        "failed_checks": [
                            item.name
                            for item in native_operation_delta.checks
                            if not item.passed
                        ][:4],
                    }
                )
                receipt["postconditions"].extend(
                    _native_delta_postconditions(native_operation_delta)
                )
                atomic_write_json(receipt_path, receipt)
                if not native_operation_delta.passed:
                    error_code = (
                        "native_commit_failed"
                        if tool_name == "route_net"
                        else "native_delta_failed"
                    )
                    raise _PCBOperationPostconditionError(
                        error_code,
                        "native KiCad operation delta postcondition failed: "
                        + ", ".join(
                            item.name
                            for item in native_operation_delta.checks
                            if not item.passed
                        ),
                    )
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            receipt["error_code"] = _operation_failure_code(
                exc, stage=stage, tool_name=tool_name
            )
            receipt["rollback_performed"] = False
            receipt["rollback"] = {
                "state": "not_required",
                "performed": False,
                "live_unchanged": True,
            }
            if isinstance(exc, RoutingFailureError):
                receipt["routing_failure"] = exc.failure.to_dict()
            elif (
                isinstance(exc, _PCBOperationPostconditionError)
                and exc.routing_failure is not None
            ):
                receipt["routing_failure"] = exc.routing_failure.to_dict()
            elif (
                tool_name == "route_net"
                and receipt["error_code"] in ROUTING_FAILURE_CODES
            ):
                receipt["routing_failure"] = RoutingFailure(
                    code=receipt["error_code"],
                    net=route_net_id or "unknown",
                    blocking_summary=f"route transaction failed during {stage}",
                    recommendations=("inspect_native_artifact",),
                    nearest_obstacle_class="native_artifact",
                    state_revision=candidate_design_revision,
                    state_context=_routing_failure_context(candidate, route_net_id),
                ).to_dict()
            live_after_progress = before_progress
            if tool_name == "route_net" and isinstance(
                receipt.get("routing_failure"), Mapping
            ):
                failures = before_progress.routing_failure_count
                current_failures = (
                    failures.value
                    if failures.is_current(baseline_design_revision)
                    and failures.value is not None
                    else 0
                )
                live_after_progress = before_progress.replace_metric(
                    "routing_failure_count",
                    MetricValue.known(current_failures + 1, baseline_design_revision),
                )
            _attach_progress(
                receipt,
                before_progress,
                live_after_progress,
                before_stage,
                before_stage,
            )
            atomic_write_json(receipt_path, receipt)
            _bind_transaction_failure(exc, transaction_id)
            raise

        stage = "publication"
        with ResourceLock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            moved_before = False
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise ValidationError(
                        "project changed while PCB operation was staged"
                    )
                current_design = open_managed_project(current.design_root)
                current_design.assert_synchronized()
                if current_design.design.content_hash() != receipt["before_hash"]:
                    raise ValidationError(
                        "authoritative design changed before publication"
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
                    "pcb.operation_applied",
                    f"Applied concrete PCB operation {tool_name}",
                )
                self._write_records(current.root, current.state, current.conversation)
                # The applied receipt is the final publication write.  If it
                # fails, the surrounding handler can still restore the native
                # design, event, and project records without ever retaining a
                # durable success receipt for the rolled-back transaction.
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
                    exc, stage=stage, tool_name=tool_name
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
                except PCBDraftError:
                    # Preserve the publication failure; an applied receipt was
                    # never authoritative without the matching project record.
                    pass
                if rollback_failures:
                    publication_failure = PCBDraftError(
                        "PCB operation publication failed and rollback was incomplete"
                    )
                    _bind_transaction_failure(publication_failure, transaction_id)
                    raise publication_failure from exc
                _bind_transaction_failure(exc, transaction_id)
                raise
        result = {
            "operation": tool_name,
            "transaction_id": transaction_id,
            "before_hash": receipt["before_hash"],
            "after_hash": receipt["after_hash"],
            "design_revision": current.state["design_revision"],
            "revision": current.state["revision"],
            "changed": candidate_diff["summary"],
            "synchronized": True,
            "consistency_passed": receipt["consistency_passed"],
            "postconditions": receipt["postconditions"],
            "transaction_artifact": transaction_id,
            "progress_before": receipt["progress_before"],
            "progress_after": receipt["progress_after"],
            "progress_delta": receipt["progress_delta"],
            "stage": receipt["stage_after"],
            "convergence": receipt["convergence"],
            "transaction_scope": receipt["transaction_scope"],
        }
        return self._with_tool_result(self.open_project(project_id), result)

    @staticmethod
    def _retain_generated_route(design: Design, net_id: str, routing: Any) -> Design:
        """Promote generated copper for one net into deterministic native intent."""

        net = next((item for item in design.nets if item.id == net_id), None)
        if net is None:
            raise ValidationError(f"net is absent: {net_id}")
        if net.name in routing.unrouted:
            failures = tuple(getattr(routing, "failures", ()))
            failure = next(
                (item for item in failures if item.net == net.name),
                RoutingFailure(
                    code="no_legal_channel",
                    net=net.name,
                    expanded_nodes=int(getattr(routing, "expanded_nodes", 0)),
                    blocking_summary="router could not complete the selected net",
                    recommendations=("change_layer", "reposition_component"),
                    nearest_obstacle_class="unknown",
                    state_revision=design.native_intent.geometry_revision,
                    state_context=_routing_failure_context(design, net_id),
                ),
            )
            raise RoutingFailureError(failure)
        segments = [item for item in routing.segments if item.net == net.name]
        if len(net.endpoints) > 1 and not segments:
            raise RoutingFailureError(
                RoutingFailure(
                    code="native_commit_failed",
                    net=net.name,
                    expanded_nodes=int(getattr(routing, "expanded_nodes", 0)),
                    blocking_summary="router completed without materializable copper",
                    recommendations=("inspect_pad_escape",),
                    nearest_obstacle_class="native_artifact",
                    state_revision=design.native_intent.geometry_revision,
                    state_context=_routing_failure_context(design, net_id),
                )
            )
        document = design.to_dict()
        native = document["native_intent"]

        def stable_id(prefix: str, value: dict[str, Any]) -> str:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:20]}"

        native["routes"] = [
            item for item in native["routes"] if item.get("net") != net_id
        ] + [
            {
                "id": stable_id(
                    "route",
                    {
                        "net": net_id,
                        "layer": item.layer,
                        "x1_mm": item.x1_mm,
                        "y1_mm": item.y1_mm,
                        "x2_mm": item.x2_mm,
                        "y2_mm": item.y2_mm,
                        "width_mm": item.width_mm,
                    },
                ),
                "net": net_id,
                "layer": item.layer,
                "x1_mm": item.x1_mm,
                "y1_mm": item.y1_mm,
                "x2_mm": item.x2_mm,
                "y2_mm": item.y2_mm,
                "width_mm": item.width_mm,
            }
            for item in segments
        ]
        native["vias"] = [
            item for item in native["vias"] if item.get("net") != net_id
        ] + [
            {
                "id": stable_id(
                    "via",
                    {
                        "net": net_id,
                        "x_mm": item.x_mm,
                        "y_mm": item.y_mm,
                        "diameter_mm": item.diameter_mm,
                        "drill_mm": item.drill_mm,
                        "from_layer": item.from_layer,
                        "to_layer": item.to_layer,
                    },
                ),
                "net": net_id,
                "x_mm": item.x_mm,
                "y_mm": item.y_mm,
                "diameter_mm": item.diameter_mm,
                "drill_mm": item.drill_mm,
                "from_layer": item.from_layer,
                "to_layer": item.to_layer,
            }
            for item in routing.vias
            if item.net == net.name
        ]
        return Design.from_dict(document)
