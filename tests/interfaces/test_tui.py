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

from rich.console import Console
from rich.text import Text
from textual.widgets import Input, OptionList

from pcbdraft.agent.events import AgentActivity, AgentUpdate
from pcbdraft.core.errors import ValidationError
from pcbdraft.core.repository import configure_repository
from pcbdraft.interfaces.chat import run_chat_command
from pcbdraft.interfaces.cli import build_parser
from pcbdraft.interfaces.cli import main as cli_main
from pcbdraft.interfaces.tui.app import (
    CommandPickerScreen,
    HelpScreen,
    ModelPickerScreen,
    NewProjectScreen,
    PCBDraftApp,
    ProjectPickerScreen,
    ProviderConnectScreen,
    ProviderPickerScreen,
    ReviewScreen,
)
from pcbdraft.interfaces.tui.controller import (
    TuiController,
    command_suggestions,
    run_tui_command,
)
from pcbdraft.interfaces.tui.projection import project_projection
from pcbdraft.interfaces.tui.widgets import (
    CommandPalette,
    Composer,
    _activity_panel,
)
from pcbdraft.model.config import connect_provider
from pcbdraft.services.application import ApplicationService


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
                    "net_count": 3,
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
    provider_id = "switched-test"

    def diagnostic(self) -> dict[str, Any]:
        return {"id": "switched-test", "model": False, "available": True}


class FakeRuntime:
    def __init__(self, service: FakeService, *, polls_before_terminal: int = 0) -> None:
        self.service = service
        self.active = False
        self.started: tuple[str, str, float] | None = None
        self.active_project_id = "terminal-board"
        self.cancelled = False
        self.approval_decisions: list[bool] = []
        self.approval_checkpoints: list[dict[str, Any]] = []
        self.polls_before_terminal = polls_before_terminal

    def start_project(self, name: str, message: str, *, timeout: float) -> AgentUpdate:
        self.started = (name, message, timeout)
        self.active_project_id = "terminal-board"
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

    def submit_message(
        self, project_id: str, message: str, *, timeout: float
    ) -> AgentUpdate:
        self.started = (project_id, message, timeout)
        self.active_project_id = project_id
        self.active = True
        return AgentUpdate(
            project_id=project_id,
            job_id="20260814T000000Z-acde1234",
            action="agent_message",
            status="queued",
            view=self.service.views[project_id],
        )

    def restore_project(self, project_id: str) -> AgentUpdate:
        return AgentUpdate(
            project_id=project_id,
            job_id="",
            action="restore",
            status="idle",
            view=self.service.views[project_id],
        )

    def poll(self) -> AgentUpdate | None:
        if self.polls_before_terminal:
            self.polls_before_terminal -= 1
            return None
        self.active = False
        view = self.service.views[self.active_project_id]
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

    def resolve_pending(
        self,
        project_id: str,
        *,
        checkpoint: dict[str, Any],
        approve: bool,
        timeout: float,
    ) -> AgentUpdate:
        del timeout
        self.approval_decisions.append(approve)
        self.approval_checkpoints.append(dict(checkpoint))
        self.active_project_id = project_id
        self.active = approve
        return AgentUpdate(
            project_id=project_id,
            job_id="approval-job",
            action="agent_message" if approve else "agent_approval",
            status="queued" if approve else "cancelled",
            view=self.service.views[project_id],
            turn_id="turn-1",
            turn_status="running" if approve else "cancelled",
        )


