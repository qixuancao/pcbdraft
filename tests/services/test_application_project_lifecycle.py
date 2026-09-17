from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.io import atomic_write_json, make_directory
from pcbdraft.core.locking import ResourceLock
from pcbdraft.services import application, application_project_lifecycle
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_project_lifecycle import (
    ApplicationProjectLifecycleMixin,
)


class ApplicationProjectLifecycleTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_creation_workflows(
        self,
    ) -> None:
        source = Path(application_project_lifecycle.__file__).read_text(
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
            ApplicationService._prepare_private_draft,
            ApplicationProjectLifecycleMixin._prepare_private_draft,
        )
        self.assertIs(
            ApplicationService.create_draft,
            ApplicationProjectLifecycleMixin.create_draft,
        )
        self.assertIs(
            ApplicationService.create_empty_project,
            ApplicationProjectLifecycleMixin.create_empty_project,
        )
        for retained in ("create_project", "open_project", "list_projects"):
            self.assertIn(retained, ApplicationService.__dict__)
            self.assertNotIn(retained, ApplicationProjectLifecycleMixin.__dict__)

    def test_draft_preserves_private_publication_and_legacy_patch_paths(self) -> None:
        real_replace = os.replace
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="auto")
            with (
                patch.object(
                    application,
                    "_safe_text",
                    return_value="Private Board",
                ) as safe_text,
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    return_value="Safe Board",
                ) as sanitizer,
                patch.object(application, "_slug", return_value="safe-board") as slug,
                patch.object(
                    application.secrets,
                    "token_hex",
                    return_value="01234567",
                ) as token,
                patch.object(application, "utc_timestamp", return_value="timestamp"),
                patch.object(
                    application,
                    "make_directory",
                    wraps=make_directory,
                ) as directory,
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
                patch.object(
                    application.os,
                    "replace",
                    side_effect=real_replace,
                ) as replace,
            ):
                view = service.create_draft("raw name token=private")

            project_id = "safe-board-01234567"
            project_root = service.projects_root / project_id
            self.assertEqual(view["project"]["id"], project_id)
            self.assertEqual(view["project"]["name"], "Safe Board")
            self.assertEqual(view["state"]["status"], "draft")
            self.assertEqual(view["state"]["revision"], 0)
            safe_text.assert_called_once_with(
                "raw name token=private", "project name", limit=512
            )
            sanitizer.assert_called_once_with("Private Board")
            slug.assert_called_once_with("Safe Board")
            token.assert_called_once_with(4)
            self.assertEqual(directory.call_count, 8)
            self.assertEqual(writer.call_count, 2)
            lock.assert_called_once_with(project_root, service.locks_root)
            self.assertTrue(
                any(call.args[1] == project_root for call in replace.call_args_list)
            )
            self.assertTrue(project_root.is_dir())
            self.assertFalse(
                any(
                    path.name.startswith(".")
                    for path in service.projects_root.iterdir()
                )
            )

    def test_empty_project_preserves_materializer_records_and_revision_contract(
        self,
    ) -> None:
        real_replace = os.replace
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="auto")

            def materialize(
                request: object,
                design: object,
                output: Path,
                **kwargs: object,
            ) -> SimpleNamespace:
                output.mkdir()
                (output / "managed.marker").write_text("managed\n", encoding="utf-8")
                self.assertEqual(request.design_id, "empty-board-deadbeef")
                self.assertEqual(
                    design.metadata["generator"],
                    "flat_toolbox_v1",
                )
                self.assertIn("graph", kwargs)
                managed = SimpleNamespace(
                    design=SimpleNamespace(content_hash=lambda: digest),
                    manifest={"hashes": {"ir": digest}},
                )
                return SimpleNamespace(project=managed)

            public = {"project": {"id": "empty-board-deadbeef", "status": "generated"}}
            with (
                patch.object(
                    application,
                    "_sanitize_secret_text",
                    side_effect=lambda value: value,
                ) as sanitizer,
                patch.object(application, "_slug", return_value="empty-board"),
                patch.object(
                    application.secrets,
                    "token_hex",
                    return_value="deadbeef",
                ),
                patch.object(application, "utc_timestamp", return_value="timestamp"),
                patch.object(
                    application,
                    "materialize_managed_design",
                    side_effect=materialize,
                ) as materializer,
                patch.object(
                    application,
                    "ResourceLock",
                    side_effect=lambda *args, **kwargs: ResourceLock(*args, **kwargs),
                ),
                patch.object(
                    application.os,
                    "replace",
                    side_effect=real_replace,
                ) as replace,
                patch.object(service, "open_project", return_value=public) as opener,
            ):
                result = service.create_empty_project("Empty Board")

            project_root = service.projects_root / "empty-board-deadbeef"
            materializer.assert_called_once()
            sanitizer.assert_any_call(
                "Created an empty synchronized semantic and KiCad project"
            )
            self.assertTrue(
                any(call.args[1] == project_root for call in replace.call_args_list)
            )
            opener.assert_called_once_with("empty-board-deadbeef")
            self.assertEqual(result["project"], public["project"])
            self.assertEqual(
                result["tool_result"],
                {
                    "created": True,
                    "synchronized": True,
                    "design_content_hash": digest,
                    "manifest_hashes": {"ir": digest},
                },
            )
            persisted = service._open("empty-board-deadbeef")
            self.assertEqual(persisted.state["status"], "generated")
            self.assertEqual(persisted.state["revision"], 1)
            self.assertEqual(persisted.state["design_revision"], 1)
            self.assertEqual(persisted.state["updated_at"], "timestamp")
            self.assertTrue((project_root / "design" / "managed.marker").is_file())


if __name__ == "__main__":
    unittest.main()
