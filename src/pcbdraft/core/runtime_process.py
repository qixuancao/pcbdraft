"""Process liveness and termination helpers used by terminal runtimes."""

from __future__ import annotations

import os
import signal
import subprocess


def terminate_pid(pid: int, force: bool = False) -> None:
    """Terminate a PID, using tree termination on Windows.

    POSIX sends SIGTERM by default or SIGKILL when forced. Windows uses
    taskkill /T, adding /F when forced. Failures propagate to the caller;
    callers remain responsible for waiting and deciding when to escalate.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if os.name != "nt":
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return

    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"Timed out terminating PID {pid}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise OSError(f"taskkill failed for PID {pid}: {detail}")


def _pid_exists(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
