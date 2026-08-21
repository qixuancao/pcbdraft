from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent.part_resolver import LocalKiCadPartResolver


class LocalKiCadPartResolverTests(unittest.TestCase):
    def test_identifier_search_is_lightweight_and_ranks_full_id_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Device.kicad_sym").write_text(
                '(kicad_symbol_lib\n  (symbol "R")\n  (symbol "R_Small")\n)',
                encoding="utf-8",
            )
            resolver = LocalKiCadPartResolver(root)

            with patch.object(
                resolver,
                "describe",
                side_effect=AssertionError("identifier search parsed a symbol"),
            ):
                matches = resolver.find_ids("Device:R", limit=8)

            self.assertEqual(matches[0], "Device:R")
            self.assertIn("Device:R_Small", matches)

    def test_full_candidate_search_preserves_describe_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Device.kicad_sym").write_text(
                '(kicad_symbol_lib\n  (symbol "R")\n)', encoding="utf-8"
            )
            resolver = LocalKiCadPartResolver(root)

            with patch.object(
                resolver, "describe", return_value="described"
            ) as describe:
                matches = resolver.find("Device:R", limit=1)

            self.assertEqual(matches, ("described",))
            describe.assert_called_once_with("Device:R")


if __name__ == "__main__":
    unittest.main()
