"""Typed, transport-neutral response contracts for GUI conversation sessions.

The local Web adapter and future terminal clients share these JSON shapes.  This
module deliberately knows nothing about FastAPI, jobs, or persistent state; it
only serializes already-authorized presentation values into the public contract.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

SESSION_SCHEMA = "pcbdraft-gui-session"
SESSION_VERSION = 3


class GuiActionResponse(TypedDict):
    """Result returned when a conversation job is started or stopped."""

    project_id: str
    job_id: str | None
    status: str
    turn_id: NotRequired[str | None]


class GuiSessionMessage(TypedDict):
    """One bounded user or assistant message in a reconnect response."""

    id: str
    turn_id: str
    role: str
    text: str
    status: str
    created_at: str


class GuiVisibleJob(TypedDict):
    """Public subset of a canonical job record."""

    id: str | None
    turn_id: str | None
    status: str | None
    attempt: int | None
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    project_revision: int | None
    design_content_hash: str | None


class GuiActiveTurn(TypedDict):
    """The active job/turn binding included in reconnect state."""

    job_id: str
    turn_id: str | None
    status: str
    started_at: str | None


class GuiProductStatus(TypedDict):
    """Product-level progress shown alongside the conversation transcript."""

    conversation: str
    candidate_gate: dict[str, Any]
    task_coverage: dict[str, Any]


class GuiSessionResponse(TypedDict):
    """Stable reconnect payload shared by local GUI/TUI clients."""

    schema: str
    version: int
    project_id: str
    status: str
    active_turn: GuiActiveTurn | None
    pending_approval: dict[str, Any] | None
    messages: list[GuiSessionMessage]
    legacy_session_id: str | None
    product_status: GuiProductStatus
    jobs: list[GuiVisibleJob]
    canonical_revision: int | None
    design_revision: int | None
    content_hash: str | None


def action_response(
    *,
    project_id: str,
    job_id: str | None,
    status: str,
    turn_id: str | None = None,
    include_turn_id: bool = True,
) -> GuiActionResponse:
    """Serialize one start/stop result without changing optional-key behavior."""

    response: GuiActionResponse = {
        "project_id": project_id,
        "job_id": job_id,
        "status": status,
    }
    if include_turn_id:
        response["turn_id"] = turn_id
    return response


def session_message(
    *,
    message_id: str,
    turn_id: str,
    role: str,
    text: str,
    status: str,
    created_at: str,
) -> GuiSessionMessage:
    """Serialize a bounded conversation message."""

    return {
        "id": message_id,
        "turn_id": turn_id,
        "role": role,
        "text": text,
        "status": status,
        "created_at": created_at,
    }


def visible_job(job: dict[str, Any]) -> GuiVisibleJob:
    """Project the public allowlist from a canonical job record."""

    args = job.get("args")
    result = job.get("result")
    return {
        "id": job.get("id"),
        "turn_id": args.get("turn_id") if isinstance(args, dict) else None,
        "status": job.get("status"),
        "attempt": job.get("attempt"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "project_revision": (
            result.get("project_revision") if isinstance(result, dict) else None
        ),
        "design_content_hash": (
            result.get("design_content_hash") if isinstance(result, dict) else None
        ),
    }


def active_turn(job: dict[str, Any], *, turn_id: str | None) -> GuiActiveTurn:
    """Serialize the current canonical job/turn binding."""

    return {
        "job_id": job["id"],
        "turn_id": turn_id,
        "status": job["status"],
        "started_at": job.get("started_at") or job.get("created_at"),
    }


def session_response(
    *,
    project_id: str,
    status: str,
    active: GuiActiveTurn | None,
    pending_approval: dict[str, Any] | None,
    messages: list[GuiSessionMessage],
    legacy_session_id: str | None,
    jobs: list[GuiVisibleJob],
    canonical_revision: int | None,
    design_revision: int | None,
    content_hash: str | None,
    product_status: GuiProductStatus | None = None,
) -> GuiSessionResponse:
    """Serialize complete reconnect state using the versioned public schema."""

    if product_status is None:
        product_status = {
            "conversation": "not_started",
            "candidate_gate": {"outcome": "incomplete", "passed": False},
            "task_coverage": {"outcome": "incomplete", "complete": False},
        }

    return {
        "schema": SESSION_SCHEMA,
        "version": SESSION_VERSION,
        "project_id": project_id,
        "status": status,
        "active_turn": active,
        "pending_approval": pending_approval,
        "messages": messages,
        "legacy_session_id": legacy_session_id,
        "product_status": product_status,
        "jobs": jobs,
        "canonical_revision": canonical_revision,
        "design_revision": design_revision,
        "content_hash": content_hash,
    }
