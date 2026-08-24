"""Deterministic BoardBench integration and comparison-campaign preflight.

This module deliberately does not run a real model campaign.  Its one small
fixture sends natural-language text through the ordinary durable Agent tool
protocol, while an explicitly labelled fake producer chooses a fixed sequence
of bounded PCB tools.  Every downstream semantic transaction, KiCad write,
native consistency read, ERC/DRC check, product terminal receipt, BoardBench
v2 receipt, and evaluator-v5 artifact is real.

The future comparison manifest is a frozen *plan*, not execution authority.
Its 60 identities are copied from a caller-supplied, read-only BoardBench
campaign.  Prompts, references, and historical traces never enter this module.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self, cast

from pcbdraft.agent.orchestrator import AgentOrchestrator
from pcbdraft.agent.permissions import PermissionBroker
from pcbdraft.agent.policy import ConversationStep, ProposedToolCall
from pcbdraft.agent.turns import ToolRunStatus, TurnRecord
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited, make_directory
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.kicad.consistency import (
    NativeConsistencyReport,
    inspect_native_consistency,
)
from pcbdraft.kicad.routing import (
    GridRouter,
    RouteSegment,
    RoutingFailure,
    RoutingFailureError,
    RoutingPad,
)
from pcbdraft.model.providers import IntentProvider
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.managed import open_managed_project
from pcbdraft.services.progress import (
    ConvergenceObservation,
    EngineeringStage,
    EvidenceStatus,
    ProcessStatus,
    ProductSessionTerminalReceipt,
    ProgressClassification,
    TaskOutcome,
    evaluate_convergence,
)
from pcbdraft.verification.boardbench import (
    BoardBenchCampaign,
    CampaignRunPlan,
    build_inventory,
)
from pcbdraft.verification.boardbench_evaluator import (
    EVALUATOR_VERSION as LEGACY_EVALUATOR_VERSION,
)
from pcbdraft.verification.boardbench_evaluator_v5 import (
    EVALUATOR_V5,
    BoardBenchEvaluationV5,
    EvaluationLayer,
    EvaluationSummaryV5,
    EvidenceFinding,
    NativeMetricEvidence,
    derive_causal_attribution,
    load_evaluation_v5,
    write_evaluation_v5,
)
from pcbdraft.verification.boardbench_v2 import (
    BUDGET_NAMES,
    MODEL_TURN_LIMIT,
    PCB_TOOL_CALL_LIMIT,
    RUN_V2_SCHEMA,
    WALL_TIME_LIMIT_SECONDS,
    ActualCostEvidence,
    BoardBenchRunV2,
    BudgetDimension,
    ContextQualityEvidence,
    NormalizedBoardBenchRun,
    load_run_v2,
    normalize_v2_run,
    start_run_v2,
    store_run_v2,
)

COMPARISON_PREFLIGHT_MANIFEST_SCHEMA = (
    "pcbdraft-boardbench-comparison-preflight-manifest"
)
COMPARISON_PREFLIGHT_MANIFEST_VERSION = 1
FORMAL_COMPARISON_LAUNCH_SCHEMA = "pcbdraft-boardbench-formal-comparison-launch"
FORMAL_COMPARISON_LAUNCH_VERSION = 1
DETERMINISTIC_PREFLIGHT_REPORT_SCHEMA = (
    "pcbdraft-deterministic-boardbench-preflight-report"
)
DETERMINISTIC_PREFLIGHT_REPORT_VERSION = 1
PREFLIGHT_FILE_LIMIT = 8 * 1024 * 1024

FORMAL_PROVIDER = "openai-codex"
FORMAL_MODEL = "gpt-5.6-luna"
FORMAL_KICAD_VERSION = "10.0.5"
FORMAL_TIER = "boardbench-tier-a"
PREFLIGHT_NAMESPACE = "deterministic-preflight"
PREFLIGHT_CAMPAIGN_ID = "deterministic-preflight"
PREFLIGHT_CASE_ID = "ground-distribution-adapter"
PREFLIGHT_RUN_ID = "deterministic-preflight-ground-adapter-run-1"
PREFLIGHT_REQUEST = (
    "Create a 30 mm by 20 mm four-contact GND distribution adapter using two "
    "2-pin headers. Tie every contact to GND, place and route the board, then "
    "run ERC and DRC."
)
PREFLIGHT_LIMITATIONS = (
    "fake_provider",
    "not_formal_campaign",
    "no_human_review",
    "no_hardware",
)

ERC_COMMAND = (
    "kicad-cli",
    "sch",
    "erc",
    "--format",
    "json",
    "--severity-error",
    "--severity-warning",
    "--output",
    "<output>",
    "<schematic>",
)
DRC_COMMAND = (
    "kicad-cli",
    "pcb",
    "drc",
    "--format",
    "json",
    "--severity-error",
    "--severity-warning",
    "--output",
    "<output>",
    "<board>",
)
GATE_RULE_IDENTITY = "kicad-cli-error-and-warning-no-preflight-waivers"

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_FAILURE_CODES = frozenset(
    {
        "invalid_seed",
        "zero_length_seed",
        "native_zero_copper",
        "native_connectivity_failed",
        "unintended_net_merge",
        "routed_footprint_transform_unsupported",
        "repeated_retry_key",
    }
)


def _uses_preflight_namespace(value: str) -> bool:
    return value == PREFLIGHT_NAMESPACE or value.startswith(f"{PREFLIGHT_NAMESPACE}-")


def _validate_formal_run_plan(
    planned_runs: tuple[CampaignRunPlan, ...], *, label: str
) -> None:
    """Require the exact formal denominator independently at every artifact boundary."""

    if len(planned_runs) != 60:
        raise ValidationError(f"{label} must plan exactly 60 runs")
    identities = [item.run_id for item in planned_runs]
    if len(identities) != len(set(identities)):
        raise ValidationError(f"{label} has duplicate run ids")
    cases: dict[str, set[int]] = {}
    for run in planned_runs:
        cases.setdefault(run.case_id, set()).add(run.repetition)
    if len(cases) != 20 or any(values != {1, 2, 3} for values in cases.values()):
        raise ValidationError(f"{label} is not an exact 20 x 3 plan")
    if any(
        _uses_preflight_namespace(identity)
        for run in planned_runs
        for identity in (run.run_id, run.case_id)
    ):
        raise ValidationError("preflight namespace entered the formal denominator")


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValidationError(f"{label} is invalid")
    return value


def _text(value: object, label: str, *, limit: int = 8_192) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{label} must be bounded non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label} must be bounded non-empty text") from exc
    if len(encoded) > limit:
        raise ValidationError(f"{label} must be bounded non-empty text")
    return value


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label, limit=128)
    if _TIMESTAMP.fullmatch(text) is None:
        raise ValidationError(f"{label} is invalid")
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    return text


def _closed(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"{label} has unexpected fields")
    return value


def _relative_artifact(value: object, label: str) -> str:
    text = _text(value, label, limit=1_024)
    path = Path(text)
    if (
        "\x00" in text
        or "\\" in text
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != text
    ):
        raise ValidationError(f"{label} is unsafe")
    return path.as_posix()


def _safe_root(value: str | Path, label: str) -> Path:
    if not isinstance(value, (str, Path)) or "\x00" in str(value):
        raise ValidationError(f"{label} path is unsafe")
    raw = Path(value).expanduser()
    if raw.name in {"", ".", ".."} or any(part == ".." for part in raw.parts):
        raise ValidationError(f"{label} path is unsafe")
    target = raw.absolute()
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ValidationError(f"{label} path traverses a symbolic link")
    return target


def _make_safe_root(value: str | Path, label: str) -> Path:
    target = _safe_root(value, label)
    make_directory(target)
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ValidationError(f"{label} path is unsafe")
    return target


def _publish_fresh_directory(
    parent: Path,
    identity: str,
    filename: str,
    document: Mapping[str, Any],
    *,
    drift_label: str,
) -> Path:
    _identity(identity, f"{drift_label} identity")
    parent = _make_safe_root(parent, drift_label)
    target = parent / identity
    if target.exists() or target.is_symlink():
        existing_path = target / filename
        if existing_path.is_file() and not existing_path.is_symlink():
            existing = load_json_limited(existing_path, PREFLIGHT_FILE_LIMIT)
            if existing != dict(document):
                raise ValidationError(f"{drift_label} configuration drift")
        raise ValidationError(f"{drift_label} already exists; overwrite rejected")
    staging = Path(tempfile.mkdtemp(prefix=f".{identity}.", dir=parent))
    published = False
    try:
        atomic_write_json(staging / filename, dict(document), mode=0o600)
        if target.exists() or target.is_symlink():
            raise ValidationError(f"{drift_label} already exists; overwrite rejected")
        try:
            os.rename(staging, target)
        except OSError as exc:
            raise PCBDraftError(f"cannot publish {drift_label}") from exc
        published = True
        return target / filename
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


@dataclass(frozen=True)
class CampaignBudgetPlan:
    name: str
    limit: float | None
    unit: str
    enforcement: Literal["hard", "observation_only"]

    def __post_init__(self) -> None:
        _identity(self.name, "campaign budget name")
        if self.name not in BUDGET_NAMES:
            raise ValidationError("campaign budget name is unsupported")
        if self.limit is not None and (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, (int, float))
            or not math.isfinite(self.limit)
            or self.limit <= 0
        ):
            raise ValidationError("campaign budget limit must be positive")
        _text(self.unit, "campaign budget unit", limit=32)
        if self.enforcement not in {"hard", "observation_only"}:
            raise ValidationError("campaign budget enforcement is invalid")
        if (self.limit is None) != (self.enforcement == "observation_only"):
            raise ValidationError("campaign budget limit/enforcement is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "limit": self.limit,
            "unit": self.unit,
            "enforcement": self.enforcement,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(value, {"name", "limit", "unit", "enforcement"}, path)
        limit = item["limit"]
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, (int, float))
        ):
            raise ValidationError(f"{path}.limit is invalid")
        enforcement = item["enforcement"]
        if enforcement not in {"hard", "observation_only"}:
            raise ValidationError(f"{path}.enforcement is invalid")
        return cls(
            _identity(item["name"], f"{path}.name"),
            float(limit) if limit is not None else None,
            _text(item["unit"], f"{path}.unit", limit=32),
            cast(Literal["hard", "observation_only"], enforcement),
        )


def _comparison_budgets() -> tuple[CampaignBudgetPlan, ...]:
    limits: dict[str, tuple[float | None, str]] = {
        "model_turns": (float(MODEL_TURN_LIMIT), "turn"),
        "pcb_tool_calls": (float(PCB_TOOL_CALL_LIMIT), "call"),
        "route_attempts": (None, "attempt"),
        "route_node_expansions": (None, "node"),
        "uncached_input_tokens": (None, "token"),
        "output_tokens": (None, "token"),
        "cache_read_tokens": (None, "token"),
        "wall_time": (WALL_TIME_LIMIT_SECONDS, "second"),
    }
    return tuple(
        CampaignBudgetPlan(
            name,
            limits[name][0],
            limits[name][1],
            "hard" if limits[name][0] is not None else "observation_only",
        )
        for name in BUDGET_NAMES
    )


@dataclass(frozen=True)
class ComparisonCampaignManifest:
    manifest_id: str
    created_at: str
    source_campaign_id: str
    corpus_id: str
    corpus_sha256: str
    provider: str
    model: str
    tier: str
    kicad_version: str
    erc_command: tuple[str, ...]
    drc_command: tuple[str, ...]
    gate_rule_identity: str
    run_schema: str
    evaluator_version: str
    tool_registry_sha256: str
    budgets: tuple[CampaignBudgetPlan, ...]
    planned_runs: tuple[CampaignRunPlan, ...]
    preflight_namespace: str
    formal_campaign_started: bool
    execution_authorized: bool
    human_review_claimed: bool
    physical_evidence_claimed: bool

    def __post_init__(self) -> None:
        _identity(self.manifest_id, "comparison manifest id")
        _timestamp(self.created_at, "comparison manifest created_at")
        _identity(self.source_campaign_id, "comparison source campaign id")
        _identity(self.corpus_id, "comparison corpus id")
        if _SHA256.fullmatch(self.corpus_sha256) is None:
            raise ValidationError("comparison corpus hash is invalid")
        if (self.provider, self.model, self.tier, self.kicad_version) != (
            FORMAL_PROVIDER,
            FORMAL_MODEL,
            FORMAL_TIER,
            FORMAL_KICAD_VERSION,
        ):
            raise ValidationError(
                "comparison frozen model/corpus/KiCad variables drifted"
            )
        if self.erc_command != ERC_COMMAND or self.drc_command != DRC_COMMAND:
            raise ValidationError("comparison ERC/DRC command identity drifted")
        if self.gate_rule_identity != GATE_RULE_IDENTITY:
            raise ValidationError("comparison ERC/DRC rule identity drifted")
        if self.run_schema != RUN_V2_SCHEMA or self.evaluator_version != EVALUATOR_V5:
            raise ValidationError("comparison schema/evaluator version drifted")
        if _SHA256.fullmatch(self.tool_registry_sha256) is None:
            raise ValidationError("comparison tool registry hash is invalid")
        if self.budgets != _comparison_budgets():
            raise ValidationError("comparison frozen budgets drifted")
        _validate_formal_run_plan(self.planned_runs, label="comparison manifest")
        if (
            self.preflight_namespace != PREFLIGHT_NAMESPACE
            or _uses_preflight_namespace(self.source_campaign_id)
            or _uses_preflight_namespace(self.corpus_id)
        ):
            raise ValidationError("preflight namespace entered the formal denominator")
        claim_flags = (
            self.formal_campaign_started,
            self.execution_authorized,
            self.human_review_claimed,
            self.physical_evidence_claimed,
        )
        if not all(isinstance(flag, bool) for flag in claim_flags):
            raise ValidationError("comparison preflight flags are malformed")
        if any(claim_flags):
            raise ValidationError(
                "comparison preflight cannot claim execution/evidence"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": COMPARISON_PREFLIGHT_MANIFEST_SCHEMA,
            "version": COMPARISON_PREFLIGHT_MANIFEST_VERSION,
            "manifest_id": self.manifest_id,
            "created_at": self.created_at,
            "source_campaign_id": self.source_campaign_id,
            "corpus": {
                "id": self.corpus_id,
                "sha256": self.corpus_sha256,
                "tier": self.tier,
                "case_count": 20,
                "repetitions": 3,
            },
            "provider": self.provider,
            "model": self.model,
            "kicad": {
                "version": self.kicad_version,
                "erc_command": list(self.erc_command),
                "drc_command": list(self.drc_command),
                "rule_identity": self.gate_rule_identity,
            },
            "run_schema": self.run_schema,
            "evaluator_version": self.evaluator_version,
            "tool_registry_sha256": self.tool_registry_sha256,
            "budgets": [item.to_dict() for item in self.budgets],
            "planned_runs": [item.to_dict() for item in self.planned_runs],
            "preflight_namespace": self.preflight_namespace,
            "formal_campaign_started": self.formal_campaign_started,
            "execution_authorized": self.execution_authorized,
            "human_review_claimed": self.human_review_claimed,
            "physical_evidence_claimed": self.physical_evidence_claimed,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "schema",
            "version",
            "manifest_id",
            "created_at",
            "source_campaign_id",
            "corpus",
            "provider",
            "model",
            "kicad",
            "run_schema",
            "evaluator_version",
            "tool_registry_sha256",
            "budgets",
            "planned_runs",
            "preflight_namespace",
            "formal_campaign_started",
            "execution_authorized",
            "human_review_claimed",
            "physical_evidence_claimed",
        }
        item = _closed(value, fields, "comparison manifest")
        if (
            item["schema"] != COMPARISON_PREFLIGHT_MANIFEST_SCHEMA
            or item["version"] != COMPARISON_PREFLIGHT_MANIFEST_VERSION
        ):
            raise ValidationError("unsupported comparison preflight manifest")
        corpus = _closed(
            item["corpus"],
            {"id", "sha256", "tier", "case_count", "repetitions"},
            "comparison corpus",
        )
        if corpus["case_count"] != 20 or corpus["repetitions"] != 3:
            raise ValidationError("comparison corpus shape drifted")
        kicad = _closed(
            item["kicad"],
            {"version", "erc_command", "drc_command", "rule_identity"},
            "comparison KiCad identity",
        )
        budgets = item["budgets"]
        runs = item["planned_runs"]
        if not isinstance(budgets, list) or not isinstance(runs, list):
            raise ValidationError("comparison manifest arrays are malformed")
        flags = (
            item["formal_campaign_started"],
            item["execution_authorized"],
            item["human_review_claimed"],
            item["physical_evidence_claimed"],
        )
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValidationError("comparison manifest flags are malformed")
        for name in ("erc_command", "drc_command"):
            if not isinstance(kicad[name], list) or not all(
                isinstance(argument, str) and argument for argument in kicad[name]
            ):
                raise ValidationError("comparison KiCad command is malformed")
        return cls(
            _identity(item["manifest_id"], "comparison manifest id"),
            _timestamp(item["created_at"], "comparison manifest created_at"),
            _identity(item["source_campaign_id"], "comparison source campaign id"),
            _identity(corpus["id"], "comparison corpus id"),
            _text(corpus["sha256"], "comparison corpus hash", limit=64),
            _text(item["provider"], "comparison provider", limit=128),
            _text(item["model"], "comparison model", limit=128),
            _text(corpus["tier"], "comparison tier", limit=128),
            _text(kicad["version"], "comparison KiCad version", limit=128),
            tuple(kicad["erc_command"]),
            tuple(kicad["drc_command"]),
            _text(kicad["rule_identity"], "comparison rule identity", limit=256),
            _text(item["run_schema"], "comparison run schema", limit=256),
            _text(item["evaluator_version"], "comparison evaluator", limit=256),
            _text(item["tool_registry_sha256"], "comparison registry hash", limit=64),
            tuple(
                CampaignBudgetPlan.from_dict(entry, f"$.budgets[{index}]")
                for index, entry in enumerate(budgets)
            ),
            tuple(
                CampaignRunPlan.from_dict(entry, f"$.planned_runs[{index}]")
                for index, entry in enumerate(runs)
            ),
            _text(item["preflight_namespace"], "preflight namespace", limit=128),
            item["formal_campaign_started"],
            item["execution_authorized"],
            item["human_review_claimed"],
            item["physical_evidence_claimed"],
        )


def comparison_manifest_from_campaign(
    source: BoardBenchCampaign,
    *,
    manifest_id: str,
    created_at: str,
) -> ComparisonCampaignManifest:
    """Freeze comparison variables from one read-only 20x3 source campaign."""

    if source.cohort != "ai_reviewed_pilot":
        raise ValidationError("source campaign is not the frozen AI-reviewed pilot")
    if source.evaluator_version != LEGACY_EVALUATOR_VERSION:
        raise ValidationError("source campaign does not retain evaluator v4")
    if source.provider != FORMAL_PROVIDER or source.model != FORMAL_MODEL:
        raise ValidationError("source campaign does not use the frozen Luna model")
    if source.kicad_version != FORMAL_KICAD_VERSION:
        raise ValidationError("source campaign does not use frozen KiCad 10.0.5")
    if source.tool_call_budget != PCB_TOOL_CALL_LIMIT:
        raise ValidationError("source campaign PCB tool budget differs from 500")
    if source.wall_timeout_seconds != WALL_TIME_LIMIT_SECONDS:
        raise ValidationError("source campaign wall-time budget differs from 3600")
    from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY

    return ComparisonCampaignManifest(
        manifest_id=manifest_id,
        created_at=created_at,
        source_campaign_id=source.campaign_id,
        corpus_id=source.corpus_id,
        corpus_sha256=source.corpus_sha256,
        provider=FORMAL_PROVIDER,
        model=FORMAL_MODEL,
        tier=FORMAL_TIER,
        kicad_version=FORMAL_KICAD_VERSION,
        erc_command=ERC_COMMAND,
        drc_command=DRC_COMMAND,
        gate_rule_identity=GATE_RULE_IDENTITY,
        run_schema=RUN_V2_SCHEMA,
        evaluator_version=EVALUATOR_V5,
        tool_registry_sha256=DEFAULT_PCB_TOOL_REGISTRY.schema_fingerprint(),
        budgets=_comparison_budgets(),
        planned_runs=source.runs,
        preflight_namespace=PREFLIGHT_NAMESPACE,
        formal_campaign_started=False,
        execution_authorized=False,
        human_review_claimed=False,
        physical_evidence_claimed=False,
    )


def freeze_comparison_manifest(
    output_root: str | Path, manifest: ComparisonCampaignManifest
) -> Path:
    root = _make_safe_root(output_root, "comparison preflight output")
    return _publish_fresh_directory(
        root / "formal-campaign-plans",
        manifest.manifest_id,
        "manifest.json",
        manifest.to_dict(),
        drift_label="comparison preflight manifest",
    )


def load_comparison_manifest(path: str | Path) -> ComparisonCampaignManifest:
    target = _safe_root(path, "comparison preflight manifest")
    if target.is_dir():
        target /= "manifest.json"
    if target.is_symlink() or not target.is_file():
        raise ValidationError("comparison preflight manifest is unavailable")
    return ComparisonCampaignManifest.from_dict(
        load_json_limited(target, PREFLIGHT_FILE_LIMIT)
    )


@dataclass(frozen=True)
class FormalComparisonLaunchRecord:
    """Immutable authorization binding for one v4-base/v5-comparison campaign."""

    campaign_id: str
    manifest_id: str
    source_campaign_id: str
    authorized_at: str
    corpus_id: str
    corpus_sha256: str
    provider: str
    model: str
    kicad_version: str
    tool_registry_sha256: str
    run_schema: str
    legacy_evaluator_version: str
    comparison_evaluator_version: str
    budgets: tuple[CampaignBudgetPlan, ...]
    planned_runs: tuple[CampaignRunPlan, ...]
    execution_authorized: bool
    formal_campaign_started: bool
    human_review_claimed: bool
    physical_evidence_claimed: bool

    def __post_init__(self) -> None:
        for value, label in (
            (self.campaign_id, "formal campaign id"),
            (self.manifest_id, "formal comparison manifest id"),
            (self.source_campaign_id, "formal source campaign id"),
            (self.corpus_id, "formal corpus id"),
        ):
            _identity(value, label)
        _timestamp(self.authorized_at, "formal authorization timestamp")
        if self.campaign_id == self.source_campaign_id or _uses_preflight_namespace(
            self.campaign_id
        ):
            raise ValidationError("formal campaign identity is not fresh")
        if (
            _SHA256.fullmatch(self.corpus_sha256) is None
            or _SHA256.fullmatch(self.tool_registry_sha256) is None
        ):
            raise ValidationError("formal comparison identity hash is invalid")
        if (self.provider, self.model, self.kicad_version) != (
            FORMAL_PROVIDER,
            FORMAL_MODEL,
            FORMAL_KICAD_VERSION,
        ):
            raise ValidationError("formal comparison environment drifted")
        if (
            self.run_schema != RUN_V2_SCHEMA
            or self.legacy_evaluator_version != LEGACY_EVALUATOR_VERSION
            or self.comparison_evaluator_version != EVALUATOR_V5
        ):
            raise ValidationError("formal comparison evaluator contract drifted")
        if self.budgets != _comparison_budgets():
            raise ValidationError("formal comparison budget contract drifted")
        _validate_formal_run_plan(self.planned_runs, label="formal comparison launch")
        if _uses_preflight_namespace(
            self.source_campaign_id
        ) or _uses_preflight_namespace(self.corpus_id):
            raise ValidationError("preflight namespace entered the formal denominator")
        claim_flags = (
            self.execution_authorized,
            self.formal_campaign_started,
            self.human_review_claimed,
            self.physical_evidence_claimed,
        )
        if not all(isinstance(flag, bool) for flag in claim_flags):
            raise ValidationError("formal comparison launch flags are malformed")
        if not self.execution_authorized or any(claim_flags[1:]):
            raise ValidationError("formal comparison evidence claims are inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FORMAL_COMPARISON_LAUNCH_SCHEMA,
            "version": FORMAL_COMPARISON_LAUNCH_VERSION,
            "campaign_id": self.campaign_id,
            "manifest_id": self.manifest_id,
            "source_campaign_id": self.source_campaign_id,
            "authorized_at": self.authorized_at,
            "corpus": {
                "id": self.corpus_id,
                "sha256": self.corpus_sha256,
            },
            "provider": self.provider,
            "model": self.model,
            "kicad_version": self.kicad_version,
            "tool_registry_sha256": self.tool_registry_sha256,
            "run_schema": self.run_schema,
            "legacy_evaluator_version": self.legacy_evaluator_version,
            "comparison_evaluator_version": self.comparison_evaluator_version,
            "budgets": [item.to_dict() for item in self.budgets],
            "planned_runs": [item.to_dict() for item in self.planned_runs],
            "execution_authorized": self.execution_authorized,
            "formal_campaign_started": self.formal_campaign_started,
            "human_review_claimed": self.human_review_claimed,
            "physical_evidence_claimed": self.physical_evidence_claimed,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {
                "schema",
                "version",
                "campaign_id",
                "manifest_id",
                "source_campaign_id",
                "authorized_at",
                "corpus",
                "provider",
                "model",
                "kicad_version",
                "tool_registry_sha256",
                "run_schema",
                "legacy_evaluator_version",
                "comparison_evaluator_version",
                "budgets",
                "planned_runs",
                "execution_authorized",
                "formal_campaign_started",
                "human_review_claimed",
                "physical_evidence_claimed",
            },
            "formal comparison launch",
        )
        if (
            item["schema"] != FORMAL_COMPARISON_LAUNCH_SCHEMA
            or item["version"] != FORMAL_COMPARISON_LAUNCH_VERSION
        ):
            raise ValidationError("unsupported formal comparison launch record")
        corpus = _closed(item["corpus"], {"id", "sha256"}, "formal corpus")
        raw_budgets = item["budgets"]
        raw_runs = item["planned_runs"]
        if not isinstance(raw_budgets, list) or not isinstance(raw_runs, list):
            raise ValidationError("formal comparison launch arrays are malformed")
        flags = (
            item["execution_authorized"],
            item["formal_campaign_started"],
            item["human_review_claimed"],
            item["physical_evidence_claimed"],
        )
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValidationError("formal comparison launch flags are malformed")
        return cls(
            campaign_id=_identity(item["campaign_id"], "formal campaign id"),
            manifest_id=_identity(item["manifest_id"], "formal manifest id"),
            source_campaign_id=_identity(
                item["source_campaign_id"], "formal source campaign id"
            ),
            authorized_at=_timestamp(
                item["authorized_at"], "formal authorization timestamp"
            ),
            corpus_id=_identity(corpus["id"], "formal corpus id"),
            corpus_sha256=_text(corpus["sha256"], "formal corpus hash", limit=64),
            provider=_text(item["provider"], "formal provider", limit=128),
            model=_text(item["model"], "formal model", limit=128),
            kicad_version=_text(
                item["kicad_version"], "formal KiCad version", limit=128
            ),
            tool_registry_sha256=_text(
                item["tool_registry_sha256"], "formal registry hash", limit=64
            ),
            run_schema=_text(item["run_schema"], "formal run schema", limit=256),
            legacy_evaluator_version=_text(
                item["legacy_evaluator_version"],
                "formal legacy evaluator",
                limit=256,
            ),
            comparison_evaluator_version=_text(
                item["comparison_evaluator_version"],
                "formal comparison evaluator",
                limit=256,
            ),
            budgets=tuple(
                CampaignBudgetPlan.from_dict(entry, f"$.budgets[{index}]")
                for index, entry in enumerate(raw_budgets)
            ),
            planned_runs=tuple(
                CampaignRunPlan.from_dict(entry, f"$.planned_runs[{index}]")
                for index, entry in enumerate(raw_runs)
            ),
            execution_authorized=item["execution_authorized"],
            formal_campaign_started=item["formal_campaign_started"],
            human_review_claimed=item["human_review_claimed"],
            physical_evidence_claimed=item["physical_evidence_claimed"],
        )


def formal_comparison_launch_record(
    manifest: ComparisonCampaignManifest,
    campaign: BoardBenchCampaign,
    *,
    authorized_at: str,
) -> FormalComparisonLaunchRecord:
    """Bind one fresh v4 base campaign to its frozen independent-v5 plan."""

    if campaign.evaluator_version != LEGACY_EVALUATOR_VERSION:
        raise ValidationError("formal base campaign must retain evaluator v4")
    if campaign.cohort != "ai_reviewed_pilot":
        raise ValidationError("formal comparison requires the AI-reviewed pilot cohort")
    if (
        campaign.corpus_id != manifest.corpus_id
        or campaign.corpus_sha256 != manifest.corpus_sha256
        or campaign.provider != manifest.provider
        or campaign.model != manifest.model
        or campaign.kicad_version != manifest.kicad_version
        or campaign.tool_registry_sha256 != manifest.tool_registry_sha256
        or campaign.tool_call_budget != PCB_TOOL_CALL_LIMIT
        or campaign.wall_timeout_seconds != WALL_TIME_LIMIT_SECONDS
        or campaign.runs != manifest.planned_runs
    ):
        raise ValidationError("formal base campaign differs from frozen comparison")
    return FormalComparisonLaunchRecord(
        campaign_id=campaign.campaign_id,
        manifest_id=manifest.manifest_id,
        source_campaign_id=manifest.source_campaign_id,
        authorized_at=authorized_at,
        corpus_id=manifest.corpus_id,
        corpus_sha256=manifest.corpus_sha256,
        provider=manifest.provider,
        model=manifest.model,
        kicad_version=manifest.kicad_version,
        tool_registry_sha256=manifest.tool_registry_sha256,
        run_schema=manifest.run_schema,
        legacy_evaluator_version=LEGACY_EVALUATOR_VERSION,
        comparison_evaluator_version=manifest.evaluator_version,
        budgets=manifest.budgets,
        planned_runs=manifest.planned_runs,
        execution_authorized=True,
        # Authorization is persisted before any selected worker is spawned.
        # Actual start evidence belongs to the first running/terminal run-v2
        # receipt; claiming it here would be false if the launcher exits in
        # the narrow window between authorization and process creation.
        formal_campaign_started=False,
        human_review_claimed=False,
        physical_evidence_claimed=False,
    )


def write_formal_comparison_launch(
    output_root: str | Path, record: FormalComparisonLaunchRecord
) -> Path:
    root = _make_safe_root(output_root, "formal comparison launch output")
    return _publish_fresh_directory(
        root / "formal-campaign-launches",
        record.campaign_id,
        "launch.json",
        record.to_dict(),
        drift_label="formal comparison launch",
    )


def load_formal_comparison_launch(path: str | Path) -> FormalComparisonLaunchRecord:
    target = _safe_root(path, "formal comparison launch")
    if target.is_dir():
        target /= "launch.json"
    if target.is_symlink() or not target.is_file():
        raise ValidationError("formal comparison launch record is unavailable")
    return FormalComparisonLaunchRecord.from_dict(
        load_json_limited(target, PREFLIGHT_FILE_LIMIT)
    )


@dataclass(frozen=True)
class FailureTopologyObservation:
    id: str
    stage: EngineeringStage
    code: str
    classification: Literal[
        "typed_route_failure", "native_postcondition", "convergence_guard"
    ]
    observation_mode: Literal["exercised", "typed_classification_summary"]
    termination_reason: Literal["tool_failure", "strategy_required"]

    def __post_init__(self) -> None:
        _identity(self.id, "failure topology id")
        if not isinstance(self.stage, EngineeringStage):
            raise ValidationError("failure topology stage is invalid")
        if self.code not in _FAILURE_CODES:
            raise ValidationError("failure topology code is unsupported")
        if self.classification not in {
            "typed_route_failure",
            "native_postcondition",
            "convergence_guard",
        }:
            raise ValidationError("failure topology classification is invalid")
        expected_mode = (
            "typed_classification_summary"
            if self.classification == "native_postcondition"
            else "exercised"
        )
        if self.observation_mode != expected_mode:
            raise ValidationError("failure topology observation mode is inconsistent")
        expected_reason = (
            "strategy_required"
            if self.classification == "convergence_guard"
            else "tool_failure"
        )
        if self.termination_reason != expected_reason:
            raise ValidationError("failure topology termination is inconsistent")

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "stage": self.stage.value,
            "code": self.code,
            "classification": self.classification,
            "observation_mode": self.observation_mode,
            "termination_reason": self.termination_reason,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {
                "id",
                "stage",
                "code",
                "classification",
                "observation_mode",
                "termination_reason",
            },
            path,
        )
        try:
            stage = EngineeringStage(item["stage"])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{path}.stage is invalid") from exc
        classification = item["classification"]
        observation_mode = item["observation_mode"]
        reason = item["termination_reason"]
        if (
            classification
            not in {
                "typed_route_failure",
                "native_postcondition",
                "convergence_guard",
            }
            or observation_mode
            not in {
                "exercised",
                "typed_classification_summary",
            }
            or reason not in {"tool_failure", "strategy_required"}
        ):
            raise ValidationError(f"{path} classification is invalid")
        return cls(
            _identity(item["id"], f"{path}.id"),
            stage,
            _text(item["code"], f"{path}.code", limit=128),
            cast(
                Literal[
                    "typed_route_failure",
                    "native_postcondition",
                    "convergence_guard",
                ],
                classification,
            ),
            cast(
                Literal["exercised", "typed_classification_summary"],
                observation_mode,
            ),
            cast(Literal["tool_failure", "strategy_required"], reason),
        )


def failure_topology_preflight() -> tuple[FailureTopologyObservation, ...]:
    """Exercise route/convergence failures and label native rows as summaries.

    The Router and convergence rows below execute their owning code paths.  The
    native-postcondition rows are only typed classification summaries of the
    dedicated M1-M3 regression fixtures; this preflight does not claim to
    re-execute those failures or to classify them as Router failures.
    """

    router = GridRouter(
        board_width_mm=10,
        board_height_mm=10,
        layers=2,
        clearance_mm=0.2,
        min_track_mm=0.2,
        min_drill_mm=0.3,
        edge_clearance_mm=0.2,
        grid_mm=0.2,
        max_expansions=10_000,
    )
    pads = (
        RoutingPad("left", "N", 2, 2, 0.5, 0.5, (0,)),
        RoutingPad("right", "N", 8, 2, 0.5, 0.5, (0,)),
    )
    route_codes: list[str] = []
    for seed in (
        RouteSegment("N", 0, 2, 2, 2, 2, 0.2),
        RouteSegment("N", 0, 3, 3, 4, 3, 0.2),
    ):
        try:
            router.route(pads, seed_segments=(seed,))
        except RoutingFailureError as exc:
            route_codes.append(exc.failure.code)
    if route_codes != ["zero_length_seed", "invalid_seed"]:
        raise ValidationError("failure topology router classifications drifted")

    # The same revision/placement/order state and retry key is retained. Two
    # neutral attempts must require a new strategy on the third.
    retry_failure = RoutingFailure(
        "native_connectivity_failed",
        "N",
        ("J1.1", "J2.1"),
        state_revision=7,
        state_context=("placement=unchanged", "layers=0,1"),
    )
    state_key = "revision=7|placement=unchanged|layers=0,1"
    repeated = evaluate_convergence(
        (
            ConvergenceObservation(
                state_key, ProgressClassification.NEUTRAL, retry_failure.retry_key
            ),
            ConvergenceObservation(
                state_key, ProgressClassification.NEUTRAL, retry_failure.retry_key
            ),
        ),
        state_key=state_key,
        retry_key=retry_failure.retry_key,
    )
    if repeated.allowed or repeated.reason != "repeated_retry_key":
        raise ValidationError("failure topology convergence classification drifted")

    return (
        FailureTopologyObservation(
            "zero-length-seed",
            EngineeringStage.ROUTING,
            route_codes[0],
            "typed_route_failure",
            "exercised",
            "tool_failure",
        ),
        FailureTopologyObservation(
            "invalid-seed",
            EngineeringStage.ROUTING,
            route_codes[1],
            "typed_route_failure",
            "exercised",
            "tool_failure",
        ),
        FailureTopologyObservation(
            "native-zero-copper",
            EngineeringStage.NATIVE_CONNECTIVITY_CONFIRMED,
            "native_zero_copper",
            "native_postcondition",
            "typed_classification_summary",
            "tool_failure",
        ),
        FailureTopologyObservation(
            "native-endpoint-disconnected",
            EngineeringStage.NATIVE_CONNECTIVITY_CONFIRMED,
            "native_connectivity_failed",
            "native_postcondition",
            "typed_classification_summary",
            "tool_failure",
        ),
        FailureTopologyObservation(
            "unintended-net-merge",
            EngineeringStage.NATIVE_CONNECTIVITY_CONFIRMED,
            "unintended_net_merge",
            "native_postcondition",
            "typed_classification_summary",
            "tool_failure",
        ),
        FailureTopologyObservation(
            "stale-routed-footprint-transform",
            EngineeringStage.PLACEMENT,
            "routed_footprint_transform_unsupported",
            "native_postcondition",
            "typed_classification_summary",
            "tool_failure",
        ),
        FailureTopologyObservation(
            "same-state-route-repeat",
            EngineeringStage.ROUTING,
            repeated.reason,
            "convergence_guard",
            "exercised",
            "strategy_required",
        ),
    )


@dataclass(frozen=True)
class CheckEvidenceSummary:
    name: Literal["erc", "drc"]
    state: Literal["pass", "fail", "unknown"]
    error_count: int | None
    command: tuple[str, ...]
    source: str

    def __post_init__(self) -> None:
        if self.name not in {"erc", "drc"} or self.state not in {
            "pass",
            "fail",
            "unknown",
        }:
            raise ValidationError("preflight check summary is invalid")
        if self.error_count is not None and (
            isinstance(self.error_count, bool)
            or not isinstance(self.error_count, int)
            or self.error_count < 0
        ):
            raise ValidationError("preflight check error count is invalid")
        if self.state == "unknown" and self.error_count is not None:
            raise ValidationError("unknown preflight check has an error count")
        if self.error_count is not None and self.state != (
            "pass" if self.error_count == 0 else "fail"
        ):
            raise ValidationError("preflight check count contradicts its state")
        expected = ERC_COMMAND if self.name == "erc" else DRC_COMMAND
        if self.command != expected:
            raise ValidationError("preflight check command identity drifted")
        _text(self.source, "preflight check source", limit=512)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "error_count": self.error_count,
            "command": list(self.command),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value, {"name", "state", "error_count", "command", "source"}, path
        )
        if item["name"] not in {"erc", "drc"} or item["state"] not in {
            "pass",
            "fail",
            "unknown",
        }:
            raise ValidationError(f"{path} is invalid")
        count = item["error_count"]
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int)
        ):
            raise ValidationError(f"{path}.error_count is invalid")
        command = item["command"]
        if not isinstance(command, list) or not all(
            isinstance(argument, str) and argument for argument in command
        ):
            raise ValidationError(f"{path}.command is invalid")
        return cls(
            cast(Literal["erc", "drc"], item["name"]),
            cast(Literal["pass", "fail", "unknown"], item["state"]),
            count,
            tuple(command),
            _text(item["source"], f"{path}.source", limit=512),
        )


@dataclass(frozen=True)
class PreflightArtifactRefs:
    project: str
    product_terminal: str
    run_v2: str
    evaluation_v5: str

    def __post_init__(self) -> None:
        for name in ("project", "product_terminal", "run_v2", "evaluation_v5"):
            _relative_artifact(getattr(self, name), f"preflight artifact {name}")

    def to_dict(self) -> dict[str, str]:
        return {
            "project": self.project,
            "product_terminal": self.product_terminal,
            "run_v2": self.run_v2,
            "evaluation_v5": self.evaluation_v5,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> Self:
        item = _closed(
            value,
            {"project", "product_terminal", "run_v2", "evaluation_v5"},
            path,
        )
        return cls(
            *(
                _relative_artifact(item[name], f"{path}.{name}")
                for name in ("project", "product_terminal", "run_v2", "evaluation_v5")
            )
        )


@dataclass(frozen=True)
class DeterministicPreflightReport:
    generated_at: str
    run_id: str
    case_id: str
    natural_language_request: str
    tool_sequence: tuple[str, ...]
    native_project_files: tuple[str, ...]
    product_terminal: ProductSessionTerminalReceipt
    normalized_run: NormalizedBoardBenchRun
    evaluation_v5: EvaluationSummaryV5
    native_consistency: NativeConsistencyReport
    checks: tuple[CheckEvidenceSummary, ...]
    failure_topologies: tuple[FailureTopologyObservation, ...]
    artifacts: PreflightArtifactRefs
    formal_campaign_started: bool = False
    human_review_claimed: bool = False
    physical_evidence_claimed: bool = False
    limitations: tuple[str, ...] = PREFLIGHT_LIMITATIONS

    def __post_init__(self) -> None:
        _timestamp(self.generated_at, "preflight report generated_at")
        if self.run_id != PREFLIGHT_RUN_ID or self.case_id != PREFLIGHT_CASE_ID:
            raise ValidationError("preflight report identity drifted")
        if (
            not isinstance(self.natural_language_request, str)
            or self.natural_language_request != PREFLIGHT_REQUEST
            or self.natural_language_request.lstrip().startswith(("{", "["))
        ):
            raise ValidationError(
                "preflight input is not the frozen natural language request"
            )
        if self.tool_sequence != tuple(name for name, _arguments in _PREFLIGHT_STEPS):
            raise ValidationError("preflight tool sequence drifted")
        if (
            len(self.native_project_files) != 3
            or len(set(self.native_project_files)) != 3
            or any(
                not isinstance(name, str)
                or not name
                or len(name.encode("utf-8")) > 512
                or "/" in name
                or "\\" in name
                or "\x00" in name
                or Path(name).name != name
                for name in self.native_project_files
            )
            or {Path(name).suffix for name in self.native_project_files}
            != {".kicad_pro", ".kicad_sch", ".kicad_pcb"}
        ):
            raise ValidationError("preflight native KiCad project is incomplete")
        if (
            self.normalized_run.campaign_id != PREFLIGHT_CAMPAIGN_ID
            or self.normalized_run.run_id != self.run_id
            or self.normalized_run.case_id != self.case_id
            or self.evaluation_v5.campaign_id != PREFLIGHT_CAMPAIGN_ID
            or self.evaluation_v5.run_id != self.run_id
            or self.evaluation_v5.case_id != self.case_id
        ):
            raise ValidationError("preflight artifact identities differ")
        terminal_facts = (
            self.product_terminal.process_status,
            self.product_terminal.task_outcome,
            self.product_terminal.termination_reason,
            self.product_terminal.stage_reached,
            self.product_terminal.release_gate_passed,
        )
        normalized_facts = (
            self.normalized_run.process_status,
            self.normalized_run.task_outcome,
            self.normalized_run.termination_reason,
            self.normalized_run.stage_reached,
            self.normalized_run.release_gate_passed,
        )
        if terminal_facts != normalized_facts:
            raise ValidationError("preflight terminal and v2 semantics differ")
        if (
            not self.product_terminal.release_gate_passed
            or self.product_terminal.task_outcome is not TaskOutcome.PASSED
            or self.product_terminal.termination_reason != "release_gate_passed"
        ):
            raise ValidationError("preflight automated release gate did not pass")
        if (
            self.evaluation_v5.design_intent != "unknown"
            or self.evaluation_v5.native_artifact != "pass"
            or self.evaluation_v5.delivery_readiness != "unknown"
            or self.evaluation_v5.legacy_overall_projection != "unknown"
        ):
            raise ValidationError(
                "preflight automated gate cannot claim design or delivery readiness"
            )
        if (
            self.normalized_run.repetition != 1
            or self.normalized_run.source_schema != RUN_V2_SCHEMA
            or tuple(item.name for item in self.normalized_run.budgets) != BUDGET_NAMES
        ):
            raise ValidationError("preflight normalized v2 contract drifted")
        if tuple(item.name for item in self.checks) != ("erc", "drc"):
            raise ValidationError("preflight ERC/DRC evidence is incomplete")
        if self.checks != _check_summaries(self.product_terminal):
            raise ValidationError(
                "preflight ERC/DRC summary differs from terminal evidence"
            )
        if not self.native_consistency.consistency_passed:
            raise ValidationError("committed preflight revision is native-inconsistent")
        if (
            self.native_consistency.candidate_revision
            != self.product_terminal.source_revision
        ):
            raise ValidationError("preflight native consistency revision differs")
        project_artifact = Path(self.artifacts.project)
        product_terminal_artifact = Path(self.artifacts.product_terminal)
        if (
            project_artifact.name != self.product_terminal.project_id
            or product_terminal_artifact.parent != project_artifact / "product-sessions"
            or product_terminal_artifact.name
            != f"{self.product_terminal.receipt_id}.json"
            or self.artifacts.run_v2 != "run-v2.json"
            or self.artifacts.evaluation_v5
            != f"evaluator-v5/{self.run_id}/evaluation.json"
        ):
            raise ValidationError("preflight artifact identity binding differs")
        required_failures = {
            "zero_length_seed",
            "invalid_seed",
            "native_zero_copper",
            "native_connectivity_failed",
            "unintended_net_merge",
            "routed_footprint_transform_unsupported",
            "repeated_retry_key",
        }
        if {item.code for item in self.failure_topologies} != required_failures:
            raise ValidationError("preflight failure-topology coverage is incomplete")
        claim_flags = (
            self.formal_campaign_started,
            self.human_review_claimed,
            self.physical_evidence_claimed,
        )
        if not all(isinstance(flag, bool) for flag in claim_flags):
            raise ValidationError("preflight evidence claim flags are malformed")
        if any(claim_flags) or self.limitations != PREFLIGHT_LIMITATIONS:
            raise ValidationError("preflight evidence limitations drifted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DETERMINISTIC_PREFLIGHT_REPORT_SCHEMA,
            "version": DETERMINISTIC_PREFLIGHT_REPORT_VERSION,
            "fixture": PREFLIGHT_NAMESPACE,
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "input_boundary": "natural_language",
            "natural_language_request": self.natural_language_request,
            "provider": "deterministic_fake_provider",
            "autonomous_model_reasoning_claimed": False,
            "tool_sequence": list(self.tool_sequence),
            "native_project_files": list(self.native_project_files),
            "product_terminal": self.product_terminal.to_dict(),
            "normalized_run": {
                "campaign_id": self.normalized_run.campaign_id,
                "run_id": self.normalized_run.run_id,
                "case_id": self.normalized_run.case_id,
                "repetition": self.normalized_run.repetition,
                "source_schema": self.normalized_run.source_schema,
                "source_version": self.normalized_run.source_version,
                "process_status": self.normalized_run.process_status.value
                if self.normalized_run.process_status is not None
                else None,
                "task_outcome": self.normalized_run.task_outcome.value
                if self.normalized_run.task_outcome is not None
                else None,
                "termination_reason": self.normalized_run.termination_reason,
                "stage_reached": self.normalized_run.stage_reached.value
                if self.normalized_run.stage_reached is not None
                else None,
                "release_gate_passed": self.normalized_run.release_gate_passed,
                "budgets": [item.to_dict() for item in self.normalized_run.budgets],
            },
            "evaluation_v5": self.evaluation_v5.to_dict(),
            "native_consistency": self.native_consistency.to_dict(),
            "checks": [item.to_dict() for item in self.checks],
            "failure_topologies": [item.to_dict() for item in self.failure_topologies],
            "artifacts": self.artifacts.to_dict(),
            "formal_campaign_started": self.formal_campaign_started,
            "human_review_claimed": self.human_review_claimed,
            "physical_evidence_claimed": self.physical_evidence_claimed,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "schema",
            "version",
            "fixture",
            "generated_at",
            "run_id",
            "case_id",
            "input_boundary",
            "natural_language_request",
            "provider",
            "autonomous_model_reasoning_claimed",
            "tool_sequence",
            "native_project_files",
            "product_terminal",
            "normalized_run",
            "evaluation_v5",
            "native_consistency",
            "checks",
            "failure_topologies",
            "artifacts",
            "formal_campaign_started",
            "human_review_claimed",
            "physical_evidence_claimed",
            "limitations",
        }
        item = _closed(value, fields, "deterministic preflight report")
        if (
            item["schema"] != DETERMINISTIC_PREFLIGHT_REPORT_SCHEMA
            or item["version"] != DETERMINISTIC_PREFLIGHT_REPORT_VERSION
            or item["fixture"] != PREFLIGHT_NAMESPACE
            or item["input_boundary"] != "natural_language"
            or item["provider"] != "deterministic_fake_provider"
            or item["autonomous_model_reasoning_claimed"] is not False
        ):
            raise ValidationError("unsupported deterministic preflight report")
        normalized = _closed(
            item["normalized_run"],
            {
                "campaign_id",
                "run_id",
                "case_id",
                "repetition",
                "source_schema",
                "source_version",
                "process_status",
                "task_outcome",
                "termination_reason",
                "stage_reached",
                "release_gate_passed",
                "budgets",
            },
            "normalized preflight run",
        )
        try:
            process = ProcessStatus(normalized["process_status"])
            outcome = TaskOutcome(normalized["task_outcome"])
            stage = EngineeringStage(normalized["stage_reached"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("normalized preflight enums are invalid") from exc
        budgets = normalized["budgets"]
        checks = item["checks"]
        failures = item["failure_topologies"]
        for rows, label in (
            (budgets, "budgets"),
            (checks, "checks"),
            (failures, "failure topologies"),
        ):
            if not isinstance(rows, list):
                raise ValidationError(f"preflight {label} is malformed")
        release = normalized["release_gate_passed"]
        if not isinstance(release, bool):
            raise ValidationError("normalized preflight release gate is malformed")
        booleans = (
            item["formal_campaign_started"],
            item["human_review_claimed"],
            item["physical_evidence_claimed"],
        )
        if not all(isinstance(flag, bool) for flag in booleans):
            raise ValidationError("preflight report claim flags are malformed")
        tool_sequence = item["tool_sequence"]
        native_files = item["native_project_files"]
        limitations = item["limitations"]
        if not all(
            isinstance(rows, list) and all(isinstance(value, str) for value in rows)
            for rows in (tool_sequence, native_files, limitations)
        ):
            raise ValidationError("preflight report string arrays are malformed")
        normalized_run = NormalizedBoardBenchRun(
            _identity(normalized["campaign_id"], "preflight campaign id"),
            _identity(normalized["run_id"], "preflight run id"),
            _identity(normalized["case_id"], "preflight case id"),
            normalized["repetition"],
            _text(normalized["source_schema"], "preflight source schema", limit=256),
            normalized["source_version"],
            process,
            outcome,
            _text(normalized["termination_reason"], "preflight termination", limit=256),
            stage,
            release,
            tuple(
                BudgetDimension.from_dict(entry, f"$.normalized_run.budgets[{index}]")
                for index, entry in enumerate(budgets)
            ),
        )
        return cls(
            _timestamp(item["generated_at"], "preflight report generated_at"),
            _identity(item["run_id"], "preflight report run id"),
            _identity(item["case_id"], "preflight report case id"),
            _text(item["natural_language_request"], "preflight request", limit=16_384),
            tuple(tool_sequence),
            tuple(native_files),
            ProductSessionTerminalReceipt.from_dict(item["product_terminal"]),
            normalized_run,
            EvaluationSummaryV5.from_dict(item["evaluation_v5"], "$.evaluation_v5"),
            NativeConsistencyReport.from_dict(item["native_consistency"]),
            tuple(
                CheckEvidenceSummary.from_dict(entry, f"$.checks[{index}]")
                for index, entry in enumerate(checks)
            ),
            tuple(
                FailureTopologyObservation.from_dict(
                    entry, f"$.failure_topologies[{index}]"
                )
                for index, entry in enumerate(failures)
            ),
            PreflightArtifactRefs.from_dict(item["artifacts"], "$.artifacts"),
            item["formal_campaign_started"],
            item["human_review_claimed"],
            item["physical_evidence_claimed"],
            tuple(limitations),
        )


def write_preflight_report(
    preflight_root: str | Path, report: DeterministicPreflightReport
) -> Path:
    root = _make_safe_root(preflight_root, "deterministic preflight output")
    return _publish_fresh_directory(
        root,
        "report",
        "preflight.json",
        report.to_dict(),
        drift_label="deterministic preflight report",
    )


def load_preflight_report(path: str | Path) -> DeterministicPreflightReport:
    target = _safe_root(path, "deterministic preflight report")
    if target.is_dir():
        target /= "preflight.json"
    if target.is_symlink() or not target.is_file():
        raise ValidationError("deterministic preflight report is unavailable")
    return DeterministicPreflightReport.from_dict(
        load_json_limited(target, PREFLIGHT_FILE_LIMIT)
    )


_PREFLIGHT_STEPS: tuple[tuple[str, Mapping[str, Any]], ...] = (
    (
        "pcb_add_block",
        {
            "value": {
                "id": "ground_adapter",
                "kind": "connector",
                "name": "Ground adapter",
                "version": "1",
                "intent": "Join four ground contacts.",
            }
        },
    ),
    (
        "pcb_add_component",
        {
            "value": {
                "id": "input",
                "reference": "J1",
                "part_id": "samtec.tsw-102-07-g-s",
                "value": "GND IN",
                "block_id": "ground_adapter",
            }
        },
    ),
    (
        "pcb_add_component",
        {
            "value": {
                "id": "output",
                "reference": "J2",
                "part_id": "samtec.tsw-102-07-g-s",
                "value": "GND OUT",
                "block_id": "ground_adapter",
            }
        },
    ),
    (
        "pcb_connect_group",
        {
            # Use the already-defined GND net.  An earlier empty-net probe was
            # correctly absent from native KiCad until it had endpoints; that
            # fixture limitation is not evidence of a Router failure.
            "connections": {
                "entries": [
                    {
                        "net_id": "gnd",
                        "component_id": component,
                        "pin": pin,
                        "role": "return",
                    }
                    for component in ("input", "output")
                    for pin in ("1", "2")
                ]
            }
        },
    ),
    ("pcb_set_board_outline", {"width_mm": 30.0, "height_mm": 20.0}),
    (
        "pcb_place_group",
        {
            "placements": {
                "entries": [
                    {
                        "component_id": "input",
                        "x_mm": 10.0,
                        "y_mm": 10.0,
                        "rotation_deg": 0.0,
                        "side": "front",
                    },
                    {
                        "component_id": "output",
                        "x_mm": 20.0,
                        "y_mm": 10.0,
                        "rotation_deg": 180.0,
                        "side": "front",
                    },
                ]
            }
        },
    ),
    ("pcb_route_net", {"net_id": "gnd"}),
    ("pcb_run_erc", {}),
    ("pcb_run_drc", {}),
)


class _NoModelIntentProvider:
    provider_id = "deterministic-preflight-no-model"

    def interpret(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ValidationError("deterministic preflight cannot call an intent model")

    def diagnostic(self) -> dict[str, Any]:
        return {"provider": self.provider_id, "available": False}


class _DeterministicPreflightProducer:
    """A fixed fake producer; it makes no autonomous reasoning claim."""

    def __init__(self, expected_request: str) -> None:
        self.expected_request = expected_request
        self.observed_inputs: list[str] = []
        self.decision_count = 0

    def conversation_step(
        self,
        record: TurnRecord,
        view: Mapping[str, Any],
        *,
        timeout: float,
    ) -> ConversationStep | None:
        del view, timeout
        if record.user_message != self.expected_request:
            raise ValidationError("deterministic preflight input boundary drifted")
        if record.user_message.lstrip().startswith(("{", "[")):
            raise ValidationError("deterministic preflight received structured input")
        self.observed_inputs.append(record.user_message)
        return None

    def next_call(
        self,
        record: TurnRecord,
        view: Mapping[str, Any],
        *,
        timeout: float,
    ) -> ProposedToolCall | None:
        del view, timeout
        self.decision_count += 1
        completed = tuple(
            item for item in record.tool_runs if item.status is ToolRunStatus.COMPLETED
        )
        if len(completed) >= len(_PREFLIGHT_STEPS):
            return None
        name, arguments = _PREFLIGHT_STEPS[len(completed)]
        return ProposedToolCall(
            name,
            arguments,
            source="model",
            tool_call_id=f"deterministic-preflight-{len(completed) + 1}",
        )


def _prepare_preflight_root(output_root: str | Path) -> Path:
    root = _make_safe_root(output_root, "deterministic preflight output")
    namespace = _make_safe_root(
        root / PREFLIGHT_NAMESPACE, "deterministic preflight namespace"
    )
    target = namespace / PREFLIGHT_RUN_ID
    if target.exists() or target.is_symlink():
        raise ValidationError("deterministic preflight run already exists")
    try:
        target.mkdir(mode=0o700)
    except OSError as exc:
        raise PCBDraftError("cannot create deterministic preflight run") from exc
    return target


def _load_product_terminal(
    project_root: Path,
) -> tuple[Path, ProductSessionTerminalReceipt]:
    directory = project_root / "product-sessions"
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError("deterministic preflight product terminal is unavailable")
    paths = tuple(
        path
        for path in sorted(directory.glob("*.json"))
        if path.is_file() and not path.is_symlink()
    )
    if len(paths) != 1:
        raise ValidationError(
            "deterministic preflight needs exactly one product terminal"
        )
    return paths[0], ProductSessionTerminalReceipt.from_dict(
        load_json_limited(paths[0], PREFLIGHT_FILE_LIMIT)
    )


def _planned_budgets() -> tuple[BudgetDimension, ...]:
    plan = _comparison_budgets()
    return tuple(
        BudgetDimension(
            item.name, item.limit, None, item.unit, "not_started", "unknown"
        )
        for item in plan
    )


def _terminal_budgets(
    *,
    model_decisions: int,
    pcb_tool_calls: int,
    wall_seconds: float,
) -> tuple[BudgetDimension, ...]:
    observed: dict[str, float | None] = {
        "model_turns": float(model_decisions),
        "pcb_tool_calls": float(pcb_tool_calls),
        "route_attempts": 1.0,
        "route_node_expansions": None,
        "uncached_input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "wall_time": wall_seconds,
    }
    plan = {item.name: item for item in _comparison_budgets()}
    dimensions: list[BudgetDimension] = []
    for name in BUDGET_NAMES:
        item = plan[name]
        consumed = observed[name]
        status = (
            "unknown"
            if consumed is None
            else "observed"
            if item.limit is None
            else "within_limit"
            if consumed <= item.limit
            else "exhausted"
        )
        source = (
            "deterministic_fake_provider"
            if name == "model_turns"
            else "agent_tool_records"
            if name in {"pcb_tool_calls", "route_attempts"}
            else "preflight_wall_clock"
            if name == "wall_time"
            else "unavailable"
        )
        dimensions.append(
            BudgetDimension(name, item.limit, consumed, item.unit, source, status)
        )
    return tuple(dimensions)


def _v2_from_terminal(
    preflight_root: Path,
    terminal: ProductSessionTerminalReceipt,
    *,
    started_at: str,
    completed_at: str,
    producer: _DeterministicPreflightProducer,
    tool_count: int,
    wall_seconds: float,
    project_root: Path,
) -> tuple[Path, BoardBenchRunV2, NormalizedBoardBenchRun]:
    prompt_sha256 = hashlib.sha256(PREFLIGHT_REQUEST.encode("utf-8")).hexdigest()
    planned = BoardBenchRunV2(
        PREFLIGHT_CAMPAIGN_ID,
        PREFLIGHT_RUN_ID,
        PREFLIGHT_CASE_ID,
        1,
        prompt_sha256,
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
        _planned_budgets(),
        ActualCostEvidence.unknown("fake_provider_no_cost"),
        ContextQualityEvidence.unknown("fake_provider_no_context_measurement"),
        (),
    )
    path = preflight_root / "run-v2.json"
    store_run_v2(path, planned)
    running = start_run_v2(planned, started_at)
    store_run_v2(path, running)
    final = BoardBenchRunV2(
        planned.campaign_id,
        planned.run_id,
        planned.case_id,
        planned.repetition,
        planned.prompt_sha256,
        "terminal",
        started_at,
        completed_at,
        terminal.process_status,
        terminal.task_outcome,
        terminal.termination_reason,
        terminal.stage_reached,
        terminal.release_gate_passed,
        "product_session_terminal",
        0,
        (
            "Deterministic fake-provider preflight completed. This is not a formal "
            "BoardBench result and does not establish delivery readiness."
        ),
        _terminal_budgets(
            model_decisions=producer.decision_count,
            pcb_tool_calls=tool_count,
            wall_seconds=wall_seconds,
        ),
        ActualCostEvidence.unknown("fake_provider_no_cost"),
        ContextQualityEvidence.unknown("fake_provider_no_context_measurement"),
        build_inventory(project_root),
    )
    store_run_v2(path, final)
    retained = load_run_v2(path)
    return path, retained, normalize_v2_run(retained)


def _metric_state(
    value: Any, revision: int
) -> tuple[Literal["pass", "fail", "unknown"], int | None]:
    if value.status is not EvidenceStatus.KNOWN or value.source_revision != revision:
        return "unknown", None
    return ("pass" if value.value == 0 else "fail"), value.value


def _native_metrics(
    terminal: ProductSessionTerminalReceipt,
    consistency: NativeConsistencyReport,
) -> tuple[NativeMetricEvidence, ...]:
    revision = terminal.source_revision
    unresolved_state, unresolved = _metric_state(
        terminal.progress.unresolved_connection_count, revision
    )
    erc_state, erc_errors = _metric_state(terminal.progress.erc_error_count, revision)
    drc_state, drc_errors = _metric_state(terminal.progress.error_drc_count, revision)
    consistency_count = len(consistency.mismatches)
    consistency_state: Literal["pass", "fail"] = (
        "pass" if consistency.consistency_passed else "fail"
    )
    return (
        NativeMetricEvidence("complete_project", "pass", None, "native_files_parsed"),
        NativeMetricEvidence(
            "library_resolution",
            "pass" if consistency.consistency_passed else "fail",
            None,
            "qualified_native_materialization",
        ),
        NativeMetricEvidence(
            "native_consistency",
            consistency_state,
            consistency_count,
            "native_consistency_v1",
        ),
        NativeMetricEvidence(
            "unresolved_connections",
            cast(Any, unresolved_state),
            unresolved,
            "product_terminal_progress",
        ),
        NativeMetricEvidence(
            "erc",
            cast(Any, erc_state),
            erc_errors,
            "product_terminal_progress",
            ERC_COMMAND,
        ),
        NativeMetricEvidence(
            "drc",
            cast(Any, drc_state),
            drc_errors,
            "product_terminal_progress",
            DRC_COMMAND,
        ),
    )


def _evaluation_v5(
    normalized: NormalizedBoardBenchRun,
    terminal: ProductSessionTerminalReceipt,
    consistency: NativeConsistencyReport,
    *,
    evaluated_at: str,
) -> BoardBenchEvaluationV5:
    metrics = _native_metrics(terminal, consistency)
    source_names = {
        "complete_project": "native_project_parse",
        "library_resolution": "native_library_resolution",
        "native_consistency": "native_consistency",
        "unresolved_connections": "native_connectivity",
        "erc": "kicad_erc",
        "drc": "kicad_drc",
    }
    design = EvaluationLayer.build(
        "design_intent",
        (
            EvidenceFinding(
                "unavailable",
                "automatic",
                "unknown",
                "deterministic fake provider is not autonomous design evidence",
            ),
        ),
    )
    native = EvaluationLayer.build(
        "native_artifact",
        tuple(
            EvidenceFinding(
                source_names[metric.name],
                "automatic",
                metric.state,
                f"{metric.name}:{metric.source}",
            )
            for metric in metrics
        ),
    )
    delivery = EvaluationLayer.build(
        "delivery_readiness",
        (
            EvidenceFinding(
                "unavailable",
                "automatic",
                "unknown",
                "human engineering and physical evidence unavailable",
            ),
        ),
    )
    projection = (
        "fail"
        if "fail" in {design.state, native.state}
        else "unknown"
        if "unknown" in {design.state, native.state}
        else "pass"
    )
    return BoardBenchEvaluationV5(
        PREFLIGHT_CAMPAIGN_ID,
        PREFLIGHT_RUN_ID,
        PREFLIGHT_CASE_ID,
        1,
        normalized.source_schema,
        normalized.source_version,
        "deterministic-preflight-no-legacy-score",
        "unknown",
        evaluated_at,
        design,
        native,
        delivery,
        cast(Any, projection),
        metrics,
        derive_causal_attribution(normalized, progress=terminal.progress),
    )


def _check_summaries(
    terminal: ProductSessionTerminalReceipt,
) -> tuple[CheckEvidenceSummary, ...]:
    revision = terminal.source_revision
    erc_state, erc_count = _metric_state(terminal.progress.erc_error_count, revision)
    drc_state, drc_count = _metric_state(terminal.progress.error_drc_count, revision)
    return (
        CheckEvidenceSummary(
            "erc", cast(Any, erc_state), erc_count, ERC_COMMAND, "retained_kicad_check"
        ),
        CheckEvidenceSummary(
            "drc", cast(Any, drc_state), drc_count, DRC_COMMAND, "retained_kicad_check"
        ),
    )


@dataclass(frozen=True)
class DeterministicPreflightResult:
    root: Path
    report_path: Path
    run_v2_path: Path
    evaluation_v5_path: Path
    report: DeterministicPreflightReport
    run_v2: BoardBenchRunV2
    evaluation_v5: BoardBenchEvaluationV5
    provider_inputs: tuple[str, ...]


def run_deterministic_preflight(
    output_root: str | Path,
    *,
    timeout: float = 90.0,
) -> DeterministicPreflightResult:
    """Run one fake-provider/real-KiCad fixture under a fresh namespace."""

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > 300
    ):
        raise ValidationError("deterministic preflight timeout must be 1..300 seconds")
    preflight_root = _prepare_preflight_root(output_root)
    repository = preflight_root / "repository"
    provider = cast(IntentProvider, _NoModelIntentProvider())
    service = ApplicationService(repository, provider=provider)
    view = service.create_empty_project(PREFLIGHT_RUN_ID)
    project_id = str(view["project"]["id"])
    producer = _DeterministicPreflightProducer(PREFLIGHT_REQUEST)
    orchestrator = AgentOrchestrator(
        service,
        producer=producer,
        permissions=PermissionBroker("workspace"),
    )
    started_at = utc_timestamp()
    started = time.monotonic()
    turn = orchestrator.start_turn(project_id, PREFLIGHT_REQUEST)
    orchestrator.run_turn(
        project_id,
        turn.turn_id,
        timeout=timeout,
        cancellation_requested=lambda: False,
    )
    wall_seconds = time.monotonic() - started
    completed_at = utc_timestamp()
    if not producer.observed_inputs or set(producer.observed_inputs) != {
        PREFLIGHT_REQUEST
    }:
        raise ValidationError("fake provider did not receive only natural language")

    record = orchestrator.store(project_id).load(turn.turn_id)
    tool_sequence = tuple(
        item.tool_name
        for item in record.tool_runs
        if item.status is ToolRunStatus.COMPLETED
    )
    if tool_sequence != tuple(name for name, _arguments in _PREFLIGHT_STEPS):
        raise ValidationError("deterministic preflight tool execution is incomplete")
    project_root = service.project_root(project_id)
    product_path, terminal = _load_product_terminal(project_root)
    managed = open_managed_project(project_root / "design")
    managed.assert_synchronized()
    consistency = inspect_native_consistency(
        managed.design,
        managed.schematic_path,
        managed.board_path,
        candidate_revision=terminal.source_revision,
        graph=managed.graph.with_footprint_overrides(managed.design),
        require_routed_net_ids=frozenset({"gnd"}),
    )
    if not consistency.consistency_passed:
        raise ValidationError("deterministic preflight committed native mismatch")
    run_v2_path, run_v2, normalized = _v2_from_terminal(
        preflight_root,
        terminal,
        started_at=started_at,
        completed_at=completed_at,
        producer=producer,
        tool_count=len(tool_sequence),
        wall_seconds=wall_seconds,
        project_root=project_root,
    )
    evaluation = _evaluation_v5(
        normalized,
        terminal,
        consistency,
        evaluated_at=completed_at,
    )
    evaluation_path = write_evaluation_v5(preflight_root, evaluation)
    retained_evaluation = load_evaluation_v5(evaluation_path)
    native_files = tuple(
        sorted(
            path.name
            for path in (
                managed.project_path,
                managed.schematic_path,
                managed.board_path,
            )
        )
    )
    artifacts = PreflightArtifactRefs(
        project=project_root.relative_to(preflight_root).as_posix(),
        product_terminal=product_path.relative_to(preflight_root).as_posix(),
        run_v2=run_v2_path.relative_to(preflight_root).as_posix(),
        evaluation_v5=evaluation_path.relative_to(preflight_root).as_posix(),
    )
    report = DeterministicPreflightReport(
        generated_at=completed_at,
        run_id=PREFLIGHT_RUN_ID,
        case_id=PREFLIGHT_CASE_ID,
        natural_language_request=PREFLIGHT_REQUEST,
        tool_sequence=tool_sequence,
        native_project_files=native_files,
        product_terminal=terminal,
        normalized_run=normalized,
        evaluation_v5=EvaluationSummaryV5.from_evaluation(retained_evaluation),
        native_consistency=consistency,
        checks=_check_summaries(terminal),
        failure_topologies=failure_topology_preflight(),
        artifacts=artifacts,
    )
    report_path = write_preflight_report(preflight_root, report)
    retained_report = load_preflight_report(report_path)
    return DeterministicPreflightResult(
        preflight_root,
        report_path,
        run_v2_path,
        evaluation_path,
        retained_report,
        run_v2,
        retained_evaluation,
        tuple(producer.observed_inputs),
    )


__all__ = [
    "COMPARISON_PREFLIGHT_MANIFEST_SCHEMA",
    "DETERMINISTIC_PREFLIGHT_REPORT_SCHEMA",
    "PREFLIGHT_LIMITATIONS",
    "PREFLIGHT_NAMESPACE",
    "PREFLIGHT_REQUEST",
    "PREFLIGHT_RUN_ID",
    "CampaignBudgetPlan",
    "CheckEvidenceSummary",
    "ComparisonCampaignManifest",
    "DeterministicPreflightReport",
    "DeterministicPreflightResult",
    "FailureTopologyObservation",
    "comparison_manifest_from_campaign",
    "failure_topology_preflight",
    "freeze_comparison_manifest",
    "load_comparison_manifest",
    "load_preflight_report",
    "run_deterministic_preflight",
    "write_preflight_report",
]
