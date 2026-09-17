"""Session archive, prune, and cleanup maintenance for SessionDB.

``SessionPruningMixin`` is composed into ``SessionDB`` and owns no connection
state. The host supplies SQLite access, presentation/deletion methods, and
dynamic compatibility hooks for shared time, SQL, filtering, and logging
helpers. This module deliberately does not import ``session_db``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class SessionPruningMixin:
    """Select, archive, and permanently prune durable session history."""

    @classmethod
    def _prune_filter_where(
        cls,
        *,
        last_active_before: float | None = None,
        last_active_after: float | None = None,
        started_before: float | None = None,
        started_after: float | None = None,
        source: str | None = None,
        title_like: str | None = None,
        end_reason: str | None = None,
        cwd_prefix: str | None = None,
        min_messages: int | None = None,
        max_messages: int | None = None,
        archived: bool | None = None,
        model_like: str | None = None,
        provider: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        chat_type: str | None = None,
        branch_like: str | None = None,
        min_tokens: int | None = None,
        max_tokens: int | None = None,
        min_cost: float | None = None,
        max_cost: float | None = None,
        min_tool_calls: int | None = None,
        max_tool_calls: int | None = None,
    ) -> tuple[str, list]:
        """Build the shared WHERE clause for bulk prune/archive selection.

        All filters AND together. Only ended sessions are ever candidates
        (``ended_at IS NOT NULL``) so a live session is never selected.
        ``archived`` is a tri-state: ``None`` = both, ``True`` = only
        archived rows, ``False`` = only unarchived rows.

        String matching conventions: ``model_like`` / ``branch_like`` /
        ``title_like`` are case-insensitive substring matches (model slugs
        and branch names vary in prefix format); ``provider`` / ``user_id``
        / ``chat_id`` / ``chat_type`` / ``source`` / ``end_reason`` are
        exact (case-insensitive for provider). Token bounds apply to
        ``input_tokens + output_tokens``; cost bounds apply to
        ``COALESCE(actual_cost_usd, estimated_cost_usd)``.

        The clause references the ``s`` table alias — callers must select
        ``FROM sessions s``.
        """
        clauses = ["s.ended_at IS NOT NULL"]
        params: list = []
        if last_active_before is not None:
            clauses.append(
                """COALESCE(
                       (SELECT MAX(m.timestamp) FROM messages m
                        WHERE m.session_id = s.id),
                       s.started_at
                   ) < ?"""
            )
            params.append(last_active_before)
        if last_active_after is not None:
            clauses.append(
                """COALESCE(
                       (SELECT MAX(m.timestamp) FROM messages m
                        WHERE m.session_id = s.id),
                       s.started_at
                   ) >= ?"""
            )
            params.append(last_active_after)
        if started_before is not None:
            clauses.append("s.started_at < ?")
            params.append(started_before)
        if started_after is not None:
            clauses.append("s.started_at >= ?")
            params.append(started_after)
        if source:
            clauses.append("s.source = ?")
            params.append(source)
        if title_like:
            clauses.append("LOWER(COALESCE(s.title, '')) LIKE ? ESCAPE '\\'")
            params.append(f"%{cls._pruning_escape_like(title_like.lower())}%")
        if end_reason:
            clauses.append("s.end_reason = ?")
            params.append(end_reason)
        if cwd_prefix:
            clause, clause_params = cls._pruning_cwd_prefix_clause(cwd_prefix)
            clauses.append(clause)
            params.extend(clause_params)
        if min_messages is not None:
            clauses.append("s.message_count >= ?")
            params.append(min_messages)
        if max_messages is not None:
            clauses.append("s.message_count <= ?")
            params.append(max_messages)
        if model_like:
            clauses.append("LOWER(COALESCE(s.model, '')) LIKE ? ESCAPE '\\'")
            params.append(f"%{cls._pruning_escape_like(model_like.lower())}%")
        if provider:
            clauses.append("LOWER(COALESCE(s.billing_provider, '')) = ?")
            params.append(provider.lower())
        if user_id:
            clauses.append("s.user_id = ?")
            params.append(user_id)
        if chat_id:
            clauses.append("s.chat_id = ?")
            params.append(chat_id)
        if chat_type:
            clauses.append("s.chat_type = ?")
            params.append(chat_type)
        if branch_like:
            clauses.append("LOWER(COALESCE(s.git_branch, '')) LIKE ? ESCAPE '\\'")
            params.append(f"%{cls._pruning_escape_like(branch_like.lower())}%")
        if min_tokens is not None:
            clauses.append(
                "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)) >= ?"
            )
            params.append(min_tokens)
        if max_tokens is not None:
            clauses.append(
                "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)) <= ?"
            )
            params.append(max_tokens)
        if min_cost is not None:
            clauses.append("COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0) >= ?")
            params.append(min_cost)
        if max_cost is not None:
            clauses.append("COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0) <= ?")
            params.append(max_cost)
        if min_tool_calls is not None:
            clauses.append("COALESCE(s.tool_call_count, 0) >= ?")
            params.append(min_tool_calls)
        if max_tool_calls is not None:
            clauses.append("COALESCE(s.tool_call_count, 0) <= ?")
            params.append(max_tool_calls)
        if archived is True:
            clauses.append("s.archived = 1")
        elif archived is False:
            clauses.append("s.archived = 0")
        return " AND ".join(clauses), params

    @classmethod
    def _apply_prune_age_filter(
        cls, older_than_days: float | None, filters: dict[str, Any]
    ) -> None:
        """Translate the legacy age window into the shared activity filter."""
        if (
            filters.get("last_active_before") is None
            and filters.get("started_before") is None
            and older_than_days is not None
        ):
            filters["last_active_before"] = cls._pruning_now() - (
                older_than_days * 86400
            )

    def list_prune_candidates(
        self,
        older_than_days: float | None = None,
        source: str | None = None,
        **filters,
    ) -> list[dict[str, Any]]:
        """Return the sessions a matching :meth:`prune_sessions` /
        :meth:`archive_sessions` call would touch, without modifying anything.

        Backs ``--dry-run`` and pre-confirmation counts. Accepts the same
        keyword filters as :meth:`_prune_filter_where` (unknown names raise
        ``TypeError`` there). Rows are ordered oldest-first and carry
        ``id, source, title, model, started_at, last_active, ended_at,
        message_count, archived``. ``older_than_days`` is an inactivity
        threshold: it uses the latest message timestamp, falling back to
        ``started_at`` for sessions without messages.
        """
        self._apply_prune_age_filter(older_than_days, filters)
        where, params = self._prune_filter_where(source=source, **filters)
        with self._lock:
            cursor = self._conn.execute(
                f"""SELECT s.id, s.source, s.title, s.model, s.started_at,
                           COALESCE(
                               (SELECT MAX(m.timestamp) FROM messages m
                                WHERE m.session_id = s.id),
                               s.started_at
                           ) AS last_active,
                           s.ended_at, s.message_count, s.archived
                    FROM sessions s WHERE {where}
                    ORDER BY last_active ASC, s.started_at ASC""",
                params,
            )
            return [dict(row) for row in cursor.fetchall()]

    def count_open_prune_matches(
        self,
        older_than_days: float | None = None,
        source: str | None = None,
        **filters,
    ) -> int:
        """Count open sessions excluded from a matching bulk prune.

        This applies every normal prune filter, but inverts only the
        ``ended_at`` safety guard. It is visibility-only: callers can explain
        why an otherwise matching session was skipped without making live
        sessions eligible for destructive pruning.
        """
        self._apply_prune_age_filter(older_than_days, filters)
        where, params = self._prune_filter_where(source=source, **filters)
        ended_guard = "s.ended_at IS NOT NULL"
        if not where.startswith(ended_guard):
            raise RuntimeError("prune filter lost its ended-session safety guard")
        open_where = f"s.ended_at IS NULL{where[len(ended_guard) :]}"
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT COUNT(*) FROM sessions s WHERE {open_where}", params
            )
            return int(cursor.fetchone()[0])

    def archive_sessions(
        self,
        older_than_days: float | None = None,
        source: str | None = None,
        **filters,
    ) -> int:
        """Bulk-archive (soft-hide) every session matching the filters.

        Same filter surface as :meth:`prune_sessions`, but instead of deleting
        rows it flips ``archived = 1`` via :meth:`set_session_archived` so
        each match's compression lineage is archived as a unit (an unarchived
        compression root would otherwise resurrect the conversation in
        Desktop's projected list). Nothing is deleted; messages and transcript
        files are untouched. Returns the number of sessions matched.

        ``archived`` defaults to ``False`` here (only select rows not yet
        archived) so repeat runs are idempotent no-ops.
        """
        filters.setdefault("archived", False)
        rows = self.list_prune_candidates(
            older_than_days=older_than_days, source=source, **filters
        )
        for row in rows:
            self.set_session_archived(row["id"], True)
        return len(rows)

    def archive_stale_sessions(
        self, idle_days: float, *, exclude_pinned: bool = True
    ) -> int:
        """Archive every session untouched for at least ``idle_days`` days.

        "Touched" is the freshest of ``last_activity_at`` and the latest
        message timestamp (else ``started_at``) — i.e. real recency, not
        creation time — so a session
        created long ago but active yesterday is spared, while an old
        abandoned one (even a still-open one) is swept. Unlike
        :meth:`archive_sessions`, this method can also archive unended
        sessions.

        Guards:
          * ``pinned = 0`` when ``exclude_pinned`` (the Desktop "keep" flag).
          * ``archived = 0`` so repeat runs are idempotent no-ops.
          * only lineage *tips* / standalone rows are candidates
            (``end_reason <> 'compression'``); a stale tip archives its whole
            chain via :meth:`set_session_archived`, so we never resurrect an
            active conversation by matching an old compressed-away root whose
            live continuation is recent.

        Returns the number of sessions archived. Never raises for an empty or
        non-positive ``idle_days`` — it simply archives nothing.
        """
        if idle_days is None or idle_days < 0:
            return 0
        cutoff = self._pruning_now() - float(idle_days) * 86400.0
        pin_clause = "AND s.pinned = 0" if exclude_pinned else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT s.id FROM sessions s
                WHERE s.archived = 0
                  AND COALESCE(s.end_reason, '') <> 'compression'
                  {pin_clause}
                  AND {self._pruning_session_last_active_sql("s")} < ?
                ORDER BY s.started_at ASC
                """,
                (cutoff,),
            ).fetchall()
        ids = [self._pruning_row_id(r) for r in rows]
        for sid in ids:
            self.set_session_archived(sid, True)
        return len(ids)

    def prune_sessions(
        self,
        older_than_days: float | None = 90,
        source: str | None = None,
        sessions_dir: Path | None = None,
        **filters,
    ) -> int:
        """Delete sessions matching the filters. Returns count deleted.

        By default, delete ended sessions inactive for
        ``older_than_days`` days, optionally restricted to ``source``.
        Activity is the latest message timestamp, falling back to
        ``started_at`` for sessions without messages. Additional keyword
        filters AND together — the full set is defined by
        :meth:`_prune_filter_where`:

        * ``last_active_before`` / ``last_active_after`` — epoch bounds on
          the latest message timestamp (falling back to ``started_at``).
        * ``started_before`` / ``started_after`` — epoch bounds on
          ``started_at``. An explicit ``started_before`` overrides the
          default ``older_than_days`` inactivity cutoff; pass
          ``older_than_days=None`` for no implicit upper age bound.
        * ``title_like`` / ``model_like`` / ``branch_like`` —
          case-insensitive substring matches.
        * ``end_reason`` / ``provider`` / ``user_id`` / ``chat_id`` /
          ``chat_type`` — exact matches (provider case-insensitive, against
          ``billing_provider``).
        * ``cwd_prefix`` — session cwd equals or is under this path.
        * ``min_messages`` / ``max_messages`` — bounds on message_count.
        * ``min_tokens`` / ``max_tokens`` — bounds on input+output tokens.
        * ``min_cost`` / ``max_cost`` — bounds on USD cost
          (actual, falling back to estimated).
        * ``min_tool_calls`` / ``max_tool_calls`` — bounds on tool_call_count.
        * ``archived`` — tri-state: None = both (default), True = only
          archived, False = only unarchived.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.  When *sessions_dir* is provided, also removes
        on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for every pruned session, outside the DB
        transaction.
        """
        self._apply_prune_age_filter(older_than_days, filters)
        where, where_params = self._prune_filter_where(source=source, **filters)
        removed_ids: list[str] = []

        def _do(conn):
            cursor = conn.execute(
                f"SELECT s.id FROM sessions s WHERE {where}", where_params
            )
            session_ids = {row["id"] for row in cursor.fetchall()}

            if not session_ids:
                return 0

            # Orphan any sessions whose parent is about to be deleted
            placeholders = ",".join("?" * len(session_ids))
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                list(session_ids),
            )

            for sid in session_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                removed_ids.append(sid)
            self._delete_unreferenced_system_prompts(conn)
            return len(session_ids)

        count = self._execute_write(_do)
        # Clean up on-disk files outside the DB transaction
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    def purge_stale_tool_call_markers(
        self, *, dry_run: bool = False, backup: bool = True
    ) -> dict[str, Any]:
        """Permanently clear bare tool-call marker content (e.g. "[memory]")
        left in the ``messages`` table by sessions persisted before the
        #78148 fix in ``agent.conversation_loop``.

        ``_strip_stale_tool_call_markers`` already repairs this in memory on
        every session load (see ``_rows_to_conversation``), so running this
        is optional — but for long-lived sessions the same rows get
        re-scanned and re-repaired on every resume, which is wasted work
        and keeps the contaminated bytes sitting in the DB (and in any
        downstream cache/backup snapshot of it) indefinitely. This rewrites
        the affected rows once, in place.

        Only the ``content`` column is touched — ``role``, ``tool_calls``,
        and every other column on the row are left exactly as they are, so
        provider tool_call/tool_result pairing is unaffected.

        Unlike the in-memory repair, this UPDATE is permanent and can't be
        undone from within the DB. Since ``backup`` defaults to True, a
        timestamped full snapshot is taken via ``VACUUM INTO`` (safe against
        a live connection, unlike the raw-copy ``_backup_db_file`` used for
        malformed-schema repair) before any row is touched — mirroring
        ``repair_state_db_schema``'s backup-by-default convention for
        destructive state.db operations. No snapshot is taken when there is
        nothing to change.

        With ``dry_run=True``, reports the affected row count/ids without
        writing or backing up (read-only, no write lock taken).

        Returns ``{"dry_run": bool, "rows_affected": int, "row_ids": [...],
        "backup_path": str|None}``.
        """

        def _find_affected(conn) -> list[int]:
            cursor = conn.execute(
                "SELECT id, content FROM messages "
                "WHERE role = 'assistant' AND tool_calls IS NOT NULL AND tool_calls != ''"
            )
            affected: list[int] = []
            for row in cursor.fetchall():
                content = row["content"]
                if isinstance(
                    content, str
                ) and self._pruning_stale_tool_call_marker_matches(content.strip()):
                    affected.append(row["id"])
            return affected

        with self._read_ctx() as conn:
            affected_ids = _find_affected(conn)

        if dry_run:
            return {
                "dry_run": True,
                "rows_affected": len(affected_ids),
                "row_ids": affected_ids,
                "backup_path": None,
            }

        if not affected_ids:
            return {
                "dry_run": False,
                "rows_affected": 0,
                "row_ids": [],
                "backup_path": None,
            }

        backup_path: str | None = None
        if backup:
            import datetime

            stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            dest = self.db_path.with_name(
                f"{self.db_path.name}.pre-clean-markers-backup-{stamp}"
            )
            with self._lock:
                self._conn.execute("VACUUM INTO ?", (str(dest),))
            backup_path = str(dest)
            self._pruning_log_info(
                "Backed up state.db to %s before clean-markers write", backup_path
            )

        def _do(conn):
            ids = _find_affected(conn)
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE messages SET content = '' WHERE id IN ({placeholders})",
                    ids,
                )
            return ids

        affected_ids = self._execute_write(_do)
        if affected_ids:
            self._pruning_log_info(
                "Permanently cleared %d stale tool-call marker row(s) in state.db (#78148)",
                len(affected_ids),
            )
        return {
            "dry_run": False,
            "rows_affected": len(affected_ids),
            "row_ids": affected_ids,
            "backup_path": backup_path,
        }

    def prune_empty_ghost_sessions(
        self,
        sessions_dir: "Path | None" = None,  # noqa: UP037
    ) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = self._pruning_now() - 86400  # Only sessions older than 24 hours

        def _do(conn):
            rows = conn.execute(
                """
                SELECT id FROM sessions
                WHERE source = 'tui'
                  AND title IS NULL
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
            """,
                (cutoff,),
            ).fetchall()
            ids = [r[0] if isinstance(r, (tuple, list)) else r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"DELETE FROM sessions WHERE id IN ({placeholders})",  # noqa: S608
                    ids,
                )
                self._delete_unreferenced_system_prompts(conn)
            return ids

        removed_ids = self._execute_write(_do) or []
        # Clean up any on-disk session files (belt-and-suspenders)
        if sessions_dir and removed_ids:
            for sid in removed_ids:
                self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)

    def finalize_orphaned_compression_sessions(self) -> int:
        """Mark orphaned compression continuation sessions as ended.

        Targets child sessions that were never finalized: parent is ended
        with reason='compression', child has messages but no end_reason/ended_at
        and api_call_count=0.  Non-destructive: preserves all messages and sets
        end_reason='orphaned_compression'.  Fix for #20001.
        """
        cutoff = self._pruning_now() - 604800  # 7 days

        def _do(conn):
            now = self._pruning_now()
            result = conn.execute(
                """
                UPDATE sessions
                SET ended_at = ?,
                    end_reason = 'orphaned_compression'
                WHERE api_call_count = 0
                  AND end_reason IS NULL
                  AND ended_at IS NULL
                  AND started_at < ?
                  AND parent_session_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM sessions p
                      WHERE p.id = sessions.parent_session_id
                        AND p.end_reason = 'compression'
                        AND p.ended_at IS NOT NULL
                  )
                  AND EXISTS (
                      SELECT 1 FROM messages m
                      WHERE m.session_id = sessions.id
                  )
                """,
                (now, cutoff),
            )
            return result.rowcount

        return self._execute_write(_do) or 0
