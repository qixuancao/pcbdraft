"""Focused ownership and compatibility tests for project confirmation."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.services import application, application_confirmation
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_confirmation import ApplicationConfirmationMixin


class ApplicationConfirmationTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_confirmation(self) -> None:
        source = Path(application_confirmation.__file__).read_text(encoding="utf-8")
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
            ApplicationService.confirm_project,
            ApplicationConfirmationMixin.confirm_project,
        )
        for retained in (
            "_open",
            "_bind_expected_revision",
            "_event",
            "_write_records",
            "_record_failure",
            "generate_project_previews",
            "validate_project",
        ):
            self.assertNotIn(retained, ApplicationConfirmationMixin.__dict__)

    def test_existing_design_reuses_preview_and_validation_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design_root = Path(temporary) / "design"
            design_root.mkdir()
            project = SimpleNamespace(
                root=Path(temporary),
                design_root=design_root,
                state={"status": "generated", "revision": 7},
            )
            managed = SimpleNamespace(assert_synchronized=Mock())
            preview = {"state": {"revision": 8}}
            expected = {"project": {"status": "validated"}}
            service = object.__new__(ApplicationService)

            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=7),
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ) as opener,
                patch.object(
                    service,
                    "generate_project_previews",
                    return_value=preview,
                ) as generate_preview,
                patch.object(
                    service,
                    "validate_project",
                    return_value=expected,
                ) as validate,
            ):
                result = service.confirm_project(
                    "board",
                    validate=True,
                    timeout=12.0,
                    expected_revision=7,
                )

            self.assertIs(result, expected)
            opener.assert_called_once_with(design_root)
            managed.assert_synchronized.assert_called_once_with()
            generate_preview.assert_called_once_with(
                "board", timeout=12.0, expected_revision=7
            )
            validate.assert_called_once_with("board", timeout=12.0, expected_revision=8)

    def test_status_rejection_resolves_legacy_validation_error_patch(self) -> None:
        class PatchedValidationError(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            project = SimpleNamespace(
                root=Path(temporary),
                design_root=Path(temporary) / "design",
                state={"status": "draft", "revision": 2},
            )
            service = object.__new__(ApplicationService)

            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=2),
                patch.object(application, "ValidationError", PatchedValidationError),
                self.assertRaisesRegex(
                    PatchedValidationError,
                    "project is not awaiting generation confirmation",
                ),
            ):
                service.confirm_project(
                    "board",
                    validate=False,
                    expected_revision=2,
                )


if __name__ == "__main__":
    unittest.main()
