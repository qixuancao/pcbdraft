from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.interfaces import terminal_launcher as launcher
from pcbdraft.interfaces.cli import build_parser, main


class TerminalLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client = self.root / "terminal_client"
        (self.client / "src").mkdir(parents=True)
        (self.client / "package.json").write_text("{}", encoding="utf-8")
        (self.client / "src" / "main.ts").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _common_patches(self, state: launcher._GuiState):
        return (
            patch.object(
                launcher, "_terminal_client_directory", return_value=self.client
            ),
            patch.object(launcher.shutil, "which", return_value="/tools/bun"),
            patch.object(launcher, "_probe_gui", return_value=state),
        )

    def test_finds_packaged_terminal_client_only_with_required_files(self) -> None:
        with patch.object(launcher.resources, "files", return_value=self.root) as files:
            self.assertEqual(launcher._terminal_client_directory(), self.client)
        files.assert_called_once_with("pcbdraft")
        (self.client / "src" / "main.ts").unlink()
        with self.assertRaisesRegex(PCBDraftError, "complete wheel or sdist"):
            launcher._terminal_client_directory(self.root)

    def test_reuses_healthy_gui_and_passes_explicit_url_to_bun(self) -> None:
        client_patch, bun_patch, probe_patch = self._common_patches(
            launcher._GuiState.HEALTHY
        )
        completed = subprocess.CompletedProcess([], 7)
        with (
            client_patch,
            bun_patch,
            probe_patch,
            patch.object(launcher, "_start_gui") as start_gui,
            patch.object(launcher, "_stop_gui") as stop_gui,
            patch.object(launcher.subprocess, "run", return_value=completed) as run,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            result = launcher.launch_terminal(port=9141)

        self.assertEqual(result, 7)
        start_gui.assert_not_called()
        stop_gui.assert_not_called()
        self.assertIn("reusing existing GUI", stdout.getvalue())
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/tools/bun", "run", "dev"])
        self.assertEqual(kwargs["cwd"], self.client)
        self.assertEqual(kwargs["env"]["PCBDRAFT_GUI_URL"], "http://127.0.0.1:9141")
        self.assertNotIn("PCBDRAFT_INITIAL_PROJECT_ID", kwargs["env"])
        self.assertFalse(kwargs["check"])

    def test_passes_initial_project_to_the_terminal_environment(self) -> None:
        client_patch, bun_patch, probe_patch = self._common_patches(
            launcher._GuiState.HEALTHY
        )
        completed = subprocess.CompletedProcess([], 0)
        with (
            client_patch,
            bun_patch,
            probe_patch,
            patch.object(launcher.subprocess, "run", return_value=completed) as run,
            redirect_stdout(io.StringIO()),
        ):
            launcher.launch_terminal(
                port=9147,
                initial_project_id="board-abc123",
            )

        self.assertEqual(
            run.call_args.kwargs["env"]["PCBDRAFT_INITIAL_PROJECT_ID"],
            "board-abc123",
        )

    def test_flushes_api_status_before_starting_bun(self) -> None:
        client_patch, bun_patch, probe_patch = self._common_patches(
            launcher._GuiState.HEALTHY
        )
        calls = Mock()
        print_status = Mock()
        run = Mock(return_value=subprocess.CompletedProcess([], 0))
        calls.attach_mock(print_status, "print_status")
        calls.attach_mock(run, "run")

        with (
            client_patch,
            bun_patch,
            probe_patch,
            patch("builtins.print", print_status),
            patch.object(launcher.subprocess, "run", run),
        ):
            launcher.launch_terminal(port=9146)

        self.assertEqual(
            calls.mock_calls[0],
            call.print_status(
                "PCBDraft Terminal API: http://127.0.0.1:9146 (reusing existing GUI)",
                flush=True,
            ),
        )
        self.assertEqual(calls.mock_calls[1][0], "run")

    def test_starts_waits_for_and_cleans_up_owned_gui(self) -> None:
        client_patch, bun_patch, probe_patch = self._common_patches(
            launcher._GuiState.FREE
        )
        process = Mock()
        completed = subprocess.CompletedProcess([], 0)
        with (
            client_patch,
            bun_patch,
            probe_patch,
            patch.object(launcher, "_start_gui", return_value=process) as start_gui,
            patch.object(launcher, "_wait_for_gui") as wait_for_gui,
            patch.object(launcher, "_stop_gui") as stop_gui,
            patch.object(launcher.subprocess, "run", return_value=completed),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            result = launcher.launch_terminal(port=9142)

        self.assertEqual(result, 0)
        start_gui.assert_called_once_with(9142)
        wait_for_gui.assert_called_once_with(process, "http://127.0.0.1:9142")
        stop_gui.assert_called_once_with(process)
        self.assertIn("started for this session", stdout.getvalue())

    def test_startup_failure_still_cleans_up_owned_gui(self) -> None:
        client_patch, bun_patch, probe_patch = self._common_patches(
            launcher._GuiState.FREE
        )
        process = Mock()
        with (
            client_patch,
            bun_patch,
            probe_patch,
            patch.object(launcher, "_start_gui", return_value=process),
            patch.object(
                launcher,
                "_wait_for_gui",
                side_effect=PCBDraftError("not ready"),
            ),
            patch.object(launcher, "_stop_gui") as stop_gui,
            patch.object(launcher.subprocess, "run") as run,
            self.assertRaisesRegex(PCBDraftError, "not ready"),
        ):
            launcher.launch_terminal(port=9143)

        stop_gui.assert_called_once_with(process)
        run.assert_not_called()

    def test_rejects_occupied_non_pcbdraft_port(self) -> None:
        client_patch, bun_patch, probe_patch = self._common_patches(
            launcher._GuiState.OCCUPIED
        )
        with (
            client_patch,
            bun_patch,
            probe_patch,
            patch.object(launcher, "_start_gui") as start_gui,
            self.assertRaisesRegex(PCBDraftError, "not a healthy PCBDraft GUI"),
        ):
            launcher.launch_terminal(port=9144)
        start_gui.assert_not_called()

    def test_no_start_gui_requires_existing_healthy_service(self) -> None:
        client_patch, bun_patch, probe_patch = self._common_patches(
            launcher._GuiState.FREE
        )
        with (
            client_patch,
            bun_patch,
            probe_patch,
            patch.object(launcher, "_start_gui") as start_gui,
            self.assertRaisesRegex(PCBDraftError, "no healthy PCBDraft GUI"),
        ):
            launcher.launch_terminal(port=9145, no_start_gui=True)
        start_gui.assert_not_called()

    def test_missing_bun_is_actionable(self) -> None:
        with (
            patch.object(
                launcher, "_terminal_client_directory", return_value=self.client
            ),
            patch.object(launcher.shutil, "which", return_value=None),
            patch.object(launcher, "_probe_gui") as probe_gui,
            self.assertRaisesRegex(PCBDraftError, "Bun is required"),
        ):
            launcher.launch_terminal()
        probe_gui.assert_not_called()

    def test_probe_distinguishes_pcbdraft_from_other_listener(self) -> None:
        with (
            patch.object(launcher, "_is_pcbdraft_gui", return_value=True),
            patch.object(launcher, "_port_is_open") as port_is_open,
        ):
            self.assertIs(launcher._probe_gui(9130), launcher._GuiState.HEALTHY)
        port_is_open.assert_not_called()

        with (
            patch.object(launcher, "_is_pcbdraft_gui", return_value=False),
            patch.object(launcher, "_port_is_open", return_value=True),
        ):
            self.assertIs(launcher._probe_gui(9130), launcher._GuiState.OCCUPIED)

    def test_force_stops_gui_that_does_not_terminate(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("gui", 3), 0]

        launcher._stop_gui(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)


class TerminalCommandTests(unittest.TestCase):
    def test_parser_exposes_bounded_terminal_arguments(self) -> None:
        parser = build_parser(prog="pcbdraft")
        defaults = parser.parse_args(["terminal"])
        self.assertEqual(defaults.command, "terminal")
        self.assertEqual(defaults.port, 9130)
        self.assertFalse(defaults.no_start_gui)

        legacy = parser.parse_args(["legacy-terminal"])
        self.assertEqual(legacy.command, "legacy-terminal")

        selected = parser.parse_args(["terminal", "--port", "9148", "--no-start-gui"])
        self.assertEqual(selected.port, 9148)
        self.assertTrue(selected.no_start_gui)

        for invalid in ("0", "65536"):
            with (
                self.subTest(port=invalid),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(["terminal", "--port", invalid])

    def test_cli_lazily_dispatches_terminal_launcher(self) -> None:
        with patch(
            "pcbdraft.interfaces.terminal_launcher.launch_terminal", return_value=19
        ) as launch:
            result = main(["terminal", "--port", "9149", "--no-start-gui"])

        self.assertEqual(result, 19)
        launch.assert_called_once_with(
            port=9149,
            no_start_gui=True,
            initial_project_id=None,
        )

    def test_bare_cli_dispatches_typescript_terminal_with_initial_project(self) -> None:
        with patch(
            "pcbdraft.interfaces.terminal_launcher.launch_terminal", return_value=17
        ) as launch:
            result = main(["--project", "board-1"])

        self.assertEqual(result, 17)
        launch.assert_called_once_with(initial_project_id="board-1")

    def test_cli_forwards_workspace_through_gui_environment(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch(
                "pcbdraft.interfaces.terminal_launcher.launch_terminal",
                return_value=0,
            ),
        ):
            os.environ.pop("PCBDRAFT_HOME", None)
            result = main(["--workspace", "/tmp/terminal-home", "terminal"])
            self.assertEqual(os.environ["PCBDRAFT_HOME"], "/tmp/terminal-home")
        self.assertEqual(result, 0)

    def test_explicit_legacy_terminal_dispatches_python_compatibility(self) -> None:
        with patch("pcbdraft.interfaces.cli.launch_cli", return_value=23) as launch:
            result = main(["--approval-mode", "review", "legacy-terminal"])

        self.assertEqual(result, 23)
        launch.assert_called_once_with([], permission_mode="review")

    def test_legacy_terminal_forwards_an_explicit_provider(self) -> None:
        with patch("pcbdraft.interfaces.cli.launch_cli", return_value=0) as launch:
            result = main(["--provider", "native", "legacy-terminal"])

        self.assertEqual(result, 0)
        launch.assert_called_once_with(
            ["--provider", "native"],
            permission_mode="workspace",
        )

    def test_legacy_terminal_rejects_the_previously_ignored_timeout(self) -> None:
        stderr = io.StringIO()
        with (
            patch("pcbdraft.interfaces.cli.launch_cli") as launch,
            redirect_stderr(stderr),
        ):
            result = main(["--timeout", "30", "legacy-terminal"])

        self.assertEqual(result, 2)
        self.assertIn("previous launcher ignored this option", stderr.getvalue())
        launch.assert_not_called()

    def test_cli_rejects_unsupported_approval_mode(self) -> None:
        stderr = io.StringIO()
        with (
            patch("pcbdraft.interfaces.terminal_launcher.launch_terminal") as launch,
            redirect_stderr(stderr),
        ):
            result = main(["--approval-mode", "read_only", "terminal"])

        self.assertEqual(result, 2)
        self.assertIn("supports only --approval-mode workspace", stderr.getvalue())
        self.assertIn("legacy-terminal", stderr.getvalue())
        launch.assert_not_called()

    def test_default_terminal_rejects_legacy_provider_and_timeout_options(self) -> None:
        for arguments, message in (
            (["--provider", "native"], "does not accept --provider"),
            (["--timeout", "30"], "does not support the legacy --timeout"),
        ):
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with (
                    patch(
                        "pcbdraft.interfaces.terminal_launcher.launch_terminal"
                    ) as launch,
                    redirect_stderr(stderr),
                ):
                    result = main(arguments)
                self.assertEqual(result, 2)
                self.assertIn(message, stderr.getvalue())
                launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
