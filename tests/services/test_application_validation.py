from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.io import load_json_limited
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services import application, application_validation
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_validation import ApplicationValidationMixin


class _Design:
    design_id = "validation-demo"
    name = "Validation demo"

    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.metadata = {"assurance": "verified"}

    def content_hash(self) -> str:
        return self.digest


class _Managed:
    def __init__(self, root: Path, digest: str = "a" * 64) -> None:
        self.root = root
        self.design = _Design(digest)
        self.manifest = {"files": {}}

    def assert_synchronized(self) -> None:
        return None

    def drift(self) -> tuple[str, ...]:
        return ()


class ApplicationValidationCompatibilityTests(unittest.TestCase):
    def _service(self, root: Path) -> tuple[ApplicationService, str, Path]:
        service = ApplicationService(root, provider_name="auto")
        project_id = service.create_draft("Validation workflow")["project"]["id"]
        project_root = service.project_root(project_id)
        design_root = project_root / "design"
        design_root.mkdir()
        state_path = project_root / "project.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({"status": "generated", "revision": 4, "design_revision": 2})
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return service, project_id, design_root

    @staticmethod
    def _write_baseline(project_root: Path) -> tuple[Path, int, str]:
        baseline = project_root / "validation" / "previous"
        baseline.mkdir(parents=True)
        drc = baseline / "drc-evidence.json"
        drc.write_text("{}\n", encoding="utf-8")
        digest = "b" * 64
        (baseline / "receipt.json").write_text(
            json.dumps(
                {
                    "schema": "pcbdraft-validation-receipt",
                    "status": "complete",
                    "source_design_revision": 1,
                    "design_content_hash": digest,
                    "complete_rule_evidence": {"drc": drc.name},
                }
            ),
            encoding="utf-8",
        )
        return drc, 1, digest

    @staticmethod
    def _validation_result(output: Path, digest: str) -> SimpleNamespace:
        output.mkdir(parents=True)
        report = output / "validation.json"
        report.write_text(json.dumps({"levels": []}), encoding="utf-8")
        receipt = output / "receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema": "pcbdraft-validation-receipt",
                    "status": "complete",
                    "design_content_hash": digest,
                }
            ),
            encoding="utf-8",
        )
        erc = output / "erc-evidence.json"
        drc = output / "drc-evidence.json"
        delta = output / "drc-delta.json"
        for path in (erc, drc, delta):
            path.write_text("{}\n", encoding="utf-8")
        return SimpleNamespace(
            report_path=report,
            report_sha256="c" * 64,
            candidate_ready=True,
            production_evidence_complete=False,
            production_ready=False,
            erc_evidence_path=erc,
            drc_evidence_path=drc,
            drc_delta_path=delta,
        )

    def test_mixin_has_no_reverse_import_and_owns_public_workflows(self) -> None:
        source = Path(application_validation.__file__).read_text(encoding="utf-8")
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
            ApplicationService.validate_project,
            ApplicationValidationMixin.validate_project,
        )
        self.assertIs(
            ApplicationService.generate_project_previews,
            ApplicationValidationMixin.generate_project_previews,
        )

    def test_validation_preserves_revision_receipt_and_native_patch_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design_root = self._service(Path(temporary))
            project_root = service.project_root(project_id)
            baseline = self._write_baseline(project_root)
            managed = _Managed(design_root)

            def validate(_managed: object, **kwargs: object) -> SimpleNamespace:
                self.assertIs(_managed, managed)
                self.assertEqual(kwargs["baseline_drc_evidence"], baseline[0])
                self.assertEqual(
                    kwargs["expected_baseline_design_revision"], baseline[1]
                )
                self.assertEqual(kwargs["expected_baseline_content_hash"], baseline[2])
                return self._validation_result(Path(kwargs["output"]), "a" * 64)

            with (
                patch.object(
                    service,
                    "_bind_expected_revision",
                    wraps=service._bind_expected_revision,
                ) as revision_binding,
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ) as opener,
                patch.object(
                    application,
                    "validate_managed_project",
                    side_effect=validate,
                ) as validator,
                patch.object(
                    application,
                    "load_json_limited",
                    wraps=load_json_limited,
                ) as loader,
                patch.object(application, "new_run_id", return_value="current"),
                patch.object(application, "utc_timestamp", return_value="timestamp"),
                patch.object(
                    application,
                    "ResourceLock",
                    side_effect=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
                ) as lock,
            ):
                result = service.validate_project(
                    project_id,
                    timeout=12.0,
                    expected_revision=4,
                )

            self.assertEqual(result["state"]["revision"], 6)
            self.assertEqual(result["state"]["status"], "validated")
            self.assertEqual(result["state"]["last_validation"]["run_id"], "current")
            revision_binding.assert_called_once()
            self.assertEqual(revision_binding.call_args.args[1], 4)
            self.assertEqual(
                revision_binding.call_args.kwargs,
                {"operation": "validation"},
            )
            self.assertGreaterEqual(opener.call_count, 2)
            validator.assert_called_once()
            self.assertGreaterEqual(loader.call_count, 2)
            self.assertEqual(lock.call_count, 2)
            receipt = load_json_limited(
                project_root / "validation" / "current" / "receipt.json",
                1024 * 1024,
            )
            self.assertEqual(receipt["source_design_revision"], 2)

    def test_validation_failure_keeps_legacy_sanitizer_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design_root = self._service(Path(temporary))
            managed = _Managed(design_root)
            with (
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ),
                patch.object(application, "new_run_id", return_value="failed"),
                patch.object(
                    application,
                    "validate_managed_project",
                    side_effect=PCBDraftError("token=private"),
                ),
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    return_value="redacted",
                ) as sanitizer,
                self.assertRaisesRegex(PCBDraftError, "token=private"),
            ):
                service.validate_project(project_id, expected_revision=4)

            project = service._open(project_id)
            self.assertEqual(project.state["status"], "validation_failed")
            self.assertEqual(project.state["revision"], 6)
            self.assertEqual(project.conversation["messages"][-1]["text"], "redacted")
            sanitizer.assert_any_call("token=private")

    def test_preview_preserves_native_renderer_and_revision_patch_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design_root = self._service(Path(temporary))
            project_root = service.project_root(project_id)
            managed = _Managed(design_root)
            preview_root = project_root / "previews" / "preview-run"
            bundle = SimpleNamespace(
                root=preview_root,
                receipt_path=preview_root / "receipt.json",
                design_content_hash="a" * 64,
                files={"board": preview_root / "board.svg"},
            )
            with (
                patch.object(
                    service,
                    "_bind_expected_revision",
                    wraps=service._bind_expected_revision,
                ) as revision_binding,
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ) as opener,
                patch.object(
                    application,
                    "generate_previews",
                    return_value=bundle,
                ) as renderer,
                patch.object(application, "new_run_id", return_value="preview-run"),
            ):
                result = service.generate_project_previews(
                    project_id,
                    timeout=15.0,
                    expected_revision=4,
                )

            self.assertEqual(result["state"]["revision"], 5)
            self.assertEqual(
                result["state"]["last_preview"]["files"],
                {"board": "previews/preview-run/board.svg"},
            )
            revision_binding.assert_called_once()
            self.assertEqual(
                revision_binding.call_args.kwargs,
                {"operation": "preview generation"},
            )
            self.assertGreaterEqual(opener.call_count, 3)
            renderer.assert_called_once_with(
                managed,
                preview_root,
                timeout=15.0,
            )


if __name__ == "__main__":
    unittest.main()
