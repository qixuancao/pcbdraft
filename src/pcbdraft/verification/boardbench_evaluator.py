"""Independent automatic scoring for immutable BoardBench runs.

The evaluator consumes the retained run tree after the Agent process exits.  It
does not reuse Agent receipts: the raw inventory is re-hashed, the single
managed project is reopened, library qualification and KiCad validation run in
fresh evidence directories, and the hidden case contract is evaluated against
semantic IR.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, make_directory, read_text_limited
from pcbdraft.core.project import copy_project
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.domain.component_qualification import qualify_components
from pcbdraft.domain.ir import Component, Design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.services.managed import ManagedProject, open_managed_project
from pcbdraft.verification.boardbench import (
    AUTOMATIC_METRICS,
    BoardBenchCampaign,
    BoardBenchCase,
    BoardBenchCorpus,
    BoardBenchRun,
    BoardBenchScore,
    EfficiencyMetrics,
    FailureClassification,
    ManufacturingConstraint,
    MetricResult,
    NetRule,
    RatingBound,
    ReferenceRequirement,
    ToolCallCount,
    artifact_sha256,
    build_inventory,
    canonical_json_bytes,
    load_run,
    write_artifact,
)
from pcbdraft.verification.validation import ValidationRun, validate_managed_project

EVALUATOR_VERSION = "boardbench-evaluator-v4"
EVALUATION_SCHEMA = "pcbdraft-boardbench-evaluation-evidence"
EVALUATION_VERSION = 1
# The writer rotates before appending an incoming bounded record, so a valid
# member may exceed its nominal 16 MiB rotation threshold by one record.  Match
# the runner's retained-member ceiling rather than rejecting that evidence.
TRACE_MEMBER_LIMIT = 32 * 1024 * 1024
USAGE_RECEIPT_LIMIT = 64 * 1024
MAX_TRACE_MEMBERS = 16
MAX_MATCH_SEARCH_NODES = 100_000
_TRACE_MEMBER = re.compile(r"agent-trace\.jsonl(?:\.(\d+))?")
_SAFE_RATING_FACT = re.compile(r"[a-z][a-z0-9_]{0,127}")
_BOM_CARDINALITY_KIND = "forbidden_unmatched_bom_component"

MetricState = Literal["pass", "fail", "unknown", "not_applicable"]
ClaimState = Literal["claims_complete", "claims_blocked_or_incomplete", "ambiguous"]
MatchMetric = Literal["all", "reference_topology", "support_circuits", "ratings"]
PinOrientation = Literal["identity", "swap_1_2"]

_IDENTITY_ORIENTATION: PinOrientation = "identity"
_SWAPPED_ORIENTATION: PinOrientation = "swap_1_2"


@dataclass(frozen=True)
class PredicateResult:
    """One deterministic hidden-contract predicate result."""

    id: str
    state: Literal["pass", "fail", "unknown"]
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "state": self.state, "reason": self.reason}


@dataclass(frozen=True)
class ContractEvaluation:
    """Reference results from a global injection or dependency-scoped fallback."""

    slot_state: Literal["pass", "fail", "unknown"]
    slot_reason: str
    assignment: tuple[tuple[str, str], ...]
    orientations: tuple[tuple[str, PinOrientation], ...]
    search_nodes: int
    search_truncated: bool
    topology: tuple[PredicateResult, ...]
    support: tuple[PredicateResult, ...]
    ratings: tuple[PredicateResult, ...]
    manufacturing: tuple[PredicateResult, ...]
    support_slot_state: Literal["pass", "fail", "unknown"]
    support_slot_reason: str
    support_assignment: tuple[tuple[str, str], ...]
    support_orientations: tuple[tuple[str, PinOrientation], ...]
    ratings_slot_state: Literal["pass", "fail", "unknown"]
    ratings_slot_reason: str
    ratings_assignment: tuple[tuple[str, str], ...]
    ratings_orientations: tuple[tuple[str, PinOrientation], ...]

    def metric_state(self, name: str) -> Literal["pass", "fail", "unknown"]:
        slot_state = {
            "reference_topology": self.slot_state,
            "support_circuits": self.support_slot_state,
            "ratings": self.ratings_slot_state,
        }[name]
        if slot_state != "pass":
            return slot_state
        collection = {
            "reference_topology": self.topology,
            "support_circuits": self.support,
            "ratings": self.ratings,
        }[name]
        return _combine_predicates(collection)

    def to_dict(self) -> dict[str, Any]:
        orientations = dict(self.orientations)
        support_orientations = dict(self.support_orientations)
        ratings_orientations = dict(self.ratings_orientations)
        return {
            "slot_state": self.slot_state,
            "slot_reason": self.slot_reason,
            "assignment": [
                {
                    "slot": slot,
                    "component": component,
                    "orientation": orientations[slot],
                }
                for slot, component in self.assignment
            ],
            "search_nodes": self.search_nodes,
            "search_truncated": self.search_truncated,
            "metric_slot_matches": {
                "reference_topology": {
                    "state": self.slot_state,
                    "reason": self.slot_reason,
                    "assignment": [
                        {
                            "slot": slot,
                            "component": component,
                            "orientation": orientations[slot],
                        }
                        for slot, component in self.assignment
                    ],
                },
                "support_circuits": {
                    "state": self.support_slot_state,
                    "reason": self.support_slot_reason,
                    "assignment": [
                        {
                            "slot": slot,
                            "component": component,
                            "orientation": support_orientations[slot],
                        }
                        for slot, component in self.support_assignment
                    ],
                },
                "ratings": {
                    "state": self.ratings_slot_state,
                    "reason": self.ratings_slot_reason,
                    "assignment": [
                        {
                            "slot": slot,
                            "component": component,
                            "orientation": ratings_orientations[slot],
                        }
                        for slot, component in self.ratings_assignment
                    ],
                },
            },
            "topology": [item.to_dict() for item in self.topology],
            "support": [item.to_dict() for item in self.support],
            "ratings": [item.to_dict() for item in self.ratings],
            "manufacturing": [item.to_dict() for item in self.manufacturing],
        }


@dataclass(frozen=True)
class TraceEvent:
    """The stable envelope shared by PCBDraft's real debug trace events."""

    seq: int
    timestamp: str
    pid: int
    event: str
    data: Mapping[str, Any]

    @classmethod
    def decode(cls, value: object) -> TraceEvent:
        if not isinstance(value, dict) or set(value) != {
            "seq",
            "timestamp",
            "pid",
            "event",
            "data",
        }:
            raise ValidationError("trace event envelope is malformed")
        seq = value["seq"]
        pid = value["pid"]
        event = value["event"]
        timestamp = value["timestamp"]
        data = value["data"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ValidationError("trace event sequence is malformed")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise ValidationError("trace event pid is malformed")
        if not isinstance(event, str) or not event or len(event) > 128:
            raise ValidationError("trace event name is malformed")
        if not isinstance(timestamp, str):
            raise ValidationError("trace event timestamp is malformed")
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValidationError("trace event timestamp is malformed") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValidationError("trace event timestamp is malformed")
        if not isinstance(data, Mapping):
            raise ValidationError("trace event data is malformed")
        return cls(seq=seq, timestamp=timestamp, pid=pid, event=event, data=data)


@dataclass(frozen=True)
class TraceReduction:
    """Typed reduction of all retained trace members."""

    members: tuple[str, ...]
    gap_detected: bool
    schema_errors: tuple[str, ...]
    session_id: str | None
    final_response: str | None
    agent_erc_invoked: bool | None
    agent_drc_invoked: bool | None
    efficiency: EfficiencyMetrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "members": list(self.members),
            "gap_detected": self.gap_detected,
            "schema_errors": list(self.schema_errors),
            "session_id": self.session_id,
            "final_response_present": self.final_response is not None,
            "agent_erc_invoked": self.agent_erc_invoked,
            "agent_drc_invoked": self.agent_drc_invoked,
            "efficiency": self.efficiency.to_dict(),
        }


@dataclass(frozen=True)
class UsageReceiptReduction:
    """Bounded Hermes one-shot accounting receipt, never model-authored evidence."""

    present: bool
    errors: tuple[str, ...]
    session_id: str | None
    model_requests: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    cost_amount: float | None
    cost_currency: str | None
    cost_status: str
    cost_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "errors": list(self.errors),
            "session_id": self.session_id,
            "model_requests": self.model_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cost_amount": self.cost_amount,
            "cost_currency": self.cost_currency,
            "cost_status": self.cost_status,
            "cost_source": self.cost_source,
        }


class _ValidationRunner(Protocol):
    def __call__(
        self,
        project_value: ManagedProject | str | Path,
        *,
        output: str | Path | None = None,
        timeout: float = 90.0,
    ) -> ValidationRun: ...


class _ProjectOpener(Protocol):
    def __call__(self, value: str | Path) -> ManagedProject: ...


class _Qualifier(Protocol):
    def __call__(self, design: Design, graph: PartGraph) -> _QualificationEvidence: ...


class _QualificationEvidence(Protocol):
    @property
    def pad_mapping_failures(self) -> tuple[str, ...]: ...

    def to_dict(self) -> dict[str, Any]: ...


def _validate_retained_project(
    project_value: ManagedProject | str | Path,
    *,
    output: str | Path | None = None,
    timeout: float = 90.0,
) -> ValidationRun:
    """Validate a private snapshot without mutating immutable run artifacts."""

    source_project = (
        open_managed_project(project_value)
        if isinstance(project_value, (str, Path))
        else project_value
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="pcbdraft-boardbench-validation-"
        ) as temporary:
            snapshot_root = Path(temporary) / "design"
            copy_project(source_project.root, snapshot_root)
            snapshot_project = open_managed_project(snapshot_root)
            snapshot_project.assert_synchronized()
            # Normal validation may refresh project-lock metadata, and KiCad may
            # create project-side preference files while checking. Both writes
            # stay inside this private disposable snapshot.
            return validate_managed_project(
                snapshot_project,
                output=output,
                timeout=timeout,
            )
    except OSError as exc:
        raise ValidationError(
            "BoardBench validation project snapshot is unavailable"
        ) from exc


