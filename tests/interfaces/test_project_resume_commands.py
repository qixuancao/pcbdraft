"""Project matching and resume behavior for the native PCBDraft TUI."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent.tool_bindings import _set_service, set_current_project_id
from pcbdraft.core.errors import ValidationError
from pcbdraft.interfaces.tui import project_commands
from pcbdraft.interfaces.tui.commands import resolve_command


class _ProjectService:
    def __init__(self, root: Path, projects: list[dict]) -> None:
        self.projects_root = root / "projects"
        self._projects = projects
        self.opened: list[str] = []

    def list_projects(self) -> list[dict]:
        return list(self._projects)

    def open_project(self, project_id: str) -> dict:
        self.opened.append(project_id)
        for project in self._projects:
            if project["id"] == project_id:
                return {"project": dict(project)}
        raise ValidationError(f"project not found: {project_id}")


def _projects() -> list[dict]:
    return [
        {
            "id": "power-board-11111111",
            "name": "Power Board",
            "status": "draft",
            "created_at": "2026-09-01T08:00:00Z",
            "updated_at": "2026-09-05T08:00:00Z",
        },
        {
            "id": "sensor-node-22222222",
            "name": "Sensor Node",
            "status": "validated",
            "created_at": "2026-09-02T08:00:00Z",
            "updated_at": "2026-09-08T09:30:00Z",
        },
        {
            "id": "sensor-hub-33333333",
            "name": "Sensor Hub",
            "status": "generated",
            "created_at": "2026-09-03T08:00:00Z",
            "updated_at": "2026-09-07T08:00:00Z",
        },
    ]


class ProjectResumeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = _ProjectService(Path(self.temporary.name), _projects())
        _set_service(self.service)
        set_current_project_id(None)

    def tearDown(self) -> None:
        set_current_project_id(None)
        _set_service(None)
        self.temporary.cleanup()

    def test_matching_projects_is_case_insensitive_ranked_and_recent_first(
        self,
    ) -> None:
        recent = project_commands.matching_projects("", self.service.list_projects())
        self.assertEqual(
            [project["id"] for project in recent],
            [
                "sensor-node-22222222",
                "sensor-hub-33333333",
                "power-board-11111111",
            ],
        )

        matches = project_commands.matching_projects(
            "SENSOR", self.service.list_projects()
        )
        self.assertEqual(
            [project["id"] for project in matches],
            ["sensor-node-22222222", "sensor-hub-33333333"],
        )
        exact_name = project_commands.matching_projects(
            "power board", self.service.list_projects()
        )
        self.assertEqual(exact_name[0]["id"], "power-board-11111111")

    def test_read_only_list_entrypoint_disables_interrupted_work_recovery(self) -> None:
        with patch.object(
            project_commands, "get_service", return_value=self.service
        ) as get_service:
            projects = project_commands.list_projects("sensor")

        get_service.assert_called_once_with(recover_interrupted=False)
        self.assertEqual(len(projects), 2)

    def test_open_resolves_full_id_prefix_exact_name_and_name_fragment(self) -> None:
        cases = (
            ("power-board-11111111", "power-board-11111111"),
            ("sensor-node-2", "sensor-node-22222222"),
            ("22222222", "sensor-node-22222222"),
            ("Power Board", "power-board-11111111"),
            ("hub", "sensor-hub-33333333"),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                result = project_commands.handle_open(query)
                self.assertIn(expected, result)
                self.assertEqual(self.service.opened[-1], expected)

    def test_open_rejects_ambiguous_and_missing_matches(self) -> None:
        with self.assertRaisesRegex(ValidationError, "ambiguous.*Sensor"):
            project_commands.handle_open("sensor")
        self.assertEqual(self.service.opened, [])

        with self.assertRaisesRegex(ValidationError, "project not found.*resume"):
            project_commands.handle_open("missing")
        self.assertEqual(self.service.opened, [])

    def test_open_does_not_hide_ambiguous_id_matches_behind_name_match(self) -> None:
        self.service._projects = [
            {
                "id": "sensor-alpha-aaaaaaaa",
                "name": "Chosen Sensor Board",
                "status": "draft",
            },
            {
                "id": "sensor-beta-bbbbbbbb",
                "name": "Power Board",
                "status": "draft",
            },
        ]

        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            project_commands.handle_open("sensor")
        self.assertEqual(self.service.opened, [])

    def test_open_rejects_duplicate_exact_names(self) -> None:
        self.service._projects.append(
            {
                "id": "power-backup-44444444",
                "name": "Power Board",
                "status": "draft",
            }
        )

        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            project_commands.handle_open("POWER BOARD")
        self.assertEqual(self.service.opened, [])

    def test_projects_text_fallback_shows_recent_current_project_details(self) -> None:
        set_current_project_id("power-board-11111111")

        result = project_commands.handle_projects("")

        self.assertLess(result.index("Sensor Node"), result.index("Sensor Hub"))
        self.assertLess(result.index("Sensor Hub"), result.index("Power Board"))
        self.assertIn("[validated]", result)
        self.assertIn("power-board-11111111", result)
        self.assertIn("short: 11111111", result)
        self.assertIn("updated 2026-09-08 09:30", result)
        self.assertIn("← current", result)

    def test_resume_handler_and_projects_alias_are_registered(self) -> None:
        listing = project_commands.HANDLERS["resume"]("")
        self.assertIn("PCB projects", listing)

        opened = project_commands.HANDLERS["resume"]("hub")
        self.assertIn("sensor-hub-33333333", opened)
        projects_alias = resolve_command("pr")
        self.assertIsNotNone(projects_alias)
        assert projects_alias is not None
        self.assertEqual(projects_alias.name, "projects")


if __name__ == "__main__":
    unittest.main()
