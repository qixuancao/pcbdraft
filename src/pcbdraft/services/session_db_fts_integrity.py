"""FTS capability, schema integrity, and runtime recovery for SessionDB.

The mixin owns FTS feature probes and the repair decisions used during schema
initialization and runtime writes. Search queries, connection lifecycle, and
session-domain operations remain in their existing modules.

This module never imports :mod:`pcbdraft.services.session_db`. The legacy
module supplies late-bound hooks so its established constants, helper
functions, logger, and monkeypatch paths remain authoritative.
"""
# mypy: disable-error-code="attr-defined,has-type"

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionFTSIntegrityHooks:
    fts_triggers: Callable[[], tuple[str, ...]]
    fts_cjk_triggers: Callable[[], tuple[str, ...]]
    fts_stale_key: Callable[[], str]
    fts_cjk_stale_key: Callable[[], str]
    fts_cjk_table_sql: Callable[[], str]
    fts_cjk_trigger_sql: Callable[[], str]
    cjk_so_path: Callable[[], Path]
    is_malformed_db_error: Callable[[BaseException], bool]
    log_info: Callable[..., None]
    log_warning: Callable[..., None]
    log_error: Callable[..., None]
    log_exception: Callable[..., None]


_fts_integrity_hooks: SessionFTSIntegrityHooks | None = None


def configure_fts_integrity_hooks(hooks: SessionFTSIntegrityHooks) -> None:
    """Install late-bound host hooks used by the FTS integrity mixin."""

    global _fts_integrity_hooks
    _fts_integrity_hooks = hooks


def _hooks() -> SessionFTSIntegrityHooks:
    hooks = _fts_integrity_hooks
    if hooks is None:
        raise RuntimeError("SessionDB FTS integrity hooks are not configured")
    return hooks


def _fts_triggers() -> tuple[str, ...]:
    return _hooks().fts_triggers()


def _fts_cjk_triggers() -> tuple[str, ...]:
    return _hooks().fts_cjk_triggers()


def _fts_stale_key() -> str:
    return _hooks().fts_stale_key()


def _fts_cjk_stale_key() -> str:
    return _hooks().fts_cjk_stale_key()


def _fts_cjk_table_sql() -> str:
    return _hooks().fts_cjk_table_sql()


def _fts_cjk_trigger_sql() -> str:
    return _hooks().fts_cjk_trigger_sql()


def _cjk_so_path() -> Path:
    return _hooks().cjk_so_path()


def _is_malformed_db_error(exc: BaseException) -> bool:
    return _hooks().is_malformed_db_error(exc)


def _log_info(message: str, *args, **kwargs) -> None:
    _hooks().log_info(message, *args, **kwargs)


def _log_warning(message: str, *args, **kwargs) -> None:
    _hooks().log_warning(message, *args, **kwargs)


def _log_error(message: str, *args, **kwargs) -> None:
    _hooks().log_error(message, *args, **kwargs)


def _log_exception(message: str, *args, **kwargs) -> None:
    _hooks().log_exception(message, *args, **kwargs)


