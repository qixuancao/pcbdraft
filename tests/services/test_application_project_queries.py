"""Focused project-query extraction and compatibility coverage."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.services import application, application_project_queries
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_project_queries import (
    ApplicationProjectQueriesMixin,
)


class ApplicationProjectQueriesTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_read_surfaces(self) -> None:
        source = Path(application_project_queries.__file__).read_text(encoding="utf-8")
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

        for name in (
            "list_projects",
            "open_project",
            "try_open_project_snapshot",
            "project_root",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(ApplicationService, name),
                    getattr(ApplicationProjectQueriesMixin, name),
                )

        for retained in (
            "set_repository",
            "_use_repository",
            "create_project",
            "record_progress",
        ):
            with self.subTest(retained=retained):
                self.assertIn(retained, ApplicationService.__dict__)
                self.assertNotIn(retained, ApplicationProjectQueriesMixin.__dict__)

    def test_real_project_queries_return_public_views_without_record_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                temporary,
                provider_name="auto",
                recover_interrupted=False,
            )
            first = service.create_draft("First query board")
            second = service.create_draft("Second query board")
            before = {
                path.relative_to(service.root): path.read_bytes()
                for path in service.projects_root.rglob("*.json")
            }

            projects = service.list_projects()
            opened = service.open_project(first["project"]["id"])
            project_root = service.project_root(first["project"]["id"])
            snapshot = service.try_open_project_snapshot(first["project"]["id"])

            after = {
                path.relative_to(service.root): path.read_bytes()
                for path in service.projects_root.rglob("*.json")
            }

        self.assertEqual(
            {item["id"] for item in projects},
            {first["project"]["id"], second["project"]["id"]},
        )
        self.assertEqual(opened["project"]["id"], first["project"]["id"])
        self.assertEqual(project_root.name, first["project"]["id"])
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["project"]["id"], first["project"]["id"])
        self.assertEqual(after, before)

    def test_snapshot_resolves_legacy_lock_and_error_patch_points(self) -> None:
        class PatchedApplicationError(Exception):
            pass

        lock = SimpleNamespace(
            acquire=Mock(
                side_effect=PatchedApplicationError(
                    "resource is locked by another runtime process"
                )
            ),
            release=Mock(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                temporary,
                provider_name="auto",
                recover_interrupted=False,
            )
            project_id = service.create_draft("Busy snapshot")["project"]["id"]
            project_root = service.project_root(project_id)

            with (
                patch.object(application, "PCBDraftError", PatchedApplicationError),
                patch.object(application, "ResourceLock", return_value=lock) as factory,
            ):
                snapshot = service.try_open_project_snapshot(project_id, timeout=0.25)

        self.assertIsNone(snapshot)
        factory.assert_called_once_with(project_root, service.locks_root, timeout=0.25)
        lock.acquire.assert_called_once_with()
        lock.release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
