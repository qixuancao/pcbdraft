"""Local dependency inspection."""

from __future__ import annotations

import shutil
from typing import Any

from .errors import PcbAgentError
from .process import printable_first_line, run_command

VERSION_TIMEOUT = 10.0
VERSION_OUTPUT_LIMIT = 128 * 1024


def probe_executable(executable: str | None, version_args: list[str]) -> dict[str, Any]:
    """Return a bounded, printable version probe suitable for a receipt."""
    if not executable:
        return {
            "available": False,
            "path": None,
            "version": None,
            "exit_code": None,
            "argv": None,
        }
    argv = [executable, *version_args]
    try:
        result = run_command(
            argv,
            cwd=None,
            timeout=VERSION_TIMEOUT,
            max_output_bytes=VERSION_OUTPUT_LIMIT,
        )
    except PcbAgentError:
        return {
            "available": False,
            "path": executable,
            "version": None,
            "exit_code": None,
            "timed_out": False,
            "output_limited": False,
            "argv": argv,
        }
    combined = result.stdout if result.stdout.strip() else result.stderr
    return {
        "available": result.returncode == 0
        and not result.timed_out
        and not result.output_limited,
        "path": executable,
        "version": printable_first_line(combined),
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
        "output_limited": result.output_limited,
        "argv": argv,
    }


def _check(name: str, version_args: list[str]) -> dict[str, Any]:
    return probe_executable(shutil.which(name), version_args)


def doctor_report() -> dict[str, Any]:
    tools = {
        "codex": _check("codex", ["--version"]),
        "kicad-cli": _check("kicad-cli", ["--version"]),
        "git": _check("git", ["--version"]),
    }
    return {"ok": all(value["available"] for value in tools.values()), "tools": tools}
