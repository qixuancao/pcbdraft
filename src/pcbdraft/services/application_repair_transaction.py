"""Agent repair staging, preflight, and transactional publication orchestration.

ApplicationService remains the project mutation, lock, revision, and durable-record
authority. This mixin coordinates a revised plan and isolated candidate through
preflight, then calls host methods for every authoritative state transition.
"""
# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _unconfigured(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("application repair-transaction hooks are not configured")


_normalize_repair_feedback: Callable[..., Any] = _unconfigured
_validation_error: Callable[[str], BaseException] = _unconfigured
_pcbdraft_error: Callable[[str], BaseException] = _unconfigured
_pcbdraft_error_type: Callable[[], type[BaseException]] = _unconfigured
_agent_design_request_from_dict: Callable[[dict[str, Any]], Any] = _unconfigured
_circuit_plan_from_dict: Callable[[dict[str, Any]], Any] = _unconfigured
_load_json_limited: Callable[..., Any] = _unconfigured
_pending_request_name: Callable[[], str] = _unconfigured
_pending_plan_name: Callable[[], str] = _unconfigured
_pending_design_name: Callable[[], str] = _unconfigured
_pending_parts_name: Callable[[], str] = _unconfigured
_app_file_limit: Callable[[], int] = _unconfigured
_open_managed_project: Callable[..., Any] = _unconfigured
_resource_lock: Callable[..., Any] = _unconfigured
_utc_timestamp: Callable[[], str] = _unconfigured
_planner_symbol_context: Callable[..., Any] = _unconfigured
_new_run_id: Callable[[], str] = _unconfigured
_compile_agent_plan: Callable[..., Any] = _unconfigured
_atomic_write_json: Callable[..., Any] = _unconfigured
_make_directory: Callable[..., Any] = _unconfigured
_attach_progress: Callable[..., Any] = _unconfigured
_materialize_managed_design: Callable[..., Any] = _unconfigured
_validate_managed_project: Callable[..., Any] = _unconfigured
_validation_feedback_from_levels: Callable[..., Any] = _unconfigured
_semantic_diff: Callable[..., Any] = _unconfigured
_unavailable_consistency_report: Callable[..., Any] = _unconfigured
_sanitize_secret_text: Callable[[str], str] = _unconfigured
_operation_failure_code: Callable[..., Any] = _unconfigured
_progress_vector_unknown: Callable[[int], Any] = _unconfigured
_engineering_stage_not_started: Callable[[], Any] = _unconfigured
_stage_projection: Callable[..., Any] = _unconfigured
_transaction_progress_projection: Callable[[dict[str, Any]], Any] = _unconfigured


