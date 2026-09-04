"""Private, isolated Hermes worker for one GUI message.

The prompt crosses the process boundary only in a private request file.  This
module deliberately calls the current vendored Hermes one-shot agent function
directly: the public one-shot command hard-exits and is therefore unsuitable
for returning a structured result to the resident GUI server.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, make_directory, read_text_limited
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.runs import utc_timestamp

REQUEST_SCHEMA = "pcbdraft-gui-worker-request"
RESULT_SCHEMA = "pcbdraft-gui-worker-result"
EVENT_SCHEMA = "pcbdraft-gui-worker-event"
WORKER_VERSION = 1
MAX_REQUEST_BYTES = 24 * 1024
MAX_PROMPT_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MODEL_TURN_LIMIT = 90
MAX_WORKER_EVENTS = 2_000

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,79}")
_PCB_TOOL = re.compile(r"pcb_[a-z0-9_]{1,63}")


@dataclass(frozen=True)
class WorkerRequest:
    """Validated closed request consumed by exactly one worker process."""

    project_id: str
    turn_id: str
    prompt: str
    repository: Path
    request_path: Path
    result_path: Path
    event_dir: Path


@dataclass(frozen=True)
class _WorkerRuntime:
    """Narrow product runtime seam used by deterministic worker tests."""

    create_service: Callable[[Path], Any]
    pin_service: Callable[[Any], None]
    bind_project: Callable[[str | None], None]
    activate: Callable[[], None]
    provider_is_usable: Callable[[], bool]
    run_agent: Callable[[str], str]
    install_observer: Callable[[_WorkerEventWriter], Any]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcbdraft-gui-worker")
    parser.add_argument("--request", required=True, type=Path)
    return parser


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("GUI worker request has duplicate fields")
        result[key] = value
    return result


def _private_regular_file(path: Path) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink() or not raw.is_file():
        raise ValidationError("GUI worker request must be a private regular file")
    try:
        info = raw.stat()
    except OSError as exc:
        raise ValidationError("GUI worker request is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValidationError("GUI worker request must be a private regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValidationError("GUI worker request permissions are too broad")
    return raw.resolve(strict=True)


def _absolute_repository(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError("GUI worker repository is invalid")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValidationError("GUI worker repository is unavailable")
    return path.resolve(strict=True)


def _valid_prompt(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValidationError("GUI message must be non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("GUI message must be valid UTF-8 text") from exc
    if len(encoded) > MAX_PROMPT_BYTES:
        raise ValidationError("GUI message exceeds the size limit")
    return value


def parse_request(argv: Sequence[str] | None = None) -> WorkerRequest:
    """Parse the closed argv and request-file boundary."""

    args = _parser().parse_args(list(argv) if argv is not None else None)
    request_path = _private_regular_file(Path(args.request))
    try:
        value = json.loads(
            read_text_limited(request_path, MAX_REQUEST_BYTES),
            object_pairs_hook=_object_without_duplicates,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("GUI worker request must be valid JSON") from exc
    fields = {
        "schema",
        "version",
        "project_id",
        "turn_id",
        "prompt",
        "repository",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("GUI worker request has unexpected fields")
    if value["schema"] != REQUEST_SCHEMA or value["version"] != WORKER_VERSION:
        raise ValidationError("unsupported GUI worker request schema/version")
    project_id = value["project_id"]
    turn_id = value["turn_id"]
    if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
        raise ValidationError("GUI worker project id is invalid")
    if not isinstance(turn_id, str) or _IDENTIFIER.fullmatch(turn_id) is None:
        raise ValidationError("GUI worker turn id is invalid")
    turn_dir = request_path.parent
    if turn_dir.is_symlink() or not turn_dir.is_dir():
        raise ValidationError("GUI worker turn directory is unavailable")
    return WorkerRequest(
        project_id=project_id,
        turn_id=turn_id,
        prompt=_valid_prompt(value["prompt"]),
        repository=_absolute_repository(value["repository"]),
        request_path=request_path,
        result_path=turn_dir / "result.json",
        event_dir=turn_dir / "events",
    )


def _bounded_text(value: object, limit: int) -> str:
    text = sanitize_user_text(value if isinstance(value, str) else "")
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    suffix = "\n[response truncated]"
    room = max(0, limit - len(suffix.encode("utf-8")))
    return encoded[:room].decode("utf-8", errors="ignore") + suffix


class _WorkerEventWriter:
    """Append-only, fixed-shape side channel for safe lifecycle facts."""

    def __init__(self, request: WorkerRequest) -> None:
        self.project_id = request.project_id
        self.turn_id = request.turn_id
        self.directory = make_directory(request.event_dir)
        self._ordinal = 0

    def emit(
        self,
        kind: str,
        state: str,
        *,
        tool: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        allowed = {
            "model": {"started", "completed", "failed"},
            "tool": {"started", "completed", "failed"},
        }
        if self._ordinal >= MAX_WORKER_EVENTS:
            return
        if kind not in allowed or state not in allowed[kind]:
            return
        event: dict[str, Any] = {
            "schema": EVENT_SCHEMA,
            "version": WORKER_VERSION,
            "project_id": self.project_id,
            "turn_id": self.turn_id,
            "ordinal": self._ordinal + 1,
            "kind": kind,
            "state": state,
            "created_at": utc_timestamp(),
        }
        if kind == "tool":
            if not isinstance(tool, str) or _PCB_TOOL.fullmatch(tool) is None:
                return
            event["tool"] = tool
            if isinstance(duration_ms, int) and not isinstance(duration_ms, bool):
                event["duration_ms"] = min(max(duration_ms, 0), 86_400_000)
        self._ordinal += 1
        atomic_write_json(
            self.directory / f"{self._ordinal:08d}.json",
            event,
        )


def _install_safe_observer(writer: _WorkerEventWriter) -> Any:
    """Register narrow callbacks that cannot receive raw Hermes payloads."""

    from pcbdraft.agent.extensions.manager import (
        PluginContext,
        PluginManifest,
        get_plugin_manager,
    )

    context = PluginContext(
        PluginManifest(
            name="pcbdraft-gui-safe-events",
            version="1.0",
            provides_hooks=[
                "pre_api_request",
                "post_api_request",
                "api_request_error",
                "pre_tool_call",
                "post_tool_call",
            ],
        ),
        get_plugin_manager(),
    )

    def model_started(api_call_count: int = 0) -> None:
        del api_call_count
        writer.emit("model", "started")

    def model_completed(api_duration: float = 0.0) -> None:
        del api_duration
        writer.emit("model", "completed")

    def model_failed(api_call_count: int = 0) -> None:
        del api_call_count
        writer.emit("model", "failed")

    def tool_started(tool_name: str = "") -> None:
        writer.emit("tool", "started", tool=tool_name)

    def tool_completed(
        tool_name: str = "", duration_ms: int = 0, status: str | None = None
    ) -> None:
        writer.emit(
            "tool",
            "failed" if status not in {None, "ok", "success"} else "completed",
            tool=tool_name,
            duration_ms=duration_ms,
        )

    context.register_hook("pre_api_request", model_started)
    context.register_hook("post_api_request", model_completed)
    context.register_hook("api_request_error", model_failed)
    context.register_hook("pre_tool_call", tool_started)
    context.register_hook("post_tool_call", tool_completed)
    return context


def _load_runtime() -> _WorkerRuntime:
    """Load only the current PCBDraft/vendored-Hermes product path."""

    from pcbdraft.agent.tool_bindings import _set_service, set_current_project_id
    from pcbdraft.interfaces.terminal import activate
    from pcbdraft.services.application import ApplicationService
    from pcbdraft.services.provider_connection import connection_status

    def run_agent(prompt: str) -> str:
        from pcbdraft.agent.session_context import declare_stateless_channel
        from pcbdraft.interfaces.tui.oneshot import _run_agent

        declare_stateless_channel()
        response, _result = _run_agent(
            prompt,
            toolsets=["pcbdraft"],
            max_iterations=MODEL_TURN_LIMIT,
            use_config_toolsets=False,
        )
        return response

    return _WorkerRuntime(
        create_service=lambda path: ApplicationService(path, recover_interrupted=False),
        pin_service=_set_service,
        bind_project=set_current_project_id,
        activate=lambda: activate(permission_mode="workspace"),
        provider_is_usable=lambda: bool(connection_status().usable),
        run_agent=run_agent,
        install_observer=_install_safe_observer,
    )


def _write_result(
    request: WorkerRequest,
    *,
    status: str,
    final_response: str = "",
    error_code: str | None = None,
) -> None:
    value: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "version": WORKER_VERSION,
        "project_id": request.project_id,
        "turn_id": request.turn_id,
        "status": status,
        "final_response": _bounded_text(final_response, MAX_RESPONSE_BYTES),
        "error_code": error_code,
        "completed_at": utc_timestamp(),
    }
    atomic_write_json(request.result_path, value)


def run_worker(
    request: WorkerRequest,
    *,
    runtime_loader: Callable[[], _WorkerRuntime] = _load_runtime,
) -> int:
    """Run one bound GUI turn and write a structured, bounded result."""

    os.environ.update(
        {
            "PCBDRAFT_DEBUG_TRACE": "0",
            "PCBDRAFT_RUNTIME_YOLO_MODE": "1",
            "PCBDRAFT_RUNTIME_ACCEPT_HOOKS": "1",
            "PCBDRAFT_RUNTIME_SINGLE_QUERY_SESSION": "1",
            "PCBDRAFT_PCB_TOOL_CALL_LIMIT": "500",
            "NO_COLOR": "1",
        }
    )
    writer = _WorkerEventWriter(request)
    runtime: _WorkerRuntime | None = None
    try:
        runtime = runtime_loader()
        service = runtime.create_service(request.repository)
        service.project_root(request.project_id)
        runtime.pin_service(service)
        runtime.bind_project(request.project_id)
        runtime.activate()
        runtime.install_observer(writer)
        if not runtime.provider_is_usable():
            _write_result(request, status="failed", error_code="provider_unavailable")
            return 1
        response = runtime.run_agent(request.prompt)
        _write_result(request, status="completed", final_response=response)
        return 0
    except KeyboardInterrupt:
        _write_result(request, status="cancelled", error_code="cancelled")
        return 130
    except PCBDraftError:
        _write_result(request, status="failed", error_code="pcbdraft_error")
        return 1
    except BaseException:  # noqa: BLE001 - process boundary must never leak a trace
        _write_result(request, status="failed", error_code="worker_error")
        return 1
    finally:
        try:
            if runtime is not None:
                runtime.bind_project(None)
        except Exception:  # noqa: BLE001,S110 - cleanup cannot replace result
            pass


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run_worker(parse_request(argv))
    except PCBDraftError as exc:
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
