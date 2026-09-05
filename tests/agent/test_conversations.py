from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pcbdraft.agent.conversations import ConversationOrchestrator
from pcbdraft.agent.permissions import PermissionBroker
from pcbdraft.agent.tool_bindings import (
    _handler,
    get_current_project_id,
    set_current_project_id,
)
from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY, ToolCall
from pcbdraft.agent.turns import ToolRunStatus, TurnStatus
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.services.gui_session import GuiSessionManager
from tests.agent.test_tool_bindings import FakePCBService


class ConversationService(FakePCBService):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.projects_root = root / "projects"
        self.locks_root = root / "locks"
        self.locks_root.mkdir()
        for name in ("board-a", "board-b"):
            (self.projects_root / name).mkdir(parents=True)
            (self.projects_root / name / "jobs").mkdir(mode=0o700)
        self.replies: list[tuple[str, str]] = []

    def project_root(self, project_id: str) -> Path:
        if project_id not in {"board-a", "board-b"}:
            raise ValueError("unexpected project")
        return self.projects_root / project_id

    def list_projects(self) -> list[dict[str, str]]:
        return [{"id": name} for name in ("board-a", "board-b")]

    def reply_message(self, project_id: str, text: str, **_kwargs: Any) -> None:
        self.replies.append((project_id, text))


class ScriptedAgent:
    def __init__(self, session_id: str, action: Any) -> None:
        self.session_id = session_id
        self.action = action
        self.interrupted = threading.Event()
        self.closed = False
        self.run_calls = 0

    def run_conversation(self, prompt: str, **_kwargs: Any) -> dict[str, Any]:
        self.run_calls += 1
        self.action(self, prompt)
        return {"completed": True, "final_response": "已查看，未修改设计。"}

    def interrupt(self, **_kwargs: Any) -> None:
        self.interrupted.set()

    def close(self) -> None:
        self.closed = True


