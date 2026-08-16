from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict

from pcbdraft.agent.turns import (
    APPROVAL_SCHEMA,
    APPROVAL_VERSION,
    TOOL_RUN_SCHEMA,
    TOOL_RUN_VERSION,
    TURN_SCHEMA,
    TURN_VERSION,
    AgentTurnStore,
    ApprovalStatus,
    ToolRunRecord,
    ToolRunStatus,
    TurnRecord,
    TurnStatus,
    hash_tool_arguments,
)
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.locking import ResourceLock


class _ApprovalBinding(TypedDict):
    tool_name: str
    effect: str
    risk: str
    args_hash: str
    baseline_revision: int
    checkpoint_id: str


class AgentTurnStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        workspace = Path(self.temporary.name)
        self.project_root = workspace / "board"
        self.project_root.mkdir()
        self.locks_root = workspace / "locks"
        self.store = AgentTurnStore(self.project_root, self.locks_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _begin_running(
        self, *, turn_id: str = "turn-test", thread_id: str = "thread-main"
    ) -> TurnRecord:
        queued = self.store.begin(
            project_id="board",
            thread_id=thread_id,
            turn_id=turn_id,
            user_message="Create a small sensor board",
            baseline_revision=7,
        )
        return self.store.update(
            queued.turn_id,
            TurnStatus.RUNNING,
            expected_record_revision=queued.record_revision,
        )

    @staticmethod
    def _tool(turn: TurnRecord, *, tool_call_id: str = "call-plan") -> ToolRunRecord:
        arguments = {"message": "Create a small sensor board"}
        return ToolRunRecord.proposed(
            project_id=turn.project_id,
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            tool_call_id=tool_call_id,
            tool_name="pcb_plan_request",
            source="runtime_policy",
            effect="conversation_write",
            risk="low",
            arguments=arguments,
            args_hash=hash_tool_arguments(arguments),
            baseline_revision=turn.baseline_revision,
            before_status="draft",
            before_revision=turn.baseline_revision,
        )

    @staticmethod
    def _approval_binding(record: TurnRecord) -> _ApprovalBinding:
        checkpoint = record.pending_approval
        if checkpoint is None:
            raise AssertionError("turn has no pending approval")
        return _ApprovalBinding(
            tool_name=checkpoint.tool_name,
            effect=checkpoint.effect,
            risk=checkpoint.risk,
            args_hash=checkpoint.args_hash,
            baseline_revision=checkpoint.baseline_revision,
            checkpoint_id=checkpoint.checkpoint_id,
        )

    def test_begin_writes_versioned_strict_round_trip(self) -> None:
        turn = self.store.begin(
            project_id="board",
            thread_id="thread-main",
            turn_id="turn-one",
            user_message="Build a board",
            baseline_revision=3,
        )

        path = self.project_root / "agent-turns" / "turn-one.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], TURN_SCHEMA)
        self.assertEqual(document["version"], TURN_VERSION)
        self.assertEqual(document["record_revision"], 0)
        self.assertEqual(self.store.load("turn-one"), turn)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_tool_run_lifecycle_retains_all_call_bindings(self) -> None:
        running = self._begin_running()
        proposed = self._tool(running)

        with_tool = self.store.append_tool_run(
            running.turn_id,
            proposed,
            expected_record_revision=running.record_revision,
        )
        executing = self.store.update_tool_run(
            running.turn_id,
            proposed.tool_call_id,
            ToolRunStatus.RUNNING,
            expected_record_revision=with_tool.record_revision,
        )
        executed = self.store.update_tool_run(
            running.turn_id,
            proposed.tool_call_id,
            ToolRunStatus.COMPLETED,
            after_status="awaiting_confirmation",
            after_revision=8,
            result={"after_revision": 8, "status": "awaiting_confirmation"},
            expected_record_revision=executing.record_revision,
        )
        completed = self.store.update(
            running.turn_id,
            TurnStatus.COMPLETED,
            stop_reason="agent_finished",
            expected_record_revision=executed.record_revision,
        )

        tool = completed.tool_run("call-plan")
        self.assertEqual(tool.project_id, "board")
        self.assertEqual(tool.thread_id, "thread-main")
        self.assertEqual(tool.turn_id, "turn-test")
        self.assertEqual(tool.tool_call_id, "call-plan")
        self.assertEqual(tool.source, "runtime_policy")
        self.assertEqual(tool.effect, "conversation_write")
        self.assertEqual(tool.risk, "low")
        self.assertEqual(tool.args_hash, hash_tool_arguments(tool.arguments))
        self.assertEqual(tool.baseline_revision, 7)
        self.assertEqual(tool.before_status, "draft")
        self.assertEqual(tool.before_revision, 7)
        self.assertEqual(tool.after_status, "awaiting_confirmation")
        self.assertEqual(tool.after_revision, 8)
        self.assertEqual(tool.status, ToolRunStatus.COMPLETED)
        self.assertEqual(tool.result["after_revision"], 8)  # type: ignore[index]
        document = json.loads(
            (self.project_root / "agent-turns" / "turn-test.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(document["tool_runs"][0]["schema"], TOOL_RUN_SCHEMA)
        self.assertEqual(document["tool_runs"][0]["version"], TOOL_RUN_VERSION)

    def test_waiting_approval_is_recoverable_after_store_restart(self) -> None:
        running = self._begin_running()
        with_tool = self.store.append_tool_run(running.turn_id, self._tool(running))
        waiting = self.store.request_approval(
            running.turn_id,
            "call-plan",
            checkpoint_id="approval-plan",
            expected_record_revision=with_tool.record_revision,
        )

        restarted = AgentTurnStore(self.project_root, self.locks_root)
        recovered = restarted.waiting_approval(thread_id="thread-main")

        self.assertIsNotNone(recovered)
        if recovered is None:
            raise AssertionError("waiting approval was not recovered")
        self.assertEqual(recovered, waiting)
        self.assertEqual(recovered.status, TurnStatus.WAITING_APPROVAL)
        checkpoint = recovered.pending_approval
        self.assertIsNotNone(checkpoint)
        if checkpoint is None:
            raise AssertionError("pending checkpoint was not recovered")
        self.assertEqual(checkpoint.checkpoint_id, "approval-plan")
        self.assertEqual(checkpoint.tool_call_id, "call-plan")
        self.assertEqual(checkpoint.tool_name, "pcb_plan_request")
        self.assertEqual(checkpoint.effect, "conversation_write")
        self.assertEqual(checkpoint.risk, "low")
        self.assertEqual(checkpoint.args_hash, self._tool(running).args_hash)
        self.assertEqual(checkpoint.baseline_revision, 7)
        document = waiting.to_dict()["approvals"][0]
        self.assertEqual(document["schema"], APPROVAL_SCHEMA)
        self.assertEqual(document["version"], APPROVAL_VERSION)

    def test_approved_checkpoint_starts_exact_bound_tool(self) -> None:
        running = self._begin_running()
        with_tool = self.store.append_tool_run(running.turn_id, self._tool(running))
        waiting = self.store.request_approval(running.turn_id, "call-plan")

        resumed = self.store.resolve_approval(
            running.turn_id,
            "call-plan",
            ApprovalStatus.APPROVED,
            **self._approval_binding(waiting),
            current_revision=7,
            decision_source="user",
            reason="Apply this exact candidate",
            expected_record_revision=waiting.record_revision,
        )

        self.assertEqual(resumed.status, TurnStatus.RUNNING)
        self.assertIsNone(resumed.pending_approval)
        self.assertEqual(resumed.tool_run("call-plan").status, ToolRunStatus.RUNNING)
        self.assertIsNone(resumed.tool_run("call-plan").dispatch_started_at)
        dispatched = self.store.begin_dispatch(
            resumed.turn_id,
            "call-plan",
            expected_record_revision=resumed.record_revision,
        )
        self.assertIsNotNone(dispatched.tool_run("call-plan").dispatch_started_at)
        self.assertEqual(resumed.approvals[-1].status, ApprovalStatus.APPROVED)
        self.assertEqual(resumed.approvals[-1].decision_source, "user")
        self.assertEqual(with_tool.tool_run("call-plan").status, ToolRunStatus.PROPOSED)

    def test_stale_revision_cannot_approve_and_checkpoint_stays_pending(self) -> None:
        running = self._begin_running()
        with_tool = self.store.append_tool_run(running.turn_id, self._tool(running))
        waiting = self.store.request_approval(running.turn_id, "call-plan")

        with self.assertRaisesRegex(ValidationError, "revision is stale"):
            self.store.resolve_approval(
                running.turn_id,
                "call-plan",
                ApprovalStatus.APPROVED,
                **self._approval_binding(waiting),
                current_revision=8,
                decision_source="user",
                expected_record_revision=waiting.record_revision,
            )

        unchanged = self.store.load(running.turn_id)
        self.assertEqual(unchanged.record_revision, waiting.record_revision)
        self.assertEqual(unchanged.status, TurnStatus.WAITING_APPROVAL)
        self.assertEqual(
            unchanged.pending_approval.status,  # type: ignore[union-attr]
            ApprovalStatus.PENDING,
        )
        self.assertEqual(with_tool.baseline_revision, 7)

    def test_approval_payload_must_match_call_hash_and_baseline(self) -> None:
        running = self._begin_running()
        self.store.append_tool_run(running.turn_id, self._tool(running))
        waiting = self.store.request_approval(running.turn_id, "call-plan")

        with self.assertRaisesRegex(ValidationError, "payload does not match"):
            self.store.resolve_approval(
                running.turn_id,
                "call-plan",
                ApprovalStatus.APPROVED,
                tool_name="pcb_plan_request",
                effect="conversation_write",
                risk="low",
                args_hash="0" * 64,
                baseline_revision=7,
                current_revision=7,
                decision_source="user",
                expected_record_revision=waiting.record_revision,
            )
        with self.assertRaisesRegex(ValidationError, "payload does not match"):
            self.store.resolve_approval(
                running.turn_id,
                "call-plan",
                ApprovalStatus.APPROVED,
                tool_name="pcb_plan_request",
                effect="conversation_write",
                risk="low",
                args_hash=self._tool(running).args_hash,
                baseline_revision=6,
                current_revision=7,
                decision_source="user",
                expected_record_revision=waiting.record_revision,
            )

        self.assertEqual(
            self.store.load(running.turn_id).status,
            TurnStatus.WAITING_APPROVAL,
        )

    def test_denial_is_safe_even_if_project_revision_changed(self) -> None:
        running = self._begin_running()
        self.store.append_tool_run(running.turn_id, self._tool(running))
        waiting = self.store.request_approval(running.turn_id, "call-plan")

        resumed = self.store.resolve_approval(
            running.turn_id,
            "call-plan",
            ApprovalStatus.DENIED,
            **self._approval_binding(waiting),
            current_revision=99,
            decision_source="user",
            reason="Do not make this change",
            expected_record_revision=waiting.record_revision,
        )

        self.assertEqual(resumed.status, TurnStatus.RUNNING)
        self.assertEqual(resumed.tool_run("call-plan").status, ToolRunStatus.DENIED)
        self.assertEqual(resumed.approvals[-1].status, ApprovalStatus.DENIED)

    def test_pending_cancel_closes_checkpoint_tool_and_turn_in_one_record(self) -> None:
        running = self._begin_running()
        self.store.append_tool_run(running.turn_id, self._tool(running))
        self.store.request_approval(running.turn_id, "call-plan")

        cancelled = self.store.cancel(
            running.turn_id,
            "user cancelled the review",
            decision_source="user",
        )

        self.assertEqual(cancelled.status, TurnStatus.CANCELLED)
        self.assertEqual(cancelled.tool_run("call-plan").status, ToolRunStatus.DENIED)
        self.assertEqual(cancelled.approvals[-1].status, ApprovalStatus.DENIED)
        self.assertEqual(cancelled.approvals[-1].decision_source, "user")

    def test_approval_revision_reader_runs_while_project_lock_is_held(self) -> None:
        running = self._begin_running()
        self.store.append_tool_run(running.turn_id, self._tool(running))
        waiting = self.store.request_approval(running.turn_id, "call-plan")
        lock_was_held = False

        def read_revision() -> int:
            nonlocal lock_was_held
            with (
                self.assertRaisesRegex(PCBDraftError, "locked by another runtime"),
                ResourceLock(self.project_root, self.locks_root, timeout=0),
            ):
                pass
            lock_was_held = True
            return 7

        approved = self.store.resolve_approval(
            running.turn_id,
            "call-plan",
            ApprovalStatus.APPROVED,
            **self._approval_binding(waiting),
            current_revision_reader=read_revision,
            decision_source="user",
        )

        self.assertTrue(lock_was_held)
        self.assertEqual(approved.status, TurnStatus.RUNNING)

    def test_direct_policy_denial_closes_an_unstarted_tool(self) -> None:
        running = self._begin_running()
        with_tool = self.store.append_tool_run(running.turn_id, self._tool(running))

        denied = self.store.update_tool_run(
            running.turn_id,
            "call-plan",
            ToolRunStatus.DENIED,
            expected_record_revision=with_tool.record_revision,
        )

        tool = denied.tool_run("call-plan")
        self.assertEqual(tool.status, ToolRunStatus.DENIED)
        self.assertIsNotNone(tool.started_at)
        self.assertIsNotNone(tool.completed_at)

    def test_interrupted_dispatched_tool_cannot_be_resumed_or_replayed(self) -> None:
        running = self._begin_running()
        with_tool = self.store.append_tool_run(running.turn_id, self._tool(running))
        executing = self.store.update_tool_run(
            running.turn_id,
            "call-plan",
            ToolRunStatus.RUNNING,
            expected_record_revision=with_tool.record_revision,
        )

        interrupted = self.store.interrupt_active(
            running.turn_id,
            "worker stopped before the tool receipt was committed",
            expected_record_revision=executing.record_revision,
        )

        self.assertEqual(interrupted.status, TurnStatus.INTERRUPTED)
        self.assertIsNotNone(interrupted.completed_at)
        self.assertEqual(
            interrupted.stop_reason,
            "worker stopped before the tool receipt was committed",
        )
        self.assertEqual(
            interrupted.tool_run("call-plan").status,
            ToolRunStatus.INTERRUPTED,
        )
        interruption_error = interrupted.tool_run("call-plan").error
        self.assertIsNotNone(interruption_error)
        assert interruption_error is not None
        self.assertIn("worker stopped", interruption_error)

        restarted = AgentTurnStore(self.project_root, self.locks_root)
        with self.assertRaisesRegex(ValidationError, "cannot be resumed"):
            restarted.resume(
                running.turn_id,
                expected_record_revision=interrupted.record_revision,
            )

    def test_resume_is_not_a_general_terminal_state_reversal(self) -> None:
        running = self._begin_running(turn_id="turn-complete")
        completed = self.store.update(
            running.turn_id,
            TurnStatus.COMPLETED,
            stop_reason="done",
        )
        with self.assertRaisesRegex(ValidationError, "can be resumed"):
            self.store.resume(completed.turn_id)
        with self.assertRaisesRegex(ValidationError, "illegal agent turn transition"):
            self.store.update(completed.turn_id, TurnStatus.RUNNING)

        queued = self.store.begin(
            project_id="board",
            thread_id="thread-main",
            turn_id="turn-cancelled",
            user_message="Cancel before start",
            baseline_revision=7,
        )
        cancelled = self.store.update(queued.turn_id, TurnStatus.CANCELLED)
        resumed = self.store.resume(cancelled.turn_id)
        self.assertEqual(resumed.status, TurnStatus.RUNNING)
        self.assertIsNone(resumed.completed_at)

    def test_unknown_fields_and_tampered_binding_are_rejected(self) -> None:
        running = self._begin_running()
        self.store.append_tool_run(running.turn_id, self._tool(running))
        self.store.request_approval(running.turn_id, "call-plan")
        path = self.project_root / "agent-turns" / "turn-test.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["unexpected"] = True
        path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaisesRegex(ValidationError, "record is malformed"):
            self.store.load("turn-test")

        del document["unexpected"]
        document["approvals"][0]["args_hash"] = "0" * 64
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "binding does not match"):
            self.store.load("turn-test")

    def test_legacy_pending_approval_migrates_fail_closed(self) -> None:
        running = self._begin_running()
        self.store.append_tool_run(running.turn_id, self._tool(running))
        self.store.request_approval(running.turn_id, "call-plan")
        path = self.project_root / "agent-turns" / "turn-test.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["version"] = 1
        del document["sequence"]
        tool = document["tool_runs"][0]
        tool["version"] = 1
        del tool["dispatch_started_at"]
        approval = document["approvals"][0]
        approval["version"] = 1
        del approval["tool_name"]
        del approval["effect"]
        del approval["risk"]
        path.write_text(json.dumps(document), encoding="utf-8")

        migrated = AgentTurnStore(self.project_root, self.locks_root).load("turn-test")

        self.assertEqual(migrated.status, TurnStatus.CANCELLED)
        self.assertEqual(migrated.tool_runs[0].status, ToolRunStatus.DENIED)
        self.assertEqual(migrated.approvals[0].status, ApprovalStatus.DENIED)
        self.assertEqual(migrated.approvals[0].decision_source, "schema_migration")
        self.assertIn("legacy approval", migrated.stop_reason or "")

    def test_legacy_completed_turn_migrates_without_losing_its_receipt(self) -> None:
        running = self._begin_running()
        with_tool = self.store.append_tool_run(running.turn_id, self._tool(running))
        executing = self.store.update_tool_run(
            running.turn_id,
            "call-plan",
            ToolRunStatus.RUNNING,
            expected_record_revision=with_tool.record_revision,
        )
        executed = self.store.update_tool_run(
            running.turn_id,
            "call-plan",
            ToolRunStatus.COMPLETED,
            after_status="awaiting_confirmation",
            after_revision=8,
            result={"status": "awaiting_confirmation"},
            expected_record_revision=executing.record_revision,
        )
        self.store.update(
            running.turn_id,
            TurnStatus.COMPLETED,
            stop_reason="done",
            expected_record_revision=executed.record_revision,
        )
        path = self.project_root / "agent-turns" / "turn-test.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["version"] = 1
        del document["sequence"]
        document["tool_runs"][0]["version"] = 1
        del document["tool_runs"][0]["dispatch_started_at"]
        path.write_text(json.dumps(document), encoding="utf-8")

        migrated = AgentTurnStore(self.project_root, self.locks_root).load("turn-test")

        self.assertEqual(migrated.status, TurnStatus.COMPLETED)
        self.assertEqual(migrated.tool_runs[0].status, ToolRunStatus.COMPLETED)
        self.assertIsNotNone(migrated.tool_runs[0].dispatch_started_at)
        self.assertEqual(
            migrated.tool_runs[0].result["status"], "awaiting_confirmation"
        )  # type: ignore[index]

    def test_illegal_transition_and_stale_record_revision_are_rejected(self) -> None:
        queued = self.store.begin(
            project_id="board",
            thread_id="thread-main",
            turn_id="turn-cas",
            user_message="Build a board",
            baseline_revision=1,
        )
        with self.assertRaisesRegex(ValidationError, "illegal agent turn transition"):
            self.store.update("turn-cas", TurnStatus.COMPLETED)

        running = self.store.update(
            "turn-cas",
            TurnStatus.RUNNING,
            expected_record_revision=queued.record_revision,
        )
        with self.assertRaisesRegex(ValidationError, "changed concurrently"):
            self.store.append_tool_run(
                "turn-cas",
                self._tool(running),
                expected_record_revision=queued.record_revision,
            )

        with_tool = self.store.append_tool_run("turn-cas", self._tool(running))
        with self.assertRaisesRegex(ValidationError, "illegal tool-run transition"):
            self.store.update_tool_run(
                "turn-cas",
                "call-plan",
                ToolRunStatus.COMPLETED,
                result={"ok": True},
                expected_record_revision=with_tool.record_revision,
            )

    def test_latest_and_list_filter_by_thread_and_status(self) -> None:
        first = self.store.begin(
            project_id="board",
            thread_id="thread-a",
            turn_id="turn-z",
            user_message="A",
            baseline_revision=0,
        )
        self.store.update(first.turn_id, TurnStatus.RUNNING)
        second = self.store.begin(
            project_id="board",
            thread_id="thread-b",
            turn_id="turn-a",
            user_message="B",
            baseline_revision=0,
        )

        self.assertEqual(self.store.latest().turn_id, second.turn_id)  # type: ignore[union-attr]
        self.assertEqual(
            self.store.latest(
                thread_id="thread-a", statuses=(TurnStatus.RUNNING,)
            ).turn_id,  # type: ignore[union-attr]
            first.turn_id,
        )
        self.assertEqual(
            [record.turn_id for record in self.store.list(statuses=("queued",))],
            [second.turn_id],
        )

    def test_begin_rejects_a_second_nonterminal_turn_in_the_same_thread(self) -> None:
        first = self.store.begin(
            project_id="board",
            thread_id="thread-main",
            turn_id="turn-first",
            user_message="First",
            baseline_revision=0,
        )
        with self.assertRaisesRegex(ValidationError, "already has"):
            self.store.begin(
                project_id="board",
                thread_id="thread-main",
                turn_id="turn-second",
                user_message="Second",
                baseline_revision=0,
            )
        self.store.cancel(first.turn_id, "cancel first")
        second = self.store.begin(
            project_id="board",
            thread_id="thread-main",
            turn_id="turn-second",
            user_message="Second",
            baseline_revision=0,
        )
        self.assertGreater(second.sequence, first.sequence)

    def test_mutation_uses_the_project_resource_lock(self) -> None:
        nonblocking_store = AgentTurnStore(
            self.project_root, self.locks_root, lock_timeout=0
        )
        with (
            ResourceLock(self.project_root, self.locks_root),
            self.assertRaisesRegex(PCBDraftError, "locked by another runtime"),
        ):
            nonblocking_store.begin(
                project_id="board",
                thread_id="thread-main",
                turn_id="turn-locked",
                user_message="Build a board",
                baseline_revision=0,
            )

    def test_wrong_project_and_path_unsafe_turn_id_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "does not match"):
            self.store.begin(
                project_id="other",
                thread_id="thread-main",
                user_message="Build a board",
                baseline_revision=0,
            )
        with self.assertRaisesRegex(ValidationError, "turn id is invalid"):
            self.store.begin(
                project_id="board",
                thread_id="thread-main",
                turn_id="../escape",
                user_message="Build a board",
                baseline_revision=0,
            )

    def test_turn_directory_symlink_is_rejected_without_chmod_or_write(self) -> None:
        outside = self.project_root.parent / "outside-turns"
        outside.mkdir(mode=0o755)
        outside.chmod(0o755)
        (self.project_root / "agent-turns").symlink_to(
            outside, target_is_directory=True
        )

        with self.assertRaisesRegex(ValidationError, "cannot be a symlink"):
            self.store.begin(
                project_id="board",
                thread_id="thread-main",
                turn_id="turn-symlink",
                user_message="Build a board",
                baseline_revision=0,
            )

        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(outside.iterdir()), [])


