from __future__ import annotations

import hashlib
import struct
import tomllib
import unittest
from pathlib import Path

from pcbdraft.interfaces.api import capabilities
from pcbdraft.interfaces.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
BRAND = ROOT / "docs" / "assets" / "brand"


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise AssertionError(f"not a PNG: {path}")
    return struct.unpack(">II", data[16:24])


class PCBDraftBrandingTests(unittest.TestCase):
    def test_distribution_and_canonical_cli_entry_point_are_declared(self) -> None:
        document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = document["project"]
        self.assertEqual(project["name"], "pcbdraft")
        self.assertEqual(project["scripts"]["pcbdraft"], "pcbdraft.interfaces.cli:main")
        self.assertEqual(build_parser(prog="pcbdraft").prog, "pcbdraft")

    def test_api_advertises_current_brand_and_module_surface(self) -> None:
        product = capabilities()["product"]
        self.assertEqual(product["name"], "PCBDraft")
        self.assertEqual(product["distribution"], "pcbdraft")
        self.assertEqual(product["primary_cli"], "pcbdraft")
        self.assertEqual(product["python_module"], "pcbdraft")

    def test_source_mark_is_preserved_and_derivative_dimensions_are_exact(self) -> None:
        source = BRAND / "pcbdraft-mark-v1-source.png"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "abbe8e65746ad62ce7a70e4a81200f1a6f2ec17d56894bc513b41d1466a3b60a",
        )
        self.assertEqual(png_dimensions(source), (1254, 1254))
        for size in (32, 64, 128, 256, 512):
            self.assertEqual(
                png_dimensions(BRAND / f"pcbdraft-mark-{size}.png"),
                (size, size),
            )
        self.assertEqual(
            png_dimensions(BRAND / "pcbdraft-social-preview-1280x640.png"),
            (1280, 640),
        )

    def test_primary_readme_is_the_single_chinese_project_homepage(self) -> None:
        document = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized = document.casefold()
        self.assertIn("docs/assets/brand/pcbdraft-mark-256.png", document)
        for literal in ("pcb 设计智能体", "KiCad", "pcbdraft", "快速开始"):
            self.assertIn(literal.casefold(), normalized)
        self.assertIn("agent-generate", document)
        self.assertNotIn("README.zh-CN.md", document)
        self.assertNotIn("README.ja.md", document)
        self.assertNotIn("README.ko.md", document)
        self.assertNotIn("examples/", document)
        self.assertNotIn("low_voltage_i2c_controller_v1", document)
        self.assertNotIn("EXPERIMENTAL_RP2040_TMP117", document)
        self.assertNotIn(
            "High-risk domains are outside the automated boundary", document
        )

    def test_smoke_script_does_not_call_nonzero_reports_a_clean_design(self) -> None:
        script = (ROOT / "scripts" / "smoke.sh").read_text(encoding="utf-8")
        self.assertIn("does not imply a clean design", script)
        self.assertNotIn("ERC/DRC passed on a temporary copy", script)


if __name__ == "__main__":
    unittest.main()
