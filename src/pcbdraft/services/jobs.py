"""Crash-visible bounded background jobs for local application surfaces."""

from __future__ import annotations

import hashlib
import json
import secrets
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from pcbdraft.agent.orchestrator import MAX_TOOL_CALLS_PER_TURN, AgentOrchestrator
from pcbdraft.agent.turns import TurnStatus
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.services.application import (
    APP_FILE_LIMIT,
    ApplicationService,
    sanitize_user_text,
)

JOB_SCHEMA = "pcbdraft-application-job"
# Version 1 records remain readable for history, but they predate durable agent
# authority. Any active v1 mutation is therefore closed rather than replayed.
LEGACY_JOB_VERSION = 1
JOB_VERSION = 2
AGENT_JOB_POLICY_SCHEMA = "pcbdraft-agent-job-policy"
AGENT_JOB_POLICY_VERSION = 1
MAX_PROJECT_JOBS = 2_000
_ACTIONS = {
    "agent_message",
    "agent_tool",
    "message",
    "confirm",
    "validate",
    "apply_change",
    "discard_change",
    "undo",
    "release",
    "previews",
}
_ADMISSION_ACTIONS = {"agent_message", "agent_tool"}
_ACTIVE = {"queued", "running", "cancel_requested"}
_RETRYABLE = {"failed", "interrupted", "cancelled"}
_JOB_STATUSES = _ACTIVE | _RETRYABLE | {"completed", "completed_after_cancel"}


