"""Compression health and retry state persistence for SessionDB.

The mixin owns per-session cooldown, fallback, ineffective-compaction, and
per-peer hygiene counters. The SessionDB host retains connection and write
transaction authority and supplies late-bound compatibility hooks for time,
logging, and SQLite runtime types. This module never imports
:mod:`pcbdraft.services.session_db`.
"""
# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

from typing import Any


class SessionCompressionHealthMixin:
    """Persist compression retry guards and gateway hygiene counters."""

    def record_compression_failure_cooldown(
        self,
        session_id: str,
        cooldown_until: float,
        error: str | None = None,
    ) -> None:
        """Persist the active compression-failure cooldown for a session."""
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_failure_cooldown_until = ?, "
                "compression_failure_error = ? WHERE id = ?",
                (cooldown_until, error, session_id),
            )

        try:
            self._execute_write(_do)
        except self._compression_health_sqlite_error_type() as exc:
            self._compression_health_log_warning(
                "record_compression_failure_cooldown(%s) failed: %s",
                session_id,
                exc,
            )

    def get_compression_failure_cooldown(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Return the active compression-failure cooldown for ``session_id``."""
        if not session_id:
            return None
        now = self._compression_health_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT compression_failure_cooldown_until, compression_failure_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        cooldown_until = (
            row["compression_failure_cooldown_until"]
            if self._compression_health_is_sqlite_row(row)
            else row[0]
        )
        if cooldown_until is None:
            return None
        cooldown_until = float(cooldown_until)
        if cooldown_until <= now:
            return None
        error = (
            row["compression_failure_error"]
            if self._compression_health_is_sqlite_row(row)
            else row[1]
        )
        return {
            "cooldown_until": cooldown_until,
            "remaining_seconds": cooldown_until - now,
            "error": error,
        }

    def get_compression_failure_cooldown_row(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Return the exact stored cooldown columns without expiry filtering.

        Compression cancellation uses this under its session lease so rollback
        can preserve an expired row, a partially-null row, or an absent session
        exactly instead of converting those states through the active-cooldown
        API.
        """
        if not session_id:
            return {"session_exists": False, "cooldown_until": None, "error": None}
        with self._lock:
            row = self._conn.execute(
                "SELECT compression_failure_cooldown_until, compression_failure_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {"session_exists": False, "cooldown_until": None, "error": None}
        cooldown_until = (
            row["compression_failure_cooldown_until"]
            if self._compression_health_is_sqlite_row(row)
            else row[0]
        )
        error = (
            row["compression_failure_error"]
            if self._compression_health_is_sqlite_row(row)
            else row[1]
        )
        return {
            "session_exists": True,
            "cooldown_until": (
                float(cooldown_until) if cooldown_until is not None else None
            ),
            "error": error,
        }

    def restore_compression_failure_cooldown_row(
        self,
        session_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        """Restore and verify an exact cooldown-row snapshot.

        Unlike the ordinary record/clear helpers, this transactional rollback
        API deliberately propagates write and verification failures. A caller
        must not report cancellation as mutation-free when compensation failed.
        """
        expected_exists = bool(snapshot.get("session_exists", False))
        if not expected_exists:
            actual = self.get_compression_failure_cooldown_row(session_id)
            if actual.get("session_exists", False):
                raise RuntimeError(
                    "cannot restore absent compression cooldown row: session now exists"
                )
            return

        deadline = snapshot.get("cooldown_until")
        error = snapshot.get("error")

        def _do(conn):
            cursor = conn.execute(
                "UPDATE sessions SET compression_failure_cooldown_until = ?, "
                "compression_failure_error = ? WHERE id = ?",
                (deadline, error, session_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"compression cooldown rollback session missing: {session_id}"
                )

        self._execute_write(_do)
        actual = self.get_compression_failure_cooldown_row(session_id)
        expected = {
            "session_exists": True,
            "cooldown_until": float(deadline) if deadline is not None else None,
            "error": error,
        }
        if actual != expected:
            raise RuntimeError(
                f"compression cooldown rollback verification failed: "
                f"expected={expected!r}, actual={actual!r}"
            )

    def clear_compression_failure_cooldown(self, session_id: str) -> None:
        """Clear any persisted compression-failure cooldown for a session."""
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_failure_cooldown_until = NULL, "
                "compression_failure_error = NULL WHERE id = ?",
                (session_id,),
            )

        try:
            self._execute_write(_do)
        except self._compression_health_sqlite_error_type() as exc:
            self._compression_health_log_warning(
                "clear_compression_failure_cooldown(%s) failed: %s",
                session_id,
                exc,
            )

    def get_compression_fallback_streak(self, session_id: str) -> int:
        """Return the persisted deterministic-fallback streak."""
        if not session_id:
            return 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            row = conn.execute(
                "SELECT compression_fallback_streak FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return 0
        value = (
            row["compression_fallback_streak"]
            if self._compression_health_is_sqlite_row(row)
            else row[0]
        )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def set_compression_fallback_streak(self, session_id: str, streak: int) -> None:
        """Persist the deterministic-fallback streak for one session."""
        if not session_id:
            return
        normalized = max(0, int(streak))

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_fallback_streak = ? WHERE id = ?",
                (normalized, session_id),
            )

        self._execute_write(_do)

    def increment_hygiene_failure_streak(self, session_key: str) -> int:
        """Atomically increment the session-hygiene failure streak for one chat."""
        if not session_key:
            return 1
        result = []

        def _do(conn):
            conn.execute(
                """INSERT INTO gateway_hygiene_state (session_key, failure_streak)
                   VALUES (?, 1)
                   ON CONFLICT(session_key) DO UPDATE SET
                       failure_streak = gateway_hygiene_state.failure_streak + 1""",
                (session_key,),
            )
            row = conn.execute(
                "SELECT failure_streak FROM gateway_hygiene_state WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            result.append(int(row[0]))

        self._execute_write(_do)
        return result[0]

    def reset_hygiene_failure_streak(self, session_key: str) -> None:
        """Clear the persisted session-hygiene failure streak for one chat."""
        if not session_key:
            return

        def _do(conn):
            conn.execute(
                "DELETE FROM gateway_hygiene_state WHERE session_key = ?",
                (session_key,),
            )

        self._execute_write(_do)

    def get_compression_ineffective_count(self, session_id: str) -> int:
        """Return the persisted ineffective-compaction strike count.

        Mirrors ``get_compression_fallback_streak``: this is the durable half
        of the anti-thrash guard (``_ineffective_compression_count`` on the
        built-in compressor), persisted so that a fresh compressor bound to a
        resumed session inherits an armed/tripped guard instead of starting
        from zero across process restarts (#54923).
        """
        if not session_id:
            return 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            row = conn.execute(
                "SELECT compression_ineffective_count FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return 0
        value = (
            row["compression_ineffective_count"]
            if self._compression_health_is_sqlite_row(row)
            else row[0]
        )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def set_compression_ineffective_count(self, session_id: str, count: int) -> None:
        """Persist the ineffective-compaction strike count for one session."""
        if not session_id:
            return
        normalized = max(0, int(count))

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_ineffective_count = ? WHERE id = ?",
                (normalized, session_id),
            )

        self._execute_write(_do)
