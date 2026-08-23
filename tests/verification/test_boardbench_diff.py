from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.domain.ir import Design
from pcbdraft.verification.boardbench import (
    BoardBenchReview,
    BoardBenchRun,
    artifact_sha256,
    build_inventory,
    load_correction,
    write_artifact,
)
from pcbdraft.verification.boardbench_diff import (
    CorrectionCapture,
    capture_correction,
    normalize_snapshot,
    verify_correction_bundle,
)
from tests.support.design_factory import minimal_design_dict
from tests.verification.test_boardbench_report import _campaign, _corpus

HASH_A = "a" * 64
NOW = "2026-08-21T10:00:00Z"
LATER = "2026-08-21T10:01:00Z"


def _schematic_snapshot(value: str) -> dict[str, object]:
    return {
        "schema": "pcbdraft-schematic-snapshot",
        "version": 1,
        "root_uuid": "root",
        "components": [
            {
                "reference": "R1",
                "value": value,
                "symbol": "Device:R",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "uuid": "component-r1",
                "position_mm": [10.0, 10.0],
                "rotation_deg": 0.0,
                "properties": {
                    "Part_ID": "yageo.rc0603fr-074k7l",
                    "Trust": "human_verified",
                },
            }
        ],
        "label_names": ["3V3", "OUT"],
        "label_count": 2,
        "no_connect_count": 0,
    }


