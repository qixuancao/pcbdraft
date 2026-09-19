"""Read-only session lookup and diagnostic projections for SessionDB.

The mixin owns single-session reads, stable ID resolution, dominant model-route
projection, and the archived-message existence probe. The host supplies
connection access, token-count flushing, and a late-bound LIKE escaping hook.
This module never imports :mod:`pcbdraft.services.session_db`.
"""
# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

import sqlite3
from typing import Any


class SessionInspectionMixin:
    """Serve focused read-only inspection queries over durable sessions."""

    @staticmethod
    def _session_row_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        if "_system_prompt_resolved" in data:
            resolved = data.pop("_system_prompt_resolved")
            if "system_prompt" in data:
                data["system_prompt"] = resolved
        return data

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get a session by ID."""
        # Cost/usage readers (/status, /usage, gateway endpoints) reach the
        # row through here; drain queued token deltas so they see exact
        # totals. No-op attribute check when nothing is queued.
        self.flush_token_counts()
        with self._read_ctx() as conn:
            cursor = conn.execute(
                "SELECT s.*, "
                "COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved "
                "FROM sessions s "
                "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
                "WHERE s.id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
        return self._session_row_dict(row) if row else None

    def get_dominant_session_model_route(
        self, session_id: str
    ) -> dict[str, Any] | None:
        """Return the main-loop model route that served most API calls.

        ``sessions`` is a legacy aggregate row and can hold model/provider fields
        written by different route changes. ``session_model_usage`` keeps the
        coherent per-call tuple, so persisted status and billing reads should use
        its dominant main-loop route when one is available.
        """
        self.flush_token_counts()
        with self._read_ctx() as conn:
            row = conn.execute(
                """SELECT model, billing_provider, billing_base_url, billing_mode,
                          api_call_count
                     FROM session_model_usage
                    WHERE session_id = ?
                      AND task = ''
                      AND model <> 'unknown'
                      AND billing_provider <> ''
                    ORDER BY api_call_count DESC,
                             (input_tokens + output_tokens + cache_read_tokens +
                              cache_write_tokens + reasoning_tokens) DESC,
                             last_seen DESC
                    LIMIT 1""",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> str | None:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = self._inspection_escape_like(session_id_or_prefix)
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' "
                "ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    def has_archived_messages(self, session_id: str) -> bool:
        """Return True if the session has any soft-archived (``active = 0``) rows.

        Cheap existence probe — does not load rows. NOTE: production rewrite
        paths no longer branch on this (they pass ``active_only=True``
        unconditionally — a probe can fail open or race a concurrent
        ``archive_and_compact``, #80216); kept for tests and diagnostics.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND active = 0 LIMIT 1",
                (session_id,),
            )
            return cursor.fetchone() is not None
