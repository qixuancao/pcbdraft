from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.verification.boardbench import BoardBenchCampaign, CampaignRunPlan
from pcbdraft.verification.boardbench_evaluator_v5 import (
    EVALUATOR_V5,
    load_evaluation_v5,
)
from pcbdraft.verification.boardbench_preflight import (
    FORMAL_KICAD_VERSION,
    FORMAL_MODEL,
    FORMAL_PROVIDER,
    PREFLIGHT_LIMITATIONS,
    PREFLIGHT_NAMESPACE,
    PREFLIGHT_REQUEST,
    PREFLIGHT_RUN_ID,
    PreflightArtifactRefs,
    _terminal_budgets,
    comparison_manifest_from_campaign,
    failure_topology_preflight,
    formal_comparison_launch_record,
    freeze_comparison_manifest,
    load_comparison_manifest,
    load_formal_comparison_launch,
    load_preflight_report,
    run_deterministic_preflight,
    write_formal_comparison_launch,
    write_preflight_report,
)
from pcbdraft.verification.boardbench_v2 import (
    MODEL_TURN_LIMIT,
    PCB_TOOL_CALL_LIMIT,
    RUN_V2_SCHEMA,
    WALL_TIME_LIMIT_SECONDS,
    load_run_v2,
)
from pcbdraft.verification.gates import find_kicad_cli

NOW = "2026-08-23T12:00:00Z"
HASH = "a" * 64


def _source_campaign() -> BoardBenchCampaign:
    cases = tuple(f"tier-a-case-{index:02d}" for index in range(1, 21))
    runs = tuple(
        CampaignRunPlan(case_id, repetition, f"{case_id}-run-{repetition}")
        for case_id in cases
        for repetition in range(1, 4)
    )
    return BoardBenchCampaign(
        campaign_id="read-only-tier-a-source",
        cohort="ai_reviewed_pilot",
        corpus_id="temporary-tier-a-fixture",
        corpus_sha256=HASH,
        created_at=NOW,
        pcbdraft_commit="b" * 40,
        dirty_state_sha256="c" * 64,
        provider=FORMAL_PROVIDER,
        model=FORMAL_MODEL,
        configuration_sha256="d" * 64,
        kicad_version=FORMAL_KICAD_VERSION,
        python_version="3.13.14",
        platform="Linux-test",
        tool_registry_sha256="e" * 64,
        wall_timeout_seconds=WALL_TIME_LIMIT_SECONDS,
        tool_call_budget=PCB_TOOL_CALL_LIMIT,
        repetitions=3,
        evaluator_version="boardbench-evaluator-v4",
        runs=runs,
    )


