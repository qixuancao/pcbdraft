from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from pcbdraft.core.debug_trace import MAX_TRACE_BYTES, DebugTraceWriter
from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_text
from pcbdraft.model.providers import IntentProvider
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.managed import open_managed_project
from pcbdraft.verification.boardbench import (
    AUTOMATIC_METRICS,
    BOARD_CATEGORIES,
    BoardBenchCampaign,
    BoardBenchCase,
    BoardBenchCorpus,
    BoardBenchRun,
    artifact_sha256,
    build_inventory,
    canonical_json_bytes,
    load_score,
    write_artifact,
)
from pcbdraft.verification.boardbench_evaluator import (
    EVALUATOR_VERSION,
    TRACE_MEMBER_LIMIT,
    USAGE_RECEIPT_LIMIT,
    _complete_project_members,
    _component_orientations,
    classify_completion_claim,
    decode_trace,
    decode_usage_receipt,
    evaluate_contracts,
    evaluate_run,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
NOW = "2026-08-21T08:00:00Z"
LATER = "2026-08-21T08:01:00Z"


def _alternative(part_id: str, symbol: str, footprint: str) -> dict[str, object]:
    return {"part_id": part_id, "symbol": symbol, "footprints": [footprint]}


def _case() -> BoardBenchCase:
    return BoardBenchCase.from_dict(
        {
            "id": "case-00",
            "category": "sensor",
            "prompt": "Design a small independently powered sensor board.",
            "applicable_metrics": list(AUTOMATIC_METRICS),
            "review_rubric": ["Check the sensor power path."],
            "component_slots": [
                {
                    "id": "controller",
                    "alternatives": [
                        _alternative("controller-part", "MCU:Controller", "Package:QFN")
                    ],
                },
                {
                    "id": "decoupler",
                    "alternatives": [
                        _alternative("capacitor-part", "Device:C", "Capacitor:C_0603")
                    ],
                },
                {
                    "id": "source",
                    "alternatives": [
                        _alternative("source-part", "Connector:Power", "Connector:JST")
                    ],
                },
            ],
            "net_rules": [
                {
                    "id": "controller-power",
                    "kind": "same_net",
                    "endpoints": ["controller.VDD", "source.OUT"],
                    "description": "The controller is powered by the source.",
                },
                {
                    "id": "controller-ground",
                    "kind": "required_endpoint",
                    "endpoints": ["controller.GND"],
                    "description": "The controller ground is connected.",
                },
            ],
            "support_requirements": [
                {
                    "id": "local-decoupling",
                    "kind": "decoupling",
                    "subjects": ["controller.VDD", "decoupler", "controller.GND"],
                    "description": "Place a two-terminal bypass across power.",
                },
                {
                    "id": "source-path",
                    "kind": "power_source",
                    "subjects": ["controller.VDD", "source.OUT"],
                    "description": "Connect the source output.",
                },
            ],
            "forbidden_conditions": [
                {
                    "id": "no-short",
                    "kind": "forbidden_connection",
                    "subjects": ["controller.VDD", "controller.GND"],
                    "description": "Power and ground must not be shorted.",
                },
                {
                    "id": "no-bad-part",
                    "kind": "forbidden_part",
                    "subjects": ["forbidden-part"],
                    "description": "Do not use the forbidden part.",
                },
                {
                    "id": "no-rating-violation",
                    "kind": "forbidden_rating",
                    "subjects": ["operating-voltage", "part-supply-rating"],
                    "description": "The referenced bounds must not be violated.",
                },
            ],
            "rating_bounds": [
                {
                    "id": "operating-voltage",
                    "source": "operating",
                    "subject": "controller.VDD",
                    "fact_key": None,
                    "quantity": "voltage",
                    "unit": "V",
                    "minimum": 3.0,
                    "maximum": 3.6,
                    "description": "Use the declared 3.3 V range.",
                },
                {
                    "id": "part-supply-rating",
                    "source": "part_rating",
                    "subject": "controller",
                    "fact_key": "supply_voltage_v",
                    "quantity": "voltage",
                    "unit": "V",
                    "minimum": 2.7,
                    "maximum": 3.6,
                    "description": "Use an attributed compatible component.",
                },
            ],
            "manufacturing_constraints": [
                {
                    "id": "clearance",
                    "kind": "min_clearance_mm",
                    "value_mm": 0.2,
                    "description": "Respect the fabrication clearance.",
                },
                {
                    "id": "height",
                    "kind": "max_component_height_mm",
                    "value_mm": 2.0,
                    "description": "Keep populated components low.",
                },
            ],
            "assembly_constraints": ["Manual assembly is reviewed by an engineer."],
        },
        "$case",
    )


def _pin(number: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(number=number, name=name, functions=())


def _part(
    part_id: str,
    symbol: str,
    footprint: str,
    pins: tuple[SimpleNamespace, ...],
    *,
    kind: str = "generic",
    bom: bool = True,
    ratings: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=part_id,
        kind=kind,
        symbol=symbol,
        footprint=footprint,
        pins=pins,
        trust="rule_validated",
        ratings=ratings or {},
        manufacturing={"height_mm": 1.0},
        bom=bom,
    )


class _Graph:
    def __init__(
        self,
        parts: tuple[SimpleNamespace, ...],
        *,
        issues: tuple[SimpleNamespace, ...] = (),
    ) -> None:
        self.parts = {part.id: part for part in parts}
        self.issues = issues

    def get_optional(self, part_id: str) -> SimpleNamespace | None:
        return self.parts.get(part_id)

    def validate_design(self, *_args: object, **_kwargs: object) -> list[object]:
        return list(self.issues)


def _component(component_id: str, part_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=component_id, part_id=part_id, attributes={})


def _design() -> tuple[SimpleNamespace, _Graph]:
    controller = _part(
        "controller-part",
        "MCU:Controller",
        "Package:QFN",
        (_pin("1", "VDD"), _pin("2", "GND")),
        ratings={"supply_voltage_v": {"min": 2.7, "max": 3.6}},
    )
    capacitor = _part(
        "capacitor-part",
        "Device:C",
        "Capacitor:C_0603",
        (_pin("1", "1"), _pin("2", "2")),
    )
    source = _part(
        "source-part",
        "Connector:Power",
        "Connector:JST",
        (_pin("1", "OUT"), _pin("2", "GND")),
        bom=False,
    )
    components = (
        _component("u1", controller.id),
        _component("c1", capacitor.id),
        _component("j1", source.id),
    )
    nets = (
        SimpleNamespace(
            id="vcc",
            power_domain="v3v3",
            endpoints=(
                SimpleNamespace(component="u1", pin="1"),
                SimpleNamespace(component="c1", pin="1"),
                SimpleNamespace(component="j1", pin="1"),
            ),
        ),
        SimpleNamespace(
            id="gnd",
            power_domain="v3v3",
            endpoints=(
                SimpleNamespace(component="u1", pin="2"),
                SimpleNamespace(component="c1", pin="2"),
                SimpleNamespace(component="j1", pin="2"),
            ),
        ),
    )
    design = SimpleNamespace(
        components=components,
        nets=nets,
        power_domains=(
            SimpleNamespace(id="v3v3", min_v=3.0, max_v=3.6, max_current_a=0.1),
        ),
        board=SimpleNamespace(
            min_track_mm=0.2,
            min_clearance_mm=0.2,
            min_drill_mm=0.3,
            width_mm=20.0,
            height_mm=20.0,
        ),
        native_intent=SimpleNamespace(routes=(), vias=()),
        metadata={},
        provenance=(SimpleNamespace(id="private-fixture"),),
    )
    return design, _Graph((controller, capacitor, source))


def _exact_bom_case(
    *, subjects: tuple[str, ...] = ("controller", "decoupler")
) -> BoardBenchCase:
    document = _case().to_dict()
    cast(list[object], document["forbidden_conditions"]).append(
        {
            "id": "no-extra-bom-components",
            "kind": "forbidden_unmatched_bom_component",
            "subjects": list(subjects),
            "description": "Every populated BOM component needs one allowed slot.",
        }
    )
    return BoardBenchCase.from_dict(document, "$case")


def _event(seq: int, event: str, data: dict[str, object]) -> dict[str, object]:
    return {
        "seq": seq,
        "timestamp": NOW,
        "pid": 1234,
        "event": event,
        "data": {"session_id": "session-1", **data},
    }


def _write_trace(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
    )


def _corpus() -> BoardBenchCorpus:
    template = _case()
    cases = tuple(
        replace(
            template,
            id=f"case-{category_index * 4 + offset:02d}",
            category=category,
            prompt=(
                template.prompt
                if category_index == 0 and offset == 0
                else f"Design private case {category_index * 4 + offset:02d}."
            ),
        )
        for category_index, category in enumerate(BOARD_CATEGORIES)
        for offset in range(4)
    )
    # Keep case-00 as the rich evaluator fixture while preserving 4/category.
    cases = (replace(template, category=BOARD_CATEGORIES[0]), *cases[1:])
    return BoardBenchCorpus(
        corpus_id="private-v1",
        corpus_version=1,
        license="CC0-1.0",
        methodology="Private evaluator fixture.",
        cohort="sealed_holdout_baseline",
        cases=cases,
    )


def _campaign(corpus: BoardBenchCorpus) -> BoardBenchCampaign:
    return BoardBenchCampaign.from_dict(
        {
            "schema": "pcbdraft-boardbench-campaign",
            "version": 1,
            "campaign_id": "campaign-v1",
            "cohort": "sealed_holdout_baseline",
            "corpus_id": "private-v1",
            "corpus_sha256": artifact_sha256(corpus),
            "created_at": NOW,
            "pcbdraft_commit": "abcdef1234567890",
            "dirty_state_sha256": HASH_B,
            "provider": "provider",
            "model": "model",
            "configuration_sha256": HASH_C,
            "kicad_version": "10.0.0",
            "python_version": "3.13.6",
            "platform": "linux-test",
            "tool_registry_sha256": HASH_A,
            "wall_timeout_seconds": 900.0,
            "tool_call_budget": 500,
            "repetitions": 3,
            "evaluator_version": EVALUATOR_VERSION,
            "runs": [
                {
                    "case_id": f"case-{case_index:02d}",
                    "repetition": repetition,
                    "run_id": f"case-{case_index:02d}-run-{repetition}",
                }
                for case_index in range(20)
                for repetition in range(1, 4)
            ],
        }
    )


class CompletionClaimTests(unittest.TestCase):
    def test_conservative_chinese_and_english_classification(self) -> None:
        for text in (
            "The PCB design is complete.",
            "Done — the project has been generated.",
            "设计已完成，可以下单。",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_completion_claim(text), "claims_complete")
        for text in (
            "The design is not complete; DRC still needs work.",
            "Unable to finish because KiCad failed.",
            "原理图尚未完成，还需处理电源连接。",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    classify_completion_claim(text),
                    "claims_blocked_or_incomplete",
                )
        for candidate_text in (
            "I updated the board.",
            "I completed the placement pass.",
            "When done, run DRC.",
            "完成了布局，接下来开始布线。",
            "已完成布局，接下来开始布线。",
            "The schematic is complete but DRC failed.",
            None,
        ):
            with self.subTest(text=candidate_text):
                self.assertEqual(classify_completion_claim(candidate_text), "ambiguous")


class TraceDecoderTests(unittest.TestCase):
    def test_accepts_one_valid_writer_record_above_rotation_threshold(self) -> None:
        self.assertGreater(TRACE_MEMBER_LIMIT, MAX_TRACE_BYTES)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "agent-trace.jsonl"
            writer = DebugTraceWriter(path)
            shared = "x" * 8_000
            writer.record(
                "large_observer_payload",
                **{f"bucket_{index}": [shared] * 200 for index in range(11)},
            )
            self.assertGreater(path.stat().st_size, MAX_TRACE_BYTES)
            reduced = decode_trace(root, wall_seconds=1.0, failure_reason=None)
        self.assertFalse(reduced.gap_detected)
        self.assertEqual(reduced.members, ("agent-trace.jsonl",))

    def test_real_event_shapes_reduce_tokens_cost_tools_retries_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = [
                _event(
                    1,
                    "model_request",
                    {"api_request_id": "api-1", "retry_count": 0},
                ),
                _event(
                    2,
                    "model_response",
                    {
                        "api_request_id": "api-1",
                        "api_duration_seconds": 0.25,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "cache_read_tokens": 2,
                            "cache_write_tokens": 1,
                            "reasoning_tokens": 3,
                            "total_tokens": 18,
                        },
                    },
                ),
                _event(
                    3,
                    "model_error",
                    {
                        "api_request_id": "api-2",
                        "retry_count": 1,
                        "api_duration_seconds": 0.1,
                    },
                ),
                _event(
                    4,
                    "model_request",
                    {"api_request_id": "api-2", "retry_count": 1},
                ),
                _event(
                    5,
                    "tool_start",
                    {"tool_name": "pcb_run_erc"},
                ),
                _event(
                    6,
                    "tool_end",
                    {
                        "tool_name": "pcb_run_erc",
                        "status": "ok",
                        "duration_ms": 40,
                    },
                ),
                _event(
                    7,
                    "tool_start",
                    {"tool_name": "pcb_search_parts"},
                ),
                _event(
                    8,
                    "tool_policy_blocked",
                    {"tool_name": "pcb_search_parts"},
                ),
                _event(
                    9,
                    "tool_end",
                    {
                        "tool_name": "pcb_search_parts",
                        "status": "ok",
                        "duration_ms": 2,
                        "result": json.dumps({"blocked": True}),
                    },
                ),
                _event(
                    10,
                    "turn_complete",
                    {"assistant_response": "The PCB design is complete."},
                ),
                _event(
                    11,
                    "session_end",
                    {
                        "cost_status": "actual",
                        "actual_cost_usd": 0.012,
                        "cost_source": "provider_cost_api",
                    },
                ),
            ]
            _write_trace(root / "agent-trace.jsonl", events)
            reduced = decode_trace(root, wall_seconds=60.0, failure_reason=None)
        self.assertFalse(reduced.gap_detected)
        self.assertEqual(reduced.final_response, "The PCB design is complete.")
        self.assertTrue(reduced.agent_erc_invoked)
        self.assertFalse(reduced.agent_drc_invoked)
        efficiency = reduced.efficiency
        self.assertEqual(efficiency.model_requests, 2)
        self.assertEqual(
            (
                efficiency.input_tokens,
                efficiency.output_tokens,
                efficiency.total_tokens,
            ),
            (10, 5, 18),
        )
        self.assertEqual(efficiency.cache_read_tokens, 2)
        self.assertEqual(efficiency.cache_write_tokens, 1)
        self.assertEqual(efficiency.reasoning_tokens, 3)
        self.assertEqual(efficiency.token_status, "reported")
        self.assertEqual(efficiency.cost_status, "actual")
        self.assertAlmostEqual(efficiency.cost_amount or 0.0, 0.012)
        self.assertEqual(efficiency.provider_retries, 1)
        self.assertEqual(efficiency.provider_errors, 1)
        self.assertEqual(efficiency.pcb_tool_calls, 2)
        self.assertEqual(efficiency.api_seconds, 0.35)
        self.assertEqual(efficiency.tool_seconds, 0.042)
        self.assertEqual(
            {
                (item.name, item.status, item.count)
                for item in efficiency.tool_call_counts
            },
            {
                ("pcb_search_parts", "denied", 1),
                ("pcb_run_erc", "completed", 1),
            },
        )

    def test_policy_blocked_tool_ends_preserve_realistic_call_counts(self) -> None:
        events: list[dict[str, object]] = []
        sequence = 1
        for _ in range(76):
            events.append(
                _event(sequence, "tool_start", {"tool_name": "pcb_search_parts"})
            )
            sequence += 1
            events.append(
                _event(
                    sequence,
                    "tool_end",
                    {
                        "tool_name": "pcb_search_parts",
                        "status": "ok",
                        "duration_ms": 10,
                    },
                )
            )
            sequence += 1
        for duration_ms in (1, 2, 3):
            events.append(
                _event(
                    sequence,
                    "tool_policy_blocked",
                    {"tool_name": "pcb_search_parts"},
                )
            )
            sequence += 1
            events.append(
                _event(
                    sequence,
                    "tool_end",
                    {
                        "tool_name": "pcb_search_parts",
                        "status": "error",
                        "duration_ms": duration_ms,
                        "result": json.dumps({"blocked": True}),
                    },
                )
            )
            sequence += 1

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_trace(root / "agent-trace.jsonl", events)
            reduced = decode_trace(root, wall_seconds=1.0, failure_reason=None)

        self.assertFalse(reduced.gap_detected)
        self.assertFalse(
            any(
                error.startswith("tool_end_without_start:")
                for error in reduced.schema_errors
            )
        )
        self.assertEqual(reduced.efficiency.pcb_tool_calls, 79)
        self.assertEqual(reduced.efficiency.tool_seconds, 0.766)
        self.assertEqual(
            {(item.status, item.count) for item in reduced.efficiency.tool_call_counts},
            {("completed", 76), ("denied", 3)},
        )

    def test_unblocked_tool_end_without_start_keeps_tool_counts_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_trace(
                root / "agent-trace.jsonl",
                [
                    _event(
                        1,
                        "tool_end",
                        {
                            "tool_name": "pcb_run_drc",
                            "status": "ok",
                            "duration_ms": 1,
                        },
                    )
                ],
            )
            reduced = decode_trace(root, wall_seconds=1.0, failure_reason=None)

        self.assertFalse(reduced.gap_detected)
        self.assertIn("tool_end_without_start:1", reduced.schema_errors)
        self.assertIsNone(reduced.efficiency.pcb_tool_calls)
        self.assertIsNone(reduced.efficiency.tool_seconds)
        self.assertIsNone(reduced.agent_drc_invoked)

    def test_gap_makes_every_affected_metric_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_trace(
                root / "agent-trace.jsonl",
                [
                    _event(
                        1,
                        "model_request",
                        {"api_request_id": "api-1", "retry_count": 0},
                    ),
                    _event(
                        3,
                        "tool_end",
                        {
                            "tool_name": "pcb_run_drc",
                            "status": "ok",
                            "duration_ms": 1,
                        },
                    ),
                ],
            )
            reduced = decode_trace(
                root, wall_seconds=1.0, failure_reason="worker_failed"
            )
        self.assertTrue(reduced.gap_detected)
        self.assertIsNone(reduced.efficiency.model_requests)
        self.assertIsNone(reduced.efficiency.pcb_tool_calls)
        self.assertIsNone(reduced.efficiency.provider_errors)
        self.assertEqual(reduced.efficiency.token_status, "unknown")
        self.assertEqual(reduced.efficiency.cost_status, "unknown")
        self.assertIsNone(reduced.agent_drc_invoked)

    def test_duplicate_trace_fields_are_a_schema_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic_write_text(
                root / "agent-trace.jsonl",
                '{"seq":1,"seq":2,"timestamp":"2026-08-21T08:00:00Z",'
                '"pid":1,"event":"session_start","data":{}}\n',
            )
            reduced = decode_trace(root, wall_seconds=1.0, failure_reason=None)
        self.assertTrue(reduced.gap_detected)
        self.assertTrue(
            any(reason.startswith("malformed:") for reason in reduced.schema_errors)
        )

    def test_missing_trace_is_reported_as_unknown_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reduced = decode_trace(
                Path(temporary) / "missing-trace",
                wall_seconds=1.0,
                failure_reason=None,
            )
        self.assertTrue(reduced.gap_detected)
        self.assertIn("trace_directory_unavailable", reduced.schema_errors)
        self.assertIsNone(reduced.efficiency.model_requests)
        self.assertEqual(reduced.efficiency.cost_status, "unknown")

    def test_local_schema_errors_do_not_erase_unaffected_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_trace(
                root / "agent-trace.jsonl",
                [
                    _event(
                        1,
                        "model_request",
                        {"api_request_id": "api-1", "retry_count": 0},
                    ),
                    _event(
                        2,
                        "model_response",
                        {
                            "api_duration_seconds": 0.25,
                            "usage": {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "cache_read_tokens": 0,
                                "cache_write_tokens": 0,
                                "reasoning_tokens": 1,
                                "total_tokens": 15,
                            },
                        },
                    ),
                    _event(3, "turn_complete", {"assistant_response": None}),
                    _event(4, "tool_start", {"tool_name": "pcb_run_erc"}),
                    _event(
                        5,
                        "tool_end",
                        {
                            "tool_name": "pcb_run_erc",
                            "status": "ok",
                            "duration_ms": 10,
                        },
                    ),
                ],
            )
            reduced = decode_trace(root, wall_seconds=1.0, failure_reason=None)
        self.assertFalse(reduced.gap_detected)
        self.assertTrue(reduced.schema_errors)
        self.assertEqual(reduced.efficiency.model_requests, 1)
        self.assertEqual(reduced.efficiency.total_tokens, 15)
        self.assertEqual(reduced.efficiency.pcb_tool_calls, 1)
        self.assertEqual(reduced.efficiency.api_seconds, 0.25)
        self.assertTrue(reduced.agent_erc_invoked)


class UsageReceiptTests(unittest.TestCase):
    def _document(self) -> dict[str, object]:
        return {
            "estimated_cost_usd": 0.125,
            "cost_status": "estimated",
            "cost_source": "official_docs_snapshot",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 30,
            "cache_write_tokens": 5,
            "reasoning_tokens": 4,
            "total_tokens": 155,
            "api_calls": 3,
            "model": "model",
            "provider": "provider",
            "session_id": "session-1",
            "completed": True,
            "failed": False,
            "service_tier": None,
        }

    def test_real_pcbdraft_usage_receipt_is_bounded_and_campaign_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "usage.json"
            atomic_write_text(path, json.dumps(self._document()) + "\n")
            result = decode_usage_receipt(
                path,
                _campaign(_corpus()),
                trace_session_id="session-1",
            )
        self.assertTrue(result.present)
        self.assertFalse(result.errors)
        self.assertEqual(result.model_requests, 3)
        self.assertEqual(result.total_tokens, 155)
        self.assertEqual(result.cache_read_tokens, 30)
        self.assertEqual(result.cost_status, "estimated")
        self.assertAlmostEqual(result.cost_amount or 0.0, 0.125)

    def test_malformed_or_mismatched_usage_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = root / "duplicate.json"
            atomic_write_text(duplicate, '{"provider":"provider","provider":"other"}\n')
            malformed = decode_usage_receipt(
                duplicate,
                _campaign(_corpus()),
                trace_session_id="session-1",
            )
            self.assertTrue(malformed.present)
            self.assertIsNone(malformed.total_tokens)
            self.assertIn("usage_receipt_malformed", malformed.errors)

            mismatch_document = self._document()
            mismatch_document["model"] = "other-model"
            mismatch = root / "mismatch.json"
            atomic_write_text(mismatch, json.dumps(mismatch_document) + "\n")
            rejected = decode_usage_receipt(
                mismatch,
                _campaign(_corpus()),
                trace_session_id="session-1",
            )
        self.assertIsNone(rejected.model_requests)
        self.assertIsNone(rejected.total_tokens)
        self.assertEqual(rejected.cost_status, "unknown")
        self.assertIn("usage_receipt_campaign_identity_mismatch", rejected.errors)

    def test_unsafe_or_invalid_usage_receipts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            negative_document = self._document()
            negative_document["input_tokens"] = -1
            negative = root / "negative.json"
            atomic_write_text(negative, json.dumps(negative_document) + "\n")
            invalid_tokens = decode_usage_receipt(
                negative,
                _campaign(_corpus()),
                trace_session_id="session-1",
            )
            self.assertIsNone(invalid_tokens.total_tokens)
            self.assertIn("usage_receipt_tokens", invalid_tokens.errors)

            nonfinite = root / "nonfinite.json"
            atomic_write_text(
                nonfinite,
                json.dumps(self._document()).replace("0.125", "NaN") + "\n",
            )
            invalid_number = decode_usage_receipt(
                nonfinite,
                _campaign(_corpus()),
                trace_session_id="session-1",
            )
            self.assertIsNone(invalid_number.cost_amount)
            self.assertIn("usage_receipt_malformed", invalid_number.errors)

            oversized = root / "oversized.json"
            atomic_write_text(oversized, " " * (USAGE_RECEIPT_LIMIT + 1))
            too_large = decode_usage_receipt(
                oversized,
                _campaign(_corpus()),
                trace_session_id="session-1",
            )
            self.assertIsNone(too_large.total_tokens)
            self.assertIn("usage_receipt_malformed", too_large.errors)

            target = root / "target.json"
            atomic_write_text(target, json.dumps(self._document()) + "\n")
            link = root / "usage-link.json"
            link.symlink_to(target)
            linked = decode_usage_receipt(
                link,
                _campaign(_corpus()),
                trace_session_id="session-1",
            )
            self.assertFalse(linked.present)
            self.assertIn("usage_receipt_unavailable", linked.errors)


class ContractEvaluatorTests(unittest.TestCase):
    def _two_terminal_fixture(
        self,
        *,
        kind: str,
        symbol: str,
        footprint: str,
        swapped: bool,
        inconsistent: bool = False,
    ) -> tuple[BoardBenchCase, SimpleNamespace, _Graph]:
        part_id = f"{kind}-part"
        applicable_metrics = ["reference_topology"]
        support_requirements: list[dict[str, object]] = []
        net_rules = [
            {
                "id": "left-terminal",
                "kind": "same_net",
                "endpoints": ["part.1", "left.OUT"],
                "description": "Reference terminal 1 reaches the left net.",
            }
        ]
        if inconsistent:
            applicable_metrics.append("support_circuits")
            support_requirements.append(
                {
                    "id": "conflicting-right-terminal",
                    "kind": "power_source",
                    "subjects": ["part.1", "right.OUT"],
                    "description": "The same reference terminal reaches the right net.",
                }
            )
        else:
            net_rules.append(
                {
                    "id": "right-terminal",
                    "kind": "same_net",
                    "endpoints": ["part.2", "right.OUT"],
                    "description": "Reference terminal 2 reaches the right net.",
                }
            )
        case = BoardBenchCase.from_dict(
            {
                "id": f"{kind}-orientation",
                "category": "sensor",
                "prompt": "Connect a two-terminal part between two distinct nets.",
                "applicable_metrics": applicable_metrics,
                "review_rubric": ["Check terminal orientation."],
                "component_slots": [
                    {
                        "id": "part",
                        "alternatives": [_alternative(part_id, symbol, footprint)],
                    },
                    {
                        "id": "left",
                        "alternatives": [
                            _alternative(
                                "left-source", "Connector:Left", "Connector:JST"
                            )
                        ],
                    },
                    {
                        "id": "right",
                        "alternatives": [
                            _alternative(
                                "right-source", "Connector:Right", "Connector:JST"
                            )
                        ],
                    },
                ],
                "net_rules": net_rules,
                "support_requirements": support_requirements,
                "forbidden_conditions": [],
                "rating_bounds": [],
                "manufacturing_constraints": [],
                "assembly_constraints": ["Review manually."],
            },
            "$case",
        )
        part = _part(
            part_id,
            symbol,
            footprint,
            (_pin("1", "1"), _pin("2", "2")),
            kind=kind,
        )
        left = _part(
            "left-source",
            "Connector:Left",
            "Connector:JST",
            (_pin("1", "OUT"),),
            kind="connector",
            bom=False,
        )
        right = _part(
            "right-source",
            "Connector:Right",
            "Connector:JST",
            (_pin("1", "OUT"),),
            kind="connector",
            bom=False,
        )
        left_pin, right_pin = ("2", "1") if swapped else ("1", "2")
        design = SimpleNamespace(
            components=(
                _component("x1", part.id),
                _component("j-left", left.id),
                _component("j-right", right.id),
            ),
            nets=(
                SimpleNamespace(
                    id="left-net",
                    power_domain=None,
                    endpoints=(
                        SimpleNamespace(component="x1", pin=left_pin),
                        SimpleNamespace(component="j-left", pin="1"),
                    ),
                ),
                SimpleNamespace(
                    id="right-net",
                    power_domain=None,
                    endpoints=(
                        SimpleNamespace(component="x1", pin=right_pin),
                        SimpleNamespace(component="j-right", pin="1"),
                    ),
                ),
            ),
            power_domains=(),
            board=SimpleNamespace(
                min_track_mm=0.2,
                min_clearance_mm=0.2,
                min_drill_mm=0.3,
                width_mm=20.0,
                height_mm=20.0,
            ),
            native_intent=SimpleNamespace(routes=(), vias=()),
        )
        return case, design, _Graph((part, left, right))

    def test_swapped_resistor_orientation_passes_and_is_reported(self) -> None:
        case, design, graph = self._two_terminal_fixture(
            kind="resistor",
            symbol="Device:R",
            footprint="Resistor:R_0603",
            swapped=True,
        )

        result = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]

        self.assertEqual(result.metric_state("reference_topology"), "pass")
        self.assertEqual(dict(result.orientations)["part"], "swap_1_2")
        assignment = result.to_dict()["assignment"]
        part_assignment = next(item for item in assignment if item["slot"] == "part")
        self.assertEqual(part_assignment["orientation"], "swap_1_2")

        design.nets[0].endpoints = tuple(
            endpoint
            for endpoint in design.nets[0].endpoints
            if endpoint.component != "x1"
        )
        disconnected = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]
        self.assertEqual(disconnected.metric_state("reference_topology"), "fail")

        _, shorted_design, shorted_graph = self._two_terminal_fixture(
            kind="resistor",
            symbol="Device:R",
            footprint="Resistor:R_0603",
            swapped=True,
        )
        shorted_design.nets[0].endpoints = (
            *shorted_design.nets[0].endpoints,
            SimpleNamespace(component="x1", pin="1"),
        )
        shorted_design.nets[1].endpoints = tuple(
            endpoint
            for endpoint in shorted_design.nets[1].endpoints
            if endpoint.component != "x1"
        )
        shorted = evaluate_contracts(  # type: ignore[arg-type]
            case, shorted_design, shorted_graph
        )
        self.assertEqual(shorted.metric_state("reference_topology"), "fail")

    def test_identity_resistor_orientation_still_passes(self) -> None:
        case, design, graph = self._two_terminal_fixture(
            kind="resistor",
            symbol="Device:R",
            footprint="Resistor:R_0603",
            swapped=False,
        )

        result = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]

        self.assertEqual(result.metric_state("reference_topology"), "pass")
        self.assertEqual(dict(result.orientations)["part"], "identity")

    def test_one_resistor_cannot_use_different_orientation_per_predicate(self) -> None:
        case, design, graph = self._two_terminal_fixture(
            kind="resistor",
            symbol="Device:R",
            footprint="Resistor:R_0603",
            swapped=False,
            inconsistent=True,
        )

        result = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]

        states = {
            result.metric_state("reference_topology"),
            result.metric_state("support_circuits"),
        }
        self.assertEqual(states, {"pass", "fail"})
        self.assertEqual(dict(result.orientations)["part"], "identity")

    def test_non_resistor_two_terminal_parts_cannot_swap(self) -> None:
        for kind, symbol, footprint in (
            ("led", "Device:LED", "LED:LED_0603"),
            ("diode", "Device:D", "Diode:D_0603"),
            ("capacitor", "Device:C", "Capacitor:C_0603"),
            (
                "connector",
                "Connector_Generic:Conn_01x02",
                "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            ),
        ):
            with self.subTest(kind=kind):
                case, design, graph = self._two_terminal_fixture(
                    kind=kind,
                    symbol=symbol,
                    footprint=footprint,
                    swapped=True,
                )

                result = evaluate_contracts(  # type: ignore[arg-type]
                    case, design, graph
                )

                self.assertEqual(result.metric_state("reference_topology"), "fail")
                self.assertEqual(dict(result.orientations)["part"], "identity")

    def test_only_two_pin_numbered_resistors_offer_swapped_orientation(self) -> None:
        component = _component("r1", "candidate")
        cases = (
            ("resistor", (_pin("1", "1"), _pin("2", "2")), True),
            (
                "resistor",
                (_pin("1", "1"), _pin("2", "2"), _pin("3", "3")),
                False,
            ),
            ("resistor", (_pin("A", "A"), _pin("B", "B")), False),
            ("capacitor", (_pin("1", "1"), _pin("2", "2")), False),
        )
        for kind, pins, can_swap in cases:
            with self.subTest(kind=kind, pins=tuple(pin.number for pin in pins)):
                part = _part(
                    "candidate",
                    "Device:R",
                    "Resistor:R_0603",
                    pins,
                    kind=kind,
                )
                orientations = _component_orientations(  # type: ignore[arg-type]
                    component, _Graph((part,))
                )
                self.assertEqual("swap_1_2" in orientations, can_swap)

    def test_identity_orientation_wins_when_both_orientations_pass(self) -> None:
        case, design, graph = self._two_terminal_fixture(
            kind="resistor",
            symbol="Device:R",
            footprint="Resistor:R_0603",
            swapped=True,
        )
        document = case.to_dict()
        document["net_rules"] = []
        unconstrained = BoardBenchCase.from_dict(document, "$case")

        result = evaluate_contracts(  # type: ignore[arg-type]
            unconstrained, design, graph
        )

        self.assertEqual(result.metric_state("reference_topology"), "pass")
        self.assertEqual(dict(result.orientations)["part"], "identity")

    def test_orientation_search_is_deterministic_and_bounded(self) -> None:
        case, design, graph = self._two_terminal_fixture(
            kind="resistor",
            symbol="Device:R",
            footprint="Resistor:R_0603",
            swapped=True,
        )

        limited = evaluate_contracts(  # type: ignore[arg-type]
            case, design, graph, search_node_limit=5
        )
        complete = evaluate_contracts(  # type: ignore[arg-type]
            case, design, graph, search_node_limit=6
        )
        repeated = evaluate_contracts(  # type: ignore[arg-type]
            case, design, graph, search_node_limit=6
        )

        self.assertTrue(limited.search_truncated)
        self.assertLessEqual(limited.search_nodes, 3 * 5)
        self.assertEqual(limited.metric_state("reference_topology"), "unknown")
        self.assertFalse(complete.search_truncated)
        self.assertLessEqual(complete.search_nodes, 3 * 6)
        self.assertEqual(complete.metric_state("reference_topology"), "pass")
        self.assertEqual(complete, repeated)

    def test_partial_predicate_pruning_handles_repeated_support_parts(self) -> None:
        count = 7
        case = BoardBenchCase.from_dict(
            {
                "id": "repeated-support",
                "category": "mcu_minimum_system",
                "prompt": "Add seven independently connected bypass components.",
                "applicable_metrics": ["support_circuits"],
                "review_rubric": ["Check every bypass branch."],
                "component_slots": [
                    {
                        "id": "controller",
                        "alternatives": [
                            _alternative("many-rail", "MCU:ManyRail", "Package:BGA")
                        ],
                    },
                    *[
                        {
                            "id": f"cap{index}",
                            "alternatives": [
                                _alternative("same-cap", "Device:C", "Capacitor:C_0603")
                            ],
                        }
                        for index in range(count)
                    ],
                ],
                "net_rules": [],
                "support_requirements": [
                    {
                        "id": f"bypass-{index}",
                        "kind": "decoupling",
                        "subjects": [
                            f"controller.V{index}",
                            f"cap{index}",
                            "controller.GND",
                        ],
                        "description": "Match this exact power branch.",
                    }
                    for index in range(count)
                ],
                "forbidden_conditions": [],
                "rating_bounds": [],
                "manufacturing_constraints": [],
                "assembly_constraints": ["Review manually."],
            },
            "$case",
        )
        controller = _part(
            "many-rail",
            "MCU:ManyRail",
            "Package:BGA",
            (
                *(_pin(str(index + 1), f"V{index}") for index in range(count)),
                _pin("99", "GND"),
            ),
        )
        capacitor = _part(
            "same-cap",
            "Device:C",
            "Capacitor:C_0603",
            (_pin("1", "1"), _pin("2", "2")),
        )
        components = (
            _component("u1", controller.id),
            *(_component(f"c{index}", capacitor.id) for index in range(count)),
        )
        ground_endpoints = [SimpleNamespace(component="u1", pin="99")]
        ground_endpoints.extend(
            SimpleNamespace(component=f"c{index}", pin="2") for index in range(count)
        )
        nets = [
            SimpleNamespace(
                id=f"v{index}",
                power_domain=None,
                endpoints=(
                    SimpleNamespace(component="u1", pin=str(index + 1)),
                    SimpleNamespace(component=f"c{count - index - 1}", pin="1"),
                ),
            )
            for index in range(count)
        ]
        nets.append(
            SimpleNamespace(
                id="gnd", power_domain=None, endpoints=tuple(ground_endpoints)
            )
        )
        design = SimpleNamespace(
            components=components,
            nets=tuple(nets),
            power_domains=(),
            board=SimpleNamespace(
                min_track_mm=0.2,
                min_clearance_mm=0.2,
                min_drill_mm=0.3,
                width_mm=20.0,
                height_mm=20.0,
            ),
            native_intent=SimpleNamespace(routes=(), vias=()),
        )
        result = evaluate_contracts(
            case,
            design,  # type: ignore[arg-type]
            _Graph((controller, capacitor)),  # type: ignore[arg-type]
            search_node_limit=1_000,
        )
        self.assertEqual(result.slot_state, "pass")
        self.assertEqual(result.metric_state("support_circuits"), "pass")
        self.assertLess(result.search_nodes, 100)

    def test_identical_components_are_matched_by_dependent_topology(self) -> None:
        case = BoardBenchCase.from_dict(
            {
                "id": "matching",
                "category": "adapter_communication",
                "prompt": "Connect one of two identical resistors to the source.",
                "applicable_metrics": ["reference_topology"],
                "review_rubric": ["Check assignment."],
                "component_slots": [
                    {
                        "id": "selected",
                        "alternatives": [
                            _alternative("resistor", "Device:R", "Resistor:R_0603")
                        ],
                    },
                    {
                        "id": "source",
                        "alternatives": [
                            _alternative("source", "Connector:J", "Connector:JST")
                        ],
                    },
                ],
                "net_rules": [
                    {
                        "id": "right-resistor",
                        "kind": "same_net",
                        "endpoints": ["selected.1", "source.OUT"],
                        "description": "Only the resistor on the source net matches.",
                    }
                ],
                "support_requirements": [],
                "forbidden_conditions": [],
                "rating_bounds": [],
                "manufacturing_constraints": [],
                "assembly_constraints": ["Review manually."],
            },
            "$case",
        )
        resistor = _part(
            "resistor", "Device:R", "Resistor:R_0603", (_pin("1", "1"), _pin("2", "2"))
        )
        source = _part(
            "source", "Connector:J", "Connector:JST", (_pin("1", "OUT"),), bom=False
        )
        design = SimpleNamespace(
            components=(
                _component("r1", "resistor"),
                _component("r2", "resistor"),
                _component("j1", "source"),
            ),
            nets=(
                SimpleNamespace(
                    id="wanted",
                    power_domain=None,
                    endpoints=(
                        SimpleNamespace(component="r2", pin="1"),
                        SimpleNamespace(component="j1", pin="1"),
                    ),
                ),
                SimpleNamespace(
                    id="other",
                    power_domain=None,
                    endpoints=(SimpleNamespace(component="r1", pin="1"),),
                ),
            ),
            power_domains=(),
            board=SimpleNamespace(
                min_track_mm=0.2,
                min_clearance_mm=0.2,
                min_drill_mm=0.3,
                width_mm=20.0,
                height_mm=20.0,
            ),
            native_intent=SimpleNamespace(routes=(), vias=()),
        )
        result = evaluate_contracts(case, design, _Graph((resistor, source)))  # type: ignore[arg-type]
        self.assertEqual(result.slot_state, "pass")
        self.assertEqual(result.metric_state("reference_topology"), "pass")
        self.assertIn(("selected", "r2"), result.assignment)

    def test_metrics_share_one_global_injective_assignment(self) -> None:
        case = BoardBenchCase.from_dict(
            {
                "id": "global-matching",
                "category": "adapter_communication",
                "prompt": "Use one resistor for the required source path.",
                "applicable_metrics": ["reference_topology", "support_circuits"],
                "review_rubric": ["Check the shared assignment."],
                "component_slots": [
                    {
                        "id": "selected",
                        "alternatives": [
                            _alternative("resistor", "Device:R", "Resistor:R_0603")
                        ],
                    },
                    {
                        "id": "topology_source",
                        "alternatives": [
                            _alternative("source-a", "Connector:A", "Connector:JST")
                        ],
                    },
                    {
                        "id": "support_source",
                        "alternatives": [
                            _alternative("source-b", "Connector:B", "Connector:JST")
                        ],
                    },
                ],
                "net_rules": [
                    {
                        "id": "topology-path",
                        "kind": "same_net",
                        "endpoints": ["selected.1", "topology_source.OUT"],
                        "description": "The selected resistor reaches source A.",
                    }
                ],
                "support_requirements": [
                    {
                        "id": "support-path",
                        "kind": "power_source",
                        "subjects": ["selected.1", "support_source.OUT"],
                        "description": "The same selected resistor reaches source B.",
                    }
                ],
                "forbidden_conditions": [],
                "rating_bounds": [],
                "manufacturing_constraints": [],
                "assembly_constraints": ["Review manually."],
            },
            "$case",
        )
        resistor = _part("resistor", "Device:R", "Resistor:R_0603", (_pin("1", "1"),))
        source_a = _part(
            "source-a", "Connector:A", "Connector:JST", (_pin("1", "OUT"),)
        )
        source_b = _part(
            "source-b", "Connector:B", "Connector:JST", (_pin("1", "OUT"),)
        )
        design = SimpleNamespace(
            components=(
                _component("r1", "resistor"),
                _component("r2", "resistor"),
                _component("j1", "source-a"),
                _component("j2", "source-b"),
            ),
            nets=(
                SimpleNamespace(
                    id="topology",
                    power_domain=None,
                    endpoints=(
                        SimpleNamespace(component="r1", pin="1"),
                        SimpleNamespace(component="j1", pin="1"),
                    ),
                ),
                SimpleNamespace(
                    id="support",
                    power_domain=None,
                    endpoints=(
                        SimpleNamespace(component="r2", pin="1"),
                        SimpleNamespace(component="j2", pin="1"),
                    ),
                ),
            ),
            power_domains=(),
            board=SimpleNamespace(
                min_track_mm=0.2,
                min_clearance_mm=0.2,
                min_drill_mm=0.3,
                width_mm=20.0,
                height_mm=20.0,
            ),
            native_intent=SimpleNamespace(routes=(), vias=()),
        )
        result = evaluate_contracts(
            case,
            design,  # type: ignore[arg-type]
            _Graph((resistor, source_a, source_b)),  # type: ignore[arg-type]
        )
        states = {
            result.metric_state("reference_topology"),
            result.metric_state("support_circuits"),
        }
        self.assertEqual(result.slot_state, "pass")
        self.assertEqual(states, {"pass", "fail"})

    def test_support_rating_forbidden_and_manufacturing_predicates_pass(self) -> None:
        design, graph = _design()
        result = evaluate_contracts(_case(), design, graph)  # type: ignore[arg-type]
        self.assertEqual(result.slot_state, "pass")
        self.assertEqual(result.metric_state("reference_topology"), "pass")
        self.assertEqual(result.metric_state("support_circuits"), "pass")
        self.assertEqual(result.metric_state("ratings"), "pass")

    def test_exact_bom_cardinality_rejects_extra_active_component(self) -> None:
        design, graph = _design()
        intruder = _part(
            "active-intruder",
            "MCU:Intruder",
            "Package:QFN",
            (_pin("1", "VDD"),),
        )
        graph.parts[intruder.id] = intruder
        design.components = (*design.components, _component("u-extra", intruder.id))

        result = evaluate_contracts(  # type: ignore[arg-type]
            _exact_bom_case(), design, graph
        )
        cardinality = next(
            item for item in result.support if item.id == "no-extra-bom-components"
        )
        self.assertEqual(result.metric_state("support_circuits"), "fail")
        self.assertEqual(cardinality.state, "fail")
        self.assertEqual(cardinality.reason, "unmatched_bom_components:u-extra")

    def test_exact_bom_cardinality_allows_attributed_non_bom_virtual(self) -> None:
        design, graph = _design()
        virtual = _part(
            "virtual-power-flag",
            "power:PWR_FLAG",
            "Virtual:None",
            (_pin("1", "pwr"),),
            bom=False,
        )
        graph.parts[virtual.id] = virtual
        design.components = (*design.components, _component("flag-extra", virtual.id))

        result = evaluate_contracts(  # type: ignore[arg-type]
            _exact_bom_case(), design, graph
        )
        cardinality = next(
            item for item in result.support if item.id == "no-extra-bom-components"
        )
        self.assertEqual(result.metric_state("support_circuits"), "pass")
        self.assertEqual(cardinality.state, "pass")
        self.assertEqual(cardinality.reason, "all_bom_components_match_allowed_slots")

    def test_exact_bom_cardinality_does_not_trust_provisional_non_bom_flag(
        self,
    ) -> None:
        design, graph = _design()
        virtual = _part(
            "provisional-virtual",
            "power:PWR_FLAG",
            "Virtual:None",
            (_pin("1", "pwr"),),
            bom=False,
        )
        virtual.trust = "extracted"
        graph.parts[virtual.id] = virtual
        design.components = (*design.components, _component("flag-extra", virtual.id))

        result = evaluate_contracts(  # type: ignore[arg-type]
            _exact_bom_case(), design, graph
        )
        cardinality = next(
            item for item in result.support if item.id == "no-extra-bom-components"
        )
        self.assertEqual(result.metric_state("support_circuits"), "unknown")
        self.assertEqual(cardinality.state, "unknown")
        self.assertEqual(
            cardinality.reason, "bom_classification_unavailable:flag-extra"
        )

    def test_exact_bom_cardinality_rejects_duplicate_pullup(self) -> None:
        case = BoardBenchCase.from_dict(
            {
                "id": "exact-pullups",
                "category": "adapter_communication",
                "prompt": "Use exactly one populated pull-up for each I2C signal.",
                "applicable_metrics": ["support_circuits"],
                "review_rubric": ["Confirm there is exactly one pull-up pair."],
                "component_slots": [
                    {
                        "id": "pullup_sda",
                        "alternatives": [
                            _alternative(
                                "pullup-resistor",
                                "Device:R",
                                "Resistor:R_0603",
                            )
                        ],
                    },
                    {
                        "id": "pullup_scl",
                        "alternatives": [
                            _alternative(
                                "pullup-resistor",
                                "Device:R",
                                "Resistor:R_0603",
                            )
                        ],
                    },
                ],
                "net_rules": [],
                "support_requirements": [],
                "forbidden_conditions": [
                    {
                        "id": "one-pullup-pair",
                        "kind": "forbidden_unmatched_bom_component",
                        "subjects": ["pullup_sda", "pullup_scl"],
                        "description": "No third populated pull-up is allowed.",
                    }
                ],
                "rating_bounds": [],
                "manufacturing_constraints": [],
                "assembly_constraints": ["Review the fitted resistor count."],
            },
            "$case",
        )
        resistor = _part(
            "pullup-resistor",
            "Device:R",
            "Resistor:R_0603",
            (_pin("1", "1"), _pin("2", "2")),
        )
        design = SimpleNamespace(
            components=tuple(
                _component(component_id, resistor.id)
                for component_id in ("r1", "r2", "r3")
            ),
            nets=(),
            power_domains=(),
            board=SimpleNamespace(
                min_track_mm=0.2,
                min_clearance_mm=0.2,
                min_drill_mm=0.3,
                width_mm=20.0,
                height_mm=20.0,
            ),
            native_intent=SimpleNamespace(routes=(), vias=()),
        )

        result = evaluate_contracts(  # type: ignore[arg-type]
            case, design, _Graph((resistor,))
        )
        cardinality = next(
            item for item in result.support if item.id == "one-pullup-pair"
        )
        self.assertEqual(result.metric_state("support_circuits"), "fail")
        self.assertEqual(cardinality.reason, "unmatched_bom_components:r3")

    def test_exact_bom_cardinality_missing_evidence_is_unknown_fail_closed(
        self,
    ) -> None:
        document = _exact_bom_case().to_dict()
        document["net_rules"] = []
        document["support_requirements"] = []
        document["forbidden_conditions"] = [
            item
            for item in cast(list[dict[str, object]], document["forbidden_conditions"])
            if item["kind"] == "forbidden_unmatched_bom_component"
        ]
        document["rating_bounds"] = []
        case = BoardBenchCase.from_dict(document, "$case")
        design, graph = _design()
        design.components = tuple(
            component for component in design.components if component.id != "c1"
        )

        result = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]
        cardinality = next(
            item for item in result.support if item.id == "no-extra-bom-components"
        )
        self.assertEqual(result.slot_state, "fail")
        self.assertEqual(result.support_slot_state, "unknown")
        self.assertEqual(result.metric_state("support_circuits"), "unknown")
        self.assertEqual(cardinality.state, "unknown")
        self.assertEqual(
            cardinality.reason, "allowed_slot_assignment_unavailable:decoupler"
        )

        unknown_design, unknown_graph = _design()
        unknown_design.components = (
            *unknown_design.components,
            _component("unknown-extra", "part-without-bom-evidence"),
        )
        unknown = evaluate_contracts(  # type: ignore[arg-type]
            _exact_bom_case(), unknown_design, unknown_graph
        )
        unknown_cardinality = next(
            item for item in unknown.support if item.id == "no-extra-bom-components"
        )
        self.assertEqual(unknown.metric_state("support_circuits"), "unknown")
        self.assertEqual(
            unknown_cardinality.reason,
            "bom_classification_unavailable:unknown-extra",
        )

    def test_existing_forbidden_condition_reasons_remain_compatible(self) -> None:
        design, graph = _design()
        extra = _part(
            "additional-part",
            "Device:R",
            "Resistor:R_0603",
            (_pin("1", "1"), _pin("2", "2")),
        )
        graph.parts[extra.id] = extra
        design.components = (*design.components, _component("r-extra", extra.id))
        result = evaluate_contracts(_case(), design, graph)  # type: ignore[arg-type]
        support = {item.id: item for item in result.support}

        self.assertEqual(support["no-short"].reason, "forbidden_connection_pass")
        self.assertEqual(support["no-bad-part"].reason, "forbidden_part_pass")
        self.assertEqual(support["no-rating-violation"].reason, "forbidden_rating_pass")

    def test_legacy_part_redirect_is_canonical_for_matching_and_forbidden_parts(
        self,
    ) -> None:
        document = _case().to_dict()
        forbidden = next(
            item
            for item in cast(list[dict[str, object]], document["forbidden_conditions"])
            if item["id"] == "no-bad-part"
        )
        forbidden["subjects"] = ["controller-part"]
        case = BoardBenchCase.from_dict(document, "$case")
        design, graph = _design()
        canonical = graph.parts["controller-part"]
        graph.parts["legacy-controller-part"] = canonical
        controller = next(item for item in design.components if item.id == "u1")
        controller.part_id = "legacy-controller-part"

        result = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]

        self.assertEqual(result.slot_state, "pass")
        self.assertEqual(result.metric_state("reference_topology"), "pass")
        self.assertEqual(result.metric_state("support_circuits"), "fail")
        forbidden_result = next(
            item for item in result.support if item.id == "no-bad-part"
        )
        self.assertEqual(forbidden_result.reason, "forbidden_part_fail")

    def test_allowed_component_alternative_is_accepted(self) -> None:
        case_data = copy.deepcopy(_case().to_dict())
        controller = cast(dict[str, object], case_data["component_slots"][0])
        alternatives = cast(list[dict[str, object]], controller["alternatives"])
        alternatives.insert(
            0,
            _alternative("different-controller", "MCU:Other", "Package:SOIC"),
        )
        case = BoardBenchCase.from_dict(case_data, "$case")
        design, graph = _design()
        result = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]
        self.assertEqual(result.slot_state, "pass")
        self.assertEqual(result.metric_state("reference_topology"), "pass")

    def test_topology_support_forbidden_and_rating_faults_are_distinguished(
        self,
    ) -> None:
        topology_design, topology_graph = _design()
        topology_design.nets[0].endpoints = tuple(
            endpoint
            for endpoint in topology_design.nets[0].endpoints
            if endpoint.component != "j1"
        )
        topology = evaluate_contracts(  # type: ignore[arg-type]
            _case(), topology_design, topology_graph
        )
        self.assertEqual(topology.metric_state("reference_topology"), "fail")

        support_design, support_graph = _design()
        support_design.nets[0].endpoints = tuple(
            endpoint
            for endpoint in support_design.nets[0].endpoints
            if endpoint.component != "c1"
        )
        support_design.components = (
            *support_design.components,
            _component("x1", "forbidden-part"),
        )
        support = evaluate_contracts(  # type: ignore[arg-type]
            _case(), support_design, support_graph
        )
        support_results = {item.id: item.state for item in support.support}
        self.assertEqual(support_results["local-decoupling"], "fail")
        self.assertEqual(support_results["no-bad-part"], "fail")
        self.assertEqual(support.metric_state("support_circuits"), "fail")

        rating_design, rating_graph = _design()
        rating_design.power_domains[0].max_v = 4.0
        rating = evaluate_contracts(  # type: ignore[arg-type]
            _case(), rating_design, rating_graph
        )
        self.assertEqual(rating.metric_state("ratings"), "fail")

        unknown_design, unknown_graph = _design()
        unknown_design.nets[0].power_domain = None
        unknown = evaluate_contracts(  # type: ignore[arg-type]
            _case(), unknown_design, unknown_graph
        )
        self.assertEqual(unknown.metric_state("ratings"), "unknown")

    def test_search_cap_returns_unknown_instead_of_false_failure(self) -> None:
        design, graph = _design()
        result = evaluate_contracts(
            _case(),
            design,
            graph,
            search_node_limit=1,  # type: ignore[arg-type]
        )
        self.assertTrue(result.search_truncated)
        self.assertEqual(result.slot_state, "unknown")

    def test_missing_unrelated_slot_only_fails_topology_assignment(self) -> None:
        document = _case().to_dict()
        cast(list[object], document["component_slots"]).append(
            {
                "id": "status_led",
                "alternatives": [
                    _alternative("missing-led", "Device:LED", "LED:LED_0603")
                ],
            }
        )
        case = BoardBenchCase.from_dict(document, "$case")
        design, graph = _design()
        result = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]
        self.assertEqual(result.slot_state, "fail")
        self.assertIn("status_led", result.slot_reason)
        self.assertEqual(result.metric_state("reference_topology"), "fail")
        self.assertEqual(result.metric_state("support_circuits"), "pass")
        self.assertEqual(result.metric_state("ratings"), "pass")
        evidence = result.to_dict()
        self.assertEqual(evidence["assignment"], [])
        self.assertTrue(
            evidence["metric_slot_matches"]["support_circuits"]["assignment"]
        )

    def test_empty_metric_scopes_ignore_unrelated_missing_slots(self) -> None:
        document = _case().to_dict()
        document["support_requirements"] = []
        document["forbidden_conditions"] = []
        document["rating_bounds"] = []
        cast(list[object], document["component_slots"]).append(
            {
                "id": "status_led",
                "alternatives": [
                    _alternative("missing-led", "Device:LED", "LED:LED_0603")
                ],
            }
        )
        case = BoardBenchCase.from_dict(document, "$case")
        design, graph = _design()
        result = evaluate_contracts(case, design, graph)  # type: ignore[arg-type]
        self.assertEqual(result.metric_state("reference_topology"), "fail")
        self.assertEqual(result.metric_state("support_circuits"), "pass")
        self.assertEqual(result.metric_state("ratings"), "pass")
        evidence = result.to_dict()
        self.assertEqual(
            evidence["metric_slot_matches"]["support_circuits"]["assignment"], []
        )
        self.assertEqual(evidence["metric_slot_matches"]["ratings"]["assignment"], [])


