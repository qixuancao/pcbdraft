"""Trusted compatibility projection for legacy project terminal history."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from pcbdraft.core.io import load_json_limited
from pcbdraft.services.progress import ProductSessionTerminalReceipt

_LOGGER = logging.getLogger(__name__)
_RECENT_SESSION_LIMIT = 50
_RECEIPT_LIMIT_BYTES = 1024 * 1024


def project_ids_from_history(messages: list[dict[str, Any]]) -> set[str]:
    """Return project IDs proven by structured PCB tool-result messages."""

    project_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        tool_name = str(message.get("tool_name") or payload.get("tool") or "")
        project_id = payload.get("project_id")
        if tool_name.startswith("pcb_") and isinstance(project_id, str) and project_id:
            project_ids.add(project_id)
    return project_ids


def find_project_session_id(
    session_db: Any, service: Any, project_id: str
) -> str | None:
    """Find one resumable CLI session proven to belong only to ``project_id``."""

    if session_db is None or not project_id:
        return None
    receipt_ids: list[str] = []
    try:
        receipt_root = service.project_root(project_id) / "product-sessions"
        if receipt_root.is_dir() and not receipt_root.is_symlink():
            receipts: list[tuple[str, str]] = []
            for path in receipt_root.iterdir():
                if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                    continue
                try:
                    receipt = ProductSessionTerminalReceipt.from_dict(
                        load_json_limited(path, _RECEIPT_LIMIT_BYTES)
                    )
                except Exception as exc:  # noqa: BLE001 - skip malformed legacy receipt
                    _LOGGER.debug(
                        "Ignoring invalid product session receipt %s: %s", path, exc
                    )
                    continue
                if receipt.project_id == project_id:
                    receipts.append((receipt.created_at, receipt.session_id))
            receipt_ids = [
                session_id for _created_at, session_id in sorted(receipts, reverse=True)
            ]
    except Exception as exc:  # noqa: BLE001 - compatibility lookup is optional
        _LOGGER.debug("Could not inspect product receipts for %s: %s", project_id, exc)

    try:
        recent = session_db.list_sessions_rich(
            source="cli",
            limit=_RECENT_SESSION_LIMIT,
            min_message_count=1,
            order_by_last_active=True,
            compact_rows=True,
        )
        recent_ids = [str(row.get("id") or "") for row in recent]
    except Exception as exc:  # noqa: BLE001 - try receipt candidates below
        _LOGGER.debug("Could not list legacy CLI sessions: %s", exc)
        recent_ids = []

    seen: set[str] = set()
    for candidate_id in recent_ids + receipt_ids:
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        try:
            metadata = session_db.get_session(candidate_id)
            if not metadata or metadata.get("source") != "cli":
                continue
            resolved_id = (
                session_db.resolve_resume_session_id(candidate_id) or candidate_id
            )
            session_db.assert_resume_safe(resolved_id)
            model_history, display_history = session_db.get_resume_conversations(
                resolved_id
            )
        except Exception as exc:  # noqa: BLE001 - one bad record must not block fallback
            _LOGGER.debug("Ignoring legacy session %s: %s", candidate_id, exc)
            continue
        if model_history and project_ids_from_history(display_history) == {project_id}:
            return resolved_id
    return None


def legacy_project_messages(
    service: Any, project_id: str
) -> tuple[str, list[dict[str, Any]]] | None:
    """Return a verified legacy transcript for a project, if one exists."""

    from pcbdraft.services.session_db import SessionDB

    session_db = SessionDB()
    try:
        session_id = find_project_session_id(session_db, service, project_id)
        if not session_id:
            return None
        _model, display = session_db.get_resume_conversations(session_id)
        return session_id, display
    finally:
        session_db.close()
