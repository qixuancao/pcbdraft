from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json, atomic_write_text
from pcbdraft.services.progress import (
    EngineeringStage,
    ProcessStatus,
    ProductSessionTerminalReceipt,
    ProgressVector,
    TaskOutcome,
)
from pcbdraft.verification.boardbench import (
    BoardBenchRun,
    build_inventory,
    load_artifact,
    load_run,
)
from pcbdraft.verification.boardbench_runner import plan_campaign
from pcbdraft.verification.boardbench_v2 import (
    BUDGET_NAMES,
    RUN_V2_SCHEMA,
    BoardBenchRunV2,
    BudgetDimension,
    load_normalized_run,
    load_run_v2,
    normalize_v1_run,
    planned_run_v2,
    start_run_v2,
    store_run_v2,
    terminal_run_v2,
    validate_campaign_denominator,
)
from tests.verification.test_boardbench_runner import NOW, _corpus, _environment

LATER = "2026-08-23T16:00:00Z"


class BoardBenchRunV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = _corpus()
        self.campaign = plan_campaign(
            self.corpus,
            campaign_id="campaign-v2",
            environment=_environment(),
            evaluator_version="boardbench-evaluator-v4",
            created_at=NOW,
        )

    def _legacy(self, index: int = 0, *, running: bool = False) -> BoardBenchRun:
        plan = self.campaign.runs[index]
        case = next(item for item in self.corpus.cases if item.id == plan.case_id)
        return BoardBenchRun(
            campaign_id=self.campaign.campaign_id,
            run_id=plan.run_id,
            case_id=plan.case_id,
            repetition=plan.repetition,
            prompt_sha256=hashlib.sha256(case.prompt.encode()).hexdigest(),
            status="running" if running else "planned",
            started_at=NOW if running else None,
            completed_at=None,
            termination_reason=None,
            final_response=None,
            inventory=(),
        )

    @staticmethod
    def _write_events(artifacts: Path, events: list[dict[str, object]]) -> None:
        trace = artifacts / "trace"
        trace.mkdir(parents=True)
        atomic_write_text(
            trace / "agent-trace.jsonl",
            "".join(json.dumps(event) + "\n" for event in events),
        )

    def _normalize(
        self,
        events: list[dict[str, object]],
        *,
        fallback_status: str = "completed",
        fallback_reason: str = "agent_returned",
        duration: float | None = 1.0,
        exit_code: int | None = 0,
        usage: dict[str, object] | None = None,
    ) -> BoardBenchRunV2:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        artifacts = Path(temporary.name)
        self._write_events(artifacts, events)
        if usage is not None:
            atomic_write_json(artifacts / "usage.json", usage)
        inventory = build_inventory(artifacts)
        return terminal_run_v2(
            self._legacy(running=True),
            self.campaign,
            artifacts,
            completed_at=LATER,
            fallback_status=fallback_status,
            fallback_reason=fallback_reason,
            final_response="done" if fallback_status == "completed" else None,
            duration_seconds=duration,
            worker_exit_code=exit_code,
            inventory=inventory,
        )

    def test_v2_round_trip_and_strict_terminal_combinations(self) -> None:
        terminal = self._normalize(
            [
                {
                    "seq": 1,
                    "event": "session_end",
                    "data": {
                        "completed": True,
                        "failed": False,
                        "interrupted": False,
                        "turn_exit_reason": "completed",
                    },
                }
            ]
        )
        self.assertEqual(BoardBenchRunV2.from_dict(terminal.to_dict()), terminal)
        self.assertEqual(terminal.task_outcome, TaskOutcome.INCOMPLETE)
        self.assertEqual(terminal.termination_reason, "agent_returned_before_gate")
        with self.assertRaisesRegex(ValidationError, "release-gate"):
            replace(terminal, release_gate_passed=True)
        with self.assertRaisesRegex(ValidationError, "matching budget"):
            replace(
                terminal,
                termination_reason="budget_exhausted:model_turns",
            )
        self.assertEqual(
            BudgetDimension(
                "model_turns", 90, 90, "turn", "test", "within_limit"
            ).status,
            "within_limit",
        )
        with self.assertRaisesRegex(ValidationError, "within-limit"):
            BudgetDimension("model_turns", 90, 91, "turn", "test", "within_limit")
        exhausted_budgets = tuple(
            replace(item, consumed=91, status="exhausted")
            if item.name == "model_turns"
            else item
            for item in terminal.budgets
        )
        with self.assertRaisesRegex(ValidationError, "termination reason"):
            replace(terminal, budgets=exhausted_budgets)
        with self.assertRaisesRegex(ValidationError, "completed_at"):
            replace(terminal, completed_at="2026-02-31T00:00:00Z")
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValidationError, "completed_at"),
        ):
            terminal_run_v2(
                self._legacy(running=True),
                self.campaign,
                Path(temporary),
                completed_at="2026-02-31T00:00:00Z",
                fallback_status="completed",
                fallback_reason="agent_returned",
                final_response="done",
                duration_seconds=1.0,
                worker_exit_code=0,
                inventory=(),
            )
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValidationError, "campaign plan"),
        ):
            terminal_run_v2(
                replace(self._legacy(running=True), campaign_id="different-campaign"),
                self.campaign,
                Path(temporary),
                completed_at=LATER,
                fallback_status="completed",
                fallback_reason="agent_returned",
                final_response="done",
                duration_seconds=1.0,
                worker_exit_code=0,
                inventory=(),
            )

    def test_release_on_exact_turn_limit_is_within_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            started_at = "2026-08-23T15:00:00Z"
            created_at = "2026-08-23T15:00:00.500000Z"
            completed_at = "2026-08-23T15:00:01Z"
            receipt = ProductSessionTerminalReceipt(
                "receipt-exact-limit",
                "board-exact-limit",
                "session-exact-limit",
                "turn-exact-limit",
                created_at,
                ProcessStatus.EXITED,
                TaskOutcome.PASSED,
                "release_gate_passed",
                EngineeringStage.RELEASE_GATE,
                True,
                4,
                ProgressVector.unknown(4),
            )
            project = artifacts / "repository" / "projects" / receipt.project_id
            terminal_path = project / "product-sessions" / f"{receipt.receipt_id}.json"
            terminal_path.parent.mkdir(parents=True)
            atomic_write_json(
                project / "project.json",
                {
                    "id": receipt.project_id,
                    "name": self._legacy().run_id,
                    "design_revision": receipt.source_revision,
                },
            )
            atomic_write_json(terminal_path, receipt.to_dict())
            events: list[dict[str, object]] = [
                {"seq": index + 1, "event": "model_request", "data": {}}
                for index in range(90)
            ]
            events.extend(
                [
                    {
                        "seq": 91,
                        "event": "product_session_terminal",
                        "data": {
                            "session_id": receipt.session_id,
                            "turn_id": receipt.turn_id,
                            "project_id": receipt.project_id,
                            "artifact": f"product-sessions/{receipt.receipt_id}.json",
                            "process_status": "exited",
                            "release_outcome": "passed",
                            "scoped_task_outcome": "unknown",
                            "scoped_task_evidence": {
                                "kind": "unavailable",
                                "source_revision": receipt.source_revision,
                            },
                            "termination_reason": "release_gate_passed",
                            "stage_reached": "release_gate",
                            "release_gate_passed": True,
                        },
                    },
                    {
                        "seq": 92,
                        "event": "session_end",
                        "data": {
                            "session_id": receipt.session_id,
                            "turn_id": receipt.turn_id,
                            "completed": True,
                            "failed": False,
                            "interrupted": False,
                            "turn_exit_reason": "text_response(finish_reason=stop)",
                        },
                    },
                ]
            )
            self._write_events(artifacts, events)
            running = replace(self._legacy(running=True), started_at=started_at)
            run = terminal_run_v2(
                running,
                self.campaign,
                artifacts,
                completed_at=completed_at,
                fallback_status="completed",
                fallback_reason="agent_returned",
                final_response="ready",
                duration_seconds=1.0,
                worker_exit_code=0,
                inventory=build_inventory(artifacts),
            )

        self.assertEqual(run.task_outcome, TaskOutcome.PASSED)
        model_turns = next(item for item in run.budgets if item.name == "model_turns")
        self.assertEqual(model_turns.consumed, 90)
        self.assertEqual(model_turns.status, "within_limit")

    def test_v1_projection_marks_v2_facts_unknown_without_mutation(self) -> None:
        legacy = self._legacy()
        before = legacy.to_dict()
        normalized = normalize_v1_run(legacy)
        self.assertIsNone(normalized.process_status)
        self.assertIsNone(normalized.task_outcome)
        self.assertIsNone(normalized.stage_reached)
        self.assertIsNone(normalized.release_gate_passed)
        self.assertTrue(all(item.status == "unknown" for item in normalized.budgets))
        self.assertTrue(all(item.limit is None for item in normalized.budgets))
        self.assertTrue(all(item.consumed is None for item in normalized.budgets))
        self.assertEqual(before, legacy.to_dict())

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.json"
            atomic_write_json(path, before)
            original = path.read_bytes()
            projected = load_normalized_run(path)
            self.assertEqual(projected.source_version, 1)
            self.assertEqual(path.read_bytes(), original)

    def test_new_run_json_is_only_v2_and_legacy_loader_is_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.json"
            planned = planned_run_v2(self._legacy(), self.campaign)
            limits = {item.name: item.limit for item in planned.budgets}
            self.assertEqual(limits["model_turns"], 90)
            self.assertEqual(limits["pcb_tool_calls"], 500)
            self.assertEqual(limits["wall_time"], 3600)
            self.assertIsNone(limits["route_attempts"])
            self.assertIsNone(limits["route_node_expansions"])
            store_run_v2(path, planned)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], RUN_V2_SCHEMA)
            self.assertEqual(document["version"], 2)
            self.assertEqual(load_run_v2(path), planned)
            self.assertIsInstance(load_run(path), BoardBenchRun)
            self.assertIsInstance(load_artifact(path), BoardBenchRun)
            self.assertEqual(json.loads(path.read_text())["schema"], RUN_V2_SCHEMA)
        with self.assertRaisesRegex(ValidationError, "campaign plan"):
            planned_run_v2(
                replace(self._legacy(), campaign_id="different-campaign"),
                self.campaign,
            )

    def test_later_worker_crash_precedes_bound_product_terminal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            receipt = ProductSessionTerminalReceipt(
                "receipt-1",
                "board-1",
                "session-1",
                "turn-1",
                NOW,
                ProcessStatus.EXITED,
                TaskOutcome.PASSED,
                "release_gate_passed",
                EngineeringStage.RELEASE_GATE,
                True,
                4,
                ProgressVector.unknown(4),
            )
            project = artifacts / "repository" / "projects" / "board-1"
            terminal_path = project / "product-sessions" / "receipt-1.json"
            terminal_path.parent.mkdir(parents=True)
            atomic_write_json(
                project / "project.json",
                {
                    "id": "board-1",
                    "name": self._legacy().run_id,
                    "design_revision": 4,
                },
            )
            atomic_write_json(terminal_path, receipt.to_dict())
            self._write_events(
                artifacts,
                [
                    {
                        "seq": 1,
                        "event": "product_session_terminal",
                        "data": {
                            "session_id": "session-1",
                            "turn_id": "turn-1",
                            "project_id": "board-1",
                            "artifact": "product-sessions/receipt-1.json",
                            "process_status": "exited",
                            "task_outcome": "passed",
                            "termination_reason": "release_gate_passed",
                            "stage_reached": "release_gate",
                            "release_gate_passed": True,
                        },
                    },
                    {
                        "seq": 2,
                        "event": "session_end",
                        "data": {
                            "session_id": "session-1",
                            "turn_id": "turn-1",
                            "completed": True,
                            "failed": False,
                            "interrupted": False,
                            "turn_exit_reason": "completed",
                        },
                    },
                ],
            )
            run = terminal_run_v2(
                self._legacy(running=True),
                self.campaign,
                artifacts,
                completed_at=LATER,
                fallback_status="failed",
                fallback_reason="worker_exit_7",
                final_response="ready",
                duration_seconds=2.0,
                worker_exit_code=7,
                inventory=build_inventory(artifacts),
            )
        self.assertEqual(run.outcome_source, "worker_fallback")
        self.assertEqual(run.process_status, ProcessStatus.CRASHED)
        self.assertEqual(run.task_outcome, TaskOutcome.FAILED)
        self.assertFalse(run.release_gate_passed)

    def test_only_current_run_bound_product_terminal_can_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            receipt = ProductSessionTerminalReceipt(
                "receipt-1",
                "board-1",
                "session-1",
                "turn-1",
                NOW,
                ProcessStatus.EXITED,
                TaskOutcome.PASSED,
                "release_gate_passed",
                EngineeringStage.RELEASE_GATE,
                True,
                4,
                ProgressVector.unknown(4),
            )
            project = artifacts / "repository" / "projects" / "board-1"
            terminal_path = project / "product-sessions" / "receipt-1.json"
            terminal_path.parent.mkdir(parents=True)
            atomic_write_json(
                project / "project.json",
                {
                    "id": "board-1",
                    "name": self._legacy().run_id,
                    "design_revision": 4,
                },
            )
            atomic_write_json(terminal_path, receipt.to_dict())
            event = {
                "seq": 1,
                "event": "product_session_terminal",
                "data": {
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "project_id": "board-1",
                    "artifact": "product-sessions/receipt-1.json",
                    "process_status": "exited",
                    "task_outcome": "passed",
                    "termination_reason": "release_gate_passed",
                    "stage_reached": "release_gate",
                    "release_gate_passed": True,
                },
            }
            session_end = {
                "seq": 2,
                "event": "session_end",
                "data": {
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "completed": True,
                    "failed": False,
                    "interrupted": False,
                    "turn_exit_reason": "completed",
                },
            }
            self._write_events(artifacts, [event, session_end])
            kwargs = {
                "completed_at": LATER,
                "fallback_status": "completed",
                "fallback_reason": "agent_returned",
                "final_response": "ready",
                "duration_seconds": 2.0,
                "worker_exit_code": 0,
            }
            passed = terminal_run_v2(
                self._legacy(running=True),
                self.campaign,
                artifacts,
                inventory=build_inventory(artifacts),
                **kwargs,
            )
            self.assertEqual(passed.outcome_source, "product_session_terminal")
            self.assertEqual(passed.task_outcome, TaskOutcome.PASSED)

            atomic_write_json(
                project / "project.json",
                {
                    "id": "board-1",
                    "name": "different-run",
                    "design_revision": 4,
                },
            )
            stale = terminal_run_v2(
                self._legacy(running=True),
                self.campaign,
                artifacts,
                inventory=build_inventory(artifacts),
                **kwargs,
            )
            self.assertEqual(stale.outcome_source, "trace_terminal")
            self.assertEqual(stale.task_outcome, TaskOutcome.INCOMPLETE)
            self.assertFalse(stale.release_gate_passed)

    def test_terminal_reasons_remain_distinct(self) -> None:
        cases = (
            (
                [
                    {"seq": index + 1, "event": "model_request", "data": {}}
                    for index in range(90)
                ]
                + [
                    {
                        "seq": 91,
                        "event": "session_end",
                        "data": {
                            "completed": False,
                            "failed": False,
                            "interrupted": False,
                            "turn_exit_reason": "max_iterations_reached(90/90)",
                        },
                    }
                ],
                "completed",
                "agent_returned",
                ProcessStatus.EXITED,
                TaskOutcome.INCOMPLETE,
                "budget_exhausted:model_turns",
            ),
            (
                [],
                "failed",
                "pcb_tool_budget_exhausted",
                ProcessStatus.EXITED,
                TaskOutcome.INCOMPLETE,
                "budget_exhausted:pcb_tool_calls",
            ),
            (
                [],
                "timed_out",
                "wall_timeout",
                ProcessStatus.TIMED_OUT,
                TaskOutcome.INCOMPLETE,
                "budget_exhausted:wall_time",
            ),
            (
                [
                    {
                        "seq": 1,
                        "event": "session_end",
                        "data": {"turn_exit_reason": "no_progress"},
                    }
                ],
                "completed",
                "agent_returned",
                ProcessStatus.EXITED,
                TaskOutcome.BLOCKED,
                "no_progress",
            ),
            (
                [
                    {
                        "seq": 1,
                        "event": "session_end",
                        "data": {"turn_exit_reason": "strategy_change_required"},
                    }
                ],
                "completed",
                "agent_returned",
                ProcessStatus.EXITED,
                TaskOutcome.BLOCKED,
                "strategy_required",
            ),
            (
                [
                    {
                        "seq": 1,
                        "event": "session_end",
                        "data": {"failed": True, "turn_exit_reason": "provider_error"},
                    }
                ],
                "failed",
                "worker_failed",
                ProcessStatus.EXITED,
                TaskOutcome.FAILED,
                "tool_failure",
            ),
            (
                [],
                "failed",
                "worker_exit_7",
                ProcessStatus.CRASHED,
                TaskOutcome.FAILED,
                "crashed",
            ),
            (
                [
                    {
                        "seq": 1,
                        "event": "session_end",
                        "data": {
                            "interrupted": True,
                            "turn_exit_reason": "interrupted_by_user",
                        },
                    }
                ],
                "interrupted",
                "parent_interrupted",
                ProcessStatus.CANCELLED,
                TaskOutcome.INCOMPLETE,
                "cancelled",
            ),
        )
        for events, status, fallback, process, outcome, reason in cases:
            with self.subTest(reason=reason):
                duration = (
                    self.campaign.wall_timeout_seconds if status == "timed_out" else 1.0
                )
                run = self._normalize(
                    events,
                    fallback_status=status,
                    fallback_reason=fallback,
                    duration=duration,
                    exit_code=7 if fallback == "worker_exit_7" else 0,
                )
                self.assertEqual(run.process_status, process)
                self.assertEqual(run.task_outcome, outcome)
                self.assertEqual(run.termination_reason, reason)
                exhausted = reason.removeprefix("budget_exhausted:")
                if reason.startswith("budget_exhausted:"):
                    budget = next(
                        item for item in run.budgets if item.name == exhausted
                    )
                    self.assertEqual(budget.status, "exhausted")

    def test_trace_budgets_cost_and_context_remain_separate(self) -> None:
        events = [
            {
                "seq": 1,
                "event": "model_request",
                "data": {
                    "context_quality": {
                        "active_context_token_estimate": 80,
                        "repeated_content_ratio": 0.25,
                        "newly_added_tokens_estimate": 20,
                    }
                },
            },
            {
                "seq": 2,
                "event": "tool_end",
                "data": {
                    "tool_name": "pcb_route_net",
                    "result": {"routing_failure": {"expanded_nodes": 12}},
                },
            },
            {
                "seq": 3,
                "event": "model_response",
                "data": {
                    "cost_metrics": {
                        "actual_cost_status": "unknown",
                        "actual_cost_value": None,
                    }
                },
            },
            {
                "seq": 4,
                "event": "session_end",
                "data": {
                    "completed": True,
                    "failed": False,
                    "interrupted": False,
                    "turn_exit_reason": "completed",
                },
            },
        ]
        run = self._normalize(
            events,
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 60,
                "estimated_cost_usd": 99.0,
                "cost_status": "estimated",
                "api_calls": 1,
            },
        )
        budgets = {item.name: item for item in run.budgets}
        self.assertEqual(tuple(budgets), BUDGET_NAMES)
        self.assertEqual(budgets["route_attempts"].consumed, 1)
        self.assertIsNone(budgets["route_attempts"].limit)
        self.assertEqual(budgets["route_node_expansions"].consumed, 12)
        self.assertEqual(budgets["uncached_input_tokens"].consumed, 40)
        self.assertEqual(budgets["cache_read_tokens"].consumed, 60)
        self.assertEqual(run.actual_cost.status, "unknown")
        self.assertIsNone(run.actual_cost.value)
        self.assertEqual(run.context_quality.status, "reported")
        self.assertEqual(run.context_quality.peak_active_tokens, 80)

    def test_negative_worker_returncode_is_a_crash_not_invalid_evidence(self) -> None:
        for reason in ("worker_exit_-9", "worker_output_limit"):
            with self.subTest(reason=reason):
                run = self._normalize(
                    [],
                    fallback_status="failed",
                    fallback_reason=reason,
                    exit_code=-9,
                )
                self.assertEqual(run.worker_exit_code, -9)
                self.assertEqual(run.process_status, ProcessStatus.CRASHED)
                self.assertEqual(run.task_outcome, TaskOutcome.FAILED)
                self.assertEqual(run.termination_reason, "crashed")

    def test_v2_storage_is_immutable_and_denominator_rejects_substitutes(self) -> None:
        runs = tuple(
            planned_run_v2(self._legacy(index), self.campaign)
            for index in range(len(self.campaign.runs))
        )
        validate_campaign_denominator(self.campaign, runs)
        with self.assertRaisesRegex(ValidationError, "immutable campaign plan"):
            validate_campaign_denominator(
                self.campaign,
                (*runs[:-1], replace(runs[-1], run_id="replacement-run")),
            )
        with self.assertRaisesRegex(ValidationError, "duplicate run id"):
            validate_campaign_denominator(self.campaign, (*runs, runs[0]))

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.json"
            store_run_v2(path, runs[0])
            running = start_run_v2(runs[0], NOW)
            store_run_v2(path, running)
            terminal = self._normalize(
                [
                    {
                        "seq": 1,
                        "event": "session_end",
                        "data": {
                            "completed": True,
                            "failed": False,
                            "interrupted": False,
                            "turn_exit_reason": "completed",
                        },
                    }
                ]
            )
            store_run_v2(path, terminal)
            with self.assertRaisesRegex(ValidationError, "immutable"):
                store_run_v2(path, terminal)


if __name__ == "__main__":
    unittest.main()