def _configure_legacy_application_hooks(
    *,
    normalize_repair_feedback_hook: Callable[..., Any],
    validation_error_hook: Callable[[str], BaseException],
    pcbdraft_error_hook: Callable[[str], BaseException],
    pcbdraft_error_type_hook: Callable[[], type[BaseException]],
    agent_design_request_from_dict_hook: Callable[[dict[str, Any]], Any],
    circuit_plan_from_dict_hook: Callable[[dict[str, Any]], Any],
    load_json_limited_hook: Callable[..., Any],
    pending_request_name_hook: Callable[[], str],
    pending_plan_name_hook: Callable[[], str],
    pending_design_name_hook: Callable[[], str],
    pending_parts_name_hook: Callable[[], str],
    app_file_limit_hook: Callable[[], int],
    open_managed_project_hook: Callable[..., Any],
    resource_lock_hook: Callable[..., Any],
    utc_timestamp_hook: Callable[[], str],
    planner_symbol_context_hook: Callable[..., Any],
    new_run_id_hook: Callable[[], str],
    compile_agent_plan_hook: Callable[..., Any],
    atomic_write_json_hook: Callable[..., Any],
    make_directory_hook: Callable[..., Any],
    attach_progress_hook: Callable[..., Any],
    materialize_managed_design_hook: Callable[..., Any],
    validate_managed_project_hook: Callable[..., Any],
    validation_feedback_from_levels_hook: Callable[..., Any],
    semantic_diff_hook: Callable[..., Any],
    unavailable_consistency_report_hook: Callable[..., Any],
    sanitize_secret_text_hook: Callable[[str], str],
    operation_failure_code_hook: Callable[..., Any],
    progress_vector_unknown_hook: Callable[[int], Any],
    engineering_stage_not_started_hook: Callable[[], Any],
    stage_projection_hook: Callable[..., Any],
    transaction_progress_projection_hook: Callable[[dict[str, Any]], Any],
) -> None:
    """Install adapters that resolve historical application globals at call time."""

    global _normalize_repair_feedback
    global _validation_error
    global _pcbdraft_error
    global _pcbdraft_error_type
    global _agent_design_request_from_dict
    global _circuit_plan_from_dict
    global _load_json_limited
    global _pending_request_name
    global _pending_plan_name
    global _pending_design_name
    global _pending_parts_name
    global _app_file_limit
    global _open_managed_project
    global _resource_lock
    global _utc_timestamp
    global _planner_symbol_context
    global _new_run_id
    global _compile_agent_plan
    global _atomic_write_json
    global _make_directory
    global _attach_progress
    global _materialize_managed_design
    global _validate_managed_project
    global _validation_feedback_from_levels
    global _semantic_diff
    global _unavailable_consistency_report
    global _sanitize_secret_text
    global _operation_failure_code
    global _progress_vector_unknown
    global _engineering_stage_not_started
    global _stage_projection
    global _transaction_progress_projection

    _normalize_repair_feedback = normalize_repair_feedback_hook
    _validation_error = validation_error_hook
    _pcbdraft_error = pcbdraft_error_hook
    _pcbdraft_error_type = pcbdraft_error_type_hook
    _agent_design_request_from_dict = agent_design_request_from_dict_hook
    _circuit_plan_from_dict = circuit_plan_from_dict_hook
    _load_json_limited = load_json_limited_hook
    _pending_request_name = pending_request_name_hook
    _pending_plan_name = pending_plan_name_hook
    _pending_design_name = pending_design_name_hook
    _pending_parts_name = pending_parts_name_hook
    _app_file_limit = app_file_limit_hook
    _open_managed_project = open_managed_project_hook
    _resource_lock = resource_lock_hook
    _utc_timestamp = utc_timestamp_hook
    _planner_symbol_context = planner_symbol_context_hook
    _new_run_id = new_run_id_hook
    _compile_agent_plan = compile_agent_plan_hook
    _atomic_write_json = atomic_write_json_hook
    _make_directory = make_directory_hook
    _attach_progress = attach_progress_hook
    _materialize_managed_design = materialize_managed_design_hook
    _validate_managed_project = validate_managed_project_hook
    _validation_feedback_from_levels = validation_feedback_from_levels_hook
    _semantic_diff = semantic_diff_hook
    _unavailable_consistency_report = unavailable_consistency_report_hook
    _sanitize_secret_text = sanitize_secret_text_hook
    _operation_failure_code = operation_failure_code_hook
    _progress_vector_unknown = progress_vector_unknown_hook
    _engineering_stage_not_started = engineering_stage_not_started_hook
    _stage_projection = stage_projection_hook
    _transaction_progress_projection = transaction_progress_projection_hook


