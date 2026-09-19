"""Session row lifecycle and compaction publication for SessionDB.

The mixin owns session creation, end/reopen/reset transitions, transcript
replacement, and the public compaction commit gateways. Connection, search,
listing, metadata, token accounting, and lease ownership remain outside this
mixin. This module never imports :mod:`pcbdraft.services.session_db`.
"""
# mypy: disable-error-code="attr-defined,has-type"

# Lifecycle recovery and plugin-era compatibility keep their historical
# best-effort logging behavior.
# ruff: noqa: BLE001, S608

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionLifecycleHooks:
    system_prompt_hash: Callable[[str], str]
    reset_end_reasons: Callable[[], tuple[str, ...]]
    legacy_reset_child_sql: Callable[[str, str], str]
    now: Callable[[], float]
    json_dumps: Callable[..., str]
    json_loads: Callable[[str], Any]
    compression_busy_error: Callable[[str], BaseException]
    compression_closed_error: Callable[[str], BaseException]
    compression_in_progress_error: Callable[[str], BaseException]
    log_debug: Callable[..., None]


_lifecycle_hooks: SessionLifecycleHooks | None = None


def configure_session_lifecycle_hooks(hooks: SessionLifecycleHooks) -> None:
    """Install late-bound host hooks used by :class:`SessionLifecycleMixin`."""

    global _lifecycle_hooks
    _lifecycle_hooks = hooks


def _hooks() -> SessionLifecycleHooks:
    hooks = _lifecycle_hooks
    if hooks is None:
        raise RuntimeError("SessionDB lifecycle hooks are not configured")
    return hooks


def _system_prompt_hash_hook(system_prompt: str) -> str:
    return _hooks().system_prompt_hash(system_prompt)


def _reset_end_reasons_hook() -> tuple[str, ...]:
    return _hooks().reset_end_reasons()


def _legacy_reset_child_sql_hook(alias: str, reasons_sql: str) -> str:
    return _hooks().legacy_reset_child_sql(alias, reasons_sql)


def _now_hook() -> float:
    return _hooks().now()


def _json_dumps_hook(value: Any, **kwargs) -> str:
    return _hooks().json_dumps(value, **kwargs)


def _json_loads_hook(value: str) -> Any:
    return _hooks().json_loads(value)


def _compression_busy_error_hook(message: str) -> BaseException:
    return _hooks().compression_busy_error(message)


def _compression_closed_error_hook(session_id: str) -> BaseException:
    return _hooks().compression_closed_error(session_id)


def _compression_in_progress_error_hook(message: str) -> BaseException:
    return _hooks().compression_in_progress_error(message)


def _log_debug_hook(message: str, *args, **kwargs) -> None:
    _hooks().log_debug(message, *args, **kwargs)


