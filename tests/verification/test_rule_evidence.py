from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pcbdraft.verification.rule_evidence import (
    capture_rule_evidence,
    compare_drc_evidence,
    load_rule_evidence,
)


class CompleteRuleEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pcbdraft-drc-evidence-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "board.kicad_pcb"
        self.source.write_text("(kicad_pcb (version 20250114))\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _capture(
        self,
        name: str,
        findings: list[dict[str, object]],
        *,
        revision: int,
        content_hash: str,
        extra: dict[str, object] | None = None,
        failure: str | None = None,
        damaged: bool = False,
    ):
        directory = self.root / name
        directory.mkdir()
        raw = directory / "drc.raw.json"
        if damaged:
            raw.write_text('{"violations": [', encoding="utf-8")
        else:
            document: dict[str, object] = {
                "$schema": "https://schemas.kicad.org/drc.v1.json",
                "kicad_version": "10.0.5",
                "included_severities": ["error", "warning"],
                "violations": findings,
                "unconnected_items": [],
                "schematic_parity": [],
            }
            if extra:
                document.update(extra)
            raw.write_text(json.dumps(document), encoding="utf-8")
        return capture_rule_evidence(
            kind="drc",
            raw_report=raw,
            output=directory / "drc.evidence.json",
            source_file=self.source,
            canonical_revision=revision + 10,
            design_revision=revision,
            design_content_hash=content_hash,
            failure=failure,
        )

    @staticmethod
    def _error(*, uuid: str, x: float, kind: str = "clearance") -> dict[str, object]:
        return {
            "severity": "error",
            "type": kind,
            "description": "clearance violation",
            "items": [{"uuid": uuid, "pos": {"x": x, "y": 2.0}}],
        }

    def test_identical_multisets_pass_and_retain_errors(self) -> None:
        finding = self._error(uuid="object-1", x=1.0)
        baseline = self._capture(
            "baseline", [finding, finding], revision=1, content_hash="a" * 64
        )
        candidate = self._capture(
            "candidate", [finding, finding], revision=2, content_hash="b" * 64
        )

        delta = compare_drc_evidence(baseline, candidate)

        self.assertTrue(delta.comparable)
        self.assertTrue(delta.passed)
        self.assertEqual(len(delta.retained_errors), 2)
        self.assertEqual(delta.new_errors, ())
        self.assertEqual(delta.fixed_errors, ())

    def test_one_fixed_error_is_recorded(self) -> None:
        retained = self._error(uuid="retained", x=1.0)
        fixed = self._error(uuid="fixed", x=2.0)
        baseline = self._capture(
            "baseline", [retained, fixed], revision=1, content_hash="a" * 64
        )
        candidate = self._capture(
            "candidate", [retained], revision=2, content_hash="b" * 64
        )

        delta = compare_drc_evidence(baseline, candidate)

        self.assertTrue(delta.passed)
        self.assertEqual(len(delta.retained_errors), 1)
        self.assertEqual(len(delta.fixed_errors), 1)

    def test_one_new_unwaived_error_blocks(self) -> None:
        baseline = self._capture("baseline", [], revision=1, content_hash="a" * 64)
        candidate = self._capture(
            "candidate",
            [self._error(uuid="new", x=1.0)],
            revision=2,
            content_hash="b" * 64,
        )

        delta = compare_drc_evidence(baseline, candidate)

        self.assertTrue(delta.comparable)
        self.assertFalse(delta.passed)
        self.assertEqual(len(delta.new_errors), 1)

    def test_changed_object_geometry_is_new_and_old_identity_is_fixed(self) -> None:
        baseline = self._capture(
            "baseline",
            [self._error(uuid="same-object", x=1.0)],
            revision=1,
            content_hash="a" * 64,
        )
        candidate = self._capture(
            "candidate",
            [self._error(uuid="same-object", x=1.25)],
            revision=2,
            content_hash="b" * 64,
        )

        delta = compare_drc_evidence(baseline, candidate)

        self.assertFalse(delta.passed)
        self.assertEqual(len(delta.new_errors), 1)
        self.assertEqual(len(delta.fixed_errors), 1)
        self.assertNotEqual(delta.new_errors, delta.fixed_errors)

    def test_timeout_or_tool_failure_fails_closed(self) -> None:
        baseline = self._capture("baseline", [], revision=1, content_hash="a" * 64)
        candidate = self._capture(
            "candidate",
            [],
            revision=2,
            content_hash="b" * 64,
            failure="timeout",
        )

        delta = compare_drc_evidence(baseline, candidate)

        self.assertFalse(delta.comparable)
        self.assertFalse(delta.passed)
        self.assertIn("candidate_evidence_unavailable", delta.failure_kinds)

    def test_damaged_report_is_unavailable_and_fails_closed(self) -> None:
        baseline = self._capture("baseline", [], revision=1, content_hash="a" * 64)
        candidate = self._capture(
            "candidate",
            [],
            revision=2,
            content_hash="b" * 64,
            damaged=True,
        )

        self.assertFalse(candidate.complete)
        self.assertEqual(candidate.failure, "damaged_or_truncated_raw_report")
        self.assertFalse(compare_drc_evidence(baseline, candidate).passed)

    def test_report_declaring_truncated_underlying_evidence_fails_closed(self) -> None:
        evidence = self._capture(
            "candidate",
            [],
            revision=2,
            content_hash="b" * 64,
            extra={"violations_truncated": True},
        )

        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.failure, "truncated_underlying_evidence")

    def test_baseline_revision_and_hash_mismatch_fail_closed(self) -> None:
        baseline = self._capture("baseline", [], revision=1, content_hash="a" * 64)
        candidate = self._capture("candidate", [], revision=2, content_hash="b" * 64)

        delta = compare_drc_evidence(
            baseline,
            candidate,
            expected_baseline_design_revision=99,
            expected_baseline_content_hash="c" * 64,
        )

        self.assertFalse(delta.comparable)
        self.assertIn("baseline_revision_mismatch", delta.failure_kinds)
        self.assertIn("baseline_content_hash_mismatch", delta.failure_kinds)

    def test_bounded_diagnostics_do_not_truncate_machine_evidence(self) -> None:
        evidence = self._capture(
            "candidate",
            [
                self._error(uuid=f"object-{index}", x=float(index))
                for index in range(101)
            ],
            revision=2,
            content_hash="b" * 64,
        )
        persisted = json.loads(evidence.path.read_text(encoding="utf-8"))

        self.assertTrue(load_rule_evidence(evidence.path).complete)
        self.assertEqual(len(persisted["findings"]), 101)
        self.assertEqual(len(persisted["diagnostic_view"]["findings"]), 100)
        self.assertTrue(persisted["diagnostic_view"]["truncated"])
        self.assertTrue(persisted["diagnostic_view"]["machine_evidence_complete"])


if __name__ == "__main__":
    unittest.main()
