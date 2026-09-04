"""Resident local web application for observing and driving PCBDraft projects.

The browser surface is deliberately a narrow adapter.  Engineering writes remain
owned by :class:`pcbdraft.services.application.ApplicationService`; this module
only serves committed scene projections, external preview-cache artifacts, and
the isolated vendored-Hermes turn manager.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import threading
import urllib.parse
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from pcbdraft import __version__
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited, make_directory
from pcbdraft.core.redaction import sanitize_user_text
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.kicad.runtime import find_kicad_app
from pcbdraft.services.application import ApplicationService

MAX_REQUEST_BYTES = 64 * 1024
MAX_URL_LENGTH = 2_048
MAX_STATIC_BYTES = 4 * 1024 * 1024
MAX_STREAM_EVENTS = 500
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,79}")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_PREFIXES = ("", "/pcbdraft")
_STATIC_ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "tokens.css": "text/css; charset=utf-8",
    "workbench.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "api.js": "text/javascript; charset=utf-8",
    "store.js": "text/javascript; charset=utf-8",
    "i18n.js": "text/javascript; charset=utf-8",
    "commands.js": "text/javascript; charset=utf-8",
    "board.js": "text/javascript; charset=utf-8",
    "projects.js": "text/javascript; charset=utf-8",
    "inspector.js": "text/javascript; charset=utf-8",
    "conversation.js": "text/javascript; charset=utf-8",
}


def _cache_home() -> Path:
    """Return the GUI cache root without placing state in a PCB project."""

    import os
    import platform

    home = Path.home()
    system = platform.system().casefold()
    if system == "windows":
        base = Path(
            os.environ.get("LOCALAPPDATA", "").strip() or home / "AppData/Local"
        )
    elif system == "darwin":
        base = home / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", "").strip() or home / ".cache")
    return base.expanduser() / "pcbdraft" / "gui"


def _safe_project_id(value: str) -> str:
    if _PROJECT_ID.fullmatch(value) is None:
        raise ValidationError("project id is invalid")
    return value


def _bounded_project_list(service: ApplicationService) -> list[dict[str, Any]]:
    """Whitelist summary fields instead of forwarding an evolving service view."""

    result: list[dict[str, Any]] = []
    for value in service.list_projects()[:500]:
        if not isinstance(value, Mapping):
            continue
        project_id = value.get("id")
        if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
            continue
        result.append(
            {
                "id": project_id,
                "name": str(value.get("name", ""))[:256],
                "status": str(value.get("status", "unknown"))[:64],
                "updated_at": str(value.get("updated_at", ""))[:64],
                "design_revision": value.get("design_revision", 0),
            }
        )
    return result


def _open_project_in_kicad(
    service: ApplicationService, project_id: str
) -> dict[str, Any]:
    """Launch only the selected application's own generated board file."""

    project_id = _safe_project_id(project_id)
    view = service.open_project(project_id)
    design = view.get("design") if isinstance(view, Mapping) else None
    managed_files = design.get("files") if isinstance(design, Mapping) else None
    board_value = (
        managed_files.get("board") if isinstance(managed_files, Mapping) else None
    )
    if not isinstance(board_value, str) or not board_value or "\x00" in board_value:
        raise PCBDraftError("selected project has no generated KiCad board")

    project_root_value = Path(service.project_root(project_id))
    candidate = Path(board_value)
    if (
        not candidate.is_absolute()
        or project_root_value.is_symlink()
        or candidate.is_symlink()
    ):
        raise ValidationError("selected project KiCad board path is unsafe")
    try:
        project_root = project_root_value.resolve(strict=True)
        relative = candidate.relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise ValidationError("selected project KiCad board path is unsafe") from exc
    if not relative.parts or ".." in relative.parts:
        raise ValidationError("selected project KiCad board path is unsafe")
    cursor = project_root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValidationError("selected project KiCad board path is unsafe")
    try:
        board_path = candidate.resolve(strict=True)
    except OSError as exc:
        raise PCBDraftError("selected project KiCad board is unavailable") from exc
    if (
        not board_path.is_relative_to(project_root)
        or not board_path.is_file()
        or board_path.suffix.casefold() != ".kicad_pcb"
    ):
        raise ValidationError("selected project KiCad board path is unsafe")

    executable = find_kicad_app()
    if not executable:
        raise PCBDraftError("KiCad desktop application is unavailable")
    session_options: dict[str, Any] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    try:
        subprocess.Popen(  # noqa: S603 - fixed executable and project-owned argv
            [executable, str(board_path)],
            cwd=board_path.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **session_options,
        )
    except OSError as exc:
        raise PCBDraftError("failed to start KiCad desktop application") from exc
    return {"opened": True, "project_id": project_id}


