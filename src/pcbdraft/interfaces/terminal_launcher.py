"""Launch the TypeScript terminal against the authoritative local GUI API."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError

_GUI_HOST = "127.0.0.1"
_HEALTH_DOCUMENT = {
    "schema": "pcbdraft-gui-health",
    "version": 1,
    "status": "ok",
}
_STARTUP_TIMEOUT_SECONDS = 10.0


class _GuiState(Enum):
    FREE = "free"
    HEALTHY = "healthy"
    OCCUPIED = "occupied"


def _terminal_client_directory(source_root: Path | None = None) -> Path:
    """Return the checked-out TypeScript client, rejecting installed-only use."""

    root = (
        source_root.resolve()
        if source_root is not None
        else Path(__file__).resolve().parents[3]
    )
    candidate = root / "clients" / "terminal"
    required = (candidate / "package.json", candidate / "src" / "main.ts")
    if not candidate.is_dir() or not all(path.is_file() for path in required):
        raise PCBDraftError(
            "TypeScript terminal client was not found; run `pcbdraft terminal` "
            "from a PCBDraft source checkout containing clients/terminal"
        )
    return candidate


def _base_url(port: int) -> str:
    return f"http://{_GUI_HOST}:{port}"


def _is_pcbdraft_gui(base_url: str, *, timeout: float = 0.35) -> bool:
    """Recognize only the bounded PCBDraft health document on loopback."""

    request = urllib.request.Request(  # noqa: S310 - fixed loopback HTTP URL
        f"{base_url}/healthz",
        headers={"Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            if getattr(response, "status", None) != 200:
                return False
            payload = response.read(1025)
    except (OSError, urllib.error.URLError, ValueError):
        return False
    if len(payload) > 1024:
        return False
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return value == _HEALTH_DOCUMENT


def _port_is_open(port: int, *, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((_GUI_HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_gui(port: int) -> _GuiState:
    if _is_pcbdraft_gui(_base_url(port)):
        return _GuiState.HEALTHY
    if _port_is_open(port):
        return _GuiState.OCCUPIED
    return _GuiState.FREE


def _gui_popen_options() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        }
    return {"start_new_session": True}


def _start_gui(port: int, *, source_root: Path) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "pcbdraft",
        "gui",
        "--host",
        _GUI_HOST,
        "--port",
        str(port),
    ]
    try:
        return subprocess.Popen(  # noqa: S603 - fixed Python module and bounded args
            command,
            cwd=source_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **_gui_popen_options(),
        )
    except OSError as exc:
        raise PCBDraftError(f"failed to start the PCBDraft GUI service: {exc}") from exc


def _wait_for_gui(
    process: subprocess.Popen[bytes],
    base_url: str,
    *,
    timeout: float = _STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_pcbdraft_gui(base_url):
            return
        returncode = process.poll()
        if returncode is not None:
            raise PCBDraftError(
                "PCBDraft GUI service exited before becoming ready "
                f"(exit status {returncode})"
            )
        time.sleep(0.05)
    raise PCBDraftError(
        f"PCBDraft GUI service did not become ready at {base_url} "
        f"within {timeout:g} seconds"
    )


def _stop_gui(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3.0)
    except OSError:
        return


def launch_terminal(*, port: int = 9130, no_start_gui: bool = False) -> int:
    """Run the source TypeScript client and own only a GUI started here."""

    client_directory = _terminal_client_directory()
    source_root = client_directory.parents[1]
    bun = shutil.which("bun")
    if bun is None:
        raise PCBDraftError(
            "Bun is required for the TypeScript terminal; install Bun and "
            "ensure `bun` is on PATH"
        )

    base_url = _base_url(port)
    state = _probe_gui(port)
    owned_gui: subprocess.Popen[bytes] | None = None
    if state is _GuiState.OCCUPIED:
        raise PCBDraftError(
            f"port {port} is occupied by a service that is not a healthy PCBDraft GUI"
        )
    if state is _GuiState.FREE and no_start_gui:
        raise PCBDraftError(
            f"no healthy PCBDraft GUI is available at {base_url}; "
            "remove --no-start-gui or start `pcbdraft gui --port "
            f"{port}` first"
        )

    try:
        if state is _GuiState.FREE:
            owned_gui = _start_gui(port, source_root=source_root)
            _wait_for_gui(owned_gui, base_url)
            print(f"PCBDraft Terminal API: {base_url} (started for this session)")
        else:
            print(f"PCBDraft Terminal API: {base_url} (reusing existing GUI)")

        environment = os.environ.copy()
        environment["PCBDRAFT_GUI_URL"] = base_url
        try:
            completed = subprocess.run(  # noqa: S603 - resolved Bun executable
                [bun, "run", "dev"],
                cwd=client_directory,
                env=environment,
                check=False,
            )
        except OSError as exc:
            raise PCBDraftError(
                f"failed to start the TypeScript terminal: {exc}"
            ) from exc
        return int(completed.returncode)
    except KeyboardInterrupt:
        return 130
    finally:
        if owned_gui is not None:
            _stop_gui(owned_gui)


__all__ = ("launch_terminal",)
