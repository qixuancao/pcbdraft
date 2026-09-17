from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services import application, application_modification_preview
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_modification_preview import (
    ApplicationModificationPreviewMixin,
)


class _Managed:
    def __init__(self, *, generator: str = "agent_plan_v1") -> None:
        self.design = SimpleNamespace(metadata={"generator": generator})
        self.synchronized = False

    def assert_synchronized(self) -> None:
        self.synchronized = True


class ApplicationModificationPreviewTests(unittest.TestCase):
    def _service(self, root: Path) -> tuple[ApplicationService, str, Path]:
        service = ApplicationService(root, provider_name="auto")
        project_id = service.create_draft("Modification preview")["project"]["id"]
        project_root = service.project_root(project_id)
        (project_root / "design").mkdir()
        state_path = project_root / "project.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({"status": "generated", "revision": 4, "design_revision": 2})
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return service, project_id, project_root

    def test_mixin_has_no_reverse_import_and_owns_only_preview_entrypoint(self) -> None:
        source = Path(application_modification_preview.__file__).read_text(
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
            ApplicationService.preview_modification,
            ApplicationModificationPreviewMixin.preview_modification,
        )
        self.assertNotIn(
            "apply_modification", ApplicationModificationPreviewMixin.__dict__
        )
        self.assertNotIn(
            "discard_modification", ApplicationModificationPreviewMixin.__dict__
        )
        self.assertNotIn(
            "undo_modification", ApplicationModificationPreviewMixin.__dict__
        )

    def test_preview_preserves_revision_records_agent_and_legacy_patch_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, project_root = self._service(Path(temporary))
            managed = _Managed()
            feedback = {"phase": "user_request"}
            staged = {"transaction": {"id": "revision-preview"}}

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
                    "ResourceLock",
                    side_effect=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
                ) as lock,
                patch.object(application, "utc_timestamp", return_value="timestamp"),
                patch.object(
                    application,
                    "user_revision_feedback",
                    return_value=feedback,
                ) as feedback_builder,
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    side_effect=lambda value: f"safe:{value}",
                ) as sanitizer,
                patch.object(
                    service,
                    "prepare_agent_repair",
                    return_value=staged,
                ) as prepare,
            ):
                result = service.preview_modification(
                    project_id,
                    "replace the connector token=private",
                    timeout=12.0,
                    expected_revision=4,
                )

            self.assertIs(result, staged)
            self.assertTrue(managed.synchronized)
            revision_binding.assert_called_once()
            self.assertEqual(revision_binding.call_args.args[1], 4)
            self.assertEqual(
                revision_binding.call_args.kwargs,
                {"operation": "revision staging"},
            )
            opener.assert_called_once_with(project_root / "design")
            lock.assert_called_once_with(project_root, service.locks_root)
            feedback_builder.assert_called_once_with(
                "replace the connector token=private"
            )
            sanitizer.assert_any_call("replace the connector token=private")
            prepare.assert_called_once_with(
                project_id,
                feedback,
                timeout=12.0,
                expected_revision=5,
            )

            persisted = service._open(project_id)
            self.assertEqual(persisted.state["revision"], 5)
            self.assertEqual(persisted.state["updated_at"], "timestamp")
            self.assertEqual(
                persisted.conversation["messages"][-1]["text"],
                "safe:replace the connector token=private",
            )
            event_paths = sorted((project_root / "events").glob("*.json"))
            self.assertEqual(len(event_paths), 1)
            event = json.loads(event_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(event["kind"], "repair.requested")
            self.assertTrue(event["message"].startswith("safe:"))

    def test_non_agent_project_is_rejected_before_revision_or_agent_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, _project_root = self._service(Path(temporary))
            with (
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=_Managed(generator="semantic_patch_v1"),
                ),
                patch.object(service, "prepare_agent_repair") as prepare,
                self.assertRaisesRegex(
                    ValidationError,
                    "not generated from a retained agent circuit plan",
                ),
            ):
                service.preview_modification(
                    project_id,
                    "replace the connector",
                    expected_revision=4,
                )

            prepare.assert_not_called()
            self.assertEqual(service._open(project_id).state["revision"], 4)


if __name__ == "__main__":
    unittest.main()
