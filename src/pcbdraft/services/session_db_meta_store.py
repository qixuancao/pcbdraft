"""Small state_meta store and kanban compatibility helpers for SessionDB.

``SessionMetaStoreMixin`` is composed into ``SessionDB`` and owns no connection
state. The host supplies the main SQLite connection, write transaction helper,
and dynamic compatibility hooks. This module deliberately does not import
``session_db`` so the composition root remains acyclic.
"""

from __future__ import annotations

import sqlite3


class SessionMetaStoreMixin:
    """Read and write namespaced metadata plus one-time kanban migration gates."""

    def get_meta(self, key: str) -> str | None:
        """Read a value from the state_meta key/value store."""
        # Kept on self._lock (not _read_ctx) because callers like
        # fts_rebuild_step read progress before entering a write
        # transaction, and the read-only WAL connection sees only
        # committed data — a pending write transaction's uncommitted
        # meta writes would be invisible.  This is a cheap point lookup,
        # not the convoy bottleneck the read-path split targets.
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return self._meta_store_row_value(row)

    def set_meta(
        self, key: str, value: str, *, cursor: sqlite3.Cursor | None = None
    ) -> None:
        """Write a value to the state_meta key/value store.

        When ``cursor`` is provided the write is issued on that cursor
        inline (used during ``_init_schema``, which already holds an open
        transaction — routing through ``_execute_write`` there would nest
        BEGIN IMMEDIATE and deadlock). Otherwise a normal write transaction
        is used.
        """
        if cursor is not None:
            cursor.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            return

        def _do(conn):
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

        self._execute_write(_do)

    def retag_kanban_worker_sessions(self, workspaces_root: str) -> int:
        """Retag legacy kanban worker rows from ``cli`` to ``kanban``.

        Workers used to spawn without ``PCBDRAFT_RUNTIME_SESSION_SOURCE``, so their runs
        landed as untitled ``cli`` rows and the sidebar rendered one per attempt
        labeled with the worker's own prompt. New workers tag themselves; this
        reclaims the rows already on disk so they drop out of the session lists
        too. Identified by cwd under the board's workspaces root — a path only
        the dispatcher ever runs a session in.

        Gated per workspaces root (``state_meta``) so each board reclaims its
        own rows exactly once. Returns the number of rows retagged.
        """
        prefix = str(workspaces_root).rstrip("/\\")
        if not prefix:
            return 0

        gate = f"kanban_worker_source_retagged:{prefix}"
        if self.get_meta(gate) == "1":
            return 0

        def _do(conn):
            cursor = conn.execute(
                "UPDATE sessions SET source = 'kanban' "
                "WHERE source = 'cli' AND (cwd = ? OR cwd LIKE ? ESCAPE '\\')",
                (prefix, self._meta_store_escape_like(prefix) + "/%"),
            )
            # Read rowcount before set_meta reuses this cursor for its INSERT,
            # which would otherwise overwrite it with the meta write's count.
            retagged = cursor.rowcount or 0
            self.set_meta(gate, "1", cursor=cursor)
            return retagged

        return self._execute_write(_do)

    def list_meta_prefix(self, prefix: str) -> list[tuple[str, str]]:
        """Return ``[(key, value), ...]`` for state_meta keys with ``prefix``.

        Used by feature stores that persist one row per session under a
        namespaced key (e.g. ``loop:<session_id>``) and need to enumerate
        them across sessions (the gateway's idle /loop wakeup watcher).
        ``prefix`` is matched literally — LIKE wildcards in it are escaped.
        """
        if not prefix:
            return []
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM state_meta WHERE key LIKE ? ESCAPE '\\'",
                (escaped + "%",),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]
