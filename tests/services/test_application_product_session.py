from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core.io import load_json_limited
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services import application, application_product_session
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_product_session import (
    ApplicationProductSessionMixin,
)
from pcbdraft.services.progress import (
    EngineeringStage,
    ProcessStatus,
    ProductSessionTerminalReceipt,
    ProgressVector,
    StageProjection,
    store_product_session_terminal,
    terminal_outcome,
)


class ApplicationProductSessionTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_terminal_recording(
        self,
    ) -> None:
        source = Path(application_product_session.__file__).read_text(encoding="utf-8")
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
            ApplicationService.record_product_session_terminal,
            ApplicationProductSessionMixin.record_product_session_terminal,
        )
        for excluded in (
            "create_draft",
            "open_project",
            "apply_modification",
            "build_release",
            "validate_project",
        ):
            self.assertNotIn(excluded, ApplicationProductSessionMixin.__dict__)

    def test_terminal_receipt_preserves_immutable_audit_and_legacy_patch_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="auto")
            project_id = service.create_draft("Terminal audit")["project"]["id"]
            project_root = service.project_root(project_id)
            before = service._open(project_id).state
            progress = ProgressVector.unknown(0)
            stage = StageProjection(
                EngineeringStage.NOT_STARTED,
                False,
                ("no_design",),
            )

            with (
                patch.object(
                    application,
                    "product_terminal_receipt_id",
                    return_value="session-patched",
                ) as identity,
                patch.object(
                    application,
                    "ResourceLock",
                    side_effect=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
                ) as lock,
                patch.object(
                    application,
                    "load_json_limited",
                    wraps=load_json_limited,
                ) as loader,
                patch.object(
                    application,
                    "terminal_outcome",
                    wraps=terminal_outcome,
                ) as classifier,
                patch.object(
                    application,
                    "ProductSessionTerminalReceipt",
                    wraps=ProductSessionTerminalReceipt,
                ) as receipt_type,
                patch.object(
                    application,
                    "store_product_session_terminal",
                    wraps=store_product_session_terminal,
                ) as store,
                patch.object(
                    application,
                    "utc_timestamp",
                    return_value="2026-09-17T00:00:00Z",
                ),
                patch.object(
                    service,
                    "_current_progress_and_stage",
                    return_value=(progress, stage),
                ) as progress_projection,
            ):
                receipt = service.record_product_session_terminal(
                    project_id,
                    session_id="pcbdraft-session",
                    turn_id="turn-1",
                    process_status=ProcessStatus.EXITED,
                )
                repeated = service.record_product_session_terminal(
                    project_id,
                    session_id="pcbdraft-session",
                    turn_id="turn-1",
                    process_status="exited",
                )

            self.assertEqual(receipt, repeated)
            self.assertEqual(receipt["receipt_id"], "session-patched")
            self.assertEqual(receipt["release_outcome"], "incomplete")
            self.assertEqual(
                receipt["termination_reason"],
                "agent_returned_before_gate",
            )
            self.assertEqual(
                receipt["artifact"],
                "product-sessions/session-patched.json",
            )
            self.assertEqual(identity.call_count, 2)
            self.assertEqual(lock.call_count, 2)
            progress_projection.assert_called_once()
            receipt_type.assert_called_once()
            receipt_type.from_dict.assert_called_once()
            loader.assert_called_once_with(
                project_root / "product-sessions" / "session-patched.json",
                application.APP_FILE_LIMIT,
            )
            self.assertEqual(classifier.call_count, 2)
            store.assert_called_once()

            after = service._open(project_id).state
            self.assertEqual(after["revision"], before["revision"])
            self.assertEqual(after["event_sequence"], before["event_sequence"])
            self.assertEqual(list((project_root / "events").iterdir()), [])
            artifacts = list((project_root / "product-sessions").iterdir())
            self.assertEqual(
                [path.name for path in artifacts], ["session-patched.json"]
            )

    def test_explicit_receipt_id_keeps_validation_patch_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="auto")
            project_id = service.create_draft("Explicit terminal receipt")["project"][
                "id"
            ]
            with (
                patch.object(
                    application,
                    "validate_product_terminal_receipt_id",
                    return_value="explicit-receipt",
                ) as validator,
                patch.object(
                    service,
                    "_current_progress_and_stage",
                    return_value=(
                        ProgressVector.unknown(0),
                        StageProjection(
                            EngineeringStage.NOT_STARTED,
                            False,
                            ("no_design",),
                        ),
                    ),
                ),
            ):
                receipt = service.record_product_session_terminal(
                    project_id,
                    session_id="pcbdraft-session",
                    turn_id="turn-explicit",
                    process_status=ProcessStatus.EXITED,
                    receipt_id="caller-receipt",
                )

            validator.assert_called_once_with("caller-receipt")
            self.assertEqual(receipt["receipt_id"], "explicit-receipt")


if __name__ == "__main__":
    unittest.main()
