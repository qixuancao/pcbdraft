from __future__ import annotations

import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from pcbdraft.core.runtime_process import terminate_pid


class TerminatePidCompatTests(unittest.TestCase):
    def test_posix_default_and_forced_signals(self) -> None:
        with patch("pcbdraft.core.runtime_process.os") as runtime_os:
            runtime_os.name = "posix"
            terminate_pid(123)
            runtime_os.kill.assert_called_once_with(123, signal.SIGTERM)
            runtime_os.kill.reset_mock()
            # SIGKILL does not exist on Windows; supply it for this POSIX test.
            with patch("pcbdraft.core.runtime_process.signal.SIGKILL", 9, create=True):
                terminate_pid(123, force=True)
            runtime_os.kill.assert_called_once_with(123, 9)

    def test_posix_errors_reach_caller(self) -> None:
        with patch("pcbdraft.core.runtime_process.os") as runtime_os:
            runtime_os.name = "posix"
            for error in (ProcessLookupError(), PermissionError(), OSError()):
                with self.subTest(error=type(error).__name__):
                    runtime_os.kill.side_effect = error
                    with self.assertRaises(type(error)) as caught:
                        terminate_pid(123)
                    self.assertIs(caught.exception, error)

    def test_invalid_pid_never_signals_or_launches_taskkill(self) -> None:
        with (
            patch("pcbdraft.core.runtime_process.os") as runtime_os,
            patch("pcbdraft.core.runtime_process.subprocess.run") as run,
        ):
            for pid in (0, -1, True, "123", None):
                with self.subTest(pid=pid), self.assertRaises(ValueError):
                    terminate_pid(pid)
            runtime_os.kill.assert_not_called()
            run.assert_not_called()

    def test_windows_terminates_tree_and_only_forces_when_requested(self) -> None:
        with (
            patch("pcbdraft.core.runtime_process.os") as runtime_os,
            patch("pcbdraft.core.runtime_process.subprocess.run") as run,
        ):
            runtime_os.name = "nt"
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            for force in (False, True):
                with self.subTest(force=force):
                    terminate_pid(123, force=force)
                    command = run.call_args.args[0]
                    self.assertEqual(
                        command,
                        ["taskkill", "/PID", "123", "/T"] + (["/F"] if force else []),
                    )
                    self.assertFalse(run.call_args.kwargs.get("shell", False))
                    self.assertLessEqual(run.call_args.kwargs["timeout"], 10)
            runtime_os.kill.assert_not_called()

    def test_windows_nonzero_exit_is_not_reported_as_success(self) -> None:
        with (
            patch("pcbdraft.core.runtime_process.os") as runtime_os,
            patch("pcbdraft.core.runtime_process.subprocess.run") as run,
        ):
            runtime_os.name = "nt"
            for stdout, stderr in (("", "Access denied"), ("Not found", "")):
                with self.subTest(stderr=stderr):
                    run.return_value = subprocess.CompletedProcess(
                        [], 1, stdout, stderr
                    )
                    with self.assertRaisesRegex(OSError, stderr or stdout):
                        terminate_pid(123, force=True)
            runtime_os.kill.assert_not_called()

    def test_windows_launch_failure_and_timeout_reach_caller_as_oserror(self) -> None:
        with (
            patch("pcbdraft.core.runtime_process.os") as runtime_os,
            patch("pcbdraft.core.runtime_process.subprocess.run") as run,
        ):
            runtime_os.name = "nt"
            for error in (
                FileNotFoundError(),
                subprocess.TimeoutExpired("taskkill", 10),
            ):
                with self.subTest(error=type(error).__name__):
                    run.side_effect = error
                    with self.assertRaises(OSError):
                        terminate_pid(123, force=True)
            runtime_os.kill.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX signal exit status")
    def test_posix_terminates_a_live_child(self) -> None:
        for force in (False, True):
            with (
                self.subTest(force=force),
                subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ) as child,
            ):
                try:
                    terminate_pid(child.pid, force=force)
                    self.assertEqual(
                        child.wait(timeout=2),
                        -(signal.SIGKILL if force else signal.SIGTERM),
                    )
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
