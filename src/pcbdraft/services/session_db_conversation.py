"""Conversation resume and lineage reads for :class:`SessionDB`.

Mixin contract: this plain mixin is consumed by
``pcbdraft.services.session_db.SessionDB``. It defines no ``__init__`` and owns
no connection state. The host supplies connection helpers, transcript decoders,
lineage predicates, safety-limit/error hooks, and compatibility sanitizers. This
module must never import ``session_db`` so the store remains the composition root.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("pcbdraft.services.session_db")


class SessionConversationMixin:
    """Restore model/display conversations and inspect session lineages."""

    def resolve_resume_session_id(self, session_id: str) -> str:
        """Redirect a resume target to the descendant session that holds the messages.

        Context compression ends the current session and forks a new child session
        (linked via ``parent_session_id``). The flush cursor is reset, so the
        child is where new messages actually land — the parent ends up with
        ``message_count = 0`` rows unless messages had already been flushed to
        it before compression. See #15000.

        This helper walks ``parent_session_id`` forward from ``session_id`` and
        returns the descendant in the chain that has the **most recent** messages.
        Unlike the original logic, it does NOT short-circuit when the starting
        session already has messages — a descendant that was created by
        compression may hold the continuation content and should be preferred
        by the WebUI and gateway for ``--resume`` and session loading.

        If no descendant (including the starting session) has any messages,
        the original ``session_id`` is returned unchanged.

        The chain is always walked via the child whose ``started_at`` is
        latest; that matches the single-chain shape that compression creates.
        A depth cap (32) guards against accidental loops in malformed data.
        """
        if not session_id:
            return session_id

        # Follow the compression-continuation chain forward to the live tip
        # FIRST. Auto-compression ends the current session and forks a
        # continuation child, but a long-lived parent keeps its own flushed
        # message rows — so the empty-head walk below never redirects it, and
        # resuming the parent id reloads the pre-compression transcript while
        # the turns generated *after* compression (and their responses) sit in
        # the continuation. ``get_compression_tip`` is lineage-aware: it only
        # follows children whose parent ended with ``end_reason='compression'``
        # (created after the parent was ended), so delegation / branch children
        # never hijack the resume. This is the fix for the desktop "I came back
        # and the reply isn't there" report on large sessions.
        try:
            tip = self.get_compression_tip(session_id)
        except Exception:
            logger.debug("Resume compression tip lookup failed", exc_info=True)
            tip = session_id
        if tip and tip != session_id:
            session_id = tip

        with self._lock:
            current = session_id
            seen = {current}
            best = None  # tracks the last (deepest) node with messages

            for _ in range(32):
                # Check if the current node has messages.
                try:
                    row = self._conn.execute(
                        "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                        (current,),
                    ).fetchone()
                except Exception:
                    logger.debug("Resume message existence probe failed", exc_info=True)
                    return session_id
                if row is not None:
                    best = current

                # Walk to the most-recently-started child — but skip explicit
                # branch (`_branched_from`), delegate/subagent (`_delegate_from`),
                # reset-continuation (`_reset_from` or the legacy same-key
                # heuristic — a post-reset conversation must never be reached
                # by resuming the parent the user reset away), and tool
                # children. They also carry a ``parent_session_id`` yet
                # are NOT compression continuations; following them would hijack
                # the resume target to an unrelated session (e.g. a subagent
                # run). This mirrors the child-exclusion in ``get_compression_tip``.
                try:
                    child_row = self._conn.execute(
                        "SELECT id FROM sessions AS child "
                        "WHERE child.parent_session_id = ? "
                        "  AND json_extract(COALESCE(child.model_config, '{}'), '$._branched_from') IS NULL "
                        "  AND json_extract(COALESCE(child.model_config, '{}'), '$._delegate_from') IS NULL "
                        "  AND json_extract(COALESCE(child.model_config, '{}'), '$._reset_from') IS NULL "
                        f"  AND NOT {self._conversation_legacy_reset_child_sql('child')} "
                        "  AND COALESCE(child.source, '') != 'tool' "
                        "ORDER BY child.started_at DESC, child.id DESC LIMIT 1",
                        (current,),
                    ).fetchone()
                except Exception:
                    logger.debug("Resume child lookup failed", exc_info=True)
                    return session_id
                if child_row is None:
                    break
                child_id = (
                    child_row["id"] if hasattr(child_row, "keys") else child_row[0]
                )
                if not child_id or child_id in seen:
                    break
                seen.add(child_id)
                current = child_id

            return best if best is not None else session_id

    def get_messages_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_inactive: bool = False,
        repair_alternation: bool = False,
        include_row_ids: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.

        By default only active messages are returned. Pass
        ``include_inactive=True`` to load soft-deleted (rewound) rows
        as well. See :meth:`rewind_to_message`.

        ``repair_alternation=True`` runs ``repair_message_sequence`` over the
        loaded list before returning it. Callers that restore a session for
        LIVE REPLAY should pass it: a durable alternation violation (e.g. a
        ``user;user`` pair left by a turn that persisted no assistant row)
        otherwise re-triggers the pre-request defensive repair on every
        single request for the rest of the session's life — the repair
        mutates only the per-request list, never the stored transcript.
        Inspection/export consumers keep the default and see the transcript
        verbatim.
        """
        session_ids = [session_id]
        if include_ancestors and not self._is_explicit_branch_session(session_id):
            session_ids = self._session_lineage_root_to_tip(session_id)

        active_clause = "" if include_inactive else " AND active = 1"
        with self._read_ctx() as conn:
            placeholders = ",".join("?" for _ in session_ids)
            rows = conn.execute(
                f"SELECT {self._CONVERSATION_ROW_COLUMNS} "
                f"FROM messages WHERE session_id IN ({placeholders})"
                # Order by AUTOINCREMENT id (true insertion order), NOT timestamp:
                # append_message stamps rows with time.time(), which is not
                # monotonic (WSL2, NTP steps, VM/laptop sleep resume). A later
                # row can carry an earlier timestamp than its predecessor, and
                # ORDER BY timestamp would then sort an assistant tool_calls row
                # after its tool response, breaking tool-call/response adjacency
                # and triggering an HTTP 400 on replay. This matches get_messages
                # — see c03acca50 for the original fix.
                f"{active_clause} ORDER BY id",
                tuple(session_ids),
            ).fetchall()

        return self._rows_to_conversation(
            rows,
            session_id=session_id,
            include_ancestors=include_ancestors,
            repair_alternation=repair_alternation,
            include_row_ids=include_row_ids,
        )

    # Columns every conversation projection decodes. Shared by
    # get_messages_as_conversation and get_resume_conversations so a single
    # SELECT can feed both the model-fed and display views.
    _CONVERSATION_ROW_COLUMNS = (
        "id, role, content, tool_call_id, tool_calls, tool_name, effect_disposition, "
        "finish_reason, reasoning, reasoning_content, reasoning_details, "
        "codex_reasoning_items, codex_message_items, platform_message_id, observed, timestamp, "
        "api_content, display_kind, display_metadata"
    )

    def _rows_to_conversation(
        self,
        rows,
        *,
        session_id: str,
        include_ancestors: bool,
        repair_alternation: bool,
        include_row_ids: bool = False,
    ) -> list[dict[str, Any]]:
        """Decode fetched message rows into the OpenAI conversation format.

        Extracted from get_messages_as_conversation so get_resume_conversations
        can build the model-fed and display views from one SELECT. ``rows`` must
        already be ordered by ``id`` (insertion order) and filtered to the
        desired session set / active state by the caller.
        """
        messages = []
        for row in rows:
            content = self._decode_content(row["content"])
            if row["role"] in {"user", "assistant"} and isinstance(content, str):
                content = self._conversation_sanitize_context(content).strip()
            msg = {"role": row["role"], "content": content}
            # Durable per-message identity for surfaces that need to address a
            # specific row later (desktop reactions). OPT-IN: only the gateway
            # asks for it — every other consumer (ACP restore, export,
            # inspection) gets the transcript in its historical shape.
            # Underscore-prefixed so every transport's convert_messages()
            # strips it before the wire.
            if include_row_ids and row["id"] is not None:
                msg["_row_id"] = row["id"]
            # api_content is the byte-fidelity sidecar: the exact string sent
            # to the API when it differed from the clean content. Returned
            # VERBATIM — no sanitize_context, no strip — because the replay
            # path substitutes it for content to keep the provider prompt
            # cache prefix byte-stable across turns. Cleaning it here would
            # re-introduce the divergence it exists to remove.
            if row["api_content"]:
                msg["api_content"] = row["api_content"]
            if row["display_kind"]:
                msg["display_kind"] = row["display_kind"]
            if row["display_metadata"]:
                decoded = self._decode_display_metadata(row["display_metadata"])
                if decoded is not None:
                    msg["display_metadata"] = decoded
            if row["timestamp"]:
                msg["timestamp"] = row["timestamp"]
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["effect_disposition"]:
                msg["effect_disposition"] = row["effect_disposition"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to deserialize tool_calls in conversation replay, falling back to []"
                    )
                    msg["tool_calls"] = []
            # Surface the platform-side message id (e.g. yuanbao msg_id,
            # telegram update_id) so platform-specific flows like recall
            # can match by external identifier instead of having to fall
            # back to content-match heuristics.  Exposed as ``message_id``
            # for backward compatibility with the JSONL transcript shape.
            if row["platform_message_id"]:
                msg["message_id"] = row["platform_message_id"]
            if row["observed"]:
                msg["observed"] = True
            # Restore reasoning fields on assistant messages so providers
            # that replay reasoning (OpenRouter, OpenAI, Nous) receive
            # coherent multi-turn reasoning context.
            if row["role"] == "assistant":
                if row["finish_reason"]:
                    msg["finish_reason"] = row["finish_reason"]
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_content"] is not None:
                    msg["reasoning_content"] = row["reasoning_content"]
                if row["reasoning_details"]:
                    try:
                        msg["reasoning_details"] = json.loads(row["reasoning_details"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "Failed to deserialize reasoning_details, falling back to None"
                        )
                        msg["reasoning_details"] = None
                if row["codex_reasoning_items"]:
                    try:
                        msg["codex_reasoning_items"] = json.loads(
                            row["codex_reasoning_items"]
                        )
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "Failed to deserialize codex_reasoning_items, falling back to None"
                        )
                        msg["codex_reasoning_items"] = None
                if row["codex_message_items"]:
                    try:
                        msg["codex_message_items"] = json.loads(
                            row["codex_message_items"]
                        )
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "Failed to deserialize codex_message_items, falling back to None"
                        )
                        msg["codex_message_items"] = None
            if include_ancestors and self._is_duplicate_replayed_user_message(
                messages, msg
            ):
                continue
            messages.append(msg)
        # DEFENSE-IN-DEPTH against background-review session pollution: a forked
        # skill/memory review that (in older builds, before the _persist_disabled
        # fix) shared the parent's session_id wrote its harness turn into this
        # real session. The harness is a user/system message instructing the
        # agent to "Review the conversation above and update the skill library /
        # save to memory" under a hard tool restriction; re-loading it as live
        # history makes the agent adopt the curator role and refuse the user's
        # actual task. Strip any such harness message AND the curator-mode
        # assistant reply immediately following it, so a polluted session
        # resumes clean even if stray rows exist.
        messages = self._conversation_strip_background_review_harness(messages)
        # DEFENSE-IN-DEPTH against #78148: before that fix, a bare tool-call
        # marker (e.g. "[memory]") could get cached as a fallback and
        # persisted as if it were the model's real answer. Sessions written
        # before the fix can still carry those rows — clear the stray
        # content on load so replaying history doesn't re-teach the model
        # to keep emitting the marker. No-op for unaffected sessions.
        messages = self._conversation_strip_stale_tool_call_markers(messages)
        if repair_alternation and messages:
            # Lazy import: session_db already depends on agent.* (see
            # sanitize_context above), but keep this optional path from
            # widening the import surface at module load.
            from pcbdraft.agent.agent_runtime_helpers import repair_message_sequence

            repaired = repair_message_sequence(None, messages)
            if repaired:
                logger.info(
                    "Repaired %d message-alternation violation(s) while "
                    "restoring session %s — durable transcript kept them, "
                    "see repair_message_sequence",
                    repaired,
                    session_id,
                )
        return messages

    def get_resume_conversations(
        self, session_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return ``(model_history, display_history)`` for a session resume in ONE SELECT.

        ``session.resume`` needs two projections of the same lineage:

        - ``model_history`` — the tip session's active rows, alternation-repaired
          (the live-replay working conversation). Equivalent to
          ``get_messages_as_conversation(session_id, repair_alternation=True)``.
        - ``display_history`` — the full compression lineage (ancestors → tip),
          verbatim, with replayed-user dedup. Explicit ``/branch`` sessions are
          excluded from this lineage because their own rows already contain the
          copied transcript; including the live parent's rows would let messages
          written to the original after the fork leak into the branch.

        The display fetch already reads a superset of the model fetch (the tip
        rows are part of the lineage), so serving both from one lineage SELECT
        halves the resume's DB work versus two separate calls, with byte-identical
        output (see test_get_resume_conversations_matches_separate_reads).
        """
        session_ids = (
            [session_id]
            if self._is_explicit_branch_session(session_id)
            else self._session_lineage_root_to_tip(session_id)
        )
        with self._read_ctx() as conn:
            placeholders = ",".join("?" for _ in session_ids)
            rows = conn.execute(
                f"SELECT session_id, {self._CONVERSATION_ROW_COLUMNS} "
                f"FROM messages WHERE session_id IN ({placeholders}) AND active = 1 "
                # ORDER BY id (insertion order) — see get_messages_as_conversation
                # for why timestamp ordering is unsafe.
                "ORDER BY id",
                tuple(session_ids),
            ).fetchall()

        # Tip rows are exactly the model-fed set (get_messages_as_conversation
        # with session_ids=[session_id]); filtering the lineage fetch preserves
        # their relative id order.
        tip_rows = [r for r in rows if r["session_id"] == session_id]
        model_history = self._rows_to_conversation(
            tip_rows,
            session_id=session_id,
            include_ancestors=False,
            repair_alternation=True,
            include_row_ids=True,
        )
        display_history = self._rows_to_conversation(
            rows,
            session_id=session_id,
            include_ancestors=True,
            repair_alternation=False,
            include_row_ids=True,
        )
        return model_history, display_history

    def get_resume_message_count(self, session_id: str) -> int:
        """Count active rows that a full resume would materialize."""
        session_ids = self._session_lineage_root_to_tip(session_id)
        placeholders = ",".join("?" for _ in session_ids)
        with self._read_ctx() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM messages "
                f"WHERE session_id IN ({placeholders}) AND active = 1",
                tuple(session_ids),
            ).fetchone()
        return int(row[0] if row else 0)

    def assert_resume_safe(
        self,
        session_id: str,
        max_messages: int | None = None,
    ) -> int:
        """Return resume row count or reject a transcript too large to load.

        ``max_messages=None`` resolves the limit from config
        (``sessions.max_resume_messages``); 0 disables the guard and returns
        the (bounded) count without raising.
        """
        if max_messages is None:
            max_messages = self._conversation_resolved_max_resume_messages()
        if max_messages < 0:
            raise ValueError("max_messages must be non-negative")
        if max_messages == 0:
            # Guard disabled by config — skip counting entirely. Every live
            # caller invokes this for its raise side effect and ignores the
            # return value, and an unbounded lineage COUNT here would do the
            # exact pathological work the disable exists to avoid.
            return 0
        session_ids = self._session_lineage_root_to_tip(session_id)
        placeholders = ",".join("?" for _ in session_ids)
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM ("
                f"SELECT 1 FROM messages WHERE session_id IN ({placeholders}) "
                "AND active = 1 LIMIT ?"
                ")",
                (*session_ids, max_messages + 1),
            ).fetchone()
        message_count = int(row[0] if row else 0)
        if message_count > max_messages:
            raise self._conversation_resume_too_large_error(message_count, max_messages)
        return message_count

    def assert_export_safe(
        self,
        session_id: str,
        max_messages: int | None = None,
    ) -> int:
        """Return active row count or reject an unsafe in-memory export.

        Exporting one session does not include compression ancestors, so this
        guard deliberately counts only the requested segment. The limited
        subquery stops as soon as it proves the transcript exceeds the bound.

        ``max_messages=None`` resolves the limit from config
        (``sessions.max_export_messages``); 0 disables the guard and returns
        the active row count without raising.
        """
        if max_messages is None:
            max_messages = self._conversation_resolved_max_export_messages()
        if max_messages < 0:
            raise ValueError("max_messages must be non-negative")
        if max_messages == 0:
            # Guard disabled by config — skip the COUNT; live callers use
            # this for its raise side effect only (and skip calling it
            # entirely when the limit is 0).
            return 0
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT 1 FROM messages WHERE session_id = ? AND active = 1 LIMIT ?"
                ")",
                (session_id, max_messages + 1),
            ).fetchone()
        message_count = int(row[0] if row else 0)
        if message_count > max_messages:
            raise self._conversation_export_too_large_error(
                session_id, message_count, max_messages
            )
        return message_count

    def get_ancestor_display_prefix(self, session_id: str) -> list[dict[str, Any]]:
        """Return the ancestor-only display messages for a session lineage.

        These are messages from parent/grandparent sessions (compression
        ancestors) that appear in the display transcript but NOT in the
        tip session's model-fed history. Used by ``session.resume`` to
        build the ``display_history_prefix`` that ``_live_session_payload``
        prepends to the live model history.

        Previously the prefix was calculated as
        ``display_history[:len(display) - len(raw)]``, but that overcounts
        when ``repair_message_sequence`` removes messages from the MIDDLE
        of the tip history (e.g. verification candidates collapsed by the
        consecutive-assistant merge) — the length difference includes both
        ancestor messages AND repair-removed tip messages, but the slice
        only captures the first N display messages (which are tip messages
        when there are no ancestors), causing duplication. This method
        returns ONLY the genuine ancestor messages, identified by
        ``session_id != tip_session_id``. (#65919)
        """
        if self._is_explicit_branch_session(session_id):
            return []

        session_ids = self._session_lineage_root_to_tip(session_id)
        if len(session_ids) <= 1:
            return []
        with self._read_ctx() as conn:
            placeholders = ",".join("?" for _ in session_ids)
            rows = conn.execute(
                f"SELECT session_id, {self._CONVERSATION_ROW_COLUMNS} "
                f"FROM messages WHERE session_id IN ({placeholders}) AND active = 1 "
                "ORDER BY id",
                tuple(session_ids),
            ).fetchall()
        ancestor_rows = [r for r in rows if r["session_id"] != session_id]
        if not ancestor_rows:
            return []
        return self._rows_to_conversation(
            ancestor_rows,
            session_id=session_id,
            include_ancestors=True,
            repair_alternation=False,
        )

    def _is_explicit_branch_session(self, session_id: str) -> bool:
        """Return whether *session_id* is a copied user-facing branch.

        Branches and compression continuations both use ``parent_session_id``,
        but they have different history semantics: a branch owns a copied
        transcript, while a compression continuation needs its ended parent's
        archived rows for display. The durable ``_branched_from`` marker is the
        existing discriminator written by all branch creation paths.
        """
        if not session_id:
            return False
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT model_config FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return False
        raw_config = row["model_config"] if hasattr(row, "keys") else row[0]
        if not raw_config:
            return False
        try:
            config = (
                json.loads(raw_config) if isinstance(raw_config, str) else raw_config
            )
        except (json.JSONDecodeError, TypeError):
            return False
        return isinstance(config, dict) and bool(config.get("_branched_from"))

    def get_conversation_root(self, session_id: str) -> str:
        """Return the ROOT id of *session_id*'s lineage chain.

        The root is the stable "conversation id": context compression
        rotates ``session_id`` to a new segment linked via
        ``parent_session_id``, and delegate subagents hang off their
        parent the same way. Walking to the root gives every segment of
        one user-facing conversation (and its delegation tree) a single
        identifier — used for Nous Portal ``conversation=`` usage tagging.
        Returns *session_id* unchanged when it has no recorded parent.
        """
        chain = self._session_lineage_root_to_tip(session_id)
        return chain[0] if chain and chain[0] else session_id

    def _session_lineage_root_to_tip(self, session_id: str) -> list[str]:
        if not session_id:
            return [session_id]

        chain = []
        current = session_id
        seen = set()
        with self._read_ctx() as conn:
            for _ in range(100):
                if not current or current in seen:
                    break
                seen.add(current)
                chain.append(current)
                row = conn.execute(
                    "SELECT parent_session_id FROM sessions WHERE id = ?",
                    (current,),
                ).fetchone()
                if row is None:
                    break
                current = row["parent_session_id"] if hasattr(row, "keys") else row[0]
        return list(reversed(chain)) or [session_id]
