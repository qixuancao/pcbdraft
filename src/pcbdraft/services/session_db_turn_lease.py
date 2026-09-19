"""Cross-process session-turn lease protocol for SessionDB.

The mixin maps compression segments to one conversation lease key and owns
acquire, wait, refresh, and release behavior. The SessionDB host retains
connection, schema, and transaction authority and supplies late-bound runtime
hooks. This module never imports :mod:`pcbdraft.services.session_db`.
"""
# mypy: disable-error-code="attr-defined,has-type"

# User callbacks are explicitly best-effort and must not break lease polling.
# ruff: noqa: BLE001

from __future__ import annotations


class SessionTurnLeaseMixin:
    """Serialize agent turns across processes and compression segments."""

    def _session_turn_lease_key_on_conn(self, conn, session_id: str) -> str:
        """Walk compression parents on ``conn`` to the conversation lease key.

        Must run on the same connection as the lease INSERT/UPDATE/DELETE.
        A prior ``get_session`` failure must not compute a child id that the
        later write then persists: refresh would walk to the parent and
        fail-close. Markers bind to ``parent_session_id`` (same contract as
        ``_NON_CONTINUATION_CHILD_FILTER_SQL``). Lock errors propagate so
        ``_execute_write`` / ``acquire_session_turn_lease`` can retry.
        """
        if not session_id:
            return session_id

        def _row(sid: str):
            row = conn.execute(
                "SELECT id, parent_session_id, source, model_config, end_reason "
                "FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            return dict(row) if row else None

        current = _row(session_id)
        seen = {session_id}
        while current:
            parent_id = current.get("parent_session_id")
            if (
                not parent_id
                or parent_id in seen
                or self._is_explicit_fork_child_row(current)
            ):
                break
            parent = _row(parent_id)
            if not parent or parent.get("end_reason") != "compression":
                break
            seen.add(parent_id)
            current = parent
        return str(current.get("id") or session_id) if current else session_id

    def _session_turn_lease_key(self, session_id: str) -> str:
        """Return the stable serialization key for every compression segment.

        Acquire/refresh/release resolve this inside their write transaction.
        This helper is for tests and diagnostics; it does not swallow lock
        errors (a swallowed walk plus a later successful write was the
        fail-open that replayed the post-rotation refresh miss).
        """
        if not session_id:
            return session_id
        with self._read_ctx() as conn:
            return self._session_turn_lease_key_on_conn(conn, session_id)

    def try_acquire_session_turn_lease(
        self,
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float = 300.0,
        patience_s: float | None = None,
    ) -> bool:
        """Atomically acquire the cross-process turn lease for a conversation.

        Compression rotates a session into child segments, so the durable key
        is the lineage root rather than the current segment id. The walk and
        INSERT share one write transaction. Expired leases and leases whose
        structured local holder PID is known dead are reclaimed in that same
        transaction.
        """
        if not session_id or not holder:
            return False
        now = self._turn_lease_now()
        expires_at = now + max(0.1, float(ttl_seconds))

        def _do(conn):
            conversation_id = self._session_turn_lease_key_on_conn(conn, session_id)
            row = conn.execute(
                "SELECT holder, expires_at FROM session_turn_leases "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is not None:
                current_holder = row["holder"]
                if float(
                    row["expires_at"]
                ) <= now or self._turn_lease_holder_process_is_dead(current_holder):
                    conn.execute(
                        "DELETE FROM session_turn_leases "
                        "WHERE conversation_id = ? AND holder = ?",
                        (conversation_id, current_holder),
                    )
            conn.execute(
                "INSERT OR IGNORE INTO session_turn_leases "
                "(conversation_id, holder, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, holder, now, expires_at),
            )
            owner = conn.execute(
                "SELECT holder FROM session_turn_leases WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            return owner is not None and owner["holder"] == holder

        return bool(self._execute_write(_do, patience_s=patience_s))

    def acquire_session_turn_lease(
        self,
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float = 300.0,
        wait_seconds: float = 1800.0,
        poll_interval_seconds: float = 1.0,
        on_wait=None,
        wait_notice_interval_seconds: float = 15.0,
        should_abort=None,
        acquire_patience_s: float = 0.5,
    ) -> bool:
        """Wait for a cross-process turn lease without holding a SQLite lock.

        ``on_wait(elapsed_seconds)`` is best-effort: invoked when the first
        attempt fails (elapsed ~0) and again about every
        ``wait_notice_interval_seconds`` while still waiting, so UIs can show
        that another process holds the conversation.

        When ``should_abort()`` returns True (for example the agent received
        ``/stop`` while waiting), acquisition stops immediately and returns
        False without consuming the full ``wait_seconds`` budget.
        """
        deadline = self._turn_lease_monotonic() + max(0.0, float(wait_seconds))
        wait_started = None
        last_notice_at = None
        notice_every = max(0.0, float(wait_notice_interval_seconds))
        while True:
            if should_abort is not None:
                try:
                    if should_abort():
                        return False
                except Exception:
                    self._turn_lease_log_debug(
                        "session turn lease should_abort callback failed",
                        exc_info=True,
                    )
            try:
                if self.try_acquire_session_turn_lease(
                    session_id,
                    holder,
                    ttl_seconds=ttl_seconds,
                    patience_s=acquire_patience_s,
                ):
                    return True
            except self._turn_lease_sqlite_error_type() as exc:
                # Long holder transactions (compression publish, large
                # flushes) can exhaust a single write-patience budget.
                # Keep polling until wait_seconds or should_abort.
                if self._turn_lease_classify_persistence_error(exc) != "locked":
                    raise
            now = self._turn_lease_monotonic()
            remaining = deadline - now
            if remaining <= 0:
                return False
            if wait_started is None:
                wait_started = now
            if on_wait is not None and (
                last_notice_at is None
                or notice_every == 0.0
                or (now - last_notice_at) >= notice_every
            ):
                try:
                    on_wait(max(0.0, now - wait_started))
                except Exception:
                    self._turn_lease_log_debug(
                        "session turn lease on_wait callback failed",
                        exc_info=True,
                    )
                last_notice_at = now
            self._turn_lease_sleep(
                min(max(0.01, float(poll_interval_seconds)), remaining)
            )

    def refresh_session_turn_lease(
        self,
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float = 300.0,
    ) -> bool:
        """Extend a turn lease only while ``holder`` still owns it."""
        if not session_id or not holder:
            return False
        expires_at = self._turn_lease_now() + max(0.1, float(ttl_seconds))

        def _do(conn):
            conversation_id = self._session_turn_lease_key_on_conn(conn, session_id)
            cursor = conn.execute(
                "UPDATE session_turn_leases SET expires_at = ? "
                "WHERE conversation_id = ? AND holder = ?",
                (expires_at, conversation_id, holder),
            )
            return cursor.rowcount > 0

        return bool(self._execute_write(_do))

    def release_session_turn_lease(self, session_id: str, holder: str) -> None:
        """Release a turn lease iff ``holder`` still owns it; idempotent."""
        if not session_id or not holder:
            return

        def _do(conn):
            conversation_id = self._session_turn_lease_key_on_conn(conn, session_id)
            conn.execute(
                "DELETE FROM session_turn_leases "
                "WHERE conversation_id = ? AND holder = ?",
                (conversation_id, holder),
            )

        self._execute_write(_do)