class RecordingSessionDB:
    def __init__(self) -> None:
        self.closed = False
        self.history_reads = 0

    def get_messages_as_conversation(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        self.history_reads += 1
        return []

    def close(self) -> None:
        self.closed = True


class NativeConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ, {"PCBDRAFT_RUNTIME_HOME": str(self.root / "runtime")}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = ConversationService(self.root)
        self.agents: list[ScriptedAgent] = []

    def orchestrator(self, action: Any, **kwargs: Any) -> ConversationOrchestrator:
        def factory(*, session_id: str, session_db: Any) -> ScriptedAgent:
            del session_db
            agent = ScriptedAgent(session_id, action)
            self.agents.append(agent)
            return agent

        return ConversationOrchestrator(self.service, agent_factory=factory, **kwargs)

    @staticmethod
    def inspect(agent: ScriptedAgent, _prompt: str) -> dict[str, Any]:
        handler = _handler(DEFAULT_PCB_TOOL_REGISTRY.resolve("inspect_project"))
        result = json.loads(handler({}, session_id=agent.session_id))
        if not result["success"]:
            raise AssertionError(result)
        return result

    def run_turn(
        self, orchestrator: ConversationOrchestrator, project_id: str = "board-a"
    ):
        turn = orchestrator.start_turn(project_id, "只查看当前设计，不要修改")
        orchestrator.run_turn(
            project_id, turn.turn_id, timeout=10, cancellation_requested=lambda: False
        )
        return orchestrator.store(project_id).load(turn.turn_id)

    def test_read_request_records_actual_tool_and_reply_without_fixed_planning(
        self,
    ) -> None:
        orchestrator = self.orchestrator(self.inspect)
        turn = self.run_turn(orchestrator)
        self.assertEqual(turn.status, TurnStatus.COMPLETED)
        self.assertEqual(
            [run.tool_name for run in turn.tool_runs], ["pcb_inspect_project"]
        )
        self.assertEqual(turn.tool_runs[0].status, ToolRunStatus.COMPLETED)
        self.assertIsNotNone(turn.tool_runs[0].dispatch_started_at)
        self.assertEqual(turn.assistant_texts, ("已查看，未修改设计。",))
        self.assertEqual(
            [call[2] for call in self.service.calls if call[0] == "execute"],
            ["inspect_project"],
        )
        self.assertTrue(self.agents[0].closed)

    def test_prose_only_reply_does_not_trigger_a_tool(self) -> None:
        turn = self.run_turn(self.orchestrator(lambda *_args: None))
        self.assertEqual(turn.status, TurnStatus.COMPLETED)
        self.assertEqual(turn.tool_runs, ())
        self.assertTrue(turn.assistant_texts)

    def test_concurrent_projects_keep_separate_tool_authority(self) -> None:
        barrier = threading.Barrier(2)
        observed: list[str] = []

        def inspect(agent: ScriptedAgent, prompt: str) -> None:
            barrier.wait(timeout=5)
            observed.append(self.inspect(agent, prompt)["project_id"])

        set_current_project_id("terminal-project")
        self.addCleanup(set_current_project_id, None)
        orchestrator = self.orchestrator(inspect)
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = [
                pool.submit(self.run_turn, orchestrator, name)
                for name in ("board-a", "board-b")
            ]
            for run in runs:
                self.assertEqual(run.result(timeout=10).status, TurnStatus.COMPLETED)
        self.assertCountEqual(observed, ["board-a", "board-b"])
        self.assertEqual(get_current_project_id(), "terminal-project")

    def test_read_only_denial_is_durable_and_stops_before_execution(self) -> None:
        def write(agent: ScriptedAgent, _prompt: str) -> None:
            handler = _handler(DEFAULT_PCB_TOOL_REGISTRY.resolve("export_gerbers"))
            receipt = json.loads(handler({}, session_id=agent.session_id))
            self.assertFalse(receipt["success"])

        turn = self.run_turn(
            self.orchestrator(write, permissions=PermissionBroker("read_only"))
        )
        self.assertEqual(turn.status, TurnStatus.CANCELLED)
        self.assertFalse(any(call[0] == "execute" for call in self.service.calls))
        self.assertTrue(self.agents[0].interrupted.is_set())

    def test_cancelled_queued_turn_never_creates_an_agent(self) -> None:
        orchestrator = self.orchestrator(self.inspect)
        turn = orchestrator.start_turn("board-a", "hello")
        orchestrator.run_turn(
            "board-a", turn.turn_id, timeout=10, cancellation_requested=lambda: True
        )
        self.assertEqual(
            orchestrator.store("board-a").load(turn.turn_id).status,
            TurnStatus.CANCELLED,
        )
        self.assertEqual(self.agents, [])

    def test_approved_write_executes_once_then_closes_with_local_receipt(self) -> None:
        def action(agent: ScriptedAgent, prompt: str) -> None:
            self.inspect(agent, prompt)
            handler = _handler(DEFAULT_PCB_TOOL_REGISTRY.resolve("unroute_net"))
            receipt = json.loads(
                handler({"net_id": "GND"}, session_id=agent.session_id)
            )
            self.assertFalse(receipt["success"])

        orchestrator = self.orchestrator(action, permissions=PermissionBroker("review"))
        waiting = self.run_turn(orchestrator)
        self.assertEqual(waiting.status, TurnStatus.WAITING_APPROVAL)
        self.assertEqual(
            [run.status for run in waiting.tool_runs],
            [ToolRunStatus.COMPLETED, ToolRunStatus.WAITING_APPROVAL],
        )
        self.assertIsNone(waiting.tool_runs[1].dispatch_started_at)
        payload = orchestrator.approval_payload(waiting)
        assert payload is not None
        orchestrator.resolve_pending_approval(
            "board-a",
            turn_id=payload["turn_id"],
            checkpoint_id=payload["checkpoint_id"],
            tool_call_id=payload["tool_call_id"],
            tool_name=payload["tool_name"],
            effect=payload["effect"],
            risk=payload["risk"],
            args_hash=payload["args_hash"],
            baseline_revision=payload["baseline_revision"],
            approve=True,
        )
        orchestrator.run_turn(
            "board-a", waiting.turn_id, timeout=10, cancellation_requested=lambda: False
        )
        completed = orchestrator.store("board-a").load(waiting.turn_id)
        self.assertEqual(completed.status, TurnStatus.COMPLETED)
        self.assertEqual(len(completed.tool_runs), 2)
        self.assertEqual(len(self.agents), 1)
        self.assertEqual(self.agents[0].run_calls, 1)
        self.assertIn('"tool": "pcb_unroute_net"', completed.assistant_texts[-1])
        self.assertIn('"after_status": "generated"', completed.assistant_texts[-1])
        self.assertIn("如需继续，请发送新消息", completed.assistant_texts[-1])
        self.assertEqual(
            [call[2] for call in self.service.calls if call[0] == "execute"],
            ["inspect_project", "unroute_net"],
        )

    def test_approved_business_failure_reply_reports_actual_status(self) -> None:
        class FailedResultService(ConversationService):
            def execute_pcb_tool(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                view = super().execute_pcb_tool(*args, **kwargs)
                view["project"]["status"] = "generation_failed"
                return view

        with tempfile.TemporaryDirectory() as tmp:
            service = FailedResultService(Path(tmp))
            agents: list[ScriptedAgent] = []

            def request_write(agent: ScriptedAgent, _prompt: str) -> None:
                handler = _handler(DEFAULT_PCB_TOOL_REGISTRY.resolve("unroute_net"))
                receipt = json.loads(
                    handler({"net_id": "GND"}, session_id=agent.session_id)
                )
                self.assertFalse(receipt["success"])

            def factory(*, session_id: str, session_db: Any) -> ScriptedAgent:
                del session_db
                agent = ScriptedAgent(session_id, request_write)
                agents.append(agent)
                return agent

            orchestrator = ConversationOrchestrator(
                service,
                agent_factory=factory,
                permissions=PermissionBroker("review"),
            )
            turn = orchestrator.start_turn("board-a", "执行需要审批的操作")
            orchestrator.run_turn(
                "board-a",
                turn.turn_id,
                timeout=10,
                cancellation_requested=lambda: False,
            )
            waiting = orchestrator.store("board-a").load(turn.turn_id)
            payload = orchestrator.approval_payload(waiting)
            assert payload is not None
            orchestrator.resolve_pending_approval(
                "board-a",
                turn_id=payload["turn_id"],
                checkpoint_id=payload["checkpoint_id"],
                tool_call_id=payload["tool_call_id"],
                tool_name=payload["tool_name"],
                effect=payload["effect"],
                risk=payload["risk"],
                args_hash=payload["args_hash"],
                baseline_revision=payload["baseline_revision"],
                approve=True,
            )
            orchestrator.run_turn(
                "board-a",
                turn.turn_id,
                timeout=10,
                cancellation_requested=lambda: False,
            )

            completed = orchestrator.store("board-a").load(turn.turn_id)
            reply = completed.assistant_texts[-1]
            self.assertEqual(completed.status, TurnStatus.COMPLETED)
            self.assertIn('"after_status": "generation_failed"', reply)
            self.assertNotIn("成功", reply)
            self.assertNotIn('"success"', reply)
            self.assertEqual(len(agents), 1)
            self.assertEqual(agents[0].run_calls, 1)

    def test_terminal_turn_with_completed_tool_cannot_replay_on_retry(self) -> None:
        def exercise(terminal_status: TurnStatus) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                service = ConversationService(Path(tmp))
                agents: list[ScriptedAgent] = []

                def write_then_stop(agent: ScriptedAgent, _prompt: str) -> None:
                    handler = _handler(DEFAULT_PCB_TOOL_REGISTRY.resolve("unroute_net"))
                    receipt = json.loads(
                        handler({"net_id": "GND"}, session_id=agent.session_id)
                    )
                    self.assertTrue(receipt["success"], receipt)
                    current = orchestrator.latest_turn("board-a")
                    assert current is not None
                    store = orchestrator.store("board-a")
                    if terminal_status is TurnStatus.INTERRUPTED:
                        store.interrupt_active(
                            current.turn_id,
                            "the process stopped after the tool receipt was committed",
                        )
                    elif terminal_status is TurnStatus.CANCELLED:
                        store.cancel(
                            current.turn_id, "cancelled after the durable receipt"
                        )
                    else:
                        store.update(
                            current.turn_id,
                            TurnStatus.FAILED,
                            error="failed after the durable receipt",
                            stop_reason="failed after the durable receipt",
                        )

                def factory(*, session_id: str, session_db: Any) -> ScriptedAgent:
                    del session_db
                    agent = ScriptedAgent(session_id, write_then_stop)
                    agents.append(agent)
                    return agent

                orchestrator = ConversationOrchestrator(service, agent_factory=factory)
                turn = orchestrator.start_turn(
                    "board-a", "完成一次写操作后模拟进程中断"
                )
                orchestrator.run_turn(
                    "board-a",
                    turn.turn_id,
                    timeout=10,
                    cancellation_requested=lambda: False,
                )
                stopped = orchestrator.store("board-a").load(turn.turn_id)
                self.assertEqual(stopped.status, terminal_status)
                self.assertEqual(stopped.tool_runs[0].status, ToolRunStatus.COMPLETED)
                self.assertIsNotNone(stopped.tool_runs[0].result)

                with self.assertRaisesRegex(PCBDraftError, "completed PCB tool"):
                    orchestrator.run_turn(
                        "board-a",
                        stopped.turn_id,
                        timeout=10,
                        cancellation_requested=lambda: False,
                    )
                self.assertEqual(
                    [call[2] for call in service.calls if call[0] == "execute"],
                    ["unroute_net"],
                )
                retained = orchestrator.store("board-a").load(stopped.turn_id)
                self.assertEqual(retained.status, terminal_status)
                self.assertEqual(len(retained.tool_runs), 1)
                self.assertEqual(len(agents), 1)

        for terminal_status in (
            TurnStatus.FAILED,
            TurnStatus.INTERRUPTED,
            TurnStatus.CANCELLED,
        ):
            with self.subTest(status=terminal_status.value):
                exercise(terminal_status)

    def test_running_turn_with_completed_tool_is_closed_before_new_turn(self) -> None:
        for with_proposal in (False, True):
            with (
                self.subTest(with_proposal=with_proposal),
                tempfile.TemporaryDirectory() as tmp,
            ):
                service = ConversationService(Path(tmp))
                agents: list[ScriptedAgent] = []

                def factory(
                    *,
                    session_id: str,
                    session_db: Any,
                    _agents: list[ScriptedAgent] = agents,
                ) -> ScriptedAgent:
                    del session_db
                    agent = ScriptedAgent(session_id, lambda *_args: None)
                    _agents.append(agent)
                    return agent

                orchestrator = ConversationOrchestrator(service, agent_factory=factory)
                turn = orchestrator.start_turn("board-a", "旧回合恢复")
                store = orchestrator.store("board-a")
                store.update(turn.turn_id, TurnStatus.RUNNING)
                view = service.open_project("board-a")
                read = ToolCall(
                    name="inspect_project",
                    project_id="board-a",
                    source="model",
                    arguments={},
                    baseline_revision=1,
                )
                orchestrator._execute_durable_call(
                    store,
                    turn.turn_id,
                    read,
                    timeout=10,
                    interrupt=lambda: None,
                )
                if with_proposal:
                    write = ToolCall(
                        name="unroute_net",
                        project_id="board-a",
                        source="model",
                        arguments={"net_id": "GND"},
                        baseline_revision=1,
                    )
                    orchestrator._persist_proposal(
                        store, store.load(turn.turn_id), write, view
                    )

                with self.assertRaisesRegex(PCBDraftError, "completed PCB tool"):
                    orchestrator.run_turn(
                        "board-a",
                        turn.turn_id,
                        timeout=10,
                        cancellation_requested=lambda: False,
                    )
                closed = store.load(turn.turn_id)
                self.assertEqual(closed.status, TurnStatus.INTERRUPTED)
                self.assertEqual(closed.tool_runs[0].status, ToolRunStatus.COMPLETED)
                if with_proposal:
                    self.assertEqual(
                        closed.tool_runs[1].status, ToolRunStatus.CANCELLED
                    )
                self.assertEqual(agents, [])
                successor = orchestrator.start_turn("board-a", "新回合继续")
                self.assertEqual(successor.status, TurnStatus.QUEUED)

    def test_reconciled_running_tool_is_closed_without_model_replay(self) -> None:
        class ReconciledService(ConversationService):
            receipt: dict[str, Any] | None = None

            def open_project(self, project_id: str) -> dict[str, Any]:
                view = super().open_project(project_id)
                if self.receipt is not None:
                    view["state"]["revision"] = 2
                    view["tool_receipts"] = {self.receipt["tool_call_id"]: self.receipt}
                return view

        with tempfile.TemporaryDirectory() as tmp:
            service = ReconciledService(Path(tmp))
            agents: list[ScriptedAgent] = []

            def factory(*, session_id: str, session_db: Any) -> ScriptedAgent:
                del session_db
                agent = ScriptedAgent(session_id, lambda *_args: None)
                agents.append(agent)
                return agent

            orchestrator = ConversationOrchestrator(service, agent_factory=factory)
            turn = orchestrator.start_turn("board-a", "恢复已落盘的写操作")
            store = orchestrator.store("board-a")
            store.update(turn.turn_id, TurnStatus.RUNNING)
            call = ToolCall(
                name="unroute_net",
                project_id="board-a",
                source="model",
                arguments={"net_id": "GND"},
                baseline_revision=1,
            )
            proposed = orchestrator._persist_proposal(
                store, store.load(turn.turn_id), call, service.open_project("board-a")
            )
            active = proposed.tool_runs[-1]
            store.update_tool_run(
                turn.turn_id,
                active.tool_call_id,
                ToolRunStatus.RUNNING,
                dispatch_started=False,
            )
            store.begin_dispatch(turn.turn_id, active.tool_call_id)
            service.receipt = {
                "tool_call_id": active.tool_call_id,
                "tool_name": active.tool_name,
                "args_hash": active.args_hash,
                "baseline_revision": active.baseline_revision,
                "after_status": "generated",
                "after_revision": 2,
            }

            with self.assertRaisesRegex(PCBDraftError, "completed PCB tool"):
                orchestrator.run_turn(
                    "board-a",
                    turn.turn_id,
                    timeout=10,
                    cancellation_requested=lambda: False,
                )
            closed = store.load(turn.turn_id)
            self.assertEqual(closed.status, TurnStatus.INTERRUPTED)
            self.assertEqual(closed.tool_runs[0].status, ToolRunStatus.COMPLETED)
            self.assertIsNotNone(closed.tool_runs[0].result)
            self.assertEqual(agents, [])
            successor = orchestrator.start_turn("board-a", "检查后继续")
            self.assertEqual(successor.status, TurnStatus.QUEUED)

    def test_running_turn_may_repeat_the_same_read_call(self) -> None:
        def inspect_twice(agent: ScriptedAgent, prompt: str) -> None:
            first = self.inspect(agent, prompt)
            second = self.inspect(agent, prompt)
            self.assertEqual(first["project_id"], "board-a")
            self.assertEqual(second["project_id"], "board-a")

        turn = self.run_turn(self.orchestrator(inspect_twice))
        self.assertEqual(turn.status, TurnStatus.COMPLETED)
        self.assertEqual(
            [run.tool_name for run in turn.tool_runs],
            ["pcb_inspect_project", "pcb_inspect_project"],
        )
        self.assertEqual(
            [call[2] for call in self.service.calls if call[0] == "execute"],
            ["inspect_project", "inspect_project"],
        )

    def test_cancellation_after_factory_skips_model_and_closes_resources(self) -> None:
        cancelled = threading.Event()
        databases: list[RecordingSessionDB] = []

        def database_factory(*_args: Any, **_kwargs: Any) -> RecordingSessionDB:
            database = RecordingSessionDB()
            databases.append(database)
            return database

        def factory(*, session_id: str, session_db: Any) -> ScriptedAgent:
            del session_db
            agent = ScriptedAgent(session_id, lambda *_args: None)
            self.agents.append(agent)
            cancelled.set()
            return agent

        orchestrator = ConversationOrchestrator(self.service, agent_factory=factory)
        turn = orchestrator.start_turn("board-a", "cancel during initialization")
        with patch(
            "pcbdraft.services.session_db.SessionDB", side_effect=database_factory
        ):
            orchestrator.run_turn(
                "board-a",
                turn.turn_id,
                timeout=10,
                cancellation_requested=cancelled.is_set,
            )

        stopped = orchestrator.store("board-a").load(turn.turn_id)
        self.assertEqual(stopped.status, TurnStatus.CANCELLED)
        self.assertEqual(self.agents[0].run_calls, 0)
        self.assertTrue(self.agents[0].closed)
        self.assertEqual(databases[0].history_reads, 0)
        self.assertTrue(databases[0].closed)
        self.assertFalse(any(call[0] == "execute" for call in self.service.calls))

    def test_deadline_after_factory_skips_model_and_closes_resources(self) -> None:
        databases: list[RecordingSessionDB] = []

        def database_factory(*_args: Any, **_kwargs: Any) -> RecordingSessionDB:
            database = RecordingSessionDB()
            databases.append(database)
            return database

        def factory(*, session_id: str, session_db: Any) -> ScriptedAgent:
            del session_db
            agent = ScriptedAgent(session_id, lambda *_args: None)
            self.agents.append(agent)
            threading.Event().wait(0.02)
            return agent

        orchestrator = ConversationOrchestrator(self.service, agent_factory=factory)
        turn = orchestrator.start_turn("board-a", "timeout during initialization")
        with (
            patch(
                "pcbdraft.services.session_db.SessionDB", side_effect=database_factory
            ),
            self.assertRaisesRegex(
                PCBDraftError, "timed out during agent initialization"
            ),
        ):
            orchestrator.run_turn(
                "board-a",
                turn.turn_id,
                timeout=0.01,
                cancellation_requested=lambda: False,
            )

        stopped = orchestrator.store("board-a").load(turn.turn_id)
        self.assertEqual(stopped.status, TurnStatus.FAILED)
        self.assertEqual(self.agents[0].run_calls, 0)
        self.assertTrue(self.agents[0].closed)
        self.assertEqual(databases[0].history_reads, 0)
        self.assertTrue(databases[0].closed)
        self.assertFalse(any(call[0] == "execute" for call in self.service.calls))

    def test_cancellation_interrupts_a_running_model_and_closes_the_turn(self) -> None:
        cancelled = threading.Event()

        def action(agent: ScriptedAgent, _prompt: str) -> None:
            cancelled.set()
            self.assertTrue(agent.interrupted.wait(timeout=2))

        orchestrator = self.orchestrator(action)
        turn = orchestrator.start_turn("board-a", "hello")
        orchestrator.run_turn(
            "board-a",
            turn.turn_id,
            timeout=10,
            cancellation_requested=cancelled.is_set,
        )
        self.assertEqual(
            orchestrator.store("board-a").load(turn.turn_id).status,
            TurnStatus.CANCELLED,
        )
        self.assertEqual(self.service.replies, [])
        self.assertTrue(self.agents[0].closed)

    def test_web_default_uses_the_native_conversation_controller(self) -> None:
        manager = GuiSessionManager(self.service)
        try:
            self.assertIsInstance(manager.jobs.agent, ConversationOrchestrator)
        finally:
            manager.shutdown()
