"""Local dependency inspection."""

from __future__ import annotations

import shutil
from typing import Any

from pcbdraft import build_identity
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.process import printable_first_line, run_command
from pcbdraft.kicad.support import evaluate_kicad_version
from pcbdraft.model.config import load_model_config

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
    except PCBDraftError:
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
        "kicad-cli": _check("kicad-cli", ["--version"]),
        "git": _check("git", ["--version"]),
    }
    kicad = tools["kicad-cli"]
    support = evaluate_kicad_version(kicad.get("version") or "")
    kicad["support"] = support.to_dict()
    core_ok = (
        tools["kicad-cli"]["available"]
        and tools["git"]["available"]
        and support.supported
    )
    try:
        config = load_model_config()
        model = {
            "configured": config.active is not None,
            "provider": config.active.name if config.active else None,
            "model": config.active_model,
            "config": str(config.path),
        }
    except PCBDraftError as exc:
        model = {"configured": False, "error": str(exc)}
    return {
        "ok": core_ok,
        "core_ok": core_ok,
        "runtime": build_identity(),
        "model_available": bool(model.get("configured")),
        "tools": tools,
        "model": model,
    }