@dataclass
class _StreamState:
    stream_id: str = field(default_factory=lambda: secrets.token_hex(16))
    next_sequence: int = 1
    application_cursor: int = 0
    scene_token: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class GUIEventBroker:
    """Merge durable safe sources into one resumable, bounded GUI sequence."""

    def __init__(
        self,
        service: ApplicationService,
        live_view: Any,
        sessions: Any,
        cache_root: Path,
    ) -> None:
        self.service = service
        self.live_view = live_view
        self.sessions = sessions
        self.cache_root = make_directory(cache_root)
        self._states: dict[str, _StreamState] = {}
        self._lock = threading.RLock()

    def _path(self, project_id: str) -> Path:
        directory = self.cache_root / _safe_project_id(project_id)
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise ValidationError("GUI event cache path is unsafe")
        return directory / "stream.json"

    def _load(self, project_id: str) -> _StreamState:
        state = self._states.get(project_id)
        if state is not None:
            return state
        path = self._path(project_id)
        state = _StreamState()
        if path.is_file() and not path.is_symlink():
            try:
                value = load_json_limited(path, 2 * 1024 * 1024)
                if (
                    not isinstance(value, dict)
                    or value.get("schema") != "pcbdraft-gui-event-stream"
                    or value.get("version") != 2
                ):
                    raise ValidationError("GUI event cache schema is obsolete")
                events = value.get("events") if isinstance(value, dict) else None
                if isinstance(events, list):
                    safe_events = [
                        item
                        for item in events[-MAX_STREAM_EVENTS:]
                        if isinstance(item, dict)
                        and isinstance(item.get("sequence"), int)
                        and item["sequence"] > 0
                    ]
                    state.events = safe_events
                    state.next_sequence = max(
                        int(value.get("next_sequence", 1)),
                        (safe_events[-1]["sequence"] + 1 if safe_events else 1),
                    )
                    state.application_cursor = max(
                        0, int(value.get("application_cursor", 0))
                    )
                    stream_id = value.get("stream_id")
                    if isinstance(stream_id, str) and re.fullmatch(
                        r"[0-9a-f]{32}", stream_id
                    ):
                        state.stream_id = stream_id
                    token = value.get("scene_token")
                    state.scene_token = token if isinstance(token, str) else None
            except (OSError, PCBDraftError, TypeError, ValueError):
                state = _StreamState()
        self._states[project_id] = state
        return state

    def _persist(self, project_id: str, state: _StreamState) -> None:
        path = self._path(project_id)
        directory = make_directory(path.parent)
        if (
            directory.is_symlink()
            or directory.resolve(strict=True).parent != self.cache_root
        ):
            raise ValidationError("GUI cache path is unsafe")
        atomic_write_json(
            path,
            {
                "schema": "pcbdraft-gui-event-stream",
                "version": 2,
                "stream_id": state.stream_id,
                "next_sequence": state.next_sequence,
                "application_cursor": state.application_cursor,
                "scene_token": state.scene_token,
                "events": state.events[-MAX_STREAM_EVENTS:],
            },
        )

    @staticmethod
    def _safe_event(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
        """Copy only presentation facts; never copy args, results, or prompts."""

        kind = str(value.get("kind", value.get("event", "activity")))[:96]
        level = str(value.get("level", value.get("status", "info")))[:32]
        state = value.get("state")
        tool = value.get("tool")
        if isinstance(tool, str) and isinstance(state, str):
            message = f"PCB tool {state}"
        elif kind == "turn" and isinstance(state, str):
            message = f"Agent turn {state.replace('_', ' ')}"
        elif kind == "model" and isinstance(state, str):
            message = f"Model request {state}"
        else:
            # Application event messages are intentionally not forwarded: an
            # evolving producer could include user text or an internal error.
            # The allowlisted event kind still gives the timeline a useful,
            # stable lifecycle label.
            message = kind.replace(".", " ").replace("_", " ").strip().capitalize()
        event: dict[str, Any] = {
            "kind": kind,
            "message": message[:2_048],
            "level": level,
            "created_at": str(value.get("created_at", utc_timestamp()))[:64],
            "source": source,
            "canonical_revision": (
                value.get("canonical_revision")
                if isinstance(value.get("canonical_revision"), int)
                else None
            ),
            "design_revision": (
                value.get("design_revision")
                if isinstance(value.get("design_revision"), int)
                else None
            ),
            "content_hash": (
                value.get("design_content_hash")
                if isinstance(value.get("design_content_hash"), str)
                and re.fullmatch(r"[0-9a-f]{64}", value["design_content_hash"])
                else None
            ),
            "binding_state": (
                "bound"
                if isinstance(value.get("design_content_hash"), str)
                and re.fullmatch(r"[0-9a-f]{64}", value["design_content_hash"])
                else "legacy_unbound"
            ),
        }
        for key in ("tool", "turn_id", "state"):
            item = value.get(key)
            if isinstance(item, str):
                event[key] = item[:128]
        duration = value.get("duration_ms")
        if (
            isinstance(duration, int)
            and not isinstance(duration, bool)
            and duration >= 0
        ):
            event["duration_ms"] = min(duration, 86_400_000)
        return event

    @staticmethod
    def _append(state: _StreamState, value: dict[str, Any]) -> None:
        value["sequence"] = state.next_sequence
        value["stream_id"] = state.stream_id
        state.next_sequence += 1
        state.events.append(value)
        del state.events[:-MAX_STREAM_EVENTS]

    def poll(self, project_id: str) -> list[dict[str, Any]]:
        project_id = _safe_project_id(project_id)
        with self._lock:
            state = self._load(project_id)
            changed = False
            for value in self.service.events(
                project_id, after=state.application_cursor
            ):
                sequence = value.get("sequence") if isinstance(value, dict) else None
                if (
                    not isinstance(sequence, int)
                    or sequence <= state.application_cursor
                ):
                    continue
                state.application_cursor = sequence
                self._append(state, self._safe_event(value, source="project"))
                changed = True

            external_change: Mapping[str, Any] | None = None
            try:
                scene = self.live_view.snapshot(project_id, timeout=0.0)
            except PCBDraftError:
                callback = getattr(self.service, "external_kicad_change_status", None)
                candidate = callback(project_id) if callable(callback) else None
                if not isinstance(candidate, Mapping) or not candidate.get(
                    "requires_import"
                ):
                    raise
                external_change = candidate
                scene = None
            if external_change is not None:
                token = ":".join(
                    (
                        "external",
                        str(external_change.get("canonical_revision", 0)),
                        str(external_change.get("board_sha256", "unknown")),
                        str(external_change.get("state", "unknown")),
                    )
                )
                if token != state.scene_token:
                    state.scene_token = token
                    self._append(
                        state,
                        {
                            "kind": "external_revision.detected",
                            "message": "External native changes require explicit review and import",
                            "level": "warning",
                            "created_at": utc_timestamp(),
                            "source": "scene",
                            "scene_token": token,
                            "canonical_revision": external_change.get(
                                "canonical_revision"
                            ),
                            "design_revision": external_change.get("design_revision"),
                            "content_hash": external_change.get("content_hash"),
                            "binding_state": "bound",
                        },
                    )
                    changed = True
            if scene is not None:
                token = ":".join(
                    (
                        str(scene.get("state_revision", 0)),
                        str(scene.get("design_revision", 0)),
                        str(scene.get("geometry_revision", 0)),
                        str(scene.get("content_hash", "")),
                    )
                )
                if token != state.scene_token:
                    previous = state.scene_token
                    state.scene_token = token
                    self._append(
                        state,
                        {
                            "kind": "scene.committed",
                            "message": "Committed board scene updated",
                            "level": "info",
                            "created_at": utc_timestamp(),
                            "source": "scene",
                            "scene_token": token,
                            "previous_scene_token": previous,
                            "canonical_revision": scene.get("state_revision"),
                            "design_revision": scene.get("design_revision"),
                            "content_hash": scene.get("content_hash"),
                            "binding_state": "bound",
                        },
                    )
                    changed = True
            if changed:
                self._persist(project_id, state)
            return list(state.events)

    def after(self, project_id: str, sequence: int) -> list[dict[str, Any]]:
        if sequence < 0:
            raise ValidationError("event cursor must be non-negative")
        events = self.poll(project_id)
        with self._lock:
            state = self._load(project_id)
            oldest = int(events[0]["sequence"]) if events else state.next_sequence
            latest = state.next_sequence - 1
            if sequence > latest or (sequence > 0 and sequence < oldest - 1):
                if sequence > latest:
                    state.next_sequence = sequence + 1
                self._append(
                    state,
                    {
                        "kind": "stream.reset_required",
                        "message": "Event cursor cannot be resumed; fetch a complete snapshot",
                        "level": "warning",
                        "created_at": utc_timestamp(),
                        "source": "stream",
                        **self._scene_binding(project_id),
                    },
                )
                self._persist(project_id, state)
                return [state.events[-1]]
        return [event for event in events if int(event["sequence"]) > sequence]

    def cursor(self, project_id: str) -> dict[str, Any]:
        self.poll(project_id)
        with self._lock:
            state = self._load(project_id)
            return {
                "stream_id": state.stream_id,
                "last_sequence": state.next_sequence - 1,
                "oldest_sequence": (
                    state.events[0]["sequence"] if state.events else None
                ),
            }

    def _scene_binding(self, project_id: str) -> dict[str, Any]:
        try:
            scene = self.live_view.snapshot(project_id, timeout=0.0)
        except PCBDraftError:
            callback = getattr(self.service, "external_kicad_change_status", None)
            status = callback(project_id) if callable(callback) else None
            if isinstance(status, Mapping) and status.get("requires_import"):
                return {
                    "canonical_revision": status.get("canonical_revision"),
                    "design_revision": status.get("design_revision"),
                    "content_hash": status.get("content_hash"),
                    "binding_state": "external_change_pending",
                }
            raise
        if scene is None:
            return {
                "canonical_revision": None,
                "design_revision": None,
                "content_hash": None,
                "binding_state": "temporarily_unavailable",
            }
        return {
            "canonical_revision": scene.get("state_revision"),
            "design_revision": scene.get("design_revision"),
            "content_hash": scene.get("content_hash"),
            "binding_state": "bound",
        }


@dataclass
class GUIRuntime:
    service: ApplicationService
    live_view: Any
    previews: Any
    artifacts: Any
    ipc: Any
    sessions: Any
    open_in_kicad: Callable[[str], dict[str, Any]]
    events: GUIEventBroker
    cache_root: Path
    csrf_token: str
    initial_project: str | None
    last_scenes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def snapshot(self, project_id: str) -> dict[str, Any]:
        external_change: dict[str, Any] | None = None
        status_callback = getattr(self.service, "external_kicad_change_status", None)
        if callable(status_callback):
            external_change = status_callback(project_id)
        blocked_by_external = bool(
            external_change and external_change.get("requires_import")
        )
        try:
            scene = (
                None
                if blocked_by_external
                else self.live_view.snapshot(project_id, timeout=0.0)
            )
        except PCBDraftError:
            if external_change is None or not external_change.get("requires_import"):
                raise
            scene = None
        busy = scene is None
        if scene is not None:
            self.last_scenes[project_id] = scene
        else:
            scene = self.last_scenes.get(project_id)
        ipc = (
            self.ipc.poll(scene)
            if scene is not None and not blocked_by_external
            else {
                "status": {
                    "state": "blocked_external_change"
                    if blocked_by_external
                    else "temporarily_unavailable",
                    "message": "External native changes require explicit import"
                    if blocked_by_external
                    else "Committed scene is temporarily unavailable",
                    "read_only": True,
                }
            }
        )
        binding_source: Mapping[str, Any] = scene or external_change or {}
        return {
            "schema": "pcbdraft-gui-snapshot",
            "version": 2,
            "busy": busy,
            "scene": scene,
            "ipc": ipc,
            "session": self.sessions.session(project_id),
            "external_change": external_change,
            "binding": {
                "canonical_revision": binding_source.get(
                    "state_revision", binding_source.get("canonical_revision")
                ),
                "design_revision": binding_source.get("design_revision"),
                "content_hash": binding_source.get("content_hash"),
            },
        }


def _host_name(value: str) -> str | None:
    if not value or len(value) > 512 or "@" in value or "/" in value or "\\" in value:
        return None
    try:
        parsed = urllib.parse.urlsplit(f"//{value}")
        return parsed.hostname.casefold() if parsed.hostname else None
    except ValueError:
        return None


def _bind_host_name(value: str) -> str | None:
    """Accept only an unadorned loopback IP or ``localhost`` for binding."""

    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if value.casefold() == "localhost":
        return "localhost"
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    return address.compressed.casefold() if address.is_loopback else None


def _origin_matches_host(origin: str, host: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.netloc.casefold() == host.casefold()
    )


def _route_paths(suffix: str) -> Iterable[str]:
    for prefix in _PREFIXES:
        yield f"{prefix}{suffix}"


def _artifact_binding(
    request: Request, scene: Mapping[str, Any] | None
) -> tuple[int, str]:
    if scene is None:
        raise PCBDraftError("committed artifact binding is temporarily unavailable")
    revision = scene.get("design_revision")
    content_hash = scene.get("content_hash")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not isinstance(content_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
    ):
        raise PCBDraftError("selected project has no committed artifact binding")
    requested_revision = request.query_params.get("revision")
    requested_hash = request.query_params.get("content_hash")
    if (requested_revision is None) != (requested_hash is None):
        raise ValidationError(
            "artifact revision and content hash must be supplied together"
        )
    if requested_revision is not None:
        try:
            parsed_revision = int(requested_revision)
        except ValueError as exc:
            raise ValidationError("artifact revision is invalid") from exc
        if parsed_revision != revision or requested_hash != content_hash:
            raise PCBDraftError("requested artifact binding is stale")
    return revision, content_hash


def _add_route(
    app: FastAPI,
    suffix: str,
    endpoint: Callable[..., Any],
    *,
    methods: list[str],
    name: str,
) -> None:
    for index, path in enumerate(_route_paths(suffix)):
        app.add_api_route(
            path,
            endpoint,
            methods=methods,
            name=f"{name}-{index}",
            include_in_schema=index == 0,
        )


def _asset_response(name: str, media_type: str) -> Response:
    resource = files("pcbdraft").joinpath("web", name)
    data = resource.read_bytes()
    if len(data) > MAX_STATIC_BYTES:
        raise ValidationError("static asset exceeds the size limit")
    if name == "index.html":
        return HTMLResponse(data.decode("utf-8"), media_type="text/html")
    return Response(data, media_type=media_type, headers={"Cache-Control": "no-cache"})


async def _local_security_response(
    request: Request,
    call_next: Callable[..., Any],
    *,
    configured_hosts: set[str],
    csrf_token: str,
) -> Response:
    """Apply the local-only request boundary and common response headers."""

    host = request.headers.get("host", "")
    hostname = _host_name(host)
    if hostname not in configured_hosts:
        response: Response = JSONResponse(
            {"error": {"message": "invalid Host header"}}, status_code=400
        )
    elif len(str(request.url)) > MAX_URL_LENGTH:
        response = JSONResponse(
            {"error": {"message": "request URL is too long"}}, status_code=414
        )
    elif request.method not in {"GET", "HEAD"}:
        origin = request.headers.get("origin", "")
        content_type = request.headers.get("content-type", "").partition(";")[0]
        raw_length = request.headers.get("content-length")
        try:
            length = int(raw_length or "")
        except ValueError:
            length = -1
        if not _origin_matches_host(origin, host):
            response = JSONResponse(
                {"error": {"message": "same-origin request required"}},
                status_code=403,
            )
        elif not hmac.compare_digest(
            request.headers.get("x-pcbdraft-csrf", ""), csrf_token
        ):
            response = JSONResponse(
                {"error": {"message": "invalid CSRF token"}}, status_code=403
            )
        elif content_type != "application/json":
            response = JSONResponse(
                {"error": {"message": "application/json required"}},
                status_code=415,
            )
        elif request.headers.get("transfer-encoding"):
            response = JSONResponse(
                {"error": {"message": "streamed request bodies are disabled"}},
                status_code=400,
            )
        elif length < 0 or length > MAX_REQUEST_BYTES:
            response = JSONResponse(
                {"error": {"message": "request body exceeds the size limit"}},
                status_code=413,
            )
        else:
            body = await request.body()
            if len(body) > MAX_REQUEST_BYTES:
                response = JSONResponse(
                    {"error": {"message": "request body exceeds the size limit"}},
                    status_code=413,
                )
            elif len(body) != length:
                response = JSONResponse(
                    {"error": {"message": "request body length is invalid"}},
                    status_code=400,
                )
            else:
                response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers.update(
        {
            "Content-Security-Policy": (
                "default-src 'self'; base-uri 'self'; connect-src 'self'; "
                "img-src 'self' data:; object-src 'none'; script-src 'self'; "
                "style-src 'self'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Cache-Control": response.headers.get("Cache-Control", "no-store"),
        }
    )
    return response


def create_gui_app(  # noqa: C901 - closed-route setup keeps security policy adjacent.
    service: ApplicationService | None = None,
    *,
    cache_root: str | Path | None = None,
    initial_project: str | None = None,
    bind_host: str = "127.0.0.1",
    allowed_hosts: Iterable[str] | None = None,
    live_view: Any | None = None,
    previews: Any | None = None,
    ipc: Any | None = None,
    ipc_enabled: bool = False,
    sessions: Any | None = None,
    artifacts: Any | None = None,
    kicad_opener: Callable[[str], dict[str, Any]] | None = None,
) -> FastAPI:
    """Build the local application with injectable focused-test boundaries."""

    from pcbdraft.kicad.ipc import KiCadIPCCompanion
    from pcbdraft.services.gui_artifacts import GUIArtifactService
    from pcbdraft.services.gui_session import GuiSessionManager
    from pcbdraft.services.live_view import ExactPreviewCache, LiveViewService

    # Opening an observation surface must not advance a project revision merely
    # because an older process ended in a transient state.  Normal CLI launches
    # retain ApplicationService's recovery default.
    service = service or ApplicationService(recover_interrupted=False)
    requested_cache = (
        Path(cache_root).expanduser() if cache_root is not None else _cache_home()
    )
    if requested_cache.exists() and (
        requested_cache.is_symlink() or not requested_cache.is_dir()
    ):
        raise ValidationError("GUI cache root must not be a symbolic link")
    cache = make_directory(requested_cache.resolve(strict=False))
    if initial_project is not None:
        initial_project = _safe_project_id(initial_project)
        service.open_project(initial_project)
    live_view = live_view or LiveViewService(service)
    previews = previews or ExactPreviewCache(service, cache_root=cache)
    artifacts = artifacts or GUIArtifactService(service, cache_root=cache / "artifacts")
    ipc = ipc if ipc is not None else KiCadIPCCompanion(enabled=ipc_enabled)
    sessions = sessions or GuiSessionManager(service, cache_root=cache)
    broker = GUIEventBroker(service, live_view, sessions, cache)
    runtime = GUIRuntime(
        service=service,
        live_view=live_view,
        previews=previews,
        artifacts=artifacts,
        ipc=ipc,
        sessions=sessions,
        open_in_kicad=kicad_opener
        or (lambda value: _open_project_in_kicad(service, value)),
        events=broker,
        cache_root=cache,
        csrf_token=secrets.token_urlsafe(32),
        initial_project=initial_project,
    )

    configured_hosts = set(_LOOPBACK_HOSTS)
    bound_host = _bind_host_name(bind_host)
    if bound_host is not None:
        configured_hosts.add(bound_host)
    for value in allowed_hosts or ():
        hostname = _host_name(value)
        if hostname:
            configured_hosts.add(hostname)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        callback = getattr(runtime.sessions, "shutdown", None)
        if callable(callback):
            await run_in_threadpool(callback)

    app = FastAPI(
        title="PCBDraft GUI",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.pcbdraft = runtime

    @app.middleware("http")
    async def local_security(
        request: Request, call_next: Callable[..., Any]
    ) -> Response:
        return await _local_security_response(
            request,
            call_next,
            configured_hosts=configured_hosts,
            csrf_token=runtime.csrf_token,
        )

    @app.exception_handler(ValidationError)
    async def validation_error(_request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": {"message": sanitize_user_text(str(exc))}}, status_code=400
        )

    @app.exception_handler(PCBDraftError)
    async def application_error(_request: Request, exc: PCBDraftError) -> JSONResponse:
        return JSONResponse(
            {"error": {"message": sanitize_user_text(str(exc))}}, status_code=409
        )

    async def index() -> Response:
        return _asset_response("index.html", "text/html; charset=utf-8")

    _add_route(app, "/", index, methods=["GET"], name="index")
    for asset_name, media_type in _STATIC_ASSETS.items():

        async def static_asset(
            name: str = asset_name, content_type: str = media_type
        ) -> Response:
            return _asset_response(name, content_type)

        _add_route(
            app,
            f"/assets/{asset_name}",
            static_asset,
            methods=["GET"],
            name=f"asset-{asset_name}",
        )

    async def healthz() -> dict[str, Any]:
        return {
            "schema": "pcbdraft-gui-health",
            "version": 1,
            "status": "ok",
        }

    _add_route(app, "/healthz", healthz, methods=["GET"], name="healthz")

    async def bootstrap() -> dict[str, Any]:
        return {
            "schema": "pcbdraft-gui-bootstrap",
            "version": 1,
            "product_version": __version__,
            "csrf_token": runtime.csrf_token,
            "initial_project": runtime.initial_project,
            "projects": await run_in_threadpool(_bounded_project_list, runtime.service),
        }

    async def projects() -> dict[str, Any]:
        return {
            "projects": await run_in_threadpool(_bounded_project_list, runtime.service)
        }

    async def snapshot(project_id: str) -> dict[str, Any]:
        project_id = _safe_project_id(project_id)
        value = await run_in_threadpool(runtime.snapshot, project_id)
        value["stream"] = await run_in_threadpool(runtime.events.cursor, project_id)
        return value

    async def validation(project_id: str) -> dict[str, Any]:
        return await run_in_threadpool(
            runtime.artifacts.validation, _safe_project_id(project_id)
        )

    async def external_change(project_id: str) -> dict[str, Any]:
        callback = getattr(runtime.service, "external_kicad_change_status", None)
        if not callable(callback):
            raise PCBDraftError("external KiCad change inspection is unavailable")
        return await run_in_threadpool(callback, _safe_project_id(project_id))

    async def import_external_change(request: Request, project_id: str) -> JSONResponse:
        project_id = _safe_project_id(project_id)
        try:
            body = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValidationError(
                "external import request must be a JSON object"
            ) from exc
        if not isinstance(body, dict) or set(body) != {"expected_revision"}:
            raise ValidationError(
                "external import requires exactly one expected_revision field"
            )
        revision = body.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValidationError("external import revision is invalid")
        callback = getattr(runtime.service, "import_external_kicad_revision", None)
        if not callable(callback):
            raise PCBDraftError("external KiCad import is unavailable")
        result = await run_in_threadpool(
            callback, project_id, expected_revision=revision
        )
        await run_in_threadpool(runtime.events.poll, project_id)
        return JSONResponse(result, status_code=202)

    async def artifact_manifest(project_id: str) -> dict[str, Any]:
        return await run_in_threadpool(
            runtime.artifacts.manifest, _safe_project_id(project_id)
        )

    def fixed_artifact_download(key: str) -> Callable[..., Any]:
        async def download(request: Request, project_id: str) -> FileResponse:
            project_id = _safe_project_id(project_id)
            scene = await run_in_threadpool(
                runtime.live_view.snapshot, project_id, timeout=0.0
            )
            revision, content_hash = _artifact_binding(request, scene)
            manifest = await run_in_threadpool(runtime.artifacts.manifest, project_id)
            entries = (
                manifest.get("artifacts") if isinstance(manifest, Mapping) else None
            )
            record = next(
                (
                    item
                    for item in entries or []
                    if isinstance(item, Mapping) and item.get("key") == key
                ),
                None,
            )
            if not isinstance(record, Mapping) or record.get("state") != "ready":
                raise PCBDraftError("artifact is not bound to the current design")
            artifact = await run_in_threadpool(
                runtime.artifacts.download, project_id, key
            )
            current = await run_in_threadpool(
                runtime.live_view.snapshot, project_id, timeout=0.0
            )
            if _artifact_binding(request, current) != (revision, content_hash):
                raise PCBDraftError("project changed while artifact was resolved")
            return FileResponse(
                artifact.path,
                media_type=artifact.media_type,
                filename=artifact.filename,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{artifact.filename}"'
                    ),
                    "ETag": f'"{content_hash}"',
                    "X-PCBDraft-Revision": str(revision),
                    "X-PCBDraft-Content-Hash": content_hash,
                },
            )

        return download

    async def session(project_id: str) -> dict[str, Any]:
        project_id = _safe_project_id(project_id)
        await run_in_threadpool(runtime.service.open_project, project_id)
        return await run_in_threadpool(runtime.sessions.session, project_id)

    async def event_stream(request: Request, project_id: str) -> StreamingResponse:
        project_id = _safe_project_id(project_id)
        await run_in_threadpool(runtime.service.open_project, project_id)
        query_after = request.query_params.get("after")
        header_after = request.headers.get("last-event-id")
        raw_cursor = header_after if header_after is not None else query_after or "0"
        try:
            cursor = int(raw_cursor)
        except ValueError as exc:
            raise ValidationError("event cursor must be an integer") from exc
        if cursor < 0:
            raise ValidationError("event cursor must be non-negative")
        once = request.query_params.get("once", "").casefold() in {"1", "true", "yes"}

        async def generate() -> AsyncIterator[bytes]:
            nonlocal cursor
            while True:
                events = await run_in_threadpool(
                    runtime.events.after, project_id, cursor
                )
                for value in events:
                    cursor = max(cursor, int(value["sequence"]))
                    payload = json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    yield b"id: " + str(cursor).encode("ascii") + b"\n"
                    yield b"event: update\n"
                    yield b"data: " + payload + b"\n\n"
                if once:
                    return
                if not events:
                    yield b": keepalive\n\n"
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.75)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def message(request: Request, project_id: str) -> JSONResponse:
        project_id = _safe_project_id(project_id)
        try:
            body = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValidationError("request body must be a JSON object") from exc
        if not isinstance(body, dict) or set(body) != {"text"}:
            raise ValidationError("message request fields are invalid")
        result = await run_in_threadpool(
            runtime.sessions.start, project_id, body["text"]
        )
        await run_in_threadpool(runtime.events.poll, project_id)
        return JSONResponse(result, status_code=202)

    async def stop(request: Request, project_id: str) -> JSONResponse:
        project_id = _safe_project_id(project_id)
        try:
            body = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValidationError("request body must be an empty JSON object") from exc
        if body != {}:
            raise ValidationError("stop request body must be an empty object")
        result = await run_in_threadpool(runtime.sessions.stop, project_id)
        await run_in_threadpool(runtime.events.poll, project_id)
        return JSONResponse(result, status_code=202)

    async def board_svg(request: Request, project_id: str) -> FileResponse:
        project_id = _safe_project_id(project_id)
        scene = await run_in_threadpool(
            runtime.live_view.snapshot, project_id, timeout=0.0
        )
        revision, content_hash = _artifact_binding(request, scene)
        artifact = await run_in_threadpool(
            runtime.previews.artifact,
            project_id,
            "board_svg",
            generate=True,
            timeout=90.0,
        )
        if artifact is None:
            raise PCBDraftError(
                "exact PCB SVG is unavailable while the project is busy"
            )
        current = await run_in_threadpool(
            runtime.live_view.snapshot, project_id, timeout=0.0
        )
        if _artifact_binding(request, current) != (revision, content_hash):
            raise PCBDraftError("project changed while exact PCB SVG was generated")
        return FileResponse(
            artifact,
            media_type="image/svg+xml",
            filename="board.svg",
            headers={
                "Content-Disposition": 'inline; filename="board.svg"',
                "Cache-Control": "private, no-cache",
                "ETag": f'"{content_hash}"',
                "X-PCBDraft-Revision": str(revision),
                "X-PCBDraft-Content-Hash": content_hash,
                "X-PCBDraft-Geometry": "exact-kicad-svg",
            },
        )

    async def board_3d(request: Request, project_id: str) -> Response:
        project_id = _safe_project_id(project_id)
        scene = await run_in_threadpool(
            runtime.live_view.snapshot, project_id, timeout=0.0
        )
        revision, content_hash = _artifact_binding(request, scene)
        artifact = await run_in_threadpool(
            runtime.previews.artifact, project_id, "board_3d", generate=False
        )
        if artifact is None:
            return JSONResponse(
                {"error": {"message": "3D preview has not been generated"}},
                status_code=404,
            )
        return FileResponse(
            artifact,
            media_type="image/png",
            filename="board-top.png",
            headers={
                "Content-Disposition": 'inline; filename="board-top.png"',
                "Cache-Control": "private, no-cache",
                "ETag": f'"{content_hash}"',
                "X-PCBDraft-Revision": str(revision),
                "X-PCBDraft-Content-Hash": content_hash,
                "X-PCBDraft-Geometry": "kicad-cli-render",
            },
        )

    async def generate_3d(request: Request, project_id: str) -> JSONResponse:
        project_id = _safe_project_id(project_id)
        try:
            body = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValidationError("request body must be an empty JSON object") from exc
        if body != {}:
            raise ValidationError("3D preview request body must be an empty object")
        artifact = await run_in_threadpool(
            runtime.previews.artifact,
            project_id,
            "board_3d",
            generate=True,
            timeout=90.0,
        )
        if artifact is None:
            raise PCBDraftError("3D preview is unavailable while the project is busy")
        scene = await run_in_threadpool(
            runtime.live_view.snapshot, project_id, timeout=0.0
        )
        revision, content_hash = _artifact_binding(request, scene)
        return JSONResponse(
            {
                "ready": True,
                "url": (
                    f"api/projects/{project_id}/artifacts/board-3d.png"
                    f"?revision={revision}&content_hash={content_hash}"
                ),
                "design_revision": revision,
                "content_hash": content_hash,
            },
            status_code=202,
        )

    async def open_in_kicad(request: Request, project_id: str) -> JSONResponse:
        project_id = _safe_project_id(project_id)
        try:
            body = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValidationError(
                "open-in-KiCad request body must be an empty JSON object"
            ) from exc
        if body != {}:
            raise ValidationError("open-in-KiCad request body must be an empty object")
        result = await run_in_threadpool(runtime.open_in_kicad, project_id)
        return JSONResponse(result)

    _add_route(app, "/api/bootstrap", bootstrap, methods=["GET"], name="bootstrap")
    _add_route(app, "/api/projects", projects, methods=["GET"], name="projects")
    _add_route(
        app,
        "/api/projects/{project_id}/snapshot",
        snapshot,
        methods=["GET"],
        name="snapshot",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/external-change",
        external_change,
        methods=["GET"],
        name="external-change",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/external-change/import",
        import_external_change,
        methods=["POST"],
        name="external-change-import",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/validation",
        validation,
        methods=["GET"],
        name="validation",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/artifacts",
        artifact_manifest,
        methods=["GET"],
        name="artifact-manifest",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/events",
        event_stream,
        methods=["GET"],
        name="events",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/session",
        session,
        methods=["GET"],
        name="session",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/messages",
        message,
        methods=["POST"],
        name="messages",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/stop",
        stop,
        methods=["POST"],
        name="stop",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/artifacts/board.svg",
        board_svg,
        methods=["GET"],
        name="board-svg",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/artifacts/board-3d.png",
        board_3d,
        methods=["GET"],
        name="board-3d",
    )
    _add_route(
        app,
        "/api/projects/{project_id}/artifacts/board-3d",
        generate_3d,
        methods=["POST"],
        name="generate-3d",
    )
    for artifact_key in (
        "bom.csv",
        "gerbers.zip",
        "drill.zip",
        "positions.csv",
        "board.step",
        "schematic.pdf",
        "schematic.svg",
    ):
        _add_route(
            app,
            f"/api/projects/{{project_id}}/artifacts/{artifact_key}",
            fixed_artifact_download(artifact_key),
            methods=["GET"],
            name=f"artifact-{artifact_key}",
        )
    _add_route(
        app,
        "/api/projects/{project_id}/open-in-kicad",
        open_in_kicad,
        methods=["POST"],
        name="open-in-kicad",
    )
    return app


def run_gui(
    *,
    host: str = "127.0.0.1",
    port: int = 9130,
    project_id: str | None = None,
    kicad_ipc: bool = False,
) -> int:
    """Run the resident server without requiring or opening a browser."""

    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
        raise ValidationError("GUI port must be between 1 and 65535")
    if _bind_host_name(host) is None:
        raise ValidationError("GUI host must be a loopback address or localhost")
    app = create_gui_app(
        initial_project=project_id, bind_host=host, ipc_enabled=kicad_ipc
    )
    shown_host = f"[{host}]" if ":" in host else host
    print(f"PCBDraft GUI: http://{shown_host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


__all__ = ("GUIEventBroker", "create_gui_app", "run_gui")
