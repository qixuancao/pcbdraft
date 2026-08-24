from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.model.providers import IntentProvider
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.progress import (
    ConvergenceObservation,
    ConvergencePolicy,
    EngineeringStage,
    EvidenceCheck,
    EvidenceStatus,
    MetricValue,
    ProcessStatus,
    ProductSessionTerminalReceipt,
    ProgressClassification,
    ProgressVector,
    StageEvidence,
    StageProjection,
    TaskOutcome,
    compare_progress,
    derive_stage,
    evaluate_convergence,
    store_product_session_terminal,
    terminal_outcome,
)


def _vector(
    revision: int,
    *,
    mismatch: MetricValue | int = 0,
    unresolved: MetricValue | int = 0,
    fatal: MetricValue | int = 0,
    drc: MetricValue | int = 0,
    erc: MetricValue | int = 0,
    unplaced: MetricValue | int = 0,
    routing_failures: MetricValue | int = 0,
) -> ProgressVector:
    def metric(value: MetricValue | int) -> MetricValue:
        return (
            value
            if isinstance(value, MetricValue)
            else MetricValue.known(value, revision)
        )

    return ProgressVector(
        revision,
        metric(mismatch),
        metric(unresolved),
        metric(fatal),
        metric(drc),
        metric(erc),
        metric(unplaced),
        metric(routing_failures),
    )


def _stage_evidence(
    revision: int, *, erc: bool = True, drc: bool = True
) -> StageEvidence:
    passed = EvidenceCheck.known(True, revision)
    return StageEvidence(
        revision,
        passed,
        passed,
        passed,
        passed,
        passed,
        passed,
        EvidenceCheck.known(erc, revision),
        EvidenceCheck.known(drc, revision),
    )


class ProgressTests(unittest.TestCase):
    def test_unknown_is_not_zero_and_round_trips(self) -> None:
        unknown = MetricValue.unknown(3)
        before = _vector(3, unresolved=unknown)
        after = _vector(4, unresolved=MetricValue.known(0, 4))

        delta = compare_progress(before, after)

        self.assertEqual(delta.classification, ProgressClassification.INDETERMINATE)
        self.assertIsNone(
            next(
                item
                for item in delta.metrics
                if item.name == "unresolved_connection_count"
            ).delta
        )
        self.assertEqual(ProgressVector.from_dict(before.to_dict()), before)

    def test_stale_evidence_does_not_create_false_improvement(self) -> None:
        before = _vector(2, drc=MetricValue.stale(5, 1))
        after = _vector(3, drc=MetricValue.known(0, 3))

        delta = compare_progress(before, after)

        self.assertEqual(delta.classification, ProgressClassification.INDETERMINATE)

    def test_unchanged_unknown_safety_evidence_blocks_lower_level_improvement(
        self,
    ) -> None:
        unavailable = MetricValue.unknown(1)
        before = _vector(1, mismatch=unavailable, unplaced=2)
        after = _vector(2, mismatch=unavailable, unplaced=0)

        delta = compare_progress(before, after)

        self.assertEqual(delta.classification, ProgressClassification.INDETERMINATE)
        self.assertIsNone(delta.decisive_metric)

    def test_any_missing_current_evidence_blocks_an_improved_classification(
        self,
    ) -> None:
        before = _vector(1, mismatch=2, drc=MetricValue.unknown(1))
        after = _vector(2, mismatch=1, drc=MetricValue.unknown(2))

        delta = compare_progress(before, after)

        self.assertEqual(delta.classification, ProgressClassification.INDETERMINATE)
        self.assertIsNone(delta.decisive_metric)

    def test_safety_metric_has_priority_over_lower_level_improvement(self) -> None:
        before = _vector(1, mismatch=0, unplaced=4)
        after = _vector(2, mismatch=1, unplaced=0)

        delta = compare_progress(before, after)

        self.assertEqual(delta.classification, ProgressClassification.REGRESSED)
        self.assertEqual(delta.decisive_metric, "semantic_native_mismatch_count")

    def test_neutral_and_improved_classifications_are_distinct(self) -> None:
        before = _vector(1, unresolved=2)
        neutral = _vector(2, unresolved=2)
        improved = _vector(2, unresolved=1)

        self.assertEqual(
            compare_progress(before, neutral).classification,
            ProgressClassification.NEUTRAL,
        )
        self.assertEqual(
            compare_progress(before, improved).classification,
            ProgressClassification.IMPROVED,
        )

    def test_missing_or_stale_stage_evidence_caps_release(self) -> None:
        progress = _vector(4)
        evidence = _stage_evidence(4)
        self.assertTrue(derive_stage(progress, evidence).release_gate_passed)

        stale_erc = StageEvidence(
            4,
            evidence.requirements_frozen,
            evidence.schematic_semantic,
            evidence.native_schematic_confirmed,
            evidence.footprint_net_sync,
            evidence.routing_started,
            evidence.native_connectivity_confirmed,
            EvidenceCheck(True, 3, EvidenceStatus.STALE),
            evidence.drc_checked,
        )
        projection = derive_stage(
            progress.replace_metric("erc_error_count", MetricValue.stale(0, 3)),
            stale_erc,
        )
        self.assertFalse(projection.release_gate_passed)
        self.assertLess(
            list(EngineeringStage).index(projection.stage),
            list(EngineeringStage).index(EngineeringStage.RELEASE_GATE),
        )

    def test_repeated_retry_stops_but_state_change_allows_new_strategy(self) -> None:
        policy = ConvergencePolicy(
            repeated_retry_key_threshold=2,
            consecutive_no_improvement_threshold=4,
        )
        observations = (
            ConvergenceObservation(
                "revision=3|placement=A", ProgressClassification.REGRESSED, "retry-a"
            ),
            ConvergenceObservation(
                "revision=3|placement=A", ProgressClassification.NEUTRAL, "retry-a"
            ),
        )

        blocked = evaluate_convergence(
            observations,
            state_key="revision=3|placement=A",
            retry_key="retry-a",
            policy=policy,
        )
        changed = evaluate_convergence(
            observations,
            state_key="revision=4|placement=B",
            retry_key="retry-b",
            policy=policy,
        )

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.action, "strategy_change_required")
        self.assertTrue(changed.allowed)

    def test_no_progress_policy_returns_explicit_blocked_decision(self) -> None:
        observations = tuple(
            ConvergenceObservation("same", ProgressClassification.NEUTRAL)
            for _index in range(3)
        )
        decision = evaluate_convergence(
            observations,
            state_key="same",
            retry_key=None,
            policy=ConvergencePolicy(2, 3),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, "blocked")
        self.assertEqual(decision.reason, "no_progress")


