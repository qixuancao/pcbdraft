#!/usr/bin/env python3
"""Prepare and run one private LQ EDA pilot attempt.

The pilot runner accepts a task package containing only ``input/prompt.txt``,
``input/symbols`` and ``input/footprints``.  It copies those inputs, the
allow-listed runtime files, and the selected KiCad libraries into a fresh
private run directory.  The worker receives the prompt through the existing
BoardBench worker boundary with the ``pcbdraft`` toolset only; no answer key or
evaluator data is ever passed to it.

``prepare`` performs the source, resource, and import preflight without
starting a model turn.  ``run`` performs the same preparation and then makes
exactly one worker attempt.  The result and trace stay in the private run
directory for the independent scorer.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "pcbdraft-lq-eda-pilot-run"
VERSION = 1
DEFAULT_WALL_TIMEOUT_SECONDS = 900
DEFAULT_PCB_TOOL_CALL_LIMIT = 120
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
RUNTIME_TEMPLATE_FILES = ("config.yaml", "auth.json", ".env")
TASK_INPUT_FILES = ("prompt.txt", "symbols", "footprints")
CONTRACT_FILE = "contract.json"
PLACEHOLDER_RE = re.compile(
    r"(?i)(?:\bplaceholder\b|\bhash[ _-]?pending\b|"
    r"\bto[ _-]?be[ _-]?generated\b)"
)


def _load_public_runner():
    path = Path(__file__).with_name("run-public-examples.py")
    spec = importlib.util.spec_from_file_location(
        "pcbdraft_public_runner_for_lq_eda", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the public runner helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PUBLIC = _load_public_runner()
_run_worker = _PUBLIC._run_worker
_runtime_probe = _PUBLIC._runtime_probe
_worker_environment = _PUBLIC._worker_environment
_inventory = _PUBLIC._inventory


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _file_record(path: Path, *, relative_to: Path | None = None) -> dict[str, object]:
    relative = (
        path.name if relative_to is None else path.relative_to(relative_to).as_posix()
    )
    return {"path": relative, "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _tree_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"resource tree contains a symbolic link: {path}")
        if path.is_file():
            records.append(_file_record(path, relative_to=root))
    return records


def _validate_tree(path: Path, label: str, *, scan_contents: bool = True) -> Path:
    raw = path.expanduser().absolute()
    if raw.is_symlink() or not raw.is_dir():
        raise RuntimeError(f"{label} must be a real directory")
    for component in (raw, *raw.parents):
        if component.is_symlink():
            raise RuntimeError(f"{label} path must not contain symlinks")
    if scan_contents:
        for member in raw.rglob("*"):
            if member.is_symlink():
                raise RuntimeError(f"{label} contains a symbolic link: {member}")
    return raw


def _validate_private_root(path: Path) -> Path:
    raw = path.expanduser().absolute()
    if raw.exists():
        raise RuntimeError("--private-root must name a fresh directory")
    for component in raw.parents:
        if component.exists() and component.is_symlink():
            raise RuntimeError("--private-root path must not contain symlinks")
    raw.mkdir(parents=True, mode=0o700)
    return raw


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, destination, follow_symlinks=False)
    try:
        destination.chmod(0o600)
    except OSError:
        pass


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a symlink-free tree while keeping the destination private."""

    _validate_tree(source, "resource")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        destination.chmod(0o700)
    except OSError:
        pass
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                destination_path.chmod(0o700)
            except OSError:
                pass
        elif source_path.is_file():
            _copy_file(source_path, destination_path)


def _merge_tree(
    source: Path, destination: Path, *, label: str
) -> list[dict[str, object]]:
    """Merge one library into a private directory, rejecting conflicting files."""

    _validate_tree(source, label)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            if destination_path.exists() and not destination_path.is_dir():
                raise RuntimeError(f"library path collision at {relative}")
            destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                destination_path.chmod(0o700)
            except OSError:
                pass
            continue
        if not source_path.is_file():
            continue
        if destination_path.exists():
            if not destination_path.is_file() or _sha256(source_path) != _sha256(
                destination_path
            ):
                raise RuntimeError(f"conflicting library files at {relative}")
            continue
        _copy_file(source_path, destination_path)
    return _tree_records(destination)


def _source_identity(source_root: Path) -> dict[str, object]:
    identity = _PUBLIC._source_identity(source_root)
    if identity.get("dirty"):
        raise RuntimeError("source root must be clean before a pilot run")
    result = dict(identity)
    tree = _PUBLIC._checked_output(["git", "rev-parse", "HEAD^{tree}"], cwd=source_root)
    if len(tree) != 40 or any(char not in "0123456789abcdef" for char in tree):
        raise RuntimeError("source root did not return a full Git tree hash")
    result["tree"] = tree
    return result


