"""Durable, strictly validated records for agent turns and PCB tool calls.

This module is deliberately independent of the model, MCP transport, job runner,
and user interfaces.  It is the durable protocol boundary between a producer
that proposes tool calls and an executor that may later run them.  Every update
is serialized under the project lock and atomically replaces one versioned turn
document, so a pending approval survives process termination without replaying a
tool side effect.
"""

from __future__ import annotations

import builtins
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import (
    atomic_write_json,
    load_json_limited,
    make_directory,
)
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.runs import new_run_id, utc_timestamp

TURN_SCHEMA = "pcbdraft-agent-turn"
TURN_VERSION = 2
TOOL_RUN_SCHEMA = "pcbdraft-agent-tool-run"
TOOL_RUN_VERSION = 2
APPROVAL_SCHEMA = "pcbdraft-agent-approval"
APPROVAL_VERSION = 2
TURN_INDEX_SCHEMA = "pcbdraft-agent-turn-index"
TURN_INDEX_VERSION = 1

TURN_FILE_LIMIT = 4 * 1024 * 1024
MAX_TURNS = 2_000
MAX_TOOL_RUNS = 256
MAX_APPROVALS = 256
MAX_ARGUMENT_BYTES = 256 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_MESSAGE_BYTES = 16 * 1024

_PATH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_HASH = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_TOOL_SOURCES = frozenset({"runtime_policy", "model", "mcp", "user"})
_TOOL_EFFECTS = frozenset(
    {
        "conversation_write",
        "candidate_write",
        "evidence_write",
        "staged_write",
        "authoritative_write",
    }
)
_TOOL_RISKS = frozenset({"low", "medium", "high"})


class TurnStatus(str, Enum):
    """Lifecycle of one durable user-to-agent turn."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class ToolRunStatus(str, Enum):
    """Lifecycle of one proposed tool call within a turn."""

    PROPOSED = "proposed"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    DENIED = "denied"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    """Decision recorded for a high-authority tool call."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


_TURN_TRANSITIONS: dict[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.QUEUED: frozenset(
        {
            TurnStatus.RUNNING,
            TurnStatus.INTERRUPTED,
            TurnStatus.CANCELLED,
        }
    ),
    TurnStatus.RUNNING: frozenset(
        {
            TurnStatus.WAITING_APPROVAL,
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.INTERRUPTED,
            TurnStatus.CANCELLED,
        }
    ),
    TurnStatus.WAITING_APPROVAL: frozenset(
        {
            TurnStatus.RUNNING,
            TurnStatus.INTERRUPTED,
            TurnStatus.CANCELLED,
        }
    ),
    TurnStatus.COMPLETED: frozenset(),
    TurnStatus.FAILED: frozenset(),
    TurnStatus.INTERRUPTED: frozenset(),
    TurnStatus.CANCELLED: frozenset(),
}

_TOOL_TRANSITIONS: dict[ToolRunStatus, frozenset[ToolRunStatus]] = {
    ToolRunStatus.PROPOSED: frozenset(
        {
            ToolRunStatus.WAITING_APPROVAL,
            ToolRunStatus.RUNNING,
            ToolRunStatus.DENIED,
            ToolRunStatus.CANCELLED,
        }
    ),
    ToolRunStatus.WAITING_APPROVAL: frozenset(
        {
            ToolRunStatus.RUNNING,
            ToolRunStatus.DENIED,
            ToolRunStatus.CANCELLED,
        }
    ),
    ToolRunStatus.RUNNING: frozenset(
        {
            ToolRunStatus.COMPLETED,
            ToolRunStatus.FAILED,
            ToolRunStatus.INTERRUPTED,
            ToolRunStatus.CANCELLED,
        }
    ),
    ToolRunStatus.COMPLETED: frozenset(),
    ToolRunStatus.FAILED: frozenset(),
    ToolRunStatus.INTERRUPTED: frozenset(),
    ToolRunStatus.DENIED: frozenset(),
    ToolRunStatus.CANCELLED: frozenset(),
}

_TERMINAL_TURNS = frozenset(
    {
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.INTERRUPTED,
        TurnStatus.CANCELLED,
    }
)
_TERMINAL_TOOLS = frozenset(
    {
        ToolRunStatus.COMPLETED,
        ToolRunStatus.FAILED,
        ToolRunStatus.INTERRUPTED,
        ToolRunStatus.DENIED,
        ToolRunStatus.CANCELLED,
    }
)


def hash_tool_arguments(arguments: Mapping[str, Any]) -> str:
    """Return the canonical hash used to bind a call to an approval."""

    canonical, _ = _canonical_json_object(
        arguments, "tool arguments", limit=MAX_ARGUMENT_BYTES
    )
    import hashlib

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json_object(
    value: Mapping[str, Any] | object,
    label: str,
    *,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{label} must be a JSON object")
    try:
        canonical = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        copied = json.loads(canonical)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"{label} must contain only JSON values") from exc
    if len(canonical.encode("utf-8")) > limit:
        raise ValidationError(f"{label} exceeds its {limit}-byte limit")
    if not isinstance(copied, dict):  # defensive: Mapping above guarantees this
        raise ValidationError(f"{label} must be a JSON object")
    return canonical, copied


