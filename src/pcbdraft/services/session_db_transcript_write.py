"""Transcript serialization and write behavior for :class:`SessionDB`.

Mixin contract: this plain mixin is consumed by
``pcbdraft.services.session_db.SessionDB``. It defines no ``__init__`` and owns
no connection state. The host supplies the writer connection, transaction
helper, transcript lease lookup, and write patience. This module must never
import ``session_db`` so the store remains the composition root.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pcbdraft.agent.message_sanitization import _sanitize_surrogates
from pcbdraft.services.session_db_runtime import (
    CompressionSessionClosedError,
    SessionTurnLeaseLostError,
)

logger = logging.getLogger("pcbdraft.services.session_db")


def _scrub_surrogates(value: Any) -> Any:
    """Replace lone surrogates when *value* is text; pass anything else through.

    sqlite3 encodes bound ``str`` parameters as UTF-8 and raises
    ``UnicodeEncodeError`` on lone surrogates (U+D800..U+DFFF), so a single
    such code point anywhere in a message aborts the whole write. No-op for
    well-formed text.
    """
    return _sanitize_surrogates(value) if isinstance(value, str) else value


class SessionTranscriptWriteMixin:
    """Serialize, append, annotate, and locate transcript message rows."""

    # =========================================================================
    # Message storage
    # =========================================================================

    # Sentinel prefix used to distinguish JSON-encoded structured content
    # (multimodal messages: lists of parts like text + image_url) from plain
    # string content. The NUL byte is not legal in normal text, so this
    # cannot collide with real user content.
    _CONTENT_JSON_PREFIX = "\x00json:"

    @classmethod
    def _encode_content(cls, content: Any) -> Any:
        """Serialize structured (list/dict) message content for sqlite.

        sqlite3 can only bind ``str``, ``bytes``, ``int``, ``float``, and ``None``
        to query parameters. Multimodal messages have ``content`` as a list of
        parts (``[{"type": "text", ...}, {"type": "image_url", ...}]``), which
        raises ``ProgrammingError: Error binding parameter N: type 'list' is
        not supported`` when bound directly.

        Returns the value unchanged when it's already a safe scalar, or a
        sentinel-prefixed JSON string for lists/dicts. Paired with
        :meth:`_decode_content` on read.
        """
        if isinstance(content, str):
            # Lone UTF-16 surrogates reach here inside tool results scraped
            # from the web/social platforms (the same input that crashed the
            # guardrail hasher). The proactive sanitizer upstream only cleans
            # the *api_messages* copy, and the recovery sanitizer only runs
            # after the API call itself raises — which it no longer does — so
            # the canonical history keeps them and this write is where they
            # land. Left raw, sqlite3 raises UnicodeEncodeError, the flush is
            # abandoned, and the session silently stops persisting for the
            # rest of its life. Scrub so persistence never fails.
            return _sanitize_surrogates(content)
        if content is None or isinstance(content, (bytes, int, float)):
            return content
        try:
            # json.dumps defaults to ensure_ascii=True, which escapes any
            # surrogate as \udXXX — already safe to bind.
            return cls._CONTENT_JSON_PREFIX + json.dumps(content)
        except (TypeError, ValueError):
            # Last-resort fallback: stringify so persistence never fails.
            return _sanitize_surrogates(str(content))

    @classmethod
    def _decode_content(cls, content: Any) -> Any:
        """Reverse :meth:`_encode_content`; returns scalars unchanged."""
        if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
            try:
                return json.loads(content[len(cls._CONTENT_JSON_PREFIX) :])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to decode JSON-encoded message content; "
                    "returning raw string"
                )
                return content
        return content

    @staticmethod
    def _encode_display_metadata(display_metadata: Any) -> str | None:
        """Serialize ``display_metadata`` for its TEXT column without double-encoding.

        Import/replace paths can hand us an already-serialized JSON string (the
        same hazard ``tool_calls`` guards against above). ``json.dumps`` on that
        string would store a quoted JSON string, and the single ``json.loads``
        on read then yields a ``str`` instead of a dict.
        """
        if not display_metadata:
            return None
        if isinstance(display_metadata, str):
            try:
                parsed = json.loads(display_metadata)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Ignoring non-JSON display metadata on write")
                return None
            if not isinstance(parsed, dict):
                logger.warning("Ignoring non-object display metadata on write")
                return None
            return json.dumps(parsed)
        if isinstance(display_metadata, dict):
            return json.dumps(display_metadata)
        logger.warning(
            "Ignoring unexpected display metadata type on write: %s",
            type(display_metadata).__name__,
        )
        return None

    def _check_transcript_write_guards(
        self,
        conn,
        session_id: str,
        compression_lock_holder: str | None,
        turn_lease_holder: str | None = None,
        turn_lease_ttl_seconds: float = 300.0,
    ) -> None:
        """Transcript-append admission checks, run INSIDE the write txn.

        Shared by :meth:`append_message` and :meth:`append_messages_batch` so
        the two writers can never diverge on these correctness invariants
        (this guard has already needed targeted fixes — see the #74478
        patience note below).
        """
        # NOTE (#75316 redesign): appends do NOT check compression_locks.
        # The lock's job is to stop two COMPRESSIONS colliding, not to fence
        # ordinary transcript writes. Concurrent appends during a compression
        # are safe by construction: archive_and_compact() commits against a
        # watermark captured at compression start and clones every row that
        # arrived after it back into the live transcript, in the same write
        # transaction. Blocking appends here was the root cause of a whole
        # symptom family — turns dying as session_persistence_failed while a
        # slow provider summary held the lease (#74568, #77386), including
        # stale locks from dead PIDs blocking writes for the full TTL.
        if turn_lease_holder:
            conversation_id = self._session_turn_lease_key_on_conn(conn, session_id)
            lease = conn.execute(
                "SELECT holder, expires_at FROM session_turn_leases "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if lease is None or lease["holder"] != turn_lease_holder:
                raise SessionTurnLeaseLostError(
                    f"Session turn lease lost; refusing transcript write "
                    f"for {session_id!r}"
                )
            now = time.time()
            if float(lease["expires_at"]) <= now:
                # Expiry makes the row reclaimable; it does not prove that a
                # takeover occurred. BEGIN IMMEDIATE serializes this renewal
                # with acquisition, so a still-matching owner can recover from
                # a starved refresher without weakening the foreign-holder fence.
                conn.execute(
                    "UPDATE session_turn_leases SET expires_at = ? "
                    "WHERE conversation_id = ? AND holder = ?",
                    (
                        now + max(0.1, float(turn_lease_ttl_seconds)),
                        conversation_id,
                        turn_lease_holder,
                    ),
                )
        session = conn.execute(
            "SELECT ended_at, end_reason FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if (
            session is not None
            and session["ended_at"] is not None
            and session["end_reason"] == "compression"
        ):
            raise CompressionSessionClosedError(session_id)

    @staticmethod
    def _decode_display_metadata(raw: Any) -> dict[str, Any] | None:
        """Decode a ``display_metadata`` column into the dict every reader expects.

        Every message read path must go through this. Returning the raw TEXT
        instead reaches the desktop as a string, where ``'task_count' in meta``
        throws and fails the whole resume. Rows written before the encode guard
        landed are double-encoded, so unwrap a second layer when we find one.
        """
        if raw is None:
            return None
        try:
            meta = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(meta, str):
                meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Ignoring invalid display metadata on message row")
            return None
        if not isinstance(meta, dict):
            logger.warning("Ignoring non-object display metadata on message row")
            return None
        return meta

    @staticmethod
    def _reasoning_json_text(value: Any) -> str | None:
        """Serialize a structured reasoning field for its TEXT column.

        ``reasoning_details`` / ``codex_reasoning_items`` / ``codex_message_items``
        arrive as list/dict structures from the live runtime, but callers that
        round-trip stored rows — ``get_messages`` straight into
        ``replace_messages``, e.g. the POST /api/sessions/{id}/fork handler —
        hand back the raw TEXT these columns already hold, because
        ``get_messages`` only deserializes ``content`` and ``tool_calls``.
        Re-dumping that TEXT double-encodes it, and the forked session's next
        ``get_messages_as_conversation`` json.loads then yields the inner
        string instead of the original list, so every reasoning-replay consumer
        (all of which check ``isinstance(..., list)``) silently drops it.
        Strings are therefore stored as-is; structures are dumped.
        """
        if not value:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str | None = None,
        tool_name: str | None = None,
        tool_calls: Any = None,
        tool_call_id: str | None = None,
        token_count: int | None = None,
        finish_reason: str | None = None,
        reasoning: str | None = None,
        reasoning_content: str | None = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        codex_message_items: Any = None,
        platform_message_id: str | None = None,
        observed: bool = False,
        effect_disposition: str | None = None,
        timestamp: Any = None,
        api_content: str | None = None,
        display_kind: str | None = None,
        display_metadata: dict[str, Any] | None = None,
        compression_lock_holder: str | None = None,
        turn_lease_holder: str | None = None,
        turn_lease_ttl_seconds: float = 300.0,
    ) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).

        ``platform_message_id`` is the external messaging platform's own
        message ID (e.g. Telegram update_id, Yuanbao msg_id).  It is
        independent of the SQLite autoincrement primary key and is used by
        platform-specific flows like yuanbao's recall guard to redact a
        message by its platform-side identifier.

        ``api_content`` is the exact content string sent to the API for this
        message when it differs from ``content`` (ephemeral memory/plugin
        injections, persist overrides).  It is a byte-fidelity sidecar for
        prompt-cache-stable replay — stored as sent, except lone surrogates
        (which sqlite3 cannot bind and which the conversation loop scrubs
        from every outgoing payload anyway, so the scrubbed form IS the
        wire bytes).
        """
        # Display metadata is presentation-only and never changes the model
        # context role/content replayed to providers.
        display_metadata_json = self._encode_display_metadata(display_metadata)
        # Serialize structured fields to JSON before entering the write txn
        reasoning_details_json = self._reasoning_json_text(reasoning_details)
        codex_items_json = self._reasoning_json_text(codex_reasoning_items)
        codex_message_items_json = self._reasoning_json_text(codex_message_items)
        # tool_calls may arrive as a Python list (from the live agent) or
        # as a JSON string (from import/export). Parse first to avoid
        # double-encoding.
        if isinstance(tool_calls, str):
            try:
                tool_calls = json.loads(tool_calls)
            except (json.JSONDecodeError, TypeError):
                tool_calls = []
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        # Multimodal content (list of parts) must be JSON-encoded: sqlite3
        # cannot bind list/dict parameters directly.
        stored_content = self._encode_content(content)

        message_timestamp = time.time()
        if timestamp is not None:
            try:
                if hasattr(timestamp, "timestamp"):
                    message_timestamp = float(timestamp.timestamp())
                else:
                    message_timestamp = float(timestamp)
            except (TypeError, ValueError):
                logger.debug(
                    "Ignoring invalid explicit message timestamp: %r", timestamp
                )

        # Pre-compute tool call count
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        def _do(conn):
            self._check_transcript_write_guards(
                conn,
                session_id,
                compression_lock_holder,
                turn_lease_holder=turn_lease_holder,
                turn_lease_ttl_seconds=turn_lease_ttl_seconds,
            )
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, effect_disposition, timestamp, token_count, finish_reason,
                   reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                   codex_message_items, platform_message_id, observed, active, api_content, display_kind, display_metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    stored_content,
                    tool_call_id,
                    tool_calls_json,
                    _scrub_surrogates(tool_name),
                    effect_disposition,
                    message_timestamp,
                    token_count,
                    finish_reason,
                    _scrub_surrogates(reasoning),
                    _scrub_surrogates(reasoning_content),
                    reasoning_details_json,
                    codex_items_json,
                    codex_message_items_json,
                    platform_message_id,
                    1 if observed else 0,
                    1,
                    _scrub_surrogates(api_content)
                    if isinstance(api_content, str)
                    else None,
                    _scrub_surrogates(display_kind)
                    if isinstance(display_kind, str)
                    else None,
                    display_metadata_json,
                ),
            )
            msg_id = cursor.lastrowid

            # Update counters
            if num_tool_calls > 0:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                    (num_tool_calls, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )
            return msg_id

        # Transcript append is THE critical write: its failure aborts the
        # user's turn (session_persistence_failed). Use the long patience so
        # a sibling process legitimately holding the write lock for seconds
        # (VACUUM, TRUNCATE checkpoint at close, an older pre-bounded-merge
        # process's FTS optimize) can't destroy a healthy turn (#74478).
        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def append_messages_batch(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        compression_lock_holder: str | None = None,
        turn_lease_holder: str | None = None,
        chunk_rows: int | None = None,
        turn_lease_ttl_seconds: float = 300.0,
    ) -> int:
        """Append multiple messages atomically in ONE write transaction.

        ``messages`` is a list of dicts in the same shape
        :meth:`_insert_message_rows` already consumes for replace/compact/
        import (role, content, tool_name, tool_calls, tool_call_id,
        finish_reason, reasoning*, codex_*, timestamp, api_content,
        display_kind, display_metadata, ...). Reusing that helper keeps ONE
        row-serialization path for every multi-row writer.

        A turn-boundary flush writes the whole turn (user + assistant + tool
        rows, typically 3-8 messages) as one BEGIN IMMEDIATE / commit pair
        instead of one transaction (and, off WAL, one fsync) per row.

        Atomicity contract: all rows land or none do (the caller re-flushes
        unstamped messages on the next attempt). The same admission guards
        as :meth:`append_message` run once for the batch — same session,
        same instant.

        ``chunk_rows`` bounds the transaction size for LARGE copies (branch
        seeds can be thousands of rows; measured: 10k rows ≈ 2.4s inside one
        BEGIN IMMEDIATE because the FTS triggers run per row, which would
        monopolize the write lock and starve concurrent writers). When set,
        the batch commits in chunks of at most that many rows — same
        recovery semantics as the old per-row loops (a mid-copy failure
        leaves a partial seed), just with bounded lock holds. A turn flush
        never needs it. Returns the inserted row count.
        """
        if not messages:
            return 0

        if chunk_rows is not None and len(messages) > chunk_rows:
            inserted_total = 0
            for start in range(0, len(messages), chunk_rows):
                inserted_total += self.append_messages_batch(
                    session_id,
                    messages[start : start + chunk_rows],
                    compression_lock_holder=compression_lock_holder,
                    turn_lease_holder=turn_lease_holder,
                    turn_lease_ttl_seconds=turn_lease_ttl_seconds,
                )
            return inserted_total

        def _do(conn):
            self._check_transcript_write_guards(
                conn,
                session_id,
                compression_lock_holder,
                turn_lease_holder=turn_lease_holder,
                turn_lease_ttl_seconds=turn_lease_ttl_seconds,
            )
            inserted, tool_calls_total = self._insert_message_rows(
                conn, session_id, messages
            )
            # One aggregated counter update for the whole batch.
            if tool_calls_total > 0:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + ?,
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                    (inserted, tool_calls_total, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + ? WHERE id = ?",
                    (inserted, session_id),
                )
            return inserted

        # Same criticality as append_message: this IS the turn's transcript.
        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def set_latest_matching_message_display_kind(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        display_kind: str,
        display_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Stamp presentation metadata on this turn's freshly persisted row.

        The model still receives ``role`` and ``content`` unchanged. Gateway and
        CLI synthetic inputs call this immediately after their serial turn has
        flushed, preserving producer provenance without classifying by content
        during transcript rendering.
        """
        if not session_id or not content or not display_kind:
            return False

        def _do(conn):
            row = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND role = ? "
                "AND content = ? AND active = 1 ORDER BY id DESC LIMIT 1",
                (session_id, role, self._encode_content(content)),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE messages SET display_kind = ?, display_metadata = ? WHERE id = ?",
                (
                    _scrub_surrogates(display_kind),
                    self._encode_display_metadata(display_metadata),
                    row[0],
                ),
            )
            return True

        return bool(self._execute_write(_do))

    #: Key under which message reactions live inside ``display_metadata``.
    #: Reactions share the existing per-message JSON column rather than a side
    #: table so they survive rewind/compaction row rewrites with the row itself.
    REACTIONS_METADATA_KEY = "reactions"

    def set_message_reaction(
        self,
        session_id: str,
        message_row_id: int,
        emoji: str | None,
        *,
        author: str = "user",
    ) -> list[dict[str, Any]] | None:
        """Set (or with ``emoji=None`` clear) *author*'s reaction on one message.

        iOS Tapback semantics: one reaction per author per message. Re-sending
        the same emoji clears it, a different emoji replaces it. Returns the
        message's full reaction list after the write, or ``None`` when the row
        doesn't exist or isn't part of *session_id*.
        """
        if not session_id or message_row_id is None:
            return None

        def _do(conn):
            row = conn.execute(
                "SELECT display_metadata FROM messages WHERE id = ? AND session_id = ?",
                (message_row_id, session_id),
            ).fetchone()
            if row is None:
                return None

            meta = self._decode_display_metadata(row[0]) or {}
            existing = meta.get(self.REACTIONS_METADATA_KEY)
            reactions = [
                r
                for r in (existing if isinstance(existing, list) else [])
                if isinstance(r, dict) and r.get("author") != author
            ]
            previous = next(
                (
                    r
                    for r in (existing if isinstance(existing, list) else [])
                    if isinstance(r, dict) and r.get("author") == author
                ),
                None,
            )
            # Tapping the live reaction again retracts it.
            toggling_off = (
                emoji is not None
                and previous is not None
                and previous.get("emoji") == emoji
            )
            if emoji and not toggling_off:
                reactions.append(
                    {
                        "emoji": _scrub_surrogates(emoji),
                        "author": author,
                        "at": time.time(),
                    }
                )

            if reactions:
                meta[self.REACTIONS_METADATA_KEY] = reactions
            else:
                meta.pop(self.REACTIONS_METADATA_KEY, None)

            conn.execute(
                "UPDATE messages SET display_metadata = ? WHERE id = ?",
                (self._encode_display_metadata(meta) if meta else None, message_row_id),
            )
            return reactions

        return self._execute_write(_do)

    def get_message_reactions(
        self, session_id: str, message_row_id: int
    ) -> list[dict[str, Any]]:
        """Return the reaction list persisted on one message row (never ``None``)."""
        if not session_id or message_row_id is None:
            return []

        with self._lock:
            row = self._conn.execute(
                "SELECT display_metadata FROM messages WHERE id = ? AND session_id = ?",
                (message_row_id, session_id),
            ).fetchone()

        if row is None:
            return []

        meta = self._decode_display_metadata(row[0]) or {}
        reactions = meta.get(self.REACTIONS_METADATA_KEY)

        return (
            [r for r in reactions if isinstance(r, dict)]
            if isinstance(reactions, list)
            else []
        )

    def take_unseen_reactions(
        self, session_id: str, *, author: str = "user"
    ) -> list[dict[str, Any]]:
        """Return *author*'s not-yet-surfaced reactions and mark them seen.

        Powers the cache-safe model-context path: reactions are announced on the
        NEXT user turn (never by rewriting the message that was reacted to), and
        the ``seen`` stamp guarantees each one is announced exactly once.
        """
        if not session_id:
            return []

        def _do(conn):
            rows = conn.execute(
                "SELECT id, role, content, display_metadata FROM messages "
                "WHERE session_id = ? AND active = 1 AND display_metadata IS NOT NULL "
                "ORDER BY id",
                (session_id,),
            ).fetchall()

            pending = []
            for row in rows:
                meta = self._decode_display_metadata(row["display_metadata"])
                if not meta:
                    continue
                reactions = meta.get(self.REACTIONS_METADATA_KEY)
                if not isinstance(reactions, list):
                    continue

                changed = False
                for reaction in reactions:
                    if (
                        not isinstance(reaction, dict)
                        or reaction.get("author") != author
                        or reaction.get("seen")
                    ):
                        continue
                    reaction["seen"] = True
                    changed = True
                    content = self._decode_content(row["content"])
                    pending.append(
                        {
                            "row_id": row["id"],
                            "role": row["role"],
                            "emoji": reaction.get("emoji") or "",
                            "text": content if isinstance(content, str) else "",
                        }
                    )

                if changed:
                    conn.execute(
                        "UPDATE messages SET display_metadata = ? WHERE id = ?",
                        (self._encode_display_metadata(meta), row["id"]),
                    )

            return pending

        return self._execute_write(_do) or []

    def latest_message_row_id(
        self,
        session_id: str,
        *,
        role: str = "user",
        offset: int = 0,
        require_text: bool = True,
    ) -> int | None:
        """Row id of the most recent active message with *role*, or ``None``.

        Two callers, same need — "the message I mean, without an id": the agent
        defaulting to the turn that triggered it, and the desktop reacting to a
        live message that hasn't round-tripped through a resume yet.
        ``offset`` steps to earlier turns (1 = the one before the latest) so a
        reaction can land retroactively — "two messages ago" is how the caller
        thinks about it.

        ``require_text`` (default) skips rows with no plain-text content —
        tool-call-only assistant turns and attachment stubs don't render as
        bubbles, so "the latest message" as a HUMAN means it must never
        resolve to one (a reaction landing on an invisible row looks dropped,
        and its annotation quotes an empty string).
        """
        if not session_id or role not in {"user", "assistant"} or offset < 0:
            return None

        text_filter = (
            "AND content IS NOT NULL AND TRIM(content) != '' " if require_text else ""
        )

        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND role = ? "
                f"AND active = 1 {text_filter}ORDER BY id DESC LIMIT 1 OFFSET ?",
                (session_id, role, int(offset)),
            ).fetchone()

        return row[0] if row else None

    def latest_user_message_row_id(self, session_id: str) -> int | None:
        """Row id of the most recent active user message, or ``None``.

        The agent's default reaction target: "the message that triggered me",
        so the model never has to thread row ids through a tool call (mirrors
        the photon adapter's ``_record_last_inbound``).
        """
        return self.latest_message_row_id(session_id, role="user")

    def get_message_role(self, session_id: str, row_id: int) -> str | None:
        """Role of the active message at *row_id* in *session_id*, or ``None``.

        Lets a reaction event carry the target's role so a renderer can match
        a live message that doesn't know its durable row id yet.
        """
        if not session_id:
            return None

        with self._lock:
            row = self._conn.execute(
                "SELECT role FROM messages WHERE id = ? AND session_id = ? AND active = 1",
                (int(row_id), session_id),
            ).fetchone()

        return row[0] if row else None

    def _insert_message_rows(
        self, conn, session_id: str, messages: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Insert *messages* as fresh active rows for *session_id*.

        Shared by :meth:`replace_messages` (delete-then-insert) and
        :meth:`archive_and_compact` (soft-archive-then-insert). Runs inside the
        caller's write transaction (takes the live ``conn``). Returns
        ``(inserted_count, tool_call_count)``. Does NOT touch sessions.* counters
        — the caller owns that, since the two flows reconcile counts differently.
        """
        now_ts = time.time()
        inserted = 0
        tool_calls_total = 0
        for msg in messages:
            role = msg.get("role", "unknown")
            tool_calls = msg.get("tool_calls")
            message_timestamp = now_ts
            if msg.get("timestamp") is not None:
                try:
                    ts_value = msg.get("timestamp")
                    if hasattr(ts_value, "timestamp"):
                        message_timestamp = float(ts_value.timestamp())
                    else:
                        message_timestamp = float(ts_value)
                except (TypeError, ValueError):
                    logger.debug(
                        "Ignoring invalid explicit message timestamp: %r",
                        msg.get("timestamp"),
                    )
            reasoning_details = (
                msg.get("reasoning_details") if role == "assistant" else None
            )
            codex_reasoning_items = (
                msg.get("codex_reasoning_items") if role == "assistant" else None
            )
            codex_message_items = (
                msg.get("codex_message_items") if role == "assistant" else None
            )
            reasoning_details_json = self._reasoning_json_text(reasoning_details)
            codex_items_json = self._reasoning_json_text(codex_reasoning_items)
            codex_message_items_json = self._reasoning_json_text(codex_message_items)
            # tool_calls may arrive as a Python list (from the live agent)
            # or as a JSON string (from import_sessions / export_session,
            # which store it as TEXT). json.dumps on an already-serialized
            # string double-encodes it, so parse first.
            if isinstance(tool_calls, str):
                try:
                    tool_calls = json.loads(tool_calls)
                except (json.JSONDecodeError, TypeError):
                    tool_calls = []
            tool_calls_json = json.dumps(tool_calls) if tool_calls else None
            # Accept either `platform_message_id` (new explicit name) or
            # `message_id` (yuanbao's existing convention on message dicts).
            platform_msg_id = msg.get("platform_message_id") or msg.get("message_id")

            api_content = msg.get("api_content")

            cur = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, effect_disposition, timestamp, token_count, finish_reason,
                   reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                   codex_message_items, platform_message_id, observed, active, api_content, display_kind, display_metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    self._encode_content(msg.get("content")),
                    msg.get("tool_call_id"),
                    tool_calls_json,
                    _scrub_surrogates(msg.get("tool_name")),
                    msg.get("effect_disposition"),
                    message_timestamp,
                    msg.get("token_count"),
                    msg.get("finish_reason"),
                    _scrub_surrogates(msg.get("reasoning"))
                    if role == "assistant"
                    else None,
                    _scrub_surrogates(msg.get("reasoning_content"))
                    if role == "assistant"
                    else None,
                    reasoning_details_json,
                    codex_items_json,
                    codex_message_items_json,
                    platform_msg_id,
                    1 if msg.get("observed") else 0,
                    1,
                    _scrub_surrogates(api_content)
                    if isinstance(api_content, str)
                    else None,
                    _scrub_surrogates(msg.get("display_kind"))
                    if isinstance(msg.get("display_kind"), str)
                    else None,
                    self._encode_display_metadata(msg.get("display_metadata")),
                ),
            )
            if isinstance(msg, dict) and cur.lastrowid is not None:
                msg["_row_id"] = cur.lastrowid
            inserted += 1
            if tool_calls is not None:
                tool_calls_total += (
                    len(tool_calls) if isinstance(tool_calls, list) else 1
                )
            now_ts = max(now_ts + 1e-6, message_timestamp + 1e-6)
        return inserted, tool_calls_total
