"""Project-bound MCP 2025-11-25 stdio adapter for PCBDraft tools."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
import mcp.types as mcp_types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from pcbdraft import __version__
from pcbdraft.agent.orchestrator import AgentOrchestrator
from pcbdraft.agent.permissions import PermissionBroker, PermissionMode
from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY, PCBToolRegistry
from pcbdraft.agent.turns import ToolRunStatus, TurnRecord, TurnStatus
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.services.application import ApplicationService, sanitize_user_text
from pcbdraft.services.jobs import JobRunner

MCP_PROTOCOL_VERSION = "2025-11-25"
MAX_MCP_CALLS = 1_000
MCP_JOB_COMPLETION_GRACE = 5.0
_ACTIVE_JOBS = frozenset({"queued", "running", "cancel_requested"})


class ProjectMCPServer:
    """Expose one project's closed PCB registry over official MCP stdio."""

    def __init__(
        self,
        service: ApplicationService,
        project_id: str,
        *,
        permission_mode: PermissionMode = "review",
        timeout: float = 420.0,
        registry: PCBToolRegistry = DEFAULT_PCB_TOOL_REGISTRY,
    ) -> None:
        if isinstance(timeout, bool) or not 0 < timeout <= 1_800:
            raise ValidationError("MCP tool timeout must be in (0, 1800] seconds")
        # Resolve the scope before stdout becomes a protocol-only stream.
        service.open_project(project_id)
        self.service = service
        self.project_id = project_id
        self.timeout = float(timeout)
        self.registry = registry
        self.orchestrator = AgentOrchestrator(
            service,
            registry=registry,
            permissions=PermissionBroker(permission_mode),
        )
        self.runner = JobRunner(
            service,
            workers=1,
            orchestrator=self.orchestrator,
        )
        self._calls = 0
        self.server = Server(
            "pcbdraft",
            version=__version__,
            instructions=(
                f"Bound to local PCBDraft project {project_id}. Tools never accept "
                "paths or project identities; local revision and permission policy "
                "remain authoritative. Approval-required calls are not executed."
            ),
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[mcp_types.Tool]:
            return [
                mcp_types.Tool.model_validate(descriptor)
                for descriptor in self.registry.mcp_tools()
            ]

        @self.server.call_tool(validate_input=True)
        async def call_tool(
            name: str, arguments: dict[str, Any]
        ) -> mcp_types.CallToolResult:
            return await self._call_tool(name, arguments)

    async def _call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> mcp_types.CallToolResult:
        self._calls += 1
        if self._calls > MAX_MCP_CALLS:
            return self._result(
                {
                    "status": "server_call_limit",
                    "project_id": self.project_id,
                    "tool": name[:64],
                    "message": "MCP process reached its bounded tool-call limit",
                },
                is_error=True,
            )
        try:
            spec = self.registry.resolve(name)
            if name != spec.external_name:
                raise ValidationError("MCP tool name is not in the published registry")
            normalized = self.registry.normalize_arguments(name, arguments)
            job = self.runner.submit_mcp_tool(
                self.project_id,
                spec.external_name,
                normalized,
                timeout=self.timeout,
            )
        except PCBDraftError as exc:
            return self._error_result(name, exc)

        job_id = str(job["id"])
        turn_id = self._job_turn_id(job)
        deadline = time.monotonic() + self.timeout + MCP_JOB_COMPLETION_GRACE
        current: Mapping[str, Any]
        try:
            while True:
                try:
                    current = self.runner.get(self.project_id, job_id)
                except PCBDraftError as exc:
                    return self._unknown_outcome_result(
                        spec.external_name,
                        job_id=job_id,
                        turn_id=turn_id,
                        job_status="unavailable",
                        reason=(
                            "the durable MCP job could not be read after submission: "
                            f"{sanitize_user_text(str(exc))[:1024]}"
                        ),
                    )
                if current.get("status") not in _ACTIVE_JOBS:
                    break
                if time.monotonic() >= deadline:
                    current = self._cancel_or_refresh_job(job_id, current)
                    if current.get("status") not in _ACTIVE_JOBS:
                        return self._job_result(
                            spec.external_name,
                            current,
                            expected_job_id=job_id,
                            expected_turn_id=turn_id,
                        )
                    return self._unknown_outcome_result(
                        spec.external_name,
                        job_id=job_id,
                        turn_id=turn_id,
                        job_status=current.get("status"),
                        reason="the MCP timeout elapsed",
                    )
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            try:
                current = self.runner.get(self.project_id, job_id)
            except PCBDraftError:
                current = {"status": "unavailable"}
            if current.get("status") in _ACTIVE_JOBS:
                current = self._cancel_or_refresh_job(job_id, current)
            if (
                current.get("status") not in _ACTIVE_JOBS
                and current.get("status") != "unavailable"
            ):
                return self._job_result(
                    spec.external_name,
                    current,
                    expected_job_id=job_id,
                    expected_turn_id=turn_id,
                )
            return self._unknown_outcome_result(
                spec.external_name,
                job_id=job_id,
                turn_id=turn_id,
                job_status=current.get("status"),
                reason="the MCP request was cancelled",
            )

        return self._job_result(
            spec.external_name,
            current,
            expected_job_id=job_id,
            expected_turn_id=turn_id,
        )

    def _job_result(
        self,
        tool_name: str,
        job: Mapping[str, Any],
        *,
        expected_job_id: str,
        expected_turn_id: str | None,
    ) -> mcp_types.CallToolResult:
        persisted_job_id = job.get("id")
        persisted_turn_id = self._job_turn_id(job)
        if (
            persisted_job_id != expected_job_id
            or expected_turn_id is None
            or persisted_turn_id != expected_turn_id
        ):
            return self._unknown_outcome_result(
                tool_name,
                job_id=expected_job_id,
                turn_id=expected_turn_id,
                job_status=job.get("status"),
                reason="the durable MCP job lost its exact job/turn binding",
            )
        try:
            turn = self.orchestrator.store(self.project_id).load(expected_turn_id)
            receipt, is_error = self._turn_receipt(
                turn,
                requested_tool=tool_name,
                job_id=expected_job_id,
                job_status=self._safe_status(job.get("status")),
            )
        except PCBDraftError as exc:
            return self._unknown_outcome_result(
                tool_name,
                job_id=expected_job_id,
                turn_id=expected_turn_id,
                job_status=job.get("status"),
                reason=(
                    "the durable MCP turn could not be reconciled after submission: "
                    f"{sanitize_user_text(str(exc))[:1024]}"
                ),
            )
        return self._result(receipt, is_error=is_error)

    def _turn_receipt(
        self,
        turn: TurnRecord,
        *,
        requested_tool: str,
        job_id: str,
        job_status: str,
    ) -> tuple[dict[str, Any], bool]:
        if len(turn.tool_runs) != 1:
            raise ValidationError("MCP turn must contain exactly one durable tool call")
        tool = turn.tool_runs[0]
        if tool.source != "mcp" or tool.tool_name != requested_tool:
            raise ValidationError("MCP turn tool binding does not match the request")
        receipt: dict[str, Any] = {
            "project_id": self.project_id,
            "job_id": job_id,
            "job_status": job_status,
            "turn_id": turn.turn_id,
            "tool_call_id": tool.tool_call_id,
            "tool": tool.tool_name,
            "source": tool.source,
            "turn_status": turn.status.value,
            "tool_status": tool.status.value,
            "before_status": tool.before_status,
            "before_revision": tool.before_revision,
            "after_status": tool.after_status,
            "after_revision": tool.after_revision,
            "result": dict(tool.result) if tool.result is not None else None,
        }
        if turn.status is TurnStatus.WAITING_APPROVAL:
            receipt.update(
                {
                    "status": "approval_required",
                    "checkpoint": self.orchestrator.approval_payload(turn),
                    "message": (
                        "not executed; approve this exact checkpoint in the "
                        "PCBDraft TUI and do not retry the MCP call"
                    ),
                }
            )
            return receipt, True
        if (
            turn.status is TurnStatus.COMPLETED
            and tool.status is ToolRunStatus.COMPLETED
        ):
            receipt.update(
                {
                    "status": "completed",
                    "message": "PCB tool completed through the durable local gateway",
                }
            )
            return receipt, False
        if tool.status is ToolRunStatus.DENIED:
            receipt.update(
                {
                    "status": "denied",
                    "message": sanitize_user_text(
                        turn.stop_reason or "PCB tool approval was denied"
                    )[:2048],
                }
            )
            return receipt, True
        if turn.status is TurnStatus.CANCELLED:
            receipt.update(
                {
                    "status": "cancelled",
                    "message": sanitize_user_text(
                        turn.stop_reason or "PCB tool was cancelled before completion"
                    )[:2048],
                }
            )
            return receipt, True
        if tool.dispatch_started_at is not None and tool.status in {
            ToolRunStatus.FAILED,
            ToolRunStatus.INTERRUPTED,
        }:
            receipt.update(
                self._unknown_outcome_fields(
                    job_id=job_id,
                    turn_id=turn.turn_id,
                    reason="the durable PCB tool stopped after dispatch",
                )
            )
            return receipt, True
        if turn.status is TurnStatus.INTERRUPTED:
            receipt.update(
                {
                    "status": "interrupted",
                    "message": sanitize_user_text(
                        turn.error
                        or turn.stop_reason
                        or "PCB tool was interrupted before dispatch"
                    )[:2048],
                }
            )
            return receipt, True
        if turn.status in {
            TurnStatus.QUEUED,
            TurnStatus.RUNNING,
            TurnStatus.WAITING_APPROVAL,
        }:
            receipt.update(
                self._unknown_outcome_fields(
                    job_id=job_id,
                    turn_id=turn.turn_id,
                    reason="the durable job stopped reporting before the turn reached a terminal state",
                )
            )
            return receipt, True
        receipt.update(
            {
                "status": "failed",
                "message": sanitize_user_text(
                    turn.error or tool.error or turn.stop_reason or "PCB tool failed"
                )[:2048],
            }
        )
        return receipt, True

    def _cancel_or_refresh_job(
        self, job_id: str, current: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        try:
            return self.runner.cancel(self.project_id, job_id)
        except PCBDraftError:
            try:
                return self.runner.get(self.project_id, job_id)
            except PCBDraftError:
                return current

    def _unknown_outcome_result(
        self,
        tool_name: str,
        *,
        job_id: str,
        turn_id: str | None,
        job_status: object,
        reason: str,
    ) -> mcp_types.CallToolResult:
        receipt: dict[str, Any] = {
            "project_id": self.project_id,
            "job_id": job_id,
            "job_status": self._safe_status(job_status),
            "tool": tool_name,
            "source": "mcp",
        }
        if turn_id is not None:
            receipt["turn_id"] = turn_id
            try:
                turn = self.orchestrator.store(self.project_id).load(turn_id)
            except PCBDraftError:
                pass
            else:
                receipt["turn_status"] = turn.status.value
                if turn.tool_runs:
                    tool = turn.tool_runs[-1]
                    receipt["tool_call_id"] = tool.tool_call_id
                    receipt["tool_status"] = tool.status.value
                    receipt["dispatch_started"] = tool.dispatch_started_at is not None
        receipt.update(
            self._unknown_outcome_fields(
                job_id=job_id,
                turn_id=turn_id,
                reason=reason,
            )
        )
        return self._result(receipt, is_error=True)

    @staticmethod
    def _unknown_outcome_fields(
        *, job_id: str, turn_id: str | None, reason: str
    ) -> dict[str, Any]:
        identity = f"job {job_id}"
        if turn_id is not None:
            identity += f" and turn {turn_id}"
        return {
            "status": "outcome_unknown",
            "retry_safe": False,
            "reconciliation": {
                "job_id": job_id,
                "turn_id": turn_id,
            },
            "message": (
                f"{reason}; the local PCB effect may still complete. Do not retry "
                f"this MCP call. Reconcile the durable {identity} before taking "
                "another action."
            ),
        }

    @staticmethod
    def _job_turn_id(job: Mapping[str, Any]) -> str | None:
        arguments = job.get("args")
        turn_id = arguments.get("turn_id") if isinstance(arguments, Mapping) else None
        return turn_id if isinstance(turn_id, str) else None

    @staticmethod
    def _safe_status(status: object) -> str:
        return status if isinstance(status, str) and status else "unavailable"

    @staticmethod
    def _result(receipt: dict[str, Any], *, is_error: bool) -> mcp_types.CallToolResult:
        encoded = json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=encoded)],
            structuredContent=receipt,
            isError=is_error,
        )

    def _error_result(
        self, tool_name: str, exc: PCBDraftError
    ) -> mcp_types.CallToolResult:
        return self._result(
            {
                "status": "rejected",
                "project_id": self.project_id,
                "tool": tool_name[:64],
                "message": sanitize_user_text(str(exc))[:2048],
            },
            is_error=True,
        )

    async def run(self) -> None:
        """Run the SDK lifecycle until the client closes stdin."""

        capabilities = self.server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        )
        initialization = InitializationOptions(
            server_name="pcbdraft",
            server_version=__version__,
            capabilities=capabilities,
            instructions=self.server.instructions,
        )
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self.server.run(
                    read_stream,
                    write_stream,
                    initialization,
                    raise_exceptions=False,
                )
        finally:
            self.runner.shutdown()


def run_mcp_stdio(
    *,
    workspace: str | Path | None,
    provider: str,
    project_id: str,
    approval_mode: PermissionMode = "review",
    timeout: float = 420.0,
) -> int:
    """Validate fixed scope, then hand stdin/stdout exclusively to MCP."""

    if workspace is not None and not Path(workspace).is_absolute():
        raise ValidationError("MCP --workspace must be an absolute path")
    service = ApplicationService(workspace, provider_name=provider)
    bound = ProjectMCPServer(
        service,
        project_id,
        permission_mode=approval_mode,
        timeout=timeout,
    )
    anyio.run(bound.run)
    return 0
