from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.verification.boardbench import (
    BoardBenchCorpus,
    BoardBenchReview,
    FailureClassification,
    artifact_sha256,
    load_campaign,
    load_review,
    write_artifact,
)
from pcbdraft.verification.boardbench_evaluator import EVALUATOR_VERSION, evaluate_run
from pcbdraft.verification.boardbench_evidence import create_review_templates
from pcbdraft.verification.boardbench_report import (
    aggregate_report,
    load_campaign_evidence,
    seal_campaign,
)
from pcbdraft.verification.boardbench_runner import (
    FIXTURE_LABEL,
    create_campaign,
    run_campaign,
)
from tests.verification.test_boardbench_runner import (
    _corpus,
    _environment,
    _FakeWorkerProcess,
)

FIXTURE_TIME = "2026-08-21T12:00:00Z"


def _non_baseline_corpus() -> BoardBenchCorpus:
    source = _corpus()
    return replace(
        source,
        cohort="public_corpus_rerun",
        methodology=(
            "non_baseline_fixture: deterministic fake-provider lifecycle test; "
            "no unseen-task, engineering-review, or physical-result claim."
        ),
        cases=tuple(
            replace(
                case,
                prompt=(f"Build deterministic non-baseline fixture board {index:02d}."),
            )
            for index, case in enumerate(source.cases)
        ),
    )


class BoardBenchEndToEndFixtureTests(unittest.TestCase):
    """This fake-provider flow is software evidence, never baseline evidence."""

    def test_non_baseline_fixture_reaches_draft_fixed_denominators_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            corpus = _non_baseline_corpus()
            environment = _environment()
            campaign_root = create_campaign(
                root / "campaigns",
                corpus,
                campaign_id="non-baseline-fixture",
                evaluator_version=EVALUATOR_VERSION,
                environment=environment,
                created_at=FIXTURE_TIME,
                wall_timeout_seconds=30.0,
            )
            campaign = load_campaign(campaign_root / "campaign.json")
            self.assertEqual("public_corpus_rerun", campaign.cohort)
            self.assertEqual(60, len(campaign.runs))

            fake_worker = _FakeWorkerProcess()
            runs = run_campaign(
                campaign_root,
                corpus,
                source_root=Path(__file__).resolve().parents[2],
                environment_probe=lambda: environment,
                command_runner=fake_worker,
                fixture_label=FIXTURE_LABEL,
            )
            self.assertEqual(60, len(fake_worker.calls))
            self.assertEqual({run.status for run in runs}, {"completed"})
            self.assertEqual(
                [request["prompt"] for request in fake_worker.requests],
                [case.prompt for case in corpus.cases for _ in range(3)],
            )
            for run in runs:
                execution = json.loads(
                    (
                        campaign_root
                        / "runs"
                        / run.run_id
                        / "artifacts"
                        / "execution.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(FIXTURE_LABEL, execution["fixture_label"])

            scores = tuple(
                evaluate_run(
                    corpus,
                    campaign,
                    run,
                    campaign_root / "runs" / run.run_id,
                    campaign_root / "scores" / run.run_id,
                )
                for run in runs
            )
            self.assertEqual(60, len(scores))
            self.assertEqual({score.overall_state for score in scores}, {"fail"})

            template_paths = create_review_templates(campaign_root, corpus)
            score_by_run = {score.run_id: score for score in scores}
            reviews: list[BoardBenchReview] = []
            for template_path in template_paths:
                template = load_review(template_path)
                self.assertEqual(
                    artifact_sha256(score_by_run[template.run_id]),
                    template.source_score_sha256,
                )
                review = replace(
                    template,
                    reviewer=FIXTURE_LABEL,
                    reviewed_at=score_by_run[template.run_id].scored_at,
                    outcome="not_applicable",
                    functional_correctness="not_applicable",
                    orderable_state="not_applicable",
                    active_engineer_minutes=0.0,
                    not_applicable_reason=(
                        "The fake worker retained no inspectable KiCad schematic."
                    ),
                    checklist=tuple(
                        replace(
                            item,
                            disposition="not_applicable",
                            evidence_note=(
                                "No retained schematic exists for this fixture item."
                            ),
                        )
                        for item in template.checklist
                    ),
                    final_failure=FailureClassification(
                        stage="kicad_materialization",
                        causes=("environment_infrastructure",),
                        owners=("environment",),
                        reason=(
                            "The deterministic fake worker did not materialize a "
                            "schematic candidate."
                        ),
                    ),
                )
                reviews.append(review)
                write_artifact(
                    campaign_root / "reviews" / review.run_id / "review.json",
                    review,
                )
            self.assertEqual({review.reviewer for review in reviews}, {FIXTURE_LABEL})

            evidence = load_campaign_evidence(campaign_root, corpus)
            report = aggregate_report(
                corpus,
                campaign,
                runs=evidence.runs,
                scores=evidence.scores,
                reviews=evidence.reviews,
                corrections=evidence.corrections,
                hardware=evidence.hardware,
                selection=evidence.selection,
                generated_at=FIXTURE_TIME,
            )
            overall = next(item for item in report.slices if item.scope == "overall")
            outcomes = {item.value: item.count for item in overall.automatic_outcomes}
            review_outcomes = {
                item.value: item.count for item in overall.reviews.outcomes
            }
            physical = {
                metric.name: {item.value: item.count for item in metric.outcomes}
                for metric in overall.physical.metrics
            }
            self.assertFalse(report.sealed)
            self.assertEqual(60, overall.planned_runs)
            self.assertEqual(60, overall.terminal_runs)
            self.assertEqual(60, outcomes["fail"])
            self.assertEqual(60, review_outcomes["not_applicable"])
            self.assertEqual(0, review_outcomes["not_reviewed"])
            self.assertEqual(0, overall.physical.records)
            self.assertEqual(0, physical["first_power_no_short"]["not_tested"])
            self.assertEqual(0, physical["core_function"]["not_tested"])
            self.assertEqual(
                {item.planned_runs for item in report.slices if item.scope == "case"},
                {3},
            )
            self.assertEqual(
                {
                    item.planned_runs
                    for item in report.slices
                    if item.scope == "category"
                },
                {12},
            )

            with self.assertRaisesRegex(ValidationError, "non-baseline fixture"):
                seal_campaign(
                    root / "fixture-seal-must-not-exist",
                    campaign_root,
                    corpus,
                    generated_at=FIXTURE_TIME,
                )
            self.assertFalse((campaign_root / "seal-manifest.json").exists())
            self.assertFalse((campaign_root / "publication-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