_INCOMPLETE_PATTERNS = (
    re.compile(
        r"\b(?:not|isn['’]?t|aren['’]?t|wasn['’]?t|never)\s+"
        r"(?:complete|completed|done|finished|ready)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:incomplete|unfinished|blocked|failed|unable|cannot|can['’]?t|"
        r"couldn['’]?t|still\s+needs?|remaining)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:未完成|尚未完成|没有完成|无法完成|不能完成|失败|受阻|还需|仍需|尚需)"
    ),
)
_COMPLETE_PATTERNS = (
    re.compile(
        r"(?:^|[\n.!?])\s*(?:done|completed|finished)\s*(?:$|[\n.!?—:;-])",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:board|design|project|schematic|pcb|task)\s+"
        r"(?:is|was|has\s+been)\s+"
        r"(?:complete|completed|finished|ready|generated)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:have\s+)?(?:completed|finished)\s+(?:the\s+)?"
        r"(?:board|design|project|schematic|pcb|task)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bready\s+(?:for|to)\s+(?:order|manufacture|fabricate)\b", re.IGNORECASE
    ),
    re.compile(
        r"(?:已完成(?:设计|工程|原理图|PCB|任务)|"
        r"(?:设计|工程|原理图|PCB|任务)(?:已)?完成|可以下单|可下单)"
    ),
)


def classify_completion_claim(text: str | None) -> ClaimState:
    """Classify only explicit Chinese/English completion or blocker language."""

    if not isinstance(text, str) or not text.strip():
        return "ambiguous"
    incomplete = any(
        pattern.search(text) is not None for pattern in _INCOMPLETE_PATTERNS
    )
    masked = text
    for pattern in _INCOMPLETE_PATTERNS:
        masked = pattern.sub(" ", masked)
    complete = any(pattern.search(masked) is not None for pattern in _COMPLETE_PATTERNS)
    if complete and not incomplete:
        return "claims_complete"
    if incomplete and not complete:
        return "claims_blocked_or_incomplete"
    return "ambiguous"


def _trace_sort_key(path: Path) -> tuple[int, str]:
    match = _TRACE_MEMBER.fullmatch(path.name)
    suffix = int(match.group(1) or 0) if match is not None else 0
    return (-suffix, path.name)


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _event_identity(data: Mapping[str, Any]) -> str | None:
    value = data.get("api_request_id")
    return value if isinstance(value, str) and value else None


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("trace JSON contains duplicate fields")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    raise ValidationError(f"trace JSON contains invalid constant: {value}")


def _usage_tokens(
    usage: Mapping[str, Any],
) -> tuple[
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
]:
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    cache_read_tokens = _nonnegative_int(usage.get("cache_read_tokens"))
    cache_write_tokens = _nonnegative_int(usage.get("cache_write_tokens"))
    reasoning_tokens = _nonnegative_int(usage.get("reasoning_tokens"))
    total_tokens = _nonnegative_int(usage.get("total_tokens"))
    return (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        reasoning_tokens,
        total_tokens,
    )


def _cost_record(
    data: Mapping[str, Any],
) -> tuple[str, float | None, str | None, str] | None:
    usage_value = data.get("usage")
    usage = usage_value if isinstance(usage_value, Mapping) else data
    raw_status = usage.get("cost_status")
    if not isinstance(raw_status, str):
        return None
    status = "subscription_included" if raw_status == "included" else raw_status
    if status not in {"actual", "estimated", "subscription_included", "unknown"}:
        return None
    source_value = usage.get("cost_source")
    source = source_value if isinstance(source_value, str) and source_value else "trace"
    if status == "actual":
        amount = _nonnegative_float(
            usage.get("actual_cost_usd", usage.get("cost_amount"))
        )
    elif status == "estimated":
        amount = _nonnegative_float(
            usage.get("estimated_cost_usd", usage.get("cost_amount"))
        )
    else:
        amount = None
    currency_value = usage.get("cost_currency")
    currency = (
        currency_value
        if isinstance(currency_value, str) and currency_value
        else "USD"
        if status in {"actual", "estimated"} and amount is not None
        else None
    )
    return status, amount, currency, source[:512]


def _reduce_tokens(
    usages: Sequence[Mapping[str, Any]], *, gap: bool
) -> tuple[
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    str,
    str,
]:
    if gap or not usages:
        return None, None, None, None, None, None, "unknown", "unavailable"
    values = [_usage_tokens(usage) for usage in usages]
    totals_by_bucket = tuple(
        sum(value for value in bucket if value is not None)
        if all(value is not None for value in bucket)
        else None
        for bucket in zip(*values, strict=True)
    )
    (
        input_total,
        output_total,
        cache_read_total,
        cache_write_total,
        reasoning_total,
        reported_total,
    ) = totals_by_bucket
    canonical_parts = (
        input_total,
        output_total,
        cache_read_total,
        cache_write_total,
    )
    if all(item is not None for item in canonical_parts):
        derived_total = sum(item for item in canonical_parts if item is not None)
        if reported_total is None:
            return (
                input_total,
                output_total,
                cache_read_total,
                cache_write_total,
                reasoning_total,
                derived_total,
                "derived" if reasoning_total is not None else "partial",
                "trace_reduction",
            )
        if reported_total != derived_total:
            reported_total = None
    available = (
        input_total,
        output_total,
        cache_read_total,
        cache_write_total,
        reasoning_total,
        reported_total,
    )
    if all(item is not None for item in available):
        return (*available, "reported", "trace_reduction")
    if any(item is not None for item in available):
        return (*available, "partial", "trace_reduction")
    return None, None, None, None, None, None, "unknown", "unavailable"


def _reduce_cost(
    response_costs: Sequence[tuple[str, float | None, str | None, str]],
    terminal_cost: tuple[str, float | None, str | None, str] | None,
    *,
    gap: bool,
) -> tuple[float | None, str | None, str, str]:
    if gap:
        return None, None, "unknown", "trace_gap"
    records = [terminal_cost] if terminal_cost is not None else list(response_costs)
    if not records:
        return None, None, "unknown", "trace_cost_unavailable"
    statuses = {item[0] for item in records}
    sources = {item[3] for item in records}
    source = next(iter(sources)) if len(sources) == 1 else "multiple_trace_sources"
    if len(statuses) != 1:
        return None, None, "unknown", source
    status = records[0][0]
    if status in {"actual", "estimated"}:
        amounts = [item[1] for item in records]
        currencies = {item[2] for item in records}
        if (
            any(item is None for item in amounts)
            or len(currencies) != 1
            or None in currencies
        ):
            return None, None, "unknown", source
        return (
            sum(item for item in amounts if item is not None),
            next(iter(currencies)),
            status,
            source,
        )
    return None, None, status, source


