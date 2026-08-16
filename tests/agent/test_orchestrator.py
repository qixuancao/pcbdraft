from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from pcbdraft.agent.orchestrator import (
    AgentOrchestrator,
    DeterministicPCBCallProducer,
)
from pcbdraft.agent.permissions import PermissionBroker
from pcbdraft.agent.runtime import AgentRuntime
from pcbdraft.agent.turns import (
    ApprovalStatus,
    ToolRunRecord,
    ToolRunStatus,
    TurnStatus,
    hash_tool_arguments,
)
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services.jobs import JobRunner


def _view(*, status: str = "draft", revision: int = 0) -> dict[str, Any]:
    return {
        "project": {"id": "board", "name": "Board", "status": status},
        "state": {"revision": revision, "event_sequence": 0},
        "conversation": {"messages": [], "proposal": None},
        "design": None,
        "artifacts": {},
        "attempts": [],
    }


def _approval_args(agent: AgentOrchestrator, turn: Any) -> dict[str, Any]:
    payload = agent.approval_payload(turn)
    if payload is None:
        raise AssertionError("turn has no pending approval")
    names = {
        "turn_id",
        "checkpoint_id",
        "tool_call_id",
        "tool_name",
        "effect",
        "risk",
        "args_hash",
        "baseline_revision",
    }
    return {name: payload[name] for name in names}


