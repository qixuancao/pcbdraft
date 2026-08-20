"""Local dependency inspection."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any

from pcbdraft import build_identity
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.process import printable_first_line, run_command
from pcbdraft.kicad.runtime import (
    ensure_kicad_library_tables,
    find_kicad_cli,
    find_pcbnew_python,
    kicad_data_directory,
    kicad_user_config_directory,
    library_table_status,
)
from pcbdraft.kicad.support import evaluate_kicad_version
from pcbdraft.services.provider_connection import connection_status

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
    if name == "kicad-cli":
        executable = find_kicad_cli()
    elif name == "pcbnew-python":
        executable = find_pcbnew_python()
    else:
        executable = shutil.which(name)
    return probe_executable(executable, version_args)


def doctor_report() -> dict[str, Any]:
    tools = {
        "kicad-cli": _check("kicad-cli", ["--version"]),
        "pcbnew-python": _check(
            "pcbnew-python",
            ["-I", "-c", "import pcbnew; print(pcbnew.GetBuildVersion())"],
        ),
        "git": _check("git", ["--version"]),
    }
    kicad = tools["kicad-cli"]
    support = evaluate_kicad_version(kicad.get("version") or "")
    kicad["support"] = support.to_dict()
    core_ok = (
        tools["kicad-cli"]["available"]
        and tools["pcbnew-python"]["available"]
        and support.supported
    )
    try:
        status = connection_status()
        model = status.to_dict()
    except PCBDraftError as exc:
        model = {"configured": False, "error": str(exc)}
    paths = {
        "kicad_config": str(kicad_user_config_directory()),
        "symbols": str(kicad_data_directory("symbols")),
        "footprints": str(kicad_data_directory("footprints")),
        "template": str(kicad_data_directory("template")),
    }
    library_data = {
        name: {"available": Path(value).is_dir(), "path": value}
        for name, value in paths.items()
        if name != "kicad_config"
    }
    library_tables = library_table_status()
    runtime_ready = (
        core_ok
        and all(item["available"] for item in library_data.values())
        and all(item["configured"] for item in library_tables.values())
    )
    return {
        "ok": runtime_ready,
        "core_ok": core_ok,
        "runtime": build_identity(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "model_available": bool(model.get("configured")),
        "tools": tools,
        "paths": paths,
        "library_data": library_data,
        "library_tables": library_tables,
        "requirements": {
            "kicad-cli": "required",
            "pcbnew-python": "required for board generation",
            "git": "optional after installation",
            "model": "required for circuit planning",
        },
        "model": model,
    }


def setup_runtime() -> dict[str, Any]:
    """Prepare non-destructive KiCad user files and return a fresh diagnosis."""

    before = doctor_report()
    kicad = before["tools"]["kicad-cli"]
    support = kicad.get("support", {})
    if not kicad.get("available"):
        raise PCBDraftError("KiCad 10.0.x was not found; run the platform installer")
    if not support.get("supported"):
        raise PCBDraftError(
            "installed KiCad is outside the supported stable 10.0.x series"
        )
    if not before["tools"]["pcbnew-python"].get("available"):
        raise PCBDraftError(
            "KiCad's bundled pcbnew Python runtime is unavailable; "
            "reinstall the complete KiCad package"
        )
    missing_data = [
        name for name, item in before["library_data"].items() if not item["available"]
    ]
    if missing_data:
        raise PCBDraftError(
            "KiCad stock-library data is unavailable: " + ", ".join(missing_data)
        )
    ensure_kicad_library_tables()
    report = doctor_report()
    report["setup_ok"] = (
        report["core_ok"]
        and all(item["configured"] for item in report["library_tables"].values())
        and all(item["available"] for item in report["library_data"].values())
    )
    if not report["setup_ok"]:
        raise PCBDraftError(
            "KiCad runtime preparation is incomplete; run `pcbdraft doctor --json`"
        )
    report["ok"] = report["setup_ok"]
    return report
