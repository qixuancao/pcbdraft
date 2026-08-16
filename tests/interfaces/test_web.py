from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from pcbdraft.agent.turns import (
    AgentTurnStore,
    ToolRunRecord,
    ToolRunStatus,
    TurnStatus,
    hash_tool_arguments,
)
from pcbdraft.interfaces.web import PCBDraftHTTPServer


class WebProjectService:
    """Small stateful service boundary for the loopback HTTP projection tests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._project_root = root / "board"
        self._project_root.mkdir()
        (self._project_root / "jobs").mkdir()
        self.locks_root = root / "locks"
        self.locks_root.mkdir()
        self.snapshot_reads = 0
        self.snapshot_sequence: list[dict[str, Any] | None] = []
        self.view = {
            "project": {
                "id": "board",
                "name": "Browser board",
                "status": "awaiting_confirmation",
                "design_revision": 0,
            },
            "state": {"revision": 4, "event_sequence": 0},
            "conversation": {"messages": [], "proposal": None},
            "design": None,
            "artifacts": {"validation": None, "previews": None, "release": None},
            "attempts": [],
            "active_change": None,
        }

    def list_projects(self) -> list[dict[str, Any]]:
        # JobRunner recovery is intentionally empty; the project is opened by id.
        return []

    def project_root(self, project_id: str) -> Path:
        if project_id != "board":
            raise AssertionError("unexpected project")
        return self._project_root

    def open_project(self, project_id: str) -> dict[str, Any]:
        if project_id != "board":
            raise AssertionError("unexpected project")
        return deepcopy(self.view)

    def try_open_project_snapshot(
        self, project_id: str, *, timeout: float = 0.0
    ) -> dict[str, Any] | None:
        if timeout < 0:
            raise AssertionError("unexpected snapshot timeout")
        self.snapshot_reads += 1
        if self.snapshot_sequence:
            value = self.snapshot_sequence.pop(0)
            return deepcopy(value) if value is not None else None
        return self.open_project(project_id)


class PCBDraftWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = WebProjectService(Path(self.temporary.name))
        self._create_durable_turns()
        self.server = PCBDraftHTTPServer(
            ("127.0.0.1", 0),
            self.service,  # type: ignore[arg-type]
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def _create_durable_turns(self) -> None:
        store = AgentTurnStore(
            self.service.project_root("board"), self.service.locks_root
        )
        queued_plan = store.begin(
            project_id="board",
            thread_id="main",
            turn_id="turn-plan",
            user_message="Plan a small sensor board",
            baseline_revision=3,
        )
        running_plan = store.update(
            queued_plan.turn_id,
            TurnStatus.RUNNING,
            expected_record_revision=queued_plan.record_revision,
        )
        plan_arguments = {"message": "Plan a small sensor board"}
        plan_tool = ToolRunRecord.proposed(
            project_id="board",
            thread_id="main",
            turn_id=running_plan.turn_id,
            tool_call_id="call-plan",
            tool_name="pcb_plan_request",
            source="runtime_policy",
            effect="conversation_write",
            risk="low",
            arguments=plan_arguments,
            args_hash=hash_tool_arguments(plan_arguments),
            baseline_revision=3,
            before_status="draft",
            before_revision=3,
        )
        with_plan = store.append_tool_run(
            running_plan.turn_id,
            plan_tool,
            expected_record_revision=running_plan.record_revision,
        )
        executing_plan = store.update_tool_run(
            running_plan.turn_id,
            plan_tool.tool_call_id,
            ToolRunStatus.RUNNING,
            expected_record_revision=with_plan.record_revision,
        )
        executed_plan = store.update_tool_run(
            running_plan.turn_id,
            plan_tool.tool_call_id,
            ToolRunStatus.COMPLETED,
            after_status="awaiting_confirmation",
            after_revision=4,
            result={
                "tool": "pcb_plan_request",
                "after_status": "awaiting_confirmation",
                "after_revision": 4,
            },
            expected_record_revision=executing_plan.record_revision,
        )
        store.update(
            running_plan.turn_id,
            TurnStatus.COMPLETED,
            stop_reason="agent_finished",
            expected_record_revision=executed_plan.record_revision,
        )

        queued = store.begin(
            project_id="board",
            thread_id="main",
            turn_id="turn-browser",
            user_message="Build a small sensor board",
            baseline_revision=4,
        )
        running = store.update(
            queued.turn_id,
            TurnStatus.RUNNING,
            expected_record_revision=queued.record_revision,
        )
        arguments: dict[str, Any] = {}
        proposed = ToolRunRecord.proposed(
            project_id="board",
            thread_id="main",
            turn_id=running.turn_id,
            tool_call_id="call-generate",
            tool_name="pcb_generate_candidate",
            source="runtime_policy",
            effect="authoritative_write",
            risk="high",
            arguments=arguments,
            args_hash=hash_tool_arguments(arguments),
            baseline_revision=4,
            before_status="awaiting_confirmation",
            before_revision=4,
        )
        with_tool = store.append_tool_run(
            running.turn_id,
            proposed,
            expected_record_revision=running.record_revision,
        )
        store.request_approval(
            running.turn_id,
            proposed.tool_call_id,
            expected_record_revision=with_tool.record_revision,
        )

    def _get_project(self, *, session: str | None) -> tuple[int, dict[str, Any]]:
        host, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"X-PCBDraft-Session": session} if session is not None else {}
        connection.request("GET", "/api/projects/board", headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def test_project_api_requires_the_per_process_session_capability(self) -> None:
        status, body = self._get_project(session=None)

        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["message"], "invalid local session token")

    def test_project_api_exposes_bounded_durable_tool_activity_and_approval(
        self,
    ) -> None:
        status, body = self._get_project(session=self.server.session_token)

        self.assertEqual(status, 200)
        agent = body["agent"]
        self.assertEqual(agent["schema"], "pcbdraft-browser-agent-view")
        self.assertEqual(agent["version"], 1)
        self.assertEqual(agent["permission_mode"], "workspace")
        self.assertEqual(agent["call_producer"], "local-policy")
        self.assertEqual(agent["turn_order"], "oldest_first")
        self.assertEqual(len(agent["turns"]), 2)
        self.assertEqual(
            [turn["turn_id"] for turn in agent["turns"]],
            ["turn-plan", "turn-browser"],
        )
        completed = agent["turns"][0]
        self.assertLess(completed["sequence"], agent["turns"][1]["sequence"])
        self.assertEqual(
            completed["tool_runs"][0]["arguments"],
            {"message": "Plan a small sensor board"},
        )
        self.assertEqual(completed["tool_runs"][0]["result"]["after_revision"], 4)
        turn = agent["turns"][1]
        self.assertEqual(turn["turn_id"], "turn-browser")
        self.assertEqual(turn["status"], "waiting_approval")
        self.assertEqual(turn["tool_runs"][0]["tool_name"], "pcb_generate_candidate")
        self.assertEqual(turn["tool_runs"][0]["status"], "waiting_approval")
        approval = agent["pending_approval"]
        self.assertEqual(approval["tool_call_id"], "call-generate")
        self.assertEqual(approval["tool_name"], "pcb_generate_candidate")
        self.assertEqual(approval["effect"], "authoritative_write")
        self.assertEqual(approval["risk"], "high")
        self.assertEqual(approval["arguments"], {})

    def test_project_api_retries_when_revision_crosses_projection_window(
        self,
    ) -> None:
        before = deepcopy(self.service.view)
        after = deepcopy(self.service.view)
        after["state"]["revision"] = 5
        after["project"]["design_revision"] = 1
        self.service.snapshot_sequence = [before, after, after, after]

        status, body = self._get_project(session=self.server.session_token)

        self.assertEqual(status, 200)
        self.assertEqual(self.service.snapshot_reads, 4)
        self.assertEqual(body["state"]["revision"], 5)
        self.assertEqual(body["project"]["design_revision"], 1)
        self.assertEqual(body["agent"]["turns"][-1]["baseline_revision"], 4)

    def test_project_api_retries_when_tool_receipt_is_newer_than_snapshot(
        self,
    ) -> None:
        stale = deepcopy(self.service.view)
        stale["state"]["revision"] = 3
        current = deepcopy(self.service.view)
        self.service.snapshot_sequence = [stale, stale, current, current]

        status, body = self._get_project(session=self.server.session_token)

        self.assertEqual(status, 200)
        self.assertEqual(self.service.snapshot_reads, 4)
        self.assertEqual(body["state"]["revision"], 4)
        self.assertEqual(body["agent"]["turns"][0]["tool_runs"][0]["after_revision"], 4)

    def test_browser_assets_use_inline_tool_activity_not_a_confirm_wizard(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        script = (repository / "src/pcbdraft/web/app.js").read_text(encoding="utf-8")
        document = (repository / "src/pcbdraft/web/index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("renderAgentActivity", script)
        self.assertIn("pcb_generate_candidate", script)
        self.assertIn("JSON.stringify(tool.result", script)
        self.assertIn("state revision ${view.state.revision}", script)
        self.assertIn("design revision ${view.project.design_revision}", script)
        self.assertIn("state revision ${tool.baseline_revision}", script)
        self.assertNotIn("· revision ${view.project.design_revision}", script)
        self.assertIn("value.textContent = String(text)", script)
        self.assertNotIn("innerHTML", script)
        self.assertIn('id="agent-activity"', document)
        self.assertNotIn("renderConfirmation", script)
        self.assertNotIn("Generate & check", script)
        self.assertNotIn('id="confirmation"', document)
        self.assertNotIn("waits for confirmation", document)


if __name__ == "__main__":
    unittest.main()
