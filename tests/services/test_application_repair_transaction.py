"""Focused ownership and compatibility tests for agent repair transactions."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.services import application, application_repair_transaction
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_repair_transaction import (
    ApplicationRepairTransactionMixin,
)


class ApplicationRepairTransactionTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_repair_orchestration(
        self,
    ) -> None:
        source = Path(application_repair_transaction.__file__).read_text(
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
            ApplicationService.prepare_agent_repair,
            ApplicationRepairTransactionMixin.prepare_agent_repair,
        )
        for retained in (
            "_open",
            "_bind_expected_revision",
            "_record_failure",
            "_append_message",
            "_event",
            "_write_records",
            "open_project",
        ):
            self.assertNotIn(retained, ApplicationRepairTransactionMixin.__dict__)

    def test_feedback_and_validation_resolve_legacy_application_patches(self) -> None:
        class PatchedValidationError(Exception):
            pass

        normalized = {"attempt": 3, "summary": "patched feedback"}
        project = SimpleNamespace(
            state={"status": "draft", "active_transaction": None, "revision": 7},
        )
        service = object.__new__(ApplicationService)
        with (
            patch.object(
                application,
                "normalize_repair_feedback",
                return_value=normalized,
            ) as normalize,
            patch.object(application, "ValidationError", PatchedValidationError),
            patch.object(service, "_open", return_value=project),
            patch.object(service, "_bind_expected_revision", return_value=7),
            self.assertRaisesRegex(
                PatchedValidationError,
                "project is not eligible for automatic plan repair",
            ),
        ):
            service.prepare_agent_repair(
                "board",
                {"attempt": 3},
                timeout=4.0,
                expected_revision=7,
            )

        normalize.assert_called_once_with({"attempt": 3})

    def test_pending_reader_resolves_legacy_application_patch(self) -> None:
        class StopAtPendingRead(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            project = SimpleNamespace(
                root=Path(temporary),
                state={
                    "status": "generation_failed",
                    "active_transaction": None,
                    "revision": 7,
                },
            )
            service = object.__new__(ApplicationService)
            with (
                patch.object(
                    application,
                    "normalize_repair_feedback",
                    return_value={"attempt": 1, "summary": "retry"},
                ),
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=7),
                patch.object(
                    application,
                    "load_json_limited",
                    side_effect=StopAtPendingRead("patched pending read"),
                ) as loader,
                self.assertRaisesRegex(StopAtPendingRead, "patched pending read"),
            ):
                service.prepare_agent_repair(
                    "board",
                    {"attempt": 1, "phase": "generation", "summary": "retry"},
                    expected_revision=7,
                )

            loader.assert_called_once_with(
                project.root / application.PENDING_REQUEST_NAME,
                application.APP_FILE_LIMIT,
            )


if __name__ == "__main__":
    unittest.main()