def _validate_identity(value: object, label: str, *, path_safe: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError(f"{label} is invalid")
    if path_safe and _PATH_ID.fullmatch(value) is None:
        raise ValidationError(f"{label} is invalid")
    return value


def _validate_revision(value: object, label: str = "baseline revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _validate_timestamp(value: object, label: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a UTC timestamp")


def _validate_optional_text(
    value: object, label: str, *, required: bool = False, limit: int = 4096
) -> None:
    if value is None and not required:
        return
    if (
        not isinstance(value, str)
        or (required and not value.strip())
        or len(value.encode("utf-8")) > limit
        or "\x00" in value
    ):
        raise ValidationError(f"{label} is invalid")


def _enum_value(enum_type: type[Enum], value: object, label: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} is invalid") from exc


@dataclass(frozen=True)
class ToolRunRecord:
    """One durable proposed/executed tool call and its bounded receipt."""

    project_id: str
    thread_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    source: str
    effect: str
    risk: str
    arguments: Mapping[str, Any]
    args_hash: str
    baseline_revision: int
    before_status: str
    before_revision: int
    status: ToolRunStatus
    created_at: str
    started_at: str | None = None
    dispatch_started_at: str | None = None
    completed_at: str | None = None
    after_status: str | None = None
    after_revision: int | None = None
    result: Mapping[str, Any] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.project_id, "tool-run project id")
        _validate_identity(self.thread_id, "tool-run thread id")
        _validate_identity(self.turn_id, "tool-run turn id", path_safe=True)
        _validate_identity(self.tool_call_id, "tool-call id")
        _validate_identity(self.tool_name, "tool name")
        if self.source not in _TOOL_SOURCES:
            raise ValidationError("tool-run source is invalid")
        if self.effect not in _TOOL_EFFECTS:
            raise ValidationError("tool-run effect is invalid")
        if self.risk not in _TOOL_RISKS:
            raise ValidationError("tool-run risk is invalid")
        if not isinstance(self.status, ToolRunStatus):
            raise ValidationError("tool-run status is invalid")
        _, arguments = _canonical_json_object(
            self.arguments, "tool arguments", limit=MAX_ARGUMENT_BYTES
        )
        if (
            not isinstance(self.args_hash, str)
            or _HASH.fullmatch(self.args_hash) is None
        ):
            raise ValidationError("tool arguments hash is invalid")
        if hash_tool_arguments(arguments) != self.args_hash:
            raise ValidationError("tool arguments hash does not match arguments")
        _validate_revision(self.baseline_revision)
        _validate_optional_text(
            self.before_status, "tool-run before status", required=True, limit=128
        )
        _validate_revision(self.before_revision, "tool-run before revision")
        if self.before_revision != self.baseline_revision:
            raise ValidationError(
                "tool-run before revision does not match its baseline revision"
            )
        _validate_optional_text(self.after_status, "tool-run after status", limit=128)
        if self.after_revision is not None:
            _validate_revision(self.after_revision, "tool-run after revision")
        if (self.after_status is None) != (self.after_revision is None):
            raise ValidationError("tool-run after state is incomplete")
        if (
            self.after_revision is not None
            and self.after_revision < self.before_revision
        ):
            raise ValidationError("tool-run after revision precedes its baseline")
        _validate_timestamp(self.created_at, "tool-run created_at")
        _validate_timestamp(self.started_at, "tool-run started_at", optional=True)
        _validate_timestamp(
            self.dispatch_started_at,
            "tool-run dispatch_started_at",
            optional=True,
        )
        _validate_timestamp(self.completed_at, "tool-run completed_at", optional=True)
        _validate_optional_text(self.error, "tool-run error")
        copied_result: dict[str, Any] | None = None
        if self.result is not None:
            _, copied_result = _canonical_json_object(
                self.result, "tool result", limit=MAX_RESULT_BYTES
            )
        if self.status in {ToolRunStatus.PROPOSED, ToolRunStatus.WAITING_APPROVAL}:
            if self.started_at is not None or self.completed_at is not None:
                raise ValidationError("unstarted tool run has execution timestamps")
        elif self.status is ToolRunStatus.RUNNING:
            if self.started_at is None or self.completed_at is not None:
                raise ValidationError("running tool run timestamps are malformed")
        elif self.started_at is None or self.completed_at is None:
            raise ValidationError("terminal tool run timestamps are malformed")
        if self.status is ToolRunStatus.COMPLETED:
            if (
                copied_result is None
                or self.error is not None
                or self.after_status is None
                or self.after_revision is None
            ):
                raise ValidationError(
                    "completed tool run must have a result and after state"
                )
        elif copied_result is not None:
            raise ValidationError("unfinished or unsuccessful tool run has a result")
        if self.status not in _TERMINAL_TOOLS and self.after_status is not None:
            raise ValidationError("active tool run cannot have an after state")
        if (
            self.status
            in {
                ToolRunStatus.PROPOSED,
                ToolRunStatus.WAITING_APPROVAL,
            }
            and self.dispatch_started_at is not None
        ):
            raise ValidationError("unstarted tool run has a dispatch timestamp")
        if self.dispatch_started_at is not None and self.started_at is None:
            raise ValidationError("tool dispatch predates its running state")
        if self.status is ToolRunStatus.COMPLETED and self.dispatch_started_at is None:
            raise ValidationError("completed tool run has no durable dispatch boundary")
        if self.status is ToolRunStatus.DENIED and self.after_status is not None:
            raise ValidationError("denied tool run cannot have an after state")
        if self.status in {ToolRunStatus.FAILED, ToolRunStatus.INTERRUPTED}:
            _validate_optional_text(self.error, "tool-run error", required=True)
        elif self.error is not None:
            raise ValidationError("tool run status does not accept an error")
        object.__setattr__(self, "arguments", MappingProxyType(arguments))
        object.__setattr__(
            self,
            "result",
            MappingProxyType(copied_result) if copied_result is not None else None,
        )

    @classmethod
    def proposed(
        cls,
        *,
        project_id: str,
        thread_id: str,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        source: str,
        effect: str,
        risk: str,
        arguments: Mapping[str, Any],
        args_hash: str,
        baseline_revision: int,
        before_status: str,
        before_revision: int,
        created_at: str | None = None,
    ) -> Self:
        return cls(
            project_id=project_id,
            thread_id=thread_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            source=source,
            effect=effect,
            risk=risk,
            arguments=arguments,
            args_hash=args_hash,
            baseline_revision=baseline_revision,
            before_status=before_status,
            before_revision=before_revision,
            status=ToolRunStatus.PROPOSED,
            created_at=created_at or utc_timestamp(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TOOL_RUN_SCHEMA,
            "version": TOOL_RUN_VERSION,
            "project_id": self.project_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "source": self.source,
            "effect": self.effect,
            "risk": self.risk,
            "arguments": dict(self.arguments),
            "args_hash": self.args_hash,
            "baseline_revision": self.baseline_revision,
            "before_status": self.before_status,
            "before_revision": self.before_revision,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "dispatch_started_at": self.dispatch_started_at,
            "completed_at": self.completed_at,
            "after_status": self.after_status,
            "after_revision": self.after_revision,
            "result": dict(self.result) if self.result is not None else None,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "schema",
            "version",
            "project_id",
            "thread_id",
            "turn_id",
            "tool_call_id",
            "tool_name",
            "source",
            "effect",
            "risk",
            "arguments",
            "args_hash",
            "baseline_revision",
            "before_status",
            "before_revision",
            "status",
            "created_at",
            "started_at",
            "dispatch_started_at",
            "completed_at",
            "after_status",
            "after_revision",
            "result",
            "error",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValidationError("agent tool-run record is malformed")
        if value["schema"] != TOOL_RUN_SCHEMA or value["version"] != TOOL_RUN_VERSION:
            raise ValidationError("unsupported agent tool-run schema/version")
        return cls(
            project_id=value["project_id"],
            thread_id=value["thread_id"],
            turn_id=value["turn_id"],
            tool_call_id=value["tool_call_id"],
            tool_name=value["tool_name"],
            source=value["source"],
            effect=value["effect"],
            risk=value["risk"],
            arguments=value["arguments"],
            args_hash=value["args_hash"],
            baseline_revision=value["baseline_revision"],
            before_status=value["before_status"],
            before_revision=value["before_revision"],
            status=_enum_value(ToolRunStatus, value["status"], "tool-run status"),
            created_at=value["created_at"],
            started_at=value["started_at"],
            dispatch_started_at=value["dispatch_started_at"],
            completed_at=value["completed_at"],
            after_status=value["after_status"],
            after_revision=value["after_revision"],
            result=value["result"],
            error=value["error"],
        )


@dataclass(frozen=True)
class ApprovalCheckpoint:
    """Durable approval bound to one exact call and observed PCB revision."""

    checkpoint_id: str
    project_id: str
    thread_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    effect: str
    risk: str
    args_hash: str
    baseline_revision: int
    status: ApprovalStatus
    created_at: str
    decided_at: str | None = None
    decision_source: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.checkpoint_id, "approval checkpoint id")
        _validate_identity(self.project_id, "approval project id")
        _validate_identity(self.thread_id, "approval thread id")
        _validate_identity(self.turn_id, "approval turn id", path_safe=True)
        _validate_identity(self.tool_call_id, "approval tool-call id")
        _validate_identity(self.tool_name, "approval tool name")
        if self.effect not in _TOOL_EFFECTS:
            raise ValidationError("approval tool effect is invalid")
        if self.risk not in _TOOL_RISKS:
            raise ValidationError("approval tool risk is invalid")
        if (
            not isinstance(self.args_hash, str)
            or _HASH.fullmatch(self.args_hash) is None
        ):
            raise ValidationError("approval arguments hash is invalid")
        _validate_revision(self.baseline_revision)
        if not isinstance(self.status, ApprovalStatus):
            raise ValidationError("approval status is invalid")
        _validate_timestamp(self.created_at, "approval created_at")
        _validate_timestamp(self.decided_at, "approval decided_at", optional=True)
        _validate_optional_text(self.decision_source, "approval decision source")
        _validate_optional_text(self.reason, "approval reason")
        if self.status is ApprovalStatus.PENDING:
            if any(
                value is not None
                for value in (self.decided_at, self.decision_source, self.reason)
            ):
                raise ValidationError("pending approval contains a decision")
        elif self.decided_at is None or not self.decision_source:
            raise ValidationError("resolved approval is missing decision metadata")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": APPROVAL_SCHEMA,
            "version": APPROVAL_VERSION,
            "checkpoint_id": self.checkpoint_id,
            "project_id": self.project_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "effect": self.effect,
            "risk": self.risk,
            "args_hash": self.args_hash,
            "baseline_revision": self.baseline_revision,
            "status": self.status.value,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decision_source": self.decision_source,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "schema",
            "version",
            "checkpoint_id",
            "project_id",
            "thread_id",
            "turn_id",
            "tool_call_id",
            "tool_name",
            "effect",
            "risk",
            "args_hash",
            "baseline_revision",
            "status",
            "created_at",
            "decided_at",
            "decision_source",
            "reason",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValidationError("agent approval record is malformed")
        if value["schema"] != APPROVAL_SCHEMA or value["version"] != APPROVAL_VERSION:
            raise ValidationError("unsupported agent approval schema/version")
        return cls(
            checkpoint_id=value["checkpoint_id"],
            project_id=value["project_id"],
            thread_id=value["thread_id"],
            turn_id=value["turn_id"],
            tool_call_id=value["tool_call_id"],
            tool_name=value["tool_name"],
            effect=value["effect"],
            risk=value["risk"],
            args_hash=value["args_hash"],
            baseline_revision=value["baseline_revision"],
            status=_enum_value(ApprovalStatus, value["status"], "approval status"),
            created_at=value["created_at"],
            decided_at=value["decided_at"],
            decision_source=value["decision_source"],
            reason=value["reason"],
        )


@dataclass(frozen=True)
class TurnRecord:
    """Versioned aggregate root for one agent turn."""

    project_id: str
    thread_id: str
    turn_id: str
    sequence: int
    user_message: str
    baseline_revision: int
    status: TurnStatus
    record_revision: int
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    tool_runs: tuple[ToolRunRecord, ...] = ()
    approvals: tuple[ApprovalCheckpoint, ...] = ()
    stop_reason: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.project_id, "turn project id")
        _validate_identity(self.thread_id, "turn thread id")
        _validate_identity(self.turn_id, "turn id", path_safe=True)
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise ValidationError("turn sequence must be a positive integer")
        _validate_optional_text(
            self.user_message,
            "turn user message",
            required=True,
            limit=MAX_MESSAGE_BYTES,
        )
        _validate_revision(self.baseline_revision)
        if not isinstance(self.status, TurnStatus):
            raise ValidationError("turn status is invalid")
        _validate_revision(self.record_revision, "turn record revision")
        _validate_timestamp(self.created_at, "turn created_at")
        _validate_timestamp(self.updated_at, "turn updated_at")
        _validate_timestamp(self.started_at, "turn started_at", optional=True)
        _validate_timestamp(self.completed_at, "turn completed_at", optional=True)
        _validate_optional_text(self.stop_reason, "turn stop reason")
        _validate_optional_text(self.error, "turn error")
        if not isinstance(self.tool_runs, tuple) or len(self.tool_runs) > MAX_TOOL_RUNS:
            raise ValidationError("turn tool-run collection is malformed")
        if not isinstance(self.approvals, tuple) or len(self.approvals) > MAX_APPROVALS:
            raise ValidationError("turn approval collection is malformed")
        self._validate_lifecycle()
        self._validate_children()

    def _validate_lifecycle(self) -> None:
        if self.status is TurnStatus.QUEUED:
            if self.started_at is not None or self.completed_at is not None:
                raise ValidationError("queued turn has execution timestamps")
        elif self.status in {TurnStatus.RUNNING, TurnStatus.WAITING_APPROVAL}:
            if self.started_at is None or self.completed_at is not None:
                raise ValidationError("active turn timestamps are malformed")
        elif self.started_at is None or self.completed_at is None:
            raise ValidationError("terminal turn timestamps are malformed")
        if self.status is TurnStatus.FAILED:
            _validate_optional_text(self.error, "turn error", required=True)
        elif self.error is not None and self.status is not TurnStatus.INTERRUPTED:
            raise ValidationError("turn status does not accept an error")
        if self.status not in _TERMINAL_TURNS and self.stop_reason is not None:
            raise ValidationError("active turn cannot have a stop reason")

    def _validate_children(self) -> None:
        tool_by_id: dict[str, ToolRunRecord] = {}
        for tool in self.tool_runs:
            if not isinstance(tool, ToolRunRecord):
                raise ValidationError("turn contains a malformed tool run")
            if (
                tool.project_id,
                tool.thread_id,
                tool.turn_id,
            ) != (self.project_id, self.thread_id, self.turn_id):
                raise ValidationError("tool-run identity does not match its turn")
            if tool.tool_call_id in tool_by_id:
                raise ValidationError("turn contains duplicate tool-call ids")
            tool_by_id[tool.tool_call_id] = tool

        pending: list[ApprovalCheckpoint] = []
        checkpoint_ids: set[str] = set()
        approved_tool_ids: set[str] = set()
        for approval in self.approvals:
            if not isinstance(approval, ApprovalCheckpoint):
                raise ValidationError("turn contains a malformed approval")
            if approval.checkpoint_id in checkpoint_ids:
                raise ValidationError("turn contains duplicate approval ids")
            checkpoint_ids.add(approval.checkpoint_id)
            if (
                approval.project_id,
                approval.thread_id,
                approval.turn_id,
            ) != (self.project_id, self.thread_id, self.turn_id):
                raise ValidationError("approval identity does not match its turn")
            referenced_tool = tool_by_id.get(approval.tool_call_id)
            if referenced_tool is None:
                raise ValidationError("approval references an unknown tool call")
            if (
                approval.tool_name != referenced_tool.tool_name
                or approval.effect != referenced_tool.effect
                or approval.risk != referenced_tool.risk
                or approval.args_hash != referenced_tool.args_hash
                or approval.baseline_revision != referenced_tool.baseline_revision
            ):
                raise ValidationError("approval binding does not match its tool call")
            if approval.tool_call_id in approved_tool_ids:
                raise ValidationError("tool call has more than one approval")
            approved_tool_ids.add(approval.tool_call_id)
            if approval.status is ApprovalStatus.PENDING:
                pending.append(approval)
                if referenced_tool.status is not ToolRunStatus.WAITING_APPROVAL:
                    raise ValidationError("pending approval tool is not waiting")
            elif approval.status is ApprovalStatus.DENIED:
                if referenced_tool.status is not ToolRunStatus.DENIED:
                    raise ValidationError("denied approval tool is not denied")
            elif referenced_tool.status in {
                ToolRunStatus.PROPOSED,
                ToolRunStatus.WAITING_APPROVAL,
            }:
                raise ValidationError("approved tool has not started")
        if len(pending) > 1:
            raise ValidationError("turn contains more than one pending approval")
        if self.status is TurnStatus.WAITING_APPROVAL:
            if len(pending) != 1:
                raise ValidationError("waiting turn has no pending approval")
        elif pending:
            raise ValidationError("non-waiting turn contains a pending approval")
        if self.status in _TERMINAL_TURNS and any(
            tool.status not in _TERMINAL_TOOLS for tool in self.tool_runs
        ):
            raise ValidationError("terminal turn contains an active tool run")

    @property
    def pending_approval(self) -> ApprovalCheckpoint | None:
        return next(
            (
                approval
                for approval in self.approvals
                if approval.status is ApprovalStatus.PENDING
            ),
            None,
        )

    def tool_run(self, tool_call_id: str) -> ToolRunRecord:
        for tool in self.tool_runs:
            if tool.tool_call_id == tool_call_id:
                return tool
        raise ValidationError("turn does not contain that tool call")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TURN_SCHEMA,
            "version": TURN_VERSION,
            "project_id": self.project_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "user_message": self.user_message,
            "baseline_revision": self.baseline_revision,
            "status": self.status.value,
            "record_revision": self.record_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "tool_runs": [tool.to_dict() for tool in self.tool_runs],
            "approvals": [approval.to_dict() for approval in self.approvals],
            "stop_reason": self.stop_reason,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "schema",
            "version",
            "project_id",
            "thread_id",
            "turn_id",
            "sequence",
            "user_message",
            "baseline_revision",
            "status",
            "record_revision",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "tool_runs",
            "approvals",
            "stop_reason",
            "error",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValidationError("agent turn record is malformed")
        if value["schema"] != TURN_SCHEMA or value["version"] != TURN_VERSION:
            raise ValidationError("unsupported agent turn schema/version")
        if not isinstance(value["tool_runs"], list) or not isinstance(
            value["approvals"], list
        ):
            raise ValidationError("agent turn child records are malformed")
        return cls(
            project_id=value["project_id"],
            thread_id=value["thread_id"],
            turn_id=value["turn_id"],
            sequence=value["sequence"],
            user_message=value["user_message"],
            baseline_revision=value["baseline_revision"],
            status=_enum_value(TurnStatus, value["status"], "turn status"),
            record_revision=value["record_revision"],
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            started_at=value["started_at"],
            completed_at=value["completed_at"],
            tool_runs=tuple(
                ToolRunRecord.from_dict(item) for item in value["tool_runs"]
            ),
            approvals=tuple(
                ApprovalCheckpoint.from_dict(item) for item in value["approvals"]
            ),
            stop_reason=value["stop_reason"],
            error=value["error"],
        )


