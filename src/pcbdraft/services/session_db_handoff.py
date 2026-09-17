"""Cross-platform session handoff persistence for SessionDB.

``SessionHandoffMixin`` is composed into ``SessionDB`` and owns no connection
state. The host supplies SQLite access, transactional writes, row shaping, and
a dynamic compatibility hook for logging. This module deliberately does not
import ``session_db`` so the composition root remains acyclic.
"""

# Handoff reads are best-effort polling paths and preserve their historical
# fail-closed behavior for any database/runtime exception.
# ruff: noqa: BLE001

from __future__ import annotations

from typing import Any


# State machine:
#   None       — no handoff in flight
#   "pending"  — CLI requested handoff, gateway hasn't picked it up yet
#   "running"  — gateway is processing (session switch + synthetic turn)
#   "completed"— gateway successfully delivered the synthetic turn
#   "failed"   — gateway hit an error; reason in handoff_error
#
# The CLI writes "pending" then poll-waits for terminal state. The gateway
# watcher transitions pending→running→{completed,failed}.
class SessionHandoffMixin:
    """Persist the pending → running → completed/failed handoff state machine."""

    def request_handoff(self, session_id: str, platform: str) -> bool:
        """Mark a session as pending handoff to the given platform.

        Returns True if the row was found and not already in flight; False if
        the session is already in a non-terminal handoff state.
        """

        def _do(conn):
            cur = conn.execute(
                "UPDATE sessions "
                "SET handoff_state = 'pending', "
                "    handoff_platform = ?, "
                "    handoff_error = NULL "
                "WHERE id = ? AND (handoff_state IS NULL "
                "                  OR handoff_state IN ('completed', 'failed'))",
                (platform, session_id),
            )
            return cur.rowcount > 0

        return self._execute_write(_do)

    def get_handoff_state(self, session_id: str) -> dict[str, Any] | None:
        """Read the current handoff state for a session.

        Returns ``{"state", "platform", "error"}`` or None if the session has
        no handoff record.
        """
        try:
            cur = self._conn.execute(
                "SELECT handoff_state, handoff_platform, handoff_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "state": row["handoff_state"],
                "platform": row["handoff_platform"],
                "error": row["handoff_error"],
            }
        except Exception:
            self._handoff_log_debug("Session handoff lookup failed", exc_info=True)
            return None

    def list_pending_handoffs(self) -> list[dict[str, Any]]:
        """Return all sessions in handoff_state='pending', oldest first.

        Used by the gateway's handoff watcher.
        """
        try:
            cur = self._conn.execute(
                "SELECT s.*, "
                "COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved "
                "FROM sessions s "
                "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
                "WHERE s.handoff_state = 'pending' "
                "ORDER BY s.started_at ASC"
            )
            return [self._session_row_dict(r) for r in cur.fetchall()]
        except Exception:
            self._handoff_log_debug("Pending handoff listing failed", exc_info=True)
            return []

    def claim_handoff(self, session_id: str) -> bool:
        """Atomically transition pending → running. Returns True if claimed."""

        def _do(conn):
            cur = conn.execute(
                "UPDATE sessions SET handoff_state = 'running' "
                "WHERE id = ? AND handoff_state = 'pending'",
                (session_id,),
            )
            return cur.rowcount > 0

        return self._execute_write(_do)

    def complete_handoff(self, session_id: str) -> None:
        """Mark a handoff as completed."""

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET handoff_state = 'completed', "
                "handoff_error = NULL WHERE id = ?",
                (session_id,),
            )

        self._execute_write(_do)

    def fail_handoff(self, session_id: str, error: str) -> None:
        """Mark a handoff as failed and record the reason."""

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET handoff_state = 'failed', "
                "handoff_error = ? WHERE id = ?",
                (error[:500], session_id),
            )

        self._execute_write(_do)
