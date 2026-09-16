"""SQLite runtime policy shared by session and auxiliary databases.

This module owns journal-mode negotiation, runtime PRAGMAs, and persistence
error classification.  It deliberately has no dependency on the SessionDB
schema or repair implementation so auxiliary stores can reuse the runtime
policy without importing the session-store monolith.
"""

import errno
import logging
import sqlite3
import sys
import threading
import time

from pcbdraft.core.runtime_environment import _exception_info_without_values
from pcbdraft.interfaces.tui.sqlite_runtime import (
    is_sqlite_wal_reset_vulnerable as _is_sqlite_wal_reset_vulnerable,
)

logger = logging.getLogger(__name__)

_WAL_INCOMPAT_MARKERS = (
    "locking protocol",
    "not authorized",
    "disk i/o error",
)
_WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

_wal_fallback_warned_paths: set[str] = set()
_wal_fallback_warned_lock = threading.Lock()
_wal_reset_bug_warned_paths: set[str] = set()
_wal_reset_bug_warned_lock = threading.Lock()


class CompressionSessionClosedError(RuntimeError):
    """A durable write targeted a parent already closed by compression."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(
            f"Session {session_id!r} is closed by compression; "
            "adopt its live continuation before appending messages"
        )


class CompressionSessionBusyError(RuntimeError):
    """A non-owner tried to write while compression owns the session."""


class SessionCompressionInProgressError(CompressionSessionBusyError):
    """A concurrent writer collided with a *live* compression lock.

    Split out from :class:`CompressionSessionBusyError` because the two
    conditions that class covers need opposite handling. This one is
    transient: a healthy compressor holds the session for a few seconds and
    the lock row carries its own ``expires_at``, so the write can simply wait
    (see ``_execute_write``'s patience loop). The other case, a compressor
    discovering its own lease is gone, is permanent and must fail fast rather
    than spin out the whole patience budget.

    Subclassing keeps every existing ``except CompressionSessionBusyError``
    handler working unchanged.
    """


class SessionTurnLeaseLostError(RuntimeError):
    """A transcript write presented a turn-lease holder that no longer owns it.

    Fail-fast fencing: do not retry inside ``_execute_write``. The caller
    either still thinks it owns the conversation after expiry/reclaim, or
    the lease row is gone. A later writer may already be persisting a
    newer turn; landing this write would interleave a stale reply.
    """


def _on_disk_journal_mode(conn: sqlite3.Connection) -> str | None:
    """Read the journal mode from the SQLite DB header on disk.

    Returns the mode string (e.g. ``"wal"``, ``"delete"``), or ``None``
    if the value cannot be determined (new DB, or PRAGMA read failed).

    A PRAGMA read can fail transiently with ``disk i/o error`` on
    virtualized block devices (XFS on cloud hosts).  Treating that as
    "mode unknown" pushes callers onto their fail-closed unknown-mode
    branch even though the on-disk mode is perfectly readable a few
    milliseconds later.  Retry the read a few times before giving up:
    transient EIO clears, deterministic unsupported-filesystem errors do
    not.  ``None`` is still returned on final failure so the caller's
    existing "unknown → refuse to downgrade" logic applies.
    """
    last_exc: Exception | None = None
    for _ in range(4):
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "disk i/o error" not in str(exc).lower():
                return None
            time.sleep(0.05)
            continue
        if row is None:
            return None
        mode = row[0]
        if isinstance(mode, bytes):  # defensive: sqlite3 occasionally returns bytes
            try:
                mode = mode.decode("ascii")
            except UnicodeDecodeError:
                return None
        return str(mode).strip().lower() if mode is not None else None
    if last_exc is not None:
        logger.debug(
            "_on_disk_journal_mode: retries exhausted on disk read (%s)", last_exc
        )
    return None


def _apply_wal_size_limit(conn: sqlite3.Connection) -> None:
    """Bound the WAL so it returns space to the OS after big transactions.

    SQLite's default ``journal_size_limit`` is -1 (unlimited): after a
    checkpoint the WAL file is *reused in place* and never truncated, so
    ``state.db-wal`` permanently retains the high-water mark of the largest
    transaction ever run against it.

    A single bulk operation is enough to strand gigabytes. Observed on a
    3.0 GB ``state.db``: offline session optimization (FTS merge + VACUUM)
    rewrites every page through the WAL, leaving a **3.07 GB**
    ``state.db-wal`` sitting next to the database indefinitely — the host
    went from 6.9 GB free to 772 MB (100% full) and stayed there, because
    nothing shrinks the WAL back down. An explicit
    ``PRAGMA wal_checkpoint(TRUNCATE)`` reclaimed the full 3.07 GB, which
    confirms the space was pure slack rather than live data.

    That also makes the maintenance command self-defeating on exactly the
    databases that need it most: the larger the DB, the larger the WAL it
    strands, so ``optimize`` can consume more disk than it frees.

    ``journal_size_limit`` makes SQLite truncate the WAL back to the limit
    at each checkpoint. 64 MiB is comfortably above normal transaction
    sizes (so steady-state commits never pay a truncate) while capping the
    stranded slack at a bounded, predictable figure.

    The kanban database module already bounds its WAL growth with
    ``wal_autocheckpoint=100``; the session store — by far the larger
    database — had no equivalent.

    Best-effort: never raises. A failure here only costs disk slack, and
    must not prevent the database from opening.
    """
    try:
        conn.execute(f"PRAGMA journal_size_limit={_WAL_SIZE_LIMIT_BYTES}")
    except sqlite3.OperationalError as exc:  # pragma: no cover - defensive
        logger.debug("journal_size_limit not applied: %s", exc)


def _apply_macos_checkpoint_barrier(conn: sqlite3.Connection) -> None:
    """Enable ``PRAGMA checkpoint_fullfsync`` on macOS (no-op elsewhere).

    On Darwin, ``synchronous=FULL`` (the WAL default) issues a plain
    ``fsync()``, which Apple documents does *not* guarantee that data
    has reached stable storage or that writes are not reordered — see
    the ``fsync(2)`` man page.  SQLite's WAL corruption-safety guarantee
    assumes the OS honors the fsync write barrier; macOS does not unless
    the app uses ``F_FULLFSYNC``.

    During a launchd *system* shutdown/reboot the OS page cache is
    dropped (effectively a power-loss event for in-flight pages), so a
    WAL checkpoint whose ``fsync()`` "reported" durable may never have
    hit the platter — corrupting ``state.db`` with a malformed image.
    This is the trigger in issue #30636 ("SIGTERM during launchd
    shutdown under high load"), distinct from a plain in-session kill
    (which the page cache survives and SQLite recovers from).

    ``checkpoint_fullfsync=1`` forces an ``F_FULLFSYNC`` barrier only at
    checkpoint boundaries — where WAL frames land in the main DB — so the
    cost amortizes to roughly +0.1 ms/commit (vs ~+4 ms for the broader
    ``fullfsync=1`` that flushes on every commit's WAL sync).  Guarded by
    ``sys.platform == "darwin"`` because ``F_FULLFSYNC`` is macOS-only;
    on other platforms the PRAGMA is a no-op, so we skip it entirely.

    Best-effort: never raises.
    """
    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA checkpoint_fullfsync=1")
    except sqlite3.OperationalError:
        pass


def _enforce_macos_synchronous_full(conn: sqlite3.Connection) -> None:
    """Enforce ``PRAGMA synchronous=FULL`` on macOS to prevent btree corruption.

    On Darwin, the default ``synchronous=NORMAL`` only calls ``fsync()``,
    which Apple's fsync(2) man page explicitly states does *not* guarantee
    data-on-platter or write-ordering. During a WAL checkpoint race with
    process termination (e.g., launchd shutdown), this can leave the main
    DB with half-written btree pages → ``btreeInitPage error 11``.

    WAL mode's durability guarantee assumes the OS honors fsync barriers;
    macOS does not unless we explicitly set ``synchronous=FULL``, which issues
    a real ``fsync()`` on every transaction commit.  The ``F_FULLFSYNC``
    barrier at checkpoint boundaries is handled separately by
    :func:`_apply_macos_checkpoint_barrier`.

    This function is called after any successful WAL activation (either
    from ``apply_wal_with_fallback()`` setting a fresh WAL or when probing
    an existing WAL mode). It ensures macOS connections always use FULL
    synchronous mode, even if a prior connection set ``synchronous=NORMAL``.

    Best-effort: never raises.
    """
    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA synchronous=FULL")
    except sqlite3.OperationalError:
        pass


def is_sqlite_wal_reset_vulnerable(
    version_info: tuple | None = None,
) -> bool:
    """Return True when the linked SQLite library has the WAL-reset bug.

    Upstream documents the bug in versions 3.7.0 through 3.51.2, fixed in
    3.51.3+, with backports 3.50.7 and 3.44.6:
    https://sqlite.org/wal.html#walresetbug

    Pre-WAL libraries (< 3.7.0) cannot hit the race and are treated as safe.
    """
    info = version_info if version_info is not None else sqlite3.sqlite_version_info
    return _is_sqlite_wal_reset_vulnerable(info)


def sqlite_source_id() -> str:
    """Return ``sqlite_source_id()``, or an empty string when unavailable."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            row = conn.execute("SELECT sqlite_source_id()").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    if not row or row[0] is None:
        return ""
    return str(row[0])


def resolve_journal_mode() -> str:
    """Return the configured journal mode (``wal`` or ``delete``).

    ``database.journal_mode`` in config.yaml is the canonical operator
    setting. ``wal`` remains the default; use ``delete`` when the backing
    filesystem does not provide WAL-safe durability (for example macOS
    virtiofs, NFS, or SMB). Invalid or malformed values fail safely to the
    existing default.
    """
    try:
        from pcbdraft.model.configuration import load_config_readonly

        config = load_config_readonly() or {}
        database = config.get("database", {})
        if not isinstance(database, dict):
            return "wal"
        raw = database.get("journal_mode", "wal")
    except Exception:
        logger.debug(
            "Journal mode config unavailable; using WAL",
            exc_info=_exception_info_without_values(),
        )
        return "wal"

    if not isinstance(raw, str):
        return "wal"
    mode = raw.strip().lower()
    return mode if mode in ("wal", "delete") else "wal"


class WalUnsupportedError(sqlite3.OperationalError):
    """Raised by :func:`apply_wal_with_fallback` when ``require_wal=True`` and
    the filesystem cannot provide WAL journal mode.

    Covers both shapes of WAL refusal on network filesystems (NFS / SMB / FUSE
    / the AgentFS NFS overlay): SQLite *raising* ``SQLITE_PROTOCOL`` ("locking
    protocol"), and the quieter macOS-NFS case where ``PRAGMA journal_mode=WAL``
    silently returns the still-effective mode without raising.  Subclasses
    ``sqlite3.OperationalError`` so existing ``except sqlite3.OperationalError``
    DB-init handling still catches it, while callers that specifically mandate
    WAL can catch this narrower type.
    """


def apply_wal_with_fallback(
    conn: sqlite3.Connection,
    *,
    db_label: str = "state.db",
    require_wal: bool = False,
) -> str:
    """Set ``journal_mode=WAL`` on ``conn``, falling back to DELETE on failure.

    Returns the journal mode actually set (``"wal"`` or ``"delete"``).

    On WAL-incompatible filesystems (NFS, SMB, some FUSE, ZFS), SQLite either
    raises ``OperationalError("locking protocol")`` /
    ``OperationalError("disk I/O error")`` or — on macOS NFS / SMB /
    the AgentFS NFS overlay — silently refuses the switch and leaves the DB in
    DELETE.  Either way the degradation is logged at ERROR level (it is a real
    loss of concurrency — a write blocks concurrent readers — not a cosmetic
    warning) and, by default, the function falls back to DELETE (the pre-WAL
    default, which works on NFS and ZFS) so the feature keeps working.

    On SQLite builds that still contain the WAL-reset corruption bug
    (issue #69784), refuse to enable WAL on fresh / non-WAL databases
    (prefer DELETE).  If the on-disk DB is already WAL, keep WAL and warn
    — never live-downgrade under possible concurrent openers.

    This gate (#70055) is deliberately RETAINED. An earlier revision of the
    lock-cancellation fix (#71724) reverted it on the theory that DELETE was
    "the mode that corrupts", but that comparison was confounded: the clean
    WAL result came from SQLite 3.53.1, which carries BOTH the WAL-reset fix
    AND 3.51.0's defenses against close()-broken POSIX locks, so it says
    nothing about 3.50.4.  Re-measured on the actually-bundled 3.50.4 with
    the lock fix in place, WAL and DELETE are both clean (0/3 each) — i.e.
    there is no evidence that WAL is safer here, and upstream still documents
    the WAL-reset bug as real through 3.51.2 with serious consequences.  Until
    a fixed runtime is delivered, keep new databases out of WAL.

    Callers that genuinely require WAL concurrency (and would rather fail loudly
    than run silently degraded) pass ``require_wal=True``; the function then
    raises :class:`WalUnsupportedError` instead of returning ``"delete"``.  All
    current callers deliberately keep the default ``require_wal=False`` so
    NFS-homed installs keep working.

    The ERROR is deduplicated per ``db_label``: repeated connections to the
    same underlying DB (e.g. kanban_db.connect() which is called on every
    kanban operation) log once per process, not once per call.  Different
    db_labels log independently, so state.db and kanban.db each get one error
    on the same NFS mount.

    Shared by :class:`SessionDB` and the kanban connection helper so
    both databases get identical fallback behavior.

    Never downgrades to DELETE if the on-disk DB header reports WAL — see
    _on_disk_journal_mode.  That holds for both the NFS path and the
    WAL-reset vulnerability path.
    """
    configured = resolve_journal_mode()

    # Vulnerable SQLite: do not enable WAL on new/non-WAL files. Resolve the
    # operator setting first so an explicit DELETE request still verifies that
    # SQLite actually accepted DELETE rather than silently returning MEMORY or
    # another connection-specific mode.
    if is_sqlite_wal_reset_vulnerable():
        return _apply_delete_for_wal_reset_bug(
            conn,
            db_label=db_label,
            require_delete=configured == "delete",
        )

    # Read-only probe — no flock, no checkpoint, no WAL/SHM unlink.
    # Skipping the set-pragma prevents WAL-init from unlinking files other connections hold open.
    current_mode = _on_disk_journal_mode(conn)
    if current_mode == "wal":
        _apply_wal_size_limit(conn)
        _apply_macos_checkpoint_barrier(conn)
        _enforce_macos_synchronous_full(conn)
        return "wal"

    # #68545: honor the canonical database.journal_mode setting. Existing
    # on-disk WAL databases were returned above and are never live-downgraded.
    if configured == "delete":
        if current_mode is None:
            # The mode probe failed (database locked / busy): another
            # process may hold this DB open in WAL. Ownership is not
            # provably exclusive, so flipping journal modes here could
            # destroy committed-but-uncheckpointed WAL transactions of a
            # concurrent writer. Fail loudly instead of downgrading — the
            # operator explicitly requested DELETE and we cannot verify it.
            raise sqlite3.OperationalError(
                "could not verify journal mode before applying configured "
                "journal_mode=delete (database is locked — possible "
                "concurrent openers); refusing to downgrade a database "
                "this process does not exclusively own"
            )
        actual = _set_journal_mode_no_wait(conn, "DELETE")
        if actual != "delete":
            raise sqlite3.OperationalError(
                f"could not set configured journal_mode=delete (got {actual or 'no result'})"
            )
        return actual

    try:
        # ``PRAGMA journal_mode=WAL`` is a query-that-sets: it RETURNS the
        # resulting journal mode. Network filesystems that refuse WAL by
        # *raising* SQLITE_PROTOCOL ("locking protocol") are handled in the
        # except branch below. But macOS NFS — and SMB/CIFS, and the AgentFS
        # NFS overlay — refuse the switch WITHOUT raising: the pragma simply
        # returns the still-effective mode (e.g. ``delete``). Trust the
        # returned row, not the mere absence of an exception; otherwise we
        # report a false ``"wal"`` AND skip the fallback WARNING, leaving the
        # DB silently in DELETE (reader-blocks-writer) with no signal.
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = str(row[0]).strip().lower() if row and row[0] is not None else ""
        if mode == "wal":
            _apply_wal_size_limit(conn)
            _apply_macos_checkpoint_barrier(conn)
            _enforce_macos_synchronous_full(conn)
            return "wal"
        # Silent refusal (macOS NFS / SMB / AgentFS overlay): WAL was not
        # honored, but nothing raised.
        silent_exc = WalUnsupportedError(
            f"journal_mode=WAL refused without raising (still {mode!r})"
        )
        if require_wal:
            raise silent_exc
        _log_wal_fallback_once(db_label, silent_exc)
        return mode or "delete"
    except sqlite3.OperationalError as exc:
        # The require_wal silent-refusal raise above is a WalUnsupportedError
        # (an OperationalError subclass) and lands here — propagate it
        # unchanged rather than re-running it through the marker logic.
        if isinstance(exc, WalUnsupportedError):
            raise
        msg = str(exc).lower()
        if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
            # Unrelated OperationalError — don't silently swallow.
            raise
        # ``disk i/o error`` is ambiguous: on ZFS / APFS-CoW it is a
        # deterministic WAL-incompatibility (SHM corruption under concurrent
        # connection bursts — #55305, #71498), but it can also be a one-shot
        # transient EIO (page-cache pressure, brief lock contention).
        # Treating a transient EIO as a permanent downgrade signal produced
        # the mixed-journal-mode corruption pattern fixed in 5c49cd0ed0
        # (process A downgrades to DELETE while sibling processes set WAL).
        # Disambiguate by retrying the pragma a couple of times: transient
        # EIO clears and we return "wal"; the deterministic filesystem cases
        # keep failing and fall through to the guarded DELETE fallback.
        if "disk i/o error" in msg:
            for _ in range(2):
                time.sleep(0.05)
                try:
                    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                except sqlite3.OperationalError as retry_exc:
                    if "disk i/o error" not in str(retry_exc).lower():
                        raise
                    exc = retry_exc
                    continue
                mode = str(row[0]).strip().lower() if row and row[0] is not None else ""
                if mode == "wal":
                    _apply_wal_size_limit(conn)
                    _apply_macos_checkpoint_barrier(conn)
                    _enforce_macos_synchronous_full(conn)
                    return "wal"
                break
        # Don't downgrade if another process already set WAL on disk, or if
        # the mode cannot be verified at all (probe blocked by a concurrent
        # opener's locks) — ownership is not provably exclusive either way.
        existing = _on_disk_journal_mode(conn)
        if existing == "wal" or existing is None:
            raise
        if require_wal:
            # Caller mandates WAL — fail loudly instead of degrading to DELETE.
            raise WalUnsupportedError(str(exc)) from exc
        _log_wal_fallback_once(db_label, exc)
        _set_journal_mode_no_wait(conn, "DELETE")
        return "delete"


def _set_journal_mode_no_wait(conn: sqlite3.Connection, mode: str) -> str:
    """Execute ``PRAGMA journal_mode=<mode>`` without waiting on other openers.

    This is the ONLY place a journal-mode switch pragma may be issued for a
    non-WAL target.  It temporarily forces ``busy_timeout=0`` so SQLite's own
    exclusivity requirement becomes a concurrent-opener detector: leaving WAL
    mode requires exclusive access to the database, so if ANY other connection
    (this process or another) holds the DB open, the pragma fails immediately
    with ``database is locked`` instead of waiting out a busy timeout and
    sneaking the flip in between a concurrent writer's transactions — which is
    exactly how committed-but-uncheckpointed WAL transactions get destroyed.

    Callers must treat a raised ``OperationalError`` as "not exclusively
    owned: leave the journal mode alone", never as a retryable condition.

    Returns the resulting journal mode as reported by SQLite (lowercase), or
    ``""`` when SQLite returned no row.
    """
    previous_timeout = 0
    try:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        if row and row[0] is not None:
            previous_timeout = int(row[0])
    except (sqlite3.OperationalError, TypeError, ValueError):
        previous_timeout = 0
    conn.execute("PRAGMA busy_timeout=0")
    try:
        row = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()
        return str(row[0]).strip().lower() if row and row[0] is not None else ""
    finally:
        try:
            conn.execute(f"PRAGMA busy_timeout={previous_timeout}")
        except sqlite3.OperationalError:
            pass


def _apply_delete_for_wal_reset_bug(
    conn: sqlite3.Connection,
    *,
    db_label: str,
    require_delete: bool = False,
) -> str:
    """Avoid enabling WAL when the linked SQLite has the WAL-reset bug.

    - Already-WAL on disk: leave WAL alone (no live downgrade) and warn.
    - Mode unreadable (probe blocked by a concurrent opener's locks):
      ownership is not provably exclusive — leave the journal mode alone
      and warn.  Never treat "could not read the mode" as "not WAL": that
      exact confusion let a vulnerable-SQLite process flip a live WAL
      state.db to DELETE under a concurrent WAL writer, destroying its
      committed-but-uncheckpointed transactions.
    - Otherwise: set DELETE (refusing to wait out concurrent openers) and
      warn.
    - For an explicit operator request, verify SQLite accepted DELETE.
    """
    current = _on_disk_journal_mode(conn)

    if current == "wal":
        # Do not TRUNCATE / journal_mode=DELETE while other processes may
        # still hold this WAL DB open — same safety rule as the NFS path.
        _log_wal_reset_bug_once(db_label, kept_wal=True)
        _apply_wal_size_limit(conn)
        _apply_macos_checkpoint_barrier(conn)
        _enforce_macos_synchronous_full(conn)
        return "wal"

    if current is None:
        # The mode probe itself failed — another opener's locks are the
        # most likely cause, and the DB may well be in WAL under a live
        # writer.  Never flip a journal mode we cannot even read.
        if require_delete:
            raise sqlite3.OperationalError(
                "could not verify journal mode before applying configured "
                "journal_mode=delete (database is locked — possible "
                "concurrent openers); refusing to downgrade a database "
                "this process does not exclusively own"
            )
        _log_wal_reset_bug_once(db_label, kept_wal=True, indeterminate=True)
        return "wal"

    actual = ""
    try:
        actual = _set_journal_mode_no_wait(conn, "DELETE")
    except sqlite3.OperationalError as exc:
        if require_delete:
            raise
        lowered = str(exc).lower()
        if "locked" in lowered or "busy" in lowered:
            # A concurrent opener appeared between the probe and the flip
            # (or already held the DB): SQLite refused the exclusive lock.
            # Leave the journal mode exactly as it is.
            _log_wal_reset_bug_once(db_label, kept_wal=True, indeterminate=True)
            return current or "delete"
        # Best-effort for the automatic vulnerable-runtime fallback: DELETE is
        # normally already the default for new file-backed databases.
    if require_delete and actual != "delete":
        raise sqlite3.OperationalError(
            "could not set configured journal_mode=delete "
            f"(got {actual or 'no result'})"
        )
    _log_wal_reset_bug_once(db_label, kept_wal=False)
    return "delete"


def _wal_reset_repair_hint() -> str:
    """Return repair guidance using the public PCBDraft diagnostic command."""
    return (
        "install a Python build bundled with SQLite 3.51.3+ "
        "(or backports 3.50.7 / 3.44.6), restart PCBDraft and run `pcbdraft doctor`"
    )


def _log_wal_reset_bug_once(
    db_label: str,
    *,
    kept_wal: bool,
    indeterminate: bool = False,
) -> None:
    """Log once per (process, db_label) about the WAL-reset vulnerability path."""
    with _wal_reset_bug_warned_lock:
        if db_label in _wal_reset_bug_warned_paths:
            return
        _wal_reset_bug_warned_paths.add(db_label)
    if indeterminate:
        action = (
            "journal mode could not be verified or exclusively switched "
            "(database is locked — possible concurrent openers); leaving the "
            "journal mode untouched (no live downgrade under concurrent "
            "openers)"
        )
    elif kept_wal:
        action = (
            "is already in WAL mode — leaving WAL in place (no live "
            "downgrade under concurrent openers)"
        )
    else:
        action = "using journal_mode=DELETE instead of enabling WAL"
    # Give public diagnostic guidance without promising an installer repair.
    repair_hint = _wal_reset_repair_hint()
    logger.warning(
        "%s: linked SQLite %s is vulnerable to the WAL-reset corruption "
        "bug (https://sqlite.org/wal.html#walresetbug) — %s. "
        "Upgrade to SQLite 3.51.3+ (or backports 3.50.7 / 3.44.6); "
        "%s. See `pcbdraft doctor`. This warning fires once per "
        "process per database.",
        db_label,
        sqlite3.sqlite_version,
        action,
        repair_hint,
    )


def _log_wal_fallback_once(db_label: str, exc: Exception) -> None:
    """Log a single ERROR per (process, db_label) about WAL fallback.

    ERROR (not WARNING): a DB silently dropped to DELETE means a real loss of
    concurrency — under the kanban dispatcher + workers a write blocks readers,
    surfacing as SQLITE_BUSY/lock contention — so it must be loud, not cosmetic.

    Without this dedup, NFS users running kanban (which opens a fresh
    connection on every operation — see the kanban database module) would
    fill errors.log with hundreds of identical errors per hour.
    """
    with _wal_fallback_warned_lock:
        if db_label in _wal_fallback_warned_paths:
            return
        _wal_fallback_warned_paths.add(db_label)
    logger.error(
        "%s: WAL journal_mode unsupported on this filesystem (%s) — "
        "falling back to journal_mode=DELETE (slower rollback-journal "
        "mode; reduces concurrency but works on NFS/SMB/FUSE/ZFS). See "
        "https://www.sqlite.org/wal.html for details. This message "
        "fires once per process per database.",
        db_label,
        exc,
    )


# ---------------------------------------------------------------------------
# Config-driven database pragmas
# ---------------------------------------------------------------------------
def apply_database_pragmas(
    conn: sqlite3.Connection,
    *,
    db_label: str = "state.db",
) -> None:
    """Apply optional performance and WAL-sizing PRAGMAs from ``config.yaml``.

    Reads the ``database:`` section and applies configurable PRAGMAs when set
    to integer values.  The journal mode itself is NOT handled here —
    ``database.journal_mode`` is owned by :func:`resolve_journal_mode` inside
    :func:`apply_wal_with_fallback`, which layers the operator setting under
    all the safety guards (never live-downgrading an on-disk WAL DB,
    filesystem fallback, WAL-reset-bug gating).

    Supported keys under ``database:`` in config.yaml:

    * ``cache_size`` — negative value = KiB, positive = pages
      (e.g. ``-262144`` = 256 MB page cache)
    * ``mmap_size`` — max bytes for memory-mapped I/O (0 = disabled)
    * ``temp_store`` — 0=DEFAULT(file), 1=FILE, 2=MEMORY, 3=ALWAYS
    * ``wal_autocheckpoint`` — WAL auto-checkpoint threshold in pages
    * ``journal_size_limit`` — max journal/WAL size in bytes

    Best-effort: config load or pragma failures are ignored so DB init
    never breaks on a malformed ``database:`` section.
    """
    try:
        # Local import avoids a circular import with model.configuration.
        from pcbdraft.model.configuration import cfg_get, load_config_readonly

        cfg = load_config_readonly()
    except Exception:
        logger.debug(
            "Database pragma config unavailable; using defaults",
            exc_info=_exception_info_without_values(),
        )
        return

    # Performance PRAGMAs (applied to ALL connection types: writer, read_only,
    # and WAL per-thread readers).
    for pragma_name in (
        "cache_size",
        "mmap_size",
        "temp_store",
        "wal_autocheckpoint",
        "journal_size_limit",
    ):
        raw_value = cfg_get(cfg, "database", pragma_name, default=None)
        if raw_value is None:
            continue
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError):
            logger.warning(
                "%s: ignoring non-integer database.%s=%r",
                db_label,
                pragma_name,
                raw_value,
            )
            continue
        try:
            conn.execute(f"PRAGMA {pragma_name}={value}")
        except sqlite3.OperationalError:
            pass


# Markers that mean the host filesystem cannot accept another write. Kept as
# plain substrings so OSError, sqlite3.OperationalError, and wrapped RPC
# error strings all match the same helper.
_DISK_FULL_MARKERS = (
    "no space left on device",
    "not enough space",
    "database or disk is full",  # SQLITE_FULL
    "disk full",
    "full disk",
    "enospc",
)


def is_disk_full_error(exc: BaseException | str | None) -> bool:
    """True when *exc* (or a stringified error) is a disk-full / ENOSPC failure.

    Covers:
      * ``OSError`` with ``errno.ENOSPC``
      * SQLite ``OperationalError: database or disk is full`` (SQLITE_FULL)
      * Plain English / errno strings that survive RPC wrapping
    """
    if exc is None:
        return False
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return True
    text = exc if isinstance(exc, str) else str(exc)
    lowered = text.lower()
    return any(marker in lowered for marker in _DISK_FULL_MARKERS)


# Every cause bucket classify_persistence_error can return. Consumers that
# enumerate causes (e.g. the cron scheduler's explainer-variant suppression)
# must iterate this tuple instead of hardcoding the list, so adding a bucket
# can never silently desynchronize them.
PERSISTENCE_ERROR_CAUSES = (
    "locked",
    "compression",
    "compression_closed",
    "turn_lease",
    "corrupt",
    "disk",
    "unknown",
)


# Markers that mean the database FILE itself is structurally damaged.  Kept
# as plain substrings so sqlite3.DatabaseError, wrapped RPC strings, and
# logged message text all match the same helper.  NOTE: "database disk image
# is malformed" contains the word "disk", so this check MUST run before the
# disk-full/readonly bucket in classify_persistence_error — otherwise real
# B-tree corruption gets reported to the user as "free some disk space"
# (the misdiagnosis documented on #77386).
_DB_CORRUPTION_MARKERS = (
    "malformed",  # "database disk image is malformed" (SQLITE_CORRUPT)
    "file is not a database",  # SQLITE_NOTADB (also connection-level poisoning)
    "not a database",
    "database corruption",
)


def classify_persistence_error(exc_or_str) -> str:
    """Classify a session-persistence failure into a coarse cause bucket.

    Fast-failing a turn on a SessionDB write error is deliberate (the
    transcript would otherwise be lost on restart), but the *guidance* the
    user gets must match the cause: sustained SQLite write-lock contention
    ("database is locked" on a shared state.db) needs "storage was busy,
    send it again", while a full disk or read-only database needs the
    disk-space/permissions advice. Returns one of PERSISTENCE_ERROR_CAUSES:

    * ``"locked"``  — SQLite lock/busy contention (another process holds the
      database write lock); transient, retry-later guidance applies.
    * ``"compression"`` — a live compression lease refused the transcript
      write; the database itself is healthy and unlocked.
    * ``"compression_closed"`` — the write targeted a session already
      rotated (closed) by compression and no live continuation was adopted;
      the store is healthy — the client must refresh/adopt the new session
      id, so disk-space advice would be a misdiagnosis.
    * ``"turn_lease"`` — a presented session-turn-lease holder no longer
      owns the conversation (expired, released, or reclaimed); fail-fast
      fencing, not a storage fault.
    * ``"corrupt"`` — the database file itself is structurally damaged
      (``database disk image is malformed`` / SQLITE_NOTADB).  Distinct from
      ``"disk"``: freeing space cannot help, the user needs the repair path
      (``pcbdraft doctor`` / automatic schema surgery).
    * ``"disk"``    — disk full / read-only / permission-shaped failures
      (delegates the disk-full patterns to :func:`is_disk_full_error` so the
      two classifiers can never drift apart — e.g. ENOSPC).
    * ``"unknown"`` — anything else (or no visible exception at all).
    """
    if exc_or_str is None:
        return "unknown"
    # A refused write during a live compression lease is contention, not
    # storage damage — but its message ("is being compressed by another
    # writer" / "Compression lease lost") contains neither "locked" nor
    # "busy", so it must be matched by type and by phrase (for strings that
    # survived RPC wrapping).
    if isinstance(exc_or_str, SessionTurnLeaseLostError):
        return "turn_lease"
    if isinstance(exc_or_str, CompressionSessionClosedError):
        return "compression_closed"
    if isinstance(exc_or_str, CompressionSessionBusyError):
        return "compression"
    text = str(exc_or_str).lower()
    if "turn lease" in text:
        return "turn_lease"
    if "closed by compression" in text:
        return "compression_closed"
    if "being compressed" in text or "compression lease" in text:
        return "compression"
    # Structural corruption BEFORE the lock and disk buckets: "database disk
    # image is malformed" contains "disk" (and some wrapped corruption
    # strings mention "locked" recovery attempts), so later buckets would
    # steal it and misdiagnose damage as space/contention.
    if any(marker in text for marker in _DB_CORRUPTION_MARKERS):
        return "corrupt"
    if "locked" in text or "busy" in text:
        return "locked"
    if (
        is_disk_full_error(exc_or_str)
        or "disk" in text
        or "readonly" in text
        or "read-only" in text
    ):
        return "disk"
    return "unknown"
