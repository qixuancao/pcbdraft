from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
import threading
import unittest
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Self, cast
from unittest import mock

from pcbdraft.core.errors import ValidationError
from pcbdraft.verification.boardbench import (
    AI_REVIEWED_PILOT_COHORT,
    ARTIFACT_FILE_LIMIT,
    AUTOMATIC_METRICS,
    BOARD_CATEGORIES,
    CORPUS_FILE_LIMIT,
    COST_STATUSES,
    FAILURE_CAUSES,
    FAILURE_OWNERS,
    FAILURE_STAGE_VALUES,
    MAX_INVENTORY_DIRECTORIES,
    MAX_INVENTORY_FILE_BYTES,
    MAX_TEXT_BYTES,
    PHYSICAL_METRICS,
    PHYSICAL_STATES,
    REVIEW_OUTCOMES,
    TOKEN_SOURCES,
    TOKEN_STATUSES,
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
    InventoryEntry,
    allocate_campaign_directory,
    artifact_sha256,
    build_inventory,
    load_artifact,
    load_campaign,
    load_corpus,
    load_correction,
    load_hardware,
    load_report,
    load_review,
    load_run,
    load_score,
    load_selection,
    review_checklist_for_case,
    store_run,
    write_artifact,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
NOW = "2026-08-21T08:00:00Z"
LATER = "2026-08-21T08:01:00Z"


def _case(index: int, category: str) -> dict[str, object]:
    return {
        "id": f"case-{index:02d}",
        "category": category,
        "prompt": f"Build board {index} from this natural-language request.",
        "applicable_metrics": list(AUTOMATIC_METRICS),
        "review_rubric": ["Check the intended core function."],
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
            },
            {
                "id": "supply",
                "alternatives": [
                    {
                        "part_id": f"supply-{index:02d}",
                        "symbol": "Regulator_Test:Device",
                        "footprints": ["Package_Test:SOT-23-5"],
                    }
                ],
            },
            {
                "id": "decoupler",
                "alternatives": [
                    {
                        "part_id": f"capacitor-{index:02d}",
                        "symbol": "Device:C",
                        "footprints": ["Capacitor_Test:C_0603"],
                    }
                ],
            },
        ],
        "net_rules": [
            {
                "id": "power-net",
                "kind": "same_net",
                "endpoints": ["controller.VDD", "supply.VOUT"],
                "description": "The controller must be powered.",
            }
        ],
        "support_requirements": [
            {
                "id": "decoupling",
                "kind": "decoupling",
                "subjects": ["controller.VDD", "decoupler", "supply.VOUT"],
                "description": "Provide local decoupling.",
            }
        ],
        "forbidden_conditions": [],
        "rating_bounds": [
            {
                "id": "supply-voltage",
                "source": "operating",
                "subject": "controller.VDD",
                "fact_key": None,
                "quantity": "voltage",
                "unit": "V",
                "minimum": 3.0,
                "maximum": 3.6,
                "description": "Stay inside the controller supply rating.",
            }
        ],
        "manufacturing_constraints": [
            {
                "id": "clearance",
                "kind": "min_clearance_mm",
                "value_mm": 0.2,
                "description": "Use the recorded board-house minimum.",
            }
        ],
        "assembly_constraints": ["Use packages suitable for manual assembly."],
    }


def _corpus_document() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for category_index, category in enumerate(BOARD_CATEGORIES):
        for offset in range(4):
            cases.append(_case(category_index * 4 + offset, category))
    return {
        "schema": "pcbdraft-boardbench-corpus",
        "version": 1,
        "corpus_id": "boardbench-v1",
        "corpus_version": 1,
        "license": "CC0-1.0",
        "methodology": "Sealed natural-language end-to-end BoardBench baseline.",
        "cohort": "sealed_holdout_baseline",
        "cases": cases,
    }


def _campaign_document() -> dict[str, object]:
    runs = [
        {
            "case_id": f"case-{case_index:02d}",
            "repetition": repetition,
            "run_id": f"case-{case_index:02d}-run-{repetition}",
        }
        for case_index in range(20)
        for repetition in range(1, 4)
    ]
    return {
        "schema": "pcbdraft-boardbench-campaign",
        "version": 1,
        "campaign_id": "campaign-v1",
        "cohort": "sealed_holdout_baseline",
        "corpus_id": "boardbench-v1",
        "corpus_sha256": HASH_A,
        "created_at": NOW,
        "pcbdraft_commit": "abcdef1234567890",
        "dirty_state_sha256": HASH_B,
        "provider": "default-provider",
        "model": "default-model",
        "configuration_sha256": HASH_C,
        "kicad_version": "9.0.4",
        "python_version": "3.13.6",
        "platform": "linux-x86_64",
        "tool_registry_sha256": HASH_A,
        "wall_timeout_seconds": 900.0,
        "tool_call_budget": 128,
        "repetitions": 3,
        "evaluator_version": "boardbench-evaluator-v1",
        "runs": runs,
    }


def _run_document(status: str = "completed") -> dict[str, object]:
    if status == "planned":
        started_at = completed_at = reason = response = None
        inventory: list[dict[str, object]] = []
    elif status == "running":
        started_at, completed_at, reason, response = NOW, None, None, None
        inventory = []
    else:
        started_at, completed_at = NOW, LATER
        reason = "agent_returned" if status == "completed" else status
        response = "The board is complete." if status == "completed" else None
        inventory = [
            {"path": "project/design.kicad_sch", "size_bytes": 12, "sha256": HASH_A}
        ]
    return {
        "schema": "pcbdraft-boardbench-run",
        "version": 1,
        "campaign_id": "campaign-v1",
        "run_id": "case-00-run-1",
        "case_id": "case-00",
        "repetition": 1,
        "prompt_sha256": HASH_B,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "termination_reason": reason,
        "final_response": response,
        "inventory": inventory,
    }


def _score_document() -> dict[str, object]:
    return {
        "schema": "pcbdraft-boardbench-score",
        "version": 1,
        "campaign_id": "campaign-v1",
        "run_id": "case-00-run-1",
        "source_campaign_sha256": HASH_B,
        "source_case_sha256": HASH_C,
        "source_run_sha256": HASH_A,
        "evaluator_version": "boardbench-evaluator-v1",
        "scored_at": LATER,
        "overall_state": "pass",
        "metrics": [
            {"name": name, "state": "pass", "reason": "Independent evidence passed."}
            for name in AUTOMATIC_METRICS
        ],
        "efficiency": {
            "model_requests": 2,
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 10,
            "total_tokens": 150,
            "token_status": "reported",
            "token_source": "provider_usage",
            "cost_amount": None,
            "cost_currency": None,
            "cost_status": "subscription_included",
            "cost_source": "canonical_provider_pricing",
            "pcb_tool_calls": 8,
            "tool_call_counts": [
                {"name": "pcb_add_component", "status": "completed", "count": 6},
                {"name": "pcb_run_erc", "status": "completed", "count": 2},
            ],
            "provider_retries": 0,
            "provider_errors": 0,
            "tool_seconds": 2.0,
            "api_seconds": 4.0,
            "wall_seconds": 10.0,
            "failure_reason": None,
        },
        "failure_suggestion": None,
    }


def _review_document() -> dict[str, object]:
    case = BoardBenchCase.from_dict(_case(0, BOARD_CATEGORIES[0]), "$case")
    checklist = review_checklist_for_case(case)
    return {
        "schema": "pcbdraft-boardbench-review",
        "version": 3,
        "campaign_id": "campaign-v1",
        "run_id": "case-00-run-1",
        "source_campaign_sha256": HASH_B,
        "source_corpus_sha256": HASH_A,
        "source_case_sha256": HASH_C,
        "source_run_sha256": HASH_A,
        "source_score_sha256": HASH_B,
        "reviewer": "engineer-1",
        "reviewed_at": LATER,
        "outcome": "pass_without_schematic_change",
        "functional_correctness": "pass",
        "orderable_state": "pass",
        "orderability_evidence": [
            {
                "slot_ids": [slot.id],
                "manufacturer_part_number": f"FIXTURE-{slot.id.upper()}",
                "status": "orderable",
                "as_of": LATER,
                "source_kind": "manufacturer",
                "source_name": "Fixture manufacturer",
                "source_url": f"https://example.com/parts/{slot.id}",
                "note": "Dated fixture source inspected by the reviewer.",
            }
            for slot in case.component_slots
        ],
        "active_engineer_minutes": 12.5,
        "not_applicable_reason": None,
        "checklist": [
            {
                "id": item.id,
                "kind": item.kind,
                "requirement": item.requirement,
                "disposition": "pass",
                "evidence_note": "Inspected the generated schematic and net intent.",
            }
            for item in checklist
        ],
        "findings": [],
        "modifications": [],
        "final_failure": None,
    }


