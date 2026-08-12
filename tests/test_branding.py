from __future__ import annotations

import hashlib
import struct
import tomllib
import unittest
from pathlib import Path

from pcb_agent.api import capabilities
from pcb_agent.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "docs" / "assets" / "brand"


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise AssertionError(f"not a PNG: {path}")
    return struct.unpack(">II", data[16:24])


class CopperWrightBrandingTests(unittest.TestCase):
    def test_distribution_and_both_cli_entry_points_are_declared(self) -> None:
        document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = document["project"]
        self.assertEqual(project["name"], "copperwright")
        self.assertEqual(project["scripts"]["copperwright"], "pcb_agent.cli:main")
        self.assertEqual(project["scripts"]["pcb-agent"], "pcb_agent.cli:main")
        self.assertEqual(build_parser(prog="copperwright").prog, "copperwright")
        self.assertEqual(build_parser(prog="pcb-agent").prog, "pcb-agent")

    def test_api_advertises_current_brand_and_compatibility_surface(self) -> None:
        product = capabilities()["product"]
        self.assertEqual(product["name"], "CopperWright")
        self.assertEqual(product["distribution"], "copperwright")
        self.assertEqual(product["primary_cli"], "copperwright")
        self.assertEqual(product["compatibility_clis"], ["pcb-agent"])
        self.assertEqual(product["python_module"], "pcb_agent")

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


if __name__ == "__main__":
    unittest.main()
