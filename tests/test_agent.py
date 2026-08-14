from __future__ import annotations

import io
import unittest
from typing import Any
from unittest.mock import patch

from copperwright.agent import (
    START_QUESTION,
    AgentController,
    interactive_agent,
    project_name_from_request,
    run_agent_command,
)
from copperwright.chat import run_chat_command
from copperwright.cli import build_parser
from copperwright.cli import main as cli_main
from copperwright.errors import ValidationError


def _view(
    project_id: str,
    *,
    name: str = "Terminal board",
    status: str = "needs_clarification",
    assistant_text: str = "Should this board use 2 or 4 copper layers?",
) -> dict[str, Any]:
    return {
        "project": {"id": project_id, "name": name, "status": status},
        "conversation": {
            "messages": [
                {
                    "role": "assistant",
                    "kind": "planning",
                    "text": assistant_text,
                }
            ],
            "proposal": None,
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
        self.views = {
            "existing-project": _view(
                "existing-project",
                name="Existing board",
                status="awaiting_confirmation",
                assistant_text="The reviewed plan is ready for confirmation.",
            )
        }

    def create_draft(self, name: str) -> dict[str, Any]:
        self.calls.append(("create_draft", name))
        self.views["new-board"] = _view("new-board", name=name, status="draft")
        return _view("new-board", name=name, status="draft", assistant_text="")

    def send_message(
        self, project_id: str, text: str, *, timeout: float
    ) -> dict[str, Any]:
        self.calls.append(("send_message", project_id, text, timeout))
        view = self.views[project_id]
        view["project"]["status"] = "needs_clarification"
        view["conversation"]["messages"] = [
            {
                "role": "assistant",
                "kind": "planning",
                "text": "Should this board use 2 or 4 copper layers?",
            }
        ]
        return view

    def open_project(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("open_project", project_id))
        return self.views[project_id]

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            {
                "id": project_id,
                "name": view["project"]["name"],
                "status": view["project"]["status"],
            }
            for project_id, view in sorted(self.views.items())
        ]

    def confirm_project(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(("confirm_project", project_id, timeout))
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


class CompactAgentTests(unittest.TestCase):
    def test_first_plain_message_creates_a_named_draft_then_continues_it(self) -> None:
        service = FakeService()
        controller = AgentController(service, timeout=123.0)

        result = controller.submit(
            "Create a 2-layer STM32F405 controller with an SHT31 sensor"
        )

        self.assertEqual(controller.project_id, "new-board")
        self.assertIsNotNone(result.view)
        self.assertEqual(
            service.calls,
            [
                ("create_draft", "2-layer STM32F405 controller with an SHT31 sensor"),
                (
                    "send_message",
                    "new-board",
                    "Create a 2-layer STM32F405 controller with an SHT31 sensor",
                    123.0,
                ),
            ],
        )

        controller.submit("2 layers")
        self.assertEqual(
            service.calls[-1],
            ("send_message", "new-board", "2 layers", 123.0),
        )

    def test_commands_keep_project_management_out_of_the_normal_prompt(self) -> None:
        service = FakeService()
        controller = AgentController(service)

        new = controller.command("/new Sensor revision")
        self.assertEqual(new.lines, (f"{START_QUESTION} Project: Sensor revision",))
        controller.submit("Use an SHT31 over I2C")
        self.assertEqual(service.calls[0], ("create_draft", "Sensor revision"))

        listed = controller.command("/projects")
        self.assertEqual(listed.lines[0], "Projects:")
        self.assertTrue(any("existing-project" in line for line in listed.lines))

        opened = controller.command("/open existing-project")
        self.assertEqual(opened.view["project"]["id"], "existing-project")

    def test_interactive_agent_starts_with_one_question_and_prints_compact_reply(
        self,
    ) -> None:
        service = FakeService()
        output = io.StringIO()

        result = interactive_agent(
            service,
            input_stream=io.StringIO(
                "Create a 2-layer STM32F405 controller with an SHT31 sensor\n/quit\n"
            ),
            output_stream=output,
            timeout=12.0,
        )

        text = output.getvalue()
        self.assertEqual(result, 0)
        self.assertTrue(text.startswith(START_QUESTION + "\n"))
        self.assertIn("[needs clarification]", text)
        self.assertIn("Should this board use 2 or 4 copper layers?", text)
        self.assertNotIn("Scope:", text)
        self.assertEqual(service.calls[0][0], "create_draft")

    def test_project_name_derivation_is_short_and_nonempty(self) -> None:
        self.assertEqual(
            project_name_from_request("Create a compact sensor board."),
            "compact sensor board",
        )
        self.assertEqual(project_name_from_request("   "), "Untitled board")

    def test_chat_interactive_mode_delegates_its_initial_message_to_agent(self) -> None:
        service = FakeService()
        with (
            patch("copperwright.chat.ApplicationService", return_value=service),
            patch("copperwright.chat.sys.stdin") as input_stream,
            patch("copperwright.chat.interactive_agent", return_value=0) as agent,
        ):
            input_stream.isatty.return_value = True
            result = run_chat_command(
                workspace=None,
                provider="builtin",
                project_id=None,
                new_name=None,
                message="Build an LED board",
                assume_yes=False,
                undo=False,
                validate=False,
                release=False,
                list_only=False,
                as_json=False,
                timeout=45.0,
            )
        self.assertEqual(result, 0)
        agent.assert_called_once_with(
            service,
            initial_message="Build an LED board",
            timeout=45.0,
        )

    def test_parser_dispatch_and_noninteractive_guard_are_exposed(self) -> None:
        parsed = build_parser(prog="copperwright").parse_args(
            ["agent", "--provider", "builtin", "--message", "Build an LED board"]
        )
        self.assertEqual(parsed.command, "agent")
        self.assertEqual(parsed.message, "Build an LED board")
        with patch("copperwright.cli.run_agent_command", return_value=0) as agent:
            self.assertEqual(
                cli_main(
                    [
                        "agent",
                        "--provider",
                        "builtin",
                        "--message",
                        "Build an LED board",
                        "--timeout",
                        "12",
                    ]
                ),
                0,
            )
        agent.assert_called_once_with(
            workspace=None,
            provider="builtin",
            project_id=None,
            initial_message="Build an LED board",
            timeout=12.0,
        )
        with (
            patch("copperwright.agent.sys.stdin", io.StringIO()),
            patch("copperwright.agent.sys.stdout", io.StringIO()),
            self.assertRaisesRegex(ValidationError, "interactive terminal"),
        ):
            run_agent_command(
                workspace=None,
                provider="builtin",
                project_id=None,
                initial_message=None,
                timeout=10.0,
            )


if __name__ == "__main__":
    unittest.main()
