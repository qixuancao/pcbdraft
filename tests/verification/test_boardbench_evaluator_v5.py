from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_text
from pcbdraft.core.process import CommandResult
from pcbdraft.kicad.routing import RoutingFailure
from pcbdraft.services.progress import (
    EngineeringStage,
    MetricValue,
    ProcessStatus,
    ProgressVector,
    TaskOutcome,
)
from pcbdraft.verification.boardbench import (
    AUTOMATIC_METRICS,
    BoardBenchHardware,
    BoardBenchReview,
    BoardBenchScore,
    EfficiencyMetrics,
    InventoryEntry,
    MetricResult,
    RailMeasurement,
    ToolCallCount,
)
from pcbdraft.verification.boardbench_evaluator_v5 import (
    BoardBenchComparisonV5,
    BoardBenchEvaluationV5,
    CausalAttribution,
    CausalSignal,
    ConnectorSlotPermutation,
    ElectricalEndpoint,
    ElectricalEquivalencePolicy,
    ElectricalGraph,
    ElectricalGraphComparison,
    EndpointState,
    EvaluationLayer,
    EvidenceFinding,
    LayerState,
    NativeMetricEvidence,
    SeriesTopology,
    TwoTerminalSymmetry,
    UnspecifiedPinOrder,
    compare_electrical_graphs,
    compare_evaluations_v5,
    derive_causal_attribution,
    evaluation_v5_from_legacy_score,
    load_comparison_v5,
    load_evaluation_v5,
    write_comparison_v5,
    write_evaluation_v5,
)
from pcbdraft.verification.boardbench_v2 import NormalizedBoardBenchRun
from pcbdraft.verification.gates import run_gate

NOW = "2026-08-23T12:00:00Z"
HASH = "a" * 64


def _graph(
    rows: Mapping[str, tuple[str, str | None]],
) -> ElectricalGraph:
    return ElectricalGraph.build(
        ElectricalEndpoint(endpoint, cast(EndpointState, state), net)
        for endpoint, (state, net) in rows.items()
    )


def _normalized_run(
    *,
    campaign_id: str = "campaign-old",
    run_id: str = "run-1",
    stage: EngineeringStage | None = EngineeringStage.ROUTING,
    reason: str | None = "agent_returned_before_gate",
    release_gate_passed: bool | None = False,
) -> NormalizedBoardBenchRun:
    return NormalizedBoardBenchRun(
        campaign_id,
        run_id,
        "case-1",
        1,
        "pcbdraft-boardbench-run-v2",
        2,
        ProcessStatus.EXITED,
        TaskOutcome.INCOMPLETE,
        reason,
        stage,
        release_gate_passed,
        (),
    )


def _efficiency() -> EfficiencyMetrics:
    return EfficiencyMetrics(
        model_requests=2,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        total_tokens=150,
        token_status="reported",  # noqa: S106 - evidence state
        token_source="provider_usage",  # noqa: S106 - evidence provenance
        cost_amount=None,
        cost_currency=None,
        cost_status="subscription_included",
        cost_source="subscription_contract",
        pcb_tool_calls=1,
        tool_call_counts=(ToolCallCount("pcb_run_drc", "completed", 1),),
        provider_retries=0,
        provider_errors=0,
        tool_seconds=1.0,
        api_seconds=2.0,
        wall_seconds=10.0,
        failure_reason=None,
    )


def _score(
    *, campaign_id: str = "campaign-old", run_id: str = "run-1"
) -> BoardBenchScore:
    return BoardBenchScore(
        campaign_id,
        run_id,
        HASH,
        HASH,
        HASH,
        "boardbench-evaluator-v4",
        NOW,
        "pass",
        tuple(MetricResult(name, "pass", "fixture pass") for name in AUTOMATIC_METRICS),
        _efficiency(),
        None,
    )


