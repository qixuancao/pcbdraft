"""Database size and automatic maintenance workflows for SessionDB.

``SessionMaintenanceMixin`` is composed into ``SessionDB`` and owns no
connection state. The host supplies SQLite access, pruning/search operations,
metadata storage, and dynamic compatibility hooks for time and logging. This
module deliberately does not import ``session_db`` so the composition root
remains acyclic.
"""
# mypy: disable-error-code="attr-defined,has-type"

# Maintenance is an explicit best-effort boundary: size probes, FTS merging,
# checkpoints, pruning, and archiving must not block startup or CLI recovery.
# ruff: noqa: BLE001

from __future__ import annotations

from pathlib import Path
from typing import Any


class SessionMaintenanceMixin:
    """Measure, compact, prune, and archive the durable session store."""

    def logical_size_bytes(self) -> int | None:
        """Database size in bytes as SQLite itself accounts for it.

        ``page_count * page_size`` — the size the main DB file will have once
        the WAL is checkpointed back into it.

        Prefer this over ``os.path.getsize(db_path)`` when reporting the effect
        of a VACUUM. In WAL mode a VACUUM's rewrite lands in the ``-wal`` file,
        and the checkpoint that folds it back is refused while any other
        connection (a live gateway) holds a read-mark. Until that happens the
        main file on disk still carries its pre-VACUUM size and keeps growing,
        so a stat()-based before/after delta understates the win and can go
        negative — the "reclaimed -3820.1 MB" report on a database that had
        actually shrunk 60%.

        Returns None if the pragmas cannot be read.
        """
        try:
            with self._lock:
                if self._conn is None:
                    return None
                page_count = self._conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
            return int(page_count) * int(page_size)
        except Exception:
            self._maintenance_log_debug("Could not read logical DB size", exc_info=True)
            return None

    def vacuum(self) -> int:
        """Run VACUUM to reclaim disk space after large deletes.

        SQLite does not shrink the database file when rows are deleted —
        freed pages just get reused on the next insert. After a prune that
        removed hundreds of sessions, the file stays bloated unless we
        explicitly VACUUM.

        VACUUM rewrites the entire DB, so it's expensive (seconds per
        100MB) and cannot run inside a transaction. It also acquires an
        exclusive lock, so callers must ensure no other writers are
        active. Safe to call at startup before the gateway/CLI starts
        serving traffic.

        FTS5 segments are merged first via :meth:`optimize_fts` so the
        subsequent VACUUM reclaims the pages freed by the merge. This is a
        layout-only optimization — search results are unchanged.

        Returns the number of FTS indexes that were optimized (0 if the
        merge step failed or no FTS tables exist).
        """
        # Merge FTS5 segments before VACUUM so the freed pages are returned
        # to the OS in the same pass. optimize_fts() manages its own lock.
        optimized = 0
        try:
            optimized = self.optimize_fts()
        except Exception:
            self._maintenance_log_warning(
                "FTS optimize before VACUUM failed", exc_info=True
            )
        # VACUUM cannot be executed inside a transaction.
        with self._lock:
            # Best-effort WAL checkpoint first, then VACUUM. PASSIVE, not
            # TRUNCATE: a manual session vacuum runs in a transient
            # CLI process, and a TRUNCATE reset here would race a live gateway
            # writer and tear B-tree pages (#45383). VACUUM folds the WAL back
            # itself; journal_size_limit bounds the file.
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                self._maintenance_log_debug(
                    "WAL checkpoint (PASSIVE) before VACUUM failed",
                    exc_info=True,
                )
            self._conn.execute("VACUUM")
            # ...and again afterwards. VACUUM rewrites every page THROUGH the
            # WAL, so the pre-VACUUM checkpoint above does nothing for the
            # slack VACUUM itself creates: on a 3.0 GB database it left a
            # 3.07 GB state.db-wal behind, so `sessions optimize` reported
            # "reclaimed -11.2 MB" while actually consuming 3 GB of disk and
            # filling the host to 100%. Truncating here is what makes the
            # command a net win instead of a net loss on large databases.
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                self._maintenance_log_debug(
                    "WAL checkpoint (TRUNCATE) after VACUUM failed",
                    exc_info=True,
                )
        return optimized

    def maybe_auto_prune_and_vacuum(
        self,
        retention_days: int = 90,
        min_interval_hours: int = 24,
        vacuum: bool = True,
        sessions_dir: Path | None = None,
        min_vacuum_interval_days: int = 30,
    ) -> dict[str, Any]:
        """Idempotent auto-maintenance: prune inactive sessions + optional VACUUM.

        Records the last run timestamp in state_meta so subsequent calls
        within ``min_interval_hours`` no-op. VACUUM has its own, typically
        longer, throttle controlled by ``min_vacuum_interval_days`` so routine
        pruning does not repeatedly rewrite the database. Designed to be
        called once at startup from long-lived entrypoints (CLI, gateway, cron
        scheduler).

        When *sessions_dir* is provided, on-disk transcript files
        (``.json`` / ``.jsonl`` / ``request_dump_*``) for pruned sessions
        are removed as part of the same sweep (issue #3015).

        Never raises. On any failure, logs a warning and returns a dict with
        ``"error"`` set.

        Returns a dict with keys:
          - ``"skipped"`` (bool) — true if within min_interval_hours of last run
          - ``"pruned"`` (int)   — number of sessions deleted
          - ``"vacuumed"`` (bool) — true if VACUUM ran
          - ``"error"`` (str, optional) — present only on failure
        """
        result: dict[str, Any] = {"skipped": False, "pruned": 0, "vacuumed": False}
        try:
            # Skip if another process/call did maintenance recently.
            last_raw = self.get_meta("last_auto_prune")
            now = self._maintenance_now()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError):
                    pass  # corrupt meta; treat as no prior run

            pruned = self.prune_sessions(
                older_than_days=retention_days,
                sessions_dir=sessions_dir,
            )
            result["pruned"] = pruned

            # Only VACUUM if we actually freed rows, and no more often than
            # once every min_vacuum_interval_days -- a large prune can free
            # enough pages that pruned > 0 fires on every subsequent startup.
            # VACUUM on this DB's size is not cheap: it holds an exclusive lock
            # for the full rewrite.
            last_vacuum_raw = self.get_meta("last_vacuum")
            vacuum_due = True
            if last_vacuum_raw:
                try:
                    vacuum_due = (
                        now - float(last_vacuum_raw)
                    ) >= min_vacuum_interval_days * 86400
                except (TypeError, ValueError):
                    vacuum_due = True
            if vacuum and pruned > 0 and vacuum_due:
                try:
                    self.vacuum()
                    result["vacuumed"] = True
                    self.set_meta("last_vacuum", str(now))
                except Exception:
                    self._maintenance_log_warning(
                        "state.db VACUUM failed", exc_info=True
                    )

            # Record the attempt even if pruned == 0, so we don't retry
            # every startup within the min_interval_hours window.
            self.set_meta("last_auto_prune", str(now))

            if pruned > 0:
                self._maintenance_log_info(
                    "state.db auto-maintenance: pruned %d session(s) inactive for %d days%s",
                    pruned,
                    retention_days,
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            # Maintenance must never block startup. Log and return error marker.
            self._maintenance_log_warning(
                "state.db auto-maintenance failed", exc_info=True
            )
            result["error"] = str(exc)

        return result

    def maybe_auto_archive(
        self,
        idle_days: float = 3,
        min_interval_hours: int = 24,
        exclude_pinned: bool = True,
    ) -> dict[str, Any]:
        """Idempotent auto-archive: soft-hide sessions idle for ``idle_days``.

        Sibling of :meth:`maybe_auto_prune_and_vacuum` but non-destructive —
        it archives (hides) rather than deletes, and ages on last activity
        (see :meth:`archive_stale_sessions`) rather than creation. Records the
        last run in ``state_meta['last_auto_archive']`` so calls within
        ``min_interval_hours`` no-op; safe to call opportunistically (startup
        hooks, or when the Desktop backend lists sessions).

        Never raises. Returns a dict with:
          - ``"skipped"`` (bool) — within min_interval_hours of last run
          - ``"archived"`` (int) — sessions archived this run
          - ``"error"`` (str, optional) — present only on failure
        """
        result: dict[str, Any] = {"skipped": False, "archived": 0}
        try:
            last_raw = self.get_meta("last_auto_archive")
            now = self._maintenance_now()
            if last_raw:
                try:
                    if now - float(last_raw) < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError):
                    pass  # corrupt meta; treat as no prior run

            archived = self.archive_stale_sessions(
                idle_days, exclude_pinned=exclude_pinned
            )
            result["archived"] = archived

            # Record even a zero-archive run so we don't re-sweep every call
            # within the interval window.
            self.set_meta("last_auto_archive", str(now))

            if archived > 0:
                self._maintenance_log_info(
                    "state.db auto-archive: archived %d session(s) idle >= %s days",
                    archived,
                    idle_days,
                )
        except Exception as exc:
            self._maintenance_log_warning("state.db auto-archive failed", exc_info=True)
            result["error"] = str(exc)

        return result
