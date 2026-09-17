"""Connection lifecycle and transaction policy for the session store.

The mixin owns SessionDB connection construction, the bounded WAL read pool,
transaction retries, runtime reconnection, checkpoints, and deterministic
close behavior. Schema, FTS/search, and session-domain operations remain in
their dedicated modules.

This module never imports :mod:`pcbdraft.services.session_db`. The legacy
module installs late-bound hooks so established monkeypatch paths continue to
control connection behavior after the extraction.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class SessionConnectionHooks:
    default_db_path: Callable[[], Path]
    ensure_test_isolation: Callable[[Path], None]
    connect_tracked_db: Callable[..., sqlite3.Connection]
    apply_database_pragmas: Callable[..., None]
    apply_wal_with_fallback: Callable[..., str]
    preflight_db_writability: Callable[..., None]
    is_zeroed_state_db: Callable[[Path], bool]
    quarantine_zeroed_state_db: Callable[[Path], Path | None]
    set_last_init_error: Callable[[str | None], None]
    is_malformed_db_error: Callable[[BaseException], bool]
    claim_repair_attempt: Callable[[Path], bool]
    repair_state_db_schema: Callable[[Path], dict[str, Any]]
    load_fts5_cjk_extension: Callable[[sqlite3.Connection], bool]
    is_not_a_database_error: Callable[[BaseException], bool]
    compression_in_progress_error: Callable[[], type[BaseException]]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    random_uniform: Callable[[float, float], float]
    read_pool_max: Callable[[], int]
    read_open_retry_seconds: Callable[[], float]
    unregister_atexit: Callable[[Callable[[], None]], None]
    log_debug: Callable[..., None]
    log_warning: Callable[..., None]
    log_error: Callable[..., None]
    log_exception: Callable[..., None]


_connection_hooks: SessionConnectionHooks | None = None


def configure_connection_hooks(hooks: SessionConnectionHooks) -> None:
    """Install late-bound host hooks used by :class:`SessionConnectionMixin`."""

    global _connection_hooks
    _connection_hooks = hooks


def _hooks() -> SessionConnectionHooks:
    hooks = _connection_hooks
    if hooks is None:
        raise RuntimeError("SessionDB connection hooks are not configured")
    return hooks


def _default_db_path() -> Path:
    return _hooks().default_db_path()


def _ensure_test_isolation(db_path: Path) -> None:
    _hooks().ensure_test_isolation(db_path)


def _connect_tracked_db(path, tracking_path=None, **kwargs):
    return _hooks().connect_tracked_db(path, tracking_path=tracking_path, **kwargs)


def apply_database_pragmas(conn: sqlite3.Connection, **kwargs) -> None:
    _hooks().apply_database_pragmas(conn, **kwargs)


def apply_wal_with_fallback(conn: sqlite3.Connection, **kwargs) -> str:
    return _hooks().apply_wal_with_fallback(conn, **kwargs)


def preflight_db_writability(db_path: Path, **kwargs) -> None:
    _hooks().preflight_db_writability(db_path, **kwargs)


def is_zeroed_state_db(db_path: Path) -> bool:
    return _hooks().is_zeroed_state_db(db_path)


def quarantine_zeroed_state_db(db_path: Path) -> Path | None:
    return _hooks().quarantine_zeroed_state_db(db_path)


def _set_last_init_error(message: str | None) -> None:
    _hooks().set_last_init_error(message)


def is_malformed_db_error(exc: BaseException) -> bool:
    return _hooks().is_malformed_db_error(exc)


def _claim_repair_attempt(db_path: Path) -> bool:
    return _hooks().claim_repair_attempt(db_path)


def repair_state_db_schema(db_path: Path) -> dict[str, Any]:
    return _hooks().repair_state_db_schema(db_path)


def load_fts5_cjk_extension(conn: sqlite3.Connection) -> bool:
    return _hooks().load_fts5_cjk_extension(conn)


def _is_not_a_database_error(exc: BaseException) -> bool:
    return _hooks().is_not_a_database_error(exc)


def _compression_in_progress_error() -> type[BaseException]:
    return _hooks().compression_in_progress_error()


def _read_pool_max() -> int:
    return _hooks().read_pool_max()


def _read_open_retry_seconds() -> float:
    return _hooks().read_open_retry_seconds()


class _LoggerProxy:
    def debug(self, message: str, *args, **kwargs) -> None:
        _hooks().log_debug(message, *args, **kwargs)

    def warning(self, message: str, *args, **kwargs) -> None:
        _hooks().log_warning(message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs) -> None:
        _hooks().log_error(message, *args, **kwargs)

    def exception(self, message: str, *args, **kwargs) -> None:
        _hooks().log_exception(message, *args, **kwargs)


class _TimeProxy:
    def monotonic(self) -> float:
        return _hooks().monotonic()

    def sleep(self, seconds: float) -> None:
        _hooks().sleep(seconds)


class _RandomSource:
    def uniform(self, low: float, high: float) -> float:
        return _hooks().random_uniform(low, high)


class _RandomProxy:
    def SystemRandom(self) -> _RandomSource:
        return _RandomSource()


class _AtexitProxy:
    def unregister(self, hook: Callable[[], None]) -> None:
        _hooks().unregister_atexit(hook)


logger = _LoggerProxy()
time = _TimeProxy()
random = _RandomProxy()
atexit = _AtexitProxy()


class SessionConnectionMixin:
    @staticmethod
    def _close_connection_quietly(conn: sqlite3.Connection | None) -> None:
        """Close a partially initialized connection without masking its error."""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            logger.debug("Could not close a SessionDB connection", exc_info=True)

    def __init__(self, db_path: Path | None = None, read_only: bool = False):
        self.db_path = db_path or _default_db_path()
        # Fail hard (before any connection/pragma/mkdir) if a pytest-context
        # process resolved the developer's production state.db — see the
        # live-DB test-isolation guard block near _default_db_path().
        _ensure_test_isolation(self.db_path)
        self.read_only = read_only

        self._lock = threading.Lock()
        # Read-path split (WAL only): recall/browse queries borrow a
        # read-only connection from a bounded pool so they never queue
        # behind writer flushes on self._lock. See _read_ctx().
        #
        # The pool is BOUNDED because the previous per-thread
        # (threading.local + strong set) scheme pinned one connection per
        # (SessionDB x thread) for the life of the process. Starlette
        # dispatches sync routes on anyio worker threads, so a SessionDB
        # that is never closed accumulated a connection — and two fds, the
        # database and its -wal — for every worker thread that ever read,
        # until the process hit the 256 soft RLIMIT_NOFILE a service manager
        # hands it and every request failed with EMFILE while the process
        # stayed alive, so the supervisor's restart-on-exit never fired.
        # Same bug class as the closing(...) fix in gateway/readiness.py
        # (#69678 / #69567).
        self._read_pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=_read_pool_max()
        )
        # One permit per live read connection, held from before the open in
        # _get_read_conn() until after the close in _close_read_conn().  This
        # is what bounds PEAK descriptors; _read_pool alone bounds only the
        # idle set.  See _read_pool_max().  Acquired non-blocking on purpose: a
        # reader that cannot get a permit must degrade to the writer lock, not
        # queue here — blocking would convert fd exhaustion into a stall, which
        # is the same outage with a different stack trace.
        self._read_permits = threading.BoundedSemaphore(_read_pool_max())
        # Count of reads that found no permit and fell back to the locked
        # writer connection. Not load-bearing; it is the only externally
        # visible signal that the ceiling is actually being reached, so a
        # too-small _read_pool_max() is diagnosable from a running process
        # instead of inferred from latency.
        self._read_permit_exhausted = 0
        self._read_conns_lock = threading.Lock()
        # Set when close() begins.  _read_ctx checks this under the lock
        # before returning a connection to the pool, so a reader still in
        # flight during the drain closes its own connection instead of
        # re-populating a pool nobody will drain again.
        self._read_conns_closed = False
        # "read-only opens are failing against this file" backoff stamp.
        # Instance-wide rather than per-thread: with a shared pool the open
        # is no longer a per-thread event, and retrying a known-bad open on
        # every query is a syscall storm for no benefit. The locked writer
        # connection still serves reads while the backoff holds.
        # Deliberately a TIMESTAMP, not a sticky bool: the likeliest trigger
        # is transient fd pressure (EMFILE) — the very condition this pool
        # exists to prevent — and a permanent flag would demote every reader
        # on this instance to the writer lock for the life of the process.
        # The gateway shares one SessionDB across every agent, so that turns
        # a momentary blip into a permanent global convoy. Expires after
        # _read_open_retry_seconds() so the read path self-heals.
        self._read_open_failed_at = 0.0
        self._wal_active = False
        self._write_count = 0
        # One-shot guard for the runtime FTS rebuild recovery on the write
        # path. A corrupt FTS shadow table makes EVERY message write raise
        # the malformed/corrupt error class via the sync triggers; we repair
        # in place at most once per SessionDB instance so a genuinely
        # unrecoverable database can't put writers into a rebuild loop.
        self._fts_runtime_rebuild_attempted = False
        # One-shot guard for the runtime connection-reopen recovery on the
        # write path. A connection whose backing file was replaced/truncated
        # by a sibling process surfaces as "file is not a database" on every
        # write; we close and reopen the connection at most once per
        # SessionDB instance so a genuinely unrecoverable database can't put
        # writers into a reconnect loop.
        self._notadb_reconnect_attempted = False
        # One-shot guard for the usermerge-floor config write on the
        # incremental FTS merge cadence (see _merge_fts_incrementally).
        self._fts_usermerge_floor_applied = False
        self._fts_enabled = False
        self._fts_stale = False
        self._trigram_available = False
        # CJK-bigram index (cjk_unicode61 loadable tokenizer). _fts_cjk_loaded:
        # extension present on the writer connection; _fts_cjk_available: the
        # messages_fts_cjk table is queryable AND not marked stale. Set during
        # _init_schema / _probe_fts_cjk.
        self._fts_cjk_loaded = False
        self._fts_cjk_available = False
        self._fts_unavailable_warned = False
        self._conn = None
        # Async token accounting (see queue_token_counts). The condition
        # guards queue + writer state; it is distinct from self._lock so
        # enqueue/flush bookkeeping never contends with SQLite writes.
        self._token_queue: deque = deque()
        self._token_queue_cond = threading.Condition(threading.Lock())
        self._token_writer_thread: threading.Thread | None = None
        self._token_writer_stop = False
        self._token_writer_busy = False
        self._token_atexit_hook: Callable[[], None] | None = None
        initialization_complete = False
        try:
            if read_only:
                # Read-only attach for cross-profile aggregation: SELECT-only,
                # so we skip schema init entirely (no DDL, no FTS probe, no
                # column reconcile). Crucially this takes NO write lock, so
                # polling another profile's live DB on every sidebar refresh
                # never contends with that profile's running backend. The DB
                # must already exist + be initialised (callers guard on
                # db_path.exists()); a SELECT against an empty file raises and
                # the caller degrades per-profile.
                self._conn = _connect_tracked_db(
                    f"file:{self.db_path}?mode=ro",
                    tracking_path=self.db_path,
                    uri=True,
                    check_same_thread=False,
                    timeout=1.0,
                    isolation_level=None,
                )
                self._conn.row_factory = sqlite3.Row
                # FTS capability flags normally come from writable schema
                # initialisation. Probe existing virtual tables with SELECTs
                # only so read-only search keeps its FTS and trigram paths.
                # Close the connection on ANY probe failure (e.g. malformed
                # schema raises DatabaseError, not the OperationalError the
                # probe handles). The constructor's outer finally also covers
                # failures before this probe and BaseException paths, so a
                # leaked tracked connection cannot block _backup_db_file's
                # raw-copy for the rest of the process — the writable heal
                # that follows would then repair WITHOUT its forensic backup.
                try:
                    apply_database_pragmas(self._conn, db_label="state.db")
                    cursor = self._conn.cursor()
                    self._fts_enabled = (
                        self._fts_table_probe(cursor, "messages_fts") is True
                    )
                    if self._fts_enabled:
                        self._trigram_available = (
                            self._fts_table_probe(
                                cursor,
                                "messages_fts_trigram",
                            )
                            is True
                        )
                except BaseException:
                    conn, self._conn = self._conn, None
                    try:
                        conn.close()
                    except Exception:
                        logger.debug(
                            "Read-only init connection cleanup failed", exc_info=True
                        )
                    raise
                initialization_complete = True
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)

            # Read-only file/sidecar preflight (port of kilocode#12508):
            # repair-or-refuse BEFORE the first connection so users get an
            # actionable message instead of an opaque "attempt to write a
            # readonly database" from deep inside _init_schema.
            if not read_only:
                preflight_db_writability(self.db_path, db_label="state.db")

            # #68474: zeroed state.db (size>0, all-NUL header) used to fail as a
            # generic "file is not a database" with no recovery path. Quarantine
            # the bytes (do not delete) and continue so a fresh DB can open;
            # point the operator at pre-update snapshots.
            if (
                not read_only
                and self.db_path.exists()
                and is_zeroed_state_db(self.db_path)
            ):
                try:
                    zsize = self.db_path.stat().st_size
                except OSError:
                    zsize = -1
                qpath = quarantine_zeroed_state_db(self.db_path)
                snaps = self.db_path.parent / "state-snapshots"
                msg = (
                    f"state.db looks ZEROED ({zsize} bytes, no SQLite header). "
                    f"Preserved at {qpath or '(quarantine failed — file left in place)'}. "
                    f"Restore from {snaps} if available; "
                    f"run `pcbdraft doctor` for diagnostics. "
                    "Opening a fresh empty database so the agent can start."
                )
                logger.error(msg)
                _set_last_init_error(msg)
                # If quarantine failed, do not open the zeroed file (would fail
                # opaquely or risk further damage). Raise with the clear message.
                if (
                    qpath is None
                    and self.db_path.exists()
                    and is_zeroed_state_db(self.db_path)
                ):
                    raise sqlite3.DatabaseError(msg)

            def _connect_and_init():
                self._conn = _connect_tracked_db(
                    str(self.db_path),
                    check_same_thread=False,
                    # Short timeout — application-level retry with random
                    # jitter handles contention instead of sitting in
                    # SQLite's internal busy handler for up to 30s.
                    timeout=1.0,
                    # auto-starts transactions on DML, which conflicts with
                    # our explicit BEGIN IMMEDIATE.  None = we manage
                    # transactions ourselves.
                    isolation_level=None,
                )
                self._conn.row_factory = sqlite3.Row
                self._wal_active = (
                    apply_wal_with_fallback(self._conn, db_label="state.db") == "wal"
                )
                apply_database_pragmas(self._conn, db_label="state.db")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._fts_cjk_loaded = load_fts5_cjk_extension(self._conn)
                self._init_schema()

            def _connect_and_init_with_lock_patience():
                # Lock contention during open: _init_schema's DDL/reconcile
                # statements run on a 1s-timeout connection with no retry, so
                # a sibling process holding the write lock (VACUUM, TRUNCATE
                # checkpoint at close, a long FTS pass from an older
                # still-running install) used to fail the ENTIRE open —
                # callers then disable persistence for the whole run
                # ("Failed to initialize SessionDB ... database is locked",
                # #74478). The store is healthy; wait it out with the same
                # jittered patience the write path uses. Non-lock errors
                # (including the malformed class) propagate immediately.
                deadline = time.monotonic() + self._WRITE_PATIENCE_S
                while True:
                    try:
                        _connect_and_init()
                        return
                    except sqlite3.OperationalError as exc:
                        err = str(exc).lower()
                        if "locked" not in err and "busy" not in err:
                            raise
                        try:
                            if self._conn is not None:
                                self._conn.close()
                        except Exception:
                            logger.debug(
                                "Locked database init cleanup failed", exc_info=True
                            )
                        now = time.monotonic()
                        if now >= deadline:
                            raise
                        time.sleep(
                            min(
                                random.SystemRandom().uniform(
                                    self._WRITE_RETRY_SLOW_MIN_S,
                                    self._WRITE_RETRY_SLOW_MAX_S,
                                ),
                                max(deadline - now, 0.001),
                            )
                        )

            try:
                _connect_and_init_with_lock_patience()
            except sqlite3.DatabaseError as exc:
                # The malformed-schema class (e.g. a duplicate sqlite_master
                # row for messages_fts) fails on the very first statement —
                # before _init_schema can run — so it can't be caught at the
                # FTS-rebuild layer. Recover by repairing sqlite_master in
                # place (backup first; canonical sessions/messages preserved),
                # then reopen once. This is what lets Desktop/Dashboard
                # self-heal instead of silently showing "no sessions".
                if not is_malformed_db_error(exc) or not _claim_repair_attempt(
                    self.db_path
                ):
                    raise
                logger.error(
                    "state.db schema is malformed (%s) — attempting automatic "
                    "repair (a backup copy is made first).",
                    exc,
                )
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    logger.debug(
                        "Database connection cleanup before repair failed",
                        exc_info=True,
                    )
                report = repair_state_db_schema(self.db_path)
                if not report.get("repaired"):
                    raise
                _connect_and_init_with_lock_patience()

            # NOTE: the v23 FTS storage optimization is OPT-IN,
            # never auto-started on open. Legacy installs keep their working
            # v22 inline FTS untouched here; only the explicit foreground
            # command demotes + rebuilds. This avoids a background worker
            # racing session lifecycle and the surprise disk/latency cost on
            # an unattended open. (An interrupted optimize resumes when the
            # user re-runs the command.)
            initialization_complete = True
        except Exception as exc:
            # Capture the cause so /resume and friends can surface WHY the
            # session DB is unavailable instead of a bare "Session database
            # not available."  Callers that catch this exception keep their
            # existing ``self._session_db = None`` degradation path.
            #
            # Note: we deliberately do NOT clear _last_init_error on the
            # success path (no else branch).  In multi-threaded callers
            # (gateway, web_server per-request SessionDB()), a concurrent
            # successful open racing past this failure would erase the
            # cause that another thread's /resume is about to format.
            # Tests that need to reset the state can call
            # ``session_db._set_last_init_error(None)`` explicitly.
            _set_last_init_error(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if not initialization_complete:
                conn, self._conn = self._conn, None
                self._close_connection_quietly(conn)

    # ── Read-path split ──

    def _get_read_conn(self) -> sqlite3.Connection | None:
        """Open a fresh read-only connection, or None when unavailable.

        Callers must return the connection to self._read_pool (see
        _read_ctx); this opens, it does not track.

        Only used under WAL: WAL readers see a consistent snapshot and never
        block on (or get blocked by) the writer, so recall/browse queries can
        skip self._lock entirely. Under DELETE journal mode (NFS fallback) a
        reader can hit SQLITE_BUSY storms during writes, so we keep the
        legacy locked single-connection path there.

        Fresh read transactions begin per statement (autocommit), so each
        query observes everything committed so far — read-your-writes holds
        for the flush-then-search patterns in a turn.
        """
        if not self._wal_active or self.read_only:
            return None
        with self._read_conns_lock:
            if self._read_conns_closed:
                return None
            if (
                self._read_open_failed_at
                and time.monotonic() - self._read_open_failed_at
                < _read_open_retry_seconds()
            ):
                return None
        # Take the descriptor permit BEFORE the open, so concurrent openers
        # race for permits rather than for file descriptors. Non-blocking:
        # losing the race means "use the writer connection", not "wait".
        if not self._read_permits.acquire(blocking=False):
            with self._read_conns_lock:
                self._read_permit_exhausted += 1
            logger.debug(
                "read pool at capacity (%d) for %s; serving this read from the "
                "locked writer connection",
                _read_pool_max(),
                self.db_path,
            )
            return None
        # Bound before the try: the except handlers close it if the open
        # half-succeeded, and an unbound name there would raise NameError over
        # the top of the real failure.
        conn = None
        try:
            conn = _connect_tracked_db(
                f"file:{self.db_path}?mode=ro",
                tracking_path=self.db_path,
                uri=True,
                # Pooled connections are borrowed by whichever thread runs
                # the next read, and sqlite3 otherwise refuses cross-thread
                # use ("SQLite objects created in a thread can only be used
                # in that same thread") — including on close(), which is how
                # the old per-thread connections became unclosable and leaked
                # their fds. Exclusive ownership is enforced by the pool
                # checkout/return, not by sqlite3. Matches the writer opens.
                check_same_thread=False,
                timeout=5.0,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            apply_database_pragmas(conn, db_label="state.db")
            # Load the CJK tokenizer extension on this connection so
            # messages_fts_cjk queries work on the read path. The .so
            # registers the tokenizer in the connection's in-memory
            # registry, not the database file, so mode=ro is fine.
            if self._fts_cjk_loaded:
                load_fts5_cjk_extension(conn)
        except sqlite3.Error:
            # A partially-constructed connection — _connect_tracked_db
            # succeeded, the CJK extension load did not — must be closed here.
            # Dropping it on the floor still open leaves a live descriptor the
            # tracking registry still counts: the same leak shape this pool
            # exists to fix, one level further down.
            self._discard_partial_read_conn(conn)
            # Back off from retrying the open on every query; the locked
            # writer connection still serves reads until the stamp expires.
            with self._read_conns_lock:
                self._read_open_failed_at = time.monotonic()
            logger.debug(
                "read-only connection open failed for %s", self.db_path, exc_info=True
            )
            self._read_permits.release()
            return None
        except BaseException:
            # Anything else (a non-sqlite3 extension-load failure, MemoryError,
            # KeyboardInterrupt landing between open and return) must not
            # strand the permit: a stranded permit is not a transient error, it
            # permanently shrinks the read path by one slot for the life of the
            # process.
            self._discard_partial_read_conn(conn)
            self._read_permits.release()
            raise
        return conn

    def _discard_partial_read_conn(self, conn) -> None:
        """Close a connection that failed between open and hand-off.

        Separate from _close_read_conn because that one releases a permit and
        this runs on paths that release their own.
        """
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            logger.warning(
                "Partially-opened read connection close failed", exc_info=True
            )

    def _close_read_conn(self, conn) -> None:
        """Close a pooled read connection and release its descriptor permit.

        This was a bare ``except Exception: pass``, which silently swallowed
        the sqlite3.ProgrammingError raised when close() ran on a thread
        other than the one that opened the connection — the exact signature
        of the fd leak this pool fixes. A close that fails leaks a tracked
        fd, so it must not be invisible.

        The permit is released even when close() raises: the descriptor is
        already lost at that point, and withholding the permit too would turn
        one leaked fd into a permanently narrower read path — failing twice for
        one fault. The warning is the signal that matters.

        Pairs with _get_read_conn(). Calling this on a connection that did not
        come from there over-releases the BoundedSemaphore, which raises
        ValueError rather than silently widening the ceiling.
        """
        try:
            conn.close()
        except Exception:
            logger.warning("Read connection close failed", exc_info=True)
        finally:
            self._read_permits.release()

    def _checkout_read_conn(self) -> sqlite3.Connection | None:
        """Borrow a read connection from the pool, opening one on a miss.

        The single acquisition seam for the read path: the WAL/read_only gate,
        the pool checkout and the open-on-miss all live here, so there is
        exactly one place to exercise (and one place for a caller to bypass by
        accident). Returns None when the read path is unavailable and the
        caller must fall back to the locked writer connection.

        A pool hit costs no permit — the connection it hands back is already
        holding one. Only the miss path can open, and only _get_read_conn() can
        take a permit, so peak live connections is bounded by _read_pool_max() no
        matter how many threads miss simultaneously.
        """
        if not self._wal_active or self.read_only:
            return None
        try:
            return self._read_pool.get_nowait()
        except queue.Empty:
            return self._get_read_conn()

    @contextmanager
    def _read_ctx(self):
        """Yield a connection for read-only statements.

        WAL: a read-only connection borrowed from a bounded pool with NO
        lock — recall queries never convoy behind writer flushes (the
        gateway shares one SessionDB across every agent, so this lock was a
        global choke point). The connection is checked out for the duration
        of the block, so no two threads ever touch it concurrently.
        Non-WAL, read-conn failure, or _read_pool_max() already reached: the
        shared writer connection under self._lock, byte-for-byte the legacy
        behavior.

        That last case is the deliberate degradation. Past the ceiling readers
        convoy on the writer lock instead of opening descriptors — measurably
        slower under a burst, and the alternative is EMFILE, which takes the
        whole process down in a way a restart-on-exit supervisor cannot see.
        """
        conn = self._checkout_read_conn()
        if conn is not None:
            try:
                yield conn
            finally:
                returned = False
                with self._read_conns_lock:
                    if not self._read_conns_closed:
                        try:
                            self._read_pool.put_nowait(conn)
                            returned = True
                        except queue.Full:
                            pass
                if not returned:
                    # close() has already drained the pool, so this connection
                    # is surplus. Close it here — dropping it on the floor is
                    # what leaked the fd.
                    #
                    # queue.Full is now unreachable in practice (permits and
                    # maxsize are both _read_pool_max(), so there can never be a
                    # ninth connection to return), but the branch stays: it is
                    # load-bearing if those two ever drift apart, and a leak is
                    # the failure mode it prevents.
                    self._close_read_conn(conn)
            return
        with self._lock:
            yield self._conn

    def _execute_write(
        self,
        fn: Callable[[sqlite3.Connection], T],
        patience_s: float | None = None,
    ) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/DELETE
        statements.  The caller must NOT call ``commit()`` — that's handled
        here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction start
        (not at commit time), so lock contention surfaces immediately.
        On ``database is locked``, we release the Python lock, sleep a
        random jitter, and retry — breaking the convoy pattern that
        SQLite's built-in deterministic backoff creates.

        *patience_s* is the total time budget for lock retries (default
        ``_WRITE_PATIENCE_S``).  Transcript-critical writes pass
        ``_TRANSCRIPT_WRITE_PATIENCE_S`` so a sibling process holding the
        lock for a legitimate long operation (VACUUM, TRUNCATE checkpoint,
        pre-bounded-merge FTS optimize from an older still-running
        install) exhausts routine writers' patience without destroying a
        user turn.  Jitter starts small (20-150ms) for fast reclaim on
        millisecond contention and backs off to 250ms-1s once the lock has
        been held longer than ``_WRITE_RETRY_SLOW_AFTER_S``.

        Returns whatever *fn* returns.
        """
        if patience_s is None:
            patience_s = self._WRITE_PATIENCE_S
        deadline = time.monotonic() + patience_s
        # Set on the first compression-busy collision so the short wait is
        # measured from then, not from the start of the write.
        compression_deadline: float | None = None

        # Transient engine-level error observed on contended WAL appends
        # (dual gateway/agent writers; FTS5 trigram sync holds the write
        # lock). The identical write succeeds standalone, so it is
        # retryable like locked/busy. The exception CLASS varies with the
        # SQLite build — some surface it as InterfaceError, which lives
        # OUTSIDE DatabaseError and escaped the retry net entirely on
        # attempt 0 — so the check is message-scoped, not class-scoped.
        def _is_no_more_rows(exc: sqlite3.Error) -> bool:
            return "no more rows available" in str(exc).lower()

        while True:
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            logger.debug("Failed write rollback failed", exc_info=True)
                        raise
                # Success — periodic best-effort checkpoint + FTS merge.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                if self._write_count % self._FTS_MERGE_EVERY_N_WRITES == 0:
                    self._try_incremental_merge_fts()
                return result
            except _compression_in_progress_error():
                # A live foreign compression lock is transient: the compressor
                # publishes in a couple of seconds. Without any wait, a steer
                # that lands mid-compression aborts the user's turn as
                # session_persistence_failed and sends the operator hunting
                # disk space that was never the problem (#75083).
                #
                # The budget is _COMPRESSION_BUSY_WAIT_S, not the write-lock
                # patience: the lease is a correctness boundary, so a writer
                # still locked out after a short wait must be refused rather
                # than left to land a stale turn once a long-running or wedged
                # compression finally lets go.
                if compression_deadline is None:
                    compression_deadline = min(
                        time.monotonic() + self._COMPRESSION_BUSY_WAIT_S, deadline
                    )
                if self._sleep_before_write_retry(
                    compression_deadline, self._COMPRESSION_BUSY_WAIT_S
                ):
                    continue
                raise
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    if self._sleep_before_write_retry(deadline, patience_s):
                        continue
                    # Patience exhausted — say what actually happened so the
                    # surfaced error doesn't read as disk/permission damage.
                    raise sqlite3.OperationalError(
                        f"database is locked (another PCBDraft process held the "
                        f"state.db write lock for over {patience_s:.0f}s — "
                        "likely a long maintenance operation such as VACUUM, "
                        "a large WAL checkpoint, or an older pre-update "
                        "process; the database itself is healthy)"
                    ) from exc
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(
                    deadline, patience_s
                ):
                    continue
                # Non-lock error or patience exhausted — propagate.
                raise
            except sqlite3.DatabaseError as exc:
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(
                    deadline, patience_s
                ):
                    continue
                # Runtime connection-corruption self-heal: a connection whose
                # backing file was replaced/truncated by a sibling process
                # (e.g. a forked curator agent inheriting and closing the
                # write fd, or an external repair pass) surfaces as "file is
                # not a database" on EVERY subsequent write. Without a
                # reconnect branch the gateway wedges permanently: every
                # transcript/routing write raises, messages stay in memory,
                # and swap grows without bound until the process is killed.
                # Close the broken connection, reopen the DB file, and retry
                # the write once.
                if _is_not_a_database_error(exc):
                    if not self._reconnect_after_notadb():
                        raise
                    continue
                # Corrupt FTS shadow tables make every write raise the
                # malformed/corrupt error class through the FTS sync triggers
                # while the canonical messages table is intact. Recover here,
                # at the shared persistence boundary, so every caller gets the
                # same guarantee. First try the cheap in-place repair. If that
                # one-shot path is unavailable or corruption recurs, detach the
                # derived indexes and retry against the canonical tables.
                if self._try_runtime_fts_rebuild(exc):
                    continue
                if self._enter_fts_fail_open(exc):
                    continue
                raise
            except sqlite3.Error as exc:
                # Catch-all for builds that surface 'no more rows available'
                # as InterfaceError (a sibling of DatabaseError, not a
                # subclass) or another sqlite3.Error class outside the two
                # handlers above. Message-scoped: anything else propagates
                # untouched.
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(
                    deadline, patience_s
                ):
                    continue
                raise

    def _sleep_before_write_retry(self, deadline: float, patience_s: float) -> bool:
        """Sleep one jitter interval if the patience budget still allows it.

        Returns True when the caller should retry, False when *deadline* has
        passed and the error should propagate. Jitter stays small for the
        first ``_WRITE_RETRY_SLOW_AFTER_S`` (fast reclaim on millisecond
        contention) and backs off after that, and never overshoots the
        deadline by a full slow-jitter.
        """
        now = time.monotonic()
        if now >= deadline:
            return False
        elapsed = now - (deadline - patience_s)
        if elapsed >= self._WRITE_RETRY_SLOW_AFTER_S:
            jitter = random.SystemRandom().uniform(
                self._WRITE_RETRY_SLOW_MIN_S,
                self._WRITE_RETRY_SLOW_MAX_S,
            )
        else:
            jitter = random.SystemRandom().uniform(
                self._WRITE_RETRY_MIN_S,
                self._WRITE_RETRY_MAX_S,
            )
        time.sleep(min(jitter, max(deadline - now, 0.001)))
        return True

    def _reconnect_after_notadb(self) -> bool:
        """Close the corrupted write connection and reopen state.db.

        Returns True when the connection was successfully replaced and the
        failed write should be retried.  Mirrors the constructor's
        ``_connect_and_init`` so WAL/schema reconciliation runs on the fresh
        connection.  Never raises — logs and returns False on failure so the
        original error propagates.

        One-shot per instance: a genuinely unrecoverable database must not
        put writers into a reconnect loop that pins CPU on every write.
        """
        if self._notadb_reconnect_attempted:
            return False
        self._notadb_reconnect_attempted = True
        logger.warning(
            "state.db connection reported 'file is not a database' — closing "
            "and reopening the connection to self-heal (one-shot)."
        )
        try:
            with self._lock:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        logger.debug(
                            "Corrupt database connection close failed", exc_info=True
                        )
                    self._conn = None
                new_conn = _connect_tracked_db(
                    str(self.db_path),
                    tracking_path=self.db_path,
                    check_same_thread=False,
                    timeout=1.0,
                    isolation_level=None,
                )
                new_conn.row_factory = sqlite3.Row
                # Publish BEFORE schema init: _init_schema/_reconcile_columns
                # operate on self._conn, not on the local variable.
                self._conn = new_conn
                self._wal_active = (
                    apply_wal_with_fallback(new_conn, db_label="state.db") == "wal"
                )
                apply_database_pragmas(new_conn, db_label="state.db")
                new_conn.execute("PRAGMA foreign_keys=ON")
                self._fts_cjk_loaded = load_fts5_cjk_extension(new_conn)
                self._init_schema()
        except Exception:
            logger.exception(
                "state.db reconnect after 'file is not a database' failed; "
                "the database may need the full offline repair path.",
            )
            return False
        logger.warning(
            "state.db connection reopened successfully; retrying the failed write."
        )
        return True

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint.  Never raises.

        Flushes committed WAL frames back into the main DB file without
        requiring an exclusive lock.  PASSIVE is safe for frequent
        periodic use because it does not block concurrent writers and
        cannot corrupt B-tree pages under I/O pressure.

        PASSIVE does not truncate the WAL file — it stays at its
        high-water mark. Explicit checkpoints on the shared ``state.db`` no
        longer truncate the WAL; it is bounded by ``journal_size_limit`` and
        the writer's natural post-checkpoint reset rather than by a TRUNCATE
        at every close or maintenance command.

        Previous TRUNCATE strategy caused B-tree corruption on large
        databases (65K+ pages) due to the exclusive-lock I/O pressure
        from checkpointing thousands of frames at once (issue #45383).
        """
        try:
            with self._lock:
                result = self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint: %d/%d pages checkpointed",
                        result[2],
                        result[1],
                    )
        except Exception:
            logger.warning("WAL checkpoint (PASSIVE) failed", exc_info=True)

    def __enter__(self) -> Self:
        """Enter a scope that closes this handle on the way out.

        Ownership of a SessionDB should be released explicitly.
        Historically an instance with a started token writer pinned ITSELF
        (bound-method writer target plus a strong ``atexit`` drain hook), so
        ``__del__`` never ran for exactly the instances that leaked
        descriptors (#88033).  The writer now retires after an idle window
        and the atexit hook holds only a weak reference, so abandoned
        handles are eventually collectible — but "eventually, after the
        idle window and a GC cycle" is not a release policy.  Call sites
        owning a handle are still expected to close it deterministically
        (see the ownership comments in ``run_agent.py`` and
        ``tui_gateway/methods_session.py``).

        This makes the correct usage the easy one, so an owning scope can be
        exception-safe by construction rather than by remembering a
        ``try/finally``:

            with SessionDB(path) as db:
                db.append_message(...)

        Purely additive: it changes nothing for callers that already call
        ``close()`` directly, and ``close()`` stays idempotent, so a scope
        that closes early still exits cleanly.
        """
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Close the handle, then let any exception propagate.

        Returns False (never suppressing), so ``with`` here only manages the
        descriptor lifetime and never swallows a caller's error.
        """
        self.close()
        return False

    def close(self):
        """Close the database connection.

        Drains queued token deltas first (the background writer needs the
        connection). Writable connections then attempt a PASSIVE WAL
        checkpoint (NOT TRUNCATE: transient per-cron-run connections close
        many times an hour, and a TRUNCATE fires a full WAL reset that
        races the gateway's live writer and tears B-tree pages — issue
        #45383). Read-only connections never request a checkpoint.
        """
        self._stop_token_writer()
        hook, self._token_atexit_hook = self._token_atexit_hook, None
        if hook is not None:
            atexit.unregister(hook)
        # Drain the read-only connection pool.  Setting the closed flag
        # under the lock first means a reader still in flight closes its own
        # connection on release instead of re-populating a pool that has
        # already been drained.
        with self._read_conns_lock:
            self._read_conns_closed = True
        while True:
            try:
                conn = self._read_pool.get_nowait()
            except queue.Empty:
                break
            self._close_read_conn(conn)
        with self._lock:
            if self._conn:
                if not self.read_only:
                    # PASSIVE, not TRUNCATE. Every cron run_agent opens+closes a
                    # transient SessionDB, so a TRUNCATE here fires a full WAL
                    # reset many times/hour, racing the gateway's long-lived
                    # writer on large WAL databases and tearing hot B-tree
                    # pages -- the #45383 corruption this class's own periodic
                    # checkpoint was already made PASSIVE to avoid. TRUNCATE
                    # belongs only on a sole-opener/quiescent connection.
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except Exception:
                        logger.debug(
                            "WAL checkpoint (PASSIVE) at close failed",
                            exc_info=True,
                        )
                conn, self._conn = self._conn, None
                self._close_connection_quietly(conn)

    def __del__(self) -> None:
        """Safety net: close the connection if the caller forgot.

        The async accounting worker retires when idle and its atexit hook
        holds only a weak reference, so neither can pin an otherwise orphaned
        instance. During interpreter teardown the order of module cleanup is
        undefined, so every attribute access remains guarded.

        Delegates to ``close()`` so the read pool, token writer, and atexit
        hook are all cleaned up — not just the writer connection.
        """
        if self.__dict__.get("_conn") is None:
            return
        try:
            self.close()
        except Exception:
            logger.debug("SessionDB finalizer close failed", exc_info=True)
