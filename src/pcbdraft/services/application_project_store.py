"""Project-record storage helpers composed into :class:`ApplicationService`.

This module owns filesystem path validation, bounded record loading, atomic
record/event writes, attempt-record reads, and public view projection. Business
decisions about when a project changes state remain in the application service.
The module deliberately does not import ``pcbdraft.services.application``.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.core.runs import utc_timestamp
from pcbdraft.domain.ir import Design
from pcbdraft.domain.task_contract import candidate_gate_status, evaluate_task_coverage
from pcbdraft.services.managed import IR_NAME
from pcbdraft.services.progress import ProductSessionTerminalReceipt

APP_PROJECT_SCHEMA = "pcbdraft-application-project"
APP_PROJECT_VERSION = 1
CONVERSATION_SCHEMA = "pcbdraft-conversation-record"
CONVERSATION_VERSION = 1
ATTEMPT_SCHEMA = "pcbdraft-generation-attempt"
ATTEMPT_VERSION = 2
_ATTEMPT_FIELDS = {
    "schema",
    "version",
    "id",
    "status",
    "phase",
    "runtime",
    "assurance",
    "started_at",
    "completed_at",
    "part_ids",
    "requested_parts",
    "files",
    "error",
}
APP_FILE_LIMIT = 4 * 1024 * 1024
MAX_MESSAGES = 2_000
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,79}")
_STATE_FIELDS = {
    "schema",
    "version",
    "id",
    "name",
    "created_at",
    "updated_at",
    "status",
    "provider",
    "revision",
    "design_revision",
    "event_sequence",
    "active_transaction",
    "last_transaction",
    "last_validation",
    "last_preview",
    "last_release",
}
_CONVERSATION_FIELDS = {
    "schema",
    "version",
    "messages",
    "proposal",
    "decisions",
}


@dataclass(frozen=True)
class ApplicationProject:
    root: Path
    state: dict[str, Any]
    conversation: dict[str, Any]

    @property
    def design_root(self) -> Path:
        return self.root / "design"


def _public_readiness_record(value: Any) -> Any:
    """Normalize legacy records without mutating retained audit artifacts."""

    if not isinstance(value, dict):
        return value
    result = dict(value)
    result.setdefault(
        "production_evidence_complete", value.get("production_ready") is True
    )
    result["production_ready"] = False
    result["production_claimed"] = False
    return result


class ApplicationProjectStoreMixin:
    """Filesystem persistence and public projection for application projects."""

    def _project_path(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            raise ValidationError("application project id is invalid")
        path = self.projects_root / project_id
        if path.is_symlink():
            raise ValidationError("application project path is unsafe")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValidationError(
                f"application project does not exist: {project_id}"
            ) from exc
        if resolved.parent != self.projects_root or not resolved.is_dir():
            raise ValidationError("application project path escapes the workspace")
        return resolved

    def _open(self, project_id: str) -> ApplicationProject:
        return self._open_path(self._project_path(project_id))

    def _open_path(self, root: Path) -> ApplicationProject:
        state = load_json_limited(root / "project.json", APP_FILE_LIMIT)
        conversation = load_json_limited(root / "conversation.json", APP_FILE_LIMIT)
        self._validate_state(state, expected_id=root.name)
        self._validate_conversation(conversation)
        return ApplicationProject(root=root, state=state, conversation=conversation)

    @staticmethod
    def _validate_state(value: Any, *, expected_id: str) -> None:
        if not isinstance(value, dict) or set(value) != _STATE_FIELDS:
            raise ValidationError("application project record is malformed")
        if (
            value["schema"] != APP_PROJECT_SCHEMA
            or value["version"] != APP_PROJECT_VERSION
        ):
            raise ValidationError("unsupported application project schema/version")
        if value["id"] != expected_id or not _PROJECT_ID.fullmatch(value["id"]):
            raise ValidationError("application project identity is malformed")
        for field in ("name", "created_at", "updated_at", "status", "provider"):
            if not isinstance(value[field], str) or not value[field]:
                raise ValidationError(
                    f"application project field is malformed: {field}"
                )
        for field in ("revision", "design_revision", "event_sequence"):
            if (
                isinstance(value[field], bool)
                or not isinstance(value[field], int)
                or value[field] < 0
            ):
                raise ValidationError(
                    f"application project counter is malformed: {field}"
                )

    @staticmethod
    def _validate_conversation(value: Any) -> None:
        if not isinstance(value, dict) or set(value) != _CONVERSATION_FIELDS:
            raise ValidationError("conversation record is malformed")
        if (
            value["schema"] != CONVERSATION_SCHEMA
            or value["version"] != CONVERSATION_VERSION
        ):
            raise ValidationError("unsupported conversation record schema/version")
        if (
            not isinstance(value["messages"], list)
            or len(value["messages"]) > MAX_MESSAGES
        ):
            raise ValidationError("conversation message history is malformed")
        if not isinstance(value["decisions"], dict):
            raise ValidationError("conversation decisions are malformed")

    @classmethod
    def _append_message(
        cls,
        conversation: dict[str, Any],
        role: str,
        kind: str,
        text: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        if len(conversation["messages"]) >= MAX_MESSAGES:
            raise ValidationError("conversation reached its 2000 message limit")
        conversation["messages"].append(
            {
                "id": secrets.token_hex(8),
                "role": role,
                "kind": kind,
                "text": cls._project_store_sanitize_secret_text(text),
                "created_at": utc_timestamp(),
                "data": data or {},
            }
        )

    @classmethod
    def _event(
        cls,
        state: dict[str, Any],
        root: Path,
        kind: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        state["event_sequence"] += 1
        sequence = state["event_sequence"]
        content_hash: str | None = None
        ir_path = root / "design" / IR_NAME
        if ir_path.is_file() and not ir_path.is_symlink():
            try:
                content_hash = Design.from_dict(
                    load_json_limited(ir_path, 16 * 1024 * 1024)
                ).content_hash()
            except PCBDraftError:
                content_hash = None
        atomic_write_json(
            root / "events" / f"{sequence:08d}.json",
            {
                "schema": "pcbdraft-structured-event",
                "version": 1,
                "sequence": sequence,
                "kind": kind,
                "level": level,
                "message": cls._project_store_sanitize_secret_text(message)[:2048],
                "created_at": utc_timestamp(),
                "canonical_revision": state.get("revision"),
                "design_revision": state.get("design_revision"),
                "design_content_hash": content_hash,
                "binding_state": "bound" if content_hash is not None else "no_design",
            },
        )

    @staticmethod
    def _write_records(
        root: Path, state: dict[str, Any], conversation: dict[str, Any]
    ) -> None:
        atomic_write_json(root / "conversation.json", conversation)
        atomic_write_json(root / "project.json", state)

    @staticmethod
    def _summary(project: ApplicationProject) -> dict[str, Any]:
        return {
            "id": project.state["id"],
            "name": project.state["name"],
            "status": project.state["status"],
            "updated_at": project.state["updated_at"],
            "design_revision": project.state["design_revision"],
            "provider": project.state["provider"],
        }

    @staticmethod
    def _attempt_records(project: ApplicationProject) -> list[dict[str, Any]]:
        attempts = project.root / "attempts"
        if attempts.is_symlink() or not attempts.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for candidate in sorted(attempts.iterdir(), reverse=True):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                record = load_json_limited(candidate / "attempt.json", APP_FILE_LIMIT)
            except PCBDraftError:
                continue
            if not ApplicationProjectStoreMixin._valid_attempt_record(
                record, expected_id=candidate.name
            ):
                continue
            public = dict(record)
            public["root"] = str(candidate)
            result.append(public)
            if len(result) >= 50:
                break
        return result

    @staticmethod
    def _valid_attempt_record(value: Any, *, expected_id: str) -> bool:
        if not isinstance(value, dict) or set(value) != _ATTEMPT_FIELDS:
            return False
        files = value.get("files")
        string_lists = (value.get("part_ids"), value.get("requested_parts"))
        return bool(
            value.get("schema") == ATTEMPT_SCHEMA
            and value.get("version") == ATTEMPT_VERSION
            and value.get("id") == expected_id
            and value.get("status") in {"running", "completed", "failed", "interrupted"}
            and isinstance(value.get("phase"), str)
            and isinstance(value.get("runtime"), str)
            and value.get("assurance") in {"unknown", "provisional"}
            and isinstance(value.get("started_at"), str)
            and (
                value.get("completed_at") is None
                or isinstance(value.get("completed_at"), str)
            )
            and all(
                isinstance(items, list)
                and len(items) <= 2_000
                and all(isinstance(item, str) for item in items)
                for items in string_lists
            )
            and isinstance(files, dict)
            and set(files)
            == {"request", "plan", "semantic_ir", "part_catalog", "retained_native"}
            and all(item is None or isinstance(item, str) for item in files.values())
            and (value.get("error") is None or isinstance(value.get("error"), str))
        )

    @staticmethod
    def _latest_product_terminal(project: ApplicationProject) -> dict[str, Any] | None:
        directory = project.root / "product-sessions"
        if not directory.is_dir() or directory.is_symlink():
            return None
        latest: tuple[str, dict[str, Any]] | None = None
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                continue
            try:
                receipt = ProductSessionTerminalReceipt.from_dict(
                    load_json_limited(path, APP_FILE_LIMIT)
                ).to_dict()
            except PCBDraftError:
                continue
            created_at = str(receipt["created_at"])
            if latest is None or created_at > latest[0]:
                receipt["artifact"] = path.relative_to(project.root).as_posix()
                latest = (created_at, receipt)
        return latest[1] if latest is not None else None

    def _public_project(self, project: ApplicationProject) -> dict[str, Any]:
        design: dict[str, Any] | None = None
        managed = None
        if project.design_root.is_dir() and not project.design_root.is_symlink():
            managed = self._project_store_open_managed_project(project.design_root)
            design = {
                "root": str(managed.root),
                "design_id": managed.design.design_id,
                "name": managed.design.name,
                "content_hash": managed.design.content_hash(),
                "drift": list(managed.drift()),
                "files": {
                    key: str(managed.root / relative)
                    for key, relative in managed.manifest["files"].items()
                },
            }
        active_change: dict[str, Any] | None = None
        transaction_id = project.state.get("active_transaction")
        if isinstance(transaction_id, str) and re.fullmatch(
            r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", transaction_id
        ):
            transaction = project.root / "transactions" / transaction_id
            receipt = load_json_limited(transaction / "receipt.json", APP_FILE_LIMIT)
            diff = load_json_limited(transaction / "semantic-diff.json", APP_FILE_LIMIT)
            active_change = {
                "transaction_id": transaction_id,
                "request": receipt.get("request"),
                "status": receipt.get("status"),
                "diff": diff,
                "validation": self._project_store_public_readiness(
                    receipt.get("validation")
                ),
                "progress": self._project_store_transaction_progress(receipt),
            }
        public_state = dict(project.state)
        public_state["last_validation"] = self._project_store_public_readiness(
            project.state["last_validation"]
        )
        public_state["last_release"] = self._project_store_public_readiness(
            project.state["last_release"]
        )
        if managed is None:
            candidate_gate = {
                "outcome": "incomplete",
                "passed": False,
                "reason": "design_missing",
                "source_design_revision": None,
                "source_content_hash": None,
            }
            task_coverage = {
                "schema": "pcbdraft-task-coverage",
                "version": 1,
                "outcome": "incomplete",
                "complete": False,
                "reason": "design_missing",
                "source_design_revision": int(project.state["design_revision"]),
                "source_content_hash": None,
                "validation_run_id": None,
                "items": [],
            }
        else:
            candidate_gate = candidate_gate_status(
                project.state.get("last_validation"),
                design_revision=int(project.state["design_revision"]),
                design_content_hash=managed.design.content_hash(),
            )
            if candidate_gate["passed"]:
                try:
                    self._require_current_candidate_validation(project, managed.design)
                except PCBDraftError:
                    candidate_gate = {
                        **candidate_gate,
                        "outcome": "incomplete",
                        "passed": False,
                        "reason": "candidate_validation_evidence_invalid",
                    }
            if hasattr(managed.design, "requirements"):
                task_coverage = evaluate_task_coverage(
                    managed.design,
                    None
                    if candidate_gate["reason"]
                    == "candidate_validation_evidence_invalid"
                    else project.state.get("last_validation"),
                    design_revision=int(project.state["design_revision"]),
                )
            else:
                task_coverage = {
                    "schema": "pcbdraft-task-coverage",
                    "version": 1,
                    "outcome": "incomplete",
                    "complete": False,
                    "reason": "task_contract_unavailable",
                    "source_design_revision": int(project.state["design_revision"]),
                    "source_content_hash": managed.design.content_hash(),
                    "validation_run_id": None,
                    "items": [],
                }
        return {
            "schema": "pcbdraft-application-view",
            "version": 1,
            "project": self._summary(project),
            "state": public_state,
            "conversation": project.conversation,
            "design": design,
            "product_status": {
                "conversation_terminal": self._latest_product_terminal(project),
                "candidate_gate": candidate_gate,
                "task_coverage": task_coverage,
            },
            "artifacts": {
                "previews": project.state["last_preview"],
                "validation": public_state["last_validation"],
                "release": public_state["last_release"],
            },
            "attempts": self._attempt_records(project),
            "active_change": active_change,
            "events": self.events(
                project.state["id"], after=max(0, project.state["event_sequence"] - 50)
            ),
        }
