"""Bounded subprocess execution without a shell."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import PcbAgentError, ValidationError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool = False
    output_limited: bool = False


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not argv:
        raise ValidationError("empty subprocess argv")
    normalized: list[str] = []
    for value in argv:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValidationError("invalid subprocess argv element")
        normalized.append(value)
    return tuple(normalized)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    timeout: float,
    max_output_bytes: int,
    stdin_data: bytes | None = None,
) -> CommandResult:
    """Run an argv-only command while bounding time and combined stdout/stderr."""
    normalized = _validate_argv(argv)
    if timeout <= 0:
        raise ValidationError("subprocess timeout must be positive")
    if max_output_bytes <= 0:
        raise ValidationError("subprocess output limit must be positive")

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(normalized),
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise PcbAgentError(f"failed to start executable: {Path(normalized[0]).name}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    stdin_view = memoryview(stdin_data or b"")
    stdin_offset = 0
    if process.stdin is not None:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")

    stdout = bytearray()
    stderr = bytearray()
    timed_out = False
    output_limited = False
    killed = False
    killed_at: float | None = None
    deadline = started + timeout

    try:
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if not killed and now >= deadline:
                timed_out = True
                killed = True
                killed_at = now
                _kill_process_group(process)

            # Do not let an escaped descendant keep inherited pipe descriptors
            # open forever after the bounded child process has been killed.
            if killed_at is not None and now - killed_at >= 1.0:
                for key in list(selector.get_map().values()):
                    stream = key.fileobj
                    selector.unregister(stream)
                    stream.close()
                break

            events = selector.select(timeout=0.05) if selector.get_map() else ()
            if not selector.get_map() and process.poll() is None:
                time.sleep(0.01)
            for key, _ in events:
                stream = key.fileobj
                if key.data == "stdin":
                    try:
                        if stdin_offset < len(stdin_view):
                            written = os.write(stream.fileno(), stdin_view[stdin_offset : stdin_offset + 65536])
                            stdin_offset += written
                        if stdin_offset >= len(stdin_view):
                            selector.unregister(stream)
                            stream.close()
                    except (BrokenPipeError, OSError):
                        try:
                            selector.unregister(stream)
                        except KeyError:
                            pass
                        stream.close()
                    continue

                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue

                remaining = max_output_bytes - len(stdout) - len(stderr)
                destination = stdout if key.data == "stdout" else stderr
                if remaining > 0:
                    destination.extend(chunk[:remaining])
                if len(chunk) > remaining and not killed:
                    output_limited = True
                    killed = True
                    killed_at = time.monotonic()
                    _kill_process_group(process)

            if process.poll() is not None:
                stdin_streams = [key.fileobj for key in selector.get_map().values() if key.data == "stdin"]
                for stream in stdin_streams:
                    selector.unregister(stream)
                    stream.close()
    finally:
        selector.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            _kill_process_group(process)
        returncode = process.wait()

    return CommandResult(
        argv=normalized,
        returncode=returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        duration_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        output_limited=output_limited,
    )


def redact_argv(argv: Sequence[str], replacements: Mapping[str, str]) -> list[str]:
    """Return complete argv with only explicitly known path prefixes replaced."""
    ordered = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)
    redacted: list[str] = []
    for argument in argv:
        value = argument
        for raw, marker in ordered:
            if raw:
                value = value.replace(raw, marker)
                escaped = json.dumps(raw, ensure_ascii=False)[1:-1]
                value = value.replace(escaped, marker)
        redacted.append(value)
    return redacted


def remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PcbAgentError("workflow timeout expired")
    return remaining


def printable_first_line(data: bytes, *, limit: int = 256) -> str:
    text = data.decode("utf-8", errors="replace").splitlines()[0] if data else ""
    return "".join(character for character in text[:limit] if character.isprintable()).strip()