def _tool_status(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.casefold()
    if normalized in {"ok", "success", "completed", "complete"}:
        return "completed"
    if normalized in {"error", "failed", "failure"}:
        return "failed"
    if normalized in {"interrupted", "cancelled", "canceled", "timed_out", "timeout"}:
        return "interrupted"
    if normalized in {"denied", "blocked", "rejected"}:
        return "denied"
    return "unknown"


def _blocked_tool_result(value: object) -> bool:
    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return False
    return isinstance(payload, Mapping) and payload.get("blocked") is True


def _tool_event_status(data: Mapping[str, Any]) -> str:
    if _blocked_tool_result(data.get("result")):
        return "denied"
    return _tool_status(data.get("status"))


def decode_trace(
    trace_root: str | Path,
    *,
    wall_seconds: float,
    failure_reason: str | None,
) -> TraceReduction:
    """Decode rotated real trace JSONL, making every affected value unknown on gaps."""

    root = Path(trace_root)
    members: tuple[Path, ...] = ()
    errors: list[str] = []
    structural_gap = False
    if root.is_symlink() or not root.is_dir():
        errors.append("trace_directory_unavailable")
        structural_gap = True
    else:
        members = tuple(
            sorted(
                (
                    path
                    for path in root.iterdir()
                    if path.is_file()
                    and not path.is_symlink()
                    and _TRACE_MEMBER.fullmatch(path.name) is not None
                ),
                key=_trace_sort_key,
            )
        )
        if len(members) > MAX_TRACE_MEMBERS:
            errors.append("trace_member_limit")
            structural_gap = True
            members = members[:MAX_TRACE_MEMBERS]

    events: list[TraceEvent] = []
    for member in members:
        try:
            lines = read_text_limited(member, TRACE_MEMBER_LIMIT).splitlines()
        except PCBDraftError:
            errors.append(f"unreadable:{member.name}")
            structural_gap = True
            continue
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(
                    line,
                    object_pairs_hook=_json_object,
                    parse_constant=_invalid_json_constant,
                )
                event = TraceEvent.decode(raw)
            except (json.JSONDecodeError, ValidationError):
                errors.append(f"malformed:{member.name}:{index}")
                structural_gap = True
                continue
            events.append(event)

    sequences = [event.seq for event in events]
    if not sequences:
        errors.append("trace_empty")
        structural_gap = True
    elif sequences != list(range(1, sequences[-1] + 1)):
        errors.append("trace_sequence_gap")
        structural_gap = True

    usages: list[Mapping[str, Any]] = []
    response_costs: list[tuple[str, float | None, str | None, str]] = []
    terminal_cost: tuple[str, float | None, str | None, str] | None = None
    final_response: str | None = None
    model_requests = 0
    provider_errors = 0
    retry_by_request: dict[str, int] = {}
    api_seconds = 0.0
    tool_seconds = 0.0
    starts: Counter[str] = Counter()
    policy_blocks: Counter[str] = Counter()
    tool_counts: Counter[tuple[str, str]] = Counter()
    invoked: set[str] = set()
    session_ids: set[str] = set()
    retry_unknown = False
    token_unknown = False
    tool_unknown = False
    tool_time_unknown = False
    api_time_unknown = False

    for event in events:
        data = event.data
        session_value = data.get("session_id")
        if isinstance(session_value, str) and session_value:
            session_ids.add(session_value)
        if event.event == "model_request":
            identity = _event_identity(data)
            retry = _nonnegative_int(data.get("retry_count"))
            if identity is None or retry is None:
                errors.append(f"model_request_schema:{event.seq}")
                retry_unknown = True
            else:
                retry_by_request[identity] = max(
                    retry_by_request.get(identity, 0), retry
                )
            model_requests += 1
        elif event.event == "model_response":
            if _event_identity(data) is None:
                errors.append(f"model_response_schema:{event.seq}")
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                usages.append(usage)
            else:
                errors.append(f"model_response_usage:{event.seq}")
                token_unknown = True
            duration = _nonnegative_float(data.get("api_duration_seconds"))
            if duration is None:
                errors.append(f"model_response_duration:{event.seq}")
                api_time_unknown = True
            else:
                api_seconds += duration
            cost = _cost_record(data)
            if cost is not None:
                response_costs.append(cost)
        elif event.event == "model_error":
            provider_errors += 1
            identity = _event_identity(data)
            retry = _nonnegative_int(data.get("retry_count"))
            if identity is None or retry is None:
                errors.append(f"model_error_schema:{event.seq}")
                retry_unknown = True
            else:
                retry_by_request[identity] = max(
                    retry_by_request.get(identity, 0), retry
                )
            duration = _nonnegative_float(data.get("api_duration_seconds"))
            if duration is None:
                errors.append(f"model_error_duration:{event.seq}")
                api_time_unknown = True
            else:
                api_seconds += duration
        elif event.event == "tool_start":
            name = data.get("tool_name")
            if not isinstance(name, str) or not name:
                errors.append(f"tool_start_schema:{event.seq}")
                tool_unknown = True
            elif name.startswith("pcb_"):
                starts[name] += 1
                invoked.add(name)
        elif event.event == "tool_end":
            name = data.get("tool_name")
            duration = _nonnegative_int(data.get("duration_ms"))
            if not isinstance(name, str) or not name:
                errors.append(f"tool_end_schema:{event.seq}")
                tool_unknown = True
            elif name.startswith("pcb_"):
                invoked.add(name)
                policy_blocked = policy_blocks[name] > 0
                if starts[name] > 0:
                    starts[name] -= 1
                elif not policy_blocked:
                    errors.append(f"tool_end_without_start:{event.seq}")
                    tool_unknown = True
                status = "denied" if policy_blocked else _tool_event_status(data)
                if policy_blocked:
                    policy_blocks[name] -= 1
                tool_counts[(name, status)] += 1
                if duration is None:
                    errors.append(f"tool_end_duration:{event.seq}")
                    tool_time_unknown = True
                else:
                    tool_seconds += duration / 1000.0
        elif event.event == "tool_policy_blocked":
            name = data.get("tool_name")
            if not isinstance(name, str) or not name.startswith("pcb_"):
                errors.append(f"tool_policy_blocked_schema:{event.seq}")
                tool_unknown = True
            else:
                invoked.add(name)
                policy_blocks[name] += 1
        elif event.event == "turn_complete":
            response = data.get("assistant_response")
            if isinstance(response, str) and response.strip():
                final_response = response
            else:
                errors.append(f"turn_complete_schema:{event.seq}")
        elif event.event == "session_end":
            cost = _cost_record(data)
            if cost is not None:
                terminal_cost = cost

    for name, count in policy_blocks.items():
        if count:
            tool_counts[(name, "denied")] += count
            starts[name] = max(0, starts[name] - count)
    for name, count in starts.items():
        if count:
            tool_counts[(name, "unknown")] += count
            tool_time_unknown = True

    if len(session_ids) > 1:
        errors.append("trace_session_identity_conflict")
    gap = structural_gap
    (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        reasoning_tokens,
        total_tokens,
        token_status,
        token_source,
    ) = _reduce_tokens(usages, gap=gap or token_unknown)
    cost_amount, cost_currency, cost_status, cost_source = _reduce_cost(
        response_costs, terminal_cost, gap=gap
    )
    if gap:
        model_request_value = None
        pcb_tool_calls = None
        breakdown: tuple[ToolCallCount, ...] = ()
        retries = None
        error_count = None
        tool_time = None
        api_time = None
        erc_invoked = drc_invoked = None
    else:
        model_request_value = model_requests
        breakdown = tuple(
            ToolCallCount(name=name, status=status, count=count)
            for (name, status), count in sorted(tool_counts.items())
        )
        pcb_tool_calls = sum(item.count for item in breakdown)
        retries = None if retry_unknown else sum(retry_by_request.values())
        error_count = provider_errors
        if tool_unknown:
            pcb_tool_calls = None
            breakdown = ()
        tool_time = (
            None if tool_unknown or tool_time_unknown else round(tool_seconds, 6)
        )
        api_time = None if api_time_unknown else round(api_seconds, 6)
        erc_invoked = None if tool_unknown else "pcb_run_erc" in invoked
        drc_invoked = None if tool_unknown else "pcb_run_drc" in invoked

    efficiency = EfficiencyMetrics(
        model_requests=model_request_value,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        token_status=token_status,
        token_source=token_source,
        cost_amount=cost_amount,
        cost_currency=cost_currency,
        cost_status=cost_status,
        cost_source=cost_source,
        pcb_tool_calls=pcb_tool_calls,
        tool_call_counts=breakdown,
        provider_retries=retries,
        provider_errors=error_count,
        tool_seconds=tool_time,
        api_seconds=api_time,
        wall_seconds=max(0.0, wall_seconds),
        failure_reason=failure_reason,
    )
    return TraceReduction(
        members=tuple(path.name for path in members),
        gap_detected=gap,
        schema_errors=tuple(errors[:256]),
        session_id=next(iter(session_ids)) if len(session_ids) == 1 else None,
        final_response=final_response,
        agent_erc_invoked=erc_invoked,
        agent_drc_invoked=drc_invoked,
        efficiency=efficiency,
    )


def _empty_usage_receipt(
    *, present: bool, errors: Sequence[str]
) -> UsageReceiptReduction:
    return UsageReceiptReduction(
        present=present,
        errors=tuple(errors),
        session_id=None,
        model_requests=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        reasoning_tokens=None,
        total_tokens=None,
        cost_amount=None,
        cost_currency=None,
        cost_status="unknown",
        cost_source="usage_receipt_unavailable",
    )


def _bounded_utf8(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if len(encoded) <= limit and "\x00" not in value else None


def decode_usage_receipt(
    path: str | Path,
    campaign: BoardBenchCampaign,
    *,
    trace_session_id: str | None,
) -> UsageReceiptReduction:
    """Decode the bounded run-local Hermes accounting receipt without guessing."""

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        return _empty_usage_receipt(
            present=False,
            errors=("usage_receipt_unavailable",),
        )
    try:
        value = json.loads(
            read_text_limited(source, USAGE_RECEIPT_LIMIT),
            object_pairs_hook=_json_object,
            parse_constant=_invalid_json_constant,
        )
    except (PCBDraftError, json.JSONDecodeError, ValidationError, RecursionError):
        return _empty_usage_receipt(
            present=True,
            errors=("usage_receipt_malformed",),
        )
    required = {
        "estimated_cost_usd",
        "cost_status",
        "cost_source",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "api_calls",
        "model",
        "provider",
        "session_id",
        "completed",
        "failed",
        "service_tier",
    }
    optional = {"failure"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or not set(value) <= required | optional
        or not isinstance(value.get("completed"), bool)
        or not isinstance(value.get("failed"), bool)
        or (
            value.get("service_tier") is not None
            and not isinstance(value.get("service_tier"), str)
        )
        or (
            "failure" in value
            and value["failure"] is not None
            and (
                not isinstance(value["failure"], str)
                or len(value["failure"].encode("utf-8", errors="ignore")) > 8_192
            )
        )
    ):
        return _empty_usage_receipt(
            present=True,
            errors=("usage_receipt_schema",),
        )

    errors: list[str] = []
    provider = value.get("provider")
    model = value.get("model")
    session_id = _bounded_utf8(value.get("session_id"), 512)
    identity_valid = True
    if provider != campaign.provider or model != campaign.model:
        errors.append("usage_receipt_campaign_identity_mismatch")
        identity_valid = False
    if session_id is None:
        errors.append("usage_receipt_session_identity_missing")
        identity_valid = False
    elif trace_session_id is not None and session_id != trace_session_id:
        errors.append("usage_receipt_trace_session_mismatch")
        identity_valid = False

    model_requests = _nonnegative_int(value.get("api_calls"))
    if model_requests is None:
        errors.append("usage_receipt_api_calls")

    token_names = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    token_values = tuple(_nonnegative_int(value.get(name)) for name in token_names)
    if any(item is None for item in token_values):
        errors.append("usage_receipt_tokens")
        token_values = (None, None, None, None, None, None)
    else:
        input_value, output_value, cache_read, cache_write, _reasoning, total = (
            token_values
        )
        canonical_total = sum(
            item
            for item in (input_value, output_value, cache_read, cache_write)
            if item is not None
        )
        if canonical_total != total:
            errors.append("usage_receipt_token_total_mismatch")
            token_values = (None, None, None, None, None, None)

    raw_cost_status = value.get("cost_status")
    cost_status = (
        "subscription_included" if raw_cost_status == "included" else raw_cost_status
    )
    cost_source = _bounded_utf8(value.get("cost_source"), 512)
    cost_amount: float | None = None
    cost_currency: str | None = None
    recorded_amount = _nonnegative_float(value.get("estimated_cost_usd"))
    if value.get("estimated_cost_usd") is not None and recorded_amount is None:
        errors.append("usage_receipt_cost_amount")
        cost_status = "unknown"
        cost_source = "usage_receipt_invalid"
    if cost_status not in {"actual", "estimated", "subscription_included", "unknown"}:
        errors.append("usage_receipt_cost_status")
        cost_status = "unknown"
        cost_source = "usage_receipt_invalid"
    elif cost_source is None:
        errors.append("usage_receipt_cost_source")
        cost_status = "unknown"
        cost_source = "usage_receipt_invalid"
    elif cost_status in {"actual", "estimated"}:
        cost_amount = recorded_amount
        if cost_amount is None:
            errors.append("usage_receipt_cost_amount")
            cost_status = "unknown"
            cost_source = "usage_receipt_invalid"
        else:
            cost_currency = "USD"

    if not identity_valid:
        return _empty_usage_receipt(present=True, errors=errors)
    return UsageReceiptReduction(
        present=True,
        errors=tuple(errors),
        session_id=session_id,
        model_requests=model_requests,
        input_tokens=token_values[0],
        output_tokens=token_values[1],
        cache_read_tokens=token_values[2],
        cache_write_tokens=token_values[3],
        reasoning_tokens=token_values[4],
        total_tokens=token_values[5],
        cost_amount=cost_amount,
        cost_currency=cost_currency,
        cost_status=str(cost_status),
        cost_source=str(cost_source),
    )


def _merge_usage_receipt(
    trace: EfficiencyMetrics,
    usage: UsageReceiptReduction,
) -> tuple[EfficiencyMetrics, tuple[str, ...]]:
    """Prefer final Hermes accounting while localizing trace/receipt conflicts."""

    if not usage.present:
        return trace, usage.errors
    errors = list(usage.errors)
    model_requests = usage.model_requests
    if (
        model_requests is not None
        and trace.model_requests is not None
        and model_requests != trace.model_requests
    ):
        errors.append("usage_trace_api_call_conflict")
        model_requests = None

    usage_tokens = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.reasoning_tokens,
        usage.total_tokens,
    )
    trace_tokens = (
        trace.input_tokens,
        trace.output_tokens,
        trace.cache_read_tokens,
        trace.cache_write_tokens,
        trace.reasoning_tokens,
        trace.total_tokens,
    )
    if all(item is not None for item in usage_tokens):
        if (
            all(item is not None for item in trace_tokens)
            and usage_tokens != trace_tokens
        ):
            errors.append("usage_trace_token_conflict")
            merged_tokens: tuple[int | None, ...] = (None,) * 6
            usage_status = "unknown"
            usage_source = "unavailable"
        else:
            merged_tokens = usage_tokens
            usage_status = "reported"
            usage_source = (
                "provider_and_trace"
                if usage_tokens == trace_tokens
                else "provider_usage"
            )
    else:
        merged_tokens = (None,) * 6
        usage_status = "unknown"
        usage_source = "unavailable"

    cost_amount = usage.cost_amount
    cost_currency = usage.cost_currency
    cost_status = usage.cost_status
    cost_source = usage.cost_source
    if trace.cost_status != "unknown" and cost_status != "unknown":
        cost_matches = trace.cost_status == cost_status
        if cost_status in {"actual", "estimated"}:
            cost_matches = (
                cost_matches
                and trace.cost_currency == cost_currency
                and trace.cost_amount is not None
                and cost_amount is not None
                and math.isclose(trace.cost_amount, cost_amount)
            )
        if not cost_matches:
            errors.append("usage_trace_cost_conflict")
            cost_amount = None
            cost_currency = None
            cost_status = "unknown"
            cost_source = "usage_trace_conflict"

    return (
        replace(
            trace,
            model_requests=model_requests,
            input_tokens=merged_tokens[0],
            output_tokens=merged_tokens[1],
            cache_read_tokens=merged_tokens[2],
            cache_write_tokens=merged_tokens[3],
            reasoning_tokens=merged_tokens[4],
            total_tokens=merged_tokens[5],
            token_status=usage_status,
            token_source=usage_source,
            cost_amount=cost_amount,
            cost_currency=cost_currency,
            cost_status=cost_status,
            cost_source=cost_source,
        ),
        tuple(errors),
    )


def _component_footprint(component: Component, graph: PartGraph) -> str | None:
    override = component.attributes.get("footprint")
    if isinstance(override, str) and override:
        return override
    part = graph.get_optional(component.part_id)
    return part.footprint if part is not None else None


def _slot_candidates(
    case: BoardBenchCase, design: Design, graph: PartGraph
) -> dict[str, tuple[Component, ...]]:
    result: dict[str, tuple[Component, ...]] = {}
    for slot in case.component_slots:
        candidates: list[Component] = []
        for component in design.components:
            part = graph.get_optional(component.part_id)
            if part is None:
                continue
            footprint = _component_footprint(component, graph)
            if any(
                alternative.part_id == part.id
                and alternative.symbol == part.symbol
                and footprint in alternative.footprints
                for alternative in slot.alternatives
            ):
                candidates.append(component)
        result[slot.id] = tuple(sorted(candidates, key=lambda item: item.id))
    return result


def _component_orientations(
    component: Component, graph: PartGraph
) -> tuple[PinOrientation, ...]:
    """Return only electrically justified reference-to-pin orientations."""

    part = graph.get_optional(component.part_id)
    if (
        part is not None
        and getattr(part, "kind", None) == "resistor"
        and len(part.pins) == 2
        and {pin.number for pin in part.pins} == {"1", "2"}
    ):
        return (_IDENTITY_ORIENTATION, _SWAPPED_ORIENTATION)
    return (_IDENTITY_ORIENTATION,)


@dataclass(frozen=True)
class _EndpointObservation:
    known: bool
    net: str | None


class _CircuitView:
    def __init__(
        self,
        design: Design,
        graph: PartGraph,
        assignment: Mapping[str, Component],
        orientations: Mapping[str, PinOrientation],
    ):
        self.design = design
        self.graph = graph
        self.assignment = assignment
        self.orientations = orientations
        self._endpoint_nets: dict[tuple[str, str], str] = {
            (endpoint.component, endpoint.pin): net.id
            for net in design.nets
            for endpoint in net.endpoints
        }
        self._nets = {net.id: net for net in design.nets}
        self._domains = {domain.id: domain for domain in design.power_domains}

    def endpoint(self, value: str) -> _EndpointObservation:
        if value.count(".") != 1:
            return _EndpointObservation(False, None)
        slot_id, pin_token = value.split(".", 1)
        component = self.assignment.get(slot_id)
        if component is None:
            return _EndpointObservation(False, None)
        part = self.graph.get_optional(component.part_id)
        if part is None:
            return _EndpointObservation(False, None)
        exact_number = [pin for pin in part.pins if pin.number == pin_token]
        semantic = [
            pin
            for pin in part.pins
            if pin.name.casefold() == pin_token.casefold()
            or pin_token.casefold() in {item.casefold() for item in pin.functions}
        ]
        matches = exact_number or semantic
        numbers = {pin.number for pin in matches}
        if len(numbers) != 1:
            return _EndpointObservation(False, None)
        number = next(iter(numbers))
        orientation = self.orientations.get(slot_id, _IDENTITY_ORIENTATION)
        if orientation == _SWAPPED_ORIENTATION:
            if _SWAPPED_ORIENTATION not in _component_orientations(
                component, self.graph
            ):
                return _EndpointObservation(False, None)
            number = "2" if number == "1" else "1" if number == "2" else number
        return _EndpointObservation(
            True, self._endpoint_nets.get((component.id, number))
        )

    def same_net(self, left: str, right: str) -> Literal["pass", "fail", "unknown"]:
        endpoints = (self.endpoint(left), self.endpoint(right))
        if not all(item.known for item in endpoints):
            return "unknown"
        if any(item.net is None for item in endpoints):
            return "fail"
        return "pass" if endpoints[0].net == endpoints[1].net else "fail"

    def support_between(
        self, target: str, support_slot: str, reference: str
    ) -> Literal["pass", "fail", "unknown"]:
        target_value, reference_value = self.endpoint(target), self.endpoint(reference)
        component = self.assignment.get(support_slot)
        if not target_value.known or not reference_value.known or component is None:
            return "unknown"
        if target_value.net is None or reference_value.net is None:
            return "fail"
        if target_value.net == reference_value.net:
            return "fail"
        part = self.graph.get_optional(component.part_id)
        if part is None or len(part.pins) != 2:
            return "unknown" if part is None else "fail"
        nets = [
            self._endpoint_nets.get((component.id, pin.number)) for pin in part.pins
        ]
        if any(net is None for net in nets):
            return "fail"
        return (
            "pass" if set(nets) == {target_value.net, reference_value.net} else "fail"
        )

    def operating_interval(
        self, endpoint: str, quantity: str
    ) -> tuple[float, float] | None:
        observed = self.endpoint(endpoint)
        if not observed.known or observed.net is None:
            return None
        net = self._nets.get(observed.net)
        if net is None or net.power_domain is None:
            return None
        domain = self._domains.get(net.power_domain)
        if domain is None:
            return None
        if quantity == "voltage":
            return float(domain.min_v), float(domain.max_v)
        if quantity == "current":
            value = float(domain.max_current_a)
            return value, value
        if quantity == "power":
            value = float(domain.max_v) * float(domain.max_current_a)
            return value, value
        return None


def _predicate(
    id_: str, state: Literal["pass", "fail", "unknown"], reason: str
) -> PredicateResult:
    return PredicateResult(id=id_, state=state, reason=reason)


def _net_rule(rule: NetRule, view: _CircuitView) -> PredicateResult:
    observations = [view.endpoint(endpoint) for endpoint in rule.endpoints]
    if any(not item.known for item in observations):
        return _predicate(rule.id, "unknown", "endpoint_unresolvable")
    nets = [item.net for item in observations]
    state: Literal["pass", "fail", "unknown"]
    if rule.kind == "required_endpoint":
        state = "pass" if all(net is not None for net in nets) else "fail"
    elif rule.kind == "forbidden_endpoint":
        state = "pass" if all(net is None for net in nets) else "fail"
    elif any(net is None for net in nets):
        state = "fail"
    elif rule.kind == "same_net":
        state = "pass" if len(set(nets)) == 1 else "fail"
    else:
        state = "pass" if len(set(nets)) == len(nets) else "fail"
    return _predicate(rule.id, state, f"{rule.kind}_{state}")


_THREE_SUBJECT_SUPPORT = frozenset(
    {"decoupling", "pull_up", "protection", "reset", "boot"}
)
_PAIR_SUPPORT = frozenset({"power_source", "power_return", "debug", "interface"})


def _support_requirement(
    requirement: ReferenceRequirement, view: _CircuitView
) -> PredicateResult:
    if requirement.kind in _THREE_SUBJECT_SUPPORT:
        target, support_slot, reference = requirement.subjects
        state = view.support_between(target, support_slot, reference)
        return _predicate(requirement.id, state, f"{requirement.kind}_{state}")
    if requirement.kind in _PAIR_SUPPORT:
        states = [
            view.same_net(requirement.subjects[index], requirement.subjects[index + 1])
            for index in range(0, len(requirement.subjects), 2)
        ]
        state = _combine_states(states)
        return _predicate(requirement.id, state, f"{requirement.kind}_{state}")
    return _predicate(requirement.id, "unknown", "unsupported_requirement_kind")


def _fact_interval(value: object) -> tuple[float, float] | None:
    number = _nonnegative_float(value)
    if number is not None:
        return number, number
    if not isinstance(value, Mapping):
        return None
    minimum = _nonnegative_float(value.get("min"))
    maximum = _nonnegative_float(value.get("max"))
    if minimum is None and maximum is None:
        exact = _nonnegative_float(value.get("value"))
        return (exact, exact) if exact is not None else None
    low = minimum if minimum is not None else maximum
    high = maximum if maximum is not None else minimum
    if low is None or high is None or low > high:
        return None
    return low, high


def _inside_bound(interval: tuple[float, float], bound: RatingBound) -> bool:
    low, high = interval
    return not (
        (bound.minimum is not None and low < bound.minimum)
        or (bound.maximum is not None and high > bound.maximum)
    )


def _rating_bound(bound: RatingBound, view: _CircuitView) -> PredicateResult:
    if bound.source == "operating":
        interval = view.operating_interval(bound.subject, bound.quantity)
        if interval is None:
            return _predicate(bound.id, "unknown", "operating_value_unavailable")
    elif bound.source == "part_rating":
        component = view.assignment.get(bound.subject)
        if (
            component is None
            or bound.fact_key is None
            or _SAFE_RATING_FACT.fullmatch(bound.fact_key) is None
        ):
            return _predicate(bound.id, "unknown", "part_rating_subject_unavailable")
        part = view.graph.get_optional(component.part_id)
        if part is None or part.trust not in {
            "rule_validated",
            "human_verified",
            "production_verified",
        }:
            return _predicate(bound.id, "unknown", "part_rating_not_attributed")
        interval = _fact_interval(part.ratings.get(bound.fact_key))
        if interval is None:
            return _predicate(bound.id, "unknown", "part_rating_fact_unavailable")
    else:
        return _predicate(bound.id, "unknown", "rating_source_unsupported")
    state: Literal["pass", "fail", "unknown"] = (
        "pass" if _inside_bound(interval, bound) else "fail"
    )
    return _predicate(bound.id, state, f"{bound.source}_rating_{state}")


def _forbidden_condition(
    requirement: ReferenceRequirement,
    view: _CircuitView,
    ratings: Mapping[str, PredicateResult],
) -> PredicateResult:
    state: Literal["pass", "fail", "unknown"]
    if requirement.kind == "forbidden_part":
        present = {component.part_id for component in view.design.components}
        present.update(
            part.id
            for component in view.design.components
            if (part := view.graph.get_optional(component.part_id)) is not None
        )
        state = "fail" if present.intersection(requirement.subjects) else "pass"
    elif requirement.kind == "forbidden_connection":
        pairs = [
            view.same_net(requirement.subjects[index], requirement.subjects[index + 1])
            for index in range(0, len(requirement.subjects), 2)
        ]
        if any(item == "pass" for item in pairs):
            state = "fail"
        elif any(item == "unknown" for item in pairs):
            state = "unknown"
        else:
            state = "pass"
    elif requirement.kind == "forbidden_rating":
        referenced = [ratings.get(item) for item in requirement.subjects]
        if any(item is None or item.state == "unknown" for item in referenced):
            state = "unknown"
        elif any(item is not None and item.state == "fail" for item in referenced):
            state = "fail"
        else:
            state = "pass"
    elif requirement.kind == _BOM_CARDINALITY_KIND:
        missing_slots = sorted(set(requirement.subjects) - set(view.assignment))
        if missing_slots:
            return _predicate(
                requirement.id,
                "unknown",
                "allowed_slot_assignment_unavailable:" + ",".join(missing_slots),
            )
        allowed_components = {
            view.assignment[slot_id].id for slot_id in requirement.subjects
        }
        unmatched: list[str] = []
        unknown_bom: list[str] = []
        for component in sorted(view.design.components, key=lambda item: item.id):
            if component.id in allowed_components:
                continue
            part = view.graph.get_optional(component.part_id)
            if part is None:
                unknown_bom.append(component.id)
            elif part.bom:
                unmatched.append(component.id)
            elif not part.bom and part.trust not in {
                "rule_validated",
                "human_verified",
                "production_verified",
            }:
                unknown_bom.append(component.id)
        if unmatched:
            return _predicate(
                requirement.id,
                "fail",
                "unmatched_bom_components:" + ",".join(unmatched),
            )
        if unknown_bom:
            return _predicate(
                requirement.id,
                "unknown",
                "bom_classification_unavailable:" + ",".join(unknown_bom),
            )
        return _predicate(
            requirement.id,
            "pass",
            "all_bom_components_match_allowed_slots",
        )
    else:
        state = "unknown"
    return _predicate(requirement.id, state, f"{requirement.kind}_{state}")


def _manufacturing(
    case: BoardBenchCase, design: Design, graph: PartGraph
) -> tuple[PredicateResult, ...]:
    results: list[PredicateResult] = []
    constraint: ManufacturingConstraint
    for constraint in case.manufacturing_constraints:
        kind = constraint.kind
        target = constraint.value_mm
        state: Literal["pass", "fail", "unknown"]
        if kind == "min_trace_width_mm":
            widths = [
                design.board.min_track_mm,
                *(item.width_mm for item in design.native_intent.routes),
            ]
            state = "pass" if min(widths) >= target else "fail"
        elif kind == "min_clearance_mm":
            state = "pass" if design.board.min_clearance_mm >= target else "fail"
        elif kind == "min_drill_mm":
            drills = [
                design.board.min_drill_mm,
                *(item.drill_mm for item in design.native_intent.vias),
            ]
            state = "pass" if min(drills) >= target else "fail"
        elif kind == "max_board_width_mm":
            state = "pass" if design.board.width_mm <= target else "fail"
        elif kind == "max_board_height_mm":
            state = "pass" if design.board.height_mm <= target else "fail"
        elif kind == "max_component_height_mm":
            heights: list[float] = []
            unavailable = False
            for component in design.components:
                part = graph.get_optional(component.part_id)
                if part is None or not part.bom:
                    continue
                height = _nonnegative_float(part.manufacturing.get("height_mm"))
                if height is None or part.trust in {"unverified", "extracted"}:
                    unavailable = True
                else:
                    heights.append(height)
            state = (
                "unknown"
                if unavailable
                else "pass"
                if all(height <= target for height in heights)
                else "fail"
            )
        else:
            state = "unknown"
        results.append(_predicate(constraint.id, state, f"{kind}_{state}"))
    return tuple(results)


def _combine_states(states: Sequence[str]) -> Literal["pass", "fail", "unknown"]:
    if "fail" in states:
        return "fail"
    if "unknown" in states:
        return "unknown"
    return "pass"


def _combine_predicates(
    values: Sequence[PredicateResult],
) -> Literal["pass", "fail", "unknown"]:
    return _combine_states([item.state for item in values])


def _assignment_results(
    case: BoardBenchCase,
    design: Design,
    graph: PartGraph,
    assignment: Mapping[str, Component],
    orientations: Mapping[str, PinOrientation],
) -> tuple[
    tuple[PredicateResult, ...],
    tuple[PredicateResult, ...],
    tuple[PredicateResult, ...],
]:
    view = _CircuitView(design, graph, assignment, orientations)
    topology = tuple(_net_rule(rule, view) for rule in case.net_rules)
    ratings = tuple(_rating_bound(bound, view) for bound in case.rating_bounds)
    rating_index = {item.id: item for item in ratings}
    support = (
        *(_support_requirement(item, view) for item in case.support_requirements),
        *(
            _forbidden_condition(item, view, rating_index)
            for item in case.forbidden_conditions
        ),
    )
    return topology, tuple(support), ratings


def _assignment_rank(
    assignment: Mapping[str, Component],
    orientations: Mapping[str, PinOrientation],
    groups: Sequence[Sequence[PredicateResult]],
    *,
    ignored_ids: frozenset[str] = frozenset(),
) -> tuple[int, int, tuple[tuple[str, str, PinOrientation], ...]]:
    states = [
        item.state for group in groups for item in group if item.id not in ignored_ids
    ]
    return (
        states.count("fail"),
        states.count("unknown"),
        tuple(
            sorted(
                (slot, component.id, orientations[slot])
                for slot, component in assignment.items()
            )
        ),
    )


def _endpoint_slot(endpoint: str) -> str:
    return endpoint.split(".", 1)[0]


def _rating_slots(bound: RatingBound) -> frozenset[str]:
    return frozenset({_endpoint_slot(bound.subject)})


def _requirement_slots(
    requirement: ReferenceRequirement,
    rating_index: Mapping[str, RatingBound],
) -> frozenset[str]:
    if requirement.kind in _THREE_SUBJECT_SUPPORT:
        return frozenset(
            {
                _endpoint_slot(requirement.subjects[0]),
                requirement.subjects[1],
                _endpoint_slot(requirement.subjects[2]),
            }
        )
    if requirement.kind in _PAIR_SUPPORT or requirement.kind == "forbidden_connection":
        return frozenset(_endpoint_slot(subject) for subject in requirement.subjects)
    if requirement.kind == "forbidden_rating":
        return frozenset(
            slot
            for rating_id in requirement.subjects
            for slot in _rating_slots(rating_index[rating_id])
        )
    if requirement.kind == _BOM_CARDINALITY_KIND:
        return frozenset(requirement.subjects)
    return frozenset()


def _partial_dependent_results(
    case: BoardBenchCase,
    design: Design,
    graph: PartGraph,
    assignment: Mapping[str, Component],
    orientations: Mapping[str, PinOrientation],
    metric: MatchMetric,
) -> tuple[PredicateResult, ...]:
    """Evaluate predicates whose complete operand set is already assigned."""

    assigned = frozenset(assignment)
    view = _CircuitView(design, graph, assignment, orientations)
    results: list[PredicateResult] = []
    if metric in {"all", "reference_topology"}:
        for rule in case.net_rules:
            required = frozenset(
                _endpoint_slot(endpoint) for endpoint in rule.endpoints
            )
            if required <= assigned:
                results.append(_net_rule(rule, view))
        if metric == "reference_topology":
            return tuple(results)
    rating_bounds = {bound.id: bound for bound in case.rating_bounds}
    if metric in {"all", "ratings"}:
        for bound in case.rating_bounds:
            if _rating_slots(bound) <= assigned:
                results.append(_rating_bound(bound, view))
        if metric == "ratings":
            return tuple(results)
    rating_results: dict[str, PredicateResult] = {}
    for bound in case.rating_bounds:
        if _rating_slots(bound) <= assigned:
            result = _rating_bound(bound, view)
            rating_results[bound.id] = result
    for requirement in case.support_requirements:
        if _requirement_slots(requirement, rating_bounds) <= assigned:
            results.append(_support_requirement(requirement, view))
    for requirement in case.forbidden_conditions:
        if requirement.kind == "forbidden_part":
            continue
        required = _requirement_slots(requirement, rating_bounds)
        if required <= assigned and (
            requirement.kind != "forbidden_rating"
            or set(requirement.subjects) <= set(rating_results)
        ):
            results.append(_forbidden_condition(requirement, view, rating_results))
    return tuple(results)


@dataclass(frozen=True)
class _ScopeMatch:
    state: Literal["pass", "fail", "unknown"]
    reason: str
    assignment: tuple[tuple[str, str], ...]
    orientations: tuple[tuple[str, PinOrientation], ...]
    search_nodes: int
    search_truncated: bool
    results: tuple[PredicateResult, ...]


def _scope_slots(
    case: BoardBenchCase,
    metric: MatchMetric,
) -> frozenset[str]:
    if metric in {"all", "reference_topology"}:
        return frozenset(slot.id for slot in case.component_slots)
    if metric == "ratings":
        return frozenset(
            slot for bound in case.rating_bounds for slot in _rating_slots(bound)
        )
    rating_index = {bound.id: bound for bound in case.rating_bounds}
    return frozenset(
        slot
        for requirement in (*case.support_requirements, *case.forbidden_conditions)
        for slot in _requirement_slots(requirement, rating_index)
    )


def _scope_results(
    case: BoardBenchCase,
    design: Design,
    graph: PartGraph,
    assignment: Mapping[str, Component],
    orientations: Mapping[str, PinOrientation],
    metric: MatchMetric,
) -> tuple[PredicateResult, ...]:
    if metric == "all":
        topology, support, ratings = _assignment_results(
            case, design, graph, assignment, orientations
        )
        return (*topology, *support, *ratings)
    return _partial_dependent_results(
        case, design, graph, assignment, orientations, metric
    )


def _match_scope(
    case: BoardBenchCase,
    design: Design,
    graph: PartGraph,
    candidates: Mapping[str, tuple[Component, ...]],
    metric: MatchMetric,
    *,
    search_node_limit: int,
) -> _ScopeMatch:
    required_slots = _scope_slots(case, metric)
    missing = sorted(slot for slot in required_slots if not candidates[slot])
    if missing:
        if metric == "support_circuits":
            rating_index = {bound.id: bound for bound in case.rating_bounds}
            cardinality_requirements = tuple(
                requirement
                for requirement in case.forbidden_conditions
                if requirement.kind == _BOM_CARDINALITY_KIND
            )
            other_requirements = (
                *case.support_requirements,
                *(
                    requirement
                    for requirement in case.forbidden_conditions
                    if requirement.kind != _BOM_CARDINALITY_KIND
                ),
            )
            other_slots = frozenset(
                slot
                for requirement in other_requirements
                for slot in _requirement_slots(requirement, rating_index)
            )
            missing_set = frozenset(missing)
            affected = tuple(
                requirement
                for requirement in cardinality_requirements
                if missing_set.intersection(requirement.subjects)
            )
            if affected and not missing_set.intersection(other_slots):
                results = tuple(
                    _predicate(
                        requirement.id,
                        "unknown",
                        "allowed_slot_assignment_unavailable:"
                        + ",".join(
                            sorted(missing_set.intersection(requirement.subjects))
                        ),
                    )
                    for requirement in affected
                )
                return _ScopeMatch(
                    state="unknown",
                    reason="missing_bom_cardinality_slot_assignment:"
                    + ",".join(missing),
                    assignment=(),
                    orientations=(),
                    search_nodes=0,
                    search_truncated=False,
                    results=results,
                )
        return _ScopeMatch(
            state="fail",
            reason="missing_component_slots:" + ",".join(missing),
            assignment=(),
            orientations=(),
            search_nodes=0,
            search_truncated=False,
            results=(),
        )

    participation: Counter[str] = Counter()
    if metric in {"all", "reference_topology"}:
        for rule in case.net_rules:
            participation.update(_endpoint_slot(item) for item in rule.endpoints)
    if metric in {"all", "ratings"}:
        for bound in case.rating_bounds:
            participation.update(_rating_slots(bound))
    if metric in {"all", "support_circuits"}:
        rating_index = {bound.id: bound for bound in case.rating_bounds}
        for requirement in (*case.support_requirements, *case.forbidden_conditions):
            participation.update(_requirement_slots(requirement, rating_index))
    order = sorted(
        required_slots,
        key=lambda slot: (len(candidates[slot]), -participation[slot], slot),
    )
    assignment: dict[str, Component] = {}
    orientations: dict[str, PinOrientation] = {}
    used: set[str] = set()
    nodes = 0
    truncated = False
    best: (
        tuple[
            tuple[int, int, tuple[tuple[str, str, PinOrientation], ...]],
            dict[str, Component],
            dict[str, PinOrientation],
            tuple[PredicateResult, ...],
        ]
        | None
    ) = None
    ignored_ids = (
        frozenset(
            requirement.id
            for requirement in case.forbidden_conditions
            if requirement.kind == "forbidden_part"
        )
        if metric in {"all", "support_circuits"}
        else frozenset()
    )

    def consider(
        candidate: Mapping[str, Component],
        candidate_orientations: Mapping[str, PinOrientation],
    ) -> bool:
        nonlocal best
        results = _scope_results(
            case, design, graph, candidate, candidate_orientations, metric
        )
        rank = _assignment_rank(
            candidate,
            candidate_orientations,
            (results,),
            ignored_ids=ignored_ids,
        )
        if best is None or rank < best[0]:
            best = (
                rank,
                dict(candidate),
                dict(candidate_orientations),
                results,
            )
        return rank[:2] == (0, 0)

    def first_completion(
        index: int,
    ) -> tuple[dict[str, Component], dict[str, PinOrientation]] | None:
        if index == len(order):
            return dict(assignment), dict(orientations)
        slot = order[index]
        for component in candidates[slot]:
            if component.id in used:
                continue
            assignment[slot] = component
            orientations[slot] = _component_orientations(component, graph)[0]
            used.add(component.id)
            completed = first_completion(index + 1)
            used.remove(component.id)
            orientations.pop(slot, None)
            assignment.pop(slot, None)
            if completed is not None:
                return completed
        return None

    def visit(index: int) -> bool:
        nonlocal nodes, truncated
        if nodes >= search_node_limit:
            truncated = True
            return False
        nodes += 1
        if index == len(order):
            return consider(assignment, orientations)
        slot = order[index]
        for component in candidates[slot]:
            if component.id in used:
                continue
            assignment[slot] = component
            used.add(component.id)
            for orientation_index, orientation in enumerate(
                _component_orientations(component, graph)
            ):
                if orientation_index:
                    if nodes >= search_node_limit:
                        truncated = True
                        used.remove(component.id)
                        assignment.pop(slot, None)
                        return False
                    nodes += 1
                orientations[slot] = orientation
                partial = _partial_dependent_results(
                    case, design, graph, assignment, orientations, metric
                )
                if any(item.state != "pass" for item in partial):
                    completed = first_completion(index + 1)
                    if completed is not None:
                        consider(*completed)
                    found = False
                else:
                    found = visit(index + 1)
                if found:
                    orientations.pop(slot, None)
                    used.remove(component.id)
                    assignment.pop(slot, None)
                    return True
                orientations.pop(slot, None)
                if truncated:
                    used.remove(component.id)
                    assignment.pop(slot, None)
                    return False
            used.remove(component.id)
            assignment.pop(slot, None)
        return False

    visit(0)
    if best is None:
        return _ScopeMatch(
            state="unknown" if truncated else "fail",
            reason="matching_search_truncated"
            if truncated
            else "no_injective_component_match",
            assignment=(),
            orientations=(),
            search_nodes=nodes,
            search_truncated=truncated,
            results=(),
        )
    rank, selected, selected_orientations, results = best
    state: Literal["pass", "fail", "unknown"] = (
        "unknown" if truncated and rank[:2] != (0, 0) else "pass"
    )
    reason = (
        "matching_search_truncated_before_conclusive_assignment"
        if state == "unknown"
        else "injective_component_match"
    )
    return _ScopeMatch(
        state=state,
        reason=reason,
        assignment=tuple(
            sorted((slot, component.id) for slot, component in selected.items())
        ),
        orientations=tuple(sorted(selected_orientations.items())),
        search_nodes=nodes,
        search_truncated=truncated,
        results=results,
    )


def evaluate_contracts(
    case: BoardBenchCase,
    design: Design,
    graph: PartGraph,
    *,
    search_node_limit: int = MAX_MATCH_SEARCH_NODES,
) -> ContractEvaluation:
    """Search candidate injections with dependent predicates, never arbitrary pairing."""

    if isinstance(search_node_limit, bool) or not 1 <= search_node_limit <= 10_000_000:
        raise ValidationError("component matching search-node limit is invalid")
    candidates = _slot_candidates(case, design, graph)
    manufacturing = _manufacturing(case, design, graph)
    global_match = _match_scope(
        case,
        design,
        graph,
        candidates,
        "all",
        search_node_limit=search_node_limit,
    )
    component_index = {component.id: component for component in design.components}
    selected = {
        slot: component_index[component] for slot, component in global_match.assignment
    }
    selected_orientations = dict(global_match.orientations)
    if selected:
        topology_results, global_support, global_ratings = _assignment_results(
            case, design, graph, selected, selected_orientations
        )
    else:
        topology_results = global_support = global_ratings = ()
    search_nodes = global_match.search_nodes
    truncated = global_match.search_truncated
    support_state: Literal["pass", "fail", "unknown"]
    ratings_state: Literal["pass", "fail", "unknown"]
    if global_match.state == "pass":
        support_state = ratings_state = "pass"
        support_reason = ratings_reason = "injective_component_match"
        support_assignment = ratings_assignment = global_match.assignment
        support_orientations = ratings_orientations = global_match.orientations
        support_results = global_support
        rating_results = global_ratings
    else:
        support = _match_scope(
            case,
            design,
            graph,
            candidates,
            "support_circuits",
            search_node_limit=search_node_limit,
        )
        ratings = _match_scope(
            case,
            design,
            graph,
            candidates,
            "ratings",
            search_node_limit=search_node_limit,
        )
        search_nodes += support.search_nodes + ratings.search_nodes
        truncated = truncated or support.search_truncated or ratings.search_truncated
        support_state, support_reason = support.state, support.reason
        support_assignment, support_results = support.assignment, support.results
        support_orientations = support.orientations
        ratings_state, ratings_reason = ratings.state, ratings.reason
        ratings_assignment, rating_results = ratings.assignment, ratings.results
        ratings_orientations = ratings.orientations
    return ContractEvaluation(
        slot_state=global_match.state,
        slot_reason=global_match.reason,
        assignment=global_match.assignment,
        orientations=global_match.orientations,
        search_nodes=search_nodes,
        search_truncated=truncated,
        topology=topology_results,
        support=support_results,
        ratings=rating_results,
        manufacturing=manufacturing,
        support_slot_state=support_state,
        support_slot_reason=support_reason,
        support_assignment=support_assignment,
        support_orientations=support_orientations,
        ratings_slot_state=ratings_state,
        ratings_slot_reason=ratings_reason,
        ratings_assignment=ratings_assignment,
        ratings_orientations=ratings_orientations,
    )


def _wall_seconds(run: BoardBenchRun) -> float:
    if run.started_at is None or run.completed_at is None:
        return 0.0
    return max(
        0.0,
        (
            datetime.fromisoformat(run.completed_at)
            - datetime.fromisoformat(run.started_at)
        ).total_seconds(),
    )


def _allocate_output_directory(output: str | Path, artifacts: Path) -> Path:
    """Create one private fresh evaluator directory outside immutable artifacts."""

    raw = Path(output).expanduser()
    if (
        raw.name in {"", ".", ".."}
        or "\x00" in str(raw)
        or any(part in {".", ".."} for part in raw.parts)
    ):
        raise ValidationError("BoardBench evaluator output path is unsafe")
    target = raw.absolute()
    for component in (target, *target.parents):
        if component.is_symlink():
            raise ValidationError(
                "BoardBench evaluator output path contains a symbolic link"
            )
    if target.exists():
        raise ValidationError("BoardBench evaluator output must be a fresh directory")
    canonical_artifacts = artifacts.resolve(strict=True)
    canonical_candidate = target.resolve(strict=False)
    if (
        canonical_candidate == canonical_artifacts
        or canonical_artifacts in canonical_candidate.parents
    ):
        raise ValidationError(
            "BoardBench evaluator output cannot modify immutable run artifacts"
        )
    if target.parent.exists():
        if not target.parent.is_dir():
            raise ValidationError("BoardBench evaluator output parent is unavailable")
    else:
        make_directory(target.parent)
    for component in (target.parent, *target.parent.parents):
        if component.is_symlink():
            raise ValidationError(
                "BoardBench evaluator output path contains a symbolic link"
            )
    canonical_parent = target.parent.resolve(strict=True)
    canonical_target = canonical_parent / target.name
    try:
        canonical_target.mkdir(mode=0o700)
        canonical_target.chmod(0o700)
    except FileExistsError as exc:
        raise ValidationError(
            "BoardBench evaluator output must be a fresh directory"
        ) from exc
    except OSError as exc:
        raise PCBDraftError(
            "cannot create private BoardBench evaluator output"
        ) from exc
    if canonical_target.is_symlink() or not canonical_target.is_dir():
        raise ValidationError("BoardBench evaluator output directory is unsafe")
    return canonical_target


def _locate_managed_project(artifacts: Path) -> Path | None:
    repository = artifacts / "repository"
    projects = repository / "projects"
    if repository.is_symlink() or projects.is_symlink() or not projects.is_dir():
        return None
    candidates: list[Path] = []
    for application_project in sorted(projects.iterdir()):
        if application_project.is_symlink() or not application_project.is_dir():
            continue
        design = application_project / "design"
        if design.is_symlink():
            raise ValidationError("BoardBench managed project contains a symlink")
        if design.is_dir():
            candidates.append(design)
    if len(candidates) > 1:
        raise ValidationError("BoardBench run contains multiple managed projects")
    return candidates[0] if candidates else None


def _complete_project_members(project: ManagedProject) -> bool:
    files = project.manifest.get("files")
    if not isinstance(files, Mapping):
        return False
    required_keys = {
        "manifest",
        "requirements",
        "ir",
        "part_catalog",
        "schematic",
        "board",
        "kicad_project",
        "worker_receipt",
    }
    if not required_keys <= set(files):
        return False
    required_paths = [
        project.project_path,
        project.schematic_path,
        project.board_path,
        project.manifest_path,
        project.ir_path,
        project.requirements_path,
    ]
    for name in (*required_keys, "circuit_plan", "component_qualification"):
        if name not in files:
            continue
        relative = files[name]
        if not isinstance(relative, str):
            return False
        required_paths.append(project.root / relative)
    return all(path.is_file() and not path.is_symlink() for path in required_paths)


def _validation_check(validation: ValidationRun, check_id: str) -> tuple[str, str]:
    for level in validation.levels:
        for check in level.checks:
            if check.id == check_id:
                return check.state, check.outcome
    return "unavailable", "unknown"


def _mapped_check(validation: ValidationRun | None, check_id: str) -> tuple[str, str]:
    if validation is None:
        return "unknown", "independent_validation_unavailable"
    state, outcome = _validation_check(validation, check_id)
    if state != "completed":
        return "unknown", f"{check_id}_unavailable"
    return outcome, f"{check_id}_{outcome}"


def _failure_suggestion(
    run: BoardBenchRun, metrics: Mapping[str, MetricResult]
) -> FailureClassification | None:
    states = {name: metric.state for name, metric in metrics.items()}
    if all(state in {"pass", "not_applicable"} for state in states.values()):
        return None
    reason = (run.termination_reason or "").casefold()
    if any(
        token in reason
        for token in (
            "configuration",
            "timeout",
            "environment",
            "interrupted",
            "worker_start",
        )
    ):
        return FailureClassification(
            stage="kicad_materialization",
            causes=("environment_infrastructure",),
            owners=("environment",),
            reason="run termination indicates an environment or infrastructure failure",
        )
    ordered = (
        (
            "library_resolution",
            "component_knowledge",
            "component_knowledge_gap",
            "knowledge_base",
        ),
        ("reference_topology", "circuit_design", "model_reasoning", "model"),
        ("support_circuits", "circuit_design", "model_reasoning", "model"),
        ("erc", "circuit_design", "model_reasoning", "model"),
        ("complete_project", "kicad_materialization", "model_reasoning", "model"),
        ("drc", "routing", "model_reasoning", "model"),
        ("false_completion", "validation", "model_reasoning", "model"),
    )
    if states.get("library_resolution") != "fail" and states.get("ratings") == "fail":
        rating_reason = metrics["ratings"].reason
        if "operating_rating_fail" in rating_reason:
            return FailureClassification(
                stage="circuit_design",
                causes=("model_reasoning",),
                owners=("model",),
                reason="automatic operating rating constraint failed",
            )
        return FailureClassification(
            stage="component_knowledge",
            causes=("component_knowledge_gap",),
            owners=("knowledge_base",),
            reason="automatic attributed part rating constraint failed",
        )
    for name, stage, cause, owner in ordered:
        if states.get(name) == "fail":
            return FailureClassification(
                stage=stage,
                causes=(cause,),
                owners=(owner,),
                reason=f"automatic metric failed: {name}",
            )
    return FailureClassification(
        stage="validation",
        causes=("insufficient_evidence",),
        owners=("environment",),
        reason="automatic evidence is incomplete or unavailable",
    )


def _metric(case: BoardBenchCase, name: str, state: str, reason: str) -> MetricResult:
    return MetricResult(
        name=name,
        state=state if name in case.applicable_metrics else "not_applicable",
        reason=reason
        if name in case.applicable_metrics
        else "case_metric_not_applicable",
    )


def _contract_metric_reason(
    contracts: ContractEvaluation,
    name: Literal["reference_topology", "support_circuits", "ratings"],
    state: Literal["pass", "fail", "unknown"],
) -> str:
    passing = {
        "reference_topology": "reference_topology_pass",
        "support_circuits": "support_and_forbidden_predicates_pass",
        "ratings": "rating_predicates_pass",
    }
    if state == "pass":
        return passing[name]
    slot_state, slot_reason = {
        "reference_topology": (contracts.slot_state, contracts.slot_reason),
        "support_circuits": (
            contracts.support_slot_state,
            contracts.support_slot_reason,
        ),
        "ratings": (contracts.ratings_slot_state, contracts.ratings_slot_reason),
    }[name]
    if slot_state != "pass":
        return slot_reason
    predicates = {
        "reference_topology": contracts.topology,
        "support_circuits": contracts.support,
        "ratings": contracts.ratings,
    }[name]
    details = [
        f"{item.id}:{item.reason}" for item in predicates if item.state == state
    ][:16]
    return f"{name}_{state}:" + ",".join(details)


def _verify_sources(
    corpus: BoardBenchCorpus,
    campaign: BoardBenchCampaign,
    run: BoardBenchRun,
    run_root: Path,
) -> tuple[BoardBenchCase, Path, str, str, str]:
    if campaign.evaluator_version != EVALUATOR_VERSION:
        raise ValidationError(
            "campaign evaluator version does not match this evaluator"
        )
    if not run.terminal:
        raise ValidationError("BoardBench evaluator requires a terminal run")
    corpus_hash = artifact_sha256(corpus)
    if (
        campaign.corpus_id != corpus.corpus_id
        or campaign.cohort != corpus.cohort
        or campaign.corpus_sha256 != corpus_hash
    ):
        raise ValidationError("BoardBench corpus does not match the frozen campaign")
    case = next((item for item in corpus.cases if item.id == run.case_id), None)
    if case is None:
        raise ValidationError("BoardBench run case is absent from the frozen corpus")
    planned = next((item for item in campaign.runs if item.run_id == run.run_id), None)
    if planned is None or (planned.case_id, planned.repetition) != (
        run.case_id,
        run.repetition,
    ):
        raise ValidationError("BoardBench run does not match the frozen campaign plan")
    if run.campaign_id != campaign.campaign_id or run.case_id != case.id:
        raise ValidationError("BoardBench run/campaign/case identity mismatch")
    prompt_hash = hashlib.sha256(case.prompt.encode("utf-8")).hexdigest()
    if prompt_hash != run.prompt_sha256:
        raise ValidationError("BoardBench case prompt does not match the run")
    receipt_path = run_root / "run.json"
    disk_run = load_run(receipt_path)
    if disk_run != run:
        raise ValidationError("BoardBench source run receipt changed before evaluation")
    artifacts = run_root / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise ValidationError("BoardBench run artifacts are unavailable")
    if build_inventory(artifacts) != run.inventory:
        raise ValidationError(
            "BoardBench run inventory failed independent verification"
        )
    return (
        case,
        artifacts,
        artifact_sha256(campaign),
        hashlib.sha256(canonical_json_bytes(case.to_dict())).hexdigest(),
        artifact_sha256(run),
    )


def evaluate_run(
    corpus: BoardBenchCorpus,
    campaign: BoardBenchCampaign,
    run: BoardBenchRun,
    run_root: str | Path,
    output: str | Path,
    *,
    timeout: float = 90.0,
    project_opener: _ProjectOpener = open_managed_project,
    validation_runner: _ValidationRunner = _validate_retained_project,
    qualifier: _Qualifier = qualify_components,
) -> BoardBenchScore:
    """Independently score one immutable run and write fresh source-bound evidence."""

    if not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValidationError("BoardBench evaluator timeout must be in (0, 3600]")
    root = Path(run_root).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("BoardBench run directory is unavailable")
    root = root.resolve(strict=True)
    target = Path(output).expanduser()
    if target.exists() or target.is_symlink() or target.name in {"", ".", ".."}:
        raise ValidationError("BoardBench evaluator output must be a fresh directory")
    case, artifacts, campaign_hash, case_hash, run_hash = _verify_sources(
        corpus, campaign, run, root
    )
    target = _allocate_output_directory(target, artifacts)

    trace = decode_trace(
        artifacts / "trace",
        wall_seconds=_wall_seconds(run),
        failure_reason=None if run.status == "completed" else run.termination_reason,
    )
    if "trace_session_identity_conflict" in trace.schema_errors:
        usage = _empty_usage_receipt(
            present=(artifacts / "usage.json").is_file(),
            errors=("usage_receipt_trace_session_mismatch",),
        )
    else:
        usage = decode_usage_receipt(
            artifacts / "usage.json",
            campaign,
            trace_session_id=trace.session_id,
        )
    merged_efficiency, usage_errors = _merge_usage_receipt(trace.efficiency, usage)
    trace = replace(trace, efficiency=merged_efficiency)
    project: ManagedProject | None = None
    project_relative: str | None = None
    complete_state: Literal["pass", "fail", "unknown"] = "fail"
    complete_reason = "managed_project_missing"
    try:
        project_path = _locate_managed_project(artifacts)
        if project_path is not None:
            project_relative = project_path.relative_to(artifacts).as_posix()
            project = project_opener(project_path)
            project.assert_synchronized()
            if _complete_project_members(project):
                complete_state, complete_reason = "pass", "managed_project_reopened"
            else:
                project = None
                complete_reason = "managed_project_required_files_missing"
    except PCBDraftError as exc:
        complete_reason = f"managed_project_reopen_failed:{type(exc).__name__}"
        project = None

    validation: ValidationRun | None = None
    validation_failure: str | None = None
    library_state: Literal["pass", "fail", "unknown"] = "unknown"
    library_reason = "managed_project_unavailable"
    contracts: ContractEvaluation | None = None
    if project is not None:
        try:
            issues = [
                issue
                for issue in project.graph.validate_design(
                    project.design,
                    check_libraries=True,
                    allow_provisional=project.design.metadata.get("assurance")
                    == "provisional",
                )
                if issue.severity == "error"
            ]
            qualification = qualifier(project.design, project.graph)
            atomic_write_json(
                target / "component-qualification.json",
                qualification.to_dict(),
                mode=0o600,
            )
            mapping_failures = tuple(qualification.pad_mapping_failures)
            library_state = "fail" if issues or mapping_failures else "pass"
            library_reason = (
                "library_or_pin_pad_validation_failed"
                if library_state == "fail"
                else "installed_libraries_and_pin_pad_mappings_resolved"
            )
        except PCBDraftError as exc:
            library_reason = f"library_validation_unavailable:{type(exc).__name__}"
        try:
            validation = validation_runner(
                project,
                output=target / "independent-validation",
                timeout=timeout,
            )
        except PCBDraftError as exc:
            validation_failure = type(exc).__name__
        contracts = evaluate_contracts(case, project.design, project.graph)

    topology_state = (
        contracts.metric_state("reference_topology") if contracts else "unknown"
    )
    support_state = (
        contracts.metric_state("support_circuits") if contracts else "unknown"
    )
    rating_state = contracts.metric_state("ratings") if contracts else "unknown"
    erc_state, erc_reason = _mapped_check(validation, "l2.erc")
    native_drc_state, native_drc_reason = _mapped_check(
        validation, "l2.drc_connectivity"
    )
    manufacturing_state = (
        _combine_predicates(contracts.manufacturing) if contracts else "unknown"
    )
    drc_state = _combine_states((native_drc_state, manufacturing_state))
    drc_reason = (
        native_drc_reason
        if manufacturing_state == "pass"
        else f"{native_drc_reason};manufacturing_envelope_{manufacturing_state}"
    )
    base = {
        "complete_project": _metric(
            case, "complete_project", complete_state, complete_reason
        ),
        "library_resolution": _metric(
            case, "library_resolution", library_state, library_reason
        ),
        "reference_topology": _metric(
            case,
            "reference_topology",
            topology_state,
            _contract_metric_reason(contracts, "reference_topology", topology_state)
            if contracts
            else "managed_project_unavailable",
        ),
        "erc": _metric(case, "erc", erc_state, erc_reason),
        "drc": _metric(case, "drc", drc_state, drc_reason),
        "support_circuits": _metric(
            case,
            "support_circuits",
            support_state,
            _contract_metric_reason(contracts, "support_circuits", support_state)
            if contracts
            else "managed_project_unavailable",
        ),
        "ratings": _metric(
            case,
            "ratings",
            rating_state,
            _contract_metric_reason(contracts, "ratings", rating_state)
            if contracts
            else "managed_project_unavailable",
        ),
    }
    mandatory_states = [
        metric.state for metric in base.values() if metric.state != "not_applicable"
    ]
    claim = classify_completion_claim(run.final_response or trace.final_response)
    if "fail" not in mandatory_states and "unknown" not in mandatory_states:
        false_state: Literal["pass", "fail", "unknown"] = "pass"
        false_reason = "no_conflict_with_mandatory_metrics"
    elif claim == "claims_complete":
        false_state, false_reason = (
            "fail",
            "explicit_completion_conflicts_with_mandatory_metric",
        )
    elif claim == "claims_blocked_or_incomplete":
        false_state, false_reason = (
            "pass",
            "agent_explicitly_reported_incomplete_or_blocked",
        )
    else:
        false_state, false_reason = "unknown", "completion_claim_ambiguous"
    base["false_completion"] = _metric(
        case, "false_completion", false_state, false_reason
    )
    metrics = tuple(base[name] for name in AUTOMATIC_METRICS)
    active_states = {item.state for item in metrics if item.state != "not_applicable"}
    overall = (
        "fail"
        if "fail" in active_states
        else "unknown"
        if "unknown" in active_states
        else "pass"
    )
    metric_index = {item.name: item for item in metrics}
    score = BoardBenchScore(
        campaign_id=campaign.campaign_id,
        run_id=run.run_id,
        source_campaign_sha256=campaign_hash,
        source_case_sha256=case_hash,
        source_run_sha256=run_hash,
        evaluator_version=EVALUATOR_VERSION,
        scored_at=utc_timestamp(),
        overall_state=overall,
        metrics=metrics,
        efficiency=merged_efficiency,
        failure_suggestion=_failure_suggestion(run, metric_index),
    )
    evidence = {
        "schema": EVALUATION_SCHEMA,
        "version": EVALUATION_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "sources": {
            "campaign_sha256": campaign_hash,
            "case_sha256": case_hash,
            "run_sha256": run_hash,
            "inventory_verified": True,
        },
        "run": {
            "campaign_id": run.campaign_id,
            "case_id": run.case_id,
            "run_id": run.run_id,
        },
        "project": {
            "relative_path": project_relative,
            "state": complete_state,
            "reason": complete_reason,
        },
        "independent_validation": {
            "state": "completed" if validation is not None else "unavailable",
            "failure_type": validation_failure,
            "report_sha256": validation.report_sha256
            if validation is not None
            else None,
        },
        "agent_checks": {
            "erc_invoked": trace.agent_erc_invoked,
            "drc_invoked": trace.agent_drc_invoked,
        },
        "completion_claim": claim,
        "trace": trace.to_dict(),
        "usage_receipt": {
            **usage.to_dict(),
            "merge_errors": list(usage_errors),
        },
        "contracts": contracts.to_dict() if contracts is not None else None,
        "metrics": [item.to_dict() for item in metrics],
    }
    atomic_write_json(target / "evaluation.json", evidence, mode=0o600)
    write_artifact(target / "score.json", score)
    return score


__all__ = (
    "EVALUATION_SCHEMA",
    "EVALUATION_VERSION",
    "EVALUATOR_VERSION",
    "ContractEvaluation",
    "PredicateResult",
    "TraceEvent",
    "TraceReduction",
    "UsageReceiptReduction",
    "classify_completion_claim",
    "decode_trace",
    "decode_usage_receipt",
    "evaluate_contracts",
    "evaluate_run",
)
