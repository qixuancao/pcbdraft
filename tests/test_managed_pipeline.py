from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pcb_agent.errors import ValidationError
from pcb_agent.external_evidence import load_external_evidence, record_external_evidence
from pcb_agent.managed import generate_managed_project
from pcb_agent.release import build_manufacturing_release
from pcb_agent.requirements import RequirementsSpec
from pcb_agent.validation import validate_managed_project
from tests.requirements_factory import controller_requirements_dict


def _real_kicad_available() -> bool:
    if shutil.which("kicad-cli") is None or not Path("/usr/bin/python3").is_file():
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", "import pcbnew"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@unittest.skipUnless(_real_kicad_available(), "real KiCad CLI/pcbnew unavailable")
class ManagedPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="pcb-agent-managed-test-")
        cls.parent = Path(cls.temporary.name)
        cls.spec = RequirementsSpec.from_dict(controller_requirements_dict())
        cls.generated = generate_managed_project(cls.spec, cls.parent / "project")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_manifest_and_native_generation_are_reproducible(self) -> None:
        first = self.generated.project
        self.assertEqual(first.drift(), ())
        self.assertEqual(
            set(first.manifest["native_snapshots"]),
            {"schematic", "board", "project"},
        )
        second = generate_managed_project(self.spec, self.parent / "second")
        self.assertEqual(
            first.design.content_hash(), second.project.design.content_hash()
        )
        self.assertEqual(first.manifest["hashes"], second.project.manifest["hashes"])
        self.assertEqual(
            first.manifest["native_snapshots"],
            second.project.manifest["native_snapshots"],
        )

    def test_l0_l7_validation_is_candidate_ready_but_not_production_claimed(
        self,
    ) -> None:
        result = validate_managed_project(
            self.generated.project, output=self.parent / "validation"
        )
        self.assertTrue(result.candidate_ready)
        self.assertFalse(result.production_ready)
        levels = {level.level: level for level in result.levels}
        for level in ("L0", "L1", "L2", "L3"):
            self.assertEqual(levels[level].outcome, "pass")
        self.assertEqual(levels["L6"].state, "human_required")
        self.assertEqual(levels["L7"].state, "human_required")
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertFalse(report["readiness"]["production_claimed"])

    def test_real_manufacturing_candidate_contains_cross_checked_outputs(self) -> None:
        result = build_manufacturing_release(
            self.generated.project,
            self.parent / "release",
        )
        self.assertTrue(result.candidate_ready)
        self.assertFalse(result.production_ready)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["contracts"]["bom"]["line_count"], 10)
        self.assertTrue(manifest["contracts"]["fabrication"]["closed_outline"])
        self.assertTrue((result.root / "manufacturing" / "board.step").is_file())
        self.assertTrue((result.root / "manufacturing" / "board-top.png").is_file())
        self.assertTrue(result.archive_path.is_file())
        self.assertFalse(manifest["readiness"]["production_claimed"])

    def test_external_evidence_is_attributed_hashed_and_tamper_evident(self) -> None:
        copy = self.parent / "evidence-project"
        shutil.copytree(self.generated.project.root, copy)
        artifact = self.parent / "review.txt"
        artifact.write_text("independent review record\n", encoding="utf-8")
        record_external_evidence(
            copy,
            level="L6",
            outcome="pass",
            actor="Test Reviewer",
            role="electronics engineer",
            performed_at="2026-08-12T00:00:00Z",
            statement="Test-only external attestation; not acceptance evidence.",
            artifacts=[artifact],
            metadata={
                "review_scope": "test fixture",
                "reviewer_qualification": "test metadata only",
            },
        )
        document = load_external_evidence(copy)
        self.assertEqual(document["entries"][0]["level"], "L6")
        self.assertEqual(
            document["entries"][0]["verification"],
            "externally_supplied_not_independently_verified",
        )
        stored = copy / document["entries"][0]["artifacts"][0]["path"]
        stored.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "hash mismatch"):
            load_external_evidence(copy)

    def test_existing_output_is_never_overwritten(self) -> None:
        occupied = self.parent / "occupied"
        occupied.mkdir()
        marker = occupied / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "already exists"):
            generate_managed_project(self.spec, occupied)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
