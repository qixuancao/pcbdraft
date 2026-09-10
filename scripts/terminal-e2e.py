#!/usr/bin/env python3
"""Run a deterministic PCBDraft one-shot through flat PCB tools and real KiCad."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import yaml
from fake_openai_provider import E2E_API_KEY, start_fake_provider

from pcbdraft.core.io import atomic_write_text, load_json_limited, read_text_limited
from pcbdraft.core.project import sha256_file
from pcbdraft.core.redaction import sanitize_user_text

REPO = Path(__file__).resolve().parent.parent
REQUEST = (
    "Create a fresh 30 mm by 20 mm KiCad board, run ERC and DRC, retain the "
    "evidence, and state the limits of those checks."
)
EXPECTED_TOOLS = (
    "pcb_create_project",
    "pcb_add_block",
    "pcb_add_component",
    "pcb_add_component",
    "pcb_add_net",
    "pcb_connect_pin",
    "pcb_connect_pin",
    "pcb_connect_pin",
    "pcb_connect_pin",
    "pcb_set_board_outline",
    "pcb_place_footprint",
    "pcb_place_footprint",
    "pcb_route_net",
    "pcb_route_net",
    "pcb_add_via",
    "pcb_run_erc",
    "pcb_run_drc",
)
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_TRACE_BYTES = 32 * 1024 * 1024
MAX_PROBE_OUTPUT_BYTES = 64 * 1024
MAX_CHILD_OUTPUT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    output_limited: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter containing the pcbdraft installation to exercise",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument(
        "--require-installed",
        action="store_true",
        help="fail if the child imports pcbdraft from this source checkout",
    )
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


def write_model_config(runtime_home: Path, base_url: str) -> Path:
    """Write an isolated fake-provider config in the PCBDraft runtime home."""

    runtime_home.mkdir(parents=True, mode=0o700)
    path = runtime_home / "config.yaml"
    document = {
        "model": {
            "provider": "custom",
            "default": "pcbdraft-e2e-model",
            "base_url": base_url,
            "api_key": E2E_API_KEY,
            "api_mode": "chat_completions",
        }
    }
    atomic_write_text(
        path,
        yaml.safe_dump(document, allow_unicode=True, sort_keys=True),
    )
    return path


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError(f"JSON artifact must not be a symbolic link: {path}")
    value = load_json_limited(path, MAX_JSON_BYTES)
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _read_trace(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise RuntimeError("debug trace must not be a symbolic link")
    events: list[dict[str, Any]] = []
    for line in read_text_limited(path, MAX_TRACE_BYTES).splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError("debug trace event must be a JSON object")
        events.append(value)
    return events


def _prepare_output(value: Path | None) -> Path:
    if value is None:
        output = Path(tempfile.mkdtemp(prefix="pcbdraft-evidence-"))
        output.chmod(0o700)
        return output.resolve()
    output = value.expanduser().resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise RuntimeError(f"evidence output already exists: {output}")
    output.mkdir(parents=True, mode=0o700)
    return output


def _write_text(path: Path, value: str) -> None:
    atomic_write_text(path, sanitize_user_text(value))


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - release check currently runs on Linux
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
    max_output_bytes: int,
) -> _ProcessResult:
    """Run one child with a hard combined stdout/stderr and wall-time bound."""

    process = subprocess.Popen(  # noqa: S603 - explicit interpreter and argv
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=os.name != "nt",
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        ),
    )
    if process.stdout is None or process.stderr is None:
        _kill_process(process)
        raise RuntimeError("failed to capture PCBDraft child output")

    stdout = bytearray()
    stderr = bytearray()
    output_limited = threading.Event()
    buffer_lock = threading.Lock()

    def drain(stream: BinaryIO, destination: bytearray) -> None:
        while True:
            try:
                chunk = stream.read(65536)
            except OSError:
                return
            if not chunk:
                return
            with buffer_lock:
                remaining = max_output_bytes - len(stdout) - len(stderr)
                if remaining > 0:
                    destination.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_limited.set()
                    return

    readers = (
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    )
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None:
        if output_limited.is_set():
            _kill_process(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_process(process)
            break
        time.sleep(0.01)
    try:
        returncode = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_process(process)
        returncode = process.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=1)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    return _ProcessResult(
        returncode=returncode,
        stdout=bytes(stdout).decode("utf-8", "replace"),
        stderr=bytes(stderr).decode("utf-8", "replace"),
        timed_out=timed_out,
        output_limited=output_limited.is_set(),
    )


def _project_path(workspace: Path) -> Path:
    projects = workspace / "projects"
    candidates = (
        [
            path
            for path in projects.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and (path / "project.json").is_file()
        ]
        if projects.is_dir()
        else []
    )
    if len(candidates) != 1:
        raise RuntimeError(
            f"PCBDraft smoke must retain exactly one project, found {len(candidates)}"
        )
    return candidates[0]


def _copy_retained_evidence(
    *,
    workspace: Path,
    trace: Path,
    output: Path,
) -> Path | None:
    if trace.is_file() and not trace.is_symlink():
        shutil.copy2(trace, output / "agent-trace.jsonl")
    try:
        project = _project_path(workspace)
    except RuntimeError:
        return None
    shutil.copytree(project, output / "project", symlinks=False)
    return project


def _tool_result(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    if not isinstance(data, dict):
        raise TypeError("tool trace event has no data object")
    result = data.get("result")
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise TypeError("tool trace event has no JSON result")
    return result


def _check_receipts(project: Path) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    receipt_directories: dict[str, Path] = {}
    receipt_paths = sorted((project / "validation").glob("*/receipt.json"))
    if len(receipt_paths) != 2:
        raise RuntimeError(
            "retained KiCad evidence must contain exactly two check receipts"
        )
    for path in receipt_paths:
        receipt = _read_json(path)
        check = receipt.get("check")
        if not isinstance(check, str) or check in receipts:
            raise RuntimeError("retained KiCad evidence has a duplicate/invalid check")
        receipts[check] = receipt
        receipt_directories[check] = path.parent
    if set(receipts) != {"run_erc", "run_drc"}:
        raise RuntimeError(
            "retained KiCad evidence must contain exactly one ERC and one DRC receipt"
        )
    for check, receipt in receipts.items():
        if (
            receipt.get("status") != "complete"
            or receipt.get("state") != "completed"
            or receipt.get("outcome") != "pass"
        ):
            raise RuntimeError(f"retained {check} evidence did not pass: {receipt}")
        report_name = receipt.get("report")
        if not isinstance(report_name, str) or Path(report_name).name != report_name:
            raise RuntimeError(f"retained {check} receipt has an unsafe report path")
        report_path = receipt_directories[check] / report_name
        report = _read_json(report_path)
        if sha256_file(report_path, max_bytes=MAX_JSON_BYTES) != receipt.get(
            "report_sha256"
        ):
            raise RuntimeError(
                f"retained {check} report hash does not match its receipt"
            )
        if any(
            report.get(field) != receipt.get(field)
            for field in ("check", "design_content_hash", "state", "outcome")
        ):
            raise RuntimeError(f"retained {check} report disagrees with its receipt")
        tool_run = report.get("tool_run")
        expected_kind = "erc" if check == "run_erc" else "drc"
        if not isinstance(tool_run, dict) or tool_run != {
            "status": "completed",
            "failure": None,
            "report": f"{expected_kind}.json",
        }:
            raise RuntimeError(
                f"retained {check} report lacks completed KiCad evidence"
            )
        for name in (f"{expected_kind}.json", f"{expected_kind}.raw.json"):
            _read_json(receipt_directories[check] / name)
    return receipts


def _validate_run(
    *,
    completed: _ProcessResult,
    project: Path | None,
    trace: Path,
    provider_requests: int,
) -> dict[str, Any]:
    if completed.timed_out:
        raise RuntimeError("PCBDraft one-shot timed out")
    if completed.output_limited:
        raise RuntimeError("PCBDraft one-shot exceeded its output limit")
    if completed.returncode != 0:
        raise RuntimeError(
            f"PCBDraft one-shot exited with status {completed.returncode}"
        )
    if project is None:
        raise RuntimeError("PCBDraft one-shot retained no project")
    if "do not establish production readiness" not in completed.stdout:
        raise RuntimeError(
            "PCBDraft final reply omitted the production-readiness limit"
        )
    state = _read_json(project / "project.json")
    design = project / "design"
    native_files = {
        suffix: list(design.glob(f"*.{suffix}"))
        for suffix in ("kicad_pro", "kicad_sch", "kicad_pcb")
    }
    if any(len(paths) != 1 for paths in native_files.values()):
        raise RuntimeError("PCBDraft project omitted a matching native KiCad file set")
    stems = {paths[0].stem for paths in native_files.values()}
    if len(stems) != 1:
        raise RuntimeError("PCBDraft project retained mismatched KiCad file names")
    receipts = _check_receipts(project)
    last_validation = state.get("last_validation")
    if not isinstance(last_validation, dict):
        raise TypeError("project omitted its latest validation pointer")
    if last_validation.get("production_ready") is not False:
        raise RuntimeError("project made an unsupported production-readiness claim")

    events = _read_trace(trace)
    tool_events = [event for event in events if event.get("event") == "tool_end"]
    sequence = [
        str(event.get("data", {}).get("tool_name"))
        for event in tool_events
        if isinstance(event.get("data"), dict)
    ]
    if sequence != list(EXPECTED_TOOLS):
        raise RuntimeError(f"unexpected PCBDraft PCB tool sequence: {sequence}")
    for event in tool_events:
        data = event["data"]
        if data.get("status") != "ok":
            raise RuntimeError(f"PCBDraft tool execution failed: {data}")
        result = _tool_result(event)
        if result.get("success") is not True:
            raise RuntimeError(f"PCBDraft tool reported failure: {result}")
        if data.get("tool_name") in {"pcb_run_erc", "pcb_run_drc"}:
            check_receipt = result.get("result")
            if (
                not isinstance(check_receipt, dict)
                or check_receipt.get("production_ready") is not False
                or check_receipt.get("production_claimed") is not False
            ):
                raise RuntimeError(
                    "KiCad check result made a production-readiness claim"
                )
    event_names = {str(event.get("event")) for event in events}
    required_events = {
        "session_start",
        "model_request",
        "model_response",
        "tool_start",
        "tool_end",
        "turn_complete",
        "session_end",
    }
    if not required_events <= event_names:
        raise RuntimeError(
            "debug trace omitted lifecycle events: "
            + ", ".join(sorted(required_events - event_names))
        )
    if provider_requests != len(EXPECTED_TOOLS) + 1:
        raise RuntimeError(
            "fake provider received "
            f"{provider_requests} requests instead of {len(EXPECTED_TOOLS) + 1}"
        )
    return {
        "schema": "pcbdraft-kicad-e2e",
        "version": 1,
        "fixture": "non_baseline_fixture",
        "pcbdraft_mode": "one_shot",
        "provider": "local-openai-compatible",
        "provider_requests": provider_requests,
        "project_id": state["id"],
        "project_status": state["status"],
        "native_project_files": sorted(
            path.name for paths in native_files.values() for path in paths
        ),
        "tool_sequence": sequence,
        "checks": {
            name: {
                "state": receipt["state"],
                "outcome": receipt["outcome"],
                "report_sha256": receipt["report_sha256"],
            }
            for name, receipt in sorted(receipts.items())
        },
        "trace_events": len(events),
        "trace_retained": True,
        "project_retained": True,
        "production_ready": False,
        "production_claimed": False,
    }


def run_pcbdraft_smoke(
    python: str,
    output: Path,
    *,
    timeout: float,
    require_installed: bool,
) -> dict[str, Any]:
    provider = start_fake_provider()
    try:
        with tempfile.TemporaryDirectory(prefix="pcbdraft-e2e-") as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(mode=0o700)
            initialize_clean_home(home)
            runtime_home = root / "runtime"
            write_model_config(runtime_home, provider.base_url)
            workspace = root / "workspace"
            trace = root / "agent-trace.jsonl"
            environment = dict(os.environ)
            for name in (
                "PCBDRAFT_CONFIG",
                "PCBDRAFT_HOME",
                "PCBDRAFT_REPOSITORY_CONFIG",
                "PCBDRAFT_RUNTIME_HOME",
                "PCBDRAFT_RUNTIME_SHARED_AUTH_DIR",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_DATA_HOME": str(home / ".local" / "share"),
                    "PCBDRAFT_RUNTIME_HOME": str(runtime_home),
                    "PCBDRAFT_REPOSITORY_CONFIG": str(root / "repository.json"),
                    "PCBDRAFT_E2E_REPOSITORY": str(workspace),
                    "PCBDRAFT_DEBUG_TRACE": "1",
                    "PCBDRAFT_DEBUG_TRACE_PATH": str(trace),
                    "NO_COLOR": "1",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                }
            )
            import_probe = _run_bounded(
                [python, "-I", "-c", "import pcbdraft; print(pcbdraft.__file__)"],
                cwd=root,
                environment=environment,
                timeout=30,
                max_output_bytes=MAX_PROBE_OUTPUT_BYTES,
            )
            if (
                import_probe.returncode != 0
                or import_probe.timed_out
                or import_probe.output_limited
                or not import_probe.stdout.strip()
            ):
                raise RuntimeError(
                    "cannot import pcbdraft with the selected interpreter"
                )
            imported_from = Path(import_probe.stdout.strip()).resolve(strict=False)
            if require_installed and imported_from.is_relative_to(REPO / "src"):
                raise RuntimeError(
                    "installed-wheel smoke imported pcbdraft from the source checkout"
                )
            command = [
                python,
                "-I",
                "-c",
                (
                    "import os; "
                    "from pcbdraft.core.repository import configure_repository; "
                    "from pcbdraft.interfaces.terminal import launch_cli; "
                    "configure_repository(os.environ['PCBDRAFT_E2E_REPOSITORY']); "
                    f"raise SystemExit(launch_cli({['--query', REQUEST]!r}))"
                ),
            ]
            completed = _run_bounded(
                command,
                cwd=root,
                environment=environment,
                timeout=timeout,
                max_output_bytes=MAX_CHILD_OUTPUT_BYTES,
            )
            _write_text(output / "stdout.txt", completed.stdout)
            _write_text(output / "stderr.txt", completed.stderr)
            project = _copy_retained_evidence(
                workspace=workspace,
                trace=trace,
                output=output,
            )
            summary = _validate_run(
                completed=completed,
                project=project,
                trace=trace,
                provider_requests=provider.server.request_count,
            )
            summary["installed_distribution"] = require_installed
            _write_text(
                output / "pcbdraft-e2e.json",
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
            )
            return summary
    finally:
        provider.close()


def main() -> int:
    arguments = parse_args()
    python = Path(arguments.python).expanduser()
    if not python.is_absolute():
        located = shutil.which(str(python))
        python = Path(located) if located else python
    if not python.is_file():
        raise SystemExit("Python interpreter containing pcbdraft is required")
    if not shutil.which("kicad-cli"):
        raise SystemExit("real KiCad is required")
    if arguments.timeout <= 0 or arguments.timeout > 900:
        raise SystemExit("timeout must be in (0, 900] seconds")
    output = _prepare_output(arguments.output)
    summary = run_pcbdraft_smoke(
        str(python),
        output,
        timeout=arguments.timeout,
        require_installed=arguments.require_installed,
    )
    print(
        json.dumps(
            {"report": str(output / "pcbdraft-e2e.json"), **summary},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
