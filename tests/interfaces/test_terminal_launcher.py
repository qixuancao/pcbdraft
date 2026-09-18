from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from pcbdraft.interfaces.cli import build_parser, main


class TerminalAliasTests(unittest.TestCase):
    def test_parser_exposes_native_terminal_and_compatibility_alias(self) -> None:
        parser = build_parser(prog="pcbdraft")

        terminal = parser.parse_args(["terminal"])
        self.assertEqual(terminal.command, "terminal")

        legacy = parser.parse_args(["legacy-terminal"])
        self.assertEqual(legacy.command, "legacy-terminal")

    def test_bare_cli_dispatches_the_python_terminal(self) -> None:
        with patch("pcbdraft.interfaces.cli.launch_cli", return_value=17) as launch:
            result = main([])

        self.assertEqual(result, 17)
        launch.assert_called_once_with([], permission_mode="workspace")

    def test_terminal_alias_dispatches_the_same_python_terminal(self) -> None:
        with patch("pcbdraft.interfaces.cli.launch_cli", return_value=19) as launch:
            result = main(["terminal"])

        self.assertEqual(result, 19)
        launch.assert_called_once_with([], permission_mode="workspace")

    def test_legacy_terminal_remains_a_compatibility_alias(self) -> None:
        with patch("pcbdraft.interfaces.cli.launch_cli", return_value=23) as launch:
            result = main(["--approval-mode", "review", "legacy-terminal"])

        self.assertEqual(result, 23)
        launch.assert_called_once_with([], permission_mode="review")

    def test_terminal_alias_forwards_an_explicit_provider(self) -> None:
        with patch("pcbdraft.interfaces.cli.launch_cli", return_value=0) as launch:
            result = main(["--provider", "native", "terminal"])

        self.assertEqual(result, 0)
        launch.assert_called_once_with(
            ["--provider", "native"],
            permission_mode="workspace",
        )

    def test_terminal_alias_applies_workspace_before_launch(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("pcbdraft.interfaces.cli.launch_cli", return_value=0),
        ):
            os.environ.pop("PCBDRAFT_HOME", None)
            result = main(["--workspace", "/tmp/terminal-home", "terminal"])
            self.assertEqual(result, 0)
            self.assertEqual(os.environ["PCBDRAFT_HOME"], "/tmp/terminal-home")

    def test_terminal_alias_has_no_gui_flags(self) -> None:
        parser = build_parser(prog="pcbdraft")
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(["terminal", "--no-start-gui"])

    def test_timeout_rejection_is_consistent_across_python_entrypoints(self) -> None:
        for tokens in (
            ["--timeout", "30"],
            ["--timeout", "30", "terminal"],
            ["--timeout", "30", "legacy-terminal"],
        ):
            with self.subTest(tokens=tokens):
                stderr = io.StringIO()
                with (
                    patch("pcbdraft.interfaces.cli.launch_cli") as launch,
                    redirect_stderr(stderr),
                ):
                    result = main(tokens)
                self.assertEqual(result, 2)
                self.assertIn("previous launcher ignored this option", stderr.getvalue())
                launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