class AgentTurnStore:
    """Project-scoped atomic store for :class:`TurnRecord` aggregates."""

    def __init__(
        self,
        project_root: Path,
        locks_root: Path,
        *,
        lock_timeout: float = 10.0,
    ) -> None:
        if project_root.is_symlink() or not project_root.is_dir():
            raise ValidationError("agent turn project root must be a directory")
        if lock_timeout < 0 or lock_timeout > 300:
            raise ValidationError("agent turn lock timeout must be between 0 and 300")
        self.project_root = project_root.resolve(strict=True)
        self.project_id = self.project_root.name
        self.locks_root = locks_root.resolve(strict=False)
        self.turns_root = self.project_root / "agent-turns"
        self.index_path = self.turns_root / "index.json"
        self.lock_timeout = lock_timeout
        self._schema_checked = False

    def begin(
        self,
        *,
        project_id: str,
        thread_id: str,
        user_message: str,
        baseline_revision: int,
        turn_id: str | None = None,
    ) -> TurnRecord:
        """Persist a queued turn before any provider or tool side effect begins."""

        if project_id != self.project_id:
            raise ValidationError("turn project id does not match the project root")
        _validate_identity(thread_id, "turn thread id")
        _validate_revision(baseline_revision)
        _validate_optional_text(
            user_message,
            "turn user message",
            required=True,
            limit=MAX_MESSAGE_BYTES,
        )
        if turn_id is not None:
            _validate_identity(turn_id, "turn id", path_safe=True)
        with self._lock():
            self._ensure_turns_root_unlocked()
            self._ensure_current_schema_unlocked()
            active = [
                self._load_path_unlocked(path) for path in self._record_paths_unlocked()
            ]
            if any(
                record.thread_id == thread_id
                and record.status
                in {
                    TurnStatus.QUEUED,
                    TurnStatus.RUNNING,
                    TurnStatus.WAITING_APPROVAL,
                }
                for record in active
            ):
                raise ValidationError(
                    "agent thread already has a queued or active turn"
                )
            if len(self._record_paths_unlocked()) >= MAX_TURNS:
                raise ValidationError(
                    "project reached its 2000 agent-turn record limit"
                )
            candidates = [turn_id] if turn_id is not None else [None] * 8
            for candidate in candidates:
                allocated = candidate or f"turn-{new_run_id()}"
                path = self.turns_root / f"{allocated}.json"
                if path.exists() or path.is_symlink():
                    if turn_id is not None:
                        raise ValidationError("agent turn identity collision")
                    continue
                sequence = self._allocate_sequence_unlocked()
                now = utc_timestamp()
                record = TurnRecord(
                    project_id=project_id,
                    thread_id=thread_id,
                    turn_id=allocated,
                    sequence=sequence,
                    user_message=user_message,
                    baseline_revision=baseline_revision,
                    status=TurnStatus.QUEUED,
                    record_revision=0,
                    created_at=now,
                    updated_at=now,
                )
                self._write_unlocked(record)
                return record
        raise PCBDraftError("could not allocate a unique agent turn id")

    def begin_with_tool(
        self,
        *,
        project_id: str,
        thread_id: str,
        user_message: str,
        baseline_revision: int,
        tool_run: ToolRunRecord,
    ) -> TurnRecord:
        """Atomically persist a queued external turn and its exact first call.

        External transports already have a concrete call with arguments. Writing
        the turn and proposal together prevents recovery from reconstructing a
        different no-argument slash command after a crash.
        """

        if project_id != self.project_id:
            raise ValidationError("turn project id does not match the project root")
        _validate_identity(thread_id, "turn thread id")
        _validate_revision(baseline_revision)
        _validate_optional_text(
            user_message,
            "turn user message",
            required=True,
            limit=MAX_MESSAGE_BYTES,
        )
        if not isinstance(tool_run, ToolRunRecord):
            raise ValidationError("external turn tool run is malformed")
        if tool_run.status is not ToolRunStatus.PROPOSED:
            raise ValidationError("external turn must begin with a proposed tool")
        if (
            tool_run.project_id != project_id
            or tool_run.thread_id != thread_id
            or tool_run.baseline_revision != baseline_revision
        ):
            raise ValidationError("external tool identity does not match its turn")
        with self._lock():
            self._ensure_turns_root_unlocked()
            self._ensure_current_schema_unlocked()
            records = [
                self._load_path_unlocked(path) for path in self._record_paths_unlocked()
            ]
            if any(
                record.thread_id == thread_id
                and record.status
                in {
                    TurnStatus.QUEUED,
                    TurnStatus.RUNNING,
                    TurnStatus.WAITING_APPROVAL,
                }
                for record in records
            ):
                raise ValidationError(
                    "agent thread already has a queued or active turn"
                )
            if len(records) >= MAX_TURNS:
                raise ValidationError(
                    "project reached its 2000 agent-turn record limit"
                )
            path = self.turns_root / f"{tool_run.turn_id}.json"
            if path.exists() or path.is_symlink():
                raise ValidationError("agent turn identity collision")
            now = utc_timestamp()
            record = TurnRecord(
                project_id=project_id,
                thread_id=thread_id,
                turn_id=tool_run.turn_id,
                sequence=self._allocate_sequence_unlocked(),
                user_message=user_message,
                baseline_revision=baseline_revision,
                status=TurnStatus.QUEUED,
                record_revision=0,
                created_at=now,
                updated_at=now,
                tool_runs=(tool_run,),
            )
            self._write_unlocked(record)
            return record

    def load(self, turn_id: str) -> TurnRecord:
        """Load and strictly validate one turn under the project lock."""

        with self._lock():
            self._ensure_current_schema_unlocked()
            return self._load_unlocked(turn_id)

    def list(
        self,
        *,
        thread_id: str | None = None,
        statuses: Iterable[TurnStatus | str] | None = None,
        limit: int | None = None,
    ) -> list[TurnRecord]:
        """Return newest-first validated records, optionally filtered."""

        if thread_id is not None:
            _validate_identity(thread_id, "turn thread id")
        selected = self._normalize_statuses(statuses)
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 2000
        ):
            raise ValidationError("agent turn list limit must be from 1 to 2000")
        with self._lock():
            self._ensure_current_schema_unlocked()
            records = [
                self._load_path_unlocked(path) for path in self._record_paths_unlocked()
            ]
        records = [
            record
            for record in records
            if (thread_id is None or record.thread_id == thread_id)
            and (selected is None or record.status in selected)
        ]
        sequences = [record.sequence for record in records]
        if len(sequences) != len(set(sequences)):
            raise ValidationError("agent turn records contain duplicate sequences")
        records.sort(key=lambda record: record.sequence, reverse=True)
        return records[:limit]

    def latest(
        self,
        *,
        thread_id: str | None = None,
        statuses: Iterable[TurnStatus | str] | None = None,
    ) -> TurnRecord | None:
        """Return the latest matching turn without mutating recovery state."""

        records = self.list(thread_id=thread_id, statuses=statuses, limit=1)
        return records[0] if records else None

    def waiting_approval(self, *, thread_id: str | None = None) -> TurnRecord | None:
        """Recover the latest crash-durable approval checkpoint, if any."""

        return self.latest(thread_id=thread_id, statuses=(TurnStatus.WAITING_APPROVAL,))

    def update(
        self,
        turn_id: str,
        status: TurnStatus | str,
        *,
        expected_record_revision: int | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
    ) -> TurnRecord:
        """Apply one legal turn-state transition with optional optimistic CAS."""

        target = _enum_value(TurnStatus, status, "turn status")
        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if target not in _TURN_TRANSITIONS[current.status]:
                raise ValidationError(
                    f"illegal agent turn transition: {current.status.value} -> {target.value}"
                )
            now = utc_timestamp()
            started_at = current.started_at
            completed_at = current.completed_at
            if target is not TurnStatus.QUEUED and started_at is None:
                started_at = now
            if target in _TERMINAL_TURNS:
                completed_at = now
            updated = replace(
                current,
                status=target,
                record_revision=current.record_revision + 1,
                updated_at=now,
                started_at=started_at,
                completed_at=completed_at,
                stop_reason=stop_reason,
                error=error,
            )
            self._write_unlocked(updated)
            return updated

    def append_tool_run(
        self,
        turn_id: str,
        tool_run: ToolRunRecord,
        *,
        expected_record_revision: int | None = None,
    ) -> TurnRecord:
        """Append one proposed call while its turn is running."""

        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if current.status is not TurnStatus.RUNNING:
                raise ValidationError(
                    "tool calls can only be proposed in a running turn"
                )
            if tool_run.status is not ToolRunStatus.PROPOSED:
                raise ValidationError("new tool run must have proposed status")
            if any(
                existing.tool_call_id == tool_run.tool_call_id
                for existing in current.tool_runs
            ):
                raise ValidationError("turn already contains that tool-call id")
            if any(
                existing.status not in _TERMINAL_TOOLS for existing in current.tool_runs
            ):
                raise ValidationError("turn already contains an active tool call")
            now = utc_timestamp()
            updated = replace(
                current,
                tool_runs=(*current.tool_runs, tool_run),
                record_revision=current.record_revision + 1,
                updated_at=now,
            )
            self._write_unlocked(updated)
            return updated

    def update_tool_run(
        self,
        turn_id: str,
        tool_call_id: str,
        status: ToolRunStatus | str,
        *,
        expected_record_revision: int | None = None,
        after_status: str | None = None,
        after_revision: int | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        dispatch_started: bool = True,
    ) -> TurnRecord:
        """Apply a legal non-approval tool transition and persist its receipt."""

        target = _enum_value(ToolRunStatus, status, "tool-run status")
        if not isinstance(dispatch_started, bool):
            raise ValidationError("tool dispatch flag must be boolean")
        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if current.status is not TurnStatus.RUNNING:
                raise ValidationError("tool runs can only advance in a running turn")
            index, tool = self._find_tool(current, tool_call_id)
            if target is ToolRunStatus.WAITING_APPROVAL:
                raise ValidationError("use request_approval for a waiting tool call")
            if tool.status is ToolRunStatus.WAITING_APPROVAL:
                raise ValidationError("use resolve_approval for a waiting tool call")
            if target not in _TOOL_TRANSITIONS[tool.status]:
                raise ValidationError(
                    f"illegal tool-run transition: {tool.status.value} -> {target.value}"
                )
            now = utc_timestamp()
            started_at = tool.started_at
            completed_at = tool.completed_at
            if target is ToolRunStatus.RUNNING or (
                target in _TERMINAL_TOOLS and started_at is None
            ):
                started_at = now
            dispatch_started_at = tool.dispatch_started_at
            if target is ToolRunStatus.RUNNING and dispatch_started:
                dispatch_started_at = now
            if target in _TERMINAL_TOOLS:
                completed_at = now
            advanced = replace(
                tool,
                status=target,
                started_at=started_at,
                dispatch_started_at=dispatch_started_at,
                completed_at=completed_at,
                after_status=after_status,
                after_revision=after_revision,
                result=result,
                error=error,
            )
            tools = list(current.tool_runs)
            tools[index] = advanced
            updated = replace(
                current,
                tool_runs=tuple(tools),
                record_revision=current.record_revision + 1,
                updated_at=now,
            )
            self._write_unlocked(updated)
            return updated

    def begin_dispatch(
        self,
        turn_id: str,
        tool_call_id: str,
        *,
        expected_record_revision: int | None = None,
    ) -> TurnRecord:
        """Persist the exact boundary immediately before a handler may run."""

        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if current.status is not TurnStatus.RUNNING:
                raise ValidationError("tool dispatch requires a running turn")
            index, tool = self._find_tool(current, tool_call_id)
            if tool.status is not ToolRunStatus.RUNNING:
                raise ValidationError("tool dispatch requires a running tool call")
            if tool.dispatch_started_at is not None:
                raise ValidationError("tool call was already dispatched")
            now = utc_timestamp()
            tools = list(current.tool_runs)
            tools[index] = replace(tool, dispatch_started_at=now)
            updated = replace(
                current,
                tool_runs=tuple(tools),
                record_revision=current.record_revision + 1,
                updated_at=now,
            )
            self._write_unlocked(updated)
            return updated

    def interrupt_active(
        self,
        turn_id: str,
        reason: str,
        *,
        expected_record_revision: int | None = None,
    ) -> TurnRecord:
        """Atomically retain a crash-visible interruption at the active boundary."""

        _validate_optional_text(reason, "turn interruption reason", required=True)
        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if current.status is not TurnStatus.RUNNING:
                raise ValidationError("only a running turn may be interrupted")
            active = [
                (index, tool)
                for index, tool in enumerate(current.tool_runs)
                if tool.status not in _TERMINAL_TOOLS
            ]
            if len(active) > 1:
                raise ValidationError(
                    "running turn contains multiple active tool calls"
                )
            now = utc_timestamp()
            tools = list(current.tool_runs)
            if active:
                index, tool = active[0]
                if tool.status is ToolRunStatus.RUNNING:
                    tools[index] = replace(
                        tool,
                        status=ToolRunStatus.INTERRUPTED,
                        completed_at=now,
                        error=reason,
                    )
                elif tool.status is ToolRunStatus.PROPOSED:
                    tools[index] = replace(
                        tool,
                        status=ToolRunStatus.CANCELLED,
                        started_at=now,
                        completed_at=now,
                    )
                else:
                    raise ValidationError(
                        "approval-waiting tool must be resumed through its checkpoint"
                    )
            updated = replace(
                current,
                status=TurnStatus.INTERRUPTED,
                record_revision=current.record_revision + 1,
                updated_at=now,
                completed_at=now,
                tool_runs=tuple(tools),
                stop_reason=reason,
                error=reason,
            )
            self._write_unlocked(updated)
            return updated

    def cancel(
        self,
        turn_id: str,
        reason: str,
        *,
        decision_source: str = "runtime",
        expected_record_revision: int | None = None,
    ) -> TurnRecord:
        """Atomically close pending approval/active tool state and cancel the turn."""

        _validate_optional_text(reason, "turn cancellation reason", required=True)
        _validate_optional_text(
            decision_source, "cancellation decision source", required=True
        )
        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if current.status is TurnStatus.CANCELLED:
                return current
            if current.status in {
                TurnStatus.COMPLETED,
                TurnStatus.FAILED,
                TurnStatus.INTERRUPTED,
            }:
                raise ValidationError("terminal agent turn cannot be cancelled")
            now = utc_timestamp()
            tools = list(current.tool_runs)
            approvals = list(current.approvals)
            pending = current.pending_approval
            if pending is not None:
                approval_index = approvals.index(pending)
                approvals[approval_index] = replace(
                    pending,
                    status=ApprovalStatus.DENIED,
                    decided_at=now,
                    decision_source=decision_source,
                    reason=reason,
                )
                tool_index, tool = self._find_tool(current, pending.tool_call_id)
                tools[tool_index] = replace(
                    tool,
                    status=ToolRunStatus.DENIED,
                    started_at=now,
                    completed_at=now,
                )
            else:
                active = [
                    (index, tool)
                    for index, tool in enumerate(current.tool_runs)
                    if tool.status not in _TERMINAL_TOOLS
                ]
                if len(active) > 1:
                    raise ValidationError(
                        "running turn contains multiple active tool calls"
                    )
                if active:
                    tool_index, tool = active[0]
                    if tool.status is ToolRunStatus.WAITING_APPROVAL:
                        raise ValidationError(
                            "approval-waiting tool has no matching checkpoint"
                        )
                    tools[tool_index] = replace(
                        tool,
                        status=ToolRunStatus.CANCELLED,
                        started_at=tool.started_at or now,
                        completed_at=now,
                    )
            cancelled = replace(
                current,
                status=TurnStatus.CANCELLED,
                record_revision=current.record_revision + 1,
                updated_at=now,
                started_at=current.started_at or now,
                completed_at=now,
                tool_runs=tuple(tools),
                approvals=tuple(approvals),
                stop_reason=reason,
                error=None,
            )
            self._write_unlocked(cancelled)
            return cancelled

    def resume(
        self,
        turn_id: str,
        *,
        expected_record_revision: int | None = None,
    ) -> TurnRecord:
        """Resume a retryable terminal turn without replaying completed tool calls."""

        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if current.status not in {
                TurnStatus.FAILED,
                TurnStatus.INTERRUPTED,
                TurnStatus.CANCELLED,
            }:
                raise ValidationError(
                    "only failed, interrupted, or cancelled turns can be resumed"
                )
            if any(
                tool.status is ToolRunStatus.INTERRUPTED
                and tool.dispatch_started_at is not None
                for tool in current.tool_runs
            ):
                raise ValidationError(
                    "an interrupted dispatched tool cannot be resumed; "
                    "inspect the project and submit a new turn"
                )
            now = utc_timestamp()
            resumed = replace(
                current,
                status=TurnStatus.RUNNING,
                record_revision=current.record_revision + 1,
                updated_at=now,
                completed_at=None,
                stop_reason=None,
                error=None,
            )
            self._write_unlocked(resumed)
            return resumed

    def request_approval(
        self,
        turn_id: str,
        tool_call_id: str,
        *,
        checkpoint_id: str | None = None,
        expected_record_revision: int | None = None,
    ) -> TurnRecord:
        """Atomically park a proposed tool call and its turn for approval."""

        if checkpoint_id is not None:
            _validate_identity(checkpoint_id, "approval checkpoint id")
        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if current.status is not TurnStatus.RUNNING:
                raise ValidationError("only a running turn may request approval")
            if current.pending_approval is not None:
                raise ValidationError("turn already has a pending approval")
            index, tool = self._find_tool(current, tool_call_id)
            if tool.status is not ToolRunStatus.PROPOSED:
                raise ValidationError("only a proposed tool call may request approval")
            now = utc_timestamp()
            waiting_tool = replace(tool, status=ToolRunStatus.WAITING_APPROVAL)
            tools = list(current.tool_runs)
            tools[index] = waiting_tool
            checkpoint = ApprovalCheckpoint(
                checkpoint_id=checkpoint_id or f"approval-{new_run_id()}",
                project_id=current.project_id,
                thread_id=current.thread_id,
                turn_id=current.turn_id,
                tool_call_id=tool.tool_call_id,
                tool_name=tool.tool_name,
                effect=tool.effect,
                risk=tool.risk,
                args_hash=tool.args_hash,
                baseline_revision=tool.baseline_revision,
                status=ApprovalStatus.PENDING,
                created_at=now,
            )
            if any(
                item.checkpoint_id == checkpoint.checkpoint_id
                for item in current.approvals
            ):
                raise ValidationError("approval checkpoint identity collision")
            updated = replace(
                current,
                status=TurnStatus.WAITING_APPROVAL,
                tool_runs=tuple(tools),
                approvals=(*current.approvals, checkpoint),
                record_revision=current.record_revision + 1,
                updated_at=now,
            )
            self._write_unlocked(updated)
            return updated

    def resolve_approval(
        self,
        turn_id: str,
        tool_call_id: str,
        decision: ApprovalStatus | str,
        *,
        tool_name: str,
        effect: str,
        risk: str,
        args_hash: str,
        baseline_revision: int,
        current_revision: int | None = None,
        current_revision_reader: Callable[[], int] | None = None,
        checkpoint_id: str | None = None,
        decision_source: str,
        reason: str | None = None,
        cancel_on_deny: bool = False,
        expected_record_revision: int | None = None,
    ) -> TurnRecord:
        """Approve or deny one exact call; stale revisions can never be approved."""

        resolved = _enum_value(ApprovalStatus, decision, "approval decision")
        if resolved is ApprovalStatus.PENDING:
            raise ValidationError("approval decision must be approved or denied")
        if not isinstance(args_hash, str) or _HASH.fullmatch(args_hash) is None:
            raise ValidationError("approval arguments hash is invalid")
        _validate_identity(tool_name, "approval tool name")
        if effect not in _TOOL_EFFECTS:
            raise ValidationError("approval tool effect is invalid")
        if risk not in _TOOL_RISKS:
            raise ValidationError("approval tool risk is invalid")
        _validate_revision(baseline_revision)
        if (current_revision is None) == (current_revision_reader is None):
            raise ValidationError(
                "approval requires exactly one authoritative revision source"
            )
        if current_revision is not None:
            _validate_revision(current_revision, "current project revision")
        if checkpoint_id is not None:
            _validate_identity(checkpoint_id, "approval checkpoint id")
        if not isinstance(cancel_on_deny, bool):
            raise ValidationError("approval deny policy must be boolean")
        _validate_optional_text(
            decision_source, "approval decision source", required=True
        )
        _validate_optional_text(reason, "approval reason")
        with self._lock():
            current = self._load_unlocked(turn_id)
            self._check_record_revision(current, expected_record_revision)
            if current.status is not TurnStatus.WAITING_APPROVAL:
                raise ValidationError("turn is not waiting for approval")
            checkpoint = current.pending_approval
            if checkpoint is None or checkpoint.tool_call_id != tool_call_id:
                raise ValidationError("turn is not waiting for that tool call")
            if checkpoint_id is not None and checkpoint.checkpoint_id != checkpoint_id:
                raise ValidationError(
                    "approval checkpoint does not match the pending tool call"
                )
            if (
                tool_name != checkpoint.tool_name
                or effect != checkpoint.effect
                or risk != checkpoint.risk
                or args_hash != checkpoint.args_hash
                or baseline_revision != checkpoint.baseline_revision
            ):
                raise ValidationError(
                    "approval payload does not match the pending tool call"
                )
            index, tool = self._find_tool(current, tool_call_id)
            if (
                checkpoint.tool_name != tool.tool_name
                or checkpoint.effect != tool.effect
                or checkpoint.risk != tool.risk
                or checkpoint.args_hash != tool.args_hash
                or checkpoint.baseline_revision != tool.baseline_revision
            ):
                raise ValidationError(
                    "approval binding no longer matches its tool call"
                )
            authoritative_revision = (
                current_revision_reader()
                if current_revision_reader is not None
                else current_revision
            )
            _validate_revision(authoritative_revision, "current project revision")
            if resolved is ApprovalStatus.APPROVED and (
                authoritative_revision != checkpoint.baseline_revision
            ):
                raise ValidationError(
                    "approval baseline revision is stale; submit a new tool call"
                )
            now = utc_timestamp()
            decided = replace(
                checkpoint,
                status=resolved,
                decided_at=now,
                decision_source=decision_source,
                reason=reason,
            )
            approvals = list(current.approvals)
            approval_index = approvals.index(checkpoint)
            approvals[approval_index] = decided
            next_tool_status = (
                ToolRunStatus.RUNNING
                if resolved is ApprovalStatus.APPROVED
                else ToolRunStatus.DENIED
            )
            next_tool = replace(
                tool,
                status=next_tool_status,
                started_at=now,
                completed_at=now if next_tool_status is ToolRunStatus.DENIED else None,
            )
            tools = list(current.tool_runs)
            tools[index] = next_tool
            updated = replace(
                current,
                status=(
                    TurnStatus.CANCELLED
                    if resolved is ApprovalStatus.DENIED and cancel_on_deny
                    else TurnStatus.RUNNING
                ),
                tool_runs=tuple(tools),
                approvals=tuple(approvals),
                record_revision=current.record_revision + 1,
                updated_at=now,
                completed_at=(
                    now
                    if resolved is ApprovalStatus.DENIED and cancel_on_deny
                    else None
                ),
                stop_reason=(
                    reason or "the pending PCB tool was rejected"
                    if resolved is ApprovalStatus.DENIED and cancel_on_deny
                    else None
                ),
            )
            self._write_unlocked(updated)
            return updated

    def _lock(self) -> ResourceLock:
        return ResourceLock(
            self.project_root, self.locks_root, timeout=self.lock_timeout
        )

    def _record_paths_unlocked(self) -> builtins.list[Path]:
        if not self.turns_root.exists():
            return []
        if self.turns_root.is_symlink() or not self.turns_root.is_dir():
            raise ValidationError("agent turn directory is malformed")
        return [
            path
            for path in self.turns_root.glob("*.json")
            if path != self.index_path and not path.is_symlink() and path.is_file()
        ]

    def _ensure_turns_root_unlocked(self) -> None:
        """Create the turn directory without following an existing symlink."""

        try:
            if self.turns_root.is_symlink():
                raise ValidationError("agent turn directory cannot be a symlink")
            if self.turns_root.exists():
                if not self.turns_root.is_dir():
                    raise ValidationError("agent turn directory is malformed")
            else:
                make_directory(self.turns_root)
            resolved = self.turns_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationError("agent turn directory is unavailable") from exc
        if resolved.parent != self.project_root:
            raise ValidationError("agent turn directory escapes its project")

    def _ensure_current_schema_unlocked(self) -> None:
        """Upgrade legacy turn documents under the project lock.

        A v1 pending approval did not bind the tool's authority metadata.  Such
        a checkpoint is cancelled during migration rather than being made
        approvable by inference.  Historical resolved records are enriched from
        their already-bound tool call, and old execution records conservatively
        treat a persisted running state as dispatched.
        """

        if self._schema_checked:
            return
        self._ensure_turns_root_unlocked()
        paths = self._record_paths_unlocked()
        raw_records: list[tuple[Path, dict[str, Any]]] = []
        needs_migration = False
        for path in paths:
            value = load_json_limited(path, TURN_FILE_LIMIT)
            if not isinstance(value, dict):
                raise ValidationError("agent turn record is malformed")
            if value.get("schema") != TURN_SCHEMA or value.get("version") not in {
                1,
                TURN_VERSION,
            }:
                raise ValidationError("unsupported agent turn schema/version")
            if (
                value.get("project_id") != self.project_id
                or value.get("turn_id") != path.stem
            ):
                raise ValidationError("agent turn record identity is malformed")
            raw_records.append((path, value))
            tool_runs = value.get("tool_runs")
            approvals = value.get("approvals")
            if (
                value.get("version") != TURN_VERSION
                or not isinstance(tool_runs, list)
                or not isinstance(approvals, list)
                or any(
                    not isinstance(item, dict)
                    or item.get("version") != TOOL_RUN_VERSION
                    for item in tool_runs
                )
                or any(
                    not isinstance(item, dict)
                    or item.get("version") != APPROVAL_VERSION
                    for item in approvals
                )
            ):
                needs_migration = True

        if needs_migration:
            ordered = sorted(
                raw_records,
                key=lambda item: (str(item[1].get("created_at", "")), item[0].stem),
            )
            for sequence, (path, value) in enumerate(ordered, start=1):
                upgraded = self._upgrade_legacy_document(value, sequence=sequence)
                record = TurnRecord.from_dict(upgraded)
                atomic_write_json(path, record.to_dict())
            next_sequence = len(ordered) + 1
        else:
            records = [TurnRecord.from_dict(value) for _path, value in raw_records]
            sequences = [record.sequence for record in records]
            if len(sequences) != len(set(sequences)):
                raise ValidationError("agent turn records contain duplicate sequences")
            next_sequence = max(sequences, default=0) + 1

        persisted_next = self._load_index_next_sequence_unlocked()
        if persisted_next is None or persisted_next < next_sequence or needs_migration:
            atomic_write_json(
                self.index_path,
                {
                    "schema": TURN_INDEX_SCHEMA,
                    "version": TURN_INDEX_VERSION,
                    "next_sequence": next_sequence,
                },
            )
        self._schema_checked = True

    @staticmethod
    def _upgrade_legacy_document(
        value: Mapping[str, Any], *, sequence: int
    ) -> dict[str, Any]:
        """Return one strict v2 aggregate, failing old pending authority closed."""

        try:
            upgraded = json.loads(
                json.dumps(
                    dict(value),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError("legacy agent turn is not valid JSON") from exc
        if not isinstance(upgraded, dict):
            raise ValidationError("legacy agent turn record is malformed")
        tools = upgraded.get("tool_runs")
        approvals = upgraded.get("approvals")
        if not isinstance(tools, list) or not isinstance(approvals, list):
            raise ValidationError("legacy agent turn child records are malformed")

        tool_by_id: dict[str, dict[str, Any]] = {}
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValidationError("legacy agent tool-run record is malformed")
            if tool.get("schema") != TOOL_RUN_SCHEMA or tool.get("version") not in {
                1,
                TOOL_RUN_VERSION,
            }:
                raise ValidationError("unsupported agent tool-run schema/version")
            if tool.get("version") == 1:
                status = tool.get("status")
                tool["dispatch_started_at"] = (
                    tool.get("started_at")
                    if status
                    in {
                        ToolRunStatus.RUNNING.value,
                        ToolRunStatus.COMPLETED.value,
                        ToolRunStatus.FAILED.value,
                        ToolRunStatus.INTERRUPTED.value,
                    }
                    else None
                )
                tool["version"] = TOOL_RUN_VERSION
            tool_call_id = tool.get("tool_call_id")
            if not isinstance(tool_call_id, str) or tool_call_id in tool_by_id:
                raise ValidationError("legacy agent tool-call identity is malformed")
            tool_by_id[tool_call_id] = tool

        legacy_pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
        migration_now = utc_timestamp()
        for approval in approvals:
            if not isinstance(approval, dict):
                raise ValidationError("legacy agent approval record is malformed")
            if approval.get("schema") != APPROVAL_SCHEMA or approval.get(
                "version"
            ) not in {1, APPROVAL_VERSION}:
                raise ValidationError("unsupported agent approval schema/version")
            approval_tool_call_id = approval.get("tool_call_id")
            if not isinstance(approval_tool_call_id, str):
                raise ValidationError("legacy approval tool identity is malformed")
            tool = tool_by_id.get(approval_tool_call_id)
            if tool is None:
                raise ValidationError("legacy approval references an unknown tool")
            if approval.get("version") == 1:
                approval["tool_name"] = tool.get("tool_name")
                approval["effect"] = tool.get("effect")
                approval["risk"] = tool.get("risk")
                approval["version"] = APPROVAL_VERSION
                if approval.get("status") == ApprovalStatus.PENDING.value:
                    legacy_pending.append((approval, tool))

        if legacy_pending:
            if len(legacy_pending) != 1:
                raise ValidationError("legacy turn contains multiple pending approvals")
            approval, tool = legacy_pending[0]
            reason = "legacy approval lacked an exact authority binding; submit the action again"
            approval.update(
                {
                    "status": ApprovalStatus.DENIED.value,
                    "decided_at": migration_now,
                    "decision_source": "schema_migration",
                    "reason": reason,
                }
            )
            tool.update(
                {
                    "status": ToolRunStatus.DENIED.value,
                    "started_at": tool.get("started_at") or migration_now,
                    "completed_at": migration_now,
                    "dispatch_started_at": None,
                    "after_status": None,
                    "after_revision": None,
                    "result": None,
                    "error": None,
                }
            )
            upgraded.update(
                {
                    "status": TurnStatus.CANCELLED.value,
                    "record_revision": int(upgraded.get("record_revision", 0)) + 1,
                    "updated_at": migration_now,
                    "started_at": upgraded.get("started_at") or migration_now,
                    "completed_at": migration_now,
                    "stop_reason": reason,
                    "error": None,
                }
            )

        upgraded["version"] = TURN_VERSION
        upgraded["sequence"] = sequence
        return upgraded

    def _load_index_next_sequence_unlocked(self) -> int | None:
        if not self.index_path.exists() and not self.index_path.is_symlink():
            return None
        if self.index_path.is_symlink() or not self.index_path.is_file():
            raise ValidationError("agent turn index is malformed")
        value = load_json_limited(self.index_path, 64 * 1024)
        fields = {"schema", "version", "next_sequence"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValidationError("agent turn index is malformed")
        if (
            value["schema"] != TURN_INDEX_SCHEMA
            or value["version"] != TURN_INDEX_VERSION
        ):
            raise ValidationError("unsupported agent turn index schema/version")
        candidate = value["next_sequence"]
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or candidate < 1
        ):
            raise ValidationError("agent turn index sequence is malformed")
        return candidate

    def _allocate_sequence_unlocked(self) -> int:
        """Reserve a monotonic project-local sequence; gaps are crash-safe."""

        next_sequence = self._load_index_next_sequence_unlocked() or 1
        atomic_write_json(
            self.index_path,
            {
                "schema": TURN_INDEX_SCHEMA,
                "version": TURN_INDEX_VERSION,
                "next_sequence": next_sequence + 1,
            },
        )
        return next_sequence

    def _turn_path(self, turn_id: str) -> Path:
        _validate_identity(turn_id, "turn id", path_safe=True)
        return self.turns_root / f"{turn_id}.json"

    def _load_unlocked(self, turn_id: str) -> TurnRecord:
        self._ensure_current_schema_unlocked()
        path = self._turn_path(turn_id)
        if path.is_symlink() or not path.is_file():
            raise ValidationError("agent turn does not exist")
        return self._load_path_unlocked(path)

    def _load_path_unlocked(self, path: Path) -> TurnRecord:
        record = TurnRecord.from_dict(load_json_limited(path, TURN_FILE_LIMIT))
        if record.project_id != self.project_id or record.turn_id != path.stem:
            raise ValidationError("agent turn record identity is malformed")
        return record

    def _write_unlocked(self, record: TurnRecord) -> None:
        if record.project_id != self.project_id:
            raise ValidationError("turn project id does not match its store")
        document = record.to_dict()
        try:
            rendered = json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError("agent turn record is not JSON serializable") from exc
        if len((rendered + "\n").encode("utf-8")) > TURN_FILE_LIMIT:
            raise ValidationError("agent turn record exceeds its 4 MiB limit")
        atomic_write_json(self._turn_path(record.turn_id), document)

    @staticmethod
    def _check_record_revision(
        record: TurnRecord, expected_record_revision: int | None
    ) -> None:
        if expected_record_revision is None:
            return
        _validate_revision(expected_record_revision, "expected turn record revision")
        if record.record_revision != expected_record_revision:
            raise ValidationError("agent turn record revision changed concurrently")

    @staticmethod
    def _find_tool(record: TurnRecord, tool_call_id: str) -> tuple[int, ToolRunRecord]:
        _validate_identity(tool_call_id, "tool-call id")
        for index, tool in enumerate(record.tool_runs):
            if tool.tool_call_id == tool_call_id:
                return index, tool
        raise ValidationError("turn does not contain that tool call")

    @staticmethod
    def _normalize_statuses(
        statuses: Iterable[TurnStatus | str] | None,
    ) -> frozenset[TurnStatus] | None:
        if statuses is None:
            return None
        try:
            result = frozenset(
                _enum_value(TurnStatus, status, "turn status") for status in statuses
            )
        except TypeError as exc:
            raise ValidationError("turn statuses must be iterable") from exc
        if not result:
            raise ValidationError("turn statuses filter cannot be empty")
        return result
