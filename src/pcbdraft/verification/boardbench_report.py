"""Source-bound BoardBench aggregation, sealing, and public metadata bundles.

This module is deliberately read-only with respect to campaign evidence.  It
projects the fixed campaign plan into 3-run case, 12-run category, and 60-run
overall slices; absent derived evidence remains unknown or not reviewed.  A
seal is stricter: every planned source artifact and every required human or
physical link must be present and hash-bound before any output is created.

The report loader treats ``boardbench_diff.verify_correction_bundle`` as the
authoritative correction API: that function owns snapshot/diff validation and
returns the strict :class:`BoardBenchCorrection` contract consumed here.  The
hardware importer owns authentication of release bytes; this loader can and
does independently rehash the copied attachment tree, but does not invent a
release path that is intentionally absent from the campaign evidence layout.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, TypeVar

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import (
    atomic_write_json,
    atomic_write_text,
    load_json_limited,
    make_directory,
    read_bytes_limited,
    read_text_limited,
)
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.verification.boardbench import (
    AI_REVIEWED_PILOT_COHORT,
    ARTIFACT_FILE_LIMIT,
    AUTOMATIC_METRICS,
    AUTOMATIC_OUTCOMES,
    BOARD_BENCH_LOCK_DIR,
    BOARD_CATEGORIES,
    CORPUS_FILE_LIMIT,
    COST_STATUSES,
    FAILURE_CAUSES,
    FAILURE_OWNERS,
    FAILURE_STAGE_VALUES,
    MAX_INVENTORY_FILE_BYTES,
    PHYSICAL_STATES,
    REVIEW_OUTCOMES,
    TOKEN_SOURCES,
    TOKEN_STATUSES,
    BoardBenchArtifact,
    BoardBenchCampaign,
    BoardBenchCase,
    BoardBenchCorpus,
    BoardBenchCorrection,
    BoardBenchHardware,
    BoardBenchReport,
    BoardBenchReview,
    BoardBenchRun,
    BoardBenchScore,
    BoardBenchSelection,
    CampaignRunPlan,
    EfficiencyAggregate,
    EvidenceValueCount,
    FailureAggregate,
    FailureClassification,
    FailureStageCount,
    MetricAggregate,
    NamedCount,
    NumericSummary,
    PhysicalAggregate,
    PhysicalMetricAggregate,
    ReportSlice,
    ReviewAggregate,
    ToolCallCount,
    artifact_sha256,
    build_inventory,
    case_sha256,
    load_campaign,
    load_hardware,
    load_review,
    load_run,
    load_score,
    load_selection,
    validate_review_sources,
    write_artifact,
)
from pcbdraft.verification.boardbench_diff import verify_correction_bundle

REPORT_JSON_NAME = "report.json"
REPORT_MARKDOWN_NAME = "report.md"
SEAL_MANIFEST_NAME = "seal-manifest.json"
PUBLICATION_MANIFEST_NAME = "publication-manifest.json"
NON_BASELINE_FIXTURE_LABEL = "non_baseline_fixture"
EXECUTION_FILE_LIMIT = 64 * 1024

_ABSOLUTE_POSIX_PATH = re.compile(
    r"(?<![A-Za-z0-9._/:-])/(?!/)(?:[^/\s\"'<>|]+/)*[^/\s\"'<>|]+"
)
_ABSOLUTE_WINDOWS_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9._-])[A-Z]:[\\/](?:[^\\/\s\"'<>|]+[\\/])*"
    r"[^\\/\s\"'<>|]+"
)


@dataclass(frozen=True)
class CampaignEvidence:
    """Strict artifacts discovered at the fixed campaign evidence locations."""

    corpus: BoardBenchCorpus
    campaign: BoardBenchCampaign
    runs: tuple[BoardBenchRun, ...]
    scores: tuple[BoardBenchScore, ...]
    reviews: tuple[BoardBenchReview, ...]
    corrections: tuple[BoardBenchCorrection, ...]
    hardware: tuple[BoardBenchHardware, ...]
    selection: BoardBenchSelection | None
    fixture_run_ids: frozenset[str] = frozenset()


IndexValue = TypeVar("IndexValue")
RunArtifact: TypeAlias = (
    BoardBenchRun
    | BoardBenchScore
    | BoardBenchReview
    | BoardBenchCorrection
    | BoardBenchHardware
)


@dataclass(frozen=True)
class _EvidenceIndex:
    cases: Mapping[str, BoardBenchCase]
    runs: Mapping[str, BoardBenchRun]
    scores: Mapping[str, BoardBenchScore]
    reviews: Mapping[str, BoardBenchReview]
    corrections: Mapping[str, BoardBenchCorrection]
    hardware: Mapping[str, BoardBenchHardware]


ArtifactValue = TypeVar("ArtifactValue")


def _optional_artifact(
    path: Path, loader: Callable[[str | Path], ArtifactValue]
) -> ArtifactValue | None:
    if not path.exists() and not path.is_symlink():
        return None
    return loader(path)


def _validate_evidence_directory(
    root: Path, name: str, planned_run_ids: frozenset[str]
) -> None:
    directory = root / name
    if not directory.exists() and not directory.is_symlink():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError(f"BoardBench canonical {name} directory is unsafe")
    for child in directory.iterdir():
        if child.name == BOARD_BENCH_LOCK_DIR:
            if child.is_symlink() or not child.is_dir():
                raise ValidationError(
                    f"BoardBench canonical {name} lock directory is unsafe"
                )
            continue
        if (
            child.name not in planned_run_ids
            or child.is_symlink()
            or not child.is_dir()
        ):
            raise ValidationError(
                f"BoardBench canonical {name} contains unexpected evidence"
            )


def _execution_fixture_label(artifacts: Path) -> tuple[bool, str | None]:
    path = artifacts / "execution.json"
    if not path.exists() and not path.is_symlink():
        return False, None
    value = load_json_limited(path, EXECUTION_FILE_LIMIT)
    fields = {
        "schema",
        "version",
        "argv",
        "returncode",
        "duration_seconds",
        "timed_out",
        "output_limited",
        "retained_project_count",
        "fixture_label",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema") != "pcbdraft-boardbench-worker-execution"
        or value.get("version") != 1
        or value.get("fixture_label") not in {None, NON_BASELINE_FIXTURE_LABEL}
    ):
        raise ValidationError("BoardBench execution evidence is malformed")
    return True, value["fixture_label"]


def load_campaign_evidence(
    campaign_root: str | Path, corpus: BoardBenchCorpus
) -> CampaignEvidence:
    """Load only the canonical BoardBench evidence layout.

    Derived evidence is stored outside immutable run artifact inventories:
    ``scores/<run_id>/score.json``, ``reviews/<run_id>/review.json``,
    ``corrections/<run_id>/correction.json``, and
    ``hardware/<run_id>/hardware.json``.  Missing optional evidence is retained
    as absence for draft aggregation and rejected later by the seal gate.
    """

    raw_root = Path(campaign_root).expanduser().absolute()
    if raw_root.is_symlink() or any(parent.is_symlink() for parent in raw_root.parents):
        raise ValidationError("BoardBench report campaign path traverses a symlink")
    if not raw_root.is_dir():
        raise ValidationError("BoardBench report campaign directory is unavailable")
    root = raw_root.resolve(strict=True)
    campaign = load_campaign(root / "campaign.json")
    planned_run_ids = frozenset(plan.run_id for plan in campaign.runs)
    for directory in ("runs", "scores", "reviews", "corrections", "hardware"):
        _validate_evidence_directory(root, directory, planned_run_ids)
    runs: list[BoardBenchRun] = []
    scores: list[BoardBenchScore] = []
    reviews: list[BoardBenchReview] = []
    corrections: list[BoardBenchCorrection] = []
    hardware: list[BoardBenchHardware] = []
    fixture_run_ids: set[str] = set()
    for plan in campaign.runs:
        run = _optional_artifact(root / "runs" / plan.run_id / "run.json", load_run)
        score = _optional_artifact(
            root / "scores" / plan.run_id / "score.json", load_score
        )
        review = _optional_artifact(
            root / "reviews" / plan.run_id / "review.json", load_review
        )
        correction_path = root / "corrections" / plan.run_id / "correction.json"
        correction = None
        if correction_path.exists() or correction_path.is_symlink():
            correction = verify_correction_bundle(
                correction_path.parent,
                run_receipt_path=root / "runs" / plan.run_id / "run.json",
                review_path=root / "reviews" / plan.run_id / "review.json",
            )
        hardware_record = _optional_artifact(
            root / "hardware" / plan.run_id / "hardware.json", load_hardware
        )
        if run is not None:
            run_root = root / "runs" / plan.run_id
            run_lock = run_root / BOARD_BENCH_LOCK_DIR
            if (run_lock.exists() or run_lock.is_symlink()) and (
                run_lock.is_symlink() or not run_lock.is_dir()
            ):
                raise ValidationError("BoardBench raw run lock directory is unsafe")
            unexpected = {
                child.name
                for child in run_root.iterdir()
                if child.name not in {"run.json", "artifacts", BOARD_BENCH_LOCK_DIR}
            }
            if unexpected:
                raise ValidationError(
                    "BoardBench raw run contains unexpected mutable evidence"
                )
            if run.terminal:
                artifacts = root / "runs" / plan.run_id / "artifacts"
                if artifacts.is_symlink() or not artifacts.is_dir():
                    raise ValidationError(
                        "BoardBench terminal run artifact tree is unavailable"
                    )
                if build_inventory(artifacts) != run.inventory:
                    raise ValidationError(
                        "BoardBench terminal run artifact inventory differs"
                    )
                execution_present, fixture_label = _execution_fixture_label(artifacts)
                if execution_present and fixture_label == NON_BASELINE_FIXTURE_LABEL:
                    fixture_run_ids.add(plan.run_id)
            runs.append(run)
        if score is not None:
            scores.append(score)
        if review is not None:
            reviews.append(review)
        if correction is not None:
            corrections.append(correction)
        if hardware_record is not None:
            attachments = root / "hardware" / plan.run_id / "attachments"
            if attachments.is_symlink() or not attachments.is_dir():
                raise ValidationError(
                    "BoardBench hardware attachment tree is unavailable"
                )
            if build_inventory(attachments) != hardware_record.attachments:
                raise ValidationError(
                    "BoardBench hardware attachment inventory differs"
                )
            if hardware_record.source_kind == "release":
                release = (
                    root / "hardware" / plan.run_id / "source" / "release-artifact"
                )
                if release.is_symlink() or not release.is_file():
                    raise ValidationError(
                        "BoardBench hardware release source is unavailable"
                    )
                release_hash = hashlib.sha256(
                    read_bytes_limited(release, MAX_INVENTORY_FILE_BYTES)
                ).hexdigest()
                if release_hash != hardware_record.source_artifact_sha256:
                    raise ValidationError(
                        "BoardBench hardware release source hash differs"
                    )
            hardware.append(hardware_record)
    selection = _optional_artifact(root / "selection.json", load_selection)
    result = CampaignEvidence(
        corpus=corpus,
        campaign=campaign,
        runs=tuple(runs),
        scores=tuple(scores),
        reviews=tuple(reviews),
        corrections=tuple(corrections),
        hardware=tuple(hardware),
        selection=selection,
        fixture_run_ids=frozenset(fixture_run_ids),
    )
    _validate_sources(result)
    return result


def _index(
    values: Sequence[IndexValue],
    label: str,
    identity: Callable[[IndexValue], str],
) -> dict[str, IndexValue]:
    result: dict[str, IndexValue] = {}
    for value in values:
        run_id = identity(value)
        if run_id in result:
            raise ValidationError(f"duplicate BoardBench {label} for one run")
        result[run_id] = value
    return result


def _validate_sources(evidence: CampaignEvidence) -> _EvidenceIndex:
    corpus = evidence.corpus
    campaign = evidence.campaign
    if (
        campaign.corpus_id != corpus.corpus_id
        or campaign.cohort != corpus.cohort
        or campaign.corpus_sha256 != artifact_sha256(corpus)
    ):
        raise ValidationError("BoardBench report corpus does not match campaign")
    cases = {case.id: case for case in corpus.cases}
    plans = {plan.run_id: plan for plan in campaign.runs}
    if {plan.case_id for plan in campaign.runs} != set(cases):
        raise ValidationError("BoardBench report campaign/corpus case set differs")
    if not evidence.fixture_run_ids <= set(plans):
        raise ValidationError("BoardBench fixture evidence is outside campaign plan")

    runs = _index(evidence.runs, "run", lambda item: item.run_id)
    scores = _index(evidence.scores, "score", lambda item: item.run_id)
    reviews = _index(evidence.reviews, "review", lambda item: item.run_id)
    corrections = _index(evidence.corrections, "correction", lambda item: item.run_id)
    hardware = _index(evidence.hardware, "hardware record", lambda item: item.run_id)
    for label, values in (
        ("run", runs),
        ("score", scores),
        ("review", reviews),
        ("correction", corrections),
        ("hardware", hardware),
    ):
        if unknown := set(values) - set(plans):
            raise ValidationError(
                f"BoardBench report contains {label} outside campaign plan: "
                f"{min(unknown)}"
            )

    campaign_hash = artifact_sha256(campaign)
    for run_id, run_value in runs.items():
        plan = plans[run_id]
        case = cases[plan.case_id]
        expected_prompt = hashlib.sha256(case.prompt.encode("utf-8")).hexdigest()
        if (
            run_value.campaign_id != campaign.campaign_id
            or run_value.case_id != plan.case_id
            or run_value.repetition != plan.repetition
            or run_value.prompt_sha256 != expected_prompt
        ):
            raise ValidationError(
                "BoardBench run does not match campaign/corpus source"
            )

    for run_id, score in scores.items():
        source_run = runs.get(run_id)
        if source_run is None or not source_run.terminal:
            raise ValidationError("BoardBench score has no source run")
        case = cases[source_run.case_id]
        if (
            score.campaign_id != campaign.campaign_id
            or score.source_campaign_sha256 != campaign_hash
            or score.source_case_sha256 != case_sha256(case)
            or score.source_run_sha256 != artifact_sha256(source_run)
            or score.evaluator_version != campaign.evaluator_version
        ):
            raise ValidationError("BoardBench score source hash or version differs")

    for run_id, review in reviews.items():
        source_run = runs.get(run_id)
        if source_run is None:
            raise ValidationError("BoardBench review source hash differs")
        source_score = scores.get(run_id)
        validate_review_sources(
            review,
            campaign=campaign,
            corpus=corpus,
            case=cases[source_run.case_id],
            run=source_run,
            score=source_score,
        )

    for run_id, correction in corrections.items():
        source_run = runs.get(run_id)
        source_review = reviews.get(run_id)
        if (
            source_run is None
            or not source_run.terminal
            or source_review is None
            or correction.campaign_id != campaign.campaign_id
            or correction.source_run_sha256 != artifact_sha256(source_run)
            or correction.source_review_sha256 != artifact_sha256(source_review)
            or set(correction.decision_ids)
            != {decision.id for decision in source_review.modifications}
        ):
            raise ValidationError("BoardBench correction source or decisions differ")

    selected = (
        {item.run_id: item for item in evidence.selection.selections}
        if evidence.selection is not None
        else {}
    )
    if (
        evidence.selection is not None
        and evidence.selection.campaign_id != campaign.campaign_id
    ):
        raise ValidationError("BoardBench selection campaign differs")
    for run_id, selection_entry in selected.items():
        source_plan = plans.get(run_id)
        source_run = runs.get(run_id)
        if (
            source_plan is None
            or source_run is None
            or not source_run.terminal
            or cases[source_plan.case_id].category != selection_entry.category
        ):
            raise ValidationError("BoardBench selection category or run differs")
        source_correction = corrections.get(run_id)
        source_review = reviews.get(run_id)
        expected_selection_hash = (
            source_correction.manufacturing_candidate_sha256
            if source_correction is not None
            else artifact_sha256(source_run)
        )
        if (
            source_review is None
            or (
                source_correction is None
                and (
                    source_review.outcome != "pass_without_schematic_change"
                    or source_review.orderable_state != "pass"
                )
            )
            or expected_selection_hash is None
            or selection_entry.source_artifact_sha256 != expected_selection_hash
        ):
            raise ValidationError("BoardBench selection source hash differs")

    for run_id, record in hardware.items():
        source_run = runs.get(run_id)
        hardware_selection = selected.get(run_id)
        if (
            source_run is None
            or not source_run.terminal
            or record.campaign_id != campaign.campaign_id
            or record.category != cases[source_run.case_id].category
            or hardware_selection is None
        ):
            raise ValidationError(
                "BoardBench hardware source run, selection, or category differs"
            )
        if hardware_selection.category != record.category:
            raise ValidationError("BoardBench hardware/selection category differs")
        if record.source_kind == "correction":
            source_correction = corrections.get(run_id)
            if (
                source_correction is None
                or artifact_sha256(source_correction) != record.source_artifact_sha256
            ):
                raise ValidationError("BoardBench hardware correction source differs")

    return _EvidenceIndex(
        cases=cases,
        runs=runs,
        scores=scores,
        reviews=reviews,
        corrections=corrections,
        hardware=hardware,
    )


def _named_counts(
    values: Sequence[str] | frozenset[str], counts: Counter[str]
) -> tuple[NamedCount, ...]:
    return tuple(NamedCount(value, counts[value]) for value in sorted(values))


def _evidence_counts(counts: Counter[str]) -> tuple[EvidenceValueCount, ...]:
    return tuple(
        EvidenceValueCount(value, count)
        for value, count in sorted(counts.items())
        if count
    )


def _numeric(values: Sequence[float | int | None]) -> NumericSummary:
    observed = [float(value) for value in values if value is not None]
    missing = len(values) - len(observed)
    if not observed:
        return NumericSummary(0, missing, None, None, None, None)
    total = sum(observed)
    return NumericSummary(
        observed_count=len(observed),
        missing_count=missing,
        sum_value=total,
        minimum=min(observed),
        maximum=max(observed),
        mean=total / len(observed),
    )


def _metric_aggregate(
    name: str,
    plans: Sequence[CampaignRunPlan],
    index: _EvidenceIndex,
) -> MetricAggregate:
    counts: Counter[str] = Counter()
    for plan in plans:
        score = index.scores.get(plan.run_id)
        if score is None:
            state = (
                "unknown"
                if name in index.cases[plan.case_id].applicable_metrics
                else "not_applicable"
            )
        else:
            state = next(item.state for item in score.metrics if item.name == name)
        counts[state] += 1
    return MetricAggregate(
        name=name,
        total=len(plans),
        passed=counts["pass"],
        failed=counts["fail"],
        unknown=counts["unknown"],
        not_applicable=counts["not_applicable"],
    )


def _review_aggregate(
    plans: Sequence[CampaignRunPlan], index: _EvidenceIndex
) -> ReviewAggregate:
    outcomes: Counter[str] = Counter()
    modifications: list[float | None] = []
    minutes: list[float | None] = []
    required = 0
    present = 0
    for plan in plans:
        review = index.reviews.get(plan.run_id)
        if review is None or review.outcome == "not_reviewed":
            outcomes["not_reviewed"] += 1
            modifications.append(None)
            minutes.append(None)
            continue
        outcomes[review.outcome] += 1
        modifications.append(float(review.modification_count))
        minutes.append(review.active_engineer_minutes)
        if review.modifications:
            required += 1
            if plan.run_id in index.corrections:
                present += 1
    return ReviewAggregate(
        denominator=len(plans),
        outcomes=_named_counts(REVIEW_OUTCOMES, outcomes),
        corrections_required=required,
        corrections_present=present,
        modifications=_numeric(modifications),
        active_minutes=_numeric(minutes),
    )


def _failure_for(run_id: str, index: _EvidenceIndex) -> FailureClassification | None:
    score = index.scores.get(run_id)
    review = index.reviews.get(run_id)
    automatic_failure = score is None or score.overall_state != "pass"
    review_failure = review is not None and review.outcome in {
        "pass_after_changes",
        "fail",
        "not_applicable",
    }
    if not automatic_failure and not review_failure:
        return None
    if review is not None and review.final_failure is not None:
        return review.final_failure
    return FailureClassification(
        stage="unclassified",
        causes=("unclassified",),
        owners=("unclassified",),
        reason="required failure classification is unavailable",
    )


def _failure_aggregate(
    plans: Sequence[CampaignRunPlan], index: _EvidenceIndex
) -> FailureAggregate:
    failures = [
        classification
        for plan in plans
        if (classification := _failure_for(plan.run_id, index)) is not None
    ]
    stages = Counter(item.stage for item in failures)
    causes = Counter(cause for item in failures for cause in item.causes)
    owners = Counter(owner for item in failures for owner in item.owners)
    return FailureAggregate(
        failed_runs=len(failures),
        stages=tuple(
            FailureStageCount(stage, stages[stage])
            for stage in sorted(FAILURE_STAGE_VALUES)
        ),
        causes=_named_counts(FAILURE_CAUSES, causes),
        owners=_named_counts(FAILURE_OWNERS, owners),
    )


def _efficiency_aggregate(
    plans: Sequence[CampaignRunPlan], index: _EvidenceIndex
) -> EfficiencyAggregate:
    token_statuses: Counter[str] = Counter()
    token_sources: Counter[str] = Counter()
    cost_statuses: Counter[str] = Counter()
    cost_sources: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    missing_failure_reasons = 0
    model_requests: list[int | None] = []
    total_tokens: list[int | None] = []
    cost_amounts: list[float | None] = []
    pcb_tool_calls: list[int | None] = []
    retries: list[int | None] = []
    errors: list[int | None] = []
    tool_seconds: list[float | None] = []
    api_seconds: list[float | None] = []
    wall_seconds: list[float | None] = []
    tool_counts: Counter[tuple[str, str]] = Counter()
    currencies: set[str] = set()
    for plan in plans:
        score = index.scores.get(plan.run_id)
        if score is None:
            token_statuses["unknown"] += 1
            token_sources["unavailable"] += 1
            cost_statuses["unknown"] += 1
            cost_sources["score_unavailable"] += 1
            failure_reasons["score_unavailable"] += 1
            model_requests.append(None)
            total_tokens.append(None)
            cost_amounts.append(None)
            pcb_tool_calls.append(None)
            retries.append(None)
            errors.append(None)
            tool_seconds.append(None)
            api_seconds.append(None)
            wall_seconds.append(None)
            continue
        efficiency = score.efficiency
        token_statuses[efficiency.token_status] += 1
        token_sources[efficiency.token_source] += 1
        cost_statuses[efficiency.cost_status] += 1
        cost_sources[efficiency.cost_source] += 1
        model_requests.append(efficiency.model_requests)
        total_tokens.append(efficiency.total_tokens)
        cost_amounts.append(efficiency.cost_amount)
        pcb_tool_calls.append(efficiency.pcb_tool_calls)
        retries.append(efficiency.provider_retries)
        errors.append(efficiency.provider_errors)
        tool_seconds.append(efficiency.tool_seconds)
        api_seconds.append(efficiency.api_seconds)
        wall_seconds.append(efficiency.wall_seconds)
        if efficiency.cost_currency is not None:
            currencies.add(efficiency.cost_currency)
        for item in efficiency.tool_call_counts:
            tool_counts[(item.name, item.status)] += item.count
        if efficiency.failure_reason is None:
            missing_failure_reasons += 1
        else:
            failure_reasons[efficiency.failure_reason] += 1
    if len(currencies) > 1:
        raise ValidationError("BoardBench report cannot aggregate mixed currencies")
    return EfficiencyAggregate(
        denominator=len(plans),
        token_statuses=_named_counts(TOKEN_STATUSES, token_statuses),
        token_sources=_named_counts(TOKEN_SOURCES, token_sources),
        cost_statuses=_named_counts(COST_STATUSES, cost_statuses),
        cost_sources=_evidence_counts(cost_sources),
        model_requests=_numeric(model_requests),
        total_tokens=_numeric(total_tokens),
        cost_amount=_numeric(cost_amounts),
        cost_currency=next(iter(currencies), None),
        pcb_tool_calls=_numeric(pcb_tool_calls),
        tool_call_counts=tuple(
            ToolCallCount(name, status, count)
            for (name, status), count in sorted(tool_counts.items())
            if count
        ),
        provider_retries=_numeric(retries),
        provider_errors=_numeric(errors),
        tool_seconds=_numeric(tool_seconds),
        api_seconds=_numeric(api_seconds),
        wall_seconds=_numeric(wall_seconds),
        failure_reasons=_evidence_counts(failure_reasons),
        missing_failure_reason_count=missing_failure_reasons,
    )


def _rail_state(record: BoardBenchHardware) -> str:
    states = {item.state for item in record.rails}
    if "fail" in states:
        return "fail"
    if "not_tested" in states:
        return "not_tested"
    return "pass"


def _physical_aggregate(
    plans: Sequence[CampaignRunPlan], index: _EvidenceIndex
) -> PhysicalAggregate:
    records = [
        index.hardware[plan.run_id] for plan in plans if plan.run_id in index.hardware
    ]
    attributes: Mapping[str, Callable[[BoardBenchHardware], str]] = {
        "fabricator_accepted": lambda item: item.fabricator_accepted,
        "solderability": lambda item: item.solderability,
        "first_power_no_short": lambda item: item.first_power_no_short,
        "power_rails": _rail_state,
        "firmware_download": lambda item: item.firmware_download,
        "core_function": lambda item: item.core_function,
    }
    metrics = tuple(
        PhysicalMetricAggregate(
            name=name,
            outcomes=_named_counts(
                PHYSICAL_STATES,
                Counter(project(record) for record in records),
            ),
        )
        for name, project in attributes.items()
    )
    return PhysicalAggregate(
        records=len(records),
        metrics=metrics,
        revision_counts=_numeric([record.revision_count for record in records]),
    )


def _slice(
    scope: str,
    identity: str,
    category: str | None,
    plans: Sequence[CampaignRunPlan],
    index: _EvidenceIndex,
) -> ReportSlice:
    outcomes = Counter(
        index.scores[plan.run_id].overall_state
        if plan.run_id in index.scores
        else "unknown"
        for plan in plans
    )
    return ReportSlice(
        scope=scope,
        id=identity,
        category=category,
        planned_runs=len(plans),
        terminal_runs=sum(
            1
            for plan in plans
            if plan.run_id in index.runs and index.runs[plan.run_id].terminal
        ),
        automatic_outcomes=_named_counts(AUTOMATIC_OUTCOMES, outcomes),
        automatic_metrics=tuple(
            _metric_aggregate(name, plans, index) for name in AUTOMATIC_METRICS
        ),
        reviews=_review_aggregate(plans, index),
        failures=_failure_aggregate(plans, index),
        efficiency=_efficiency_aggregate(plans, index),
        physical=_physical_aggregate(plans, index),
    )


def _require_sealable(evidence: CampaignEvidence, index: _EvidenceIndex) -> None:
    if evidence.campaign.cohort == AI_REVIEWED_PILOT_COHORT:
        raise ValidationError(
            "BoardBench AI-reviewed pilot campaigns cannot be sealed or published "
            "as a human-reviewed baseline"
        )
    if evidence.fixture_run_ids:
        raise ValidationError(
            "BoardBench non-baseline fixture runs cannot be sealed or published"
        )
    expected = {plan.run_id for plan in evidence.campaign.runs}
    if set(index.runs) != expected or any(
        not run.terminal for run in index.runs.values()
    ):
        raise ValidationError("BoardBench seal requires 60 terminal run receipts")
    if set(index.scores) != expected:
        raise ValidationError("BoardBench seal requires 60 source-bound scores")
    if set(index.reviews) != expected or any(
        review.outcome == "not_reviewed" for review in index.reviews.values()
    ):
        raise ValidationError("BoardBench seal requires 60 completed reviews")
    unclassified = [
        plan.run_id
        for plan in evidence.campaign.runs
        if (failure := _failure_for(plan.run_id, index)) is not None
        and (
            failure.stage == "unclassified"
            or "unclassified" in failure.causes
            or "unclassified" in failure.owners
        )
    ]
    if unclassified:
        raise ValidationError("BoardBench seal contains unclassified failures")
    required_corrections = {
        run_id for run_id, review in index.reviews.items() if review.modifications
    }
    if not required_corrections <= set(index.corrections):
        raise ValidationError("BoardBench seal has missing correction artifacts")
    if evidence.selection is None:
        raise ValidationError("BoardBench seal requires a selection artifact")
    selected = {item.run_id for item in evidence.selection.selections}
    if set(index.hardware) != selected:
        raise ValidationError(
            "BoardBench seal requires one hardware record per selected run"
        )
    if len(index.hardware) < 5 or {
        record.category for record in index.hardware.values()
    } != set(BOARD_CATEGORIES):
        raise ValidationError(
            "BoardBench seal requires real hardware in five categories"
        )
    for record in index.hardware.values():
        physical_states = (
            record.fabricator_accepted,
            record.solderability,
            record.first_power_no_short,
            record.firmware_download,
            record.core_function,
            *(rail.state for rail in record.rails),
        )
        if "not_tested" in physical_states:
            raise ValidationError(
                "BoardBench seal requires completed physical hardware observations"
            )


def aggregate_report(
    corpus: BoardBenchCorpus,
    campaign: BoardBenchCampaign,
    *,
    runs: Sequence[BoardBenchRun] = (),
    scores: Sequence[BoardBenchScore] = (),
    reviews: Sequence[BoardBenchReview] = (),
    corrections: Sequence[BoardBenchCorrection] = (),
    hardware: Sequence[BoardBenchHardware] = (),
    selection: BoardBenchSelection | None = None,
    sealed: bool = False,
    generated_at: str | None = None,
) -> BoardBenchReport:
    """Aggregate a draft exact campaign plan without dropping missing evidence."""

    if sealed:
        raise ValidationError(
            "sealed BoardBench reports require canonical campaign evidence"
        )

    evidence = CampaignEvidence(
        corpus=corpus,
        campaign=campaign,
        runs=tuple(runs),
        scores=tuple(scores),
        reviews=tuple(reviews),
        corrections=tuple(corrections),
        hardware=tuple(hardware),
        selection=selection,
    )
    return _aggregate_evidence(evidence, sealed=False, generated_at=generated_at)


def _aggregate_evidence(
    evidence: CampaignEvidence,
    *,
    sealed: bool,
    generated_at: str | None,
) -> BoardBenchReport:
    index = _validate_sources(evidence)
    if sealed:
        _require_sealable(evidence, index)
    corpus = evidence.corpus
    campaign = evidence.campaign
    case_slices = []
    for case in corpus.cases:
        plans = tuple(plan for plan in campaign.runs if plan.case_id == case.id)
        case_slices.append(_slice("case", case.id, case.category, plans, index))
    category_slices = []
    for category in BOARD_CATEGORIES:
        plans = tuple(
            plan
            for plan in campaign.runs
            if index.cases[plan.case_id].category == category
        )
        category_slices.append(_slice("category", category, category, plans, index))
    overall = _slice("overall", "overall", None, campaign.runs, index)
    return BoardBenchReport(
        campaign_id=campaign.campaign_id,
        campaign_sha256=artifact_sha256(campaign),
        cohort=campaign.cohort,
        evaluator_version=campaign.evaluator_version,
        generated_at=generated_at or utc_timestamp(),
        slices=(overall, *category_slices, *case_slices),
        sealed=sealed,
    )


def render_markdown(report: BoardBenchReport) -> str:
    """Render a bounded factual summary without implying engineering sign-off."""

    overall = next(item for item in report.slices if item.scope == "overall")
    outcomes = {item.value: item.count for item in overall.automatic_outcomes}
    reviews = {item.value: item.count for item in overall.reviews.outcomes}
    physical = {
        metric.name: {item.value: item.count for item in metric.outcomes}
        for metric in overall.physical.metrics
    }
    lines = [
        "# BoardBench report",
        "",
        f"Campaign: `{report.campaign_id}`  ",
        f"Cohort: `{report.cohort}`  ",
        f"Sealed: `{'yes' if report.sealed else 'no'}`",
    ]
    if report.cohort == AI_REVIEWED_PILOT_COHORT:
        lines.extend(
            [
                "",
                (
                    "This is an AI-reviewed pilot, not an independent human-reviewed "
                    "or sealed BoardBench baseline."
                ),
            ]
        )
    lines.extend(
        [
            "",
            (
                "Every percentage and count uses the frozen 60-run campaign plan. "
                "Missing evidence remains unknown or not reviewed."
            ),
            "",
            "## Overall",
            "",
            "| Planned | Terminal | Auto pass | Auto fail | Auto unknown | No-change review pass |",
            "| ---: | ---: | ---: | ---: | ---: | ---: |",
            (
                f"| {overall.planned_runs} | {overall.terminal_runs} | "
                f"{outcomes['pass']} | {outcomes['fail']} | {outcomes['unknown']} | "
                f"{reviews['pass_without_schematic_change']} |"
            ),
            "",
            "## Automatic metrics",
            "",
            "| Metric | Total | Pass | Fail | Unknown | Not applicable |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        f"| `{metric.name}` | {metric.total} | {metric.passed} | "
        f"{metric.failed} | {metric.unknown} | {metric.not_applicable} |"
        for metric in overall.automatic_metrics
    )
    token_statuses = {
        item.value: item.count for item in overall.efficiency.token_statuses
    }
    cost_statuses = {
        item.value: item.count for item in overall.efficiency.cost_statuses
    }
    cost = overall.efficiency.cost_amount
    priced_cost = (
        "unknown"
        if cost.sum_value is None
        else f"{cost.sum_value:g} {overall.efficiency.cost_currency or ''}".strip()
    )
    lines.extend(
        [
            "",
            "## Efficiency and cost evidence",
            "",
            "| Dimension | Observed | Missing | Sum |",
            "| --- | ---: | ---: | ---: |",
            (
                f"| Model requests | {overall.efficiency.model_requests.observed_count} | "
                f"{overall.efficiency.model_requests.missing_count} | "
                f"{overall.efficiency.model_requests.sum_value or 0:g} |"
            ),
            (
                f"| Total tokens | {overall.efficiency.total_tokens.observed_count} | "
                f"{overall.efficiency.total_tokens.missing_count} | "
                f"{overall.efficiency.total_tokens.sum_value or 0:g} |"
            ),
            (
                f"| PCB tool calls | {overall.efficiency.pcb_tool_calls.observed_count} | "
                f"{overall.efficiency.pcb_tool_calls.missing_count} | "
                f"{overall.efficiency.pcb_tool_calls.sum_value or 0:g} |"
            ),
            (
                f"| Wall seconds | {overall.efficiency.wall_seconds.observed_count} | "
                f"{overall.efficiency.wall_seconds.missing_count} | "
                f"{overall.efficiency.wall_seconds.sum_value or 0:g} |"
            ),
            "",
            (
                f"Token status: {token_statuses['reported']} reported, "
                f"{token_statuses['derived']} derived, "
                f"{token_statuses['partial']} partial, "
                f"{token_statuses['unknown']} unknown."
            ),
            (
                f"Cost status: {cost_statuses['actual']} actual, "
                f"{cost_statuses['estimated']} estimated, "
                f"{cost_statuses['subscription_included']} subscription-included, "
                f"{cost_statuses['unknown']} unknown; priced sum: {priced_cost}."
            ),
            "",
            "## Failure classification",
            "",
            "| Primary stage | Runs |",
            "| --- | ---: |",
        ]
    )
    lines.extend(
        f"| `{item.stage}` | {item.count} |" for item in overall.failures.stages
    )
    lines.extend(
        [
            "",
            "## Category coverage",
            "",
            "| Category | Planned | Terminal | Reviews completed | Hardware records |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in (slice_ for slice_ in report.slices if slice_.scope == "category"):
        not_reviewed = next(
            count.count
            for count in item.reviews.outcomes
            if count.value == "not_reviewed"
        )
        lines.append(
            f"| `{item.id}` | {item.planned_runs} | {item.terminal_runs} | "
            f"{item.planned_runs - not_reviewed} | {item.physical.records} |"
        )
    lines.extend(
        [
            "",
            "## Case results",
            "",
            "| Case | Category | Auto pass | Auto fail | Auto unknown | No-change review pass |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in (slice_ for slice_ in report.slices if slice_.scope == "case"):
        case_outcomes = {count.value: count.count for count in item.automatic_outcomes}
        case_reviews = {count.value: count.count for count in item.reviews.outcomes}
        lines.append(
            f"| `{item.id}` | `{item.category}` | {case_outcomes['pass']} | "
            f"{case_outcomes['fail']} | {case_outcomes['unknown']} | "
            f"{case_reviews['pass_without_schematic_change']} |"
        )
    first_power = physical["first_power_no_short"]
    core = physical["core_function"]
    lines.extend(
        [
            "",
            "## Physical evidence",
            "",
            (
                f"First-power no-short: {first_power['pass']} pass, "
                f"{first_power['fail']} fail, "
                f"{first_power['not_tested']} not tested."
            ),
            (
                f"Core function: {core['pass']} pass, {core['fail']} fail, "
                f"{core['not_tested']} not tested."
            ),
            "",
            (
                "ERC/DRC success is necessary evidence only; it is not proof of "
                "functional correctness, manufacturability, or production readiness."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _staging_directory(path: str | Path, label: str) -> tuple[Path, Path]:
    raw = Path(path).expanduser()
    if any(part in {".", ".."} for part in raw.parts) or raw.name in {"", ".", ".."}:
        raise ValidationError(f"BoardBench {label} output path is unsafe")
    target = raw.absolute()
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ValidationError(f"BoardBench {label} output path traverses a symlink")
    if target.exists():
        raise ValidationError(f"BoardBench {label} output must be a fresh directory")
    parent = make_directory(target.parent).resolve(strict=True)
    if parent != target.parent or any(
        component.is_symlink() for component in (parent, *parent.parents)
    ):
        raise ValidationError(f"BoardBench {label} output parent is unsafe")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    return target, staging


def _publish_directory(staging: Path, target: Path, label: str) -> Path:
    if target.exists() or target.is_symlink():
        raise ValidationError(f"BoardBench {label} output must be a fresh directory")
    try:
        os.rename(staging, target)
    except OSError as exc:
        raise ValidationError(f"cannot publish BoardBench {label} output") from exc
    return target


def _write_report_files(root: Path, report: BoardBenchReport) -> None:
    write_artifact(root / REPORT_JSON_NAME, report)
    atomic_write_text(root / REPORT_MARKDOWN_NAME, render_markdown(report), mode=0o600)


def write_report(output: str | Path, report: BoardBenchReport) -> Path:
    """Write one new private JSON/Markdown report directory."""

    target, staging = _staging_directory(output, "report")
    published = False
    try:
        _write_report_files(staging, report)
        result = _publish_directory(staging, target, "report")
        published = True
        return result
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


def _source_manifest(
    evidence: CampaignEvidence, report: BoardBenchReport
) -> dict[str, object]:
    artifacts: list[dict[str, str]] = [
        {
            "kind": "corpus",
            "id": evidence.corpus.corpus_id,
            "sha256": artifact_sha256(evidence.corpus),
        },
        {
            "kind": "campaign",
            "id": evidence.campaign.campaign_id,
            "sha256": artifact_sha256(evidence.campaign),
        },
        {"kind": "report", "id": report.campaign_id, "sha256": artifact_sha256(report)},
    ]

    def add_run_sources(kind: str, values: Sequence[RunArtifact]) -> None:
        artifacts.extend(
            {"kind": kind, "id": item.run_id, "sha256": artifact_sha256(item)}
            for item in sorted(values, key=lambda value: value.run_id)
        )

    add_run_sources("run", evidence.runs)
    add_run_sources("score", evidence.scores)
    add_run_sources("review", evidence.reviews)
    add_run_sources("correction", evidence.corrections)
    add_run_sources("hardware", evidence.hardware)
    if evidence.selection is not None:
        artifacts.append(
            {
                "kind": "selection",
                "id": evidence.selection.campaign_id,
                "sha256": artifact_sha256(evidence.selection),
            }
        )
    return {
        "schema": "pcbdraft-boardbench-seal-manifest",
        "version": 1,
        "campaign_id": evidence.campaign.campaign_id,
        "artifacts": artifacts,
    }


def seal_campaign(
    output: str | Path,
    campaign_root: str | Path,
    corpus: BoardBenchCorpus,
    *,
    generated_at: str | None = None,
) -> BoardBenchReport:
    """Deep-load canonical evidence, then atomically publish a sealed report."""

    evidence = load_campaign_evidence(campaign_root, corpus)
    report = _aggregate_evidence(evidence, sealed=True, generated_at=generated_at)
    campaign_path = Path(campaign_root).expanduser().resolve(strict=True)
    candidate = Path(output).expanduser().absolute().resolve(strict=False)
    raw_runs = (campaign_path / "runs").resolve(strict=True)
    if candidate == raw_runs or raw_runs in candidate.parents:
        raise ValidationError("BoardBench seal output cannot mutate raw runs")
    target, staging = _staging_directory(output, "seal")
    published = False
    try:
        _write_report_files(staging, report)
        manifest = _source_manifest(evidence, report)
        manifest["report_files"] = [
            entry.to_dict() for entry in build_inventory(staging)
        ]
        atomic_write_json(staging / SEAL_MANIFEST_NAME, manifest, mode=0o600)
        _publish_directory(staging, target, "seal")
        published = True
        return report
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


def _sanitize_public_string(value: str) -> str:
    result = sanitize_user_text(value)
    for private_root in (str(Path.cwd().resolve()), str(Path.home().resolve())):
        if private_root not in {"", "/"}:
            result = result.replace(private_root, "[REDACTED_PATH]")
    result = _ABSOLUTE_WINDOWS_PATH.sub("[REDACTED_PATH]", result)
    return _ABSOLUTE_POSIX_PATH.sub("[REDACTED_PATH]", result)


def _sanitize_public_value(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_public_string(value)
    if isinstance(value, Mapping):
        return {key: _sanitize_public_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_public_value(item) for item in value]
    return value


def _published_artifact(
    root: Path,
    relative: str,
    artifact: BoardBenchArtifact,
    source_hashes: dict[str, str | None],
) -> None:
    path = root / relative
    atomic_write_json(path, _sanitize_public_value(artifact.to_dict()), mode=0o600)
    source_hashes[relative] = artifact_sha256(artifact)


def _published_run_artifacts(
    root: Path,
    directory: str,
    filename: str,
    artifacts: Sequence[RunArtifact],
    source_hashes: dict[str, str | None],
) -> None:
    for artifact in sorted(artifacts, key=lambda item: item.run_id):
        _published_artifact(
            root,
            f"{directory}/{artifact.run_id}/{filename}",
            artifact,
            source_hashes,
        )


def _assert_publication_safe(root: Path) -> None:
    for entry in build_inventory(root):
        path = root / entry.path
        text = read_text_limited(path, max(ARTIFACT_FILE_LIMIT, CORPUS_FILE_LIMIT))
        if _sanitize_public_string(text) != text:
            raise ValidationError("BoardBench publication safety scan failed")


def create_publication_bundle(
    output: str | Path,
    campaign_root: str | Path,
    report: BoardBenchReport,
    corpus: BoardBenchCorpus,
) -> Path:
    """Create a metadata-only, redacted bundle from an exact sealed report.

    Raw KiCad projects, traces, installed libraries, and third-party source data
    are intentionally not copied.  Their immutable run inventories remain the
    hash index for a separately authorized large-artifact archive.
    """

    evidence = load_campaign_evidence(campaign_root, corpus)
    expected = _aggregate_evidence(
        evidence,
        sealed=True,
        generated_at=report.generated_at,
    )
    if report != expected:
        raise ValidationError("BoardBench publication report differs from sources")
    campaign_path = Path(campaign_root).expanduser().resolve(strict=True)
    candidate = Path(output).expanduser().absolute().resolve(strict=False)
    raw_runs = (campaign_path / "runs").resolve(strict=True)
    if candidate == raw_runs or raw_runs in candidate.parents:
        raise ValidationError("BoardBench publication output cannot mutate raw runs")
    target, staging = _staging_directory(output, "publication")
    published = False
    try:
        source_hashes: dict[str, str | None] = {}
        _published_artifact(staging, "corpus.json", corpus, source_hashes)
        _published_artifact(staging, "campaign.json", evidence.campaign, source_hashes)
        _published_artifact(staging, REPORT_JSON_NAME, report, source_hashes)
        if evidence.selection is None:  # Sealing validated this invariant.
            raise ValidationError("BoardBench publication selection is unavailable")
        _published_artifact(
            staging, "selection.json", evidence.selection, source_hashes
        )
        _published_run_artifacts(
            staging, "runs", "run.json", evidence.runs, source_hashes
        )
        _published_run_artifacts(
            staging, "scores", "score.json", evidence.scores, source_hashes
        )
        _published_run_artifacts(
            staging, "reviews", "review.json", evidence.reviews, source_hashes
        )
        _published_run_artifacts(
            staging,
            "corrections",
            "correction.json",
            evidence.corrections,
            source_hashes,
        )
        _published_run_artifacts(
            staging, "hardware", "hardware.json", evidence.hardware, source_hashes
        )
        markdown = _sanitize_public_string(render_markdown(report))
        atomic_write_text(staging / REPORT_MARKDOWN_NAME, markdown, mode=0o600)
        source_hashes[REPORT_MARKDOWN_NAME] = None
        entries = [
            {
                "path": entry.path,
                "published_sha256": entry.sha256,
                "source_sha256": source_hashes[entry.path],
            }
            for entry in build_inventory(staging)
        ]
        atomic_write_json(
            staging / PUBLICATION_MANIFEST_NAME,
            {
                "schema": "pcbdraft-boardbench-publication-manifest",
                "version": 1,
                "campaign_id": evidence.campaign.campaign_id,
                "cohort": evidence.campaign.cohort,
                "metadata_only": True,
                "files": sorted(entries, key=lambda item: str(item["path"])),
            },
            mode=0o600,
        )
        _assert_publication_safe(staging)
        result = _publish_directory(staging, target, "publication")
        published = True
        return result
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


__all__ = (
    "CampaignEvidence",
    "aggregate_report",
    "create_publication_bundle",
    "load_campaign_evidence",
    "render_markdown",
    "seal_campaign",
    "write_report",
)
