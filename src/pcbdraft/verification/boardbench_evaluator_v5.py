"""BoardBench evaluator v5 evidence, equivalence, and comparison contracts.

V5 is deliberately additive.  It consumes existing automatic KiCad evidence
and normalized run receipts, but it never rewrites v1/v4 run, score, review, or
hardware artifacts.  Electrical equivalence is enabled only by explicit
reference policy; this module does not infer symmetry from a two-pin shape or
connector kind.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited, make_directory
from pcbdraft.kicad.routing import RoutingFailure
from pcbdraft.services.progress import EngineeringStage, ProgressVector
from pcbdraft.verification.boardbench import (
    BoardBenchHardware,
    BoardBenchReview,
    BoardBenchScore,
    MetricResult,
    artifact_sha256,
)
from pcbdraft.verification.boardbench_v2 import NormalizedBoardBenchRun

EVALUATOR_V5 = "boardbench-evaluator-v5"
EVALUATION_V5_SCHEMA = "pcbdraft-boardbench-evaluation-v5"
EVALUATION_V5_VERSION = 5
COMPARISON_V5_SCHEMA = "pcbdraft-boardbench-comparison-v5"
COMPARISON_V5_VERSION = 1
EVALUATOR_V5_DIRECTORY = "evaluator-v5"
EVALUATION_V5_FILENAME = "evaluation.json"
COMPARISON_V5_FILENAME = "comparison.json"
EVALUATION_FILE_LIMIT = 8 * 1024 * 1024
MAX_GRAPH_MAPPINGS = 100_000

LayerState = Literal["pass", "fail", "unknown"]
EvidenceKind = Literal["automatic", "ai_review", "human_engineer", "physical"]
EndpointState = Literal["connected", "no_connect", "unconnected"]

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_ENDPOINT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.[A-Za-z0-9][A-Za-z0-9_~+/#-]{0,127}"
)
_LAYERS = ("design_intent", "native_artifact", "delivery_readiness")
_LAYER_STATES = frozenset({"pass", "fail", "unknown"})
_EVIDENCE_KINDS = frozenset({"automatic", "ai_review", "human_engineer", "physical"})

_SOURCE_KINDS: Mapping[str, EvidenceKind] = {
    "automatic_topology": "automatic",
    "automatic_support_circuits": "automatic",
    "automatic_ratings": "automatic",
    "ai_functional_review": "ai_review",
    "ai_orderability_review": "ai_review",
    "human_engineer_functional_review": "human_engineer",
    "human_engineer_orderability": "human_engineer",
    "native_project_parse": "automatic",
    "native_library_resolution": "automatic",
    "native_connectivity": "automatic",
    "native_consistency": "automatic",
    "kicad_erc": "automatic",
    "kicad_drc": "automatic",
    "physical_manufacturing": "physical",
    "physical_assembly": "physical",
    "physical_first_power": "physical",
    "physical_power_rails": "physical",
    "physical_firmware": "physical",
    "physical_core_function": "physical",
    "unavailable": "automatic",
}
_DESIGN_SOURCES = frozenset(
    {
        "automatic_topology",
        "automatic_support_circuits",
        "automatic_ratings",
        "ai_functional_review",
        "human_engineer_functional_review",
        "unavailable",
    }
)
_NATIVE_SOURCES = frozenset(
    {
        "native_project_parse",
        "native_library_resolution",
        "native_connectivity",
        "native_consistency",
        "kicad_erc",
        "kicad_drc",
        "unavailable",
    }
)
_DELIVERY_SOURCES = frozenset(
    {
        "ai_orderability_review",
        "human_engineer_orderability",
        "physical_manufacturing",
        "physical_assembly",
        "physical_first_power",
        "physical_power_rails",
        "physical_firmware",
        "physical_core_function",
        "unavailable",
    }
)
_DELIVERY_QUALIFYING_SOURCES = frozenset(
    {
        "human_engineer_orderability",
        "physical_manufacturing",
        "physical_assembly",
        "physical_first_power",
        "physical_power_rails",
        "physical_firmware",
        "physical_core_function",
    }
)
_PHYSICAL_PASS_SET = frozenset(
    {
        "physical_manufacturing",
        "physical_assembly",
        "physical_first_power",
        "physical_power_rails",
        "physical_core_function",
    }
)

NATIVE_METRIC_NAMES = (
    "complete_project",
    "library_resolution",
    "native_consistency",
    "unresolved_connections",
    "erc",
    "drc",
)


def _closed(value: object, fields: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"{path} is malformed")
    return value


def _text(value: object, path: str, *, limit: int = 8_192) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{path} must be bounded non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{path} must be bounded non-empty text") from exc
    if len(encoded) > limit:
        raise ValidationError(f"{path} must be bounded non-empty text")
    return value


def _optional_text(value: object, path: str, *, limit: int = 8_192) -> str | None:
    return None if value is None else _text(value, path, limit=limit)


def _identity(value: object, path: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValidationError(f"{path} is invalid")
    return value


def _endpoint_id(value: object, path: str) -> str:
    if not isinstance(value, str) or _ENDPOINT.fullmatch(value) is None:
        raise ValidationError(f"{path} is invalid")
    return value


def _choice(value: object, choices: Iterable[str], path: str) -> str:
    allowed = frozenset(choices)
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{path} is invalid")
    return value


def _nonnegative_integer(
    value: object, path: str, *, optional: bool = False
) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{path} must be a non-negative integer")
    return value


def _timestamp(value: object, path: str) -> str:
    text = _text(value, path, limit=128)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{path} is invalid") from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValidationError(f"{path} is invalid")
    return text


def _texts(
    value: object,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = 256,
    choices: Iterable[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValidationError(f"{path} is invalid")
    items = tuple(_text(item, f"{path}[{index}]") for index, item in enumerate(value))
    if len(items) != len(set(items)):
        raise ValidationError(f"{path} contains duplicates")
    if choices is not None and not set(items) <= frozenset(choices):
        raise ValidationError(f"{path} contains an unsupported value")
    return items


def _combine_states(states: Sequence[str]) -> LayerState:
    if not states or "unknown" in states:
        return "fail" if "fail" in states else "unknown"
    return "fail" if "fail" in states else "pass"


@dataclass(frozen=True)
class EvidenceFinding:
    """One attributed, non-model-inferred v5 observation."""

    source: str
    kind: EvidenceKind
    state: LayerState
    reason: str

    def __post_init__(self) -> None:
        expected_kind = _SOURCE_KINDS.get(self.source)
        if expected_kind is None or self.kind != expected_kind:
            raise ValidationError("v5 evidence source/kind is invalid")
        _choice(self.state, _LAYER_STATES, "v5 evidence state")
        _text(self.reason, "v5 evidence reason")
        if self.source == "unavailable" and self.state != "unknown":
            raise ValidationError("unavailable v5 evidence must remain unknown")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "state": self.state,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"source", "kind", "state", "reason"}, path)
        source = _choice(item["source"], _SOURCE_KINDS, f"{path}.source")
        return cls(
            source,
            _choice(item["kind"], _EVIDENCE_KINDS, f"{path}.kind"),  # type: ignore[arg-type]
            _choice(item["state"], _LAYER_STATES, f"{path}.state"),  # type: ignore[arg-type]
            _text(item["reason"], f"{path}.reason"),
        )


def _delivery_state(evidence: Sequence[EvidenceFinding]) -> LayerState:
    qualifying = tuple(
        item for item in evidence if item.source in _DELIVERY_QUALIFYING_SOURCES
    )
    if not qualifying:
        return "unknown"
    states = [item.state for item in qualifying]
    if "fail" in states:
        return "fail"
    if "unknown" in states:
        return "unknown"
    passed_sources = {item.source for item in qualifying if item.state == "pass"}
    if "human_engineer_orderability" in passed_sources:
        return "pass"
    if _PHYSICAL_PASS_SET <= passed_sources:
        return "pass"
    return "unknown"


@dataclass(frozen=True)
class EvaluationLayer:
    """An independent v5 result layer with its own attributed evidence."""

    name: str
    state: LayerState
    evidence: tuple[EvidenceFinding, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _choice(self.name, _LAYERS, "v5 layer name")
        _choice(self.state, _LAYER_STATES, "v5 layer state")
        if not self.evidence or len(self.evidence) > 64:
            raise ValidationError("v5 layer needs 1..64 evidence findings")
        sources = [item.source for item in self.evidence]
        if len(sources) != len(set(sources)):
            raise ValidationError("v5 layer repeats an evidence source")
        if tuple(sorted(self.evidence, key=lambda item: item.source)) != self.evidence:
            raise ValidationError("v5 layer evidence must use deterministic ordering")
        if not self.reasons or len(self.reasons) > 64:
            raise ValidationError("v5 layer needs 1..64 reasons")
        if len(self.reasons) != len(set(self.reasons)):
            raise ValidationError("v5 layer reasons contain duplicates")
        if tuple(sorted(self.reasons)) != self.reasons:
            raise ValidationError("v5 layer reasons must use deterministic ordering")
        expected_reasons = tuple(sorted({item.reason for item in self.evidence}))
        if self.reasons != expected_reasons:
            raise ValidationError("v5 layer reasons contradict its evidence")
        allowed_sources = {
            "design_intent": _DESIGN_SOURCES,
            "native_artifact": _NATIVE_SOURCES,
            "delivery_readiness": _DELIVERY_SOURCES,
        }[self.name]
        if not set(sources) <= allowed_sources:
            raise ValidationError("v5 layer contains evidence from the wrong layer")
        expected = (
            _delivery_state(self.evidence)
            if self.name == "delivery_readiness"
            else _combine_states([item.state for item in self.evidence])
        )
        if self.state != expected:
            raise ValidationError("v5 layer state contradicts its evidence")

    @classmethod
    def build(cls, name: str, evidence: Sequence[EvidenceFinding]) -> Self:
        items = tuple(sorted(evidence, key=lambda item: item.source))
        state = (
            _delivery_state(items)
            if name == "delivery_readiness"
            else _combine_states([item.state for item in items])
        )
        reasons = tuple(sorted({item.reason for item in items}))
        return cls(name, state, items, reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "evidence": [item.to_dict() for item in self.evidence],
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"name", "state", "evidence", "reasons"}, path)
        raw_evidence = item["evidence"]
        if not isinstance(raw_evidence, list):
            raise ValidationError(f"{path}.evidence is invalid")
        return cls(
            _choice(item["name"], _LAYERS, f"{path}.name"),
            _choice(item["state"], _LAYER_STATES, f"{path}.state"),  # type: ignore[arg-type]
            tuple(
                EvidenceFinding.from_dict(entry, f"{path}.evidence[{index}]")
                for index, entry in enumerate(raw_evidence)
            ),
            _texts(item["reasons"], f"{path}.reasons", minimum=1, maximum=64),
        )


_STAGE_ORDER = tuple(stage.value for stage in EngineeringStage)
_STAGE_INDEX = {stage: index for index, stage in enumerate(_STAGE_ORDER)}
ROOT_CAUSE_CODES = (
    "requirements_misunderstanding",
    "component_knowledge_gap",
    "circuit_design_error",
    "model_reasoning",
    "native_materialization_mismatch",
    "placement_constraint_failure",
    "router_invalid_seed",
    "router_zero_length_seed",
    "router_pad_escape_blocked",
    "router_no_legal_channel",
    "router_congestion_exhausted",
    "router_search_budget_exhausted",
    "native_commit_failure",
    "native_connectivity_failure",
    "unintended_net_merge",
    "deterministic_tool_defect",
    "environment_infrastructure",
    "unsupported_requirement",
    "human_intervention_required",
    "insufficient_evidence",
)
SYMPTOM_CODES = (
    "semantic_native_mismatch",
    "open_circuit",
    "short_circuit",
    "wrong_net",
    "unresolved_connection",
    "placement_incomplete",
    "routing_failure",
    "erc_error",
    "drc_error",
    "budget_exhausted",
    "no_progress",
    "agent_returned_before_gate",
    "process_crashed",
    "process_cancelled",
    "configuration_drift",
    "release_gate_not_passed",
)
_ROOT_INDEX = {code: index for index, code in enumerate(ROOT_CAUSE_CODES)}
_SYMPTOM_INDEX = {code: index for index, code in enumerate(SYMPTOM_CODES)}

_ROUTING_CAUSES = {
    "invalid_seed": "router_invalid_seed",
    "zero_length_seed": "router_zero_length_seed",
    "pad_escape_blocked": "router_pad_escape_blocked",
    "no_legal_channel": "router_no_legal_channel",
    "congestion_exhausted": "router_congestion_exhausted",
    "search_budget_exhausted": "router_search_budget_exhausted",
    "native_commit_failed": "native_commit_failure",
    "native_connectivity_failed": "native_connectivity_failure",
    "unintended_net_merge": "unintended_net_merge",
}
_NATIVE_CAUSES = {
    "native_connectivity_failed": "native_connectivity_failure",
    "unintended_board_net_merge": "unintended_net_merge",
    "unintended_net_merge": "unintended_net_merge",
}


@dataclass(frozen=True)
class CausalSignal:
    """One structured evaluator/root-cause signal, never free-text parsed."""

    stage: str
    root_cause: str | None = None
    symptom: str | None = None

    def __post_init__(self) -> None:
        _choice(self.stage, _STAGE_ORDER, "causal signal stage")
        if self.root_cause is None and self.symptom is None:
            raise ValidationError("causal signal needs a cause or symptom")
        if self.root_cause is not None:
            _choice(self.root_cause, ROOT_CAUSE_CODES, "causal root cause")
        if self.symptom is not None:
            _choice(self.symptom, SYMPTOM_CODES, "causal symptom")


@dataclass(frozen=True)
class CausalAttribution:
    first_blocking_stage: str | None
    terminal_stage: str | None
    root_causes: tuple[str, ...]
    symptoms: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.first_blocking_stage is not None:
            _choice(
                self.first_blocking_stage,
                _STAGE_ORDER,
                "first blocking stage",
            )
        if self.terminal_stage is not None:
            _choice(self.terminal_stage, _STAGE_ORDER, "terminal stage")
        if not set(self.root_causes) <= set(ROOT_CAUSE_CODES):
            raise ValidationError("causal root causes are invalid")
        if not set(self.symptoms) <= set(SYMPTOM_CODES):
            raise ValidationError("causal symptoms are invalid")
        if (
            len(self.root_causes) != len(set(self.root_causes))
            or tuple(sorted(self.root_causes, key=_ROOT_INDEX.__getitem__))
            != self.root_causes
        ):
            raise ValidationError(
                "causal root causes are not deterministically ordered"
            )
        if (
            len(self.symptoms) != len(set(self.symptoms))
            or tuple(sorted(self.symptoms, key=_SYMPTOM_INDEX.__getitem__))
            != self.symptoms
        ):
            raise ValidationError("causal symptoms are not deterministically ordered")
        if bool(self.root_causes) != (self.first_blocking_stage is not None):
            raise ValidationError(
                "causal first blocking stage must correspond to a root cause"
            )
        if (
            self.first_blocking_stage is not None
            and self.terminal_stage is not None
            and _STAGE_INDEX[self.first_blocking_stage]
            > _STAGE_INDEX[self.terminal_stage]
        ):
            raise ValidationError("causal first blocking stage follows terminal stage")

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_blocking_stage": self.first_blocking_stage,
            "terminal_stage": self.terminal_stage,
            "root_causes": list(self.root_causes),
            "symptoms": list(self.symptoms),
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$.causal") -> Self:
        item = _closed(
            value,
            {
                "first_blocking_stage",
                "terminal_stage",
                "root_causes",
                "symptoms",
            },
            path,
        )
        first = item["first_blocking_stage"]
        terminal = item["terminal_stage"]
        return cls(
            None
            if first is None
            else _choice(first, _STAGE_ORDER, f"{path}.first_blocking_stage"),
            None
            if terminal is None
            else _choice(terminal, _STAGE_ORDER, f"{path}.terminal_stage"),
            _texts(
                item["root_causes"],
                f"{path}.root_causes",
                maximum=len(ROOT_CAUSE_CODES),
                choices=ROOT_CAUSE_CODES,
            ),
            _texts(
                item["symptoms"],
                f"{path}.symptoms",
                maximum=len(SYMPTOM_CODES),
                choices=SYMPTOM_CODES,
            ),
        )


def _known_positive(progress: ProgressVector, metric: str) -> bool:
    value = progress.metric(metric)
    return value.is_current(progress.source_revision) and bool(value.value)


def derive_causal_attribution(
    run: NormalizedBoardBenchRun,
    *,
    progress: ProgressVector | None = None,
    routing_failures: Sequence[RoutingFailure] = (),
    native_mismatch_codes: Sequence[str] = (),
    evaluator_signals: Sequence[CausalSignal] = (),
) -> CausalAttribution:
    """Derive ordered causes/symptoms without treating terminal DRC as root cause."""

    staged_causes: list[tuple[str, str]] = []
    roots: set[str] = set()
    symptoms: set[str] = set()

    def add_root(stage: str, code: str) -> None:
        staged_causes.append((stage, code))
        roots.add(code)

    for signal in evaluator_signals:
        if signal.root_cause is not None:
            add_root(signal.stage, signal.root_cause)
        if signal.symptom is not None:
            symptoms.add(signal.symptom)

    for failure in routing_failures:
        add_root(EngineeringStage.ROUTING.value, _ROUTING_CAUSES[failure.code])
        symptoms.add("routing_failure")

    for code in native_mismatch_codes:
        cause = _NATIVE_CAUSES.get(code, "native_materialization_mismatch")
        stage = (
            EngineeringStage.NATIVE_CONNECTIVITY_CONFIRMED.value
            if cause in {"native_connectivity_failure", "unintended_net_merge"}
            else EngineeringStage.NATIVE_SCHEMATIC_CONFIRMED.value
        )
        add_root(stage, cause)
        symptoms.add("semantic_native_mismatch")

    if progress is not None:
        if _known_positive(progress, "semantic_native_mismatch_count"):
            symptoms.add("semantic_native_mismatch")
        if _known_positive(progress, "unresolved_connection_count"):
            symptoms.add("unresolved_connection")
        if _known_positive(progress, "unplaced_component_count"):
            symptoms.add("placement_incomplete")
        if _known_positive(progress, "routing_failure_count"):
            symptoms.add("routing_failure")
        if _known_positive(progress, "erc_error_count"):
            symptoms.add("erc_error")
        if _known_positive(progress, "fatal_drc_count") or _known_positive(
            progress, "error_drc_count"
        ):
            symptoms.add("drc_error")

    termination = run.termination_reason
    if isinstance(termination, str) and termination.startswith("budget_exhausted:"):
        symptoms.add("budget_exhausted")
    elif termination == "no_progress":
        symptoms.add("no_progress")
    elif termination == "agent_returned_before_gate":
        symptoms.add("agent_returned_before_gate")
    elif termination == "configuration_drift":
        symptoms.add("configuration_drift")
        add_root(
            run.stage_reached.value
            if run.stage_reached is not None
            else EngineeringStage.NOT_STARTED.value,
            "environment_infrastructure",
        )
    elif termination == "unsupported_requirement":
        add_root(
            run.stage_reached.value
            if run.stage_reached is not None
            else EngineeringStage.REQUIREMENTS_FROZEN.value,
            "unsupported_requirement",
        )
    elif termination == "human_intervention_required":
        add_root(
            run.stage_reached.value
            if run.stage_reached is not None
            else EngineeringStage.NOT_STARTED.value,
            "human_intervention_required",
        )

    if run.process_status is not None:
        if run.process_status.value == "crashed":
            symptoms.add("process_crashed")
        elif run.process_status.value == "cancelled":
            symptoms.add("process_cancelled")
    if run.release_gate_passed is False:
        symptoms.add("release_gate_not_passed")

    first_stage = (
        min(staged_causes, key=lambda item: (_STAGE_INDEX[item[0]], item[1]))[0]
        if staged_causes
        else None
    )
    return CausalAttribution(
        first_stage,
        run.stage_reached.value if run.stage_reached is not None else None,
        tuple(sorted(roots, key=_ROOT_INDEX.__getitem__)),
        tuple(sorted(symptoms, key=_SYMPTOM_INDEX.__getitem__)),
    )


@dataclass(frozen=True)
class NativeMetricEvidence:
    """Raw native metric evidence retained independently from evaluator policy."""

    name: str
    state: LayerState
    value: int | None
    source: str
    command: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _choice(self.name, NATIVE_METRIC_NAMES, "native metric name")
        _choice(self.state, _LAYER_STATES, "native metric state")
        _nonnegative_integer(self.value, "native metric value", optional=True)
        _text(self.source, "native metric source", limit=512)
        if self.name in {"complete_project", "library_resolution"}:
            if self.value is not None:
                raise ValidationError("boolean native metric cannot contain a count")
        elif self.value is not None:
            expected_state = "pass" if self.value == 0 else "fail"
            if self.state != expected_state:
                raise ValidationError("native metric state contradicts its count")
        if self.command is not None:
            if self.name not in {"erc", "drc"}:
                raise ValidationError("only ERC/DRC metrics may retain a command")
            if not self.command or len(self.command) > 32:
                raise ValidationError("native metric command is invalid")
            for index, argument in enumerate(self.command):
                _text(argument, f"native metric command[{index}]", limit=1_024)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "value": self.value,
            "source": self.source,
            "command": list(self.command) if self.command is not None else None,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"name", "state", "value", "source", "command"}, path)
        raw_command = item["command"]
        command = (
            None
            if raw_command is None
            else _texts(raw_command, f"{path}.command", minimum=1, maximum=32)
        )
        return cls(
            _choice(item["name"], NATIVE_METRIC_NAMES, f"{path}.name"),
            _choice(item["state"], _LAYER_STATES, f"{path}.state"),  # type: ignore[arg-type]
            _nonnegative_integer(item["value"], f"{path}.value", optional=True),
            _text(item["source"], f"{path}.source", limit=512),
            command,
        )


@dataclass(frozen=True)
class BoardBenchEvaluationV5:
    campaign_id: str
    run_id: str
    case_id: str
    repetition: int
    source_run_schema: str
    source_run_version: int
    previous_evaluator_version: str
    previous_overall_state: LayerState
    evaluated_at: str
    design_intent: EvaluationLayer
    native_artifact: EvaluationLayer
    delivery_readiness: EvaluationLayer
    legacy_overall_projection: LayerState
    native_metrics: tuple[NativeMetricEvidence, ...]
    causal: CausalAttribution

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "v5 campaign id")
        _identity(self.run_id, "v5 run id")
        _identity(self.case_id, "v5 case id")
        if isinstance(self.repetition, bool) or self.repetition not in {1, 2, 3}:
            raise ValidationError("v5 repetition is invalid")
        _text(self.source_run_schema, "v5 source run schema", limit=256)
        version = _nonnegative_integer(self.source_run_version, "v5 source run version")
        if version is None or version < 1:
            raise ValidationError("v5 source run version must be positive")
        _text(
            self.previous_evaluator_version,
            "v5 previous evaluator version",
            limit=512,
        )
        _choice(
            self.previous_overall_state,
            _LAYER_STATES,
            "v5 previous overall state",
        )
        _timestamp(self.evaluated_at, "v5 evaluated_at")
        expected_layers = (
            (self.design_intent, "design_intent"),
            (self.native_artifact, "native_artifact"),
            (self.delivery_readiness, "delivery_readiness"),
        )
        if any(layer.name != expected for layer, expected in expected_layers):
            raise ValidationError("v5 evaluation layers are misplaced")
        expected_projection = _combine_states(
            [self.design_intent.state, self.native_artifact.state]
        )
        if self.legacy_overall_projection != expected_projection:
            raise ValidationError("v5 legacy projection contradicts independent layers")
        if (
            len(self.native_metrics) != len(NATIVE_METRIC_NAMES)
            or tuple(item.name for item in self.native_metrics) != NATIVE_METRIC_NAMES
        ):
            raise ValidationError("v5 native metrics are incomplete or unordered")
        if self.native_artifact != _native_layer(self.native_metrics):
            raise ValidationError(
                "v5 native artifact layer contradicts raw native metrics"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EVALUATION_V5_SCHEMA,
            "version": EVALUATION_V5_VERSION,
            "evaluator_version": EVALUATOR_V5,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "repetition": self.repetition,
            "source_run_schema": self.source_run_schema,
            "source_run_version": self.source_run_version,
            "previous_evaluator_version": self.previous_evaluator_version,
            "previous_overall_state": self.previous_overall_state,
            "evaluated_at": self.evaluated_at,
            "design_intent": self.design_intent.to_dict(),
            "native_artifact": self.native_artifact.to_dict(),
            "delivery_readiness": self.delivery_readiness.to_dict(),
            "legacy_overall_projection": self.legacy_overall_projection,
            "native_metrics": [item.to_dict() for item in self.native_metrics],
            "causal": self.causal.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "schema",
            "version",
            "evaluator_version",
            "campaign_id",
            "run_id",
            "case_id",
            "repetition",
            "source_run_schema",
            "source_run_version",
            "previous_evaluator_version",
            "previous_overall_state",
            "evaluated_at",
            "design_intent",
            "native_artifact",
            "delivery_readiness",
            "legacy_overall_projection",
            "native_metrics",
            "causal",
        }
        item = _closed(value, fields, path)
        if (
            item["schema"] != EVALUATION_V5_SCHEMA
            or item["version"] != EVALUATION_V5_VERSION
            or item["evaluator_version"] != EVALUATOR_V5
        ):
            raise ValidationError("unsupported BoardBench evaluator v5 artifact")
        raw_metrics = item["native_metrics"]
        if not isinstance(raw_metrics, list):
            raise ValidationError(f"{path}.native_metrics is invalid")
        source_version = _nonnegative_integer(
            item["source_run_version"], f"{path}.source_run_version"
        )
        if source_version is None:
            raise ValidationError(f"{path}.source_run_version is invalid")
        return cls(
            _identity(item["campaign_id"], f"{path}.campaign_id"),
            _identity(item["run_id"], f"{path}.run_id"),
            _identity(item["case_id"], f"{path}.case_id"),
            _nonnegative_integer(item["repetition"], f"{path}.repetition") or 0,
            _text(item["source_run_schema"], f"{path}.source_run_schema", limit=256),
            source_version,
            _text(
                item["previous_evaluator_version"],
                f"{path}.previous_evaluator_version",
                limit=512,
            ),
            _choice(
                item["previous_overall_state"],
                _LAYER_STATES,
                f"{path}.previous_overall_state",
            ),  # type: ignore[arg-type]
            _timestamp(item["evaluated_at"], f"{path}.evaluated_at"),
            EvaluationLayer.from_dict(item["design_intent"], f"{path}.design_intent"),
            EvaluationLayer.from_dict(
                item["native_artifact"], f"{path}.native_artifact"
            ),
            EvaluationLayer.from_dict(
                item["delivery_readiness"], f"{path}.delivery_readiness"
            ),
            _choice(
                item["legacy_overall_projection"],
                _LAYER_STATES,
                f"{path}.legacy_overall_projection",
            ),  # type: ignore[arg-type]
            tuple(
                NativeMetricEvidence.from_dict(entry, f"{path}.native_metrics[{index}]")
                for index, entry in enumerate(raw_metrics)
            ),
            CausalAttribution.from_dict(item["causal"], f"{path}.causal"),
        )


@dataclass(frozen=True, order=True)
class ElectricalEndpoint:
    """One logical endpoint and its observed electrical representation."""

    id: str
    state: EndpointState
    net: str | None

    def __post_init__(self) -> None:
        _endpoint_id(self.id, "electrical endpoint id")
        _choice(
            self.state,
            {"connected", "no_connect", "unconnected"},
            "electrical endpoint state",
        )
        if self.state == "connected":
            _text(self.net, "electrical endpoint net", limit=512)
        elif self.net is not None:
            raise ValidationError("non-connected electrical endpoint cannot name a net")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "net": self.net}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"id", "state", "net"}, path)
        return cls(
            _endpoint_id(item["id"], f"{path}.id"),
            _choice(
                item["state"],
                {"connected", "no_connect", "unconnected"},
                f"{path}.state",
            ),  # type: ignore[arg-type]
            _optional_text(item["net"], f"{path}.net", limit=512),
        )


@dataclass(frozen=True)
class ElectricalGraph:
    """A net-name-independent graph view; endpoint partitions carry topology."""

    endpoints: tuple[ElectricalEndpoint, ...]

    def __post_init__(self) -> None:
        if not self.endpoints or len(self.endpoints) > 4_096:
            raise ValidationError("electrical graph needs 1..4096 endpoints")
        identities = [item.id for item in self.endpoints]
        if len(identities) != len(set(identities)):
            raise ValidationError("electrical graph contains duplicate endpoints")

    @classmethod
    def build(cls, endpoints: Iterable[ElectricalEndpoint]) -> Self:
        return cls(tuple(sorted(endpoints, key=lambda item: item.id)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoints": [
                item.to_dict()
                for item in sorted(self.endpoints, key=lambda row: row.id)
            ]
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        item = _closed(value, {"endpoints"}, path)
        rows = item["endpoints"]
        if not isinstance(rows, list):
            raise ValidationError(f"{path}.endpoints is invalid")
        return cls.build(
            ElectricalEndpoint.from_dict(entry, f"{path}.endpoints[{index}]")
            for index, entry in enumerate(rows)
        )


def _pin_tokens(value: object, path: str, *, minimum: int = 2) -> tuple[str, ...]:
    values = _texts(value, path, minimum=minimum, maximum=8)
    for index, pin in enumerate(values):
        _text(pin, f"{path}[{index}]", limit=128)
        if "." in pin:
            raise ValidationError(f"{path}[{index}] is not a pin token")
    return values


@dataclass(frozen=True, order=True)
class TwoTerminalSymmetry:
    slot: str
    pins: tuple[str, str]

    def __post_init__(self) -> None:
        _identity(self.slot, "symmetric two-terminal slot")
        if len(self.pins) != 2 or len(set(self.pins)) != 2:
            raise ValidationError("symmetric two-terminal pins are invalid")
        for pin in self.pins:
            _text(pin, "symmetric two-terminal pin", limit=128)

    def to_dict(self) -> dict[str, Any]:
        return {"slot": self.slot, "pins": list(self.pins)}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"slot", "pins"}, path)
        pins = _pin_tokens(item["pins"], f"{path}.pins")
        if len(pins) != 2:
            raise ValidationError(f"{path}.pins must contain two pins")
        return cls(_identity(item["slot"], f"{path}.slot"), (pins[0], pins[1]))


@dataclass(frozen=True, order=True)
class SeriesTopology:
    """Explicitly interchangeable two-terminal slots in one declared series path."""

    slots: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 2 <= len(self.slots) <= 8 or len(set(self.slots)) != len(self.slots):
            raise ValidationError("series topology slots are invalid")
        for slot in self.slots:
            _identity(slot, "series topology slot")

    def to_dict(self) -> dict[str, Any]:
        return {"slots": list(self.slots)}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"slots"}, path)
        return cls(_texts(item["slots"], f"{path}.slots", minimum=2, maximum=8))


@dataclass(frozen=True, order=True)
class ConnectorSlotPermutation:
    """Slots the reference explicitly declares to have the same connector model."""

    model_id: str
    slots: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.model_id, "connector permutation model id", limit=512)
        if not 2 <= len(self.slots) <= 8 or len(set(self.slots)) != len(self.slots):
            raise ValidationError("connector permutation slots are invalid")
        for slot in self.slots:
            _identity(slot, "connector permutation slot")

    def to_dict(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "slots": list(self.slots)}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"model_id", "slots"}, path)
        return cls(
            _text(item["model_id"], f"{path}.model_id", limit=512),
            _texts(item["slots"], f"{path}.slots", minimum=2, maximum=8),
        )


@dataclass(frozen=True, order=True)
class UnspecifiedPinOrder:
    """Pins whose ordering the prompt/reference explicitly leaves unspecified."""

    slot: str
    pins: tuple[str, ...]

    def __post_init__(self) -> None:
        _identity(self.slot, "unspecified pin-order slot")
        if not 2 <= len(self.pins) <= 8 or len(set(self.pins)) != len(self.pins):
            raise ValidationError("unspecified pin-order pins are invalid")
        for pin in self.pins:
            _text(pin, "unspecified pin-order pin", limit=128)

    def to_dict(self) -> dict[str, Any]:
        return {"slot": self.slot, "pins": list(self.pins)}

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"slot", "pins"}, path)
        return cls(
            _identity(item["slot"], f"{path}.slot"),
            _pin_tokens(item["pins"], f"{path}.pins"),
        )


@dataclass(frozen=True)
class ElectricalEquivalencePolicy:
    """Closed reference-authored equivalence policy; no heuristic inference."""

    symmetric_two_terminal: tuple[TwoTerminalSymmetry, ...] = ()
    series_topologies: tuple[SeriesTopology, ...] = ()
    connector_slot_permutations: tuple[ConnectorSlotPermutation, ...] = ()
    unspecified_pin_orders: tuple[UnspecifiedPinOrder, ...] = ()
    legal_no_connect_forms: tuple[str, ...] = ("explicit",)

    def __post_init__(self) -> None:
        if tuple(sorted(self.symmetric_two_terminal)) != self.symmetric_two_terminal:
            raise ValidationError(
                "symmetric policies must be deterministically ordered"
            )
        if tuple(sorted(self.series_topologies)) != self.series_topologies:
            raise ValidationError("series policies must be deterministically ordered")
        if (
            tuple(sorted(self.connector_slot_permutations))
            != self.connector_slot_permutations
        ):
            raise ValidationError(
                "connector policies must be deterministically ordered"
            )
        if tuple(sorted(self.unspecified_pin_orders)) != self.unspecified_pin_orders:
            raise ValidationError(
                "pin-order policies must be deterministically ordered"
            )
        symmetric_slots = [item.slot for item in self.symmetric_two_terminal]
        pin_order_slots = [item.slot for item in self.unspecified_pin_orders]
        if len(symmetric_slots) != len(set(symmetric_slots)) or len(
            pin_order_slots
        ) != len(set(pin_order_slots)):
            raise ValidationError("electrical equivalence policy repeats a slot")
        if set(symmetric_slots).intersection(pin_order_slots):
            raise ValidationError("slot cannot be both symmetric and pin-unspecified")
        slot_groups = [item.slots for item in self.series_topologies]
        slot_groups.extend(item.slots for item in self.connector_slot_permutations)
        grouped = [slot for group in slot_groups for slot in group]
        if len(grouped) != len(set(grouped)):
            raise ValidationError("slot permutation groups overlap")
        allowed_forms = {"explicit", "unconnected", "omitted"}
        if (
            not self.legal_no_connect_forms
            or len(self.legal_no_connect_forms) != len(set(self.legal_no_connect_forms))
            or tuple(sorted(self.legal_no_connect_forms)) != self.legal_no_connect_forms
            or not set(self.legal_no_connect_forms) <= allowed_forms
        ):
            raise ValidationError("legal no-connect forms are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symmetric_two_terminal": [
                item.to_dict() for item in self.symmetric_two_terminal
            ],
            "series_topologies": [item.to_dict() for item in self.series_topologies],
            "connector_slot_permutations": [
                item.to_dict() for item in self.connector_slot_permutations
            ],
            "unspecified_pin_orders": [
                item.to_dict() for item in self.unspecified_pin_orders
            ],
            "legal_no_connect_forms": list(self.legal_no_connect_forms),
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "symmetric_two_terminal",
            "series_topologies",
            "connector_slot_permutations",
            "unspecified_pin_orders",
            "legal_no_connect_forms",
        }
        item = _closed(value, fields, path)

        def rows(name: str) -> list[object]:
            raw = item[name]
            if not isinstance(raw, list):
                raise ValidationError(f"{path}.{name} is invalid")
            return raw

        return cls(
            tuple(
                sorted(
                    TwoTerminalSymmetry.from_dict(
                        entry, f"{path}.symmetric_two_terminal[{index}]"
                    )
                    for index, entry in enumerate(rows("symmetric_two_terminal"))
                )
            ),
            tuple(
                sorted(
                    SeriesTopology.from_dict(
                        entry, f"{path}.series_topologies[{index}]"
                    )
                    for index, entry in enumerate(rows("series_topologies"))
                )
            ),
            tuple(
                sorted(
                    ConnectorSlotPermutation.from_dict(
                        entry, f"{path}.connector_slot_permutations[{index}]"
                    )
                    for index, entry in enumerate(rows("connector_slot_permutations"))
                )
            ),
            tuple(
                sorted(
                    UnspecifiedPinOrder.from_dict(
                        entry, f"{path}.unspecified_pin_orders[{index}]"
                    )
                    for index, entry in enumerate(rows("unspecified_pin_orders"))
                )
            ),
            tuple(
                sorted(
                    _texts(
                        item["legal_no_connect_forms"],
                        f"{path}.legal_no_connect_forms",
                        minimum=1,
                        maximum=3,
                        choices={"explicit", "unconnected", "omitted"},
                    )
                )
            ),
        )


DEFAULT_ELECTRICAL_EQUIVALENCE_POLICY = ElectricalEquivalencePolicy()


@dataclass(frozen=True)
class ElectricalGraphComparison:
    state: LayerState
    endpoint_mapping: tuple[tuple[str, str], ...]
    reasons: tuple[str, ...]
    candidates_checked: int

    def __post_init__(self) -> None:
        _choice(self.state, _LAYER_STATES, "electrical comparison state")
        if self.state == "pass" and self.reasons:
            raise ValidationError("passing electrical comparison cannot have reasons")
        if self.state != "pass" and not self.reasons:
            raise ValidationError("non-passing electrical comparison needs reasons")
        if tuple(sorted(self.endpoint_mapping)) != self.endpoint_mapping:
            raise ValidationError("electrical endpoint mapping is not ordered")
        sources = [source for source, _target in self.endpoint_mapping]
        targets = [target for _source, target in self.endpoint_mapping]
        if len(sources) != len(set(sources)) or len(targets) != len(set(targets)):
            raise ValidationError("electrical endpoint mapping is not bijective")
        for source, target in self.endpoint_mapping:
            _endpoint_id(source, "electrical endpoint mapping source")
            _endpoint_id(target, "electrical endpoint mapping target")
        checked = _nonnegative_integer(
            self.candidates_checked, "electrical comparison candidate count"
        )
        if checked is None or checked == 0:
            raise ValidationError("electrical comparison candidate count is invalid")
        if self.state == "unknown" and self.endpoint_mapping:
            raise ValidationError("unknown electrical comparison cannot pick a mapping")
        if self.state != "unknown" and not self.endpoint_mapping:
            raise ValidationError("decided electrical comparison needs a mapping")


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    slot, pin = endpoint.split(".", 1)
    return slot, pin


def _slot_pins(graph: ElectricalGraph) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for endpoint in graph.endpoints:
        slot, pin = _split_endpoint(endpoint.id)
        grouped.setdefault(slot, []).append(pin)
    return {slot: tuple(sorted(pins)) for slot, pins in grouped.items()}


def _validate_equivalence_policy(
    reference: ElectricalGraph, policy: ElectricalEquivalencePolicy
) -> None:
    pins = _slot_pins(reference)
    for symmetry in policy.symmetric_two_terminal:
        if pins.get(symmetry.slot) != tuple(sorted(symmetry.pins)):
            raise ValidationError(
                f"symmetric policy does not match reference slot {symmetry.slot}"
            )
    for order in policy.unspecified_pin_orders:
        if pins.get(order.slot) != tuple(sorted(order.pins)):
            raise ValidationError(
                f"pin-order policy does not match reference slot {order.slot}"
            )
    slot_groups = [item.slots for item in policy.series_topologies]
    slot_groups.extend(item.slots for item in policy.connector_slot_permutations)
    for group in slot_groups:
        unknown = [slot for slot in group if slot not in pins]
        if unknown:
            raise ValidationError(
                "slot permutation policy references unknown slots: "
                + ",".join(sorted(unknown))
            )
        shapes = {pins[slot] for slot in group}
        if len(shapes) != 1:
            raise ValidationError("slot permutation policy mixes incompatible pins")
    endpoint_index = {item.id: item for item in reference.endpoints}
    for series in policy.series_topologies:
        for slot in series.slots:
            rows = [endpoint_index[f"{slot}.{pin}"] for pin in pins.get(slot, ())]
            if len(rows) != 2 or any(row.state != "connected" for row in rows):
                raise ValidationError(
                    "series topology requires declared two-terminal connected slots"
                )
    if any(item.state == "unconnected" for item in reference.endpoints):
        raise ValidationError(
            "reference graph must use explicit no_connect, not unconnected"
        )


def _slot_mapping_options(
    reference: ElectricalGraph, policy: ElectricalEquivalencePolicy
) -> Iterable[dict[str, str]]:
    slots = sorted(_slot_pins(reference))
    groups = [item.slots for item in policy.series_topologies]
    groups.extend(item.slots for item in policy.connector_slot_permutations)

    def visit(index: int, mapping: dict[str, str]) -> Iterable[dict[str, str]]:
        if index == len(groups):
            yield dict(mapping)
            return
        group = groups[index]
        for target_slots in itertools.permutations(group):
            for source, target in zip(group, target_slots, strict=True):
                mapping[source] = target
            yield from visit(index + 1, mapping)
        for source in group:
            mapping[source] = source

    yield from visit(0, {slot: slot for slot in slots})


def _pin_mapping_options(
    reference: ElectricalGraph, policy: ElectricalEquivalencePolicy
) -> Iterable[dict[tuple[str, str], str]]:
    pins = _slot_pins(reference)
    choices: list[tuple[str, tuple[str, ...], str]] = []
    symmetry: dict[str, tuple[str, ...]] = {
        item.slot: item.pins for item in policy.symmetric_two_terminal
    }
    unspecified = {item.slot: item.pins for item in policy.unspecified_pin_orders}
    for slot in sorted(pins):
        original = pins[slot]
        if slot in symmetry:
            choices.append((slot, symmetry[slot], "symmetric"))
        elif slot in unspecified:
            choices.append((slot, unspecified[slot], "permuted"))
        else:
            choices.append((slot, original, "fixed"))

    def options(declared: tuple[str, ...], kind: str) -> Iterable[tuple[str, ...]]:
        if kind == "symmetric":
            yield declared
            yield tuple(reversed(declared))
        elif kind == "permuted":
            yield from itertools.permutations(declared)
        else:
            yield declared

    def visit(
        index: int, mapping: dict[tuple[str, str], str]
    ) -> Iterable[dict[tuple[str, str], str]]:
        if index == len(choices):
            yield dict(mapping)
            return
        slot, declared, kind = choices[index]
        for target_pins in options(declared, kind):
            for source_pin, target_pin in zip(pins[slot], target_pins, strict=True):
                mapping[(slot, source_pin)] = target_pin
            yield from visit(index + 1, mapping)

    yield from visit(0, {})


def _candidate_endpoint_mapping(
    reference: ElectricalGraph,
    slot_mapping: Mapping[str, str],
    pin_mapping: Mapping[tuple[str, str], str],
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for endpoint in sorted(reference.endpoints, key=lambda item: item.id):
        slot, pin = _split_endpoint(endpoint.id)
        result.append((endpoint.id, f"{slot_mapping[slot]}.{pin_mapping[(slot, pin)]}"))
    return tuple(result)


def _mapping_reasons(
    reference: ElectricalGraph,
    observed: ElectricalGraph,
    endpoint_mapping: tuple[tuple[str, str], ...],
    legal_no_connect_forms: frozenset[str],
) -> tuple[str, ...]:
    reference_index = {item.id: item for item in reference.endpoints}
    observed_index = {item.id: item for item in observed.endpoints}
    mapped = dict(endpoint_mapping)
    reasons: set[str] = set()
    connected_reference: list[ElectricalEndpoint] = []

    for reference_id, observed_id in endpoint_mapping:
        expected = reference_index[reference_id]
        actual = observed_index.get(observed_id)
        if expected.state == "connected":
            connected_reference.append(expected)
            if actual is None or actual.state != "connected":
                reasons.add(f"missing_required_endpoint:{reference_id}")
            continue
        if actual is None:
            if "omitted" not in legal_no_connect_forms:
                reasons.add(f"illegal_no_connect:{reference_id}:omitted")
        elif actual.state == "no_connect":
            if "explicit" not in legal_no_connect_forms:
                reasons.add(f"illegal_no_connect:{reference_id}:explicit")
        elif actual.state == "unconnected":
            if "unconnected" not in legal_no_connect_forms:
                reasons.add(f"illegal_no_connect:{reference_id}:unconnected")
        else:
            reasons.add(f"illegal_no_connect:{reference_id}:connected")

    mapped_targets = set(mapped.values())
    for endpoint_id in sorted(set(observed_index) - mapped_targets):
        reasons.add(f"unexpected_endpoint:{endpoint_id}")

    for index, left in enumerate(connected_reference):
        actual_left = observed_index.get(mapped[left.id])
        if actual_left is None or actual_left.state != "connected":
            continue
        for right in connected_reference[index + 1 :]:
            actual_right = observed_index.get(mapped[right.id])
            if actual_right is None or actual_right.state != "connected":
                continue
            expected_same = left.net == right.net
            actual_same = actual_left.net == actual_right.net
            if expected_same and not actual_same:
                reasons.add(f"open_circuit:{left.id},{right.id}")
            elif not expected_same and actual_same:
                reasons.add(f"unintended_merge:{left.id},{right.id}")
    return tuple(sorted(reasons))


def compare_electrical_graphs(
    reference: ElectricalGraph,
    observed: ElectricalGraph,
    policy: ElectricalEquivalencePolicy | None = None,
    *,
    max_mappings: int = MAX_GRAPH_MAPPINGS,
) -> ElectricalGraphComparison:
    """Compare endpoint partitions under only reference-declared transformations."""

    if (
        isinstance(max_mappings, bool)
        or not isinstance(max_mappings, int)
        or not 1 <= max_mappings <= MAX_GRAPH_MAPPINGS
    ):
        raise ValidationError("electrical equivalence search limit is invalid")
    active_policy = policy or DEFAULT_ELECTRICAL_EQUIVALENCE_POLICY
    _validate_equivalence_policy(reference, active_policy)
    checked = 0
    best: tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None = None
    for slot_mapping in _slot_mapping_options(reference, active_policy):
        for pin_mapping in _pin_mapping_options(reference, active_policy):
            checked += 1
            if checked > max_mappings:
                return ElectricalGraphComparison(
                    "unknown", (), ("equivalence_search_limit",), max_mappings
                )
            mapping = _candidate_endpoint_mapping(reference, slot_mapping, pin_mapping)
            reasons = _mapping_reasons(
                reference,
                observed,
                mapping,
                frozenset(active_policy.legal_no_connect_forms),
            )
            if not reasons:
                return ElectricalGraphComparison("pass", mapping, (), checked)
            candidate = (reasons, mapping)
            if best is None or (len(reasons), reasons, mapping) < (
                len(best[0]),
                best[0],
                best[1],
            ):
                best = candidate
    if best is None:
        raise ValidationError("electrical equivalence policy produced no mappings")
    return ElectricalGraphComparison("fail", best[1], best[0], checked)


def _metric_index(score: BoardBenchScore) -> dict[str, MetricResult]:
    return {item.name: item for item in score.metrics}


def _automatic_state(metric: MetricResult) -> LayerState | None:
    if metric.state == "not_applicable":
        return None
    if metric.state not in _LAYER_STATES:
        raise ValidationError("legacy automatic metric state is invalid")
    return metric.state  # type: ignore[return-value]


def _review_state(value: str) -> LayerState:
    return value if value in _LAYER_STATES else "unknown"  # type: ignore[return-value]


def _physical_state(value: str) -> LayerState:
    return value if value in {"pass", "fail"} else "unknown"  # type: ignore[return-value]


def _default_native_metrics(
    score: BoardBenchScore,
    *,
    native_consistency_state: LayerState,
) -> tuple[NativeMetricEvidence, ...]:
    metrics = _metric_index(score)
    states: dict[str, LayerState] = {
        "complete_project": _automatic_state(metrics["complete_project"]) or "unknown",
        "library_resolution": _automatic_state(metrics["library_resolution"])
        or "unknown",
        "native_consistency": native_consistency_state,
        "unresolved_connections": "unknown",
        "erc": _automatic_state(metrics["erc"]) or "unknown",
        "drc": _automatic_state(metrics["drc"]) or "unknown",
    }
    sources = {
        "complete_project": "legacy_score:complete_project",
        "library_resolution": "legacy_score:library_resolution",
        "native_consistency": "native_consistency_adapter",
        "unresolved_connections": "unavailable_in_legacy_score",
        "erc": "legacy_score:erc",
        "drc": "legacy_score:drc",
    }
    return tuple(
        NativeMetricEvidence(name, states[name], None, sources[name])
        for name in NATIVE_METRIC_NAMES
    )


def _native_layer(metrics: Sequence[NativeMetricEvidence]) -> EvaluationLayer:
    by_name = {item.name: item for item in metrics}
    source_for_metric = {
        "complete_project": "native_project_parse",
        "library_resolution": "native_library_resolution",
        "native_consistency": "native_consistency",
        "unresolved_connections": "native_connectivity",
        "erc": "kicad_erc",
        "drc": "kicad_drc",
    }
    return EvaluationLayer.build(
        "native_artifact",
        [
            EvidenceFinding(
                source_for_metric[name],
                "automatic",
                by_name[name].state,
                f"{name}:{by_name[name].source}",
            )
            for name in NATIVE_METRIC_NAMES
        ],
    )


def _review_identity(
    score: BoardBenchScore,
    review: BoardBenchReview | None,
    hardware: BoardBenchHardware | None,
) -> None:
    for evidence, label in ((review, "review"), (hardware, "hardware")):
        if evidence is not None and (
            evidence.campaign_id != score.campaign_id or evidence.run_id != score.run_id
        ):
            raise ValidationError(f"v5 {label} identity differs from source run")
    if review is not None:
        if (
            review.source_campaign_sha256 != score.source_campaign_sha256
            or review.source_case_sha256 != score.source_case_sha256
            or review.source_run_sha256 != score.source_run_sha256
        ):
            raise ValidationError("v5 review source binding differs from source score")
        if (
            review.source_score_sha256 is not None
            and review.source_score_sha256 != artifact_sha256(score)
        ):
            raise ValidationError("v5 review score binding differs from source score")


def _hardware_findings(hardware: BoardBenchHardware) -> tuple[EvidenceFinding, ...]:
    rail_states = [item.state for item in hardware.rails]
    rail_state: LayerState = (
        "fail"
        if "fail" in rail_states
        else "unknown"
        if "not_tested" in rail_states
        else "pass"
    )
    rows: list[tuple[str, LayerState]] = []
    for source, value in (
        ("physical_manufacturing", hardware.fabricator_accepted),
        ("physical_assembly", hardware.solderability),
        ("physical_first_power", hardware.first_power_no_short),
        ("physical_firmware", hardware.firmware_download),
        ("physical_core_function", hardware.core_function),
    ):
        if value != "not_applicable":
            rows.append((source, _physical_state(value)))
    rows.append(("physical_power_rails", rail_state))
    return tuple(
        EvidenceFinding(source, "physical", state, f"hardware:{source}:{state}")
        for source, state in rows
    )


_LEGACY_STAGE_MAP = {
    "requirements_understanding": EngineeringStage.REQUIREMENTS_FROZEN.value,
    "component_knowledge": EngineeringStage.SCHEMATIC_SEMANTIC.value,
    "circuit_design": EngineeringStage.SCHEMATIC_SEMANTIC.value,
    "kicad_materialization": EngineeringStage.NATIVE_SCHEMATIC_CONFIRMED.value,
    "layout": EngineeringStage.PLACEMENT.value,
    "routing": EngineeringStage.ROUTING.value,
    "validation": EngineeringStage.ERC_DRC.value,
}
_LEGACY_CAUSE_MAP = {
    "model_reasoning": "model_reasoning",
    "component_knowledge_gap": "component_knowledge_gap",
    "deterministic_tool_defect": "deterministic_tool_defect",
    "environment_infrastructure": "environment_infrastructure",
    "insufficient_evidence": "insufficient_evidence",
    "human_process": "human_intervention_required",
    "unclassified": "insufficient_evidence",
}


def _legacy_failure_signals(score: BoardBenchScore) -> tuple[CausalSignal, ...]:
    failure = score.failure_suggestion
    if failure is None or failure.stage == "unclassified":
        return ()
    stage = _LEGACY_STAGE_MAP[failure.stage]
    return tuple(
        CausalSignal(stage, _LEGACY_CAUSE_MAP[cause]) for cause in failure.causes
    )


def _graph_failure_signals(
    comparison: ElectricalGraphComparison | None,
) -> tuple[CausalSignal, ...]:
    if comparison is None or comparison.state != "fail":
        return ()
    symptoms: set[str] = set()
    for reason in comparison.reasons:
        if reason.startswith(("open_circuit:", "missing_required_endpoint:")):
            symptoms.add("open_circuit")
        elif reason.startswith("unintended_merge:"):
            symptoms.add("short_circuit")
        elif reason.startswith("unexpected_endpoint:"):
            symptoms.add("wrong_net")
    rows = [
        CausalSignal(
            EngineeringStage.SCHEMATIC_SEMANTIC.value,
            "circuit_design_error",
        )
    ]
    rows.extend(
        CausalSignal(EngineeringStage.SCHEMATIC_SEMANTIC.value, symptom=code)
        for code in sorted(symptoms, key=_SYMPTOM_INDEX.__getitem__)
    )
    return tuple(rows)


def evaluation_v5_from_legacy_score(
    score: BoardBenchScore,
    run: NormalizedBoardBenchRun,
    *,
    evaluated_at: str,
    electrical_comparison: ElectricalGraphComparison | None = None,
    native_metrics: Sequence[NativeMetricEvidence] | None = None,
    native_consistency_state: LayerState = "unknown",
    human_review: BoardBenchReview | None = None,
    hardware: BoardBenchHardware | None = None,
    ai_functional_state: LayerState | None = None,
    ai_orderability_state: LayerState | None = None,
    progress: ProgressVector | None = None,
    routing_failures: Sequence[RoutingFailure] = (),
    native_mismatch_codes: Sequence[str] = (),
    evaluator_signals: Sequence[CausalSignal] = (),
) -> BoardBenchEvaluationV5:
    """Adapt existing score/evidence without treating its overall state as v5 truth."""

    if score.campaign_id != run.campaign_id or score.run_id != run.run_id:
        raise ValidationError("v5 source score identity differs from normalized run")
    _identity(run.case_id, "v5 normalized run case id")
    if isinstance(run.repetition, bool) or run.repetition not in {1, 2, 3}:
        raise ValidationError("v5 normalized run repetition is invalid")
    _review_identity(score, human_review, hardware)
    _choice(native_consistency_state, _LAYER_STATES, "native consistency state")
    for value, label in (
        (ai_functional_state, "AI functional state"),
        (ai_orderability_state, "AI orderability state"),
    ):
        if value is not None:
            _choice(value, _LAYER_STATES, label)

    metric_index = _metric_index(score)
    topology_state = (
        electrical_comparison.state
        if electrical_comparison is not None
        else _automatic_state(metric_index["reference_topology"]) or "unknown"
    )
    topology_reason = (
        "electrical_graph_equivalence"
        if electrical_comparison is not None and electrical_comparison.state == "pass"
        else ";".join(electrical_comparison.reasons)
        if electrical_comparison is not None
        else metric_index["reference_topology"].reason
    )
    design_findings = [
        EvidenceFinding(
            "automatic_topology", "automatic", topology_state, topology_reason
        )
    ]
    for metric_name, source in (
        ("support_circuits", "automatic_support_circuits"),
        ("ratings", "automatic_ratings"),
    ):
        state = _automatic_state(metric_index[metric_name])
        if state is not None:
            design_findings.append(
                EvidenceFinding(
                    source,
                    "automatic",
                    state,
                    metric_index[metric_name].reason,
                )
            )
    if ai_functional_state is not None:
        design_findings.append(
            EvidenceFinding(
                "ai_functional_review",
                "ai_review",
                ai_functional_state,
                "AI-reviewed functional assessment",
            )
        )
    if human_review is not None:
        design_findings.append(
            EvidenceFinding(
                "human_engineer_functional_review",
                "human_engineer",
                _review_state(human_review.functional_correctness),
                "canonical human engineer functional review",
            )
        )
    design_layer = EvaluationLayer.build("design_intent", design_findings)

    materialized_metrics = tuple(
        native_metrics
        if native_metrics is not None
        else _default_native_metrics(
            score, native_consistency_state=native_consistency_state
        )
    )
    if (
        len(materialized_metrics) != len(NATIVE_METRIC_NAMES)
        or tuple(item.name for item in materialized_metrics) != NATIVE_METRIC_NAMES
    ):
        raise ValidationError(
            "v5 native metric adapter input is incomplete or unordered"
        )
    native_layer = _native_layer(materialized_metrics)

    delivery_findings: list[EvidenceFinding] = []
    if ai_orderability_state is not None:
        delivery_findings.append(
            EvidenceFinding(
                "ai_orderability_review",
                "ai_review",
                ai_orderability_state,
                "AI-reviewed orderability estimate; not human evidence",
            )
        )
    if human_review is not None:
        delivery_findings.append(
            EvidenceFinding(
                "human_engineer_orderability",
                "human_engineer",
                _review_state(human_review.orderable_state),
                "canonical human engineer orderability review",
            )
        )
    if hardware is not None:
        delivery_findings.extend(_hardware_findings(hardware))
    if not delivery_findings:
        delivery_findings.append(
            EvidenceFinding(
                "unavailable",
                "automatic",
                "unknown",
                "human engineering and physical evidence unavailable",
            )
        )
    delivery_layer = EvaluationLayer.build("delivery_readiness", delivery_findings)

    signals = (
        *evaluator_signals,
        *_legacy_failure_signals(score),
        *_graph_failure_signals(electrical_comparison),
    )
    causal = derive_causal_attribution(
        run,
        progress=progress,
        routing_failures=routing_failures,
        native_mismatch_codes=native_mismatch_codes,
        evaluator_signals=signals,
    )
    return BoardBenchEvaluationV5(
        score.campaign_id,
        score.run_id,
        run.case_id,
        run.repetition,
        run.source_schema,
        run.source_version,
        score.evaluator_version,
        score.overall_state,  # type: ignore[arg-type]
        _timestamp(evaluated_at, "v5 evaluated_at"),
        design_layer,
        native_layer,
        delivery_layer,
        _combine_states([design_layer.state, native_layer.state]),
        materialized_metrics,
        causal,
    )


@dataclass(frozen=True)
class EvaluationSummaryV5:
    campaign_id: str
    run_id: str
    case_id: str
    repetition: int
    design_intent: LayerState
    native_artifact: LayerState
    delivery_readiness: LayerState
    legacy_overall_projection: LayerState

    def __post_init__(self) -> None:
        _identity(self.campaign_id, "v5 summary campaign id")
        _identity(self.run_id, "v5 summary run id")
        _identity(self.case_id, "v5 summary case id")
        if isinstance(self.repetition, bool) or self.repetition not in {1, 2, 3}:
            raise ValidationError("v5 summary repetition is invalid")
        for value, label in (
            (self.design_intent, "design intent"),
            (self.native_artifact, "native artifact"),
            (self.delivery_readiness, "delivery readiness"),
            (self.legacy_overall_projection, "legacy overall projection"),
        ):
            _choice(value, _LAYER_STATES, f"v5 summary {label}")
        if self.legacy_overall_projection != _combine_states(
            [self.design_intent, self.native_artifact]
        ):
            raise ValidationError("v5 summary legacy projection is inconsistent")

    @classmethod
    def from_evaluation(cls, value: BoardBenchEvaluationV5) -> Self:
        return cls(
            value.campaign_id,
            value.run_id,
            value.case_id,
            value.repetition,
            value.design_intent.state,
            value.native_artifact.state,
            value.delivery_readiness.state,
            value.legacy_overall_projection,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "repetition": self.repetition,
            "design_intent": self.design_intent,
            "native_artifact": self.native_artifact,
            "delivery_readiness": self.delivery_readiness,
            "legacy_overall_projection": self.legacy_overall_projection,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        fields = {
            "campaign_id",
            "run_id",
            "case_id",
            "repetition",
            "design_intent",
            "native_artifact",
            "delivery_readiness",
            "legacy_overall_projection",
        }
        item = _closed(value, fields, path)
        return cls(
            _identity(item["campaign_id"], f"{path}.campaign_id"),
            _identity(item["run_id"], f"{path}.run_id"),
            _identity(item["case_id"], f"{path}.case_id"),
            _nonnegative_integer(item["repetition"], f"{path}.repetition") or 0,
            _choice(item["design_intent"], _LAYER_STATES, f"{path}.design_intent"),  # type: ignore[arg-type]
            _choice(item["native_artifact"], _LAYER_STATES, f"{path}.native_artifact"),  # type: ignore[arg-type]
            _choice(
                item["delivery_readiness"],
                _LAYER_STATES,
                f"{path}.delivery_readiness",
            ),  # type: ignore[arg-type]
            _choice(
                item["legacy_overall_projection"],
                _LAYER_STATES,
                f"{path}.legacy_overall_projection",
            ),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class NativeMetricDelta:
    name: str
    old: NativeMetricEvidence
    new: NativeMetricEvidence
    value_delta: int | None

    def __post_init__(self) -> None:
        _choice(self.name, NATIVE_METRIC_NAMES, "native metric delta name")
        if self.old.name != self.name or self.new.name != self.name:
            raise ValidationError("native metric delta names differ")
        expected = (
            self.new.value - self.old.value
            if self.old.value is not None and self.new.value is not None
            else None
        )
        if self.value_delta != expected:
            raise ValidationError("native metric value delta is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "old": self.old.to_dict(),
            "new": self.new.to_dict(),
            "value_delta": self.value_delta,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"name", "old", "new", "value_delta"}, path)
        return cls(
            _choice(item["name"], NATIVE_METRIC_NAMES, f"{path}.name"),
            NativeMetricEvidence.from_dict(item["old"], f"{path}.old"),
            NativeMetricEvidence.from_dict(item["new"], f"{path}.new"),
            (
                None
                if item["value_delta"] is None
                else _signed_integer(item["value_delta"], f"{path}.value_delta")
            ),
        )


def _signed_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{path} must be an integer")
    return value


@dataclass(frozen=True)
class EvaluatorCalibrationDelta:
    cohort: str
    previous_evaluator_version: str
    previous_overall_state: LayerState
    v5_legacy_projection: LayerState
    changed: bool

    def __post_init__(self) -> None:
        _choice(self.cohort, {"old", "new"}, "evaluator delta cohort")
        _text(
            self.previous_evaluator_version,
            "previous evaluator version",
            limit=512,
        )
        _choice(
            self.previous_overall_state,
            _LAYER_STATES,
            "previous evaluator state",
        )
        _choice(self.v5_legacy_projection, _LAYER_STATES, "v5 evaluator projection")
        if self.changed != (self.previous_overall_state != self.v5_legacy_projection):
            raise ValidationError("evaluator calibration delta is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort,
            "previous_evaluator_version": self.previous_evaluator_version,
            "previous_overall_state": self.previous_overall_state,
            "v5_legacy_projection": self.v5_legacy_projection,
            "changed": self.changed,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "cohort",
                "previous_evaluator_version",
                "previous_overall_state",
                "v5_legacy_projection",
                "changed",
            },
            path,
        )
        if not isinstance(item["changed"], bool):
            raise ValidationError(f"{path}.changed must be boolean")
        return cls(
            _choice(item["cohort"], {"old", "new"}, f"{path}.cohort"),
            _text(
                item["previous_evaluator_version"],
                f"{path}.previous_evaluator_version",
                limit=512,
            ),
            _choice(
                item["previous_overall_state"],
                _LAYER_STATES,
                f"{path}.previous_overall_state",
            ),  # type: ignore[arg-type]
            _choice(
                item["v5_legacy_projection"],
                _LAYER_STATES,
                f"{path}.v5_legacy_projection",
            ),  # type: ignore[arg-type]
            item["changed"],
        )


@dataclass(frozen=True)
class BoardBenchComparisonV5:
    old_v5: EvaluationSummaryV5
    new_v5: EvaluationSummaryV5
    raw_native_metric_delta: tuple[NativeMetricDelta, ...]
    evaluator_delta: tuple[EvaluatorCalibrationDelta, ...]

    def __post_init__(self) -> None:
        if self.old_v5.campaign_id == self.new_v5.campaign_id:
            raise ValidationError("v5 comparison needs distinct old/new campaigns")
        if (
            self.old_v5.case_id != self.new_v5.case_id
            or self.old_v5.repetition != self.new_v5.repetition
        ):
            raise ValidationError(
                "v5 comparison old/new case or repetition identity differs"
            )
        if self.old_v5.run_id != self.new_v5.run_id:
            raise ValidationError("v5 comparison old/new run identity differs")
        if (
            len(self.raw_native_metric_delta) != len(NATIVE_METRIC_NAMES)
            or tuple(item.name for item in self.raw_native_metric_delta)
            != NATIVE_METRIC_NAMES
        ):
            raise ValidationError("v5 comparison native metrics are incomplete")
        cohorts = tuple(item.cohort for item in self.evaluator_delta)
        if cohorts != ("old", "new"):
            raise ValidationError(
                "v5 comparison needs ordered old/new evaluator calibration"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": COMPARISON_V5_SCHEMA,
            "version": COMPARISON_V5_VERSION,
            "evaluator_version": EVALUATOR_V5,
            "same_evaluator_v5": {
                "old": self.old_v5.to_dict(),
                "new": self.new_v5.to_dict(),
            },
            "raw_native_metric_delta": [
                item.to_dict() for item in self.raw_native_metric_delta
            ],
            "evaluator_delta": [item.to_dict() for item in self.evaluator_delta],
        }

    @classmethod
    def from_dict(cls, value: object, path: str = "$") -> Self:
        fields = {
            "schema",
            "version",
            "evaluator_version",
            "same_evaluator_v5",
            "raw_native_metric_delta",
            "evaluator_delta",
        }
        item = _closed(value, fields, path)
        if (
            item["schema"] != COMPARISON_V5_SCHEMA
            or item["version"] != COMPARISON_V5_VERSION
            or item["evaluator_version"] != EVALUATOR_V5
        ):
            raise ValidationError("unsupported BoardBench v5 comparison artifact")
        summaries = _closed(
            item["same_evaluator_v5"],
            {"old", "new"},
            f"{path}.same_evaluator_v5",
        )
        raw_metrics = item["raw_native_metric_delta"]
        raw_deltas = item["evaluator_delta"]
        if not isinstance(raw_metrics, list) or not isinstance(raw_deltas, list):
            raise ValidationError("v5 comparison arrays are malformed")
        return cls(
            EvaluationSummaryV5.from_dict(
                summaries["old"], f"{path}.same_evaluator_v5.old"
            ),
            EvaluationSummaryV5.from_dict(
                summaries["new"], f"{path}.same_evaluator_v5.new"
            ),
            tuple(
                NativeMetricDelta.from_dict(
                    entry, f"{path}.raw_native_metric_delta[{index}]"
                )
                for index, entry in enumerate(raw_metrics)
            ),
            tuple(
                EvaluatorCalibrationDelta.from_dict(
                    entry, f"{path}.evaluator_delta[{index}]"
                )
                for index, entry in enumerate(raw_deltas)
            ),
        )


def compare_evaluations_v5(
    old: BoardBenchEvaluationV5,
    new: BoardBenchEvaluationV5,
) -> BoardBenchComparisonV5:
    """Separate raw KiCad deltas, same-v5 results, and evaluator calibration."""

    old_metrics = {item.name: item for item in old.native_metrics}
    new_metrics = {item.name: item for item in new.native_metrics}

    def value_delta(name: str) -> int | None:
        old_value = old_metrics[name].value
        new_value = new_metrics[name].value
        if old_value is None or new_value is None:
            return None
        return new_value - old_value

    native_delta = tuple(
        NativeMetricDelta(
            name,
            old_metrics[name],
            new_metrics[name],
            value_delta(name),
        )
        for name in NATIVE_METRIC_NAMES
    )
    calibration: list[EvaluatorCalibrationDelta] = []
    for cohort, evaluation in (
        ("old", old),
        ("new", new),
    ):
        calibration.append(
            EvaluatorCalibrationDelta(
                cohort,
                evaluation.previous_evaluator_version,
                evaluation.previous_overall_state,
                evaluation.legacy_overall_projection,
                evaluation.previous_overall_state
                != evaluation.legacy_overall_projection,
            )
        )
    return BoardBenchComparisonV5(
        EvaluationSummaryV5.from_evaluation(old),
        EvaluationSummaryV5.from_evaluation(new),
        native_delta,
        tuple(calibration),
    )


def _safe_output_namespace(output_root: str | Path) -> Path:
    if not isinstance(output_root, (str, Path)) or "\x00" in str(output_root):
        raise ValidationError("BoardBench evaluator v5 output path is unsafe")
    raw = Path(output_root).expanduser()
    if raw.name in {"", ".", ".."} or any(part in {".", ".."} for part in raw.parts):
        raise ValidationError("BoardBench evaluator v5 output path is unsafe")
    root = raw.absolute()
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValidationError("BoardBench evaluator v5 output traverses a symlink")
    make_directory(root)
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValidationError("BoardBench evaluator v5 output traverses a symlink")
    namespace = root / EVALUATOR_V5_DIRECTORY
    if namespace.is_symlink():
        raise ValidationError("BoardBench evaluator v5 namespace is unsafe")
    make_directory(namespace)
    if namespace.is_symlink():
        raise ValidationError("BoardBench evaluator v5 namespace is unsafe")
    return namespace


def _publish_fresh_json_directory(
    parent: Path,
    identity: str,
    filename: str,
    document: Mapping[str, Any],
) -> Path:
    _identity(identity, "BoardBench evaluator v5 output identity")
    target = parent / identity
    if target.exists() or target.is_symlink():
        raise ValidationError("BoardBench evaluator v5 output already exists")
    staging = Path(tempfile.mkdtemp(prefix=f".{identity}.", dir=parent))
    published = False
    try:
        atomic_write_json(staging / filename, dict(document), mode=0o600)
        if target.exists() or target.is_symlink():
            raise ValidationError("BoardBench evaluator v5 output already exists")
        try:
            os.rename(staging, target)
        except OSError as exc:
            raise PCBDraftError(
                "cannot publish BoardBench evaluator v5 output"
            ) from exc
        published = True
        return target / filename
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


def write_evaluation_v5(
    output_root: str | Path, evaluation: BoardBenchEvaluationV5
) -> Path:
    """Write only ``evaluator-v5/<run-id>/evaluation.json`` as a fresh artifact."""

    namespace = _safe_output_namespace(output_root)
    return _publish_fresh_json_directory(
        namespace,
        evaluation.run_id,
        EVALUATION_V5_FILENAME,
        evaluation.to_dict(),
    )


def write_comparison_v5(
    output_root: str | Path,
    comparison_id: str,
    comparison: BoardBenchComparisonV5,
) -> Path:
    """Write one fresh comparison below the separate evaluator-v5 namespace."""

    namespace = _safe_output_namespace(output_root) / "comparisons"
    if namespace.is_symlink():
        raise ValidationError("BoardBench evaluator v5 comparison path is unsafe")
    make_directory(namespace)
    return _publish_fresh_json_directory(
        namespace,
        comparison_id,
        COMPARISON_V5_FILENAME,
        comparison.to_dict(),
    )


def _safe_artifact_path(path: str | Path, filename: str) -> Path:
    if not isinstance(path, (str, Path)) or "\x00" in str(path):
        raise ValidationError("BoardBench evaluator v5 input path is unsafe")
    raw = Path(path).expanduser()
    if any(part == ".." for part in raw.parts):
        raise ValidationError("BoardBench evaluator v5 input path is unsafe")
    target = raw.absolute()
    if target.is_dir():
        target /= filename
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ValidationError("BoardBench evaluator v5 artifact path is unsafe")
    return target


def load_evaluation_v5(path: str | Path) -> BoardBenchEvaluationV5:
    target = _safe_artifact_path(path, EVALUATION_V5_FILENAME)
    return BoardBenchEvaluationV5.from_dict(
        load_json_limited(target, EVALUATION_FILE_LIMIT)
    )


def load_comparison_v5(path: str | Path) -> BoardBenchComparisonV5:
    target = _safe_artifact_path(path, COMPARISON_V5_FILENAME)
    return BoardBenchComparisonV5.from_dict(
        load_json_limited(target, EVALUATION_FILE_LIMIT)
    )
