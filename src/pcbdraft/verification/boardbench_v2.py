"""BoardBench v2 run receipts and evidence normalization.

The v2 receipt is the authoritative on-disk run record for newly initialized
campaign runs.  Legacy v1 receipts remain read-only and are projected into an
explicit in-memory compatibility view; this module never rewrites them.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited, read_text_limited
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services.progress import (
    EngineeringStage,
    ProcessStatus,
    ProductSessionTerminalReceipt,
    TaskOutcome,
)
from pcbdraft.verification.boardbench import (
    MAX_INVENTORY_BYTES,
    MAX_INVENTORY_FILES,
    MAX_TEXT_BYTES,
    BoardBenchCampaign,
    BoardBenchRun,
    InventoryEntry,
    _artifact_lock_parent,
    _load_document,
    _prepare_artifact_target,
    _verify_artifact_parent,
)

RUN_V2_SCHEMA = "pcbdraft-boardbench-run-v2"
RUN_V2_VERSION = 2
TRACE_MEMBER_LIMIT = 32 * 1024 * 1024

MODEL_TURN_LIMIT = 90
PCB_TOOL_CALL_LIMIT = 500
WALL_TIME_LIMIT_SECONDS = 3600.0

RUN_STATES = frozenset({"planned", "running", "terminal"})
BUDGET_STATUSES = frozenset({"within_limit", "observed", "exhausted", "unknown"})
EVIDENCE_STATUSES = frozenset({"reported", "partial", "unknown"})
OUTCOME_SOURCES = frozenset(
    {"product_session_terminal", "trace_terminal", "worker_fallback"}
)
TERMINATION_REASONS = frozenset(
    {
        "release_gate_passed",
        "agent_returned_before_gate",
        "no_progress",
        "strategy_required",
        "human_intervention_required",
        "unsupported_requirement",
        "tool_failure",
        "crashed",
        "cancelled",
        "configuration_drift",
    }
)
BUDGET_NAMES = (
    "model_turns",
    "pcb_tool_calls",
    "route_attempts",
    "route_node_expansions",
    "uncached_input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "wall_time",
)

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_BUDGET_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_BUDGET_REASON = re.compile(r"budget_exhausted:([a-z][a-z0-9_]{0,63})")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
_TRACE_MEMBER = re.compile(r"agent-trace\.jsonl(?:\.([1-9][0-9]*))?")


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValidationError(f"{label} is invalid")
    return value


def _timestamp(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValidationError(f"{label} is invalid")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    return value


def _timestamp_value(value: str) -> datetime:
    """Return the already-validated UTC timestamp's chronological value."""

    return datetime.fromisoformat(value)


def _number(value: object, label: str, *, optional: bool = False) -> float | None:
    if optional and value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValidationError(f"{label} must be a non-negative finite number")
    return float(value)


def _integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _signed_integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label} must be an integer")
    return value


def _optional_text(
    value: object, label: str, *, limit: int = MAX_TEXT_BYTES
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    if len(encoded) > limit:
        raise ValidationError(f"{label} is invalid")
    return value


def _closed(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"{label} has unexpected fields")
    return value


@dataclass(frozen=True)
class BudgetDimension:
    """One independently named limit and its observed consumption."""

    name: str
    limit: float | None
    consumed: float | None
    unit: str
    source: str
    status: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _BUDGET_NAME.fullmatch(self.name) is None:
            raise ValidationError("budget dimension name is invalid")
        _number(self.limit, "budget limit", optional=True)
        _number(self.consumed, "budget consumption", optional=True)
        _optional_text(self.unit, "budget dimension unit", limit=32)
        _optional_text(self.source, "budget dimension source", limit=128)
        if self.status not in BUDGET_STATUSES:
            raise ValidationError("budget dimension status is invalid")
        if self.status == "unknown" and self.consumed is not None:
            raise ValidationError("unknown budget consumption must be null")
        if self.status == "observed" and (
            self.limit is not None or self.consumed is None
        ):
            raise ValidationError("observed budget needs consumption and no limit")
        if self.status == "within_limit" and (
            self.limit is None or self.consumed is None or self.consumed > self.limit
        ):
            raise ValidationError("within-limit budget values are inconsistent")
        if self.status == "exhausted" and (
            self.limit is None
            or (self.consumed is not None and self.consumed < self.limit)
        ):
            raise ValidationError("exhausted budget values are inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "limit": self.limit,
            "consumed": self.consumed,
            "unit": self.unit,
            "source": self.source,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {"name", "limit", "consumed", "unit", "source", "status"},
            path,
        )
        return cls(
            name=_identity(item["name"], f"{path}.name"),
            limit=_number(item["limit"], f"{path}.limit", optional=True),
            consumed=_number(item["consumed"], f"{path}.consumed", optional=True),
            unit=_optional_text(item["unit"], f"{path}.unit", limit=32) or "",
            source=_optional_text(item["source"], f"{path}.source", limit=128) or "",
            status=_optional_text(item["status"], f"{path}.status", limit=32) or "",
        )


@dataclass(frozen=True)
class ActualCostEvidence:
    value: float | None
    currency: str | None
    status: str
    source: str

    def __post_init__(self) -> None:
        _number(self.value, "actual cost", optional=True)
        _optional_text(self.currency, "actual cost currency", limit=16)
        if self.status not in {"reported", "unknown"}:
            raise ValidationError("actual cost status is invalid")
        _optional_text(self.source, "actual cost source", limit=128)
        if self.status == "reported":
            if self.value is None or self.value <= 0 or self.currency is None:
                raise ValidationError("reported actual cost needs value and currency")
        elif self.value is not None or self.currency is not None:
            raise ValidationError("unknown actual cost cannot contain priced evidence")

    @classmethod
    def unknown(cls, source: str = "unavailable") -> Self:
        return cls(None, None, "unknown", source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "currency": self.currency,
            "status": self.status,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"value", "currency", "status", "source"}, path)
        return cls(
            _number(item["value"], f"{path}.value", optional=True),
            _optional_text(item["currency"], f"{path}.currency", limit=16),
            _optional_text(item["status"], f"{path}.status", limit=32) or "",
            _optional_text(item["source"], f"{path}.source", limit=128) or "",
        )


