"""Session row search and lightweight store metrics for SessionDB.

``SessionSearchMetricsMixin`` is composed into ``SessionDB`` and owns no
connection state. The host supplies SQLite access, row shaping, and dynamic
compatibility hooks for shared SQL predicates. This module deliberately does
not import ``session_db`` so the composition root remains acyclic.
"""
# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

from typing import Any


class SessionSearchMetricsMixin:
    """Search session rows and expose inexpensive session/message counts."""

    def search_sessions(
        self,
        source: str | None = None,
        limit: int = 20,
        offset: int = 0,
        workspace_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by source.

        Returns rows enriched with a computed ``last_active`` column
        (freshest of ``last_activity_at`` and latest message timestamp,
        else ``started_at``), ordered by most-recently-used first.

        Pass ``workspace_key`` to scope rows to one workspace - matching
        :func:`workspace_key` semantics (git repo root, else cwd). Used by
        session resume so the "last" session is the last one in
        the *current* workspace, not the global MRU.
        """
        select_with_last_active = (
            "SELECT s.*, "
            "COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved, "
            f"{self._search_metrics_last_active_sql('s')} AS last_active "
            "FROM sessions s "
            "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
        )
        where_clauses = []
        params: list = []
        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if workspace_key:
            ws_clause, ws_params = self._search_metrics_workspace_key_clause(
                workspace_key
            )
            where_clauses.append(ws_clause)
            params.extend(ws_params)
        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params.extend([limit, offset])
        with self._lock:
            cursor = self._conn.execute(
                f"{select_with_last_active}"
                f"{where_sql} "
                "ORDER BY last_active DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?",
                params,
            )
            return [self._session_row_dict(row) for row in cursor.fetchall()]

    def session_count(
        self,
        source: str | None = None,
        sources: list[str] | None = None,
        cwd_prefix: str | None = None,
        min_message_count: int = 0,
        include_archived: bool = False,
        archived_only: bool = False,
        exclude_children: bool = False,
        exclude_sources: list[str] | None = None,
    ) -> int:
        """Count sessions, optionally filtered by source.

        Pass ``exclude_children=True`` to count only the conversations that
        ``list_sessions_rich`` surfaces (root + branch/reset sessions), hiding
        sub-agent runs and compression continuations. Use it whenever the count
        is paired with a ``list_sessions_rich`` page (e.g. sidebar "load more"
        totals) so the total matches the number of listable rows — otherwise the
        raw row count is inflated by children and "load more" never settles.

        Pass ``exclude_sources`` to drop whole source classes from the count
        (e.g. ``["cron"]`` so the recents "load more" total matches a
        cron-excluded ``list_sessions_rich`` page and doesn't keep "load more"
        stuck on for buried scheduler sessions).
        """
        where_clauses = []
        params = []

        if exclude_children:
            # Mirror list_sessions_rich's child-exclusion clause exactly so the
            # count lines up with the rows: roots plus user-visible branch/reset
            # children.
            where_clauses.append(self._search_metrics_listable_child_sql())
            where_clauses.append(
                f"{self._search_metrics_delegate_from_json('s.model_config')} IS NULL"
            )
        include_sources = [source] if source else list(sources or [])
        if include_sources:
            placeholders = ",".join("?" for _ in include_sources)
            where_clauses.append(f"s.source IN ({placeholders})")
            params.extend(include_sources)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)
        if cwd_prefix:
            clause, clause_params = self._search_metrics_cwd_prefix_clause(cwd_prefix)
            where_clauses.append(clause)
            params.extend(clause_params)
        if min_message_count > 0:
            where_clauses.append("s.message_count >= ?")
            params.append(min_message_count)
        if archived_only:
            where_clauses.append("s.archived = 1")
        elif not include_archived:
            where_clauses.append("s.archived = 0")

        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self._lock:
            cursor = self._conn.execute(
                f"SELECT COUNT(*) FROM sessions s{where_sql}", params
            )
            return cursor.fetchone()[0]

    def session_count_ge(self, n: int = 1) -> bool:
        """Check if at least N sessions exist (archived included).

        Short-circuits via LIMIT — much cheaper than ``session_count()``,
        which pays a full index scan for its default ``archived = 0``
        filter (measured 543us vs 4us on a 20k-session DB). Archived
        sessions count: every caller so far asks "has this install ever
        had sessions", and an archived session is still a created one.
        Use this instead of ``session_count() >= n`` when the exact count
        is irrelevant.
        """
        with self._lock:
            cursor = self._conn.execute("SELECT 1 FROM sessions LIMIT ?", (n,))
            rows = cursor.fetchall()
        return len(rows) >= n

    def session_count_by_source(
        self,
        *,
        include_archived: bool = False,
        archived_only: bool = False,
        exclude_children: bool = False,
    ) -> dict[str, int]:
        """Return a ``{source: count}`` dict via a single ``GROUP BY`` query.

        Replaces the O(N) ``list_sessions_rich`` histogram loop with an
        aggregate query. When ``exclude_children`` is False the query uses
        ``idx_sessions_source``; when True, the child-exclusion predicates
        require a full table scan (same as ``session_count`` and
        ``list_sessions_rich``).

        ``exclude_children=True`` mirrors ``list_sessions_rich`` visibility
        (roots + branch/reset sessions, excluding sub-agent runs, delegates,
        and compression continuations) so the source counts match what the
        Sessions page actually lists.
        """
        where_clauses = []
        params: list = []

        if exclude_children:
            where_clauses.append(self._search_metrics_listable_child_sql())
            where_clauses.append(
                f"{self._search_metrics_delegate_from_json('s.model_config')} IS NULL"
            )
        if archived_only:
            where_clauses.append("s.archived = 1")
        elif not include_archived:
            where_clauses.append("s.archived = 0")

        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self._lock:
            if self._conn is None:
                raise RuntimeError("SessionDB connection is closed")
            rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(s.source, ''), 'cli') AS source, COUNT(*) AS count "
                f"FROM sessions s{where_sql} "
                "GROUP BY COALESCE(NULLIF(s.source, ''), 'cli') "
                "ORDER BY count DESC",
                params,
            ).fetchall()
        return {str(row["source"]): int(row["count"] or 0) for row in rows}

    def message_count(self, session_id: str | None = None) -> int:
        """Count messages, optionally for a specific session."""
        with self._lock:
            if session_id:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
            return cursor.fetchone()[0]

    def has_platform_message_id(
        self, session_id: str, platform_message_id: str
    ) -> bool:
        """Check if a message with the given platform_message_id exists.

        Uses the idx_messages_platform_msg_id partial index for efficient
        lookup. Used by the gateway's transient-failure dedupe guard (#47237)
        to skip re-persisting a user message that was already saved on a
        prior retry of the same inbound platform message.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM messages "
                "WHERE session_id = ? AND platform_message_id = ? LIMIT 1",
                (session_id, platform_message_id),
            )
            return cursor.fetchone() is not None
