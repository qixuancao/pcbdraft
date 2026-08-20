from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pcbdraft.agent.hermes_tools import (
    _set_service,
    get_current_project_id,
    set_current_project_id,
)
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.hermes_paths import (
    hermes_home,
    hermes_vendor_dir,
    install_vendor_path,
)
from pcbdraft.core.repository import configure_repository
from pcbdraft.interfaces.commands import (
    HANDLERS,
    KEPT_HERMES_COMMANDS,
    PCBDRAFT_CATEGORY,
    apply_command_surface,
)
from pcbdraft.interfaces.hermes_cli import (
    _apply_connection_lifecycle_patch,
    _apply_model_persistence_patch,
    _apply_process_command_patch,
    _take_deferred_connection,
    launch_cli,
)
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.provider_connection import ConnectionOptions, ConnectionStatus


class FakeService:
    """Minimal stand-in for ApplicationService used by the slash handlers."""

    def __init__(self, root: Path) -> None:
        self.projects_root = root / "projects"
        self.views: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []

    def create_draft(self, name: str) -> dict[str, Any]:
        self.calls.append(("create_draft", name))
        project_id = f"{name}-abcd1234"
        self.views[project_id] = {
            "project": {"id": project_id, "name": name, "status": "draft"},
            "state": {
                "revision": 0,
                "design_revision": 0,
                "last_validation": None,
                "last_preview": None,
                "last_release": None,
            },
            "artifacts": {},
        }
        return {"project": {"id": project_id, "name": name, "status": "draft"}}

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            {
                "id": project_id,
                "name": value["project"]["name"],
                "status": value["project"]["status"],
            }
            for project_id, value in sorted(self.views.items())
        ]

    def open_project(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("open_project", project_id))
        try:
            return self.views[project_id]
        except KeyError as exc:
            raise ValidationError(f"project not found: {project_id}") from exc

    def set_repository(self, directory: str | Path) -> Any:
        self.calls.append(("set_repository", str(directory)))
        self.projects_root = Path(directory) / "projects"
        return SimpleNamespace(
            root=Path(directory), projects_root=Path(directory) / "projects"
        )

    def events(self, project_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        self.calls.append(("events", project_id))
        return []

    def confirm_project(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(("confirm_project", project_id, timeout))
        view = self.views[project_id]
        view["project"]["status"] = "generated"
        return view

    def discard_modification(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("discard_modification", project_id))
        return self.views[project_id]

    def validate_project(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(("validate_project", project_id, timeout))
        view = self.views[project_id]
        view["artifacts"] = {"validation": {"candidate_ready": True}}
        return view

    def build_release(self, project_id: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append(("build_release", project_id, timeout))
        return self.views[project_id]


class SlashHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.service = FakeService(self.root / "repo")
        _set_service(self.service)
        set_current_project_id(None)

    def tearDown(self) -> None:
        _set_service(None)
        set_current_project_id(None)
        self._temporary.cleanup()

    def test_new_creates_named_project_and_sets_current_id(self) -> None:
        result = HANDLERS["new"]("sensor board")
        self.assertIn("sensor board", result)
        self.assertIn("sensor board-abcd1234", result)
        self.assertIn("next step", result)
        self.assertEqual(get_current_project_id(), "sensor board-abcd1234")
        self.assertEqual(self.service.calls[0], ("create_draft", "sensor board"))

    def test_bare_new_shows_usage_and_creates_nothing(self) -> None:
        result = HANDLERS["new"]("  ")
        self.assertIn("Usage: /new <name>", result)
        self.assertEqual(self.service.calls, [])
        self.assertIsNone(get_current_project_id())

    def test_projects_empty_state_is_actionable(self) -> None:
        result = HANDLERS["projects"]("")
        self.assertIn("No projects yet", result)
        self.assertIn("/new <name>", result)

    def test_projects_lists_created_projects(self) -> None:
        HANDLERS["new"]("alpha board")
        result = HANDLERS["projects"]("")
        self.assertIn("alpha board-abcd1234", result)
        self.assertIn("[draft]", result)

    def test_project_shows_current_repository_without_argument(self) -> None:
        with patch.dict(
            os.environ,
            {"PCBDRAFT_REPOSITORY_CONFIG": str(self.root / "pointer.json")},
            clear=False,
        ):
            configure_repository(self.root / "chosen-repo")
            result = HANDLERS["project"]("")
        self.assertIn("chosen-repo", result)
        self.assertIn("configured", result)

    def test_project_switch_rebinds_roots_and_clears_current_id(self) -> None:
        HANDLERS["new"]("board one")
        self.assertEqual(get_current_project_id(), "board one-abcd1234")
        target = self.root / "second-repo"
        result = HANDLERS["project"](str(target))
        self.assertIn("switched", result)
        self.assertEqual(self.service.projects_root, target / "projects")
        self.assertIsNone(get_current_project_id())
        self.assertEqual(self.service.calls[-1][0], "set_repository")

    def test_open_invalid_id_raises_validation_error(self) -> None:
        with self.assertRaisesRegex(ValidationError, "project not found"):
            HANDLERS["open"]("missing-project")

    def test_open_valid_project_sets_current_id(self) -> None:
        HANDLERS["new"]("board one")
        result = HANDLERS["open"]("board one-abcd1234")
        self.assertIn("Opened", result)
        self.assertEqual(get_current_project_id(), "board one-abcd1234")

    def test_workflow_commands_require_a_project_context(self) -> None:
        with self.assertRaisesRegex(PCBDraftError, "no project selected"):
            HANDLERS["review"]("")

    def test_review_and_logs_summarize_the_current_project(self) -> None:
        HANDLERS["new"]("board one")
        review = HANDLERS["review"]("")
        self.assertIn("board one-abcd1234", review)
        self.assertIn("status: draft", review)
        logs = HANDLERS["logs"]("")
        self.assertIn("No events recorded", logs)

    def test_validate_reports_candidate_readiness(self) -> None:
        HANDLERS["new"]("board one")
        result = HANDLERS["validate"]("")
        self.assertIn("candidate_ready=True", result)

    def test_confirm_and_discard_and_release_use_the_service(self) -> None:
        HANDLERS["new"]("board one")
        confirmed = HANDLERS["confirm"]("")
        self.assertIn("generated", confirmed)
        HANDLERS["discard"]("")
        released = HANDLERS["release"]("")
        self.assertIn("Release built", released)
        self.assertEqual(
            [call[0] for call in self.service.calls],
            [
                "create_draft",
                "confirm_project",
                "discard_modification",
                "build_release",
            ],
        )

    def test_connect_without_provider_is_actionable(self) -> None:
        with patch(
            "pcbdraft.interfaces.commands.connect",
            return_value=ConnectionStatus(
                False, False, None, None, None, None, outcome="cancelled"
            ),
        ):
            result = HANDLERS["connect"]("")
        self.assertIn("No model provider connected", result)

    def test_successful_connect_refreshes_cached_planning_provider(self) -> None:
        connected = ConnectionStatus(
            True,
            True,
            "zai",
            "glm-test",
            "api_key",
            "hermes-config",
            outcome="changed",
        )
        with (
            patch("pcbdraft.interfaces.commands.connect", return_value=connected),
            patch(
                "pcbdraft.interfaces.commands.refresh_service_provider"
            ) as refresh_provider,
        ):
            result = HANDLERS["connect"]("")
        refresh_provider.assert_called_once_with()
        self.assertIn("zai / glm-test", result)

    def test_process_command_wrapper_routes_and_renders_errors(self) -> None:
        install_vendor_path()
        _apply_process_command_patch()
        import cli as hermes_cli_module

        with patch.object(hermes_cli_module, "_cprint") as mocked_print:
            continue_loop = hermes_cli_module.HermesCLI.process_command(
                object(), "/new wrapper board"
            )
        self.assertTrue(continue_loop)
        rendered = "\n".join(str(call.args[0]) for call in mocked_print.call_args_list)
        self.assertIn("wrapper board", rendered)
        self.assertEqual(get_current_project_id(), "wrapper board-abcd1234")

        with patch.object(hermes_cli_module, "_cprint") as mocked_print:
            continue_loop = hermes_cli_module.HermesCLI.process_command(
                object(), "/open missing-project"
            )
        self.assertTrue(continue_loop)
        rendered = "\n".join(str(call.args[0]) for call in mocked_print.call_args_list)
        self.assertIn("✗", rendered)
        self.assertIn("project not found", rendered)

    def test_repl_connect_defers_wizard_until_after_terminal_exit(self) -> None:
        install_vendor_path()
        _apply_process_command_patch()
        import cli as hermes_cli_module

        with (
            patch("pcbdraft.interfaces.hermes_cli.connect") as wizard,
            patch.object(hermes_cli_module, "_cprint") as mocked_print,
        ):
            continue_loop = hermes_cli_module.HermesCLI.process_command(
                object(), "/connect --no-browser --reauthenticate"
            )
        self.assertFalse(continue_loop)
        wizard.assert_not_called()
        requested = _take_deferred_connection()
        self.assertIsNotNone(requested)
        assert requested is not None
        self.assertTrue(requested.no_browser)
        self.assertTrue(requested.reauthenticate)
        rendered = "\n".join(str(call.args[0]) for call in mocked_print.call_args_list)
        self.assertIn("Leaving the terminal", rendered)

    def test_bare_model_defers_full_wizard_outside_process_command(self) -> None:
        install_vendor_path()
        _apply_process_command_patch()
        import cli as hermes_cli_module

        with patch("pcbdraft.interfaces.hermes_cli.connect") as wizard:
            continue_loop = hermes_cli_module.HermesCLI.process_command(
                object(), "/model --refresh"
            )
        self.assertFalse(continue_loop)
        wizard.assert_not_called()
        requested = _take_deferred_connection()
        self.assertIsNotNone(requested)
        assert requested is not None
        self.assertTrue(requested.refresh)

    def test_deferred_exit_restores_terminal_without_global_cleanup(self) -> None:
        install_vendor_path()
        import cli as hermes_cli_module

        _apply_connection_lifecycle_patch()
        before_cleanup_done = hermes_cli_module._cleanup_done
        with patch.object(
            hermes_cli_module, "_reset_terminal_input_modes_on_exit"
        ) as reset_modes:
            from pcbdraft.interfaces.hermes_cli import _defer_connection

            _defer_connection(ConnectionOptions(refresh=True))
            hermes_cli_module._run_cleanup()
        reset_modes.assert_called_once_with()
        self.assertEqual(hermes_cli_module._cleanup_done, before_cleanup_done)
        self.assertEqual(_take_deferred_connection(), ConnectionOptions(refresh=True))

    def test_deferred_wizard_runs_on_main_thread_and_relaunches(self) -> None:
        install_vendor_path()
        import hermes_cli.main as hermes_main

        connected = ConnectionStatus(
            True,
            True,
            "openai-codex",
            "gpt-5-codex",
            "oauth_device_code",
            "provider-auth",
            outcome="changed",
        )
        launches = 0

        def run_terminal() -> None:
            nonlocal launches
            launches += 1
            if launches == 1:
                import cli as hermes_cli_module

                self.assertFalse(
                    hermes_cli_module.HermesCLI.process_command(
                        object(), "/connect --refresh"
                    )
                )

        def run_wizard(_options: ConnectionOptions) -> ConnectionStatus:
            self.assertIs(threading.current_thread(), threading.main_thread())
            return connected

        with (
            patch("pcbdraft.interfaces.hermes_cli.activate"),
            patch(
                "pcbdraft.interfaces.hermes_cli.connection_status",
                return_value=connected,
            ),
            patch.object(hermes_main, "main", side_effect=run_terminal) as terminal,
            patch(
                "pcbdraft.interfaces.hermes_cli.connect", side_effect=run_wizard
            ) as wizard,
            patch("pcbdraft.agent.hermes_tools.refresh_service_provider") as refresh,
        ):
            self.assertEqual(launch_cli([]), 0)
        self.assertEqual(terminal.call_count, 2)
        wizard.assert_called_once_with(ConnectionOptions(refresh=True))
        refresh.assert_called_once_with()

    def test_deferred_cancellation_relaunches_without_refreshing(self) -> None:
        install_vendor_path()
        import hermes_cli.main as hermes_main

        connected = ConnectionStatus(
            True, True, "zai", "glm-test", "api_key", "hermes-config"
        )
        cancelled = ConnectionStatus(
            True,
            True,
            "zai",
            "glm-test",
            "api_key",
            "hermes-config",
            outcome="cancelled",
            state="cancelled",
        )
        launches = 0

        def run_terminal() -> None:
            nonlocal launches
            launches += 1
            if launches == 1:
                import cli as hermes_cli_module

                self.assertFalse(
                    hermes_cli_module.HermesCLI.process_command(object(), "/connect")
                )

        with (
            patch("pcbdraft.interfaces.hermes_cli.activate"),
            patch(
                "pcbdraft.interfaces.hermes_cli.connection_status",
                return_value=connected,
            ),
            patch.object(hermes_main, "main", side_effect=run_terminal) as terminal,
            patch("pcbdraft.interfaces.hermes_cli.connect", return_value=cancelled),
            patch("pcbdraft.agent.hermes_tools.refresh_service_provider") as refresh,
        ):
            self.assertEqual(launch_cli([]), 0)
        self.assertEqual(terminal.call_count, 2)
        refresh.assert_not_called()

    def test_deferred_wizard_error_is_reported_before_relaunch(self) -> None:
        install_vendor_path()
        import hermes_cli.main as hermes_main

        connected = ConnectionStatus(
            True, True, "zai", "glm-test", "api_key", "hermes-config"
        )
        launches = 0

        def run_terminal() -> None:
            nonlocal launches
            launches += 1
            if launches == 1:
                import cli as hermes_cli_module

                self.assertFalse(
                    hermes_cli_module.HermesCLI.process_command(object(), "/connect")
                )

        errors = io.StringIO()
        with (
            patch("pcbdraft.interfaces.hermes_cli.activate"),
            patch(
                "pcbdraft.interfaces.hermes_cli.connection_status",
                return_value=connected,
            ),
            patch.object(hermes_main, "main", side_effect=run_terminal) as terminal,
            patch(
                "pcbdraft.interfaces.hermes_cli.connect",
                side_effect=PCBDraftError("safe connection failure"),
            ),
            patch("sys.stderr", errors),
        ):
            self.assertEqual(launch_cli([]), 0)
        self.assertEqual(terminal.call_count, 2)
        self.assertIn("safe connection failure", errors.getvalue())

    def test_normal_exit_clears_stale_request_without_relaunching(self) -> None:
        install_vendor_path()
        import hermes_cli.main as hermes_main

        from pcbdraft.interfaces.hermes_cli import _defer_connection

        connected = ConnectionStatus(
            True, True, "zai", "glm-test", "api_key", "hermes-config"
        )
        _defer_connection(ConnectionOptions(refresh=True))
        with (
            patch("pcbdraft.interfaces.hermes_cli.activate"),
            patch(
                "pcbdraft.interfaces.hermes_cli.connection_status",
                return_value=connected,
            ),
            patch.object(hermes_main, "main") as terminal,
            patch("pcbdraft.interfaces.hermes_cli.connect") as wizard,
        ):
            self.assertEqual(launch_cli([]), 0)
        terminal.assert_called_once_with()
        wizard.assert_not_called()
        self.assertIsNone(_take_deferred_connection())

    def test_explicit_model_forms_always_use_the_persistent_authority(self) -> None:
        install_vendor_path()
        _apply_model_persistence_patch()
        import cli as hermes_cli_module
        from hermes_cli import inventory, model_switch

        context = SimpleNamespace(user_providers={}, custom_providers=[])
        context.with_overrides = lambda **_kwargs: context
        failed_result = SimpleNamespace(success=False, error_message="test stop")
        target = SimpleNamespace(
            provider="zai",
            model="glm-4.5",
            base_url="",
            api_key="",
        )
        with (
            patch.object(inventory, "load_picker_context", return_value=context),
            patch.object(
                model_switch, "switch_model", return_value=failed_result
            ) as switch,
            patch.object(hermes_cli_module, "_cprint"),
        ):
            hermes_cli_module.HermesCLI._handle_model_switch(
                target, "/model board-target"
            )
            self.assertTrue(switch.call_args.kwargs["is_global"])
            hermes_cli_module.HermesCLI._handle_model_switch(
                target, "/model board-target --provider anthropic"
            )
            self.assertTrue(switch.call_args.kwargs["is_global"])
            self.assertEqual(switch.call_args.kwargs["explicit_provider"], "anthropic")

    def test_ephemeral_model_flags_are_rejected_before_switching(self) -> None:
        install_vendor_path()
        _apply_model_persistence_patch()
        import cli as hermes_cli_module
        from hermes_cli import model_switch

        with (
            patch.object(model_switch, "switch_model") as switch,
            patch.object(hermes_cli_module, "_cprint") as rendered,
        ):
            hermes_cli_module.HermesCLI._handle_model_switch(
                object(), "/model board-target --session"
            )
        switch.assert_not_called()
        self.assertIn("one persistent model", str(rendered.call_args.args[0]))


class CommandSurfaceTests(unittest.TestCase):
    def test_registry_contains_only_the_pcbdraft_surface(self) -> None:
        install_vendor_path()
        from hermes_cli import commands

        apply_command_surface()
        names = [command.name for command in commands.COMMAND_REGISTRY]
        self.assertEqual(
            set(names),
            KEPT_HERMES_COMMANDS | set(HANDLERS),
        )
        self.assertIsNone(commands.resolve_command("kanban"))
        self.assertIsNone(commands.resolve_command("billing"))
        self.assertIsNone(commands.resolve_command("skills"))
        resolved = commands.resolve_command("new")
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.category, PCBDRAFT_CATEGORY)
        self.assertIn("/new", commands.COMMANDS)
        self.assertNotIn("/kanban", commands.COMMANDS)
        self.assertIn(PCBDRAFT_CATEGORY, commands.COMMANDS_BY_CATEGORY)
        self.assertIn("/projects", commands.COMMANDS_BY_CATEGORY[PCBDRAFT_CATEGORY])
        self.assertIn("new", commands.GATEWAY_KNOWN_COMMANDS)

    def test_apply_command_surface_is_idempotent(self) -> None:
        install_vendor_path()
        from hermes_cli import commands

        apply_command_surface()
        first_names = [command.name for command in commands.COMMAND_REGISTRY]
        first_lookup = dict(commands._COMMAND_LOOKUP)
        apply_command_surface()
        self.assertEqual(
            [command.name for command in commands.COMMAND_REGISTRY], first_names
        )
        self.assertEqual(dict(commands._COMMAND_LOOKUP), first_lookup)


class HermesVendorDirTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ, {"PCBDRAFT_HERMES_DIR": str(temporary)}, clear=False
            ),
        ):
            self.assertEqual(hermes_vendor_dir(), Path(temporary).resolve())

    def test_installed_wheel_data_path_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "lib" / "pcbdraft" / "data" / "vendor" / "hermes"
            marker.mkdir(parents=True)
            (marker / "cli.py").write_text("", encoding="utf-8")
            fake_module = root / "lib" / "pcbdraft" / "core" / "hermes_paths.py"
            fake_module.parent.mkdir(parents=True)
            fake_module.write_text("", encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("pcbdraft.core.hermes_paths.__file__", str(fake_module)),
            ):
                self.assertEqual(hermes_vendor_dir(), marker.resolve())

    def test_checkout_path_is_found_next_to_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "repo" / "vendor" / "hermes"
            checkout.mkdir(parents=True)
            (checkout / "cli.py").write_text("", encoding="utf-8")
            fake_module = (
                root / "repo" / "src" / "pcbdraft" / "core" / "hermes_paths.py"
            )
            fake_module.parent.mkdir(parents=True)
            fake_module.write_text("", encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("pcbdraft.core.hermes_paths.__file__", str(fake_module)),
            ):
                self.assertEqual(hermes_vendor_dir(), checkout.resolve())

    def test_missing_runtime_raises_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_module = root / "elsewhere" / "pcbdraft" / "core" / "hermes_paths.py"
            fake_module.parent.mkdir(parents=True)
            fake_module.write_text("", encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("pcbdraft.core.hermes_paths.__file__", str(fake_module)),
                self.assertRaisesRegex(RuntimeError, "PCBDRAFT_HERMES_DIR"),
            ):
                hermes_vendor_dir()

    def test_checkout_runtime_is_resolvable_in_this_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            vendor = hermes_vendor_dir()
        self.assertTrue((vendor / "cli.py").is_file())


class RepositoryInvariantTests(unittest.TestCase):
    def test_projects_stay_under_the_repository_and_hermes_home_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository_path = root / "pbd-repo"
            environment = {
                "PCBDRAFT_REPOSITORY_CONFIG": str(root / "pointer.json"),
                "PCBDRAFT_HERMES_HOME": str(root / "hermes-home"),
            }
            with patch.dict(os.environ, environment, clear=False):
                configure_repository(repository_path)
                service = ApplicationService(provider_name="auto")
                view = service.create_draft("invariant board")
                project_id = str(view["project"]["id"])
                project_dir = service.projects_root / project_id
                self.assertTrue(project_dir.is_dir())
                self.assertTrue(
                    str(project_dir).startswith(str(repository_path.resolve()))
                )
                home = hermes_home()
                self.assertEqual(home, root / "hermes-home")
                self.assertFalse(str(home).startswith(str(repository_path.resolve())))
                self.assertIsNone(get_current_project_id())


if __name__ == "__main__":
    unittest.main()