def _native_metrics(
    *, erc_errors: int = 0, drc_errors: int = 0
) -> tuple[NativeMetricEvidence, ...]:
    commands = {
        "erc": (
            "kicad-cli",
            "sch",
            "erc",
            "--format",
            "json",
            "--severity-error",
            "--severity-warning",
            "--output",
            "erc.json",
            "design.kicad_sch",
        ),
        "drc": (
            "kicad-cli",
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-error",
            "--severity-warning",
            "--output",
            "drc.json",
            "design.kicad_pcb",
        ),
    }
    return (
        NativeMetricEvidence("complete_project", "pass", None, "fixture"),
        NativeMetricEvidence("library_resolution", "pass", None, "fixture"),
        NativeMetricEvidence("native_consistency", "pass", 0, "fixture"),
        NativeMetricEvidence("unresolved_connections", "pass", 0, "fixture"),
        NativeMetricEvidence(
            "erc",
            "pass" if erc_errors == 0 else "fail",
            erc_errors,
            "fixture",
            commands["erc"],
        ),
        NativeMetricEvidence(
            "drc",
            "pass" if drc_errors == 0 else "fail",
            drc_errors,
            "fixture",
            commands["drc"],
        ),
    )


def _hardware(**changes: object) -> BoardBenchHardware:
    hardware = BoardBenchHardware(
        campaign_id="campaign-old",
        run_id="run-1",
        source_kind="release",
        source_artifact_sha256="b" * 64,
        category="mcu_minimum_system",
        board_revision="A",
        board_serial="fixture-1",
        revision_count=1,
        fabricator="Fixture board house",
        operator="engineer-1",
        observed_at=NOW,
        fabricator_accepted="pass",
        solderability="pass",
        first_power_no_short="pass",
        firmware_download="pass",
        core_function="pass",
        rails=(RailMeasurement("3V3", "V", 3.2, 3.4, 3.3, "pass"),),
        notes="Current-limited fixture evidence.",
        attachments=(InventoryEntry("lab/measurement.txt", 12, "c" * 64),),
    )
    return replace(hardware, **changes)


def _evaluation(
    *,
    campaign_id: str = "campaign-old",
    run_id: str = "run-1",
    erc_errors: int = 0,
    drc_errors: int = 0,
) -> BoardBenchEvaluationV5:
    metrics = _native_metrics(erc_errors=erc_errors, drc_errors=drc_errors)
    native_sources = (
        "native_project_parse",
        "native_library_resolution",
        "native_consistency",
        "native_connectivity",
        "kicad_erc",
        "kicad_drc",
    )
    native = EvaluationLayer.build(
        "native_artifact",
        [
            EvidenceFinding(
                source,
                "automatic",
                metric.state,
                f"{metric.name}:{metric.source}",
            )
            for source, metric in zip(native_sources, metrics, strict=True)
        ],
    )
    design = EvaluationLayer.build(
        "design_intent",
        [EvidenceFinding("automatic_topology", "automatic", "pass", "topology")],
    )
    delivery = EvaluationLayer.build(
        "delivery_readiness",
        [
            EvidenceFinding(
                "unavailable",
                "automatic",
                "unknown",
                "human and physical evidence unavailable",
            )
        ],
    )
    projection: LayerState = "fail" if native.state == "fail" else "pass"
    return BoardBenchEvaluationV5(
        campaign_id,
        run_id,
        "case-1",
        1,
        "pcbdraft-boardbench-run-v2",
        2,
        "boardbench-evaluator-v4",
        "pass",
        NOW,
        design,
        native,
        delivery,
        projection,
        metrics,
        CausalAttribution(None, EngineeringStage.ERC_DRC.value, (), ()),
    )


