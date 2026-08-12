from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from pcb_agent.errors import ValidationError
from pcb_agent.external_evidence import load_external_evidence, record_external_evidence
from pcb_agent.managed import generate_managed_project
from pcb_agent.release import (
    build_manufacturing_release,
    verify_manufacturing_release,
)
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

    def test_four_layer_generation_and_real_validation_pass(self) -> None:
        value = copy.deepcopy(controller_requirements_dict())
        value["scope"]["layers"] = 4
        value["board"]["layers"] = 4
        generated = generate_managed_project(
            RequirementsSpec.from_dict(value), self.parent / "four-layer"
        )
        validation = validate_managed_project(
            generated.project, output=self.parent / "four-layer-validation"
        )
        self.assertTrue(validation.candidate_ready)
        self.assertEqual(
            generated.project.manifest["native_snapshots"]["board"]["board"]["layers"],
            4,
        )
        zones = generated.project.manifest["native_snapshots"]["board"]["zones"]
        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0]["net"], "/GND")
        self.assertEqual(zones[0]["layer"], "In1.Cu")
        self.assertTrue(zones[0]["filled"])

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
        checks = {
            check["id"]: check
            for level in report["levels"]
            for check in level["checks"]
        }
        self.assertEqual(checks["l2.ignored_rule_policy"]["outcome"], "pass")
        self.assertEqual(
            checks["l2.not_applicable.erc.simulation_model_issue"]["state"],
            "not_applicable",
        )
        self.assertEqual(checks["l3.i2c_electrical_budget"]["outcome"], "pass")
        self.assertEqual(checks["l3.updi_power_policy"]["outcome"], "pass")

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
        self.assertNotIn("created_at", manifest)
        self.assertTrue(
            all("duration_seconds" not in run for run in manifest["tool_runs"])
        )
        self.assertTrue((result.root / "validation" / "erc.raw.json").is_file())
        with zipfile.ZipFile(result.archive_path) as archive:
            names = set(archive.namelist())
        self.assertNotIn("validation/erc.raw.json", names)
        self.assertNotIn("validation/receipt.json", names)

        repeated = build_manufacturing_release(
            self.generated.project,
            self.parent / "release-repeated",
        )
        self.assertEqual(result.manifest_sha256, repeated.manifest_sha256)
        self.assertEqual(result.archive_sha256, repeated.archive_sha256)
        self.assertEqual(
            result.manifest_path.read_bytes(), repeated.manifest_path.read_bytes()
        )
        verified = verify_manufacturing_release(result.root)
        self.assertEqual(verified.archive_sha256, result.archive_sha256)
        receipt = json.loads((result.root / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {entry["path"] for entry in receipt["audit_artifacts"]},
            {
                "validation/receipt.json",
                "validation/erc.raw.json",
                "validation/drc.raw.json",
            },
        )

        raw_erc = result.root / "validation" / "erc.raw.json"
        original_raw = raw_erc.read_bytes()
        raw_erc.write_bytes(original_raw + b"tamper")
        with self.assertRaisesRegex(ValidationError, "audit artifact hash mismatch"):
            verify_manufacturing_release(result.root)
        raw_erc.write_bytes(original_raw)

        hidden = result.root / "extra"
        hidden.mkdir()
        (hidden / "receipt.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "inventory|untracked"):
            verify_manufacturing_release(result.root)
        (hidden / "receipt.json").unlink()
        hidden.rmdir()

        positions = result.root / "manufacturing" / "positions.csv"
        positions.write_bytes(positions.read_bytes() + b"tamper")
        with self.assertRaisesRegex(ValidationError, "hash mismatch"):
            verify_manufacturing_release(result.root)

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
        index = copy / "external-evidence.json"
        original_index = index.read_bytes()
        malformed = json.loads(original_index)
        del malformed["entries"][0]["metadata"]["reviewer_qualification"]
        index.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "requires metadata"):
            load_external_evidence(copy)
        index.write_bytes(original_index)
        stored = copy / document["entries"][0]["artifacts"][0]["path"]
        stored.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "hash mismatch"):
            load_external_evidence(copy)

    def test_complete_external_l4_l6_l7_chain_can_unlock_production_readiness(
        self,
    ) -> None:
        copy = self.parent / "production-gate-project"
        shutil.copytree(self.generated.project.root, copy)
        records = {
            "L4": {
                "metadata": {
                    "authorized_sourcing_snapshot": "test-only authorized distributor snapshot",
                    "fabricator_capability": "test-only selected process capability",
                    "assembly_profile": "test-only process review",
                },
                "role": "supply and manufacturing engineer",
            },
            "L6": {
                "metadata": {
                    "review_scope": "complete test-only design review",
                    "reviewer_qualification": "test-only electronics engineer",
                },
                "role": "electronics engineer",
            },
            "L7": {
                "metadata": {
                    "board_serial": "TEST-NOT-A-PHYSICAL-BOARD",
                    "test_plan": "test-only schema exercise",
                    "result_summary": "test-only pass record",
                },
                "role": "test engineer",
            },
        }
        for level, record in records.items():
            artifact = self.parent / f"{level.lower()}-test-evidence.txt"
            artifact.write_text(
                f"Synthetic unit-test evidence for {level}; not acceptance evidence.\n",
                encoding="utf-8",
            )
            record_external_evidence(
                copy,
                level=level,
                outcome="pass",
                actor=f"Test {level} Actor",
                role=record["role"],
                performed_at="2026-08-12T00:00:00Z",
                statement="Schema-only test record; not a real external attestation.",
                artifacts=[artifact],
                metadata=record["metadata"],
            )
        result = validate_managed_project(
            copy, output=self.parent / "production-gate-validation"
        )
        self.assertTrue(result.candidate_ready)
        self.assertTrue(result.production_ready)
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertFalse(report["readiness"]["production_claimed"])

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