@dataclass(frozen=True)
class ContextQualityEvidence:
    peak_active_tokens: int | None
    peak_repeated_ratio: float | None
    newly_added_tokens: int | None
    status: str
    source: str

    def __post_init__(self) -> None:
        _integer(self.peak_active_tokens, "peak active context", optional=True)
        ratio = _number(
            self.peak_repeated_ratio, "peak repeated-content ratio", optional=True
        )
        if ratio is not None and ratio > 1:
            raise ValidationError("peak repeated-content ratio exceeds one")
        _integer(self.newly_added_tokens, "new context tokens", optional=True)
        if self.status not in EVIDENCE_STATUSES:
            raise ValidationError("context quality status is invalid")
        _optional_text(self.source, "context quality source", limit=128)
        values = (
            self.peak_active_tokens,
            self.peak_repeated_ratio,
            self.newly_added_tokens,
        )
        if self.status == "unknown" and any(item is not None for item in values):
            raise ValidationError("unknown context quality cannot contain values")
        if self.status == "reported" and any(item is None for item in values):
            raise ValidationError("reported context quality needs every value")

    @classmethod
    def unknown(cls, source: str = "unavailable") -> Self:
        return cls(None, None, None, "unknown", source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peak_active_tokens": self.peak_active_tokens,
            "peak_repeated_ratio": self.peak_repeated_ratio,
            "newly_added_tokens": self.newly_added_tokens,
            "status": self.status,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "peak_active_tokens",
                "peak_repeated_ratio",
                "newly_added_tokens",
                "status",
                "source",
            },
            path,
        )
        return cls(
            _integer(
                item["peak_active_tokens"],
                f"{path}.peak_active_tokens",
                optional=True,
            ),
            _number(
                item["peak_repeated_ratio"],
                f"{path}.peak_repeated_ratio",
                optional=True,
            ),
            _integer(
                item["newly_added_tokens"],
                f"{path}.newly_added_tokens",
                optional=True,
            ),
            _optional_text(item["status"], f"{path}.status", limit=32) or "",
            _optional_text(item["source"], f"{path}.source", limit=128) or "",
        )