def _board_snapshot(value: str, x_mm: float) -> dict[str, object]:
    return {
        "schema": "pcbdraft-pcbnew-result",
        "version": 1,
        "mode": "inspect_board",
        "kicad_version": "9.0.4",
        "components": [
            {
                "reference": "R1",
                "value": value,
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "schematic_path": "/component-r1",
                "x_mm": x_mm,
                "y_mm": 10.0,
                "rotation_deg": 0.0,
                "side": "front",
                "properties": {},
                "pads": [
                    {"number": "1", "net": "3V3"},
                    {"number": "2", "net": "OUT"},
                ],
            }
        ],
        "tracks": [],
        "zones": [],
        "board": {
            "layers": 2,
            "thickness_mm": 1.6,
            "min_clearance_mm": 0.2,
            "min_track_mm": 0.2,
            "min_drill_mm": 0.3,
            "edge_clearance_mm": 0.5,
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )


def _snapshot(root: Path, *, value: str = "4.7k", x_mm: float = 10.0) -> Path:
    root.mkdir(parents=True)
    design = minimal_design_dict()
    cast(dict[str, Any], cast(list[object], design["components"])[0])["value"] = value
    _write_json(root / "design.pcbir.json", design)
    _write_json(root / "fixture.kicad_sch", _schematic_snapshot(value))
    _write_json(root / "fixture.kicad_pcb", _board_snapshot(value, x_mm))
    _write_json(root / "fixture.kicad_pro", {})
    return root


def _native_inspector(
    _design: Design, schematic: Path, board: Path
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        cast(dict[str, object], json.loads(schematic.read_text(encoding="utf-8"))),
        cast(dict[str, object], json.loads(board.read_text(encoding="utf-8"))),
    )


def _review_document(run: BoardBenchRun, *, source_hash: str) -> dict[str, object]:
    return {
        "schema": "pcbdraft-boardbench-review",
        "version": 3,
        "campaign_id": run.campaign_id,
        "run_id": run.run_id,
        "source_campaign_sha256": source_hash,
        "source_corpus_sha256": source_hash,
        "source_case_sha256": source_hash,
        "source_run_sha256": source_hash,
        "source_score_sha256": None,
        "reviewer": "engineer@example.invalid",
        "reviewed_at": LATER,
        "outcome": "pass_after_changes",
        "functional_correctness": "pass",
        "orderable_state": "pass",
        "orderability_evidence": [
            {
                "slot_ids": ["resistor"],
                "manufacturer_part_number": "FIXTURE-R-4K7",
                "status": "orderable",
                "as_of": LATER,
                "source_kind": "manufacturer",
                "source_name": "Fixture manufacturer",
                "source_url": "https://example.com/parts/fixture-r-4k7",
                "note": "Dated fixture sourcing evidence.",
            }
        ],
        "active_engineer_minutes": 12.0,
        "not_applicable_reason": None,
        "checklist": [
            {
                "id": "review-rubric-001",
                "kind": "review_rubric",
                "requirement": "Check the corrected circuit function.",
                "disposition": "pass",
                "evidence_note": "Compared the generated and corrected schematics.",
            },
            {
                "id": "assembly-constraint-001",
                "kind": "assembly_constraint",
                "requirement": "Check assembly suitability.",
                "disposition": "pass",
                "evidence_note": "Inspected the corrected component placement.",
            },
        ],
        "findings": [
            {
                "id": "finding-1",
                "category": "model_reasoning",
                "description": "The generated resistor value was incorrect.",
            }
        ],
        "modifications": [
            {
                "id": "decision-1",
                "change_type": "functional",
                "description": "Correct the resistor value and placement.",
                "finding_ids": ["finding-1"],
            }
        ],
        "final_failure": {
            "stage": "circuit_design",
            "causes": ["model_reasoning"],
            "owners": ["model"],
            "reason": "The generated circuit needed one functional correction.",
        },
    }


class CorrectionFixture:
    def __init__(self, root: Path):
        self.campaign = root / "campaign"
        write_artifact(self.campaign / "campaign.json", _campaign(_corpus()))
        self.run_root = self.campaign / "runs" / "case-00-run-1"
        self.artifacts = self.run_root / "artifacts"
        self.generated = _snapshot(self.artifacts / "project")
        self.run = BoardBenchRun(
            campaign_id="campaign-v1",
            run_id="case-00-run-1",
            case_id="case-00",
            repetition=1,
            prompt_sha256=HASH_A,
            status="completed",
            started_at=NOW,
            completed_at=LATER,
            termination_reason="agent_returned",
            final_response="The board is complete.",
            inventory=build_inventory(self.artifacts),
        )
        self.run_path = self.run_root / "run.json"
        write_artifact(self.run_path, self.run)
        self.review_path = self.campaign / "reviews" / self.run.run_id / "review.json"
        self.review = BoardBenchReview.from_dict(
            _review_document(self.run, source_hash=artifact_sha256(self.run))
        )
        write_artifact(self.review_path, self.review)
        self.corrected = _snapshot(root / "engineer-corrected", value="10k", x_mm=12.0)

    def capture(self, **kwargs: object) -> CorrectionCapture:
        arguments: dict[str, object] = {
            "run_receipt_path": self.run_path,
            "review_path": self.review_path,
            "generated_snapshot": self.generated,
            "corrected_snapshot": self.corrected,
            "manufacturing_candidate_snapshot": self.corrected,
            "created_at": LATER,
            "native_inspector": _native_inspector,
        }
        arguments.update(kwargs)
        return capture_correction(self.campaign, **arguments)  # type: ignore[arg-type]


class BoardBenchDiffTests(unittest.TestCase):
    def test_capture_copies_snapshots_and_publishes_fixed_discovery_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CorrectionFixture(Path(temporary))
            raw_inventory = build_inventory(fixture.artifacts)
            raw_ir = (fixture.generated / "design.pcbir.json").read_bytes()

            capture = fixture.capture()

            expected_root = (
                fixture.campaign / "corrections" / fixture.run.run_id
            ).resolve()
            self.assertEqual(expected_root, capture.root)
            self.assertEqual(expected_root / "correction.json", capture.record_path)
            self.assertTrue(
                (capture.root / "artifacts/generated/design.pcbir.json").is_file()
            )
            self.assertTrue(
                (capture.root / "artifacts/corrected/design.pcbir.json").is_file()
            )
            self.assertTrue(
                (
                    capture.root / "artifacts/manufacturing-candidate/design.pcbir.json"
                ).is_file()
            )
            self.assertEqual(("decision-1",), capture.correction.decision_ids)
            self.assertEqual(
                artifact_sha256(fixture.run), capture.correction.source_run_sha256
            )
            self.assertEqual(
                artifact_sha256(fixture.review),
                capture.correction.source_review_sha256,
            )
            self.assertIsNotNone(capture.correction.manufacturing_candidate_sha256)
            self.assertIn(
                "components", {item.area for item in capture.correction.changes}
            )
            self.assertIn(
                "placement", {item.area for item in capture.correction.changes}
            )
            self.assertIn("files", {item.area for item in capture.correction.changes})
            self.assertEqual(raw_inventory, build_inventory(fixture.artifacts))
            self.assertEqual(
                raw_ir, (fixture.generated / "design.pcbir.json").read_bytes()
            )
            self.assertEqual(
                capture.correction,
                verify_correction_bundle(
                    capture.root,
                    run_receipt_path=fixture.run_path,
                    review_path=fixture.review_path,
                ),
            )

    def test_normalization_is_semantic_native_and_path_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = _snapshot(Path(temporary) / "snapshot")

            normalized = normalize_snapshot(
                snapshot, native_inspector=_native_inspector
            ).to_dict()

            self.assertEqual("semantic_ir_only", normalized["semantic_state"])
            areas = cast(dict[str, dict[str, object]], normalized["areas"])
            self.assertIn("load_r", areas["components"])
            self.assertIn("net_3v3", areas["nets"])
            self.assertIn("native-board:R1", areas["placement"])
            self.assertIn("native-board-settings", areas["board_geometry"])
            self.assertIn("design.pcbir.json", areas["files"])
            self.assertNotIn(str(snapshot), json.dumps(normalized, sort_keys=True))

    def test_capture_rejects_empty_diff_and_leaves_no_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CorrectionFixture(Path(temporary))
            identical = Path(temporary) / "identical"
            shutil.copytree(fixture.generated, identical)

            with self.assertRaisesRegex(ValidationError, "empty diff"):
                fixture.capture(
                    corrected_snapshot=identical,
                    manufacturing_candidate_snapshot=None,
                )

            self.assertFalse(
                (fixture.campaign / "corrections" / fixture.run.run_id).exists()
            )

    def test_capture_rejects_symlinks_and_malformed_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CorrectionFixture(Path(temporary))
            linked = Path(temporary) / "linked-correction"
            shutil.copytree(fixture.corrected, linked)
            (linked / "external-link").symlink_to(
                fixture.corrected / "fixture.kicad_pcb"
            )
            with self.assertRaisesRegex(ValidationError, "symlink"):
                fixture.capture(corrected_snapshot=linked)

            malformed = Path(temporary) / "malformed-correction"
            shutil.copytree(fixture.corrected, malformed)
            (malformed / "fixture.kicad_pro").unlink()
            with self.assertRaisesRegex(ValidationError, "matching .kicad_pro"):
                fixture.capture(corrected_snapshot=malformed)

    def test_capture_rejects_changed_or_unbound_source_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = CorrectionFixture(root)
            wrong_review = BoardBenchReview.from_dict(
                _review_document(fixture.run, source_hash="b" * 64)
            )
            wrong_review_path = fixture.campaign / "reviews" / "wrong" / "review.json"
            write_artifact(wrong_review_path, wrong_review)
            with self.assertRaisesRegex(ValidationError, "does not match"):
                fixture.capture(review_path=wrong_review_path)

            (fixture.generated / "fixture.kicad_pro").write_text(
                '{"tampered":true}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "changed after"):
                fixture.capture()

    def test_verifier_detects_bundle_tampering_and_capture_is_write_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CorrectionFixture(Path(temporary))
            capture = fixture.capture()
            with self.assertRaisesRegex(ValidationError, "already exists"):
                fixture.capture()

            corrected_ir = capture.root / "artifacts/corrected/design.pcbir.json"
            value = json.loads(corrected_ir.read_text(encoding="utf-8"))
            value["name"] = "Tampered after capture"
            _write_json(corrected_ir, value)
            with self.assertRaisesRegex(ValidationError, "inventory has changed"):
                verify_correction_bundle(
                    capture.root,
                    run_receipt_path=fixture.run_path,
                    review_path=fixture.review_path,
                )

    def test_verifier_cross_checks_structural_diff_not_only_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CorrectionFixture(Path(temporary))
            capture = fixture.capture()
            structural_path = capture.root / "artifacts/structural-diff.json"
            structural = json.loads(structural_path.read_text(encoding="utf-8"))
            structural["changes"] = []
            atomic_write_json(structural_path, structural)
            correction = replace(
                load_correction(capture.record_path),
                inventory=build_inventory(capture.root / "artifacts"),
            )
            atomic_write_json(capture.record_path, correction.to_dict())
            with self.assertRaisesRegex(ValidationError, "structural diff"):
                verify_correction_bundle(
                    capture.root,
                    run_receipt_path=fixture.run_path,
                    review_path=fixture.review_path,
                )

    def test_malformed_native_inspector_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = _snapshot(Path(temporary) / "snapshot")

            def malformed(
                _design: Design, _schematic: Path, _board: Path
            ) -> tuple[dict[str, object], dict[str, object]]:
                return {}, {}

            with self.assertRaisesRegex(ValidationError, "malformed evidence"):
                normalize_snapshot(snapshot, native_inspector=malformed)


if __name__ == "__main__":
    unittest.main()
