from __future__ import annotations

import hashlib
import struct
import tomllib
import unittest
from pathlib import Path

from copperwright.api import capabilities
from copperwright.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "docs" / "assets" / "brand"


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise AssertionError(f"not a PNG: {path}")
    return struct.unpack(">II", data[16:24])


class CopperWrightBrandingTests(unittest.TestCase):
    def test_distribution_and_canonical_cli_entry_point_are_declared(self) -> None:
        document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = document["project"]
        self.assertEqual(project["name"], "copperwright")
        self.assertEqual(project["scripts"]["copperwright"], "copperwright.cli:main")
        self.assertEqual(build_parser(prog="copperwright").prog, "copperwright")

    def test_api_advertises_current_brand_and_module_surface(self) -> None:
        product = capabilities()["product"]
        self.assertEqual(product["name"], "CopperWright")
        self.assertEqual(product["distribution"], "copperwright")
        self.assertEqual(product["primary_cli"], "copperwright")
        self.assertEqual(product["python_module"], "copperwright")

    def test_source_mark_is_preserved_and_derivative_dimensions_are_exact(self) -> None:
        source = BRAND / "copperwright-mark-v1-source.png"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "eb9a60f013b0e9413ee58442779884e2fed67f8080b105038367412b406b4004",
        )
        self.assertEqual(png_dimensions(source), (1254, 1254))
        for size in (32, 64, 128, 256, 512):
            self.assertEqual(
                png_dimensions(BRAND / f"copperwright-mark-{size}.png"),
                (size, size),
            )
        self.assertEqual(
            png_dimensions(BRAND / "copperwright-social-preview-1280x640.png"),
            (1280, 640),
        )

    def test_multilingual_readmes_describe_the_generic_runtime(self) -> None:
        paths = [
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "README.ja.md",
            ROOT / "README.ko.md",
        ]
        documents = [path.read_text(encoding="utf-8") for path in paths]
        expected_navigation = [path.name for path in paths]
        for document in documents:
            normalized = document.casefold()
            for name in expected_navigation:
                self.assertIn(f'href="{name}"', document)
            self.assertIn("docs/assets/brand/copperwright-mark-256.png", document)
            for literal in ("agent-safe", "KiCad", "copperwright app", "copperwright"):
                self.assertIn(literal.casefold(), normalized)
            self.assertNotIn("low_voltage_i2c_controller_v1", document)
            self.assertNotIn("EXPERIMENTAL_RP2040_TMP117", document)

        for document in documents[:2]:
            self.assertIn("agent-compile", document)
        self.assertIn("generic", documents[0].casefold())
        self.assertIn("通用", documents[1])

        primary = documents[0]
        self.assertIn("examples/basic_stock_board", primary)
        self.assertIn("stock KiCad", primary)
        self.assertNotIn(
            "High-risk domains are outside the automated boundary", primary
        )

    def test_smoke_script_does_not_call_nonzero_reports_a_clean_design(self) -> None:
        script = (ROOT / "scripts" / "smoke.sh").read_text(encoding="utf-8")
        self.assertIn("does not imply a clean design", script)
        self.assertNotIn("ERC/DRC passed on a temporary copy", script)


if __name__ == "__main__":
    unittest.main()
