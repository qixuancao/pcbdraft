from __future__ import annotations

import hashlib
import re
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

    def test_multilingual_readmes_remain_structurally_aligned(self) -> None:
        paths = [
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "README.ja.md",
            ROOT / "README.ko.md",
        ]
        documents = [path.read_text(encoding="utf-8") for path in paths]
        expected_navigation = [path.name for path in paths]
        for document in documents:
            for name in expected_navigation:
                self.assertIn(f'href="{name}"', document)
            self.assertEqual(document.count("\n## "), 15)
            self.assertIn("docs/assets/brand/copperwright-mark-256.png", document)
            for literal in (
                "low_voltage_i2c_controller_v1",
                "low_voltage_spi_environment_v1",
                "low_voltage_uart_ldo_controller_v1",
                "copperwright app",
                "copperwright chat",
                "copperwright",
                "Apache-2.0",
                "CC0-1.0",
                "CC-BY-SA 4.0",
            ):
                self.assertIn(literal, document)

        def link_targets(document: str) -> list[str]:
            return re.findall(r"\]\(([^)]+)\)", document)

        expected_links = link_targets(documents[0])
        for document in documents[1:]:
            self.assertEqual(link_targets(document), expected_links)
        for target in expected_links:
            if target.startswith(("http://", "https://", "#")):
                continue
            local = target.split("#", 1)[0]
            self.assertTrue((ROOT / local).exists(), f"broken README link: {target}")

        def shell_commands(document: str) -> list[str]:
            blocks = re.findall(r"```bash\n(.*?)```", document, flags=re.DOTALL)
            self.assertEqual(len(blocks), 12)
            return [
                line
                for block in blocks
                for line in block.splitlines()
                if line and not line.lstrip().startswith("#")
            ]

        expected_commands = shell_commands(documents[0])
        for document in documents[1:]:
            self.assertEqual(shell_commands(document), expected_commands)

        expected_table_shape = [
            line.count("|")
            for line in documents[0].splitlines()
            if line.startswith("|")
        ]
        self.assertEqual(len(expected_table_shape), 20)
        for document in documents[1:]:
            self.assertEqual(
                [
                    line.count("|")
                    for line in document.splitlines()
                    if line.startswith("|")
                ],
                expected_table_shape,
            )


if __name__ == "__main__":
    unittest.main()