class SessionFTSIntegrityMixin:
    @staticmethod
    def _is_fts5_unavailable_error(exc: sqlite3.OperationalError) -> bool:
        err = str(exc).lower()
        if "no such module" in err and "fts5" in err:
            return True
        # SQLite builds that have FTS5 but lack the optional trigram tokenizer
        # raise "no such tokenizer: trigram" instead of "no such module".
        # Scope to trigram specifically to avoid masking unrelated tokenizer errors.
        if "no such tokenizer: trigram" in err:
            return True
        # The cjk_unicode61 tokenizer is a loadable extension — a process
        # that couldn't load it sees the same capability-error shape.
        return "no such tokenizer: cjk_unicode61" in err

    @staticmethod
    def _is_trigram_unavailable_error(exc: sqlite3.OperationalError) -> bool:
        """True when only an optional tokenizer is missing (FTS5 itself works).

        Covers the built-in trigram tokenizer (needs SQLite >= 3.34) and the
        loadable cjk_unicode61 tokenizer — both mean "this one index can't be
        served here", never "disable FTS".
        """
        err = str(exc).lower()
        return (
            "no such tokenizer: trigram" in err
            or "no such tokenizer: cjk_unicode61" in err
        )

    @staticmethod
    def _db_has_legacy_inline_fts(cursor: sqlite3.Cursor) -> bool:
        """True when messages_fts exists in ANY pre-v23 shape.

        v23's messages_fts is external-content over THREE real columns
        (content, tool_name, tool_calls). Every pre-v23 shape lacks the
        tool_name/tool_calls columns — whether the old inline single-column
        form (v11..v22) or the even older external-content single-column form
        (v10-era, pre-#16751). We therefore detect "needs optimize" as "the
        stored CREATE lacks the tool_name column", which is the precise v23
        marker and correctly catches BOTH legacy variants.

        Returns False when messages_fts doesn't exist yet (fresh DB mid-init):
        the post-migration FTS setup block will create it in the v23 shape.
        """
        row = cursor.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'messages_fts'"
        ).fetchone()
        if row is None:
            return False
        sql = (row[0] if not isinstance(row, sqlite3.Row) else row["sql"]) or ""
        # The v23 table declares tool_name/tool_calls columns. Their absence
        # means a legacy shape that doesn't index tool metadata → optimize.
        return "tool_name" not in sql

    def _warn_trigram_unavailable(self, exc: sqlite3.OperationalError) -> None:
        """Log once that the trigram tokenizer is missing; base FTS5 stays enabled."""
        if getattr(self, "_trigram_unavailable_warned", False):
            return
        self._trigram_unavailable_warned = True
        _log_info(
            "SQLite trigram tokenizer unavailable for %s "
            "(requires SQLite >= 3.34, this build is %s); "
            "CJK/substring search will fall back to LIKE: %s",
            self.db_path,
            sqlite3.sqlite_version,
            exc,
        )

    def _warn_fts5_unavailable(self, exc: sqlite3.OperationalError) -> None:
        self._fts_enabled = False
        if self._fts_unavailable_warned:
            return
        self._fts_unavailable_warned = True
        _log_warning(
            "SQLite FTS5 unavailable for %s; full-text session search "
            "disabled. Run `pcbdraft doctor` and install a Python build with a "
            "current Python (managed uv guarantees FTS5). "
            "(underlying error: %s)",
            self.db_path,
            exc,
        )

    def _ensure_fts_cjk_schema(self, cursor) -> None:
        """Create / repair / self-heal the CJK-bigram index surface.

        ``cursor`` may be a Cursor or a Connection (both expose execute /
        executescript). Called only for v23-shape DBs with the base FTS
        surface healthy. Sets ``self._fts_cjk_available``. Never raises;
        every failure mode degrades to "no cjk index" (trigram/LIKE routing
        keeps working).

        Cases:
          tokenizer loaded, table absent  → create. Empty DB: index is
              complete by construction (triggers cover everything). Populated
              DB: set the cjk backfill markers so the id-gated triggers stay
              correct and `optimize-storage` can backfill; the index is NOT
              served until the backfill completes.
          tokenizer loaded, table present → ensure triggers (recreates any
              dropped by a tokenizer-less process), honour the stale
              breadcrumb (serve only when absent and no backfill pending).
          tokenizer NOT loaded, table present with live triggers → drop the
              cjk triggers so message INSERTs don't fail at trigger time,
              and leave the stale breadcrumb (#self-heal). The table itself
              stays for a later capable open to rebuild.
        """
        cjk_present = bool(
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'messages_fts_cjk'"
            ).fetchone()
        )

        if not self._fts_cjk_loaded:
            if cjk_present:
                live = [
                    r[0]
                    for r in cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "  # noqa: S608 - fixed trigger allowlist
                        f"AND name IN ({','.join('?' for _ in _fts_cjk_triggers())})",
                        _fts_cjk_triggers(),
                    ).fetchall()
                ]
                if live:
                    # Self-heal: this process cannot tokenize, so every
                    # message INSERT would die inside the cjk trigger.
                    # Breadcrumb FIRST (crash between the two statements is
                    # merely conservative), then drop.
                    _log_warning(
                        "messages_fts_cjk triggers present but the "
                        "cjk_unicode61 tokenizer is unavailable (%s) — "
                        "dropping the cjk triggers so message writes keep "
                        "working. CJK search falls back to trigram/LIKE; "
                        "run `pcbdraft doctor` to inspect support on a host "
                        "with the extension to rebuild.",
                        _cjk_so_path(),
                    )
                    cursor.execute(
                        "INSERT INTO state_meta (key, value) VALUES (?, '1') "
                        "ON CONFLICT(key) DO UPDATE SET value = '1'",
                        (_fts_cjk_stale_key(),),
                    )
                    for trig in live:
                        cursor.execute(f"DROP TRIGGER IF EXISTS {trig}")
            self._fts_cjk_available = False
            return

        try:
            cursor.executescript(_fts_cjk_table_sql())
            if not cjk_present:
                # Freshly created. An empty DB's index is complete by
                # construction (triggers will cover every future row); a
                # populated DB (e.g. a v23 install predating the cjk index)
                # gets the dedicated marker pair so the id-gated triggers
                # keep NEW rows indexed while old rows await the
                # `optimize-storage` backfill. Either way any old stale
                # breadcrumb refers to a table that no longer exists.
                cursor.execute(
                    "DELETE FROM state_meta WHERE key = ?",
                    (_fts_cjk_stale_key(),),
                )
                n_msgs = cursor.execute(
                    "SELECT COUNT(*) FROM messages WHERE role <> 'tool'"
                ).fetchone()[0]
                if n_msgs > 0:
                    hw = cursor.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM messages"
                    ).fetchone()[0]
                    for k, v in (
                        ("fts_cjk_rebuild_high_water", str(hw)),
                        ("fts_cjk_rebuild_progress", "0"),
                    ):
                        cursor.execute(
                            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (k, v),
                        )
            stale = cursor.execute(
                "SELECT 1 FROM state_meta WHERE key = ?",
                (_fts_cjk_stale_key(),),
            ).fetchone()
            if stale:
                # A tokenizer-less process dropped the triggers at some
                # unknown point — the index has a gap of unknown extent.
                # Do NOT reinstall triggers (an external-content 'delete'
                # for an unindexed rowid corrupts the index); the next
                # `optimize-storage` run rebuilds from scratch.
                self._fts_cjk_available = False
                return
            cursor.executescript(_fts_cjk_trigger_sql())
            backfill_pending = cursor.execute(
                "SELECT 1 FROM state_meta "
                "WHERE key = 'fts_cjk_rebuild_high_water' LIMIT 1"
            ).fetchone()
            self._fts_cjk_available = not backfill_pending
        except sqlite3.OperationalError:
            # Includes "no such tokenizer: cjk_unicode61" if the extension
            # loaded but registration failed — degrade to trigram/LIKE.
            _log_warning(
                "messages_fts_cjk ensure failed; CJK search stays on trigram/LIKE",
                exc_info=True,
            )
            self._fts_cjk_available = False

    @staticmethod
    def _drop_fts_triggers(cursor: sqlite3.Cursor) -> None:
        for trigger in _fts_triggers():
            try:
                cursor.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            except sqlite3.OperationalError:
                pass

    def _ensure_fts_schema(
        self,
        cursor: sqlite3.Cursor,
        table_name: str,
        ddl: str,
    ) -> bool:
        status = self._fts_table_probe(cursor, table_name)
        if status is None:
            return False
        try:
            # Run even when the virtual table exists so any dropped or missing
            # triggers are recreated after a previous no-FTS5 runtime disabled
            # them to keep message writes working.
            cursor.executescript(ddl)
            return True
        except sqlite3.OperationalError as exc:
            if not self._is_fts5_unavailable_error(exc):
                raise
            # Only disable FTS entirely when the whole FTS5 module is missing.
            # A missing specific tokenizer (e.g. trigram) means only that
            # particular table cannot be created — the base FTS5 table is fine.
            if self._is_trigram_unavailable_error(exc):
                self._warn_trigram_unavailable(exc)
            else:
                self._warn_fts5_unavailable(exc)
            return False

    @staticmethod
    def _is_fts_write_corruption_error(exc: sqlite3.DatabaseError) -> bool:
        """True for the error class a corrupt FTS index raises on writes.

        The message varies by SQLite version: older builds raise the generic
        ``database disk image is malformed`` (covered by
        ``is_malformed_db_error``); newer builds (e.g. ubuntu-latest CI)
        raise the FTS5-specific ``fts5: corrupt structure record for table
        "messages_fts"``. Both mean the same thing for the write path: the
        canonical rows are fine, the FTS shadow tables are not.
        """
        if _is_malformed_db_error(exc):
            return True
        msg = str(exc).lower()
        return "fts5" in msg and "corrupt" in msg

    def _try_runtime_fts_rebuild(self, exc: sqlite3.DatabaseError) -> bool:
        """One-shot in-place FTS rebuild after a corrupt-index write failure.

        Returns True when a rebuild was performed and the failed write should
        be retried; False when the error isn't the FTS-corruption class, FTS
        is disabled, or a rebuild was already attempted for this instance.

        Delegates to :meth:`rebuild_fts` (the FTS5 ``'rebuild'`` command —
        index rewritten from the canonical messages table, zero message-row
        mutation). Safe to call from ``_execute_write``'s except path: the
        failed transaction was rolled back and ``self._lock`` released before
        the exception propagated, and ``rebuild_fts`` re-acquires it.
        E2E-verified: a corrupted ``messages_fts_data`` shadow table rejects
        every append; after the in-place rebuild the same append succeeds and
        search works again.
        """
        if self._fts_runtime_rebuild_attempted:
            return False
        if not self._fts_enabled:
            return False
        if not self._is_fts_write_corruption_error(exc):
            return False
        self._fts_runtime_rebuild_attempted = True
        _log_warning(
            "state.db write failed with an FTS-corruption error (%s) — "
            "attempting one-shot in-place FTS rebuild; canonical message "
            "rows are preserved.",
            exc,
        )
        try:
            rebuilt = self.rebuild_fts()
        except Exception:  # noqa: BLE001 - recovery must fail closed on any rebuild fault
            _log_exception(
                "In-place FTS rebuild failed; the database needs the "
                "full offline repair path (repair_state_db_schema).",
            )
            return False
        if not rebuilt:
            _log_error(
                "In-place FTS rebuild made no progress; the database needs "
                "the full offline repair path (repair_state_db_schema)."
            )
            return False
        _log_warning(
            "state.db FTS indexes rebuilt in place (%d); retrying the failed write.",
            rebuilt,
        )
        return True

    def _enter_fts_fail_open(self, exc: sqlite3.DatabaseError) -> bool:
        """Detach corrupt FTS indexes so canonical writes can continue.

        The stale breadcrumb and trigger removal commit atomically. Its
        ordering is load-bearing: after triggers are absent, new canonical
        rows create an index gap of unknown extent, so another process must
        never reinstall the triggers without first rebuilding every row.
        """
        if not self._fts_enabled or not self._is_fts_write_corruption_error(exc):
            return False

        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(
                        "INSERT INTO state_meta (key, value) VALUES (?, '1') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (_fts_stale_key(),),
                    )
                    cjk_triggers_present = self._conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "  # noqa: S608 - fixed trigger allowlist
                        f"AND name IN ({','.join('?' for _ in _fts_cjk_triggers())}) "
                        "LIMIT 1",
                        _fts_cjk_triggers(),
                    ).fetchone()
                    if cjk_triggers_present:
                        self._conn.execute(
                            "INSERT INTO state_meta (key, value) VALUES (?, '1') "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (_fts_cjk_stale_key(),),
                        )
                    self._drop_all_fts_triggers(self._conn.cursor())
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
        except sqlite3.Error as detach_exc:
            _log_error(
                "Could not detach corrupt FTS indexes; canonical write still "
                "cannot proceed: %s",
                detach_exc,
            )
            return False

        self._fts_stale = True
        self._fts_enabled = False
        self._trigram_available = False
        self._fts_cjk_available = False
        _log_error(
            "state.db FTS indexes remain corrupt (%s); disabled FTS sync and "
            "retrying the canonical write. Search temporarily uses LIKE until "
            "a later SessionDB open rebuilds the indexes.",
            exc,
        )
        return True

    def _has_fts_trash(self, conn) -> bool:
        """True when demoted v22 shadow tables are still awaiting teardown.
        Caller must hold ``self._lock`` (or pass a migration-time cursor)."""
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE ? ESCAPE '\\' LIMIT 1",
                (self._FTS_TRASH_PREFIX.replace("_", "\\_") + "%",),
            ).fetchone()
        )

    # =========================================================================
    # Session lifecycle
