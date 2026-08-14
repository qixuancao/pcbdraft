from __future__ import annotations

import io
import unittest
from typing import Any
from unittest.mock import patch

from copperwright.cli import build_parser
from copperwright.errors import ValidationError
from copperwright.tui import TuiController, run_tui_command


def _view(project_id: str, *, status: str = "awaiting_confirmation") -> dict[str, Any]:
    return {
        "project": {
            "id": project_id,
            "name": "Terminal board",
            "status": status,
        },
        "conversation": {
            "messages": [],
            "proposal": {
                "scope": {"decision": "attempted", "warnings": []},
                "brief": {
                    "purpose": "A generic terminal-driven PCB request.",
                    "board": {"width_mm": 60.0, "height_mm": 40.0, "layers": 2},
                    "bom": [],
                    "plan_review": {
                        "summary": {"attention_required": 1, "failed": 1},
                        "findings": [
                            {
                                "id": "power.rail_source_evidence",
                                "outcome": "fail",
                            }
                        ],
                    },
                },
            },
        },
        "artifacts": {},
        "attempts": [],
    }


class FakeProvider:
    provider_id = "fake-planner"


class FakeService:
    def __init__(self) -> None:
        self.provider = FakeProvider()
        self.calls: list[tuple[str, Any]] = []
        self.views = {"existing-project": _view("existing-project", status="draft")}

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            {
                "id": project_id,
                "name": value["project"]["name"],
                "status": value["project"]["status"],
            }
            for project_id, value in sorted(self.views.items())
        ]

    def create_draft(self, name: str) -> dict[str, Any]:
        self.calls.append(("create_draft", name))
        self.views["terminal-board"] = _view("terminal-board", status="draft")
        self.views["terminal-board"]["project"]["name"] = name
        return {"project": {"id": "terminal-board"}}

    def send_message(
        self, project_id: str, text: str, *, timeout: float
    ) -> dict[str, Any]:
        self.calls.append(("send_message", project_id, text, timeout))
        value = self.views[project_id]
        value["project"]["status"] = "awaiting_confirmation"
        value["conversation"]["messages"].append(
            {"role": "user", "kind": "request", "text": text}
        )
        return value

    def open_project(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("open_project", project_id))
        return self.views[project_id]

    def confirm_project(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(("confirm_project", project_id, timeout))
        self.views[project_id]["project"]["status"] = "generated"
        return self.views[project_id]

    def apply_modification(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("apply_modification", project_id))
        return self.views[project_id]

    def validate_project(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(("validate_project", project_id, timeout))
        return self.views[project_id]

    def undo_last_modification(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("undo_last_modification", project_id))
        return self.views[project_id]

    def discard_modification(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("discard_modification", project_id))
        return self.views[project_id]

    def build_release(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(("build_release", project_id, timeout))
        return self.views[project_id]


class CopperWrightTuiTests(unittest.TestCase):
    def test_new_project_flow_uses_the_same_application_service(self) -> None:
        service = FakeService()
        controller = TuiController(service=service, timeout=123.0)

        controller.begin_new()
        controller.submit("Terminal board")
        controller.submit("Create a low-voltage indicator board")

        self.assertEqual(controller.project_id, "terminal-board")
        self.assertEqual(controller.mode, "message")
        self.assertEqual(service.calls[0], ("create_draft", "Terminal board"))
        self.assertEqual(
            service.calls[1],
            (
                "send_message",
                "terminal-board",
                "Create a low-voltage indicator board",
                123.0,
            ),
        )
        self.assertEqual(controller.provider_name, "fake-planner")

    def test_confirm_and_command_palette_are_routed_through_the_controller(
        self,
    ) -> None:
        service = FakeService()
        controller = TuiController(service=service, project_id="existing-project")
        controller.view = _view("existing-project", status="awaiting_confirmation")

        controller.submit("/confirm")
        self.assertEqual(controller.view["project"]["status"], "generated")
        self.assertTrue(any(call[0] == "confirm_project" for call in service.calls))

        controller.submit("/projects")
        self.assertEqual(controller.mode, "project_picker")
        controller.select_picker()
        self.assertEqual(controller.mode, "message")

    def test_tui_is_explicitly_interactive_and_parser_exposes_it(self) -> None:
        parsed = build_parser(prog="copperwright").parse_args(
            ["tui", "--provider", "builtin"]
        )
        self.assertEqual(parsed.command, "tui")
        self.assertEqual(parsed.provider, "builtin")
        with (
            patch("copperwright.tui.sys.stdin", io.StringIO()),
            patch("copperwright.tui.sys.stdout", io.StringIO()),
            self.assertRaisesRegex(ValidationError, "interactive terminal"),
        ):
            run_tui_command(
                workspace=None,
                provider="builtin",
                project_id=None,
                timeout=10.0,
            )


if __name__ == "__main__":
    unittest.main()