class JobRunner:
    """Persist every asynchronous action before a bounded worker starts it."""

    def __init__(
        self,
        service: ApplicationService,
        *,
        workers: int = 2,
        orchestrator: AgentOrchestrator | None = None,
    ) -> None:
        if workers < 1 or workers > 4:
            raise ValidationError("application job workers must be between 1 and 4")
        self.service = service
        self.agent = orchestrator or AgentOrchestrator(service)
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="pcbdraft-job"
        )
        self._cancel: dict[str, threading.Event] = {}
        self._guard = threading.Lock()
        self._recover_jobs()
        self._recover_orphan_turns()

    def submit(
        self,
        project_id: str,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        retry_of: str | None = None,
    ) -> dict[str, Any]:
        if action not in _ADMISSION_ACTIONS:
            raise ValidationError(f"unsupported application job action: {action}")
        normalized = self._normalize_args(action, args or {})
        jobs_dir = self._jobs_dir(project_id)
        # The active-job check and queued record must be one cross-process
        # transaction. Otherwise concurrent HTTP requests can both observe an
        # empty queue and start conflicting mutations of the same project.
        with ResourceLock(jobs_dir, self.service.locks_root):
            existing = self.list(project_id)
            if len(existing) >= MAX_PROJECT_JOBS:
                raise ValidationError("project reached its 2000 job record limit")
            if any(job["status"] in _ACTIVE for job in existing):
                raise ValidationError("project already has an active application job")
            if action == "agent_message" and "text" in normalized:
                turn = self.agent.start_turn(project_id, normalized.pop("text"))
                normalized["turn_id"] = turn.turn_id
            elif action == "agent_tool" and "tool" in normalized:
                turn = self.agent.start_tool_turn(project_id, normalized.pop("tool"))
                normalized["turn_id"] = turn.turn_id
            attempt = self._retry_attempt_unlocked(
                project_id,
                action,
                normalized,
                retry_of=retry_of,
            )
            job, path = self._create_job_unlocked(
                jobs_dir,
                project_id,
                action,
                normalized,
                attempt=attempt,
                retry_of=retry_of,
            )
        self._schedule(project_id, path, cancelled=False)
        return job

    def submit_mcp_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Admit one exact MCP call without exposing source or trust as arguments."""

        normalized = self._normalize_args(
            "agent_tool", {"tool": tool_name, "timeout": timeout}
        )
        jobs_dir = self._jobs_dir(project_id)
        with ResourceLock(jobs_dir, self.service.locks_root):
            existing = self.list(project_id)
            if len(existing) >= MAX_PROJECT_JOBS:
                raise ValidationError("project reached its 2000 job record limit")
            if any(job["status"] in _ACTIVE for job in existing):
                raise ValidationError("project already has an active application job")
            turn = self.agent.start_external_tool_turn(
                project_id,
                str(normalized["tool"]),
                arguments,
            )
            persisted = {
                "turn_id": turn.turn_id,
                "timeout": float(normalized["timeout"]),
            }
            job, path = self._create_job_unlocked(
                jobs_dir,
                project_id,
                "agent_tool",
                persisted,
                attempt=1,
                retry_of=None,
            )
        self._schedule(project_id, path, cancelled=False)
        return job

    def resolve_approval(
        self,
        project_id: str,
        *,
        turn_id: str,
        checkpoint_id: str,
        tool_call_id: str,
        tool_name: str,
        effect: str,
        risk: str,
        args_hash: str,
        baseline_revision: int,
        approve: bool,
        timeout: float,
        decision_source: str = "user",
    ) -> tuple[dict[str, Any] | None, Any]:
        """Resolve one approval with a durable continuation written first.

        The queued continuation is an outbox entry.  A crash before the approval
        mutation leaves the turn waiting and the harmless job reports that
        checkpoint; a crash after it leaves enough durable state for startup to
        resume the exact authorized call.
        """

        normalized = self._normalize_args(
            "agent_message", {"turn_id": turn_id, "timeout": timeout}
        )
        if not approve:
            record = self.agent.resolve_pending_approval(
                project_id,
                turn_id=turn_id,
                checkpoint_id=checkpoint_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                effect=effect,
                risk=risk,
                args_hash=args_hash,
                baseline_revision=baseline_revision,
                approve=False,
                decision_source=decision_source,
            )
            return None, record

        jobs_dir = self._jobs_dir(project_id)
        with ResourceLock(jobs_dir, self.service.locks_root):
            existing = self.list(project_id)
            if len(existing) >= MAX_PROJECT_JOBS:
                raise ValidationError("project reached its 2000 job record limit")
            if any(job["status"] in _ACTIVE for job in existing):
                raise ValidationError("project already has an active application job")
            turn = self.agent.store(project_id).load(turn_id)
            action = (
                "agent_tool"
                if turn.user_message.startswith("/pcb_")
                else "agent_message"
            )
            job, path = self._create_job_unlocked(
                jobs_dir,
                project_id,
                action,
                normalized,
                attempt=1,
                retry_of=None,
            )
            try:
                record = self.agent.resolve_pending_approval(
                    project_id,
                    turn_id=turn_id,
                    checkpoint_id=checkpoint_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    effect=effect,
                    risk=risk,
                    args_hash=args_hash,
                    baseline_revision=baseline_revision,
                    approve=True,
                    decision_source=decision_source,
                )
            except BaseException as exc:
                job["status"] = "failed"
                job["completed_at"] = self._next_job_timestamp(job)
                job["error"] = sanitize_user_text(str(exc))[:2048]
                self._write_job(project_id, path, job)
                raise
        self._schedule(project_id, path, cancelled=False)
        return job, record

    def list(self, project_id: str) -> list[dict[str, Any]]:
        jobs_dir = self._jobs_dir(project_id)
        result: list[dict[str, Any]] = []
        for path in jobs_dir.glob("*.json"):
            self._validate_existing_job_path(project_id, path)
            job = self._load_job(project_id, path)
            self._validate_job(job, expected_project=project_id, expected_id=path.stem)
            result.append(job)
        result.sort(key=self._job_sort_key, reverse=True)
        return result

    def get(self, project_id: str, job_id: str) -> dict[str, Any]:
        path = self._job_path(project_id, job_id)
        job = self._load_job(project_id, path)
        self._validate_job(job, expected_project=project_id, expected_id=job_id)
        return job

    def cancel(self, project_id: str, job_id: str) -> dict[str, Any]:
        path = self._job_path(project_id, job_id)
        with ResourceLock(path, self.service.locks_root):
            job = self._load_job(project_id, path)
            self._validate_job(job, expected_project=project_id, expected_id=job_id)
            if job["status"] not in _ACTIVE:
                raise ValidationError("job is no longer cancellable")
            was_queued = job["status"] == "queued"
            job["status"] = "cancel_requested"
            job["cancel_requested_at"] = self._next_job_timestamp(job)
            self._write_job(project_id, path, job)
        with self._guard:
            event = self._cancel.get(job_id)
        if event is not None:
            event.set()
        if was_queued:
            self._cancel_agent_turn(job)
            with ResourceLock(path, self.service.locks_root):
                current = self._load_job(project_id, path)
                if current["status"] == "cancel_requested":
                    current["status"] = "cancelled"
                    current["completed_at"] = self._next_job_timestamp(current)
                    self._write_job(project_id, path, current)
                job = current
        return job

    def retry(self, project_id: str, job_id: str) -> dict[str, Any]:
        previous = self.get(project_id, job_id)
        if previous["status"] not in _RETRYABLE:
            raise ValidationError(
                "only failed, interrupted, or cancelled jobs can be retried"
            )
        self._assert_dispatch_authority(previous)
        return self.submit(
            project_id,
            previous["action"],
            previous["args"],
            retry_of=job_id,
        )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _retry_attempt_unlocked(
        self,
        project_id: str,
        action: str,
        args: dict[str, Any],
        *,
        retry_of: str | None,
    ) -> int:
        if retry_of is None:
            return 1
        previous = self.get(project_id, retry_of)
        if previous["status"] not in _RETRYABLE:
            raise ValidationError(
                "only failed, interrupted, or cancelled jobs can be retried"
            )
        self._assert_dispatch_authority(previous)
        if previous["action"] != action or previous["args"] != args:
            raise ValidationError("retry job must preserve its action and arguments")
        return int(previous["attempt"]) + 1

    def _create_job_unlocked(
        self,
        jobs_dir: Path,
        project_id: str,
        action: str,
        args: dict[str, Any],
        *,
        attempt: int,
        retry_of: str | None,
    ) -> tuple[dict[str, Any], Path]:
        self._validate_jobs_directory(jobs_dir)
        now = utc_timestamp()
        persisted_orders = []
        for candidate in jobs_dir.glob("*.json"):
            self._validate_existing_job_path(project_id, candidate)
            parts = candidate.stem.split("-")
            if len(parts) >= 3 and len(parts[-2]) == 20 and parts[-2].isdigit():
                persisted_orders.append(int(parts[-2]))
        order = max(time.time_ns(), max(persisted_orders, default=-1) + 1)
        job_id = (
            f"{now.replace(':', '').replace('-', '')}-{order:020d}-"
            f"{secrets.token_hex(4)}"
        )
        job = {
            "schema": JOB_SCHEMA,
            "version": JOB_VERSION,
            "id": job_id,
            "project_id": project_id,
            "action": action,
            "args": args,
            "status": "queued",
            "attempt": attempt,
            "retry_of": retry_of,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "cancel_requested_at": None,
            "result": None,
            "error": None,
            "agent_policy": (
                self._agent_policy_snapshot() if action in _ADMISSION_ACTIONS else None
            ),
        }
        path = jobs_dir / f"{job_id}.json"
        self._write_job(project_id, path, job, allow_missing=True)
        return job, path

    @staticmethod
    def _job_sort_key(job: dict[str, Any]) -> tuple[int, int, str, str]:
        job_id = str(job.get("id", ""))
        parts = job_id.split("-")
        embedded_order = (
            int(parts[-2])
            if len(parts) >= 3 and len(parts[-2]) == 20 and parts[-2].isdigit()
            else -1
        )
        if embedded_order >= 0:
            return 1, embedded_order, "", job_id
        # Legacy IDs had no monotonic component.  Keep their deterministic
        # historical fallback, while every newly admitted job sorts after them.
        return 0, 0, str(job.get("created_at", "")), job_id

    @staticmethod
    def _next_job_timestamp(job: dict[str, Any]) -> str:
        """Advance lifecycle time without breaking records after clock rollback."""

        candidates = [utc_timestamp()]
        for name in (
            "created_at",
            "started_at",
            "cancel_requested_at",
            "completed_at",
        ):
            value = job.get(name)
            if isinstance(value, str):
                candidates.append(value)
        return max(candidates)

    def _agent_policy_snapshot(self) -> dict[str, Any]:
        """Bind a queued turn to the exact local authority that admitted it."""

        registry_contract = [
            {
                "name": spec.name,
                "external_name": spec.external_name,
                "effect": spec.effect,
                "risk": spec.risk,
                "allowed_statuses": sorted(spec.allowed_statuses),
                "input_schema": spec.input_schema,
                "annotations": spec.mcp_annotations,
            }
            for spec in sorted(self.agent.registry.specs, key=lambda item: item.name)
        ]
        canonical = json.dumps(
            registry_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return {
            "schema": AGENT_JOB_POLICY_SCHEMA,
            "version": AGENT_JOB_POLICY_VERSION,
            "permission_mode": self.agent.permissions.mode,
            "max_tool_calls": MAX_TOOL_CALLS_PER_TURN,
            "registry_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _validate_agent_policy(value: Any) -> None:
        fields = {
            "schema",
            "version",
            "permission_mode",
            "max_tool_calls",
            "registry_sha256",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValidationError("agent job policy binding is malformed")
        if (
            value["schema"] != AGENT_JOB_POLICY_SCHEMA
            or isinstance(value["version"], bool)
            or value["version"] != AGENT_JOB_POLICY_VERSION
        ):
            raise ValidationError("unsupported agent job policy binding")
        if value["permission_mode"] not in {"workspace", "review", "read_only"}:
            raise ValidationError("agent job permission binding is malformed")
        if (
            isinstance(value["max_tool_calls"], bool)
            or not isinstance(value["max_tool_calls"], int)
            or value["max_tool_calls"] < 1
            or value["max_tool_calls"] > 256
        ):
            raise ValidationError("agent job tool-call bound is malformed")
        fingerprint = value["registry_sha256"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValidationError("agent job registry fingerprint is malformed")

    def _assert_dispatch_authority(self, job: dict[str, Any]) -> None:
        """Reject legacy or differently-authorized mutations before dispatch."""

        if job.get("action") not in _ADMISSION_ACTIONS:
            raise ValidationError(
                "legacy direct mutation jobs cannot be dispatched; submit a new agent turn"
            )
        if job.get("version") != JOB_VERSION:
            raise ValidationError(
                "legacy agent job has no durable permission binding; submit a new turn"
            )
        persisted = job.get("agent_policy")
        self._validate_agent_policy(persisted)
        if not isinstance(persisted, dict):
            raise ValidationError("agent job policy binding is malformed")
        current = self._agent_policy_snapshot()
        if persisted["permission_mode"] != current["permission_mode"]:
            raise ValidationError(
                "agent job permission mode differs from its admitting runtime; "
                "submit a new turn under the current policy"
            )
        if persisted["max_tool_calls"] != current["max_tool_calls"]:
            raise ValidationError(
                "agent job tool-call bound differs from its admitting runtime; "
                "submit a new turn"
            )
        if persisted["registry_sha256"] != current["registry_sha256"]:
            raise ValidationError(
                "agent job tool authority changed after admission; submit a new turn"
            )

    @staticmethod
    def _validate_jobs_directory(jobs_dir: Path) -> None:
        """Reject a missing, renamed, or symlinked job-record directory."""

        try:
            if jobs_dir.name != "jobs" or jobs_dir.is_symlink():
                raise ValidationError("application jobs directory cannot be a symlink")
            info = jobs_dir.stat()
            parent = jobs_dir.parent.resolve(strict=True)
            resolved = jobs_dir.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationError("application jobs directory is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode) or resolved.parent != parent:
            raise ValidationError("application jobs directory escapes its project")

    def _jobs_dir(self, project_id: str) -> Path:
        project_root = self.service.project_root(project_id)
        try:
            if project_root.is_symlink() or not project_root.is_dir():
                raise ValidationError("application project root is not a directory")
            resolved_project = project_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationError("application project root is unavailable") from exc
        jobs_dir = project_root / "jobs"
        self._validate_jobs_directory(jobs_dir)
        if jobs_dir.resolve(strict=True).parent != resolved_project:
            raise ValidationError("application jobs directory escapes its project")
        return jobs_dir

    def _validate_existing_job_path(self, project_id: str, path: Path) -> None:
        jobs_dir = self._jobs_dir(project_id)
        try:
            if path.parent != jobs_dir or path.is_symlink():
                raise ValidationError("application job path escapes its project")
            info = path.stat()
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationError("application job does not exist") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or resolved.parent != jobs_dir.resolve(strict=True)
        ):
            raise ValidationError("application job is not a private project record")

    def _load_job(self, project_id: str, path: Path) -> dict[str, Any]:
        self._validate_existing_job_path(project_id, path)
        value = load_json_limited(path, APP_FILE_LIMIT)
        if not isinstance(value, dict):
            raise ValidationError("application job record is malformed")
        return value

    def _write_job(
        self,
        project_id: str,
        path: Path,
        value: dict[str, Any],
        *,
        allow_missing: bool = False,
    ) -> None:
        jobs_dir = self._jobs_dir(project_id)
        if path.parent != jobs_dir:
            raise ValidationError("application job path escapes its project")
        if path.exists() or path.is_symlink():
            self._validate_existing_job_path(project_id, path)
        elif not allow_missing:
            raise ValidationError("application job does not exist")
        atomic_write_json(path, value)

    def _schedule(self, project_id: str, path: Path, *, cancelled: bool) -> None:
        self._validate_existing_job_path(project_id, path)
        event = threading.Event()
        if cancelled:
            event.set()
        with self._guard:
            self._cancel[path.stem] = event
        self._pool.submit(self._run, project_id, path, event)

    @staticmethod
    def _execution_lease_resource(path: Path) -> Path:
        return path.parent / ".execution-leases" / path.stem

    def _cancel_agent_turn(
        self,
        job: dict[str, Any],
        reason: str = "the queued application job was cancelled before dispatch",
        *,
        decision_source: str = "job_runner",
    ) -> None:
        args = job.get("args")
        turn_id = args.get("turn_id") if isinstance(args, dict) else None
        if job.get("action") not in {"agent_message", "agent_tool"} or not isinstance(
            turn_id, str
        ):
            return
        try:
            self.agent.store(str(job["project_id"])).cancel(
                turn_id,
                reason,
                decision_source=decision_source,
            )
        except PCBDraftError:
            # A concurrent worker may already have closed the same aggregate.
            pass

    def _recover_orphan_turns(self) -> None:
        """Close orphan turns whose admitting permission policy is unknowable."""

        for project in self.service.list_projects():
            project_id = project["id"]
            jobs_dir = self._jobs_dir(project_id)
            closed_count = 0
            with ResourceLock(jobs_dir, self.service.locks_root):
                jobs = self.list(project_id)
                if len(jobs) >= MAX_PROJECT_JOBS or any(
                    job["status"] in _ACTIVE for job in jobs
                ):
                    continue
                referenced = {
                    turn_id
                    for job in jobs
                    if job["status"] in _ACTIVE
                    if isinstance(job.get("args"), dict)
                    and isinstance((turn_id := job["args"].get("turn_id")), str)
                }
                candidates = self.agent.store(project_id).list(
                    statuses=(TurnStatus.QUEUED, TurnStatus.RUNNING)
                )
                orphans = [
                    turn for turn in candidates if turn.turn_id not in referenced
                ]
                if not orphans:
                    continue
                reason = (
                    "startup found an agent turn without a permission-bound job; "
                    "it was not dispatched and must be submitted again"
                )
                store = self.agent.store(project_id)
                for orphan in orphans:
                    if orphan.status is TurnStatus.RUNNING:
                        store.interrupt_active(orphan.turn_id, reason)
                    else:
                        store.cancel(
                            orphan.turn_id,
                            reason,
                            decision_source="job_runner_recovery",
                        )
                    closed_count += 1
            if closed_count:
                self.service.record_progress(
                    project_id,
                    "job.recovery_blocked",
                    f"Closed {closed_count} orphan agent turn(s) because their original permission policy was unavailable.",
                    level="warning",
                )

    def _run(self, project_id: str, path: Path, cancel: threading.Event) -> None:
        try:
            path = self._job_path(project_id, path.stem)
        except PCBDraftError:
            # A swapped directory or record is never followed by a worker. The
            # invalid artifact remains visible to a subsequent list/get audit.
            with self._guard:
                self._cancel.pop(path.stem, None)
            return
        lease = ResourceLock(
            self._execution_lease_resource(path), self.service.locks_root, timeout=0
        )
        try:
            lease.acquire()
        except PCBDraftError as exc:
            if "resource is locked by another runtime process" in str(exc):
                with self._guard:
                    self._cancel.pop(path.stem, None)
                return
            raise
        try:
            self._run_owned(project_id, path, cancel)
        finally:
            lease.release()
            with self._guard:
                self._cancel.pop(path.stem, None)

    def _run_owned(self, project_id: str, path: Path, cancel: threading.Event) -> None:
        job = self._load_job(project_id, path)
        try:
            with ResourceLock(path, self.service.locks_root):
                job = self._load_job(project_id, path)
                self._validate_job(
                    job, expected_project=project_id, expected_id=path.stem
                )
                if cancel.is_set() or job["status"] == "cancel_requested":
                    self._cancel_agent_turn(job)
                    job["status"] = "cancelled"
                    job["completed_at"] = self._next_job_timestamp(job)
                    self._write_job(project_id, path, job)
                    return
                if job["status"] != "queued":
                    return
                try:
                    self._assert_dispatch_authority(job)
                except ValidationError as exc:
                    self._cancel_agent_turn(
                        job,
                        str(exc),
                        decision_source="job_runner_policy",
                    )
                    job["status"] = "failed"
                    job["completed_at"] = self._next_job_timestamp(job)
                    job["error"] = sanitize_user_text(str(exc))[:2048]
                    self._write_job(project_id, path, job)
                    return
                job["status"] = "running"
                job["started_at"] = self._next_job_timestamp(job)
                self._write_job(project_id, path, job)
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
            turn_id = job["args"].get("turn_id")
            if job["action"] in {"agent_message", "agent_tool"} and isinstance(
                turn_id, str
            ):
                turn = self.agent.store(project_id).load(turn_id)
                result["turn_id"] = turn.turn_id
                result["turn_status"] = turn.status.value
                result["pending_approval"] = self.agent.approval_payload(turn)
            with ResourceLock(path, self.service.locks_root):
                current = self._load_job(project_id, path)
                cancelled_after_dispatch = (
                    cancel.is_set() or current["status"] == "cancel_requested"
                )
                current["status"] = (
                    "completed_after_cancel"
                    if cancelled_after_dispatch
                    else "completed"
                )
                current["completed_at"] = self._next_job_timestamp(current)
                current["result"] = result
                self._write_job(project_id, path, current)
            self.service.record_progress(
                project_id,
                "job.complete",
                (
                    f"Completed {job['action']} after cancellation was requested"
                    if cancelled_after_dispatch
                    else f"Completed {job['action']} job"
                ),
                level="warning" if cancelled_after_dispatch else "info",
            )
        except Exception as exc:  # noqa: BLE001 - persist every worker failure
            try:
                with ResourceLock(path, self.service.locks_root):
                    current = self._load_job(project_id, path)
                    if current["status"] in _ACTIVE:
                        current["status"] = "failed"
                        current["completed_at"] = self._next_job_timestamp(current)
                        current["error"] = sanitize_user_text(str(exc))[:2048]
                        self._write_job(project_id, path, current)
                if current["status"] == "failed" and isinstance(
                    current.get("error"), str
                ):
                    self.service.record_progress(
                        project_id,
                        "job.failed",
                        current["error"],
                        level="error",
                    )
            except PCBDraftError:
                pass

    def _dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        self._assert_dispatch_authority(job)
        project_id = job["project_id"]
        action = job["action"]
        args = job["args"]
        timeout = float(args.get("timeout", 180.0))
        turn_id = args.get("turn_id")
        if action not in _ADMISSION_ACTIONS or not isinstance(turn_id, str):
            raise ValidationError(
                "application mutation job is not bound to a durable agent turn"
            )
        return self.agent.run_turn(
            project_id,
            turn_id,
            timeout=timeout,
            cancellation_requested=lambda: self._cancel_requested(
                project_id, job["id"]
            ),
        )

    @staticmethod
    def _normalize_args(action: str, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ValidationError("job arguments must be an object")
        if action == "agent_message":
            allowed = {"text", "turn_id", "timeout"}
        elif action == "agent_tool":
            allowed = {"tool", "turn_id", "timeout"}
        elif action == "message":
            allowed = {"text", "timeout"}
        else:
            allowed = {"timeout"}
        if set(args) - allowed:
            raise ValidationError("job arguments contain unsupported fields")
        result: dict[str, Any] = {}
        if action in {"agent_message", "message"} and "turn_id" not in args:
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValidationError("message job requires non-empty text")
            encoded = text.encode("utf-8")
            if len(encoded) > 16_384:
                raise ValidationError("message job text exceeds 16 KiB")
            result["text"] = sanitize_user_text(text.strip())
        if action == "agent_tool" and "turn_id" not in args:
            tool = args.get("tool")
            if not isinstance(tool, str) or not tool:
                raise ValidationError("agent tool job requires a tool name")
            result["tool"] = tool
        if action in {"agent_message", "agent_tool"} and "turn_id" in args:
            turn_id = args.get("turn_id")
            if (
                not isinstance(turn_id, str)
                or not turn_id.startswith("turn-")
                or len(turn_id) > 256
                or any(
                    character
                    not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
                    for character in turn_id
                )
            ):
                raise ValidationError("agent job turn id is invalid")
            result["turn_id"] = turn_id
        if action == "agent_message" and "text" in args and "turn_id" in args:
            raise ValidationError("agent message job cannot mix text and turn id")
        if action == "agent_tool" and "tool" in args and "turn_id" in args:
            raise ValidationError("agent tool job cannot mix tool and turn id")
        timeout = args.get("timeout", 180.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValidationError("job timeout must be a number")
        if timeout <= 0 or timeout > 1800:
            raise ValidationError("job timeout must be in (0, 1800] seconds")
        result["timeout"] = float(timeout)
        return result

    def _cancel_requested(self, project_id: str, job_id: str) -> bool:
        """Read both local and cross-process cancellation at a safe boundary."""

        with self._guard:
            event = self._cancel.get(job_id)
        if event is not None and event.is_set():
            return True
        return self.get(project_id, job_id)["status"] == "cancel_requested"

    @staticmethod
    def _valid_job_id(value: Any) -> bool:
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 256
            and all(character in "0123456789TZabcdef-" for character in value)
        )

    @staticmethod
    def _validate_job_timestamp(
        value: Any, label: str, *, optional: bool = False
    ) -> None:
        if optional and value is None:
            return
        if not isinstance(value, str):
            raise ValidationError(f"application job {label} is malformed")
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError as exc:
            raise ValidationError(f"application job {label} is malformed") from exc

    def _job_path(self, project_id: str, job_id: str) -> Path:
        if not self._valid_job_id(job_id):
            raise ValidationError("application job id is invalid")
        path = self._jobs_dir(project_id) / f"{job_id}.json"
        self._validate_existing_job_path(project_id, path)
        return path

    @staticmethod
    def _validate_job(value: Any, *, expected_project: str, expected_id: str) -> None:
        legacy_fields = {
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
        if not isinstance(value, dict):
            raise ValidationError("application job record is malformed")
        version = value.get("version")
        if isinstance(version, bool):
            raise ValidationError("unsupported application job schema/version")
        expected_fields = (
            legacy_fields
            if version == LEGACY_JOB_VERSION
            else legacy_fields | {"agent_policy"}
            if version == JOB_VERSION
            else set()
        )
        if set(value) != expected_fields:
            raise ValidationError("application job record is malformed")
        if value["schema"] != JOB_SCHEMA or version not in {
            LEGACY_JOB_VERSION,
            JOB_VERSION,
        }:
            raise ValidationError("unsupported application job schema/version")
        if (
            not JobRunner._valid_job_id(value["id"])
            or not JobRunner._valid_job_id(expected_id)
            or value["id"] != expected_id
            or not isinstance(value["project_id"], str)
            or value["project_id"] != expected_project
        ):
            raise ValidationError("application job identity is malformed")
        action = value["action"]
        if (
            not isinstance(action, str)
            or action not in _ACTIONS
            or not isinstance(value["args"], dict)
        ):
            raise ValidationError("application job action is malformed")
        JobRunner._normalize_args(action, value["args"])

        status = value["status"]
        if not isinstance(status, str) or status not in _JOB_STATUSES:
            raise ValidationError("application job status is malformed")
        attempt = value["attempt"]
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt < 1
            or attempt > MAX_PROJECT_JOBS
        ):
            raise ValidationError("application job attempt is malformed")
        retry_of = value["retry_of"]
        if retry_of is None:
            if attempt != 1:
                raise ValidationError("initial application job attempt must be one")
        elif (
            not JobRunner._valid_job_id(retry_of)
            or retry_of == value["id"]
            or attempt < 2
        ):
            raise ValidationError("application job retry binding is malformed")

        JobRunner._validate_job_timestamp(value["created_at"], "created_at")
        for timestamp_name in (
            "started_at",
            "completed_at",
            "cancel_requested_at",
        ):
            JobRunner._validate_job_timestamp(
                value[timestamp_name], timestamp_name, optional=True
            )
        created_at = value["created_at"]
        started_at = value["started_at"]
        completed_at = value["completed_at"]
        cancel_requested_at = value["cancel_requested_at"]
        for timestamp in (started_at, completed_at, cancel_requested_at):
            if timestamp is not None and timestamp < created_at:
                raise ValidationError("application job timestamps are out of order")
        if (
            (
                started_at is not None
                and completed_at is not None
                and completed_at < started_at
            )
            or (
                started_at is not None
                and cancel_requested_at is not None
                and cancel_requested_at < started_at
            )
            or (
                cancel_requested_at is not None
                and completed_at is not None
                and completed_at < cancel_requested_at
            )
        ):
            raise ValidationError("application job timestamps are out of order")

        result = value["result"]
        error = value["error"]
        if result is not None and not isinstance(result, dict):
            raise ValidationError("application job result is malformed")
        if error is not None and (
            not isinstance(error, str)
            or not error.strip()
            or len(error.encode("utf-8")) > 2048
            or "\x00" in error
        ):
            raise ValidationError("application job error is malformed")
        if status == "queued" and any(
            item is not None
            for item in (started_at, completed_at, cancel_requested_at, result, error)
        ):
            raise ValidationError("queued application job lifecycle is malformed")
        if status == "running" and (
            started_at is None
            or completed_at is not None
            or cancel_requested_at is not None
            or result is not None
            or error is not None
        ):
            raise ValidationError("running application job lifecycle is malformed")
        if status == "cancel_requested" and (
            completed_at is not None
            or cancel_requested_at is None
            or result is not None
            or error is not None
        ):
            raise ValidationError(
                "cancel-requested application job lifecycle is malformed"
            )
        if status in {"completed", "completed_after_cancel"} and (
            started_at is None
            or completed_at is None
            or not isinstance(result, dict)
            or error is not None
            or (status == "completed" and cancel_requested_at is not None)
            or (status == "completed_after_cancel" and cancel_requested_at is None)
        ):
            raise ValidationError("completed application job lifecycle is malformed")
        if status == "cancelled" and (
            completed_at is None
            or cancel_requested_at is None
            or result is not None
            or error is not None
        ):
            raise ValidationError("cancelled application job lifecycle is malformed")
        if status == "failed" and (
            completed_at is None or result is not None or not isinstance(error, str)
        ):
            raise ValidationError("failed application job lifecycle is malformed")
        if status == "interrupted" and (
            started_at is None
            or completed_at is None
            or result is not None
            or not isinstance(error, str)
        ):
            raise ValidationError("interrupted application job lifecycle is malformed")
        if version == JOB_VERSION:
            policy = value["agent_policy"]
            if action in _ADMISSION_ACTIONS:
                JobRunner._validate_agent_policy(policy)
                normalized = JobRunner._normalize_args(action, value["args"])
                if normalized != value["args"] or "turn_id" not in normalized:
                    raise ValidationError("agent job is not bound to a durable turn")
            elif policy is not None:
                raise ValidationError(
                    "direct application job cannot carry agent authority"
                )

    def _recover_jobs(self) -> None:
        for project in self.service.list_projects():
            project_id = project["id"]
            for job in self.list(project_id):
                if job["status"] not in _ACTIVE:
                    continue
                path = self._job_path(project_id, job["id"])
                lease = ResourceLock(
                    self._execution_lease_resource(path),
                    self.service.locks_root,
                    timeout=0,
                )
                try:
                    lease.acquire()
                except PCBDraftError as exc:
                    if "resource is locked by another runtime process" in str(exc):
                        # Another JobRunner still owns and is executing this job.
                        continue
                    raise
                schedule = False
                try:
                    with ResourceLock(path, self.service.locks_root):
                        current = self._load_job(project_id, path)
                        self._validate_job(
                            current,
                            expected_project=project_id,
                            expected_id=path.stem,
                        )
                        if current["status"] == "cancel_requested":
                            self._cancel_agent_turn(current)
                            current["status"] = "cancelled"
                            current["completed_at"] = self._next_job_timestamp(current)
                            self._write_job(project_id, path, current)
                        elif current["status"] == "queued":
                            try:
                                self._assert_dispatch_authority(current)
                            except ValidationError as exc:
                                self._cancel_agent_turn(
                                    current,
                                    str(exc),
                                    decision_source="job_runner_recovery",
                                )
                                current["status"] = "failed"
                                current["completed_at"] = self._next_job_timestamp(
                                    current
                                )
                                current["error"] = sanitize_user_text(str(exc))[:2048]
                                self._write_job(project_id, path, current)
                            else:
                                schedule = True
                        elif current["status"] == "running":
                            # The lease is free, so the prior owner is dead.  Keep
                            # the dispatch marker while closing the turn/tool in
                            # one aggregate write.  Retry will therefore fail
                            # closed when that effect may have started.
                            self._interrupt_recovered_turn(current)
                            current["status"] = "interrupted"
                            current["completed_at"] = self._next_job_timestamp(current)
                            current["error"] = (
                                "The application stopped before this job recorded "
                                "completion; its PCB effect was not replayed."
                            )
                            self._write_job(project_id, path, current)
                finally:
                    lease.release()
                if schedule:
                    self._schedule(project_id, path, cancelled=False)

    def _interrupt_recovered_turn(self, job: dict[str, Any]) -> None:
        args = job.get("args")
        turn_id = args.get("turn_id") if isinstance(args, dict) else None
        if job.get("action") not in {"agent_message", "agent_tool"} or not isinstance(
            turn_id, str
        ):
            return
        store = self.agent.store(str(job["project_id"]))
        try:
            turn = store.load(turn_id)
            if turn.status is TurnStatus.QUEUED:
                store.update(
                    turn_id,
                    TurnStatus.INTERRUPTED,
                    stop_reason="interrupted before the recovered job started its turn",
                    error="the previous job owner stopped before the turn started",
                )
            elif turn.status is TurnStatus.RUNNING:
                store.interrupt_active(
                    turn_id,
                    "the previous job owner stopped before the PCB tool receipt was committed",
                )
        except PCBDraftError:
            # Terminal and approval-waiting turns already have a coherent durable
            # boundary; malformed records remain visible through normal reads.
            return
