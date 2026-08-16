"""Loopback-only HTTP application exposing the shared PCBDraft service."""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, ClassVar

from pcbdraft import __version__, build_identity
from pcbdraft.agent.turns import AgentTurnStore, ToolRunRecord, TurnRecord
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.services.application import ApplicationService, sanitize_user_text
from pcbdraft.services.jobs import JobRunner

MAX_REQUEST_BYTES = 64 * 1024
MAX_URL_LENGTH = 2048
_AGENT_THREAD_ID = "main"
_RECENT_AGENT_TURNS = 20
_PROJECT_VIEW_SNAPSHOT_ATTEMPTS = 6
_PROJECT_VIEW_LOCK_TIMEOUT_SECONDS = 0.05
_PROJECT_VIEW_RETRY_DELAY_SECONDS = 0.005
_PROJECT_ROUTE = re.compile(r"/api/projects/([a-z][a-z0-9-]{2,79})(?:/(.*))?")
_ARTIFACT_KEYS = {
    "schematic_svg",
    "schematic_pdf",
    "board_svg",
    "board_render",
    "schematic",
    "board",
    "kicad_project",
    "requirements",
    "ir",
    "circuit_plan",
    "validation_report",
    "release_archive",
}


class PCBDraftHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        service: ApplicationService,
    ) -> None:
        super().__init__(address, PCBDraftHandler)
        self.service = service
        self.jobs = JobRunner(service)
        self.csrf_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        host, port = self.server_address[:2]
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValidationError("PCBDraft app only binds to loopback")
        public_host = host
        public_netloc = (
            f"[{public_host}]:{port}" if ":" in public_host else f"{public_host}:{port}"
        )
        self.base_url = f"http://{public_netloc}"
        self.launch_url = f"{self.base_url}/#session={self.session_token}"
        origins = {self.base_url}
        if host in {"127.0.0.1", "localhost", "::1"}:
            origins.add(f"http://localhost:{port}")
            origins.add(f"http://127.0.0.1:{port}")
            origins.add(f"http://[::1]:{port}")
        self.allowed_origins = origins
        self.allowed_hosts = {
            urllib.parse.urlsplit(origin).netloc for origin in self.allowed_origins
        }

    def server_close(self) -> None:
        self.jobs.shutdown()
        super().server_close()