class ComparisonCampaignPreflightTests(unittest.TestCase):
    def test_preflight_rejects_non_numeric_or_non_finite_timeouts(self) -> None:
        for timeout in ("90", float("nan"), float("inf")):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(ValidationError, "timeout"),
            ):
                run_deterministic_preflight(
                    Path("unused"),
                    timeout=timeout,  # type: ignore[arg-type]
                )

    def test_preflight_exact_hard_limits_are_not_exhausted(self) -> None:
        exact = {
            item.name: item
            for item in _terminal_budgets(
                model_decisions=MODEL_TURN_LIMIT,
                pcb_tool_calls=PCB_TOOL_CALL_LIMIT,
                wall_seconds=WALL_TIME_LIMIT_SECONDS,
            )
        }
        self.assertEqual(exact["model_turns"].status, "within_limit")
        self.assertEqual(exact["pcb_tool_calls"].status, "within_limit")
        self.assertEqual(exact["wall_time"].status, "within_limit")

    def test_manifest_freezes_exact_20x3_without_preflight_replacements(self) -> None:
        manifest = comparison_manifest_from_campaign(
            _source_campaign(),
            manifest_id="comparison-plan-v1",
            created_at=NOW,
        )
        self.assertEqual(len(manifest.planned_runs), 60)
        self.assertEqual(len({run.run_id for run in manifest.planned_runs}), 60)
        self.assertEqual(
            {(run.case_id, run.repetition) for run in manifest.planned_runs},
            {
                (f"tier-a-case-{index:02d}", repetition)
                for index in range(1, 21)
                for repetition in range(1, 4)
            },
        )
        self.assertNotIn(
            PREFLIGHT_RUN_ID, {run.run_id for run in manifest.planned_runs}
        )
        self.assertEqual(manifest.preflight_namespace, PREFLIGHT_NAMESPACE)
        self.assertFalse(manifest.formal_campaign_started)
        self.assertFalse(manifest.execution_authorized)
        self.assertFalse(manifest.human_review_claimed)
        self.assertFalse(manifest.physical_evidence_claimed)
        with self.assertRaisesRegex(ValidationError, "flags"):
            replace(manifest, execution_authorized=0)  # type: ignore[arg-type]
        limits = {item.name: item.limit for item in manifest.budgets}
        self.assertEqual(limits["model_turns"], MODEL_TURN_LIMIT)
        self.assertEqual(limits["pcb_tool_calls"], PCB_TOOL_CALL_LIMIT)
        self.assertEqual(limits["wall_time"], WALL_TIME_LIMIT_SECONDS)
        self.assertIsNone(limits["route_attempts"])
        self.assertIsNone(limits["route_node_expansions"])
        self.assertIsNone(limits["uncached_input_tokens"])
        self.assertEqual(manifest.run_schema, RUN_V2_SCHEMA)
        self.assertEqual(manifest.evaluator_version, EVALUATOR_V5)
        replacement = replace(manifest.planned_runs[0], run_id=PREFLIGHT_RUN_ID)
        with self.assertRaisesRegex(ValidationError, "preflight namespace"):
            replace(
                manifest,
                planned_runs=(replacement, *manifest.planned_runs[1:]),
            )
        exact_namespace = replace(manifest.planned_runs[0], run_id=PREFLIGHT_NAMESPACE)
        with self.assertRaisesRegex(ValidationError, "preflight namespace"):
            replace(
                manifest,
                planned_runs=(exact_namespace, *manifest.planned_runs[1:]),
            )
        first_case_id = manifest.planned_runs[0].case_id
        preflight_case_runs = tuple(
            replace(run, case_id=f"{PREFLIGHT_NAMESPACE}-fixture")
            if run.case_id == first_case_id
            else run
            for run in manifest.planned_runs
        )
        with self.assertRaisesRegex(ValidationError, "preflight namespace"):
            replace(manifest, planned_runs=preflight_case_runs)

    def test_manifest_writer_is_fresh_and_rejects_drift_and_unsafe_paths(self) -> None:
        manifest = comparison_manifest_from_campaign(
            _source_campaign(),
            manifest_id="comparison-plan-v1",
            created_at=NOW,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = freeze_comparison_manifest(root / "evidence", manifest)
            self.assertEqual(load_comparison_manifest(path), manifest)
            with self.assertRaisesRegex(ValidationError, "overwrite rejected"):
                freeze_comparison_manifest(root / "evidence", manifest)
            drift = replace(manifest, created_at="2026-08-23T12:00:01Z")
            with self.assertRaisesRegex(ValidationError, "configuration drift"):
                freeze_comparison_manifest(root / "evidence", drift)
            with self.assertRaisesRegex(ValidationError, "unsafe"):
                freeze_comparison_manifest(root / "safe" / ".." / "escape", manifest)
            with self.assertRaisesRegex(ValidationError, "unsafe"):
                freeze_comparison_manifest(f"{root / 'unsafe'}\x00suffix", manifest)
            symlink = root / "linked"
            symlink.symlink_to(root / "actual", target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "symbolic link"):
                freeze_comparison_manifest(symlink / "evidence", manifest)

    def test_source_campaign_drift_fails_before_a_manifest_is_created(self) -> None:
        source = _source_campaign()
        for changed, message in (
            (
                replace(source, cohort="public_corpus_rerun"),
                "frozen AI-reviewed pilot",
            ),
            (
                replace(source, evaluator_version="boardbench-evaluator-v3"),
                "retain evaluator v4",
            ),
            (replace(source, model="another-model"), "frozen Luna"),
            (replace(source, kicad_version="11.0.0"), "KiCad 10.0.5"),
            (replace(source, tool_call_budget=499), "differs from 500"),
            (replace(source, wall_timeout_seconds=3599.0), "differs from 3600"),
        ):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValidationError, message),
            ):
                comparison_manifest_from_campaign(
                    changed,
                    manifest_id="comparison-plan-v1",
                    created_at=NOW,
                )

    def test_formal_launch_binds_v4_base_to_independent_v5_comparison(self) -> None:
        manifest = comparison_manifest_from_campaign(
            _source_campaign(),
            manifest_id="comparison-plan-v1",
            created_at=NOW,
        )
        campaign = replace(
            _source_campaign(),
            campaign_id="formal-campaign-v1",
            tool_registry_sha256=manifest.tool_registry_sha256,
        )
        record = formal_comparison_launch_record(
            manifest,
            campaign,
            authorized_at="2026-08-24T02:00:00Z",
        )

        self.assertEqual(record.legacy_evaluator_version, "boardbench-evaluator-v4")
        self.assertEqual(record.comparison_evaluator_version, EVALUATOR_V5)
        self.assertTrue(record.execution_authorized)
        self.assertFalse(record.formal_campaign_started)
        self.assertFalse(record.human_review_claimed)
        self.assertFalse(record.physical_evidence_claimed)
        self.assertFalse(manifest.execution_authorized)
        self.assertFalse(manifest.formal_campaign_started)
        with self.assertRaisesRegex(ValidationError, "flags"):
            replace(record, execution_authorized=1)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = write_formal_comparison_launch(root, record)
            self.assertEqual(load_formal_comparison_launch(path), record)
            with self.assertRaisesRegex(ValidationError, "overwrite rejected"):
                write_formal_comparison_launch(root, record)

        with self.assertRaisesRegex(ValidationError, "retain evaluator v4"):
            formal_comparison_launch_record(
                manifest,
                replace(campaign, evaluator_version=EVALUATOR_V5),
                authorized_at="2026-08-24T02:00:00Z",
            )
        changed_runs = (
            replace(campaign.runs[0], run_id="replacement-run"),
            *campaign.runs[1:],
        )
        with self.assertRaisesRegex(ValidationError, "frozen comparison"):
            formal_comparison_launch_record(
                manifest,
                replace(campaign, runs=changed_runs),
                authorized_at="2026-08-24T02:00:00Z",
            )

        malformed_shape = tuple(
            replace(run, case_id="single-case", run_id=f"replacement-{index}")
            for index, run in enumerate(record.planned_runs)
        )
        with self.assertRaisesRegex(ValidationError, "exact 20 x 3"):
            replace(record, planned_runs=malformed_shape)
        preflight_run = replace(
            record.planned_runs[0], run_id=f"{PREFLIGHT_NAMESPACE}-formal-run"
        )
        with self.assertRaisesRegex(ValidationError, "preflight namespace"):
            replace(record, planned_runs=(preflight_run, *record.planned_runs[1:]))
        with self.assertRaisesRegex(ValidationError, "timestamp"):
            replace(record, authorized_at="2026-02-31T00:00:00Z")

    def test_old_failure_topologies_have_stable_codes_and_stop_semantics(self) -> None:
        results = failure_topology_preflight()
        self.assertEqual(
            {item.code for item in results},
            {
                "invalid_seed",
                "zero_length_seed",
                "native_zero_copper",
                "native_connectivity_failed",
                "unintended_net_merge",
                "routed_footprint_transform_unsupported",
                "repeated_retry_key",
            },
        )
        repeated = next(item for item in results if item.code == "repeated_retry_key")
        self.assertEqual(repeated.classification, "convergence_guard")
        self.assertEqual(repeated.termination_reason, "strategy_required")
        self.assertEqual(
            {
                item.termination_reason
                for item in results
                if item.code != "repeated_retry_key"
            },
            {"tool_failure"},
        )
        self.assertEqual(
            {
                item.observation_mode
                for item in results
                if item.classification in {"typed_route_failure", "convergence_guard"}
            },
            {"exercised"},
        )
        self.assertEqual(
            {
                item.observation_mode
                for item in results
                if item.classification == "native_postcondition"
            },
            {"typed_classification_summary"},
        )

    def test_preflight_artifact_references_are_always_relative(self) -> None:
        for unsafe in ("../old/terminal.json", "..\\old\\terminal.json"):
            with (
                self.subTest(unsafe=unsafe),
                self.assertRaisesRegex(ValidationError, "unsafe"),
            ):
                PreflightArtifactRefs(
                    project="repository/project",
                    product_terminal=unsafe,
                    run_v2="run-v2.json",
                    evaluation_v5="evaluator-v5/run/evaluation.json",
                )

    def test_deterministic_namespace_rejects_a_symlink_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = root / "evidence"
            output.mkdir()
            actual = root / "actual"
            actual.mkdir()
            (output / PREFLIGHT_NAMESPACE).symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "symbolic link"):
                run_deterministic_preflight(output)


class DeterministicPreflightIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(find_kicad_cli(), "kicad-cli is unavailable")
    def test_natural_language_to_native_terminal_v2_and_v5(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sentinel = root / "historical-artifact.json"
            sentinel.write_text('{"immutable":true}\n', encoding="utf-8")

            result = run_deterministic_preflight(root / "new-evidence")

            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), '{"immutable":true}\n'
            )
            self.assertTrue(result.provider_inputs)
            self.assertEqual(set(result.provider_inputs), {PREFLIGHT_REQUEST})
            self.assertFalse(PREFLIGHT_REQUEST.lstrip().startswith(("{", "[")))
            self.assertEqual(result.report.run_id, PREFLIGHT_RUN_ID)
            self.assertTrue(result.report.native_consistency.consistency_passed)
            self.assertEqual(
                {Path(name).suffix for name in result.report.native_project_files},
                {".kicad_pro", ".kicad_sch", ".kicad_pcb"},
            )
            self.assertEqual(result.run_v2.outcome_source, "product_session_terminal")
            self.assertEqual(
                result.run_v2.task_outcome,
                result.report.product_terminal.task_outcome,
            )
            self.assertEqual(
                result.run_v2.termination_reason,
                result.report.product_terminal.termination_reason,
            )
            self.assertEqual(load_run_v2(result.run_v2_path), result.run_v2)
            self.assertEqual(
                load_evaluation_v5(result.evaluation_v5_path), result.evaluation_v5
            )
            self.assertEqual(load_preflight_report(result.report_path), result.report)
            self.assertEqual(result.evaluation_v5.delivery_readiness.state, "unknown")
            self.assertEqual(result.evaluation_v5.design_intent.state, "unknown")
            self.assertEqual(result.evaluation_v5.native_artifact.state, "pass")
            self.assertTrue(result.report.product_terminal.release_gate_passed)
            self.assertFalse(result.report.formal_campaign_started)
            self.assertFalse(result.report.human_review_claimed)
            self.assertFalse(result.report.physical_evidence_claimed)
            self.assertEqual(result.report.limitations, PREFLIGHT_LIMITATIONS)
            self.assertEqual(
                tuple(item.name for item in result.report.checks), ("erc", "drc")
            )
            self.assertNotEqual(
                {item.state for item in result.report.checks}, {"unknown"}
            )
            self.assertFalse(
                any(
                    run_id == PREFLIGHT_RUN_ID
                    for run_id in (
                        run.run_id
                        for run in comparison_manifest_from_campaign(
                            _source_campaign(),
                            manifest_id="comparison-plan-v1",
                            created_at=NOW,
                        ).planned_runs
                    )
                )
            )

            with self.assertRaisesRegex(ValidationError, "overwrite rejected"):
                write_preflight_report(result.root, result.report)
            drift = replace(result.report, generated_at="2026-08-23T23:59:59Z")
            with self.assertRaisesRegex(ValidationError, "configuration drift"):
                write_preflight_report(result.root, drift)
            false_design_claim = replace(
                result.report.evaluation_v5,
                design_intent="pass",
                legacy_overall_projection="pass",
            )
            with self.assertRaisesRegex(
                ValidationError, "cannot claim design or delivery readiness"
            ):
                replace(result.report, evaluation_v5=false_design_claim)
            with self.assertRaisesRegex(ValidationError, "unsafe"):
                write_preflight_report(root / "safe" / ".." / "escape", result.report)
            symlink = root / "report-link"
            symlink.symlink_to(root / "real-report-root", target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "symbolic link"):
                write_preflight_report(symlink, result.report)


if __name__ == "__main__":
    unittest.main()
