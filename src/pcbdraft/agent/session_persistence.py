"""Persist AIAgent session messages and repair durable transcript state."""

# Persistence remains best-effort across provider and SQLite compatibility APIs.
# ruff: noqa: BLE001

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pcbdraft.agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
)
from pcbdraft.agent.memory_manager import sanitize_context
from pcbdraft.agent.tool_dispatch_helpers import (
    _is_multimodal_tool_result,
    _multimodal_text_summary,
)

logger = logging.getLogger(__name__)

_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_kanban_stop_synthetic",
    "_dropped_toolcall_nudge",
)
_DB_PERSISTED_MARKER = "_db_persisted"


def _is_ephemeral_scaffolding(message: Any) -> bool:
    """Return whether a message is internal recovery-only scaffolding."""
    return isinstance(message, dict) and any(
        message.get(flag) for flag in _EPHEMERAL_SCAFFOLDING_FLAGS
    )


_classify_summary_content_hook: Callable[[Any], str]
_compressed_summary_metadata_key_hook: Callable[[], str]
_db_persisted_marker_hook: Callable[[], str]
_is_ephemeral_scaffolding_hook: Callable[[Any], bool]
_is_multimodal_tool_result_hook: Callable[[Any], bool]
_multimodal_text_summary_hook: Callable[[Any], str]
_sanitize_context_hook: Callable[[str], str]
_warning_hook: Callable[..., None]


def configure_session_persistence_runtime(
    *,
    classify_summary_content: Callable[[Any], str] | None = None,
    compressed_summary_metadata_key: Callable[[], str] | None = None,
    db_persisted_marker: Callable[[], str] | None = None,
    is_ephemeral_scaffolding: Callable[[Any], bool] | None = None,
    is_multimodal_tool_result: Callable[[Any], bool] | None = None,
    multimodal_text_summary: Callable[[Any], str] | None = None,
    sanitize_content: Callable[[str], str] | None = None,
    warning: Callable[..., None] | None = None,
) -> None:
    """Inject helpers exposed through the legacy agent module."""
    global _classify_summary_content_hook
    global _compressed_summary_metadata_key_hook
    global _db_persisted_marker_hook
    global _is_ephemeral_scaffolding_hook
    global _is_multimodal_tool_result_hook
    global _multimodal_text_summary_hook
    global _sanitize_context_hook
    global _warning_hook

    if classify_summary_content is not None:
        _classify_summary_content_hook = classify_summary_content
    if compressed_summary_metadata_key is not None:
        _compressed_summary_metadata_key_hook = compressed_summary_metadata_key
    if db_persisted_marker is not None:
        _db_persisted_marker_hook = db_persisted_marker
    if is_ephemeral_scaffolding is not None:
        _is_ephemeral_scaffolding_hook = is_ephemeral_scaffolding
    if is_multimodal_tool_result is not None:
        _is_multimodal_tool_result_hook = is_multimodal_tool_result
    if multimodal_text_summary is not None:
        _multimodal_text_summary_hook = multimodal_text_summary
    if sanitize_content is not None:
        _sanitize_context_hook = sanitize_content
    if warning is not None:
        _warning_hook = warning