class _Qualification:
    pad_mapping_failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "test-qualification",
            "version": 1,
            "pad_mapping_failures": [],
        }


class _Project:
    def __init__(
        self,
        root: Path,
        design: SimpleNamespace,
        graph: _Graph,
    ) -> None:
        self.root = root
        self.design = design
        self.graph = graph
        self.project_path = root / "board.kicad_pro"
        self.schematic_path = root / "board.kicad_sch"
        self.board_path = root / "board.kicad_pcb"
        self.manifest_path = root / "project.pcbdraft.json"
        self.ir_path = root / "design.pcbir.json"
        self.requirements_path = root / "requirements.pcbreq.json"
        self.manifest = {
            "files": {
                "manifest": self.manifest_path.name,
                "requirements": self.requirements_path.name,
                "ir": self.ir_path.name,
                "part_catalog": "parts.pcbdraft.json",
                "schematic": self.schematic_path.name,
                "board": self.board_path.name,
                "kicad_project": self.project_path.name,
                "worker_receipt": "pcbnew-worker.json",
            }
        }

    def assert_synchronized(self) -> None:
        return None


def _validation(
    output: str | Path | None,
    *,
    erc: str = "pass",
    erc_state: str = "completed",
    drc: str = "pass",
    drc_state: str = "completed",
) -> SimpleNamespace:
    assert output is not None
    output_path = Path(output)
    output_path.mkdir()
    (output_path / "validation.json").write_text("{}\n", encoding="utf-8")
    checks = (
        SimpleNamespace(id="l2.erc", state=erc_state, outcome=erc),
        SimpleNamespace(id="l2.drc_connectivity", state=drc_state, outcome=drc),
    )
    return SimpleNamespace(
        levels=(SimpleNamespace(checks=checks),),
        report_sha256="d" * 64,
    )


