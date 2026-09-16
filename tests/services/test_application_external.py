from __future__ import annotations

import ast
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.kicad.sync import SyncPreview
from pcbdraft.services import application, application_external_revision
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_external_revision import (
    ApplicationExternalRevisionMixin,
)


class _Design:
    design_id = "external-demo"
    name = "External demo"

    def __init__(self, digest: str) -> None:
        self.digest = digest

    def content_hash(self) -> str:
        return self.digest


class _Managed:
    def __init__(self, root: Path, digest: str, drift: tuple[str, ...]) -> None:
        self.root = root
        self.design = _Design(digest)
        self.manifest = {"files": {}}
        self._drift = drift

    def drift(self) -> tuple[str, ...]:
        return self._drift

    def assert_synchronized(self) -> None:
        if self._drift:
            raise PCBDraftError("unexpected drift")


class ApplicationExternalRevisionTests(unittest.TestCase):
    def _service(self, root: Path) -> tuple[ApplicationService, str, Path]:
        service = ApplicationService(root, provider_name="auto")
        project_id = service.create_draft("External revision")["project"]["id"]
        project_root = service.project_root(project_id)
        design_root = project_root / "design"
        design_root.mkdir()
        state_path = project_root / "project.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({"status": "generated", "revision": 4, "design_revision": 2})
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return service, project_id, design_root

    @staticmethod
    def _preview() -> SyncPreview:
        return SyncPreview(
            project_root=Path("/test-preview"),
            board_sha256="b" * 64,
            manifest_sha256="c" * 64,
            tracked_hashes={"board": "b" * 64, "schematic": "d" * 64},
            change_set=SimpleNamespace(id="kicad_import_reviewed"),
            native_changes=(
                {
                    "reference": "U1",
                    "before": {"x_mm": 1.0},
                    "after": {"x_mm": 2.0},
                },
            ),
            diff={"summary": {"objects_modified": 1}},
        )

    def test_status_distinguishes_clean_and_reviewable_external_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design_root = self._service(Path(temporary))
            clean = _Managed(design_root, "a" * 64, ())
            drifted = _Managed(design_root, "a" * 64, ("board:hash_mismatch",))
            with patch(
                "pcbdraft.services.application.open_managed_project",
                return_value=clean,
            ):
                self.assertEqual(
                    service.external_kicad_change_status(project_id)["state"],
                    "clean",
                )
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    return_value=drifted,
                ),
                patch(
                    "pcbdraft.services.application.preview_kicad_import",
                    return_value=self._preview(),
                ),
            ):
                status = service.external_kicad_change_status(project_id)
            self.assertEqual(status["state"], "review_required")
            self.assertTrue(status["requires_import"])
            self.assertTrue(status["importable"])
            self.assertEqual(status["canonical_revision"], 4)

    def test_status_preserves_legacy_sanitizer_and_native_adapter_patch_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design_root = self._service(Path(temporary))
            drifted = _Managed(design_root, "a" * 64, ("board:hash_mismatch",))
            with (
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=drifted,
                ) as opener,
                patch.object(
                    application,
                    "preview_kicad_import",
                    side_effect=PCBDraftError("token=private"),
                ) as preview,
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    return_value="redacted",
                ) as sanitizer,
            ):
                status = service.external_kicad_change_status(project_id)

            self.assertEqual(status["state"], "unsupported_external_change")
            self.assertEqual(status["limitation"], "redacted")
            opener.assert_called_once_with(design_root)
            preview.assert_called_once_with(drifted)
            sanitizer.assert_called_once_with("token=private")

    def test_explicit_import_advances_one_design_revision_and_invalidates_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design_root = self._service(Path(temporary))
            before = _Managed(design_root, "a" * 64, ("board:hash_mismatch",))
            after = _Managed(design_root, "c" * 64, ())
            transaction = (
                design_root.parent / ".pcbdraft-transactions" / "sync-reviewed"
            )
            transaction.mkdir(parents=True)
            with (
                patch.object(
                    service,
                    "_bind_expected_revision",
                    wraps=service._bind_expected_revision,
                ) as revision_binding,
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=[before, after, after],
                ),
                patch(
                    "pcbdraft.services.application.preview_kicad_import",
                    return_value=self._preview(),
                ),
                patch(
                    "pcbdraft.services.application.apply_kicad_import",
                    return_value=transaction,
                ),
            ):
                result = service.import_external_kicad_revision(
                    project_id,
                    expected_revision=4,
                    expected_preview_token=self._preview().review_token,
                )
            revision_binding.assert_called_once()
            self.assertEqual(revision_binding.call_args.args[1], 4)
            self.assertEqual(
                revision_binding.call_args.kwargs,
                {"operation": "external KiCad import"},
            )
            self.assertEqual(result["state"]["revision"], 6)
            self.assertEqual(result["state"]["design_revision"], 3)
            self.assertEqual(result["state"]["status"], "generated")
            self.assertIsNone(result["state"]["last_validation"])
            self.assertIsNone(result["state"]["last_release"])
            self.assertEqual(result["external_revision"]["content_hash"], "c" * 64)

    def test_failed_import_keeps_design_revision_and_records_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design_root = self._service(Path(temporary))
            before = _Managed(design_root, "a" * 64, ("board:hash_mismatch",))
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    return_value=before,
                ),
                patch(
                    "pcbdraft.services.application.preview_kicad_import",
                    return_value=self._preview(),
                ),
                patch(
                    "pcbdraft.services.application.apply_kicad_import",
                    side_effect=PCBDraftError("staged import failed"),
                ),
                self.assertRaisesRegex(PCBDraftError, "staged import failed"),
            ):
                service.import_external_kicad_revision(
                    project_id,
                    expected_revision=4,
                    expected_preview_token=self._preview().review_token,
                )
            state = json.loads(
                (service.project_root(project_id) / "project.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["revision"], 6)
            self.assertEqual(state["design_revision"], 2)
            self.assertEqual(state["status"], "interrupted")

    def test_changed_review_inputs_are_rejected_before_state_or_native_mutation(
        self,
    ) -> None:
        original = self._preview()
        for changed in (
            replace(original, board_sha256="e" * 64),
            replace(original, manifest_sha256="e" * 64),
            replace(
                original,
                tracked_hashes={**original.tracked_hashes, "schematic": "e" * 64},
            ),
        ):
            with (
                self.subTest(token=changed.review_token),
                tempfile.TemporaryDirectory() as temporary,
            ):
                service, project_id, design_root = self._service(Path(temporary))
                state_path = service.project_root(project_id) / "project.json"
                baseline = state_path.read_bytes()
                with (
                    patch(
                        "pcbdraft.services.application.open_managed_project",
                        return_value=_Managed(
                            design_root, "a" * 64, ("board:hash_mismatch",)
                        ),
                    ),
                    patch(
                        "pcbdraft.services.application.preview_kicad_import",
                        return_value=changed,
                    ),
                    patch("pcbdraft.services.application.apply_kicad_import") as apply,
                    self.assertRaisesRegex(PCBDraftError, "changed since review"),
                ):
                    service.import_external_kicad_revision(
                        project_id,
                        expected_revision=4,
                        expected_preview_token=original.review_token,
                    )
                apply.assert_not_called()
                self.assertEqual(state_path.read_bytes(), baseline)

    def test_mixin_boundary_and_host_adapters_remain_patchable(self) -> None:
        source = Path(application_external_revision.__file__).read_text(
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
            ApplicationService.external_kicad_change_status,
            ApplicationExternalRevisionMixin.external_kicad_change_status,
        )
        self.assertIs(
            ApplicationService.import_external_kicad_revision,
            ApplicationExternalRevisionMixin.import_external_kicad_revision,
        )

        sentinel = object()
        root = Path("/project")
        locks_root = Path("/locks")
        with (
            patch.object(application, "ResourceLock", return_value=sentinel) as lock,
            patch.object(
                application, "utc_timestamp", return_value="timestamp"
            ) as clock,
        ):
            self.assertIs(
                ApplicationService._external_revision_resource_lock(root, locks_root),
                sentinel,
            )
            self.assertEqual(
                ApplicationService._external_revision_timestamp(),
                "timestamp",
            )
        lock.assert_called_once_with(root, locks_root)
        clock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
