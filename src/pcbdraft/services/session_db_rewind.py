"""Transcript rewind behavior for :class:`SessionDB`.

Mixin contract: this plain mixin is consumed by
``pcbdraft.services.session_db.SessionDB``. It defines no ``__init__`` and owns
no connection state. The host supplies the SQLite connection/lock, write helper,
and transcript content decoder. This module must never import ``session_db`` so
the store remains the composition root.
"""
# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

from typing import Any


class SessionRewindMixin:
    """Detect replay duplicates and soft-delete or restore transcript tails."""

    @staticmethod
    def _is_duplicate_replayed_user_message(
        messages: list[dict[str, Any]], msg: dict[str, Any]
    ) -> bool:
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return False
        for prev in reversed(messages):
            if prev.get("role") == "user" and prev.get("content") == content:
                return True
            if prev.get("role") == "assistant" and (
                prev.get("content") or prev.get("tool_calls")
            ):
                return False
        return False

    # =========================================================================
    # Rewind (soft-delete) — see /rewind slash command + issue #21910
    # =========================================================================

    def rewind_to_message(
        self, session_id: str, target_message_id: int
    ) -> dict[str, Any]:
        """Soft-delete all messages with id >= ``target_message_id`` in *session_id*.

        The target message itself becomes inactive as well so the caller
        can pre-fill it as the next user prompt without it appearing
        twice in the replayed transcript.  Rewound rows are kept on
        disk with ``active=0`` for audit / forensic inspection — use
        :meth:`get_messages` with ``include_inactive=True`` to see them.

        Returns a dict::

            {
                "rewound_count": int,    # number of rows newly flipped to active=0
                "target_message": dict,  # full row dict of the target
                "new_head_id":   int|None  # id of the last still-active row, or None
            }

        Raises ``ValueError`` if the target message does not exist in
        *session_id* or if its role is not ``"user"``.

        Always increments ``sessions.rewind_count`` — even when the
        target is already inactive — so the counter accurately reflects
        the number of rewind operations performed against the session.
        Idempotent on the ``active`` flag: re-rewinding past the same
        target is a no-op on row state but still bumps the counter.
        """

        # 1) Validate target up-front (read-only, outside the write txn).
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ? AND session_id = ?",
                (target_message_id, session_id),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"message {target_message_id} not found in session {session_id}"
            )
        target_row = dict(row)
        if target_row.get("role") != "user":
            raise ValueError(
                f"rewind target must be a 'user' message (got role="
                f"{target_row.get('role')!r}, id={target_message_id})"
            )

        # Decode content for callers (prefill the prompt buffer).
        target_row["content"] = self._decode_content(target_row.get("content"))

        rewound: list[int] = []

        def _do(conn):
            cursor = conn.execute(
                "SELECT id FROM messages "
                "WHERE session_id = ? AND id >= ? AND active = 1",
                (session_id, target_message_id),
            )
            ids = [r[0] for r in cursor.fetchall()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE messages SET active = 0 WHERE id IN ({placeholders})",
                    ids,
                )
            conn.execute(
                "UPDATE sessions SET rewind_count = COALESCE(rewind_count, 0) + 1 "
                "WHERE id = ?",
                (session_id,),
            )
            return ids

        rewound = self._execute_write(_do)

        # 2) Compute new head id (largest still-active row id in session).
        with self._lock:
            head_row = self._conn.execute(
                "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1",
                (session_id,),
            ).fetchone()
        new_head_id = head_row[0] if head_row and head_row[0] is not None else None

        return {
            "rewound_count": len(rewound),
            "target_message": target_row,
            "new_head_id": new_head_id,
        }

    def restore_rewound(self, session_id: str, since_message_id: int) -> int:
        """Mark inactive messages with id >= *since_message_id* active again.

        Returns the number of rows flipped back to ``active=1``.
        Intended for undo-of-rewind and test cleanup; not wired to a
        slash command in v1.
        """

        def _do(conn):
            cursor = conn.execute(
                "SELECT id FROM messages "
                "WHERE session_id = ? AND id >= ? AND active = 0",
                (session_id, since_message_id),
            )
            ids = [r[0] for r in cursor.fetchall()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE messages SET active = 1 WHERE id IN ({placeholders})",
                    ids,
                )
            return len(ids)

        return self._execute_write(_do)