class ProductTerminalTests(unittest.TestCase):
    def test_application_records_early_return_as_durable_incomplete_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temporary), provider=provider)
            project_id = str(service.create_draft("Terminal receipt")["project"]["id"])

            receipt = service.record_product_session_terminal(
                project_id,
                session_id="hermes-session",
                turn_id="turn-1",
                process_status=ProcessStatus.EXITED,
            )
            repeated = service.record_product_session_terminal(
                project_id,
                session_id="hermes-session",
                turn_id="turn-1",
                process_status=ProcessStatus.EXITED,
            )

            self.assertEqual(receipt, repeated)
            self.assertEqual(receipt["process_status"], "exited")
            self.assertEqual(receipt["task_outcome"], "incomplete")
            self.assertEqual(
                receipt["termination_reason"], "agent_returned_before_gate"
            )
            self.assertEqual(receipt["stage_reached"], "not_started")
            self.assertFalse(receipt["release_gate_passed"])
            artifact = service._open(project_id).root / str(receipt["artifact"])
            self.assertTrue(artifact.is_file())
            self.assertEqual(
                len(
                    list(
                        (service._open(project_id).root / "product-sessions").iterdir()
                    )
                ),
                1,
            )
            with self.assertRaisesRegex(ValidationError, "facts conflict"):
                service.record_product_session_terminal(
                    project_id,
                    session_id="hermes-session",
                    turn_id="turn-1",
                    process_status=ProcessStatus.CANCELLED,
                )
            with self.assertRaisesRegex(ValidationError, "receipt id"):
                service.record_product_session_terminal(
                    project_id,
                    session_id="hermes-session",
                    turn_id="turn-2",
                    process_status=ProcessStatus.EXITED,
                    receipt_id="../../outside",
                )

    def test_process_and_task_outcomes_remain_separate(self) -> None:
        before_gate = StageProjection(EngineeringStage.ROUTING, False, ("unrouted",))
        cases = (
            (
                ProcessStatus.EXITED,
                None,
                TaskOutcome.INCOMPLETE,
                "agent_returned_before_gate",
            ),
            (ProcessStatus.CRASHED, None, TaskOutcome.FAILED, "crashed"),
            (ProcessStatus.CANCELLED, None, TaskOutcome.INCOMPLETE, "cancelled"),
            (ProcessStatus.TIMED_OUT, None, TaskOutcome.INCOMPLETE, "timed_out"),
            (ProcessStatus.EXITED, "no_progress", TaskOutcome.BLOCKED, "no_progress"),
            (ProcessStatus.EXITED, "tool_failure", TaskOutcome.FAILED, "tool_failure"),
        )
        for process, reason, expected_outcome, expected_reason in cases:
            with self.subTest(process=process, reason=reason):
                outcome, normalized = terminal_outcome(
                    process_status=process,
                    requested_reason=reason,
                    stage=before_gate,
                )
                self.assertEqual(outcome, expected_outcome)
                self.assertEqual(normalized, expected_reason)

        passed = StageProjection(EngineeringStage.RELEASE_GATE, True, ())
        self.assertEqual(
            terminal_outcome(
                process_status=ProcessStatus.EXITED,
                requested_reason="agent_returned_before_gate",
                stage=passed,
            ),
            (TaskOutcome.PASSED, "release_gate_passed"),
        )
        self.assertEqual(
            terminal_outcome(
                process_status=ProcessStatus.CRASHED,
                requested_reason=None,
                stage=passed,
            ),
            (TaskOutcome.FAILED, "crashed"),
        )

    def test_terminal_receipt_round_trip_and_immutable_storage(self) -> None:
        progress = _vector(5)
        receipt = ProductSessionTerminalReceipt(
            "receipt-1",
            "board-1",
            "session:one",
            "turn:one",
            "2026-08-23T00:00:00Z",
            ProcessStatus.EXITED,
            TaskOutcome.PASSED,
            "release_gate_passed",
            EngineeringStage.RELEASE_GATE,
            True,
            5,
            progress,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = store_product_session_terminal(root, receipt)
            self.assertEqual(
                ProductSessionTerminalReceipt.from_dict(receipt.to_dict()), receipt
            )
            self.assertEqual(store_product_session_terminal(root, receipt), path)
            self.assertTrue(path.is_file())

        malformed = receipt.to_dict()
        malformed["release_gate_passed"] = False
        with self.assertRaisesRegex(ValidationError, "release outcome"):
            ProductSessionTerminalReceipt.from_dict(malformed)

        invalid_timestamp = receipt.to_dict()
        invalid_timestamp["created_at"] = "2026-02-31T00:00:00Z"
        with self.assertRaisesRegex(ValidationError, "timestamp"):
            ProductSessionTerminalReceipt.from_dict(invalid_timestamp)

        oversized_session = receipt.to_dict()
        oversized_session["session_id"] = "s" * 513
        with self.assertRaisesRegex(ValidationError, "session id"):
            ProductSessionTerminalReceipt.from_dict(oversized_session)

    def test_retained_checks_require_the_exact_design_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temporary), provider=provider)
            project_id = str(service.create_draft("Evidence binding")["project"]["id"])
            project = service._open(project_id)
            run = project.root / "validation" / "run-1"
            run.mkdir(parents=True)
            atomic_write_json(
                run / "receipt.json",
                {
                    "schema": "pcbdraft-individual-check-receipt",
                    "version": 1,
                    "status": "complete",
                    "check": "run_drc",
                    "design_content_hash": "same-design",
                    "source_revision": 3,
                    "source_design_revision": 0,
                    "state": "completed",
                    "outcome": "pass",
                    "report": "check.json",
                },
            )
            atomic_write_json(
                run / "check.json",
                {
                    "check": "run_drc",
                    "design_content_hash": "same-design",
                    "state": "completed",
                    "outcome": "pass",
                    "details": {"violations": []},
                },
            )

            current, _fatal, current_check = service._retained_check_progress(
                project, "same-design", "run_drc", 0
            )
            stale, _fatal, stale_check = service._retained_check_progress(
                project, "same-design", "run_drc", 1
            )

            self.assertTrue(current.is_current(0))
            self.assertTrue(current_check.current_pass(0))
            self.assertEqual(stale.status, EvidenceStatus.UNKNOWN)
            self.assertFalse(stale_check.current_pass(1))


if __name__ == "__main__":
    unittest.main()
