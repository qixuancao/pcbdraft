from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services import application, application_release
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_release import ApplicationReleaseMixin


class _Managed:
    def __init__(self, digest: str) -> None:
        self.design = SimpleNamespace(content_hash=lambda: digest)
        self.synchronized = False

    def assert_synchronized(self) -> None:
        self.synchronized = True


class ApplicationReleaseTests(unittest.TestCase):
    def _service(
        self,
        root: Path,
        *,
        digest: str = "a" * 64,
    ) -> tuple[ApplicationService, str, Path, Path]:
        service = ApplicationService(root, provider_name="auto")
        project_id = service.create_draft("Release workflow")["project"]["id"]
        project_root = service.project_root(project_id)
        design_root = project_root / "design"
        design_root.mkdir()
        baseline = project_root / "validation" / "run" / "drc.evidence.json"
        baseline.parent.mkdir(parents=True)
        baseline.write_text("{}\n", encoding="utf-8")
        state_path = project_root / "project.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(
            {
                "status": "validated",
                "revision": 4,
                "design_revision": 2,
                "last_validation": {
                    "candidate_ready": True,
                    "drc_evidence": baseline.relative_to(project_root).as_posix(),
                    "source_design_revision": 2,
                    "source_content_hash": digest,
                },
            }
        )
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return service, project_id, project_root, baseline

    def test_mixin_has_no_reverse_import_and_owns_only_release_build(self) -> None:
        source = Path(application_release.__file__).read_text(encoding="utf-8")
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
            ApplicationService.build_release,
            ApplicationReleaseMixin.build_release,
        )
        self.assertIn("verify_release", ApplicationService.__dict__)
        for excluded in (
            "verify_release",
            "apply_modification",
            "discard_modification",
            "preview_modification",
            "validate_project",
        ):
            self.assertNotIn(excluded, ApplicationReleaseMixin.__dict__)

    def test_release_preserves_audit_verification_and_legacy_patch_paths(self) -> None:
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, project_root, baseline = self._service(
                Path(temporary),
                digest=digest,
            )
            managed = _Managed(digest)
            release_root = project_root / "releases" / "release-run"
            release = SimpleNamespace(
                root=release_root,
                manifest_path=release_root / "release-manifest.json",
                manifest_sha256="b" * 64,
                archive_path=release_root / "release.zip",
                archive_sha256="c" * 64,
                candidate_ready=True,
                production_evidence_complete=True,
                production_ready=False,
            )
            verification_record = {
                "verified": True,
                "manifest_sha256": "b" * 64,
                "archive_sha256": "c" * 64,
            }
            verified = SimpleNamespace(to_dict=lambda: verification_record)
            public = {"project": {"status": "released"}}

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
                ) as managed_opener,
                patch.object(application, "new_run_id", return_value="release-run"),
                patch.object(
                    application,
                    "ResourceLock",
                    side_effect=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
                ) as lock,
                patch.object(application, "utc_timestamp", return_value="timestamp"),
                patch.object(
                    application,
                    "build_manufacturing_release",
                    return_value=release,
                ) as builder,
                patch.object(
                    application,
                    "verify_manufacturing_release",
                    return_value=verified,
                ) as verifier,
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    side_effect=lambda value: f"safe:{value}",
                ) as sanitizer,
                patch.object(service, "open_project", return_value=public) as opener,
            ):
                result = service.build_release(
                    project_id,
                    timeout=15.0,
                    expected_revision=4,
                )

            self.assertIs(result, public)
            self.assertTrue(managed.synchronized)
            revision_binding.assert_called_once()
            self.assertEqual(
                revision_binding.call_args.kwargs,
                {"operation": "release build"},
            )
            managed_opener.assert_called_once_with(project_root / "design")
            self.assertEqual(lock.call_count, 2)
            builder.assert_called_once_with(
                project_root / "design",
                release_root,
                timeout=15.0,
                canonical_revision=5,
                design_revision=2,
                baseline_drc_evidence=baseline,
                expected_baseline_design_revision=2,
                expected_baseline_content_hash=digest,
            )
            verifier.assert_called_once_with(release_root)
            sanitizer.assert_any_call(
                "Manufacturing-candidate bundle was built and verified offline; it is "
                "not a production or physical sign-off claim."
            )
            opener.assert_called_once_with(project_id)

            persisted = service._open(project_id)
            self.assertEqual(persisted.state["status"], "released")
            self.assertEqual(persisted.state["revision"], 6)
            self.assertEqual(persisted.state["updated_at"], "timestamp")
            summary = persisted.state["last_release"]
            self.assertEqual(summary["id"], "release-run")
            self.assertEqual(summary["source_revision"], 5)
            self.assertEqual(summary["source_design_revision"], 2)
            self.assertEqual(summary["source_content_hash"], digest)
            self.assertEqual(summary["offline_verification"], verification_record)
            self.assertFalse(summary["production_claimed"])

    def test_release_failure_retains_recording_and_sanitizer_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, _project_root, _baseline = self._service(
                Path(temporary)
            )
            with (
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=_Managed("a" * 64),
                ),
                patch.object(application, "new_run_id", return_value="failed-run"),
                patch.object(
                    application,
                    "build_manufacturing_release",
                    side_effect=PCBDraftError("token=private"),
                ),
                patch.object(application, "verify_manufacturing_release") as verifier,
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    return_value="redacted failure",
                ) as sanitizer,
                self.assertRaisesRegex(PCBDraftError, "token=private"),
            ):
                service.build_release(project_id, expected_revision=4)

            verifier.assert_not_called()
            sanitizer.assert_any_call("token=private")
            persisted = service._open(project_id)
            self.assertEqual(persisted.state["status"], "release_failed")
            self.assertEqual(persisted.state["revision"], 6)
            self.assertEqual(
                persisted.conversation["messages"][-1]["text"],
                "redacted failure",
            )


if __name__ == "__main__":
    unittest.main()