class EvaluationLayerTests(unittest.TestCase):
    def test_three_layer_round_trip_and_strict_combinations(self) -> None:
        value = _evaluation()
        self.assertEqual(BoardBenchEvaluationV5.from_dict(value.to_dict()), value)
        self.assertEqual(value.design_intent.state, "pass")
        self.assertEqual(value.native_artifact.state, "pass")
        self.assertEqual(value.delivery_readiness.state, "unknown")
        with self.assertRaisesRegex(ValidationError, "contradicts"):
            replace(value.delivery_readiness, state="pass")
        with self.assertRaisesRegex(ValidationError, "legacy projection"):
            replace(value, legacy_overall_projection="unknown")
        with self.assertRaisesRegex(ValidationError, "source/kind"):
            EvidenceFinding(
                "human_engineer_orderability",
                "ai_review",
                "pass",
                "not actually a human review",
            )
        with self.assertRaisesRegex(ValidationError, "contradicts its count"):
            NativeMetricEvidence("drc", "pass", 2, "impossible fixture")

    def test_layer_reasons_and_unavailable_evidence_cannot_claim_success(self) -> None:
        evidence = EvidenceFinding(
            "automatic_topology", "automatic", "fail", "reference graph differs"
        )
        with self.assertRaisesRegex(ValidationError, "reasons contradict"):
            EvaluationLayer(
                "design_intent",
                "fail",
                (evidence,),
                ("different reason that hides the evidence",),
            )
        with self.assertRaisesRegex(ValidationError, "must remain unknown"):
            EvidenceFinding("unavailable", "automatic", "pass", "missing evidence")

    def test_ai_only_delivery_is_unknown_and_explicitly_labeled(self) -> None:
        evaluation = evaluation_v5_from_legacy_score(
            _score(),
            _normalized_run(),
            evaluated_at=NOW,
            native_metrics=_native_metrics(),
            ai_functional_state="pass",
            ai_orderability_state="pass",
        )
        self.assertEqual(evaluation.delivery_readiness.state, "unknown")
        evidence = evaluation.delivery_readiness.evidence[0]
        self.assertEqual(evidence.kind, "ai_review")
        self.assertIn("not human evidence", evidence.reason)

    def test_human_or_complete_physical_evidence_can_qualify_delivery(self) -> None:
        human = EvaluationLayer.build(
            "delivery_readiness",
            [
                EvidenceFinding(
                    "human_engineer_orderability",
                    "human_engineer",
                    "pass",
                    "engineer approved orderability",
                )
            ],
        )
        self.assertEqual(human.state, "pass")

        physical = EvaluationLayer.build(
            "delivery_readiness",
            [
                EvidenceFinding(source, "physical", "pass", source)
                for source in (
                    "physical_manufacturing",
                    "physical_assembly",
                    "physical_first_power",
                    "physical_power_rails",
                    "physical_core_function",
                )
            ],
        )
        self.assertEqual(physical.state, "pass")
        partial = EvaluationLayer.build(
            "delivery_readiness",
            [
                EvidenceFinding(
                    "physical_manufacturing",
                    "physical",
                    "pass",
                    "fabricator accepted only",
                )
            ],
        )
        self.assertEqual(partial.state, "unknown")
        failed = EvaluationLayer.build(
            "delivery_readiness",
            [
                EvidenceFinding(
                    "human_engineer_orderability",
                    "human_engineer",
                    "fail",
                    "engineer found an order blocker",
                )
            ],
        )
        self.assertEqual(failed.state, "fail")

        contradictory = EvaluationLayer.build(
            "delivery_readiness",
            [
                EvidenceFinding(
                    "human_engineer_orderability",
                    "human_engineer",
                    "pass",
                    "engineer approved orderability",
                ),
                EvidenceFinding(
                    "physical_first_power",
                    "physical",
                    "fail",
                    "first power shorted",
                ),
            ],
        )
        self.assertEqual(contradictory.state, "fail")

        ambiguous_physical = EvaluationLayer.build(
            "delivery_readiness",
            [
                *[
                    EvidenceFinding(source, "physical", "pass", source)
                    for source in (
                        "physical_manufacturing",
                        "physical_assembly",
                        "physical_first_power",
                        "physical_power_rails",
                        "physical_core_function",
                    )
                ],
                EvidenceFinding(
                    "physical_firmware",
                    "physical",
                    "unknown",
                    "firmware evidence is ambiguous",
                ),
            ],
        )
        self.assertEqual(ambiguous_physical.state, "unknown")

    def test_native_fail_or_unknown_cannot_be_projected_as_pass(self) -> None:
        unknown_metrics = list(_native_metrics())
        unknown_metrics[-1] = NativeMetricEvidence(
            "drc", "unknown", None, "DRC evidence unavailable"
        )
        unknown = evaluation_v5_from_legacy_score(
            _score(),
            _normalized_run(),
            evaluated_at=NOW,
            native_metrics=unknown_metrics,
        )
        self.assertEqual(unknown.native_artifact.state, "unknown")
        self.assertEqual(unknown.legacy_overall_projection, "unknown")

        failed = evaluation_v5_from_legacy_score(
            _score(),
            _normalized_run(),
            evaluated_at=NOW,
            native_metrics=_native_metrics(drc_errors=1),
        )
        self.assertEqual(failed.native_artifact.state, "fail")
        self.assertEqual(failed.legacy_overall_projection, "fail")

    def test_adapters_preserve_run_case_repetition_and_reject_cross_binding(
        self,
    ) -> None:
        run = replace(_normalized_run(), case_id="case-17", repetition=3)
        evaluation = evaluation_v5_from_legacy_score(
            _score(), run, evaluated_at=NOW, native_metrics=_native_metrics()
        )
        self.assertEqual(evaluation.case_id, "case-17")
        self.assertEqual(evaluation.repetition, 3)

        with self.assertRaisesRegex(ValidationError, "repetition"):
            evaluation_v5_from_legacy_score(
                _score(),
                replace(run, repetition=4),
                evaluated_at=NOW,
                native_metrics=_native_metrics(),
            )

        score = _score()
        mismatched_review = cast(
            BoardBenchReview,
            SimpleNamespace(
                campaign_id=score.campaign_id,
                run_id=score.run_id,
                source_campaign_sha256=score.source_campaign_sha256,
                source_case_sha256="b" * 64,
                source_run_sha256=score.source_run_sha256,
                source_score_sha256=None,
            ),
        )
        with self.assertRaisesRegex(ValidationError, "review source binding"):
            evaluation_v5_from_legacy_score(
                score,
                _normalized_run(),
                evaluated_at=NOW,
                native_metrics=_native_metrics(),
                human_review=mismatched_review,
            )

        mismatched_hardware = cast(
            BoardBenchHardware,
            SimpleNamespace(campaign_id="other-campaign", run_id=score.run_id),
        )
        with self.assertRaisesRegex(ValidationError, "hardware identity"):
            evaluation_v5_from_legacy_score(
                score,
                _normalized_run(),
                evaluated_at=NOW,
                native_metrics=_native_metrics(),
                hardware=mismatched_hardware,
            )

    def test_hardware_adapter_requires_complete_unambiguous_evidence(self) -> None:
        passing = evaluation_v5_from_legacy_score(
            _score(),
            _normalized_run(),
            evaluated_at=NOW,
            native_metrics=_native_metrics(),
            hardware=_hardware(),
        )
        self.assertEqual(passing.delivery_readiness.state, "pass")

        ambiguous = evaluation_v5_from_legacy_score(
            _score(),
            _normalized_run(),
            evaluated_at=NOW,
            native_metrics=_native_metrics(),
            hardware=_hardware(firmware_download="not_tested"),
        )
        self.assertEqual(ambiguous.delivery_readiness.state, "unknown")

        failed = evaluation_v5_from_legacy_score(
            _score(),
            _normalized_run(),
            evaluated_at=NOW,
            native_metrics=_native_metrics(),
            hardware=_hardware(first_power_no_short="fail"),
        )
        self.assertEqual(failed.delivery_readiness.state, "fail")


