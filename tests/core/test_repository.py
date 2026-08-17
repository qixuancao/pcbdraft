from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core.io import load_json_limited
from pcbdraft.core.repository import (
    REPOSITORY_CONFIG_SCHEMA,
    REPOSITORY_MARKER_NAME,
    REPOSITORY_MARKER_SCHEMA,
    configure_repository,
    current_repository,
)
from pcbdraft.interfaces.cli import main as cli_main
from pcbdraft.services.application import ApplicationService


class ProjectRepositoryTests(unittest.TestCase):
    def test_configured_repository_is_persisted_and_owns_new_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository_path = root / "pcb-repository"
            config_path = root / "config" / "repository.json"
            unrelated_cwd = root / "unrelated-shell-directory"
            unrelated_cwd.mkdir()
            environment = {"PCBDRAFT_REPOSITORY_CONFIG": str(config_path)}
            with patch.dict(os.environ, environment, clear=False):
                configured = configure_repository(repository_path)
                self.assertEqual(configured.root, repository_path.resolve())
                self.assertTrue(config_path.is_file())
                self.assertTrue((repository_path / REPOSITORY_MARKER_NAME).is_file())
                config = load_json_limited(config_path, 16 * 1024)
                marker = load_json_limited(
                    repository_path / REPOSITORY_MARKER_NAME, 16 * 1024
                )
                self.assertEqual(config["schema"], REPOSITORY_CONFIG_SCHEMA)
                self.assertEqual(config["root"], str(repository_path.resolve()))
                self.assertEqual(marker["schema"], REPOSITORY_MARKER_SCHEMA)

                before = Path.cwd()
                try:
                    os.chdir(unrelated_cwd)
                    service = ApplicationService(provider_name="auto")
                    draft = service.create_draft("Repository board")
                finally:
                    os.chdir(before)

                project_id = draft["project"]["id"]
                self.assertEqual(service.root, repository_path.resolve())
                self.assertTrue(
                    service.project_root(project_id).is_relative_to(repository_path)
                )
                self.assertFalse((unrelated_cwd / "projects").exists())

                reopened = current_repository()
                self.assertEqual(reopened.root, repository_path.resolve())
                self.assertFalse(reopened.configured_now)

    def test_explicit_workspace_does_not_replace_persisted_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config" / "repository.json"
            persistent = root / "persistent"
            isolated = root / "isolated-test-workspace"
            environment = {"PCBDRAFT_REPOSITORY_CONFIG": str(config_path)}
            with patch.dict(os.environ, environment, clear=False):
                configure_repository(persistent)
                service = ApplicationService(isolated, provider_name="auto")
                self.assertEqual(service.root, isolated.resolve())
                self.assertEqual(current_repository().root, persistent.resolve())

    def test_repository_command_sets_and_reports_the_persisted_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config" / "repository.json"
            repository_path = root / "from-cli"
            environment = {"PCBDRAFT_REPOSITORY_CONFIG": str(config_path)}
            with patch.dict(os.environ, environment, clear=False):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        cli_main(["repository", str(repository_path), "--json"]),
                        0,
                    )
                self.assertIn(str(repository_path.resolve()), output.getvalue())

                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli_main(["repository", "--json"]), 0)
                self.assertIn('"configured_now": false', output.getvalue())

    def test_existing_legacy_projects_are_recorded_without_moving_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config" / "repository.json"
            legacy = root / "legacy-application"
            (legacy / "projects").mkdir(parents=True)
            environment = {"PCBDRAFT_REPOSITORY_CONFIG": str(config_path)}
            with (
                patch.dict(os.environ, environment, clear=False),
                patch(
                    "pcbdraft.core.repository.legacy_repository_path",
                    return_value=legacy,
                ),
                patch(
                    "pcbdraft.core.repository.default_repository_path",
                    return_value=root / "unused-default",
                ),
            ):
                migrated = current_repository()

            self.assertEqual(migrated.root, legacy.resolve())
            self.assertEqual(migrated.source, "migrated-legacy")
            self.assertTrue((legacy / "projects").is_dir())
            self.assertFalse((root / "unused-default").exists())


if __name__ == "__main__":
    unittest.main()
