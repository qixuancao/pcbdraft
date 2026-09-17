"""Compression lease ownership and recovery for SessionDB.

The mixin owns the ``compression_locks`` transaction protocol used to fence
compaction publication. The host supplies write-transaction execution and the
shared process-liveness predicate also used by session-turn leases. This module
never imports :mod:`pcbdraft.services.session_db`.
"""

from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger("pcbdraft.services.session_db")


class SessionCompressionLeaseMixin:
    """Acquire, refresh, inspect, and release compression leases."""

    @staticmethod
    def _compression_lease_matches_on_conn(
        conn, session_id: str, holder: str | None
    ) -> bool:
        """Check a compression lease inside the caller's write transaction."""
        if not holder:
            return False
        lock_row = conn.execute(
            "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return bool(
            lock_row is not None
            and lock_row["holder"] == holder
            and float(lock_row["expires_at"]) > time.time()
        )

    @staticmethod
    def _reclaim_expired_compression_lease_on_conn(conn, session_id: str) -> bool:
        """Reclaim an expired lease atomically, or fail closed when it is live."""
        now = time.time()
        lock_row = conn.execute(
            "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if lock_row is None:
            return True
        expires_at = lock_row["expires_at"]
        if expires_at is None or float(expires_at) >= now:
            return False
        deleted = conn.execute(
            "DELETE FROM compression_locks "
            "WHERE session_id = ? AND holder = ? AND expires_at = ?",
            (session_id, lock_row["holder"], expires_at),
        )
        return deleted.rowcount == 1

    def refresh_compression_lock(
        self,
        session_id: str,
        holder: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        """Extend the compression lock lease if ``holder`` still owns it.

        Ownership is decided by the ``holder`` column alone, deliberately NOT
        by ``expires_at``: a live owner whose refresher thread was starved
        (GC pause, loaded CI runner, a slow write escaping ``_execute_write``'s
        retry budget) past its own TTL must be able to revive its still-unclaimed
        row on the next tick. Requiring ``expires_at >= now`` here made such a
        stall permanent — every later refresh matched 0 rows, so the owner kept
        compressing and rotating with no lease at all, which is exactly the
        unprotected window a competing path can fork the session lineage in.

        This does not resurrect a lock somebody else already took: SQLite
        serialises writes, so a reclaim (DELETE-expired + INSERT-or-IGNORE in
        :meth:`try_acquire_compression_lock`) and this UPDATE never interleave.
        Reclaim-first replaces ``holder``, so this UPDATE matches nothing and
        returns False; refresh-first pushes ``expires_at`` into the future, so
        the reclaimer's DELETE-expired matches nothing and its acquire fails.
        """
        if not session_id or not holder:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            cur = conn.execute(
                "UPDATE compression_locks SET expires_at = ? "
                "WHERE session_id = ? AND holder = ?",
                (expires_at, session_id, holder),
            )
            return cur.rowcount > 0

        try:
            return bool(self._execute_write(_do))
        except sqlite3.Error as exc:
            logger.warning(
                "refresh_compression_lock(%s) failed: %s",
                session_id,
                exc,
            )
            return False

    def try_acquire_compression_lock(
        self,
        session_id: str,
        holder: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        """Try to atomically acquire the compression lock for ``session_id``.

        Returns ``True`` on success (caller now owns the lock and must
        release via :meth:`release_compression_lock`).  Returns ``False``
        if another holder already owns a non-expired lock — the caller
        MUST NOT proceed with compression in that case (its rotation would
        race against the holder's, splitting the session lineage).

        Expired locks (``expires_at < now``) are reclaimed transparently.
        Structured holders whose local ``pid=`` no longer exists are reclaimed
        immediately, so a gateway killed during compression does not stall the
        replacement process for the full lease TTL.

        Implementation: single-transaction DELETE-expired + INSERT-or-IGNORE,
        followed by a SELECT to confirm we got the row. SQLite serialises
        writes, so the whole sequence is atomic against other writers.
        """
        if not session_id:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            reclaimed_holder = None
            row = conn.execute(
                "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None:
                current_holder = (
                    row["holder"] if isinstance(row, sqlite3.Row) else row[0]
                )
                current_expires_at = (
                    row["expires_at"] if isinstance(row, sqlite3.Row) else row[1]
                )
                if (
                    current_expires_at < now
                    or self._compression_lease_holder_process_is_dead(current_holder)
                ):
                    conn.execute(
                        "DELETE FROM compression_locks "
                        "WHERE session_id = ? AND holder = ?",
                        (session_id, current_holder),
                    )
                    reclaimed_holder = current_holder
            # Then: try to insert. INSERT OR IGNORE returns no rowcount
            # difference — verify ownership via SELECT.
            conn.execute(
                "INSERT OR IGNORE INTO compression_locks "
                "(session_id, holder, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, holder, now, expires_at),
            )
            row = conn.execute(
                "SELECT holder FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            acquired = (
                row is not None
                and (row["holder"] if isinstance(row, sqlite3.Row) else row[0])
                == holder
            )
            return acquired, reclaimed_holder

        try:
            acquired, reclaimed_holder = self._execute_write(_do)
            if reclaimed_holder:
                logger.warning(
                    "Reclaimed stale compression lock for session=%s (holder=%s)",
                    session_id,
                    reclaimed_holder,
                )
            return bool(acquired)
        except sqlite3.Error as exc:
            logger.warning(
                "try_acquire_compression_lock(%s) failed: %s",
                session_id,
                exc,
            )
            # Fail open: returning False makes the caller skip compression,
            # which is the safe behaviour when the lock subsystem is broken.
            return False

    def release_compression_lock(self, session_id: str, holder: str) -> None:
        """Release the compression lock for ``session_id`` iff we own it.

        Idempotent: no-op when the lock has already expired and been
        reclaimed by a different holder, or when no lock exists. The
        ``holder`` check prevents a late-returning compressor from
        clobbering a fresh lock held by someone else.
        """
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
                (session_id, holder),
            )

        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning(
                "release_compression_lock(%s) failed: %s",
                session_id,
                exc,
            )

    def get_compression_lock_holder(self, session_id: str) -> str | None:
        """Return the current (non-expired) holder for ``session_id``, or None.

        Diagnostic helper — not used by the locking protocol itself.
        """
        if not session_id:
            return None
        now = time.time()
        row = self._conn.execute(
            "SELECT holder FROM compression_locks "
            "WHERE session_id = ? AND expires_at >= ?",
            (session_id, now),
        ).fetchone()
        if row is None:
            return None
        return row["holder"] if isinstance(row, sqlite3.Row) else row[0]