class SessionLifecycleMixin:
    """Own durable session transitions and compaction commit gateways."""

    @staticmethod
    def _store_system_prompt(conn, system_prompt: str | None) -> str | None:
        if system_prompt is None:
            return None
        prompt_hash = _system_prompt_hash_hook(system_prompt)
        conn.execute(
            "INSERT OR IGNORE INTO system_prompts (hash, prompt) VALUES (?, ?)",
            (prompt_hash, system_prompt),
        )
        return prompt_hash

    @staticmethod
    def _delete_unreferenced_system_prompts(conn) -> None:
        conn.execute(
            "DELETE FROM system_prompts "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM sessions "
            "WHERE sessions.system_prompt_hash = system_prompts.hash"
            ")"
        )

    def _insert_session_row(
        self,
        session_id: str,
        source: str,
        model: str | None = None,
        model_config: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
        session_key: str | None = None,
        chat_id: str | None = None,
        chat_type: str | None = None,
        thread_id: str | None = None,
        parent_session_id: str | None = None,
        cwd: str | None = None,
        profile_name: str | None = None,
        git_repo_root: str | None = None,
        origin_json: str | None = None,
        display_name: str | None = None,
    ) -> None:
        """Insert a session row, enriching NULL metadata on conflict.

        The gateway's ``get_or_create_session`` creates a bare row (source +
        user_id) *before* the agent exists; the agent's later
        ``create_session`` then carries the real ``model`` / ``model_config`` /
        ``system_prompt``. A plain ``INSERT OR IGNORE`` silently dropped that
        enrichment, leaving gateway sessions with NULL model/billing metadata.
        The ``ON CONFLICT`` upsert backfills those fields via ``COALESCE`` —
        only filling columns that are still NULL, never overwriting values an
        earlier writer already set (so a later bare call with source="unknown"
        can't clobber a real source/model).

        ``chat_id``/``thread_id`` record the messaging origin (the chat/room and
        thread the session was started in) so that gateway ``/resume`` can prove
        a persisted, now-inactive row belongs to the caller's chat/thread before
        switching to it (IDOR scoping — without them the ``sessions`` table has
        no chat/thread to compare).

        When ``parent_session_id`` is set (compression fork, delegate/subagent
        spawn, branch continuation) and this row's own ``cwd``/``git_repo_root``/
        ``git_branch``/``profile_name`` are still NULL after the insert, they are
        backfilled from the parent row. Callers of ``create_session`` for a child
        session historically didn't propagate these fields themselves (e.g. the
        compression-fork path), so a lineage could silently lose its working
        directory and drop out of the project sidebar every time it forked
        (#64709), or lose its owning profile and be aggregated as "default" every
        time it rotated or branched (the cross-profile session-jump bug). This
        only fills NULLs — an explicit value on the child is never overwritten.
        For compression forks specifically
        (parent ended with ``end_reason='compression'``), the gateway origin
        columns (``user_id``/``session_key``/``chat_id``/``chat_type``/
        ``thread_id``/``display_name``/``origin_json``) are inherited too, so a
        crash before the gateway re-records the peer can't strand the child
        without a recoverable routing mapping (#59527).
        """

        def _do(conn):
            system_prompt_hash = self._store_system_prompt(conn, system_prompt)
            conn.execute(
                """INSERT INTO sessions (
                   id, source, user_id, session_key, chat_id, chat_type, thread_id,
                   model, model_config, system_prompt, system_prompt_hash,
                   parent_session_id, cwd, profile_name, git_repo_root,
                   origin_json, display_name, started_at
                )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       model = COALESCE(sessions.model, excluded.model),
                       model_config = CASE
                           WHEN excluded.model_config IS NOT NULL
                                AND json_type(
                                    sessions.model_config, '$._reset_from'
                                ) IS NOT NULL
                                AND json_remove(
                                    sessions.model_config, '$._reset_from'
                                ) = '{}'
                           THEN json_set(
                               excluded.model_config,
                               '$._reset_from',
                               json_extract(
                                   sessions.model_config, '$._reset_from'
                               )
                           )
                           ELSE COALESCE(
                               sessions.model_config, excluded.model_config
                           )
                       END,
                       system_prompt_hash = COALESCE(
                           sessions.system_prompt_hash,
                           excluded.system_prompt_hash
                       ),
                       system_prompt = CASE
                           WHEN sessions.system_prompt_hash IS NULL
                                AND excluded.system_prompt_hash IS NOT NULL
                           THEN NULL
                           ELSE sessions.system_prompt
                       END,
                       session_key = COALESCE(sessions.session_key, excluded.session_key),
                       chat_id = COALESCE(sessions.chat_id, excluded.chat_id),
                       chat_type = COALESCE(sessions.chat_type, excluded.chat_type),
                       thread_id = COALESCE(sessions.thread_id, excluded.thread_id),
                       parent_session_id = COALESCE(sessions.parent_session_id, excluded.parent_session_id),
                       cwd = COALESCE(sessions.cwd, excluded.cwd),
                       profile_name = COALESCE(sessions.profile_name, excluded.profile_name),
                       git_repo_root = COALESCE(sessions.git_repo_root, excluded.git_repo_root),
                       origin_json = COALESCE(sessions.origin_json, excluded.origin_json),
                       display_name = COALESCE(sessions.display_name, excluded.display_name)""",
                (
                    session_id,
                    source,
                    user_id,
                    session_key,
                    chat_id,
                    chat_type,
                    thread_id,
                    model,
                    _json_dumps_hook(model_config) if model_config else None,
                    system_prompt_hash,
                    parent_session_id,
                    cwd,
                    profile_name,
                    git_repo_root,
                    origin_json,
                    display_name,
                    _now_hook(),
                ),
            )
            if system_prompt_hash is not None:
                self._delete_unreferenced_system_prompts(conn)
            if parent_session_id:
                conn.execute(
                    """UPDATE sessions
                       SET cwd = COALESCE(sessions.cwd,
                                 (SELECT p.cwd FROM sessions p
                                   WHERE p.id = sessions.parent_session_id)),
                           git_repo_root = COALESCE(sessions.git_repo_root,
                                           (SELECT p.git_repo_root FROM sessions p
                                             WHERE p.id = sessions.parent_session_id)),
                           git_branch = COALESCE(sessions.git_branch,
                                        (SELECT p.git_branch FROM sessions p
                                          WHERE p.id = sessions.parent_session_id)),
                           profile_name = COALESCE(sessions.profile_name,
                                          (SELECT p.profile_name FROM sessions p
                                            WHERE p.id = sessions.parent_session_id))
                     WHERE id = ? AND parent_session_id IS NOT NULL""",
                    (session_id,),
                )
                # Belt-and-suspenders for gateway routing metadata (#59527):
                # the gateway re-records the peer on the child after rotation
                # (d5b4879d4), but a hard crash between child creation and that
                # write leaves the child row without origin columns, so
                # ``find_latest_gateway_session_for_peer`` can't recover the
                # mapping on restart. Inherit them from the parent at creation
                # time — but ONLY for compression forks (parent already ended
                # with end_reason='compression'). Delegate/subagent children
                # are spawned while the parent is still live and must NOT
                # inherit routing keys, or peer recovery could repoint gateway
                # traffic into a subagent's session.
                conn.execute(
                    """UPDATE sessions
                       SET user_id = COALESCE(sessions.user_id,
                                     (SELECT p.user_id FROM sessions p
                                       WHERE p.id = sessions.parent_session_id)),
                           session_key = COALESCE(sessions.session_key,
                                         (SELECT p.session_key FROM sessions p
                                           WHERE p.id = sessions.parent_session_id)),
                           chat_id = COALESCE(sessions.chat_id,
                                     (SELECT p.chat_id FROM sessions p
                                       WHERE p.id = sessions.parent_session_id)),
                           chat_type = COALESCE(sessions.chat_type,
                                       (SELECT p.chat_type FROM sessions p
                                         WHERE p.id = sessions.parent_session_id)),
                           thread_id = COALESCE(sessions.thread_id,
                                       (SELECT p.thread_id FROM sessions p
                                         WHERE p.id = sessions.parent_session_id)),
                           display_name = COALESCE(sessions.display_name,
                                          (SELECT p.display_name FROM sessions p
                                            WHERE p.id = sessions.parent_session_id)),
                           origin_json = COALESCE(sessions.origin_json,
                                         (SELECT p.origin_json FROM sessions p
                                           WHERE p.id = sessions.parent_session_id))
                     WHERE id = ? AND parent_session_id IS NOT NULL
                       AND EXISTS (
                           SELECT 1 FROM sessions p
                           WHERE p.id = sessions.parent_session_id
                             AND p.end_reason = 'compression'
                       )""",
                    (session_id,),
                )

        # Session-row creation is transcript-critical: if it fails, the
        # first flush of a new session fails and the turn is aborted as
        # session_persistence_failed. Ride out long sibling holds.
        self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def create_session(self, session_id: str, source: str, **kwargs) -> str:
        """Create a new session record. Returns the session_id."""
        self._insert_session_row(session_id, source, **kwargs)
        return session_id

    # Children that carry a ``parent_session_id`` but are NOT compression
    # continuations: branches, delegate/subagent runs, and tool sessions.
    # A marker only disqualifies a child when it points at the parent being
    # queried — compression continuations inherit the rotated agent's
    # ``model_config`` verbatim (``publish_compression_child`` callers pass
    # ``agent._session_init_model_config``), so a delegate subagent's
    # continuation carries ``_delegate_from=<the delegate's own parent>``.
    # Matching markers by mere presence misclassified those real
    # continuations as delegate children (fail-open for orphan reopen,
    # fail-closed for adoption). Bind the parent id for both markers.
    _NON_CONTINUATION_CHILD_FILTER_SQL = (
        "  AND COALESCE(json_extract(COALESCE({alias}model_config, '{{}}'),"
        " '$._branched_from'), '') != ?\n"
        "  AND COALESCE(json_extract(COALESCE({alias}model_config, '{{}}'),"
        " '$._delegate_from'), '') != ?\n"
        "  AND COALESCE({alias}source, '') != 'tool'\n"
    )

    def find_live_compression_child(
        self, parent_session_id: str
    ) -> dict[str, Any] | None:
        """Return the unique live direct child of a compression-ended session.

        A stale agent may observe that another compression path already rotated
        its parent. Recovery is safe only when the durable lineage identifies
        exactly one live direct continuation. Multiple children are treated as
        ambiguous and fail closed rather than guessing which transcript owns
        subsequent messages.
        """
        if not parent_session_id:
            return None
        with self._lock:
            parent = self._conn.execute(
                "SELECT ended_at, end_reason FROM sessions WHERE id = ?",
                (parent_session_id,),
            ).fetchone()
            if (
                parent is None
                or parent["ended_at"] is None
                or parent["end_reason"] != "compression"
            ):
                return None
            rows = self._conn.execute(
                """
                SELECT s.*,
                       COALESCE(sp.prompt, s.system_prompt)
                           AS _system_prompt_resolved
                FROM sessions s
                LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash
                WHERE s.parent_session_id = ?
                  AND s.ended_at IS NULL
                """
                + self._NON_CONTINUATION_CHILD_FILTER_SQL.format(alias="s.")
                + """
                ORDER BY s.started_at ASC
                LIMIT 2
                """,
                (parent_session_id, parent_session_id, parent_session_id),
            ).fetchall()
        return self._session_row_dict(rows[0]) if len(rows) == 1 else None

    def reopen_orphaned_compression_session(self, session_id: str) -> bool:
        """Reopen a compression parent only when no continuation was published.

        Compression publication is atomic in current builds, but older builds
        could leave a closed parent behind after an interrupted handoff.  This
        recovery is deliberately conservative: an active compression lease or
        any canonical child means the lineage is still owned by another path,
        so the caller must fail closed instead of reopening the parent.
        """
        if not session_id:
            return False

        def _do(conn):
            parent = conn.execute(
                "SELECT ended_at, end_reason FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if (
                parent is None
                or parent["ended_at"] is None
                or parent["end_reason"] != "compression"
            ):
                return False

            # Treat any direct non-branch/non-delegate/non-tool child as a
            # continuation, regardless of its current ended state. Reopening
            # in that case could create a second live head for one lineage.
            child = conn.execute(
                """
                SELECT 1
                FROM sessions
                WHERE parent_session_id = ?
                """
                + self._NON_CONTINUATION_CHILD_FILTER_SQL.format(alias="")
                + """
                LIMIT 1
                """,
                (session_id, session_id, session_id),
            ).fetchone()
            if child is not None:
                return False

            # Lease ownership remains in the dedicated host mixin; reclaim
            # inside this same transaction so a later refresh cannot resurrect
            # the old holder.
            if not self._reclaim_expired_compression_lease_on_conn(conn, session_id):
                return False

            updated = conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL "
                "WHERE id = ? AND ended_at IS NOT NULL "
                "AND end_reason = 'compression'",
                (session_id,),
            )
            # rowcount==1 is guaranteed by the parent SELECT at the top of
            # this same BEGIN IMMEDIATE transaction. If this is ever edited
            # to return False past this point, note that the lease DELETE
            # above will still COMMIT (_execute_write commits unless _do
            # raises) — raise instead of returning False to roll back.
            return updated.rowcount == 1

        return bool(self._execute_write(_do))

    def publish_compression_child(
        self,
        *,
        parent_session_id: str,
        child_session_id: str,
        source: str,
        messages: list[dict[str, Any]],
        model: str | None = None,
        model_config: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        profile_name: str | None = None,
        compression_lock_holder: str | None = None,
        require_compression_lease: bool = True,
        watermark: int | None = None,
        watermark_ceiling: int | None = None,
    ) -> None:
        """Atomically close a parent and publish its durable compression child.

        The parent closure, child row, and compacted handoff become visible in
        one transaction. Readers can therefore observe either the live parent or
        a complete child, never an ended parent with a missing/empty child.

        Concurrent-append safety (#75316): when *watermark* is provided (the
        parent's :meth:`get_active_message_watermark` captured at compression
        start), parent rows that arrived during the slow summary call
        (``id > watermark``) are cloned into the child AFTER the handoff —
        same pure-SQL column clone as :meth:`archive_and_compact`, with the
        session id rewritten — so a mid-compression append survives rotation
        instead of stranding in the closed parent.

        *watermark_ceiling* bounds the clone from above: the rotation path
        flushes its OWN un-persisted input transcript to the parent right
        before publishing (#47202), and those rows are already represented in
        the compacted handoff — cloning them would duplicate the transcript.
        The caller captures ``MAX(id)`` immediately BEFORE that flush; only
        rows in ``(watermark, watermark_ceiling]`` are foreign concurrent
        tail. ``None`` = unbounded (no internal flush happened).
        """

        def _do(conn):
            if (
                require_compression_lease
                and not self._compression_lease_matches_on_conn(
                    conn, parent_session_id, compression_lock_holder
                )
            ):
                raise _compression_busy_error_hook(
                    f"Compression lease lost before publication: {parent_session_id}"
                )
            parent = conn.execute(
                """SELECT ended_at, cwd, git_branch, git_repo_root,
                          user_id, session_key, chat_id, chat_type,
                          thread_id, display_name, origin_json, profile_name
                   FROM sessions WHERE id = ?""",
                (parent_session_id,),
            ).fetchone()
            if parent is None:
                raise RuntimeError(f"Compression parent not found: {parent_session_id}")
            if parent["ended_at"] is not None:
                raise RuntimeError(
                    f"Compression parent already ended: {parent_session_id}"
                )
            if not messages:
                raise RuntimeError("Compression child handoff must not be empty")
            system_prompt_hash = self._store_system_prompt(conn, system_prompt)

            conn.execute(
                """INSERT INTO sessions (
                   id, source, model, model_config, system_prompt,
                   system_prompt_hash,
                   parent_session_id, cwd, git_branch, git_repo_root,
                   profile_name, user_id, session_key, chat_id, chat_type,
                   thread_id, display_name, origin_json, started_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    child_session_id,
                    source,
                    model,
                    _json_dumps_hook(model_config) if model_config else None,
                    system_prompt_hash,
                    parent_session_id,
                    cwd or parent["cwd"],
                    parent["git_branch"],
                    parent["git_repo_root"],
                    # Same inheritance contract as _insert_session_row's
                    # compression-fork backfill (#59527 / cross-profile jump
                    # fix): the child stays on the parent's profile and keeps
                    # the gateway routing/origin columns so peer recovery
                    # still works after a crash at the boundary.
                    profile_name or parent["profile_name"],
                    parent["user_id"],
                    parent["session_key"],
                    parent["chat_id"],
                    parent["chat_type"],
                    parent["thread_id"],
                    parent["display_name"],
                    parent["origin_json"],
                    _now_hook(),
                ),
            )
            total_messages, total_tool_calls = self._insert_message_rows(
                conn, child_session_id, messages
            )
            if watermark is not None:
                # Clone the parent's concurrent tail (rows landed after the
                # watermark, at or below the ceiling — see docstring) into the
                # child, after the handoff. Column-exact except id/session_id;
                # originals stay in the (closed) parent for lineage recovery.
                _ceiling_clause = ""
                _params: list = [parent_session_id, int(watermark)]
                if watermark_ceiling is not None:
                    _ceiling_clause = " AND id <= ?"
                    _params.append(int(watermark_ceiling))
                tail_rows = conn.execute(
                    "SELECT id, tool_calls FROM messages "
                    "WHERE session_id = ? AND active = 1 AND id > ?"
                    f"{_ceiling_clause} ORDER BY id",
                    _params,
                ).fetchall()
                if tail_rows:
                    tail_ids = [int(r["id"]) for r in tail_rows]
                    placeholders = ",".join("?" for _ in tail_ids)
                    clone_cols = [
                        c
                        for c in self._message_column_names(conn)
                        if c not in ("id", "session_id", "active", "compacted")
                    ]
                    col_list = ", ".join(clone_cols)
                    conn.execute(
                        f"INSERT INTO messages ({col_list}, session_id, active, compacted) "
                        f"SELECT {col_list}, ?, 1, 0 FROM messages "
                        f"WHERE id IN ({placeholders}) ORDER BY id",
                        [child_session_id, *tail_ids],
                    )
                    total_messages += len(tail_ids)
                    for r in tail_rows:
                        raw = r["tool_calls"]
                        if raw:
                            try:
                                parsed = (
                                    _json_loads_hook(raw)
                                    if isinstance(raw, str)
                                    else raw
                                )
                                total_tool_calls += (
                                    len(parsed) if isinstance(parsed, list) else 0
                                )
                            except (TypeError, ValueError):
                                pass
            conn.execute(
                "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
                (total_messages, total_tool_calls, child_session_id),
            )
            updated = conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = 'compression' "
                "WHERE id = ? AND ended_at IS NULL",
                (_now_hook(), parent_session_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"Compression parent changed during publication: {parent_session_id}"
                )

        self._execute_write(_do)

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended.

        No-ops when the session is already ended. The first end_reason wins:
        compression-split sessions must keep their ``end_reason = 'compression'``
        record even if a later stale ``end_session()`` call (e.g. from a
        desynced CLI session_id after ``/resume`` or ``/branch``) targets them
        with a different reason. Use ``reopen_session()`` first if you
        intentionally need to re-end a closed session with a new reason.
        """

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (_now_hook(), end_reason, session_id),
            )

        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed.

        Before clearing a reset boundary, stabilize markerless legacy reset
        children that still depend on the parent's mutable end_reason.
        """

        def _do(conn):
            placeholders = ",".join("?" for _ in _reset_end_reasons_hook())
            # WHERE shape shared with _RESET_CHILD_SQL's fallback arm via
            # _legacy_reset_child_sql so the stamping and the listing
            # predicate cannot drift.
            conn.execute(
                "UPDATE sessions AS child SET model_config = json_set("
                "COALESCE(child.model_config, '{}'), '$._reset_from', "
                "child.parent_session_id) "
                "WHERE child.parent_session_id = ? "
                "AND json_extract(COALESCE(child.model_config, '{}'), "
                "                 '$._reset_from') IS NULL "
                f"AND {_legacy_reset_child_sql_hook('child', placeholders)}",
                (session_id, *_reset_end_reasons_hook()),
            )
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                (session_id,),
            )

        self._execute_write(_do)

    def promote_to_session_reset(
        self, session_id: str, reason: str = "session_reset"
    ) -> bool:
        """Durably mark a session as ended by an intentional reset boundary.

        Promotes *only* live rows (``ended_at IS NULL``) or rows carrying an
        accidental end_reason that the recovery query
        (``find_latest_gateway_session_for_peer``) treats as recoverable:
        ``agent_close`` (older gateway cleanup bug) and ``ws_orphan_reap``
        (mistaken TUI reaper).  Explicit conversation boundaries such as
        ``compression``, ``session_reset``, ``session_switch``, etc. are
        preserved — the first writer wins for those, and a later expiry
        finalization must not silently overwrite them.

        Plain ``end_session()`` is NOT sufficient for reset boundaries: it
        no-ops on an already-ended row, so a row that agent cleanup already
        closed as ``agent_close`` would stay recoverable and stale-route
        recovery would resurrect the reset session with its full history
        (#61220, #61993, #63539).

        Keep this promotion set in sync with the recoverable set in
        ``find_latest_gateway_session_for_peer`` — any reason recovery would
        reopen must be promotable here.

        ``reason`` lets reset paths keep their auditable specific reasons
        (``idle``, ``daily``, ``suspended``, ``resume_pending_expired``).

        Returns ``True`` when the row was promoted, ``False`` when skipped
        (already has a different explicit end_reason, or row not found).
        """
        if not session_id:
            return False
        now = _now_hook()

        def _do(conn):
            cursor = conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND (ended_at IS NULL "
                "OR end_reason IN ('agent_close', 'ws_orphan_reap'))",
                (now, reason, session_id),
            )
            return cursor.rowcount

        try:
            rows = self._execute_write(_do)
            return bool(rows)
        except Exception:
            _log_debug_hook("Session reset-boundary promotion failed", exc_info=True)
            return False

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str | None = None,
        **kwargs,
    ) -> str:
        """Ensure a session row exists (INSERT OR IGNORE). Accepts optional kwargs."""
        self._insert_session_row(session_id, source, model=model, **kwargs)
        return session_id

    def replace_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        active_only: bool = False,
        archive_dropped: bool = False,
    ) -> None:
        """Atomically replace the stored messages for a session.

        Used by transcript-rewrite flows such as /retry, /undo, and /compress.
        The delete + reinsert sequence must commit as one transaction so a
        mid-rewrite failure does not leave SQLite with a partial transcript.

        DESTRUCTIVE by default: every row for the session is DELETEd (and drops
        out of the FTS index). For compaction that must preserve the
        pre-compaction transcript under the same id, use
        :meth:`archive_and_compact` instead.

        Pass ``active_only=True`` to replace ONLY the live (``active = 1``) rows,
        leaving soft-archived rows (``active = 0`` — e.g. the ``compacted = 1``
        turns that :meth:`archive_and_compact` keeps on disk for #38763
        durability, or rewind/undo rows) untouched. Callers that share a session
        id with an agent already running in-place compaction must use this so a
        full-history rewrite doesn't wipe the rows the agent deliberately
        archived. ``message_count``/``tool_call_count`` then track the live set,
        matching :meth:`archive_and_compact`.

        Pass ``archive_dropped=True`` to SOFT-archive the live rows instead of
        DELETEing them: the replaced turns stay on disk with ``active = 0``,
        ``compacted = 0`` — the same "the user took it back" marking
        :meth:`rewind_to_message` applies — and stay readable via
        :meth:`get_messages` with ``include_inactive=True``. This is the mode a
        rewind/edit/regenerate must use: those flows overwrite a transcript the
        user may not have meant to drop, and a plain DELETE also evicts the rows
        from the FTS index, leaving nothing to recover from (#82756). It implies
        active-only handling — already-archived rows are never touched — so
        ``active_only`` is redundant with it. The rewritten set is inserted as
        fresh active rows exactly as in the destructive path, so the live view
        is identical either way; only the durability of the dropped turns
        differs.
        """

        active_clause = " AND active = 1" if active_only else ""

        def _do(conn):
            session = conn.execute(
                "SELECT ended_at, end_reason FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if (
                session is not None
                and session["ended_at"] is not None
                and session["end_reason"] == "compression"
            ):
                raise _compression_closed_error_hook(session_id)
            if archive_dropped:
                # Content-preserving UPDATE: the rows keep their FTS entries
                # (the messages_fts triggers fire on INSERT / DELETE / UPDATE
                # of content columns, not on `active`), so the replaced turns
                # stay readable via get_messages(include_inactive=True) and
                # searchable with include_inactive=True after the rewrite.
                conn.execute(
                    "UPDATE messages SET active = 0 "
                    "WHERE session_id = ? AND active = 1",
                    (session_id,),
                )
            else:
                conn.execute(
                    f"DELETE FROM messages WHERE session_id = ?{active_clause}",
                    (session_id,),
                )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,),
            )
            total_messages, total_tool_calls = self._insert_message_rows(
                conn, session_id, messages
            )
            conn.execute(
                "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
                (total_messages, total_tool_calls, session_id),
            )

        self._execute_write(_do)

    def get_active_message_watermark(self, session_id: str) -> int:
        """MAX(id) of the session's active rows — the compression watermark.

        Captured at compression START (before the slow provider summary call).
        Every active row with ``id > watermark`` at commit time arrived
        concurrently and must survive the compaction verbatim. Returns 0 for
        an empty/unknown session.
        """
        if not session_id:
            return 0
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages "
                "WHERE session_id = ? AND active = 1",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def archive_and_compact(
        self,
        session_id: str,
        compacted_messages: list[dict[str, Any]],
        model_config_patch: dict[str, Any] | None = None,
        watermark: int | None = None,
        lock_holder: str | None = None,
    ) -> int:
        """Non-destructive in-place compaction for a single durable session id.

        Soft-archives the active messages (``active = 0``) and inserts
        *compacted_messages* as fresh active rows — atomically, in one write
        transaction. The conversation keeps ONE session id for life (#38763)
        WITHOUT destroying history:

        - The live-context load (:meth:`get_messages_as_conversation`,
          :meth:`get_messages`) filters ``active = 1`` by default, so the model
          reloads ONLY the compacted set.
        - The archived pre-compaction turns stay on disk (active=0) and stay
          DISCOVERABLE: they are marked compacted=1, and search_messages()
          includes compacted=1 rows by default — so session_search still finds
          them, unlike rewind/undo rows (active=0, compacted=0) which stay
          hidden. They remain in the FTS index (the messages_fts* triggers
          index on INSERT / drop on DELETE and don't key on active/compacted;
          flipping to active=0 is a content-preserving UPDATE) and are
          recoverable via get_messages(..., include_inactive=True).

        Concurrent-append safety (#75316): when *watermark* is provided (the
        value of :meth:`get_active_message_watermark` captured at compression
        START), rows that arrived during the slow provider summary call
        (``id > watermark``) are NOT summarized away. They are re-sequenced
        after the compacted set by a pure-SQL column clone (every column
        except ``id`` — content, api_content, platform_message_id, token
        counts, reasoning sidecars all survive byte-exact, and the FTS
        triggers index the clones naturally), and the originals are archived.
        NOTE: re-sequencing assigns the tail rows fresh ids; consumers that
        reference durable row ids re-resolve by content (see 3e8ab0610).
        ``watermark=None`` preserves the historical archive-everything
        behavior.

        Commit-fence safety: when *lock_holder* is provided, the commit
        verifies INSIDE the transaction that the compression lock is still
        held by that holder and unexpired — a compression whose lease was
        reclaimed (crash cleanup, TTL expiry, competing writer) fails the
        commit instead of clobbering the winner's transcript.

        ``message_count`` is set to the ACTIVE count after commit, matching
        what the live load returns. ``model_config_patch`` is merged into the
        session's JSON config in the same transaction; a ``None`` value
        removes that key. Returns the new active count.
        """

        def _do(conn):
            if lock_holder is not None and not self._compression_lease_matches_on_conn(
                conn, session_id, lock_holder
            ):
                raise _compression_in_progress_error_hook(
                    f"Compression lease for {session_id!r} lost before "
                    "commit; refusing to publish a stale compaction"
                )

            patched_model_config = None
            if model_config_patch is not None:
                # on_missing="raise": a prune/compaction must not commit
                # against a vanished session row (the compressor's caller
                # converts the raised error into a safe keep-the-original
                # no-op), unlike the flag setters which tolerate missing rows.
                patched_model_config = self._merge_model_config_json(
                    conn, session_id, model_config_patch, on_missing="raise"
                )

            # Concurrent tail: active rows that arrived after the watermark.
            # Snapshot their ids and tool_calls now — the clone below needs a
            # stable id list, and the tool-call count keeps sessions.* honest.
            tail_ids: list[int] = []
            tail_tool_calls = 0
            if watermark is not None:
                for row in conn.execute(
                    "SELECT id, tool_calls FROM messages "
                    "WHERE session_id = ? AND active = 1 AND id > ? "
                    "ORDER BY id",
                    (session_id, int(watermark)),
                ).fetchall():
                    tail_ids.append(int(row["id"]))
                    raw = row["tool_calls"]
                    if raw:
                        try:
                            parsed = (
                                _json_loads_hook(raw) if isinstance(raw, str) else raw
                            )
                            tail_tool_calls += (
                                len(parsed) if isinstance(parsed, list) else 0
                            )
                        except (TypeError, ValueError):
                            pass

            # Soft-archive the live turns: active=0 hides them from the live
            # context load, compacted=1 marks them as "summarized away" (vs
            # rewind/undo's active=0+compacted=0, which means "user took it
            # back"). search_messages includes compacted=1 rows by default so
            # the pre-compaction transcript stays discoverable; live-context
            # loads (active=1 only) still exclude them. Tail originals are
            # archived too — their clones (below) carry the live copy.
            conn.execute(
                "UPDATE messages SET active = 0, compacted = 1 "
                "WHERE session_id = ? AND active = 1",
                (session_id,),
            )
            inserted, tool_calls_total = self._insert_message_rows(
                conn, session_id, compacted_messages
            )

            if tail_ids:
                # Re-sequence the concurrent tail after the compacted set via
                # a pure-SQL column clone: no decode/re-encode round trip, no
                # field drift — new id, active=1, compacted=0, all else exact.
                placeholders = ",".join("?" for _ in tail_ids)
                clone_cols = [
                    c
                    for c in self._message_column_names(conn)
                    if c not in ("id", "active", "compacted")
                ]
                col_list = ", ".join(clone_cols)
                conn.execute(
                    f"INSERT INTO messages ({col_list}, active, compacted) "
                    f"SELECT {col_list}, 1, 0 FROM messages "
                    f"WHERE id IN ({placeholders}) ORDER BY id",
                    tail_ids,
                )
                inserted += len(tail_ids)
                tool_calls_total += tail_tool_calls

            # message_count / tool_call_count reflect the LIVE (active) set —
            # the archived rows are still on disk but not part of the live count.
            if model_config_patch is None:
                conn.execute(
                    "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
                    (inserted, tool_calls_total, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET message_count = ?, tool_call_count = ?, "
                    "model_config = ? WHERE id = ?",
                    (inserted, tool_calls_total, patched_model_config, session_id),
                )
            return inserted

        return self._execute_write(_do)
