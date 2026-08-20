from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from pcbdraft.agent.footprint_resolver import LocalKiCadFootprintResolver
from pcbdraft.core.errors import ValidationError


class FootprintResolverTests(unittest.TestCase):
    def test_search_and_describe_return_installed_file_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / "Resistor_SMD.pretty"
            library.mkdir()
            raw = b'(footprint "R_0603" (pad "1" smd rect) (pad 2 smd rect))'
            (library / "R_0603.kicad_mod").write_bytes(raw)
            resolver = LocalKiCadFootprintResolver(root)

            matches = resolver.find("0603")
            described = resolver.describe("Resistor_SMD:R_0603")

            self.assertEqual(
                [item.footprint for item in matches], [described.footprint]
            )
            self.assertEqual(described.pad_numbers, ("1", "2"))
            self.assertEqual(described.sha256, hashlib.sha256(raw).hexdigest())

    def test_describe_rejects_paths_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / "Safe.pretty"
            library.mkdir()
            target = library / "Target.kicad_mod"
            target.write_text("(footprint Target)", encoding="utf-8")
            (library / "Alias.kicad_mod").symlink_to(target)
            resolver = LocalKiCadFootprintResolver(root)

            with self.assertRaisesRegex(ValidationError, "library id"):
                resolver.describe("../Safe:Target")
            with self.assertRaisesRegex(ValidationError, "unavailable"):
                resolver.describe("Safe:Alias")


if __name__ == "__main__":
    unittest.main()