class ApplicationRepairTransactionMixin:
    """Prepare and preflight one repair candidate without owning project state."""

    def prepare_agent_repair(
        self,
        project_id: str,
        feedback: dict[str, Any],
        *,
        timeout: float = 180.0,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Revise a plan from bounded tool evidence and stage it transactionally.

        A project without an authoritative design receives a replacement pending
        plan.  A generated project is never edited in place: its replacement is
        generated and validated under ``transactions/`` before it can be applied.
        """

        normalized = _normalize_repair_feedback(feedback)
        project = self._open(project_id)
        expected_revision = self._bind_expected_revision(
            project, expected_revision, operation="plan repair"
        )
        if project.state["status"] not in {
            "generation_failed",
            "generated",
            "validated",
            "validation_failed",
            "repair_failed",
            "released",
            "release_failed",
            "interrupted",
        }:
            raise _validation_error("project is not eligible for automatic plan repair")
        if project.state["active_transaction"] is not None:
            raise _validation_error("project already has a staged semantic change")
        request = _agent_design_request_from_dict(
            _load_json_limited(
                project.root / _pending_request_name(), _app_file_limit()
            )
        )
        previous_plan = _circuit_plan_from_dict(
            _load_json_limited(project.root / _pending_plan_name(), _app_file_limit())
        )
        if request.design_id != previous_plan.design_id:
            raise _validation_error("pending repair request and plan identities differ")
        authoritative = None
        baseline_design_revision = int(project.state["design_revision"])
        before_progress: Any | None = None
        before_stage: Any | None = None
        before_consistency: Any | None = None
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            authoritative = _open_managed_project(project.design_root)
            authoritative.assert_synchronized()
            if authoritative.design.design_id != request.design_id:
                raise _validation_error(
                    "authoritative design identity differs from the pending repair plan"
                )
            before_progress, before_stage, before_consistency = (
                self._managed_progress_and_stage(
                    project,
                    authoritative,
                    baseline_design_revision,
                )
            )
        prior_status = project.state["status"]
        prior_validation = project.state["last_validation"]
        prior_preview = project.state["last_preview"]
        prior_release = project.state["last_release"]
        with _resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise _validation_error("project changed before plan repair started")
            current.state["status"] = "repairing"
            current.state["revision"] += 1
            current.state["updated_at"] = _utc_timestamp()
            self._event(
                current.state,
                current.root,
                "repair.started",
                f"Revising the circuit plan (attempt {normalized['attempt']})",
            )
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]

        reviser = getattr(self.provider, "revise_plan", None)
        try:
            if not callable(reviser) or not getattr(
                self.provider, "supports_planning", True
            ):
                raise _pcbdraft_error(
                    "the selected provider cannot revise a circuit plan from tool feedback"
                )
            revised_plan = reviser(
                request,
                previous_plan,
                normalized,
                symbol_context=_planner_symbol_context(request),
                project_dir=project.root,
                run_dir=project.root / "provider-runs" / _new_run_id(),
                timeout=timeout,
            )
            if revised_plan.canonical_bytes() == previous_plan.canonical_bytes():
                raise _validation_error(
                    "repair provider returned the unchanged circuit plan"
                )
            compilation = _compile_agent_plan(request, revised_plan)
        except BaseException as exc:
            self._record_failure(
                project_id,
                expected_revision,
                "repair_failed",
                "repair.failed",
                str(exc),
            )
            raise

        proposal = project.conversation.get("proposal")
        revised_proposal = (
            self._attach_plan(proposal, compilation)
            if isinstance(proposal, dict)
            else None
        )
        with _resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            if current.state["revision"] != expected_revision:
                raise _validation_error(
                    "project changed while its plan was being revised"
                )
            _atomic_write_json(
                current.root / _pending_request_name(), compilation.request.to_dict()
            )
            _atomic_write_json(
                current.root / _pending_plan_name(), compilation.plan.to_dict()
            )
            _atomic_write_json(
                current.root / _pending_design_name(), compilation.design.to_dict()
            )
            _atomic_write_json(
                current.root / _pending_parts_name(), compilation.graph.to_dict()
            )
            if revised_proposal is not None:
                current.conversation["proposal"] = revised_proposal
            current.state["revision"] += 1
            current.state["updated_at"] = _utc_timestamp()
            if authoritative is None:
                current.state["status"] = "awaiting_confirmation"
                text = (
                    "A replacement circuit plan was compiled from retained tool "
                    "evidence and is ready for native KiCad generation."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "repair_plan",
                    text,
                    data={"attempt": normalized["attempt"]},
                )
                self._event(current.state, current.root, "repair.plan_ready", text)
            self._write_records(current.root, current.state, current.conversation)
            expected_revision = current.state["revision"]
        if authoritative is None:
            return self.open_project(project_id)

        transaction_id = _new_run_id()
        transaction = _make_directory(project.root / "transactions" / transaction_id)
        staged = transaction / "staged"
        receipt_path = transaction / "receipt.json"
        receipt: dict[str, Any] = {
            "schema": "pcbdraft-agent-repair-transaction",
            "version": 2,
            "status": "preparing",
            "created_at": _utc_timestamp(),
            "request": normalized["summary"],
            "feedback": normalized,
            "before_hash": authoritative.design.content_hash(),
            "after_hash": compilation.design.content_hash(),
            "prior_status": prior_status,
            "prior_validation": prior_validation,
            "prior_preview": prior_preview,
            "prior_release": prior_release,
            "validation": None,
            "result_status": None,
            "baseline_design_revision": baseline_design_revision,
            "candidate_revision": baseline_design_revision + 1,
            "postconditions": [],
            "artifact": {},
        }
        if before_progress is None or before_stage is None:
            raise _validation_error("repair transaction lacks authoritative progress")
        _attach_progress(
            receipt,
            before_progress,
            before_progress,
            before_stage,
            before_stage,
        )
        receipt["convergence_classification"] = receipt["progress_delta"][
            "classification"
        ]
        _atomic_write_json(receipt_path, receipt)
        try:
            _materialize_managed_design(
                compilation.request,
                compilation.design,
                staged,
                graph=compilation.graph,
                plan=compilation.plan,
                retain_failed_attempt=transaction / "failed-native",
            )
            candidate = _open_managed_project(staged)
            candidate.assert_synchronized()
            validation_run = _validate_managed_project(
                candidate,
                output=transaction / "validation",
                timeout=timeout,
                canonical_revision=expected_revision,
                design_revision=baseline_design_revision + 1,
            )
            self._bind_aggregate_validation_revision(
                transaction / "validation",
                candidate.design.content_hash(),
                baseline_design_revision + 1,
            )
            validation_report = _load_json_limited(
                validation_run.report_path, _app_file_limit()
            )
            levels = validation_report["levels"]
            candidate_feedback = _validation_feedback_from_levels(
                levels, attempt=normalized["attempt"]
            )
            validation_summary = {
                "report": validation_run.report_path.relative_to(
                    transaction
                ).as_posix(),
                "report_sha256": validation_run.report_sha256,
                "candidate_ready": validation_run.candidate_ready,
                "production_evidence_complete": (
                    validation_run.production_evidence_complete
                ),
                "production_ready": validation_run.production_ready,
                "production_claimed": False,
                "source_design_revision": baseline_design_revision + 1,
                "source_content_hash": candidate.design.content_hash(),
                "assurance": str(
                    candidate.design.metadata.get("assurance", "provisional")
                ),
            }
            _atomic_write_json(
                transaction / "semantic-diff.json",
                _semantic_diff(authoritative.design, candidate.design),
            )
            candidate_progress, candidate_stage, candidate_consistency = (
                self._managed_progress_and_stage(
                    project,
                    candidate,
                    baseline_design_revision + 1,
                    validation_root=transaction / "validation",
                    include_routing_failures=False,
                )
            )
            verified_candidate = self._require_current_native_consistency(
                candidate_consistency,
                baseline_design_revision + 1,
                label="staged candidate",
            )
            _atomic_write_json(
                transaction / "native-consistency-before.json",
                (
                    before_consistency.to_dict()
                    if before_consistency is not None
                    else _unavailable_consistency_report(
                        baseline_design_revision
                    ).to_dict()
                ),
            )
            _atomic_write_json(
                transaction / "native-consistency-candidate.json",
                verified_candidate.to_dict(),
            )
            receipt["artifact"] = {
                "semantic_diff": "semantic-diff.json",
                "native_consistency_before": "native-consistency-before.json",
                "native_consistency_candidate": "native-consistency-candidate.json",
                "validation": "validation",
            }
            receipt["candidate_progress"] = candidate_progress.to_dict()
            receipt["candidate_stage"] = candidate_stage.to_dict()
            receipt["postconditions"] = [
                {
                    "name": "candidate_native_consistency",
                    "passed": verified_candidate.consistency_passed,
                }
            ]
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["failed_at"] = _utc_timestamp()
            receipt["failure"] = _sanitize_secret_text(str(exc))[:2048]
            receipt["error_code"] = _operation_failure_code(
                exc, stage="native_consistency", tool_name="repair_candidate"
            )
            _attach_progress(
                receipt,
                before_progress,
                before_progress,
                before_stage,
                before_stage,
            )
            receipt["convergence_classification"] = receipt["progress_delta"][
                "classification"
            ]
            _atomic_write_json(receipt_path, receipt)
            self._record_failure(
                project_id,
                expected_revision,
                "repair_failed",
                "repair.failed",
                str(exc),
            )
            raise

        receipt["validation"] = validation_summary
        rejected = candidate_feedback is not None
        if rejected:
            receipt["repair_feedback"] = candidate_feedback
        else:
            receipt["result_status"] = (
                "validated" if validation_run.candidate_ready else "generated"
            )
        # Candidate-only evidence may be durable while the top-level live
        # progress remains neutral.  The terminal ready/rejected fact is
        # published only after matching project records and its event.
        prepublication_receipt = copy.deepcopy(receipt)
        _atomic_write_json(receipt_path, prepublication_receipt)
        with _resource_lock(project.root, self.locks_root):
            current = self._open(project_id)
            original_state = copy.deepcopy(current.state)
            original_conversation = copy.deepcopy(current.conversation)
            event_path: Path | None = None
            try:
                if current.state["revision"] != expected_revision:
                    raise _validation_error(
                        "project changed while a repair candidate was validated"
                    )
                current_managed = _open_managed_project(current.design_root)
                current_managed.assert_synchronized()
                if current_managed.design.content_hash() != receipt["before_hash"]:
                    raise _validation_error(
                        "authoritative design changed while a repair was staged"
                    )
                current.state["status"] = (
                    "repair_failed" if rejected else "change_ready"
                )
                if not rejected:
                    current.state["active_transaction"] = transaction_id
                current.state["revision"] += 1
                current.state["updated_at"] = _utc_timestamp()
                text = (
                    "The repair candidate retained deterministic L1-L3 failures; "
                    "the authoritative design was not changed."
                    if rejected
                    else "A replacement design passed deterministic L1-L3 repair "
                    "gates and is staged for atomic application."
                )
                self._append_message(
                    current.conversation,
                    "assistant",
                    "repair_rejected" if rejected else "repair_ready",
                    text,
                    data=(
                        {
                            "transaction_id": transaction_id,
                            "repair_feedback": candidate_feedback,
                        }
                        if rejected
                        else {"transaction_id": transaction_id}
                    ),
                )
                event_path = (
                    current.root
                    / "events"
                    / f"{current.state['event_sequence'] + 1:08d}.json"
                )
                self._event(
                    current.state,
                    current.root,
                    "repair.candidate_failed" if rejected else "repair.ready",
                    text,
                    level="error" if rejected else "info",
                )
                self._write_records(current.root, current.state, current.conversation)
                terminal = "rejected" if rejected else "ready"
                receipt["status"] = terminal
                receipt[f"{terminal}_at"] = _utc_timestamp()
                receipt["publication"] = {
                    "status": "committed",
                    "rollback": {
                        "state": "committed",
                        "performed": False,
                        "live_unchanged": True,
                    },
                }
                _atomic_write_json(receipt_path, receipt)
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                try:
                    _atomic_write_json(
                        current.root / "conversation.json", original_conversation
                    )
                    _atomic_write_json(current.root / "project.json", original_state)
                    if event_path is not None and event_path.is_file():
                        event_path.unlink()
                except BaseException as rollback_exc:  # noqa: BLE001 - audit rollback
                    rollback_failures.append(rollback_exc)
                receipt.clear()
                receipt.update(copy.deepcopy(prepublication_receipt))
                receipt["publication"] = {
                    "status": (
                        "rollback_incomplete" if rollback_failures else "failed"
                    ),
                    "error_code": "publication_failed",
                    "failure": _sanitize_secret_text(str(exc))[:2048],
                    "rollback": {
                        "state": "incomplete" if rollback_failures else "restored",
                        "performed": not rollback_failures,
                        "live_unchanged": not rollback_failures,
                    },
                }
                if rollback_failures:
                    receipt["status"] = "rollback_incomplete"
                    _attach_progress(
                        receipt,
                        before_progress,
                        _progress_vector_unknown(before_progress.source_revision),
                        before_stage,
                        _stage_projection(
                            _engineering_stage_not_started(),
                            False,
                            ("rollback_state_unknown",),
                        ),
                    )
                    receipt["convergence_classification"] = receipt["progress_delta"][
                        "classification"
                    ]
                try:
                    _atomic_write_json(receipt_path, receipt)
                except _pcbdraft_error_type():
                    pass
                if rollback_failures:
                    raise _pcbdraft_error(
                        "repair candidate publication failed and rollback was incomplete"
                    ) from exc
                raise
        result = self.open_project(project_id)
        result["transaction_progress"] = _transaction_progress_projection(receipt)
        return result
