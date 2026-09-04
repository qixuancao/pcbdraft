#!/usr/bin/env python3
"""Operate an explicit, source-bound BoardBench campaign.

This is deliberately a thin interface over ``pcbdraft.verification``.  It
does not discover a corpus, reinterpret evidence, or implement scoring,
review, sealing, or publication rules of its own.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import portable_record_path
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.verification.boardbench import BoardBenchCampaign
from pcbdraft.verification.boardbench import BoardBenchCorpus
from pcbdraft.verification.boardbench import load_campaign
from pcbdraft.verification.boardbench import load_corpus
from pcbdraft.verification.boardbench import load_report
from pcbdraft.verification.boardbench import load_run
from pcbdraft.verification.boardbench import load_score
from pcbdraft.verification.boardbench_diff import capture_correction
from pcbdraft.verification.boardbench_evaluator import EVALUATOR_VERSION
from pcbdraft.verification.boardbench_evaluator import evaluate_run
from pcbdraft.verification.boardbench_evidence import create_review_templates
from pcbdraft.verification.boardbench_evidence import import_hardware
from pcbdraft.verification.boardbench_evidence import import_review
from pcbdraft.verification.boardbench_evidence import import_selection
from pcbdraft.verification.boardbench_preflight import formal_comparison_launch_record
from pcbdraft.verification.boardbench_preflight import load_comparison_manifest
from pcbdraft.verification.boardbench_preflight import write_formal_comparison_launch
from pcbdraft.verification.boardbench_report import aggregate_report
from pcbdraft.verification.boardbench_report import create_publication_bundle
from pcbdraft.verification.boardbench_report import load_campaign_evidence
from pcbdraft.verification.boardbench_report import seal_campaign
from pcbdraft.verification.boardbench_report import write_report
from pcbdraft.verification.boardbench_runner import DEFAULT_WALL_TIMEOUT_SECONDS
from pcbdraft.verification.boardbench_runner import create_campaign
from pcbdraft.verification.boardbench_runner import initialize_run_receipts
from pcbdraft.verification.boardbench_runner import run_campaign
from pcbdraft.verification.boardbench_v2 import load_run_v2

Handler = Callable[[argparse.Namespace], int]


def _path_argument(parser: argparse.ArgumentParser, name: str, help_text: str) -> None:
    parser.add_argument(name, required=True, type=Path, help=help_text)


def _print_path(label: str, path: Path) -> None:
    print(f"{label}: {portable_record_path(path)}")


def _planned_run_id(campaign: BoardBenchCampaign, requested: str) -> str:
    plan = next((item for item in campaign.runs if item.run_id == requested), None)
    if plan is None:
        raise ValidationError("BoardBench run id is not in the campaign plan")
    return plan.run_id


def _command_corpus_validate(args: argparse.Namespace) -> int:
    load_corpus(args.corpus)
    print("BoardBench corpus is valid.")
    return 0


def _command_campaign_create(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    root = create_campaign(
        args.output,
        corpus,
        campaign_id=args.campaign_id,
        evaluator_version=EVALUATOR_VERSION,
        wall_timeout_seconds=args.wall_timeout_seconds,
    )
    _print_path("BoardBench campaign created", root)
    return 0


def _command_campaign_run(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    if args.run_ids:
        selected = tuple(args.run_ids)
        receipts = run_campaign(args.campaign, corpus, run_ids=selected)
        print(
            f"BoardBench campaign has {len(receipts)} run receipts; "
            f"{len(selected)} run ids were selected."
        )
    else:
        receipts = run_campaign(args.campaign, corpus)
        print(f"BoardBench campaign has {len(receipts)} run receipts.")
    return 0


def _command_campaign_initialize(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    campaign_root = args.campaign.expanduser()
    campaign = load_campaign(campaign_root / "campaign.json")
    receipts = initialize_run_receipts(campaign_root, campaign, corpus)
    print(f"BoardBench initialized {len(receipts)} planned run receipts.")
    return 0


def _command_campaign_authorize_comparison(args: argparse.Namespace) -> int:
    campaign_root = args.campaign.expanduser()
    campaign = load_campaign(campaign_root / "campaign.json")
    manifest = load_comparison_manifest(args.manifest)
    if len(campaign.runs) != 60:
        raise ValidationError("formal comparison requires 60 initialized runs")
    for plan in campaign.runs:
        run = load_run_v2(campaign_root / "runs" / plan.run_id / "run.json")
        if (
            run.run_state != "planned"
            or run.campaign_id != campaign.campaign_id
            or run.run_id != plan.run_id
            or run.case_id != plan.case_id
            or run.repetition != plan.repetition
        ):
            raise ValidationError(
                "formal comparison authorization requires untouched planned receipts"
            )
    record = formal_comparison_launch_record(
        manifest,
        campaign,
        authorized_at=utc_timestamp(),
    )
    path = write_formal_comparison_launch(args.output, record)
    _print_path("BoardBench formal comparison authorized", path)
    return 0


def _score_runs(
    corpus: BoardBenchCorpus,
    campaign: BoardBenchCampaign,
    campaign_root: Path,
    run_ids: Sequence[str],
    *,
    allow_nonterminal: bool,
) -> tuple[int, int, int]:
    runs = []
    existing_scores = []
    pending = []
    nonterminal = 0
    for run_id in run_ids:
        run_root = campaign_root / "runs" / run_id
        run = load_run(run_root / "run.json")
        runs.append(run)
        score_path = campaign_root / "scores" / run_id / "score.json"
        if score_path.exists() or score_path.is_symlink():
            existing_scores.append(load_score(score_path))
        elif run.terminal:
            pending.append((run, run_root, score_path.parent))
        elif allow_nonterminal:
            nonterminal += 1
        else:
            raise ValidationError("BoardBench scoring requires a terminal run")

    # Reuse the report owner's source-chain validation before treating any
    # existing score as resumable evidence. Nothing is written by aggregation.
    aggregate_report(
        corpus,
        campaign,
        runs=tuple(runs),
        scores=tuple(existing_scores),
    )
    for run, run_root, output in pending:
        evaluate_run(corpus, campaign, run, run_root, output)
    return len(pending), len(existing_scores), nonterminal


def _command_score(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    campaign_root = args.campaign.expanduser()
    campaign = load_campaign(campaign_root / "campaign.json")
    if args.score_all:
        run_ids = tuple(plan.run_id for plan in campaign.runs)
    else:
        run_ids = (_planned_run_id(campaign, args.run_id),)
    scored, verified, nonterminal = _score_runs(
        corpus,
        campaign,
        campaign_root,
        run_ids,
        allow_nonterminal=args.score_all,
    )
    if args.score_all:
        print(
            "BoardBench scoring complete: "
            f"{scored} scored, {verified} existing verified, "
            f"{nonterminal} nonterminal skipped."
        )
    elif scored:
        _print_path(
            "BoardBench score stored",
            campaign_root / "scores" / run_ids[0] / "score.json",
        )
    else:
        print("BoardBench existing score verified; no artifact was overwritten.")
    return 0


def _command_review_templates(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    paths = create_review_templates(args.campaign, corpus)
    print(f"BoardBench created {len(paths)} review templates.")
    return 0


def _command_review_import(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    path = import_review(args.campaign, corpus, args.submission)
    _print_path("BoardBench review stored", path)
    return 0


def _command_correction_capture(args: argparse.Namespace) -> int:
    campaign_root = args.campaign.expanduser()
    campaign = load_campaign(campaign_root / "campaign.json")
    run_id = _planned_run_id(campaign, args.run_id)
    capture = capture_correction(
        campaign_root,
        run_receipt_path=campaign_root / "runs" / run_id / "run.json",
        review_path=campaign_root / "reviews" / run_id / "review.json",
        generated_snapshot=args.generated,
        corrected_snapshot=args.corrected,
        manufacturing_candidate_snapshot=args.manufacturing_candidate,
    )
    _print_path("BoardBench correction stored", capture.record_path)
    return 0


def _command_selection_import(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    path = import_selection(args.campaign, corpus, args.submission)
    _print_path("BoardBench selection stored", path)
    return 0


def _command_hardware_import(args: argparse.Namespace) -> int:
    imported = import_hardware(
        args.campaign,
        args.submission,
        release_artifact=args.release_artifact,
    )
    _print_path("BoardBench hardware evidence stored", imported.record_path)
    return 0


def _command_report(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    evidence = load_campaign_evidence(args.campaign, corpus)
    report = aggregate_report(
        corpus,
        evidence.campaign,
        runs=evidence.runs,
        scores=evidence.scores,
        reviews=evidence.reviews,
        corrections=evidence.corrections,
        hardware=evidence.hardware,
        selection=evidence.selection,
    )
    path = write_report(args.output, report)
    _print_path("BoardBench draft report stored", path)
    return 0


def _command_seal(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    seal_campaign(args.output, args.campaign, corpus)
    _print_path("BoardBench sealed report stored", args.output)
    return 0


def _command_publication_bundle(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    report = load_report(args.report)
    path = create_publication_bundle(
        args.output,
        args.campaign,
        report,
        corpus,
    )
    _print_path("BoardBench publication bundle stored", path)
    return 0


def _set_handler(parser: argparse.ArgumentParser, handler: Handler) -> None:
    parser.set_defaults(handler=handler)


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit-path BoardBench operator parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Operate a source-bound BoardBench campaign without exposing its "
            "reference corpus to the tested Agent."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    corpus = commands.add_parser("corpus", help="validate an explicit corpus")
    corpus_actions = corpus.add_subparsers(dest="corpus_action", required=True)
    corpus_validate = corpus_actions.add_parser("validate")
    _path_argument(corpus_validate, "--corpus", "private or public corpus JSON")
    _set_handler(corpus_validate, _command_corpus_validate)

    campaign = commands.add_parser("campaign", help="create or execute a campaign")
    campaign_actions = campaign.add_subparsers(dest="campaign_action", required=True)
    campaign_create = campaign_actions.add_parser("create")
    _path_argument(campaign_create, "--corpus", "private or public corpus JSON")
    _path_argument(
        campaign_create,
        "--output",
        "fresh parent directory that will receive <campaign-id>/",
    )
    campaign_create.add_argument("--campaign-id", required=True)
    campaign_create.add_argument(
        "--wall-timeout-seconds",
        type=float,
        default=DEFAULT_WALL_TIMEOUT_SECONDS,
        help=(
            "hard per-run fail-safe in seconds; valid range 1..86400 "
            f"(default: {DEFAULT_WALL_TIMEOUT_SECONDS:g})"
        ),
    )
    _set_handler(campaign_create, _command_campaign_create)
    campaign_initialize = campaign_actions.add_parser("initialize")
    _path_argument(campaign_initialize, "--campaign", "campaign directory")
    _path_argument(
        campaign_initialize,
        "--corpus",
        "the campaign's exact frozen corpus JSON",
    )
    _set_handler(campaign_initialize, _command_campaign_initialize)
    campaign_authorize = campaign_actions.add_parser("authorize-comparison")
    _path_argument(campaign_authorize, "--campaign", "fresh base campaign directory")
    _path_argument(
        campaign_authorize,
        "--manifest",
        "frozen evaluator-v5 comparison preflight manifest",
    )
    _path_argument(
        campaign_authorize,
        "--output",
        "fresh local root for the immutable authorization record",
    )
    _set_handler(campaign_authorize, _command_campaign_authorize_comparison)
    for action in ("run", "resume"):
        campaign_execute = campaign_actions.add_parser(action)
        _path_argument(campaign_execute, "--campaign", "campaign directory")
        _path_argument(
            campaign_execute, "--corpus", "the campaign's exact frozen corpus JSON"
        )
        campaign_execute.add_argument(
            "--run-id",
            action="append",
            dest="run_ids",
            help=(
                "execute only this planned run id; repeat for a bounded subset "
                "without changing the 60-run manifest"
            ),
        )
        _set_handler(campaign_execute, _command_campaign_run)

    score = commands.add_parser(
        "score", help="score one terminal run or all missing terminal runs"
    )
    _path_argument(score, "--campaign", "campaign directory")
    _path_argument(score, "--corpus", "the campaign's exact frozen corpus JSON")
    score_target = score.add_mutually_exclusive_group(required=True)
    score_target.add_argument("--run-id")
    score_target.add_argument(
        "--all",
        action="store_true",
        dest="score_all",
        help="score missing terminal runs in frozen campaign order",
    )
    _set_handler(score, _command_score)

    review = commands.add_parser("review", help="manage engineering review records")
    review_actions = review.add_subparsers(dest="review_action", required=True)
    review_templates = review_actions.add_parser("templates")
    _path_argument(review_templates, "--campaign", "campaign directory")
    _path_argument(
        review_templates, "--corpus", "the campaign's exact frozen corpus JSON"
    )
    _set_handler(review_templates, _command_review_templates)
    review_import = review_actions.add_parser("import")
    _path_argument(review_import, "--campaign", "campaign directory")
    _path_argument(review_import, "--corpus", "the campaign's exact frozen corpus JSON")
    _path_argument(review_import, "--submission", "completed review JSON")
    _set_handler(review_import, _command_review_import)

    correction = commands.add_parser("correction", help="capture immutable corrections")
    correction_actions = correction.add_subparsers(
        dest="correction_action", required=True
    )
    correction_capture = correction_actions.add_parser("capture")
    _path_argument(correction_capture, "--campaign", "campaign directory")
    correction_capture.add_argument("--run-id", required=True)
    _path_argument(
        correction_capture,
        "--generated",
        "generated snapshot retained inside the raw run artifacts",
    )
    _path_argument(
        correction_capture, "--corrected", "engineer-corrected project snapshot"
    )
    correction_capture.add_argument(
        "--manufacturing-candidate",
        type=Path,
        help="optional separately retained manufacturing-candidate snapshot",
    )
    _set_handler(correction_capture, _command_correction_capture)

    selection = commands.add_parser("selection", help="import physical selections")
    selection_actions = selection.add_subparsers(dest="selection_action", required=True)
    selection_import = selection_actions.add_parser("import")
    _path_argument(selection_import, "--campaign", "campaign directory")
    _path_argument(
        selection_import, "--corpus", "the campaign's exact frozen corpus JSON"
    )
    _path_argument(selection_import, "--submission", "selection JSON")
    _set_handler(selection_import, _command_selection_import)

    hardware = commands.add_parser("hardware", help="import physical evidence")
    hardware_actions = hardware.add_subparsers(dest="hardware_action", required=True)
    hardware_import = hardware_actions.add_parser("import")
    _path_argument(hardware_import, "--campaign", "campaign directory")
    _path_argument(
        hardware_import,
        "--submission",
        "directory containing hardware.json and attachments/",
    )
    hardware_import.add_argument(
        "--release-artifact",
        type=Path,
        help="required source artifact for release-backed hardware evidence",
    )
    _set_handler(hardware_import, _command_hardware_import)

    report = commands.add_parser("report", help="write a draft aggregate report")
    _path_argument(report, "--campaign", "campaign directory")
    _path_argument(report, "--corpus", "the campaign's exact frozen corpus JSON")
    _path_argument(report, "--output", "fresh report output directory")
    _set_handler(report, _command_report)

    seal = commands.add_parser("seal", help="fail closed and seal a complete campaign")
    _path_argument(seal, "--campaign", "campaign directory")
    _path_argument(seal, "--corpus", "the campaign's exact frozen corpus JSON")
    _path_argument(seal, "--output", "fresh sealed-report output directory")
    _set_handler(seal, _command_seal)

    publication = commands.add_parser(
        "publication", help="create a sanitized metadata publication bundle"
    )
    publication_actions = publication.add_subparsers(
        dest="publication_action", required=True
    )
    publication_bundle = publication_actions.add_parser("bundle")
    _path_argument(publication_bundle, "--campaign", "campaign directory")
    _path_argument(
        publication_bundle, "--corpus", "the campaign's exact frozen corpus JSON"
    )
    _path_argument(publication_bundle, "--report", "sealed report.json")
    _path_argument(publication_bundle, "--output", "fresh publication output directory")
    _set_handler(publication_bundle, _command_publication_bundle)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler = args.handler
    try:
        return handler(args)
    except PCBDraftError as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