class ElectricalEquivalenceTests(unittest.TestCase):
    @staticmethod
    def _two_terminal(*, swapped: bool) -> ElectricalGraph:
        left_pin, right_pin = ("2", "1") if swapped else ("1", "2")
        return _graph(
            {
                "left.out": ("connected", "left-net"),
                f"part.{left_pin}": ("connected", "left-net"),
                f"part.{right_pin}": ("connected", "right-net"),
                "right.in": ("connected", "right-net"),
            }
        )

    def test_only_declared_resistor_or_capacitor_swap_passes(self) -> None:
        reference = self._two_terminal(swapped=False)
        observed = self._two_terminal(swapped=True)
        self.assertEqual(compare_electrical_graphs(reference, observed).state, "fail")
        for declared_kind in ("resistor", "capacitor"):
            with self.subTest(declared_kind=declared_kind):
                policy = ElectricalEquivalencePolicy(
                    symmetric_two_terminal=(TwoTerminalSymmetry("part", ("1", "2")),)
                )
                self.assertEqual(
                    compare_electrical_graphs(reference, observed, policy).state,
                    "pass",
                )

    def test_polarized_or_undeclared_two_terminal_swap_fails(self) -> None:
        reference = self._two_terminal(swapped=False)
        observed = self._two_terminal(swapped=True)
        result = compare_electrical_graphs(reference, observed)
        self.assertEqual(result.state, "fail")
        self.assertTrue(
            any(reason.startswith("open_circuit:") for reason in result.reasons)
        )

    def test_declared_series_topology_ignores_intermediate_names_and_order(
        self,
    ) -> None:
        reference = _graph(
            {
                "source.out": ("connected", "n0"),
                "r1.1": ("connected", "n0"),
                "r1.2": ("connected", "n1"),
                "r2.1": ("connected", "n1"),
                "r2.2": ("connected", "n2"),
                "sink.in": ("connected", "n2"),
            }
        )
        observed = _graph(
            {
                "source.out": ("connected", "left"),
                "r2.1": ("connected", "left"),
                "r2.2": ("connected", "middle-renamed"),
                "r1.1": ("connected", "middle-renamed"),
                "r1.2": ("connected", "right"),
                "sink.in": ("connected", "right"),
            }
        )
        self.assertEqual(compare_electrical_graphs(reference, observed).state, "fail")
        policy = ElectricalEquivalencePolicy(
            series_topologies=(SeriesTopology(("r1", "r2")),)
        )
        self.assertEqual(
            ElectricalEquivalencePolicy.from_dict(policy.to_dict()), policy
        )
        self.assertEqual(
            compare_electrical_graphs(reference, observed, policy).state, "pass"
        )

        faults = (
            _graph(
                {
                    "source.out": ("connected", "left"),
                    "r1.1": ("connected", "left"),
                    "r1.2": ("connected", "right"),
                    "r2.1": ("connected", "right"),
                    "r2.2": ("connected", "right"),
                    "sink.in": ("connected", "right"),
                }
            ),
            _graph(
                {
                    "source.out": ("connected", "left"),
                    "r1.1": ("connected", "left"),
                    "r1.2": ("connected", "middle"),
                    "r2.1": ("connected", "middle"),
                    "r2.2": ("connected", "wrong-external"),
                    "sink.in": ("connected", "right"),
                }
            ),
        )
        for fault in faults:
            with self.subTest(fault=fault.to_dict()):
                self.assertEqual(
                    compare_electrical_graphs(reference, fault, policy).state,
                    "fail",
                )

    def test_declared_same_model_connector_slots_may_permute(self) -> None:
        reference = _graph(
            {
                "left.out": ("connected", "left"),
                "j1.1": ("connected", "left"),
                "right.out": ("connected", "right"),
                "j2.1": ("connected", "right"),
            }
        )
        observed = _graph(
            {
                "left.out": ("connected", "left"),
                "j2.1": ("connected", "left"),
                "right.out": ("connected", "right"),
                "j1.1": ("connected", "right"),
            }
        )
        policy = ElectricalEquivalencePolicy(
            connector_slot_permutations=(
                ConnectorSlotPermutation("same-header-model", ("j1", "j2")),
            )
        )
        self.assertEqual(
            compare_electrical_graphs(reference, observed, policy).state, "pass"
        )

    def test_connector_permutation_is_bijective_and_does_not_relax_pin_order(
        self,
    ) -> None:
        reference = _graph(
            {
                "left.out": ("connected", "left"),
                "left.return": ("connected", "left-return"),
                "j1.1": ("connected", "left"),
                "j1.2": ("connected", "left-return"),
                "right.out": ("connected", "right"),
                "right.return": ("connected", "right-return"),
                "j2.1": ("connected", "right"),
                "j2.2": ("connected", "right-return"),
            }
        )
        policy = ElectricalEquivalencePolicy(
            connector_slot_permutations=(
                ConnectorSlotPermutation("same-header-model", ("j1", "j2")),
            )
        )
        pin_swapped = _graph(
            {
                "left.out": ("connected", "left"),
                "left.return": ("connected", "left-return"),
                "j2.1": ("connected", "left-return"),
                "j2.2": ("connected", "left"),
                "right.out": ("connected", "right"),
                "right.return": ("connected", "right-return"),
                "j1.1": ("connected", "right-return"),
                "j1.2": ("connected", "right"),
            }
        )
        self.assertEqual(
            compare_electrical_graphs(reference, pin_swapped, policy).state,
            "fail",
        )
        with self.assertRaisesRegex(ValidationError, "not bijective"):
            ElectricalGraphComparison(
                "fail",
                (("j1.1", "j2.1"), ("j1.2", "j2.1")),
                ("unintended_merge:j1.1,j1.2",),
                1,
            )

    def test_legal_no_connect_forms_are_explicit_policy(self) -> None:
        reference = _graph(
            {
                "u.nc": ("no_connect", None),
                "u.vcc": ("connected", "vcc"),
                "source.out": ("connected", "vcc"),
            }
        )
        observations = (
            _graph(
                {
                    "u.nc": ("no_connect", None),
                    "u.vcc": ("connected", "rail"),
                    "source.out": ("connected", "rail"),
                }
            ),
            _graph(
                {
                    "u.nc": ("unconnected", None),
                    "u.vcc": ("connected", "rail"),
                    "source.out": ("connected", "rail"),
                }
            ),
            _graph(
                {
                    "u.vcc": ("connected", "rail"),
                    "source.out": ("connected", "rail"),
                }
            ),
        )
        policy = ElectricalEquivalencePolicy(
            legal_no_connect_forms=("explicit", "omitted", "unconnected")
        )
        for observed in observations:
            with self.subTest(observed=observed.to_dict()):
                self.assertEqual(
                    compare_electrical_graphs(reference, observed, policy).state,
                    "pass",
                )
        self.assertEqual(
            compare_electrical_graphs(reference, observations[-1]).state,
            "fail",
        )

        required_endpoint_omitted = _graph(
            {
                "u.nc": ("no_connect", None),
                "source.out": ("connected", "rail"),
            }
        )
        self.assertEqual(
            compare_electrical_graphs(
                reference, required_endpoint_omitted, policy
            ).state,
            "fail",
        )

    def test_unspecified_pin_order_passes_but_specified_order_is_enforced(self) -> None:
        reference = _graph(
            {
                "a.out": ("connected", "a"),
                "j.1": ("connected", "a"),
                "b.out": ("connected", "b"),
                "j.2": ("connected", "b"),
            }
        )
        observed = _graph(
            {
                "a.out": ("connected", "a"),
                "j.2": ("connected", "a"),
                "b.out": ("connected", "b"),
                "j.1": ("connected", "b"),
            }
        )
        self.assertEqual(compare_electrical_graphs(reference, observed).state, "fail")
        policy = ElectricalEquivalencePolicy(
            unspecified_pin_orders=(UnspecifiedPinOrder("j", ("1", "2")),)
        )
        self.assertEqual(
            compare_electrical_graphs(reference, observed, policy).state, "pass"
        )

    def test_mapping_limit_returns_unknown_before_guessing(self) -> None:
        reference = _graph(
            {
                "a.out": ("connected", "a"),
                "j.1": ("connected", "a"),
                "b.out": ("connected", "b"),
                "j.2": ("connected", "b"),
            }
        )
        observed = _graph(
            {
                "a.out": ("connected", "a"),
                "j.2": ("connected", "a"),
                "b.out": ("connected", "b"),
                "j.1": ("connected", "b"),
            }
        )
        result = compare_electrical_graphs(
            reference,
            observed,
            ElectricalEquivalencePolicy(
                unspecified_pin_orders=(UnspecifiedPinOrder("j", ("1", "2")),)
            ),
            max_mappings=1,
        )
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.reasons, ("equivalence_search_limit",))
        self.assertEqual(result.candidates_checked, 1)

    def test_open_short_wrong_net_missing_endpoint_and_merge_still_fail(self) -> None:
        reference = _graph(
            {
                "a.out": ("connected", "a"),
                "u.a": ("connected", "a"),
                "b.out": ("connected", "b"),
                "u.b": ("connected", "b"),
            }
        )
        faults = {
            "open": {
                "a.out": ("connected", "a"),
                "u.a": ("connected", "open"),
                "b.out": ("connected", "b"),
                "u.b": ("connected", "b"),
            },
            "short_or_merge": {
                "a.out": ("connected", "merged"),
                "u.a": ("connected", "merged"),
                "b.out": ("connected", "merged"),
                "u.b": ("connected", "merged"),
            },
            "wrong_net": {
                "a.out": ("connected", "a"),
                "u.a": ("connected", "b"),
                "b.out": ("connected", "b"),
                "u.b": ("connected", "a"),
            },
            "missing": {
                "a.out": ("connected", "a"),
                "b.out": ("connected", "b"),
                "u.b": ("connected", "b"),
            },
        }
        for name, rows in faults.items():
            with self.subTest(name=name):
                self.assertEqual(
                    compare_electrical_graphs(reference, _graph(rows)).state,
                    "fail",
                )


