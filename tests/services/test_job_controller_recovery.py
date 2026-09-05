from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from pcbdraft.agent.conversations import ConversationOrchestrator
from pcbdraft.agent.orchestrator import AgentOrchestrator
from pcbdraft.agent.permissions import PermissionBroker
from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
from pcbdraft.agent.turns import TurnStatus
from pcbdraft.core.errors import ValidationError
from pcbdraft.services.jobs import (
    LEGACY_AGENT_JOB_POLICY_VERSION,
    LEGACY_CONTROLLER_ID,
    NATIVE_CONTROLLER_ID,
    JobRunner,
)


class _FakeService:
    def __init__(self, root: Path) -> None:
        self.projects_root = root / "projects"
        self.locks_root = root / "locks"
        self.locks_root.mkdir()
        project_root = self.projects_root / "board-a"
        project_root.mkdir(parents=True)
        (project_root / "jobs").mkdir(mode=0o700)
        self.progress: list[tuple[str, str]] = []

    def project_root(self, project_id: str) -> Path:
        if project_id != "board-a":
            raise ValidationError("unexpected test project")
        return self.projects_root / project_id

    def list_projects(self) -> list[dict[str, str]]:
        return [{"id": "board-a"}]

    def open_project(self, project_id: str) -> dict[str, Any]:
        self.project_root(project_id)
        return {
            "project": {"id": project_id, "status": "draft"},
            "state": {"revision": 0},
            "design": None,
        }

    def record_progress(
        self, project_id: str, kind: str, _message: str, *, level: str = "info"
    ) -> None:
        self.project_root(project_id)
        self.progress.append((kind, level))


class _RecoveryController(AgentOrchestrator):
    job_controller_identity = ""

    def __init__(self, service: _FakeService) -> None:
        self.service = service
        self.registry = DEFAULT_PCB_TOOL_REGISTRY
        self.permissions = PermissionBroker("workspace")
        self.run_calls: list[str] = []

    def run_turn(
        self,
        project_id: str,
        turn_id: str,
        *,
        timeout: float,
        cancellation_requested: Any,
    ) -> dict[str, Any]:
        del timeout
        if cancellation_requested():
            raise AssertionError("recovered test job was unexpectedly cancelled")
        store = self.store(project_id)
        current = store.load(turn_id)
        if current.status is TurnStatus.QUEUED:
            store.update(turn_id, TurnStatus.RUNNING)
        store.update(turn_id, TurnStatus.COMPLETED)
        self.run_calls.append(turn_id)
        return self.service.open_project(project_id)


class _NativeController(_RecoveryController):
    job_controller_identity = NATIVE_CONTROLLER_ID


class _LegacyController(_RecoveryController):
    job_controller_identity = LEGACY_CONTROLLER_ID


class JobControllerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = _FakeService(Path(self.temporary.name))

    def _admit_without_scheduling(
        self,
        service: _FakeService,
        controller_type: type[_RecoveryController],
    ) -> dict[str, Any]:
        runner = JobRunner(
            service,
            workers=1,
            orchestrator=controller_type(service),
        )
        runner._schedule = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        try:
            return runner.submit(
                "board-a",
                "agent_message",
                {"text": "Inspect this board", "timeout": 5.0},
            )
        finally:
            runner.shutdown()

    @staticmethod
    def _wait_for_terminal(
        runner: JobRunner, job_id: str, *, timeout: float = 2.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = runner.get("board-a", job_id)
            if job["status"] not in {"queued", "running", "cancel_requested"}:
                return job
            time.sleep(0.01)
        raise AssertionError("recovered job did not reach a terminal state")

    def test_cross_controller_restart_fails_closed_and_retry_is_rejected(self) -> None:
        cases = (
            (_NativeController, _LegacyController),
            (_LegacyController, _NativeController),
        )
        for admitting_type, restarting_type in cases:
            with (
                self.subTest(
                    admitted=admitting_type.job_controller_identity,
                    restarted=restarting_type.job_controller_identity,
                ),
                tempfile.TemporaryDirectory() as temporary,
            ):
                service = _FakeService(Path(temporary))
                queued = self._admit_without_scheduling(service, admitting_type)
                controller = restarting_type(service)
                restarted = JobRunner(service, workers=1, orchestrator=controller)
                try:
                    recovered = restarted.get("board-a", queued["id"])
                    self.assertEqual(recovered["status"], "failed")
                    self.assertIn("controller", recovered["error"])
                    self.assertIn("submit a new turn", recovered["error"])
                    self.assertEqual(controller.run_calls, [])
                    turn = controller.store("board-a").load(queued["args"]["turn_id"])
                    self.assertEqual(turn.status, TurnStatus.CANCELLED)
                    with self.assertRaisesRegex(ValidationError, "controller.*differs"):
                        restarted.retry("board-a", queued["id"])
                finally:
                    restarted.shutdown()

    def test_same_controller_restart_resumes_native_and_legacy_jobs(self) -> None:
        for controller_type in (_NativeController, _LegacyController):
            with (
                self.subTest(controller=controller_type.job_controller_identity),
                tempfile.TemporaryDirectory() as temporary,
            ):
                service = _FakeService(Path(temporary))
                queued = self._admit_without_scheduling(service, controller_type)
                controller = controller_type(service)
                restarted = JobRunner(service, workers=1, orchestrator=controller)
                try:
                    recovered = self._wait_for_terminal(restarted, queued["id"])
                    self.assertEqual(recovered["status"], "completed")
                    self.assertEqual(controller.run_calls, [queued["args"]["turn_id"]])
                finally:
                    restarted.shutdown()

    def test_controllerless_v1_policy_is_readable_but_never_dispatched(self) -> None:
        queued = self._admit_without_scheduling(self.service, _NativeController)
        path = self.service.project_root("board-a") / "jobs" / f"{queued['id']}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["agent_policy"]["version"] = LEGACY_AGENT_JOB_POLICY_VERSION
        del document["agent_policy"]["controller_id"]
        path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")

        controller = _NativeController(self.service)
        restarted = JobRunner(self.service, workers=1, orchestrator=controller)
        try:
            recovered = restarted.get("board-a", queued["id"])
            self.assertEqual(recovered["status"], "failed")
            self.assertIn("predates controller identity binding", recovered["error"])
            self.assertIn("cannot be determined", recovered["error"])
            self.assertEqual(controller.run_calls, [])
            with self.assertRaisesRegex(
                ValidationError, "predates controller identity binding"
            ):
                restarted.retry("board-a", queued["id"])
        finally:
            restarted.shutdown()

    def test_missing_or_invalid_policy_version_is_a_validation_error(self) -> None:
        for value in ({}, {"version": 3}, {"version": True}, {"version": 1.5}):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValidationError, "unsupported agent job policy binding"
                ),
            ):
                JobRunner._validate_agent_policy(value)

    def test_product_controller_types_have_stable_explicit_contracts(self) -> None:
        legacy = AgentOrchestrator(self.service, producer=object())  # type: ignore[arg-type]
        native = ConversationOrchestrator(
            self.service,
            producer=object(),  # type: ignore[arg-type]
            agent_factory=lambda **_kwargs: None,
        )
        self.assertEqual(
            JobRunner._resolve_controller_identity(legacy), LEGACY_CONTROLLER_ID
        )
        self.assertEqual(
            JobRunner._resolve_controller_identity(native), NATIVE_CONTROLLER_ID
        )


if __name__ == "__main__":
    unittest.main()
