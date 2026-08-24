"""Revision-bound engineering progress, convergence, and product outcomes.

The values in this module are deliberately evidence carrying.  An unavailable
ERC/DRC result is not the number zero, and evidence from an older design
revision cannot advance the current stage or release gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Self

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited, make_directory

PROGRESS_VECTOR_SCHEMA = "pcbdraft-progress-vector"
PROGRESS_VECTOR_VERSION = 1
PROGRESS_DELTA_SCHEMA = "pcbdraft-progress-delta"
PROGRESS_DELTA_VERSION = 1
STAGE_EVIDENCE_SCHEMA = "pcbdraft-stage-evidence"
STAGE_EVIDENCE_VERSION = 1
PRODUCT_SESSION_TERMINAL_SCHEMA = "pcbdraft-product-session-terminal"
PRODUCT_SESSION_TERMINAL_VERSION = 1
PRODUCT_SESSION_FILE_LIMIT = 1024 * 1024

METRIC_NAMES = (
    "semantic_native_mismatch_count",
    "unresolved_connection_count",
    "fatal_drc_count",
    "error_drc_count",
    "erc_error_count",
    "unplaced_component_count",
    "routing_failure_count",
)

# Lexicographic comparison is intentional: a reduction in routing noise must
# never conceal a newly observed consistency or safety regression.
METRIC_PRIORITY = (
    "semantic_native_mismatch_count",
    "fatal_drc_count",
    "unresolved_connection_count",
    "erc_error_count",
    "unplaced_component_count",
    "routing_failure_count",
    "error_drc_count",
)

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_TERMINATION = re.compile(r"budget_exhausted:[a-z][a-z0-9_]{0,63}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")


class EvidenceStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    STALE = "stale"


class ProgressClassification(str, Enum):
    IMPROVED = "improved"
    NEUTRAL = "neutral"
    REGRESSED = "regressed"
    INDETERMINATE = "indeterminate"


class EngineeringStage(str, Enum):
    NOT_STARTED = "not_started"
    REQUIREMENTS_FROZEN = "requirements_frozen"
    SCHEMATIC_SEMANTIC = "schematic_semantic"
    NATIVE_SCHEMATIC_CONFIRMED = "native_schematic_confirmed"
    FOOTPRINT_NET_SYNC = "footprint_net_sync"
    PLACEMENT = "placement"
    ROUTING = "routing"
    NATIVE_CONNECTIVITY_CONFIRMED = "native_connectivity_confirmed"
    ERC_DRC = "erc_drc"
    RELEASE_GATE = "release_gate"


class ProcessStatus(str, Enum):
    EXITED = "exited"
    CRASHED = "crashed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"


TERMINATION_REASONS = frozenset(
    {
        "release_gate_passed",
        "no_progress",
        "agent_returned_before_gate",
        "tool_failure",
        "unsupported_requirement",
        "human_intervention_required",
        "cancelled",
        "timed_out",
        "crashed",
    }
)


def _revision(value: object, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValidationError(f"{label} is invalid")
    return value


def _session_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    if len(encoded) > 512:
        raise ValidationError(f"{label} is invalid")
    return value


def product_terminal_receipt_id(session_id: str, turn_id: str) -> str:
    """Derive one stable, path-safe receipt id without adding a hash contract."""

    _session_identity(session_id, "product receipt session id")
    _session_identity(turn_id, "product receipt turn id")
    identity = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{session_id}-{turn_id}").strip("-.")
    if not identity:
        raise ValidationError("product receipt identity is invalid")
    # Hermes turn ids end in a UUID-derived suffix.  Retaining the tail keeps
    # that uniqueness when a provider supplies an unusually long session id.
    return _identity(f"session-{identity[-247:]}", "product receipt id")


def validate_product_terminal_receipt_id(value: object) -> str:
    """Validate a caller-supplied product receipt id before path construction."""

    return _identity(value, "product receipt id")


@dataclass(frozen=True)
class MetricValue:
    """One metric with the revision and quality of its evidence."""

    value: int | None
    source_revision: int | None
    status: EvidenceStatus

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvidenceStatus):
            raise ValidationError("progress metric status is invalid")
        _revision(self.source_revision, "metric source revision", optional=True)
        if self.status is EvidenceStatus.UNKNOWN:
            if self.value is not None:
                raise ValidationError("unknown progress metric cannot contain a value")
            return
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or self.value < 0
            or self.source_revision is None
        ):
            raise ValidationError(
                "known/stale progress metric needs a non-negative value and revision"
            )

    @classmethod
    def known(cls, value: int, source_revision: int) -> Self:
        return cls(value, source_revision, EvidenceStatus.KNOWN)

    @classmethod
    def unknown(cls, source_revision: int | None = None) -> Self:
        return cls(None, source_revision, EvidenceStatus.UNKNOWN)

    @classmethod
    def stale(cls, value: int, source_revision: int) -> Self:
        return cls(value, source_revision, EvidenceStatus.STALE)

    def for_revision(self, revision: int) -> MetricValue:
        """Return this evidence normalized for a requested design revision."""

        _revision(revision, "progress revision")
        if self.status is EvidenceStatus.UNKNOWN:
            return self
        if self.source_revision == revision:
            return MetricValue.known(self.value or 0, revision)
        return MetricValue.stale(self.value or 0, self.source_revision or 0)

    def is_current(self, revision: int) -> bool:
        return (
            self.status is EvidenceStatus.KNOWN
            and self.source_revision == revision
            and self.value is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source_revision": self.source_revision,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$.metric") -> Self:
        if not isinstance(value, dict) or set(value) != {
            "value",
            "source_revision",
            "status",
        }:
            raise ValidationError(f"{path} is malformed")
        try:
            status = EvidenceStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{path}.status is invalid") from exc
        return cls(value["value"], value["source_revision"], status)


@dataclass(frozen=True)
class ProgressVector:
    """Versioned, fixed-shape progress evidence for one design revision."""

    source_revision: int
    semantic_native_mismatch_count: MetricValue
    unresolved_connection_count: MetricValue
    fatal_drc_count: MetricValue
    error_drc_count: MetricValue
    erc_error_count: MetricValue
    unplaced_component_count: MetricValue
    routing_failure_count: MetricValue

    def __post_init__(self) -> None:
        _revision(self.source_revision, "progress source revision")
        for name in METRIC_NAMES:
            metric = getattr(self, name)
            if not isinstance(metric, MetricValue):
                raise ValidationError(f"progress metric {name} is malformed")
            if (
                metric.status is EvidenceStatus.KNOWN
                and metric.source_revision != self.source_revision
            ):
                raise ValidationError(
                    f"current progress metric {name} has a different source revision"
                )
            if (
                metric.status is EvidenceStatus.STALE
                and metric.source_revision == self.source_revision
            ):
                raise ValidationError(
                    f"stale progress metric {name} claims the current revision"
                )

    @classmethod
    def unknown(cls, source_revision: int) -> Self:
        metric = MetricValue.unknown(source_revision)
        return cls(source_revision, *(metric for _name in METRIC_NAMES))

    def metric(self, name: str) -> MetricValue:
        if name not in METRIC_NAMES:
            raise ValidationError(f"unknown progress metric: {name}")
        return getattr(self, name)

    def replace_metric(self, name: str, value: MetricValue) -> ProgressVector:
        values = {item: self.metric(item) for item in METRIC_NAMES}
        if name not in values:
            raise ValidationError(f"unknown progress metric: {name}")
        values[name] = value.for_revision(self.source_revision)
        return ProgressVector(self.source_revision, **values)

    def at_revision(self, source_revision: int) -> ProgressVector:
        return ProgressVector(
            source_revision,
            **{
                name: self.metric(name).for_revision(source_revision)
                for name in METRIC_NAMES
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROGRESS_VECTOR_SCHEMA,
            "version": PROGRESS_VECTOR_VERSION,
            "source_revision": self.source_revision,
            "metrics": {name: self.metric(name).to_dict() for name in METRIC_NAMES},
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "version",
            "source_revision",
            "metrics",
        }:
            raise ValidationError("progress vector is malformed")
        if (
            value["schema"] != PROGRESS_VECTOR_SCHEMA
            or value["version"] != PROGRESS_VECTOR_VERSION
            or not isinstance(value["metrics"], dict)
            or set(value["metrics"]) != set(METRIC_NAMES)
        ):
            raise ValidationError("unsupported progress vector schema/version")
        metrics = {
            name: MetricValue.from_dict(value["metrics"][name], f"$.metrics.{name}")
            for name in METRIC_NAMES
        }
        return cls(value["source_revision"], **metrics)


@dataclass(frozen=True)
class MetricDelta:
    name: str
    before: MetricValue
    after: MetricValue
    delta: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "delta": self.delta,
        }


@dataclass(frozen=True)
class ProgressDelta:
    before_revision: int
    after_revision: int
    classification: ProgressClassification
    decisive_metric: str | None
    metrics: tuple[MetricDelta, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROGRESS_DELTA_SCHEMA,
            "version": PROGRESS_DELTA_VERSION,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "classification": self.classification.value,
            "decisive_metric": self.decisive_metric,
            "metrics": [item.to_dict() for item in self.metrics],
        }


def compare_progress(before: ProgressVector, after: ProgressVector) -> ProgressDelta:
    """Compare current evidence lexicographically without coercing unknown to zero."""

    deltas: list[MetricDelta] = []
    first_uncertain_priority: int | None = None
    decisive: tuple[str, int] | None = None
    for priority, name in enumerate(METRIC_PRIORITY):
        old = before.metric(name)
        new = after.metric(name)
        both_current = old.is_current(before.source_revision) and new.is_current(
            after.source_revision
        )
        delta = new.value - old.value if both_current else None  # type: ignore[operator]
        deltas.append(MetricDelta(name, old, new, delta))
        if both_current:
            if delta and decisive is None:
                decisive = (name, delta)
            continue
        # A repeated unknown/stale value is still not evidence that the hidden
        # engineering state remained unchanged at a new revision.  In
        # particular it must not let a lower-priority known improvement mask an
        # unavailable safety or connectivity metric.
        if first_uncertain_priority is None:
            first_uncertain_priority = priority

    if decisive is not None:
        name, value = decisive
        if value > 0:
            # A current, observed regression remains factual even if some other
            # metric is unavailable.  Missing evidence can conceal more harm,
            # but it cannot turn the observed harm into improvement.
            classification = ProgressClassification.REGRESSED
            decisive_name = name
        elif first_uncertain_priority is not None:
            classification = ProgressClassification.INDETERMINATE
            decisive_name = None
        else:
            classification = ProgressClassification.IMPROVED
            decisive_name = name
    elif first_uncertain_priority is not None:
        same_live_evidence = before.source_revision == after.source_revision and all(
            before.metric(name) == after.metric(name) for name in METRIC_PRIORITY
        )
        classification = (
            ProgressClassification.NEUTRAL
            if same_live_evidence
            else ProgressClassification.INDETERMINATE
        )
        decisive_name = None
    else:
        classification = ProgressClassification.NEUTRAL
        decisive_name = None
    return ProgressDelta(
        before.source_revision,
        after.source_revision,
        classification,
        decisive_name,
        tuple(deltas),
    )


@dataclass(frozen=True)
class EvidenceCheck:
    passed: bool | None
    source_revision: int | None
    status: EvidenceStatus

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvidenceStatus):
            raise ValidationError("stage evidence status is invalid")
        _revision(self.source_revision, "stage evidence source revision", optional=True)
        if self.status is EvidenceStatus.UNKNOWN:
            if self.passed is not None:
                raise ValidationError("unknown stage evidence cannot contain a result")
        elif not isinstance(self.passed, bool) or self.source_revision is None:
            raise ValidationError(
                "known/stale stage evidence needs a result and revision"
            )

    @classmethod
    def known(cls, passed: bool, source_revision: int) -> Self:
        return cls(passed, source_revision, EvidenceStatus.KNOWN)

    @classmethod
    def unknown(cls, source_revision: int | None = None) -> Self:
        return cls(None, source_revision, EvidenceStatus.UNKNOWN)

    def current_pass(self, revision: int) -> bool:
        return (
            self.status is EvidenceStatus.KNOWN
            and self.source_revision == revision
            and self.passed is True
        )

    def current_known(self, revision: int) -> bool:
        return self.status is EvidenceStatus.KNOWN and self.source_revision == revision

    def for_revision(self, revision: int) -> EvidenceCheck:
        _revision(revision, "stage evidence revision")
        if self.status is EvidenceStatus.UNKNOWN:
            return EvidenceCheck.unknown(self.source_revision)
        if self.source_revision == revision:
            return EvidenceCheck.known(bool(self.passed), revision)
        return EvidenceCheck(self.passed, self.source_revision, EvidenceStatus.STALE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "source_revision": self.source_revision,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class StageEvidence:
    source_revision: int
    requirements_frozen: EvidenceCheck
    schematic_semantic: EvidenceCheck
    native_schematic_confirmed: EvidenceCheck
    footprint_net_sync: EvidenceCheck
    routing_started: EvidenceCheck
    native_connectivity_confirmed: EvidenceCheck
    erc_checked: EvidenceCheck
    drc_checked: EvidenceCheck

    def __post_init__(self) -> None:
        _revision(self.source_revision, "stage evidence revision")
        for name in (
            "requirements_frozen",
            "schematic_semantic",
            "native_schematic_confirmed",
            "footprint_net_sync",
            "routing_started",
            "native_connectivity_confirmed",
            "erc_checked",
            "drc_checked",
        ):
            if not isinstance(getattr(self, name), EvidenceCheck):
                raise ValidationError(f"stage evidence {name} is malformed")

    def to_dict(self) -> dict[str, Any]:
        names = (
            "requirements_frozen",
            "schematic_semantic",
            "native_schematic_confirmed",
            "footprint_net_sync",
            "routing_started",
            "native_connectivity_confirmed",
            "erc_checked",
            "drc_checked",
        )
        return {
            "schema": STAGE_EVIDENCE_SCHEMA,
            "version": STAGE_EVIDENCE_VERSION,
            "source_revision": self.source_revision,
            "checks": {name: getattr(self, name).to_dict() for name in names},
        }


@dataclass(frozen=True)
class StageProjection:
    stage: EngineeringStage
    release_gate_passed: bool
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stage, EngineeringStage):
            raise ValidationError("engineering stage is invalid")
        if not isinstance(self.release_gate_passed, bool):
            raise ValidationError("release gate result must be boolean")
        if not isinstance(self.blockers, tuple) or not all(
            isinstance(item, str) and item for item in self.blockers
        ):
            raise ValidationError("stage blockers are malformed")
        if self.release_gate_passed != (self.stage is EngineeringStage.RELEASE_GATE):
            raise ValidationError("release gate and stage are inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "release_gate_passed": self.release_gate_passed,
            "blockers": list(self.blockers),
        }


def _current_zero(metric: MetricValue, revision: int) -> bool:
    return metric.is_current(revision) and metric.value == 0


def derive_stage(progress: ProgressVector, evidence: StageEvidence) -> StageProjection:
    """Derive the highest evidenced stage; missing prerequisites cap the result."""

    revision = progress.source_revision
    if evidence.source_revision != revision:
        return StageProjection(
            EngineeringStage.NOT_STARTED, False, ("stage_evidence_stale",)
        )
    gates: tuple[tuple[EngineeringStage, bool, str], ...] = (
        (
            EngineeringStage.REQUIREMENTS_FROZEN,
            evidence.requirements_frozen.current_pass(revision),
            "requirements_not_frozen",
        ),
        (
            EngineeringStage.SCHEMATIC_SEMANTIC,
            evidence.schematic_semantic.current_pass(revision),
            "schematic_semantics_unconfirmed",
        ),
        (
            EngineeringStage.NATIVE_SCHEMATIC_CONFIRMED,
            evidence.native_schematic_confirmed.current_pass(revision),
            "native_schematic_unconfirmed",
        ),
        (
            EngineeringStage.FOOTPRINT_NET_SYNC,
            evidence.footprint_net_sync.current_pass(revision),
            "footprint_net_sync_unconfirmed",
        ),
        (
            EngineeringStage.PLACEMENT,
            _current_zero(progress.unplaced_component_count, revision),
            "placement_incomplete_or_unknown",
        ),
        (
            EngineeringStage.ROUTING,
            evidence.routing_started.current_pass(revision),
            "routing_not_evidenced",
        ),
        (
            EngineeringStage.NATIVE_CONNECTIVITY_CONFIRMED,
            evidence.native_connectivity_confirmed.current_pass(revision)
            and _current_zero(progress.unresolved_connection_count, revision),
            "native_connectivity_unconfirmed",
        ),
        (
            EngineeringStage.ERC_DRC,
            evidence.erc_checked.current_known(revision)
            and evidence.drc_checked.current_known(revision)
            and progress.erc_error_count.is_current(revision)
            and progress.error_drc_count.is_current(revision),
            "current_erc_drc_evidence_missing",
        ),
    )
    stage = EngineeringStage.NOT_STARTED
    for candidate, passed, blocker in gates:
        if not passed:
            return StageProjection(stage, False, (blocker,))
        stage = candidate
    release_metrics = (
        progress.semantic_native_mismatch_count,
        progress.unresolved_connection_count,
        progress.fatal_drc_count,
        progress.error_drc_count,
        progress.erc_error_count,
        progress.unplaced_component_count,
    )
    release_passed = (
        all(_current_zero(metric, revision) for metric in release_metrics)
        and evidence.erc_checked.current_pass(revision)
        and evidence.drc_checked.current_pass(revision)
    )
    if release_passed:
        return StageProjection(EngineeringStage.RELEASE_GATE, True, ())
    return StageProjection(
        EngineeringStage.ERC_DRC, False, ("release_checks_failed_or_unknown",)
    )


@dataclass(frozen=True)
class ConvergencePolicy:
    repeated_retry_key_threshold: int = 2
    consecutive_no_improvement_threshold: int = 4

    def __post_init__(self) -> None:
        for value, label in (
            (self.repeated_retry_key_threshold, "retry threshold"),
            (
                self.consecutive_no_improvement_threshold,
                "no-improvement threshold",
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 100
            ):
                raise ValidationError(f"{label} must be from 1 to 100")


DEFAULT_CONVERGENCE_POLICY = ConvergencePolicy()


@dataclass(frozen=True)
class ConvergenceObservation:
    state_key: str
    progress: ProgressClassification
    retry_key: str | None = None


@dataclass(frozen=True)
class ConvergenceDecision:
    allowed: bool
    action: str
    reason: str | None
    repeated_retry_count: int
    consecutive_no_improvement_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "reason": self.reason,
            "repeated_retry_count": self.repeated_retry_count,
            "consecutive_no_improvement_count": self.consecutive_no_improvement_count,
        }


def evaluate_convergence(
    observations: tuple[ConvergenceObservation, ...],
    *,
    state_key: str,
    retry_key: str | None,
    policy: ConvergencePolicy = DEFAULT_CONVERGENCE_POLICY,
) -> ConvergenceDecision:
    """Reject blind repetition while letting a relevant strategy state reset it."""

    same_state = tuple(item for item in observations if item.state_key == state_key)
    repeated = (
        sum(item.retry_key == retry_key for item in same_state)
        if retry_key is not None
        else 0
    )
    no_improvement = 0
    for item in reversed(same_state):
        if item.progress is ProgressClassification.IMPROVED:
            break
        no_improvement += 1
    if repeated >= policy.repeated_retry_key_threshold:
        return ConvergenceDecision(
            False,
            "strategy_change_required",
            "repeated_retry_key",
            repeated,
            no_improvement,
        )
    if no_improvement >= policy.consecutive_no_improvement_threshold:
        return ConvergenceDecision(
            False,
            "blocked",
            "no_progress",
            repeated,
            no_improvement,
        )
    return ConvergenceDecision(True, "continue", None, repeated, no_improvement)


@dataclass(frozen=True)
class ProductSessionTerminalReceipt:
    receipt_id: str
    project_id: str
    session_id: str
    turn_id: str
    created_at: str
    process_status: ProcessStatus
    task_outcome: TaskOutcome
    termination_reason: str
    stage_reached: EngineeringStage
    release_gate_passed: bool
    source_revision: int
    progress: ProgressVector

    def __post_init__(self) -> None:
        _identity(self.receipt_id, "product receipt id")
        _identity(self.project_id, "product receipt project id")
        _session_identity(self.session_id, "product receipt session id")
        _session_identity(self.turn_id, "product receipt turn id")
        if (
            not isinstance(self.created_at, str)
            or _TIMESTAMP.fullmatch(self.created_at) is None
        ):
            raise ValidationError("product receipt timestamp is invalid")
        try:
            datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise ValidationError("product receipt timestamp is invalid") from exc
        if not isinstance(self.process_status, ProcessStatus) or not isinstance(
            self.task_outcome, TaskOutcome
        ):
            raise ValidationError("product receipt outcome is invalid")
        if not isinstance(self.stage_reached, EngineeringStage):
            raise ValidationError("product receipt stage is invalid")
        if not isinstance(self.release_gate_passed, bool):
            raise ValidationError("product receipt release gate is invalid")
        if not isinstance(self.termination_reason, str) or (
            self.termination_reason not in TERMINATION_REASONS
            and _TERMINATION.fullmatch(self.termination_reason) is None
        ):
            raise ValidationError("product receipt termination reason is invalid")
        _revision(self.source_revision, "product receipt source revision")
        if self.progress.source_revision != self.source_revision:
            raise ValidationError("product receipt progress revision differs")
        release_facts = (
            self.release_gate_passed,
            self.task_outcome is TaskOutcome.PASSED,
            self.termination_reason == "release_gate_passed",
            self.stage_reached is EngineeringStage.RELEASE_GATE,
        )
        if any(release_facts) and not all(release_facts):
            raise ValidationError("product receipt release outcome is inconsistent")
        if self.release_gate_passed and self.process_status is not ProcessStatus.EXITED:
            raise ValidationError("product receipt release outcome is inconsistent")
        lifecycle_reason = {
            ProcessStatus.CRASHED: "crashed",
            ProcessStatus.CANCELLED: "cancelled",
            ProcessStatus.TIMED_OUT: "timed_out",
        }.get(self.process_status)
        if lifecycle_reason is not None and self.termination_reason != lifecycle_reason:
            raise ValidationError("product receipt process outcome is inconsistent")
        if self.process_status is ProcessStatus.EXITED and self.termination_reason in {
            "crashed",
            "cancelled",
            "timed_out",
        }:
            raise ValidationError("product receipt process outcome is inconsistent")
        expected_outcome = (
            TaskOutcome.PASSED
            if self.termination_reason == "release_gate_passed"
            else TaskOutcome.BLOCKED
            if self.termination_reason
            in {
                "no_progress",
                "unsupported_requirement",
                "human_intervention_required",
            }
            else TaskOutcome.FAILED
            if self.termination_reason in {"tool_failure", "crashed"}
            else TaskOutcome.INCOMPLETE
        )
        if self.task_outcome is not expected_outcome:
            raise ValidationError("product receipt task outcome is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PRODUCT_SESSION_TERMINAL_SCHEMA,
            "version": PRODUCT_SESSION_TERMINAL_VERSION,
            "receipt_id": self.receipt_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "process_status": self.process_status.value,
            "task_outcome": self.task_outcome.value,
            "termination_reason": self.termination_reason,
            "stage_reached": self.stage_reached.value,
            "release_gate_passed": self.release_gate_passed,
            "source_revision": self.source_revision,
            "progress": self.progress.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "schema",
            "version",
            "receipt_id",
            "project_id",
            "session_id",
            "turn_id",
            "created_at",
            "process_status",
            "task_outcome",
            "termination_reason",
            "stage_reached",
            "release_gate_passed",
            "source_revision",
            "progress",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValidationError("product session terminal receipt is malformed")
        if (
            value["schema"] != PRODUCT_SESSION_TERMINAL_SCHEMA
            or value["version"] != PRODUCT_SESSION_TERMINAL_VERSION
        ):
            raise ValidationError("unsupported product session terminal schema/version")
        try:
            process = ProcessStatus(value["process_status"])
            outcome = TaskOutcome(value["task_outcome"])
            stage = EngineeringStage(value["stage_reached"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("product session terminal enums are invalid") from exc
        return cls(
            value["receipt_id"],
            value["project_id"],
            value["session_id"],
            value["turn_id"],
            value["created_at"],
            process,
            outcome,
            value["termination_reason"],
            stage,
            value["release_gate_passed"],
            value["source_revision"],
            ProgressVector.from_dict(value["progress"]),
        )


def terminal_outcome(
    *,
    process_status: ProcessStatus,
    requested_reason: str | None,
    stage: StageProjection,
) -> tuple[TaskOutcome, str]:
    """Normalize lifecycle facts without treating a normal exit as PCB success."""

    # A valid board already present in the project cannot rewrite how this
    # session actually stopped.  Only a normal exit without a failure reason
    # may promote current release evidence to a passed task outcome.
    if process_status is ProcessStatus.CRASHED:
        return TaskOutcome.FAILED, "crashed"
    if process_status is ProcessStatus.CANCELLED:
        return TaskOutcome.INCOMPLETE, "cancelled"
    if process_status is ProcessStatus.TIMED_OUT:
        return TaskOutcome.INCOMPLETE, "timed_out"
    if stage.release_gate_passed and requested_reason in {
        None,
        "agent_returned_before_gate",
        "release_gate_passed",
    }:
        return TaskOutcome.PASSED, "release_gate_passed"
    reason = requested_reason
    if reason is None:
        reason = {
            ProcessStatus.EXITED: "agent_returned_before_gate",
            ProcessStatus.CRASHED: "crashed",
            ProcessStatus.CANCELLED: "cancelled",
            ProcessStatus.TIMED_OUT: "timed_out",
        }[process_status]
    if reason not in TERMINATION_REASONS and _TERMINATION.fullmatch(reason) is None:
        reason = "tool_failure"
    if reason in {
        "no_progress",
        "unsupported_requirement",
        "human_intervention_required",
    }:
        outcome = TaskOutcome.BLOCKED
    elif reason in {"tool_failure", "crashed"}:
        outcome = TaskOutcome.FAILED
    else:
        outcome = TaskOutcome.INCOMPLETE
    return outcome, reason


def store_product_session_terminal(
    project_root: Path, receipt: ProductSessionTerminalReceipt
) -> Path:
    """Persist one immutable terminal artifact under the owning project."""

    root = make_directory(project_root / "product-sessions")
    path = root / f"{receipt.receipt_id}.json"
    if path.exists():
        existing = ProductSessionTerminalReceipt.from_dict(
            load_json_limited(path, PRODUCT_SESSION_FILE_LIMIT)
        )
        if existing != receipt:
            raise ValidationError("product session terminal receipt is immutable")
        return path
    atomic_write_json(path, receipt.to_dict())
    return path
