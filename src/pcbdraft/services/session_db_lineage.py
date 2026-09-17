"""Read-only compression-lineage classification for SessionDB.

The mixin distinguishes explicit forks from compression continuations and
walks durable parent/child chains. The host supplies session lookup, SQLite
access, and a late-bound JSON decoder. This module never imports
:mod:`pcbdraft.services.session_db`.
"""

from __future__ import annotations

import json
from typing import Any


class SessionLineageMixin:
    """Classify and project durable compression lineages without mutation."""

    def _is_explicit_fork_child_row(self, session: dict[str, Any]) -> bool:
        """True when ``session`` is a branch, delegate, or tool child of its parent.

        Markers only count as a fork when they point at ``parent_session_id``.
        Compression copies ``model_config`` onto the continuation
        (``publish_compression_child`` callers pass
        ``agent._session_init_model_config``), so a delegate's continuation
        carries ``_delegate_from=<the delegate's own parent>``. Presence-only
        matching would treat that real continuation as a fork — the same
        misclassification ``_NON_CONTINUATION_CHILD_FILTER_SQL`` already
        avoids by binding both markers to the queried parent.
        """
        if session.get("source") == "tool":
            return True
        raw = session.get("model_config")
        if not raw:
            return False
        try:
            cfg = self._lineage_json_loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(cfg, dict):
            return False
        parent_id = session.get("parent_session_id")
        branched = cfg.get("_branched_from")
        delegated = cfg.get("_delegate_from")
        if parent_id:
            return branched == parent_id or delegated == parent_id
        return branched is not None or delegated is not None

    def _is_compression_child_row(self, child: dict[str, Any]) -> bool:
        parent_id = child.get("parent_session_id")
        if not parent_id or self._is_explicit_fork_child_row(child):
            return False
        parent = self.get_session(parent_id)
        return bool(parent and parent.get("end_reason") == "compression")

    def get_compression_lineage(self, session_id: str) -> list[str]:
        """Return compression ancestors through tip in chronological order."""
        session = self.get_session(session_id)
        if not session or self._is_explicit_fork_child_row(session):
            return [session_id] if session else []

        root = session
        ancestors = {root["id"]}
        while self._is_compression_child_row(root):
            parent = self.get_session(root["parent_session_id"])
            if not parent or parent["id"] in ancestors:
                break
            root = parent
            ancestors.add(root["id"])

        lineage = [root["id"]]
        seen = {root["id"]}
        current = root
        while current.get("end_reason") == "compression":
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE parent_session_id = ?
                    ORDER BY started_at ASC
                    """,
                    (current["id"],),
                ).fetchall()
            next_child = None
            for row in rows:
                candidate = dict(row)
                if self._is_compression_child_row(candidate):
                    next_child = candidate
                    break
            if not next_child or next_child["id"] in seen:
                break
            lineage.append(next_child["id"])
            seen.add(next_child["id"])
            current = next_child
            if current["id"] == session_id:
                # Continue to include later compression tips only when the
                # requested session itself was compacted.
                continue
        return lineage if session_id in lineage else [session_id]
