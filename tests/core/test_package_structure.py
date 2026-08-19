from __future__ import annotations

import ast
import importlib
import tomllib
import unittest
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

    def test_agent_execution_depends_on_ports_not_application_service(self) -> None:
        """Keep the tool workflow out of the agent/model/service import cycle."""

        package_root = Path(pcbdraft.__file__).resolve().parent
        module_paths = (
            package_root / "agent" / "orchestrator.py",
            package_root / "agent" / "tooling.py",
            package_root / "agent" / "tools.py",
            package_root / "model" / "tool_calls.py",
        )
        forbidden = "pcbdraft.services.application"
        violations: list[str] = []
        imports_by_path: dict[Path, set[str]] = {}
        for path in module_paths:
            imports: set[str] = set()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
                    if node.module == forbidden:
                        violations.append(
                            f"{path.relative_to(package_root)}:{node.lineno}"
                        )
                elif isinstance(node, ast.Import):
                    for name in node.names:
                        imports.add(name.name)
                        if name.name == forbidden:
                            violations.append(
                                f"{path.relative_to(package_root)}:{node.lineno}"
                            )
            imports_by_path[path] = imports
        self.assertEqual(violations, [])
        model_router = package_root / "model" / "tool_calls.py"
        self.assertIn("pcbdraft.agent.policy", imports_by_path[model_router])
        self.assertNotIn("pcbdraft.agent.orchestrator", imports_by_path[model_router])

    def test_application_redaction_alias_stays_compatible(self) -> None:
        from pcbdraft.core.redaction import sanitize_user_text
        from pcbdraft.services.application import sanitize_user_text as legacy_alias

        self.assertIs(legacy_alias, sanitize_user_text)

    def test_agent_design_facade_reexports_the_split_contracts(self) -> None:
        from pcbdraft.agent import compiler, design, part_resolver, plan, review

        self.assertIs(design.AgentDesignRequest, plan.AgentDesignRequest)
        self.assertIs(design.CircuitPlan, plan.CircuitPlan)
        self.assertIs(
            design.LocalKiCadPartResolver, part_resolver.LocalKiCadPartResolver
        )
        self.assertIs(design.review_agent_plan, review.review_agent_plan)
        self.assertIs(design.compile_agent_plan, compiler.compile_agent_plan)

    def test_canonical_source_does_not_depend_on_agent_design_facade(self) -> None:
        package_root = Path(pcbdraft.__file__).resolve().parent
        forbidden = "pcbdraft.agent.design"
        violations: list[str] = []
        for path in package_root.rglob("*.py"):
            if path == package_root / "agent" / "design.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                is_from_facade = (
                    isinstance(node, ast.ImportFrom) and node.module == forbidden
                )
                is_module_facade = isinstance(node, ast.Import) and any(
                    name.name == forbidden for name in node.names
                )
                if is_from_facade or is_module_facade:
                    violations.append(f"{path.relative_to(package_root)}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_provider_catalog_uses_low_level_contracts_not_model_transport(
        self,
    ) -> None:
        package_root = Path(pcbdraft.__file__).resolve().parent
        config_path = package_root / "model" / "config.py"
        contracts_path = package_root / "model" / "contracts.py"

        def imported_modules(path: Path) -> set[str]:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            return {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }

        config_imports = imported_modules(config_path)
        contracts_imports = imported_modules(contracts_path)
        self.assertIn("pcbdraft.model.contracts", config_imports)
        self.assertNotIn("pcbdraft.model.api", config_imports)
        self.assertNotIn("pcbdraft.model.api", contracts_imports)
        self.assertNotIn("pcbdraft.model.config", contracts_imports)

    def test_canonical_package_import_graph_is_acyclic(self) -> None:
        """Prevent module cycles from re-coupling UI, services, and adapters."""

        package_root = Path(pcbdraft.__file__).resolve().parent
        names_by_path: dict[Path, str] = {}
        for path in package_root.rglob("*.py"):
            relative = path.relative_to(package_root).with_suffix("")
            parts = (
                relative.parts[:-1] if relative.name == "__init__" else relative.parts
            )
            names_by_path[path] = ".".join(("pcbdraft", *parts))
        known = set(names_by_path.values())

        def canonical_module(name: str) -> str | None:
            candidate = name
            while candidate not in known and "." in candidate:
                candidate = candidate.rsplit(".", 1)[0]
            return candidate if candidate in known else None

        edges: dict[str, set[str]] = {name: set() for name in known}
        for path, source in names_by_path.items():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(name.name for name in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
                    imports.update(f"{node.module}.{name.name}" for name in node.names)
            for imported in imports:
                target = canonical_module(imported)
                if target is not None and target != source:
                    edges[source].add(target)

        visiting: set[str] = set()
        visited: set[str] = set()
        cycles: list[tuple[str, ...]] = []

        def visit(module: str, trail: tuple[str, ...]) -> None:
            if module in visiting:
                cycles.append((*trail[trail.index(module) :], module))
                return
            if module in visited:
                return
            visiting.add(module)
            for dependency in sorted(edges[module]):
                visit(dependency, (*trail, module))
            visiting.remove(module)
            visited.add(module)

        for module in sorted(known):
            visit(module, ())
        self.assertEqual(cycles, [])

    def test_historical_imports_resolve_to_the_canonical_module(self) -> None:
        for historical_name, canonical_name in MOVED_MODULES.items():
            if canonical_name == "pcbdraft.kicad.pcbnew_worker":
                continue  # The isolated worker requires KiCad's system Python.
            with self.subTest(historical_name=historical_name):
                canonical = importlib.import_module(canonical_name)
                historical = importlib.import_module(historical_name)
                self.assertIs(historical, canonical)
                self.assertEqual(historical.__name__, canonical_name)

    def test_interfaces_own_the_cli_and_hermes_terminal_only(self) -> None:
        """The interactive frontend is the Hermes terminal, not a TUI package."""

        package_root = Path(pcbdraft.__file__).resolve().parent
        interfaces = package_root / "interfaces"
        self.assertEqual(
            {path.name for path in interfaces.glob("*.py")},
            {
                "__init__.py",
                "cli.py",
                "commands.py",
                "hermes_cli.py",
                "hermes_plugin.py",
                "terminal_text.py",
            },
        )
        self.assertFalse(
            (interfaces / "tui").exists(),
            "the old Textual frontend must not remain in the package",
        )

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
