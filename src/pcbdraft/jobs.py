"""Crash-visible bounded background jobs for local application surfaces."""

from __future__ import annotations

import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .agent_tools import run_design_turn
from .application import APP_FILE_LIMIT, ApplicationService, sanitize_user_text
from .errors import PCBDraftError, ValidationError
from .io import atomic_write_json, load_json_limited
from .locking import ResourceLock
from .runs import utc_timestamp

JOB_SCHEMA = "pcbdraft-application-job"
JOB_VERSION = 1
MAX_PROJECT_JOBS = 2_000
_ACTIONS = {
    "agent_message",
    "message",
    "confirm",
    "validate",
    "apply_change",
    "discard_change",
    "undo",
    "release",
    "previews",
}
_ACTIVE = {"queued", "running", "cancel_requested"}
_RETRYABLE = {"failed", "interrupted", "cancelled"}


class JobRunner:
    """Persist every asynchronous action before a bounded worker starts it."""

    def __init__(self, service: ApplicationService, *, workers: int = 2) -> None:
        if workers < 1 or workers > 4:
            raise ValidationError("application job workers must be between 1 and 4")
        self.service = service
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="pcbdraft-job"
        )
        self._cancel: dict[str, threading.Event] = {}
        self._guard = threading.Lock()
        self._recover_jobs()

    def submit(
        self,
        project_id: str,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        retry_of: str | None = None,
    ) -> dict[str, Any]:
        if action not in _ACTIONS:
            raise ValidationError(f"unsupported application job action: {action}")
        normalized = self._normalize_args(action, args or {})
        jobs_dir = self.service.project_root(project_id) / "jobs"
        existing = self.list(project_id)
        if len(existing) >= MAX_PROJECT_JOBS:
            raise ValidationError("project reached its 2000 job record limit")
        if any(job["status"] in _ACTIVE for job in existing):
            raise ValidationError("project already has an active application job")
        job_id = f"{utc_timestamp().replace(':', '').replace('-', '')}-{secrets.token_hex(4)}"
        now = utc_timestamp()
        attempt = 1
        if retry_of is not None:
            attempt = int(self.get(project_id, retry_of)["attempt"]) + 1
        job = {
            "schema": JOB_SCHEMA,
            "version": JOB_VERSION,
            "id": job_id,
            "project_id": project_id,
            "action": action,
            "args": normalized,
            "status": "queued",
            "attempt": attempt,
            "retry_of": retry_of,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "cancel_requested_at": None,
            "result": None,
            "error": None,
        }
        path = jobs_dir / f"{job_id}.json"
        atomic_write_json(path, job)
        event = threading.Event()
        with self._guard:
            self._cancel[job_id] = event
        self._pool.submit(self._run, path, event)
        return job

    def list(self, project_id: str) -> list[dict[str, Any]]:
        jobs_dir = self.service.project_root(project_id) / "jobs"
        result: list[dict[str, Any]] = []
        for path in sorted(jobs_dir.glob("*.json"), reverse=True):
            if path.is_symlink() or not path.is_file():
                continue
            job = load_json_limited(path, APP_FILE_LIMIT)
            self._validate_job(job, expected_project=project_id, expected_id=path.stem)
            result.append(job)
        return result

    def get(self, project_id: str, job_id: str) -> dict[str, Any]:
        path = self._job_path(project_id, job_id)
        job = load_json_limited(path, APP_FILE_LIMIT)
        self._validate_job(job, expected_project=project_id, expected_id=job_id)
        return job

    def cancel(self, project_id: str, job_id: str) -> dict[str, Any]:
        path = self._job_path(project_id, job_id)
        with ResourceLock(path, self.service.locks_root):
            job = load_json_limited(path, APP_FILE_LIMIT)
            self._validate_job(job, expected_project=project_id, expected_id=job_id)
            if job["status"] not in _ACTIVE:
                raise ValidationError("job is no longer cancellable")
            job["status"] = "cancel_requested"
            job["cancel_requested_at"] = utc_timestamp()
            atomic_write_json(path, job)
        with self._guard:
            event = self._cancel.get(job_id)
        if event is not None:
            event.set()
        return job

    def retry(self, project_id: str, job_id: str) -> dict[str, Any]:
        previous = self.get(project_id, job_id)
        if previous["status"] not in _RETRYABLE:
            raise ValidationError(
                "only failed, interrupted, or cancelled jobs can be retried"
            )
        return self.submit(
            project_id,
            previous["action"],
            previous["args"],
            retry_of=job_id,
        )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, path: Path, cancel: threading.Event) -> None:
        job = load_json_limited(path, APP_FILE_LIMIT)
        project_id = job["project_id"]
        try:
            with ResourceLock(path, self.service.locks_root):
                job = load_json_limited(path, APP_FILE_LIMIT)
                if cancel.is_set() or job["status"] == "cancel_requested":
                    job["status"] = "cancelled"
                    job["completed_at"] = utc_timestamp()
                    atomic_write_json(path, job)
                    return
                job["status"] = "running"
                job["started_at"] = utc_timestamp()
                atomic_write_json(path, job)
            self.service.record_progress(
                project_id, "job.started", f"Started {job['action']} job"
            )
            view = self._dispatch(job)
            result = {
                "project_status": view["project"]["status"],
                "project_revision": view["state"]["revision"],
                "design_content_hash": (
                    view["design"]["content_hash"] if view.get("design") else None
                ),
            }
            with ResourceLock(path, self.service.locks_root):
                current = load_json_limited(path, APP_FILE_LIMIT)
                current["status"] = (
                    "completed_after_cancel" if cancel.is_set() else "completed"
                )
                current["completed_at"] = utc_timestamp()
                current["result"] = result
                atomic_write_json(path, current)
            self.service.record_progress(
                project_id,
                "job.complete",
                (
                    f"Completed {job['action']} after cancellation was requested"
                    if cancel.is_set()
                    else f"Completed {job['action']} job"
                ),
                level="warning" if cancel.is_set() else "info",
            )
        except Exception as exc:  # noqa: BLE001 - persist every worker failure
            try:
                with ResourceLock(path, self.service.locks_root):
                    current = load_json_limited(path, APP_FILE_LIMIT)
                    current["status"] = "failed"
                    current["completed_at"] = utc_timestamp()
                    current["error"] = sanitize_user_text(str(exc))[:2048]
                    atomic_write_json(path, current)
                self.service.record_progress(
                    project_id,
                    "job.failed",
                    current["error"],
                    level="error",
                )
            except PCBDraftError:
                pass
        finally:
            with self._guard:
                self._cancel.pop(job["id"], None)

    def _dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = job["project_id"]
        action = job["action"]
        args = job["args"]
        timeout = float(args.get("timeout", 180.0))
        if action == "agent_message":
            return run_design_turn(
                self.service,
                project_id,
                args["text"],
                timeout=timeout,
                cancellation_requested=lambda: self._cancel_requested(job["id"]),
            )
        if action == "message":
            return self.service.send_message(project_id, args["text"], timeout=timeout)
        if action == "confirm":
            return self.service.confirm_project(project_id, timeout=timeout)
        if action == "validate":
            return self.service.validate_project(project_id, timeout=timeout)
        if action == "apply_change":
            return self.service.apply_modification(project_id)
        if action == "discard_change":
            return self.service.discard_modification(project_id)
        if action == "undo":
            return self.service.undo_last_modification(project_id)
        if action == "release":
            return self.service.build_release(project_id, timeout=timeout)
        if action == "previews":
            return self.service.generate_project_previews(project_id, timeout=timeout)
        raise ValidationError(f"unsupported application job action: {action}")

    @staticmethod
    def _normalize_args(action: str, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ValidationError("job arguments must be an object")
        allowed = (
            {"text", "timeout"}
            if action in {"agent_message", "message"}
            else {"timeout"}
        )
        if set(args) - allowed:
            raise ValidationError("job arguments contain unsupported fields")
        result: dict[str, Any] = {}
        if action in {"agent_message", "message"}:
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValidationError("message job requires non-empty text")
            encoded = text.encode("utf-8")
            if len(encoded) > 16_384:
                raise ValidationError("message job text exceeds 16 KiB")
            result["text"] = sanitize_user_text(text.strip())
        timeout = args.get("timeout", 180.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValidationError("job timeout must be a number")
        if timeout <= 0 or timeout > 1800:
            raise ValidationError("job timeout must be in (0, 1800] seconds")
        result["timeout"] = float(timeout)
        return result

    def _cancel_requested(self, job_id: str) -> bool:
        """Read the in-process cancellation latch at a safe tool boundary."""

        with self._guard:
            event = self._cancel.get(job_id)
        return event.is_set() if event is not None else False

    def _job_path(self, project_id: str, job_id: str) -> Path:
        if (
            not isinstance(job_id, str)
            or not job_id
            or any(character not in "0123456789TZabcdef-" for character in job_id)
        ):
            raise ValidationError("application job id is invalid")
        path = self.service.project_root(project_id) / "jobs" / f"{job_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ValidationError("application job does not exist")
        return path

    @staticmethod
    def _validate_job(value: Any, *, expected_project: str, expected_id: str) -> None:
        required = {
            "schema",
            "version",
            "id",
            "project_id",
            "action",
            "args",
            "status",
            "attempt",
            "retry_of",
            "created_at",
            "started_at",
            "completed_at",
            "cancel_requested_at",
            "result",
            "error",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValidationError("application job record is malformed")
        if value["schema"] != JOB_SCHEMA or value["version"] != JOB_VERSION:
            raise ValidationError("unsupported application job schema/version")
        if value["id"] != expected_id or value["project_id"] != expected_project:
            raise ValidationError("application job identity is malformed")
        if value["action"] not in _ACTIONS or not isinstance(value["args"], dict):
            raise ValidationError("application job action is malformed")

    def _recover_jobs(self) -> None:
        for project in self.service.list_projects():
            project_id = project["id"]
            for job in self.list(project_id):
                if job["status"] not in _ACTIVE:
                    continue
                path = self._job_path(project_id, job["id"])
                with ResourceLock(path, self.service.locks_root):
                    current = load_json_limited(path, APP_FILE_LIMIT)
                    if current["status"] in _ACTIVE:
                        current["status"] = "interrupted"
                        current["completed_at"] = utc_timestamp()
                        current["error"] = (
                            "The application stopped before this job recorded completion."
                        )
                        atomic_write_json(path, current)