class AgentTurnRecordValidationTests(unittest.TestCase):
    def test_tool_arguments_hash_is_recomputed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "does not match"):
            ToolRunRecord.proposed(
                project_id="board",
                thread_id="thread",
                turn_id="turn-one",
                tool_call_id="call-one",
                tool_name="pcb_validate",
                source="runtime_policy",
                effect="evidence_write",
                risk="low",
                arguments={"unexpected": True},
                args_hash="0" * 64,
                baseline_revision=0,
                before_status="generated",
                before_revision=0,
            )

    def test_nested_records_reject_unknown_fields(self) -> None:
        arguments: dict[str, object] = {}
        tool = ToolRunRecord.proposed(
            project_id="board",
            thread_id="thread",
            turn_id="turn-one",
            tool_call_id="call-one",
            tool_name="pcb_validate",
            source="runtime_policy",
            effect="evidence_write",
            risk="low",
            arguments=arguments,
            args_hash=hash_tool_arguments(arguments),
            baseline_revision=0,
            before_status="generated",
            before_revision=0,
        ).to_dict()
        tool["extra"] = None

        with self.assertRaisesRegex(ValidationError, "tool-run record is malformed"):
            ToolRunRecord.from_dict(tool)


if __name__ == "__main__":
    unittest.main()
