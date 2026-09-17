"""Focused ownership and compatibility tests for modification application."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.services import application, application_modification_apply
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_modification_apply import (
    ApplicationModificationApplyMixin,
)


class ApplicationModificationApplyTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_apply_workflow(self) -> None:
        source = Path(application_modification_apply.__file__).read_text(
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
            ApplicationService.apply_modification,
            ApplicationModificationApplyMixin.apply_modification,
        )
        for retained in (
            "_open",
            "_bind_expected_revision",
            "_current_progress_and_stage",
            "_managed_progress_and_stage",
            "_require_current_native_consistency",
            "_append_message",
            "_event",
            "_write_records",
            "generate_project_previews",
        ):
            self.assertNotIn(retained, ApplicationModificationApplyMixin.__dict__)

    def test_validation_error_resolves_legacy_application_patch(self) -> None:
        class PatchedValidationError(Exception):
            pass

        project = SimpleNamespace(
            state={"status": "generated", "active_transaction": None, "revision": 4},
        )
        service = object.__new__(ApplicationService)
        with (
            patch.object(service, "_open", return_value=project),
            patch.object(service, "_bind_expected_revision", return_value=4),
            patch.object(application, "ValidationError", PatchedValidationError),
            self.assertRaisesRegex(
                PatchedValidationError,
                "project has no semantic change awaiting confirmation",
            ),
        ):
            service.apply_modification("board", expected_revision=4)

    def test_receipt_read_reuses_legacy_revert_reader_hook(self) -> None:
        class StopAtReceiptRead(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transaction_id = "candidate-1"
            project = SimpleNamespace(
                root=root,
                state={
                    "status": "change_ready",
                    "active_transaction": transaction_id,
                    "revision": 4,
                },
            )
            service = object.__new__(ApplicationService)
            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_bind_expected_revision", return_value=4),
                patch.object(
                    application,
                    "load_json_limited",
                    side_effect=StopAtReceiptRead("patched receipt read"),
                ) as loader,
                self.assertRaisesRegex(StopAtReceiptRead, "patched receipt read"),
            ):
                service.apply_modification("board", expected_revision=4)

            loader.assert_called_once_with(
                root / "transactions" / transaction_id / "receipt.json",
                application.APP_FILE_LIMIT,
            )


if __name__ == "__main__":
    unittest.main()
