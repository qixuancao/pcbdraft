#!/usr/bin/env python3
"""Drive the real Textual TUI through a PTY and verify durable project state."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import re
import shutil
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path
from typing import Any

from fake_openai_provider import E2E_API_KEY, start_fake_provider

REPO = Path(__file__).resolve().parent.parent
TERMINAL_JOB_STATES = {
    "completed",
    "completed_after_cancel",
    "failed",
    "cancelled",
    "interrupted",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", default=shutil.which("pcbdraft"))
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def initialize_clean_home(home: Path) -> None:
    target = home / ".config" / "kicad" / "10.0"
    target.mkdir(parents=True, mode=0o700)
    template = Path("/usr/share/kicad/template")
    for name in ("sym-lib-table", "fp-lib-table"):
        source = template / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"KiCad library-table template unavailable: {source}")
        shutil.copy2(source, target / name)


def write_model_config(home: Path, base_url: str) -> Path:
    """Give the E2E process the same private config path as a real user."""

    path = home / ".config" / "pcbdraft" / "config.toml"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(
        "\n".join(
            (
                "version = 1",
                'active_provider = "local-e2e"',
                'active_model = "pcbdraft-e2e-model"',
                "",
                "[providers.local-e2e]",
                'name = "Local E2E provider"',
                f'base_url = "{base_url}"',
                f'api_key = "{E2E_API_KEY}"',
                'models = ["pcbdraft-e2e-model"]',
                "",
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _project_state(workspace: Path) -> tuple[Path, dict[str, Any]] | None:
    projects = workspace / "projects"
    if not projects.is_dir():
        return None
    candidates = [
        path
        for path in projects.iterdir()
        if path.is_dir() and not path.is_symlink() and (path / "project.json").is_file()
    ]
    if len(candidates) != 1:
        return None
    return candidates[0], _read_json(candidates[0] / "project.json")


def _jobs_settled(project: Path) -> bool:
    jobs = list((project / "jobs").glob("*.json"))
    return bool(jobs) and all(
        _read_json(path).get("status") in TERMINAL_JOB_STATES for path in jobs
    )


def _drain(master: int, transcript: bytearray) -> None:
    while True:
        try:
            chunk = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise
        if not chunk:
            return
        transcript.extend(chunk)
        if len(transcript) > 8 * 1024 * 1024:
            del transcript[: len(transcript) - 8 * 1024 * 1024]


def _quit_tui(
    process: subprocess.Popen[bytes], master: int, transcript: bytearray
) -> None:
    """Quit even when the first request only asks an active turn to stop."""

    deadline = time.monotonic() + 15
    while process.poll() is None and time.monotonic() < deadline:
        try:
            os.write(master, b"/quit\r")
        except OSError:
            break
        wait_until = min(deadline, time.monotonic() + 0.75)
        while process.poll() is None and time.monotonic() < wait_until:
            _drain(master, transcript)
            time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)


def _wait_for_status(
    workspace: Path,
    master: int,
    transcript: bytearray,
    expected: str,
    *,
    timeout: float,
) -> tuple[Path, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _drain(master, transcript)
        current = _project_state(workspace)
        if current is not None:
            project, state = current
            last = state
            if state.get("status") == expected and _jobs_settled(project):
                time.sleep(0.5)
                _drain(master, transcript)
                return project, state
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for TUI project status {expected}: {last}")


def _plain_terminal(value: bytes) -> str:
    text = value.decode("utf-8", "replace")
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(
        character for character in text if character in "\n\t" or character >= " "
    )


def run_tui(executable: str, output: Path) -> dict[str, Any]:
    provider = start_fake_provider()
    transcript = bytearray()
    process: subprocess.Popen[bytes] | None = None
    master = -1
    slave = -1
    try:
        with tempfile.TemporaryDirectory(prefix="pcbdraft-tui-e2e-") as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(mode=0o700)
            initialize_clean_home(home)
            config_path = write_model_config(home, provider.base_url)
            workspace = root / "workspace"
            environment = dict(os.environ)
            environment.pop("NO_COLOR", None)
            environment.update(
                {
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "TERM": "xterm-256color",
                    "COLORTERM": "truecolor",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PCBDRAFT_CONFIG": str(config_path),
                }
            )
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 140, 0, 0))
            os.set_blocking(master, False)
            process = subprocess.Popen(  # noqa: S603 - caller supplies the test executable
                [
                    executable,
                    "--workspace",
                    str(workspace),
                    "--provider",
                    "openai-compatible",
                    "--timeout",
                    "180",
                ],
                cwd=REPO,
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave)
            slave = -1
            time.sleep(1)
            _drain(master, transcript)
            if process.poll() is not None:
                raise RuntimeError(
                    f"TUI exited before input with status {process.returncode}"
                )

            request = (
                "Design a small 3.3 V LED status indicator PCB with a power connector"
            )
            # A PTY needs carriage return for Textual's Enter key; line-feed is
            # inserted as text by the terminal input widget.
            os.write(master, request.encode() + b"\r")
            project, validated = _wait_for_status(
                workspace, master, transcript, "validated", timeout=180
            )
            validation = _read_json(project / validated["last_validation"]["report"])
            os.write(master, b"/logs on\r")
            time.sleep(0.5)
            _drain(master, transcript)
            os.write(master, b"/review\r")
            time.sleep(0.8)
            _drain(master, transcript)
            os.write(master, b"\x1b")
            time.sleep(0.2)
            _drain(master, transcript)
            os.write(master, b"/release\r")
            project, released = _wait_for_status(
                workspace, master, transcript, "released", timeout=180
            )
            time.sleep(0.5)
            _drain(master, transcript)
            _quit_tui(process, master, transcript)
            _drain(master, transcript)
            if process.returncode != 0:
                raise RuntimeError(f"TUI exited with status {process.returncode}")

            session_record = _read_json(workspace / "tui-session.json")
            if session_record.get("project_id") != released["id"]:
                raise RuntimeError("TUI did not persist its non-secret session pointer")

            os.close(master)
            master = -1
            resume_transcript = bytearray()
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 140, 0, 0))
            os.set_blocking(master, False)
            process = subprocess.Popen(  # noqa: S603 - caller supplies the test executable
                [
                    executable,
                    "--workspace",
                    str(workspace),
                    "--provider",
                    "openai-compatible",
                    "--timeout",
                    "180",
                ],
                cwd=REPO,
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave)
            slave = -1
            time.sleep(1.2)
            _drain(master, resume_transcript)
            _quit_tui(process, master, resume_transcript)
            _drain(master, resume_transcript)
            if process.returncode != 0:
                raise RuntimeError(
                    f"resumed TUI exited with status {process.returncode}"
                )

            conversation = _read_json(project / "conversation.json")
            # The store also keeps a monotonic sequence index in this directory;
            # only aggregate records use the stable turn-* filename namespace.
            turn_paths = sorted((project / "agent-turns").glob("turn-*.json"))
            turns = [_read_json(path) for path in turn_paths]
            managed_plan = project / "design" / "circuit-plan.json"
            managed_qualification = project / "design" / "component-qualification.json"
            release = released["last_release"]
            if not isinstance(release, dict):
                raise TypeError("TUI release evidence is absent")
            plain = _plain_terminal(bytes(transcript))
            resumed_plain = _plain_terminal(bytes(resume_transcript))
            required_activity = {
                "pcb_plan_request",
                "pcb_generate_candidate",
                "pcb_validate",
                "pcb_build_release",
            }
            missing_activity = sorted(
                tool for tool in required_activity if tool not in plain
            )
            if missing_activity:
                raise RuntimeError(
                    f"TUI did not render expected agent activity: {missing_activity}"
                )
            for expected_text in (
                "Engineering review",
                "Circuit plan",
                "Expanded activity details are visible",
            ):
                if expected_text not in plain:
                    raise RuntimeError(
                        f"TUI did not render review/log UX: {expected_text}"
                    )
            if "Resumed the last terminal project" not in resumed_plain:
                raise RuntimeError("TUI did not resume its last project on restart")
            if "Release built" not in resumed_plain:
                raise RuntimeError("resumed TUI did not render the released project")
            if len(turns) != 2 or any(
                turn.get("status") != "completed" for turn in turns
            ):
                raise RuntimeError(
                    "TUI did not retain its completed durable agent turns"
                )
            durable_tools = {
                tool.get("tool_name")
                for turn in turns
                for tool in turn.get("tool_runs", [])
                if isinstance(tool, dict)
            }
            missing_durable_tools = required_activity - durable_tools
            if missing_durable_tools:
                raise RuntimeError(
                    "durable turns omitted expected PCB tools: "
                    f"{sorted(missing_durable_tools)}"
                )
            if not managed_plan.is_file():
                raise RuntimeError(
                    "TUI-generated managed project omitted its circuit plan"
                )
            if not managed_qualification.is_file():
                raise RuntimeError(
                    "TUI-generated managed project omitted component qualification evidence"
                )
            qualification = _read_json(managed_qualification)
            if qualification.get("summary", {}).get("pad_mapping_failures") != 0:
                raise RuntimeError(
                    "TUI-generated project has an invalid footprint pad map"
                )
            if not validation["readiness"]["engineering_candidate"]:
                raise RuntimeError(
                    "TUI-generated project did not pass candidate checks"
                )
            if validation["readiness"]["production"]:
                raise RuntimeError("TUI made an unsupported production-readiness claim")

            output.mkdir(parents=True, exist_ok=True)
            (output / "tui-transcript.txt").write_text(plain, encoding="utf-8")
            (output / "tui-resume-transcript.txt").write_text(
                resumed_plain, encoding="utf-8"
            )
            summary = {
                "schema": "pcbdraft-tui-e2e",
                "version": 2,
                "project_id": released["id"],
                "status": released["status"],
                "provider": "local-openai-compatible",
                "provider_requests": provider.server.request_count,
                "agent_activity_tools": sorted(required_activity),
                "durable_turns": len(turns),
                "messages": len(conversation["messages"]),
                "candidate_ready": True,
                "production_ready": False,
                "circuit_plan_retained": True,
                "component_qualification_retained": True,
                "release_verified": bool(release["offline_verification"]["verified"]),
                "review_overlay": True,
                "expanded_logs": True,
                "session_resumed": True,
                "clean_exit": True,
            }
            (output / "tui-e2e.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return summary
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if slave >= 0:
            os.close(slave)
        if master >= 0:
            os.close(master)
        provider.close()


def main() -> int:
    arguments = parse_args()
    if not arguments.executable or not Path(arguments.executable).is_file():
        raise SystemExit("pcbdraft executable is required")
    if not shutil.which("kicad-cli"):
        raise SystemExit("real KiCad is required")
    output = (
        arguments.output or Path(tempfile.mkdtemp(prefix="pcbdraft-tui-evidence-"))
    ).resolve()
    summary = run_tui(arguments.executable, output)
    print(
        json.dumps({"report": str(output / "tui-e2e.json"), **summary}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