class PCBDraftTuiControllerTests(unittest.TestCase):
    def test_new_creates_only_a_named_project_folder(self) -> None:
        service = FakeService()
        controller = TuiController(service=service, timeout=123.0)

        controller.submit("/new")
        self.assertEqual(controller.mode, "new_name")
        controller.submit("Low-voltage indicator")

        self.assertEqual(controller.project_id, "terminal-board")
        self.assertEqual(controller.mode, "message")
        self.assertEqual(controller.view["project"]["status"], "draft")
        self.assertEqual(
            service.calls,
            [
                ("create_draft", "Low-voltage indicator"),
                ("open_project", "terminal-board"),
            ],
        )
        self.assertIn("Describe the board", controller.notice)

    def test_first_message_starts_a_project_and_agent_turn_directly(self) -> None:
        service = FakeService()
        runtime = FakeRuntime(service)
        controller = TuiController(
            service=service,
            runtime=runtime,  # type: ignore[arg-type]
            timeout=123.0,
        )

        controller.submit("Create a low-voltage indicator board")

        self.assertEqual(controller.project_id, "terminal-board")
        self.assertEqual(
            runtime.started,
            (
                "low-voltage indicator board",
                "Create a low-voltage indicator board",
                123.0,
            ),
        )
        self.assertEqual(
            controller.pending_user_text, "Create a low-voltage indicator board"
        )
        self.assertTrue(controller.is_busy)
        self.assertEqual(controller.error, "")

    def test_command_startup_prefers_the_project_chooser_over_auto_resume(self) -> None:
        service = FakeService()

        chooser = TuiController(service=service, startup_project_picker=True)
        self.assertIsNone(chooser.project_id)
        self.assertEqual(chooser.mode, "project_picker")
        self.assertIn("Choose a project", chooser.notice)

        explicit = TuiController(
            service=service,
            startup_project_picker=True,
            project_id="existing-project",
        )
        self.assertEqual(explicit.project_id, "existing-project")
        self.assertEqual(explicit.mode, "message")

    def test_named_tui_project_is_persisted_as_an_empty_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(
                workspace=Path(temporary) / "repository", provider_name="auto"
            )
            controller = TuiController(service=service)

            controller.create_named_project("Bench supply")

            self.assertIsNotNone(controller.project_id)
            self.assertEqual(controller.view["project"]["status"], "draft")
            project_root = service.project_root(controller.project_id or "")
            self.assertTrue(project_root.is_dir())
            self.assertTrue((project_root / "conversation.json").is_file())
            self.assertEqual(
                controller.view["conversation"]["messages"],
                [],
            )

    def test_runtime_submission_is_non_blocking_and_keeps_local_echo(self) -> None:
        service = FakeService()
        runtime = FakeRuntime(service)
        controller = TuiController(
            service=service,
            runtime=runtime,  # type: ignore[arg-type]
            timeout=123.0,
            project_id="existing-project",
        )

        controller.submit("Create a small indicator board")

        self.assertTrue(controller.is_busy)
        self.assertEqual(controller.pending_user_text, "Create a small indicator board")
        self.assertEqual(
            runtime.started,
            ("existing-project", "Create a small indicator board", 123.0),
        )
        self.assertFalse(any(call[0] == "send_message" for call in service.calls))
        self.assertTrue(controller.poll())
        self.assertFalse(controller.is_busy)
        self.assertEqual(controller.pending_user_text, "")
        self.assertEqual(controller.view["project"]["status"], "validated")
        self.assertEqual(controller.activities[-1].tool, "pcb_requirements")

    def test_new_turn_keeps_prior_tool_activity_for_the_same_project(self) -> None:
        service = FakeService()
        runtime = FakeRuntime(service)
        controller = TuiController(
            service=service,
            runtime=runtime,  # type: ignore[arg-type]
            project_id="existing-project",
        )
        historical = AgentActivity(
            sequence=0,
            kind="tool.completed",
            tool="pcb_validate",
            label="Checking the PCB candidate",
            message="Tool call completed",
            state="completed",
            level="info",
            created_at="2026-08-14T00:00:00Z",
            turn_id="turn-history",
            tool_call_id="call-history",
            effect="evidence_write",
            risk="low",
            arguments={},
            args_hash="a" * 64,
            before_revision=2,
            after_revision=3,
            result={"after_status": "validated"},
        )
        controller.activities = [historical]

        controller.submit("Add a status LED")

        self.assertEqual(controller.activities, [historical])

        output = io.StringIO()
        Console(file=output, width=180, color_system=None).print(
            _activity_panel([historical], expanded=True, busy=False)
        )
        rendered = output.getvalue()
        self.assertIn("evidence write", rendered)
        self.assertIn("low risk", rendered)
        self.assertIn("rev 2 → 3", rendered)
        self.assertIn('result  {"after_status":"validated"}', rendered)

    def test_quit_requests_stop_before_leaving_an_active_turn(self) -> None:
        service = FakeService()
        runtime = FakeRuntime(service)
        controller = TuiController(
            service=service,
            runtime=runtime,  # type: ignore[arg-type]
            timeout=123.0,
            project_id="existing-project",
        )
        controller.submit("Create a small indicator board")

        self.assertEqual(controller.submit("/quit"), "continue")
        self.assertTrue(runtime.cancelled)
        self.assertIn("Stop requested", controller.notice)

    def test_quit_command_is_never_treated_as_a_new_project_request(self) -> None:
        service = FakeService()
        controller = TuiController(service=service)
        controller.begin_new()

        self.assertEqual(controller.submit("/quit"), "quit")
        self.assertFalse(any(call[0] == "create_draft" for call in service.calls))

    def test_new_confirm_and_project_commands_route_through_controller(self) -> None:
        service = FakeService()
        controller = TuiController(service=service, timeout=123.0)
        controller.submit("/new")
        self.assertEqual(controller.mode, "new_name")
        controller.submit("Low-voltage indicator")
        self.assertEqual(controller.view["project"]["status"], "draft")

        controller.view = _view("terminal-board", status="awaiting_confirmation")
        controller.submit("/confirm")
        self.assertEqual(controller.view["project"]["status"], "generated")
        controller.submit("/projects")
        self.assertEqual(controller.mode, "project_picker")
        controller.select_picker()
        self.assertEqual(controller.mode, "message")

    def test_confirm_resolves_the_exact_pending_tool_instead_of_guessing_from_status(
        self,
    ) -> None:
        service = FakeService()
        runtime = FakeRuntime(service)
        controller = TuiController(
            service=service,
            runtime=runtime,  # type: ignore[arg-type]
            project_id="existing-project",
        )
        controller.pending_approval = {
            "turn_id": "turn-1",
            "checkpoint_id": "approval-1",
            "tool_call_id": "call-1",
            "tool_name": "pcb_generate_candidate",
            "effect": "authoritative_write",
            "risk": "high",
            "args_hash": "0" * 64,
            "baseline_revision": 3,
        }

        controller.submit("/confirm")

        self.assertEqual(runtime.approval_decisions, [True])
        self.assertEqual(runtime.approval_checkpoints[0]["checkpoint_id"], "approval-1")
        self.assertTrue(runtime.active)
        self.assertIsNone(controller.pending_approval)
        self.assertNotIn("confirm_project", [call[0] for call in service.calls])

    def test_slash_suggestions_show_all_commands_and_filter(self) -> None:
        self.assertEqual(
            [command.name for command in command_suggestions("/")],
            [
                "/help",
                "/new",
                "/project",
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

    def test_project_command_reports_and_switches_the_persistent_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config" / "repository.json"
            original = root / "original-repository"
            replacement = root / "replacement-repository"
            with patch.dict(
                os.environ,
                {"PCBDRAFT_REPOSITORY_CONFIG": str(config_path)},
                clear=False,
            ):
                configure_repository(original)
                service = ApplicationService(provider_name="auto")
                controller = TuiController(service=service)

                controller.submit("/project")
                self.assertIn(str(original.resolve()), controller.notice)

                controller.submit(f"/project {replacement}")
                self.assertEqual(service.root, replacement.resolve())
                self.assertEqual(controller.project_id, None)
                self.assertIn(str(replacement.resolve()), controller.notice)

                draft = service.create_draft("Repository-selected board")
                project_id = draft["project"]["id"]
                self.assertTrue(
                    service.project_root(project_id).is_relative_to(replacement)
                )
                self.assertFalse((original / "projects" / project_id).exists())

    def test_model_command_reports_and_switches_only_the_live_provider(self) -> None:
        service = FakeService()
        controller = TuiController(service=service)
        controller.submit("/model")
        self.assertIn("fake-planner", controller.notice)
        self.assertIn("test-model", controller.notice)

        with patch(
            "pcbdraft.interfaces.tui.controller.resolve_provider",
            return_value=SwitchedProvider(),
        ) as resolve:
            controller.submit("/model auto")
        resolve.assert_called_once_with("auto")
        self.assertIsInstance(service.provider, SwitchedProvider)

        controller.submit("/model arbitrary-model-id")
        self.assertIn("auto, or openai-compatible", controller.error)
        self.assertIn("supported providers", controller.error.casefold())

    def test_projection_exposes_pcb_facts_without_a_fixed_workflow_model(self) -> None:
        view = _view("terminal-board", status="awaiting_confirmation")
        projection = project_projection(view)
        self.assertEqual(projection.board_size, "60 × 40 mm")
        self.assertEqual(projection.layer_label, "2 copper")
        self.assertEqual(projection.component_count, 4)
        self.assertEqual(projection.net_count, 3)
        self.assertFalse(hasattr(projection, "stages"))

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
                "auto",
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

        with patch(
            "pcbdraft.interfaces.cli.run_tui_command", return_value=0
        ) as run_tui:
            self.assertEqual(
                cli_main(
                    [
                        "--workspace",
                        "/tmp/pcbdraft-workspace",
                        "--provider",
                        "auto",
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
            provider="auto",
            project_id="existing-project",
            timeout=12.0,
            approval_mode="workspace",
        )

    def test_mcp_inherits_explicit_root_scope_and_local_options_win(self) -> None:
        inherited = [
            "--workspace",
            "/tmp/pcbdraft-mcp-root",
            "--provider",
            "auto",
            "--approval-mode",
            "read_only",
            "--timeout",
            "13",
            "mcp",
            "--project",
            "board",
        ]
        with patch("pcbdraft.interfaces.mcp.run_mcp_stdio", return_value=0) as run_mcp:
            self.assertEqual(cli_main(inherited), 0)
        run_mcp.assert_called_once_with(
            workspace="/tmp/pcbdraft-mcp-root",
            provider="auto",
            project_id="board",
            approval_mode="read_only",
            timeout=13.0,
        )

        overridden = [
            "--workspace=/tmp/ignored-root",
            "--provider",
            "auto",
            "--approval-mode",
            "read_only",
            "--timeout",
            "13",
            "mcp",
            "--project",
            "board",
            "--workspace",
            "/tmp/selected-local",
            "--provider",
            "auto",
            "--approval-mode",
            "review",
            "--timeout",
            "21",
        ]
        with patch("pcbdraft.interfaces.mcp.run_mcp_stdio", return_value=0) as run_mcp:
            self.assertEqual(cli_main(overridden), 0)
        run_mcp.assert_called_once_with(
            workspace="/tmp/selected-local",
            provider="auto",
            project_id="board",
            approval_mode="review",
            timeout=21.0,
        )

    def test_chat_without_an_explicit_action_does_not_open_a_repl(self) -> None:
        with (
            patch("pcbdraft.interfaces.chat.ApplicationService") as service,
            self.assertRaisesRegex(
                ValidationError, "requires --new, --project, or --list"
            ),
        ):
            run_chat_command(
                workspace=None,
                provider="auto",
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
            patch("pcbdraft.interfaces.tui.controller.sys.stdin", io.StringIO()),
            patch("pcbdraft.interfaces.tui.controller.sys.stdout", io.StringIO()),
            self.assertRaisesRegex(ValidationError, "interactive terminal"),
        ):
            run_tui_command(
                workspace=None,
                provider="auto",
                project_id=None,
                timeout=10.0,
            )

    def test_tui_launch_opens_project_picker_only_without_explicit_project(
        self,
    ) -> None:
        class InteractiveIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}),
            patch("pcbdraft.interfaces.tui.controller.sys.stdin", InteractiveIO()),
            patch("pcbdraft.interfaces.tui.controller.sys.stdout", InteractiveIO()),
            patch(
                "pcbdraft.interfaces.tui.controller.ApplicationService"
            ) as application_service,
            patch("pcbdraft.interfaces.tui.controller.AgentRuntime") as agent_runtime,
            patch(
                "pcbdraft.interfaces.tui.controller.TuiSessionStore"
            ) as session_store,
            patch("pcbdraft.interfaces.tui.controller.TuiController") as controller,
            patch("pcbdraft.interfaces.tui.app.PCBDraftApp") as textual_app,
        ):
            textual_app.return_value.run.return_value = 0

            self.assertEqual(
                run_tui_command(
                    workspace="/tmp/pcbdraft-picker",
                    provider="auto",
                    project_id=None,
                    timeout=10.0,
                ),
                0,
            )
            self.assertEqual(
                run_tui_command(
                    workspace="/tmp/pcbdraft-picker",
                    provider="auto",
                    project_id="existing-project",
                    timeout=10.0,
                ),
                0,
            )

        self.assertEqual(application_service.call_count, 2)
        self.assertEqual(agent_runtime.call_count, 2)
        self.assertEqual(session_store.call_count, 2)
        self.assertEqual(controller.call_count, 2)
        self.assertIs(
            controller.call_args_list[0].kwargs["startup_project_picker"], True
        )
        self.assertIs(
            controller.call_args_list[1].kwargs["startup_project_picker"], False
        )
        self.assertIsNone(controller.call_args_list[0].kwargs["project_id"])
        self.assertEqual(
            controller.call_args_list[1].kwargs["project_id"], "existing-project"
        )
        self.assertEqual(textual_app.call_count, 2)
        self.assertEqual(agent_runtime.return_value.shutdown.call_count, 2)


class PCBDraftTextualTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_frame_has_agent_workspace_and_responsive_pcb_rail(
        self,
    ) -> None:
        controller = TuiController(service=FakeService())
        app = PCBDraftApp(controller)
        async with app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            self.assertEqual(app.theme, "pcbdraft-dark")
            screenshot = html.unescape(app.export_screenshot()).replace("\xa0", " ")
            self.assertIn("PCBDraft", screenshot)
            self.assertIn("Describe a board to begin", screenshot)
            self.assertNotIn("project-rail", screenshot)

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
            self.assertEqual(controller.mode, "new_name")
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
        app = PCBDraftApp(
            TuiController(service=service, timeout=12.0, project_id="existing-project")
        )
        async with app.run_test(size=(120, 38)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "做一块小型温度传感器板"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.controller.project_id, "existing-project")
            self.assertEqual(
                app.controller.view["project"]["status"], "awaiting_confirmation"
            )
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

    async def test_new_project_and_empty_projects_are_visible_dialogs(self) -> None:
        service = FakeService()
        service.views.clear()
        app = PCBDraftApp(TuiController(service=service, timeout=12.0))
        async with app.run_test(size=(120, 38)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "/projects"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ProjectPickerScreen)
            self.assertIn(
                "No projects in this repository",
                html.unescape(app.export_screenshot()).replace("\xa0", " "),
            )
            await pilot.press("escape")
            await pilot.pause()

            composer.value = "/new Sensor board"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, NewProjectScreen)
            self.assertEqual(
                app.screen.query_one("#new-project-name", Input).value,
                "Sensor board",
            )
            new_project_frame = html.unescape(app.export_screenshot()).replace(
                "\xa0", " "
            )
            self.assertIn("Create project folder", new_project_frame)
            self.assertIn("No board work starts yet", new_project_frame)
            self.assertNotIn("Board request", new_project_frame)
            await pilot.press("down", "right", "enter")
            await pilot.pause()
            self.assertEqual(app.controller.mode, "message")
            self.assertIsNone(app.controller.project_id)

            composer.value = "/new Sensor board"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, NewProjectScreen)
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertEqual(app.controller.project_id, "terminal-board")
            self.assertEqual(app.controller.mode, "message")
            self.assertEqual(app.controller.view["project"]["status"], "draft")

    async def test_command_startup_opens_project_chooser_with_new_project_option(
        self,
    ) -> None:
        service = FakeService()
        app = PCBDraftApp(TuiController(service=service, startup_project_picker=True))
        async with app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            self.assertIsInstance(app.screen, ProjectPickerScreen)
            picker = app.screen.query_one("#projects-list", OptionList)
            card = app.screen.query_one("#project-picker-card")
            self.assertEqual(picker.option_count, 2)
            self.assertTrue(picker.has_focus)
            self.assertEqual(picker.highlighted, 0)
            initial_prompt = picker.get_option_at_index(0).prompt
            self.assertIsInstance(initial_prompt, Text)
            self.assertTrue(initial_prompt.plain.startswith("›"))
            self.assertEqual(picker.get_option_at_index(1).id, "__new__")
            self.assertLess(card.region.height, 30)
            chooser_frame = html.unescape(app.export_screenshot()).replace("\xa0", " ")
            self.assertIn("Choose a project", chooser_frame)
            self.assertIn("New project", chooser_frame)

            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(picker.highlighted, 1)
            new_prompt = picker.get_option_at_index(1).prompt
            self.assertIsInstance(new_prompt, Text)
            self.assertTrue(new_prompt.plain.startswith("›"))

            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(picker.highlighted, 0)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.controller.project_id, "existing-project")
            self.assertEqual(app.controller.mode, "message")
            self.assertIn(("open_project", "existing-project"), service.calls)

    async def test_project_picker_filter_focus_moves_exactly_one_option(self) -> None:
        service = FakeService()
        service.views["second-project"] = _view("second-project", status="validated")
        service.views["second-project"]["project"]["name"] = "Amplifier board"
        app = PCBDraftApp(TuiController(service=service, startup_project_picker=True))

        async with app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            picker = app.screen.query_one("#projects-list", OptionList)
            project_filter = app.screen.query_one("#project-filter", Input)
            self.assertEqual(picker.option_count, 3)
            self.assertEqual(picker.highlighted, 0)

            await pilot.click("#project-filter")
            await pilot.pause()
            self.assertTrue(project_filter.has_focus)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(picker.highlighted, 1)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(picker.highlighted, 0)

    async def test_typing_on_project_list_focuses_and_filters(self) -> None:
        service = FakeService()
        service.views["second-project"] = _view("second-project", status="validated")
        service.views["second-project"]["project"]["name"] = "Amplifier board"
        app = PCBDraftApp(TuiController(service=service, startup_project_picker=True))

        async with app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            picker = app.screen.query_one("#projects-list", OptionList)
            project_filter = app.screen.query_one("#project-filter", Input)
            self.assertTrue(picker.has_focus)

            await pilot.press("t", "e", "r", "m")
            await pilot.pause()
            self.assertTrue(project_filter.has_focus)
            self.assertEqual(project_filter.value, "term")
            self.assertEqual(picker.option_count, 2)
            self.assertEqual(picker.get_option_at_index(0).id, "existing-project")
            self.assertEqual(picker.get_option_at_index(1).id, "__new__")

    async def test_no_project_match_requires_navigation_before_new_project(
        self,
    ) -> None:
        app = PCBDraftApp(
            TuiController(service=FakeService(), startup_project_picker=True)
        )

        async with app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            await pilot.press("z", "z", "z")
            await pilot.pause()
            picker = app.screen.query_one("#projects-list", OptionList)
            self.assertEqual(picker.option_count, 2)
            self.assertEqual(picker.get_option_at_index(0).id, "__empty__")
            self.assertTrue(picker.get_option_at_index(0).disabled)
            self.assertEqual(picker.get_option_at_index(1).id, "__new__")
            self.assertIsNone(picker.highlighted)

            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ProjectPickerScreen)
            self.assertEqual(app.controller.mode, "project_picker")
            self.assertIsNone(app.controller.project_id)

            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(picker.highlighted, 1)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, NewProjectScreen)
            self.assertEqual(app.controller.mode, "new_name")

    async def test_slash_quit_and_ctrl_c_exit_the_application(self) -> None:
        slash_app = PCBDraftApp(TuiController(service=FakeService()))
        async with slash_app.run_test(size=(120, 38)) as pilot:
            composer = slash_app.query_one("#composer-input", Input)
            composer.value = "/quit"
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(slash_app._exit)

        keyboard_app = PCBDraftApp(TuiController(service=FakeService()))
        async with keyboard_app.run_test(size=(120, 38)) as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(keyboard_app._exit)

    async def test_busy_header_uses_a_live_spinner_without_redrawing_input(
        self,
    ) -> None:
        service = FakeService()
        controller = TuiController(
            service=service,
            runtime=FakeRuntime(service, polls_before_terminal=10),  # type: ignore[arg-type]
            project_id="existing-project",
        )
        app = PCBDraftApp(controller)
        async with app.run_test(size=(120, 38)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "Design a compact sensor"
            await pilot.press("enter")
            first_frame = app._spinner_index
            await pilot.pause(delay=0.3)
            self.assertTrue(controller.is_busy)
            self.assertNotEqual(app._spinner_index, first_frame)
            self.assertTrue(composer.has_focus)

    async def test_rejected_submission_keeps_the_draft_and_ctrl_c_stops_work(
        self,
    ) -> None:
        service = FakeService()
        runtime = FakeRuntime(service, polls_before_terminal=100)
        controller = TuiController(
            service=service,
            runtime=runtime,  # type: ignore[arg-type]
            project_id="existing-project",
        )
        app = PCBDraftApp(controller)
        async with app.run_test(size=(120, 38)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "First request"
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(controller.is_busy)

            composer.value = "Keep this follow-up draft"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(composer.value, "Keep this follow-up draft")
            self.assertIn("still running", controller.error)

            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(runtime.cancelled)
            self.assertFalse(app._exit)
            self.assertEqual(composer.value, "Keep this follow-up draft")

    async def test_ctrl_c_clears_a_draft_before_it_exits(self) -> None:
        app = PCBDraftApp(TuiController(service=FakeService()))
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "unsent draft"
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertEqual(composer.value, "")
            self.assertFalse(app._exit)
            self.assertIn("Input cleared", app.controller.notice)

            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app._exit)

    async def test_input_history_restores_the_current_draft(self) -> None:
        app = PCBDraftApp(
            TuiController(service=FakeService(), project_id="existing-project")
        )
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer-input", Input)
            composer.value = "Remember this request"
            await pilot.press("enter")
            await pilot.pause()
            composer.value = "current draft"

            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(composer.value, "Remember this request")
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(composer.value, "current draft")

    async def test_no_board_context_rail_is_rendered(self) -> None:
        wide = PCBDraftApp(
            TuiController(service=FakeService(), project_id="existing-project")
        )
        async with wide.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            self.assertFalse(wide.query("ProjectRail"))
            frame = html.unescape(wide.export_screenshot()).replace("\xa0", " ")
            self.assertIn("Terminal board", frame)
            self.assertNotIn("Board context", frame)

    async def test_custom_provider_form_stays_open_and_shows_validation_errors(
        self,
    ) -> None:
        app = PCBDraftApp(TuiController(service=FakeService()))
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(ProviderConnectScreen("custom"))
            await pilot.pause()
            self.assertIsInstance(app.screen, ProviderConnectScreen)
            self.assertTrue(app.screen.query_one("#provider-id", Input).has_focus)

            app.screen.query_one("#provider-id", Input).value = "invalid id"
            app.screen.query_one("#provider-name", Input).value = "Local provider"
            app.screen.query_one(
                "#provider-base-url", Input
            ).value = "https://example.com/v1"
            app.screen.query_one("#provider-model", Input).value = "test-model"
            app.screen.query_one("#provider-api-key", Input).value = "sk-test"
            await pilot.press("ctrl+s")
            await pilot.pause()

            self.assertIsInstance(app.screen, ProviderConnectScreen)
            frame = html.unescape(app.export_screenshot()).replace("\xa0", " ")
            self.assertIn("provider id must use", frame)
            save = app.screen.query_one("#save-provider")
            self.assertLessEqual(save.region.bottom, app.size.height)

    async def test_ctrl_c_does_not_exit_from_a_modal(self) -> None:
        app = PCBDraftApp(TuiController(service=FakeService()))
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(NewProjectScreen("keep me"))
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertFalse(app._exit)
            self.assertIsInstance(app.screen, NewProjectScreen)

    async def test_new_project_actions_fit_a_small_terminal(self) -> None:
        app = PCBDraftApp(TuiController(service=FakeService()))
        async with app.run_test(size=(60, 18)) as pilot:
            app.push_screen(
                NewProjectScreen("Sensor", repository_root="/var/lib/pcbdraft")
            )
            await pilot.pause()

            card = app.screen.query_one(".new-project-card")
            create = app.screen.query_one("#create-project")
            cancel = app.screen.query_one("#cancel-project")
            self.assertTrue(app.screen.has_class("compact-modal"))
            self.assertLessEqual(card.region.bottom, app.size.height)
            self.assertLessEqual(create.region.bottom, app.size.height)
            self.assertLessEqual(cancel.region.bottom, app.size.height)
            frame = html.unescape(app.export_screenshot()).replace("\xa0", " ")
            self.assertIn("Create project", frame)
            self.assertIn("Cancel", frame)

    async def test_startup_picker_search_and_empty_repository_welcome(self) -> None:
        service = FakeService()
        app = PCBDraftApp(TuiController(service=service, startup_project_picker=True))
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            self.assertIsInstance(app.screen, ProjectPickerScreen)
            search = app.screen.query_one("#project-filter", Input)
            search.value = "missing"
            await pilot.pause()
            picker = app.screen.query_one("#projects-list", OptionList)
            self.assertEqual(picker.option_count, 2)
            self.assertEqual(picker.get_option_at_index(0).id, "__empty__")
            self.assertEqual(picker.get_option_at_index(1).id, "__new__")
            self.assertGreaterEqual(picker.region.height, 4)

        service.views.clear()
        empty = PCBDraftApp(TuiController(service=service, startup_project_picker=True))
        async with empty.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertNotIsInstance(empty.screen, ProjectPickerScreen)
            self.assertEqual(empty.controller.mode, "message")
            self.assertTrue(empty.query_one("#composer-input", Input).has_focus)


if __name__ == "__main__":
    unittest.main()