@dataclass(frozen=True)
class BoardBenchRunV2:
    """One immutable-identity run with truthful lifecycle and PCB outcome."""

    campaign_id: str
    run_id: str
    case_id: str
    repetition: int
    prompt_sha256: str
    run_state: str
    started_at: str | None
    completed_at: str | None
    process_status: ProcessStatus | None
    task_outcome: TaskOutcome | None
    termination_reason: str | None
    stage_reached: EngineeringStage | None
    release_gate_passed: bool | None
    outcome_source: str | None
    worker_exit_code: int | None
    final_response: str | None
    budgets: tuple[BudgetDimension, ...]
    actual_cost: ActualCostEvidence
    context_quality: ContextQualityEvidence
    inventory: tuple[InventoryEntry, ...]

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "v2 run campaign id")
        _identity(self.run_id, "v2 run id")
        _identity(self.case_id, "v2 run case id")
        if isinstance(self.repetition, bool) or self.repetition not in {1, 2, 3}:
            raise ValidationError("v2 run repetition is invalid")
        if (
            not isinstance(self.prompt_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.prompt_sha256) is None
        ):
            raise ValidationError("v2 run prompt hash is invalid")
        if self.run_state not in RUN_STATES:
            raise ValidationError("v2 run state is invalid")
        _timestamp(self.started_at, "v2 run started_at", optional=True)
        _timestamp(self.completed_at, "v2 run completed_at", optional=True)
        _signed_integer(self.worker_exit_code, "v2 worker exit code", optional=True)
        _optional_text(self.termination_reason, "v2 termination reason", limit=256)
        _optional_text(self.final_response, "v2 final response")
        if (
            len(self.budgets) != len(BUDGET_NAMES)
            or tuple(item.name for item in self.budgets) != BUDGET_NAMES
        ):
            raise ValidationError(
                "v2 run budget dimensions are incomplete or unordered"
            )
        if (
            len(self.inventory) > MAX_INVENTORY_FILES
            or sum(item.size_bytes for item in self.inventory) > MAX_INVENTORY_BYTES
        ):
            raise ValidationError("v2 run inventory exceeds its limit")
        if len({item.path for item in self.inventory}) != len(self.inventory):
            raise ValidationError("v2 run inventory contains duplicate paths")

        terminal_values = (
            self.process_status,
            self.task_outcome,
            self.termination_reason,
            self.release_gate_passed,
            self.outcome_source,
        )
        if self.run_state == "planned":
            if (
                self.started_at is not None
                or self.completed_at is not None
                or any(value is not None for value in terminal_values)
                or self.stage_reached is not None
                or self.worker_exit_code is not None
                or self.final_response is not None
                or self.inventory
                or any(item.consumed is not None for item in self.budgets)
                or self.actual_cost.status != "unknown"
                or self.context_quality.status != "unknown"
            ):
                raise ValidationError(
                    "planned v2 run already contains execution evidence"
                )
            return
        if self.run_state == "running":
            if (
                self.started_at is None
                or self.completed_at is not None
                or any(value is not None for value in terminal_values)
                or self.stage_reached is not None
                or self.worker_exit_code is not None
                or self.final_response is not None
                or self.inventory
                or any(item.consumed is not None for item in self.budgets)
                or self.actual_cost.status != "unknown"
                or self.context_quality.status != "unknown"
            ):
                raise ValidationError("running v2 run evidence is malformed")
            return
        if (
            self.started_at is None
            or self.completed_at is None
            or any(value is None for value in terminal_values)
            or not self.inventory
            or not isinstance(self.process_status, ProcessStatus)
            or not isinstance(self.task_outcome, TaskOutcome)
            or not isinstance(self.release_gate_passed, bool)
            or self.outcome_source not in OUTCOME_SOURCES
        ):
            raise ValidationError("terminal v2 run evidence is incomplete")
        if _timestamp_value(self.completed_at) < _timestamp_value(self.started_at):
            raise ValidationError("v2 run completed_at precedes started_at")
        if self.termination_reason not in TERMINATION_REASONS and (
            not isinstance(self.termination_reason, str)
            or _BUDGET_REASON.fullmatch(self.termination_reason) is None
        ):
            raise ValidationError("v2 termination reason is invalid")
        self._validate_terminal_combination()

    def _validate_terminal_combination(self) -> None:
        if (
            self.process_status is None
            or self.task_outcome is None
            or self.termination_reason is None
            or self.release_gate_passed is None
        ):
            raise ValidationError("terminal v2 run evidence is incomplete")
        release = (
            self.release_gate_passed,
            self.task_outcome is TaskOutcome.PASSED,
            self.termination_reason == "release_gate_passed",
            self.stage_reached is EngineeringStage.RELEASE_GATE,
        )
        if any(release) and not all(release):
            raise ValidationError("v2 release-gate outcome is inconsistent")
        if self.release_gate_passed and self.process_status is not ProcessStatus.EXITED:
            raise ValidationError("v2 release-gate process status is inconsistent")
        if self.release_gate_passed and self.worker_exit_code not in {None, 0}:
            raise ValidationError("v2 release-gate worker status is inconsistent")
        if (
            self.process_status is ProcessStatus.EXITED
            and self.worker_exit_code is not None
            and self.worker_exit_code != 0
        ):
            raise ValidationError("v2 exited-process worker status is inconsistent")
        if self.process_status is ProcessStatus.CRASHED and (
            self.task_outcome is not TaskOutcome.FAILED
            or self.termination_reason != "crashed"
        ):
            raise ValidationError("v2 crash outcome is inconsistent")
        if self.process_status is ProcessStatus.CANCELLED and (
            self.task_outcome is not TaskOutcome.INCOMPLETE
            or self.termination_reason != "cancelled"
        ):
            raise ValidationError("v2 cancellation outcome is inconsistent")
        if self.process_status is ProcessStatus.TIMED_OUT and (
            self.task_outcome is not TaskOutcome.INCOMPLETE
            or self.termination_reason != "budget_exhausted:wall_time"
        ):
            raise ValidationError("v2 timeout outcome is inconsistent")
        if self.process_status is ProcessStatus.EXITED and self.termination_reason in {
            "crashed",
            "cancelled",
            "budget_exhausted:wall_time",
        }:
            raise ValidationError("v2 exited-process outcome is inconsistent")
        blocked_reasons = {
            "no_progress",
            "strategy_required",
            "human_intervention_required",
            "unsupported_requirement",
        }
        expected = (
            TaskOutcome.PASSED
            if self.termination_reason == "release_gate_passed"
            else TaskOutcome.BLOCKED
            if self.termination_reason in blocked_reasons
            else TaskOutcome.FAILED
            if self.termination_reason
            in {"tool_failure", "crashed", "configuration_drift"}
            else TaskOutcome.INCOMPLETE
        )
        if self.task_outcome is not expected:
            raise ValidationError("v2 task outcome is inconsistent")
        if self.task_outcome is TaskOutcome.PASSED and any(
            item.status == "exhausted" for item in self.budgets
        ):
            raise ValidationError(
                "v2 passed outcome cannot contain an exhausted budget"
            )
        exhausted = _BUDGET_REASON.fullmatch(self.termination_reason)
        exhausted_budgets = tuple(
            item.name for item in self.budgets if item.status == "exhausted"
        )
        if exhausted is None and exhausted_budgets:
            raise ValidationError(
                "v2 exhausted budget lacks a budget-exhausted termination reason"
            )
        if exhausted is not None:
            dimension = next(
                (item for item in self.budgets if item.name == exhausted.group(1)), None
            )
            if dimension is None or dimension.status != "exhausted":
                raise ValidationError(
                    "v2 exhausted reason lacks matching budget evidence"
                )

    @property
    def terminal(self) -> bool:
        return self.run_state == "terminal"

    @property
    def status(self) -> str:
        """Legacy process-oriented status for compatibility-only callers."""

        return self.to_legacy().status

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_V2_SCHEMA,
            "version": RUN_V2_VERSION,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "repetition": self.repetition,
            "prompt_sha256": self.prompt_sha256,
            "run_state": self.run_state,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "process_status": (
                self.process_status.value if self.process_status is not None else None
            ),
            "task_outcome": (
                self.task_outcome.value if self.task_outcome is not None else None
            ),
            "termination_reason": self.termination_reason,
            "stage_reached": (
                self.stage_reached.value if self.stage_reached is not None else None
            ),
            "release_gate_passed": self.release_gate_passed,
            "outcome_source": self.outcome_source,
            "worker_exit_code": self.worker_exit_code,
            "final_response": self.final_response,
            "budgets": [item.to_dict() for item in self.budgets],
            "actual_cost": self.actual_cost.to_dict(),
            "context_quality": self.context_quality.to_dict(),
            "inventory": [item.to_dict() for item in self.inventory],
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "schema",
            "version",
            "campaign_id",
            "run_id",
            "case_id",
            "repetition",
            "prompt_sha256",
            "run_state",
            "started_at",
            "completed_at",
            "process_status",
            "task_outcome",
            "termination_reason",
            "stage_reached",
            "release_gate_passed",
            "outcome_source",
            "worker_exit_code",
            "final_response",
            "budgets",
            "actual_cost",
            "context_quality",
            "inventory",
        }
        item = _closed(value, fields, path)
        if item["schema"] != RUN_V2_SCHEMA or item["version"] != RUN_V2_VERSION:
            raise ValidationError("unsupported BoardBench run v2 schema/version")
        try:
            process_status = (
                ProcessStatus(item["process_status"])
                if item["process_status"] is not None
                else None
            )
            task_outcome = (
                TaskOutcome(item["task_outcome"])
                if item["task_outcome"] is not None
                else None
            )
            stage = (
                EngineeringStage(item["stage_reached"])
                if item["stage_reached"] is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("BoardBench run v2 enums are invalid") from exc
        budgets = item["budgets"]
        inventory = item["inventory"]
        if not isinstance(budgets, list) or not isinstance(inventory, list):
            raise ValidationError("BoardBench run v2 arrays are invalid")
        release = item["release_gate_passed"]
        if release is not None and not isinstance(release, bool):
            raise ValidationError("BoardBench run v2 release gate is invalid")
        return cls(
            campaign_id=_identity(item["campaign_id"], f"{path}.campaign_id"),
            run_id=_identity(item["run_id"], f"{path}.run_id"),
            case_id=_identity(item["case_id"], f"{path}.case_id"),
            repetition=_integer(item["repetition"], f"{path}.repetition") or 0,
            prompt_sha256=_optional_text(
                item["prompt_sha256"], f"{path}.prompt_sha256", limit=64
            )
            or "",
            run_state=_optional_text(item["run_state"], f"{path}.run_state", limit=16)
            or "",
            started_at=_timestamp(
                item["started_at"], f"{path}.started_at", optional=True
            ),
            completed_at=_timestamp(
                item["completed_at"], f"{path}.completed_at", optional=True
            ),
            process_status=process_status,
            task_outcome=task_outcome,
            termination_reason=_optional_text(
                item["termination_reason"], f"{path}.termination_reason", limit=256
            ),
            stage_reached=stage,
            release_gate_passed=release,
            outcome_source=_optional_text(
                item["outcome_source"], f"{path}.outcome_source", limit=64
            ),
            worker_exit_code=_signed_integer(
                item["worker_exit_code"], f"{path}.worker_exit_code", optional=True
            ),
            final_response=_optional_text(
                item["final_response"], f"{path}.final_response"
            ),
            budgets=tuple(
                BudgetDimension.from_dict(entry, f"{path}.budgets[{index}]")
                for index, entry in enumerate(budgets)
            ),
            actual_cost=ActualCostEvidence.from_dict(
                item["actual_cost"], f"{path}.actual_cost"
            ),
            context_quality=ContextQualityEvidence.from_dict(
                item["context_quality"], f"{path}.context_quality"
            ),
            inventory=tuple(
                InventoryEntry.from_dict(entry, f"{path}.inventory[{index}]")
                for index, entry in enumerate(inventory)
            ),
        )

    def to_legacy(self) -> BoardBenchRun:
        if self.run_state == "planned":
            status = "planned"
        elif self.run_state == "running":
            status = "running"
        elif self.process_status is ProcessStatus.TIMED_OUT:
            status = "timed_out"
        elif self.process_status is ProcessStatus.CANCELLED:
            status = "interrupted"
        elif self.termination_reason == "configuration_drift":
            status = "configuration_drift"
        elif (
            self.process_status is ProcessStatus.CRASHED or self.final_response is None
        ):
            status = "failed"
        else:
            status = "completed"
        return BoardBenchRun(
            campaign_id=self.campaign_id,
            run_id=self.run_id,
            case_id=self.case_id,
            repetition=self.repetition,
            prompt_sha256=self.prompt_sha256,
            status=status,
            started_at=self.started_at,
            completed_at=self.completed_at,
            termination_reason=self.termination_reason,
            final_response=self.final_response,
            inventory=self.inventory,
        )


@dataclass(frozen=True)
class NormalizedBoardBenchRun:
    """Read model shared by v1 and v2 without guessing absent v1 facts."""

    campaign_id: str
    run_id: str
    case_id: str
    repetition: int
    source_schema: str
    source_version: int
    process_status: ProcessStatus | None
    task_outcome: TaskOutcome | None
    termination_reason: str | None
    stage_reached: EngineeringStage | None
    release_gate_passed: bool | None
    budgets: tuple[BudgetDimension, ...]


def _unknown_budgets(
    *,
    model_turn_limit: float | None = None,
    pcb_tool_call_limit: float | None = None,
    wall_limit: float | None = None,
) -> tuple[BudgetDimension, ...]:
    limits: dict[str, float | None] = {
        "model_turns": model_turn_limit,
        "pcb_tool_calls": pcb_tool_call_limit,
        "route_attempts": None,
        "route_node_expansions": None,
        "uncached_input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "wall_time": wall_limit,
    }
    units = {
        "model_turns": "turn",
        "pcb_tool_calls": "call",
        "route_attempts": "attempt",
        "route_node_expansions": "node",
        "uncached_input_tokens": "token",
        "output_tokens": "token",
        "cache_read_tokens": "token",
        "wall_time": "second",
    }
    return tuple(
        BudgetDimension(name, limits[name], None, units[name], "unavailable", "unknown")
        for name in BUDGET_NAMES
    )


def _run_matches_campaign_plan(
    run: BoardBenchRun, campaign: BoardBenchCampaign
) -> bool:
    planned = next((item for item in campaign.runs if item.run_id == run.run_id), None)
    return (
        run.campaign_id == campaign.campaign_id
        and planned is not None
        and planned.case_id == run.case_id
        and planned.repetition == run.repetition
    )


def planned_run_v2(
    legacy: BoardBenchRun, campaign: BoardBenchCampaign
) -> BoardBenchRunV2:
    if legacy.status != "planned":
        raise ValidationError("v2 initialization requires a planned run")
    if not _run_matches_campaign_plan(legacy, campaign):
        raise ValidationError("v2 initialization run differs from its campaign plan")
    return BoardBenchRunV2(
        legacy.campaign_id,
        legacy.run_id,
        legacy.case_id,
        legacy.repetition,
        legacy.prompt_sha256,
        "planned",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        _unknown_budgets(
            model_turn_limit=MODEL_TURN_LIMIT,
            pcb_tool_call_limit=campaign.tool_call_budget,
            wall_limit=campaign.wall_timeout_seconds,
        ),
        ActualCostEvidence.unknown(),
        ContextQualityEvidence.unknown(),
        (),
    )


def start_run_v2(run: BoardBenchRunV2, started_at: str) -> BoardBenchRunV2:
    if run.run_state != "planned":
        raise ValidationError("only a planned v2 run can start")
    return replace(run, run_state="running", started_at=started_at)


def normalize_v1_run(run: BoardBenchRun) -> NormalizedBoardBenchRun:
    """Project v1 without inferring task or gate facts from `completed`."""

    return NormalizedBoardBenchRun(
        run.campaign_id,
        run.run_id,
        run.case_id,
        run.repetition,
        run.schema,
        1,
        None,
        None,
        run.termination_reason,
        None,
        None,
        _unknown_budgets(),
    )


def normalize_v2_run(run: BoardBenchRunV2) -> NormalizedBoardBenchRun:
    return NormalizedBoardBenchRun(
        run.campaign_id,
        run.run_id,
        run.case_id,
        run.repetition,
        RUN_V2_SCHEMA,
        RUN_V2_VERSION,
        run.process_status,
        run.task_outcome,
        run.termination_reason,
        run.stage_reached,
        run.release_gate_passed,
        run.budgets,
    )


def _load_json(path: Path) -> dict[str, Any]:
    return _load_document(path)


def load_run_v2(path: str | Path) -> BoardBenchRunV2:
    target = Path(path).expanduser()
    value = _load_json(target)
    if value.get("schema") != RUN_V2_SCHEMA:
        raise ValidationError("legacy BoardBench v1 run is read-only")
    return BoardBenchRunV2.from_dict(value)


def load_normalized_run(path: str | Path) -> NormalizedBoardBenchRun:
    target = Path(path).expanduser()
    value = _load_json(target)
    if value.get("schema") == RUN_V2_SCHEMA:
        return normalize_v2_run(BoardBenchRunV2.from_dict(value))
    return normalize_v1_run(BoardBenchRun.from_dict(value))


def store_run_v2(path: str | Path, run: BoardBenchRunV2) -> Path:
    """Write a v2 lifecycle transition; terminal receipts are immutable."""

    target, parent_identity = _prepare_artifact_target(path)
    lock_parent = _artifact_lock_parent(target)
    with ResourceLock(target, lock_parent, timeout=10.0):
        _verify_artifact_parent(target, parent_identity)
        if target.exists():
            current = load_run_v2(target)
            if current.terminal:
                raise ValidationError("terminal BoardBench run v2 is immutable")
            if (
                current.campaign_id,
                current.run_id,
                current.case_id,
                current.repetition,
                current.prompt_sha256,
            ) != (
                run.campaign_id,
                run.run_id,
                run.case_id,
                run.repetition,
                run.prompt_sha256,
            ):
                raise ValidationError(
                    "run v2 transition changes its immutable identity"
                )
            allowed = {"planned": {"running"}, "running": {"terminal"}}
            if run.run_state not in allowed[current.run_state]:
                raise ValidationError("invalid BoardBench run v2 transition")
        elif run.run_state != "planned":
            raise ValidationError("a new BoardBench run v2 must be planned")
        atomic_write_json(target, run.to_dict(), mode=0o600)
        _verify_artifact_parent(target, parent_identity)
        return target


def validate_campaign_denominator(
    campaign: BoardBenchCampaign,
    runs: Sequence[BoardBenchRun | BoardBenchRunV2 | NormalizedBoardBenchRun],
) -> None:
    """Require exactly the immutable 60 planned identities, never substitutes."""

    expected = {plan.run_id: (plan.case_id, plan.repetition) for plan in campaign.runs}
    observed: dict[str, tuple[str, int]] = {}
    for run in runs:
        if run.run_id in observed:
            raise ValidationError("BoardBench denominator contains a duplicate run id")
        observed[run.run_id] = (run.case_id, run.repetition)
    if observed != expected:
        raise ValidationError(
            "BoardBench denominator differs from the immutable campaign plan"
        )


@dataclass(frozen=True)
class _TraceEvidence:
    events: tuple[Mapping[str, Any], ...]
    gap: bool
    model_turns: int | None
    pcb_tool_calls: int | None
    route_attempts: int | None
    route_node_expansions: int | None
    uncached_input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    actual_cost: ActualCostEvidence
    context_quality: ContextQualityEvidence


def _trace_sort_key(path: Path) -> tuple[int, str]:
    match = _TRACE_MEMBER.fullmatch(path.name)
    return (-(int(match.group(1) or 0)), path.name) if match else (0, path.name)


def _trace_events(trace_root: Path) -> tuple[tuple[Mapping[str, Any], ...], bool]:
    if trace_root.is_symlink() or not trace_root.is_dir():
        return (), True
    members = sorted(
        (
            path
            for path in trace_root.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and _TRACE_MEMBER.fullmatch(path.name)
        ),
        key=_trace_sort_key,
    )
    events: list[Mapping[str, Any]] = []
    sequences: list[int] = []
    malformed = not members
    for member in members:
        try:
            lines = read_text_limited(member, TRACE_MEMBER_LIMIT).splitlines()
        except PCBDraftError:
            malformed = True
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                malformed = True
                continue
            if (
                not isinstance(value, dict)
                or isinstance(value.get("seq"), bool)
                or not isinstance(value.get("seq"), int)
            ):
                malformed = True
                continue
            sequences.append(value["seq"])
            events.append(value)
    if sequences:
        malformed = malformed or sequences != list(range(1, sequences[-1] + 1))
    else:
        malformed = True
    return tuple(events), malformed


def _nonnegative_int(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _load_usage(path: Path) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        value = load_json_limited(path, 1024 * 1024)
    except PCBDraftError:
        return {}
    return value if isinstance(value, Mapping) else {}


def _expanded_nodes(value: object, *, depth: int = 0) -> tuple[int, bool]:
    if depth > 6:
        return 0, False
    if isinstance(value, str):
        try:
            return _expanded_nodes(json.loads(value), depth=depth + 1)
        except (json.JSONDecodeError, RecursionError):
            return 0, False
    if isinstance(value, Mapping):
        total = 0
        found = False
        direct = _nonnegative_int(value.get("expanded_nodes"))
        if direct is not None:
            total += direct
            found = True
        for key, item in list(value.items())[:128]:
            if key == "expanded_nodes":
                continue
            child, present = _expanded_nodes(item, depth=depth + 1)
            total += child
            found = found or present
        return total, found
    if isinstance(value, (list, tuple)):
        total = 0
        found = False
        for item in value[:128]:
            child, present = _expanded_nodes(item, depth=depth + 1)
            total += child
            found = found or present
        return total, found
    return 0, False


def _actual_cost(
    events: Sequence[Mapping[str, Any]], usage: Mapping[str, Any]
) -> ActualCostEvidence:
    response_costs: list[tuple[float, str, str]] = []
    responses = 0
    for event in events:
        if event.get("event") != "model_response":
            continue
        responses += 1
        data = event.get("data")
        metrics = data.get("cost_metrics") if isinstance(data, Mapping) else None
        if (
            not isinstance(metrics, Mapping)
            or metrics.get("actual_cost_status") != "reported"
        ):
            continue
        amount = _nonnegative_float(metrics.get("actual_cost_value"))
        currency = metrics.get("actual_cost_currency")
        source = metrics.get("cost_source")
        if (
            amount is not None
            and amount > 0
            and isinstance(currency, str)
            and isinstance(source, str)
        ):
            response_costs.append((amount, currency, source))
    if responses and len(response_costs) == responses:
        currencies = {item[1] for item in response_costs}
        sources = {item[2] for item in response_costs}
        if len(currencies) == 1:
            return ActualCostEvidence(
                sum(item[0] for item in response_costs),
                next(iter(currencies)),
                "reported",
                next(iter(sources)) if len(sources) == 1 else "mixed_provider_usage",
            )
    raw_status = usage.get("cost_status")
    amount = _nonnegative_float(usage.get("actual_cost_usd", usage.get("cost_amount")))
    currency = (
        "USD"
        if usage.get("actual_cost_usd") is not None
        else usage.get("cost_currency")
    )
    if raw_status in {"actual", "reported"} and amount and isinstance(currency, str):
        source = usage.get("cost_source")
        return ActualCostEvidence(
            amount,
            currency,
            "reported",
            source if isinstance(source, str) and source else "usage_receipt",
        )
    return ActualCostEvidence.unknown("provider_cost_unavailable")


def _context_evidence(events: Sequence[Mapping[str, Any]]) -> ContextQualityEvidence:
    requests = [item for item in events if item.get("event") == "model_request"]
    values: list[tuple[int, float, int]] = []
    for event in requests:
        data = event.get("data")
        context = data.get("context_quality") if isinstance(data, Mapping) else None
        if not isinstance(context, Mapping):
            continue
        active = _nonnegative_int(context.get("active_context_token_estimate"))
        repeated = _nonnegative_float(context.get("repeated_content_ratio"))
        added = _nonnegative_int(context.get("newly_added_tokens_estimate"))
        if (
            active is not None
            and repeated is not None
            and repeated <= 1
            and added is not None
        ):
            values.append((active, repeated, added))
    if not values:
        return ContextQualityEvidence.unknown("trace_context_unavailable")
    return ContextQualityEvidence(
        max(item[0] for item in values),
        max(item[1] for item in values),
        sum(item[2] for item in values),
        "reported" if len(values) == len(requests) else "partial",
        "trace_model_request",
    )


def collect_trace_evidence(artifacts: Path) -> _TraceEvidence:
    events, gap = _trace_events(artifacts / "trace")
    usage = _load_usage(artifacts / "usage.json")
    model_turns = (
        None if gap else sum(item.get("event") == "model_request" for item in events)
    )
    tool_events = [item for item in events if item.get("event") == "tool_end"]
    tool_starts = [item for item in events if item.get("event") == "tool_start"]
    pcb_events = []
    route_events = []
    counted_tool_events = tool_starts or tool_events
    for event in counted_tool_events:
        data = event.get("data")
        name = data.get("tool_name") if isinstance(data, Mapping) else None
        if isinstance(name, str) and name.startswith("pcb_"):
            pcb_events.append(event)
            if name == "pcb_route_net":
                route_events.append(event)
    pcb_calls = None if gap else len(pcb_events)
    for event in reversed(events):
        if event.get("event") != "pcb_tool_budget_exhausted":
            continue
        data = event.get("data")
        consumed = (
            _nonnegative_int(data.get("consumed"))
            if isinstance(data, Mapping)
            else None
        )
        if consumed is not None:
            pcb_calls = consumed
            break
    route_attempts = None if gap else len(route_events)
    expansions = 0
    expansion_complete = not gap
    route_results = []
    for event in tool_events:
        data = event.get("data")
        if isinstance(data, Mapping) and data.get("tool_name") == "pcb_route_net":
            route_results.append(event)
    for event in route_results:
        data = event.get("data")
        result = data.get("result") if isinstance(data, Mapping) else None
        amount, found = _expanded_nodes(result)
        expansions += amount
        expansion_complete = expansion_complete and found
    if route_attempts != len(route_results):
        expansion_complete = False
    route_nodes = expansions if expansion_complete else None

    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    cache_tokens = _nonnegative_int(usage.get("cache_read_tokens"))
    uncached = _nonnegative_int(usage.get("uncached_input_tokens"))
    if (
        uncached is None
        and input_tokens is not None
        and cache_tokens is not None
        and cache_tokens <= input_tokens
    ):
        uncached = input_tokens - cache_tokens
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    usage_calls = _nonnegative_int(usage.get("api_calls"))
    if model_turns is None and usage_calls is not None:
        model_turns = usage_calls
    return _TraceEvidence(
        events,
        gap,
        model_turns,
        pcb_calls,
        route_attempts,
        route_nodes,
        uncached,
        output_tokens,
        cache_tokens,
        _actual_cost(events, usage),
        _context_evidence(events),
    )


def _retained_product_terminal(
    repository: Path,
    events: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
) -> ProductSessionTerminalReceipt | None:
    """Load only the current run/session receipt named by matching trace evidence."""

    terminals = [
        item for item in events if item.get("event") == "product_session_terminal"
    ]
    for event in reversed(terminals):
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        project_id = data.get("project_id")
        artifact = data.get("artifact")
        session_id = data.get("session_id")
        turn_id = data.get("turn_id")
        if (
            not isinstance(project_id, str)
            or not project_id
            or not isinstance(artifact, str)
            or not artifact
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(turn_id, str)
            or not turn_id
        ):
            continue
        relative = Path(artifact)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "product-sessions"
            or relative.suffix != ".json"
        ):
            continue
        project = repository / "projects" / project_id
        candidate = project / relative
        if (
            project.is_symlink()
            or not project.is_dir()
            or candidate.parent.is_symlink()
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            continue
        try:
            project_record = load_json_limited(project / "project.json", 1024 * 1024)
            value = load_json_limited(candidate, 1024 * 1024)
            receipt = ProductSessionTerminalReceipt.from_dict(value)
        except PCBDraftError:
            continue
        if not isinstance(project_record, Mapping):
            continue
        current_revision = _nonnegative_int(project_record.get("design_revision"))
        if (
            project_record.get("id") != project_id
            or project_record.get("name") != run_id
            or current_revision != receipt.source_revision
            or receipt.project_id != project_id
            or receipt.session_id != session_id
            or receipt.turn_id != turn_id
            or artifact != f"product-sessions/{receipt.receipt_id}.json"
            or _TIMESTAMP.fullmatch(receipt.created_at) is None
            or not _timestamp_value(started_at)
            <= _timestamp_value(receipt.created_at)
            <= _timestamp_value(completed_at)
        ):
            continue
        trace_facts = (
            data.get("process_status"),
            data.get("task_outcome"),
            data.get("termination_reason"),
            data.get("stage_reached"),
            data.get("release_gate_passed"),
        )
        receipt_facts = (
            receipt.process_status.value,
            receipt.task_outcome.value,
            receipt.termination_reason,
            receipt.stage_reached.value,
            receipt.release_gate_passed,
        )
        if trace_facts != receipt_facts:
            continue
        conflicting_session_end = False
        if receipt.release_gate_passed:
            for later_event in events:
                if later_event.get("event") != "session_end":
                    continue
                end_data = later_event.get("data")
                if (
                    not isinstance(end_data, Mapping)
                    or end_data.get("session_id") != session_id
                    or end_data.get("turn_id") != turn_id
                ):
                    continue
                terminal = _session_end_terminal(end_data)
                if terminal is not None and terminal[2] != "agent_returned_before_gate":
                    conflicting_session_end = True
                    break
        if conflicting_session_end:
            continue
        return receipt
    return None


def _session_end_terminal(
    data: Mapping[str, Any],
) -> tuple[ProcessStatus, TaskOutcome, str, EngineeringStage | None, bool] | None:
    reason = str(data.get("turn_exit_reason", "")).casefold()
    if bool(data.get("interrupted")):
        return (
            ProcessStatus.CANCELLED,
            TaskOutcome.INCOMPLETE,
            "cancelled",
            None,
            False,
        )
    if "timeout" in reason:
        return (
            ProcessStatus.TIMED_OUT,
            TaskOutcome.INCOMPLETE,
            "budget_exhausted:wall_time",
            None,
            False,
        )
    if "max_iterations_reached" in reason or reason == "budget_exhausted":
        return (
            ProcessStatus.EXITED,
            TaskOutcome.INCOMPLETE,
            "budget_exhausted:model_turns",
            None,
            False,
        )
    if "tool" in reason and "budget" in reason:
        return (
            ProcessStatus.EXITED,
            TaskOutcome.INCOMPLETE,
            "budget_exhausted:pcb_tool_calls",
            None,
            False,
        )
    if "no_progress" in reason:
        return ProcessStatus.EXITED, TaskOutcome.BLOCKED, "no_progress", None, False
    if "strategy" in reason or "guardrail_halt" in reason or "blocked" in reason:
        return (
            ProcessStatus.EXITED,
            TaskOutcome.BLOCKED,
            "strategy_required",
            None,
            False,
        )
    if bool(data.get("failed")):
        return ProcessStatus.EXITED, TaskOutcome.FAILED, "tool_failure", None, False
    if bool(data.get("completed")):
        return (
            ProcessStatus.EXITED,
            TaskOutcome.INCOMPLETE,
            "agent_returned_before_gate",
            None,
            False,
        )
    return None


def _trace_terminal(
    events: Sequence[Mapping[str, Any]],
) -> tuple[ProcessStatus, TaskOutcome, str, EngineeringStage | None, bool] | None:
    # A trace event alone cannot prove native release facts.  Only the bound,
    # current, schema-validated artifact above may supply those facts.
    for event in reversed(events):
        if event.get("event") != "session_end":
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        terminal = _session_end_terminal(data)
        if terminal is not None:
            return terminal
    return None


def _fallback_terminal(
    status: str, reason: str
) -> tuple[ProcessStatus, TaskOutcome, str, EngineeringStage | None, bool]:
    folded = reason.casefold()
    if status == "timed_out" or "wall_timeout" in folded:
        return (
            ProcessStatus.TIMED_OUT,
            TaskOutcome.INCOMPLETE,
            "budget_exhausted:wall_time",
            None,
            False,
        )
    if status == "interrupted":
        return ProcessStatus.CANCELLED, TaskOutcome.INCOMPLETE, "cancelled", None, False
    if re.fullmatch(r"worker_exit_-?[0-9]+", folded):
        return ProcessStatus.CRASHED, TaskOutcome.FAILED, "crashed", None, False
    if "max_iterations_reached" in folded:
        return (
            ProcessStatus.EXITED,
            TaskOutcome.INCOMPLETE,
            "budget_exhausted:model_turns",
            None,
            False,
        )
    if "tool" in folded and "budget" in folded:
        return (
            ProcessStatus.EXITED,
            TaskOutcome.INCOMPLETE,
            "budget_exhausted:pcb_tool_calls",
            None,
            False,
        )
    if "no_progress" in folded:
        return ProcessStatus.EXITED, TaskOutcome.BLOCKED, "no_progress", None, False
    if "strategy" in folded or "blocked" in folded:
        return (
            ProcessStatus.EXITED,
            TaskOutcome.BLOCKED,
            "strategy_required",
            None,
            False,
        )
    if status == "configuration_drift":
        return (
            ProcessStatus.EXITED,
            TaskOutcome.FAILED,
            "configuration_drift",
            None,
            False,
        )
    if status == "completed":
        return (
            ProcessStatus.EXITED,
            TaskOutcome.INCOMPLETE,
            "agent_returned_before_gate",
            None,
            False,
        )
    return ProcessStatus.EXITED, TaskOutcome.FAILED, "tool_failure", None, False


def _dimension(
    name: str,
    limit: float | None,
    consumed: float | None,
    unit: str,
    source: str,
    exhausted_name: str | None,
) -> BudgetDimension:
    if exhausted_name == name:
        return BudgetDimension(name, limit, consumed, unit, source, "exhausted")
    if consumed is None:
        return BudgetDimension(name, limit, None, unit, source, "unknown")
    if limit is None:
        return BudgetDimension(name, None, consumed, unit, source, "observed")
    if consumed > limit:
        return BudgetDimension(name, limit, consumed, unit, source, "exhausted")
    return BudgetDimension(name, limit, consumed, unit, source, "within_limit")


def terminal_run_v2(
    running: BoardBenchRun,
    campaign: BoardBenchCampaign,
    artifacts: Path,
    *,
    completed_at: str,
    fallback_status: str,
    fallback_reason: str,
    final_response: str | None,
    duration_seconds: float | None,
    worker_exit_code: int | None,
    inventory: tuple[InventoryEntry, ...],
) -> BoardBenchRunV2:
    """Normalize one terminal run, preferring retained product-session facts."""

    if running.status != "running" or running.started_at is None:
        raise ValidationError("terminal BoardBench run needs a start timestamp")
    if not _run_matches_campaign_plan(running, campaign):
        raise ValidationError("terminal BoardBench run differs from its campaign plan")
    _timestamp(completed_at, "v2 run completed_at")
    trace = collect_trace_evidence(artifacts)
    product = _retained_product_terminal(
        artifacts / "repository",
        trace.events,
        run_id=running.run_id,
        started_at=running.started_at,
        completed_at=completed_at,
    )
    # Frozen campaign/environment binding is authoritative runner evidence,
    # not a weak return-code heuristic.  A product receipt cannot turn a
    # configuration-drifted run into a comparable pass.
    runner_binding_failure = fallback_reason in {
        "configuration_drift",
        "environment_probe_failed",
    }
    runner_process_failure = fallback_status != "completed" or (
        worker_exit_code is not None and worker_exit_code != 0
    )
    if runner_binding_failure:
        process, outcome, reason, stage, gate = _fallback_terminal(
            fallback_status, fallback_reason
        )
        source = "worker_fallback"
    elif (
        worker_exit_code is not None
        and worker_exit_code != 0
        and fallback_status not in {"timed_out", "interrupted"}
    ):
        process, outcome, reason, stage, gate = (
            ProcessStatus.CRASHED,
            TaskOutcome.FAILED,
            "crashed",
            None,
            False,
        )
        source = "worker_fallback"
    elif runner_process_failure:
        process, outcome, reason, stage, gate = _fallback_terminal(
            fallback_status, fallback_reason
        )
        source = "worker_fallback"
    elif product is not None:
        process = product.process_status
        outcome = product.task_outcome
        reason = product.termination_reason
        stage = product.stage_reached
        gate = product.release_gate_passed
        source = "product_session_terminal"
        if process is ProcessStatus.TIMED_OUT:
            reason = "budget_exhausted:wall_time"
    else:
        terminal = _trace_terminal(trace.events)
        if terminal is None:
            terminal = _fallback_terminal(fallback_status, fallback_reason)
            source = "worker_fallback"
        else:
            source = "trace_terminal"
        process, outcome, reason, stage, gate = terminal
        if process is ProcessStatus.TIMED_OUT:
            reason = "budget_exhausted:wall_time"
    exhausted = _BUDGET_REASON.fullmatch(reason)
    exhausted_name = exhausted.group(1) if exhausted is not None else None
    budgets = (
        _dimension(
            "model_turns",
            MODEL_TURN_LIMIT,
            trace.model_turns,
            "turn",
            "hermes_trace",
            exhausted_name,
        ),
        _dimension(
            "pcb_tool_calls",
            campaign.tool_call_budget,
            trace.pcb_tool_calls,
            "call",
            "pcb_tool_trace",
            exhausted_name,
        ),
        _dimension(
            "route_attempts",
            None,
            trace.route_attempts,
            "attempt",
            "pcb_tool_trace",
            exhausted_name,
        ),
        _dimension(
            "route_node_expansions",
            None,
            trace.route_node_expansions,
            "node",
            "tool_receipts",
            exhausted_name,
        ),
        _dimension(
            "uncached_input_tokens",
            None,
            trace.uncached_input_tokens,
            "token",
            "usage_receipt",
            exhausted_name,
        ),
        _dimension(
            "output_tokens",
            None,
            trace.output_tokens,
            "token",
            "usage_receipt",
            exhausted_name,
        ),
        _dimension(
            "cache_read_tokens",
            None,
            trace.cache_read_tokens,
            "token",
            "usage_receipt",
            exhausted_name,
        ),
        _dimension(
            "wall_time",
            campaign.wall_timeout_seconds,
            duration_seconds,
            "second",
            "worker_process",
            exhausted_name,
        ),
    )
    return BoardBenchRunV2(
        running.campaign_id,
        running.run_id,
        running.case_id,
        running.repetition,
        running.prompt_sha256,
        "terminal",
        running.started_at,
        completed_at,
        process,
        outcome,
        reason,
        stage,
        gate,
        source,
        worker_exit_code,
        final_response,
        budgets,
        trace.actual_cost,
        trace.context_quality,
        inventory,
    )


__all__ = [
    "BUDGET_NAMES",
    "MODEL_TURN_LIMIT",
    "PCB_TOOL_CALL_LIMIT",
    "RUN_V2_SCHEMA",
    "WALL_TIME_LIMIT_SECONDS",
    "ActualCostEvidence",
    "BoardBenchRunV2",
    "BudgetDimension",
    "ContextQualityEvidence",
    "NormalizedBoardBenchRun",
    "collect_trace_evidence",
    "load_normalized_run",
    "load_run_v2",
    "normalize_v1_run",
    "normalize_v2_run",
    "planned_run_v2",
    "start_run_v2",
    "store_run_v2",
    "terminal_run_v2",
    "validate_campaign_denominator",
]
