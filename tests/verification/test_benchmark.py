from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.verification.benchmark import (
    _select_model_cases,
    load_corpus,
    run_benchmark,
)


class IndependentBenchmarkTests(unittest.TestCase):
    def test_bundled_corpus_is_license_clear_balanced_and_substantial(self) -> None:
        document, cases = load_corpus()
        self.assertEqual(document["license"], "CC0-1.0")
        self.assertEqual(len(cases), 90)
        self.assertEqual(sum(case.label == "fault" for case in cases), 64)
        self.assertEqual(sum(case.label == "clean" for case in cases), 26)
        self.assertGreaterEqual(len({case.category for case in cases}), 15)
        self.assertIn("No competitor fixture", document["methodology"])
        self.assertTrue(
            all(case.expected_codes for case in cases if case.label == "fault")
        )
        model_cases = _select_model_cases(cases)
        self.assertEqual(len(model_cases), 24)
        self.assertEqual(sum(case.label == "fault" for case in model_cases), 16)
        self.assertEqual(sum(case.label == "clean" for case in model_cases), 8)

    def test_full_corpus_measures_detection_repairs_repeatability_and_latency(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "benchmark.json"
            result = run_benchmark(output, repetitions=3)
            metrics = result.result["metrics"]
            self.assertEqual(
                metrics["confusion_matrix"],
                {
                    "true_positive": 64,
                    "false_negative": 0,
                    "false_positive": 0,
                    "true_negative": 26,
                },
            )
            self.assertEqual(metrics["detection"]["targeted_code_recall"], 1.0)
            self.assertEqual(metrics["repair"]["eligible"], 63)
            self.assertEqual(metrics["repair"]["success_rate"], 1.0)
            self.assertEqual(metrics["repair"]["introduced_regression_cases"], 0)
            self.assertEqual(metrics["repeatability"]["rate"], 1.0)
            self.assertEqual(metrics["latency"]["samples"], 270)
            self.assertGreater(metrics["latency"]["mean_ms"], 0)
            self.assertEqual(result.result["model_consistency"]["state"], "unavailable")
            self.assertTrue(output.is_file())

    def test_output_is_create_only_and_argument_bounds_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "benchmark.json"
            output.write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "already exists"):
                run_benchmark(output)
            with self.assertRaisesRegex(ValidationError, "repetitions"):
                run_benchmark(Path(temporary) / "other.json", repetitions=1)
            with self.assertRaisesRegex(ValidationError, "model_runs"):
                run_benchmark(Path(temporary) / "third.json", model_runs=1)

    def test_missing_model_is_reported_without_fabricating_consistency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "benchmark.json"
            with patch(
                "pcbdraft.verification.benchmark.HermesIntentProvider.from_config",
                return_value=None,
            ):
                result = run_benchmark(output, repetitions=2, model_runs=2)
            model = result.result["model_consistency"]
            self.assertEqual(model["state"], "unavailable")
            self.assertEqual(model["completed_runs"], 0)
            self.assertTrue(
                model["deterministic_results_are_not_reported_as_model_results"]
            )


if __name__ == "__main__":
    unittest.main()
