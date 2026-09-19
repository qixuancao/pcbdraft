"""Read-only session listing, usage, and lifecycle projections.

``SessionListingMixin`` is composed into ``SessionDB`` and owns no connection
state. The host supplies SQLite access, row shaping, token flushing, compression
projection helpers, and dynamic compatibility hooks. This module deliberately
does not import ``session_db`` so the composition root remains acyclic.
"""
# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

import re
from typing import Any


class SessionListingMixin:
    """Project durable session rows into list, usage, and lifecycle views."""

    def usage_totals(
        self, *, min_message_count: int = 1, include_archived: bool = False
    ) -> dict[str, float]:
        """Tokens and spend across this store, as one aggregate.

        The sidebar shows a profile's totals beside a page of its sessions, so
        summing the rows it happens to have loaded would report a fraction of
        the truth and shrink as paging changed. SQLite adds the columns up over
        every row instead, at the cost of one scan.

        Spend is the billed figure when the provider returned one and the
        estimate otherwise — the same precedence a single row renders.
        """
        where = ["parent_session_id IS NULL", "message_count >= ?"]
        params: list[Any] = [min_message_count]
        if not include_archived:
            where.append("COALESCE(archived, 0) = 0")

        with self._read_ctx() as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0),
                       COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 0)
                  FROM sessions
                 WHERE {" AND ".join(where)}
                """,
                params,
            ).fetchone()

        return {"tokens": int(row[0] or 0), "cost_usd": float(row[1] or 0.0)}

    def list_sessions_rich(
        self,
        source: str | None = None,
        sources: list[str] | None = None,
        exclude_sources: list[str] | None = None,
        cwd_prefix: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        min_message_count: int = 0,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
        include_archived: bool = False,
        archived_only: bool = False,
        id_query: str | None = None,
        search_query: str | None = None,
        compact_rows: bool = False,
        include_pinned: bool = False,
        session_key: str | None = None,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (freshest of last_activity_at heartbeat and latest
        message timestamp, else started_at).

        Uses a single query with correlated subqueries instead of N+2 queries.

        By default, child sessions that represent implementation details
        (subagent runs, compression continuations) are excluded. User-visible
        branch and reset children remain listable. Pass ``include_children=True``
        to include every child.

        With ``project_compression_tips=True`` (default), sessions that are
        roots of compression chains are projected forward to their latest
        continuation — one logical conversation = one list entry, showing the
        live continuation's id/message_count/title/last_active. This prevents
        compressed continuations from being invisible to users while keeping
        delegate subagents and branches hidden. Pass ``False`` to return the
        raw root rows (useful for admin/debug UIs).

        Pass ``order_by_last_active=True`` to sort by most-recent activity
        instead of original conversation start time. For compression chains,
        the "most-recent activity" is taken from the live tip (not the root),
        so an old conversation that was compressed and continued recently
        surfaces in the correct slot. Ordering is computed at SQL level via
        a recursive CTE that walks compression-continuation edges, so LIMIT
        and OFFSET still apply efficiently.

        ``search_query`` matches case-insensitive substrings against each
        surfaced row's title and id (and, like ``id_query``, every title/id in
        its forward compression chain). A punctuation-stripped variant is also
        matched so e.g. ``an94`` finds ``AN-94``. Only honored in the
        ``order_by_last_active`` path.

        Pass ``compact_rows=True`` for dashboard and picker callers that only
        need lightweight metadata. This omits the ``system_prompt`` blob from
        the SELECT so SQLite never copies it out of the B-tree page — a
        significant I/O saving on large databases where the blob routinely
        runs to tens of kilobytes per row.

        Pass ``include_pinned=True`` to back-fill any conversation carrying the
        durable ``pinned`` flag that the LIMIT/OFFSET window left out. A pin is
        a "this must always be reachable" statement, so a pinned conversation
        aging past the requested page is a bug, not a paging outcome — the
        desktop sidebar would render an empty Pinned section. Back-filled rows
        obey the same filters (source, archived, min_message_count) as the
        page: an archived or filtered-out conversation stays out.

        Pass ``session_key`` to restrict results to one stable gateway
        conversation scope (DM, group, channel, or thread, including the
        configured per-user isolation policy).
        """
        # Rows carry token/cost totals — drain queued deltas first so
        # listings (sidebar, /resume, dashboards) show exact counters.
        self.flush_token_counts()
        where_clauses = []
        params = []

        if not include_children:
            # Show roots and user-visible branch/reset sessions, while still
            # hiding sub-agent runs and compression continuations. All four
            # carry parent_session_id, so the shared predicate classifies the
            # edge from stable markers plus legacy-compatible parent metadata.
            #
            # Branch sessions are identified two ways, OR'd for robustness:
            #   1. A stable ``_branched_from`` marker in model_config, written
            #      by /branch at creation time. This survives the parent being
            #      reopened and re-ended with a different end_reason (e.g.
            #      tui_shutdown overwriting 'branched'), which otherwise hides
            #      the branch — see issue #20856.
            #   2. The legacy heuristic (parent ended with 'branched' before the
            #      child started), covering branch sessions created before the
            #      marker existed.
            where_clauses.append(self._listing_listable_child_sql())
            where_clauses.append(
                f"{self._listing_delegate_from_json('s.model_config')} IS NULL"
            )

        include_sources = [source] if source else list(sources or [])
        if include_sources:
            placeholders = ",".join("?" for _ in include_sources)
            where_clauses.append(f"s.source IN ({placeholders})")
            params.extend(include_sources)
        if session_key:
            where_clauses.append("s.session_key = ?")
            params.append(session_key)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)
        if cwd_prefix:
            clause, clause_params = self._listing_cwd_prefix_clause(cwd_prefix)
            where_clauses.append(clause)
            params.extend(clause_params)
        if min_message_count > 0:
            where_clauses.append("s.message_count >= ?")
            params.append(min_message_count)
        if archived_only:
            where_clauses.append("s.archived = 1")
        elif not include_archived:
            where_clauses.append("s.archived = 0")
        if not include_hidden:
            where_clauses.append("s.hidden = 0")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        # Snapshot the filter params before the query builders below extend
        # them with LIMIT/OFFSET — the pinned back-fill reuses the same WHERE.
        base_where_params = list(params)
        prompt_select = (
            ""
            if compact_rows
            else ", COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved"
        )
        prompt_join = (
            ""
            if compact_rows
            else "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash"
        )

        # Optional session-id filter, pushed into SQL so callers (Desktop
        # session-id search) don't have to fetch every row and filter in
        # Python. ``id_query`` is matched as a case-insensitive substring
        # against each surfaced row's id AND every id in its forward
        # compression chain — so searching a compression *root* id or a *tip*
        # id both resolve to the same projected conversation. Only used in the
        # order_by_last_active path (which builds the chain CTE); other callers
        # pass id_query=None.
        id_needle = (id_query or "").strip().lower()
        search_needle = (search_query or "").strip().lower()
        if order_by_last_active:
            # Compute effective_last_active by walking each surfaced session's
            # compression-continuation chain forward in SQL and taking the MAX
            # timestamp across the chain. This lets us ORDER BY + LIMIT at SQL
            # level instead of fetching every row and sorting in Python, while
            # still surfacing old compression roots whose live tip is fresh.
            #
            # The CTE seeds from rows the outer WHERE admits (roots +
            # user-visible branch/reset children), then recursively joins through
            # compression-continuation edges. Do NOT require
            # child.started_at >= parent.ended_at here: real desktop/gateway
            # races can insert the continuation row before the parent's
            # ended_at is written, while stale websocket siblings may satisfy
            # the timestamp test and hijack resume/list projection.
            outer_where = where_sql
            id_params: list[Any] = []
            filter_clauses: list[str] = []

            def _like_pattern(needle: str) -> str:
                return f"%{self._listing_escape_like(needle)}%"

            if id_needle:
                # Admit a surfaced row if its own id or any id in its forward
                # compression chain matches the needle. LIKE with a leading
                # wildcard can't use an index, but the chain membership and
                # the small result set keep this bounded — far cheaper than
                # fetching every session and scanning in Python.
                filter_clauses.append(
                    "EXISTS (SELECT 1 FROM chain cq"
                    "        WHERE cq.root_id = s.id"
                    "          AND LOWER(cq.cur_id) LIKE ? ESCAPE '\\')"
                )
                id_params.append(_like_pattern(id_needle))
            if search_needle:
                # Same chain-membership trick as id_query, but matching either
                # the title or the id of any session in the chain. The compact
                # (punctuation-stripped) variant lets `an94` match `AN-94`.
                compact_needle = re.sub(r"[\W_]+", "", search_needle)
                compact_sql = (
                    "REPLACE(REPLACE(REPLACE(REPLACE(LOWER(COALESCE({0}, '')),"
                    " '-', ''), '_', ''), '.', ''), ' ', '')"
                )
                search_clause = (
                    "EXISTS (SELECT 1 FROM chain cq"
                    " JOIN sessions cs ON cs.id = cq.cur_id"
                    " WHERE cq.root_id = s.id"
                    " AND (LOWER(COALESCE(cs.title, '')) LIKE ? ESCAPE '\\'"
                    " OR LOWER(cq.cur_id) LIKE ? ESCAPE '\\'"
                )
                id_params.extend([_like_pattern(search_needle)] * 2)
                if compact_needle:
                    search_clause += (
                        f" OR {compact_sql.format('cs.title')} LIKE ? ESCAPE '\\'"
                    )
                    id_params.append(_like_pattern(compact_needle))
                filter_clauses.append(search_clause + "))")
            if filter_clauses:
                combined = " AND ".join(filter_clauses)
                outer_where = (
                    f"{where_sql} AND {combined}" if where_sql else f"WHERE {combined}"
                )
            _sel = self._compact_session_cols() if compact_rows else "s.*"
            query = f"""
                WITH RECURSIVE chain(root_id, cur_id) AS (
                    SELECT s.id, s.id FROM sessions s {where_sql}
                    UNION ALL
                    SELECT c.root_id, child.id
                    FROM chain c
                    JOIN sessions parent ON parent.id = c.cur_id
                    JOIN sessions child ON child.parent_session_id = c.cur_id
                    WHERE parent.end_reason = 'compression'
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
                      AND COALESCE(child.source, '') != 'tool'
                ),
                chain_max AS (
                    SELECT
                        root_id,
                        MAX({self._listing_session_last_active_by_id_sql("cur_id")}) AS effective_last_active
                    FROM chain
                    GROUP BY root_id
                )
                SELECT {_sel}{prompt_select},
                    COALESCE(
                        (SELECT {self._listing_preview_raw_select()}
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw,
                    {self._listing_session_last_active_sql("s")} AS last_active,
                    COALESCE(cm.effective_last_active, s.started_at) AS _effective_last_active
                FROM sessions s
                LEFT JOIN chain_max cm ON cm.root_id = s.id
                {prompt_join}
                {outer_where}
                ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC
                LIMIT ? OFFSET ?
            """
            # WHERE params apply twice (CTE seed + outer select); the id filter
            # only applies to the outer select.
            params = params + params + id_params + [limit, offset]
        else:
            _sel = self._compact_session_cols() if compact_rows else "s.*"
            query = f"""
                SELECT {_sel}{prompt_select},
                    COALESCE(
                        (SELECT {self._listing_preview_raw_select()}
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw,
                    {self._listing_session_last_active_sql("s")} AS last_active
                FROM sessions s
                {prompt_join}
                {where_sql}
                ORDER BY s.started_at DESC
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])
        with self._read_ctx() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = self._session_row_dict(row)
            s["preview"] = self._listing_shape_preview(s.pop("_preview_raw", ""))
            # Drop the internal ordering column so callers see a clean dict.
            s.pop("_effective_last_active", None)
            sessions.append(s)

        # Back-fill pinned conversations the page missed. A pin outlives
        # recency, so this runs BEFORE compression projection below — a
        # back-filled root then projects to its live tip exactly like a row
        # that had made the page on its own. One extra query, bounded by the
        # number of pins (a handful), never N+1 per pin.
        if include_pinned:
            seen_ids = {s["id"] for s in sessions}
            pinned_where = (
                f"{where_sql} AND s.pinned = 1" if where_sql else "WHERE s.pinned = 1"
            )
            _sel = self._compact_session_cols() if compact_rows else "s.*"
            pinned_query = f"""
                SELECT {_sel}{prompt_select},
                    COALESCE(
                        (SELECT {self._listing_preview_raw_select()}
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw,
                    COALESCE(
                        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                        s.started_at
                    ) AS last_active
                FROM sessions s
                {prompt_join}
                {pinned_where}
                ORDER BY s.started_at DESC
            """
            with self._read_ctx() as conn:
                pinned_cursor = conn.execute(pinned_query, base_where_params)
                pinned_rows = pinned_cursor.fetchall()
            for row in pinned_rows:
                s = self._session_row_dict(row)
                if s["id"] in seen_ids:
                    continue
                s["preview"] = self._listing_shape_preview(s.pop("_preview_raw", ""))
                seen_ids.add(s["id"])
                sessions.append(s)

        # Project compression roots forward to their tips. Each row whose
        # end_reason is 'compression' has a continuation child; replace the
        # surfaced fields (id, message_count, title, last_active, ended_at,
        # end_reason, preview) with the tip's values so the list entry acts
        # as the live conversation. Keep the root's started_at to preserve
        # chronological ordering by original conversation start.
        if project_compression_tips and not include_children:
            # get_compression_tip() walks each root's chain individually (it's
            # a per-session graph walk, not batchable in one query), but the
            # tip *row* fetch afterward was previously one _get_session_rich_row()
            # call per compression root. Batch that half instead: resolve
            # every tip id first, then fetch all tip rows in a single query.
            tip_ids_by_root: dict[str, str] = {}
            for s in sessions:
                if s.get("end_reason") != "compression":
                    continue
                tip_id = self.get_compression_tip(s["id"])
                if tip_id != s["id"]:
                    tip_ids_by_root[s["id"]] = tip_id

            tip_rows = (
                self._get_session_rich_rows_batch(
                    set(tip_ids_by_root.values()), compact_rows=compact_rows
                )
                if tip_ids_by_root
                else {}
            )

            projected = []
            for s in sessions:
                tip_id = tip_ids_by_root.get(s["id"])
                tip_row = tip_rows.get(tip_id) if tip_id else None
                if not tip_row:
                    projected.append(s)
                    continue
                # Preserve the root's started_at for stable sort order, but
                # surface the tip's identity and activity data.
                merged = dict(s)
                for key in (
                    "id",
                    "ended_at",
                    "end_reason",
                    "message_count",
                    "tool_call_count",
                    "title",
                    "last_active",
                    "preview",
                    "model",
                    "system_prompt",
                    "cwd",
                    "git_branch",
                    "git_repo_root",
                ):
                    if key in tip_row:
                        merged[key] = tip_row[key]
                merged["_lineage_root_id"] = s["id"]
                projected.append(merged)
            sessions = projected

        # Derive read state per surfaced conversation. ``last_read_at`` is
        # lineage-stamped by set_session_read, so a projected row's root
        # watermark and its tip's are the same value — comparing it against
        # the tip's last_active is correct either way.
        for s in sessions:
            s["unread"] = self._listing_session_unread(s)

        return sessions

    def session_lifecycle_statuses(self, session_ids: list[str]) -> dict[str, str]:
        """Classify each session's lifecycle state from its LAST message row.

        Returns ``{session_id: status}`` where status is one of:

        - ``'complete'``    — last message is a normal assistant reply
        - ``'interrupted'`` — last message is a user turn, a pending assistant
          tool call (no tool result followed), or a tool result the assistant
          never responded to
        - ``'error'``       — last message carries an error finish_reason
        - ``'empty'``       — session has no messages

        Cost-bounded by design: one query that resolves each listed session's
        newest message id via ``MAX(id)`` (an index seek on
        ``idx_messages_session_id``) and joins back for that single row's
        role/tool_calls/finish_reason. Never scans transcripts, so it stays
        cheap on large databases regardless of total message volume.
        """
        ids = [sid for sid in (session_ids or []) if sid]
        if not ids:
            return {}
        statuses: dict[str, str] = {sid: "empty" for sid in ids}
        placeholders = ",".join("?" for _ in ids)
        query = f"""
            SELECT m.session_id, m.role,
                   m.tool_calls IS NOT NULL AS has_tool_calls,
                   m.finish_reason
            FROM messages m
            JOIN (
                SELECT session_id, MAX(id) AS max_id
                FROM messages
                WHERE session_id IN ({placeholders})
                GROUP BY session_id
            ) latest ON m.id = latest.max_id
        """
        with self._read_ctx() as conn:
            rows = conn.execute(query, ids).fetchall()
        for row in rows:
            statuses[row["session_id"]] = self._listing_classify_session_status(
                role=row["role"],
                has_tool_calls=bool(row["has_tool_calls"]),
                finish_reason=row["finish_reason"],
            )
        return statuses
