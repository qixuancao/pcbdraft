from __future__ import annotations

import ast
import importlib
import tomllib
import unittest
from importlib.resources import files
from pathlib import Path

import pcbdraft
from pcbdraft._compat import MOVED_MODULES


class PackageStructureTests(unittest.TestCase):
    def test_implementation_modules_are_grouped_by_responsibility(self) -> None:
        package_root = Path(pcbdraft.__file__).resolve().parent
        root_modules = {path.name for path in package_root.glob("*.py")}
        self.assertEqual(
            root_modules,
            {"__init__.py", "__main__.py", "_compat.py"},
        )
        expected_packages = {
            "agent",
            "core",
            "domain",
            "interfaces",
            "kicad",
            "model",
            "services",
            "verification",
        }
        self.assertEqual(
            {
                path.name
                for path in package_root.iterdir()
                if path.is_dir() and (path / "__init__.py").is_file()
            },
            expected_packages,
        )

    def test_canonical_source_does_not_import_historical_module_paths(self) -> None:
        package_root = Path(pcbdraft.__file__).resolve().parent
        violations: list[str] = []
        for path in package_root.rglob("*.py"):
            if path.name == "_compat.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in MOVED_MODULES:
                    violations.append(f"{path.relative_to(package_root)}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    for name in node.names:
                        if name.name in MOVED_MODULES:
                            violations.append(
                                f"{path.relative_to(package_root)}:{node.lineno}"
                            )
        self.assertEqual(violations, [])

    def test_historical_imports_resolve_to_the_canonical_module(self) -> None:
        for historical_name, canonical_name in MOVED_MODULES.items():
            if canonical_name == "pcbdraft.kicad.pcbnew_worker":
                continue  # The isolated worker requires KiCad's system Python.
            with self.subTest(historical_name=historical_name):
                canonical = importlib.import_module(canonical_name)
                historical = importlib.import_module(historical_name)
                self.assertIs(historical, canonical)
                self.assertEqual(historical.__name__, canonical_name)

    def test_tui_styles_are_packaged_beside_the_tui_app(self) -> None:
        stylesheet = files("pcbdraft").joinpath("interfaces", "tui", "styles.tcss")
        self.assertTrue(stylesheet.is_file())

    def test_package_discovery_excludes_historical_distribution_names(self) -> None:
        repository = Path(pcbdraft.__file__).resolve().parents[2]
        document = tomllib.loads(
            (repository / "pyproject.toml").read_text(encoding="utf-8")
        )
        discovery = document["tool"]["setuptools"]["packages"]["find"]
        self.assertEqual(discovery["include"], ["pcbdraft*"])
        self.assertEqual(discovery["exclude"], ["copperwright*"])


if __name__ == "__main__":
    unittest.main()
