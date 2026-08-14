from __future__ import annotations

import html
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

from rich.text import Text
from textual.widgets import Input

from pcbdraft.agent_events import AgentActivity, AgentUpdate
from pcbdraft.chat import run_chat_command
from pcbdraft.cli import build_parser
from pcbdraft.cli import main as cli_main
from pcbdraft.errors import ValidationError
from pcbdraft.model_config import connect_provider
from pcbdraft.tui import TuiController, command_suggestions, run_tui_command
from pcbdraft.tui_app import (
    CommandPickerScreen,
    HelpScreen,
    ModelPickerScreen,
    PCBDraftApp,
    ProjectPickerScreen,
    ProviderPickerScreen,
    ReviewScreen,
)
from pcbdraft.tui_projection import project_projection
from pcbdraft.tui_widgets import CommandPalette, Composer, ProjectRail


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
                    "bom": [
                        {"part_id": "mcu", "value": "STM32F405", "quantity": 1},
                        {"part_id": "cap", "value": "100n", "quantity": 3},
                    ],
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


class SwitchedProvider:
    provider_id = "builtin"

    def diagnostic(self) -> dict[str, Any]:
        return {"id": "builtin", "model": False, "available": True}


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self.service = service
        self.active = False
        self.started: tuple[str, str, float] | None = None
        self.cancelled = False

    def start_project(self, name: str, message: str, *, timeout: float) -> AgentUpdate:
        self.started = (name, message, timeout)
        self.active = True
        self.service.views["terminal-board"] = _view("terminal-board", status="draft")
        self.service.views["terminal-board"]["project"]["name"] = name
        return AgentUpdate(
            project_id="terminal-board",
            job_id="20260814T000000Z-acde1234",
            action="agent_message",
            status="queued",
            view=self.service.views["terminal-board"],
        )

    def poll(self) -> AgentUpdate:
        self.active = False
        view = self.service.views["terminal-board"]
        view["project"]["status"] = "validated"
        return AgentUpdate(
            project_id="terminal-board",
            job_id="20260814T000000Z-acde1234",
            action="agent_message",
            status="completed",
            activities=(
                AgentActivity(
                    sequence=1,
                    kind="provider.started",
                    tool="pcb_requirements",
                    label="Understanding board requirements",
                    message="Interpreting request",
                    state="running",
                    level="info",
                    created_at="2026-08-14T00:00:00Z",
                ),
            ),
            view=view,
        )

    def cancel(self) -> AgentUpdate:
        self.cancelled = True
        return AgentUpdate(
            project_id="terminal-board",
            job_id="20260814T000000Z-acde1234",
            action="agent_message",
            status="cancel_requested",
        )


