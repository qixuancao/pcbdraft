from __future__ import annotations

import unittest
from typing import Any

from pcbdraft.agent_runtime import AgentRuntime
from pcbdraft.agent_tools import run_design_turn
from pcbdraft.errors import ValidationError


def _view(
    project_id: str, *, sequence: int = 0, status: str = "draft"
) -> dict[str, Any]:
    return {
        "project": {"id": project_id, "name": "Agent board", "status": status},
        "state": {"event_sequence": sequence, "revision": sequence},
        "conversation": {"messages": [], "proposal": None},
        "design": None,
        "artifacts": {},
        "attempts": [],
    }


class StubService:
    def __init__(self) -> None:
        self.views = {"board": _view("board")}
        self.event_rows: list[dict[str, Any]] = []

    def create_draft(self, name: str) -> dict[str, Any]:
        self.views["board"]["project"]["name"] = name
        return self.views["board"]

    def open_project(self, project_id: str) -> dict[str, Any]:
        return self.views[project_id]

    def events(self, project_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        del project_id
        return [event for event in self.event_rows if event["sequence"] > after]


class StubRunner:
    def __init__(self) -> None:
        self.job = {
            "id": "20260814T000000Z-acde1234",
            "project_id": "board",
            "action": "agent_message",
            "status": "queued",
            "error": None,
        }
        self.submissions: list[tuple[str, str, dict[str, Any]]] = []
        self.cancelled = False

    def submit(
        self, project_id: str, action: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        self.submissions.append((project_id, action, args))
        self.job["project_id"] = project_id
        self.job["action"] = action
        return dict(self.job)

    def list(self, project_id: str) -> list[dict[str, Any]]:
        return [dict(self.job)] if self.job["project_id"] == project_id else []

    def get(self, project_id: str, job_id: str) -> dict[str, Any]:
        del project_id, job_id
        return dict(self.job)

    def cancel(self, project_id: str, job_id: str) -> dict[str, Any]:
        del project_id, job_id
        self.cancelled = True
        self.job["status"] = "cancel_requested"
        return dict(self.job)

    def retry(self, project_id: str, job_id: str) -> dict[str, Any]:
        del job_id
        self.job["status"] = "queued"
        return self.submit(project_id, str(self.job["action"]), {"timeout": 12.0})


class DispatchService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_message(
        self, project_id: str, text: str, *, timeout: float
    ) -> dict[str, Any]:
        del project_id, text, timeout
        self.calls.append("message")
        return _view("board", status="awaiting_confirmation")

    def confirm_project(
        self, project_id: str, *, validate: bool, timeout: float
    ) -> dict[str, Any]:
        del project_id, timeout
        if validate:
            raise AssertionError("agent orchestration must own the validation boundary")
        self.calls.append("confirm")
        return _view("board", status="generated")

    def validate_project(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        del project_id, timeout
        self.calls.append("validate")
        return _view("board", status="validated")

    def apply_modification(self, project_id: str) -> dict[str, Any]:
        del project_id
        self.calls.append("apply")
        return _view("board", status="validated")

    def record_progress(
        self, project_id: str, kind: str, message: str, *, level: str = "info"
    ) -> None:
        del project_id, message, level
        self.calls.append(kind)


class FailingGenerationService(DispatchService):
    def __init__(self) -> None:
        super().__init__()
        self.current = _view("board", status="awaiting_confirmation")
        self.repair_feedbacks: list[dict[str, Any]] = []

    def send_message(
        self, project_id: str, text: str, *, timeout: float
    ) -> dict[str, Any]:
        del project_id, text, timeout
        self.calls.append("message")
        return self.current

    def confirm_project(
        self, project_id: str, *, validate: bool, timeout: float
    ) -> dict[str, Any]:
        del project_id, validate, timeout
        self.calls.append("confirm")
        self.current = _view("board", status="generation_failed")
        self.current["conversation"]["messages"].append(
            {
                "kind": "failure",
                "text": "router retained one unrouted net",
            }
        )
        raise ValidationError("router retained one unrouted net")

    def open_project(self, project_id: str) -> dict[str, Any]:
        del project_id
        return self.current

    def prepare_agent_repair(
        self,
        project_id: str,
        feedback: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        del project_id, timeout
        self.calls.append("repair")
        self.repair_feedbacks.append(feedback)
        self.current = _view("board", status="awaiting_confirmation")
        return self.current


class AgentRuntimeTests(unittest.TestCase):
    def test_runtime_streams_normalized_events_and_settles_a_turn(self) -> None:
        service = StubService()
        runner = StubRunner()
        runtime = AgentRuntime(service, runner=runner)  # type: ignore[arg-type]

        started = runtime.start_project(
            "Temperature board", "Build a temperature sensor", timeout=12
        )

        self.assertTrue(runtime.active)
        self.assertEqual(started.status, "queued")
        self.assertEqual(runner.submissions[0][1], "agent_message")
        service.event_rows.extend(
            [
                {
                    "sequence": 1,
                    "kind": "provider.started",
                    "level": "info",
                    "message": "Interpreting request",
                    "created_at": "2026-08-14T00:00:00Z",
                },
                {
                    "sequence": 2,
                    "kind": "plan.ready",
                    "level": "info",
                    "message": "Plan ready",
                    "created_at": "2026-08-14T00:00:01Z",
                },
            ]
        )
        runner.job["status"] = "running"
        update = runtime.poll()
        assert update is not None
        self.assertEqual(
            [item.tool for item in update.activities],
            ["pcb_requirements", "pcb_circuit_plan"],
        )
        self.assertEqual(update.activities[-1].state, "completed")
        self.assertIsNone(update.view)

        runner.job["status"] = "completed"
        service.views["board"] = _view("board", sequence=2, status="validated")
        completed = runtime.poll()
        assert completed is not None
        self.assertTrue(completed.terminal)
        self.assertEqual(completed.view["project"]["status"], "validated")
        self.assertFalse(runtime.active)

    def test_runtime_rejects_overlap_and_exposes_cancel(self) -> None:
        service = StubService()
        runner = StubRunner()
        runtime = AgentRuntime(service, runner=runner)  # type: ignore[arg-type]
        runtime.submit_message("board", "first", timeout=12)

        with self.assertRaisesRegex(ValidationError, "already running"):
            runtime.submit_message("board", "second", timeout=12)

        cancelled = runtime.cancel()
        self.assertTrue(runner.cancelled)
        self.assertEqual(cancelled.status, "cancel_requested")

    def test_runtime_restores_history_without_replaying_and_retries_explicitly(
        self,
    ) -> None:
        service = StubService()
        service.views["board"] = _view("board", sequence=2, status="interrupted")
        service.event_rows = [
            {
                "sequence": 1,
                "kind": "provider.started",
                "level": "info",
                "message": "Interpreting request",
                "created_at": "2026-08-14T00:00:00Z",
            },
            {
                "sequence": 2,
                "kind": "job.failed",
                "level": "error",
                "message": "The prior process stopped",
                "created_at": "2026-08-14T00:00:01Z",
            },
        ]
        runner = StubRunner()
        runner.job["status"] = "interrupted"
        runner.job["error"] = "The application stopped before completion."
        runtime = AgentRuntime(service, runner=runner)  # type: ignore[arg-type]

        restored = runtime.restore_project("board")

        self.assertFalse(runtime.active)
        self.assertEqual(restored.status, "interrupted")
        self.assertEqual(len(restored.activities), 2)
        self.assertEqual(runner.submissions, [])

        retried = runtime.retry_last("board")
        self.assertTrue(runtime.active)
        self.assertEqual(retried.status, "queued")
        self.assertEqual(runner.submissions[-1][1], "agent_message")

    def test_agent_message_dispatch_runs_generation_without_layer_question(
        self,
    ) -> None:
        service = DispatchService()
        result = run_design_turn(
            service,  # type: ignore[arg-type]
            "board",
            "Build a small sensor board",
            timeout=12.0,
            cancellation_requested=lambda: False,
        )

        self.assertEqual(service.calls, ["message", "confirm", "validate"])
        self.assertEqual(result["project"]["status"], "validated")

    def test_agent_message_stops_before_generation_at_safe_boundary(self) -> None:
        service = DispatchService()
        result = run_design_turn(
            service,  # type: ignore[arg-type]
            "board",
            "Build a small sensor board",
            timeout=12.0,
            cancellation_requested=lambda: True,
        )

        self.assertEqual(service.calls, ["message", "agent.stopped"])
        self.assertEqual(result["project"]["status"], "awaiting_confirmation")

    def test_agent_message_stops_after_generation_before_validation(self) -> None:
        service = DispatchService()
        checks = iter((False, True))
        result = run_design_turn(
            service,  # type: ignore[arg-type]
            "board",
            "Build a small sensor board",
            timeout=12.0,
            cancellation_requested=lambda: next(checks),
        )

        self.assertEqual(
            service.calls,
            ["message", "confirm", "agent.stopped"],
        )
        self.assertEqual(result["project"]["status"], "generated")

    def test_agent_message_applies_a_ready_change(self) -> None:
        service = DispatchService()
        service.send_message = lambda *args, **kwargs: _view(  # type: ignore[method-assign]
            "board", status="change_ready"
        )

        result = run_design_turn(
            service,  # type: ignore[arg-type]
            "board",
            "Move the connector",
            timeout=12.0,
            cancellation_requested=lambda: False,
        )

        self.assertEqual(service.calls, ["apply"])
        self.assertEqual(result["project"]["status"], "validated")

    def test_agent_repairs_deterministic_validation_failure_then_applies(self) -> None:
        service = DispatchService()
        captured: list[dict[str, Any]] = []
        failed = _view("board", status="generated")
        failed["artifacts"] = {
            "validation": {
                "levels": [
                    {
                        "level": "L2",
                        "checks": [
                            {
                                "id": "kicad.erc",
                                "state": "completed",
                                "outcome": "fail",
                                "summary": "one unconnected required pin",
                            }
                        ],
                    }
                ]
            }
        }

        def validate(*args: object, **kwargs: object) -> dict[str, Any]:
            del args, kwargs
            service.calls.append("validate")
            return failed

        def repair(
            project_id: str,
            feedback: dict[str, Any],
            *,
            timeout: float,
        ) -> dict[str, Any]:
            del project_id, timeout
            service.calls.append("repair")
            captured.append(feedback)
            return _view("board", status="change_ready")

        service.validate_project = validate  # type: ignore[method-assign]
        service.prepare_agent_repair = repair  # type: ignore[attr-defined]
        result = run_design_turn(
            service,  # type: ignore[arg-type]
            "board",
            "Build a small sensor board",
            timeout=12.0,
            cancellation_requested=lambda: False,
        )

        self.assertEqual(
            service.calls, ["message", "confirm", "validate", "repair", "apply"]
        )
        self.assertEqual(captured[0]["phase"], "validation")
        self.assertIn("kicad.erc", captured[0]["findings"][0])
        self.assertEqual(result["project"]["status"], "validated")

    def test_agent_bounds_repeated_generation_repairs(self) -> None:
        service = FailingGenerationService()

        with self.assertRaisesRegex(ValidationError, "unrouted"):
            run_design_turn(
                service,  # type: ignore[arg-type]
                "board",
                "Build a small sensor board",
                timeout=12.0,
                cancellation_requested=lambda: False,
            )

        self.assertEqual(
            service.calls,
            ["message", "confirm", "repair", "confirm", "repair", "confirm"],
        )
        self.assertEqual(
            [feedback["attempt"] for feedback in service.repair_feedbacks], [1, 2]
        )

    def test_agent_does_not_repair_unknown_non_deterministic_evidence(self) -> None:
        service = DispatchService()
        provisional = _view("board", status="generated")
        provisional["artifacts"] = {
            "validation": {
                "levels": [
                    {
                        "level": "L1",
                        "checks": [
                            {
                                "id": "parts.datasheet",
                                "state": "human_required",
                                "outcome": "unknown",
                                "summary": "datasheet review is required",
                            }
                        ],
                    }
                ]
            }
        }

        def validate(*args: object, **kwargs: object) -> dict[str, Any]:
            del args, kwargs
            service.calls.append("validate")
            return provisional

        service.validate_project = validate  # type: ignore[method-assign]
        result = run_design_turn(
            service,  # type: ignore[arg-type]
            "board",
            "Build a small sensor board",
            timeout=12.0,
            cancellation_requested=lambda: False,
        )

        self.assertEqual(service.calls, ["message", "confirm", "validate"])
        self.assertEqual(result["project"]["status"], "generated")


if __name__ == "__main__":
    unittest.main()
