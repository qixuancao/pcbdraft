from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from copperwright.managed import generate_managed_project, open_managed_project
from copperwright.requirements import RequirementsSpec
from copperwright.sync import (
    apply_kicad_import,
    preview_kicad_import,
    undo_kicad_import,
)
from tests.requirements_factory import controller_requirements_dict


def _real_kicad_available() -> bool:
    return shutil.which("kicad-cli") is not None and Path("/usr/bin/python3").is_file()


@unittest.skipUnless(_real_kicad_available(), "real KiCad CLI/pcbnew unavailable")
class BidirectionalSyncTests(unittest.TestCase):
    def test_native_pose_preview_apply_and_undo_are_transactional(self) -> None:
        with tempfile.TemporaryDirectory(prefix="copperwright-sync-test-") as temporary:
            root = Path(temporary) / "project"
            generated = generate_managed_project(
                RequirementsSpec.from_dict(controller_requirements_dict()), root
            )
            self.assertFalse(preview_kicad_import(generated.project).has_changes)
            script = """
import pcbnew, sys
path = sys.argv[1]
board = pcbnew.LoadBoard(path)
footprint = next(item for item in board.GetFootprints() if item.GetReference() == 'D1')
position = footprint.GetPosition()
footprint.SetPosition(pcbnew.VECTOR2I(position.x - pcbnew.FromMM(0.25), position.y))
if pcbnew.SaveBoard(path, board) is False:
    raise SystemExit(2)
"""
            result = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-c",
                    script,
                    str(generated.project.board_path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            drifted = open_managed_project(root)
            self.assertIn("board:hash_mismatch", drifted.drift())
            preview = preview_kicad_import(drifted)
            self.assertTrue(preview.has_changes)
            self.assertEqual(preview.native_changes[0]["reference"], "D1")
            self.assertTrue(preview.diff["summary"]["requires_geometry_validation"])

            transaction = apply_kicad_import(preview)
            applied = open_managed_project(root)
            imported = next(
                component
                for component in applied.design.components
                if component.reference == "D1"
            )
            self.assertEqual(imported.placement.x_mm, 13.98)
            self.assertTrue(imported.placement.fixed)
            self.assertEqual(applied.drift(), ())
            receipt = json.loads(
                (transaction / "receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["status"], "applied")

            undo_kicad_import(transaction)
            restored = open_managed_project(root)
            original = next(
                component
                for component in restored.design.components
                if component.reference == "D1"
            )
            self.assertFalse(original.placement.fixed)
            self.assertIn("board:hash_mismatch", restored.drift())


if __name__ == "__main__":
    unittest.main()
