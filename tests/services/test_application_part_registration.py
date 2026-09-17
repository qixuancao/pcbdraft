"""Focused ownership and compatibility tests for KiCad part registration."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.services import application, application_part_registration
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_part_registration import (
    ApplicationPartRegistrationMixin,
)


class ApplicationPartRegistrationTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_registration(self) -> None:
        source = Path(application_part_registration.__file__).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("pcbdraft.services.application", imports)
        self.assertIs(
            ApplicationService.register_kicad_part,
            ApplicationPartRegistrationMixin.register_kicad_part,
        )
        for retained in (
            "_open",
            "_bind_expected_revision",
            "_current_progress_and_stage",
            "_event",
            "_write_records",
            "_with_tool_result",
            "open_project",
        ):
            self.assertNotIn(retained, ApplicationPartRegistrationMixin.__dict__)

    def test_missing_design_resolves_legacy_validation_error_patch(self) -> None:
        class PatchedValidationError(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            project = SimpleNamespace(
                design_root=Path(temporary) / "missing-design",
                state={"revision": 4},
            )
            service = object.__new__(ApplicationService)
            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=4),
                patch.object(application, "ValidationError", PatchedValidationError),
                self.assertRaisesRegex(
                    PatchedValidationError,
                    "project has no synchronized design to modify",
                ),
            ):
                service.register_kicad_part(
                    "board",
                    {},
                    timeout=4.0,
                    expected_revision=4,
                )

    def test_managed_project_open_resolves_the_legacy_application_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design_root = Path(temporary) / "design"
            design_root.mkdir()
            project = SimpleNamespace(
                design_root=design_root,
                state={"revision": 4},
            )
            synchronized = Mock(side_effect=RuntimeError("stop after patched open"))
            managed = SimpleNamespace(assert_synchronized=synchronized)
            service = object.__new__(ApplicationService)
            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=4),
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ) as opener,
                self.assertRaisesRegex(RuntimeError, "stop after patched open"),
            ):
                service.register_kicad_part(
                    "board",
                    {},
                    timeout=4.0,
                    expected_revision=4,
                )

            opener.assert_called_once_with(design_root)
            synchronized.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
