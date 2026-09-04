"""Bounded persistent GUI chat sessions backed by isolated Hermes workers."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psutil

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import (
    atomic_write_json,
    load_json_limited,
    make_directory,
)
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.runs import new_run_id, utc_timestamp
from pcbdraft.interfaces.gui_worker import (
    EVENT_SCHEMA,
    MAX_PROMPT_BYTES,
    MAX_RESPONSE_BYTES,
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    WORKER_VERSION,
)

SESSION_SCHEMA = "pcbdraft-gui-session"
SESSION_VERSION = 1
MAX_SESSION_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 96 * 1024
MAX_EVENT_BYTES = 8 * 1024
MAX_MESSAGES = 60
MAX_EVENTS = 1_000
MAX_EVENT_PAGE = 500
MAX_TURN_DIRECTORIES = 20
MAX_WORKER_EVENT_FILES = 2_000

_EVENT_FILE = re.compile(r"[0-9]{8}\.json")
_PCB_TOOL = re.compile(r"pcb_[a-z0-9_]{1,63}")
_TURN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ACTIVE_FIELDS = {
    "turn_id",
    "status",
    "started_at",
    "pid",
    "pid_started",
    "request_path",
    "result_path",
    "event_dir",
    "event_cursor",
    "cancel_requested",
}
_MESSAGE_FIELDS = {"id", "turn_id", "role", "text", "status", "created_at"}
_RESULT_FIELDS = {
    "schema",
    "version",
    "project_id",
    "turn_id",
    "status",
    "final_response",
    "error_code",
    "completed_at",
}
_WORKER_EVENT_REQUIRED = {
    "schema",
    "version",
    "project_id",
    "turn_id",
    "ordinal",
    "kind",
    "state",
    "created_at",
}
_WORKER_EVENT_OPTIONAL = {"tool", "duration_ms"}


def _bounded_text(value: str, limit: int) -> str:
    text = sanitize_user_text(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    suffix = "\n[text truncated]"
    room = max(0, limit - len(suffix.encode("utf-8")))
    return encoded[:room].decode("utf-8", errors="ignore") + suffix


def _validate_message(text: object) -> str:
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise ValidationError("GUI message must be non-empty text")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("GUI message must be valid UTF-8 text") from exc
    if len(encoded) > MAX_PROMPT_BYTES:
        raise ValidationError("GUI message exceeds the size limit")
    return text


def _private_directory(path: Path) -> Path:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValidationError("GUI session cache path is unsafe")
    directory = make_directory(path)
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError("GUI session cache path is unsafe")
    return directory


class GuiSessionManager:
    """Own one fail-fast isolated turn per existing application project.

    Public values are deliberately narrow UI projections. Internal process
    ids, request paths, error details, prompts sent to Hermes, and tool
    arguments/results are never returned by this class.
    """

    def __init__(
        self,
        service: Any,
        cache_root: str | Path | None = None,
        *,
        worker_command: Sequence[str] | None = None,
        stop_grace_seconds: float = 1.0,
    ) -> None:
        self.service = service
        requested = (
            Path(cache_root).expanduser()
            if cache_root is not None
            else Path.home() / ".cache" / "pcbdraft" / "gui"
        )
        if requested.exists() and requested.is_symlink():
            raise ValidationError("GUI cache root must not be a symbolic link")
        self.cache_root = _private_directory(requested.resolve(strict=False))
        self._projects_root = _private_directory(self.cache_root / "projects")
        self._locks_root = _private_directory(self.cache_root / "locks")
        self._worker_command = tuple(
            worker_command or (sys.executable, "-m", "pcbdraft.interfaces.gui_worker")
        )
        if not self._worker_command or any(not item for item in self._worker_command):
            raise ValidationError("GUI worker command is invalid")
        if not 0.01 <= stop_grace_seconds <= 10.0:
            raise ValidationError("GUI worker stop grace is invalid")
        self._stop_grace_seconds = float(stop_grace_seconds)
        self._handles: dict[tuple[str, str], subprocess.Popen[bytes]] = {}
        self._thread_lock = threading.RLock()

    def start(self, project_id: str, text: str) -> dict[str, Any]:
        """Start one non-blocking turn, failing fast if the project is busy."""

        self.service.project_root(project_id)
        prompt = _validate_message(text)
        with self._thread_lock, self._lock(project_id):
            state = self._load_state(project_id)
            self._harvest_locked(state)
            if state["active"] is not None:
                raise PCBDraftError("project already has an active GUI turn")

            turn_id = new_run_id()
            turns_dir = _private_directory(self._session_dir(project_id) / "turns")
            turn_dir = _private_directory(turns_dir / turn_id)
            request_path = turn_dir / "request.json"
            started_at = utc_timestamp()
            request = {
                "schema": REQUEST_SCHEMA,
                "version": WORKER_VERSION,
                "project_id": project_id,
                "turn_id": turn_id,
                "prompt": prompt,
                "repository": str(Path(self.service.root).resolve(strict=True)),
            }
            atomic_write_json(request_path, request, mode=0o600)
            state["messages"].append(
                {
                    "id": f"{turn_id}-user",
                    "turn_id": turn_id,
                    "role": "user",
                    "text": _bounded_text(prompt, MAX_PROMPT_BYTES),
                    "status": "running",
                    "created_at": started_at,
                }
            )
            state["active"] = {
                "turn_id": turn_id,
                "status": "starting",
                "started_at": started_at,
                "pid": None,
                "pid_started": None,
                "request_path": str(request_path),
                "result_path": str(turn_dir / "result.json"),
                "event_dir": str(turn_dir / "events"),
                "event_cursor": 0,
                "cancel_requested": False,
            }
            self._append_event(state, turn_id, "turn", "started", started_at)
            self._save_state(state)
            try:
                process = self._spawn_worker(request_path)
            except (OSError, ValueError) as exc:
                self._finalize_locked(state, "failed", error_code="spawn_failed")
                self._save_state(state)
                raise PCBDraftError("could not start the GUI agent worker") from exc
            self._handles[(project_id, turn_id)] = process
            active = state["active"]
            if not isinstance(active, dict):  # pragma: no cover - assigned above
                raise PCBDraftError("GUI session worker state was lost")
            active["pid"] = process.pid
            active["pid_started"] = self._process_create_time(process.pid)
            active["status"] = "running"
            self._save_state(state)
        return {
            "project_id": project_id,
            "turn_id": turn_id,
            "status": "running",
            "started_at": started_at,
        }

    def stop(self, project_id: str) -> dict[str, Any]:
        """Cancel the active project worker and its process group."""

        self.service.project_root(project_id)
        with self._thread_lock, self._lock(project_id):
            state = self._load_state(project_id)
            changed = self._harvest_locked(state)
            active = state["active"]
            if active is None:
                if changed:
                    self._save_state(state)
                return {"project_id": project_id, "turn_id": None, "status": "idle"}
            turn_id = active["turn_id"]
            active["status"] = "stopping"
            active["cancel_requested"] = True
            self._append_event(state, turn_id, "turn", "cancel_requested")
            self._save_state(state)
            handle = self._handles.get((project_id, turn_id))
            active_snapshot = dict(active)

        stopped = self._interrupt_worker(handle, active_snapshot)

        with self._thread_lock, self._lock(project_id):
            state = self._load_state(project_id)
            changed = self._harvest_locked(state)
            if (
                state["active"] is not None
                and state["active"].get("turn_id") == turn_id
            ):
                if not stopped and self._active_is_alive(
                    {**state["active"], "project_id": project_id}
                ):
                    if changed:
                        self._save_state(state)
                    raise PCBDraftError("GUI worker did not stop")
                self._finalize_locked(state, "cancelled", error_code="cancelled")
                self._save_state(state)
            elif changed:
                self._save_state(state)
        return {"project_id": project_id, "turn_id": turn_id, "status": "cancelled"}

    def session(self, project_id: str) -> dict[str, Any]:
        """Return bounded reconnect state without internal worker metadata."""

        self.service.project_root(project_id)
        with self._thread_lock, self._lock(project_id):
            state = self._load_state(project_id)
            changed = self._harvest_locked(state)
            if changed:
                self._save_state(state)
            active = state["active"]
            active_public = None
            if active is not None:
                active_public = {
                    "turn_id": active["turn_id"],
                    "status": active["status"],
                    "started_at": active["started_at"],
                }
            return {
                "schema": SESSION_SCHEMA,
                "version": SESSION_VERSION,
                "project_id": project_id,
                "status": active["status"] if active is not None else "idle",
                "active_turn": active_public,
                "messages": [
                    {
                        **message,
                        "text": _bounded_text(message["text"], MAX_RESPONSE_BYTES),
                    }
                    for message in state["messages"]
                ],
                "last_sequence": state["next_sequence"] - 1,
            }

    def events(self, project_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Return up to 500 persisted safe lifecycle events after a sequence."""

        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValidationError("GUI event sequence is invalid")
        self.service.project_root(project_id)
        with self._thread_lock, self._lock(project_id):
            state = self._load_state(project_id)
            changed = self._harvest_locked(state)
            if changed:
                self._save_state(state)
            return copy.deepcopy(
                [event for event in state["events"] if event["sequence"] > after][
                    :MAX_EVENT_PAGE
                ]
            )

    def drain(self, project_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Compatibility name for the same resumable, non-destructive event read."""

        return self.events(project_id, after=after)

    def shutdown(self) -> list[dict[str, Any]]:
        """Cancel workers owned by this manager during application shutdown."""

        summaries: list[dict[str, Any]] = []
        with self._thread_lock:
            projects = sorted({project_id for project_id, _turn in self._handles})
        for project_id in projects:
            try:
                summaries.append(self.stop(project_id))
            except PCBDraftError:
                summaries.append(
                    {"project_id": project_id, "turn_id": None, "status": "failed"}
                )
        return summaries

    def _session_dir(self, project_id: str) -> Path:
        key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        return _private_directory(self._projects_root / key)

    def _lock(self, project_id: str) -> ResourceLock:
        return ResourceLock(
            self._session_dir(project_id),
            self._locks_root,
            timeout=0.25,
        )

    def _state_path(self, project_id: str) -> Path:
        return self._session_dir(project_id) / "session.json"

    def _new_state(self, project_id: str) -> dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA,
            "version": SESSION_VERSION,
            "project_id": project_id,
            "next_sequence": 1,
            "messages": [],
            "events": [],
            "active": None,
        }

    def _load_state(self, project_id: str) -> dict[str, Any]:
        path = self._state_path(project_id)
        if not path.exists():
            return self._new_state(project_id)
        if path.is_symlink() or not path.is_file():
            raise ValidationError("GUI session state is unsafe")
        value = load_json_limited(path, MAX_SESSION_BYTES)
        fields = {
            "schema",
            "version",
            "project_id",
            "next_sequence",
            "messages",
            "events",
            "active",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != SESSION_SCHEMA
            or value["version"] != SESSION_VERSION
            or value["project_id"] != project_id
            or isinstance(value["next_sequence"], bool)
            or not isinstance(value["next_sequence"], int)
            or value["next_sequence"] < 1
            or not isinstance(value["messages"], list)
            or not isinstance(value["events"], list)
            or (value["active"] is not None and not isinstance(value["active"], dict))
        ):
            raise ValidationError("GUI session state is invalid")
        if len(value["messages"]) > MAX_MESSAGES or not all(
            self._valid_message(message) for message in value["messages"]
        ):
            raise ValidationError("GUI session messages are invalid")
        if len(value["events"]) > MAX_EVENTS or not self._valid_stored_events(
            value["events"], project_id, value["next_sequence"]
        ):
            raise ValidationError("GUI session events are invalid")
        if value["active"] is not None and not self._valid_active(
            project_id, value["active"]
        ):
            raise ValidationError("GUI session worker state is invalid")
        return value

    @staticmethod
    def _valid_message(value: Any) -> bool:
        return bool(
            isinstance(value, dict)
            and set(value) == _MESSAGE_FIELDS
            and isinstance(value.get("id"), str)
            and len(value["id"]) <= 256
            and isinstance(value.get("turn_id"), str)
            and _TURN_ID.fullmatch(value["turn_id"]) is not None
            and value.get("role") in {"user", "assistant"}
            and isinstance(value.get("text"), str)
            and len(value["text"].encode("utf-8", errors="replace"))
            <= MAX_RESPONSE_BYTES
            and value.get("status") in {"running", "completed", "failed", "cancelled"}
            and isinstance(value.get("created_at"), str)
            and len(value["created_at"]) <= 64
        )

    @staticmethod
    def _valid_stored_events(
        values: list[Any], project_id: str, next_sequence: int
    ) -> bool:
        previous = 0
        allowed = {
            "turn": {
                "started",
                "cancel_requested",
                "completed",
                "failed",
                "cancelled",
            },
            "model": {"started", "completed", "failed"},
            "tool": {"started", "completed", "failed"},
        }
        for value in values:
            if not isinstance(value, dict):
                return False
            required = {
                "sequence",
                "project_id",
                "turn_id",
                "kind",
                "state",
                "created_at",
            }
            if not required <= set(value) <= (required | {"tool", "duration_ms"}):
                return False
            sequence = value.get("sequence")
            kind = value.get("kind")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence <= previous
                or sequence >= next_sequence
                or value.get("project_id") != project_id
                or not isinstance(value.get("turn_id"), str)
                or _TURN_ID.fullmatch(value["turn_id"]) is None
                or kind not in allowed
                or value.get("state") not in allowed[kind]
                or not isinstance(value.get("created_at"), str)
                or len(value["created_at"]) > 64
            ):
                return False
            tool = value.get("tool")
            duration = value.get("duration_ms")
            if kind == "tool":
                if not isinstance(tool, str) or _PCB_TOOL.fullmatch(tool) is None:
                    return False
            elif tool is not None or duration is not None:
                return False
            if duration is not None and (
                isinstance(duration, bool)
                or not isinstance(duration, int)
                or not 0 <= duration <= 86_400_000
            ):
                return False
            previous = sequence
        return (not values and next_sequence == 1) or (
            bool(values) and previous == next_sequence - 1
        )

    def _valid_active(self, project_id: str, value: dict[str, Any]) -> bool:
        if set(value) != _ACTIVE_FIELDS:
            return False
        turn_id = value.get("turn_id")
        pid = value.get("pid")
        pid_started = value.get("pid_started")
        cursor = value.get("event_cursor")
        if (
            not isinstance(turn_id, str)
            or _TURN_ID.fullmatch(turn_id) is None
            or value.get("status") not in {"starting", "running", "stopping"}
            or not isinstance(value.get("started_at"), str)
            or len(value["started_at"]) > 64
            or (
                pid is not None
                and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0)
            )
            or (
                pid_started is not None
                and (
                    isinstance(pid_started, bool)
                    or not isinstance(pid_started, (int, float))
                    or pid_started <= 0
                )
            )
            or isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not 0 <= cursor <= MAX_WORKER_EVENT_FILES
            or not isinstance(value.get("cancel_requested"), bool)
        ):
            return False
        turn_dir = self._session_dir(project_id) / "turns" / turn_id
        if turn_dir.is_symlink() or not turn_dir.is_dir():
            return False
        expected = {
            "request_path": turn_dir / "request.json",
            "result_path": turn_dir / "result.json",
            "event_dir": turn_dir / "events",
        }
        return all(
            isinstance(value.get(field), str)
            and Path(value[field]).is_absolute()
            and Path(value[field]) == path
            for field, path in expected.items()
        )

    def _save_state(self, state: dict[str, Any]) -> None:
        state["messages"] = state["messages"][-MAX_MESSAGES:]
        state["events"] = state["events"][-MAX_EVENTS:]
        atomic_write_json(self._state_path(state["project_id"]), state, mode=0o600)

    def _append_event(
        self,
        state: dict[str, Any],
        turn_id: str,
        kind: str,
        event_state: str,
        created_at: str | None = None,
        *,
        tool: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "sequence": state["next_sequence"],
            "project_id": state["project_id"],
            "turn_id": turn_id,
            "kind": kind,
            "state": event_state,
            "created_at": created_at or utc_timestamp(),
        }
        if tool is not None:
            event["tool"] = tool
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        state["next_sequence"] += 1
        state["events"].append(event)

    def _spawn_worker(self, request_path: Path) -> subprocess.Popen[bytes]:
        command = [*self._worker_command, "--request", str(request_path)]
        environment = dict(os.environ)
        environment.update({"PCBDRAFT_DEBUG_TRACE": "0", "NO_COLOR": "1"})
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "close_fds": True,
            "cwd": str(Path(self.service.root).resolve(strict=True)),
            "env": environment,
        }
        if sys.platform == "win32":  # pragma: no cover - Windows CI seam
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        # Command components are constructor-owned, never accepted from HTTP.
        return subprocess.Popen(command, **kwargs)  # noqa: S603

    @staticmethod
    def _process_create_time(pid: int) -> float | None:
        try:
            return float(psutil.Process(pid).create_time())
        except (psutil.Error, OSError, ValueError):
            return None

    def _active_is_alive(self, active: dict[str, Any]) -> bool:
        turn_id = active.get("turn_id")
        project_id = active.get("project_id")
        if isinstance(project_id, str) and isinstance(turn_id, str):
            handle = self._handles.get((project_id, turn_id))
            if handle is not None:
                return handle.poll() is None
        pid = active.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        try:
            process = psutil.Process(pid)
            expected = active.get("pid_started")
            if (
                isinstance(expected, (int, float))
                and abs(process.create_time() - float(expected)) > 0.01
            ):
                return False
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.Error, OSError, ValueError):
            return False

    def _harvest_worker_events(
        self, state: dict[str, Any], active: dict[str, Any]
    ) -> bool:
        directory = Path(active["event_dir"])
        if not directory.exists() or directory.is_symlink() or not directory.is_dir():
            return False
        cursor = active["event_cursor"]
        changed = False
        paths = sorted(
            (
                path
                for path in directory.iterdir()
                if _EVENT_FILE.fullmatch(path.name) is not None
            ),
            key=lambda item: item.name,
        )[:MAX_WORKER_EVENT_FILES]
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            ordinal = int(path.stem)
            if ordinal <= cursor:
                continue
            try:
                value = load_json_limited(path, MAX_EVENT_BYTES)
            except PCBDraftError:
                value = None
            if not self._valid_worker_event(
                value,
                state["project_id"],
                active["turn_id"],
                expected_ordinal=ordinal,
            ):
                cursor = max(cursor, ordinal)
                changed = True
                continue
            self._append_event(
                state,
                active["turn_id"],
                value["kind"],
                value["state"],
                value["created_at"],
                tool=value.get("tool"),
                duration_ms=value.get("duration_ms"),
            )
            cursor = max(cursor, ordinal)
            changed = True
        active["event_cursor"] = cursor
        return changed

    @staticmethod
    def _valid_worker_event(
        value: Any, project_id: str, turn_id: str, *, expected_ordinal: int
    ) -> bool:
        if not isinstance(value, dict):
            return False
        if (
            not _WORKER_EVENT_REQUIRED
            <= set(value)
            <= (_WORKER_EVENT_REQUIRED | _WORKER_EVENT_OPTIONAL)
        ):
            return False
        allowed = {
            "model": {"started", "completed", "failed"},
            "tool": {"started", "completed", "failed"},
        }
        if (
            value.get("schema") != EVENT_SCHEMA
            or value.get("version") != WORKER_VERSION
            or value.get("project_id") != project_id
            or value.get("turn_id") != turn_id
            or value.get("kind") not in allowed
            or value.get("state") not in allowed[value["kind"]]
            or not isinstance(value.get("created_at"), str)
            or isinstance(value.get("ordinal"), bool)
            or not isinstance(value.get("ordinal"), int)
            or value.get("ordinal") != expected_ordinal
        ):
            return False
        if value["kind"] == "tool":
            if (
                not isinstance(value.get("tool"), str)
                or _PCB_TOOL.fullmatch(value["tool"]) is None
            ):
                return False
        elif "tool" in value or "duration_ms" in value:
            return False
        duration = value.get("duration_ms")
        return duration is None or (
            isinstance(duration, int)
            and not isinstance(duration, bool)
            and 0 <= duration <= 86_400_000
        )

    def _harvest_locked(self, state: dict[str, Any]) -> bool:
        active = state["active"]
        if active is None:
            return False
        active["project_id"] = state["project_id"]
        changed = self._harvest_worker_events(state, active)
        result_path = Path(active["result_path"])
        if (
            result_path.exists()
            and not result_path.is_symlink()
            and result_path.is_file()
        ):
            try:
                result = load_json_limited(result_path, MAX_RESULT_BYTES)
            except PCBDraftError:
                result = None
            if self._valid_result(result, state["project_id"], active["turn_id"]):
                status = result["status"]
                if active.get("cancel_requested"):
                    status = "cancelled"
                self._finalize_locked(
                    state,
                    status,
                    final_response=result["final_response"],
                    error_code=result["error_code"],
                    completed_at=result["completed_at"],
                )
                return True
        if not self._active_is_alive(active):
            self._finalize_locked(
                state,
                "cancelled" if active.get("cancel_requested") else "failed",
                error_code=(
                    "cancelled" if active.get("cancel_requested") else "worker_exit"
                ),
            )
            return True
        active.pop("project_id", None)
        return changed

    @staticmethod
    def _valid_result(value: Any, project_id: str, turn_id: str) -> bool:
        return bool(
            isinstance(value, dict)
            and set(value) == _RESULT_FIELDS
            and value.get("schema") == RESULT_SCHEMA
            and value.get("version") == WORKER_VERSION
            and value.get("project_id") == project_id
            and value.get("turn_id") == turn_id
            and value.get("status") in {"completed", "cancelled", "failed"}
            and isinstance(value.get("final_response"), str)
            and isinstance(value.get("completed_at"), str)
            and (
                value.get("error_code") is None
                or (
                    isinstance(value.get("error_code"), str)
                    and len(value["error_code"]) <= 64
                )
            )
        )

    def _finalize_locked(
        self,
        state: dict[str, Any],
        status: str,
        *,
        final_response: str = "",
        error_code: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        del error_code
        active = state["active"]
        if active is None:
            return
        turn_id = active["turn_id"]
        timestamp = completed_at or utc_timestamp()
        for message in state["messages"]:
            if message.get("turn_id") == turn_id and message.get("role") == "user":
                message["status"] = status
        if status == "completed":
            state["messages"].append(
                {
                    "id": f"{turn_id}-assistant",
                    "turn_id": turn_id,
                    "role": "assistant",
                    "text": _bounded_text(final_response, MAX_RESPONSE_BYTES),
                    "status": "completed",
                    "created_at": timestamp,
                }
            )
        self._append_event(state, turn_id, "turn", status, timestamp)
        request_path = Path(active["request_path"])
        try:
            if request_path.is_file() and not request_path.is_symlink():
                request_path.unlink()
        except OSError:
            pass
        self._handles.pop((state["project_id"], turn_id), None)
        state["active"] = None
        self._prune_turn_directories(state["project_id"])

    def _prune_turn_directories(self, project_id: str) -> None:
        turns = self._session_dir(project_id) / "turns"
        if not turns.is_dir() or turns.is_symlink():
            return
        candidates = sorted(
            (
                path
                for path in turns.iterdir()
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: path.name,
        )
        for path in candidates[:-MAX_TURN_DIRECTORIES]:
            try:
                shutil.rmtree(path)
            except OSError:
                continue

    def _interrupt_worker(
        self,
        handle: subprocess.Popen[bytes] | None,
        active: dict[str, Any],
    ) -> bool:
        pid = active.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        if handle is None and not self._safe_external_worker(active):
            raise PCBDraftError("cannot safely identify the GUI worker process")
        if handle is not None and handle.pid != pid:
            raise PCBDraftError("GUI worker process identity changed")
        if handle is not None and handle.poll() is not None:
            return True
        if sys.platform == "win32":  # pragma: no cover - Windows CI seam
            try:
                if handle is not None:
                    handle.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    psutil.Process(pid).terminate()
            except (OSError, psutil.Error):
                return False
        else:
            try:
                os.killpg(pid, signal.SIGINT)
            except ProcessLookupError:
                return True
            except OSError:
                return False
        if self._wait_for_exit(handle, pid, self._stop_grace_seconds):
            return True
        try:
            if sys.platform == "win32":  # pragma: no cover
                psutil.Process(pid).terminate()
            else:
                os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except (OSError, psutil.Error):
            return False
        if self._wait_for_exit(handle, pid, self._stop_grace_seconds / 2):
            return True
        try:
            if sys.platform == "win32":  # pragma: no cover
                psutil.Process(pid).kill()
            else:
                os.killpg(pid, signal.SIGKILL)
        except (OSError, psutil.Error):
            return False
        return self._wait_for_exit(handle, pid, self._stop_grace_seconds / 2)

    @staticmethod
    def _wait_for_exit(
        handle: subprocess.Popen[bytes] | None, pid: int, timeout: float
    ) -> bool:
        if handle is not None:
            try:
                handle.wait(timeout=timeout)
                return True
            except subprocess.TimeoutExpired:
                return False
        try:
            psutil.Process(pid).wait(timeout=timeout)
            return True
        except psutil.TimeoutExpired:
            return False
        except psutil.Error:
            return True

    @staticmethod
    def _safe_external_worker(active: dict[str, Any]) -> bool:
        pid = active.get("pid")
        try:
            process = psutil.Process(pid)
            expected = active.get("pid_started")
            if (
                not isinstance(expected, (int, float))
                or abs(process.create_time() - float(expected)) > 0.01
            ):
                return False
            command = process.cmdline()
        except (psutil.Error, OSError, ValueError, TypeError):
            return False
        return (
            "pcbdraft.interfaces.gui_worker" in command
            and active.get("request_path") in command
        )
