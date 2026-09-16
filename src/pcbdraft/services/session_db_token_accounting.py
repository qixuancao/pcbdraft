"""Asynchronous token accounting behavior for :class:`SessionDB`.

Mixin contract: this plain mixin is consumed by
``pcbdraft.services.session_db.SessionDB``. It defines no ``__init__`` and owns
no connection state. The host supplies the token queue state, ``_execute_write``,
``_insert_session_row``, and the SQLite connection lifecycle. This module must
never import ``session_db`` so the store remains the composition root.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
import weakref
from typing import Any

logger = logging.getLogger("pcbdraft.services.session_db")


class SessionTokenAccountingMixin:
    """Queue, flush, and persist per-session token and model usage."""

    # Demand-started accounting workers retire after an idle window so their
    # bound targets do not keep abandoned SessionDB instances (and SQLite
    # descriptors) alive forever. A later enqueue starts a fresh worker.
    _TOKEN_WRITER_IDLE_SECONDS = 30.0

    # ── Async token accounting ──
    # update_token_counts() runs a sessions UPDATE (plus a per-model usage
    # upsert) inside BEGIN IMMEDIATE; against a cold multi-GB state.db one
    # call can stall the turn thread for tens to hundreds of ms, and the
    # tool loop pays it after EVERY API call (measured p50 3.3ms / p95 70ms
    # per call in production). queue_token_counts() reduces the critical
    # path to a deque append: a dedicated single-writer thread applies
    # deltas in enqueue order, coalescing consecutive same-route deltas
    # into one UPDATE when a backlog forms. Readers that need exact
    # mid-turn totals (get_session and friends) call flush_token_counts()
    # first — a plain attribute check when nothing is queued.

    # Delta fields summed when coalescing. Route fields must be equal for
    # two deltas to merge: model/billing_* feed COALESCE backfill and the
    # per-model usage attribution key, and cost_status/cost_source are
    # last-non-None-wins — equality makes the merged UPDATE byte-for-byte
    # equivalent to applying the deltas sequentially.
    _TOKEN_DELTA_SUM_FIELDS = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "api_call_count",
    )
    _TOKEN_DELTA_COST_FIELDS = ("estimated_cost_usd", "actual_cost_usd")
    _TOKEN_DELTA_ROUTE_FIELDS = (
        "model",
        "cost_status",
        "cost_source",
        "pricing_version",
        "billing_provider",
        "billing_base_url",
        "billing_mode",
    )

    def queue_token_counts(self, session_id: str, **kwargs) -> None:
        """Enqueue a token/cost delta for the background writer.

        Accepts the same keyword arguments as :meth:`update_token_counts`
        and applies them asynchronously with identical semantics.  Cheap
        (append + notify) — safe to call on the turn thread after every
        API call.  After close() has stopped the writer, falls back to the
        synchronous path and may raise like :meth:`update_token_counts`.
        """
        with self._token_queue_cond:
            thread = self._token_writer_thread
            writer_stopped = self._token_writer_stop and (
                thread is None or not thread.is_alive()
            )
            if not writer_stopped:
                self._token_queue.append((session_id, kwargs))
                if thread is None or not thread.is_alive():
                    # Daemon so process exit never hangs on accounting; the
                    # atexit hook drains anything still queued at interpreter
                    # shutdown (registered once per instance, on first use).
                    # ``not is_alive()`` (rather than ``is None`` only)
                    # respawns the writer if it ever died from an unexpected
                    # escape — otherwise a dead thread object would block
                    # respawn forever and deltas would pile up on the deque
                    # until a reader's flush drained them synchronously.
                    thread = threading.Thread(
                        target=self._token_writer_loop,
                        name="pcbdraft-session-db-token-writer",
                        daemon=True,
                    )
                    self._token_writer_thread = thread
                    thread.start()
                    if self._token_atexit_hook is None:
                        self_ref = weakref.ref(self)

                        def _drain_at_exit() -> None:
                            db = self_ref()
                            if db is not None:
                                db._drain_token_queue_at_exit()

                        self._token_atexit_hook = _drain_at_exit
                        atexit.register(_drain_at_exit)
                self._token_queue_cond.notify_all()
        if writer_stopped:
            # Writer permanently stopped (close() ran; a stop-flagged but
            # still-live writer keeps accepting — its loop drains before
            # exiting). Enqueueing now would drop the delta silently: no
            # writer will run and close() already unregistered the atexit
            # hook. Apply inline instead so a closed-connection failure
            # raises at the call site, exactly like the old synchronous
            # update_token_counts path these call sites still guard for.
            self.update_token_counts(session_id, **kwargs)

    def flush_token_counts(self, timeout: float = 5.0) -> bool:
        """Block until every queued token delta has been applied.

        Returns True when the queue is fully drained, False on timeout
        (callers then read totals that are stale by the still-queued
        deltas — no worse than reading before the flush existed).
        Never raises: apply failures are logged by the writer.
        """
        # Fast path — nothing queued, nothing in flight.
        if not self._token_queue and not self._token_writer_busy:
            return True
        batch = None
        with self._token_queue_cond:
            deadline = time.monotonic() + timeout
            while self._token_queue or self._token_writer_busy:
                # A live writer is authoritative even when stop-flagged
                # (close() in progress): its loop drains the queue before
                # exiting, and draining here instead would race its
                # in-flight batch — newer deltas committing before older
                # ones breaks the last-non-None-wins / first-accounted-
                # route / COALESCE-backfill fields. Only when the writer is
                # dead (or never started for these deltas) does the caller
                # take the leftovers. Re-checked each wakeup: the writer
                # can exit mid-wait with deltas enqueued after its final
                # empty-queue check. busy is claimed while draining (same
                # protocol as the writer) so a concurrent flush cannot
                # report drained — or pop a newer delta — while this batch
                # is still unapplied; a claimed busy therefore also means
                # "wait", never "drain alongside".
                thread = self._token_writer_thread
                if (
                    thread is None or not thread.is_alive()
                ) and not self._token_writer_busy:
                    self._token_writer_busy = True
                    batch = list(self._token_queue)
                    self._token_queue.clear()
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._token_queue_cond.wait(remaining)
        if batch:
            try:
                self._apply_token_batch(batch)
            finally:
                with self._token_queue_cond:
                    self._token_writer_busy = False
                    self._token_queue_cond.notify_all()
        return True

    def _token_writer_loop(self) -> None:
        while True:
            with self._token_queue_cond:
                idle_deadline = time.monotonic() + self._TOKEN_WRITER_IDLE_SECONDS
                while not self._token_queue and not self._token_writer_stop:
                    remaining = idle_deadline - time.monotonic()
                    if remaining <= 0:
                        # Publish retirement under the same lock used by
                        # queue_token_counts() to decide whether to spawn. An
                        # enqueue cannot strand a delta behind an exiting worker.
                        self._token_writer_thread = None
                        return
                    self._token_queue_cond.wait(remaining)
                if not self._token_queue:
                    self._token_writer_thread = None
                    return  # stop requested and fully drained
                # busy is set BEFORE the queue is cleared: the lock-free
                # fast path in flush_token_counts() reads queue-then-busy,
                # so this order guarantees it can never observe an empty
                # queue while the popped batch is still unapplied.
                self._token_writer_busy = True
                batch = list(self._token_queue)
                self._token_queue.clear()
            try:
                self._apply_token_batch(batch)
            finally:
                with self._token_queue_cond:
                    self._token_writer_busy = False
                    self._token_queue_cond.notify_all()

    def _apply_token_batch(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        """Apply queued deltas in order, coalescing where safe. Never raises."""
        try:
            coalesced = self._coalesce_token_deltas(batch)
        except Exception:
            # Coalescing must never kill the writer thread (a dead writer
            # can't be observed by callers). Fall back to applying the raw
            # batch delta-by-delta — the merge is an optimization only.
            logger.warning(
                "async token accounting: coalesce failed, applying raw batch",
                exc_info=True,
            )
            coalesced = batch
        for session_id, kwargs in coalesced:
            try:
                self.update_token_counts(session_id, **kwargs)
            except Exception:
                # Same contract as the old inline call sites: accounting
                # loss is logged, never raised into a turn.
                logger.warning(
                    "async token accounting: apply failed",
                    exc_info=True,
                )

    def _coalesce_token_deltas(
        self, batch: list[tuple[str, dict[str, Any]]]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Merge consecutive incremental deltas with an identical route.

        Only adjacent deltas merge, so ordering across sessions and across
        a mid-session /model switch is preserved exactly.  absolute=True
        deltas (cumulative overwrites) never merge.
        """
        groups: list[tuple[tuple | None, str, dict[str, Any]]] = []
        for session_id, kwargs in batch:
            key = None
            if not kwargs.get("absolute"):
                key = (session_id,) + tuple(
                    kwargs.get(f) for f in self._TOKEN_DELTA_ROUTE_FIELDS
                )
            if groups and key is not None and groups[-1][0] == key:
                merged = groups[-1][2]
                for f in self._TOKEN_DELTA_SUM_FIELDS:
                    merged[f] = merged.get(f, 0) + kwargs.get(f, 0)
                for f in self._TOKEN_DELTA_COST_FIELDS:
                    value = kwargs.get(f)
                    if value is not None:
                        # None-preserving sum: an all-None run must stay
                        # None so COALESCE keeps the stored value untouched.
                        merged[f] = (merged.get(f) or 0.0) + value
            else:
                groups.append((key, session_id, dict(kwargs)))
        return [(sid, kw) for _, sid, kw in groups]

    def _stop_token_writer(self, join_timeout: float = 10.0) -> None:
        """Stop the writer thread and drain remaining deltas. Never raises."""
        with self._token_queue_cond:
            self._token_writer_stop = True
            self._token_queue_cond.notify_all()
            thread = self._token_writer_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                # Writer stuck mid-apply (pathological lock contention).
                # Leave any queued deltas unapplied rather than racing the
                # stuck apply and misordering/double-counting.
                logger.warning(
                    "async token accounting: writer did not stop within %.0fs; "
                    "%d queued delta(s) not persisted",
                    join_timeout,
                    len(self._token_queue),
                )
                return
        # Writer exited (or never started) — apply leftovers synchronously.
        # Claim busy like the writer/flush drains do, so a concurrent
        # flush_token_counts cannot fast-path True while this batch is
        # still being applied; conversely, wait out a flush caller-drain
        # that already claimed busy — close() nulls the connection right
        # after this returns, and must not yank it mid-batch.
        with self._token_queue_cond:
            deadline = time.monotonic() + join_timeout
            while self._token_writer_busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "async token accounting: concurrent drain did not "
                        "finish within %.0fs; %d queued delta(s) not persisted",
                        join_timeout,
                        len(self._token_queue),
                    )
                    return
                self._token_queue_cond.wait(remaining)
            # busy is claimed BEFORE the queue is cleared — same ordering
            # as the writer loop and the flush caller-drain. The lock-free
            # fast path in flush_token_counts() reads queue-then-busy
            # without the cond, so clearing first would let a concurrent
            # flush observe "empty and idle" and return True while this
            # popped batch is still unapplied.
            batch = list(self._token_queue)
            if batch:
                self._token_writer_busy = True
                self._token_queue.clear()
        if batch:
            try:
                self._apply_token_batch(batch)
            finally:
                with self._token_queue_cond:
                    self._token_writer_busy = False
                    self._token_queue_cond.notify_all()

    def _drain_token_queue_at_exit(self) -> None:
        try:
            self._stop_token_writer()
        except Exception:
            logger.debug("Token writer shutdown failed", exc_info=True)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: float | None = None,
        actual_cost_usd: float | None = None,
        cost_status: str | None = None,
        cost_source: str | None = None,
        pricing_version: str | None = None,
        billing_provider: str | None = None,
        billing_base_url: str | None = None,
        billing_mode: str | None = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** — use
        this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this when
        the caller already holds cumulative totals (gateway path, where the
        cached agent accumulates across messages).
        """
        # Ensure the session row exists so the UPDATE doesn't silently affect
        # 0 rows.  Under concurrent load (cron + kanban + delegate_task) the
        # initial create_session() may have failed due to SQLite locking.
        # INSERT OR IGNORE is cheap and idempotent.
        self._insert_session_row(session_id, "unknown", model=model)
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = ?
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = COALESCE(api_call_count, 0) + ?
                   WHERE id = ?"""
        has_accounted_usage = bool(
            input_tokens
            or output_tokens
            or cache_read_tokens
            or cache_write_tokens
            or reasoning_tokens
            or api_call_count
            or estimated_cost_usd
            or actual_cost_usd
        )
        params = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            estimated_cost_usd,
            actual_cost_usd,
            actual_cost_usd,
            cost_status,
            cost_source,
            pricing_version,
            billing_provider if has_accounted_usage else None,
            billing_base_url if has_accounted_usage else None,
            billing_mode if has_accounted_usage else None,
            model if has_accounted_usage else None,
            api_call_count,
            session_id,
        )
        # Per-model usage attribution.  ``update_token_counts`` is the single
        # chokepoint every per-API-call delta flows through (CLI, gateway, cron,
        # delegated runs — see conversation_loop / codex_runtime), and each call
        # carries the model/provider *active at the time of that call*.  The
        # ``sessions`` row only keeps one (model, billing_provider) pair, so a
        # mid-session ``/model`` switch otherwise attributes every token to the
        # initial model (issue #51607).  Recording the per-call delta into
        # session_model_usage keyed by the live model preserves an accurate
        # per-model breakdown regardless of how many times the user switches.
        #
        # Only the incremental path records here. Absolute cumulative updates
        # cannot be split back into routes; Insights reconciles any positive
        # residual against the aggregate session row instead.
        record_model_usage = (not absolute) and (
            input_tokens
            or output_tokens
            or cache_read_tokens
            or cache_write_tokens
            or reasoning_tokens
            or api_call_count
            or estimated_cost_usd
        )

        def _do(conn):
            row = conn.execute(
                "SELECT model, billing_provider, api_call_count FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            existing_model = row["model"] if row is not None else None
            existing_provider = row["billing_provider"] if row is not None else None
            existing_api_calls = int(
                (row["api_call_count"] if row is not None else 0) or 0
            )

            # Session creation records the requested primary route before any API
            # call. If it fails and fallback succeeds, the first accounted usage
            # event is the first authoritative route. After that, preserve the
            # legacy row: one row cannot represent mixed-provider usage.
            first_accounted_route = (
                existing_api_calls == 0
                and has_accounted_usage
                and bool(model)
                and bool(billing_provider)
                and (existing_model != model or existing_provider != billing_provider)
            )
            if first_accounted_route:
                conn.execute(
                    """UPDATE sessions
                       SET model = ?, billing_provider = ?,
                       billing_base_url = ?, billing_mode = ?
                       WHERE id = ?""",
                    (
                        model,
                        billing_provider,
                        billing_base_url,
                        billing_mode,
                        session_id,
                    ),
                )
            conn.execute(sql, params)
            if record_model_usage:
                self._record_model_usage(
                    conn,
                    session_id,
                    model=model,
                    billing_provider=billing_provider,
                    billing_base_url=billing_base_url,
                    billing_mode=billing_mode,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    reasoning_tokens=reasoning_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    actual_cost_usd=actual_cost_usd,
                    cost_status=cost_status,
                    cost_source=cost_source,
                    api_call_count=api_call_count,
                )

        self._execute_write(_do)

    def _record_model_usage(
        self,
        conn,
        session_id: str,
        *,
        model: str | None,
        billing_provider: str | None,
        billing_base_url: str | None,
        billing_mode: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        reasoning_tokens: int,
        estimated_cost_usd: float | None,
        actual_cost_usd: float | None,
        cost_status: str | None,
        cost_source: str | None,
        api_call_count: int,
        task: str = "",
    ) -> None:
        """Accumulate a per-API-call usage delta into session_model_usage.

        Runs inside the caller's write transaction (after the ``sessions``
        UPDATE) so the per-model rows stay consistent with the summary row.
        When the caller omits the model/provider (some paths only pass token
        deltas), fall back to the values already recorded on the session row —
        the same COALESCE-from-session behaviour the summary update uses.

        ``task`` distinguishes what kind of work consumed the tokens:
        ``''`` (empty) is the main agent loop; auxiliary calls record their
        task name (``vision``, ``compression``, ``title_generation``, ...)
        via :meth:`record_auxiliary_usage` (issue #23270).
        """
        row = conn.execute(
            "SELECT model, billing_provider, billing_base_url, billing_mode "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        sess_model = row["model"] if row is not None else None
        sess_provider = row["billing_provider"] if row is not None else None
        sess_base_url = row["billing_base_url"] if row is not None else None
        sess_billing_mode = row["billing_mode"] if row is not None else None

        # Aux-task rows (task != '') must NOT inherit the session's main-loop
        # route: an aux call may use a completely different provider/model
        # (vision on gemini while the main loop runs anthropic). Missing info
        # stays 'unknown'/empty rather than borrowing a misleading route.
        if task:
            eff_model = model or "unknown"
            eff_provider = billing_provider or ""
            eff_base_url = billing_base_url or ""
            eff_billing_mode = billing_mode or ""
        else:
            eff_model = model or sess_model or "unknown"
            eff_provider = billing_provider or sess_provider or ""
            eff_base_url = billing_base_url or sess_base_url or ""
            eff_billing_mode = billing_mode or sess_billing_mode or ""
        now = time.time()
        conn.execute(
            """INSERT INTO session_model_usage (
                   session_id, model, billing_provider, billing_base_url, billing_mode,
                   task, api_call_count, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, reasoning_tokens,
                   estimated_cost_usd, actual_cost_usd, cost_status, cost_source,
                   first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, model, billing_provider, billing_base_url, billing_mode, task)
               DO UPDATE SET
                   api_call_count = api_call_count + excluded.api_call_count,
                   input_tokens = input_tokens + excluded.input_tokens,
                   output_tokens = output_tokens + excluded.output_tokens,
                   cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                   cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
                   reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                   estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                   actual_cost_usd = actual_cost_usd + excluded.actual_cost_usd,
                   cost_status = COALESCE(excluded.cost_status, cost_status),
                   cost_source = COALESCE(excluded.cost_source, cost_source),
                   last_seen = excluded.last_seen""",
            (
                session_id,
                eff_model,
                eff_provider,
                eff_base_url,
                eff_billing_mode,
                task or "",
                api_call_count or 0,
                input_tokens or 0,
                output_tokens or 0,
                cache_read_tokens or 0,
                cache_write_tokens or 0,
                reasoning_tokens or 0,
                float(estimated_cost_usd or 0.0),
                float(actual_cost_usd or 0.0),
                cost_status,
                cost_source,
                now,
                now,
            ),
        )