class PCBDraftTuiControllerTests(unittest.TestCase):
    def test_first_message_creates_and_immediately_generates_a_project(self) -> None:
        service = FakeService()
        controller = TuiController(service=service, timeout=123.0)

        controller.submit("Create a low-voltage indicator board")

        self.assertEqual(controller.project_id, "terminal-board")
        self.assertEqual(controller.mode, "message")
        self.assertEqual(controller.view["project"]["status"], "generated")
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
                ("confirm_project", "terminal-board", 123.0),
            ],
        )

    def test_runtime_submission_is_non_blocking_and_keeps_local_echo(self) -> None:
        service = FakeService()
        runtime = FakeRuntime(service)
        controller = TuiController(
            service=service,
            runtime=runtime,  # type: ignore[arg-type]
            timeout=123.0,
        )

        controller.submit("Create a small indicator board")

        self.assertTrue(controller.is_busy)
        self.assertEqual(controller.pending_user_text, "Create a small indicator board")
        self.assertEqual(
            runtime.started,
            ("small indicator board", "Create a small indicator board", 123.0),
        )
        self.assertFalse(any(call[0] == "send_message" for call in service.calls))
        self.assertTrue(controller.poll())
        self.assertFalse(controller.is_busy)
        self.assertEqual(controller.pending_user_text, "")
        self.assertEqual(controller.view["project"]["status"], "validated")
        self.assertEqual(controller.activities[-1].tool, "pcb_requirements")

    def test_quit_requests_stop_before_leaving_an_active_turn(self) -> None:
        service = FakeService()
        runtime = FakeRuntime(service)
        controller = TuiController(
            service=service,
            runtime=runtime,  # type: ignore[arg-type]
            timeout=123.0,
        )
        controller.submit("Create a small indicator board")

        self.assertEqual(controller.submit("/quit"), "continue")
        self.assertTrue(runtime.cancelled)
        self.assertIn("Stop requested", controller.notice)

    def test_new_confirm_and_project_commands_route_through_controller(self) -> None:
        service = FakeService()
        controller = TuiController(service=service, timeout=123.0)
        controller.submit("/new")
        self.assertEqual(controller.mode, "new_request")
        controller.submit("Create a low-voltage indicator board")
        self.assertEqual(controller.view["project"]["status"], "generated")

        controller.view = _view("terminal-board", status="awaiting_confirmation")
        controller.submit("/confirm")
        self.assertEqual(controller.view["project"]["status"], "generated")
        controller.submit("/projects")
        self.assertEqual(controller.mode, "project_picker")
        controller.select_picker()
        self.assertEqual(controller.mode, "message")

    def test_slash_suggestions_show_all_commands_and_filter(self) -> None:
        self.assertEqual(
            [command.name for command in command_suggestions("/")],
            [
                "/help",
                "/new",
                "/projects",
                "/open",
                "/status",
                "/review",
                "/logs",
                "/connect",
                "/models",
                "/stop",
                "/retry",
                "/confirm",
                "/validate",
                "/undo",
                "/discard",
                "/release",
                "/quit",
            ],
        )
        self.assertEqual(
            [command.name for command in command_suggestions("/mo")], ["/models"]
        )

    def test_model_command_reports_and_switches_only_the_live_provider(self) -> None:
        service = FakeService()
        controller = TuiController(service=service)
        controller.submit("/model")
        self.assertIn("fake-planner", controller.notice)
        self.assertIn("test-model", controller.notice)

        with patch(
            "pcbdraft.tui.resolve_provider", return_value=SwitchedProvider()
        ) as resolve:
            controller.submit("/model builtin")
        resolve.assert_called_once_with("builtin")
        self.assertIsInstance(service.provider, SwitchedProvider)

        controller.submit("/model arbitrary-model-id")
        self.assertIn("auto, openai-compatible, or builtin", controller.error)
        self.assertIn("supported providers", controller.error.casefold())

    def test_projection_exposes_pcb_facts_and_pipeline_without_ui_types(self) -> None:
        view = _view("terminal-board", status="awaiting_confirmation")
        projection = project_projection(view)
        self.assertEqual(projection.board_size, "60 × 40 mm")
        self.assertEqual(projection.layer_label, "2 copper")
        self.assertEqual(projection.component_count, 4)
        self.assertEqual(
            [stage.state for stage in projection.stages],
            ["complete", "complete", "pending", "pending", "pending"],
        )

    def test_review_and_log_state_remain_controller_owned(self) -> None:
        service = FakeService()
        controller = TuiController(service=service, project_id="existing-project")
        controller.view = _view("existing-project")
        controller.submit("/logs on")
        self.assertTrue(controller.logs_expanded)
        controller.submit("/review")
        self.assertEqual(controller.mode, "review")
        controller.cancel_overlay()
        self.assertEqual(controller.mode, "message")

    def test_root_parser_launches_tui_and_agent_is_not_a_command(self) -> None:
        parser = build_parser(prog="pcbdraft")
        parsed = parser.parse_args(
            [
                "--workspace",
                "/tmp/pcbdraft-workspace",
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
        with (
            io.StringIO() as stderr,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(["--provider", "unknown-provider"])
        for legacy_command in ("agent", "tui"):
            with self.subTest(command=legacy_command):
                with (
                    io.StringIO() as stderr,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as rejected,
                ):
                    parser.parse_args([legacy_command])
                self.assertEqual(rejected.exception.code, 2)

        with patch("pcbdraft.cli.run_tui_command", return_value=0) as run_tui:
            self.assertEqual(
                cli_main(
                    [
                        "--workspace",
                        "/tmp/pcbdraft-workspace",
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
            workspace="/tmp/pcbdraft-workspace",
            provider="builtin",
            project_id="existing-project",
            timeout=12.0,
        )

    def test_chat_without_an_explicit_action_does_not_open_a_repl(self) -> None:
        with (
            patch("pcbdraft.chat.ApplicationService") as service,
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
            patch("pcbdraft.tui.sys.stdin", io.StringIO()),
            patch("pcbdraft.tui.sys.stdout", io.StringIO()),
            self.assertRaisesRegex(ValidationError, "interactive terminal"),
        ):
            run_tui_command(
                workspace=None,
                provider="builtin",
                project_id=None,
                timeout=10.0,
            )


class PCBDraftTextualTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_frame_has_agent_workspace_and_responsive_pcb_rail(
        self,
    ) -> None:
        controller = TuiController(service=FakeService())
        app = PCBDraftApp(controller)
        async with app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            screenshot = html.unescape(app.export_screenshot()).replace("\xa0", " ")
            self.assertIn("PCBDraft", screenshot)
            self.assertIn("Turn an idea into a reviewable KiCad project", screenshot)
            self.assertIn("AGENT PIPELINE", screenshot)
            self.assertTrue(app.query_one("#project-rail", ProjectRail).display)

        compact = PCBDraftApp(TuiController(service=FakeService()))
        async with compact.run_test(size=(84, 28)) as pilot:
            await pilot.pause()
            self.assertFalse(compact.query_one("#project-rail", ProjectRail).display)

    async def test_slash_palette_filters_and_completes_commands(self) -> None:
        app = PCBDraftApp(TuiController(service=FakeService()))
        async with app.run_test(size=(120, 38)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "/"
            await pilot.pause()
            palette = app.query_one("#command-palette", CommandPalette)
            self.assertTrue(palette.display)
            self.assertEqual(palette.option_count, len(command_suggestions("/")))
            self.assertTrue(composer.has_focus)
            self.assertFalse(composer.cursor_blink)
            first_prompt = palette.get_option_at_index(0).prompt
            self.assertIsInstance(first_prompt, Text)
            self.assertTrue(first_prompt.plain.startswith("›"))

            await pilot.press("down")
            self.assertEqual(palette.highlighted, 1)
            self.assertTrue(composer.has_focus)
            second_prompt = palette.get_option_at_index(1).prompt
            self.assertIsInstance(second_prompt, Text)
            self.assertTrue(second_prompt.plain.startswith("›"))

            await pilot.press("tab")
            self.assertEqual(composer.value, "/new ")
            self.assertFalse(palette.display)

            composer.value = "/va"
            await pilot.press("enter")
            self.assertEqual(composer.value, "/validate")
            self.assertFalse(palette.display)

            composer.value = "/"
            await pilot.pause()
            await pilot.press("up")
            self.assertEqual(palette.selected_command().name, "/quit")

            composer.value = "/not-a-command"
            await pilot.pause()
            self.assertTrue(palette.display)
            self.assertIn(
                "No matching commands",
                palette.get_option_at_index(0).prompt.plain,
            )

    async def test_empty_model_picker_can_open_provider_picker(self) -> None:
        app = PCBDraftApp(TuiController(service=FakeService()))
        async with app.run_test(size=(120, 38)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "/models"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelPickerScreen)
            await pilot.click("#model-connect")
            await pilot.pause()
            self.assertIsInstance(app.screen, ProviderPickerScreen)

    async def test_agent_style_command_palette_and_leader_shortcuts(self) -> None:
        controller = TuiController(service=FakeService())
        app = PCBDraftApp(controller)
        async with app.run_test(size=(120, 38)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "keep this draft"
            await pilot.press("ctrl+p")
            await pilot.pause()
            self.assertIsInstance(app.screen, CommandPickerScreen)
            self.assertEqual(composer.value, "keep this draft")

            command_filter = app.screen.query_one("#command-filter", Input)
            command_filter.value = "help"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(composer.value, "keep this draft")

            composer.value = ""
            await pilot.press("n")
            self.assertEqual(composer.value, "n")
            composer.value = ""
            await pilot.pause()
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertTrue(
                app.query_one("#composer", Composer).has_class("leader-active")
            )
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(controller.mode, "new_request")
            self.assertEqual(composer.value, "")

    async def test_model_filter_arrows_switch_the_highlighted_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            connect_provider("deepseek", api_key="sk-test", path=config_path)
            with patch.dict(os.environ, {"PCBDRAFT_CONFIG": str(config_path)}):
                controller = TuiController(service=FakeService())
                app = PCBDraftApp(controller)
                async with app.run_test(size=(120, 38)) as pilot:
                    composer = app.query_one("#composer-input", Input)
                    composer.value = "/models"
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ModelPickerScreen)
                    model_filter = app.screen.query_one("#model-filter", Input)
                    model_filter.value = "deepseek"
                    await pilot.pause()
                    await pilot.press("down", "enter")
                    await pilot.pause()
                    self.assertEqual(controller.mode, "message")
                    self.assertIn("deepseek-v4-flash", controller.notice)

    async def test_unicode_request_runs_through_the_real_composer(self) -> None:
        service = FakeService()
        app = PCBDraftApp(TuiController(service=service, timeout=12.0))
        async with app.run_test(size=(120, 38)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "做一块小型温度传感器板"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.controller.project_id, "terminal-board")
            self.assertEqual(app.controller.view["project"]["status"], "generated")
            self.assertIn("做一块小型温度传感器板", app.export_screenshot())

    async def test_review_and_project_picker_are_real_modal_screens(self) -> None:
        service = FakeService()
        controller = TuiController(service=service, project_id="existing-project")
        controller.view = _view("existing-project")
        controller.view["conversation"]["proposal"]["brief"]["identity"] = {
            "requested_parts": ["STM32F405"],
            "planned_symbols": [
                {
                    "reference": "U1",
                    "symbol": "MCU_ST_STM32F4:STM32F405RGTx",
                }
            ],
        }
        app = PCBDraftApp(controller)
        async with app.run_test(size=(120, 38)) as pilot:
            await pilot.press("ctrl+x")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            self.assertIsInstance(app.screen, ReviewScreen)
            self.assertIn("STM32F405", app.export_screenshot())
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(controller.mode, "message")

            await pilot.press("ctrl+x")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            self.assertIsInstance(app.screen, ProjectPickerScreen)


if __name__ == "__main__":
    unittest.main()