class PCBDraftHandler(BaseHTTPRequestHandler):
    server: PCBDraftHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "PCBDraft"
    sys_version = ""
    _STATIC: ClassVar[dict[str, tuple[str, str]]] = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
        "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/assets/pcbdraft-mark.png": ("pcbdraft-mark-128.png", "image/png"),
    }

    def log_message(self, format: str, *args: object) -> None:
        # URLs can contain user-provided text in accidental clients. Do not log them.
        del format, args

    def do_GET(self) -> None:
        if not self._valid_host():
            return
        parsed = self._parsed_path()
        if parsed is None:
            return
        path = parsed.path
        try:
            if path in self._STATIC:
                self._serve_static(path)
                return
            if path.startswith("/api/") and not self._valid_session(parsed):
                return
            if path == "/api/bootstrap":
                self._json_response(
                    {
                        "schema": "pcbdraft-browser-bootstrap",
                        "version": 1,
                        "product_version": __version__,
                        "product_build": build_identity(),
                        "csrf_token": self.server.csrf_token,
                        "diagnostics": self.server.service.diagnostics(),
                        "projects": self.server.service.list_projects(),
                    }
                )
                return
            match = _PROJECT_ROUTE.fullmatch(path)
            if match:
                project_id, suffix = match.groups()
                if not suffix:
                    self._json_response(self._project_view(project_id))
                    return
                if suffix == "jobs":
                    self._json_response({"jobs": self.server.jobs.list(project_id)})
                    return
                if suffix == "events":
                    query = urllib.parse.parse_qs(parsed.query, strict_parsing=False)
                    after = int(query.get("after", ["0"])[0])
                    self._event_stream(project_id, after)
                    return
                if suffix.startswith("artifact/"):
                    self._serve_artifact(project_id, suffix.removeprefix("artifact/"))
                    return
            self._error(HTTPStatus.NOT_FOUND, "resource not found")
        except (PCBDraftError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, sanitize_user_text(str(exc)))
        except Exception:  # noqa: BLE001 - HTTP boundary hides internal details
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal application error")

    def do_POST(self) -> None:
        if not self._valid_host():
            return
        parsed = self._parsed_path()
        if parsed is None:
            return
        if not self._valid_session(parsed) or not self._valid_mutation_request():
            return
        try:
            body = self._json_body()
            path = parsed.path
            if path == "/api/projects":
                if set(body) != {"name", "request"}:
                    raise ValidationError("project request fields are invalid")
                draft = self.server.service.create_draft(body["name"])
                project_id = draft["project"]["id"]
                job = self.server.jobs.submit(
                    project_id,
                    "agent_message",
                    {"text": body["request"], "timeout": 420.0},
                )
                self._json_response(
                    {"project": self._project_view(project_id), "job": job},
                    status=HTTPStatus.ACCEPTED,
                )
                return
            match = _PROJECT_ROUTE.fullmatch(path)
            if match:
                project_id, suffix = match.groups()
                if suffix == "messages":
                    if set(body) != {"text"}:
                        raise ValidationError("message fields are invalid")
                    job = self.server.jobs.submit(
                        project_id,
                        "agent_message",
                        {"text": body["text"], "timeout": 420.0},
                    )
                    self._json_response({"job": job}, status=HTTPStatus.ACCEPTED)
                    return
                actions = {
                    "confirm": "generate_candidate",
                    "validate": "validate",
                    "apply-change": "apply_candidate",
                    "discard-change": "discard_candidate",
                    "undo": "undo_last_change",
                    "release": "build_release",
                    "previews": "render_previews",
                }
                if suffix in actions:
                    if body:
                        raise ValidationError("action body must be an empty object")
                    job = self.server.jobs.submit(
                        project_id,
                        "agent_tool",
                        {"tool": actions[suffix], "timeout": 420.0},
                    )
                    self._json_response({"job": job}, status=HTTPStatus.ACCEPTED)
                    return
                job_match = re.fullmatch(
                    r"jobs/([0-9TZabcdef-]+)/(cancel|retry)", suffix or ""
                )
                if job_match:
                    if body:
                        raise ValidationError("job action body must be an empty object")
                    job_id, action = job_match.groups()
                    job = (
                        self.server.jobs.cancel(project_id, job_id)
                        if action == "cancel"
                        else self.server.jobs.retry(project_id, job_id)
                    )
                    self._json_response({"job": job}, status=HTTPStatus.ACCEPTED)
                    return
                if suffix == "open-kicad":
                    if body:
                        raise ValidationError(
                            "open action body must be an empty object"
                        )
                    self._open_kicad(project_id)
                    return
            self._error(HTTPStatus.NOT_FOUND, "resource not found")
        except ValidationError as exc:
            self._error(HTTPStatus.BAD_REQUEST, sanitize_user_text(str(exc)))
        except PCBDraftError as exc:
            self._error(HTTPStatus.CONFLICT, sanitize_user_text(str(exc)))
        except Exception:  # noqa: BLE001 - HTTP boundary hides internal details
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal application error")

    def do_OPTIONS(self) -> None:
        # No CORS support by design; a cross-origin preflight must not be authorized.
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "cross-origin access is disabled")

    def _project_view(self, project_id: str) -> dict[str, Any]:
        permission_mode = getattr(
            getattr(self.server.jobs.agent, "permissions", None),
            "mode",
            "workspace",
        )
        normalized_permission_mode = (
            permission_mode if isinstance(permission_mode, str) else "workspace"
        )
        # Do not hold the project lock while entering the independently locked
        # job or turn stores.  Instead, use project snapshots as bookends and
        # retry if an engineering revision crossed the projection window.  This
        # avoids both lock-order deadlocks and cross-revision browser views.
        for attempt in range(_PROJECT_VIEW_SNAPSHOT_ATTEMPTS):
            before = _try_project_snapshot(self.server.service, project_id)
            if before is None:
                if attempt + 1 < _PROJECT_VIEW_SNAPSHOT_ATTEMPTS:
                    time.sleep(_PROJECT_VIEW_RETRY_DELAY_SECONDS)
                continue
            try:
                jobs = self.server.jobs.list(project_id)[:50]
                agent = _durable_agent_view(
                    self.server.service,
                    project_id,
                    permission_mode=normalized_permission_mode,
                )
            except PCBDraftError:
                # These adapters resolve the project root before entering their
                # own stores. If a writer acquired the project lock immediately
                # after ``before``, that path validation can briefly observe a
                # multi-file commit in flight. Retry only when the bookend proves
                # the project moved; stable failures remain real errors.
                after_error = _try_project_snapshot(self.server.service, project_id)
                if after_error is not None and _project_revision_token(
                    before
                ) == _project_revision_token(after_error):
                    raise
                if attempt + 1 < _PROJECT_VIEW_SNAPSHOT_ATTEMPTS:
                    time.sleep(_PROJECT_VIEW_RETRY_DELAY_SECONDS)
                continue
            after = _try_project_snapshot(self.server.service, project_id)
            if (
                after is not None
                and _project_revision_token(before) == _project_revision_token(after)
                and _agent_projection_fits_revision(
                    agent, state_revision=_state_revision(after)
                )
            ):
                after["jobs"] = jobs
                after["agent"] = agent
                return after
            if attempt + 1 < _PROJECT_VIEW_SNAPSHOT_ATTEMPTS:
                time.sleep(_PROJECT_VIEW_RETRY_DELAY_SECONDS)
        raise PCBDraftError(
            "project changed while the browser view was assembled; retry the request"
        )

    def _parsed_path(self) -> urllib.parse.SplitResult | None:
        if len(self.path) > MAX_URL_LENGTH:
            self._error(HTTPStatus.REQUEST_URI_TOO_LONG, "request URL is too long")
            return None
        return urllib.parse.urlsplit(self.path)

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in self.server.allowed_hosts:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Host header")
            return False
        return True

    def _valid_session(self, parsed: urllib.parse.SplitResult) -> bool:
        supplied = self.headers.get("X-PCBDraft-Session", "")
        if not supplied:
            values = urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True, strict_parsing=False
            ).get("session", [])
            if len(values) == 1:
                supplied = values[0]
        if not _secret_matches(supplied, self.server.session_token):
            self._error(HTTPStatus.UNAUTHORIZED, "invalid local session token")
            return False
        return True

    def _valid_mutation_request(self) -> bool:
        origin = self.headers.get("Origin", "")
        if origin not in self.server.allowed_origins:
            self._error(HTTPStatus.FORBIDDEN, "same-origin request required")
            return False
        supplied = self.headers.get("X-PCBDraft-CSRF", "")
        if not _secret_matches(supplied, self.server.csrf_token):
            self._error(HTTPStatus.FORBIDDEN, "invalid CSRF token")
            return False
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "application/json required")
            return False
        if self.headers.get("Transfer-Encoding"):
            self._error(HTTPStatus.BAD_REQUEST, "streamed request bodies are disabled")
            return False
        return True

    def _json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as exc:
            raise ValidationError("valid Content-Length is required") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValidationError("request body exceeds the 64 KiB limit")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ValidationError("request body must be a JSON object")
        return value

    def _serve_static(self, path: str) -> None:
        resource_name, content_type = self._STATIC[path]
        resource = files("pcbdraft").joinpath("web", resource_name)
        data = resource.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            raise ValidationError("static application asset is too large")
        self._bytes_response(data, content_type, cache="no-cache")

    def _serve_artifact(self, project_id: str, key: str) -> None:
        if key not in _ARTIFACT_KEYS:
            raise ValidationError("unknown project artifact")
        view = self.server.service.open_project(project_id)
        project_root = self.server.service.project_root(project_id)
        relative: str | None = None
        if key in {"schematic_svg", "schematic_pdf", "board_svg", "board_render"}:
            previews = view["artifacts"]["previews"]
            if isinstance(previews, dict):
                relative = previews["files"].get(key)
        elif key == "release_archive":
            release = view["artifacts"]["release"]
            if isinstance(release, dict):
                release_path = Path(release["archive"])
                relative = release_path.relative_to(project_root).as_posix()
        elif key == "validation_report":
            validation = view["artifacts"]["validation"]
            if isinstance(validation, dict):
                relative = validation.get("report")
        else:
            design = view.get("design")
            managed_key = {
                "schematic": "schematic",
                "board": "board",
                "kicad_project": "kicad_project",
                "requirements": "requirements",
                "ir": "ir",
                "circuit_plan": "circuit_plan",
            }[key]
            if isinstance(design, dict):
                source = design.get("files", {}).get(managed_key)
                if isinstance(source, str):
                    relative = Path(source).relative_to(project_root).as_posix()
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValidationError("project artifact is unavailable")
        artifact = (project_root / relative).resolve(strict=True)
        if (
            not artifact.is_relative_to(project_root)
            or artifact.is_symlink()
            or not artifact.is_file()
        ):
            raise ValidationError("project artifact path is unsafe")
        size = artifact.stat().st_size
        if size > 512 * 1024 * 1024:
            raise ValidationError("project artifact exceeds the browser size limit")
        content_type = (
            mimetypes.guess_type(artifact.name)[0] or "application/octet-stream"
        )
        disposition = (
            "inline"
            if key
            in {
                "schematic_svg",
                "schematic_pdf",
                "board_svg",
                "board_render",
            }
            else "attachment"
        )
        self._bytes_response(
            artifact.read_bytes(),
            content_type,
            cache="no-store",
            extra_headers={
                "Content-Disposition": f'{disposition}; filename="{artifact.name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    def _event_stream(self, project_id: str, after: int) -> None:
        if after < 0:
            raise ValidationError("event cursor must be non-negative")
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        cursor = after
        deadline = time.monotonic() + 20.0
        try:
            while time.monotonic() < deadline:
                events = self.server.service.events(project_id, after=cursor)
                for event in events:
                    cursor = max(cursor, int(event["sequence"]))
                    payload = json.dumps(
                        event, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    self.wfile.write(b"id: " + str(cursor).encode() + b"\n")
                    self.wfile.write(b"event: progress\n")
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    self.wfile.flush()
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _open_kicad(self, project_id: str) -> None:
        view = self.server.service.open_project(project_id)
        design = view.get("design")
        if not isinstance(design, dict):
            raise ValidationError("project has not generated KiCad files")
        executable = shutil.which("kicad")
        if not executable:
            raise PCBDraftError("KiCad desktop application is unavailable")
        project_file = Path(design["files"]["kicad_project"]).resolve(strict=True)
        root = self.server.service.project_root(project_id)
        if not project_file.is_relative_to(root) or project_file.is_symlink():
            raise ValidationError("KiCad project path is unsafe")
        try:
            subprocess.Popen(  # noqa: S603 - fixed executable and argv; no shell
                [executable, str(project_file)],
                cwd=project_file.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise PCBDraftError("failed to start KiCad desktop application") from exc
        self._json_response({"opened": True, "path": str(project_file)})

    def _json_response(self, value: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self._bytes_response(data, "application/json; charset=utf-8", status=status)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json_response(
            {"error": {"status": int(status), "message": message[:2048]}},
            status=status,
        )

    def _bytes_response(
        self,
        data: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        cache: str = "no-store",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )


def _try_project_snapshot(
    service: ApplicationService, project_id: str
) -> dict[str, Any] | None:
    """Take one self-consistent project snapshot without retaining its lock."""

    snapshot_reader = getattr(service, "try_open_project_snapshot", None)
    if callable(snapshot_reader):
        return snapshot_reader(project_id, timeout=_PROJECT_VIEW_LOCK_TIMEOUT_SECONDS)
    # Compatibility for in-memory service adapters. Production services expose
    # the lock-aware reader above.
    return service.open_project(project_id)


def _state_revision(view: dict[str, Any]) -> int:
    state = view.get("state")
    revision = state.get("revision") if isinstance(state, dict) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValidationError("project snapshot has an invalid state revision")
    return revision


def _project_revision_token(view: dict[str, Any]) -> tuple[int, int]:
    project = view.get("project")
    design_revision = (
        project.get("design_revision") if isinstance(project, dict) else None
    )
    if (
        isinstance(design_revision, bool)
        or not isinstance(design_revision, int)
        or design_revision < 0
    ):
        raise ValidationError("project snapshot has an invalid design revision")
    return _state_revision(view), design_revision


def _agent_projection_fits_revision(
    agent: dict[str, Any], *, state_revision: int
) -> bool:
    """Reject a receipt projection that could only come from a newer state."""

    for turn in agent["turns"]:
        if turn["baseline_revision"] > state_revision:
            return False
        for tool in turn["tool_runs"]:
            if (
                tool["baseline_revision"] > state_revision
                or tool["before_revision"] > state_revision
            ):
                return False
            after_revision = tool["after_revision"]
            if after_revision is not None and after_revision > state_revision:
                return False
    pending = agent["pending_approval"]
    return pending is None or pending["baseline_revision"] <= state_revision


def _durable_agent_view(
    service: ApplicationService,
    project_id: str,
    *,
    permission_mode: str,
) -> dict[str, Any]:
    """Project recent durable turns into a bounded browser-safe activity view.

    The browser receives stable turn and tool-call identities, not synthesized
    workflow stages.  Mutation authority remains in the orchestrator; this is a
    read-only projection over the same project-scoped turn store used by the TUI.
    """

    store = AgentTurnStore(service.project_root(project_id), service.locks_root)
    newest = store.list(thread_id=_AGENT_THREAD_ID, limit=_RECENT_AGENT_TURNS)
    # ``list`` reads every selected turn under one turn-store lock. Deriving the
    # pending checkpoint from that result avoids combining it with a second,
    # newer read of the same durable records.
    waiting = next((turn for turn in newest if turn.pending_approval is not None), None)
    pending: dict[str, Any] | None = None
    if waiting is not None and waiting.pending_approval is not None:
        checkpoint = waiting.pending_approval
        tool = waiting.tool_run(checkpoint.tool_call_id)
        pending = {
            **checkpoint.to_dict(),
            "tool_name": tool.tool_name,
            "source": tool.source,
            "effect": tool.effect,
            "risk": tool.risk,
            "arguments": dict(tool.arguments),
        }
    from pcbdraft.model.tool_calls import provider_agent_protocol

    return {
        "schema": "pcbdraft-browser-agent-view",
        "version": 1,
        "thread_id": _AGENT_THREAD_ID,
        "permission_mode": permission_mode,
        "call_producer": provider_agent_protocol(getattr(service, "provider", None)),
        "turn_order": "oldest_first",
        "turns": [_browser_turn(turn) for turn in reversed(newest)],
        "pending_approval": pending,
    }


def _browser_turn(turn: TurnRecord) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "sequence": turn.sequence,
        "status": turn.status.value,
        "request": turn.user_message,
        "baseline_revision": turn.baseline_revision,
        "created_at": turn.created_at,
        "updated_at": turn.updated_at,
        "started_at": turn.started_at,
        "completed_at": turn.completed_at,
        "stop_reason": turn.stop_reason,
        "error": turn.error,
        "tool_runs": [_browser_tool_run(tool) for tool in turn.tool_runs],
    }


def _browser_tool_run(tool: ToolRunRecord) -> dict[str, Any]:
    return {
        "tool_call_id": tool.tool_call_id,
        "tool_name": tool.tool_name,
        "source": tool.source,
        "effect": tool.effect,
        "risk": tool.risk,
        "arguments": dict(tool.arguments),
        "args_hash": tool.args_hash,
        "baseline_revision": tool.baseline_revision,
        "before_status": tool.before_status,
        "before_revision": tool.before_revision,
        "status": tool.status.value,
        "created_at": tool.created_at,
        "started_at": tool.started_at,
        "dispatch_started_at": tool.dispatch_started_at,
        "completed_at": tool.completed_at,
        "after_status": tool.after_status,
        "after_revision": tool.after_revision,
        "result": dict(tool.result) if tool.result is not None else None,
        "error": tool.error,
    }


def create_app_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    workspace: str | Path | None = None,
    provider: str = "auto",
) -> PCBDraftHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValidationError("PCBDraft app only binds to an explicit loopback host")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValidationError("application port must be between 0 and 65535")
    service = ApplicationService(workspace, provider_name=provider)
    return PCBDraftHTTPServer((host, port), service)


def _secret_matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(
        supplied.encode("utf-8", errors="surrogatepass"), expected.encode("ascii")
    )


def run_app(
    *,
    host: str,
    port: int,
    workspace: str | Path | None,
    provider: str,
    open_browser: bool,
) -> int:
    server = create_app_server(
        host=host, port=port, workspace=workspace, provider=provider
    )
    print(f"PCBDraft app: {server.launch_url}", flush=True)
    print("Local-only session; press Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(server.launch_url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