class CausalAttributionTests(unittest.TestCase):
    def test_first_blocker_and_terminal_symptoms_remain_distinct(self) -> None:
        revision = 4
        progress = ProgressVector(
            revision,
            MetricValue.known(0, revision),
            MetricValue.known(1, revision),
            MetricValue.known(0, revision),
            MetricValue.known(3, revision),
            MetricValue.known(0, revision),
            MetricValue.known(0, revision),
            MetricValue.known(1, revision),
        )
        run = _normalized_run(
            stage=EngineeringStage.ERC_DRC,
            reason="no_progress",
        )
        result = derive_causal_attribution(
            run,
            progress=progress,
            routing_failures=(RoutingFailure("no_legal_channel", "SCL"),),
            evaluator_signals=(
                CausalSignal(
                    EngineeringStage.PLACEMENT.value,
                    "placement_constraint_failure",
                ),
            ),
        )
        self.assertEqual(result.first_blocking_stage, "placement")
        self.assertEqual(result.terminal_stage, "erc_drc")
        self.assertIn("placement_constraint_failure", result.root_causes)
        self.assertIn("router_no_legal_channel", result.root_causes)
        self.assertIn("routing_failure", result.symptoms)
        self.assertIn("drc_error", result.symptoms)
        self.assertIn("no_progress", result.symptoms)
        self.assertEqual(
            CausalAttribution.from_dict(result.to_dict()),
            result,
        )

    def test_unknown_v1_causal_evidence_stays_unknown(self) -> None:
        run = _normalized_run(stage=None, reason=None, release_gate_passed=None)
        result = derive_causal_attribution(run)
        self.assertIsNone(result.first_blocking_stage)
        self.assertIsNone(result.terminal_stage)
        self.assertEqual(result.root_causes, ())
        self.assertEqual(result.symptoms, ())

    def test_drc_is_a_symptom_not_an_invented_placement_root(self) -> None:
        revision = 7
        progress = ProgressVector(
            revision,
            MetricValue.known(0, revision),
            MetricValue.known(0, revision),
            MetricValue.known(0, revision),
            MetricValue.known(2, revision),
            MetricValue.known(0, revision),
            MetricValue.known(0, revision),
            MetricValue.known(0, revision),
        )
        result = derive_causal_attribution(
            _normalized_run(stage=EngineeringStage.ERC_DRC),
            progress=progress,
        )
        self.assertIn("drc_error", result.symptoms)
        self.assertEqual(result.root_causes, ())
        self.assertIsNone(result.first_blocking_stage)

    def test_causal_stage_cannot_follow_the_terminal_stage(self) -> None:
        with self.assertRaisesRegex(ValidationError, "follows terminal"):
            CausalAttribution(
                EngineeringStage.ROUTING.value,
                EngineeringStage.PLACEMENT.value,
                ("router_no_legal_channel",),
                ("routing_failure",),
            )