def _correction_document() -> dict[str, object]:
    return {
        "schema": "pcbdraft-boardbench-correction",
        "version": 1,
        "campaign_id": "campaign-v1",
        "run_id": "case-00-run-1",
        "source_run_sha256": HASH_A,
        "source_review_sha256": HASH_B,
        "created_at": LATER,
        "generated_snapshot_sha256": HASH_A,
        "corrected_snapshot_sha256": HASH_B,
        "manufacturing_candidate_sha256": HASH_C,
        "decision_ids": ["change-1"],
        "changes": [
            {
                "area": "components",
                "operation": "changed",
                "identity": "C1",
                "before_sha256": HASH_A,
                "after_sha256": HASH_B,
                "description": "Changed the decoupling capacitor value.",
            }
        ],
        "inventory": [
            {"path": "corrected/design.kicad_sch", "size_bytes": 12, "sha256": HASH_C}
        ],
    }


def _hardware_document(category: str = BOARD_CATEGORIES[0]) -> dict[str, object]:
    return {
        "schema": "pcbdraft-boardbench-hardware",
        "version": 1,
        "campaign_id": "campaign-v1",
        "run_id": "case-00-run-1",
        "source_kind": "correction",
        "source_artifact_sha256": HASH_C,
        "category": category,
        "board_revision": "A",
        "board_serial": "unit-001",
        "revision_count": 1,
        "fabricator": "Example board house",
        "operator": "engineer-1",
        "observed_at": LATER,
        "fabricator_accepted": "pass",
        "solderability": "pass",
        "first_power_no_short": "pass",
        "firmware_download": "pass",
        "core_function": "pass",
        "rails": [
            {
                "name": "3V3",
                "unit": "V",
                "expected_minimum": 3.2,
                "expected_maximum": 3.4,
                "measured": 3.31,
                "state": "pass",
            }
        ],
        "notes": "Current-limited first power at the documented setting.",
        "attachments": [
            {"path": "lab/3v3-meter.jpg", "size_bytes": 100, "sha256": HASH_A}
        ],
    }


def _selection_document() -> dict[str, object]:
    return {
        "schema": "pcbdraft-boardbench-selection",
        "version": 1,
        "campaign_id": "campaign-v1",
        "created_at": LATER,
        "selector": "engineer-1",
        "selections": [
            {
                "category": category,
                "run_id": f"selected-{index}",
                "source_artifact_sha256": HASH_A,
                "rationale": "Representative engineering risk for this category.",
            }
            for index, category in enumerate(BOARD_CATEGORIES)
        ],
    }


def _named_counts(
    values: Iterable[str], selected: dict[str, int]
) -> list[dict[str, object]]:
    return [
        {"value": value, "count": selected.get(value, 0)} for value in sorted(values)
    ]


def _set_named_count(counts: list[dict[str, object]], value: str, count: int) -> None:
    for item in counts:
        if item["value"] == value:
            item["count"] = count
            return
    raise AssertionError(f"missing named count: {value}")


def _set_stage_count(counts: list[dict[str, object]], stage: str, count: int) -> None:
    for item in counts:
        if item["stage"] == stage:
            item["count"] = count
            return
    raise AssertionError(f"missing stage count: {stage}")


def _summary(observed: int, missing: int, value: float = 0.0) -> dict[str, object]:
    if observed == 0:
        return {
            "observed_count": 0,
            "missing_count": missing,
            "sum": None,
            "minimum": None,
            "maximum": None,
            "mean": None,
        }
    return {
        "observed_count": observed,
        "missing_count": missing,
        "sum": observed * value,
        "minimum": value,
        "maximum": value,
        "mean": value,
    }


def _report_slice(
    scope: str,
    identity: str,
    category: str | None,
    planned: int,
    physical_records: int,
    *,
    sealed: bool,
) -> dict[str, object]:
    terminal = planned if sealed else 0
    automatic_outcomes = {"pass": planned} if sealed else {"unknown": planned}
    review_outcomes = (
        {"pass_without_schematic_change": planned}
        if sealed
        else {"not_reviewed": planned}
    )
    token_statuses = {"reported": planned} if sealed else {"unknown": planned}
    token_sources = {"provider_usage": planned} if sealed else {"unavailable": planned}
    cost_statuses = (
        {"subscription_included": planned} if sealed else {"unknown": planned}
    )
    cost_source = "canonical_provider_pricing" if sealed else "unavailable"
    observed = planned if sealed else 0
    missing = 0 if sealed else planned
    failed = 0 if sealed else planned
    failure_stages = {"unclassified": failed} if failed else {}
    failure_causes = {"unclassified": failed} if failed else {}
    failure_owners = {"unclassified": failed} if failed else {}
    return {
        "scope": scope,
        "id": identity,
        "category": category,
        "planned_runs": planned,
        "terminal_runs": terminal,
        "automatic_outcomes": _named_counts(
            {"pass", "fail", "unknown"}, automatic_outcomes
        ),
        "automatic_metrics": [
            {
                "name": name,
                "total": planned,
                "passed": planned if sealed else 0,
                "failed": 0,
                "unknown": 0 if sealed else planned,
                "not_applicable": 0,
            }
            for name in AUTOMATIC_METRICS
        ],
        "reviews": {
            "denominator": planned,
            "outcomes": _named_counts(REVIEW_OUTCOMES, review_outcomes),
            "corrections_required": 0,
            "corrections_present": 0,
            "modifications": _summary(observed, missing, 0.0),
            "active_minutes": _summary(observed, missing, 10.0),
        },
        "failures": {
            "failed_runs": failed,
            "stages": [
                {"stage": stage, "count": failure_stages.get(stage, 0)}
                for stage in sorted(FAILURE_STAGE_VALUES)
            ],
            "causes": _named_counts(FAILURE_CAUSES, failure_causes),
            "owners": _named_counts(FAILURE_OWNERS, failure_owners),
        },
        "efficiency": {
            "denominator": planned,
            "token_statuses": _named_counts(TOKEN_STATUSES, token_statuses),
            "token_sources": _named_counts(TOKEN_SOURCES, token_sources),
            "cost_statuses": _named_counts(COST_STATUSES, cost_statuses),
            "cost_sources": [{"value": cost_source, "count": planned}],
            "model_requests": _summary(observed, missing, 2.0),
            "total_tokens": _summary(observed, missing, 150.0),
            "cost_amount": _summary(0, planned),
            "cost_currency": None,
            "pcb_tool_calls": _summary(observed, missing, 8.0),
            "tool_call_counts": (
                [
                    {
                        "name": "pcb_add_component",
                        "status": "completed",
                        "count": 6 * planned,
                    },
                    {
                        "name": "pcb_run_erc",
                        "status": "completed",
                        "count": 2 * planned,
                    },
                ]
                if sealed
                else []
            ),
            "provider_retries": _summary(observed, missing, 0.0),
            "provider_errors": _summary(observed, missing, 0.0),
            "tool_seconds": _summary(observed, missing, 2.0),
            "api_seconds": _summary(observed, missing, 4.0),
            "wall_seconds": _summary(observed, missing, 10.0),
            "failure_reasons": [],
            "missing_failure_reason_count": planned,
        },
        "physical": {
            "records": physical_records,
            "metrics": [
                {
                    "name": name,
                    "outcomes": _named_counts(
                        PHYSICAL_STATES, {"pass": physical_records}
                    ),
                }
                for name in PHYSICAL_METRICS
            ],
            "revision_counts": _summary(physical_records, 0, 1.0),
        },
    }


