from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import mcp.types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import NotificationOptions
from mcp.shared.memory import create_connected_server_and_client_session

from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
from pcbdraft.agent.turns import ToolRunStatus, TurnStatus
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.interfaces.cli import build_parser
from pcbdraft.interfaces.mcp import MCP_PROTOCOL_VERSION, ProjectMCPServer
from pcbdraft.services.application import ApplicationService


def _view(*, status: str = "draft", revision: int = 0) -> dict[str, Any]:
    return {
        "project": {
            "id": "board",
            "name": "MCP board",
            "status": status,
            "design_revision": 0,
        },
        "state": {"revision": revision, "event_sequence": 0},
        "conversation": {"messages": [], "proposal": None},
        "design": None,
        "artifacts": {"validation": None, "previews": None, "release": None},
        "attempts": [],
        "active_change": None,
    }


class MCPProjectService:
    """Small authoritative boundary used to observe MCP dispatch exactly."""

    def __init__(
        self,
        root: Path,
        *,
        status: str = "draft",
        revision: int = 0,
    ) -> None:
        self.root = root
        self._project_root = root / "board"
        self._project_root.mkdir()
        (self._project_root / "jobs").mkdir()
        self.locks_root = root / "locks"
        self.locks_root.mkdir()
        self.view = _view(status=status, revision=revision)
        self.calls: list[tuple[Any, ...]] = []

    def list_projects(self) -> list[dict[str, Any]]:
        # Recovery is exercised by JobRunner tests. These interface tests start
        # from an intentionally empty queue.
        return []

    def project_root(self, project_id: str) -> Path:
        if project_id != "board":
            raise AssertionError("unexpected project")
        return self._project_root

    def open_project(self, project_id: str) -> dict[str, Any]:
        if project_id != "board":
            raise AssertionError("unexpected project")
        return deepcopy(self.view)

    def record_progress(
        self,
        project_id: str,
        kind: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        del project_id, kind, message, level

    def send_message(
        self,
        project_id: str,
        message: str,
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        if project_id != "board":
            raise AssertionError("unexpected project")
        if expected_revision != self.view["state"]["revision"]:
            raise AssertionError("MCP call lost its revision binding")
        self.calls.append(("send_message", message, timeout, expected_revision))
        self.view = _view(
            status="awaiting_confirmation", revision=expected_revision + 1
        )
        return deepcopy(self.view)

    def confirm_project(
        self,
        project_id: str,
        *,
        validate: bool,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        if project_id != "board":
            raise AssertionError("unexpected project")
        self.calls.append(("confirm_project", validate, timeout, expected_revision))
        self.view = _view(status="generated", revision=expected_revision + 1)
        return deepcopy(self.view)


class ActiveMCPJobRunner:
    """Deterministic runner double for transport outcome-unknown races."""

    job_id = "20260815T000000Z-00000000000000000001-deadbeef"
    turn_id = "turn-mcp-outcome-unknown"

    def __init__(self, *, cancel_first_get: bool = False) -> None:
        self.cancel_first_get = cancel_first_get
        self.get_calls = 0
        self.cancel_calls = 0

    def submit_mcp_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        del project_id, tool_name, arguments, timeout
        return {
            "id": self.job_id,
            "args": {"turn_id": self.turn_id},
            "status": "running",
        }

    def get(self, project_id: str, job_id: str) -> dict[str, Any]:
        del project_id, job_id
        self.get_calls += 1
        if self.cancel_first_get and self.get_calls == 1:
            raise asyncio.CancelledError
        return {
            "id": self.job_id,
            "args": {"turn_id": self.turn_id},
            "status": "running",
        }

    def cancel(self, project_id: str, job_id: str) -> dict[str, Any]:
        del project_id, job_id
        self.cancel_calls += 1
        return {
            "id": self.job_id,
            "args": {"turn_id": self.turn_id},
            "status": "cancel_requested",
        }

    def shutdown(self) -> None:
        return None


class UnreadableMCPJobRunner(ActiveMCPJobRunner):
    def get(self, project_id: str, job_id: str) -> dict[str, Any]:
        del project_id, job_id
        self.get_calls += 1
        raise PCBDraftError("durable job record is unavailable")


class MCPInterfaceTests(unittest.TestCase):
    def test_registry_descriptors_round_trip_through_official_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary))
            bound = ProjectMCPServer(service, "board")  # type: ignore[arg-type]
            try:
                capabilities = bound.server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                )
                self.assertIsNotNone(capabilities.tools)
                assert capabilities.tools is not None
                self.assertFalse(capabilities.tools.listChanged)
                self.assertIsNone(capabilities.prompts)
                self.assertIsNone(capabilities.resources)

                expected = DEFAULT_PCB_TOOL_REGISTRY.mcp_tools()

                async def list_tools() -> list[mcp_types.Tool]:
                    async with create_connected_server_and_client_session(
                        bound.server
                    ) as session:
                        return (await session.list_tools()).tools

                actual = asyncio.run(list_tools())
                serialized = [
                    tool.model_dump(by_alias=True, exclude_none=True) for tool in actual
                ]
                self.assertEqual(serialized, expected)
            finally:
                bound.runner.shutdown()

    def test_low_risk_call_uses_durable_mcp_turn_and_one_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary))
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="review",
                timeout=12.0,
            )
            try:

                async def call_plan() -> mcp_types.CallToolResult:
                    async with create_connected_server_and_client_session(
                        bound.server
                    ) as session:
                        return await session.call_tool(
                            "pcb_plan_request", {"message": "Build a sensor board"}
                        )

                result = asyncio.run(call_plan())
                self.assertFalse(result.isError)
                receipt = result.structuredContent
                self.assertIsInstance(receipt, dict)
                assert isinstance(receipt, dict)
                self.assertEqual(receipt["status"], "completed")
                self.assertIsInstance(receipt["job_id"], str)
                self.assertEqual(receipt["job_status"], "completed")
                self.assertEqual(receipt["source"], "mcp")
                self.assertEqual(receipt["tool"], "pcb_plan_request")

                encoded = result.content[0]
                self.assertIsInstance(encoded, mcp_types.TextContent)
                assert isinstance(encoded, mcp_types.TextContent)
                self.assertEqual(json.loads(encoded.text), receipt)

                turn = bound.orchestrator.store("board").load(receipt["turn_id"])
                self.assertEqual(turn.status, TurnStatus.COMPLETED)
                self.assertEqual(turn.user_message, "/pcb_plan_request")
                self.assertEqual(len(turn.tool_runs), 1)
                tool = turn.tool_runs[0]
                self.assertEqual(tool.source, "mcp")
                self.assertEqual(tool.status, ToolRunStatus.COMPLETED)
                self.assertIsNotNone(tool.dispatch_started_at)
                self.assertEqual(tool.arguments, {"message": "Build a sensor board"})
                self.assertEqual(
                    service.calls,
                    [("send_message", "Build a sensor board", 12.0, 0)],
                )
            finally:
                bound.runner.shutdown()

    def test_review_high_risk_call_waits_for_exact_approval_without_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(
                Path(temporary), status="awaiting_confirmation", revision=3
            )
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="review",
                timeout=12.0,
            )
            try:
                result = asyncio.run(bound._call_tool("pcb_generate_candidate", {}))
                self.assertTrue(result.isError)
                receipt = result.structuredContent
                self.assertIsInstance(receipt, dict)
                assert isinstance(receipt, dict)
                self.assertEqual(receipt["status"], "approval_required")
                self.assertIn("do not retry", receipt["message"])

                turn = bound.orchestrator.store("board").load(receipt["turn_id"])
                self.assertEqual(turn.status, TurnStatus.WAITING_APPROVAL)
                self.assertEqual(len(turn.tool_runs), 1)
                tool = turn.tool_runs[0]
                self.assertEqual(tool.source, "mcp")
                self.assertEqual(tool.status, ToolRunStatus.WAITING_APPROVAL)
                self.assertIsNone(tool.dispatch_started_at)
                self.assertEqual(service.calls, [])

                checkpoint = receipt["checkpoint"]
                self.assertEqual(checkpoint["turn_id"], turn.turn_id)
                self.assertEqual(checkpoint["tool_call_id"], tool.tool_call_id)
                self.assertEqual(checkpoint["tool_name"], tool.tool_name)
                self.assertEqual(checkpoint["args_hash"], tool.args_hash)
                self.assertEqual(checkpoint["baseline_revision"], 3)
                self.assertEqual(checkpoint["effect"], "authoritative_write")
                self.assertEqual(checkpoint["risk"], "high")
            finally:
                bound.runner.shutdown()

    def test_read_only_rejects_low_risk_call_without_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary))
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="read_only",
                timeout=12.0,
            )
            try:
                result = asyncio.run(
                    bound._call_tool(
                        "pcb_plan_request", {"message": "Build a sensor board"}
                    )
                )
                self.assertTrue(result.isError)
                receipt = result.structuredContent
                self.assertIsInstance(receipt, dict)
                assert isinstance(receipt, dict)
                self.assertEqual(receipt["status"], "cancelled")
                self.assertIn("read-only mode", receipt["message"])

                turn = bound.orchestrator.store("board").load(receipt["turn_id"])
                self.assertEqual(turn.status, TurnStatus.CANCELLED)
                self.assertEqual(len(turn.tool_runs), 1)
                self.assertEqual(turn.tool_runs[0].status, ToolRunStatus.CANCELLED)
                self.assertIsNone(turn.tool_runs[0].dispatch_started_at)
                self.assertEqual(service.calls, [])
            finally:
                bound.runner.shutdown()

    def test_internal_registry_alias_is_not_an_mcp_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary), status="generated")
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="workspace",
                timeout=12.0,
            )
            try:
                result = asyncio.run(bound._call_tool("validate", {}))
                receipt = result.structuredContent
                self.assertTrue(result.isError)
                self.assertIsInstance(receipt, dict)
                assert isinstance(receipt, dict)
                self.assertEqual(receipt["status"], "rejected")
                self.assertIn("published registry", receipt["message"])
                self.assertEqual(service.calls, [])
            finally:
                bound.runner.shutdown()

    def test_denied_approval_has_denied_top_level_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(
                Path(temporary), status="awaiting_confirmation", revision=3
            )
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="review",
                timeout=12.0,
            )
            try:
                requested = asyncio.run(bound._call_tool("pcb_generate_candidate", {}))
                requested_receipt = requested.structuredContent
                self.assertIsInstance(requested_receipt, dict)
                assert isinstance(requested_receipt, dict)
                checkpoint = requested_receipt["checkpoint"]
                turn = bound.orchestrator.resolve_pending_approval(
                    "board",
                    turn_id=checkpoint["turn_id"],
                    checkpoint_id=checkpoint["checkpoint_id"],
                    tool_call_id=checkpoint["tool_call_id"],
                    tool_name=checkpoint["tool_name"],
                    effect=checkpoint["effect"],
                    risk=checkpoint["risk"],
                    args_hash=checkpoint["args_hash"],
                    baseline_revision=checkpoint["baseline_revision"],
                    approve=False,
                )

                receipt, is_error = bound._turn_receipt(
                    turn,
                    requested_tool="pcb_generate_candidate",
                    job_id=requested_receipt["job_id"],
                    job_status="completed",
                )
                self.assertTrue(is_error)
                self.assertEqual(receipt["status"], "denied")
                self.assertEqual(receipt["turn_status"], "cancelled")
                self.assertEqual(receipt["tool_status"], "denied")
                self.assertIn("rejected by user", receipt["message"])
            finally:
                bound.runner.shutdown()

    def test_dispatched_interruption_is_outcome_unknown_and_not_retryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(
                Path(temporary), status="awaiting_confirmation", revision=3
            )
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="workspace",
                timeout=12.0,
            )
            try:
                turn = bound.orchestrator.start_external_tool_turn(
                    "board", "pcb_generate_candidate", {}
                )
                store = bound.orchestrator.store("board")
                turn = store.update(turn.turn_id, TurnStatus.RUNNING)
                tool = turn.tool_runs[-1]
                store.update_tool_run(
                    turn.turn_id,
                    tool.tool_call_id,
                    ToolRunStatus.RUNNING,
                    dispatch_started=False,
                )
                store.begin_dispatch(turn.turn_id, tool.tool_call_id)
                interrupted = store.interrupt_active(
                    turn.turn_id, "worker stopped after dispatch"
                )

                receipt, is_error = bound._turn_receipt(
                    interrupted,
                    requested_tool="pcb_generate_candidate",
                    job_id=ActiveMCPJobRunner.job_id,
                    job_status="interrupted",
                )
                self.assertTrue(is_error)
                self.assertEqual(receipt["status"], "outcome_unknown")
                self.assertFalse(receipt["retry_safe"])
                self.assertEqual(
                    receipt["reconciliation"],
                    {
                        "job_id": ActiveMCPJobRunner.job_id,
                        "turn_id": turn.turn_id,
                    },
                )
                self.assertIn("Do not retry", receipt["message"])
            finally:
                bound.runner.shutdown()

    def test_failed_turn_with_interrupted_dispatched_tool_is_outcome_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(
                Path(temporary), status="awaiting_confirmation", revision=3
            )
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="workspace",
                timeout=12.0,
            )
            try:
                turn = bound.orchestrator.start_external_tool_turn(
                    "board", "pcb_generate_candidate", {}
                )
                store = bound.orchestrator.store("board")
                running = store.update(turn.turn_id, TurnStatus.RUNNING)
                tool = running.tool_runs[0]
                executing = store.update_tool_run(
                    turn.turn_id,
                    tool.tool_call_id,
                    ToolRunStatus.RUNNING,
                    dispatch_started=False,
                )
                store.begin_dispatch(
                    turn.turn_id,
                    tool.tool_call_id,
                    expected_record_revision=executing.record_revision,
                )
                with self.assertRaisesRegex(PCBDraftError, "was not replayed"):
                    bound.orchestrator.run_turn(
                        "board",
                        turn.turn_id,
                        timeout=12.0,
                        cancellation_requested=lambda: False,
                    )
                failed = store.load(turn.turn_id)
                self.assertEqual(failed.status, TurnStatus.FAILED)
                self.assertEqual(failed.tool_runs[0].status, ToolRunStatus.INTERRUPTED)

                receipt, is_error = bound._turn_receipt(
                    failed,
                    requested_tool="pcb_generate_candidate",
                    job_id=ActiveMCPJobRunner.job_id,
                    job_status="interrupted",
                )
                self.assertTrue(is_error)
                self.assertEqual(receipt["status"], "outcome_unknown")
                self.assertFalse(receipt["retry_safe"])
                self.assertIn("Do not retry", receipt["message"])
            finally:
                bound.runner.shutdown()

    def test_mismatched_durable_turn_tool_is_an_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary))
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="workspace",
                timeout=12.0,
            )
            try:
                turn = bound.orchestrator.start_external_tool_turn(
                    "board", "pcb_plan_request", {"message": "Build a sensor board"}
                )
                result = bound._job_result(
                    "pcb_validate",
                    {
                        "id": ActiveMCPJobRunner.job_id,
                        "args": {"turn_id": turn.turn_id},
                        "status": "failed",
                    },
                    expected_job_id=ActiveMCPJobRunner.job_id,
                    expected_turn_id=turn.turn_id,
                )
                receipt = result.structuredContent
                self.assertTrue(result.isError)
                self.assertIsInstance(receipt, dict)
                assert isinstance(receipt, dict)
                self.assertEqual(receipt["status"], "outcome_unknown")
                self.assertFalse(receipt["retry_safe"])
            finally:
                bound.runner.shutdown()

    def test_pre_dispatch_interruption_has_honest_interrupted_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary), revision=3)
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="workspace",
                timeout=12.0,
            )
            try:
                turn = bound.orchestrator.start_external_tool_turn(
                    "board", "pcb_plan_request", {"message": "Build a sensor board"}
                )
                store = bound.orchestrator.store("board")
                store.update(turn.turn_id, TurnStatus.RUNNING)
                interrupted = store.interrupt_active(
                    turn.turn_id, "worker stopped before dispatch"
                )

                receipt, is_error = bound._turn_receipt(
                    interrupted,
                    requested_tool="pcb_plan_request",
                    job_id=ActiveMCPJobRunner.job_id,
                    job_status="interrupted",
                )
                self.assertTrue(is_error)
                self.assertEqual(receipt["status"], "interrupted")
                self.assertEqual(receipt["turn_status"], "interrupted")
                self.assertEqual(receipt["tool_status"], "cancelled")
                self.assertNotIn("retry_safe", receipt)
                self.assertIn("before dispatch", receipt["message"])
            finally:
                bound.runner.shutdown()

    def test_timeout_returns_unknown_outcome_with_durable_reconciliation_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary))
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="workspace",
                timeout=12.0,
            )
            bound.runner.shutdown()
            runner = ActiveMCPJobRunner()
            bound.runner = runner  # type: ignore[assignment]
            with patch("pcbdraft.interfaces.mcp.MCP_JOB_COMPLETION_GRACE", -12.0):
                result = asyncio.run(
                    bound._call_tool(
                        "pcb_plan_request", {"message": "Build a sensor board"}
                    )
                )

            self.assertTrue(result.isError)
            receipt = result.structuredContent
            self.assertIsInstance(receipt, dict)
            assert isinstance(receipt, dict)
            self.assertEqual(receipt["status"], "outcome_unknown")
            self.assertEqual(receipt["job_id"], runner.job_id)
            self.assertEqual(receipt["turn_id"], runner.turn_id)
            self.assertEqual(receipt["job_status"], "cancel_requested")
            self.assertFalse(receipt["retry_safe"])
            self.assertEqual(
                receipt["reconciliation"],
                {"job_id": runner.job_id, "turn_id": runner.turn_id},
            )
            self.assertIn("Do not retry", receipt["message"])
            self.assertEqual(runner.cancel_calls, 1)

    def test_cancelled_request_returns_unknown_outcome_instead_of_rethrowing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary))
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="workspace",
                timeout=12.0,
            )
            bound.runner.shutdown()
            runner = ActiveMCPJobRunner(cancel_first_get=True)
            bound.runner = runner  # type: ignore[assignment]

            result = asyncio.run(
                bound._call_tool(
                    "pcb_plan_request", {"message": "Build a sensor board"}
                )
            )

            self.assertTrue(result.isError)
            receipt = result.structuredContent
            self.assertIsInstance(receipt, dict)
            assert isinstance(receipt, dict)
            self.assertEqual(receipt["status"], "outcome_unknown")
            self.assertEqual(receipt["job_id"], runner.job_id)
            self.assertEqual(receipt["turn_id"], runner.turn_id)
            self.assertEqual(receipt["job_status"], "cancel_requested")
            self.assertFalse(receipt["retry_safe"])
            self.assertIn("Do not retry", receipt["message"])
            self.assertEqual(runner.cancel_calls, 1)

    def test_post_submit_job_read_failure_retains_reconciliation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MCPProjectService(Path(temporary))
            bound = ProjectMCPServer(
                service,  # type: ignore[arg-type]
                "board",
                permission_mode="workspace",
                timeout=12.0,
            )
            bound.runner.shutdown()
            runner = UnreadableMCPJobRunner()
            bound.runner = runner  # type: ignore[assignment]

            result = asyncio.run(
                bound._call_tool(
                    "pcb_plan_request", {"message": "Build a sensor board"}
                )
            )
            receipt = result.structuredContent
            self.assertTrue(result.isError)
            self.assertIsInstance(receipt, dict)
            assert isinstance(receipt, dict)
            self.assertEqual(receipt["status"], "outcome_unknown")
            self.assertEqual(receipt["job_id"], runner.job_id)
            self.assertEqual(receipt["turn_id"], runner.turn_id)
            self.assertFalse(receipt["retry_safe"])
            self.assertIn("Do not retry", receipt["message"])

    def test_cli_parser_requires_project_and_uses_restricted_defaults(self) -> None:
        parser = build_parser(prog="pcbdraft")
        args = parser.parse_args(["mcp", "--project", "board"])

        self.assertEqual(args.command, "mcp")
        self.assertEqual(args.mcp_project_id, "board")
        self.assertIsNone(args.mcp_workspace)
        self.assertEqual(args.mcp_provider, "auto")
        self.assertEqual(args.mcp_approval_mode, "review")
        self.assertEqual(args.mcp_timeout, 420.0)

        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(["mcp"])

    def test_subprocess_stdio_handshake_and_tool_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            service = ApplicationService(workspace, provider_name="builtin")
            draft = service.create_draft("MCP handshake board")
            project_id = draft["project"]["id"]
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:

                async def handshake() -> tuple[mcp_types.InitializeResult, list[str]]:
                    parameters = StdioServerParameters(
                        command=sys.executable,
                        args=[
                            "-m",
                            "pcbdraft",
                            "mcp",
                            "--workspace",
                            str(workspace),
                            "--provider",
                            "builtin",
                            "--project",
                            project_id,
                        ],
                        cwd=Path(__file__).resolve().parents[2],
                    )
                    async with (
                        stdio_client(parameters, errlog=stderr) as streams,
                        ClientSession(streams[0], streams[1]) as session,
                    ):
                        initialized = await session.initialize()
                        listed = await session.list_tools()
                        return initialized, [tool.name for tool in listed.tools]

                initialized, names = asyncio.run(
                    asyncio.wait_for(handshake(), timeout=20.0)
                )
                self.assertEqual(initialized.protocolVersion, MCP_PROTOCOL_VERSION)
                self.assertIsNotNone(initialized.capabilities.tools)
                self.assertEqual(
                    names,
                    [spec.external_name for spec in DEFAULT_PCB_TOOL_REGISTRY.specs],
                )
                stderr.seek(0)
                self.assertNotIn("Failed to parse JSONRPC", stderr.read())


if __name__ == "__main__":
    unittest.main()
