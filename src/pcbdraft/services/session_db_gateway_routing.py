"""Gateway peer and routing-index persistence for SessionDB.

The mixin records durable gateway peer identity, maintains the scoped routing
index, and prunes keyed sessions that never became active. The SessionDB host
retains connection, locking, transaction, schema, and session-deletion
authority and supplies late-bound runtime hooks. This module never imports
:mod:`pcbdraft.services.session_db`.
"""

# Peer lineage SQL interpolates only fixed internal clauses. Routing-entry
# decoding is best-effort while pruning malformed legacy index rows.
# ruff: noqa: BLE001, S608

from __future__ import annotations

from pathlib import Path
from typing import Any


class SessionGatewayRoutingMixin:
    """Persist gateway routing identity and maintain its durable index."""

    def record_gateway_session_peer(
        self,
        session_id: str,
        *,
        source: str,
        user_id: str | None = None,
        session_key: str | None = None,
        chat_id: str | None = None,
        chat_type: str | None = None,
        thread_id: str | None = None,
        display_name: str | None = None,
        origin_json: str | None = None,
        include_compression_ancestors: bool = False,
    ) -> None:
        """Persist the gateway routing peer for an existing session row.

        ``display_name`` / ``origin_json`` carry the gateway's presentation
        and full origin metadata (#9006) so consumers (mcp_serve, mirror,
        channel directory) can read routing data from state.db instead of
        sessions.json.  They are COALESCE'd only in the sense that ``None``
        leaves the existing value untouched.

        ``include_compression_ancestors`` keeps a logical compression lineage
        on one routing peer when an explicit gateway resume moves its tip to a
        different lane. Normal per-turn metadata refreshes update only the
        supplied row.

        Self-healing (#82616): when the target row does not exist yet — the
        gateway's ``create_session`` write failed and was deferred, or a
        crash landed between routing publication and row creation — this
        recorder INSERTs the row with the full identity instead of silently
        no-opping. Every per-turn peer refresh is therefore a repair
        opportunity: a gateway session row can no longer be first-created by
        an identity-less lazy writer (``update_token_counts`` /
        ``record_auxiliary_usage``) and stay unroutable forever.
        """
        if not session_id or not session_key:
            return

        def _do(conn):
            lineage_cte = ""
            target_clause = "WHERE id = ?"
            query_params = []
            if include_compression_ancestors:
                lineage_cte = """
                    WITH RECURSIVE compression_lineage(id) AS (
                        SELECT ?
                        UNION
                        SELECT parent.id
                        FROM compression_lineage lineage
                        JOIN sessions child ON child.id = lineage.id
                        JOIN sessions parent ON parent.id = child.parent_session_id
                        WHERE parent.end_reason = 'compression'
                          AND json_extract(
                              COALESCE(child.model_config, '{}'),
                              '$._branched_from'
                          ) IS NULL
                          AND json_extract(
                              COALESCE(child.model_config, '{}'),
                              '$._delegate_from'
                          ) IS NULL
                          AND COALESCE(child.source, '') != 'tool'
                    )
                """
                target_clause = "WHERE id IN (SELECT id FROM compression_lineage)"
                query_params.append(session_id)
            query_params.extend(
                (
                    session_key,
                    source,
                    user_id,
                    chat_id,
                    chat_type,
                    thread_id,
                    display_name,
                    origin_json,
                )
            )
            if not include_compression_ancestors:
                query_params.append(session_id)
            conn.execute(
                f"""{lineage_cte}
                   UPDATE sessions
                   SET session_key = ?, source = ?, user_id = ?, chat_id = ?,
                       chat_type = ?, thread_id = ?,
                       display_name = COALESCE(?, display_name),
                       origin_json = COALESCE(?, origin_json)
                   {target_clause}""",
                query_params,
            )
            # Self-heal (#82616): the UPDATE is a silent no-op when the row
            # is missing (create_session failed earlier, or a crash landed
            # between routing publication and row creation). Insert it with
            # the full identity so the session is durably routable — never
            # leave first-creation to an identity-less lazy writer.
            if not include_compression_ancestors:
                cur = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
                )
                if cur.fetchone() is None:
                    conn.execute(
                        """INSERT INTO sessions (
                               id, source, user_id, session_key, chat_id,
                               chat_type, thread_id, display_name, origin_json,
                               started_at
                           )
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET
                               session_key = COALESCE(sessions.session_key, excluded.session_key),
                               chat_id = COALESCE(sessions.chat_id, excluded.chat_id),
                               chat_type = COALESCE(sessions.chat_type, excluded.chat_type),
                               thread_id = COALESCE(sessions.thread_id, excluded.thread_id),
                               display_name = COALESCE(sessions.display_name, excluded.display_name),
                               origin_json = COALESCE(sessions.origin_json, excluded.origin_json)""",
                        (
                            session_id,
                            source,
                            user_id,
                            session_key,
                            chat_id,
                            chat_type,
                            thread_id,
                            display_name,
                            origin_json,
                            self._gateway_routing_now(),
                        ),
                    )

        self._execute_write(_do)

    def set_expiry_finalized(self, session_id: str, finalized: bool = True) -> None:
        """Mark a gateway session's expiry-finalization flag in state.db.

        Mirrors ``SessionEntry.expiry_finalized`` (sessions.json) so the flag
        survives even if the JSON index is pruned or lost (#9006).
        """
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET expiry_finalized = ? WHERE id = ?",
                (1 if finalized else 0, session_id),
            )

        self._execute_write(_do)

    # ── Gateway routing index (replaces sessions.json, #9006 follow-up) ────

    def save_gateway_routing_entry(
        self, session_key: str, entry_json: str, *, scope: str = ""
    ) -> None:
        """Upsert one gateway routing entry (session_key -> SessionEntry JSON).

        The gateway_routing table is the durable replacement for
        sessions.json: one row per routing key, holding the full serialized
        ``SessionEntry`` so the gateway can rehydrate exactly what it wrote.

        ``scope`` namespaces the index the way separate sessions.json files
        did (one per sessions_dir) — callers pass their sessions_dir path so
        two stores with different directories never share routing state.
        """
        if not session_key or not entry_json:
            return

        def _do(conn):
            conn.execute(
                """INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope, session_key) DO UPDATE SET
                       entry_json = excluded.entry_json,
                       updated_at = excluded.updated_at""",
                (scope, session_key, entry_json, self._gateway_routing_now()),
            )

        self._execute_write(_do)

    def replace_gateway_routing_entries(
        self, entries: dict[str, str], *, scope: str = ""
    ) -> None:
        """Atomically replace the routing index for *scope* with *entries*.

        Mirrors the sessions.json full-rewrite semantics: keys absent from
        *entries* are removed (pruned/reset sessions disappear from the
        index).  Runs as a single write transaction.  Other scopes are
        untouched.
        """
        now = self._gateway_routing_now()

        def _do(conn):
            conn.execute("DELETE FROM gateway_routing WHERE scope = ?", (scope,))
            if entries:
                conn.executemany(
                    "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    [(scope, k, v, now) for k, v in entries.items() if k and v],
                )

        self._execute_write(_do)

    def load_gateway_routing_entries(self, *, scope: str = "") -> dict[str, str]:
        """Load routing entries for *scope* as {session_key: entry_json}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_key, entry_json FROM gateway_routing WHERE scope = ?",
                (scope,),
            ).fetchall()
        return {r["session_key"]: r["entry_json"] for r in rows}

    def delete_gateway_routing_entries(
        self, session_keys: list[str], *, scope: str = ""
    ) -> None:
        """Remove routing entries for the given session keys in *scope*."""
        if not session_keys:
            return

        def _do(conn):
            conn.executemany(
                "DELETE FROM gateway_routing WHERE scope = ? AND session_key = ?",
                [(scope, k) for k in session_keys],
            )

        self._execute_write(_do)

    def list_never_active_keyed_sessions(
        self, *, older_than_days: float
    ) -> list[dict[str, Any]]:
        """Keyed gateway rows that were opened and then never used at all.

        Selects rows that are keyed (``session_key IS NOT NULL``), still open
        (``ended_at IS NULL``) and carry no evidence of a single turn: no
        messages, no tokens, no tool or API calls, no recorded activity, no
        title.  Such a row is indistinguishable from "never happened".

        That is exactly the shape of a leaked test fixture (#82770) — and
        also of a chat that was routed but never answered.  Both are safe to
        drop: there is no transcript to lose, and the gateway mints a fresh
        session on the next inbound message either way.

        ``bulk prune``/``archive`` cannot reach these rows: their shared
        selector is pinned to ``ended_at IS NOT NULL`` so that a live session
        is never picked, which permanently excludes every never-closed row.
        Hence a separate, narrower selector rather than another filter flag.

        ``pinned`` and ``archived`` rows are excluded — both are explicit
        user intent to keep the row around.
        """
        cutoff = self._gateway_routing_now() - (float(older_than_days) * 86400.0)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.id, s.session_key, s.source, s.chat_id,
                       s.chat_type, s.user_id, s.started_at
                  FROM sessions s
                 WHERE s.session_key IS NOT NULL
                   AND s.ended_at IS NULL
                   AND s.title IS NULL
                   AND s.last_activity_at IS NULL
                   AND COALESCE(s.message_count, 0) = 0
                   AND COALESCE(s.tool_call_count, 0) = 0
                   AND COALESCE(s.api_call_count, 0) = 0
                   AND COALESCE(s.input_tokens, 0) = 0
                   AND COALESCE(s.output_tokens, 0) = 0
                   AND COALESCE(s.pinned, 0) = 0
                   AND COALESCE(s.archived, 0) = 0
                   AND s.started_at IS NOT NULL
                   AND s.started_at < ?
                   AND NOT EXISTS (
                           SELECT 1 FROM messages m WHERE m.session_id = s.id
                       )
                 ORDER BY s.started_at
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _delete_routing_entries_for_sessions(self, session_ids: set[str]) -> int:
        """Drop ``gateway_routing`` rows pointing at any of *session_ids*.

        Routing entries are keyed by ``(scope, session_key)`` and record their
        target session inside ``entry_json``, so there is no way to reach them
        by session id in SQL — the match is done in Python over all scopes.
        """
        if not session_ids:
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT scope, session_key, entry_json FROM gateway_routing"
            ).fetchall()
        doomed: list[tuple[str, str]] = []
        for row in rows:
            try:
                entry = self._gateway_routing_json_loads(row["entry_json"] or "{}")
            except Exception:
                self._gateway_routing_log_debug(
                    "Gateway routing entry decode failed",
                    exc_info=self._gateway_routing_exception_info(),
                )
                continue
            if isinstance(entry, dict) and entry.get("session_id") in session_ids:
                doomed.append((row["scope"], row["session_key"]))
        if not doomed:
            return 0

        def _do(conn):
            conn.executemany(
                "DELETE FROM gateway_routing WHERE scope = ? AND session_key = ?",
                doomed,
            )

        self._execute_write(_do)
        return len(doomed)

    def prune_never_active_keyed_sessions(
        self,
        *,
        older_than_days: float,
        sessions_dir: Path | None = None,
    ) -> tuple[int, int]:
        """Delete never-active keyed rows and the routing entries naming them.

        Returns ``(sessions_deleted, routing_entries_deleted)``.

        The routing entries go first: a stale entry that outlived its target
        would leave the gateway resuming a session id that no longer exists.
        Deleting the pair is what leaving them both would have amounted to
        anyway — the target had no transcript to resume.

        Deletion goes through :meth:`delete_session` rather than a bulk
        ``DELETE`` so the delegate cascade, FTS bookkeeping and on-disk
        transcript cleanup stay owned by one implementation.
        """
        candidates = self.list_never_active_keyed_sessions(
            older_than_days=older_than_days
        )
        if not candidates:
            return (0, 0)
        ids = {str(row["id"]) for row in candidates}
        routing_deleted = self._delete_routing_entries_for_sessions(ids)
        deleted = 0
        for session_id in ids:
            if self.delete_session(session_id, sessions_dir=sessions_dir):
                deleted += 1
        return (deleted, routing_deleted)
