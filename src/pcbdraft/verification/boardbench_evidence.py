"""Import human and physical BoardBench evidence without mutating raw runs.

This module only validates, copies, and links externally supplied evidence.  It
never invents an engineering judgment or physical result.  Raw run repositories
are immutable; optional L6/L7 linkage is therefore allowed only on a derived
managed-project copy outside ``runs/``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_bytes, make_directory, read_bytes_limited
from pcbdraft.verification.boardbench import (
    MAX_INVENTORY_FILE_BYTES,
    BoardBenchCampaign,
    BoardBenchCorpus,
    BoardBenchHardware,
    BoardBenchReview,
    BoardBenchRun,
    BoardBenchScore,
    artifact_sha256,
    build_inventory,
    case_sha256,
    load_campaign,
    load_hardware,
    load_review,
    load_run,
    load_score,
    load_selection,
    review_checklist_for_case,
    validate_review_sources,
    write_artifact,
)
from pcbdraft.verification.boardbench_diff import verify_correction_bundle
from pcbdraft.verification.evidence import record_external_evidence

CAMPAIGN_NAME = "campaign.json"
RUNS_DIRECTORY = "runs"
REVIEWS_DIRECTORY = "reviews"
REVIEW_TEMPLATES_DIRECTORY = "review-templates"
CORRECTIONS_DIRECTORY = "corrections"
HARDWARE_DIRECTORY = "hardware"
SELECTION_NAME = "selection.json"
SCORES_DIRECTORY = "scores"


@dataclass(frozen=True)
class HardwareImport:
    """Paths and typed record produced by one physical-evidence import."""

    root: Path
    record_path: Path
    hardware: BoardBenchHardware


def _reject_symlink_components(path: Path, label: str) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValidationError(f"BoardBench {label} path contains a symbolic link")


def _campaign_root(value: str | Path) -> tuple[Path, BoardBenchCampaign]:
    raw = Path(value).expanduser()
    _reject_symlink_components(raw, "campaign")
    if not raw.is_dir():
        raise ValidationError("BoardBench campaign directory is unavailable")
    root = raw.resolve()
    return root, load_campaign(root / CAMPAIGN_NAME)


def _campaign_run(
    root: Path, campaign: BoardBenchCampaign, run_id: str
) -> BoardBenchRun:
    plan = next((item for item in campaign.runs if item.run_id == run_id), None)
    if plan is None:
        raise ValidationError("BoardBench evidence run is outside the campaign plan")
    run = load_run(root / RUNS_DIRECTORY / run_id / "run.json")
    if (
        run.campaign_id != campaign.campaign_id
        or run.run_id != run_id
        or run.case_id != plan.case_id
        or run.repetition != plan.repetition
    ):
        raise ValidationError("BoardBench evidence run does not match the campaign")
    if not run.terminal:
        raise ValidationError("BoardBench evidence requires a terminal source run")
    artifacts = root / RUNS_DIRECTORY / run_id / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise ValidationError("BoardBench evidence run artifacts are unavailable")
    if build_inventory(artifacts) != run.inventory:
        raise ValidationError("BoardBench evidence run artifact inventory changed")
    return run


def _validate_corpus(campaign: BoardBenchCampaign, corpus: BoardBenchCorpus) -> None:
    if (
        corpus.corpus_id != campaign.corpus_id
        or corpus.cohort != campaign.cohort
        or artifact_sha256(corpus) != campaign.corpus_sha256
    ):
        raise ValidationError("BoardBench corpus does not match the campaign")


def _campaign_score(root: Path, run_id: str) -> BoardBenchScore | None:
    path = root / SCORES_DIRECTORY / run_id / "score.json"
    if not path.exists() and not path.is_symlink():
        return None
    return load_score(path)


def create_review_templates(
    campaign_root: str | Path, corpus: BoardBenchCorpus
) -> tuple[Path, ...]:
    """Create one source-bound, explicitly unreviewed template per terminal run."""

    root, campaign = _campaign_root(campaign_root)
    _validate_corpus(campaign, corpus)
    cases = {case.id: case for case in corpus.cases}
    target = root / REVIEW_TEMPLATES_DIRECTORY
    _reject_symlink_components(target, "review templates")
    if target.exists() or target.is_symlink():
        raise ValidationError("BoardBench review template directory already exists")
    staging = Path(tempfile.mkdtemp(prefix=".review-templates-", dir=root))
    published = False
    try:
        for plan in campaign.runs:
            run = _campaign_run(root, campaign, plan.run_id)
            case = cases[run.case_id]
            score = _campaign_score(root, run.run_id)
            template = BoardBenchReview(
                campaign_id=campaign.campaign_id,
                run_id=run.run_id,
                source_campaign_sha256=artifact_sha256(campaign),
                source_corpus_sha256=artifact_sha256(corpus),
                source_case_sha256=case_sha256(case),
                source_run_sha256=artifact_sha256(run),
                source_score_sha256=(
                    artifact_sha256(score) if score is not None else None
                ),
                reviewer=None,
                reviewed_at=None,
                outcome="not_reviewed",
                functional_correctness="unknown",
                orderable_state="unknown",
                orderability_evidence=(),
                active_engineer_minutes=None,
                not_applicable_reason=None,
                checklist=review_checklist_for_case(case),
                findings=(),
                modifications=(),
                final_failure=None,
            )
            validate_review_sources(
                template,
                campaign=campaign,
                corpus=corpus,
                case=case,
                run=run,
                score=score,
            )
            write_artifact(staging / run.run_id / "review.json", template)
        try:
            os.rename(staging, target)
        except OSError as exc:
            raise PCBDraftError("cannot publish BoardBench review templates") from exc
        published = True
        return tuple(target / plan.run_id / "review.json" for plan in campaign.runs)
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


def import_review(
    campaign_root: str | Path,
    corpus: BoardBenchCorpus,
    submission: str | Path,
) -> Path:
    """Validate and store one completed external engineering review."""

    root, campaign = _campaign_root(campaign_root)
    _validate_corpus(campaign, corpus)
    review = load_review(submission)
    if review.campaign_id != campaign.campaign_id:
        raise ValidationError("BoardBench review campaign id does not match")
    if review.outcome == "not_reviewed":
        raise ValidationError("BoardBench review import requires completed evidence")
    run = _campaign_run(root, campaign, review.run_id)
    cases = {case.id: case for case in corpus.cases}
    validate_review_sources(
        review,
        campaign=campaign,
        corpus=corpus,
        case=cases[run.case_id],
        run=run,
        score=_campaign_score(root, run.run_id),
    )
    return write_artifact(
        root / REVIEWS_DIRECTORY / review.run_id / "review.json", review
    )


def import_selection(
    campaign_root: str | Path,
    corpus: BoardBenchCorpus,
    submission: str | Path,
) -> Path:
    """Validate the five-category manufacturing selection and its source hashes."""

    root, campaign = _campaign_root(campaign_root)
    _validate_corpus(campaign, corpus)
    selection = load_selection(submission)
    if selection.campaign_id != campaign.campaign_id:
        raise ValidationError("BoardBench selection campaign id does not match")
    cases_by_id = {case.id: case for case in corpus.cases}
    category_by_case = {case_id: case.category for case_id, case in cases_by_id.items()}
    plan_by_run = {plan.run_id: plan for plan in campaign.runs}
    for entry in selection.selections:
        plan = plan_by_run.get(entry.run_id)
        if plan is None or category_by_case.get(plan.case_id) != entry.category:
            raise ValidationError("BoardBench selection run/category does not match")
        run = _campaign_run(root, campaign, entry.run_id)
        review = load_review(root / REVIEWS_DIRECTORY / entry.run_id / "review.json")
        correction_root = root / CORRECTIONS_DIRECTORY / entry.run_id
        validate_review_sources(
            review,
            campaign=campaign,
            corpus=corpus,
            case=cases_by_id[run.case_id],
            run=run,
            score=_campaign_score(root, run.run_id),
        )
        if correction_root.is_dir() and not correction_root.is_symlink():
            correction = verify_correction_bundle(
                correction_root,
                run_receipt_path=root / RUNS_DIRECTORY / entry.run_id / "run.json",
                review_path=root / REVIEWS_DIRECTORY / entry.run_id / "review.json",
            )
        elif (
            not correction_root.exists()
            and review.outcome == "pass_without_schematic_change"
            and review.orderable_state == "pass"
            and entry.source_artifact_sha256 == artifact_sha256(run)
        ):
            continue
        else:
            raise ValidationError(
                "BoardBench selection lacks a verified source candidate"
            )
        if (
            correction.campaign_id != campaign.campaign_id
            or correction.run_id != entry.run_id
            or correction.source_run_sha256 != artifact_sha256(run)
            or correction.source_review_sha256 != artifact_sha256(review)
            or correction.manufacturing_candidate_sha256 is None
            or correction.manufacturing_candidate_sha256 != entry.source_artifact_sha256
        ):
            raise ValidationError(
                "BoardBench selection is not bound to a manufacturing candidate"
            )
    return write_artifact(root / SELECTION_NAME, selection)


def _copy_inventory(source: Path, target: Path) -> None:
    expected = build_inventory(source)
    make_directory(target)
    for entry in expected:
        source_path = source / entry.path
        target_path = target / entry.path
        make_directory(target_path.parent)
        data = read_bytes_limited(source_path, MAX_INVENTORY_FILE_BYTES)
        atomic_write_bytes(target_path, data, mode=0o600)
    if build_inventory(target) != expected:
        raise ValidationError("BoardBench copied attachment inventory changed")


def _file_sha256(path: Path) -> str:
    _reject_symlink_components(path, "source artifact")
    if not path.is_file():
        raise ValidationError("BoardBench source artifact is unavailable")
    return hashlib.sha256(
        read_bytes_limited(path, MAX_INVENTORY_FILE_BYTES)
    ).hexdigest()


def import_hardware(
    campaign_root: str | Path,
    submission_root: str | Path,
    *,
    release_artifact: str | Path | None = None,
) -> HardwareImport:
    """Copy one attributed hardware record and its non-empty attachments."""

    root, campaign = _campaign_root(campaign_root)
    raw_submission = Path(submission_root).expanduser()
    _reject_symlink_components(raw_submission, "hardware submission")
    if not raw_submission.is_dir():
        raise ValidationError("BoardBench hardware submission is unavailable")
    source_root = raw_submission.resolve()
    hardware = load_hardware(source_root / "hardware.json")
    if hardware.campaign_id != campaign.campaign_id:
        raise ValidationError("BoardBench hardware campaign id does not match")
    run = _campaign_run(root, campaign, hardware.run_id)
    selection = load_selection(root / SELECTION_NAME)
    if selection.campaign_id != campaign.campaign_id:
        raise ValidationError("BoardBench hardware selection campaign does not match")
    selected = next(
        (item for item in selection.selections if item.run_id == hardware.run_id), None
    )
    if selected is None or selected.category != hardware.category:
        raise ValidationError("BoardBench hardware run/category was not selected")

    if hardware.source_kind == "correction":
        correction = verify_correction_bundle(
            root / CORRECTIONS_DIRECTORY / hardware.run_id,
            run_receipt_path=root / RUNS_DIRECTORY / hardware.run_id / "run.json",
            review_path=root / REVIEWS_DIRECTORY / hardware.run_id / "review.json",
        )
        if hardware.source_artifact_sha256 != artifact_sha256(correction):
            raise ValidationError("BoardBench hardware correction hash does not match")
        if selected.source_artifact_sha256 != correction.manufacturing_candidate_sha256:
            raise ValidationError(
                "BoardBench selected manufacturing candidate hash does not match"
            )
    else:
        if release_artifact is None:
            raise ValidationError(
                "BoardBench release-backed hardware needs its artifact"
            )
        release_path = Path(release_artifact).expanduser()
        if _file_sha256(release_path) != hardware.source_artifact_sha256:
            raise ValidationError("BoardBench hardware release hash does not match")
        if selected.source_artifact_sha256 not in {
            hardware.source_artifact_sha256,
            artifact_sha256(run),
        }:
            raise ValidationError("BoardBench selected release hash does not match")

    attachments = source_root / "attachments"
    if build_inventory(attachments) != hardware.attachments:
        raise ValidationError("BoardBench hardware attachment inventory does not match")
    parent = make_directory(root / HARDWARE_DIRECTORY)
    target = parent / hardware.run_id
    _reject_symlink_components(target, "hardware target")
    if target.exists() or target.is_symlink():
        raise ValidationError("BoardBench hardware evidence already exists")
    staging = Path(tempfile.mkdtemp(prefix=f".{hardware.run_id}-", dir=parent))
    try:
        _copy_inventory(attachments, staging / "attachments")
        if hardware.source_kind == "release":
            if release_artifact is None:  # Defensive after source validation above.
                raise ValidationError(
                    "BoardBench release-backed hardware needs its artifact"
                )
            release_bytes = read_bytes_limited(
                Path(release_artifact).expanduser(), MAX_INVENTORY_FILE_BYTES
            )
            atomic_write_bytes(
                staging / "source" / "release-artifact", release_bytes, mode=0o600
            )
            if (
                hashlib.sha256(release_bytes).hexdigest()
                != hardware.source_artifact_sha256
            ):
                raise ValidationError(
                    "BoardBench hardware release changed while importing"
                )
        write_artifact(staging / "hardware.json", hardware)
        try:
            os.rename(staging, target)
        except OSError as exc:
            raise PCBDraftError("cannot publish BoardBench hardware evidence") from exc
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return HardwareImport(
        root=target, record_path=target / "hardware.json", hardware=hardware
    )


def _derived_project(
    campaign_root: str | Path, project_value: str | Path
) -> tuple[Path, Path]:
    root, _campaign = _campaign_root(campaign_root)
    raw_project = Path(project_value).expanduser()
    _reject_symlink_components(raw_project, "derived project")
    project = raw_project.resolve()
    immutable_runs = (root / RUNS_DIRECTORY).resolve()
    if project == immutable_runs or project.is_relative_to(immutable_runs):
        raise ValidationError("BoardBench cannot mutate an immutable raw run project")
    return root, project


def link_review_external_evidence(
    campaign_root: str | Path,
    project_value: str | Path,
    review_path: str | Path,
    *,
    reviewer_qualification: str,
) -> Path:
    """Link a validated review as L6 evidence on a derived managed project."""

    root, project = _derived_project(campaign_root, project_value)
    submitted = Path(review_path).expanduser()
    review = load_review(submitted)
    expected = root / REVIEWS_DIRECTORY / review.run_id / "review.json"
    if submitted.resolve(strict=True) != expected.resolve(strict=True):
        raise ValidationError("BoardBench L6 review is not canonical imported evidence")
    _root, campaign = _campaign_root(root)
    run = _campaign_run(root, campaign, review.run_id)
    if review.source_run_sha256 != artifact_sha256(run):
        raise ValidationError("BoardBench L6 review source hash differs")
    if (
        review.outcome == "not_reviewed"
        or review.reviewer is None
        or review.reviewed_at is None
    ):
        raise ValidationError("BoardBench L6 linkage requires a completed review")
    outcome = (
        "pass"
        if review.functional_correctness == "pass" and review.orderable_state == "pass"
        else "fail"
    )
    return record_external_evidence(
        project,
        level="L6",
        outcome=outcome,
        actor=review.reviewer,
        role="PCB engineer",
        performed_at=review.reviewed_at,
        statement=f"BoardBench engineering review outcome: {review.outcome}.",
        artifacts=[review_path],
        metadata={
            "review_scope": "BoardBench schematic function and orderability",
            "reviewer_qualification": reviewer_qualification,
            "source_run_sha256": review.source_run_sha256,
        },
    )


def link_hardware_external_evidence(
    campaign_root: str | Path,
    project_value: str | Path,
    hardware_root: str | Path,
    *,
    test_plan: str,
) -> Path:
    """Link a validated physical record as L7 evidence on a derived project."""

    root, project = _derived_project(campaign_root, project_value)
    submitted = Path(hardware_root).expanduser()
    _reject_symlink_components(submitted, "hardware evidence")
    evidence_root = submitted.resolve(strict=True)
    hardware_path = evidence_root / "hardware.json"
    hardware = load_hardware(hardware_path)
    expected = root / HARDWARE_DIRECTORY / hardware.run_id
    if evidence_root != expected.resolve(strict=True):
        raise ValidationError(
            "BoardBench L7 hardware is not canonical imported evidence"
        )
    _root, campaign = _campaign_root(root)
    _campaign_run(root, campaign, hardware.run_id)
    selection = load_selection(root / SELECTION_NAME)
    if selection.campaign_id != campaign.campaign_id or not any(
        item.run_id == hardware.run_id and item.category == hardware.category
        for item in selection.selections
    ):
        raise ValidationError("BoardBench L7 hardware selection source differs")
    attachments = evidence_root / "attachments"
    if build_inventory(attachments) != hardware.attachments:
        raise ValidationError("BoardBench L7 attachment inventory does not match")
    source_artifact: Path | None = None
    if hardware.source_kind == "release":
        source = evidence_root / "source" / "release-artifact"
        if _file_sha256(source) != hardware.source_artifact_sha256:
            raise ValidationError("BoardBench L7 release source hash differs")
        source_artifact = source
    else:
        correction = verify_correction_bundle(
            root / CORRECTIONS_DIRECTORY / hardware.run_id,
            run_receipt_path=root / RUNS_DIRECTORY / hardware.run_id / "run.json",
            review_path=root / REVIEWS_DIRECTORY / hardware.run_id / "review.json",
        )
        if artifact_sha256(correction) != hardware.source_artifact_sha256:
            raise ValidationError("BoardBench L7 correction source hash differs")
    artifact_paths = [
        hardware_path,
        *(attachments / item.path for item in hardware.attachments),
    ]
    if source_artifact is not None:
        artifact_paths.append(source_artifact)
    required = (
        hardware.fabricator_accepted,
        hardware.solderability,
        hardware.first_power_no_short,
        hardware.core_function,
    )
    outcome = "pass" if all(value == "pass" for value in required) else "fail"
    return record_external_evidence(
        project,
        level="L7",
        outcome=outcome,
        actor=hardware.operator,
        role="BoardBench hardware operator",
        performed_at=hardware.observed_at,
        statement=f"BoardBench physical result for {hardware.board_serial}.",
        artifacts=[str(path) for path in artifact_paths],
        metadata={
            "board_serial": hardware.board_serial,
            "test_plan": test_plan,
            "result_summary": f"core_function={hardware.core_function}",
            "source_artifact_sha256": hardware.source_artifact_sha256,
        },
    )