def _read_json_file(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(  # noqa: TRY004 - CLI validation has one public error
            f"{label} must contain a JSON object"
        )
    return value


def _assert_no_placeholders(path: Path, *, label: str) -> None:
    """Reject unfinished task/resource hashes before any model call."""

    for member in sorted(path.rglob("*")):
        if member.is_symlink():
            raise RuntimeError(f"{label} contains a symbolic link: {member}")
        if not member.is_file():
            continue
        if PLACEHOLDER_RE.search(member.name):
            raise RuntimeError(f"{label} contains a placeholder marker")
        try:
            text = member.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"cannot read {label} resource") from exc
        if PLACEHOLDER_RE.search(text):
            raise RuntimeError(f"{label} contains a placeholder marker")


def _contract_preflight(task: Path) -> dict[str, object]:
    """Validate the public contract and bind its declared resource hashes."""

    contract_path = task / CONTRACT_FILE
    contract = _read_json_file(contract_path, "task contract")
    if contract.get("schema") != "pcbdraft-lq-eda-task-contract":
        raise RuntimeError("unsupported LQ EDA task contract schema")
    if contract.get("version") != 1:
        raise RuntimeError("unsupported LQ EDA task contract version")
    _assert_no_placeholders(contract_path.parent, label="task")

    input_spec = contract.get("input")
    if not isinstance(input_spec, dict):
        raise RuntimeError(  # noqa: TRY004 - CLI validation has one public error
            "task contract input section is missing"
        )
    prompt_path = input_spec.get("prompt")
    if prompt_path != "input/prompt.txt":
        raise RuntimeError("task contract prompt path is not the worker input")
    prompt_hashes = [
        value
        for value in (contract.get("prompt_sha256"), input_spec.get("prompt_sha256"))
        if value is not None
    ]
    if not prompt_hashes or not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        for value in prompt_hashes
    ):
        raise RuntimeError("task contract prompt_sha256 is missing or invalid")
    if len(set(prompt_hashes)) != 1:
        raise RuntimeError("task contract prompt_sha256 fields disagree")
    prompt_file = task / prompt_path
    if prompt_file.is_symlink() or not prompt_file.is_file():
        raise RuntimeError("task contract prompt resource is unavailable")
    if _sha256(prompt_file) != prompt_hashes[0]:
        raise RuntimeError("task contract prompt hash mismatch")
    declared_files: list[tuple[str, object]] = []
    for key in ("custom_symbols", "custom_footprints"):
        entries = input_spec.get(key)
        if not isinstance(entries, list) or not entries:
            raise RuntimeError(f"task contract {key} are required")
        declared_files.extend((key, entry) for entry in entries)
    for category, entry in declared_files:
        if not isinstance(entry, dict):
            raise RuntimeError(  # noqa: TRY004 - CLI validation has one public error
                f"task contract {category} entry is invalid"
            )
        relative = entry.get("path")
        expected_hash = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        ):
            raise RuntimeError(f"task contract {category} hash/path is invalid")
        resource = task / relative
        if resource.is_symlink() or not resource.is_file():
            raise RuntimeError(f"task contract resource is unavailable: {relative}")
        if _sha256(resource) != expected_hash:
            raise RuntimeError(f"task contract resource hash mismatch: {relative}")

    stock_spec = input_spec.get("stock_libraries")
    if not isinstance(stock_spec, dict) or stock_spec.get("required") is not True:
        raise RuntimeError("task contract must require stock KiCad libraries")
    for key in ("symbol_ids", "footprint_ids"):
        values = stock_spec.get(key)
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise RuntimeError(f"task contract stock {key} are invalid")
    return {
        "path": CONTRACT_FILE,
        "sha256": _sha256(contract_path),
        "task_id": contract.get("task_id"),
        "prompt_sha256": prompt_hashes[0],
        "stock_symbol_ids": stock_spec["symbol_ids"],
        "stock_footprint_ids": stock_spec["footprint_ids"],
        "custom_symbol_ids": [
            entry["library_id"]
            for entry in input_spec["custom_symbols"]
            if isinstance(entry, dict)
        ],
        "custom_footprint_ids": [
            entry["library_id"]
            for entry in input_spec["custom_footprints"]
            if isinstance(entry, dict)
        ],
    }