class V5PersistenceAndComparisonTests(unittest.TestCase):
    def test_v5_write_is_separate_and_old_artifacts_remain_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_files = {
                root / "runs" / "run-1.json": b"legacy run bytes\n",
                root / "scores" / "run-1" / "score.json": b"legacy score bytes\n",
                root / "reviews" / "run-1" / "review.json": b"legacy review bytes\n",
                root
                / "hardware"
                / "run-1"
                / "hardware.json": b"legacy hardware bytes\n",
            }
            for path, content in old_files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            before = {path: path.read_bytes() for path in old_files}
            target = write_evaluation_v5(root, _evaluation())
            self.assertEqual(
                target.relative_to(root).as_posix(),
                "evaluator-v5/run-1/evaluation.json",
            )
            self.assertEqual(load_evaluation_v5(target), _evaluation())
            self.assertEqual({path: path.read_bytes() for path in old_files}, before)
            with self.assertRaisesRegex(ValidationError, "already exists"):
                write_evaluation_v5(root, _evaluation())

            cross_campaign = replace(_evaluation(), campaign_id="another-campaign")
            with self.assertRaisesRegex(ValidationError, "already exists"):
                write_evaluation_v5(root, cross_campaign)

    def test_comparison_separates_native_and_evaluator_deltas(self) -> None:
        old = _evaluation()
        new = _evaluation(
            campaign_id="campaign-new",
            drc_errors=3,
        )
        comparison = compare_evaluations_v5(old, new)
        document = comparison.to_dict()
        self.assertIn("same_evaluator_v5", document)
        self.assertIn("raw_native_metric_delta", document)
        self.assertIn("evaluator_delta", document)
        drc = next(
            item for item in comparison.raw_native_metric_delta if item.name == "drc"
        )
        self.assertEqual(drc.value_delta, 3)
        self.assertEqual(drc.old.command, _native_metrics()[-1].command)
        self.assertEqual(drc.new.command, _native_metrics(drc_errors=3)[-1].command)
        self.assertFalse(comparison.evaluator_delta[0].changed)
        self.assertTrue(comparison.evaluator_delta[1].changed)
        self.assertEqual(BoardBenchComparisonV5.from_dict(document), comparison)
        self.assertEqual(
            document["same_evaluator_v5"]["old"]["case_id"],  # type: ignore[index]
            "case-1",
        )
        with self.assertRaisesRegex(ValidationError, "old/new evaluator calibration"):
            replace(comparison, evaluator_delta=())

        wrong_case = replace(new, case_id="case-2")
        with self.assertRaisesRegex(ValidationError, "case or repetition"):
            compare_evaluations_v5(
                old,
                wrong_case,
            )
        with self.assertRaisesRegex(ValidationError, "distinct old/new campaigns"):
            compare_evaluations_v5(old, replace(new, campaign_id=old.campaign_id))
        with self.assertRaisesRegex(ValidationError, "run identity"):
            compare_evaluations_v5(old, replace(new, run_id="different-run"))

        with tempfile.TemporaryDirectory() as temporary:
            path = write_comparison_v5(temporary, "old-new", comparison)
            self.assertEqual(load_comparison_v5(path), comparison)

    def test_paths_reject_traversal_and_parent_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "identity"):
                write_comparison_v5(
                    root,
                    "../escape",
                    compare_evaluations_v5(
                        _evaluation(),
                        _evaluation(campaign_id="campaign-new"),
                    ),
                )
            with self.assertRaisesRegex(ValidationError, "path is unsafe"):
                write_evaluation_v5(f"{root / 'unsafe'}\x00suffix", _evaluation())

            target = write_evaluation_v5(root / "real", _evaluation())
            link = root / "linked"
            link.symlink_to(root / "real", target_is_directory=True)
            linked_target = link / target.relative_to(root / "real")
            with self.assertRaisesRegex(ValidationError, "path is unsafe"):
                load_evaluation_v5(linked_target)

            writer_real = root / "writer-real"
            writer_real.mkdir()
            writer_link = root / "writer-linked"
            writer_link.symlink_to(writer_real, target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                write_evaluation_v5(
                    writer_link,
                    replace(_evaluation(), run_id="writer-root-symlink"),
                )
            with self.assertRaisesRegex(ValidationError, "symlink"):
                write_evaluation_v5(
                    writer_link / "nested",
                    replace(_evaluation(), run_id="writer-parent-symlink"),
                )


class ExistingGateIdentityTests(unittest.TestCase):
    def test_v5_keeps_existing_erc_and_drc_command_identity(self) -> None:
        calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def fake_run(
                argv: list[str],
                **_kwargs: object,
            ) -> CommandResult:
                calls.append(tuple(argv))
                output_index = argv.index("--output") + 1
                kind = "erc" if "erc" in argv else "drc"
                document = {
                    "$schema": f"https://schemas.kicad.org/{kind}.v1.json",
                    "kicad_version": "10.0.5-fake",
                    **(
                        {"sheets": [{"violations": []}]}
                        if kind == "erc"
                        else {
                            "violations": [],
                            "unconnected_items": [],
                            "schematic_parity": [],
                        }
                    ),
                }
                atomic_write_text(Path(argv[output_index]), json.dumps(document))
                return CommandResult(tuple(argv), 0, b"", b"", 0.01)

            with patch("pcbdraft.verification.gates.run_command", side_effect=fake_run):
                for name, filename in (
                    ("erc", "design.kicad_sch"),
                    ("drc", "design.kicad_pcb"),
                ):
                    result = run_gate(
                        name=name,
                        input_file=root / filename,
                        raw_output=root / f"{name}.json",
                        executable="kicad-cli",
                        deadline=time.monotonic() + 10,
                        redactions={},
                    )
                    self.assertEqual(result.tool_status, "ok")

        common = (
            "--format",
            "json",
            "--severity-error",
            "--severity-warning",
            "--output",
        )
        self.assertEqual(calls[0][:3], ("kicad-cli", "sch", "erc"))
        self.assertEqual(calls[0][3:8], common)
        self.assertEqual(calls[1][:3], ("kicad-cli", "pcb", "drc"))
        self.assertEqual(calls[1][3:8], common)


if __name__ == "__main__":
    unittest.main()
