#!/usr/bin/env python3
"""Run the three public PCBDraft tutorials through the real one-shot product path.

The runner is intentionally separate from BoardBench: these are single-attempt
tutorial demonstrations, not benchmark samples.  Raw traces, configuration and
credentials remain in a private run directory.  Nothing is published by ``run``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

SCHEMA = "pcbdraft-public-examples-run"
VERSION = 1
DEFAULT_WALL_TIMEOUT_SECONDS = 600
DEFAULT_PCB_TOOL_CALL_LIMIT = 80
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_CHUNK_BYTES = 64 * 1024
CASES = (
    ("led-3v3-330r", "led-3v3-330r.txt"),
    ("rc-1k-100nf", "rc-1k-100nf.txt"),
    ("i2c-3v3-pullups", "i2c-3v3-pullups.txt"),
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checked_output(argv: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(  # noqa: S603 - fixed environment probes only
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"environment probe failed: {Path(argv[0]).name}")
    return result.stdout.strip()


def _source_identity(source_root: Path) -> dict[str, object]:
    commit = _checked_output(["git", "rev-parse", "HEAD"], cwd=source_root)
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise RuntimeError("source root did not return a full Git commit")
    dirty_output = _checked_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=source_root,
    )
    return {"commit": commit, "dirty": bool(dirty_output)}


def _runtime_probe(python: Path, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    code = (
        "import json,platform,pcbdraft;"
        "from pcbdraft.services.provider_connection import "
        "activate_provider_runtime,connection_status;"
        "activate_provider_runtime();s=connection_status(verify=False);"
        "print(json.dumps({'python_version':platform.python_version(),"
        "'package_version':getattr(pcbdraft,'__version__',None),"
        "'package_file':pcbdraft.__file__,'configured':s.configured,'usable':s.usable,"
        "'provider':s.provider,'model':s.model,'auth_kind':s.auth_kind}))"
    )
    result = subprocess.run(  # noqa: S603 - explicit selected interpreter
        [str(python), "-c", code],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("installed PCBDraft/provider preflight failed")
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "installed PCBDraft/provider preflight was malformed"
        ) from exc
    if (
        not isinstance(value, dict)
        or not value.get("configured")
        or not value.get("usable")
    ):
        raise RuntimeError("the isolated runtime does not have a usable provider")
    return value


def _copy_runtime_template(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError("runtime template must be a real directory")
    for member in (source, *source.parents):
        if member.is_symlink():
            raise RuntimeError("runtime template path must not contain symlinks")
    for root, directories, files in os.walk(source, followlinks=False):
        for name in (*directories, *files):
            if (Path(root) / name).is_symlink():
                raise RuntimeError("runtime template must not contain symlinks")
    shutil.copytree(source, destination, symlinks=False)
    for root, directories, files in os.walk(destination):
        try:
            Path(root).chmod(0o700)
        except OSError:
            pass
        for name in directories:
            try:
                (Path(root) / name).chmod(0o700)
            except OSError:
                pass
        for name in files:
            try:
                (Path(root) / name).chmod(0o600)
            except OSError:
                pass


def _worker_environment(runtime_home: Path, app_config: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PCBDRAFT_HOME",
        "PCBDRAFT_REPOSITORY_CONFIG",
    ):
        env.pop(name, None)
    env.update(
        {
            "PCBDRAFT_RUNTIME_HOME": str(runtime_home),
            "PCBDRAFT_CONFIG": str(app_config),
            "NO_COLOR": "1",
        }
    )
    return env


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass


def _run_worker(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[int | None, bool, bytes, bytes, float, bool]:
    """Run one worker with bounded combined output and a hard wall timeout."""

    if max_output_bytes <= 0:
        raise ValueError("worker output limit must be positive")
    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S603 - argv uses an explicit interpreter
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=(os.name == "posix"),
    )
    stdout = bytearray()
    stderr = bytearray()
    output_limited = threading.Event()
    output_lock = threading.Lock()
    readers: tuple[threading.Thread, ...] = ()

    def drain(stream: BinaryIO, destination: bytearray) -> None:
        while True:
            try:
                chunk = stream.read(_MAX_OUTPUT_CHUNK_BYTES)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with output_lock:
                remaining = max_output_bytes - len(stdout) - len(stderr)
                if remaining > 0:
                    destination.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_limited.set()
            if output_limited.is_set():
                return

    timed_out = False
    try:
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("failed to capture worker output")
        readers = (
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        )
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if output_limited.is_set():
                _terminate(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate(process)
                break
            time.sleep(0.01)
        process.wait(timeout=5)
    except BaseException:
        _terminate(process)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    return (
        process.returncode,
        timed_out,
        bytes(stdout),
        bytes(stderr),
        time.monotonic() - started,
        output_limited.is_set(),
    )


def _inventory(repository: Path) -> dict[str, object]:
    if not repository.is_dir():
        return {
            "project_count": 0,
            "native_artifacts": [],
            "board_svgs": [],
            "receipts": [],
        }
    projects = (
        [
            item
            for item in (repository / "projects").glob("*")
            if item.is_dir()
            and not item.is_symlink()
            and (item / "project.json").is_file()
        ]
        if (repository / "projects").is_dir()
        else []
    )
    native_suffixes = {".kicad_pro", ".kicad_sch", ".kicad_pcb"}
    native = []
    svgs = []
    receipts = []
    for path in sorted(repository.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(repository).as_posix()
        record = {
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        if path.suffix in native_suffixes:
            native.append(record)
        if path.name == "board.svg":
            svgs.append(record)
        if path.name == "receipt.json" and (
            "transactions" in path.parts
            or "previews" in path.parts
            or "releases" in path.parts
        ):
            receipts.append(record)
    return {
        "project_count": len(projects),
        "native_artifacts": native,
        "board_svgs": svgs,
        "receipts": receipts,
    }


def _usage_summary(path: Path) -> dict[str, object]:
    unknown = {
        "present": False,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "estimated_cost_usd": None,
        "cost_status": "unknown",
    }
    if path.is_symlink() or not path.is_file():
        return unknown
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return unknown
    if not isinstance(value, dict):
        return unknown
    return {
        "present": True,
        "input_tokens": value.get("input_tokens"),
        "output_tokens": value.get("output_tokens"),
        "total_tokens": value.get("total_tokens"),
        "estimated_cost_usd": value.get("estimated_cost_usd"),
        "cost_status": value.get("cost_status") or "unknown",
    }


def _default_private_parent() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "pcbdraft" / "public-example-runs"


def _case_result(
    *,
    case_id: str,
    prompt: str | None,
    started_at: str,
    elapsed: float,
    python: Path,
    runtime: dict[str, Any],
    usage: Path,
    inventory: dict[str, object],
    returncode: int | None,
    timed_out: bool,
    output_limited: bool,
    failure_reason: str | None,
    interrupted: bool = False,
) -> dict[str, object]:
    prompt_sha256 = (
        hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if prompt is not None
        else None
    )
    result: dict[str, object] = {
        "case_id": case_id,
        "status": "completed" if failure_reason is None else "failed",
        "prompt": prompt,
        "prompt_sha256": prompt_sha256,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "elapsed_seconds": round(elapsed, 6),
        "returncode": returncode,
        "timed_out": timed_out,
        "output_limited": output_limited,
        "interrupted": interrupted,
        "provider": runtime.get("provider"),
        "model": runtime.get("model"),
        "auth_kind": runtime.get("auth_kind"),
        "package_version": runtime.get("package_version"),
        "worker_python_version": runtime.get("python_version"),
        "worker_python_launcher": str(python),
        "usage": _usage_summary(usage),
        "artifacts": inventory,
        "terminal_capture": (
            "raw stdout/stderr with run timestamps; not an interactive TUI recording"
        ),
    }
    if failure_reason is not None:
        result["failure_reason"] = failure_reason
    return result


def _store_case_result(
    run_root: Path,
    case_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
) -> None:
    _write_json(case_root / "result.json", result)
    if result.get("status") == "failed":
        reason = result.get("failure_reason")
        _write_json(
            case_root / "failure.json",
            {
                "schema": "pcbdraft-public-example-case-failure",
                "version": 1,
                "case_id": result["case_id"],
                "status": "failed",
                "reason": reason if isinstance(reason, str) else "unknown",
                "started_at": result["started_at"],
                "completed_at": result["completed_at"],
                "elapsed_seconds": result["elapsed_seconds"],
                "returncode": result["returncode"],
                "timed_out": result["timed_out"],
                "output_limited": result["output_limited"],
                "interrupted": result["interrupted"],
            },
        )
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise TypeError("run manifest case list is unavailable")
    cases.append(result)
    _write_json(run_root / "manifest.json", manifest)


def _run(args: argparse.Namespace) -> int:
    script_root = Path(__file__).resolve().parents[1]
    prompts_root = script_root / "examples" / "prompts"
    source_root = args.source_root.expanduser().resolve()
    # Preserve the venv launcher: resolving its symlink selects the base Python.
    python = args.python.expanduser().absolute()
    # Keep this path unresolved so symlinks in or below the template are rejected.
    template = args.runtime_template.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("--python must name an executable file")
    if not source_root.is_dir():
        raise RuntimeError("--source-root must name a Git worktree")
    if not 1 <= args.pcb_tool_call_limit <= 500:
        raise RuntimeError("--pcb-tool-call-limit must be between 1 and 500")
    if not 1 <= args.wall_timeout_seconds <= 3600:
        raise RuntimeError("--wall-timeout-seconds must be between 1 and 3600")
    max_output_bytes = getattr(args, "max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
    if not 1 <= max_output_bytes <= 64 * 1024 * 1024:
        raise RuntimeError("--max-output-bytes must be between 1 and 67108864")
    parent = (args.private_root or _default_private_parent()).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_root = parent / f"tutorial-{_stamp()}-{os.getpid()}"
    run_root.mkdir(mode=0o700)
    identity = _source_identity(source_root)
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "version": VERSION,
        "started_at": _utc_now(),
        "completed_at": None,
        "purpose": "public_tutorial_single_attempts_not_benchmark",
        "publication_status": "private_review_required",
        "source": identity,
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "worker_python_launcher": str(python),
            "kicad": _checked_output(["kicad-cli", "--version"]),
        },
        "limits": {
            "wall_timeout_seconds_per_case": args.wall_timeout_seconds,
            "pcb_tool_calls_per_case": args.pcb_tool_call_limit,
            "max_output_bytes_per_case": max_output_bytes,
            "attempts_per_case": 1,
        },
        "cases": [],
    }
    _write_json(run_root / "manifest.json", manifest)
    print(f"Private evidence: {run_root}", flush=True)
    overall = 0
    interrupted = False
    for case_id, filename in CASES:
        case_root = run_root / case_id
        case_root.mkdir(mode=0o700)
        runtime_home = case_root / "runtime"
        app_root = case_root / "application"
        app_config = app_root / "config.json"
        repository = case_root / "repository"
        repository_config = case_root / "repository-config.json"
        trace = case_root / "agent-trace.jsonl"
        usage = case_root / "usage.json"
        prompt: str | None = None
        runtime: dict[str, Any] = {}
        inventory: dict[str, object] = {
            "project_count": 0,
            "native_artifacts": [],
            "board_svgs": [],
            "receipts": [],
        }
        returncode: int | None = None
        timed_out = False
        output_limited = False
        started_at = _utc_now()
        started = time.monotonic()
        failure_reason: str | None = None
        stage = "runtime_template"
        print(f"[{case_id}] one real attempt started", flush=True)
        try:
            _copy_runtime_template(template, runtime_home)
            stage = "application_setup"
            app_root.mkdir(mode=0o700)
            stage = "prompt"
            prompt = (prompts_root / filename).read_text(encoding="utf-8").strip()
            stage = "request"
            _write_json(
                case_root / "request.json",
                {
                    "schema": "pcbdraft-boardbench-worker-request",
                    "version": 1,
                    "run_id": case_id,
                    "prompt": prompt,
                },
            )
            env = _worker_environment(runtime_home, app_config)
            stage = "provider_preflight"
            runtime_value = _runtime_probe(python, env, case_root)
            if not isinstance(runtime_value, dict):
                raise TypeError("provider preflight returned a non-object")
            runtime = runtime_value
            argv = [
                str(python),
                "-m",
                "pcbdraft.interfaces.boardbench_worker",
                "--request",
                str(case_root / "request.json"),
                "--repository",
                str(repository),
                "--repository-config",
                str(repository_config),
                "--trace",
                str(trace),
                "--usage",
                str(usage),
                "--pcb-tool-call-limit",
                str(args.pcb_tool_call_limit),
            ]
            stage = "worker"
            (
                returncode,
                timed_out,
                stdout,
                stderr,
                _worker_elapsed,
                output_limited,
            ) = _run_worker(
                argv,
                cwd=case_root,
                env=env,
                timeout=args.wall_timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
            stage = "worker_output"
            (case_root / "stdout.txt").write_bytes(stdout)
            (case_root / "stderr.txt").write_bytes(stderr)
            stage = "artifact_collection"
            inventory = _inventory(repository)
        except KeyboardInterrupt:
            failure_reason = "parent_interrupted"
            interrupted = True
        except Exception:  # noqa: BLE001 - isolate one case and continue
            # Keep a categorized, secret-free receipt and move to the next case.
            failure_reason = f"{stage}_failed"

        elapsed = time.monotonic() - started
        if failure_reason is None:
            if timed_out:
                failure_reason = "wall_timeout"
            elif output_limited:
                failure_reason = "worker_output_limit"
            elif returncode != 0:
                failure_reason = "worker_failed"
            elif inventory.get("project_count") != 1:
                failure_reason = "invalid_project_artifact_count"
        result = _case_result(
            case_id=case_id,
            prompt=prompt,
            started_at=started_at,
            elapsed=elapsed,
            python=python,
            runtime=runtime,
            usage=usage,
            inventory=inventory,
            returncode=returncode,
            timed_out=timed_out,
            output_limited=output_limited,
            failure_reason=failure_reason,
            interrupted=interrupted,
        )
        _store_case_result(run_root, case_root, manifest, result)
        if failure_reason is not None:
            overall = 130 if interrupted else 1
        print(
            f"[{case_id}] finished rc={returncode} timeout={timed_out} "
            f"projects={inventory['project_count']} elapsed={elapsed:.1f}s",
            file=sys.stderr if interrupted else sys.stdout,
            flush=True,
        )
        if interrupted:
            break

    if interrupted:
        manifest["interrupted_at"] = _utc_now()
    else:
        manifest["completed_at"] = _utc_now()
    _write_json(run_root / "manifest.json", manifest)
    if not interrupted:
        print(
            "No public bundle was created; review the private evidence first.",
            flush=True,
        )
    return overall


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python from the installed clean PCBDraft wheel",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="clean Git worktree used to build the wheel",
    )
    parser.add_argument(
        "--runtime-template",
        type=Path,
        required=True,
        help="private provider/config snapshot copied once per case",
    )
    parser.add_argument(
        "--private-root",
        type=Path,
        help="private evidence parent; defaults outside the checkout",
    )
    parser.add_argument(
        "--wall-timeout-seconds", type=int, default=DEFAULT_WALL_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--pcb-tool-call-limit", type=int, default=DEFAULT_PCB_TOOL_CALL_LIMIT
    )
    parser.add_argument(
        "--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(_parser().parse_args(argv))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"run-public-examples: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