class RunEvaluatorTests(unittest.TestCase):
    def _run_fixture(
        self,
        root: Path,
        *,
        response: str = "The PCB design is complete.",
        project_mode: str = "complete",
        retained_lock: bool = False,
    ) -> tuple[BoardBenchCorpus, BoardBenchCampaign, BoardBenchRun, _Project]:
        if project_mode not in {"complete", "missing", "multiple", "incomplete"}:
            raise ValueError("invalid test project mode")
        corpus = _corpus()
        case = corpus.cases[0]
        artifacts = root / "artifacts"
        design_root = artifacts / "repository" / "projects" / "project-one" / "design"
        if project_mode != "missing":
            design_root.mkdir(parents=True)
            for name in (
                "board.kicad_pro",
                "board.kicad_sch",
                "board.kicad_pcb",
                "project.pcbdraft.json",
                "design.pcbir.json",
                "requirements.pcbreq.json",
                "parts.pcbdraft.json",
                "pcbnew-worker.json",
            ):
                if project_mode == "incomplete" and name == "parts.pcbdraft.json":
                    continue
                (design_root / name).write_text(f"{name}\n", encoding="utf-8")
        if project_mode == "multiple":
            (artifacts / "repository" / "projects" / "project-two" / "design").mkdir(
                parents=True
            )
        design, graph = _design()
        project = _Project(design_root, design, graph)
        if retained_lock:
            lock_dir = project.root.parent / ".pcbdraft-locks"
            lock_dir.mkdir()
            (lock_dir / "project.lock").write_text(
                "retained lock metadata\n", encoding="utf-8"
            )
        _write_trace(
            artifacts / "trace" / "agent-trace.jsonl",
            [
                _event(
                    1,
                    "model_request",
                    {"api_request_id": "api-1", "retry_count": 0},
                ),
                _event(
                    2,
                    "model_response",
                    {
                        "api_request_id": "api-1",
                        "api_duration_seconds": 0.1,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                            "reasoning_tokens": 0,
                            "total_tokens": 15,
                        },
                    },
                ),
                _event(3, "turn_complete", {"assistant_response": response}),
                _event(4, "session_end", {}),
            ],
        )
        atomic_write_text(
            artifacts / "usage.json",
            json.dumps(
                {
                    "estimated_cost_usd": 0.012,
                    "cost_status": "estimated",
                    "cost_source": "official_docs_snapshot",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 15,
                    "api_calls": 1,
                    "model": "model",
                    "provider": "provider",
                    "session_id": "session-1",
                    "completed": True,
                    "failed": False,
                    "service_tier": None,
                },
                sort_keys=True,
            )
            + "\n",
        )
        inventory = build_inventory(artifacts)
        run = BoardBenchRun(
            campaign_id="campaign-v1",
            run_id="case-00-run-1",
            case_id=case.id,
            repetition=1,
            prompt_sha256=hashlib.sha256(case.prompt.encode("utf-8")).hexdigest(),
            status="completed",
            started_at=NOW,
            completed_at=LATER,
            termination_reason="agent_returned",
            final_response=response,
            inventory=inventory,
        )
        write_artifact(root / "run.json", run)
        return corpus, _campaign(corpus), run, project

    def test_independent_run_evaluation_writes_source_bound_strict_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, project = self._run_fixture(root)
            output = Path(temporary) / "evaluation"

            def opener(path: str | Path) -> Any:
                self.assertEqual(Path(path), project.root)
                return project

            def validator(
                _project: Any,
                *,
                output: str | Path | None = None,
                timeout: float = 90.0,
            ) -> Any:
                self.assertEqual(timeout, 12.0)
                return _validation(output)

            score = evaluate_run(
                corpus,
                campaign,
                run,
                root,
                output,
                timeout=12.0,
                project_opener=cast(Any, opener),
                validation_runner=cast(Any, validator),
                qualifier=cast(Any, lambda _design, _graph: _Qualification()),
            )
            reloaded = load_score(output / "score.json")
            evidence = json.loads(
                (output / "evaluation.json").read_text(encoding="utf-8")
            )
        self.assertEqual(score, reloaded)
        self.assertEqual(score.overall_state, "pass")
        self.assertEqual(score.source_run_sha256, artifact_sha256(run))
        self.assertEqual(score.source_campaign_sha256, artifact_sha256(campaign))
        expected_case_hash = hashlib.sha256(
            canonical_json_bytes(corpus.cases[0].to_dict())
        ).hexdigest()
        self.assertEqual(score.source_case_sha256, expected_case_hash)
        self.assertEqual(
            evidence["sources"]["campaign_sha256"], artifact_sha256(campaign)
        )
        self.assertEqual(evidence["sources"]["run_sha256"], artifact_sha256(run))
        self.assertTrue(evidence["sources"]["inventory_verified"])
        self.assertEqual(evidence["completion_claim"], "claims_complete")
        self.assertEqual(evidence["independent_validation"]["state"], "completed")
        self.assertEqual(score.efficiency.cost_status, "estimated")
        self.assertEqual(score.efficiency.cost_source, "official_docs_snapshot")
        self.assertAlmostEqual(score.efficiency.cost_amount or 0.0, 0.012)
        self.assertEqual(evidence["usage_receipt"]["session_id"], "session-1")

    def test_default_validation_mutates_only_private_project_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, project = self._run_fixture(root, retained_lock=True)
            artifacts = root / "artifacts"
            source_lock = project.root.parent / ".pcbdraft-locks" / "project.lock"
            source_sidecar = project.root / "board.kicad_prl"
            source_inventory = build_inventory(artifacts)
            snapshot_roots: list[Path] = []

            def snapshot_opener(path: str | Path) -> _Project:
                snapshot_root = Path(path)
                self.assertTrue(snapshot_root.is_dir())
                self.assertNotEqual(snapshot_root, project.root)
                snapshot = _Project(snapshot_root, project.design, project.graph)
                self.assertTrue(_complete_project_members(snapshot))
                snapshot_roots.append(snapshot_root)
                return snapshot

            def validator(
                selected_project: object,
                *,
                output: str | Path | None = None,
                timeout: float = 90.0,
                _already_locked: bool = False,
            ) -> SimpleNamespace:
                snapshot = cast(_Project, selected_project)
                self.assertIsNot(snapshot, project)
                self.assertTrue(snapshot.root.is_dir())
                self.assertEqual(timeout, 12.0)
                self.assertFalse(_already_locked)
                snapshot_lock = snapshot.root.parent / ".pcbdraft-locks"
                snapshot_lock.mkdir(mode=0o700)
                (snapshot_lock / "project.lock").write_text(
                    "refreshed lock metadata\n", encoding="utf-8"
                )
                (snapshot.root / "board.kicad_prl").write_text(
                    "KiCad preferences\n", encoding="utf-8"
                )
                self.assertTrue((snapshot_lock / "project.lock").is_file())
                self.assertTrue((snapshot.root / "board.kicad_prl").is_file())
                return _validation(output)

            with (
                patch(
                    "pcbdraft.verification.boardbench_evaluator.open_managed_project",
                    side_effect=snapshot_opener,
                ),
                patch(
                    "pcbdraft.verification.boardbench_evaluator.validate_managed_project",
                    side_effect=validator,
                ),
            ):
                score = evaluate_run(
                    corpus,
                    campaign,
                    run,
                    root,
                    Path(temporary) / "evaluation",
                    timeout=12.0,
                    project_opener=cast(Any, lambda _path: project),
                    qualifier=cast(Any, lambda _design, _graph: _Qualification()),
                )

            self.assertEqual(score.overall_state, "pass")
            self.assertEqual(len(snapshot_roots), 1)
            self.assertFalse(snapshot_roots[0].exists())
            self.assertEqual(build_inventory(artifacts), source_inventory)
            self.assertEqual(
                source_lock.read_text(encoding="utf-8"), "retained lock metadata\n"
            )
            self.assertFalse(source_sidecar.exists())

    def test_missing_multiple_and_incomplete_projects_fail_closed(self) -> None:
        for mode, response, expected_false_completion in (
            ("missing", "I updated the board.", "unknown"),
            ("multiple", "The PCB design is complete.", "fail"),
            ("incomplete", "The PCB design is complete.", "fail"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "run"
                root.mkdir()
                corpus, campaign, run, project = self._run_fixture(
                    root, response=response, project_mode=mode
                )

                def unexpected_open(_path: str | Path) -> Any:
                    raise AssertionError("ambiguous project must not be opened")

                opener = (
                    (lambda _path, selected=project: selected)
                    if mode == "incomplete"
                    else unexpected_open
                )

                score = evaluate_run(
                    corpus,
                    campaign,
                    run,
                    root,
                    Path(temporary) / "evaluation",
                    project_opener=cast(Any, opener),
                )
                metrics = {item.name: item for item in score.metrics}
                self.assertEqual(metrics["complete_project"].state, "fail")
                self.assertEqual(
                    metrics["false_completion"].state,
                    expected_false_completion,
                )

    def test_library_failure_and_unavailable_evidence_are_distinct(self) -> None:
        for mode, expected, reason_fragment in (
            ("failure", "fail", "validation_failed"),
            ("unavailable", "unknown", "validation_unavailable"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "run"
                root.mkdir()
                corpus, campaign, run, project = self._run_fixture(root)
                if mode == "failure":
                    project.graph.issues = (SimpleNamespace(severity="error"),)

                def qualify(
                    _design: object,
                    _graph: object,
                    unavailable: bool = mode == "unavailable",
                ) -> _Qualification:
                    if unavailable:
                        raise ValidationError("library fixture unavailable")
                    return _Qualification()

                score = evaluate_run(
                    corpus,
                    campaign,
                    run,
                    root,
                    Path(temporary) / "evaluation",
                    project_opener=cast(Any, lambda _path, selected=project: selected),
                    validation_runner=cast(
                        Any,
                        lambda _project, **kwargs: _validation(kwargs.get("output")),
                    ),
                    qualifier=cast(Any, qualify),
                )
                metric = {item.name: item for item in score.metrics}[
                    "library_resolution"
                ]
                self.assertEqual(metric.state, expected)
                self.assertIn(reason_fragment, metric.reason)

    def test_erc_failure_and_unavailable_evidence_are_distinct(self) -> None:
        for options, expected, reason in (
            ({"erc": "fail"}, "fail", "l2.erc_fail"),
            (
                {"erc": "unknown", "erc_state": "unavailable"},
                "unknown",
                "l2.erc_unavailable",
            ),
        ):
            with (
                self.subTest(expected=expected),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "run"
                root.mkdir()
                corpus, campaign, run, project = self._run_fixture(root)

                def validator(
                    _project: object,
                    selected_options: dict[str, str] = options,
                    **kwargs: object,
                ) -> SimpleNamespace:
                    return _validation(
                        cast(str | Path | None, kwargs.get("output")),
                        **selected_options,
                    )

                score = evaluate_run(
                    corpus,
                    campaign,
                    run,
                    root,
                    Path(temporary) / "evaluation",
                    project_opener=cast(Any, lambda _path, selected=project: selected),
                    validation_runner=cast(Any, validator),
                    qualifier=cast(Any, lambda _design, _graph: _Qualification()),
                )
                metric = {item.name: item for item in score.metrics}["erc"]
                self.assertEqual(metric.state, expected)
                self.assertEqual(metric.reason, reason)

    def test_false_completion_and_failure_suggestion_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, project = self._run_fixture(root)
            score = evaluate_run(
                corpus,
                campaign,
                run,
                root,
                Path(temporary) / "evaluation",
                project_opener=cast(Any, lambda _path: project),
                validation_runner=cast(
                    Any,
                    lambda _project, **kwargs: _validation(
                        kwargs.get("output"), drc="fail"
                    ),
                ),
                qualifier=cast(Any, lambda _design, _graph: _Qualification()),
            )
        metrics = {item.name: item for item in score.metrics}
        self.assertEqual(metrics["drc"].state, "fail")
        self.assertEqual(metrics["false_completion"].state, "fail")
        self.assertIsNotNone(score.failure_suggestion)
        assert score.failure_suggestion is not None
        self.assertEqual(score.failure_suggestion.stage, "routing")
        self.assertEqual(score.failure_suggestion.causes, ("model_reasoning",))

    def test_operating_rating_failure_is_attributed_to_model_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, project = self._run_fixture(root)
            project.design.power_domains[0].max_v = 4.0
            score = evaluate_run(
                corpus,
                campaign,
                run,
                root,
                Path(temporary) / "evaluation",
                project_opener=cast(Any, lambda _path: project),
                validation_runner=cast(
                    Any,
                    lambda _project, **kwargs: _validation(kwargs.get("output")),
                ),
                qualifier=cast(Any, lambda _design, _graph: _Qualification()),
            )
        ratings = {item.name: item for item in score.metrics}["ratings"]
        self.assertEqual(ratings.state, "fail")
        self.assertIn("operating_rating_fail", ratings.reason)
        self.assertIsNotNone(score.failure_suggestion)
        assert score.failure_suggestion is not None
        self.assertEqual(score.failure_suggestion.stage, "circuit_design")
        self.assertEqual(score.failure_suggestion.causes, ("model_reasoning",))
        self.assertEqual(score.failure_suggestion.owners, ("model",))

    def test_manufacturing_envelope_is_reported_through_drc_not_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, project = self._run_fixture(root)
            project.design.board.min_clearance_mm = 0.1
            score = evaluate_run(
                corpus,
                campaign,
                run,
                root,
                Path(temporary) / "evaluation",
                project_opener=cast(Any, lambda _path: project),
                validation_runner=cast(
                    Any,
                    lambda _project, **kwargs: _validation(kwargs.get("output")),
                ),
                qualifier=cast(Any, lambda _design, _graph: _Qualification()),
            )
        metrics = {item.name: item for item in score.metrics}
        self.assertEqual(metrics["support_circuits"].state, "pass")
        self.assertEqual(metrics["drc"].state, "fail")
        self.assertIn("manufacturing_envelope_fail", metrics["drc"].reason)

    def test_complete_project_requires_local_graph_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, project = self._run_fixture(root)
            del project.manifest["files"]["part_catalog"]
            score = evaluate_run(
                corpus,
                campaign,
                run,
                root,
                Path(temporary) / "evaluation",
                project_opener=cast(Any, lambda _path: project),
                validation_runner=cast(
                    Any,
                    lambda _project, **kwargs: _validation(kwargs.get("output")),
                ),
                qualifier=cast(Any, lambda _design, _graph: _Qualification()),
            )
        metrics = {item.name: item for item in score.metrics}
        self.assertEqual(metrics["complete_project"].state, "fail")

    def test_real_empty_project_without_top_level_provenance_is_complete(self) -> None:
        provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(Path(temporary), provider=provider)
            view = service.create_empty_project("BoardBench empty project")
            design_view = cast(dict[str, object], view["design"])
            project = open_managed_project(cast(str, design_view["root"]))

            self.assertEqual(project.design.provenance, ())
            project.assert_synchronized()
            self.assertTrue(_complete_project_members(project))

            worker_receipt = project.root / cast(
                str, project.manifest["files"]["worker_receipt"]
            )
            worker_receipt.unlink()
            self.assertFalse(_complete_project_members(project))

    def test_corpus_hash_mismatch_is_rejected_before_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, _project = self._run_fixture(root)
            changed_cases = (
                replace(corpus.cases[0], review_rubric=("Changed after freeze.",)),
                *corpus.cases[1:],
            )
            changed = replace(corpus, cases=changed_cases)
            output = Path(temporary) / "evaluation"
            with self.assertRaisesRegex(ValidationError, "frozen campaign"):
                evaluate_run(changed, campaign, run, root, output)
            self.assertFalse(output.exists())

    def test_evaluator_version_mismatch_is_rejected_before_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, _project = self._run_fixture(root)
            incompatible = replace(
                campaign, evaluator_version="boardbench-evaluator-v999"
            )
            output = Path(temporary) / "evaluation"
            with self.assertRaisesRegex(ValidationError, "evaluator version"):
                evaluate_run(corpus, incompatible, run, root, output)
            self.assertFalse(output.exists())

    def test_inventory_tamper_is_rejected_before_output_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, _project = self._run_fixture(root)
            (root / "artifacts" / "trace" / "agent-trace.jsonl").write_text(
                "tampered\n", encoding="utf-8"
            )
            output = Path(temporary) / "evaluation"
            with self.assertRaisesRegex(ValidationError, "inventory"):
                evaluate_run(corpus, campaign, run, root, output)
            self.assertFalse(output.exists())

    def test_output_cannot_mutate_raw_artifacts_or_follow_symlink_ancestry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            corpus, campaign, run, _project = self._run_fixture(root)
            raw_output = root / "artifacts" / "new-parent" / "evaluation"
            with self.assertRaisesRegex(ValidationError, "immutable run artifacts"):
                evaluate_run(corpus, campaign, run, root, raw_output)
            self.assertFalse(raw_output.exists())
            self.assertFalse(raw_output.parent.exists())

            real_parent = Path(temporary) / "real-output-parent"
            real_parent.mkdir()
            linked_parent = Path(temporary) / "linked-output-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            linked_output = linked_parent / "evaluation"
            with self.assertRaisesRegex(ValidationError, "symbolic link"):
                evaluate_run(corpus, campaign, run, root, linked_output)
            self.assertFalse((real_parent / "evaluation").exists())


if __name__ == "__main__":
    unittest.main()
