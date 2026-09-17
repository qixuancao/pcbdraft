from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services import application, application_modification_revert
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_modification_revert import (
    ApplicationModificationRevertMixin,
)
from pcbdraft.services.application_progress import (
    _attach_progress,
    _transaction_progress_projection,
)
from pcbdraft.services.progress import EngineeringStage, ProgressVector, StageProjection


class _Managed:
    def __init__(self, digest: str) -> None:
        self.design = SimpleNamespace(content_hash=lambda: digest)
        self.synchronized = False

    def assert_synchronized(self) -> None:
        self.synchronized = True


class _Consistency:
    consistency_passed = True

    @staticmethod
    def to_dict() -> dict[str, object]:
        return {"consistency_passed": True}


class ApplicationModificationRevertTests(unittest.TestCase):
    def _service(
        self,
        root: Path,
        *,
        status: str,
        active_transaction: str | None = None,
        last_transaction: str | None = None,
    ) -> tuple[ApplicationService, str, Path]:
        service = ApplicationService(root, provider_name="auto")
        project_id = service.create_draft("Modification revert")["project"]["id"]
        project_root = service.project_root(project_id)
        design_root = project_root / "design"
        design_root.mkdir()
        (design_root / "live.txt").write_text("live\n", encoding="utf-8")
        state_path = project_root / "project.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(
            {
                "status": status,
                "revision": 4,
                "design_revision": 2,
                "active_transaction": active_transaction,
                "last_transaction": last_transaction,
            }
        )
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return service, project_id, project_root

    def test_mixin_has_no_reverse_import_and_owns_only_revert_workflows(self) -> None:
        source = Path(application_modification_revert.__file__).read_text(
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
            ApplicationService.discard_modification,
            ApplicationModificationRevertMixin.discard_modification,
        )
        self.assertIs(
            ApplicationService.undo_last_modification,
            ApplicationModificationRevertMixin.undo_last_modification,
        )
        for excluded in (
            "apply_modification",
            "preview_modification",
            "build_release",
            "validate_project",
        ):
            self.assertNotIn(excluded, ApplicationModificationRevertMixin.__dict__)

    def test_discard_preserves_revision_records_and_legacy_patch_paths(self) -> None:
        transaction_id = "discard-preview"
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, project_root = self._service(
                Path(temporary),
                status="change_ready",
                active_transaction=transaction_id,
            )
            transaction = project_root / "transactions" / transaction_id
            transaction.mkdir(parents=True)
            receipt_path = transaction / "receipt.json"
            atomic_write_json(
                receipt_path,
                {"status": "ready", "prior_status": "validated"},
            )
            public = {"project": {"status": "validated"}}

            with (
                patch.object(
                    service,
                    "_bind_expected_revision",
                    wraps=service._bind_expected_revision,
                ) as revision_binding,
                patch.object(
                    application,
                    "load_json_limited",
                    wraps=load_json_limited,
                ) as loader,
                patch.object(
                    application,
                    "atomic_write_json",
                    wraps=atomic_write_json,
                ) as writer,
                patch.object(
                    application,
                    "ResourceLock",
                    side_effect=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
                ) as lock,
                patch.object(application, "utc_timestamp", return_value="timestamp"),
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    side_effect=lambda value: f"safe:{value}",
                ) as sanitizer,
                patch.object(service, "open_project", return_value=public) as opener,
            ):
                result = service.discard_modification(
                    project_id,
                    expected_revision=4,
                )

            self.assertIs(result, public)
            revision_binding.assert_called_once()
            self.assertEqual(
                revision_binding.call_args.kwargs,
                {"operation": "candidate discard"},
            )
            loader.assert_called_once_with(receipt_path, application.APP_FILE_LIMIT)
            writer.assert_called_once()
            lock.assert_called_once_with(project_root, service.locks_root)
            sanitizer.assert_any_call(
                "Staged semantic change discarded; authoritative design was untouched."
            )
            opener.assert_called_once_with(project_id)

            persisted = service._open(project_id)
            self.assertEqual(persisted.state["revision"], 5)
            self.assertEqual(persisted.state["status"], "validated")
            self.assertIsNone(persisted.state["active_transaction"])
            self.assertEqual(persisted.state["updated_at"], "timestamp")
            receipt = load_json_limited(receipt_path, application.APP_FILE_LIMIT)
            self.assertEqual(receipt["status"], "discarded")
            self.assertEqual(receipt["discarded_at"], "timestamp")

    def test_undo_preserves_native_swap_progress_and_legacy_patch_paths(self) -> None:
        transaction_id = "applied-revision"
        before_hash = "a" * 64
        after_hash = "b" * 64
        real_replace = os.replace
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, project_root = self._service(
                Path(temporary),
                status="validated",
                last_transaction=transaction_id,
            )
            design_root = project_root / "design"
            transaction = project_root / "transactions" / transaction_id
            before = transaction / "before"
            before.mkdir(parents=True)
            (before / "restored.txt").write_text("restored\n", encoding="utf-8")
            receipt_path = transaction / "receipt.json"
            atomic_write_json(
                receipt_path,
                {
                    "schema": "pcbdraft-agent-repair-transaction",
                    "version": 2,
                    "status": "applied",
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "prior_status": "generated",
                    "prior_validation": None,
                    "prior_preview": None,
                    "prior_release": None,
                },
            )
            atomic_write_json(
                transaction / "semantic-diff.json",
                {
                    "schema": "pcbdraft-semantic-diff",
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                },
            )
            live = _Managed(after_hash)
            restored = _Managed(before_hash)
            before_progress = ProgressVector.unknown(2)
            after_progress = ProgressVector.unknown(3)
            stage = StageProjection(
                EngineeringStage.NOT_STARTED,
                False,
                ("test_fixture",),
            )
            consistency = _Consistency()
            public = {"project": {"status": "generated"}}

            with (
                patch.object(
                    application,
                    "open_managed_project",
                    side_effect=[live, restored],
                ) as managed_opener,
                patch.object(
                    application,
                    "ResourceLock",
                    side_effect=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
                ),
                patch.object(application, "utc_timestamp", return_value="timestamp"),
                patch.object(
                    application,
                    "load_json_limited",
                    wraps=load_json_limited,
                ) as loader,
                patch.object(
                    application,
                    "atomic_write_json",
                    wraps=atomic_write_json,
                ) as writer,
                patch.object(
                    application.os, "replace", side_effect=real_replace
                ) as swap,
                patch.object(
                    application,
                    "_attach_progress",
                    wraps=_attach_progress,
                ) as progress_attacher,
                patch.object(
                    application,
                    "_transaction_progress_projection",
                    wraps=_transaction_progress_projection,
                ) as progress_projection,
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    side_effect=lambda value: f"safe:{value}",
                ) as sanitizer,
                patch.object(
                    service,
                    "_current_progress_and_stage",
                    return_value=(before_progress, stage),
                ),
                patch.object(
                    service,
                    "_managed_progress_and_stage",
                    side_effect=[
                        (before_progress, stage, consistency),
                        (after_progress, stage, consistency),
                    ],
                ),
                patch.object(
                    service,
                    "_require_current_native_consistency",
                    side_effect=lambda value, *_args, **_kwargs: value,
                ),
                patch.object(service, "open_project", return_value=public),
            ):
                result = service.undo_last_modification(
                    project_id,
                    expected_revision=4,
                )

            self.assertIs(result, public)
            self.assertTrue(live.synchronized)
            self.assertTrue(restored.synchronized)
            self.assertEqual(managed_opener.call_count, 2)
            loader.assert_any_call(receipt_path, application.APP_FILE_LIMIT)
            self.assertGreaterEqual(writer.call_count, 4)
            swap.assert_any_call(design_root, transaction / "after")
            swap.assert_any_call(before, design_root)
            progress_attacher.assert_called_once()
            progress_projection.assert_called_once()
            sanitizer.assert_any_call(
                "Undo restored the exact previous authoritative managed project."
            )

            self.assertTrue((transaction / "after" / "live.txt").is_file())
            self.assertTrue((design_root / "restored.txt").is_file())
            persisted = service._open(project_id)
            self.assertEqual(persisted.state["revision"], 5)
            self.assertEqual(persisted.state["design_revision"], 3)
            self.assertEqual(persisted.state["status"], "generated")
            self.assertIsNone(persisted.state["last_transaction"])
            receipt = load_json_limited(receipt_path, application.APP_FILE_LIMIT)
            self.assertEqual(receipt["status"], "undone")
            self.assertEqual(receipt["undo"]["status"], "committed")

    def test_undo_failure_preserves_error_classifier_and_sanitizer_patch_paths(
        self,
    ) -> None:
        transaction_id = "failed-undo"
        before_hash = "a" * 64
        after_hash = "b" * 64
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, project_root = self._service(
                Path(temporary),
                status="validated",
                last_transaction=transaction_id,
            )
            transaction = project_root / "transactions" / transaction_id
            before = transaction / "before"
            before.mkdir(parents=True)
            receipt_path = transaction / "receipt.json"
            atomic_write_json(
                receipt_path,
                {
                    "schema": "pcbdraft-agent-repair-transaction",
                    "version": 2,
                    "status": "applied",
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                },
            )
            progress = ProgressVector.unknown(2)
            stage = StageProjection(
                EngineeringStage.NOT_STARTED,
                False,
                ("test_fixture",),
            )

            with (
                patch.object(
                    application,
                    "open_managed_project",
                    side_effect=[_Managed("wrong-hash"), _Managed(before_hash)],
                ),
                patch.object(
                    application,
                    "ResourceLock",
                    side_effect=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
                ),
                patch.object(
                    application,
                    "_operation_failure_code",
                    return_value="patched_failure_code",
                ) as classifier,
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    return_value="redacted failure",
                ) as sanitizer,
                patch.object(
                    application,
                    "_attach_progress",
                    wraps=_attach_progress,
                ),
                patch.object(
                    service,
                    "_current_progress_and_stage",
                    return_value=(progress, stage),
                ),
                self.assertRaisesRegex(
                    ValidationError,
                    "authoritative design changed after the last transaction",
                ),
            ):
                service.undo_last_modification(
                    project_id,
                    expected_revision=4,
                )

            classifier.assert_called_once()
            self.assertEqual(
                classifier.call_args.kwargs,
                {"stage": "publication", "tool_name": "undo_modification"},
            )
            sanitizer.assert_called_once_with(
                "authoritative design changed after the last transaction"
            )
            receipt = load_json_limited(receipt_path, application.APP_FILE_LIMIT)
            self.assertEqual(receipt["status"], "applied")
            self.assertEqual(receipt["undo"]["status"], "failed")
            self.assertEqual(receipt["undo"]["error_code"], "patched_failure_code")
            self.assertEqual(receipt["undo"]["failure"], "redacted failure")
            self.assertTrue((project_root / "design").is_dir())
            self.assertTrue(before.is_dir())


if __name__ == "__main__":
    unittest.main()