def _task_input(task: Path, destination: Path) -> tuple[str, list[dict[str, object]]]:
    task = _validate_tree(task, "task")
    input_root = task / "input"
    if input_root.is_symlink() or not input_root.is_dir():
        raise RuntimeError(
            "task must contain input/prompt.txt, input/symbols, and input/footprints"
        )
    entries = {item.name for item in input_root.iterdir()}
    if entries - set(TASK_INPUT_FILES):
        raise RuntimeError("task input contains files outside the allow-list")
    for name in TASK_INPUT_FILES:
        member = input_root / name
        if name == "prompt.txt":
            if member.is_symlink() or not member.is_file():
                raise RuntimeError("task input/prompt.txt must be a regular file")
        elif member.is_symlink() or not member.is_dir():
            raise RuntimeError(f"task input/{name} must be a real directory")
    prompt = (input_root / "prompt.txt").read_text(encoding="utf-8")
    if not prompt.strip() or "\x00" in prompt:
        raise RuntimeError("task prompt must be non-empty and NUL-free")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    _copy_file(input_root / "prompt.txt", destination / "prompt.txt")
    _copy_tree(input_root / "symbols", destination / "symbols")
    _copy_tree(input_root / "footprints", destination / "footprints")
    return prompt, _tree_records(destination)


def _runtime_template(source: Path, destination: Path) -> list[dict[str, object]]:
    source = _validate_tree(source, "runtime template")
    allowed = set(RUNTIME_TEMPLATE_FILES)
    entries = {item.name for item in source.iterdir()}
    if entries - allowed:
        raise RuntimeError(
            "runtime template may contain only config.yaml, auth.json, and .env"
        )
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in RUNTIME_TEMPLATE_FILES:
        member = source / name
        if not member.exists():
            continue
        if member.is_symlink() or not member.is_file():
            raise RuntimeError(f"runtime template member {name} must be a regular file")
        _copy_file(member, destination / name)
    return _tree_records(destination)


def _arg_path(value: Path | None, *environment_names: str) -> Path | None:
    if value is not None:
        return value
    for name in environment_names:
        raw = os.environ.get(name, "").strip()
        if raw:
            return Path(raw)
    return None


def _kicad_stock_directory(
    python: Path, env: dict[str, str], cwd: Path, kind: str
) -> Path:
    code = (
        "from pcbdraft.kicad.runtime import kicad_data_directory;"
        f"print(kicad_data_directory({kind!r}))"
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
        raise RuntimeError(f"KiCad {kind} stock library discovery failed")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"KiCad {kind} stock library discovery was empty")
    return Path(lines[-1]).expanduser().absolute()


def _prepare_libraries(
    args: argparse.Namespace,
    task_input: Path,
    run_root: Path,
    *,
    python: Path,
    discovery_env: dict[str, str],
) -> dict[str, object]:
    library_root = run_root / "libraries"
    symbols = library_root / "symbols"
    footprints = library_root / "footprints"
    symbol_sources: list[tuple[str, Path]] = [("task-input", task_input / "symbols")]
    footprint_sources: list[tuple[str, Path]] = [
        ("task-input", task_input / "footprints")
    ]
    stock_symbols = _arg_path(
        args.stock_symbol_dir, "KICAD_SYMBOL_DIR", "KICAD10_SYMBOL_DIR"
    )
    stock_footprints = _arg_path(
        args.stock_footprint_dir, "KICAD_FOOTPRINT_DIR", "KICAD10_FOOTPRINT_DIR"
    )
    if stock_symbols is None:
        stock_symbols = _kicad_stock_directory(
            python, discovery_env, run_root, "symbols"
        )
    if stock_footprints is None:
        stock_footprints = _kicad_stock_directory(
            python, discovery_env, run_root, "footprints"
        )
    symbol_sources.insert(0, ("stock", stock_symbols))
    footprint_sources.insert(0, ("stock", stock_footprints))
    if args.custom_symbol_dir is not None:
        symbol_sources.append(("custom", args.custom_symbol_dir))
    if args.custom_footprint_dir is not None:
        footprint_sources.append(("custom", args.custom_footprint_dir))

    symbol_records: list[dict[str, object]] = []
    footprint_records: list[dict[str, object]] = []
    for label, source in symbol_sources:
        symbol_records = _merge_tree(
            source.expanduser().absolute(), symbols, label=label
        )
    for label, source in footprint_sources:
        footprint_records = _merge_tree(
            source.expanduser().absolute(), footprints, label=label
        )
    return {
        "symbols": {"destination": "libraries/symbols", "files": symbol_records},
        "footprints": {
            "destination": "libraries/footprints",
            "files": footprint_records,
        },
    }


