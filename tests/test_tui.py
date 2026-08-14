from __future__ import annotations

import curses
import io
import unittest
from contextlib import redirect_stderr
from typing import Any
from unittest.mock import patch

from copperwright.chat import run_chat_command
from copperwright.cli import build_parser
from copperwright.cli import main as cli_main
from copperwright.errors import ValidationError
from copperwright.providers import CodexIntentProvider
from copperwright.tui import (
    CopperWrightTui,
    TuiController,
    command_suggestions,
    run_tui_command,
)


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

    def diagnostic(self) -> dict[str, Any]:
        return {"id": self.provider_id, "model": "test-model", "available": True}


class CountingProvider:
    provider_id = "counting-planner"

    def __init__(self) -> None:
        self.diagnostic_calls = 0

    def diagnostic(self) -> dict[str, Any]:
        self.diagnostic_calls += 1
        return {"id": self.provider_id, "model": "test-model", "available": True}


class FakeService:
    def __init__(self) -> None:
        self.provider: Any = FakeProvider()
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


class FakeScreen:
    def __init__(self, *, height: int = 30, width: int = 110) -> None:
        self.height = height
        self.width = width
        self.writes: list[str] = []
        self.moves: list[tuple[int, int]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def keypad(self, _enabled: bool) -> None:
        return None

    def erase(self) -> None:
        self.writes = []

    def addnstr(self, _y: int, _x: int, text: str, limit: int, _style: int) -> None:
        self.writes.append(text[:limit])

    def move(self, y: int, x: int) -> None:
        self.moves.append((y, x))

    def refresh(self) -> None:
        return None

    @property
    def text(self) -> str:
        return "\n".join(self.writes)


class QueuedScreen(FakeScreen):
    def __init__(self, keys: list[str | int]) -> None:
        super().__init__()
        self.keys = iter(keys)
        self.erase_count = 0

    def erase(self) -> None:
        self.erase_count += 1
        super().erase()

    def get_wch(self) -> str | int:
        return next(self.keys)


class SwitchedProvider:
    provider_id = "builtin"

    def diagnostic(self) -> dict[str, Any]:
        return {"id": "builtin", "model": False, "available": True}


class CopperWrightTuiTests(unittest.TestCase):
    def _ui(self, controller: TuiController) -> tuple[CopperWrightTui, FakeScreen]:
        screen = FakeScreen()
        with patch("copperwright.tui.curses.has_colors", return_value=False):
            return CopperWrightTui(screen, controller), screen

    def test_first_message_creates_a_project_with_the_same_service(self) -> None:
        service = FakeService()
        controller = TuiController(service=service, timeout=123.0)

        controller.submit("Create a low-voltage indicator board")

        self.assertEqual(controller.project_id, "terminal-board")
        self.assertEqual(controller.mode, "message")
        self.assertEqual(
            service.calls,
            [
                ("create_draft", "low-voltage indicator board"),
                (
                    "send_message",
                    "terminal-board",
                    "Create a low-voltage indicator board",
                    123.0,
                ),
            ],
        )

    def test_confirm_and_project_commands_route_through_controller(self) -> None:
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

    def test_slash_suggestions_show_all_commands_and_filter(self) -> None:
        all_commands = command_suggestions("/")
        self.assertEqual(
            [command.name for command in all_commands],
            [
                "/help",
                "/new",
                "/projects",
                "/open",
                "/status",
                "/model",
                "/confirm",
                "/validate",
                "/undo",
                "/discard",
                "/release",
                "/quit",
            ],
        )
        self.assertEqual(
            [command.name for command in command_suggestions("/mo")], ["/model"]
        )
        self.assertEqual(
            [command.name for command in command_suggestions("/PROJECT")],
            ["/projects"],
        )

    def test_model_command_reports_and_switches_only_the_live_provider(self) -> None:
        service = FakeService()
        controller = TuiController(service=service)

        controller.submit("/model")
        self.assertIn("fake-planner", controller.notice)
        self.assertIn("test-model", controller.notice)

        with patch(
            "copperwright.tui.resolve_provider", return_value=SwitchedProvider()
        ) as resolve:
            controller.submit("/model builtin")
        resolve.assert_called_once_with("builtin")
        self.assertIsInstance(service.provider, SwitchedProvider)
        self.assertIn("builtin", controller.notice)
        self.assertEqual(service.calls, [])

        controller.submit("/model arbitrary-model-id")
        self.assertIn("auto, codex, openai-compatible, or builtin", controller.error)

    def test_codex_diagnostic_reports_the_pinned_model(self) -> None:
        provider = CodexIntentProvider(executable="/not-a-real-codex")
        diagnostic = provider.diagnostic()
        self.assertEqual(diagnostic["model"], "gpt-5.6-sol")
        self.assertEqual(diagnostic["reasoning_effort"], "max")

        service = FakeService()
        service.provider = provider
        self.assertEqual(
            TuiController(service=service).provider_name,
            "codex / gpt-5.6-sol · max",
        )

    def test_palette_renders_all_commands_and_keyboard_completes_or_runs_them(
        self,
    ) -> None:
        service = FakeService()
        controller = TuiController(service=service, project_id="existing-project")
        ui, screen = self._ui(controller)

        ui.input_text = "/"
        ui._render()
        self.assertNotIn("Design evidence", screen.text)
        for command in command_suggestions("/"):
            self.assertIn(command.name, screen.text)
        self.assertIn("show command help", screen.text)
        self.assertIn("Tab complete", screen.text)
        self.assertIn("Esc dismiss", screen.text)

        ui._handle_key("\x1b")
        self.assertFalse(ui._palette_visible())
        ui._handle_key("m")
        self.assertTrue(ui._palette_visible())

        ui.input_text = "/"
        ui.palette_dismissed = False
        ui.palette_index = 0
        ui._handle_key(curses.KEY_DOWN)
        ui._handle_key("\t")
        self.assertEqual(ui.input_text, "/new ")

        ui.input_text = "/va"
        ui._handle_key("\n")
        self.assertEqual(ui.input_text, "/validate")
        self.assertFalse(any(call[0] == "validate_project" for call in service.calls))
        ui._handle_key("\n")
        self.assertTrue(any(call[0] == "validate_project" for call in service.calls))

        ui.input_text = "/open"
        ui._handle_key("\n")
        self.assertEqual(ui.input_text, "/open ")
        ui._handle_key("\n")
        self.assertIn("project id is required", controller.error.casefold())

    def test_plain_unicode_typing_keeps_cbreak_and_does_not_rebuild_the_screen(
        self,
    ) -> None:
        screen = QueuedScreen(["你", "好", "\x11"])
        service = FakeService()
        provider = CountingProvider()
        service.provider = provider
        controller = TuiController(service=service)
        with (
            patch("copperwright.tui.curses.raw") as raw,
            patch("copperwright.tui.curses.curs_set"),
            patch("copperwright.tui.curses.has_colors", return_value=False),
        ):
            ui = CopperWrightTui(screen, controller)
            self.assertEqual(ui.run(), 0)

        raw.assert_not_called()
        self.assertEqual(ui.input_text, "你好")
        self.assertEqual(provider.diagnostic_calls, 1)
        self.assertEqual(screen.erase_count, 1)

    def test_unicode_input_uses_terminal_cell_width_for_the_cursor(self) -> None:
        controller = TuiController(service=FakeService())
        ui, screen = self._ui(controller)
        ui.input_text = "你好"

        ui._render()

        self.assertEqual(
            screen.moves[-1],
            (screen.height - 1, 1 + len("Message › ") + 4),
        )

    def test_unicode_conversation_wraps_by_terminal_cell_width(self) -> None:
        self.assertEqual(
            CopperWrightTui._wrap("你" * 70, 70),
            ["你" * 35, "你" * 35],
        )

    def test_root_parser_launches_tui_and_agent_is_not_a_command(self) -> None:
        parser = build_parser(prog="copperwright")
        parsed = parser.parse_args(
            [
                "--workspace",
                "/tmp/copperwright-workspace",
                "--provider",
                "builtin",
                "--project",
                "existing-project",
                "--timeout",
                "12",
            ]
        )
        self.assertIsNone(parsed.command)
        self.assertIn("without a subcommand", parser.format_help())
        chat = parser.parse_args(
            [
                "chat",
                "--project",
                "existing-project",
                "--message",
                "continue planning",
                "--yes",
                "--validate",
                "--json",
                "--timeout",
                "12",
            ]
        )
        self.assertTrue(chat.as_json)
        self.assertTrue(chat.yes)
        self.assertTrue(chat.chat_validate)
        self.assertEqual(chat.timeout, 12.0)
        for legacy_command in ("agent", "tui"):
            with self.subTest(command=legacy_command):
                with (
                    io.StringIO() as stderr,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as rejected,
                ):
                    parser.parse_args([legacy_command])
                self.assertEqual(rejected.exception.code, 2)

        with patch("copperwright.cli.run_tui_command", return_value=0) as run_tui:
            self.assertEqual(
                cli_main(
                    [
                        "--workspace",
                        "/tmp/copperwright-workspace",
                        "--provider",
                        "builtin",
                        "--project",
                        "existing-project",
                        "--timeout",
                        "12",
                    ]
                ),
                0,
            )
        run_tui.assert_called_once_with(
            workspace="/tmp/copperwright-workspace",
            provider="builtin",
            project_id="existing-project",
            timeout=12.0,
        )

    def test_chat_without_an_explicit_action_does_not_open_a_repl(self) -> None:
        with (
            patch("copperwright.chat.ApplicationService") as service,
            self.assertRaisesRegex(
                ValidationError, "requires --new, --project, or --list"
            ),
        ):
            run_chat_command(
                workspace=None,
                provider="builtin",
                project_id=None,
                new_name=None,
                message=None,
                assume_yes=False,
                undo=False,
                validate=False,
                release=False,
                list_only=False,
                as_json=False,
                timeout=45.0,
            )
        service.assert_not_called()

    def test_tui_requires_an_interactive_terminal(self) -> None:
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