def _report_document(*, sealed: bool = True) -> dict[str, object]:
    category_slices = [
        _report_slice(
            "category",
            category,
            category,
            12,
            1 if sealed else 0,
            sealed=sealed,
        )
        for category in BOARD_CATEGORIES
    ]
    case_slices = [
        _report_slice(
            "case",
            f"case-{index:02d}",
            BOARD_CATEGORIES[index // 4],
            3,
            1 if sealed and index % 4 == 0 else 0,
            sealed=sealed,
        )
        for index in range(20)
    ]
    return {
        "schema": "pcbdraft-boardbench-report",
        "version": 1,
        "campaign_id": "campaign-v1",
        "campaign_sha256": HASH_A,
        "cohort": "sealed_holdout_baseline",
        "evaluator_version": "boardbench-evaluator-v1",
        "generated_at": LATER,
        "slices": [
            _report_slice(
                "overall", "overall", None, 60, 5 if sealed else 0, sealed=sealed
            ),
            *category_slices,
            *case_slices,
        ],
        "sealed": sealed,
    }


class BoardBenchContractTests(unittest.TestCase):
    def test_every_artifact_round_trips_through_strict_dataclass(self) -> None:
        fixtures = (
            (BoardBenchCorpus, _corpus_document()),
            (BoardBenchCampaign, _campaign_document()),
            (BoardBenchRun, _run_document()),
            (BoardBenchScore, _score_document()),
            (BoardBenchReview, _review_document()),
            (BoardBenchCorrection, _correction_document()),
            (BoardBenchHardware, _hardware_document()),
            (BoardBenchSelection, _selection_document()),
            (BoardBenchReport, _report_document()),
        )
        for artifact_type, document in fixtures:
            with self.subTest(schema=document["schema"]):
                artifact = artifact_type.from_dict(document)
                self.assertEqual(document, artifact.to_dict())
                self.assertEqual(64, len(artifact_sha256(artifact)))

    def test_corpus_requires_twenty_unique_cases_and_four_per_category(self) -> None:
        too_short = _corpus_document()
        too_short["cases"] = too_short["cases"][:-1]  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "20"):
            BoardBenchCorpus.from_dict(too_short)

        duplicated = _corpus_document()
        cases = duplicated["cases"]  # type: ignore[assignment]
        cases[1]["id"] = cases[0]["id"]  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            BoardBenchCorpus.from_dict(duplicated)

        unbalanced = _corpus_document()
        unbalanced["cases"][0]["category"] = BOARD_CATEGORIES[1]  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "four cases per category"):
            BoardBenchCorpus.from_dict(unbalanced)

    def test_ai_reviewed_pilot_is_explicit_and_cannot_claim_a_sealed_baseline(
        self,
    ) -> None:
        corpus_document = _corpus_document()
        corpus_document["cohort"] = AI_REVIEWED_PILOT_COHORT
        corpus = BoardBenchCorpus.from_dict(corpus_document)
        self.assertEqual(AI_REVIEWED_PILOT_COHORT, corpus.cohort)

        campaign_document = _campaign_document()
        campaign_document["cohort"] = AI_REVIEWED_PILOT_COHORT
        campaign = BoardBenchCampaign.from_dict(campaign_document)
        self.assertEqual(AI_REVIEWED_PILOT_COHORT, campaign.cohort)

        draft_document = _report_document(sealed=False)
        draft_document["cohort"] = AI_REVIEWED_PILOT_COHORT
        draft = BoardBenchReport.from_dict(draft_document)
        self.assertFalse(draft.sealed)

        sealed_document = _report_document()
        sealed_document["cohort"] = AI_REVIEWED_PILOT_COHORT
        with self.assertRaisesRegex(ValidationError, "cannot claim a sealed"):
            BoardBenchReport.from_dict(sealed_document)

    def test_campaign_requires_exact_twenty_by_three_matrix(self) -> None:
        duplicate_pair = _campaign_document()
        duplicate_pair["runs"][1]["case_id"] = "case-00"  # type: ignore[index]
        duplicate_pair["runs"][1]["repetition"] = 1  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "pairs must be unique"):
            BoardBenchCampaign.from_dict(duplicate_pair)

        missing = _campaign_document()
        missing["runs"] = missing["runs"][:-1]  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "60"):
            BoardBenchCampaign.from_dict(missing)

    def test_unknown_fields_and_versions_fail_closed_at_nested_boundaries(self) -> None:
        outer = _run_document()
        outer["surprise"] = True
        with self.assertRaisesRegex(ValidationError, "unexpected fields"):
            BoardBenchRun.from_dict(outer)

        nested = _corpus_document()
        nested["cases"][0]["component_slots"][0]["surprise"] = True  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "unexpected fields"):
            BoardBenchCorpus.from_dict(nested)

        version = _campaign_document()
        version["version"] = 2
        with self.assertRaisesRegex(ValidationError, "schema/version"):
            BoardBenchCampaign.from_dict(version)

        boolean_version = _campaign_document()
        boolean_version["version"] = True
        with self.assertRaisesRegex(ValidationError, "schema/version"):
            BoardBenchCampaign.from_dict(boolean_version)

        for legacy_version in (1, 2):
            with self.subTest(legacy_review_version=legacy_version):
                legacy_review = _review_document()
                legacy_review["version"] = legacy_version
                with self.assertRaisesRegex(ValidationError, "schema/version"):
                    BoardBenchReview.from_dict(legacy_review)

    def test_review_checklist_rejects_missing_duplicate_and_unjustified_items(
        self,
    ) -> None:
        missing = _review_document()
        missing["checklist"] = missing["checklist"][:-1]  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "2..256"):
            BoardBenchReview.from_dict(missing)

        duplicate = _review_document()
        checklist = duplicate["checklist"]  # type: ignore[assignment]
        checklist.append(dict(checklist[0]))  # type: ignore[attr-defined, index]
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            BoardBenchReview.from_dict(duplicate)

        unjustified = _review_document()
        unjustified["checklist"][0].update(  # type: ignore[index, union-attr]
            {"disposition": "not_applicable", "evidence_note": None}
        )
        with self.assertRaisesRegex(ValidationError, "nonempty evidence note"):
            BoardBenchReview.from_dict(unjustified)

    def test_passing_review_requires_positive_outcomes_for_all_applicable_items(
        self,
    ) -> None:
        for label, mutation, error in (
            (
                "functional",
                lambda review: review.update(functional_correctness="unknown"),
                "functional correctness pass",
            ),
            (
                "orderable",
                lambda review: review.update(orderable_state="unknown"),
                "contradicts orderability evidence",
            ),
            (
                "checklist",
                lambda review: review["checklist"][0].update(  # type: ignore[index, union-attr]
                    disposition="fail",
                    evidence_note="The schematic violates this requirement.",
                ),
                "every case-authored checklist item to pass",
            ),
        ):
            review = _review_document()
            mutation(review)
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(ValidationError, error),
            ):
                BoardBenchReview.from_dict(review)

        skipped_applicable = _review_document()
        skipped_applicable["checklist"][0].update(  # type: ignore[index, union-attr]
            disposition="not_applicable",
            evidence_note="Reviewer attempted to skip a source-authored obligation.",
        )
        with self.assertRaisesRegex(ValidationError, "cannot skip"):
            BoardBenchReview.from_dict(skipped_applicable)

        unsupported_orderability = _review_document()
        unsupported_orderability["orderability_evidence"] = []
        with self.assertRaisesRegex(ValidationError, "orderability evidence"):
            BoardBenchReview.from_dict(unsupported_orderability)

    def test_orderability_evidence_requires_dated_attributed_https_sources(
        self,
    ) -> None:
        for label, mutation, error in (
            (
                "http source",
                lambda item: item.update(source_url="http://example.com/part"),
                "HTTPS URL",
            ),
            (
                "missing MPN",
                lambda item: item.update(manufacturer_part_number=""),
                "manufacturer_part_number",
            ),
            (
                "invalid status",
                lambda item: item.update(status="in_stock_now"),
                "status",
            ),
        ):
            review = _review_document()
            evidence = review["orderability_evidence"][0]  # type: ignore[index]
            mutation(evidence)  # type: ignore[arg-type]
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(ValidationError, error),
            ):
                BoardBenchReview.from_dict(review)

        inconsistent = _review_document()
        inconsistent["orderability_evidence"][0]["status"] = "unknown"  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "contradicts"):
            BoardBenchReview.from_dict(inconsistent)

    def test_fixed_metric_and_failure_taxonomies_reject_unknown_values(self) -> None:
        score = _score_document()
        score["metrics"][0]["name"] = "looks_pretty"  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "metric"):
            BoardBenchScore.from_dict(score)

        review = _review_document()
        review["outcome"] = "fail"
        review["functional_correctness"] = "fail"
        review["final_failure"] = {
            "stage": "marketing",
            "causes": ["model_reasoning"],
            "owners": ["model"],
            "reason": "Unsupported stage.",
        }
        with self.assertRaisesRegex(ValidationError, "stage"):
            BoardBenchReview.from_dict(review)

    def test_score_requires_exact_campaign_case_and_run_provenance_hashes(self) -> None:
        score = BoardBenchScore.from_dict(_score_document())
        self.assertEqual(HASH_B, score.source_campaign_sha256)
        self.assertEqual(HASH_C, score.source_case_sha256)
        self.assertEqual(HASH_A, score.source_run_sha256)

        for field in (
            "source_campaign_sha256",
            "source_case_sha256",
            "source_run_sha256",
        ):
            with self.subTest(malformed=field):
                malformed = _score_document()
                malformed[field] = "A" * 64
                with self.assertRaisesRegex(ValidationError, "lowercase SHA-256"):
                    BoardBenchScore.from_dict(malformed)

            with self.subTest(missing=field):
                missing = _score_document()
                missing.pop(field)
                with self.assertRaisesRegex(ValidationError, "unexpected fields"):
                    BoardBenchScore.from_dict(missing)

    def test_efficiency_requires_typed_token_evidence_and_exact_tool_breakdown(
        self,
    ) -> None:
        unknown = _score_document()
        unknown_efficiency = cast(dict[str, Any], unknown["efficiency"])
        unknown_efficiency.update(
            {
                "input_tokens": None,
                "output_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": None,
                "token_status": "unknown",
                "token_source": "provider_usage",
            }
        )
        with self.assertRaisesRegex(ValidationError, "source must be unavailable"):
            BoardBenchScore.from_dict(unknown)

        mismatched = _score_document()
        cast(dict[str, Any], mismatched["efficiency"])["pcb_tool_calls"] = 7
        with self.assertRaisesRegex(ValidationError, "breakdown does not match"):
            BoardBenchScore.from_dict(mismatched)

        duplicated = _score_document()
        duplicated_efficiency = cast(dict[str, Any], duplicated["efficiency"])
        tool_counts = cast(
            list[dict[str, object]], duplicated_efficiency["tool_call_counts"]
        )
        tool_counts.append(copy.deepcopy(tool_counts[0]))
        duplicated_efficiency["pcb_tool_calls"] = 14
        with self.assertRaisesRegex(ValidationError, "duplicate pairs"):
            BoardBenchScore.from_dict(duplicated)

        unbounded_name = _score_document()
        unbounded_efficiency = cast(dict[str, Any], unbounded_name["efficiency"])
        cast(list[dict[str, object]], unbounded_efficiency["tool_call_counts"])[0][
            "name"
        ] = "shell_exec"
        with self.assertRaisesRegex(ValidationError, "PCB tool"):
            BoardBenchScore.from_dict(unbounded_name)

        false_partial = _score_document()
        cast(dict[str, Any], false_partial["efficiency"]).update(
            {"token_status": "partial"}
        )
        with self.assertRaisesRegex(ValidationError, "missing token count"):
            BoardBenchScore.from_dict(false_partial)

        zero_breakdown = _score_document()
        zero_efficiency = cast(dict[str, Any], zero_breakdown["efficiency"])
        tool_counts = cast(list[dict[str, object]], zero_efficiency["tool_call_counts"])
        zero_efficiency["pcb_tool_calls"] = 2
        tool_counts[0]["count"] = 0
        with self.assertRaisesRegex(ValidationError, r"\[1, 1000000\]"):
            BoardBenchScore.from_dict(zero_breakdown)

    def test_report_requires_complete_additive_case_category_overall_slices(
        self,
    ) -> None:
        missing = _report_document()
        cast(list[dict[str, object]], missing["slices"]).pop()
        with self.assertRaisesRegex(ValidationError, "26"):
            BoardBenchReport.from_dict(missing)

        drifted = _report_document()
        overall = cast(list[dict[str, Any]], drifted["slices"])[0]
        first_metric = cast(list[dict[str, object]], overall["automatic_metrics"])[0]
        first_metric["passed"] = 59
        first_metric["unknown"] = 1
        with self.assertRaisesRegex(ValidationError, "overall slice does not sum"):
            BoardBenchReport.from_dict(drifted)

    def test_report_efficiency_aggregate_round_trips_failure_reason_coverage(
        self,
    ) -> None:
        document = _report_document(sealed=False)
        slices = cast(list[dict[str, Any]], document["slices"])
        for index, denominator in ((0, 60), (1, 12), (6, 3)):
            efficiency = cast(dict[str, Any], slices[index]["efficiency"])
            efficiency["failure_reasons"] = [{"value": "provider_timeout", "count": 1}]
            efficiency["missing_failure_reason_count"] = denominator - 1

        report = BoardBenchReport.from_dict(document)

        self.assertEqual(document, report.to_dict())
        self.assertEqual(
            "provider_timeout", report.slices[0].efficiency.failure_reasons[0].value
        )
        self.assertEqual(59, report.slices[0].efficiency.missing_failure_reason_count)

    def test_report_efficiency_aggregate_rejects_incomplete_or_unbounded_evidence(
        self,
    ) -> None:
        bad_token_source = _report_document()
        efficiency = cast(
            dict[str, Any],
            cast(list[dict[str, Any]], bad_token_source["slices"])[0]["efficiency"],
        )
        _set_named_count(
            cast(list[dict[str, object]], efficiency["token_sources"]),
            "provider_usage",
            59,
        )
        _set_named_count(
            cast(list[dict[str, object]], efficiency["token_sources"]),
            "unavailable",
            1,
        )
        with self.assertRaisesRegex(ValidationError, "token-source coverage"):
            BoardBenchReport.from_dict(bad_token_source)

        duplicate_cost_source = _report_document()
        efficiency = cast(
            dict[str, Any],
            cast(list[dict[str, Any]], duplicate_cost_source["slices"])[0][
                "efficiency"
            ],
        )
        efficiency["cost_sources"] = [
            {"value": "canonical_provider_pricing", "count": 30},
            {"value": "canonical_provider_pricing", "count": 30},
        ]
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            BoardBenchReport.from_dict(duplicate_cost_source)

        oversized_cost_source = _report_document()
        efficiency = cast(
            dict[str, Any],
            cast(list[dict[str, Any]], oversized_cost_source["slices"])[0][
                "efficiency"
            ],
        )
        efficiency["cost_sources"] = [{"value": "x" * 513, "count": 60}]
        with self.assertRaisesRegex(ValidationError, "cost source is invalid"):
            BoardBenchReport.from_dict(oversized_cost_source)

        for field, label in (
            ("provider_retries", "provider retries"),
            ("provider_errors", "provider errors"),
            ("tool_seconds", "tool seconds"),
            ("api_seconds", "API seconds"),
        ):
            with self.subTest(missing_numeric_coverage=field):
                missing_numeric = _report_document()
                efficiency = cast(
                    dict[str, Any],
                    cast(list[dict[str, Any]], missing_numeric["slices"])[0][
                        "efficiency"
                    ],
                )
                efficiency[field] = _summary(59, 0)
                with self.assertRaisesRegex(ValidationError, f"{label} counts"):
                    BoardBenchReport.from_dict(missing_numeric)

        fractional_errors = _report_document()
        efficiency = cast(
            dict[str, Any],
            cast(list[dict[str, Any]], fractional_errors["slices"])[0]["efficiency"],
        )
        efficiency["provider_errors"] = _summary(60, 0, 0.5)
        with self.assertRaisesRegex(ValidationError, "provider errors must contain"):
            BoardBenchReport.from_dict(fractional_errors)

        missing_reason_coverage = _report_document()
        efficiency = cast(
            dict[str, Any],
            cast(list[dict[str, Any]], missing_reason_coverage["slices"])[0][
                "efficiency"
            ],
        )
        efficiency["missing_failure_reason_count"] = 59
        with self.assertRaisesRegex(ValidationError, "failure reasons counts"):
            BoardBenchReport.from_dict(missing_reason_coverage)

        duplicate_reason = _report_document()
        efficiency = cast(
            dict[str, Any],
            cast(list[dict[str, Any]], duplicate_reason["slices"])[0]["efficiency"],
        )
        efficiency["failure_reasons"] = [
            {"value": "provider_timeout", "count": 1},
            {"value": "provider_timeout", "count": 1},
        ]
        efficiency["missing_failure_reason_count"] = 58
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            BoardBenchReport.from_dict(duplicate_reason)

        oversized_reason = _report_document()
        efficiency = cast(
            dict[str, Any],
            cast(list[dict[str, Any]], oversized_reason["slices"])[0]["efficiency"],
        )
        efficiency["failure_reasons"] = [
            {"value": "x" * (MAX_TEXT_BYTES + 1), "count": 1}
        ]
        efficiency["missing_failure_reason_count"] = 59
        with self.assertRaisesRegex(ValidationError, "value is invalid"):
            BoardBenchReport.from_dict(oversized_reason)

    def test_report_efficiency_dimensions_are_additive_across_slices(self) -> None:
        for dimension in (
            "token_sources",
            "cost_sources",
            "provider_retries",
            "provider_errors",
            "tool_seconds",
            "api_seconds",
            "failure_reasons",
        ):
            with self.subTest(dimension=dimension):
                document = _report_document()
                overall = cast(list[dict[str, Any]], document["slices"])[0]
                efficiency = cast(dict[str, Any], overall["efficiency"])
                if dimension == "token_sources":
                    counts = cast(list[dict[str, object]], efficiency["token_sources"])
                    _set_named_count(counts, "provider_usage", 59)
                    _set_named_count(counts, "trace_reduction", 1)
                elif dimension == "cost_sources":
                    efficiency["cost_sources"] = [
                        {"value": "canonical_provider_pricing", "count": 59},
                        {"value": "subscription_contract", "count": 1},
                    ]
                elif dimension == "failure_reasons":
                    efficiency["failure_reasons"] = [
                        {"value": "provider_timeout", "count": 1}
                    ]
                    efficiency["missing_failure_reason_count"] = 59
                else:
                    values = {
                        "provider_retries": 1.0,
                        "provider_errors": 1.0,
                        "tool_seconds": 3.0,
                        "api_seconds": 5.0,
                    }
                    efficiency[dimension] = _summary(60, 0, values[dimension])
                with self.assertRaisesRegex(
                    ValidationError, r"overall slice does not sum at efficiency\."
                ):
                    BoardBenchReport.from_dict(document)

    def test_sealed_report_rejects_missing_review_and_correction_evidence(self) -> None:
        missing_reviews = _report_document(sealed=False)
        missing_reviews["sealed"] = True
        with self.assertRaisesRegex(ValidationError, "terminal runs or reviews"):
            BoardBenchReport.from_dict(missing_reviews)

        missing_correction = _report_document()
        slices = cast(list[dict[str, Any]], missing_correction["slices"])
        for index, denominator in ((0, 60), (1, 12), (6, 3)):
            reviews = cast(dict[str, Any], slices[index]["reviews"])
            reviews["corrections_required"] = 1
            reviews["corrections_present"] = 0
            reviews["modifications"] = {
                "observed_count": denominator,
                "missing_count": 0,
                "sum": 1.0,
                "minimum": 0.0,
                "maximum": 1.0,
                "mean": 1.0 / denominator,
            }
        with self.assertRaisesRegex(ValidationError, "missing correction"):
            BoardBenchReport.from_dict(missing_correction)

        hidden_required_correction = _report_document()
        slices = cast(list[dict[str, Any]], hidden_required_correction["slices"])
        for index, denominator in ((0, 60), (1, 12), (6, 3)):
            reviews = cast(dict[str, Any], slices[index]["reviews"])
            outcomes = cast(list[dict[str, object]], reviews["outcomes"])
            _set_named_count(outcomes, "pass_without_schematic_change", denominator - 1)
            _set_named_count(outcomes, "pass_after_changes", 1)
            reviews["modifications"] = {
                "observed_count": denominator,
                "missing_count": 0,
                "sum": 1.0,
                "minimum": 0.0,
                "maximum": 1.0,
                "mean": 1.0 / denominator,
            }
        with self.assertRaisesRegex(ValidationError, "require correction artifacts"):
            BoardBenchReport.from_dict(hidden_required_correction)

    def test_report_represents_human_only_failures_and_requires_classification(
        self,
    ) -> None:
        human_failure = _report_document()
        slices = cast(list[dict[str, Any]], human_failure["slices"])
        for index, denominator in ((0, 60), (1, 12), (6, 3)):
            current = slices[index]
            reviews = cast(dict[str, Any], current["reviews"])
            outcomes = cast(list[dict[str, object]], reviews["outcomes"])
            _set_named_count(outcomes, "pass_without_schematic_change", denominator - 1)
            _set_named_count(outcomes, "pass_after_changes", 1)
            reviews["corrections_required"] = 1
            reviews["corrections_present"] = 1
            reviews["modifications"] = {
                "observed_count": denominator,
                "missing_count": 0,
                "sum": 1.0,
                "minimum": 0.0,
                "maximum": 1.0,
                "mean": 1.0 / denominator,
            }
            failures = cast(dict[str, Any], current["failures"])
            failures["failed_runs"] = 1
            _set_stage_count(
                cast(list[dict[str, object]], failures["stages"]),
                "circuit_design",
                1,
            )
            _set_named_count(
                cast(list[dict[str, object]], failures["causes"]),
                "model_reasoning",
                1,
            )
            _set_named_count(
                cast(list[dict[str, object]], failures["owners"]), "model", 1
            )
        BoardBenchReport.from_dict(human_failure)

        missing_classification = copy.deepcopy(human_failure)
        slices = cast(list[dict[str, Any]], missing_classification["slices"])
        for index in (0, 1, 6):
            failures = cast(dict[str, Any], slices[index]["failures"])
            failures["failed_runs"] = 0
            _set_stage_count(
                cast(list[dict[str, object]], failures["stages"]),
                "circuit_design",
                0,
            )
            _set_named_count(
                cast(list[dict[str, object]], failures["causes"]),
                "model_reasoning",
                0,
            )
            _set_named_count(
                cast(list[dict[str, object]], failures["owners"]), "model", 0
            )
        with self.assertRaisesRegex(ValidationError, "failure coverage"):
            BoardBenchReport.from_dict(missing_classification)

    def test_sealed_report_rejects_unclassified_and_untested_evidence(self) -> None:
        unclassified = _report_document()
        slices = cast(list[dict[str, Any]], unclassified["slices"])
        for index, denominator in ((0, 60), (1, 12), (6, 3)):
            current = slices[index]
            outcomes = cast(list[dict[str, object]], current["automatic_outcomes"])
            _set_named_count(outcomes, "pass", denominator - 1)
            _set_named_count(outcomes, "fail", 1)
            failures = cast(dict[str, Any], current["failures"])
            failures["failed_runs"] = 1
            _set_stage_count(
                cast(list[dict[str, object]], failures["stages"]),
                "unclassified",
                1,
            )
            _set_named_count(
                cast(list[dict[str, object]], failures["causes"]),
                "unclassified",
                1,
            )
            _set_named_count(
                cast(list[dict[str, object]], failures["owners"]),
                "unclassified",
                1,
            )
        with self.assertRaisesRegex(ValidationError, "unclassified failures"):
            BoardBenchReport.from_dict(unclassified)

        untested = _report_document()
        slices = cast(list[dict[str, Any]], untested["slices"])
        for index, passed in ((0, 5), (1, 1), (6, 1)):
            physical = cast(dict[str, Any], slices[index]["physical"])
            metrics = cast(list[dict[str, Any]], physical["metrics"])
            core = next(item for item in metrics if item["name"] == "core_function")
            outcomes = cast(list[dict[str, object]], core["outcomes"])
            _set_named_count(outcomes, "pass", passed - 1)
            _set_named_count(outcomes, "not_tested", 1)
        with self.assertRaisesRegex(ValidationError, "untested physical"):
            BoardBenchReport.from_dict(untested)

        omitted = _report_document()
        slices = cast(list[dict[str, Any]], omitted["slices"])
        for index, passed in ((0, 5), (1, 1), (6, 1)):
            physical = cast(dict[str, Any], slices[index]["physical"])
            metrics = cast(list[dict[str, Any]], physical["metrics"])
            core = next(item for item in metrics if item["name"] == "core_function")
            outcomes = cast(list[dict[str, object]], core["outcomes"])
            _set_named_count(outcomes, "pass", passed - 1)
            _set_named_count(outcomes, "not_applicable", 1)
        with self.assertRaisesRegex(ValidationError, "required physical outcome"):
            BoardBenchReport.from_dict(omitted)

    def test_relative_inventory_rejects_escape_absolute_and_duplicates(self) -> None:
        for value in (
            "../secret",
            "/absolute/file",
            "project\\file",
            "C:/absolute/file",
        ):
            with (
                self.subTest(path=value),
                self.assertRaisesRegex(ValidationError, "relative POSIX"),
            ):
                InventoryEntry(value, 1, HASH_A)

        run = _run_document()
        run["inventory"].append(copy.deepcopy(run["inventory"][0]))  # type: ignore[union-attr,index]
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            BoardBenchRun.from_dict(run)

    def test_non_finite_numbers_are_rejected_in_memory_and_on_load(self) -> None:
        campaign = _campaign_document()
        campaign["wall_timeout_seconds"] = float("nan")
        with self.assertRaisesRegex(ValidationError, "finite"):
            BoardBenchCampaign.from_dict(campaign)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nan.json"
            path.write_text(
                '{"schema":"pcbdraft-boardbench-run","version":1,"value":NaN}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "non-finite"):
                load_artifact(path)

    def test_json_duplicate_keys_and_invalid_unicode_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            duplicate = Path(temporary) / "duplicate.json"
            duplicate.write_text(
                '{"schema":"pcbdraft-boardbench-run","schema":"other"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "duplicate JSON object key"):
                load_artifact(duplicate)

        corpus = _corpus_document()
        corpus["methodology"] = "\ud800"
        with self.assertRaisesRegex(ValidationError, "methodology is invalid"):
            BoardBenchCorpus.from_dict(corpus)

    def test_reference_contracts_reject_semantically_invalid_combinations(self) -> None:
        bad_unit = _corpus_document()
        bad_unit["cases"][0]["rating_bounds"][0]["unit"] = "A"  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "unit does not match"):
            BoardBenchCorpus.from_dict(bad_unit)

        forbidden_support = _corpus_document()
        requirement = forbidden_support["cases"][0]["support_requirements"][0]  # type: ignore[index]
        requirement["kind"] = "forbidden_part"  # type: ignore[index]
        requirement["subjects"] = ["unsafe-part"]  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "support requirements"):
            BoardBenchCorpus.from_dict(forbidden_support)

        required_forbidden = _corpus_document()
        required_forbidden["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            copy.deepcopy(
                required_forbidden["cases"][0]["support_requirements"][0]  # type: ignore[index]
            )
        ]
        with self.assertRaisesRegex(ValidationError, "forbidden conditions"):
            BoardBenchCorpus.from_dict(required_forbidden)

        zero_manufacturing_limit = _corpus_document()
        zero_manufacturing_limit["cases"][0]["manufacturing_constraints"][0][  # type: ignore[index]
            "value_mm"
        ] = 0.0
        with self.assertRaisesRegex(ValidationError, "must be positive"):
            BoardBenchCorpus.from_dict(zero_manufacturing_limit)

    def test_reference_requirement_subject_grammar_round_trips(self) -> None:
        document = _corpus_document()
        case = cast(dict[str, Any], cast(list[object], document["cases"])[0])
        case["support_requirements"] = [
            {
                "id": kind,
                "kind": kind,
                "subjects": ["controller.VDD", "decoupler", "supply.VOUT"],
                "description": f"Typed {kind} support contract.",
            }
            for kind in ("decoupling", "pull_up", "protection", "reset", "boot")
        ] + [
            {
                "id": "power-source",
                "kind": "power_source",
                "subjects": ["supply.VOUT", "controller.VDD"],
                "description": "Typed power-source endpoint pair.",
            },
            {
                "id": "power-return",
                "kind": "power_return",
                "subjects": ["controller.GND", "supply.GND"],
                "description": "Typed power-return endpoint pair.",
            },
            {
                "id": "debug",
                "kind": "debug",
                "subjects": [
                    "controller.SWDIO",
                    "supply.DIO",
                    "controller.SWCLK",
                    "supply.CLK",
                ],
                "description": "Two consecutive debug endpoint pairs.",
            },
            {
                "id": "interface",
                "kind": "interface",
                "subjects": ["controller.TX", "supply.RX"],
                "description": "One interface endpoint pair.",
            },
        ]
        case["forbidden_conditions"] = [
            {
                "id": "forbidden-part",
                "kind": "forbidden_part",
                "subjects": ["unsafe.part-id"],
                "description": "Canonical part identity is forbidden.",
            },
            {
                "id": "forbidden-connection",
                "kind": "forbidden_connection",
                "subjects": [
                    "controller.VDD",
                    "controller.GND",
                    "supply.VOUT",
                    "supply.GND",
                ],
                "description": "Two forbidden endpoint pairs.",
            },
            {
                "id": "forbidden-rating",
                "kind": "forbidden_rating",
                "subjects": ["supply-voltage"],
                "description": "A declared rating bound must not be violated.",
            },
        ]

        corpus = BoardBenchCorpus.from_dict(document)

        self.assertEqual(document, corpus.to_dict())

    def test_reference_contracts_reject_malformed_and_dangling_endpoints(self) -> None:
        for endpoint in (
            "controller/VDD",
            "controller.VDD.extra",
            ".VDD",
            "controller.",
            f"{'s' * 129}.VDD",
            f"controller.{'p' * 129}",
        ):
            with self.subTest(endpoint=endpoint):
                malformed = _corpus_document()
                malformed["cases"][0]["net_rules"][0]["endpoints"][0] = endpoint  # type: ignore[index]
                with self.assertRaisesRegex(ValidationError, "slot.pin endpoint"):
                    BoardBenchCorpus.from_dict(malformed)

        dangling_net = _corpus_document()
        dangling_net["cases"][0]["net_rules"][0]["endpoints"][0] = (  # type: ignore[index]
            "missing.VDD"
        )
        with self.assertRaisesRegex(ValidationError, "unknown slot missing"):
            BoardBenchCorpus.from_dict(dangling_net)

        dangling_support = _corpus_document()
        dangling_support["cases"][0]["support_requirements"][0]["subjects"][1] = (  # type: ignore[index]
            "missing"
        )
        with self.assertRaisesRegex(ValidationError, "unknown support slot missing"):
            BoardBenchCorpus.from_dict(dangling_support)

        dotted_slot = _corpus_document()
        dotted_slot["cases"][0]["component_slots"][0]["id"] = "controller.main"  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "path-safe slot identity"):
            BoardBenchCorpus.from_dict(dotted_slot)

        noncanonical_alternative = _corpus_document()
        noncanonical_alternative["cases"][0]["component_slots"][0][  # type: ignore[index]
            "alternatives"
        ][0]["part_id"] = "Vendor.Part"  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "canonical part identity"):
            BoardBenchCorpus.from_dict(noncanonical_alternative)

    def test_requirement_kinds_enforce_typed_subject_cardinality(self) -> None:
        support = _corpus_document()
        support["cases"][0]["support_requirements"][0]["subjects"] = [  # type: ignore[index]
            "controller.VDD",
            "decoupler",
        ]
        with self.assertRaisesRegex(ValidationError, "target endpoint, support slot"):
            BoardBenchCorpus.from_dict(support)

        power_pair = _corpus_document()
        requirement = power_pair["cases"][0]["support_requirements"][0]  # type: ignore[index]
        requirement.update(  # type: ignore[union-attr]
            {
                "kind": "power_source",
                "subjects": [
                    "supply.VOUT",
                    "controller.VDD",
                    "controller.GND",
                ],
            }
        )
        with self.assertRaisesRegex(ValidationError, "one endpoint pair"):
            BoardBenchCorpus.from_dict(power_pair)

        repeated_pairs = _corpus_document()
        requirement = repeated_pairs["cases"][0]["support_requirements"][0]  # type: ignore[index]
        requirement.update(  # type: ignore[union-attr]
            {
                "kind": "debug",
                "subjects": [
                    "controller.SWDIO",
                    "supply.DIO",
                    "controller.SWCLK",
                ],
            }
        )
        with self.assertRaisesRegex(ValidationError, "one or more endpoint pairs"):
            BoardBenchCorpus.from_dict(repeated_pairs)

        forbidden_connection = _corpus_document()
        forbidden_connection["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "forbidden-connection",
                "kind": "forbidden_connection",
                "subjects": ["controller.VDD", "controller.GND", "supply.VOUT"],
                "description": "Odd endpoint lists are ambiguous.",
            }
        ]
        with self.assertRaisesRegex(ValidationError, "one or more endpoint pairs"):
            BoardBenchCorpus.from_dict(forbidden_connection)

        endpoint_rule = _corpus_document()
        rule = endpoint_rule["cases"][0]["net_rules"][0]  # type: ignore[index]
        rule["kind"] = "required_endpoint"  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "exactly one endpoint"):
            BoardBenchCorpus.from_dict(endpoint_rule)

    def test_forbidden_subjects_are_safe_and_cross_referenced(self) -> None:
        unsafe_part = _corpus_document()
        unsafe_part["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "forbidden-part",
                "kind": "forbidden_part",
                "subjects": ["../unsafe-part"],
                "description": "Unsafe canonical identity.",
            }
        ]
        with self.assertRaisesRegex(ValidationError, "canonical part identity"):
            BoardBenchCorpus.from_dict(unsafe_part)

        noncanonical_part = _corpus_document()
        noncanonical_part["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "forbidden-part",
                "kind": "forbidden_part",
                "subjects": ["Vendor.Part"],
                "description": "Part ids use the canonical stable-id grammar.",
            }
        ]
        with self.assertRaisesRegex(ValidationError, "canonical part identity"):
            BoardBenchCorpus.from_dict(noncanonical_part)

        dangling_rating = _corpus_document()
        dangling_rating["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "forbidden-rating",
                "kind": "forbidden_rating",
                "subjects": ["missing-rating"],
                "description": "Every rating reference must resolve.",
            }
        ]
        with self.assertRaisesRegex(ValidationError, "unknown rating bound"):
            BoardBenchCorpus.from_dict(dangling_rating)

        dangling_connection = _corpus_document()
        dangling_connection["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "forbidden-connection",
                "kind": "forbidden_connection",
                "subjects": ["controller.VDD", "missing.GND"],
                "description": "Every endpoint slot must resolve.",
            }
        ]
        with self.assertRaisesRegex(ValidationError, "unknown slot missing"):
            BoardBenchCorpus.from_dict(dangling_connection)

    def test_exact_bom_cardinality_subjects_are_typed_component_slots(self) -> None:
        exact_bom = _corpus_document()
        exact_bom["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "no-extra-bom-components",
                "kind": "forbidden_unmatched_bom_component",
                "subjects": ["controller", "decoupler", "supply"],
                "description": "Every populated BOM component needs one allowed slot.",
            }
        ]
        corpus = BoardBenchCorpus.from_dict(exact_bom)
        self.assertEqual(corpus.to_dict(), exact_bom)

        endpoint_subject = _corpus_document()
        endpoint_subject["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "no-extra-bom-components",
                "kind": "forbidden_unmatched_bom_component",
                "subjects": ["controller.VDD"],
                "description": "Subjects are slot identities, not endpoints.",
            }
        ]
        with self.assertRaisesRegex(ValidationError, "path-safe slot identity"):
            BoardBenchCorpus.from_dict(endpoint_subject)

        dangling_slot = _corpus_document()
        dangling_slot["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "no-extra-bom-components",
                "kind": "forbidden_unmatched_bom_component",
                "subjects": ["missing"],
                "description": "Every subject must resolve to a declared slot.",
            }
        ]
        with self.assertRaisesRegex(
            ValidationError, "unknown allowed BOM component slot missing"
        ):
            BoardBenchCorpus.from_dict(dangling_slot)

        disabled_metric = _corpus_document()
        disabled_metric["cases"][0]["applicable_metrics"].remove(  # type: ignore[index,union-attr]
            "support_circuits"
        )
        disabled_metric["cases"][0]["forbidden_conditions"] = [  # type: ignore[index]
            {
                "id": "no-extra-bom-components",
                "kind": "forbidden_unmatched_bom_component",
                "subjects": ["controller", "decoupler", "supply"],
                "description": "The predicate must affect the automatic score.",
            }
        ]
        with self.assertRaisesRegex(
            ValidationError, "requires the support_circuits metric"
        ):
            BoardBenchCorpus.from_dict(disabled_metric)

    def test_rating_bounds_distinguish_operating_and_exact_part_facts(self) -> None:
        part_rating = _corpus_document()
        bound = part_rating["cases"][0]["rating_bounds"][0]  # type: ignore[index]
        bound.update(  # type: ignore[union-attr]
            {
                "source": "part_rating",
                "subject": "controller",
                "fact_key": "absolute_max_voltage_v",
            }
        )
        corpus = BoardBenchCorpus.from_dict(part_rating)
        self.assertEqual("part_rating", corpus.cases[0].rating_bounds[0].source)
        self.assertEqual(
            "absolute_max_voltage_v", corpus.cases[0].rating_bounds[0].fact_key
        )

        operating_fact = _corpus_document()
        operating_fact["cases"][0]["rating_bounds"][0]["fact_key"] = (  # type: ignore[index]
            "absolute_max_voltage_v"
        )
        with self.assertRaisesRegex(ValidationError, "null fact_key"):
            BoardBenchCorpus.from_dict(operating_fact)

        missing_fact = copy.deepcopy(part_rating)
        missing_fact["cases"][0]["rating_bounds"][0]["fact_key"] = None  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "needs a ratings fact_key"):
            BoardBenchCorpus.from_dict(missing_fact)

        unsafe_fact = copy.deepcopy(part_rating)
        unsafe_fact["cases"][0]["rating_bounds"][0]["fact_key"] = (  # type: ignore[index]
            "ratings.absolute_max_voltage_v"
        )
        with self.assertRaisesRegex(ValidationError, "exact safe ratings fact key"):
            BoardBenchCorpus.from_dict(unsafe_fact)

        dangling_part = copy.deepcopy(part_rating)
        dangling_part["cases"][0]["rating_bounds"][0]["subject"] = "missing"  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "unknown slot missing"):
            BoardBenchCorpus.from_dict(dangling_part)

        dangling_operating = _corpus_document()
        dangling_operating["cases"][0]["rating_bounds"][0]["subject"] = (  # type: ignore[index]
            "missing.VDD"
        )
        with self.assertRaisesRegex(ValidationError, "unknown slot missing"):
            BoardBenchCorpus.from_dict(dangling_operating)

        invalid_source = _corpus_document()
        invalid_source["cases"][0]["rating_bounds"][0]["source"] = "description"  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "source is invalid"):
            BoardBenchCorpus.from_dict(invalid_source)

        legacy_untyped = _corpus_document()
        legacy_bound = legacy_untyped["cases"][0]["rating_bounds"][0]  # type: ignore[index]
        legacy_bound.pop("source")  # type: ignore[union-attr]
        legacy_bound.pop("fact_key")  # type: ignore[union-attr]
        with self.assertRaisesRegex(ValidationError, "unexpected fields"):
            BoardBenchCorpus.from_dict(legacy_untyped)

    def test_oversized_text_and_nested_arrays_are_rejected(self) -> None:
        prompt = _corpus_document()
        prompt["cases"][0]["prompt"] = "x" * (MAX_TEXT_BYTES + 1)  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "prompt is invalid"):
            BoardBenchCorpus.from_dict(prompt)

        slots = _corpus_document()
        first_slot = slots["cases"][0]["component_slots"][0]  # type: ignore[index]
        slots["cases"][0]["component_slots"] = [  # type: ignore[index]
            copy.deepcopy(first_slot) for _ in range(129)
        ]
        with self.assertRaisesRegex(ValidationError, "128"):
            BoardBenchCorpus.from_dict(slots)

    def test_all_typed_loaders_dispatch_and_reject_wrong_schema(self) -> None:
        fixtures = (
            (load_corpus, _corpus_document()),
            (load_campaign, _campaign_document()),
            (load_run, _run_document()),
            (load_score, _score_document()),
            (load_review, _review_document()),
            (load_correction, _correction_document()),
            (load_hardware, _hardware_document()),
            (load_selection, _selection_document()),
            (load_report, _report_document()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (loader, document) in enumerate(fixtures):
                path = root / f"artifact-{index}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                loaded = loader(path)
                self.assertEqual(document["schema"], loaded.schema)
            wrong = root / "wrong.json"
            wrong.write_text(json.dumps(_run_document()), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "expected"):
                load_score(wrong)

    def test_bounded_loader_rejects_oversized_and_symlink_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (ARTIFACT_FILE_LIMIT + 1))
            with self.assertRaisesRegex(ValidationError, "exceeds"):
                load_artifact(oversized)

            target = root / "run.json"
            target.write_text(json.dumps(_run_document()), encoding="utf-8")
            link = root / "linked.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                load_run(link)

    def test_artifact_size_limit_comes_from_schema_not_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            padding = " " * (ARTIFACT_FILE_LIMIT + 1)

            corpus = root / "private-holdout.json"
            corpus.write_text(
                padding + json.dumps(_corpus_document()), encoding="utf-8"
            )
            self.assertLess(corpus.stat().st_size, CORPUS_FILE_LIMIT)
            self.assertEqual("boardbench-v1", load_corpus(corpus).corpus_id)

            disguised_run = root / "run-corpus.json"
            disguised_run.write_text(
                padding + json.dumps(_run_document()), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "exceeds"):
                load_run(disguised_run)

    def test_private_atomic_storage_and_terminal_run_immutability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "campaigns"
            campaign_dir = allocate_campaign_directory(root, "campaign-v1")
            self.assertEqual(0o700, stat.S_IMODE(campaign_dir.stat().st_mode))
            campaign_path = campaign_dir / "campaign.json"
            write_artifact(
                campaign_path, BoardBenchCampaign.from_dict(_campaign_document())
            )
            self.assertEqual(0o600, stat.S_IMODE(campaign_path.stat().st_mode))
            with self.assertRaisesRegex(ValidationError, "already exists"):
                write_artifact(
                    campaign_path, BoardBenchCampaign.from_dict(_campaign_document())
                )

            run_path = campaign_dir / "run.json"
            store_run(run_path, BoardBenchRun.from_dict(_run_document("planned")))
            store_run(run_path, BoardBenchRun.from_dict(_run_document("running")))
            terminal = BoardBenchRun.from_dict(_run_document("completed"))
            store_run(run_path, terminal)
            original = run_path.read_bytes()
            with self.assertRaisesRegex(ValidationError, "immutable"):
                store_run(run_path, terminal)
            self.assertEqual(original, run_path.read_bytes())

            skipped_running = campaign_dir / "skipped-running.json"
            store_run(
                skipped_running, BoardBenchRun.from_dict(_run_document("planned"))
            )
            with self.assertRaisesRegex(ValidationError, "status transition"):
                store_run(skipped_running, terminal)

    def test_concurrent_write_once_and_terminal_transitions_have_one_winner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "campaign.json"
            artifact = BoardBenchCampaign.from_dict(_campaign_document())
            barrier = threading.Barrier(2)

            def write_once() -> str:
                barrier.wait()
                try:
                    write_artifact(artifact_path, artifact)
                except ValidationError as exc:
                    return str(exc)
                return "written"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: write_once(), range(2)))
            self.assertEqual(1, results.count("written"))
            self.assertEqual(1, sum("already exists" in result for result in results))
            self.assertEqual(artifact.to_dict(), load_campaign(artifact_path).to_dict())

            run_path = root / "run.json"
            store_run(run_path, BoardBenchRun.from_dict(_run_document("planned")))
            store_run(run_path, BoardBenchRun.from_dict(_run_document("running")))
            completed = BoardBenchRun.from_dict(_run_document("completed"))
            failed_document = _run_document("failed")
            failed = BoardBenchRun.from_dict(failed_document)
            barrier = threading.Barrier(2)

            def finish(run: BoardBenchRun) -> str:
                barrier.wait()
                try:
                    store_run(run_path, run)
                except ValidationError as exc:
                    return str(exc)
                return run.status

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(finish, (completed, failed)))
            self.assertEqual(
                1, sum(result in {"completed", "failed"} for result in results)
            )
            self.assertEqual(1, sum("immutable" in result for result in results))
            self.assertTrue(load_run(run_path).terminal)

    def test_storage_rechecks_parent_after_lock_and_rejects_path_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = root / "campaign"
            backup = root / "campaign-backup"
            outside = root / "outside"
            campaign.mkdir()
            outside.mkdir()
            target = campaign / "campaign.json"
            artifact = BoardBenchCampaign.from_dict(_campaign_document())

            class SwappingLock:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def __enter__(self) -> Self:
                    campaign.rename(backup)
                    campaign.symlink_to(outside, target_is_directory=True)
                    return self

                def __exit__(self, *_args: object) -> None:
                    campaign.unlink()
                    backup.rename(campaign)

            with (
                mock.patch(
                    "pcbdraft.verification.boardbench.ResourceLock", SwappingLock
                ),
                self.assertRaisesRegex(ValidationError, "symlink|parent changed"),
            ):
                write_artifact(target, artifact)
            self.assertFalse((outside / "campaign.json").exists())

    @unittest.skipUnless(hasattr(os, "link"), "hard-link support is required")
    def test_loader_rejects_hard_link_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "campaign.json"
            alias = root / "alias.json"
            write_artifact(original, BoardBenchCampaign.from_dict(_campaign_document()))
            os.link(original, alias)
            with self.assertRaisesRegex(ValidationError, "single-link regular file"):
                load_campaign(alias)

    def test_running_review_and_failure_states_reject_contradictory_evidence(
        self,
    ) -> None:
        running = _run_document("running")
        running["final_response"] = "Done."
        with self.assertRaisesRegex(ValidationError, "running run"):
            BoardBenchRun.from_dict(running)

        terminal_without_artifacts = _run_document("failed")
        terminal_without_artifacts["inventory"] = []
        with self.assertRaisesRegex(ValidationError, "retained artifact inventory"):
            BoardBenchRun.from_dict(terminal_without_artifacts)

        review = _review_document()
        review.update(
            {
                "outcome": "not_applicable",
                "functional_correctness": "not_applicable",
                "orderable_state": "unknown",
                "not_applicable_reason": "No inspectable schematic.",
            }
        )
        with self.assertRaisesRegex(ValidationError, "orderable"):
            BoardBenchReview.from_dict(review)

        not_applicable = _review_document()
        not_applicable.update(
            {
                "outcome": "not_applicable",
                "functional_correctness": "not_applicable",
                "orderable_state": "not_applicable",
                "orderability_evidence": [],
                "not_applicable_reason": "No inspectable schematic.",
            }
        )
        for item in not_applicable["checklist"]:  # type: ignore[union-attr]
            item.update(  # type: ignore[union-attr]
                disposition="not_applicable",
                evidence_note="No retained schematic exists for this obligation.",
            )
        with self.assertRaisesRegex(ValidationError, "failure classification"):
            BoardBenchReview.from_dict(not_applicable)

        changed = _review_document()
        changed.update(
            {
                "outcome": "pass_after_changes",
                "modifications": [
                    {
                        "id": "change-1",
                        "change_type": "functional",
                        "description": "Add required decoupling.",
                        "finding_ids": [],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValidationError, "generated result"):
            BoardBenchReview.from_dict(changed)

        failed = _review_document()
        failed.update(
            {
                "outcome": "fail",
                "functional_correctness": "fail",
                "final_failure": {
                    "stage": "circuit_design",
                    "causes": ["unclassified", "model_reasoning"],
                    "owners": ["model"],
                    "reason": "Contradictory cause state.",
                },
            }
        )
        with self.assertRaisesRegex(ValidationError, "must stand alone"):
            BoardBenchReview.from_dict(failed)

    def test_hardware_attachments_are_nonempty_and_total_bounded(self) -> None:
        empty = _hardware_document()
        empty["attachments"][0]["size_bytes"] = 0  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "must not be empty"):
            BoardBenchHardware.from_dict(empty)

        oversized = _hardware_document()
        oversized["attachments"] = [
            {
                "path": f"lab/evidence-{index}.bin",
                "size_bytes": MAX_INVENTORY_FILE_BYTES,
                "sha256": HASH_A,
            }
            for index in range(9)
        ]
        with self.assertRaisesRegex(ValidationError, "total size"):
            BoardBenchHardware.from_dict(oversized)

    def test_inventory_is_sorted_hashed_bounded_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            (root / "z.txt").write_bytes(b"z")
            (root / "nested" / "a.txt").write_bytes(b"alpha")
            inventory = build_inventory(root)
            self.assertEqual(
                ["nested/a.txt", "z.txt"], [item.path for item in inventory]
            )
            self.assertEqual(hashlib.sha256(b"alpha").hexdigest(), inventory[0].sha256)

            (root / "linked").symlink_to(root / "z.txt")
            with self.assertRaisesRegex(ValidationError, "symlink"):
                build_inventory(root)

        with tempfile.TemporaryDirectory() as temporary:
            directories = [
                f"directory-{index}" for index in range(MAX_INVENTORY_DIRECTORIES + 1)
            ]
            with (
                mock.patch(
                    "pcbdraft.verification.boardbench.os.walk",
                    return_value=[(temporary, directories, [])],
                ),
                self.assertRaisesRegex(ValidationError, "too many directories"),
            ):
                build_inventory(temporary)

    def test_hardware_selection_and_sealed_report_enforce_physical_coverage(
        self,
    ) -> None:
        selection = _selection_document()
        selection["selections"][0]["category"] = BOARD_CATEGORIES[1]  # type: ignore[index]
        with self.assertRaisesRegex(ValidationError, "every BoardBench category"):
            BoardBenchSelection.from_dict(selection)

        report = _report_document()
        slices = report["slices"]  # type: ignore[assignment]
        slices[0]["physical"] = _report_slice(  # type: ignore[index]
            "overall", "overall", None, 60, 4, sealed=True
        )["physical"]
        slices[1]["physical"] = _report_slice(  # type: ignore[index]
            "category",
            BOARD_CATEGORIES[0],
            BOARD_CATEGORIES[0],
            12,
            0,
            sealed=True,
        )["physical"]
        slices[6]["physical"] = _report_slice(  # type: ignore[index]
            "case", "case-00", BOARD_CATEGORIES[0], 3, 0, sealed=True
        )["physical"]
        with self.assertRaisesRegex(ValidationError, "five-category physical"):
            BoardBenchReport.from_dict(report)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support is required")
    def test_campaign_allocator_rejects_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                allocate_campaign_directory(linked, "campaign-v1")


if __name__ == "__main__":
    unittest.main()