def _worker_environment_for_run(
    run_root: Path,
    runtime_home: Path,
    app_config: Path,
    input_root: Path,
    libraries: dict[str, object],
) -> dict[str, str]:
    env = _worker_environment(runtime_home, app_config)
    env.update(
        {
            "PCBDRAFT_LQ_EDA_INPUT": str(input_root),
            "PCBDRAFT_LQ_EDA_TOOLSET": "pcbdraft",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "PCBDraft LQ EDA Pilot",
            "GIT_AUTHOR_EMAIL": "pcbdraft-pilot@localhost",
            "GIT_COMMITTER_NAME": "PCBDraft LQ EDA Pilot",
            "GIT_COMMITTER_EMAIL": "pcbdraft-pilot@localhost",
        }
    )
    symbol_dir = run_root / str(libraries["symbols"]["destination"])
    footprint_dir = run_root / str(libraries["footprints"]["destination"])
    for name in ("KICAD_SYMBOL_DIR", "KICAD10_SYMBOL_DIR"):
        env[name] = str(symbol_dir)
    for name in ("KICAD_FOOTPRINT_DIR", "KICAD10_FOOTPRINT_DIR"):
        env[name] = str(footprint_dir)
    return env


def _import_probe(python: Path, env: dict[str, str], cwd: Path) -> dict[str, object]:
    code = (
        "import importlib,json,pcbdraft;"
        "mods=['pcbdraft.interfaces.boardbench_worker','pcbdraft.kicad','pcbdraft.agent.tool_bindings'];"
        "[importlib.import_module(m) for m in mods];"
        "print(json.dumps({'package_file':pcbdraft.__file__,'modules':mods}))"
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
        raise RuntimeError("installed PCBDraft import preflight failed")
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed PCBDraft import preflight was malformed") from exc
    if not isinstance(value, dict) or not value.get("package_file"):
        raise RuntimeError(
            "installed PCBDraft import preflight returned no package path"
        )
    return value


def _resolver_probe(
    python: Path,
    env: dict[str, str],
    cwd: Path,
    contract: dict[str, object],
) -> dict[str, object]:
    """Describe every contract resource in a child process with KiCad env set."""

    symbol_ids = [
        *contract["stock_symbol_ids"],
        *contract["custom_symbol_ids"],
    ]
    footprint_ids = [
        *contract["stock_footprint_ids"],
        *contract["custom_footprint_ids"],
    ]
    code = (
        "import json;"
        "from pcbdraft.agent.part_resolver import LocalKiCadPartResolver;"
        "from pcbdraft.agent.footprint_resolver import LocalKiCadFootprintResolver;"
        f"symbols={symbol_ids!r};footprints={footprint_ids!r};"
        "part=LocalKiCadPartResolver();foot=LocalKiCadFootprintResolver();"
        "[part.describe(value) for value in symbols];"
        "[foot.describe(value) for value in footprints];"
        "print(json.dumps({'symbols':symbols,'footprints':footprints}))"
    )
    result = subprocess.run(  # noqa: S603 - explicit selected interpreter
        [str(python), "-c", code],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("KiCad resource resolver preflight failed")
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("KiCad resource resolver preflight was malformed") from exc
    if value != {"symbols": symbol_ids, "footprints": footprint_ids}:
        raise RuntimeError("KiCad resource resolver preflight was incomplete")
    return value


def _assert_import_binds_source(
    import_probe: dict[str, object], source_root: Path
) -> None:
    package_file = import_probe.get("package_file")
    if not isinstance(package_file, str) or not package_file:
        raise RuntimeError("import preflight returned no package file")
    try:
        package_path = Path(package_file).expanduser().resolve(strict=True)
        package_path.relative_to((source_root / "src").resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "import preflight did not bind the clean source checkout"
        ) from exc


def _trace_timing(path: Path) -> dict[str, object]:
    stage_counts: dict[str, int] = {}
    stage_duration_ms: dict[str, float] = {}
    tool_counts: dict[str, int] = {}
    tool_duration_ms: dict[str, float] = {}
    event_count = 0
    malformed = 0
    if path.is_file() and not path.is_symlink():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(value, dict):
                malformed += 1
                continue
            event = value.get("event")
            data = value.get("data")
            if not isinstance(event, str) or not isinstance(data, dict):
                malformed += 1
                continue
            event_count += 1
            stage = str(data.get("stage") or event)
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            raw_duration = data.get("duration_ms")
            if isinstance(raw_duration, (int, float)) and not isinstance(
                raw_duration, bool
            ):
                stage_duration_ms[stage] = stage_duration_ms.get(stage, 0.0) + float(
                    raw_duration
                )
            if event == "tool_end":
                tool = str(data.get("tool_name") or data.get("tool") or "unknown")
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
                if isinstance(raw_duration, (int, float)) and not isinstance(
                    raw_duration, bool
                ):
                    tool_duration_ms[tool] = tool_duration_ms.get(tool, 0.0) + float(
                        raw_duration
                    )
    return {
        "trace_present": path.is_file() and not path.is_symlink(),
        "event_count": event_count,
        "malformed_lines": malformed,
        "stage_counts": dict(sorted(stage_counts.items())),
        "stage_duration_ms": dict(sorted(stage_duration_ms.items())),
        "tool_counts": dict(sorted(tool_counts.items())),
        "tool_duration_ms": dict(sorted(tool_duration_ms.items())),
        "aggregation_note": (
            "stage and tool duration sums are derived independently from trace events; "
            "overlapping work must not be added as total wall time"
        ),
    }


def _evidence(path: Path) -> dict[str, object] | None:
    if path.is_symlink() or not path.is_file():
        return None
    return _file_record(path)


def _common_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--runtime-template", required=True, type=Path)
    parser.add_argument(
        "--private-root", required=True, type=Path, help="fresh private run directory"
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--stock-symbol-dir", type=Path)
    parser.add_argument("--stock-footprint-dir", type=Path)
    parser.add_argument("--custom-symbol-dir", type=Path)
    parser.add_argument("--custom-footprint-dir", type=Path)
    parser.add_argument(
        "--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="copy resources and run preflight only"
    )
    _common_parser(prepare)
    run = commands.add_parser("run", help="prepare and make exactly one worker attempt")
    _common_parser(run)
    run.add_argument(
        "--wall-timeout-seconds", type=int, default=DEFAULT_WALL_TIMEOUT_SECONDS
    )
    run.add_argument(
        "--pcb-tool-call-limit", type=int, default=DEFAULT_PCB_TOOL_CALL_LIMIT
    )
    return parser


def _prepare(
    args: argparse.Namespace,
) -> tuple[Path, dict[str, object], str, dict[str, str]]:
    python = args.python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("--python must name an executable file")
    if args.command == "run":
        if not 1 <= args.wall_timeout_seconds <= 3600:
            raise RuntimeError("--wall-timeout-seconds must be between 1 and 3600")
        if not 1 <= args.pcb_tool_call_limit <= 500:
            raise RuntimeError("--pcb-tool-call-limit must be between 1 and 500")
    if not 1 <= args.max_output_bytes <= 64 * 1024 * 1024:
        raise RuntimeError("--max-output-bytes must be between 1 and 67108864")
    source_root = _validate_tree(args.source_root, "source root", scan_contents=False)
    source = _source_identity(source_root)
    task = _validate_tree(args.task, "task")
    contract = _contract_preflight(task)
    template = _validate_tree(args.runtime_template, "runtime template")
    run_root = _validate_private_root(args.private_root)
    input_root = run_root / "input"
    prompt, input_records = _task_input(task, input_root)
    runtime_root = run_root / "runtime"
    runtime_records = _runtime_template(template, runtime_root)
    app_config = run_root / "application" / "config.json"
    discovery_env = _worker_environment(runtime_root, app_config)
    libraries = _prepare_libraries(
        args,
        input_root,
        run_root,
        python=python,
        discovery_env=discovery_env,
    )
    env = _worker_environment_for_run(
        run_root, runtime_root, app_config, input_root, libraries
    )
    runtime = _runtime_probe(python, env, run_root)
    import_probe = _import_probe(python, env, run_root)
    _assert_import_binds_source(import_probe, source_root)
    resolver_probe = _resolver_probe(python, env, run_root, contract)
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "version": VERSION,
        "status": "prepared",
        "started_at": _utc_now(),
        "completed_at": None,
        "source": source,
        "contract": contract,
        "task": {
            "id": task.name,
            "input_files": input_records,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "answer_key_supplied_to_worker": False,
        },
        "runtime_template": {"files": runtime_records},
        "libraries": libraries,
        "preflight": {
            "runtime": runtime,
            "import": import_probe,
            "resolver": resolver_probe,
        },
        "limits": {
            "attempts": 0,
            "wall_timeout_seconds": getattr(args, "wall_timeout_seconds", None),
            "pcb_tool_call_limit": getattr(args, "pcb_tool_call_limit", None),
            "max_output_bytes": args.max_output_bytes,
        },
        "worker": {"toolsets": ["pcbdraft"], "answer_key": None},
    }
    _write_json(run_root / "manifest.json", manifest)
    return run_root, manifest, prompt, env


def _run(args: argparse.Namespace) -> int:
    run_root, manifest, prompt, env = _prepare(args)
    if args.command == "prepare":
        print(f"Prepared private run: {run_root}")
        return 0

    request_path = run_root / "request.json"
    (run_root / "output").mkdir(parents=True, exist_ok=True, mode=0o700)
    (run_root / "trace").mkdir(parents=True, exist_ok=True, mode=0o700)
    repository = run_root / "output" / "repository"
    repository_config = run_root / "output" / "repository-config.json"
    trace = run_root / "trace" / "agent-trace.jsonl"
    usage = run_root / "trace" / "usage.json"
    _write_json(
        request_path,
        {
            "schema": "pcbdraft-boardbench-worker-request",
            "version": 1,
            "run_id": "lq-eda-pilot",
            "prompt": prompt,
        },
    )
    argv = [
        str(args.python.expanduser().absolute()),
        "-m",
        "pcbdraft.interfaces.boardbench_worker",
        "--request",
        str(request_path),
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
    started = time.monotonic()
    returncode: int | None = None
    timed_out = False
    output_limited = False
    failure_reason: str | None = None
    worker_started = False
    worker_elapsed = 0.0
    stdout = b""
    stderr = b""
    try:
        manifest["status"] = "running"
        manifest["limits"] = {
            **dict(manifest["limits"]),
            "attempts": 1,
            "wall_timeout_seconds": args.wall_timeout_seconds,
            "pcb_tool_call_limit": args.pcb_tool_call_limit,
        }
        _write_json(run_root / "manifest.json", manifest)
        worker_started = True
        returncode, timed_out, stdout, stderr, worker_elapsed, output_limited = (
            _run_worker(
                argv,
                cwd=run_root,
                env=env,
                timeout=args.wall_timeout_seconds,
                max_output_bytes=args.max_output_bytes,
            )
        )
    except KeyboardInterrupt:
        failure_reason = "parent_interrupted"
        worker_elapsed = time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 - receipt records only a safe category
        failure_reason = "worker_failed" if worker_started else "preflight_failed"
        worker_elapsed = time.monotonic() - started
        stderr = str(exc).encode("utf-8", errors="replace")
    finally:
        (run_root / "worker.stdout").write_bytes(stdout)
        (run_root / "worker.stderr").write_bytes(stderr)
    inventory = _inventory(repository)
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
    result: dict[str, object] = {
        "schema": "pcbdraft-lq-eda-pilot-result",
        "version": 1,
        "status": "completed" if failure_reason is None else "failed",
        "started_at": manifest["started_at"],
        "completed_at": _utc_now(),
        "elapsed_seconds": round(elapsed, 6),
        "worker_elapsed_seconds": round(worker_elapsed, 6),
        "returncode": returncode,
        "timed_out": timed_out,
        "output_limited": output_limited,
        "attempts": int(worker_started),
        "answer_key_supplied_to_worker": False,
        "toolset": "pcbdraft",
        "evaluation": "not_run",
        "score": None,
        "artifacts": inventory,
        "timing": _trace_timing(trace),
        "evidence": {
            "trace": _evidence(trace),
            "usage": _evidence(usage),
            "stdout": _evidence(run_root / "worker.stdout"),
            "stderr": _evidence(run_root / "worker.stderr"),
        },
    }
    if failure_reason is not None:
        result["failure_reason"] = failure_reason
    _write_json(run_root / "result.json", result)
    manifest["status"] = result["status"]
    manifest["completed_at"] = result["completed_at"]
    manifest["result"] = {
        **result,
        "path": "result.json",
        "sha256": _sha256(run_root / "result.json"),
    }
    _write_json(run_root / "manifest.json", manifest)
    print(f"Private evidence: {run_root}")
    if failure_reason is not None:
        print(f"Pilot failed: {failure_reason}", file=sys.stderr)
        return 130 if failure_reason == "parent_interrupted" else 1
    print(f"Pilot completed in {elapsed:.1f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(_parser().parse_args(argv))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"run-lq-eda-pilot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
