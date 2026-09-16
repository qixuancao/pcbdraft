"""First-slice transcript queries for :class:`SessionDB`.

Mixin contract: this plain mixin is consumed by
``pcbdraft.services.session_db.SessionDB``. It defines no ``__init__`` and owns
no connection state. The host supplies read/write connection helpers and the
transcript serialization methods. This module must never import ``session_db``
so the store remains the composition root.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pcbdraft.services.session_db_transcript_write import _scrub_surrogates

logger = logging.getLogger("pcbdraft.services.session_db")


class SessionTranscriptQueryMixin:
    """Update transcript sidecars and serve bounded message queries."""

    def _message_column_names(self, conn) -> list[str]:
        """Column names of the messages table, cached per-connection era."""
        cached = getattr(self, "_message_columns_cache", None)
        if cached:
            return cached
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        self._message_columns_cache = cols
        return cols

    def set_latest_user_api_content(
        self, session_id: str, content: Any, api_content: str
    ) -> int:
        """Backfill the ``api_content`` sidecar onto the newest ACTIVE user row.

        In-place preflight compaction (:meth:`archive_and_compact`) inserts the
        current turn's user row BEFORE the turn prologue composes the
        prefetch/plugin sidecar, and the subsequent crash persist identity-skips
        every compacted dict — without this backfill the stamped sidecar would
        never land in the DB and any reload would replay clean content,
        re-introducing the prompt-cache divergence the sidecar exists to close.

        The ``content`` match is a defensive guard: if the newest active user
        row is not the message the caller stamped (racing rewrite, unexpected
        tail shape), nothing is written. Returns the number of rows updated
        (0 or 1).
        """
        encoded = self._encode_content(content)

        def _do(conn):
            cursor = conn.execute(
                "UPDATE messages SET api_content = ? WHERE id = ("
                "SELECT id FROM messages "
                "WHERE session_id = ? AND role = 'user' AND active = 1 "
                "ORDER BY id DESC LIMIT 1"
                ") AND content IS ?",
                (_scrub_surrogates(api_content), session_id, encoded),
            )
            return cursor.rowcount

        return self._execute_write(_do)

    def get_messages(
        self,
        session_id: str,
        include_inactive: bool = False,
        include_compacted: bool = False,
        limit: int | None = None,
        offset: int = 0,
        latest: bool = False,
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load messages for a session in insertion order.

        By default only active messages are returned. Pass
        ``include_inactive=True`` to load soft-deleted rows (e.g. for
        audit / debug views of rewound history). See
        :meth:`rewind_to_message` for the soft-delete mechanic.

        Pass ``include_compacted=True`` to additionally load rows preserved
        by in-place context compaction (``active=0, compacted=1``). Those are
        durable display history, not soft-deleted rows — a user-visible
        transcript read must not drop them, or earlier turns silently become
        unreachable once the UI exhausts its active-only window. Soft-deleted
        Undo/Rewind rows (``active=0, compacted=0``) stay excluded; use
        ``include_inactive`` for those.

        Ordered by AUTOINCREMENT id (true insertion order) rather than
        timestamp — see c03acca50 for the WSL2 clock-regression rationale.

        When ``limit`` is provided, returns at most ``limit`` messages
        starting from ``offset`` (0-based, in insertion order). Enables
        pagination for the API endpoint to avoid loading entire transcripts.
        With ``latest=True``, the offset is measured back from the newest
        message and the selected page is still returned in chronological
        order. ``offset`` alone (without ``limit``) also pages — SQLite
        requires a LIMIT clause for OFFSET, so it's emitted as ``LIMIT -1``
        (unbounded).

        ``after_id`` enables keyset pagination (``id > after_id``): O(1)
        page seeks on huge transcripts where OFFSET degrades to O(n) per
        page. Ascending order only (incompatible with ``latest``/``offset``).
        """
        if after_id is not None and (latest or offset):
            raise ValueError("after_id is incompatible with latest/offset paging")
        if after_id is not None and include_compacted:
            raise ValueError(
                "after_id is incompatible with include_compacted (deduped display reads use offset paging)"
            )
        if include_inactive:
            # Audit / debug reads: every row, including soft-deleted.
            active_clause = ""
        elif include_compacted:
            # Display history: active rows plus rows preserved by in-place
            # compaction (active=0, compacted=1), but never soft-deleted
            # Undo/Rewind rows (active=0, compacted=0).
            active_clause = " AND (active = 1 OR compacted = 1)"
        else:
            active_clause = " AND active = 1"
        keyset_clause = " AND id > ?" if after_id is not None else ""
        sql = (
            "SELECT * FROM messages WHERE session_id = ?"
            f"{active_clause}{keyset_clause} ORDER BY id {'DESC' if latest else 'ASC'}"
        )
        params: list = [session_id]
        if after_id is not None:
            params.append(after_id)
        if include_compacted:
            # Compaction epochs copy the protected tail into each new
            # generation, so the same logical message can exist as several
            # rows (identical role/content/timestamp) with different active
            # flags and ids. A display read must surface each message exactly
            # once: prefer the live row, then the newest generation. Read the
            # full display set (a session's rows are bounded; the UI-level
            # 500-row cap lives in the endpoint, not here), dedupe in Python,
            # then apply paging.
            with self._read_ctx() as conn:
                cursor = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ?"
                    + active_clause
                    + " ORDER BY id ASC",
                    [session_id],
                )
                all_rows = cursor.fetchall()
            seen: dict = {}
            for row in all_rows:
                # Tool fields participate in the dedupe key: compaction copies
                # them verbatim, so identical tool messages across generations
                # still collapse, while distinct tool calls that happen to
                # share role/content/timestamp are never merged.
                key = (
                    row["role"],
                    row["content"],
                    row["timestamp"],
                    row["tool_call_id"],
                    row["tool_calls"],
                    row["tool_name"],
                )
                cur = seen.get(key)
                if cur is None or (row["active"], row["id"]) > (
                    cur["active"],
                    cur["id"],
                ):
                    seen[key] = row
            rows = sorted(seen.values(), key=lambda r: r["id"])
            if latest:
                rows = rows[::-1]
            rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
            if latest:
                rows = rows[::-1]
        else:
            if limit is not None or offset:
                # SQLite's OFFSET requires LIMIT; -1 means "no limit".
                sql += " LIMIT ? OFFSET ?"
                params.extend([-1 if limit is None else limit, offset])
            with self._read_ctx() as conn:
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()
            if latest:
                rows.reverse()
        result = []
        for row in rows:
            msg = dict(row)
            if "content" in msg:
                msg["content"] = self._decode_content(msg["content"])
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to deserialize tool_calls in get_messages, falling back to []"
                    )
                    msg["tool_calls"] = []
            if msg.get("display_metadata") is not None:
                msg["display_metadata"] = self._decode_display_metadata(
                    msg["display_metadata"]
                )
            result.append(msg)
        return result

    def find_pr_url_messages(self, session_ids: list[str]) -> list[dict[str, Any]]:
        """Tool results in these sessions that mention a GitHub PR url.

        A candidate scan, deliberately loose: it hands back every tool result
        containing ``/pull/`` and leaves the caller to decide which ones make a
        claim (see the desktop's PR recovery, which only accepts an output that
        is a bare PR url — the signature of ``gh pr create``). Ordered
        oldest-first per session so the caller can take the last match.
        """
        found: list[dict[str, Any]] = []
        ids = [s for s in session_ids if s]
        for start in range(0, len(ids), 900):  # SQLite's bound-variable ceiling.
            chunk = ids[start : start + 900]
            placeholders = ",".join("?" * len(chunk))
            with self._read_ctx() as conn:
                rows = conn.execute(
                    f"""SELECT session_id, content FROM messages
                        WHERE session_id IN ({placeholders})
                          AND role = 'tool' AND content LIKE '%/pull/%'
                        ORDER BY id ASC""",
                    chunk,
                ).fetchall()
            found.extend({"session_id": row[0], "content": row[1]} for row in rows)
        return found

    def get_messages_around(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
    ) -> dict[str, Any]:
        """Load a window of messages anchored on a specific message id.

        Returns a dict with:
          - ``window``: up to ``window`` messages before the anchor, the anchor
            itself, and up to ``window`` messages after, ordered by id ascending.
          - ``messages_before``: count of messages strictly before the anchor
            still in the session (== window unless we hit the start).
          - ``messages_after``: count of messages strictly after the anchor
            still in the session (== window unless we hit the end).

        Used by ``session_search`` for both the discovery shape (anchored on the
        FTS5 match) and the scroll shape (anchored on any message id). The
        ``messages_before`` / ``messages_after`` counts let the caller detect
        session boundaries: when either is less than ``window``, the agent has
        reached one end of the session.

        Returns an empty window when ``around_message_id`` is not a real id in
        ``session_id`` — callers decide how to surface that.
        """
        window = max(window, 0)
        with self._read_ctx() as conn:
            # Confirm the anchor exists in this session.
            anchor_exists = conn.execute(
                "SELECT 1 FROM messages WHERE id = ? AND session_id = ? LIMIT 1",
                (around_message_id, session_id),
            ).fetchone()
            if not anchor_exists:
                return {"window": [], "messages_before": 0, "messages_after": 0}

            # Two queries: anchor + before (DESC, take window+1), and after
            # (ASC, take window). Final order is id ASC.
            before_rows = conn.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id <= ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, around_message_id, window + 1),
            ).fetchall()
            after_rows = conn.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (session_id, around_message_id, window),
            ).fetchall()

        # before_rows is DESC; reverse so it's ASC, then concatenate after_rows.
        rows = list(reversed(before_rows)) + list(after_rows)
        result = []
        for row in rows:
            msg = dict(row)
            if "content" in msg:
                msg["content"] = self._decode_content(msg["content"])
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to deserialize tool_calls in get_messages_around, falling back to []"
                    )
                    msg["tool_calls"] = []
            if msg.get("display_metadata") is not None:
                msg["display_metadata"] = self._decode_display_metadata(
                    msg["display_metadata"]
                )
            result.append(msg)

        # before_rows includes the anchor itself; subtract 1 for the count of
        # messages strictly before the anchor in the returned slice.
        messages_before = max(0, len(before_rows) - 1)
        messages_after = len(after_rows)
        return {
            "window": result,
            "messages_before": messages_before,
            "messages_after": messages_after,
        }
