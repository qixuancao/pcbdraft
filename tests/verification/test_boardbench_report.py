from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from unittest import mock

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_bytes, atomic_write_json, atomic_write_text
from pcbdraft.verification.boardbench import (
    AI_REVIEWED_PILOT_COHORT,
    AUTOMATIC_METRICS,
    BOARD_BENCH_LOCK_DIR,
    BOARD_CATEGORIES,
    BoardBenchCampaign,
    BoardBenchCase,
    BoardBenchCorpus,
    BoardBenchCorrection,
    BoardBenchHardware,
    BoardBenchReview,
    BoardBenchRun,
    BoardBenchScore,
    BoardBenchSelection,
    CampaignRunPlan,
    EfficiencyMetrics,
    FailureClassification,
    InventoryEntry,
    MetricResult,
    ModificationDecision,
    OrderabilityEvidence,
    RailMeasurement,
    SelectionEntry,
    StructuralDiffEntry,
    ToolCallCount,
    artifact_sha256,
    build_inventory,
    canonical_json_bytes,
    case_sha256,
    load_hardware,
    load_report,
    load_review,
    load_run,
    load_score,
    review_checklist_for_case,
    write_artifact,
)
from pcbdraft.verification.boardbench_report import (
    aggregate_report,
    create_publication_bundle,
    load_campaign_evidence,
    render_markdown,
    seal_campaign,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
NOW = "2026-08-21T08:00:00Z"
LATER = "2026-08-21T08:01:00Z"
EVALUATOR_VERSION = "boardbench-evaluator-v1"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _case(index: int, category: str) -> BoardBenchCase:
    metrics = tuple(
        name for name in AUTOMATIC_METRICS if not (index == 0 and name == "drc")
    )
    return BoardBenchCase.from_dict(
        {
            "id": f"case-{index:02d}",
            "category": category,
            "prompt": f"Design private board {index:02d} from natural language.",
            "applicable_metrics": list(metrics),
            "review_rubric": ["Review functional intent before editing."],
            "component_slots": [
                {
                    "id": "controller",
                    "alternatives": [
                        {
                            "part_id": f"part-{index:02d}",
                            "symbol": "MCU_Test:Device",
                            "footprints": ["Package_Test:QFN-16"],
                        }
                    ],
                }
            ],
            "net_rules": [],
            "support_requirements": [],
            "forbidden_conditions": [],
            "rating_bounds": [],
            "manufacturing_constraints": [],
            "assembly_constraints": ["Review assembly constraints manually."],
        },
        "$case",
    )


def _corpus(*, cohort: str = "sealed_holdout_baseline") -> BoardBenchCorpus:
    return BoardBenchCorpus(
        corpus_id="private-v1",
        corpus_version=1,
        license="CC0-1.0",
        methodology="Non-baseline deterministic report fixture.",
        cohort=cohort,
        cases=tuple(
            _case(category_index * 4 + offset, category)
            for category_index, category in enumerate(BOARD_CATEGORIES)
            for offset in range(4)
        ),
    )


def _campaign(corpus: BoardBenchCorpus) -> BoardBenchCampaign:
    return BoardBenchCampaign(
        campaign_id="campaign-v1",
        cohort=corpus.cohort,
        corpus_id=corpus.corpus_id,
        corpus_sha256=artifact_sha256(corpus),
        created_at=NOW,
        pcbdraft_commit="abcdef1234567890",
        dirty_state_sha256=HASH_A,
        provider="fixture-provider",
        model="fixture-model",
        configuration_sha256=HASH_B,
        kicad_version="10.0.0",
        python_version="3.13.6",
        platform="linux-test",
        tool_registry_sha256=HASH_C,
        wall_timeout_seconds=900.0,
        tool_call_budget=500,
        repetitions=3,
        evaluator_version=EVALUATOR_VERSION,
        runs=tuple(
            CampaignRunPlan(
                case_id=case.id,
                repetition=repetition,
                run_id=f"{case.id}-run-{repetition}",
            )
            for case in corpus.cases
            for repetition in range(1, 4)
        ),
    )


def _run(
    campaign: BoardBenchCampaign, case: BoardBenchCase, repetition: int
) -> BoardBenchRun:
    return BoardBenchRun(
        campaign_id=campaign.campaign_id,
        run_id=f"{case.id}-run-{repetition}",
        case_id=case.id,
        repetition=repetition,
        prompt_sha256=_sha256(case.prompt),
        status="completed",
        started_at=NOW,
        completed_at=LATER,
        termination_reason="agent_returned",
        final_response="The board is complete.",
        inventory=(InventoryEntry("project/design.kicad_sch", 1, HASH_A),),
    )


def _efficiency(
    *,
    cost_status: str = "subscription_included",
    cost_amount: float | None = None,
    cost_currency: str | None = None,
) -> EfficiencyMetrics:
    return EfficiencyMetrics(
        model_requests=2,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        total_tokens=150,
        token_status="reported",  # noqa: S106 - evidence state, not a credential
        token_source="provider_usage",  # noqa: S106 - evidence provenance
        cost_amount=cost_amount,
        cost_currency=cost_currency,
        cost_status=cost_status,
        cost_source=(
            "provider_cost_api" if cost_status == "actual" else "subscription_contract"
        ),
        pcb_tool_calls=2,
        tool_call_counts=(ToolCallCount("pcb_run_erc", "completed", 2),),
        provider_retries=0,
        provider_errors=0,
        tool_seconds=1.0,
        api_seconds=2.0,
        wall_seconds=10.0,
        failure_reason=None,
    )


def _score(
    corpus: BoardBenchCorpus,
    campaign: BoardBenchCampaign,
    run: BoardBenchRun,
    *,
    efficiency: EfficiencyMetrics | None = None,
) -> BoardBenchScore:
    case = next(item for item in corpus.cases if item.id == run.case_id)
    metrics = tuple(
        MetricResult(
            name=name,
            state="pass" if name in case.applicable_metrics else "not_applicable",
            reason="fixture independent evidence",
        )
        for name in AUTOMATIC_METRICS
    )
    return BoardBenchScore(
        campaign_id=campaign.campaign_id,
        run_id=run.run_id,
        source_campaign_sha256=artifact_sha256(campaign),
        source_case_sha256=hashlib.sha256(
            canonical_json_bytes(case.to_dict())
        ).hexdigest(),
        source_run_sha256=artifact_sha256(run),
        evaluator_version=campaign.evaluator_version,
        scored_at=LATER,
        overall_state="pass",
        metrics=metrics,
        efficiency=efficiency or _efficiency(),
        failure_suggestion=None,
    )


def _review(
    corpus: BoardBenchCorpus, campaign: BoardBenchCampaign, run: BoardBenchRun
) -> BoardBenchReview:
    case = next(case for case in corpus.cases if case.id == run.case_id)
    score = _score(corpus, campaign, run)
    return BoardBenchReview(
        campaign_id=campaign.campaign_id,
        run_id=run.run_id,
        source_campaign_sha256=artifact_sha256(campaign),
        source_corpus_sha256=artifact_sha256(corpus),
        source_case_sha256=case_sha256(case),
        source_run_sha256=artifact_sha256(run),
        source_score_sha256=artifact_sha256(score),
        reviewer="engineer-1",
        reviewed_at=LATER,
        outcome="pass_without_schematic_change",
        functional_correctness="pass",
        orderable_state="pass",
        orderability_evidence=(
            OrderabilityEvidence(
                slot_ids=("controller",),
                manufacturer_part_number=f"FIXTURE-{run.case_id.upper()}",
                status="orderable",
                as_of=LATER,
                source_kind="manufacturer",
                source_name="Fixture manufacturer",
                source_url=f"https://example.com/parts/{run.case_id}",
                note="Reviewer inspected dated fixture sourcing evidence.",
            ),
        ),
        active_engineer_minutes=10.0,
        not_applicable_reason=None,
        checklist=tuple(
            replace(
                item,
                disposition="pass",
                evidence_note="Engineer inspected this case-authored obligation.",
            )
            for item in review_checklist_for_case(case)
        ),
        findings=(),
        modifications=(),
        final_failure=None,
    )


def _release_hash(category: str) -> str:
    return _sha256(f"release:{category}")


def _hardware(
    campaign: BoardBenchCampaign,
    run: BoardBenchRun,
    category: str,
    *,
    notes: str = "Current-limited first-power evidence.",
) -> BoardBenchHardware:
    return BoardBenchHardware(
        campaign_id=campaign.campaign_id,
        run_id=run.run_id,
        source_kind="release",
        source_artifact_sha256=_release_hash(category),
        category=category,
        board_revision="A",
        board_serial=f"unit-{category}",
        revision_count=1,
        fabricator="Fixture board house",
        operator="engineer-1",
        observed_at=LATER,
        fabricator_accepted="pass",
        solderability="pass",
        first_power_no_short="pass",
        firmware_download="pass",
        core_function="pass",
        rails=(RailMeasurement("3V3", "V", 3.2, 3.4, 3.3, "pass"),),
        notes=notes,
        attachments=(InventoryEntry(f"lab/{category}-measurement.jpg", 100, HASH_C),),
    )


def _correction(
    campaign: BoardBenchCampaign,
    run: BoardBenchRun,
    review: BoardBenchReview,
    candidate_hash: str,
) -> BoardBenchCorrection:
    return BoardBenchCorrection(
        campaign_id=campaign.campaign_id,
        run_id=run.run_id,
        source_run_sha256=artifact_sha256(run),
        source_review_sha256=artifact_sha256(review),
        created_at=LATER,
        generated_snapshot_sha256=HASH_A,
        corrected_snapshot_sha256=HASH_B,
        manufacturing_candidate_sha256=candidate_hash,
        decision_ids=tuple(item.id for item in review.modifications),
        changes=(
            StructuralDiffEntry(
                area="components",
                operation="changed",
                identity="U1",
                before_sha256=HASH_A,
                after_sha256=HASH_B,
                description="Fixture correction.",
            ),
        ),
        inventory=(),
    )


@dataclass(frozen=True)
class _Fixture:
    corpus: BoardBenchCorpus
    campaign: BoardBenchCampaign
    runs: tuple[BoardBenchRun, ...]
    scores: tuple[BoardBenchScore, ...]
    reviews: tuple[BoardBenchReview, ...]
    selection: BoardBenchSelection
    hardware: tuple[BoardBenchHardware, ...]


def _fixture(
    *,
    private_notes: str | None = None,
    cohort: str = "sealed_holdout_baseline",
) -> _Fixture:
    corpus = _corpus(cohort=cohort)
    campaign = _campaign(corpus)
    runs = tuple(
        _run(campaign, case, repetition)
        for case in corpus.cases
        for repetition in range(1, 4)
    )
    scores = tuple(_score(corpus, campaign, run) for run in runs)
    reviews = tuple(_review(corpus, campaign, run) for run in runs)
    selected_runs = tuple(runs[index * 12] for index in range(5))
    selection = BoardBenchSelection(
        campaign_id=campaign.campaign_id,
        created_at=LATER,
        selector="engineer-1",
        selections=tuple(
            SelectionEntry(
                category=category,
                run_id=run.run_id,
                source_artifact_sha256=artifact_sha256(run),
                rationale="Representative category candidate.",
            )
            for category, run in zip(BOARD_CATEGORIES, selected_runs, strict=True)
        ),
    )
    hardware = tuple(
        _hardware(
            campaign,
            run,
            category,
            notes=(
                private_notes
                if category == BOARD_CATEGORIES[0] and private_notes is not None
                else "Current-limited first-power evidence."
            ),
        )
        for category, run in zip(BOARD_CATEGORIES, selected_runs, strict=True)
    )
    return _Fixture(corpus, campaign, runs, scores, reviews, selection, hardware)


def _materialize_layout(
    root: Path,
    fixture: _Fixture,
    *,
    fixture_label: str | None = None,
) -> Path:
    write_artifact(root / "campaign.json", fixture.campaign)
    materialized_runs: dict[str, BoardBenchRun] = {}
    for source_run, source_score, source_review in zip(
        fixture.runs, fixture.scores, fixture.reviews, strict=True
    ):
        artifacts = root / "runs" / source_run.run_id / "artifacts"
        artifacts.mkdir(parents=True)
        atomic_write_json(
            artifacts / "execution.json",
            {
                "schema": "pcbdraft-boardbench-worker-execution",
                "version": 1,
                "argv": ["python", "-m", "pcbdraft.interfaces.boardbench_worker"],
                "returncode": 0,
                "duration_seconds": 1.0,
                "timed_out": False,
                "output_limited": False,
                "retained_project_count": 1,
                "fixture_label": fixture_label,
            },
        )
        atomic_write_text(artifacts / "evidence.txt", "retained run evidence\n")
        atomic_write_text(
            artifacts / "project" / "design.kicad_sch",
            "(kicad_sch (version 20250114) (generator pcbdraft))\n",
        )
        run = replace(source_run, inventory=build_inventory(artifacts))
        materialized_runs[run.run_id] = run
        score = replace(source_score, source_run_sha256=artifact_sha256(run))
        review = replace(
            source_review,
            source_run_sha256=artifact_sha256(run),
            source_score_sha256=artifact_sha256(score),
        )
        write_artifact(root / "runs" / run.run_id / "run.json", run)
        write_artifact(root / "scores" / run.run_id / "score.json", score)
        write_artifact(root / "reviews" / run.run_id / "review.json", review)

    selection = replace(
        fixture.selection,
        selections=tuple(
            replace(
                item,
                source_artifact_sha256=artifact_sha256(materialized_runs[item.run_id]),
            )
            for item in fixture.selection.selections
        ),
    )
    write_artifact(root / "selection.json", selection)
    hardware_by_run = {item.run_id: item for item in fixture.hardware}
    for entry in selection.selections:
        source = hardware_by_run[entry.run_id]
        hardware_root = root / "hardware" / entry.run_id
        attachments = hardware_root / "attachments"
        attachments.mkdir(parents=True)
        atomic_write_text(
            attachments / "measurement.txt", "current-limited hardware evidence\n"
        )
        release_bytes = f"release:{entry.category}".encode()
        atomic_write_bytes(hardware_root / "source" / "release-artifact", release_bytes)
        hardware = replace(
            source,
            source_artifact_sha256=hashlib.sha256(release_bytes).hexdigest(),
            attachments=build_inventory(attachments),
        )
        write_artifact(hardware_root / "hardware.json", hardware)
    return root


class BoardBenchReportTests(unittest.TestCase):
    def test_fixed_denominators_preserve_missing_unknown_and_not_applicable(
        self,
    ) -> None:
        corpus = _corpus()
        campaign = _campaign(corpus)
        report = aggregate_report(corpus, campaign, generated_at=LATER)
        overall = next(item for item in report.slices if item.scope == "overall")
        outcomes = {item.value: item.count for item in overall.automatic_outcomes}
        metrics = {item.name: item for item in overall.automatic_metrics}
        reviews = {item.value: item.count for item in overall.reviews.outcomes}
        token_statuses = {
            item.value: item.count for item in overall.efficiency.token_statuses
        }
        cost_statuses = {
            item.value: item.count for item in overall.efficiency.cost_statuses
        }

        self.assertFalse(report.sealed)
        self.assertEqual(overall.planned_runs, 60)
        self.assertEqual(overall.terminal_runs, 0)
        self.assertEqual(outcomes, {"fail": 0, "pass": 0, "unknown": 60})
        self.assertEqual(metrics["drc"].unknown, 57)
        self.assertEqual(metrics["drc"].not_applicable, 3)
        self.assertEqual(reviews["not_reviewed"], 60)
        self.assertEqual(token_statuses["unknown"], 60)
        self.assertEqual(cost_statuses["unknown"], 60)
        self.assertEqual(overall.efficiency.model_requests.missing_count, 60)
        self.assertEqual(overall.efficiency.cost_amount.observed_count, 0)
        self.assertEqual(
            len([item for item in report.slices if item.scope == "case"]), 20
        )
        self.assertTrue(
            all(
                item.planned_runs == 12
                for item in report.slices
                if item.scope == "category"
            )
        )

    def test_efficiency_and_cost_statuses_keep_the_60_run_denominator(self) -> None:
        fixture = _fixture()
        priced = replace(
            fixture.scores[0],
            efficiency=_efficiency(
                cost_status="actual", cost_amount=0.25, cost_currency="USD"
            ),
        )
        report = aggregate_report(
            fixture.corpus,
            fixture.campaign,
            runs=fixture.runs[:2],
            scores=(priced, fixture.scores[1]),
            generated_at=LATER,
        )
        overall = next(item for item in report.slices if item.scope == "overall")
        statuses = {item.value: item.count for item in overall.efficiency.cost_statuses}
        self.assertEqual(statuses["actual"], 1)
        self.assertEqual(statuses["subscription_included"], 1)
        self.assertEqual(statuses["unknown"], 58)
        self.assertEqual(overall.efficiency.cost_amount.observed_count, 1)
        self.assertEqual(overall.efficiency.cost_amount.sum_value, 0.25)
        self.assertEqual(overall.efficiency.cost_currency, "USD")
        self.assertEqual(overall.efficiency.total_tokens.observed_count, 2)
        self.assertEqual(overall.efficiency.total_tokens.missing_count, 58)

    def test_changed_score_and_review_source_hashes_are_rejected(self) -> None:
        fixture = _fixture()
        for label, score, review in (
            (
                "score",
                replace(fixture.scores[0], source_run_sha256=HASH_B),
                fixture.reviews[0],
            ),
            (
                "review",
                fixture.scores[0],
                replace(fixture.reviews[0], source_run_sha256=HASH_B),
            ),
        ):
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(ValidationError, "source hash"),
            ):
                aggregate_report(
                    fixture.corpus,
                    fixture.campaign,
                    runs=(fixture.runs[0],),
                    scores=(score,),
                    reviews=(review,),
                )

    def test_review_rejects_score_content_drift_and_hidden_case_replay(self) -> None:
        fixture = _fixture()
        run = fixture.runs[0]
        review = fixture.reviews[0]

        changed_score = replace(fixture.scores[0], scored_at=NOW)
        with self.assertRaisesRegex(ValidationError, "source score hash"):
            aggregate_report(
                fixture.corpus,
                fixture.campaign,
                runs=(run,),
                scores=(changed_score,),
                reviews=(review,),
            )

        original_case = fixture.corpus.cases[0]
        changed_case = replace(
            original_case,
            applicable_metrics=tuple(
                name
                for name in original_case.applicable_metrics
                if name != "support_circuits"
            ),
        )
        changed_corpus = replace(
            fixture.corpus,
            cases=(changed_case, *fixture.corpus.cases[1:]),
        )
        changed_campaign = replace(
            fixture.campaign,
            corpus_sha256=artifact_sha256(changed_corpus),
        )
        changed_score = replace(
            fixture.scores[0],
            source_campaign_sha256=artifact_sha256(changed_campaign),
            source_case_sha256=case_sha256(changed_case),
        )
        replayed_review = replace(
            review,
            source_campaign_sha256=artifact_sha256(changed_campaign),
            source_corpus_sha256=artifact_sha256(changed_corpus),
            source_score_sha256=artifact_sha256(changed_score),
        )
        with self.assertRaisesRegex(ValidationError, "source hash or identity"):
            aggregate_report(
                changed_corpus,
                changed_campaign,
                runs=(run,),
                scores=(changed_score,),
                reviews=(replayed_review,),
            )

    def test_automatic_failure_requires_reviewer_final_classification(self) -> None:
        fixture = _fixture()
        failure = FailureClassification(
            stage="validation",
            causes=("insufficient_evidence",),
            owners=("environment",),
            reason="Automatic evidence did not pass.",
        )
        score = replace(
            fixture.scores[0],
            overall_state="fail",
            metrics=(
                replace(fixture.scores[0].metrics[0], state="fail"),
                *fixture.scores[0].metrics[1:],
            ),
            failure_suggestion=failure,
        )
        review = replace(
            fixture.reviews[0],
            source_score_sha256=artifact_sha256(score),
            final_failure=None,
        )
        with self.assertRaisesRegex(ValidationError, "reviewer final failure"):
            aggregate_report(
                fixture.corpus,
                fixture.campaign,
                runs=(fixture.runs[0],),
                scores=(score,),
                reviews=(review,),
            )

        report = aggregate_report(
            fixture.corpus,
            fixture.campaign,
            runs=(fixture.runs[0],),
            scores=(score,),
        )
        overall = next(item for item in report.slices if item.scope == "overall")
        stages = {item.stage: item.count for item in overall.failures.stages}
        self.assertEqual(60, stages["unclassified"])
        self.assertEqual(0, stages["validation"])

    def test_aggregate_rejects_failure_classification_hidden_by_dual_pass(
        self,
    ) -> None:
        fixture = _fixture()
        failure = FailureClassification(
            stage="circuit_design",
            causes=("model_reasoning",),
            owners=("model",),
            reason="The source candidate failed despite contradictory pass labels.",
        )
        review = replace(fixture.reviews[0], final_failure=failure)
        with self.assertRaisesRegex(ValidationError, "contradicts passing"):
            aggregate_report(
                fixture.corpus,
                fixture.campaign,
                runs=(fixture.runs[0],),
                scores=(fixture.scores[0],),
                reviews=(review,),
            )

        score = replace(
            fixture.scores[0],
            overall_state="fail",
            metrics=(
                replace(fixture.scores[0].metrics[0], state="fail"),
                *fixture.scores[0].metrics[1:],
            ),
            failure_suggestion=failure,
        )
        review = replace(review, source_score_sha256=artifact_sha256(score))
        aggregate_report(
            fixture.corpus,
            fixture.campaign,
            runs=(fixture.runs[0],),
            scores=(score,),
            reviews=(review,),
        )

    def test_seal_revalidates_review_items_against_the_source_case(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            run_id = fixture.runs[0].run_id
            run = load_run(campaign_root / "runs" / run_id / "run.json")
            review = replace(
                fixture.reviews[0],
                source_run_sha256=artifact_sha256(run),
                checklist=(
                    replace(
                        fixture.reviews[0].checklist[0],
                        requirement="Foreign replacement review obligation.",
                    ),
                    *fixture.reviews[0].checklist[1:],
                ),
            )
            atomic_write_json(
                campaign_root / "reviews" / run_id / "review.json",
                review.to_dict(),
            )
            output = Path(temporary) / "seal"
            with self.assertRaisesRegex(
                ValidationError, "checklist item differs from source case"
            ):
                seal_campaign(output, campaign_root, fixture.corpus)
            self.assertFalse(output.exists())

    def test_report_and_seal_reject_review_v3_source_bypass(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            run_id = fixture.runs[0].run_id
            review_path = campaign_root / "reviews" / run_id / "review.json"
            review = replace(
                load_review(review_path),
                source_corpus_sha256=HASH_B,
            )
            atomic_write_json(review_path, review.to_dict())
            with self.assertRaisesRegex(ValidationError, "source hash or identity"):
                load_campaign_evidence(campaign_root, fixture.corpus)
            output = Path(temporary) / "seal-must-not-exist"
            with self.assertRaisesRegex(ValidationError, "source hash or identity"):
                seal_campaign(output, campaign_root, fixture.corpus)
            self.assertFalse(output.exists())

    def test_seal_rejects_source_corpus_hash_drift_before_writing(self) -> None:
        fixture = _fixture()
        changed_corpus = replace(
            fixture.corpus,
            methodology=fixture.corpus.methodology + " Changed after the campaign.",
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            output = Path(temporary) / "seal"
            with self.assertRaisesRegex(ValidationError, "corpus does not match"):
                seal_campaign(output, campaign_root, changed_corpus)
            self.assertFalse(output.exists())

    def test_correction_hardware_and_selection_bind_distinct_source_hashes(
        self,
    ) -> None:
        fixture = _fixture()
        run = fixture.runs[0]
        changed_review = replace(
            fixture.reviews[0],
            outcome="pass_after_changes",
            modifications=(
                ModificationDecision(
                    "change-1",
                    "functional",
                    "Repair the generated circuit.",
                    (),
                ),
            ),
            final_failure=FailureClassification(
                "circuit_design",
                ("model_reasoning",),
                ("model",),
                "Engineer-confirmed generated-design failure.",
            ),
        )
        candidate_hash = _sha256("manufacturing-candidate")
        correction = _correction(fixture.campaign, run, changed_review, candidate_hash)
        correction_hash = artifact_sha256(correction)
        self.assertNotEqual(candidate_hash, correction_hash)
        selections = list(fixture.selection.selections)
        selections[0] = replace(selections[0], source_artifact_sha256=candidate_hash)
        selection = replace(fixture.selection, selections=tuple(selections))
        hardware = (
            replace(
                fixture.hardware[0],
                source_kind="correction",
                source_artifact_sha256=correction_hash,
            ),
            *fixture.hardware[1:],
        )

        report = aggregate_report(
            fixture.corpus,
            fixture.campaign,
            runs=fixture.runs,
            scores=fixture.scores,
            reviews=(changed_review, *fixture.reviews[1:]),
            corrections=(correction,),
            hardware=hardware,
            selection=selection,
            generated_at=LATER,
        )
        self.assertFalse(report.sealed)

        for label, changed_selection, changed_hardware in (
            (
                "selection",
                replace(
                    selection,
                    selections=(
                        replace(selections[0], source_artifact_sha256=correction_hash),
                        *selections[1:],
                    ),
                ),
                hardware,
            ),
            (
                "hardware",
                selection,
                (
                    replace(hardware[0], source_artifact_sha256=candidate_hash),
                    *hardware[1:],
                ),
            ),
        ):
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(ValidationError, label),
            ):
                aggregate_report(
                    fixture.corpus,
                    fixture.campaign,
                    runs=fixture.runs,
                    scores=fixture.scores,
                    reviews=(changed_review, *fixture.reviews[1:]),
                    corrections=(correction,),
                    hardware=changed_hardware,
                    selection=changed_selection,
                )

    def test_seal_fails_closed_for_missing_review_correction_or_category(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            missing_review = (
                campaign_root / "reviews" / fixture.reviews[-1].run_id / "review.json"
            )
            missing_review.unlink()
            output = Path(temporary) / "missing-review"
            with self.assertRaisesRegex(ValidationError, "60 completed reviews"):
                seal_campaign(
                    output,
                    campaign_root,
                    fixture.corpus,
                    generated_at=LATER,
                )
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            changed_run_id = fixture.reviews[1].run_id
            run = load_run(campaign_root / "runs" / changed_run_id / "run.json")
            score = load_score(campaign_root / "scores" / changed_run_id / "score.json")
            changed_review = replace(
                fixture.reviews[1],
                source_run_sha256=artifact_sha256(run),
                source_score_sha256=artifact_sha256(score),
                outcome="pass_after_changes",
                modifications=(
                    ModificationDecision(
                        "change-1",
                        "functional",
                        "Repair the generated circuit.",
                        (),
                    ),
                ),
                final_failure=FailureClassification(
                    "circuit_design",
                    ("model_reasoning",),
                    ("model",),
                    "Engineer-confirmed generated-design failure.",
                ),
            )
            atomic_write_json(
                campaign_root / "reviews" / changed_run_id / "review.json",
                changed_review.to_dict(),
            )
            with self.assertRaisesRegex(ValidationError, "missing correction"):
                seal_campaign(
                    Path(temporary) / "missing-correction",
                    campaign_root,
                    fixture.corpus,
                    generated_at=LATER,
                )

        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            shutil.rmtree(campaign_root / "hardware" / fixture.hardware[-1].run_id)
            with self.assertRaisesRegex(ValidationError, "hardware record"):
                seal_campaign(
                    Path(temporary) / "missing-hardware",
                    campaign_root,
                    fixture.corpus,
                    generated_at=LATER,
                )

    def test_seal_writes_strict_report_markdown_and_source_manifest(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            output = Path(temporary) / "seal"
            report = seal_campaign(
                output,
                campaign_root,
                fixture.corpus,
                generated_at=LATER,
            )
            reloaded = load_report(output / "report.json")
            markdown = (output / "report.md").read_text(encoding="utf-8")
            manifest = json.loads(
                (output / "seal-manifest.json").read_text(encoding="utf-8")
            )
        self.assertEqual(report, reloaded)
        self.assertTrue(report.sealed)
        self.assertIn("frozen 60-run campaign plan", markdown)
        self.assertEqual(manifest["schema"], "pcbdraft-boardbench-seal-manifest")
        self.assertEqual(len(manifest["artifacts"]), 189)

    def test_ai_reviewed_pilot_report_is_labeled_and_cannot_seal_or_publish(
        self,
    ) -> None:
        fixture = _fixture(cohort=AI_REVIEWED_PILOT_COHORT)
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            evidence = load_campaign_evidence(campaign_root, fixture.corpus)
            report = aggregate_report(
                fixture.corpus,
                evidence.campaign,
                runs=evidence.runs,
                scores=evidence.scores,
                reviews=evidence.reviews,
                corrections=evidence.corrections,
                hardware=evidence.hardware,
                selection=evidence.selection,
                generated_at=LATER,
            )
            self.assertEqual(AI_REVIEWED_PILOT_COHORT, report.cohort)
            self.assertFalse(report.sealed)
            self.assertIn("not an independent human-reviewed", render_markdown(report))

            seal_output = Path(temporary) / "pilot-seal"
            with self.assertRaisesRegex(ValidationError, "cannot be sealed"):
                seal_campaign(seal_output, campaign_root, fixture.corpus)
            self.assertFalse(seal_output.exists())

            publication_output = Path(temporary) / "pilot-publication"
            with self.assertRaisesRegex(ValidationError, "cannot be sealed"):
                create_publication_bundle(
                    publication_output,
                    campaign_root,
                    report,
                    fixture.corpus,
                )
            self.assertFalse(publication_output.exists())

    def test_seal_requires_canonical_non_fixture_classified_physical_evidence(
        self,
    ) -> None:
        fixture = _fixture()
        with self.assertRaisesRegex(ValidationError, "canonical campaign evidence"):
            aggregate_report(
                fixture.corpus,
                fixture.campaign,
                runs=fixture.runs,
                scores=fixture.scores,
                reviews=fixture.reviews,
                hardware=fixture.hardware,
                selection=fixture.selection,
                sealed=True,
            )

        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(
                Path(temporary) / "fixture-campaign",
                fixture,
                fixture_label="non_baseline_fixture",
            )
            output = Path(temporary) / "fixture-seal"
            with self.assertRaisesRegex(ValidationError, "non-baseline fixture"):
                seal_campaign(output, campaign_root, fixture.corpus)
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(
                Path(temporary) / "unclassified-campaign", fixture
            )
            run_id = fixture.runs[1].run_id
            score_path = campaign_root / "scores" / run_id / "score.json"
            score = load_score(score_path)
            failed_metric = replace(score.metrics[0], state="fail")
            unclassified = replace(
                score,
                overall_state="fail",
                metrics=(failed_metric, *score.metrics[1:]),
                failure_suggestion=FailureClassification(
                    "unclassified",
                    ("unclassified",),
                    ("unclassified",),
                    "Awaiting engineer classification.",
                ),
            )
            atomic_write_json(score_path, unclassified.to_dict())
            review_path = campaign_root / "reviews" / run_id / "review.json"
            review = replace(
                load_review(review_path),
                source_score_sha256=artifact_sha256(unclassified),
                final_failure=unclassified.failure_suggestion,
            )
            atomic_write_json(review_path, review.to_dict())
            with self.assertRaisesRegex(ValidationError, "unclassified"):
                seal_campaign(
                    Path(temporary) / "unclassified-seal",
                    campaign_root,
                    fixture.corpus,
                )

        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(
                Path(temporary) / "untested-campaign", fixture
            )
            run_id = fixture.hardware[0].run_id
            hardware_path = campaign_root / "hardware" / run_id / "hardware.json"
            untested = replace(
                load_hardware(hardware_path), first_power_no_short="not_tested"
            )
            atomic_write_json(hardware_path, untested.to_dict())
            with self.assertRaisesRegex(ValidationError, "physical hardware"):
                seal_campaign(
                    Path(temporary) / "untested-seal",
                    campaign_root,
                    fixture.corpus,
                )

    def test_publication_bundle_redacts_secrets_and_absolute_paths(self) -> None:
        secret = "sk-" + "fixture-secret-123456789"
        private_path = "/home/alice/private/measurement.jpg"
        fixture = _fixture(private_notes=f"token={secret} stored at {private_path}")
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            report = seal_campaign(
                Path(temporary) / "seal",
                campaign_root,
                fixture.corpus,
                generated_at=LATER,
            )
            bundle = create_publication_bundle(
                Path(temporary) / "public",
                campaign_root,
                report,
                fixture.corpus,
            )
            rendered = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(bundle.rglob("*"))
                if path.is_file()
            )
            manifest = json.loads(
                (bundle / "publication-manifest.json").read_text(encoding="utf-8")
            )
        self.assertNotIn(secret, rendered)
        self.assertNotIn(private_path, rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertIn("[REDACTED_PATH]", rendered)
        self.assertTrue(manifest["metadata_only"])
        self.assertFalse(any("kicad" in item["path"] for item in manifest["files"]))

    def test_publication_failure_leaves_no_partial_output(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = _materialize_layout(Path(temporary) / "campaign", fixture)
            report = seal_campaign(
                Path(temporary) / "seal", campaign_root, fixture.corpus
            )
            output = Path(temporary) / "publication"
            with (
                mock.patch(
                    "pcbdraft.verification.boardbench_report._assert_publication_safe",
                    side_effect=ValidationError("forced publication scan failure"),
                ),
                self.assertRaisesRegex(ValidationError, "forced publication"),
            ):
                create_publication_bundle(output, campaign_root, report, fixture.corpus)
            self.assertFalse(output.exists())

    def test_layout_loader_discovers_only_fixed_derived_evidence_paths(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "campaign"
            write_artifact(root / "campaign.json", fixture.campaign)
            artifacts = root / "runs" / fixture.runs[0].run_id / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "evidence.txt").write_text("evidence", encoding="utf-8")
            (artifacts / "design.kicad_sch").write_text(
                "(kicad_sch (version 20250114) (generator pcbdraft))\n",
                encoding="utf-8",
            )
            run = replace(fixture.runs[0], inventory=build_inventory(artifacts))
            score = _score(fixture.corpus, fixture.campaign, run)
            review = _review(fixture.corpus, fixture.campaign, run)
            write_artifact(root / "runs" / run.run_id / "run.json", run)
            write_artifact(root / "scores" / run.run_id / "score.json", score)
            write_artifact(root / "reviews" / run.run_id / "review.json", review)
            (root / "corrections" / BOARD_BENCH_LOCK_DIR).mkdir(parents=True)
            loaded = load_campaign_evidence(root, fixture.corpus)
        self.assertEqual(loaded.runs, (run,))
        self.assertEqual(loaded.scores, (score,))
        self.assertEqual(loaded.reviews, (review,))
        self.assertIsNone(loaded.selection)

    def test_layout_loader_rejects_inventory_drift_and_symlink_root(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "campaign"
            artifacts = root / "runs" / fixture.runs[0].run_id / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "evidence.txt").write_text("before", encoding="utf-8")
            run = replace(fixture.runs[0], inventory=build_inventory(artifacts))
            write_artifact(root / "campaign.json", fixture.campaign)
            write_artifact(root / "runs" / run.run_id / "run.json", run)
            (artifacts / "evidence.txt").write_text("after", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "inventory differs"):
                load_campaign_evidence(root, fixture.corpus)

            link = base / "campaign-link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                load_campaign_evidence(link, fixture.corpus)

    def test_layout_loader_rejects_release_tamper_and_unexpected_evidence(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = _materialize_layout(Path(temporary) / "campaign", fixture)
            release = (
                root
                / "hardware"
                / fixture.hardware[0].run_id
                / "source"
                / "release-artifact"
            )
            release.write_bytes(b"tampered release")
            with self.assertRaisesRegex(ValidationError, "release source hash"):
                load_campaign_evidence(root, fixture.corpus)

        with tempfile.TemporaryDirectory() as temporary:
            root = _materialize_layout(Path(temporary) / "campaign", fixture)
            (root / "scores" / "unexpected-run").mkdir(parents=True)
            with self.assertRaisesRegex(ValidationError, "unexpected evidence"):
                load_campaign_evidence(root, fixture.corpus)


if __name__ == "__main__":
    unittest.main()
