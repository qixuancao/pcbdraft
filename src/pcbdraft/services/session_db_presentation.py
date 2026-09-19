"""Session titles and user-facing presentation state for :class:`SessionDB`.

Mixin contract: this plain mixin is consumed by
``pcbdraft.services.session_db.SessionDB``. It defines no ``__init__`` and owns
no connection state. The host supplies connection/write helpers, row shaping,
and compatibility hooks for shared SQL and sanitization helpers. This module
must never import ``session_db`` so the store remains the composition root.
"""
# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

import re
from typing import Any, ClassVar


class SessionPresentationStateMixin:
    """Manage titles, visibility flags, read state, and compression tips."""

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    # Title provenance, lowest to highest authority. An auto-titling write may
    # only replace a title of strictly lower authority, so the instant
    # ``derived`` title upgrades to the model's ``llm`` title exactly once and
    # nothing the agent generates can ever clobber a name the user typed.
    TITLE_SOURCE_DERIVED = "derived"
    TITLE_SOURCE_LLM = "llm"
    TITLE_SOURCE_USER = "user"
    _TITLE_SOURCE_RANK: ClassVar[dict[str, int]] = {
        TITLE_SOURCE_DERIVED: 0,
        TITLE_SOURCE_LLM: 1,
        TITLE_SOURCE_USER: 2,
    }

    @classmethod
    def _title_rank(cls, source: str | None) -> int:
        """Rank a stored title_source. NULL means a pre-provenance row.

        Rows written before this column existed carry NULL. They were almost
        always set by the old auto-titler, but a manual ``/title`` from that
        era is indistinguishable — so treat NULL as ``user`` and refuse to
        overwrite it. Auto-titling only ever fills genuinely empty titles on
        legacy rows, which is the conservative direction.
        """
        if source is None:
            return cls._TITLE_SOURCE_RANK[cls.TITLE_SOURCE_USER]
        return cls._TITLE_SOURCE_RANK.get(str(source), 0)

    @classmethod
    def sanitize_title(cls, title: str | None) -> str | None:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Lone surrogates cannot be bound by sqlite3 (UnicodeEncodeError at
        # UTF-8 encode time) — scrub them like every other write path here.
        title = cls._presentation_sanitize_title_text(title)

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r"[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]",
            "",
            cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > cls.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {cls.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def _is_compression_ancestor(
        self, conn, *, ancestor_id: str, descendant_id: str
    ) -> bool:
        """Return True if *ancestor_id* is a compression predecessor of
        *descendant_id* (walking parent links up the continuation chain).

        The continuation edge is the canonical one shared with
        :func:`_ephemeral_child_sql` / :meth:`set_session_archived`
        (``_COMPRESSION_CHILD_SQL``): a parent → child edge counts only when the
        parent ended with ``end_reason = 'compression'`` and the child started
        at or after the parent's ``ended_at``, which distinguishes continuations
        from delegate subagents / branch children that also carry a
        ``parent_session_id``. Expressed as a single recursive CTE rather than a
        per-hop Python walk so the edge definition lives in exactly one place.
        """
        if not ancestor_id or not descendant_id or ancestor_id == descendant_id:
            return False
        # Walk parent links up from the descendant, following only compression
        # continuation edges, and check whether ancestor_id is reached.
        edge = self._presentation_compression_child_sql("child")
        row = conn.execute(
            f"""
            WITH RECURSIVE ancestors(id) AS (
                SELECT ?
                UNION
                SELECT parent.id
                FROM ancestors a
                JOIN sessions child ON child.id = a.id
                JOIN sessions parent ON parent.id = child.parent_session_id
                WHERE {edge}
            )
            SELECT 1 FROM ancestors WHERE id = ? AND id != ? LIMIT 1
            """,
            (descendant_id, ancestor_id, descendant_id),
        ).fetchone()
        return row is not None

    def _set_session_title(
        self,
        session_id: str,
        title: str,
        *,
        source: str,
    ) -> bool:
        """Write a title, enforcing provenance precedence.

        ``source`` is one of ``TITLE_SOURCE_{DERIVED,LLM,USER}``. A ``user``
        write always lands — an explicit rename is authoritative. An automatic
        write (``derived``/``llm``) lands only when the row is untitled or the
        stored title has strictly lower authority, so the instant ``derived``
        title upgrades to ``llm`` exactly once and neither can ever overwrite a
        name the user typed. Re-running the titler on an already-``llm`` row is
        a no-op, which is what stops a session renaming itself.

        The read and the write are one compare-and-swap inside a single
        transaction, so a manual ``/title`` racing an in-flight generation
        cannot be clobbered by the late arrival.
        """
        title = self.sanitize_title(title)
        is_user = source == self.TITLE_SOURCE_USER
        new_rank = self._title_rank(source) if not is_user else None

        def _do(conn):
            current = conn.execute(
                "SELECT title, title_source FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if current is None:
                return 0
            if (
                not is_user
                and current["title"] is not None
                and self._title_rank(current["title_source"]) >= new_rank
            ):
                return 0

            if title:
                # Check uniqueness (allow the same session to keep its own title)
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    conflict_id = conflict["id"]
                    # A compression continuation is the live, projected-forward
                    # head of its conversation; its compressed predecessors are
                    # ended and hidden from the session list (list_sessions_rich
                    # projects roots → tip). When the title that "conflicts" is
                    # held by such a hidden ancestor, the user has no way to free
                    # it — renaming the visible tip back to the base name would
                    # dead-end with "already in use by <session they can't see>".
                    # Treat this as a transfer: move the title off the ancestor
                    # onto the continuation. Uniqueness is preserved (still only
                    # one session carries the exact title) and the parent-link
                    # lineage is untouched.
                    if self._is_compression_ancestor(
                        conn, ancestor_id=conflict_id, descendant_id=session_id
                    ):
                        conn.execute(
                            "UPDATE sessions SET title = NULL WHERE id = ?",
                            (conflict_id,),
                        )
                    else:
                        raise ValueError(
                            f"Title '{title}' is already in use by session {conflict_id}"
                        )
            # Compare-and-swap on the exact values we just read (``IS`` is
            # NULL-safe in SQLite), so a concurrent write between the SELECT
            # and here loses instead of being silently overwritten.
            cursor = conn.execute(
                "UPDATE sessions SET title = ?, title_source = ? "
                "WHERE id = ? AND title IS ? AND title_source IS ?",
                (
                    title,
                    source if title else None,
                    session_id,
                    current["title"],
                    current["title_source"],
                ),
            )
            return cursor.rowcount

        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Set or update a session's title on the user's behalf.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).

        This records ``user`` provenance, so auto-titling will never replace
        the result. Automatic callers must use :meth:`set_auto_title`.
        """
        return self._set_session_title(session_id, title, source=self.TITLE_SOURCE_USER)

    def set_auto_title(self, session_id: str, title: str, *, source: str) -> bool:
        """Set an automatically generated title, honoring provenance precedence.

        Returns True when the title was written, False when a higher-authority
        title already holds the row (nothing is modified in that case).
        """
        if source not in (self.TITLE_SOURCE_DERIVED, self.TITLE_SOURCE_LLM):
            raise ValueError(f"invalid automatic title source: {source!r}")
        return self._set_session_title(session_id, title, source=source)

    def set_auto_title_if_empty(self, session_id: str, title: str) -> bool:
        """Back-compat shim: set an LLM title only if nothing better exists.

        Retained because older callers (and third-party plugins) reference it
        by name. New code should call :meth:`set_auto_title` with an explicit
        source.
        """
        return self.set_auto_title(session_id, title, source=self.TITLE_SOURCE_LLM)

    def get_session_title(self, session_id: str) -> str | None:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def get_session_title_source(self, session_id: str) -> str | None:
        """Get the provenance of a session's title, or None when untitled."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title, title_source FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
        if not row or row["title"] is None:
            return None
        return row["title_source"]

    def set_session_title_source(self, session_id: str, source: str) -> bool:
        """Overwrite a title's provenance without touching the title text.

        Used when a title is carried across a session boundary (compression
        rotation) and the copy must keep the original's authority rather than
        the authority of whichever setter performed the copy.
        """
        if source not in self._TITLE_SOURCE_RANK:
            raise ValueError(f"invalid title source: {source!r}")

        def _do(conn):
            cursor = conn.execute(
                "UPDATE sessions SET title_source = ? "
                "WHERE id = ? AND title IS NOT NULL",
                (source, session_id),
            )
            return cursor.rowcount

        return self._execute_write(_do) > 0

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        """Archive or unarchive a session.

        Archived sessions are hidden from the default session list but keep all
        their messages — this is a soft hide, not a delete. For compression
        chains, archive the whole logical conversation. Desktop lists compression
        roots projected forward to their latest continuation; updating only the
        displayed tip lets the still-unarchived root resurrect it on refresh.
        Returns True when at least one row was updated.
        """

        def _do(conn):
            cursor = conn.execute(
                """
                WITH RECURSIVE
                  ancestors(id) AS (
                    SELECT ?
                    UNION
                    SELECT parent.id
                    FROM ancestors a
                    JOIN sessions child ON child.id = a.id
                    JOIN sessions parent ON parent.id = child.parent_session_id
                    WHERE parent.end_reason = 'compression'
                  ),
                  descendants(id) AS (
                    SELECT ?
                    UNION
                    SELECT child.id
                    FROM descendants d
                    JOIN sessions parent ON parent.id = d.id
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.end_reason = 'compression'
                  ),
                  lineage(id) AS (
                    SELECT id FROM ancestors
                    UNION
                    SELECT id FROM descendants
                  )
                UPDATE sessions
                SET archived = ?
                WHERE id IN (SELECT id FROM lineage)
                """,
                (session_id, session_id, 1 if archived else 0),
            )
            rowcount = cursor.rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute("SELECT changes()").fetchone()[0]
            return rowcount

        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_pinned(self, session_id: str, pinned: bool) -> bool:
        """Pin or unpin a session (and its whole compression lineage).

        ``pinned`` is a durable "keep" flag: pinned sessions are exempt from
        the ``sessions.auto_archive`` stale sweep (see
        :meth:`archive_stale_sessions`). Desktop is the current writer — its
        sidebar pins mirror here so a backend/other-surface sweep honours
        them. Like :meth:`set_session_archived` the whole compression chain is
        flipped as a unit, so pinning the surfaced tip protects the root (and
        vice-versa) no matter which id the caller holds. Returns True when at
        least one row changed.
        """

        def _do(conn):
            cursor = conn.execute(
                """
                WITH RECURSIVE
                  ancestors(id) AS (
                    SELECT ?
                    UNION
                    SELECT parent.id
                    FROM ancestors a
                    JOIN sessions child ON child.id = a.id
                    JOIN sessions parent ON parent.id = child.parent_session_id
                    WHERE parent.end_reason = 'compression'
                  ),
                  descendants(id) AS (
                    SELECT ?
                    UNION
                    SELECT child.id
                    FROM descendants d
                    JOIN sessions parent ON parent.id = d.id
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.end_reason = 'compression'
                  ),
                  lineage(id) AS (
                    SELECT id FROM ancestors
                    UNION
                    SELECT id FROM descendants
                  )
                UPDATE sessions
                SET pinned = ?
                WHERE id IN (SELECT id FROM lineage)
                """,
                (session_id, session_id, 1 if pinned else 0),
            )
            rowcount = cursor.rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute("SELECT changes()").fetchone()[0]
            return rowcount

        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_hidden(self, session_id: str, hidden: bool) -> bool:
        """Hide or unhide a session (and its whole compression lineage).

        ``hidden`` is a generic "don't show in the global Sessions sidebar"
        flag: a hidden session is dropped from the default
        :meth:`list_sessions_rich` listing (which omits ``include_hidden``) but
        stays fully resumable by the surface that owns it — useful for plugins
        that manage their own sessions (e.g. kanban) and don't want them
        cluttering the shared recents list. Like :meth:`set_session_archived`
        / :meth:`set_session_pinned` the whole compression chain is flipped as
        a unit, so hiding the surfaced tip hides the root (and vice-versa) no
        matter which id the caller holds. Returns True when at least one row
        changed.
        """

        def _do(conn):
            cursor = conn.execute(
                """
                WITH RECURSIVE
                  ancestors(id) AS (
                    SELECT ?
                    UNION
                    SELECT parent.id
                    FROM ancestors a
                    JOIN sessions child ON child.id = a.id
                    JOIN sessions parent ON parent.id = child.parent_session_id
                    WHERE parent.end_reason = 'compression'
                  ),
                  descendants(id) AS (
                    SELECT ?
                    UNION
                    SELECT child.id
                    FROM descendants d
                    JOIN sessions parent ON parent.id = d.id
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.end_reason = 'compression'
                  ),
                  lineage(id) AS (
                    SELECT id FROM ancestors
                    UNION
                    SELECT id FROM descendants
                  )
                UPDATE sessions
                SET hidden = ?
                WHERE id IN (SELECT id FROM lineage)
                """,
                (session_id, session_id, 1 if hidden else 0),
            )
            rowcount = cursor.rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute("SELECT changes()").fetchone()[0]
            return rowcount

        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_read(self, session_id: str, read: bool = True) -> bool:
        """Mark a session read or unread (and its whole compression lineage).

        Read state is a watermark, not a flag: ``last_read_at`` records when
        the conversation was last read, and it counts as unread when activity
        postdates that watermark (the derived ``unread`` key on
        :meth:`list_sessions_rich` rows). New messages therefore flip a read
        conversation back to unread without any write on the message path.
        Three states:

        * NULL — never tracked (every pre-feature row): treated as read, so
          shipping the column doesn't badge a user's entire history at once.
        * 0 — explicitly marked unread: any activity postdates it.
        * timestamp — read up to that moment.

        Like :meth:`set_session_archived` / :meth:`set_session_pinned`, the
        whole compression chain is stamped as a unit, so reading the surfaced
        tip clears the root (and vice-versa) no matter which id the caller
        holds. Returns True when at least one row changed.
        """

        def _do(conn):
            cursor = conn.execute(
                """
                WITH RECURSIVE
                  ancestors(id) AS (
                    SELECT ?
                    UNION
                    SELECT parent.id
                    FROM ancestors a
                    JOIN sessions child ON child.id = a.id
                    JOIN sessions parent ON parent.id = child.parent_session_id
                    WHERE parent.end_reason = 'compression'
                  ),
                  descendants(id) AS (
                    SELECT ?
                    UNION
                    SELECT child.id
                    FROM descendants d
                    JOIN sessions parent ON parent.id = d.id
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.end_reason = 'compression'
                  ),
                  lineage(id) AS (
                    SELECT id FROM ancestors
                    UNION
                    SELECT id FROM descendants
                  )
                UPDATE sessions
                SET last_read_at = ?
                WHERE id IN (SELECT id FROM lineage)
                """,
                (session_id, session_id, self._presentation_now() if read else 0.0),
            )
            rowcount = cursor.rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute("SELECT changes()").fetchone()[0]
            return rowcount

        rowcount = self._execute_write(_do)
        return rowcount > 0

    @staticmethod
    def session_unread(session_row: dict[str, Any]) -> bool:
        """Derive unread from a session row's watermark and activity.

        Shared by ``list_sessions_rich`` and any future surface that holds a
        row (or projected row) with ``last_read_at`` and ``last_active``.
        NULL watermark = never tracked = read.
        """
        last_read = session_row.get("last_read_at")
        if last_read is None:
            return False
        last_active = session_row.get("last_active") or session_row.get("started_at")
        return float(last_active or 0) > float(last_read)

    def get_session_by_title(self, title: str) -> dict[str, Any] | None:
        """Look up a session by exact title. Returns session dict or None."""
        with self._read_ctx() as conn:
            cursor = conn.execute(
                "SELECT s.*, "
                "COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved "
                "FROM sessions s "
                "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
                "WHERE s.title = ?",
                (title,),
            )
            row = cursor.fetchone()
        return self._session_row_dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> str | None:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        # First try exact match
        exact = self.get_session_by_title(title)

        # Also search for numbered variants: "title #2", "title #3", etc.
        # Escape SQL LIKE wildcards (%, _) in the title to prevent false matches
        escaped = self._presentation_escape_like(title)
        with self._read_ctx() as conn:
            cursor = conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        match = re.match(r"^(.*?) #(\d+)$", base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Find all existing numbered variants
        # Escape SQL LIKE wildcards (%, _) in the base to prevent false matches
        escaped = self._presentation_escape_like(base)
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # No conflict, use the base name as-is

        # Find the highest number
        max_num = 1  # The unnumbered original counts as #1
        for t in existing:
            m = re.match(r"^.* #(\d+)$", t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"

    def get_compression_tip(self, session_id: str) -> str | None:
        """Walk the compression-continuation chain forward and return the tip.

        A compression continuation is a child of a session whose
        ``end_reason = 'compression'``.  Older builds tried to distinguish
        continuations from branches/subagents by requiring
        ``child.started_at >= parent.ended_at``.  That ordering is too brittle:
        gateway + compression races can insert the real continuation row before
        the parent row's ``ended_at`` is written, while a stale websocket later
        creates/reuses a sibling that *does* satisfy the timestamp test.  The
        visible symptom is brutal: desktop resume follows the stale sibling and
        the user's latest messages look "lost" even though they are persisted in
        the real continuation chain.

        Instead, only follow children of compression-ended parents, exclude
        explicit branch/delegate/tool children, and prefer children that are
        themselves continuing the compression chain (``end_reason='compression'``)
        or still live over stale closed siblings such as ``ws_orphan_reap``.
        Returns the latest continuation tip, or the input id when no
        continuation exists.
        """
        current = session_id
        seen = {current} if current else set()
        # Bound the walk defensively — compression chains this deep are
        # pathological and shouldn't happen in practice. 100 = plenty.
        for _ in range(100):
            with self._lock:
                cursor = self._conn.execute(
                    f"""
                    SELECT child.id
                    FROM sessions parent
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.id = ?
                      AND parent.end_reason = 'compression'
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
                      AND COALESCE(child.source, '') != 'tool'
                    ORDER BY
                      CASE
                        WHEN child.end_reason = 'compression' THEN 0
                        WHEN child.ended_at IS NULL THEN 1
                        ELSE 2
                      END,
                      {self._presentation_session_last_active_sql("child")} DESC,
                      child.started_at DESC,
                      child.id DESC
                    LIMIT 1
                    """,
                    (current,),
                )
                row = cursor.fetchone()
            if row is None:
                return current
            child_id = row["id"]
            if not child_id or child_id in seen:
                return current
            seen.add(child_id)
            current = child_id
        return current
