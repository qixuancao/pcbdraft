from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import call, patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "boardbench.py"


def _load_cli() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "pcbdraft_boardbench_operator", SCRIPT
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load the BoardBench operator script")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


class BoardBenchCliTests(unittest.TestCase):
    cli: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _load_cli()

    def _invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.cli.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_help_exposes_the_complete_operator_surface(self) -> None:
        help_text = self.cli.build_parser().format_help()

        for command in (
            "corpus",
            "campaign",
            "score",
            "review",
            "correction",
            "selection",
            "hardware",
            "report",
            "seal",
            "publication",
        ):
            self.assertIn(command, help_text)
        self.assertNotIn("holdout.json", help_text)

    def test_campaign_create_help_describes_wall_timeout_policy(self) -> None:
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            self.cli.main(["campaign", "create", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("--wall-timeout-seconds", help_text)
        self.assertIn("valid range 1..86400", help_text)
        self.assertIn("default: 3600", help_text)

    def test_corpus_path_is_required_and_validation_does_not_print_contents(
        self,
    ) -> None:
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            self.cli.main(["corpus", "validate"])
        self.assertEqual(2, raised.exception.code)

        private_corpus = SimpleNamespace(
            cases=(SimpleNamespace(prompt="PRIVATE HOLDOUT PROMPT"),)
        )
        with patch.object(self.cli, "load_corpus", return_value=private_corpus):
            result, stdout, command_stderr = self._invoke(
                ["corpus", "validate", "--corpus", "private/corpus.json"]
            )

        self.assertEqual(0, result)
        self.assertIn("corpus is valid", stdout)
        self.assertNotIn("PRIVATE HOLDOUT PROMPT", stdout)
        self.assertEqual("", command_stderr)

    def test_campaign_create_run_and_resume_delegate_to_runner(self) -> None:
        corpus = object()
        created = Path("campaigns/campaign-v1")
        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(self.cli, "create_campaign", return_value=created) as create,
        ):
            result, _stdout, stderr = self._invoke(
                [
                    "campaign",
                    "create",
                    "--corpus",
                    "private/corpus.json",
                    "--output",
                    "campaigns",
                    "--campaign-id",
                    "campaign-v1",
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual("", stderr)
        create.assert_called_once_with(
            Path("campaigns"),
            corpus,
            campaign_id="campaign-v1",
            evaluator_version=self.cli.EVALUATOR_VERSION,
            wall_timeout_seconds=self.cli.DEFAULT_WALL_TIMEOUT_SECONDS,
        )

        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(self.cli, "create_campaign", return_value=created) as create,
        ):
            result, _stdout, stderr = self._invoke(
                [
                    "campaign",
                    "create",
                    "--corpus",
                    "private/corpus.json",
                    "--output",
                    "campaigns",
                    "--campaign-id",
                    "campaign-custom-timeout",
                    "--wall-timeout-seconds",
                    "7200",
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual("", stderr)
        create.assert_called_once_with(
            Path("campaigns"),
            corpus,
            campaign_id="campaign-custom-timeout",
            evaluator_version=self.cli.EVALUATOR_VERSION,
            wall_timeout_seconds=7200.0,
        )

        for action in ("run", "resume"):
            with (
                patch.object(self.cli, "load_corpus", return_value=corpus),
                patch.object(
                    self.cli, "run_campaign", return_value=(object(),) * 60
                ) as run,
            ):
                result, stdout, stderr = self._invoke(
                    [
                        "campaign",
                        action,
                        "--campaign",
                        "campaigns/campaign-v1",
                        "--corpus",
                        "private/corpus.json",
                    ]
                )
            self.assertEqual(0, result)
            self.assertIn("60 run receipts", stdout)
            self.assertEqual("", stderr)
            run.assert_called_once_with(Path("campaigns/campaign-v1"), corpus)

        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(
                self.cli, "run_campaign", return_value=(object(),) * 60
            ) as run,
        ):
            result, stdout, stderr = self._invoke(
                [
                    "campaign",
                    "run",
                    "--campaign",
                    "campaigns/campaign-v1",
                    "--corpus",
                    "private/corpus.json",
                    "--run-id",
                    "case-00-run-1",
                    "--run-id",
                    "case-01-run-1",
                ]
            )
        self.assertEqual(0, result)
        self.assertIn("60 run receipts; 2 run ids were selected", stdout)
        self.assertEqual("", stderr)
        run.assert_called_once_with(
            Path("campaigns/campaign-v1"),
            corpus,
            run_ids=("case-00-run-1", "case-01-run-1"),
        )

    def test_campaign_create_invalid_wall_timeout_is_reported(self) -> None:
        corpus = object()
        for raw, expected, message in (
            ("0", 0.0, "campaign wall timeout is below 1.0"),
            ("86401", 86_401.0, "campaign wall timeout exceeds 86400.0"),
            ("nan", float("nan"), "campaign wall timeout must be a finite number"),
            ("inf", float("inf"), "campaign wall timeout must be a finite number"),
            ("-inf", float("-inf"), "campaign wall timeout must be a finite number"),
        ):
            with (
                self.subTest(raw=raw),
                patch.object(self.cli, "load_corpus", return_value=corpus),
                patch.object(
                    self.cli,
                    "create_campaign",
                    side_effect=self.cli.ValidationError(message),
                ) as create,
            ):
                result, stdout, stderr = self._invoke(
                    [
                        "campaign",
                        "create",
                        "--corpus",
                        "private/corpus.json",
                        "--output",
                        "campaigns",
                        "--campaign-id",
                        "invalid-timeout",
                        f"--wall-timeout-seconds={raw}",
                    ]
                )

                self.assertEqual(2, result)
                self.assertEqual("", stdout)
                self.assertIn(message, stderr)
                self.assertEqual(create.call_count, 1)
                call_arguments = create.call_args
                self.assertEqual(call_arguments.args, (Path("campaigns"), corpus))
                self.assertEqual(
                    call_arguments.kwargs["campaign_id"], "invalid-timeout"
                )
                self.assertEqual(
                    call_arguments.kwargs["evaluator_version"],
                    self.cli.EVALUATOR_VERSION,
                )
                actual = call_arguments.kwargs["wall_timeout_seconds"]
                if raw == "nan":
                    self.assertNotEqual(actual, actual)
                else:
                    self.assertEqual(actual, expected)

    def test_score_uses_the_fixed_derived_layout(self) -> None:
        campaign_root = Path("campaigns/campaign-v1")
        corpus = object()
        campaign = SimpleNamespace(runs=(SimpleNamespace(run_id="run-1"),))
        run = SimpleNamespace(terminal=True)
        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(self.cli, "load_campaign", return_value=campaign),
            patch.object(self.cli, "load_run", return_value=run),
            patch.object(self.cli, "aggregate_report"),
            patch.object(self.cli, "evaluate_run") as evaluate,
        ):
            result, _stdout, stderr = self._invoke(
                [
                    "score",
                    "--campaign",
                    str(campaign_root),
                    "--corpus",
                    "private/corpus.json",
                    "--run-id",
                    "run-1",
                ]
            )

        self.assertEqual(0, result)
        self.assertEqual("", stderr)
        evaluate.assert_called_once_with(
            corpus,
            campaign,
            run,
            campaign_root / "runs" / "run-1",
            campaign_root / "scores" / "run-1",
        )

        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(self.cli, "load_campaign", return_value=campaign),
            patch.object(self.cli, "evaluate_run") as evaluate,
        ):
            result, _stdout, stderr = self._invoke(
                [
                    "score",
                    "--campaign",
                    str(campaign_root),
                    "--corpus",
                    "private/corpus.json",
                    "--run-id",
                    "../../outside",
                ]
            )
        self.assertEqual(2, result)
        self.assertIn("not in the campaign plan", stderr)
        evaluate.assert_not_called()

    def test_score_all_resumes_in_campaign_order_and_verifies_existing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = Path(temporary) / "campaign-v1"
            existing_path = campaign_root / "scores" / "run-1" / "score.json"
            existing_path.parent.mkdir(parents=True)
            existing_path.write_text("fixture score placeholder\n", encoding="utf-8")
            corpus = object()
            campaign = SimpleNamespace(
                runs=tuple(
                    SimpleNamespace(run_id=run_id)
                    for run_id in ("run-1", "run-2", "run-3", "run-4")
                )
            )
            runs = {
                "run-1": SimpleNamespace(run_id="run-1", terminal=True),
                "run-2": SimpleNamespace(run_id="run-2", terminal=True),
                "run-3": SimpleNamespace(run_id="run-3", terminal=False),
                "run-4": SimpleNamespace(run_id="run-4", terminal=True),
            }
            existing_score = object()

            def load_run(path: Path) -> object:
                return runs[path.parent.name]

            with (
                patch.object(self.cli, "load_corpus", return_value=corpus),
                patch.object(self.cli, "load_campaign", return_value=campaign),
                patch.object(self.cli, "load_run", side_effect=load_run),
                patch.object(
                    self.cli, "load_score", return_value=existing_score
                ) as load_score,
                patch.object(self.cli, "aggregate_report") as validate,
                patch.object(self.cli, "evaluate_run") as evaluate,
            ):
                result, stdout, stderr = self._invoke(
                    [
                        "score",
                        "--campaign",
                        str(campaign_root),
                        "--corpus",
                        "private/corpus.json",
                        "--all",
                    ]
                )

            self.assertEqual(0, result)
            self.assertEqual("", stderr)
            self.assertIn(
                "2 scored, 1 existing verified, 1 nonterminal skipped", stdout
            )
            self.assertEqual(
                "fixture score placeholder\n",
                existing_path.read_text(encoding="utf-8"),
            )
            load_score.assert_called_once_with(existing_path)
            validate.assert_called_once_with(
                corpus,
                campaign,
                runs=tuple(runs.values()),
                scores=(existing_score,),
            )
            self.assertEqual(
                evaluate.call_args_list,
                [
                    call(
                        corpus,
                        campaign,
                        runs["run-2"],
                        campaign_root / "runs" / "run-2",
                        campaign_root / "scores" / "run-2",
                    ),
                    call(
                        corpus,
                        campaign,
                        runs["run-4"],
                        campaign_root / "runs" / "run-4",
                        campaign_root / "scores" / "run-4",
                    ),
                ],
            )

            parser_stderr = io.StringIO()
            with (
                contextlib.redirect_stderr(parser_stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                self.cli.main(
                    [
                        "score",
                        "--campaign",
                        str(campaign_root),
                        "--corpus",
                        "private/corpus.json",
                        "--run-id",
                        "run-1",
                        "--all",
                    ]
                )
            self.assertEqual(2, raised.exception.code)

    def test_score_all_rejects_changed_existing_source_before_new_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_root = Path(temporary) / "campaign-v1"
            score_path = campaign_root / "scores" / "run-1" / "score.json"
            score_path.parent.mkdir(parents=True)
            score_path.write_text("fixture score placeholder\n", encoding="utf-8")
            corpus = object()
            campaign = SimpleNamespace(
                runs=(
                    SimpleNamespace(run_id="run-1"),
                    SimpleNamespace(run_id="run-2"),
                )
            )
            runs = {
                "run-1": SimpleNamespace(run_id="run-1", terminal=True),
                "run-2": SimpleNamespace(run_id="run-2", terminal=True),
            }

            def load_run(path: Path) -> object:
                return runs[path.parent.name]

            with (
                patch.object(self.cli, "load_corpus", return_value=corpus),
                patch.object(self.cli, "load_campaign", return_value=campaign),
                patch.object(self.cli, "load_run", side_effect=load_run),
                patch.object(self.cli, "load_score", return_value=object()),
                patch.object(
                    self.cli,
                    "aggregate_report",
                    side_effect=self.cli.ValidationError(
                        "BoardBench score source hash or version differs"
                    ),
                ),
                patch.object(self.cli, "evaluate_run") as evaluate,
            ):
                result, _stdout, stderr = self._invoke(
                    [
                        "score",
                        "--campaign",
                        str(campaign_root),
                        "--corpus",
                        "private/corpus.json",
                        "--all",
                    ]
                )

            self.assertEqual(2, result)
            self.assertIn("source hash or version differs", stderr)
            evaluate.assert_not_called()

    def test_review_and_correction_commands_use_canonical_sources(self) -> None:
        campaign_root = Path("campaigns/campaign-v1")
        corpus = object()
        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(
                self.cli,
                "create_review_templates",
                return_value=(Path("one"), Path("two")),
            ) as templates,
        ):
            result, stdout, _stderr = self._invoke(
                [
                    "review",
                    "templates",
                    "--campaign",
                    str(campaign_root),
                    "--corpus",
                    "private/corpus.json",
                ]
            )
        self.assertEqual(0, result)
        self.assertIn("2 review templates", stdout)
        templates.assert_called_once_with(campaign_root, corpus)

        stored_review = campaign_root / "reviews" / "run-1" / "review.json"
        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(
                self.cli, "import_review", return_value=stored_review
            ) as import_review,
        ):
            result, _stdout, _stderr = self._invoke(
                [
                    "review",
                    "import",
                    "--campaign",
                    str(campaign_root),
                    "--submission",
                    "submissions/review.json",
                    "--corpus",
                    "private/corpus.json",
                ]
            )
        self.assertEqual(0, result)
        import_review.assert_called_once_with(
            campaign_root, corpus, Path("submissions/review.json")
        )

        campaign = SimpleNamespace(runs=(SimpleNamespace(run_id="run-1"),))
        capture_result = SimpleNamespace(
            record_path=campaign_root / "corrections" / "run-1" / "correction.json"
        )
        with (
            patch.object(self.cli, "load_campaign", return_value=campaign),
            patch.object(
                self.cli, "capture_correction", return_value=capture_result
            ) as capture,
        ):
            result, _stdout, _stderr = self._invoke(
                [
                    "correction",
                    "capture",
                    "--campaign",
                    str(campaign_root),
                    "--run-id",
                    "run-1",
                    "--generated",
                    "generated",
                    "--corrected",
                    "corrected",
                    "--manufacturing-candidate",
                    "candidate",
                ]
            )
        self.assertEqual(0, result)
        capture.assert_called_once_with(
            campaign_root,
            run_receipt_path=campaign_root / "runs" / "run-1" / "run.json",
            review_path=campaign_root / "reviews" / "run-1" / "review.json",
            generated_snapshot=Path("generated"),
            corrected_snapshot=Path("corrected"),
            manufacturing_candidate_snapshot=Path("candidate"),
        )

    def test_review_commands_require_an_explicit_corpus(self) -> None:
        for arguments in (
            ["review", "templates", "--campaign", "campaigns/campaign-v1"],
            [
                "review",
                "import",
                "--campaign",
                "campaigns/campaign-v1",
                "--submission",
                "submissions/review.json",
            ],
        ):
            stderr = io.StringIO()
            with (
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                self.cli.main(arguments)
            self.assertEqual(2, raised.exception.code)
            self.assertIn("--corpus", stderr.getvalue())

    def test_selection_and_hardware_import_delegate_to_evidence_owners(self) -> None:
        campaign_root = Path("campaigns/campaign-v1")
        corpus = object()
        selection_path = campaign_root / "selection.json"
        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(
                self.cli, "import_selection", return_value=selection_path
            ) as import_selection,
        ):
            result, _stdout, _stderr = self._invoke(
                [
                    "selection",
                    "import",
                    "--campaign",
                    str(campaign_root),
                    "--corpus",
                    "private/corpus.json",
                    "--submission",
                    "submissions/selection.json",
                ]
            )
        self.assertEqual(0, result)
        import_selection.assert_called_once_with(
            campaign_root, corpus, Path("submissions/selection.json")
        )

        hardware_result = SimpleNamespace(
            record_path=campaign_root / "hardware" / "run-1" / "hardware.json"
        )
        with patch.object(
            self.cli, "import_hardware", return_value=hardware_result
        ) as import_hardware:
            result, _stdout, _stderr = self._invoke(
                [
                    "hardware",
                    "import",
                    "--campaign",
                    str(campaign_root),
                    "--submission",
                    "submissions/hardware",
                    "--release-artifact",
                    "release.zip",
                ]
            )
        self.assertEqual(0, result)
        import_hardware.assert_called_once_with(
            campaign_root,
            Path("submissions/hardware"),
            release_artifact=Path("release.zip"),
        )

    def test_report_seal_and_publication_delegate_without_reaggregating_rules(
        self,
    ) -> None:
        campaign_root = Path("campaigns/campaign-v1")
        corpus = object()
        selection = object()
        evidence = SimpleNamespace(
            campaign=object(),
            runs=(object(),),
            scores=(object(),),
            reviews=(object(),),
            corrections=(object(),),
            hardware=(object(),),
            selection=selection,
        )
        report = object()
        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(
                self.cli, "load_campaign_evidence", return_value=evidence
            ) as load_evidence,
            patch.object(
                self.cli, "aggregate_report", return_value=report
            ) as aggregate,
            patch.object(
                self.cli, "write_report", return_value=Path("draft-report")
            ) as write,
        ):
            result, _stdout, _stderr = self._invoke(
                [
                    "report",
                    "--campaign",
                    str(campaign_root),
                    "--corpus",
                    "private/corpus.json",
                    "--output",
                    "draft-report",
                ]
            )
        self.assertEqual(0, result)
        load_evidence.assert_called_once_with(campaign_root, corpus)
        aggregate.assert_called_once_with(
            corpus,
            evidence.campaign,
            runs=evidence.runs,
            scores=evidence.scores,
            reviews=evidence.reviews,
            corrections=evidence.corrections,
            hardware=evidence.hardware,
            selection=selection,
        )
        write.assert_called_once_with(Path("draft-report"), report)

        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(self.cli, "seal_campaign", return_value=report) as seal,
        ):
            result, _stdout, _stderr = self._invoke(
                [
                    "seal",
                    "--campaign",
                    str(campaign_root),
                    "--corpus",
                    "private/corpus.json",
                    "--output",
                    "sealed-report",
                ]
            )
        self.assertEqual(0, result)
        seal.assert_called_once_with(
            Path("sealed-report"),
            campaign_root,
            corpus,
        )

        with (
            patch.object(self.cli, "load_corpus", return_value=corpus),
            patch.object(self.cli, "load_report", return_value=report),
            patch.object(
                self.cli,
                "create_publication_bundle",
                return_value=Path("publication"),
            ) as publish,
        ):
            result, _stdout, _stderr = self._invoke(
                [
                    "publication",
                    "bundle",
                    "--campaign",
                    str(campaign_root),
                    "--corpus",
                    "private/corpus.json",
                    "--report",
                    "sealed-report/report.json",
                    "--output",
                    "publication",
                ]
            )
        self.assertEqual(0, result)
        publish.assert_called_once_with(
            Path("publication"),
            campaign_root,
            report,
            corpus,
        )

    def test_expected_errors_use_stable_cli_exit_code(self) -> None:
        with patch.object(
            self.cli,
            "load_corpus",
            side_effect=self.cli.ValidationError("invalid BoardBench corpus"),
        ):
            result, stdout, stderr = self._invoke(
                ["corpus", "validate", "--corpus", "private/corpus.json"]
            )

        self.assertEqual(2, result)
        self.assertEqual("", stdout)
        self.assertIn("invalid BoardBench corpus", stderr)


if __name__ == "__main__":
    unittest.main()