class SessionPersistenceMixin:
    """Write session snapshots and append durable transcript messages."""

    def _build_memory_write_metadata(
        self,
        *,
        write_origin: str | None = None,
        execution_context: str | None = None,
        task_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        """Forwarder — see ``agent.background_review.build_memory_write_metadata``."""
        from pcbdraft.agent.background_review import build_memory_write_metadata

        return build_memory_write_metadata(
            self,
            write_origin=write_origin,
            execution_context=execution_context,
            task_id=task_id,
            tool_call_id=tool_call_id,
        )

    def _apply_persist_user_message_override(self, messages: list[dict]) -> None:
        """Rewrite the current-turn user message before persistence/return.

        Some call paths need an API-only user-message variant without letting
        that synthetic text leak into persisted transcripts or resumed session
        history. When an override is configured for the active turn, mutate the
        in-memory messages list in place so both persistence and returned
        history stay clean.  A paired timestamp override preserves the platform
        event time as message metadata, rather than embedding it in content.
        """
        idx = getattr(self, "_persist_user_message_idx", None)
        override = getattr(self, "_persist_user_message_override", None)
        timestamp = getattr(self, "_persist_user_message_timestamp", None)
        if idx is None or (override is None and timestamp is None):
            return
        if 0 <= idx < len(messages):
            msg = messages[idx]
            if isinstance(msg, dict) and msg.get("role") == "user":
                # Text-only call paths may pass a synthetic API-facing prompt
                # and a cleaner transcript string separately. Before the API
                # call, a plain-text override must not replace native image/audio
                # blocks. A list override, however, is the original clean
                # multimodal payload (for example before a queued /model note)
                # and must replace the API-local list once the turn is final.
                # Preflight compaction can re-anchor this index at a message
                # whose content was MERGED with the compaction summary
                # (merge-summary-into-tail).  That is not an accident:
                # ``reanchor_current_turn_user_idx`` falls back to the last
                # user row precisely BECAUSE the merge rewrote the content and
                # the exact-match lookup misses.  Overwriting it with the clean
                # text would drop the summary from the continuation history the
                # next turn is built from — the same hazard the DB-write twin
                # below already refuses (see the sibling guard in
                # ``_flush_messages_to_session_db_unlocked``).
                if (
                    override is not None
                    and not msg.get(_compressed_summary_metadata_key_hook())
                    and (
                        not isinstance(msg.get("content"), list)
                        or isinstance(override, list)
                    )
                ):
                    msg["content"] = override
                if timestamp is not None:
                    msg["timestamp"] = timestamp

    def _persist_session(
        self, messages: list[dict], conversation_history: list[dict] | None = None
    ):
        """Save session state to both JSON log and SQLite on any exit path.

        Ensures conversations are never lost, even on errors or early returns.

        Trailing empty-response scaffolding is dropped from the live list in
        place (it is ephemeral junk the real transcript should shed). The
        persist user-message *override* is NOT applied here — it is resolved
        inside ``_flush_messages_to_session_db`` and written only to the DB row,
        never mutating the live message list used by the API call (#48677 is
        thus closed for every persist caller, not just this one).
        """
        # Scaffolding removal mutates the live list (desired — ephemeral
        # retry/failure sentinels must not survive into the real transcript).
        # Close and turn-start persistence can run on separate CLI threads; the
        # marker test-and-append below must be one critical section or both can
        # observe the same unmarked dict and write duplicate durable rows.
        from pcbdraft.agent.agent_runtime_helpers import note_turn_persisted

        persist_lock = getattr(self, "_session_persist_lock", None)

        def _persist_and_drain() -> None:
            self._drop_trailing_empty_response_scaffolding(messages)
            self._session_messages = messages
            self._save_session_log(messages)
            self._flush_messages_to_session_db(messages, conversation_history)
            # Drain async token-accounting deltas at every persist point (turn
            # finalize + error exits) so a crash after this line loses at most
            # the in-flight API call's delta. Cheap no-op when nothing queued.
            if self._session_db is not None:
                self._session_db.flush_token_counts()
            note_turn_persisted(self)

        if persist_lock is None:
            _persist_and_drain()
            return

        with persist_lock:
            _persist_and_drain()

    def _drop_trailing_empty_response_scaffolding(self, messages: list[dict]) -> None:
        """Remove private empty-response retry/failure scaffolding from transcript tails.

        Also rewinds past any trailing tool-result / assistant(tool_calls) pair
        that the failed iteration left hanging. Without this, the tail ends at
        a raw ``tool`` message and the next user turn lands as
        ``...tool, user, user`` — a protocol-invalid sequence that most
        providers silently reject (returns empty content), causing the
        empty-retry loop to fire forever. (issue number to be backfilled once filed)
        """
        # Pass 1: strip the flagged scaffolding messages themselves.
        dropped_scaffolding = False
        while (
            messages
            and isinstance(messages[-1], dict)
            and (
                messages[-1].get("_empty_recovery_synthetic")
                or messages[-1].get("_empty_terminal_sentinel")
            )
        ):
            messages.pop()
            dropped_scaffolding = True

        # Pass 2: if we stripped scaffolding, rewind through any trailing
        # tool-result messages plus the assistant(tool_calls) message that
        # produced them. This preserves role alternation so the next user
        # message follows a user or assistant message, not an orphan tool
        # result. Only runs when scaffolding was actually present — normal
        # conversation tails (real tool loops mid-progress) are untouched.
        if not dropped_scaffolding:
            return

        # Drop any trailing tool-result messages
        while (
            messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "tool"
        ):
            messages.pop()

        # Drop the assistant message that issued the tool calls, if the tail
        # now ends in an assistant-with-tool_calls (the pair that owned the
        # just-popped tool results). Without this, the tail is
        # ``assistant(tool_calls=...)`` with no tool answers, which some
        # providers also reject.
        if (
            messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "assistant"
            and messages[-1].get("tool_calls")
        ):
            messages.pop()

    def _repair_message_sequence(self, messages: list[dict]) -> int:
        """Forwarder — see ``agent.agent_runtime_helpers.repair_message_sequence``."""
        from pcbdraft.agent.agent_runtime_helpers import repair_message_sequence

        return repair_message_sequence(self, messages)

    def _flush_messages_to_session_db(
        self,
        messages: list[dict],
        conversation_history: list[dict] | None = None,
    ):
        """Serialize direct and turn-boundary session flushes per agent."""
        persist_lock = getattr(self, "_session_persist_lock", None)
        if persist_lock is None:
            return self._flush_messages_to_session_db_unlocked(
                messages, conversation_history
            )
        with persist_lock:
            return self._flush_messages_to_session_db_unlocked(
                messages, conversation_history
            )

    def _flush_messages_to_session_db_unlocked(
        self,
        messages: list[dict],
        conversation_history: list[dict] | None = None,
        _adoption_budget: int = 1,
    ):
        """Persist any un-flushed messages to the SQLite session store.

        Deduplicates via an intrinsic ``_DB_PERSISTED_MARKER`` stamped on each
        written message dict, so repeated calls (from multiple exit paths) only
        write truly new messages — preventing the duplicate-write bug (#860)
        without relying on positional slices that can drift after
        message-sequence repair, and without a retained ``id(msg)`` set that
        CPython could alias onto a freed-then-reused address (#50372). The
        ``_flushed_db_message_ids`` attribute is now only a one-shot seed
        (translated to markers, then cleared each flush), not a persisted set.

        Note: the marker is stamped on the live/shared conversation dict, which
        correctly makes re-persistence idempotent across turns. No code path
        edits a persisted message's content/role in place expecting a re-write
        (in-place compaction resets the seed and re-diffs by identity).
        """
        # Persistence-isolated agents (e.g. the background skill/memory review
        # fork) must NEVER write into the canonical session store. The fork
        # shares the parent's session_id for prompt-cache warmth, so any write
        # here would land its harness turn ("Review the conversation above and
        # update the skill library…") inside the user's real session history,
        # where the next live turn re-reads it as an instruction and the agent
        # "becomes" the curator. Hard-stop before any DB touch.
        if getattr(self, "_persist_disabled", False):
            return None
        if not self._session_db:
            return None
        # Persist user-message override (#48677 chokepoint): historically this
        # mutated the live `messages` list in place, which — on the early
        # crash-resilience persist that runs BEFORE the API call is built —
        # stripped observed group-chat context off the live user message and
        # silently dropped it. Instead, resolve the override here and apply it
        # ONLY to the value written to the DB (see the write loop below); the
        # live dict is never mutated, so every caller (early persist, mid-loop
        # flush, /resume, /branch) is protected uniformly. Timestamp override is
        # metadata and is likewise applied only to the written row.
        _ov_idx = getattr(self, "_persist_user_message_idx", None)
        _ov_content = getattr(self, "_persist_user_message_override", None)
        _ov_timestamp = getattr(self, "_persist_user_message_timestamp", None)
        try:
            # Retry row creation if the earlier attempt failed transiently.
            if not self._session_db_created:
                self._ensure_db_session()
            # Positional flushing used to slice at
            # max(len(conversation_history), _last_flushed_db_idx). That
            # assumes the live `messages` list is the original history plus a
            # new tail. repair_message_sequence can shrink/merge the history
            # copy before the final flush, making len(conversation_history)
            # larger than len(messages); the slice is then empty and delivered
            # assistant responses never reach state.db (#46053).
            #
            # Track persistence with an intrinsic per-message marker rather than
            # id(msg). `messages` is a shallow copy of `conversation_history`, so
            # history dicts are skipped by identity, and new dicts appended
            # during this turn are written once even if repair compacts the list
            # around them. Unlike an id()-keyed set, a marker bound to the dict
            # cannot be aliased onto a freed-then-reused address, so a real turn
            # can never be silently skipped (see _DB_PERSISTED_MARKER).
            #
            # `self._flushed_db_message_ids` is still honoured as a *one-shot*
            # seed: external callers (gateway shutdown, tests) populate it with
            # {id(m) for m in already_persisted} immediately before the flush,
            # while those objects are alive — so the ids are valid at that
            # instant. We translate the seed into durable markers and then clear
            # the set, so stale ids can never accumulate across turns and alias a
            # future message.
            current_session_id = getattr(self, "session_id", None)
            flushed_session_id = getattr(self, "_flushed_db_message_session_id", None)
            if (
                flushed_session_id != current_session_id
                or self._last_flushed_db_idx == 0
            ):
                seed_ids = set()
            else:
                seed_ids = getattr(self, "_flushed_db_message_ids", None)
                if not isinstance(seed_ids, set):
                    seed_ids = set()
            self._flushed_db_message_session_id = current_session_id
            history_ids = {
                id(item)
                for item in (conversation_history or [])
                if isinstance(item, dict)
            }

            # Bounded scan: skip the longest identity-matched prefix of the
            # list snapshot taken at the end of the previous successful flush.
            # Every message in that snapshot was already given its final
            # disposition (written+stamped, stamped as durable history, or
            # skipped as ephemeral scaffolding / non-dict), and no code path
            # pops _DB_PERSISTED_MARKER from a live dict in place (compression
            # strips markers on fresh copies, which breaks identity here and
            # forces a full re-scan). Identity match ⇒ identical skip decision,
            # so starting after the matched prefix is behavior-preserving.
            _scan_start = 0
            _prev_prefix = getattr(self, "_db_flush_scan_prefix", None)
            if isinstance(_prev_prefix, list):
                _limit = min(len(_prev_prefix), len(messages))
                while (
                    _scan_start < _limit
                    and messages[_scan_start] is _prev_prefix[_scan_start]
                ):
                    _scan_start += 1

            # Collect this flush's new rows and write them in ONE transaction
            # at the end of the scan (see append_messages_batch).
            _batch_rows: list[dict[str, Any]] = []
            _batch_msgs: list[dict] = []
            for _msg_idx in range(_scan_start, len(messages)):
                msg = messages[_msg_idx]
                if not isinstance(msg, dict):
                    continue
                # Never write ephemeral recovery scaffolding to the session
                # store. The flush is append-only (it only advances
                # _last_flushed_db_idx via identity tracking), so a synthetic
                # message committed by a mid-turn persist cannot be un-written
                # when the end-of-turn drop removes it from the in-memory list —
                # the resumed transcript would then replay synthetic
                # "(empty)"/nudge/thinking-prefill turns as if they were genuine
                # context. Skip regardless of position: an answered nudge leaves
                # the synthetic pair buried mid-list, not just at the tail.
                if _is_ephemeral_scaffolding_hook(msg):
                    continue
                if msg.get(_db_persisted_marker_hook()):
                    continue
                # Already-durable messages: either carried over from the loaded
                # history copy, or seeded by a caller. Stamp them so future
                # flushes skip them without consulting any id() set again.
                if id(msg) in history_ids or id(msg) in seed_ids:
                    msg[_db_persisted_marker_hook()] = True
                    continue
                role = msg.get("role", "unknown")
                content = msg.get("content")
                # api_content sidecar: the exact bytes sent to the API when
                # they differ from the clean content (stamped by the turn
                # prologue for prefetch/plugin injections). Written verbatim
                # so replay can reproduce the sent prefix byte-for-byte.
                _row_api_content = msg.get("api_content")
                if not isinstance(_row_api_content, str):
                    _row_api_content = None
                _row_timestamp = msg.get("timestamp")
                # Apply the persist override to THIS row's written values only
                # (never to the live dict). A multimodal override is a complete
                # clean replacement for an API-local noted payload. Preserve the
                # historical text-only guard for a list payload, though: a plain
                # text override must not erase its image/audio transcript summary.
                # The close safety-net may flush a shortened snapshot while
                # turn setup still owns its staged CLI dict. In that shape the
                # normal turn index refers to the full history, not this list;
                # preserve the API-local override by recognizing the same dict.
                pending_cli_message = getattr(self, "_pending_cli_user_message", None)
                is_current_turn_user = _ov_idx == _msg_idx or msg is pending_cli_message
                if is_current_turn_user and msg.get("role") == "user":
                    # Preflight compaction can re-anchor the override index at
                    # a message whose content was MERGED with the compaction
                    # summary (merge-summary-into-tail). Overwriting that with
                    # the clean gateway text would silently drop the summary
                    # from the durable transcript. The wire is already
                    # consistent — the merge popped the sidecar and the merged
                    # content is what gets sent — so keep it.
                    if (
                        _ov_content is not None
                        and (
                            not isinstance(content, list)
                            or isinstance(_ov_content, list)
                        )
                        and not msg.get(_compressed_summary_metadata_key_hook())
                    ):
                        # The live content is what the API call sends; the
                        # override is the cleaned transcript value. If they
                        # differ and no injection already stamped the sidecar,
                        # keep the sent bytes in api_content so replay matches
                        # the wire (#48677 divergence, closed for the cache
                        # prefix too).
                        if (
                            _row_api_content is None
                            and isinstance(content, str)
                            and content != _ov_content
                        ):
                            _row_api_content = content
                        content = _ov_content
                    if _ov_timestamp is not None:
                        _row_timestamp = _ov_timestamp
                # Store the sidecar only when it actually differs.
                if _row_api_content == content:
                    _row_api_content = None
                # Load-time sanitize divergence: get_messages_as_conversation
                # replays user/assistant rows through
                # ``_sanitize_context_hook(content).strip()``, so content that
                # sanitize would rewrite (echoed/pasted <memory-context>
                # fences or system notes) replays different bytes after a
                # session reload even though THIS turn sent it verbatim.
                # Capture the sent bytes in the sidecar so a reloaded session
                # replays what was actually on the wire. Compared in wire form
                # (both sides .strip()-ed — the api_messages build strips
                # every outgoing content string) so plain surrounding
                # whitespace doesn't grow redundant sidecars.
                if (
                    _row_api_content is None
                    and role in ("user", "assistant")
                    and isinstance(content, str)
                    and content
                    and _sanitize_context_hook(content).strip() != content.strip()
                ):
                    _row_api_content = content
                # Persist multimodal tool results as their text summary only —
                # base64 images would bloat the session DB and aren't useful
                # for cross-session replay.
                if _is_multimodal_tool_result_hook(content):
                    content = _multimodal_text_summary_hook(content)
                elif isinstance(content, list):
                    # List of OpenAI-style content parts: strip images, keep text.
                    _txt = []
                    for p in content:
                        if isinstance(p, dict) and p.get("type") == "text":
                            _txt.append(str(p.get("text", "")))
                        elif isinstance(p, dict) and p.get("type") in {
                            "image",
                            "image_url",
                            "input_image",
                        }:
                            _txt.append("[screenshot]")
                    content = "\n".join(_txt) if _txt else None
                tool_calls_data = None
                if (
                    hasattr(msg, "tool_calls")
                    and isinstance(msg.tool_calls, list)
                    and msg.tool_calls
                ):
                    tool_calls_data = [
                        {"name": tc.function.name, "arguments": tc.function.arguments}
                        for tc in msg.tool_calls
                    ]
                elif isinstance(msg.get("tool_calls"), list):
                    tool_calls_data = msg["tool_calls"]
                _batch_rows.append(
                    {
                        "role": role,
                        "content": content,
                        "tool_name": msg.get("tool_name"),
                        "tool_calls": tool_calls_data,
                        "tool_call_id": msg.get("tool_call_id"),
                        "finish_reason": msg.get("finish_reason"),
                        # Reasoning/codex fields are role-gated (assistant-only)
                        # inside _insert_message_rows — pass through untouched.
                        "reasoning": msg.get("reasoning"),
                        "reasoning_content": msg.get("reasoning_content"),
                        "reasoning_details": msg.get("reasoning_details"),
                        "codex_reasoning_items": msg.get("codex_reasoning_items"),
                        "codex_message_items": msg.get("codex_message_items"),
                        "timestamp": _row_timestamp,
                        "api_content": _row_api_content,
                        # Standalone reference handoffs are always hidden, even
                        # when the summarized transcript contained a user turn —
                        # otherwise they occupy the active user slot in
                        # retry/undo/session dispatch (#80622). Merge-into-tail
                        # carriers keep prior visibility rules so preserved tail
                        # content stays readable.
                        "display_kind": (
                            "hidden"
                            if (
                                msg.get(_compressed_summary_metadata_key_hook())
                                and (
                                    _classify_summary_content_hook(msg.get("content"))
                                    == "standalone"
                                    or not msg.get("_compressed_summary_has_user_turn")
                                )
                            )
                            else msg.get("display_kind")
                        ),
                        "display_metadata": msg.get("display_metadata"),
                    }
                )
                _batch_msgs.append(msg)
            # One transaction for the whole turn's new rows (typically 3-8
            # messages): one BEGIN IMMEDIATE / commit — and, off WAL, one
            # fsync — instead of one per row. All-or-nothing pairs exactly
            # with the marker stamping below: on failure NO rows landed and
            # NO markers were stamped, so the next flush re-scans and
            # re-writes the whole tail (same recovery contract as before,
            # minus the partial-prefix case that could double-pay counters).
            if _batch_rows:
                self._session_db.append_messages_batch(
                    session_id=self.session_id,
                    messages=_batch_rows,
                    compression_lock_holder=getattr(
                        self, "_active_compression_lock_holder", None
                    ),
                    turn_lease_holder=getattr(
                        self, "_active_session_turn_lease_holder", None
                    ),
                    turn_lease_ttl_seconds=getattr(
                        self, "_active_session_turn_lease_ttl_seconds", 300.0
                    )
                    or 300.0,
                )
                for _written in _batch_msgs:
                    _written[_db_persisted_marker_hook()] = True
            # The intrinsic markers are now the sole source of truth. Reset the
            # one-shot seed so no id() outlives this flush to alias a message
            # allocated next turn at a recycled address.
            self._flushed_db_message_ids = set()
            self._last_flushed_db_idx = len(messages)
            # Snapshot for the bounded scan above — only on full success, so
            # a partially-processed list can never be treated as settled.
            self._db_flush_scan_prefix = messages[:]
            return True
        except Exception as e:
            # Force a full re-scan on the next flush: an exception mid-loop
            # leaves messages with mixed dispositions.
            self._db_flush_scan_prefix = None
            # This is the one place the underlying SQLite error is visible
            # before it is swallowed into a bare ``False`` — classify it here
            # so the turn-end explanation can distinguish lock contention
            # ("storage was busy, send it again") from disk-full/read-only.
            from pcbdraft.services.session_db import (
                CompressionSessionClosedError,
                classify_persistence_error,
            )

            self._last_persistence_error_cause = classify_persistence_error(e)
            if isinstance(e, CompressionSessionClosedError):
                # Compression race: another path rotated this session while
                # this turn was still writing against it. The store resolves
                # the continuation chain transitively via the canonical API
                # ``get_compression_tip`` (bounded walk, excludes branch/
                # delegate/tool children, prefers live children over stale
                # closed siblings such as ``ws_orphan_reap``). Adopt the tip
                # ONLY when it is a different row AND still live, and retry
                # the flush exactly once (adoption budget) — a second
                # closed-parent write must fail closed, never loop. The tip
                # walk returns the input id when no continuation exists, so
                # ``tip == session_id`` means fail closed.
                if _adoption_budget > 0:
                    old_id = self.session_id
                    tip = None
                    try:
                        tip = self._session_db.get_compression_tip(old_id)
                    except Exception as tip_exc:
                        _warning_hook(
                            "compression tip lookup failed for %s: %s",
                            old_id,
                            tip_exc,
                        )
                    if tip and tip != old_id:
                        tip_row = None
                        try:
                            tip_row = self._session_db.get_session(tip)
                        except Exception:
                            tip_row = None
                        if tip_row is not None and tip_row.get("ended_at") is None:
                            _warning_hook(
                                "Adopted live compression tip %s for closed "
                                "session %s; retrying flush once",
                                tip,
                                old_id,
                            )
                            self.session_id = tip
                            self._flushed_db_message_ids = set()
                            self._last_flushed_db_idx = 0
                            self._compression_adoption_failed = False
                            return self._flush_messages_to_session_db_unlocked(
                                messages,
                                conversation_history,
                                _adoption_budget=0,
                            )
                # No live tip (or budget exhausted): fail closed — never guess
                # a target session. The per-turn diagnostic flag lets the
                # turn-completion explanation name compression rotation
                # instead of the historical (misleading) full-disk advice.
                self._compression_adoption_failed = True
                _warning_hook("Session DB append_message failed: %s", e)
                return False
            _warning_hook("Session DB append_message failed: %s", e)
            return False


_classify_summary_content_hook = ContextCompressor.classify_summary_content
_compressed_summary_metadata_key_hook = lambda: COMPRESSED_SUMMARY_METADATA_KEY
_db_persisted_marker_hook = lambda: _DB_PERSISTED_MARKER
_is_ephemeral_scaffolding_hook = _is_ephemeral_scaffolding
_is_multimodal_tool_result_hook = _is_multimodal_tool_result
_multimodal_text_summary_hook = _multimodal_text_summary
_sanitize_context_hook = sanitize_context
_warning_hook = logger.warning