class OrchestratorService:
    def __init__(self, root: Path) -> None:
        self.root = root / "board"
        self.root.mkdir()
        (self.root / "jobs").mkdir()
        self.locks_root = root / "locks"
        self.locks_root.mkdir()
        self.view = _view()
        self.calls: list[str] = []
        self.fail_validation_once = False

    def list_projects(self) -> list[dict[str, Any]]:
        return []

    def record_progress(
        self,
        project_id: str,
        kind: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        del project_id, kind, message, level

    def events(self, project_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        del project_id, after
        return []

    def try_open_project_snapshot(
        self, project_id: str, *, timeout: float = 0.0
    ) -> dict[str, Any]:
        del timeout
        return self.open_project(project_id)

    def project_root(self, project_id: str) -> Path:
        if project_id != "board":
            raise AssertionError("unexpected project")
        return self.root

    def open_project(self, project_id: str) -> dict[str, Any]:
        if project_id != "board":
            raise AssertionError("unexpected project")
        return self.view

    def send_message(
        self, project_id: str, message: str, *, timeout: float
    ) -> dict[str, Any]:
        del project_id, message, timeout
        self.calls.append("plan")
        self.view = _view(status="awaiting_confirmation", revision=1)
        return self.view

    def confirm_project(
        self, project_id: str, *, validate: bool, timeout: float
    ) -> dict[str, Any]:
        del project_id, timeout
        if validate:
            raise AssertionError("orchestrator owns validation")
        self.calls.append("generate")
        self.view = _view(status="generated", revision=2)
        return self.view

    def validate_project(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        del project_id, timeout
        self.calls.append("validate")
        if self.fail_validation_once:
            self.fail_validation_once = False
            raise ValidationError("validator process stopped")
        self.view = _view(status="validated", revision=3)
        return self.view


class AgentOrchestratorTests(unittest.TestCase):
    @staticmethod
    def _wait_job(runner: JobRunner, project_id: str, job_id: str) -> dict[str, Any]:
        job: dict[str, Any] = {}
        for _ in range(250):
            job = runner.get(project_id, job_id)
            if job["status"] not in {"queued", "running", "cancel_requested"}:
                return job
            time.sleep(0.01)
        raise AssertionError("job did not settle")

    def test_turn_persists_each_call_before_completing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            turn = agent.start_turn("board", "Build a sensor board")

            result = agent.run_turn(
                "board",
                turn.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )

            self.assertEqual(result["project"]["status"], "validated")
            self.assertEqual(service.calls, ["plan", "generate", "validate"])
            stored = agent.store("board").load(turn.turn_id)
            self.assertEqual(stored.status, TurnStatus.COMPLETED)
            self.assertEqual(
                [tool.tool_name for tool in stored.tool_runs],
                ["pcb_plan_request", "pcb_generate_candidate", "pcb_validate"],
            )
            self.assertTrue(
                all(tool.status is ToolRunStatus.COMPLETED for tool in stored.tool_runs)
            )
            self.assertTrue(all(tool.result is not None for tool in stored.tool_runs))

    def test_model_direct_intent_is_not_reinterpreted_as_a_plan_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            service.view = _view(status="validated", revision=4)
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            queued = agent.start_turn("board", "Build the manufacturing bundle")
            store = agent.store("board")
            running = store.update(queued.turn_id, TurnStatus.RUNNING)
            tool = ToolRunRecord.proposed(
                project_id="board",
                thread_id=running.thread_id,
                turn_id=running.turn_id,
                tool_call_id="provider-call-release",
                tool_name="pcb_build_release",
                source="model",
                effect="evidence_write",
                risk="medium",
                arguments={},
                args_hash=hash_tool_arguments({}),
                baseline_revision=4,
                before_status="validated",
                before_revision=4,
            )
            store.append_tool_run(running.turn_id, tool)
            executing = store.update_tool_run(
                running.turn_id,
                tool.tool_call_id,
                ToolRunStatus.RUNNING,
                dispatch_started=False,
            )
            store.begin_dispatch(
                executing.turn_id,
                tool.tool_call_id,
                expected_record_revision=executing.record_revision,
            )
            completed = store.update_tool_run(
                running.turn_id,
                tool.tool_call_id,
                ToolRunStatus.COMPLETED,
                after_status="released",
                after_revision=5,
                result={"status": "released"},
            )

            proposal = DeterministicPCBCallProducer().next_call(
                completed,
                _view(status="released", revision=5),
                timeout=12.0,
            )

        self.assertIsNone(proposal)

    def test_model_generated_design_still_receives_local_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            service.view = _view(status="awaiting_confirmation", revision=4)
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            queued = agent.start_turn("board", "Generate the accepted board")
            store = agent.store("board")
            running = store.update(queued.turn_id, TurnStatus.RUNNING)
            tool = ToolRunRecord.proposed(
                project_id="board",
                thread_id=running.thread_id,
                turn_id=running.turn_id,
                tool_call_id="provider-call-generate",
                tool_name="pcb_generate_candidate",
                source="model",
                effect="authoritative_write",
                risk="high",
                arguments={},
                args_hash=hash_tool_arguments({}),
                baseline_revision=4,
                before_status="awaiting_confirmation",
                before_revision=4,
            )
            store.append_tool_run(running.turn_id, tool)
            executing = store.update_tool_run(
                running.turn_id,
                tool.tool_call_id,
                ToolRunStatus.RUNNING,
                dispatch_started=False,
            )
            store.begin_dispatch(
                executing.turn_id,
                tool.tool_call_id,
                expected_record_revision=executing.record_revision,
            )
            completed = store.update_tool_run(
                running.turn_id,
                tool.tool_call_id,
                ToolRunStatus.COMPLETED,
                after_status="generated",
                after_revision=5,
                result={"status": "generated"},
            )

            proposal = DeterministicPCBCallProducer().next_call(
                completed,
                _view(status="generated", revision=5),
                timeout=12.0,
            )

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.name, "validate")
        self.assertEqual(proposal.source, "runtime_policy")

    def test_failed_model_discard_cannot_reverse_into_apply_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            service.view = _view(status="change_ready", revision=4)
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            queued = agent.start_turn("board", "Discard the staged candidate")
            store = agent.store("board")
            running = store.update(queued.turn_id, TurnStatus.RUNNING)
            tool = ToolRunRecord.proposed(
                project_id="board",
                thread_id=running.thread_id,
                turn_id=running.turn_id,
                tool_call_id="provider-call-discard",
                tool_name="pcb_discard_candidate",
                source="model",
                effect="staged_write",
                risk="medium",
                arguments={},
                args_hash=hash_tool_arguments({}),
                baseline_revision=4,
                before_status="change_ready",
                before_revision=4,
            )
            store.append_tool_run(running.turn_id, tool)
            executing = store.update_tool_run(
                running.turn_id,
                tool.tool_call_id,
                ToolRunStatus.RUNNING,
                dispatch_started=False,
            )
            store.begin_dispatch(
                executing.turn_id,
                tool.tool_call_id,
                expected_record_revision=executing.record_revision,
            )
            store.update_tool_run(
                running.turn_id,
                tool.tool_call_id,
                ToolRunStatus.FAILED,
                error="discard failed",
            )
            failed = store.update(
                running.turn_id,
                TurnStatus.FAILED,
                stop_reason="direct model tool failed",
                error="discard failed",
            )

            proposal = DeterministicPCBCallProducer().next_call(
                failed, service.view, timeout=12.0
            )
            with self.assertRaisesRegex(PCBDraftError, "submit a new turn"):
                agent.run_turn(
                    "board",
                    running.turn_id,
                    timeout=12.0,
                    cancellation_requested=lambda: False,
                )

        self.assertIsNone(proposal)
        self.assertEqual(service.calls, [])

    def test_turn_and_tool_records_redact_credentials_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            sentinel = "sk-agent-secret-1234567890"
            turn = agent.start_turn("board", f"Build a sensor; api_key={sentinel}")

            queued = agent.store("board").load(turn.turn_id)
            self.assertNotIn(sentinel, queued.user_message)
            self.assertIn("[REDACTED]", queued.user_message)

            agent.run_turn(
                "board",
                turn.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )
            completed = agent.store("board").load(turn.turn_id)
            serialized = json.dumps(completed.to_dict(), sort_keys=True)
            self.assertNotIn(sentinel, serialized)
            self.assertIn("[REDACTED]", serialized)

    def test_review_mode_binds_approval_then_resumes_same_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(
                service,  # type: ignore[arg-type]
                permissions=PermissionBroker("review"),
            )
            turn = agent.start_turn("board", "Build a sensor board")

            waiting_view = agent.run_turn(
                "board",
                turn.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )

            self.assertEqual(waiting_view["project"]["status"], "awaiting_confirmation")
            waiting = agent.store("board").load(turn.turn_id)
            self.assertEqual(waiting.status, TurnStatus.WAITING_APPROVAL)
            self.assertIsNotNone(waiting.pending_approval)
            assert waiting.pending_approval is not None
            call_id = waiting.pending_approval.tool_call_id

            approved = agent.resolve_pending_approval(
                "board", **_approval_args(agent, waiting), approve=True
            )
            self.assertEqual(approved.status, TurnStatus.RUNNING)
            self.assertEqual(approved.tool_run(call_id).status, ToolRunStatus.RUNNING)

            completed_view = agent.run_turn(
                "board",
                turn.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )
            completed = agent.store("board").load(turn.turn_id)

            self.assertEqual(completed_view["project"]["status"], "validated")
            self.assertEqual(completed.status, TurnStatus.COMPLETED)
            approval = completed.approvals[0]
            self.assertEqual(approval.status, ApprovalStatus.APPROVED)
            self.assertEqual(approval.tool_call_id, call_id)
            self.assertEqual(service.calls, ["plan", "generate", "validate"])

    def test_stale_project_revision_cannot_approve_a_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(
                service,  # type: ignore[arg-type]
                permissions=PermissionBroker("review"),
            )
            turn = agent.start_turn("board", "Build a sensor board")
            agent.run_turn(
                "board",
                turn.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )
            service.view["state"]["revision"] = 2
            waiting = agent.store("board").load(turn.turn_id)

            with self.assertRaisesRegex(ValidationError, "stale"):
                agent.resolve_pending_approval(
                    "board", **_approval_args(agent, waiting), approve=True
                )

            waiting = agent.store("board").load(turn.turn_id)
            self.assertEqual(waiting.status, TurnStatus.WAITING_APPROVAL)

    def test_approval_resolves_the_displayed_turn_not_the_latest_checkpoint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(
                service,  # type: ignore[arg-type]
                permissions=PermissionBroker("review"),
            )
            first = agent.start_turn("board", "First board", thread_id="thread-a")
            agent.run_turn(
                "board",
                first.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )
            first_waiting = agent.store("board").load(first.turn_id)
            first_binding = _approval_args(agent, first_waiting)

            second = agent.start_turn("board", "Second board", thread_id="thread-b")
            agent.run_turn(
                "board",
                second.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )
            agent.resolve_pending_approval(
                "board", **first_binding, approve=True, decision_source="tui"
            )

            self.assertEqual(
                agent.store("board").load(first.turn_id).status,
                TurnStatus.RUNNING,
            )
            self.assertEqual(
                agent.store("board").load(second.turn_id).status,
                TurnStatus.WAITING_APPROVAL,
            )

    def test_post_dispatch_error_is_interrupted_and_never_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            service.fail_validation_once = True
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            turn = agent.start_turn("board", "Build a sensor board")

            with self.assertRaisesRegex(PCBDraftError, "outcome is unknown"):
                agent.run_turn(
                    "board",
                    turn.turn_id,
                    timeout=12.0,
                    cancellation_requested=lambda: False,
                )

            failed = agent.store("board").load(turn.turn_id)
            self.assertEqual(failed.status, TurnStatus.FAILED)
            self.assertEqual(service.calls, ["plan", "generate", "validate"])
            self.assertEqual(
                failed.tool_runs[-1].status,
                ToolRunStatus.INTERRUPTED,
            )
            self.assertIsNotNone(failed.tool_runs[-1].dispatch_started_at)

            with self.assertRaisesRegex(PCBDraftError, "submit a new turn"):
                agent.run_turn(
                    "board",
                    turn.turn_id,
                    timeout=12.0,
                    cancellation_requested=lambda: False,
                )

            self.assertEqual(service.calls, ["plan", "generate", "validate"])

    def test_crash_after_effect_never_replays_the_same_dispatched_call(self) -> None:
        class SimulatedProcessCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            turn = agent.start_turn("board", "Build a sensor board")
            execute = agent.executor.execute

            def commit_then_crash(*args: Any, **kwargs: Any) -> Any:
                execute(*args, **kwargs)
                raise SimulatedProcessCrash from None

            agent.executor.execute = commit_then_crash  # type: ignore[method-assign]
            with self.assertRaises(SimulatedProcessCrash):
                agent.run_turn(
                    "board",
                    turn.turn_id,
                    timeout=12.0,
                    cancellation_requested=lambda: False,
                )
            agent.executor.execute = execute  # type: ignore[method-assign]

            with self.assertRaisesRegex(PCBDraftError, "was not replayed"):
                agent.run_turn(
                    "board",
                    turn.turn_id,
                    timeout=12.0,
                    cancellation_requested=lambda: False,
                )
            with self.assertRaisesRegex(PCBDraftError, "submit a new turn"):
                agent.run_turn(
                    "board",
                    turn.turn_id,
                    timeout=12.0,
                    cancellation_requested=lambda: False,
                )

            self.assertEqual(service.calls, ["plan"])
            failed = agent.store("board").load(turn.turn_id)
            self.assertEqual(failed.status, TurnStatus.FAILED)
            self.assertEqual(
                failed.tool_runs[-1].status,
                ToolRunStatus.INTERRUPTED,
            )

    def test_job_retry_reuses_turn_id_but_refuses_ambiguous_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            service.fail_validation_once = True
            runner = JobRunner(service, workers=1)  # type: ignore[arg-type]
            try:
                submitted = runner.submit(
                    "board",
                    "agent_message",
                    {"text": "Build a sensor board", "timeout": 12.0},
                )
                self.assertNotIn("text", submitted["args"])
                turn_id = submitted["args"]["turn_id"]

                failed = self._wait_job(runner, "board", submitted["id"])
                self.assertEqual(failed["status"], "failed")

                retried = runner.retry("board", submitted["id"])
                self.assertEqual(retried["args"]["turn_id"], turn_id)
                blocked = self._wait_job(runner, "board", retried["id"])

                self.assertEqual(blocked["status"], "failed")
                self.assertIn("submit a new turn", blocked["error"])
                self.assertEqual(service.calls, ["plan", "generate", "validate"])
            finally:
                runner.shutdown()

    def test_queued_agent_job_cancellation_closes_its_turn(self) -> None:
        class DeferredPool:
            def submit(self, *_args: object, **_kwargs: object) -> object:
                return object()

            def shutdown(self, **_kwargs: object) -> None:
                del _kwargs

        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            runner = JobRunner(service, workers=1)  # type: ignore[arg-type]
            runner._pool.shutdown(wait=True)
            runner._pool = DeferredPool()  # type: ignore[assignment]

            submitted = runner.submit(
                "board",
                "agent_message",
                {"text": "Build a sensor board", "timeout": 12.0},
            )
            cancelled = runner.cancel("board", submitted["id"])
            turn = runner.agent.store("board").load(submitted["args"]["turn_id"])

            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(turn.status, TurnStatus.CANCELLED)
            self.assertEqual(
                turn.stop_reason,
                "the queued application job was cancelled before dispatch",
            )
            runner.shutdown()

    def test_explicit_user_action_is_also_a_durable_registered_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            service.view = _view(status="generated", revision=2)
            runner = JobRunner(service, workers=1)  # type: ignore[arg-type]
            try:
                submitted = runner.submit(
                    "board",
                    "agent_tool",
                    {"tool": "validate", "timeout": 12.0},
                )
                completed = self._wait_job(runner, "board", submitted["id"])
                turn_id = submitted["args"]["turn_id"]
                turn = runner.agent.store("board").load(turn_id)

                self.assertEqual(completed["status"], "completed")
                self.assertEqual(service.calls, ["validate"])
                self.assertEqual(turn.tool_runs[0].tool_name, "pcb_validate")
                self.assertEqual(turn.tool_runs[0].source, "user")
            finally:
                runner.shutdown()

    def test_runtime_surfaces_inline_approval_and_resumes_the_same_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(
                service,  # type: ignore[arg-type]
                permissions=PermissionBroker("review"),
            )
            runner = JobRunner(
                service,
                workers=1,
                orchestrator=agent,  # type: ignore[arg-type]
            )
            runtime = AgentRuntime(service, runner=runner)  # type: ignore[arg-type]
            try:
                started = runtime.submit_message(
                    "board", "Build a sensor board", timeout=12.0
                )
                original_turn_id = started.turn_id
                waiting_update = None
                for _ in range(250):
                    update = runtime.poll()
                    if update is not None and update.terminal:
                        waiting_update = update
                        break
                    time.sleep(0.01)
                assert waiting_update is not None

                self.assertEqual(waiting_update.turn_status, "waiting_approval")
                self.assertIsNotNone(waiting_update.pending_approval)
                self.assertFalse(runtime.active)

                checkpoint = runtime.pending_approval("board")
                assert checkpoint is not None
                resumed = runtime.resolve_pending(
                    "board", checkpoint=checkpoint, approve=True, timeout=12.0
                )
                self.assertEqual(resumed.turn_id, original_turn_id)
                completed_update = None
                for _ in range(250):
                    update = runtime.poll()
                    if update is not None and update.terminal:
                        completed_update = update
                        break
                    time.sleep(0.01)
                assert completed_update is not None

                self.assertEqual(completed_update.turn_status, "completed")
                self.assertEqual(service.calls, ["plan", "generate", "validate"])
                self.assertTrue(
                    any(
                        activity.tool == "pcb_generate_candidate"
                        for activity in completed_update.activities
                    )
                )
            finally:
                runner.shutdown()

    def test_approved_continuation_outbox_recovers_before_worker_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(
                service,  # type: ignore[arg-type]
                permissions=PermissionBroker("review"),
            )
            turn = agent.start_turn("board", "Build a sensor board")
            agent.run_turn(
                "board",
                turn.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )
            waiting = agent.store("board").load(turn.turn_id)
            runner = JobRunner(
                service,
                workers=1,
                orchestrator=agent,  # type: ignore[arg-type]
            )
            runner._schedule = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            job, approved = runner.resolve_approval(
                "board",
                **_approval_args(agent, waiting),
                approve=True,
                timeout=12.0,
                decision_source="tui",
            )
            assert job is not None
            self.assertEqual(job["status"], "queued")
            self.assertIsNone(
                approved.tool_run(
                    waiting.pending_approval.tool_call_id
                ).dispatch_started_at  # type: ignore[union-attr]
            )
            runner.shutdown()

            service.list_projects = lambda: [{"id": "board"}]  # type: ignore[method-assign]
            restarted = JobRunner(
                service,
                workers=1,
                orchestrator=agent,  # type: ignore[arg-type]
            )
            try:
                completed = self._wait_job(restarted, "board", job["id"])
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["result"]["turn_status"], "completed")
                self.assertEqual(service.calls, ["plan", "generate", "validate"])
            finally:
                restarted.shutdown()

    def test_startup_closes_an_orphan_turn_without_dispatching_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            turn = agent.start_turn("board", "Build a sensor board")
            service.list_projects = lambda: [{"id": "board"}]  # type: ignore[method-assign]

            runner = JobRunner(
                service,
                workers=1,
                orchestrator=agent,  # type: ignore[arg-type]
            )
            try:
                self.assertEqual(runner.list("board"), [])
                closed = agent.store("board").load(turn.turn_id)
                self.assertEqual(closed.status, TurnStatus.CANCELLED)
                self.assertIn("without a permission-bound job", closed.stop_reason)
                self.assertEqual(service.calls, [])
            finally:
                runner.shutdown()

    def test_startup_interrupts_a_running_orphan_with_a_diagnostic_reason(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            queued = agent.start_turn("board", "Build a sensor board")
            running = agent.store("board").update(queued.turn_id, TurnStatus.RUNNING)
            service.list_projects = lambda: [{"id": "board"}]  # type: ignore[method-assign]

            runner = JobRunner(
                service,
                workers=1,
                orchestrator=agent,  # type: ignore[arg-type]
            )
            try:
                self.assertEqual(runner.list("board"), [])
                closed = agent.store("board").load(running.turn_id)
                self.assertEqual(closed.status, TurnStatus.INTERRUPTED)
                self.assertIn("without a permission-bound job", closed.stop_reason)
                self.assertEqual(closed.error, closed.stop_reason)
                self.assertEqual(service.calls, [])
            finally:
                runner.shutdown()

    def test_dead_running_job_interrupts_its_turn_and_retry_stays_fail_closed(
        self,
    ) -> None:
        class DeferredPool:
            def submit(self, *_args: object, **_kwargs: object) -> object:
                return object()

            def shutdown(self, **_kwargs: object) -> None:
                del _kwargs

        class SimulatedProcessCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            first = JobRunner(
                service,
                workers=1,
                orchestrator=agent,  # type: ignore[arg-type]
            )
            first._pool.shutdown(wait=True)
            first._pool = DeferredPool()  # type: ignore[assignment]
            job = first.submit(
                "board",
                "agent_message",
                {"text": "Build a sensor board", "timeout": 12.0},
            )
            turn_id = job["args"]["turn_id"]
            execute = agent.executor.execute

            def crash_before_handler(*_args: Any, **_kwargs: Any) -> Any:
                raise SimulatedProcessCrash

            agent.executor.execute = crash_before_handler  # type: ignore[method-assign]
            with self.assertRaises(SimulatedProcessCrash):
                agent.run_turn(
                    "board",
                    turn_id,
                    timeout=12.0,
                    cancellation_requested=lambda: False,
                )
            agent.executor.execute = execute  # type: ignore[method-assign]
            job_path = service.root / "jobs" / f"{job['id']}.json"
            document = json.loads(job_path.read_text(encoding="utf-8"))
            document["status"] = "running"
            document["started_at"] = document["created_at"]
            job_path.write_text(json.dumps(document), encoding="utf-8")
            first.shutdown()

            service.list_projects = lambda: [{"id": "board"}]  # type: ignore[method-assign]
            restarted = JobRunner(
                service,
                workers=1,
                orchestrator=agent,  # type: ignore[arg-type]
            )
            try:
                recovered_job = restarted.get("board", job["id"])
                recovered_turn = agent.store("board").load(turn_id)
                self.assertEqual(recovered_job["status"], "interrupted")
                self.assertEqual(recovered_turn.status, TurnStatus.INTERRUPTED)
                self.assertEqual(
                    recovered_turn.tool_runs[-1].status,
                    ToolRunStatus.INTERRUPTED,
                )
                self.assertIsNotNone(recovered_turn.tool_runs[-1].dispatch_started_at)

                retried = restarted.retry("board", job["id"])
                failed = self._wait_job(restarted, "board", retried["id"])
                self.assertEqual(failed["status"], "failed")
                self.assertIn("submit a new turn", failed["error"])
                self.assertEqual(service.calls, [])
            finally:
                restarted.shutdown()

    def test_runtime_turn_poll_is_nonblocking_but_does_not_hide_corruption(
        self,
    ) -> None:
        class RunnerStub:
            def __init__(self, agent: AgentOrchestrator) -> None:
                self.agent = agent

        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            turn = agent.start_turn("board", "Build a sensor board")
            runtime = AgentRuntime(
                service,
                runner=RunnerStub(agent),  # type: ignore[arg-type]
            )

            started = time.monotonic()
            with ResourceLock(service.root, service.locks_root):
                self.assertIsNone(runtime._load_turn("board", turn.turn_id))
            self.assertLess(time.monotonic() - started, 0.25)

            path = service.root / "agent-turns" / f"{turn.turn_id}.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["unexpected"] = True
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "record is malformed"):
                runtime._load_turn("board", turn.turn_id)

    def test_runtime_restore_keeps_bounded_tool_history_across_turns(self) -> None:
        class RunnerStub:
            def __init__(self, agent: AgentOrchestrator) -> None:
                self.agent = agent

            def list(self, _project_id: str) -> list[dict[str, Any]]:
                return []

        with tempfile.TemporaryDirectory() as temporary:
            service = OrchestratorService(Path(temporary))
            agent = AgentOrchestrator(service)  # type: ignore[arg-type]
            first = agent.start_turn("board", "Build a sensor board")
            agent.run_turn(
                "board",
                first.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )
            second = agent.start_tool_turn("board", "validate")
            agent.run_turn(
                "board",
                second.turn_id,
                timeout=12.0,
                cancellation_requested=lambda: False,
            )
            runtime = AgentRuntime(
                service,
                runner=RunnerStub(agent),  # type: ignore[arg-type]
            )

            restored = runtime.restore_project("board")

            tool_activity = [
                activity
                for activity in restored.activities
                if activity.tool_call_id is not None
            ]
            self.assertEqual(len(tool_activity), 4)
            self.assertEqual(
                [activity.turn_id for activity in tool_activity],
                [first.turn_id, first.turn_id, first.turn_id, second.turn_id],
            )
            self.assertEqual(restored.turn_id, second.turn_id)
            self.assertEqual(tool_activity[-1].risk, "low")
            self.assertEqual(tool_activity[-1].before_revision, 3)
            self.assertEqual(tool_activity[-1].after_revision, 3)


if __name__ == "__main__":
    unittest.main()
