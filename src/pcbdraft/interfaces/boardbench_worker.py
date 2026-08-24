"""Isolated prompt-only BoardBench worker process.

The parent runner supplies only run-local paths and a closed request containing
the run id plus natural-language prompt.  This module establishes the local
repository and trace environment before importing Hermes, creates one trusted
blank project, and then enters the ordinary PCBDraft one-shot product path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import read_text_limited
from pcbdraft.core.redaction import sanitize_user_text

MAX_PROMPT_BYTES = 64 * 1024
MAX_REQUEST_BYTES = MAX_PROMPT_BYTES + 4 * 1024
REQUEST_SCHEMA = "pcbdraft-boardbench-worker-request"
REQUEST_VERSION = 1
MODEL_TURN_LIMIT = 90
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True)
class WorkerRequest:
    """Validated, evaluator-free boundary accepted by the worker."""

    run_id: str
    prompt: str
    repository: Path
    repository_config: Path
    trace: Path
    usage: Path
    pcb_tool_call_limit: int


@dataclass(frozen=True)
class _WorkerRuntime:
    configure_repository: Callable[[Path], Any]
    get_service: Callable[[], Any]
    set_current_project_id: Callable[[str | None], None]
    launch_cli: Callable[..., int]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcbdraft-boardbench-worker")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--repository-config", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--usage", required=True, type=Path)
    parser.add_argument("--pcb-tool-call-limit", required=True, type=int)
    return parser


def _request_document(path: Path) -> tuple[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("BoardBench worker request must be a regular file")
    try:
        value = json.loads(
            read_text_limited(path, MAX_REQUEST_BYTES),
            object_pairs_hook=_object_without_duplicates,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("BoardBench worker request must be valid JSON") from exc
    fields = {"schema", "version", "run_id", "prompt"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("BoardBench worker request has unexpected fields")
    if value["schema"] != REQUEST_SCHEMA or value["version"] != REQUEST_VERSION:
        raise ValidationError("unsupported BoardBench worker request schema/version")
    run_id = value["run_id"]
    prompt = value["prompt"]
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValidationError("BoardBench worker run id is invalid")
    if not isinstance(prompt, str):
        raise ValidationError("BoardBench prompt must be natural-language text")
    if not prompt.strip() or "\x00" in prompt:
        raise ValidationError(
            "BoardBench prompt must be non-empty natural-language text"
        )
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(
            "BoardBench prompt must be valid natural-language text"
        ) from exc
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise ValidationError("BoardBench prompt exceeds the size limit")
    return run_id, prompt


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("BoardBench worker request has duplicate fields")
        result[key] = value
    return result


def _validate_path(path: Path, label: str, *, may_exist: bool) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or "\x00" in str(raw):
        raise ValidationError(f"BoardBench {label} path must be absolute")
    for component in (raw, *raw.parents):
        if component.is_symlink():
            raise ValidationError(
                f"BoardBench {label} path must not contain a symbolic link"
            )
    if not may_exist and raw.exists():
        raise ValidationError(f"BoardBench {label} path must be fresh")
    if raw.parent.is_symlink() or not raw.parent.is_dir():
        raise ValidationError(f"BoardBench {label} parent is unavailable")
    return raw.resolve(strict=False)


def parse_request(argv: Sequence[str] | None = None) -> WorkerRequest:
    """Parse the closed worker argv contract; unknown fields are rejected."""

    args = _parser().parse_args(list(argv) if argv is not None else None)
    request_path = _validate_path(Path(args.request), "request", may_exist=True)
    run_id, prompt = _request_document(request_path)
    pcb_tool_call_limit = int(args.pcb_tool_call_limit)
    if not 1 <= pcb_tool_call_limit <= 100_000:
        raise ValidationError("BoardBench PCB tool-call limit is invalid")
    return WorkerRequest(
        run_id=run_id,
        prompt=prompt,
        repository=_validate_path(Path(args.repository), "repository", may_exist=False),
        repository_config=_validate_path(
            Path(args.repository_config), "repository config", may_exist=False
        ),
        trace=_validate_path(Path(args.trace), "trace", may_exist=False),
        usage=_validate_path(Path(args.usage), "usage", may_exist=False),
        pcb_tool_call_limit=pcb_tool_call_limit,
    )


def _load_runtime() -> _WorkerRuntime:
    """Import product/Hermes integration only after run-local env is installed."""

    from pcbdraft.agent.hermes_tools import (
        get_service,
        set_current_project_id,
    )
    from pcbdraft.core.repository import configure_repository
    from pcbdraft.interfaces.hermes_cli import launch_cli

    return _WorkerRuntime(
        configure_repository=configure_repository,
        get_service=get_service,
        set_current_project_id=set_current_project_id,
        launch_cli=launch_cli,
    )


def run_worker(
    request: WorkerRequest,
    *,
    runtime_loader: Callable[[], _WorkerRuntime] = _load_runtime,
) -> int:
    """Create one blank trusted project and submit exactly the corpus prompt."""

    os.environ.update(
        {
            "PCBDRAFT_REPOSITORY_CONFIG": str(request.repository_config),
            "PCBDRAFT_DEBUG_TRACE": "1",
            "PCBDRAFT_DEBUG_TRACE_PATH": str(request.trace),
            "PCBDRAFT_PCB_TOOL_CALL_LIMIT": str(request.pcb_tool_call_limit),
            "NO_COLOR": "1",
        }
    )
    runtime = runtime_loader()
    runtime.configure_repository(request.repository)
    view = runtime.get_service().create_empty_project(request.run_id)
    project = view.get("project") if isinstance(view, dict) else None
    project_id = project.get("id") if isinstance(project, dict) else None
    if not isinstance(project_id, str) or not project_id:
        raise PCBDraftError("BoardBench worker could not create a blank project")
    runtime.set_current_project_id(project_id)
    return int(
        runtime.launch_cli(
            [
                "--oneshot",
                request.prompt,
                "--usage-file",
                str(request.usage),
            ],
            permission_mode="workspace",
            model_turn_limit=MODEL_TURN_LIMIT,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run_worker(parse_request(argv))
    except PCBDraftError as exc:
        print(
            f"pcbdraft-boardbench-worker: {sanitize_user_text(str(exc))}",
            file=sys.stderr,
        )
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
