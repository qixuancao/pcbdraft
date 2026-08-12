from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcb_agent.errors import ValidationError
from pcb_agent.project import canonical_project, discover_project, resolve_member


class ProjectDiscoveryTests(unittest.TestCase):
    def test_root_ambiguity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for stem in ("alpha", "beta"):
                (root / f"{stem}.kicad_sch").write_text("schematic", encoding="utf-8")
                (root / f"{stem}.kicad_pcb").write_text("board", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "ambiguous root-level"):
                discover_project(root)

    def test_matching_single_pair_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schematic = root / "demo.kicad_sch"
            board = root / "demo.kicad_pcb"
            schematic.write_text("schematic", encoding="utf-8")
            board.write_text("board", encoding="utf-8")
            found = discover_project(root)
            self.assertEqual(found.schematic, schematic)
            self.assertEqual(found.board, board)


class SafePathTests(unittest.TestCase):
    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = canonical_project(temporary)
            with self.assertRaisesRegex(ValidationError, "unsafe relative_path"):
                resolve_member(root, "../escape.kicad_pcb", must_exist=False)

    def test_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("secret", encoding="utf-8")
            try:
                (root / "link.txt").symlink_to(outside)
                with self.assertRaisesRegex(ValidationError, "symlink"):
                    resolve_member(root, "link.txt")
            finally:
                outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

