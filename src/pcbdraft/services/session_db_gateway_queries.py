"""Gateway peer query APIs for SessionDB.

The mixin projects durable session rows for gateway status, origin lookup,
peer recovery, and orphan-repair previews. Routing writes, routing-index
authority, lease ownership, and orphan adoption remain on the SessionDB host.
The list projection retains its historical host accounting flush before the
read, but never mutates routing identity. This module never imports
:mod:`pcbdraft.services.session_db`.
"""

# Query strings interpolate only fixed clauses and trusted internal SQL helpers.
# ruff: noqa: S608

from __future__ import annotations

from typing import Any


class SessionGatewayQueryMixin:
    """List and resolve gateway session peers without mutating routing identity."""

    # Widest plausible gap between a keyed predecessor going quiet and its
    # unkeyed successor being minted. The reported incident gap was ~60s;
    # 15 minutes stays generous without spanning unrelated conversations.
    _ORPHAN_ADOPTION_MAX_GAP_S = 900.0

    def list_gateway_sessions(
        self,
        *,
        platform: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List gateway sessions (rows with a session_key) from state.db.

        Returns the newest row per session_key — the same shape consumers got
        from sessions.json: one live mapping per routing key.  ``platform``
        filters on ``source``; ``active_only`` restricts to sessions that
        have not ended.
        """
        # Full rows carry token/cost totals (MCP listings, /status) — drain
        # queued async accounting deltas so consumers see exact counters.
        self.flush_token_counts()
        query = f"""
            SELECT sessions.*,
                   COALESCE(sp.prompt, sessions.system_prompt)
                       AS _system_prompt_resolved,
                   {self._gateway_queries_last_active_sql("sessions")} AS last_active
            FROM sessions
            LEFT JOIN system_prompts sp
              ON sp.hash = sessions.system_prompt_hash
            WHERE session_key IS NOT NULL
              AND started_at = (
                  SELECT MAX(s2.started_at) FROM sessions s2
                  WHERE s2.session_key = sessions.session_key
              )
        """
        params: list = []
        if platform:
            query += " AND LOWER(source) = LOWER(?)"
            params.append(platform)
        if active_only:
            query += " AND ended_at IS NULL"
        query += " ORDER BY last_active DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._session_row_dict(r) for r in rows]

    def find_session_by_origin(
        self,
        *,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> str | None:
        """Find the most recent live session_id for a platform + chat origin.

        Equivalent of gateway/mirror's sessions.json scan: matches on
        source + chat_id (+ thread_id when provided).  When ``user_id`` is
        provided, exact sender matches are preferred; if multiple distinct
        users share the chat and none matches, returns None rather than
        contaminating another participant's session.
        """
        if not platform or chat_id in (None, ""):
            return None
        query = """
            SELECT id, user_id, started_at FROM sessions
            WHERE LOWER(source) = LOWER(?)
              AND session_key IS NOT NULL
              AND chat_id = ?
              AND ended_at IS NULL
        """
        params: list = [platform, str(chat_id)]
        if thread_id is not None:
            query += " AND COALESCE(thread_id, '') = ?"
            params.append(str(thread_id))
        query += " ORDER BY started_at DESC"
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(query, params).fetchall()]
        if not rows:
            return None
        if user_id:
            exact = [r for r in rows if str(r.get("user_id") or "") == str(user_id)]
            if exact:
                return str(exact[0]["id"])
            if len(rows) > 1:
                return None
        elif len(rows) > 1:
            distinct_users = {
                str(r.get("user_id") or "").strip()
                for r in rows
                if str(r.get("user_id") or "").strip()
            }
            if len(distinct_users) > 1:
                return None
        return str(rows[0]["id"])

    def find_latest_gateway_session_for_peer(
        self,
        *,
        source: str,
        user_id: str | None = None,
        session_key: str | None = None,
        chat_id: str | None = None,
        chat_type: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the latest recoverable gateway session for a routing peer.

        ``sessions.json`` is the fast routing index, but it can be missing or
        pruned after process-level restart bugs.  New gateway sessions persist
        the deterministic ``session_key`` on the durable session row so the
        mapping can be rebuilt exactly.  Rows ended only by older gateway
        cleanup's ``agent_close`` bug or a mistaken TUI ``ws_orphan_reap``
        (dashboard viewer disconnect before #60609) are treated as recoverable;
        explicit conversation boundaries such as /new, /resume switches, and
        compression splits are not.

        Ordering and emptiness (#82616): candidates are ranked by actual
        conversation recency (``last_activity_at``, falling back to
        ``started_at``) — ``started_at`` alone resurrected days-old zombie
        rows over the live conversation. Rows with messages are preferred,
        but an empty keyed row is still returned rather than ``None``:
        returning ``None`` mints a brand-new session id, which is a worse
        outcome than resuming an empty-but-correctly-keyed row (and "empty"
        may just mean the transcript lives under a compression child).

        Reset boundaries fence recovery (#68539): an intentional boundary
        such as ``session_reset`` (or any explicit non-recoverable
        end_reason) must block fallback to an *older* row for the same
        peer. Without the fence, the has-messages ranking above could reach
        behind a /new reset and silently restore the exact context the user
        reset. Each candidate is therefore rejected when a boundary row for
        the peer ended *after* the candidate's last activity — if the
        conversation's most recent event is an intentional reset, recovery
        returns nothing rather than reaching behind it.
        """
        if not session_key:
            return None
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT s.*,
                       COALESCE(sp.prompt, s.system_prompt)
                           AS _system_prompt_resolved,
                       (COALESCE(s.message_count, 0) > 0 OR EXISTS (
                           SELECT 1 FROM messages WHERE messages.session_id = s.id LIMIT 1
                       )) AS _has_messages
                FROM sessions s
                LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash
                WHERE s.session_key = ?
                  AND s.source = ?
                  AND (s.ended_at IS NULL OR s.end_reason IN ('agent_close', 'ws_orphan_reap'))
                  AND NOT EXISTS (
                      SELECT 1 FROM sessions b
                      WHERE b.session_key = s.session_key
                        AND b.source = s.source
                        AND b.ended_at IS NOT NULL
                        AND b.end_reason IN ({self._gateway_queries_reset_end_reasons_sql()})
                        AND b.ended_at
                            > COALESCE(s.last_activity_at, s.started_at)
                  )
                ORDER BY _has_messages DESC,
                         COALESCE(s.last_activity_at, s.started_at) DESC
                LIMIT 1
                """,
                (session_key, source),
            ).fetchone()
            if row is not None:
                return self._session_row_dict(row)

            # Conservative fallback for rows created by current code but with a
            # temporarily-missing exact key: still require the complete peer
            # tuple so we never cross chats/threads/users.
            if chat_id is None or chat_type is None:
                return None
            row = self._conn.execute(
                f"""
                SELECT s.*,
                       COALESCE(sp.prompt, s.system_prompt)
                           AS _system_prompt_resolved,
                       (COALESCE(s.message_count, 0) > 0 OR EXISTS (
                           SELECT 1 FROM messages WHERE messages.session_id = s.id LIMIT 1
                       )) AS _has_messages
                FROM sessions s
                LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash
                WHERE s.source = ?
                  AND COALESCE(s.user_id, '') = COALESCE(?, '')
                  AND COALESCE(s.chat_id, '') = COALESCE(?, '')
                  AND COALESCE(s.chat_type, '') = COALESCE(?, '')
                  AND COALESCE(s.thread_id, '') = COALESCE(?, '')
                  AND (s.ended_at IS NULL OR s.end_reason IN ('agent_close', 'ws_orphan_reap'))
                  AND (COALESCE(s.message_count, 0) > 0 OR EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = s.id LIMIT 1
                  ))
                  AND NOT EXISTS (
                      SELECT 1 FROM sessions b
                      WHERE b.source = s.source
                        AND COALESCE(b.user_id, '') = COALESCE(s.user_id, '')
                        AND COALESCE(b.chat_id, '') = COALESCE(s.chat_id, '')
                        AND COALESCE(b.chat_type, '') = COALESCE(s.chat_type, '')
                        AND COALESCE(b.thread_id, '') = COALESCE(s.thread_id, '')
                        AND b.ended_at IS NOT NULL
                        AND b.end_reason IN ({self._gateway_queries_reset_end_reasons_sql()})
                        AND b.ended_at
                            > COALESCE(s.last_activity_at, s.started_at)
                  )
                ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC
                LIMIT 1
                """,
                (source, user_id, chat_id, chat_type, thread_id),
            ).fetchone()
        return self._session_row_dict(row) if row else None

    def find_orphaned_gateway_sessions(
        self, *, max_gap_s: float | None = None
    ) -> list[dict[str, Any]]:
        """Report message-bearing session rows that lost their routing identity.

        A row is a candidate orphan when it has messages but no
        ``session_key``. It is only *adoptable* when exactly one keyed
        predecessor can be named as the conversation it continues:

        * ``lineage`` — ``parent_session_id`` points at a keyed row of the
          same source. That is a recorded fact, so no time window applies.
        * ``contiguity`` — exactly one keyed row of the same source (and
          compatible ``user_id``) fell quiet within *max_gap_s* of the
          orphan's start, and is older than the orphan's own last activity.

        Anything ambiguous is reported with ``adoptable=False`` and a reason
        rather than guessed at: mis-adopting would splice one person's
        conversation into another person's chat. Branch/delegate/tool rows
        are excluded outright — they are unkeyed by design, not by damage.
        """
        gap = self._ORPHAN_ADOPTION_MAX_GAP_S if max_gap_s is None else float(max_gap_s)
        orphan_active = self._gateway_queries_last_active_sql("o")
        donor_active = self._gateway_queries_last_active_sql("d")
        donor_columns = (
            "d.id, d.session_key, d.chat_id, d.chat_type, d.thread_id, "
            "d.user_id, d.origin_json, d.display_name, d.end_reason"
        )
        records: list[dict[str, Any]] = []

        with self._lock:
            orphans = self._conn.execute(
                f"""
                SELECT o.id, o.source, o.user_id, o.started_at,
                       o.parent_session_id,
                       {orphan_active} AS last_active,
                       (SELECT COUNT(*) FROM messages m
                         WHERE m.session_id = o.id) AS message_count
                FROM sessions o
                WHERE o.session_key IS NULL
                  AND EXISTS (SELECT 1 FROM messages m
                               WHERE m.session_id = o.id)
                  AND COALESCE(o.source, '') != 'tool'
                  AND json_extract(COALESCE(o.model_config, '{{}}'),
                                   '$._branched_from') IS NULL
                  AND json_extract(COALESCE(o.model_config, '{{}}'),
                                   '$._delegate_from') IS NULL
                ORDER BY o.started_at ASC
                """
            ).fetchall()

            for orphan in orphans:
                donor = None
                evidence = ""
                reason = ""

                if orphan["parent_session_id"]:
                    evidence = "lineage"
                    donor = self._conn.execute(
                        f"""
                        SELECT {donor_columns}
                        FROM sessions d
                        WHERE d.id = ?
                          AND d.session_key IS NOT NULL
                          AND COALESCE(d.source, '') = COALESCE(?, '')
                        """,
                        (orphan["parent_session_id"], orphan["source"]),
                    ).fetchone()
                    if donor is None:
                        reason = (
                            "parent session carries no gateway identity of this source"
                        )
                else:
                    evidence = "contiguity"
                    candidates = self._conn.execute(
                        f"""
                        SELECT {donor_columns}, {donor_active} AS last_active
                        FROM sessions d
                        WHERE d.session_key IS NOT NULL
                          AND d.id != ?
                          AND COALESCE(d.source, '') = COALESCE(?, '')
                          AND (COALESCE(d.user_id, '') = ''
                               OR COALESCE(?, '') = ''
                               OR d.user_id = ?)
                          AND {donor_active} BETWEEN ? AND ?
                          AND {donor_active} < ?
                        ORDER BY last_active DESC
                        LIMIT 2
                        """,
                        (
                            orphan["id"],
                            orphan["source"],
                            orphan["user_id"],
                            orphan["user_id"],
                            (orphan["started_at"] or 0) - gap,
                            (orphan["started_at"] or 0) + gap,
                            orphan["last_active"],
                        ),
                    ).fetchall()
                    if not candidates:
                        reason = (
                            f"no keyed predecessor fell quiet within {gap:.0f}s "
                            "of this session's start"
                        )
                    elif len(candidates) > 1:
                        reason = (
                            "ambiguous: more than one keyed predecessor "
                            "matches this window"
                        )
                    else:
                        donor = candidates[0]

                records.append(
                    {
                        "orphan_id": orphan["id"],
                        "source": orphan["source"],
                        "message_count": orphan["message_count"],
                        "started_at": orphan["started_at"],
                        "last_active": orphan["last_active"],
                        "donor_id": donor["id"] if donor else None,
                        "session_key": donor["session_key"] if donor else None,
                        "evidence": evidence if donor else "",
                        "adoptable": donor is not None,
                        "reason": reason,
                    }
                )

        # Two unkeyed successors claiming the same predecessor means at most
        # one of them continues that chat, and nothing here says which.
        contested = {
            r["donor_id"]
            for r in records
            if r["adoptable"]
            and sum(1 for x in records if x["donor_id"] == r["donor_id"]) > 1
        }
        for record in records:
            if record["donor_id"] in contested:
                record["adoptable"] = False
                record["reason"] = (
                    "ambiguous: more than one unkeyed session claims this predecessor"
                )
        return records
