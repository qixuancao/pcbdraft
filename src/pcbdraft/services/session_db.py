"""
SQLite State Store for PCBDraft.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import asyncio
import atexit
import contextlib
import hashlib
import json
import logging
import os
import queue
import random
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pcbdraft.agent import skill_commands as _skill_commands
from pcbdraft.agent.memory_manager import sanitize_context
from pcbdraft.agent.message_sanitization import _sanitize_surrogates
from pcbdraft.core.runtime_environment import (
    _exception_info_without_values,
    get_runtime_home,
)
from pcbdraft.services import session_db_common as _session_db_common
from pcbdraft.services import session_db_connection as _session_db_connection
from pcbdraft.services import session_db_fts_integrity as _session_db_fts_integrity
from pcbdraft.services import session_db_metadata as _session_db_metadata
from pcbdraft.services import session_db_runtime as _session_db_runtime
from pcbdraft.services import (
    session_db_transcript_write as _session_db_transcript_write,
)
from pcbdraft.services.session_db_common import (
    _COMPRESSION_CHILD_SQL,
    _FTS_CJK_TRIGGERS,
    _FTS_TRIGGERS,
    _LISTABLE_CHILD_SQL,
    _PREVIEW_RAW_SELECT,
    _RESET_END_REASONS,
    _RESET_END_REASONS_SQL,
    FTS_CJK_STALE_KEY,
    FTS_STALE_KEY,
    _legacy_reset_child_sql,
    _shape_preview,
    _sql_session_last_active,
    _sql_session_last_active_by_id,
)
from pcbdraft.services.session_db_common import escape_like as _escape_like
from pcbdraft.services.session_db_connection import SessionConnectionMixin
from pcbdraft.services.session_db_conversation import SessionConversationMixin
from pcbdraft.services.session_db_deletion import SessionDeletionMixin
from pcbdraft.services.session_db_fts_integrity import SessionFTSIntegrityMixin
from pcbdraft.services.session_db_handoff import SessionHandoffMixin
from pcbdraft.services.session_db_listing import SessionListingMixin
from pcbdraft.services.session_db_maintenance import SessionMaintenanceMixin
from pcbdraft.services.session_db_meta_store import SessionMetaStoreMixin
from pcbdraft.services.session_db_metadata import SessionMetadataMixin
from pcbdraft.services.session_db_portability import SessionPortabilityMixin
from pcbdraft.services.session_db_presentation import SessionPresentationStateMixin
from pcbdraft.services.session_db_pruning import SessionPruningMixin
from pcbdraft.services.session_db_rewind import SessionRewindMixin
from pcbdraft.services.session_db_schema import SessionSchemaMixin
from pcbdraft.services.session_db_search import SessionSearchMixin
from pcbdraft.services.session_db_search_metrics import SessionSearchMetricsMixin
from pcbdraft.services.session_db_telegram_topics import SessionTelegramTopicsMixin
from pcbdraft.services.session_db_token_accounting import SessionTokenAccountingMixin
from pcbdraft.services.session_db_transcript_query import SessionTranscriptQueryMixin
from pcbdraft.services.session_db_transcript_write import SessionTranscriptWriteMixin

# Compatibility exports from before the SessionDB module split. Keep these
# bindings available to callers even though the implementation uses the mixins.
SKILL_EXCERPT_JOINT = _skill_commands.SKILL_EXCERPT_JOINT
SKILL_SCAFFOLD_SQL_LIKE = _skill_commands.SKILL_SCAFFOLD_SQL_LIKE
describe_skill_invocation = _skill_commands.describe_skill_invocation
_BRANCH_CHILD_SQL = _session_db_common._BRANCH_CHILD_SQL
_PREVIEW_CONTENT_SQL = _session_db_common._PREVIEW_CONTENT_SQL
_PREVIEW_HEAD_CHARS = _session_db_common._PREVIEW_HEAD_CHARS
_PREVIEW_MAX_CHARS = _session_db_common._PREVIEW_MAX_CHARS
_PREVIEW_SCAFFOLD_WINDOW = _session_db_common._PREVIEW_SCAFFOLD_WINDOW
_PREVIEW_SCAFFOLDED_SQL = _session_db_common._PREVIEW_SCAFFOLDED_SQL
DEFERRED_INDEX_SQL = _session_db_common.DEFERRED_INDEX_SQL
FTS_SQL = _session_db_common.FTS_SQL
FTS_STORAGE_VERSION = _session_db_common.FTS_STORAGE_VERSION
FTS_TRIGRAM_SQL = _session_db_common.FTS_TRIGRAM_SQL
LEGACY_FTS_SQL = _session_db_common.LEGACY_FTS_SQL
LEGACY_FTS_TRIGRAM_SQL = _session_db_common.LEGACY_FTS_TRIGRAM_SQL
MAX_FTS5_QUERY_CHARS = _session_db_common.MAX_FTS5_QUERY_CHARS
SCHEMA_SQL = _session_db_common.SCHEMA_SQL
SCHEMA_VERSION = _session_db_common.SCHEMA_VERSION
_ephemeral_child_sql = _session_db_common._ephemeral_child_sql

# Compatibility exports from before the SQLite runtime policy was extracted.
PERSISTENCE_ERROR_CAUSES = _session_db_runtime.PERSISTENCE_ERROR_CAUSES
CompressionSessionBusyError = _session_db_runtime.CompressionSessionBusyError
CompressionSessionClosedError = _session_db_runtime.CompressionSessionClosedError
SessionCompressionInProgressError = (
    _session_db_runtime.SessionCompressionInProgressError
)
SessionTurnLeaseLostError = _session_db_runtime.SessionTurnLeaseLostError
WalUnsupportedError = _session_db_runtime.WalUnsupportedError
_apply_delete_for_wal_reset_bug = _session_db_runtime._apply_delete_for_wal_reset_bug
_apply_macos_checkpoint_barrier = _session_db_runtime._apply_macos_checkpoint_barrier
_apply_wal_size_limit = _session_db_runtime._apply_wal_size_limit
_DB_CORRUPTION_MARKERS = _session_db_runtime._DB_CORRUPTION_MARKERS
_DISK_FULL_MARKERS = _session_db_runtime._DISK_FULL_MARKERS
_enforce_macos_synchronous_full = _session_db_runtime._enforce_macos_synchronous_full
_is_sqlite_wal_reset_vulnerable = _session_db_runtime._is_sqlite_wal_reset_vulnerable
_log_wal_fallback_once = _session_db_runtime._log_wal_fallback_once
_log_wal_reset_bug_once = _session_db_runtime._log_wal_reset_bug_once
_on_disk_journal_mode = _session_db_runtime._on_disk_journal_mode
_set_journal_mode_no_wait = _session_db_runtime._set_journal_mode_no_wait
_WAL_INCOMPAT_MARKERS = _session_db_runtime._WAL_INCOMPAT_MARKERS
_WAL_SIZE_LIMIT_BYTES = _session_db_runtime._WAL_SIZE_LIMIT_BYTES
_wal_fallback_warned_lock = _session_db_runtime._wal_fallback_warned_lock
_wal_fallback_warned_paths = _session_db_runtime._wal_fallback_warned_paths
_wal_reset_bug_warned_lock = _session_db_runtime._wal_reset_bug_warned_lock
_wal_reset_bug_warned_paths = _session_db_runtime._wal_reset_bug_warned_paths
_wal_reset_repair_hint = _session_db_runtime._wal_reset_repair_hint
apply_database_pragmas = _session_db_runtime.apply_database_pragmas
apply_wal_with_fallback = _session_db_runtime.apply_wal_with_fallback
classify_persistence_error = _session_db_runtime.classify_persistence_error
is_disk_full_error = _session_db_runtime.is_disk_full_error
is_sqlite_wal_reset_vulnerable = _session_db_runtime.is_sqlite_wal_reset_vulnerable
resolve_journal_mode = _session_db_runtime.resolve_journal_mode
sqlite_source_id = _session_db_runtime.sqlite_source_id

# Compatibility exports from before the session metadata behavior was split.
ActivityProvenance = _session_db_metadata.ActivityProvenance
_BARE_BILLING_PROVIDERS = _session_db_metadata._BARE_BILLING_PROVIDERS
_MODEL_CONFIG_ROW_MISSING = _session_db_metadata._MODEL_CONFIG_ROW_MISSING

try:  # Hard dependency, but tolerate scaffold-phase imports before pip install.
    import psutil
except ImportError:  # pragma: no cover - stripped/scaffold installs only
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Keep the historical ``session_db.queue`` patch surface. The extracted
# connection module imports the same module object, so patching queue classes
# through the legacy path still controls connection-pool behavior.
_SESSION_DB_QUEUE_MODULE = queue

MAX_SAFE_RESUME_MESSAGES = 20_000
MAX_SAFE_EXPORT_MESSAGES = 20_000


def _configured_transcript_limit(key: str, fallback: int) -> int:
    """Resolve a transcript safety limit from config at call time.

    Reads ``sessions.<key>`` from config.yaml lazily (avoiding a circular
    import at module load) and falls back to the module constant when the
    config subsystem is unavailable (scaffold installs, stripped test
    environments). A value of 0 disables the guard entirely. No caching:
    ``load_config_readonly`` is already mtime-cached, and resolving fresh
    keeps tests that monkeypatch config or the module constants working.
    """
    try:
        from pcbdraft.model.configuration import load_config_readonly

        sessions_cfg = load_config_readonly().get("sessions") or {}
        value = sessions_cfg.get(key)
        if value is None:
            return fallback
        limit = int(value)
        return limit if limit >= 0 else fallback
    except Exception:
        logger.debug(
            "Transcript limit config unavailable; using default",
            exc_info=_exception_info_without_values(),
        )
        return fallback


def resolved_max_resume_messages() -> int:
    """Config-resolved resume guard limit (0 disables the guard)."""
    return _configured_transcript_limit("max_resume_messages", MAX_SAFE_RESUME_MESSAGES)


def resolved_max_export_messages() -> int:
    """Config-resolved in-memory export guard limit (0 disables the guard)."""
    return _configured_transcript_limit("max_export_messages", MAX_SAFE_EXPORT_MESSAGES)


class SessionResumeTooLargeError(ValueError):
    def __init__(
        self,
        message_count: int,
        limit: int = MAX_SAFE_RESUME_MESSAGES,
        scope: str = "across its lineage",
    ):
        self.message_count = message_count
        self.limit = limit
        super().__init__(
            f"session has at least {message_count} active messages {scope}; "
            f"safe resume limit is {limit}. Export the session instead, or set "
            "sessions.max_resume_messages: 0 in config.yaml to disable the guard."
        )


class SessionExportTooLargeError(ValueError):
    def __init__(
        self,
        session_id: str,
        message_count: int,
        limit: int = MAX_SAFE_EXPORT_MESSAGES,
    ):
        self.session_id = session_id
        self.message_count = message_count
        self.limit = limit
        super().__init__(
            f"session '{session_id}' has at least {message_count} active messages; "
            f"safe in-memory export limit is {limit}"
        )


_COMPRESSION_LOCK_HOLDER_PID_RE = re.compile(r"(?:^|:)pid=(\d+)(?::|$)")


def _system_prompt_hash(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def _compression_lock_holder_process_is_dead(holder: str) -> bool:
    """Return True only when a structured lock holder's local PID is gone.

    Compression locks are stored in a host-local SQLite database and holder
    IDs created by ``conversation_compression`` start with ``pid=<n>``. A
    process killed during gateway shutdown cannot release its lease, so waiting
    for the full TTL makes every new turn repeatedly attempt compaction. Reclaim
    only when the kernel proves that PID no longer exists; legacy/unstructured
    holders, same-process holders, permission errors, and any probe doubt
    remain protected until normal TTL expiry (conservative: PID reuse must
    never steal a live lease, and a wrongly-kept lease self-heals via TTL).
    """
    match = _COMPRESSION_LOCK_HOLDER_PID_RE.search(holder or "")
    if match is None:
        return False
    try:
        pid = int(match.group(1))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        # Same-process holder (e.g. another thread's live lease): never
        # self-reclaim — the lease refresher and release path own it.
        return False
    if psutil is not None:
        try:
            # psutil is the canonical cross-platform liveness answer
            # (CONTRIBUTING.md "Critical rules" #1). pid_exists() reports
            # recycled PIDs as alive — conservative, the TTL still applies.
            return not psutil.pid_exists(pid)
        except Exception:
            logger.debug("Compression lease holder probe failed", exc_info=True)
            return False  # any doubt → keep the lease until TTL expiry
    # Scaffold-phase fallback only (psutil missing), and POSIX-only: stdlib
    # os.kill(pid, 0) is NOT a no-op probe on Windows (bpo-14484 — sig=0 maps
    # to CTRL_C_EVENT and can kill the target's console group). Without psutil
    # a Windows host stays TTL-only; the lease TTL remains the recovery path.
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)  # windows-footgun: ok — nt early-returns just above
    except ProcessLookupError:
        return True
    except (PermissionError, OSError, OverflowError):
        return False
    return False


_scrub_surrogates = _session_db_transcript_write._scrub_surrogates


def workspace_key(row: dict[str, Any]) -> str | None:
    """A session's workspace grouping key: its git repo root when known, else
    its cwd.

    Branch is deliberately excluded so checking out a new branch doesn't
    fragment a workspace's session history. Returns None for cwd-less (unbound)
    sessions. Both fields are already recorded on ``sessions`` — this just picks
    the coarser identity for grouping/filtering.
    """
    root = (row.get("git_repo_root") or "").strip()
    if root:
        return root

    cwd = (row.get("cwd") or "").strip()
    return cwd or None


def _delegate_from_json(col: str = "model_config") -> str:
    return f"json_extract(COALESCE({col}, '{{}}'), '$._delegate_from')"


def _cwd_prefix_clause(cwd_prefix: str) -> tuple[str, list[str]]:
    prefix = cwd_prefix.rstrip("/\\") or cwd_prefix
    # ``_`` and ``%`` are LIKE wildcards but ordinary characters in a path
    # (``my_project``), so an unescaped prefix also matches sibling directories.
    # Escape the needle and pair it with ESCAPE; the literal separator
    # backslash in the Windows pattern needs escaping for the same reason. The
    # ``=`` arm is an exact compare and keeps the raw prefix.
    esc = _escape_like(prefix)
    return (
        "(s.cwd = ? OR s.cwd LIKE ? ESCAPE '\\' OR s.cwd LIKE ? ESCAPE '\\')",
        [prefix, f"{esc}/%", f"{esc}\\\\%"],
    )


def _workspace_key_clause(key: str) -> tuple[str, list[str]]:
    """Match sessions whose ``workspace_key(row)`` equals ``key``.

    Mirrors :func:`workspace_key`: a session belongs to workspace ``key``
    when its recorded ``git_repo_root`` equals ``key``, or — for rows that
    predate per-session git metadata — when its ``cwd`` is at or under
    ``key`` (so a session started in ``repo/src`` still groups with ``repo``).
    Used by conversation resume to continue the most recent session in
    the *current* workspace rather than the global MRU.
    """
    prefix = key.rstrip("/\\") or key
    cwd_clause, cwd_params = _cwd_prefix_clause(prefix)
    return (
        f"(s.git_repo_root = ? OR (COALESCE(s.git_repo_root, '') = '' AND {cwd_clause}))",
        [prefix, *cwd_params],
    )


def _collect_delegate_child_ids(conn, parent_ids: list[str]) -> list[str]:
    """Delegate-subagent ids to cascade-delete with *parent_ids*.

    Only rows carrying the ``_delegate_from`` marker (set at creation, and
    backfilled by the v16 migration) — generic untagged children keep the
    orphan-don't-delete contract. Walks marker chains recursively so an
    orchestrator subagent's own delegate children go too (FK safety).
    """
    df = _delegate_from_json()
    seeds = {sid for sid in parent_ids if sid}
    # Seed the visited set with the parents themselves. A delegation marker
    # chain can loop back onto a parent — a cycle, or a parent that is also
    # another parent's delegate child when several ids are deleted at once —
    # and without this guard that parent would be collected as one of its own
    # descendants and cascade-deleted along with all of its messages. Callers
    # delete the parents separately, so parents must never appear in the
    # returned child set. (#49148)
    found: set[str] = set(seeds)
    frontier = list(seeds)
    while frontier:
        ph = ",".join("?" * len(frontier))
        cursor = conn.execute(
            f"SELECT id FROM sessions WHERE {df} IN ({ph}) "
            f"OR (parent_session_id IN ({ph}) AND {df} IS NOT NULL)",
            frontier + frontier,
        )
        frontier = [row["id"] for row in cursor.fetchall() if row["id"] not in found]
        found.update(frontier)
    # Return only the discovered children — never the parents themselves.
    return [sid for sid in found if sid not in seeds]


def _delete_delegate_children(conn, parent_ids: list[str]) -> list[str]:
    ids = _collect_delegate_child_ids(conn, parent_ids)
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", ids)
        # FK safety: orphan any untagged stragglers pointing at a doomed row.
        conn.execute(
            f"UPDATE sessions SET parent_session_id = NULL "
            f"WHERE parent_session_id IN ({ph})",
            ids,
        )
        conn.execute(f"DELETE FROM sessions WHERE id IN ({ph})", ids)
    return ids


DEFAULT_DB_PATH = get_runtime_home() / "state.db"

# How long SessionDB stops attempting read-only opens after one fails, before
# probing again. Long enough that a genuinely unreadable file isn't retried per
# query; short enough that transient fd pressure doesn't strand the read pool.
_READ_OPEN_RETRY_SECONDS = 60.0

# Hard ceiling on read-only connections ALIVE at once per SessionDB — pooled
# idle ones and checked-out ones together.
#
# Deliberately one constant for both the pool's maxsize and the permit count,
# because bounding only the pool bounds the wrong thing. A LifoQueue caps how
# many connections are *returned*; it says nothing about how many are *open*.
# With an open-on-miss checkout, N readers arriving on an empty pool all miss,
# all open, and peak at N — the surplus is closed on release, so nothing
# accumulates forever, but EMFILE is a peak-instant condition and the burst
# that empties the pool is exactly the burst that exhausts the fd table.
#
# So a connection holds a permit for its whole lifetime: acquired in
# _get_read_conn() before the open, released in _close_read_conn() after the
# close. Once permits are gone the read path degrades to the locked writer
# connection instead of opening more descriptors — slower under load, which is
# the correct trade against a process-wide wedge the supervisor cannot see.
_READ_POOL_MAX = 8

# Import-time snapshot used by _default_db_path() to detect a deliberately
# re-pointed DEFAULT_DB_PATH (tests monkeypatch the constant directly).
_IMPORT_DEFAULT_DB_PATH = DEFAULT_DB_PATH


def _default_db_path() -> Path:
    """Resolve the default state DB path at call time.

    ``DEFAULT_DB_PATH`` is computed when this module is first imported, which
    freezes the developer's real runtime home even when a test fixture later
    redirects ``PCBDRAFT_RUNTIME_HOME`` — importing this module during collection was
    enough to point every default ``SessionDB()`` at the real state.db.

    Precedence:

    1. A deliberately re-pointed ``DEFAULT_DB_PATH`` (differs from the
       import-time snapshot — the established test escape hatch) wins.
    2. Otherwise resolve ``get_runtime_home()`` fresh so a runtime
       ``PCBDRAFT_RUNTIME_HOME`` redirect takes effect regardless of import order.
    """
    if DEFAULT_DB_PATH != _IMPORT_DEFAULT_DB_PATH:
        return DEFAULT_DB_PATH
    return get_runtime_home() / "state.db"


# ---------------------------------------------------------------------------
# Live-DB test-isolation guard
# ---------------------------------------------------------------------------
# Forensic evidence (Aug 2026, live developer machine): the production
# upstream state.db accumulated pytest fixture rows — sessions with
# chat_id='chat-1'/'123'/'wx-chat' and gateway_routing scopes literally under
# /tmp/pytest-of-*/ — and a pytest-spawned process flipped the journal mode
# out from under the WAL-mode gateway writer, destroying committed
# transcripts ("Persisted transcript lagged live cached history ... possible
# FTS write corruption").  The hermetic conftest redirects PCBDRAFT_RUNTIME_HOME per
# test, but any escape (a session-scoped fixture running before the autouse
# fixture, a subprocess child launched without PCBDRAFT_RUNTIME_HOME, a stale worktree
# without the re-pin, or a developer shell that exports PCBDRAFT_RUNTIME_HOME to the
# real home so the conftest session sandbox is skipped) silently fell
# through to the real database.
#
# This guard is the single choke point: EVERY ``SessionDB`` construction
# resolves its path here, so under pytest a resolution that lands on a
# production state.db fails hard instead of corrupting live data.  It is
# env-based (``PYTEST_CURRENT_TEST`` / ``PYTEST_VERSION`` are set by pytest
# and inherited by subprocess children), so it also protects children that
# never import the test conftest.

#: Escape hatch for the rare legitimate case (a test that genuinely needs
#: the real DB).  The in-tree conftest sets this for tests marked
#: ``@pytest.mark.live_system_guard_bypass``; scripts may set it explicitly.
_STATE_DB_GUARD_BYPASS = False

#: Env-carried twin of ``_STATE_DB_GUARD_BYPASS``.  A module global cannot
#: cross a process boundary, so a test that deliberately points a *child* at
#: the live DB has no way to opt out once ancestry arms the guard there.
#: Export this in the child's env instead.
_STATE_DB_GUARD_BYPASS_ENV = "PCBDRAFT_RUNTIME_STATE_DB_GUARD_BYPASS"

#: Additional production roots to refuse (beyond the platform default
#: product runtime directory). The test conftest injects the pre-sandbox production
#: root here so custom-``PCBDRAFT_RUNTIME_HOME`` deployments are covered too.
_STATE_DB_GUARD_EXTRA_DENY_ROOTS: tuple[Path, ...] = ()


def _real_platform_state_root() -> Path | None:
    """Resolve the native PCBDraft production root without reading state."""
    try:
        from pcbdraft.core.platform_paths import production_runtime_roots

        return production_runtime_roots()[0]
    except Exception:
        logger.debug("Production state root resolution failed", exc_info=True)
        return None


#: Env marker exported by the hermetic test conftest at the same moment it
#: redirects ``PCBDRAFT_RUNTIME_HOME`` to the per-session tmp isolation root.  Its
#: value is that isolation root.  Unlike ``PYTEST_*`` (owned by pytest, and
#: routinely scrubbed by tests that rebuild a child environment), this marker
#: is OURS: it declares "this process tree is running under PCBDraft test
#: isolation", and it inherits into subprocess children by default — so a
#: child that received the patched ``PCBDRAFT_RUNTIME_HOME`` also received the marker,
#: and a child that resolves a production DB while carrying it is, by
#: definition, an isolation escape (#82770).
_TEST_ISOLATION_MARKER_ENV = "PCBDRAFT_RUNTIME_TEST_ISOLATION"


def _running_under_pytest() -> bool:
    """True when this process (or a parent test process) is a pytest run."""
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("PYTEST_VERSION")
        or os.environ.get(_TEST_ISOLATION_MARKER_ENV)
    )


#: Names that identify a pytest launcher in a process command line.  Matched
#: against the *basename* of each argv token so ``/tmp/pytest-of-dev/...``
#: paths — which do show up in real argv — cannot false-positive.
_PYTEST_LAUNCHER_NAMES = frozenset({"pytest", "py.test", "pytest.exe", "py.test.exe"})

#: Memoised ancestry answer.  The process tree above us does not change in a
#: way that matters here, and the walk must not cost anything on the hot path.
_PYTEST_ANCESTOR: bool | None = None


def _process_looks_like_pytest(proc: Any) -> bool:
    """True when *proc*'s command line is a pytest invocation.

    Covers both ``pytest ...`` (launcher on argv[0]) and ``python -m pytest``
    (launcher as a bare ``pytest`` token).  A process whose command line we
    cannot read is treated as "not pytest": guessing the other way would
    refuse production opens for unrelated reasons.
    """
    try:
        cmdline = proc.cmdline() or []
    except Exception:
        logger.debug(
            "Test ancestry command-line inspection failed",
            exc_info=_exception_info_without_values(),
        )
        return False
    for arg in cmdline:
        try:
            token = str(arg).strip('"').strip("'")
            # Split on both separators on every host: os.path.basename is
            # POSIX-only under Linux and would leave a Windows-style path
            # intact, making the matcher's answer depend on the platform.
            name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        except Exception:
            logger.debug(
                "Test ancestry command token inspection failed",
                exc_info=_exception_info_without_values(),
            )
            continue
        if name in _PYTEST_LAUNCHER_NAMES:
            return True
    return False


def _has_pytest_ancestor() -> bool:
    """True when some ancestor process of this one is a pytest run.

    ``_running_under_pytest`` reads ``PYTEST_*`` env vars, which a child
    spawned with a rebuilt environment loses at the same moment it loses the
    ``PCBDRAFT_RUNTIME_HOME`` redirect: that child aims at the production DB *and*
    disarms the guard in one step (#82770).  Ancestry is the one test-context
    signal that survives an env rebuild, so it backs the env check up.

    Fails open (``False``) when ``psutil`` is unavailable or the walk errors —
    that restores the previous env-only behaviour rather than blocking real
    user runs on a psutil hiccup.
    """
    global _PYTEST_ANCESTOR
    if _PYTEST_ANCESTOR is not None:
        return _PYTEST_ANCESTOR
    found = False
    if psutil is not None:
        try:
            for parent in psutil.Process().parents():
                if _process_looks_like_pytest(parent):
                    found = True
                    break
        except Exception:
            logger.debug("Test process ancestry lookup failed", exc_info=True)
            found = False
    _PYTEST_ANCESTOR = found
    return found


def _in_test_context() -> bool:
    """True when this process is a test run, by environment or by ancestry.

    Order matters for cost: the env probe is two dict lookups and covers the
    common in-process case, so the ancestry walk only runs for processes the
    environment claims are ordinary user runs — and its answer is memoised,
    so a real ``pcbdraft`` invocation pays for at most one walk.
    """
    if _running_under_pytest():
        return True
    return _has_pytest_ancestor()


def _production_state_roots() -> list[Path]:
    roots: list[Path] = []
    real_root = _real_platform_state_root()
    if real_root is not None:
        roots.append(real_root)
    from pcbdraft.core.platform_paths import production_runtime_roots

    try:
        roots.extend(production_runtime_roots()[1:])
    except (OSError, RuntimeError):
        logger.debug("Product config guard root resolution failed", exc_info=True)
    for extra in _STATE_DB_GUARD_EXTRA_DENY_ROOTS:
        try:
            roots.append(Path(extra).expanduser().resolve())
        except Exception:
            logger.debug(
                "Additional production state root resolution failed", exc_info=True
            )
            continue
    return roots


def _is_production_state_db(resolved: Path, root: Path) -> bool:
    """True when *resolved* is a DB file of the real PCBDraft home *root*.

    Matches files directly in the root (``<root>/state.db``) and profile
    homes (``<root>/profiles/<name>/state.db``).  Deliberately does NOT
    match deeper scratch paths (e.g. repo worktrees that happen to live
    under ``<runtime>/worktrees/...``) so hermetic tests using unusual
    tempdirs cannot false-positive.
    """
    if resolved.parent == root:
        return True
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    return len(parts) == 3 and parts[0] == "profiles"


def _ensure_test_isolation(db_path: Path) -> None:
    """Fail hard when a pytest-context process resolves a production DB.

    Raises ``RuntimeError`` before any connection, mkdir, journal-mode
    pragma, or byte probe can touch the live database.  No-op outside
    pytest and for hermetic (tmp ``PCBDRAFT_RUNTIME_HOME``) paths.

    "pytest context" means environment *or* process ancestry — see
    :func:`_in_test_context`.  Env alone is not enough: a child spawned with
    a rebuilt environment loses ``PYTEST_*`` and ``PCBDRAFT_RUNTIME_HOME`` together,
    which is precisely the state in which it writes to production (#82770).
    """
    if _STATE_DB_GUARD_BYPASS or os.environ.get(_STATE_DB_GUARD_BYPASS_ENV):
        return
    if not _in_test_context():
        return
    try:
        resolved = Path(db_path).expanduser().resolve()
    except Exception:
        logger.debug("Test isolation database path resolution failed", exc_info=True)
        return
    for root in _production_state_roots():
        if _is_production_state_db(resolved, root):
            raise RuntimeError(
                "live-system guard: test attempted to open production "
                f"state.db at {resolved} (under real PCBDraft root {root}). "
                "Tests must run against a temporary PCBDRAFT_RUNTIME_HOME — pass an "
                "explicit tmp db_path or let the hermetic conftest redirect "
                "PCBDRAFT_RUNTIME_HOME. If this test genuinely needs the live "
                "database, mark it with "
                "@pytest.mark.live_system_guard_bypass — or, for a spawned "
                f"child process, export {_STATE_DB_GUARD_BYPASS_ENV}=1 in "
                "its environment."
            )


# Last SessionDB() init error, per-process.  Surfaced in /resume and
# related slash-command error strings so users know WHY the DB is
# unavailable instead of getting a bare "Session database not available."
# Only SessionDB.__init__ writes to this; kanban_db.connect() failures
# do not update it (by design — kanban failures are reported via their
# own caller's error handling, not via /resume-style slash commands).
_last_init_error: str | None = None
_last_init_error_lock = threading.Lock()


def _set_last_init_error(msg: str | None) -> None:
    """Record (or clear) the most recent state.db init failure.

    Thread-safe via _last_init_error_lock.  Callers pass a message to
    record a failure or None to clear.  SessionDB.__init__ only calls
    this to SET on failure — it deliberately does NOT clear on success,
    because in a multi-threaded caller (e.g. gateway / web_server per-
    request SessionDB() instantiation), a concurrent successful open
    racing past a different thread's failure would erase the cause
    string that thread's /resume handler is about to format.  Explicit
    clears (e.g. test fixtures) are still supported by passing None.
    """
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = msg


def get_last_init_error() -> str | None:
    """Return the most recent state.db init failure, if any.

    Slash-command handlers (``/resume``, ``/title``, ``/history``, ``/branch``)
    call this to surface the underlying cause in their error messages when
    ``_session_db is None``.  Returns ``None`` if SessionDB initialized
    successfully (or hasn't been attempted).
    """
    return _last_init_error


# Distinctive opening shared by both background-review harness prompts
# (_SKILL_REVIEW_PROMPT and _MEMORY_REVIEW_PROMPT in agent/background_review.py).
# Matched case-sensitively against the leading content of a user/system message.
_REVIEW_HARNESS_PREFIXES = (
    "Review the conversation above and update the skill library",
    "Review the conversation above and consider saving to memory",
)


def _is_background_review_harness_message(msg: dict[str, Any]) -> bool:
    """True when ``msg`` is a persisted background-review harness prompt.

    These are user/system turns the forked skill/memory review agent wrote into
    a real session in older builds (before the ``_persist_disabled`` isolation
    fix). They instruct the agent to act as the curator under a hard tool
    restriction, so replaying them as live history hijacks the session.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("role") not in {"user", "system"}:
        return False
    content = msg.get("content")
    if not isinstance(content, str):
        return False
    head = content.lstrip()
    return any(head.startswith(p) for p in _REVIEW_HARNESS_PREFIXES)


def _strip_background_review_harness(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop background-review harness messages and the curator-mode assistant
    reply that immediately followed each one.

    Walk the list once; when a harness user/system message is found, skip it and
    also skip the next message if it is the assistant turn that answered it.
    Everything else passes through untouched and in order.
    """
    if not messages:
        return messages
    out: list[dict[str, Any]] = []
    skip_next_assistant = False
    for msg in messages:
        if _is_background_review_harness_message(msg):
            skip_next_assistant = True
            continue
        if skip_next_assistant:
            skip_next_assistant = False
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                # The curator-mode reply to the harness prompt — drop it.
                continue
        out.append(msg)
    return out


# Matches a bare protocol/tool-name marker such as "[memory]" or "[skill_manage]".
_STALE_TOOL_CALL_MARKER_RE = re.compile(r"^\[[A-Za-z_][A-Za-z0-9_.-]*\]$")


def _is_stale_tool_call_marker_message(msg: dict[str, Any]) -> bool:
    """True when ``msg`` is a persisted assistant turn whose content is a bare
    bracketed marker (e.g. ``[memory]``) left over from a tool-call turn.

    Before the #78148 fix in ``agent.conversation_loop``, a local tool-call
    template could emit a bare marker as assistant content alongside a real
    tool call. The loop cached that marker as a fallback and later replayed
    it as the "final response", persisting it into the session. Sessions
    written before the fix can still carry these rows.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("role") != "assistant":
        return False
    if not msg.get("tool_calls"):
        return False
    content = msg.get("content")
    if not isinstance(content, str):
        return False
    return bool(_STALE_TOOL_CALL_MARKER_RE.fullmatch(content.strip()))


def _strip_stale_tool_call_markers(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Clear bare protocol-marker content persisted before the #78148 fix.

    Replaying "[memory]" as if the model had actually answered teaches the
    model, by example, to keep emitting the same marker in later turns — the
    exact symptom the issue reported. Only the stray ``content`` field is
    blanked; the tool call and its result are left untouched so provider
    tool_call/tool_result pairing stays intact. Sessions with no affected
    rows pass through unchanged.
    """
    repaired = 0
    for msg in messages:
        if _is_stale_tool_call_marker_message(msg):
            msg["content"] = ""
            repaired += 1
    if repaired:
        logger.info(
            "Cleared %d stale tool-call marker message(s) while restoring session (#78148)",
            repaired,
        )
    return messages


def format_session_db_unavailable(
    prefix: str = "Session database not available",
) -> str:
    """Format a user-facing 'session DB unavailable' message with cause.

    When ``SessionDB()`` init fails, callers set ``_session_db = None`` and
    several slash commands (/resume, /title, /history, /branch) previously
    responded with a bare ``"Session database not available."`` — no
    indication of WHY.  This helper includes the captured cause (typically
    ``"locking protocol"`` from NFS/SMB) and points users at the known
    culprit so they can fix it themselves.

    Example output:
        Session database not available: locking protocol (state.db may be
        on NFS/SMB — see https://www.sqlite.org/wal.html).
    """
    cause = get_last_init_error()
    if not cause:
        return f"{prefix}."
    hint = ""
    if any(marker in cause.lower() for marker in _WAL_INCOMPAT_MARKERS):
        hint = " (state.db may be on NFS/SMB/FUSE/ZFS — see https://www.sqlite.org/wal.html)"
    return f"{prefix}: {cause}{hint}."


# SQLite runtime and journal-mode helpers are re-exported from
# pcbdraft.services.session_db_runtime for backward compatibility.


# ---------------------------------------------------------------------------
# Malformed-schema recovery
# ---------------------------------------------------------------------------
# A distinct, nastier failure class than a malformed FTS *inverted index*:
# the ``sqlite_master`` schema table itself becomes inconsistent — most
# commonly a DUPLICATE object definition, e.g. two ``CREATE VIRTUAL TABLE
# messages_fts`` rows.  SQLite parses the entire schema while preparing the
# FIRST statement on a connection, so on this class *every* statement raises
# before it runs — including ``PRAGMA journal_mode`` (which is why this trips
# in ``apply_wal_with_fallback`` during ``SessionDB.__init__``, long before
# ``_init_schema`` is reached) and even ``PRAGMA integrity_check`` and a plain
# ``DROP TABLE``.  The only operations that still work are
# ``PRAGMA writable_schema=ON`` plus direct ``sqlite_master`` surgery.
#
# Symptom users hit (Desktop/Dashboard show "no sessions" while 200+ JSON
# files sit on disk):
#   sqlite3.DatabaseError: malformed database schema (messages_fts) -
#   table messages_fts already exists
#
# The canonical ``sessions`` / ``messages`` data is intact in these cases —
# only the derived schema is broken — so recovery preserves all transcripts
# and merely rebuilds the FTS layer.
_MALFORMED_SCHEMA_MARKERS = (
    "malformed database schema",
    "database disk image is malformed",
)

# Process-global guard so auto-repair is attempted at most once per DB path
# per process (prevents repair loops and serialises concurrent web_server /
# gateway opens against the same malformed file).
_repair_attempted_paths: set[str] = set()
_repair_attempt_lock = threading.Lock()


def is_malformed_db_error(exc: BaseException) -> bool:
    """True if *exc* is a SQLite 'malformed schema / disk image' error.

    These are the corruption classes where the schema fails to parse, so
    targeted ``sqlite_master`` surgery (not an ordinary FTS rebuild) is the
    only recovery path.
    """
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return any(marker in str(exc).lower() for marker in _MALFORMED_SCHEMA_MARKERS)


def _is_not_a_database_error(exc: BaseException) -> bool:
    """True if *exc* is SQLite's 'file is not a database' error.

    Raised when a connection's backing file is not a SQLite database — the
    runtime connection-corruption class: a sibling process (forked curator
    agent, external repair pass) replaced/truncated the file out from under
    the live connection.  The file on disk may be perfectly healthy; the
    CONNECTION is broken.  Distinct from the malformed-schema class: the fix
    is a reconnect, not schema surgery.
    """
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return "file is not a database" in str(exc).lower()


# Persistence error helpers are re-exported from session_db_runtime.


def _claim_repair_attempt(db_path: Path) -> bool:
    """Claim the one-shot repair attempt for *db_path* in this process.

    Returns True for the first caller, False afterwards. Keeps a malformed
    DB from triggering an unbounded repair/reopen loop and stops concurrent
    callers from racing surgery on the same file.
    """
    key = str(db_path)
    with _repair_attempt_lock:
        if key in _repair_attempted_paths:
            return False
        _repair_attempted_paths.add(key)
        return True


# Cross-process serialisation for the schema-surgery paths below.  The
# ``_repair_attempt_lock`` above is a ``threading.Lock`` — it only covers
# threads inside ONE interpreter, yet a normal PCBDraft host runs several
# independent processes against the same ``state.db``: the gateway service,
# the desktop app's own backend, interactive CLI sessions,
# and the TUI slash worker.  Two of those hitting a malformed DB at once each
# ran the full ``writable_schema`` surgery + ``VACUUM`` on their own private
# connection, with nothing serialising them.
#
# The timeout is sized for the slowest legitimate holder — a ``VACUUM`` over a
# multi-GB DB in strategy 2.  Waiting that long is not a new stall: before this
# lock the losing caller spent the same minutes running its own surgery, it
# just did so on top of the winner's.
_REPAIR_LOCK_TIMEOUT_SECONDS = 120.0
_REPAIR_LOCK_POLL_SECONDS = 0.1
_IS_WINDOWS = sys.platform == "win32"


@contextlib.contextmanager
def _cross_process_repair_lock(db_path: Path):
    """Serialize state.db schema surgery across processes.

    Yields True when this process holds the repair lock for *db_path*, False
    when the bounded acquire timed out.  Unlike the kanban init lock — whose
    critical section is idempotent, so proceeding without the lock is merely
    redundant work — proceeding here would be exactly the unsafe interleaving
    we are trying to prevent, so a caller that gets False must NOT do surgery.

    ``flock`` is the right primitive for this: the kernel drops the lock when
    the holding process dies, so a crashed repairer cannot leave a stale lock
    that wedges every future repair (a pidfile would).  The acquire is still
    bounded because a *live* repairer can legitimately sit in ``VACUUM`` for
    minutes on a large DB, and an unbounded wait would hang the caller's open
    with no traceback (the failure shape of #36644).
    """
    lock_path = db_path.with_name(db_path.name + ".repair.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError as exc:
        # Read-only dir, exhausted fds, exotic filesystem: fall back to the
        # in-process behaviour that shipped before this lock existed rather
        # than refusing to repair a DB we could otherwise heal.
        logger.warning(
            "Could not open state.db repair lock %s (%s) — proceeding with "
            "in-process serialisation only.",
            lock_path,
            exc,
        )
        yield True
        return

    acquired = False
    try:
        deadline = time.monotonic() + _REPAIR_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if _IS_WINDOWS:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    break
                time.sleep(_REPAIR_LOCK_POLL_SECONDS)
        if not acquired:
            logger.warning(
                "state.db repair lock %s held by another process for more "
                "than %.0fs — skipping schema surgery in this process to "
                "avoid racing the repairer.",
                lock_path,
                _REPAIR_LOCK_TIMEOUT_SECONDS,
            )
        yield acquired
    finally:
        try:
            if acquired:
                if _IS_WINDOWS:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - best effort release
            pass
        finally:
            handle.close()


def _bump_schema_cookie(conn: sqlite3.Connection) -> None:
    """Increment the schema cookie after direct ``sqlite_master`` surgery.

    Ordinary DDL bumps this counter for free, and every other connection
    compares it before running a prepared statement — that is how they learn
    to discard a cached schema.  Editing ``sqlite_master`` under
    ``PRAGMA writable_schema=ON`` does NOT bump it, so live connections in
    other processes keep compiling statements against the schema we just
    deleted objects from — e.g. writing ``messages`` rows through triggers
    into ``messages_fts*`` shadow tables that no longer exist.  SQLite's
    writable_schema documentation calls out incrementing ``schema_version``
    as the required companion to such an edit.

    Best-effort and never raises: a failed bump leaves exactly the
    pre-existing behaviour, and the repair itself is still worth completing.
    """
    try:
        current = conn.execute("PRAGMA schema_version").fetchone()[0]
        # Wraps within the 32-bit signed range SQLite stores this in; the
        # comparison other connections make is equality, not ordering.
        conn.execute(f"PRAGMA schema_version={(int(current) + 1) & 0x7FFFFFFF}")
    except (sqlite3.DatabaseError, TypeError, IndexError) as exc:
        logger.warning("Could not bump state.db schema cookie: %s", exc)


# ── Repair-loop bounding + dead-backup hygiene (#86747) ─────────────────────
#
# ``_claim_repair_attempt`` above is an in-memory set: it bounds the loop
# only WITHIN one process. A corruption class the strategies cannot heal
# (b-tree page damage) failed repair on EVERY process start, and each pass
# took a fresh ~900MB forensic backup — 105 attempts / 89GB of identical
# dead copies in the reporting install. Two persistent bounds fix the class:
#
# * a sidecar attempt ledger (``<db>.repair-attempts.json``) that refuses
#   further surgery after ``_MAX_PERSISTENT_REPAIR_ATTEMPTS`` failures on
#   the SAME damaged file (fingerprint = size + mtime; any successful repair
#   or replacement changes it and resets the count);
# * backup dedupe + a retention cap in ``_backup_db_file`` — an identical
#   damaged file is never copied twice, and only the newest
#   ``_MAX_MALFORMED_BACKUPS`` forensic copies are kept.

_MAX_PERSISTENT_REPAIR_ATTEMPTS = 3
_MAX_MALFORMED_BACKUPS = 3


def _repair_ledger_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".repair-attempts.json")


def _db_fingerprint(db_path: Path) -> "str | None":
    """Cheap identity for a damaged DB file: size + mtime_ns.

    Hashing a multi-GB corrupt file on every open is exactly the kind of
    repeated cost this ledger exists to avoid; size+mtime is stable for a
    file nothing can successfully write to, and any successful repair,
    truncation or manual restore changes it (resetting the attempt count).
    """
    try:
        st = db_path.stat()
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return None


def _read_repair_ledger(db_path: Path) -> "dict[str, Any]":
    try:
        raw = json.loads(_repair_ledger_path(db_path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, ValueError):
        pass
    return {}


def _persistent_repair_attempts_exhausted(db_path: Path) -> bool:
    """Whether *db_path* has already burned its cross-restart repair budget.

    True only when the ledger records ``_MAX_PERSISTENT_REPAIR_ATTEMPTS``
    failed attempts against the CURRENT file fingerprint. Never raises; a
    missing/corrupt ledger or unstatable DB reads as "not exhausted" (the
    in-process claim and cross-process lock still bound a single run).
    """
    fp = _db_fingerprint(db_path)
    if fp is None:
        return False
    ledger = _read_repair_ledger(db_path)
    return (
        ledger.get("fingerprint") == fp
        and int(ledger.get("failed_attempts", 0)) >= _MAX_PERSISTENT_REPAIR_ATTEMPTS
    )


def _record_repair_outcome(
    db_path: Path, *, repaired: bool, fingerprint: "str | None" = None
) -> None:
    """Update the persistent attempt ledger after a repair pass. Never raises.

    Defaults to the post-attempt fingerprint — the file state the NEXT
    attempt's exhaustion probe will observe.
    """
    ledger_path = _repair_ledger_path(db_path)
    try:
        if repaired:
            ledger_path.unlink(missing_ok=True)
            return
        fp = fingerprint if fingerprint is not None else _db_fingerprint(db_path)
        if fp is None:
            return
        ledger = _read_repair_ledger(db_path)
        attempts = (
            int(ledger.get("failed_attempts", 0)) + 1
            if ledger.get("fingerprint") == fp
            else 1
        )
        import datetime

        ledger_path.write_text(
            json.dumps(
                {
                    "fingerprint": fp,
                    "failed_attempts": attempts,
                    "last_attempt": datetime.datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )
    except Exception:  # pragma: no cover - best effort
        logger.warning("Could not update state.db repair ledger", exc_info=True)


def _existing_malformed_backups(db_path: Path) -> "list[Path]":
    """Timestamped forensic backups of *db_path*, newest first."""
    prefix = f"{db_path.name}.malformed-backup-"
    try:
        found = [
            p
            for p in db_path.parent.iterdir()
            if p.name.startswith(prefix) and not p.name.endswith(("-wal", "-shm"))
        ]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name, reverse=True)


def _prune_malformed_backups(db_path: Path, keep: int = _MAX_MALFORMED_BACKUPS) -> None:
    """Delete all but the *keep* newest forensic backups (and sidecars)."""
    for stale in _existing_malformed_backups(db_path)[keep:]:
        for victim in (
            stale,
            stale.with_name(stale.name + "-wal"),
            stale.with_name(stale.name + "-shm"),
        ):
            try:
                victim.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - best effort
                logger.warning("Could not prune stale DB backup %s: %s", victim, exc)


def _backup_db_file(db_path: Path) -> "tuple[Path | None, str | None]":
    """Copy a (possibly malformed) DB file to a timestamped backup beside it.
    Raw file copy on purpose: the DB won't open cleanly, so we preserve the
    bytes exactly for forensics / manual restore. WAL and SHM sidecars are
    copied too when present. Returns ``(backup_path, None)`` on success or
    ``(None, reason)`` on failure — callers on the repair path treat a
    refused backup as a HARD STOP (see #69603: proceeding without the
    pre-repair backup leaves the writable_schema surgery, FTS deletion and
    VACUUM strategies mutating the only remaining copy of the damaged DB).

    Refuses when a connection to this database is still live in the process:
    reading the file would ``close()`` a descriptor for it and cancel that
    connection's POSIX advisory locks (see ``pcbdraft.services.sqlite_safe_read``).
    The repair path can be entered by one SessionDB while the gateway holds
    others, so this is a real possibility rather than a theoretical one.
    """
    import datetime
    import shutil

    try:
        from pcbdraft.interfaces.tui.sqlite_safe_read import has_live_connection
    except ImportError:
        has_live_connection = None  # type: ignore[assignment]

    if has_live_connection is not None and has_live_connection(db_path):
        reason = (
            f"a connection to {db_path} is still open in this process; "
            "raw-copying it would cancel that connection's POSIX advisory "
            "locks. Close all SessionDB handles first."
        )
        logger.error("Refusing to raw-copy %s for backup: %s", db_path, reason)
        return None, reason

    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.malformed-backup-{stamp}")
    # Same-second collision (two distinct damaged states within one second)
    # must not silently overwrite the earlier forensic copy.
    seq = 1
    while backup_path.exists():
        backup_path = db_path.with_name(
            f"{db_path.name}.malformed-backup-{stamp}_{seq}"
        )
        seq += 1
    try:
        # Dedupe (#86747): a repair loop used to copy the SAME damaged bytes
        # on every restart — ~900MB a pass, 89GB over 11 days in the
        # reporting install. If the newest existing backup already matches
        # this file (size + mtime preserved by copy2), reuse it.
        try:
            src_stat = db_path.stat()
            for existing in _existing_malformed_backups(db_path)[:1]:
                est = existing.stat()
                if (
                    est.st_size == src_stat.st_size
                    and est.st_mtime_ns == src_stat.st_mtime_ns
                ):
                    logger.info(
                        "Reusing existing forensic backup %s (identical to the "
                        "damaged DB).",
                        existing,
                    )
                    return existing, None
        except OSError:
            pass
        shutil.copy2(db_path, backup_path)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, backup_path.with_name(backup_path.name + suffix))
        # Retention cap (#86747): keep only the newest few forensic copies.
        _prune_malformed_backups(db_path)
        return backup_path, None
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Could not back up malformed DB", exc_info=True)
        return None, f"backup copy failed: {exc}"


def preflight_db_writability(
    db_path: Path,
    *,
    db_label: str = "state.db",
) -> None:
    """Refuse-or-repair read-only DB files BEFORE the first connection opens.

    Port of Kilo-Org/kilocode#12508's startup preflight. A stray read-only
    ``state.db`` / ``-wal`` / ``-shm`` (sudo run, restored backup, copied
    dotfiles) previously surfaced as an opaque
    ``sqlite3.OperationalError: attempt to write a readonly database`` raised
    from deep inside ``_init_schema`` — naming no file and no fix — and the
    obvious wrong "fix" (deleting the ``-wal``) silently loses committed
    transactions. This preflight:

    - **Repairs** permissions with ``chmod u+rw`` when the file lives inside
      the PCBDraft home tree (``get_runtime_home()``) — the safe repair scope:
      PCBDraft owns those files, and the OS makes ``chmod`` fail on files the
      user doesn't own, which bounds the repair exactly.
    - **Fails fast with an actionable error** naming the exact file and the
      exact ``chmod`` command for anything else (root-owned files, read-only
      mounts, custom paths outside the home tree).
    - Never deletes or truncates a WAL sidecar — once writable, the normal
      open path checkpoints its committed frames into the DB as intended.

    ``:memory:`` and ``file:`` URI paths are skipped (no plain on-disk files
    to check). Shared by :class:`SessionDB` and the kanban database module.
    """
    raw = str(db_path)
    if raw == ":memory:" or raw.startswith("file:"):
        return

    try:
        home: Path | None = Path(get_runtime_home()).resolve()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Database permission repair scope unavailable", exc_info=True)
        home = None

    def _in_repair_scope(p: Path) -> bool:
        if home is None:
            return False
        try:
            return p.resolve().is_relative_to(home)
        except (OSError, ValueError):
            return False

    def _ensure_writable(p: Path, *, is_dir: bool = False) -> None:
        import stat as _stat

        if os.access(p, os.R_OK | os.W_OK):
            return
        if _in_repair_scope(p):
            try:
                add = _stat.S_IRUSR | _stat.S_IWUSR | (_stat.S_IXUSR if is_dir else 0)
                os.chmod(p, p.stat().st_mode | add)
            except OSError:
                pass
            if os.access(p, os.R_OK | os.W_OK):
                logger.info(
                    "%s preflight: repaired read-only %s (chmod u+rw%s)",
                    db_label,
                    p,
                    "x" if is_dir else "",
                )
                return
        kind = "directory" if is_dir else "file"
        wal_note = (
            " Do NOT delete the -wal file — it contains committed data that "
            "will be merged into the database once it is writable."
            if p.name.endswith("-wal")
            else ""
        )
        raise sqlite3.OperationalError(
            f"{db_label} is not writable: {kind} {p} is read-only for this "
            f"user. PCBDraft needs read-write access to open the database. "
            f"Fix with: chmod u+rw{'x' if is_dir else ''} '{p}'"
            f" (files owned by another user may need sudo/chown).{wal_note}"
        )

    parent = db_path.parent
    if parent.is_dir():
        # SQLite needs a writable directory in every journal mode (WAL and
        # SHM sidecars in WAL mode; the rollback journal in DELETE mode).
        _ensure_writable(parent, is_dir=True)

    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix) if suffix else db_path
        if p.is_file():
            _ensure_writable(p)


def _db_opens_cleanly(db_path: Path) -> str | None:
    """Probe a DB on a fresh connection. Returns None if healthy, else a reason.

    Runs the same first-statement (``PRAGMA journal_mode``) that trips the
    malformed-schema parse, then ``PRAGMA integrity_check`` and a canonical
    ``sessions`` read, and finally a rolled-back ``messages`` write so that
    FTS5 index corruption — which leaves base-table reads and
    ``integrity_check`` passing while every ``INSERT INTO messages`` fails
    through the FTS triggers — is reported as unhealthy rather than slipping
    past as a false "ok" (#50502).
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # Best-effort tokenizer load: a DB carrying the messages_fts_cjk
        # index needs the cjk_unicode61 extension before any statement can
        # touch that table — including the trigger-driven write probe below.
        # Without it, this probe sees the DB exactly as a tokenizer-less
        # SessionDB open would (which drops the cjk triggers to keep writes
        # working), so tokenizer absence must never classify as corruption.
        load_fts5_cjk_extension(conn)
        conn.execute("PRAGMA journal_mode").fetchone()
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        problems = [str(r[0]) for r in rows if r and str(r[0]).lower() != "ok"]
        if problems:
            return "; ".join(problems[:3])
        conn.execute("SELECT COUNT(*) FROM sessions").fetchone()

        # FTS5 read probe: run a representative MATCH query against the
        # messages_fts* virtual tables. The FTS *write* probe below catches
        # the corruption class where base tables read fine but writes fail
        # through the triggers (#50502). It does NOT catch partial FTS5
        # index corruption — bad shadow-table segments where reads still
        # parse but MATCH / snippet / rank queries error out with
        # "database disk image is malformed" (a `sqlite3.DatabaseError`,
        # not `OperationalError`). session_search, /resume title resolution,
        # and any feature relying on FTS5 discovery then break silently
        # because the official repair tool's check-only path reports the
        # DB as healthy. #66724.
        # Catch the full sqlite3 exception hierarchy (not just
        # OperationalError) so the malformed-shadow-table class is reported
        # rather than letting it crash the caller.
        for fts_table in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
            try:
                # No-op queries against the actual FTS5 APIs the search
                # tools use. The trigram table is included because it backs
                # the title-resolution path; either corruption mode would
                # break session recall without this probe. MATCH '""' is
                # the empty phrase-token probe — FTS5 rejects MATCH ''
                # outright ("fts5: syntax error"), but a quoted empty
                # phrase parses, scans zero rows, and exercises the same
                # shadow-table read path the search tools use.
                conn.execute(
                    f"SELECT 1 FROM {fts_table} WHERE {fts_table} MATCH '\"\"' LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                # Use the canonical capability classifier instead of a
                # hand-rolled substring check. On SQLite builds without the
                # fts5 module, the legacy messages_fts table may exist on
                # disk (from a prior build that had FTS5) and MATCH queries
                # against it raise OperationalError("no such module: fts5");
                # the substring check below would misclassify that as
                # corruption and send the DB into the repair path, whose
                # final fallback deletes the messages_fts% schema
                # (hermes_state.py:645-723). The supported degraded-runtime
                # path (SessionDB._is_fts5_unavailable_error + the
                # regression suite in tests/test_hermes_state.py:600-632)
                # treats both "no such module: fts5" and
                # "no such tokenizer: trigram" as the capability error.
                if SessionDB._is_fts5_unavailable_error(exc):
                    # Degraded runtime — not the corruption class we probe.
                    continue
                msg = str(exc).lower()
                if "no such table" in msg or "no such column" in msg:
                    # FTS5 not built yet (brand new file mid-init) — not the
                    # corruption class we probe.
                    continue
                return f"fts5 read probe failed on {fts_table}: {exc}"
            except sqlite3.DatabaseError as exc:
                # This is the corruption class #66724 actually wants caught:
                # partial shadow-table damage where MATCH / snippet / rank
                # queries raise DatabaseError("database disk image is malformed")
                # while reads of the FTS5 table itself parse fine.
                return f"fts5 read probe failed on {fts_table}: {exc}"

        # FTS write probe: drive a row through the messages_fts* triggers in a
        # transaction that is always rolled back, so a corrupt FTS index that
        # rejects writes is caught even though reads look healthy. The probe is
        # best-effort — if the messages/sessions tables don't exist yet (brand
        # new file mid-init) the OperationalError is treated as "not yet a
        # populated DB", not corruption.
        probe_session_id = f"_pcbdraft_fts_health_probe_{time.time_ns()}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                (probe_session_id, "_health_probe", time.time()),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (probe_session_id, "user", "_fts_health_probe", time.time()),
            )
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            # Missing tables / FTS disabled — not the corruption class we probe.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            msg = str(exc).lower()
            if "no such table" in msg or "no such column" in msg:
                return None
            if "no such tokenizer: cjk_unicode61" in msg:
                # This probe process couldn't load the cjk extension while
                # the DB carries the cjk index — capability gap, not
                # corruption. A tokenizer-capable SessionDB serves it fine;
                # a tokenizer-less one self-heals by dropping the triggers.
                return None
            return str(exc)
        return None
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        conn.close()


def repair_state_db_schema(db_path: Path, *, backup: bool = True) -> dict[str, Any]:
    """Repair a state.db whose ``sqlite_master`` schema is malformed or whose
    FTS indexes reject writes.

    Handles two corruption classes: the "duplicate object definition" /
    malformed-schema class where even ``PRAGMA`` statements fail, and the FTS
    write-corruption class (#50502) where base tables read fine and
    ``integrity_check`` passes but writes fail through the ``messages_fts*``
    triggers. Tries least-destructive recovery first and escalates:

      1. **Rebuild FTS indexes in place** via the FTS5 ``'rebuild'`` command,
         which rewrites the internal b-tree segments from the canonical
         ``messages`` rows without dropping or recreating anything. Fixes the
         FTS write-corruption class while preserving the schema intact.
      2. **De-duplicate** ``sqlite_master`` (keep the lowest rowid per
         ``type``/``name``). Fixes the canonical "table X already exists"
         case and PRESERVES the existing FTS index intact.
      3. **Drop the FTS schema** (every ``messages_fts*`` object) + ``VACUUM``.
         The next ``SessionDB()`` open rebuilds the FTS indexes from the
         canonical ``messages`` table.

    Canonical ``sessions`` / ``messages`` rows are never modified. A
    timestamped raw backup is taken first unless ``backup=False``.

    The surgery below is serialised across processes (see
    :func:`_cross_process_repair_lock`): the gateway service, the Desktop
    app's backend and interactive CLI sessions all open the same file, and
    two of them running ``writable_schema`` surgery concurrently is itself a
    corruption source.

    Returns a report dict: ``{repaired: bool, strategy: str|None,
    backup_path: str|None, error: str|None}``.
    """
    report: dict[str, Any] = {
        "repaired": False,
        "strategy": None,
        "backup_path": None,
        "error": None,
    }

    db_path = Path(db_path)
    if not db_path.exists():
        report["error"] = f"{db_path} does not exist"
        return report

    # Cross-restart attempt cap (#86747): the in-memory claim bounds one
    # process, but a corruption class the strategies below cannot heal
    # (b-tree page damage) previously re-ran the whole surgery — and took a
    # fresh multi-hundred-MB forensic backup — on EVERY restart, forever.
    # After _MAX_PERSISTENT_REPAIR_ATTEMPTS failures against the same
    # damaged file, stop retrying and surface a terminal, actionable error.
    if _persistent_repair_attempts_exhausted(db_path):
        report["error"] = (
            f"automatic repair has already failed "
            f"{_MAX_PERSISTENT_REPAIR_ATTEMPTS} times on this exact file — "
            "the corruption is beyond the schema/FTS repair strategies "
            "(likely b-tree page damage). Manual recovery required: restore "
            f'a backup, or salvage with `sqlite3 {db_path} ".recover"`. '
            f"Delete {_repair_ledger_path(db_path).name} to force another "
            "automatic attempt."
        )
        logger.error("state.db repair skipped: %s", report["error"])
        return report

    with _cross_process_repair_lock(db_path) as holding_lock:
        if not holding_lock:
            # Another process is still inside its critical section. It may
            # nonetheless have healed the file already (long VACUUM after a
            # successful strategy), so re-probe before reporting failure.
            if _db_opens_cleanly(db_path) is None:
                report["repaired"] = True
                report["strategy"] = "repaired_by_other_process"
                return report
            report["error"] = (
                "another process holds the state.db repair lock; skipped "
                "schema surgery to avoid racing it"
            )
            return report
        result = _repair_state_db_schema_locked(db_path, backup=backup, report=report)
        # Persist the outcome AFTER surgery, keyed on the post-attempt
        # fingerprint — that is the file state the NEXT attempt's exhaustion
        # probe will observe. Failures count toward the cross-restart cap;
        # success clears the ledger. (A failing strategy that mutates the
        # file re-keys the ledger and restarts the count: that keeps a
        # genuinely NEW corruption event from inheriting a stale budget,
        # while the backup dedupe/cap above bounds the disk cost either way.)
        _record_repair_outcome(db_path, repaired=bool(result.get("repaired")))
        return result


def _repair_state_db_schema_locked(
    db_path: Path, *, backup: bool, report: dict[str, Any]
) -> dict[str, Any]:
    """Repair strategies for :func:`repair_state_db_schema`.

    Caller must hold the cross-process repair lock for *db_path*.
    """
    # Re-probe under the lock: a process we queued behind may have just
    # repaired the file, in which case redoing the surgery would undo its
    # work on a now-healthy DB (the repair/re-corrupt cascade this lock
    # exists to break).
    if _db_opens_cleanly(db_path) is None:
        report["repaired"] = True
        report["strategy"] = "already_healthy"
        return report

    if backup:
        bpath, backup_error = _backup_db_file(db_path)
        report["backup_path"] = str(bpath) if bpath else None
        if bpath is None:
            # HARD STOP (#69603): every strategy below mutates the damaged
            # file in place (FTS rebuild, REINDEX, writable_schema surgery,
            # VACUUM). Without the pre-repair backup, the damaged DB is the
            # only copy of the user's data — a failed or interrupted repair
            # would then be unrecoverable. Abort and surface the reason
            # instead of proceeding fail-open.
            report["error"] = (
                "pre-repair backup refused; aborting schema repair to avoid "
                f"mutating the only copy of the damaged DB: {backup_error}"
            )
            logger.error("state.db repair aborted: %s", report["error"])
            return report

    # ── Strategy 0: rebuild FTS indexes in place (FTS write-corruption) ──
    # The FTS5 'rebuild' command rewrites the internal index from the canonical
    # content table. This is the recommended, least-destructive recovery for a
    # corrupt FTS index that rejects message writes while reads still succeed.
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            # The cjk index can only be rebuilt with its tokenizer loaded;
            # best-effort (a tokenizer-less host skips it at the probe below).
            load_fts5_cjk_extension(conn)
            for table_name in (
                "messages_fts",
                "messages_fts_trigram",
                "messages_fts_cjk",
            ):
                try:
                    conn.execute(
                        f"INSERT INTO {table_name}({table_name}) VALUES('rebuild')"
                    )
                except sqlite3.OperationalError:
                    # Table absent (FTS disabled / trigram off / cjk not
                    # present or tokenizer unavailable) — skip it.
                    continue
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report["repaired"] = True
            report["strategy"] = "rebuild_fts"
            logger.warning(
                "state.db FTS indexes rebuilt in place (schema preserved): %s",
                db_path,
            )
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning("state.db FTS in-place rebuild pass failed: %s", exc)

    # ── Strategy 0.5: rebuild stale B-tree indexes (#63386) ──
    # PRAGMA integrity_check can report "wrong # of entries in index" when a
    # B-tree index (e.g. idx_sessions_handoff_state) falls out of sync with its
    # base table. REINDEX rewrites the index b-tree from the canonical table
    # rows using the existing index definition, fixing the mismatch without
    # touching data or FTS schema.
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("REINDEX")
            conn.commit()
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report["repaired"] = True
            report["strategy"] = "reindex_btree"
            logger.warning("state.db B-tree indexes rebuilt via REINDEX: %s", db_path)
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning("state.db REINDEX pass failed: %s", exc)

    # ── Strategy 1: de-duplicate sqlite_master (keeps FTS index) ──
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA writable_schema=ON")
            dupes = conn.execute(
                "SELECT type, name, COUNT(*) AS c, MIN(rowid) AS keep "
                "FROM sqlite_master GROUP BY type, name HAVING c > 1"
            ).fetchall()
            for type_, name, _count, keep in dupes:
                conn.execute(
                    "DELETE FROM sqlite_master "
                    "WHERE type IS ? AND name IS ? AND rowid <> ?",
                    (type_, name, keep),
                )
            if dupes:
                _bump_schema_cookie(conn)
            conn.execute("PRAGMA writable_schema=OFF")
            conn.commit()
        finally:
            conn.close()
        if _db_opens_cleanly(db_path) is None:
            report["repaired"] = True
            report["strategy"] = "dedup_schema"
            logger.warning(
                "state.db schema repaired by de-duplicating sqlite_master "
                "(FTS index preserved): %s",
                db_path,
            )
            return report
    except sqlite3.DatabaseError as exc:
        logger.warning("state.db dedup repair pass failed: %s", exc)

    # ── Strategy 2: drop all FTS schema, VACUUM, rebuild on next open ──
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%'")
            _bump_schema_cookie(conn)
            conn.execute("PRAGMA writable_schema=OFF")
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        reason = _db_opens_cleanly(db_path)
        if reason is None:
            report["repaired"] = True
            report["strategy"] = "drop_fts_rebuild"
            logger.warning(
                "state.db schema repaired by dropping FTS schema; indexes "
                "will rebuild from messages on next open: %s",
                db_path,
            )
            return report
        report["error"] = reason
    except sqlite3.DatabaseError as exc:
        report["error"] = str(exc)

    if not report["repaired"]:
        logger.error(
            "state.db schema repair could not recover %s automatically "
            "(backup: %s); manual restore from backup may be required.",
            db_path,
            report["backup_path"],
        )
    return report


# ── CJK-bigram FTS index (replaces the trigram index when available) ────
#
# The trigram tokenizer needs >=3 chars per query term, so 1-2 char CJK
# terms (ubiquitous in Korean/Chinese: 일본, 구글, 项目, ...) fall through
# to a LIKE full-table scan — measured 3-6s CPU per query on multi-GB
# installs and the dominant base cost of session_search on CJK workloads.
#
# ``cjk_unicode61`` (native/fts5_cjk/, a ~250-line loadable FTS5 tokenizer
# with no dependencies) wraps unicode61: maximal CJK runs are re-emitted as
# overlapping character bigrams (Lucene CJKAnalyzer semantics), everything
# else passes through unchanged. FTS5 phrase semantics turn a query term's
# consecutive bigrams into exact substring matching down to 2 chars at
# index speed. Contributed by Soju06 (PR #65544).
#
# Same v23 storage discipline as the trigram table it replaces:
# external-content over a tool-row-excluding view (zero inline text
# copies; tool rows stay searchable via ``messages_fts``), triggers gated
# on a DEDICATED marker pair (``fts_cjk_rebuild_high_water`` /
# ``fts_cjk_rebuild_progress``) so a cjk-only backfill — e.g. the
# trigram→cjk upgrade on an already-optimized DB — never gates the
# complete ``messages_fts`` index's triggers.
#
# The table exists ONLY when the loadable tokenizer is available
# (``<runtime_home>/lib/libfts5_cjk.so``, built by ``native/fts5_cjk/build.sh``).
# A process that cannot load it self-heals by dropping the cjk triggers
# (message writes keep working; the index goes stale and is rebuilt by the
# next offline storage optimization on a capable host).
#
# Split DDL: the table/view part is safe to ensure any time; the triggers
# are created ONLY while the index is complete-or-marker-gated. A stale
# index (trigger gap of unknown extent) must keep its triggers DROPPED —
# an external-content 'delete' op for a rowid the index never held is the
# canonical FTS5 index-corruption hazard the v23 marker gating exists to
# prevent.
FTS_CJK_TABLE_SQL = """
CREATE VIEW IF NOT EXISTS messages_fts_cjk_src AS
    SELECT id, role, content, tool_name, tool_calls
    FROM messages
    WHERE role <> 'tool';

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_cjk USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages_fts_cjk_src',
    content_rowid='id',
    tokenize='cjk_unicode61'
);
"""

FTS_CJK_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_insert AFTER INSERT ON messages
WHEN new.role <> 'tool'
   AND (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_delete AFTER DELETE ON messages
WHEN old.role <> 'tool'
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_update
AFTER UPDATE OF content, tool_name, tool_calls, role ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls
    OR old.role IS NOT new.role)
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)
    SELECT 'delete', old.id, old.content, old.tool_name, old.tool_calls
    WHERE old.role <> 'tool';
    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
    SELECT new.id, new.content, new.tool_name, new.tool_calls
    WHERE new.role <> 'tool';
END;
"""


def fts5_cjk_so_path() -> Path:
    """Location of the cjk_unicode61 loadable extension."""
    env = os.getenv("PCBDRAFT_RUNTIME_FTS5_CJK_SO")
    if env:
        return Path(env).expanduser()
    return get_runtime_home() / "lib" / "libfts5_cjk.so"


def _cjk_fts_config_enabled() -> bool:
    """config.yaml ``sessions.cjk_fts`` (default on), via its env bridge."""
    return os.getenv("PCBDRAFT_RUNTIME_CJK_FTS", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def load_fts5_cjk_extension(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the cjk_unicode61 tokenizer into ``conn``.

    Returns False (never raises) when the .so is absent, the feature is
    disabled via ``sessions.cjk_fts``, or this Python build has extension
    loading compiled out — every caller treats False as "behave exactly as
    before the cjk index existed".
    """
    if not _cjk_fts_config_enabled():
        return False
    path = fts5_cjk_so_path()
    if not path.exists():
        return False
    try:
        conn.enable_load_extension(True)
        try:
            conn.load_extension(str(path))
        finally:
            conn.enable_load_extension(False)
        return True
    except Exception:
        logger.warning("fts5_cjk extension load failed (%s)", path, exc_info=True)
        return False


# Session lifecycle exceptions are re-exported from session_db_runtime.


def _connect_tracked_db(path, tracking_path=None, **kwargs):
    """``sqlite3.connect`` that registers the open fd for lock-safety.

    While a connection is live, byte-level probes of the same file are
    refused: an ``open()``/``close()`` cancels every POSIX advisory lock this
    process holds on it -- including a running VACUUM's EXCLUSIVE lock.
    Released automatically on ``close()``.

    The ONLY tolerated fallback is the helper being absent entirely
    (scaffold/embed installs that ship SessionDB without the terminal). A
    real connection failure must propagate: silently retrying an *untracked*
    connect would disable the guard for the lifetime of that connection,
    which is precisely the failure mode this module exists to prevent.
    """
    try:
        from pcbdraft.interfaces.tui.sqlite_safe_read import connect_tracked
    except ImportError:
        logger.debug(
            "pcbdraft.services.sqlite_safe_read unavailable; opening %s untracked "
            "(byte-probe guard inactive in this install)",
            path,
        )
        return sqlite3.connect(str(path), **kwargs)

    # Open through THIS module's sqlite3.connect so callers (and tests) that
    # patch session_db.sqlite3.connect keep control of connection creation;
    # the helper still owns tracking.
    return connect_tracked(
        path,
        tracking_path=tracking_path,
        connect_fn=sqlite3.connect,
        **kwargs,
    )


def is_zeroed_state_db(
    path: Path, *, probe_bytes: int = 100, force: bool = False
) -> bool:
    """Detect the #68474 zeroed state.db signature (size>0, NUL header).

    Byte-level probe, so it is only safe BEFORE any connection to *path*
    exists in this process: ``close()`` cancels every POSIX advisory lock the
    process holds on the file, which can pull the EXCLUSIVE lock out from
    under a running VACUUM and corrupt the database. The read is routed
    through ``read_header_bytes_preopen``, which refuses (returning False
    here) once a connection is live. Pass ``force=True`` only for offline
    files -- quarantined copies, snapshots, archives.

    Prefer ``pcbdraft.services.backup.is_zeroed_sqlite_file`` when available; this
    local copy keeps SessionDB openable without importing the CLI package
    in constrained embed paths.
    """
    try:
        from pcbdraft.interfaces.tui.backup import is_zeroed_sqlite_file

        return is_zeroed_sqlite_file(path, probe_bytes=probe_bytes, force=force)
    except Exception:
        logger.debug(
            "Shared zeroed-database probe unavailable; using fallback", exc_info=True
        )
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    from pcbdraft.interfaces.tui.sqlite_safe_read import read_header_bytes_preopen

    head = read_header_bytes_preopen(path, length=max(16, probe_bytes), force=force)
    if not head or head.startswith(b"SQLite format 3"):
        return False
    return all(byte == 0 for byte in head)


def quarantine_zeroed_state_db(path: Path) -> Path | None:
    """Move a zeroed state.db aside (preserve bytes) and return quarantine path.

    Uses a cross-process lock (``#68805``) so two concurrent startups cannot
    race: the first process moves the zeroed file and the second re-checks
    under the lock, finding the file already gone (or a fresh DB in its place)
    instead of clobbering the quarantine.
    """
    import platform

    lock_path = path.with_name(path.name + ".quarantine.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        deadline = time.monotonic() + 5.0
        if platform.system() == "Windows":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.020)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.020)
        if not acquired:
            # Fail closed: do NOT proceed without the lock. A slow or paused
            # startup that still owns the lock can overlap this fallback and
            # the two processes can act on the same live file (#68805 review).
            logger.error(
                "quarantine lock for %s not acquired within 5s — refusing to "
                "quarantine without the cross-process lock. The zeroed file "
                "is left in place. If sessions fail to load, restore from "
                "state-snapshots; run `pcbdraft doctor` for diagnostics.",
                path,
            )
            return None
        # Re-check under the lock: another process may have already quarantined
        # the file, leaving a fresh DB (or no file at all) in its place.
        if not path.exists():
            logger.info(
                "quarantine_zeroed_state_db: %s already moved by another process",
                path,
            )
            return None
        if not is_zeroed_state_db(path):
            logger.info(
                "quarantine_zeroed_state_db: %s is no longer zeroed (another "
                "process quarantined it and a fresh DB was created)",
                path,
            )
            return None

        try:
            ts = time.strftime("%Y%m%d-%H%M%S")
        except Exception:
            logger.debug("Zeroed-database backup timestamp unavailable", exc_info=True)
            ts = "unknown"
        # Unique destination with PID suffix to avoid collision across
        # concurrent startups that somehow both enter the lock.
        dest = path.with_name(f"{path.name}.zeroed-{ts}-{os.getpid()}.bak")
        # Non-clobbering: if dest somehow exists, append a counter.
        n = 0
        while dest.exists():
            n += 1
            dest = path.with_name(f"{path.name}.zeroed-{ts}-{os.getpid()}-{n}.bak")
        try:
            path.rename(dest)
        except OSError as exc:
            logger.error("Failed to quarantine zeroed %s: %s", path, exc)
            return None
        # Also move empty WAL/SHM if present so a fresh open is clean
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                try:
                    side.rename(Path(str(dest) + suffix))
                except OSError:
                    pass
        return dest
    finally:
        try:
            if acquired:
                if platform.system() == "Windows":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, AttributeError):
            pass
        finally:
            handle.close()


# ── Read-only health/stats probes (pcbdraft doctor, dashboards) ──────────


def collect_state_db_stats(db_path: Path) -> dict[str, Any]:
    """Best-effort, strictly read-only stats snapshot of a state.db file.

    Opens the database with ``mode=ro`` (URI) and a short timeout so it can
    run against a *live* database held by a gateway without ever taking a
    write lock or mutating the file. Every field is collected independently:
    a failed pragma/SELECT yields ``None`` for that field, and the helper
    itself never raises.

    Deliberately does NOT instantiate :class:`SessionDB` — its constructor
    runs schema DDL (migrations, FTS table creation), which is exactly the
    kind of write a diagnostics probe must never perform.

    Returned keys (all present, any may be None on failure):

    - ``page_count``, ``page_size``, ``freelist_count`` — PRAGMA values
    - ``logical_size_bytes`` — page_count * page_size (post-checkpoint size)
    - ``wal_size_bytes`` — stat() of ``<db>-wal`` (0 when absent)
    - ``journal_mode`` — PRAGMA journal_mode string
    - ``messages`` / ``sessions`` — row counts
    - ``fts_tables`` — dict of {table_name: bool} presence for
      messages_fts / messages_fts_trigram / messages_fts_cjk
    - ``fts_storage_version`` — int from state_meta, None when the marker is
      absent (legacy pre-v23 inline layout)
    - ``fts_rebuild_pending`` — True when the deferred v23 backfill has not
      finished (high_water present and progress < high_water)
    - ``fts_rebuild_high_water`` / ``fts_rebuild_progress`` — raw ints
    """
    stats: dict[str, Any] = {
        "page_count": None,
        "page_size": None,
        "freelist_count": None,
        "logical_size_bytes": None,
        "wal_size_bytes": None,
        "journal_mode": None,
        "messages": None,
        "sessions": None,
        "fts_tables": None,
        "fts_storage_version": None,
        "fts_rebuild_pending": None,
        "fts_rebuild_high_water": None,
        "fts_rebuild_progress": None,
    }

    # WAL sidecar size needs no connection at all.
    try:
        wal_path = Path(str(db_path) + "-wal")
        stats["wal_size_bytes"] = wal_path.stat().st_size if wal_path.exists() else 0
    except OSError:
        pass

    conn = None
    try:
        # mode=ro refuses to create the file and refuses every write; a
        # short timeout keeps doctor snappy when a writer holds the lock.
        # Route through the tracked connect so byte-probe helpers
        # (read_header_bytes_preopen) see this connection and refuse raw
        # opens that could cancel our POSIX locks mid-read.
        conn = _connect_tracked_db(
            f"file:{Path(db_path)}?mode=ro",
            tracking_path=Path(db_path),
            uri=True,
            timeout=2.0,
        )
    except Exception:
        logger.debug(
            "collect_state_db_stats: cannot open database read-only", exc_info=True
        )
        return stats

    def _scalar(sql: str) -> Any:
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row else None
        except Exception:
            logger.debug("Database statistics scalar probe failed", exc_info=True)
            return None

    try:
        pc = _scalar("PRAGMA page_count")
        ps = _scalar("PRAGMA page_size")
        stats["page_count"] = int(pc) if pc is not None else None
        stats["page_size"] = int(ps) if ps is not None else None
        if stats["page_count"] is not None and stats["page_size"] is not None:
            stats["logical_size_bytes"] = stats["page_count"] * stats["page_size"]

        fl = _scalar("PRAGMA freelist_count")
        stats["freelist_count"] = int(fl) if fl is not None else None

        jm = _scalar("PRAGMA journal_mode")
        stats["journal_mode"] = str(jm) if jm is not None else None

        msgs = _scalar("SELECT COUNT(*) FROM messages")
        stats["messages"] = int(msgs) if msgs is not None else None
        sess = _scalar("SELECT COUNT(*) FROM sessions")
        stats["sessions"] = int(sess) if sess is not None else None

        # FTS table presence via sqlite_master (never SELECTs from the
        # virtual tables themselves — a corrupt index must not fail stats).
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN (?, ?, ?)",
                    ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"),
                ).fetchall()
            }
            stats["fts_tables"] = {
                t: (t in names)
                for t in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk")
            }
        except Exception:
            logger.debug("Database statistics FTS table probe failed", exc_info=True)

        # Raw state_meta reads — cheap, and independent of SessionDB.
        def _meta_int(key: str) -> int | None:
            try:
                row = conn.execute(
                    "SELECT value FROM state_meta WHERE key = ?", (key,)
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else None
            except Exception:
                logger.debug("Database statistics metadata probe failed", exc_info=True)
                return None

        stats["fts_storage_version"] = _meta_int("fts_storage_version")
        high_water = _meta_int("fts_rebuild_high_water")
        progress = _meta_int("fts_rebuild_progress")
        stats["fts_rebuild_high_water"] = high_water
        stats["fts_rebuild_progress"] = progress
        if high_water is None:
            stats["fts_rebuild_pending"] = False
        else:
            stats["fts_rebuild_pending"] = (progress or 0) < high_water
    finally:
        try:
            conn.close()
        except Exception:
            logger.debug("Database statistics connection close failed", exc_info=True)

    return stats


def count_db_holders(db_path: Path) -> int | None:
    """Best-effort count of processes holding ``db_path`` open (Linux only).

    Scans ``/proc/*/fd`` symlinks for the resolved database path. Returns
    the number of distinct PIDs with the file open, or ``None`` on any
    error or on non-Linux platforms. Never raises; no lsof dependency.
    Unreadable per-process fd dirs (other users' processes without root)
    are silently skipped, so the count is a lower bound.
    """
    try:
        if not sys.platform.startswith("linux"):
            return None
        target = os.path.realpath(str(db_path))
        holders = 0
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            fd_dir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue  # process gone or not ours
            for fd in fds:
                try:
                    if os.readlink(f"{fd_dir}/{fd}") == target:
                        holders += 1
                        break  # one hit per PID
                except OSError:
                    continue
        return holders
    except Exception:
        logger.debug("Database holder count unavailable", exc_info=True)
        return None


# Lifecycle statuses surfaced by session pickers. Classification looks ONLY at
# a session's final message row — role, whether it carries tool_calls, and its
# finish_reason — so it stays O(1) per session (see
# SessionDB.session_lifecycle_statuses).
SESSION_STATUS_COMPLETE = "complete"
SESSION_STATUS_INTERRUPTED = "interrupted"
SESSION_STATUS_ERROR = "error"
SESSION_STATUS_EMPTY = "empty"

# finish_reason values that mark the turn as having ended in a provider or
# agent error (vs. a normal 'stop'/'length'/'tool_calls' completion).
_ERROR_FINISH_REASONS = frozenset({"error", "agent_error", "content_filter"})


def classify_session_status(
    role: str | None,
    has_tool_calls: bool,
    finish_reason: str | None,
) -> str:
    """Classify a session's lifecycle from the shape of its final message.

    - assistant with a normal finish → ``complete``
    - assistant that still has pending tool_calls (no tool result row ever
      followed, or it would be the last row instead) → ``interrupted``
    - user or tool as the last row → ``interrupted`` (the agent never got to
      answer / never consumed the tool result)
    - an error finish_reason on the last row → ``error``
    - anything unrecognized → ``complete`` (benign default; pickers must not
      alarm on unknown shapes)
    """
    if (finish_reason or "").strip().lower() in _ERROR_FINISH_REASONS:
        return SESSION_STATUS_ERROR
    r = (role or "").strip().lower()
    if r == "assistant":
        # The last row being an assistant message WITH tool_calls means the
        # matching tool result never landed — an interrupted tool turn.
        return SESSION_STATUS_INTERRUPTED if has_tool_calls else SESSION_STATUS_COMPLETE
    if r in {"user", "tool"}:
        return SESSION_STATUS_INTERRUPTED
    return SESSION_STATUS_COMPLETE


_session_db_connection.configure_connection_hooks(
    _session_db_connection.SessionConnectionHooks(
        default_db_path=lambda: _default_db_path(),
        ensure_test_isolation=lambda db_path: _ensure_test_isolation(db_path),
        connect_tracked_db=lambda path, tracking_path=None, **kwargs: (
            _connect_tracked_db(path, tracking_path=tracking_path, **kwargs)
        ),
        apply_database_pragmas=lambda conn, **kwargs: apply_database_pragmas(
            conn, **kwargs
        ),
        apply_wal_with_fallback=lambda conn, **kwargs: apply_wal_with_fallback(
            conn, **kwargs
        ),
        preflight_db_writability=lambda db_path, **kwargs: preflight_db_writability(
            db_path, **kwargs
        ),
        is_zeroed_state_db=lambda db_path: is_zeroed_state_db(db_path),
        quarantine_zeroed_state_db=lambda db_path: quarantine_zeroed_state_db(db_path),
        set_last_init_error=lambda message: _set_last_init_error(message),
        is_malformed_db_error=lambda exc: is_malformed_db_error(exc),
        claim_repair_attempt=lambda db_path: _claim_repair_attempt(db_path),
        repair_state_db_schema=lambda db_path: repair_state_db_schema(db_path),
        load_fts5_cjk_extension=lambda conn: load_fts5_cjk_extension(conn),
        is_not_a_database_error=lambda exc: _is_not_a_database_error(exc),
        compression_in_progress_error=lambda: SessionCompressionInProgressError,
        monotonic=lambda: time.monotonic(),
        sleep=lambda seconds: time.sleep(seconds),
        random_uniform=lambda low, high: random.SystemRandom().uniform(low, high),
        read_pool_max=lambda: _READ_POOL_MAX,
        read_open_retry_seconds=lambda: _READ_OPEN_RETRY_SECONDS,
        unregister_atexit=lambda hook: atexit.unregister(hook),
        log_debug=lambda message, *args, **kwargs: logger.debug(
            message, *args, **kwargs
        ),
        log_warning=lambda message, *args, **kwargs: logger.warning(
            message, *args, **kwargs
        ),
        log_error=lambda message, *args, **kwargs: logger.error(
            message, *args, **kwargs
        ),
        log_exception=lambda message, *args, **kwargs: logger.exception(
            message, *args, **kwargs
        ),
    )
)

_session_db_fts_integrity.configure_fts_integrity_hooks(
    _session_db_fts_integrity.SessionFTSIntegrityHooks(
        fts_triggers=lambda: _FTS_TRIGGERS,
        fts_cjk_triggers=lambda: _FTS_CJK_TRIGGERS,
        fts_stale_key=lambda: FTS_STALE_KEY,
        fts_cjk_stale_key=lambda: FTS_CJK_STALE_KEY,
        fts_cjk_table_sql=lambda: FTS_CJK_TABLE_SQL,
        fts_cjk_trigger_sql=lambda: FTS_CJK_TRIGGER_SQL,
        cjk_so_path=lambda: fts5_cjk_so_path(),
        is_malformed_db_error=lambda exc: is_malformed_db_error(exc),
        log_info=lambda message, *args, **kwargs: logger.info(message, *args, **kwargs),
        log_warning=lambda message, *args, **kwargs: logger.warning(
            message, *args, **kwargs
        ),
        log_error=lambda message, *args, **kwargs: logger.error(
            message, *args, **kwargs
        ),
        log_exception=lambda message, *args, **kwargs: logger.exception(
            message, *args, **kwargs
        ),
    )
)


class SessionDB(
    SessionConnectionMixin,
    SessionFTSIntegrityMixin,
    SessionHandoffMixin,
    SessionMaintenanceMixin,
    SessionTelegramTopicsMixin,
    SessionMetaStoreMixin,
    SessionPruningMixin,
    SessionDeletionMixin,
    SessionSearchMetricsMixin,
    SessionListingMixin,
    SessionPresentationStateMixin,
    SessionRewindMixin,
    SessionConversationMixin,
    SessionTranscriptQueryMixin,
    SessionTranscriptWriteMixin,
    SessionTokenAccountingMixin,
    SessionMetadataMixin,
    SessionSearchMixin,
    SessionSchemaMixin,
    SessionPortabilityMixin,
):
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    # Compatibility hooks for state_meta helpers moved to a mixin. Resolve
    # legacy module globals at call time so existing patch paths remain valid.
    @staticmethod
    def _meta_store_row_value(row) -> str:
        return row["value"] if isinstance(row, sqlite3.Row) else row[0]

    @staticmethod
    def _meta_store_escape_like(value: str) -> str:
        return _escape_like(value)

    # Compatibility hook for handoff reads moved to a mixin. Resolve the
    # legacy module logger at call time so existing monkeypatch paths remain
    # effective without a reverse import from the handoff module.
    @staticmethod
    def _handoff_log_debug(message: str, *args, **kwargs) -> None:
        logger.debug(message, *args, **kwargs)

    # Compatibility hooks for maintenance methods moved to a mixin. Resolve
    # legacy module globals at call time so existing time/logger monkeypatch
    # paths keep affecting behavior without a reverse import.
    @staticmethod
    def _maintenance_now() -> float:
        return time.time()

    @staticmethod
    def _maintenance_log_debug(message: str, *args, **kwargs) -> None:
        logger.debug(message, *args, **kwargs)

    @staticmethod
    def _maintenance_log_info(message: str, *args, **kwargs) -> None:
        logger.info(message, *args, **kwargs)

    @staticmethod
    def _maintenance_log_warning(message: str, *args, **kwargs) -> None:
        logger.warning(message, *args, **kwargs)

    # Compatibility hooks for Telegram topic persistence moved to a mixin.
    # Resolve shared helpers from this legacy module at call time so existing
    # monkeypatch paths keep affecting behavior without a reverse import.
    @staticmethod
    def _telegram_topics_now() -> float:
        return time.time()

    @staticmethod
    def _telegram_topics_preview_raw_select() -> str:
        return _PREVIEW_RAW_SELECT

    @staticmethod
    def _telegram_topics_session_last_active_sql(alias: str) -> str:
        return _sql_session_last_active(alias)

    @staticmethod
    def _telegram_topics_shape_preview(value: str) -> str:
        return _shape_preview(value)

    # Compatibility hooks for pruning methods moved to a mixin. Resolve the
    # legacy module globals at call time so time, SQL, logger, and helper
    # monkeypatches keep affecting behavior without a reverse import.
    @staticmethod
    def _pruning_escape_like(value: str) -> str:
        return _escape_like(value)

    @staticmethod
    def _pruning_cwd_prefix_clause(cwd_prefix: str) -> tuple[str, list[str]]:
        return _cwd_prefix_clause(cwd_prefix)

    @staticmethod
    def _pruning_now() -> float:
        return time.time()

    @staticmethod
    def _pruning_session_last_active_sql(alias: str) -> str:
        return _sql_session_last_active(alias)

    @staticmethod
    def _pruning_row_id(row) -> str:
        return row["id"] if isinstance(row, sqlite3.Row) else row[0]

    @staticmethod
    def _pruning_stale_tool_call_marker_matches(content: str) -> bool:
        return _STALE_TOOL_CALL_MARKER_RE.fullmatch(content) is not None

    @staticmethod
    def _pruning_log_info(message: str, *args) -> None:
        logger.info(message, *args)

    # Compatibility hooks for deletion methods moved to a mixin. Resolve the
    # legacy module globals at call time so established monkeypatch paths keep
    # affecting delegate cascade behavior without a reverse import.
    @staticmethod
    def _deletion_collect_delegate_child_ids(conn, parent_ids: list[str]) -> list[str]:
        return _collect_delegate_child_ids(conn, parent_ids)

    @staticmethod
    def _deletion_delete_delegate_children(conn, parent_ids: list[str]) -> list[str]:
        return _delete_delegate_children(conn, parent_ids)

    # Compatibility hooks for search/metric projections moved to a mixin.
    # Resolve legacy module globals at call time so established monkeypatch
    # paths keep affecting behavior without a reverse import from the mixin.
    @staticmethod
    def _search_metrics_last_active_sql(alias: str) -> str:
        return _sql_session_last_active(alias)

    @staticmethod
    def _search_metrics_workspace_key_clause(key: str) -> tuple[str, list[str]]:
        return _workspace_key_clause(key)

    @staticmethod
    def _search_metrics_listable_child_sql() -> str:
        return _LISTABLE_CHILD_SQL

    @staticmethod
    def _search_metrics_delegate_from_json(col: str = "model_config") -> str:
        return _delegate_from_json(col)

    @staticmethod
    def _search_metrics_cwd_prefix_clause(
        cwd_prefix: str,
    ) -> tuple[str, list[str]]:
        return _cwd_prefix_clause(cwd_prefix)

    # Compatibility hooks for listing methods moved to a mixin. Resolve the
    # legacy module globals at call time so established monkeypatch paths keep
    # affecting projections without a reverse import from the mixin.
    @staticmethod
    def _listing_listable_child_sql() -> str:
        return _LISTABLE_CHILD_SQL

    @staticmethod
    def _listing_delegate_from_json(col: str = "model_config") -> str:
        return _delegate_from_json(col)

    @staticmethod
    def _listing_cwd_prefix_clause(cwd_prefix: str) -> tuple[str, list[str]]:
        return _cwd_prefix_clause(cwd_prefix)

    @staticmethod
    def _listing_escape_like(value: str) -> str:
        return _escape_like(value)

    @staticmethod
    def _listing_session_last_active_by_id_sql(session_id_expr: str) -> str:
        return _sql_session_last_active_by_id(session_id_expr)

    @staticmethod
    def _listing_preview_raw_select() -> str:
        return _PREVIEW_RAW_SELECT

    @staticmethod
    def _listing_session_last_active_sql(alias: str) -> str:
        return _sql_session_last_active(alias)

    @staticmethod
    def _listing_shape_preview(value: str) -> str:
        return _shape_preview(value)

    def _listing_session_unread(self, session_row: dict[str, Any]) -> bool:
        return self.session_unread(session_row)

    @staticmethod
    def _listing_classify_session_status(
        role: str | None,
        has_tool_calls: bool,
        finish_reason: str | None,
    ) -> str:
        return classify_session_status(role, has_tool_calls, finish_reason)

    # Compatibility hooks for presentation-state methods moved to a mixin.
    # Resolve legacy module globals at call time so established monkeypatch
    # paths keep affecting behavior without a reverse import from the mixin.
    @staticmethod
    def _presentation_sanitize_title_text(value: str) -> str:
        return _sanitize_surrogates(value)

    @staticmethod
    def _presentation_compression_child_sql(alias: str) -> str:
        return _COMPRESSION_CHILD_SQL.format(a=alias)

    @staticmethod
    def _presentation_escape_like(value: str) -> str:
        return _escape_like(value)

    @staticmethod
    def _presentation_now() -> float:
        return time.time()

    @staticmethod
    def _presentation_session_last_active_sql(alias: str) -> str:
        return _sql_session_last_active(alias)

    # Compatibility hooks for conversation methods moved to a mixin. Resolve
    # the legacy module globals at call time so existing monkeypatch paths keep
    # affecting resume behavior without a reverse import from the mixin.
    @staticmethod
    def _conversation_legacy_reset_child_sql(alias: str) -> str:
        return _legacy_reset_child_sql(alias, _RESET_END_REASONS_SQL)

    @staticmethod
    def _conversation_sanitize_context(content: str) -> str:
        return sanitize_context(content)

    @staticmethod
    def _conversation_strip_background_review_harness(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return _strip_background_review_harness(messages)

    @staticmethod
    def _conversation_strip_stale_tool_call_markers(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return _strip_stale_tool_call_markers(messages)

    @staticmethod
    def _conversation_resolved_max_resume_messages() -> int:
        return resolved_max_resume_messages()

    @staticmethod
    def _conversation_resolved_max_export_messages() -> int:
        return resolved_max_export_messages()

    @staticmethod
    def _conversation_resume_too_large_error(
        message_count: int, limit: int
    ) -> Exception:
        return SessionResumeTooLargeError(message_count, limit)

    @staticmethod
    def _conversation_export_too_large_error(
        session_id: str, message_count: int, limit: int
    ) -> Exception:
        return SessionExportTooLargeError(session_id, message_count, limit)

    # ── Write-contention tuning ──
    # With multiple PCBDraft processes (gateway + CLI sessions + worktree agents)
    # all sharing one state.db, WAL write-lock contention causes visible TUI
    # freezes.  SQLite's built-in busy handler uses a deterministic sleep
    # schedule that causes convoy effects under high concurrency.
    #
    # Instead, we keep the SQLite timeout short (1s) and handle retries at the
    # application level with random jitter, which naturally staggers competing
    # writers and avoids the convoy.
    #
    # Patience is TIME-based, not attempt-based.  A shared state.db is
    # legitimately held for multi-second stretches by sibling PCBDraft
    # processes: a TRUNCATE checkpoint at close on a large WAL, VACUUM after
    # an auto-prune, offline recovery, or an older still-running process
    # whose FTS maintenance predates the bounded-merge protocol (every
    # Runtime replacement leaves mixed-version processes sharing the DB until
    # the old ones exit).  An attempt-counted budget (~15s incidental worst
    # case) silently loses that race and surfaces as
    # session_persistence_failed — a destroyed turn — even though the store
    # is healthy and merely busy (#74478).
    #
    # Two budgets: routine writes give up after _WRITE_PATIENCE_S so
    # background/UI callers don't stall excessively, while transcript
    # writes (append_message / session-row creation — the ones whose
    # failure aborts the user's turn) ride out anything shorter than
    # _TRANSCRIPT_WRITE_PATIENCE_S.  Jitter stays small for the first
    # _WRITE_RETRY_SLOW_AFTER_S (fast reclaim on millisecond contention),
    # then backs off so a long hold isn't hammered with BEGIN IMMEDIATE
    # attempts.
    _WRITE_PATIENCE_S = 20.0
    _TRANSCRIPT_WRITE_PATIENCE_S = 60.0
    # Observation-only activity heartbeat/label writes (#76354 review S1):
    # these run on (or adjacent to) the response-critical path and must never
    # wait out the full routine patience under contention. Sub-second budget;
    # a skipped write is retried naturally at the next heartbeat window.
    _ACTIVITY_WRITE_PATIENCE_S = 0.5
    # A live compression lock gets its own, much shorter budget than the write
    # lock. Compression publishes in a couple of seconds, so a brief wait saves
    # the overwhelming majority of concurrent turns (#75083). It deliberately
    # stays short: the lease is a correctness boundary, not just a busy signal
    # (see test_compression_lease_blocks_non_owner_but_allows_owner_flush), so
    # a writer that is still locked out after this budget must still be
    # refused rather than allowed to land a stale turn in a session whose
    # compression is genuinely long-running or wedged.
    _COMPRESSION_BUSY_WAIT_S = 5.0
    _WRITE_RETRY_MIN_S = 0.020  # 20ms
    _WRITE_RETRY_MAX_S = 0.150  # 150ms
    _WRITE_RETRY_SLOW_AFTER_S = 2.0
    _WRITE_RETRY_SLOW_MIN_S = 0.250  # 250ms
    _WRITE_RETRY_SLOW_MAX_S = 1.000  # 1s
    # Attempt a WAL checkpoint every N successful writes (PASSIVE mode).
    _CHECKPOINT_EVERY_N_WRITES = 50
    # Retain the existing coarse 1000-write maintenance cadence, but replace
    # the unbounded FTS5 ``'optimize'`` (measured holding the write lock for
    # 9-18 s per index on a 10 GB production DB — longer than a competing
    # writer's full retry patience, surfacing as "database is locked" /
    # session_persistence_failed) with bounded ``'merge'`` commands. A
    # positive merge rank is an approximate output-page budget, so each
    # command holds the write lock for milliseconds; up to
    # ``_FTS_MERGE_COMMANDS_PER_PASS`` commands run per index per cadence,
    # stopping early on the documented no-progress signal. ``usermerge`` is
    # lowered to 2 so positive merges act on any level with >= 2 segments —
    # without that, levels below the default threshold of 4 are skipped and
    # a fragmented index never converges (SQLite FTS5 §6.8-6.9).
    _FTS_MERGE_EVERY_N_WRITES = 1000
    _FTS_MERGE_MAX_PAGES_PER_INDEX = 500
    _FTS_MERGE_COMMANDS_PER_PASS = 4
    # Session imports intentionally use a lower cap than exports: import holds
    # one BEGIN IMMEDIATE transaction, so bounded batches avoid starving live
    # gateway/CLI writers. The dashboard accepts one exported JSON/JSONL file
    # at a time, so these still cover normal history restores.
    _IMPORT_MAX_SESSIONS = 500
    _IMPORT_MAX_MESSAGES_PER_SESSION = 10_000
    _IMPORT_MAX_TOTAL_MESSAGES = 50_000
    _IMPORT_MAX_SESSION_BYTES = 5 * 1024 * 1024
    _IMPORT_MAX_TOTAL_BYTES = 25 * 1024 * 1024

    @staticmethod
    def _store_system_prompt(conn, system_prompt: str | None) -> str | None:
        if system_prompt is None:
            return None
        prompt_hash = _system_prompt_hash(system_prompt)
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

    @staticmethod
    def _session_row_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        if "_system_prompt_resolved" in data:
            resolved = data.pop("_system_prompt_resolved")
            if "system_prompt" in data:
                data["system_prompt"] = resolved
        return data

    # ── Chunked FTS rebuild engine (v23 opt-in optimize) ──
    #
    # `optimize_fts_storage()` (the offline storage optimization
    # command) drops the legacy inline FTS indexes and backfills the new
    # external-content ones. A single blocking rebuild measured ~16 minutes
    # of held write lock on a real 25 GB DB, so the backfill runs in small
    # chunks, each in its own short write transaction:
    #   - concurrent readers/writers are never starved (WAL stays small,
    #     each chunk checkpoints via the normal _execute_write cadence);
    #   - an interrupted run (Ctrl-C, crash) resumes from
    #     fts_rebuild_progress when the command is re-run;
    #   - multiple processes sharing the DB don't double-run it — each chunk
    #     claims work by compare-and-swap on fts_rebuild_progress, so even a
    #     concurrent second runner just interleaves chunks safely.
    #
    # THROTTLING (the part that keeps a live gateway sharing the DB
    # responsive): a greedy chunk loop re-acquires BEGIN IMMEDIATE nearly
    # back-to-back and can starve another process's writer into exhausting
    # its lock retries (an early 5000-row/50ms version owned the write lock
    # ~85% of the time and visibly froze concurrent CLI sessions on a large
    # install). Two layers prevent that:
    #   1. Small chunks (500 rows) — a foreground write queues behind a
    #      chunk for at most ~tens of ms.
    #   2. Inter-chunk pause — the loop sleeps max(_FTS_REBUILD_MIN_PAUSE,
    #      chunk cost x _FTS_REBUILD_DUTY_FACTOR) between chunks, capping
    #      this process's share of DB bandwidth so concurrent writers always
    #      find open windows. This works cross-process (unlike any
    #      same-process activity stamp) because it bounds our own duty
    #      cycle unconditionally.

    _FTS_REBUILD_CHUNK_ROWS = 500
    _FTS_REBUILD_DUTY_FACTOR = 4.0  # sleep >= 4x chunk cost (≤20% duty)
    _FTS_REBUILD_MIN_PAUSE = 0.2  # seconds — floor between chunks

    # Demoted v22 FTS shadow tables awaiting teardown (see the v23 migration:
    # DROP of a multi-GB FTS vtable blocks for minutes, so the migration
    # demotes the vtable definitions out of sqlite_master and renames the
    # orphaned shadow tables — now plain tables — to fts_v22_trash_*; the
    # worker empties them in bounded chunks, then drops them cheaply).
    _FTS_TRASH_PREFIX = "fts_v22_trash_"

    # ── CJK-bigram index backfill (dedicated marker pair) ──
    #
    # Same chunk engine as the main deferred rebuild, but on the
    # ``fts_cjk_rebuild_*`` markers so a cjk-only backfill (the common case:
    # an already-optimized v23 DB gaining the cjk index) never gates the
    # complete ``messages_fts`` / trigram triggers.

    # ── Opt-in v23 FTS storage optimization ──
    #
    # This is the ONLY path that migrates an existing legacy (v22 inline) DB
    # to the v23 external-content schema. It is deliberately foreground and
    # user-invoked, never automatic, because it is disk-heavy and long. It
    # runs the throttled/resumable chunk engine above to completion
    # synchronously — demote → new schema → chunked backfill → chunked
    # teardown — with progress callbacks, a disk preflight in the CLI
    # wrapper, a VACUUM at the end, and a defensive schema_version bump.

    # =========================================================================
    # Session lifecycle
    # =========================================================================

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
                    json.dumps(model_config) if model_config else None,
                    system_prompt_hash,
                    parent_session_id,
                    cwd,
                    profile_name,
                    git_repo_root,
                    origin_json,
                    display_name,
                    time.time(),
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

    def record_gateway_session_peer(
        self,
        session_id: str,
        *,
        source: str,
        user_id: str | None = None,
        session_key: str | None = None,
        chat_id: str | None = None,
        chat_type: str | None = None,
        thread_id: str | None = None,
        display_name: str | None = None,
        origin_json: str | None = None,
        include_compression_ancestors: bool = False,
    ) -> None:
        """Persist the gateway routing peer for an existing session row.

        ``display_name`` / ``origin_json`` carry the gateway's presentation
        and full origin metadata (#9006) so consumers (mcp_serve, mirror,
        channel directory) can read routing data from state.db instead of
        sessions.json.  They are COALESCE'd only in the sense that ``None``
        leaves the existing value untouched.

        ``include_compression_ancestors`` keeps a logical compression lineage
        on one routing peer when an explicit gateway resume moves its tip to a
        different lane. Normal per-turn metadata refreshes update only the
        supplied row.

        Self-healing (#82616): when the target row does not exist yet — the
        gateway's ``create_session`` write failed and was deferred, or a
        crash landed between routing publication and row creation — this
        recorder INSERTs the row with the full identity instead of silently
        no-opping. Every per-turn peer refresh is therefore a repair
        opportunity: a gateway session row can no longer be first-created by
        an identity-less lazy writer (``update_token_counts`` /
        ``record_auxiliary_usage``) and stay unroutable forever.
        """
        if not session_id or not session_key:
            return

        def _do(conn):
            lineage_cte = ""
            target_clause = "WHERE id = ?"
            query_params = []
            if include_compression_ancestors:
                lineage_cte = """
                    WITH RECURSIVE compression_lineage(id) AS (
                        SELECT ?
                        UNION
                        SELECT parent.id
                        FROM compression_lineage lineage
                        JOIN sessions child ON child.id = lineage.id
                        JOIN sessions parent ON parent.id = child.parent_session_id
                        WHERE parent.end_reason = 'compression'
                          AND json_extract(
                              COALESCE(child.model_config, '{}'),
                              '$._branched_from'
                          ) IS NULL
                          AND json_extract(
                              COALESCE(child.model_config, '{}'),
                              '$._delegate_from'
                          ) IS NULL
                          AND COALESCE(child.source, '') != 'tool'
                    )
                """
                target_clause = "WHERE id IN (SELECT id FROM compression_lineage)"
                query_params.append(session_id)
            query_params.extend(
                (
                    session_key,
                    source,
                    user_id,
                    chat_id,
                    chat_type,
                    thread_id,
                    display_name,
                    origin_json,
                )
            )
            if not include_compression_ancestors:
                query_params.append(session_id)
            conn.execute(
                f"""{lineage_cte}
                   UPDATE sessions
                   SET session_key = ?, source = ?, user_id = ?, chat_id = ?,
                       chat_type = ?, thread_id = ?,
                       display_name = COALESCE(?, display_name),
                       origin_json = COALESCE(?, origin_json)
                   {target_clause}""",
                query_params,
            )
            # Self-heal (#82616): the UPDATE is a silent no-op when the row
            # is missing (create_session failed earlier, or a crash landed
            # between routing publication and row creation). Insert it with
            # the full identity so the session is durably routable — never
            # leave first-creation to an identity-less lazy writer.
            if not include_compression_ancestors:
                cur = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
                )
                if cur.fetchone() is None:
                    conn.execute(
                        """INSERT INTO sessions (
                               id, source, user_id, session_key, chat_id,
                               chat_type, thread_id, display_name, origin_json,
                               started_at
                           )
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET
                               session_key = COALESCE(sessions.session_key, excluded.session_key),
                               chat_id = COALESCE(sessions.chat_id, excluded.chat_id),
                               chat_type = COALESCE(sessions.chat_type, excluded.chat_type),
                               thread_id = COALESCE(sessions.thread_id, excluded.thread_id),
                               display_name = COALESCE(sessions.display_name, excluded.display_name),
                               origin_json = COALESCE(sessions.origin_json, excluded.origin_json)""",
                        (
                            session_id,
                            source,
                            user_id,
                            session_key,
                            chat_id,
                            chat_type,
                            thread_id,
                            display_name,
                            origin_json,
                            time.time(),
                        ),
                    )

        self._execute_write(_do)

    def set_expiry_finalized(self, session_id: str, finalized: bool = True) -> None:
        """Mark a gateway session's expiry-finalization flag in state.db.

        Mirrors ``SessionEntry.expiry_finalized`` (sessions.json) so the flag
        survives even if the JSON index is pruned or lost (#9006).
        """
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET expiry_finalized = ? WHERE id = ?",
                (1 if finalized else 0, session_id),
            )

        self._execute_write(_do)

    # ── Gateway routing index (replaces sessions.json, #9006 follow-up) ────

    def save_gateway_routing_entry(
        self, session_key: str, entry_json: str, *, scope: str = ""
    ) -> None:
        """Upsert one gateway routing entry (session_key -> SessionEntry JSON).

        The gateway_routing table is the durable replacement for
        sessions.json: one row per routing key, holding the full serialized
        ``SessionEntry`` so the gateway can rehydrate exactly what it wrote.

        ``scope`` namespaces the index the way separate sessions.json files
        did (one per sessions_dir) — callers pass their sessions_dir path so
        two stores with different directories never share routing state.
        """
        if not session_key or not entry_json:
            return

        def _do(conn):
            conn.execute(
                """INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope, session_key) DO UPDATE SET
                       entry_json = excluded.entry_json,
                       updated_at = excluded.updated_at""",
                (scope, session_key, entry_json, time.time()),
            )

        self._execute_write(_do)

    def replace_gateway_routing_entries(
        self, entries: dict[str, str], *, scope: str = ""
    ) -> None:
        """Atomically replace the routing index for *scope* with *entries*.

        Mirrors the sessions.json full-rewrite semantics: keys absent from
        *entries* are removed (pruned/reset sessions disappear from the
        index).  Runs as a single write transaction.  Other scopes are
        untouched.
        """
        now = time.time()

        def _do(conn):
            conn.execute("DELETE FROM gateway_routing WHERE scope = ?", (scope,))
            if entries:
                conn.executemany(
                    "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    [(scope, k, v, now) for k, v in entries.items() if k and v],
                )

        self._execute_write(_do)

    def load_gateway_routing_entries(self, *, scope: str = "") -> dict[str, str]:
        """Load routing entries for *scope* as {session_key: entry_json}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_key, entry_json FROM gateway_routing WHERE scope = ?",
                (scope,),
            ).fetchall()
        return {r["session_key"]: r["entry_json"] for r in rows}

    def delete_gateway_routing_entries(
        self, session_keys: list[str], *, scope: str = ""
    ) -> None:
        """Remove routing entries for the given session keys in *scope*."""
        if not session_keys:
            return

        def _do(conn):
            conn.executemany(
                "DELETE FROM gateway_routing WHERE scope = ? AND session_key = ?",
                [(scope, k) for k in session_keys],
            )

        self._execute_write(_do)

    def list_never_active_keyed_sessions(
        self, *, older_than_days: float
    ) -> list[dict[str, Any]]:
        """Keyed gateway rows that were opened and then never used at all.

        Selects rows that are keyed (``session_key IS NOT NULL``), still open
        (``ended_at IS NULL``) and carry no evidence of a single turn: no
        messages, no tokens, no tool or API calls, no recorded activity, no
        title.  Such a row is indistinguishable from "never happened".

        That is exactly the shape of a leaked test fixture (#82770) — and
        also of a chat that was routed but never answered.  Both are safe to
        drop: there is no transcript to lose, and the gateway mints a fresh
        session on the next inbound message either way.

        ``bulk prune``/``archive`` cannot reach these rows: their shared
        selector is pinned to ``ended_at IS NOT NULL`` so that a live session
        is never picked, which permanently excludes every never-closed row.
        Hence a separate, narrower selector rather than another filter flag.

        ``pinned`` and ``archived`` rows are excluded — both are explicit
        user intent to keep the row around.
        """
        cutoff = time.time() - (float(older_than_days) * 86400.0)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.id, s.session_key, s.source, s.chat_id,
                       s.chat_type, s.user_id, s.started_at
                  FROM sessions s
                 WHERE s.session_key IS NOT NULL
                   AND s.ended_at IS NULL
                   AND s.title IS NULL
                   AND s.last_activity_at IS NULL
                   AND COALESCE(s.message_count, 0) = 0
                   AND COALESCE(s.tool_call_count, 0) = 0
                   AND COALESCE(s.api_call_count, 0) = 0
                   AND COALESCE(s.input_tokens, 0) = 0
                   AND COALESCE(s.output_tokens, 0) = 0
                   AND COALESCE(s.pinned, 0) = 0
                   AND COALESCE(s.archived, 0) = 0
                   AND s.started_at IS NOT NULL
                   AND s.started_at < ?
                   AND NOT EXISTS (
                           SELECT 1 FROM messages m WHERE m.session_id = s.id
                       )
                 ORDER BY s.started_at
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _delete_routing_entries_for_sessions(self, session_ids: set[str]) -> int:
        """Drop ``gateway_routing`` rows pointing at any of *session_ids*.

        Routing entries are keyed by ``(scope, session_key)`` and record their
        target session inside ``entry_json``, so there is no way to reach them
        by session id in SQL — the match is done in Python over all scopes.
        """
        if not session_ids:
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT scope, session_key, entry_json FROM gateway_routing"
            ).fetchall()
        doomed: list[tuple[str, str]] = []
        for row in rows:
            try:
                entry = json.loads(row["entry_json"] or "{}")
            except Exception:
                logger.debug(
                    "Gateway routing entry decode failed",
                    exc_info=_exception_info_without_values(),
                )
                continue
            if isinstance(entry, dict) and entry.get("session_id") in session_ids:
                doomed.append((row["scope"], row["session_key"]))
        if not doomed:
            return 0

        def _do(conn):
            conn.executemany(
                "DELETE FROM gateway_routing WHERE scope = ? AND session_key = ?",
                doomed,
            )

        self._execute_write(_do)
        return len(doomed)

    def prune_never_active_keyed_sessions(
        self,
        *,
        older_than_days: float,
        sessions_dir: Path | None = None,
    ) -> tuple[int, int]:
        """Delete never-active keyed rows and the routing entries naming them.

        Returns ``(sessions_deleted, routing_entries_deleted)``.

        The routing entries go first: a stale entry that outlived its target
        would leave the gateway resuming a session id that no longer exists.
        Deleting the pair is what leaving them both would have amounted to
        anyway — the target had no transcript to resume.

        Deletion goes through :meth:`delete_session` rather than a bulk
        ``DELETE`` so the delegate cascade, FTS bookkeeping and on-disk
        transcript cleanup stay owned by one implementation.
        """
        candidates = self.list_never_active_keyed_sessions(
            older_than_days=older_than_days
        )
        if not candidates:
            return (0, 0)
        ids = {str(row["id"]) for row in candidates}
        routing_deleted = self._delete_routing_entries_for_sessions(ids)
        deleted = 0
        for session_id in ids:
            if self.delete_session(session_id, sessions_dir=sessions_dir):
                deleted += 1
        return (deleted, routing_deleted)

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
                   {_sql_session_last_active("sessions")} AS last_active
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
                        AND b.end_reason IN ({_RESET_END_REASONS_SQL})
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
                        AND b.end_reason IN ({_RESET_END_REASONS_SQL})
                        AND b.ended_at
                            > COALESCE(s.last_activity_at, s.started_at)
                  )
                ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC
                LIMIT 1
                """,
                (source, user_id, chat_id, chat_type, thread_id),
            ).fetchone()
        return self._session_row_dict(row) if row else None

    # ── Orphaned gateway-session repair (#82616) ──────────────────────────
    # A write-path failure (corrupt FTS, crash between routing publication
    # and row creation) can leave the live conversation in a session row
    # that never received its identity columns. Both queries above require
    # those columns, so the row holding the real transcript is invisible to
    # recovery: the chat resolves to the last keyed row instead — days older
    # — and the conversation time-travels. Hardening the write side cannot
    # reach a row that is *already* damaged; these two methods are the
    # offline routing repair path.

    # Widest plausible gap between a keyed predecessor going quiet and its
    # unkeyed successor being minted. The reported incident gap was ~60s;
    # 15 minutes stays generous without spanning unrelated conversations.
    _ORPHAN_ADOPTION_MAX_GAP_S = 900.0

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
        orphan_active = _sql_session_last_active("o")
        donor_active = _sql_session_last_active("d")
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

    def adopt_orphaned_gateway_session(self, orphan_id: str, donor_id: str) -> bool:
        """Stamp *orphan_id* with *donor_id*'s routing identity, retire *donor_id*.

        Re-verifies the pair inside the write transaction, so a concurrent
        gateway that healed either row in the meantime turns this into a
        no-op instead of a conflicting write. Existing non-NULL columns on
        the orphan are preserved. Returns True when the adoption applied.
        """
        if not orphan_id or not donor_id or orphan_id == donor_id:
            return False

        def _do(conn):
            donor = conn.execute(
                "SELECT session_key, chat_id, chat_type, thread_id, user_id, "
                "origin_json, display_name, source FROM sessions WHERE id = ?",
                (donor_id,),
            ).fetchone()
            orphan = conn.execute(
                "SELECT session_key, source FROM sessions WHERE id = ?",
                (orphan_id,),
            ).fetchone()
            if donor is None or orphan is None:
                return False
            if not donor["session_key"] or orphan["session_key"]:
                return False
            if (donor["source"] or "") != (orphan["source"] or ""):
                return False

            conn.execute(
                """UPDATE sessions
                      SET session_key = ?,
                          chat_id = COALESCE(chat_id, ?),
                          chat_type = COALESCE(chat_type, ?),
                          thread_id = COALESCE(thread_id, ?),
                          user_id = COALESCE(user_id, ?),
                          origin_json = COALESCE(origin_json, ?),
                          display_name = COALESCE(display_name, ?),
                          parent_session_id = COALESCE(parent_session_id, ?)
                    WHERE id = ? AND session_key IS NULL""",
                (
                    donor["session_key"],
                    donor["chat_id"],
                    donor["chat_type"],
                    donor["thread_id"],
                    donor["user_id"],
                    donor["origin_json"],
                    donor["display_name"],
                    donor_id,
                    orphan_id,
                ),
            )
            # Retire the predecessor under a reason recovery does NOT treat
            # as resumable — 'agent_close'/'ws_orphan_reap' would keep it in
            # the running, and the newly keyed orphan could lose the chat
            # again on the next restart.
            conn.execute(
                "UPDATE sessions SET ended_at = COALESCE(ended_at, ?), "
                "end_reason = 'superseded_by_repair' WHERE id = ?",
                (time.time(), donor_id),
            )
            return True

        return self._execute_write(_do)

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

            # refresh_compression_lock() deliberately lets an owner revive its
            # own expired row. Reclaim that row inside this write transaction
            # before reopening: refresh-first makes the lease active and aborts
            # recovery; recovery-first deletes the holder identity so a later
            # refresh cannot resurrect it.
            now = time.time()
            lock_row = conn.execute(
                "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if lock_row is not None:
                expires_at = lock_row["expires_at"]
                if expires_at is None or float(expires_at) >= now:
                    return False
                deleted = conn.execute(
                    "DELETE FROM compression_locks "
                    "WHERE session_id = ? AND holder = ? AND expires_at = ?",
                    (session_id, lock_row["holder"], expires_at),
                )
                if deleted.rowcount != 1:
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
            lock_row = conn.execute(
                "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?",
                (parent_session_id,),
            ).fetchone()
            if require_compression_lease and (
                lock_row is None
                or not compression_lock_holder
                or lock_row["holder"] != compression_lock_holder
                or float(lock_row["expires_at"]) <= time.time()
            ):
                raise CompressionSessionBusyError(
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
                    json.dumps(model_config) if model_config else None,
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
                    time.time(),
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
                                    json.loads(raw) if isinstance(raw, str) else raw
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
                (time.time(), parent_session_id),
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
                (time.time(), end_reason, session_id),
            )

        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed.

        Before clearing a reset boundary, stabilize markerless legacy reset
        children that still depend on the parent's mutable end_reason.
        """

        def _do(conn):
            placeholders = ",".join("?" for _ in _RESET_END_REASONS)
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
                f"AND {_legacy_reset_child_sql('child', placeholders)}",
                (session_id, *_RESET_END_REASONS),
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
        now = time.time()

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
            logger.debug("Session reset-boundary promotion failed", exc_info=True)
            return False

    def update_session_cwd(
        self,
        session_id: str,
        cwd: str,
        git_branch: str | None = None,
        git_repo_root: str | None = None,
        replace_git_meta: bool = False,
    ) -> int | None:
        """Persist the authoritative cwd and claim a Git metadata generation.

        ``git_branch`` records the git branch checked out in ``cwd`` at the time
        the session started/resumed. The sidebar groups main-checkout sessions
        by this so feature-branch work doesn't pile under a single "main" row
        (the main checkout's *current* branch is transient and would
        misattribute past sessions).

        ``git_repo_root`` records the git repo this cwd belongs to — the
        authoritative project key. Resolving it here, at the lowest level, means
        every surface reads the same membership instead of re-probing git in the
        GUI over a partial page. Each field is only written when non-empty so a
        probe failure never clobbers a previously-captured value.

        ``replace_git_meta`` inverts that non-empty rule: a deliberate workspace
        MOVE (re-homing a session into another project) must overwrite the old
        repo identity even when the new cwd resolves to none — keeping the stale
        root would leave the session grouped under the project it just left.

        Every call increments ``git_metadata_generation`` in the same write
        transaction. Async Git probes must publish through
        :meth:`publish_session_git_metadata` with the returned generation, so
        an older worker cannot overwrite a newer cwd claim even after an
        A -> B -> A transition or from another process sharing this database.
        Metadata from a different cwd is cleared atomically with the move.
        """
        if not session_id or not cwd:
            return None

        branch = (git_branch or "").strip()
        repo_root = (git_repo_root or "").strip()

        def _do(conn):
            current = conn.execute(
                "SELECT cwd FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if current is None:
                return None

            current_cwd = (
                current["cwd"] if isinstance(current, sqlite3.Row) else current[0]
            )
            sets = [
                "cwd = ?",
                "git_metadata_generation = COALESCE(git_metadata_generation, 0) + 1",
            ]
            params: list[Any] = [cwd]
            if current_cwd != cwd or replace_git_meta:
                sets.extend(("git_branch = ?", "git_repo_root = ?"))
                params.extend((branch or None, repo_root or None))
            elif branch:
                sets.append("git_branch = ?")
                params.append(branch)
            if repo_root and current_cwd == cwd and not replace_git_meta:
                sets.append("git_repo_root = ?")
                params.append(repo_root)
            params.append(session_id)
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)
            row = conn.execute(
                "SELECT git_metadata_generation FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            value = (
                row["git_metadata_generation"]
                if isinstance(row, sqlite3.Row)
                else row[0]
            )
            return int(value)

        return self._execute_write(_do)

    def publish_session_git_metadata(
        self,
        session_id: str,
        cwd: str,
        generation: int,
        git_branch: str | None = None,
        git_repo_root: str | None = None,
    ) -> bool:
        """Publish async Git enrichment only while its cwd claim is current."""
        if (
            not session_id
            or not cwd
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            return False

        branch = (git_branch or "").strip()
        repo_root = (git_repo_root or "").strip()
        if not branch and not repo_root:
            return False

        sets: list[str] = []
        params: list[Any] = []
        if branch:
            sets.append("git_branch = ?")
            params.append(branch)
        if repo_root:
            sets.append("git_repo_root = ?")
            params.append(repo_root)
        params.extend((session_id, cwd, generation))

        def _do(conn):
            cursor = conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} "
                "WHERE id = ? AND cwd = ? "
                "AND git_metadata_generation = ?",
                params,
            )
            return cursor.rowcount == 1

        return bool(self._execute_write(_do))

    def backfill_repo_roots(self, cwd_to_root: dict[str, str]) -> None:
        """Persist resolved git repo roots for cwds that don't have one yet.

        Backfills history so projects light up for sessions created before the
        column existed, without clobbering an already-recorded root. Only
        non-empty roots are written (a non-git cwd stays NULL).
        """
        pairs = [(root, cwd) for cwd, root in cwd_to_root.items() if root and cwd]
        if not pairs:
            return

        def _do(conn):
            for root, cwd in pairs:
                conn.execute(
                    "UPDATE sessions SET git_repo_root = ? "
                    "WHERE cwd = ? AND COALESCE(git_repo_root, '') = ''",
                    (root, cwd),
                )

        self._execute_write(_do)

    def record_compression_failure_cooldown(
        self,
        session_id: str,
        cooldown_until: float,
        error: str | None = None,
    ) -> None:
        """Persist the active compression-failure cooldown for a session."""
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_failure_cooldown_until = ?, "
                "compression_failure_error = ? WHERE id = ?",
                (cooldown_until, error, session_id),
            )

        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning(
                "record_compression_failure_cooldown(%s) failed: %s",
                session_id,
                exc,
            )

    def get_compression_failure_cooldown(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Return the active compression-failure cooldown for ``session_id``."""
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT compression_failure_cooldown_until, compression_failure_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        cooldown_until = (
            row["compression_failure_cooldown_until"]
            if isinstance(row, sqlite3.Row)
            else row[0]
        )
        if cooldown_until is None:
            return None
        cooldown_until = float(cooldown_until)
        if cooldown_until <= now:
            return None
        error = (
            row["compression_failure_error"] if isinstance(row, sqlite3.Row) else row[1]
        )
        return {
            "cooldown_until": cooldown_until,
            "remaining_seconds": cooldown_until - now,
            "error": error,
        }

    def get_compression_failure_cooldown_row(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Return the exact stored cooldown columns without expiry filtering.

        Compression cancellation uses this under its session lease so rollback
        can preserve an expired row, a partially-null row, or an absent session
        exactly instead of converting those states through the active-cooldown
        API.
        """
        if not session_id:
            return {"session_exists": False, "cooldown_until": None, "error": None}
        with self._lock:
            row = self._conn.execute(
                "SELECT compression_failure_cooldown_until, compression_failure_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {"session_exists": False, "cooldown_until": None, "error": None}
        cooldown_until = (
            row["compression_failure_cooldown_until"]
            if isinstance(row, sqlite3.Row)
            else row[0]
        )
        error = (
            row["compression_failure_error"] if isinstance(row, sqlite3.Row) else row[1]
        )
        return {
            "session_exists": True,
            "cooldown_until": (
                float(cooldown_until) if cooldown_until is not None else None
            ),
            "error": error,
        }

    def restore_compression_failure_cooldown_row(
        self,
        session_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        """Restore and verify an exact cooldown-row snapshot.

        Unlike the ordinary record/clear helpers, this transactional rollback
        API deliberately propagates write and verification failures. A caller
        must not report cancellation as mutation-free when compensation failed.
        """
        expected_exists = bool(snapshot.get("session_exists", False))
        if not expected_exists:
            actual = self.get_compression_failure_cooldown_row(session_id)
            if actual.get("session_exists", False):
                raise RuntimeError(
                    "cannot restore absent compression cooldown row: session now exists"
                )
            return

        deadline = snapshot.get("cooldown_until")
        error = snapshot.get("error")

        def _do(conn):
            cursor = conn.execute(
                "UPDATE sessions SET compression_failure_cooldown_until = ?, "
                "compression_failure_error = ? WHERE id = ?",
                (deadline, error, session_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"compression cooldown rollback session missing: {session_id}"
                )

        self._execute_write(_do)
        actual = self.get_compression_failure_cooldown_row(session_id)
        expected = {
            "session_exists": True,
            "cooldown_until": float(deadline) if deadline is not None else None,
            "error": error,
        }
        if actual != expected:
            raise RuntimeError(
                f"compression cooldown rollback verification failed: "
                f"expected={expected!r}, actual={actual!r}"
            )

    def clear_compression_failure_cooldown(self, session_id: str) -> None:
        """Clear any persisted compression-failure cooldown for a session."""
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_failure_cooldown_until = NULL, "
                "compression_failure_error = NULL WHERE id = ?",
                (session_id,),
            )

        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning(
                "clear_compression_failure_cooldown(%s) failed: %s",
                session_id,
                exc,
            )

    def get_compression_fallback_streak(self, session_id: str) -> int:
        """Return the persisted deterministic-fallback streak."""
        if not session_id:
            return 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            row = conn.execute(
                "SELECT compression_fallback_streak FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return 0
        value = (
            row["compression_fallback_streak"]
            if isinstance(row, sqlite3.Row)
            else row[0]
        )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def set_compression_fallback_streak(self, session_id: str, streak: int) -> None:
        """Persist the deterministic-fallback streak for one session."""
        if not session_id:
            return
        normalized = max(0, int(streak))

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_fallback_streak = ? WHERE id = ?",
                (normalized, session_id),
            )

        self._execute_write(_do)

    def increment_hygiene_failure_streak(self, session_key: str) -> int:
        """Atomically increment the session-hygiene failure streak for one chat."""
        if not session_key:
            return 1
        result = []

        def _do(conn):
            conn.execute(
                """INSERT INTO gateway_hygiene_state (session_key, failure_streak)
                   VALUES (?, 1)
                   ON CONFLICT(session_key) DO UPDATE SET
                       failure_streak = gateway_hygiene_state.failure_streak + 1""",
                (session_key,),
            )
            row = conn.execute(
                "SELECT failure_streak FROM gateway_hygiene_state WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            result.append(int(row[0]))

        self._execute_write(_do)
        return result[0]

    def reset_hygiene_failure_streak(self, session_key: str) -> None:
        """Clear the persisted session-hygiene failure streak for one chat."""
        if not session_key:
            return

        def _do(conn):
            conn.execute(
                "DELETE FROM gateway_hygiene_state WHERE session_key = ?",
                (session_key,),
            )

        self._execute_write(_do)

    def get_compression_ineffective_count(self, session_id: str) -> int:
        """Return the persisted ineffective-compaction strike count.

        Mirrors ``get_compression_fallback_streak``: this is the durable half
        of the anti-thrash guard (``_ineffective_compression_count`` on the
        built-in compressor), persisted so that a fresh compressor bound to a
        resumed session inherits an armed/tripped guard instead of starting
        from zero across process restarts (#54923).
        """
        if not session_id:
            return 0
        with self._lock:
            conn = self._conn
            if conn is None:
                return 0
            row = conn.execute(
                "SELECT compression_ineffective_count FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return 0
        value = (
            row["compression_ineffective_count"]
            if isinstance(row, sqlite3.Row)
            else row[0]
        )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def set_compression_ineffective_count(self, session_id: str, count: int) -> None:
        """Persist the ineffective-compaction strike count for one session."""
        if not session_id:
            return
        normalized = max(0, int(count))

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET compression_ineffective_count = ? WHERE id = ?",
                (normalized, session_id),
            )

        self._execute_write(_do)

    # ──────────────────────────────────────────────────────────────────────
    # Compression locks
    # ──────────────────────────────────────────────────────────────────────
    # Atomic per-session locks that prevent two compression paths from
    # racing on the same session_id and producing orphan child sessions.
    #
    # The race: ``conversation_compression.py`` rotates ``agent.session_id``
    # as a side effect of a successful compression (end old session, create
    # new). That mutation is local to the AIAgent instance — but ``state.db``
    # is shared across all instances. Two AIAgents that share the same
    # ``session_id`` at the moment they both decide to compress (most
    # commonly the parent turn's agent + a background-review fork started
    # right after the turn ended) each end the parent and create their own
    # NEW session, parented to the same old id. The gateway SessionEntry
    # only catches one rotation; the other child silently accumulates
    # writes — Damien's "parent → two orphan children" repro shape.
    #
    # The lock is keyed by ``session_id`` and is held for the duration of
    # the compress() call plus the rotation. ``holder`` identifies the
    # current owner (pid:tid:nonce) for diagnostics; the lock is recovered
    # via ``expires_at`` if the holder process crashed without releasing.
    def refresh_compression_lock(
        self,
        session_id: str,
        holder: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        """Extend the compression lock lease if ``holder`` still owns it.

        Ownership is decided by the ``holder`` column alone, deliberately NOT
        by ``expires_at``: a live owner whose refresher thread was starved
        (GC pause, loaded CI runner, a slow write escaping ``_execute_write``'s
        retry budget) past its own TTL must be able to revive its still-unclaimed
        row on the next tick. Requiring ``expires_at >= now`` here made such a
        stall permanent — every later refresh matched 0 rows, so the owner kept
        compressing and rotating with no lease at all, which is exactly the
        unprotected window a competing path can fork the session lineage in.

        This does not resurrect a lock somebody else already took: SQLite
        serialises writes, so a reclaim (DELETE-expired + INSERT-or-IGNORE in
        :meth:`try_acquire_compression_lock`) and this UPDATE never interleave.
        Reclaim-first replaces ``holder``, so this UPDATE matches nothing and
        returns False; refresh-first pushes ``expires_at`` into the future, so
        the reclaimer's DELETE-expired matches nothing and its acquire fails.
        """
        if not session_id or not holder:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            cur = conn.execute(
                "UPDATE compression_locks SET expires_at = ? "
                "WHERE session_id = ? AND holder = ?",
                (expires_at, session_id, holder),
            )
            return cur.rowcount > 0

        try:
            return bool(self._execute_write(_do))
        except sqlite3.Error as exc:
            logger.warning(
                "refresh_compression_lock(%s) failed: %s",
                session_id,
                exc,
            )
            return False

    def try_acquire_compression_lock(
        self,
        session_id: str,
        holder: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        """Try to atomically acquire the compression lock for ``session_id``.

        Returns ``True`` on success (caller now owns the lock and must
        release via :meth:`release_compression_lock`).  Returns ``False``
        if another holder already owns a non-expired lock — the caller
        MUST NOT proceed with compression in that case (its rotation would
        race against the holder's, splitting the session lineage).

        Expired locks (``expires_at < now``) are reclaimed transparently.
        Structured holders whose local ``pid=`` no longer exists are reclaimed
        immediately, so a gateway killed during compression does not stall the
        replacement process for the full lease TTL.

        Implementation: single-transaction DELETE-expired + INSERT-or-IGNORE,
        followed by a SELECT to confirm we got the row. SQLite serialises
        writes, so the whole sequence is atomic against other writers.
        """
        if not session_id:
            return False
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            reclaimed_holder = None
            row = conn.execute(
                "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None:
                current_holder = (
                    row["holder"] if isinstance(row, sqlite3.Row) else row[0]
                )
                current_expires_at = (
                    row["expires_at"] if isinstance(row, sqlite3.Row) else row[1]
                )
                if current_expires_at < now or _compression_lock_holder_process_is_dead(
                    current_holder
                ):
                    conn.execute(
                        "DELETE FROM compression_locks "
                        "WHERE session_id = ? AND holder = ?",
                        (session_id, current_holder),
                    )
                    reclaimed_holder = current_holder
            # Then: try to insert. INSERT OR IGNORE returns no rowcount
            # difference — verify ownership via SELECT.
            conn.execute(
                "INSERT OR IGNORE INTO compression_locks "
                "(session_id, holder, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, holder, now, expires_at),
            )
            row = conn.execute(
                "SELECT holder FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            acquired = (
                row is not None
                and (row["holder"] if isinstance(row, sqlite3.Row) else row[0])
                == holder
            )
            return acquired, reclaimed_holder

        try:
            acquired, reclaimed_holder = self._execute_write(_do)
            if reclaimed_holder:
                logger.warning(
                    "Reclaimed stale compression lock for session=%s (holder=%s)",
                    session_id,
                    reclaimed_holder,
                )
            return bool(acquired)
        except sqlite3.Error as exc:
            logger.warning(
                "try_acquire_compression_lock(%s) failed: %s",
                session_id,
                exc,
            )
            # Fail open: returning False makes the caller skip compression,
            # which is the safe behaviour when the lock subsystem is broken.
            return False

    def release_compression_lock(self, session_id: str, holder: str) -> None:
        """Release the compression lock for ``session_id`` iff we own it.

        Idempotent: no-op when the lock has already expired and been
        reclaimed by a different holder, or when no lock exists. The
        ``holder`` check prevents a late-returning compressor from
        clobbering a fresh lock held by someone else.
        """
        if not session_id:
            return

        def _do(conn):
            conn.execute(
                "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
                (session_id, holder),
            )

        try:
            self._execute_write(_do)
        except sqlite3.Error as exc:
            logger.warning(
                "release_compression_lock(%s) failed: %s",
                session_id,
                exc,
            )

    def _session_turn_lease_key_on_conn(self, conn, session_id: str) -> str:
        """Walk compression parents on ``conn`` to the conversation lease key.

        Must run on the same connection as the lease INSERT/UPDATE/DELETE.
        A prior ``get_session`` failure must not compute a child id that the
        later write then persists: refresh would walk to the parent and
        fail-close. Markers bind to ``parent_session_id`` (same contract as
        ``_NON_CONTINUATION_CHILD_FILTER_SQL``). Lock errors propagate so
        ``_execute_write`` / ``acquire_session_turn_lease`` can retry.
        """
        if not session_id:
            return session_id

        def _row(sid: str):
            row = conn.execute(
                "SELECT id, parent_session_id, source, model_config, end_reason "
                "FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            return dict(row) if row else None

        current = _row(session_id)
        seen = {session_id}
        while current:
            parent_id = current.get("parent_session_id")
            if (
                not parent_id
                or parent_id in seen
                or self._is_explicit_fork_child_row(current)
            ):
                break
            parent = _row(parent_id)
            if not parent or parent.get("end_reason") != "compression":
                break
            seen.add(parent_id)
            current = parent
        return str(current.get("id") or session_id) if current else session_id

    def _session_turn_lease_key(self, session_id: str) -> str:
        """Return the stable serialization key for every compression segment.

        Acquire/refresh/release resolve this inside their write transaction.
        This helper is for tests and diagnostics; it does not swallow lock
        errors (a swallowed walk plus a later successful write was the
        fail-open that replayed the post-rotation refresh miss).
        """
        if not session_id:
            return session_id
        with self._read_ctx() as conn:
            return self._session_turn_lease_key_on_conn(conn, session_id)

    def try_acquire_session_turn_lease(
        self,
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float = 300.0,
        patience_s: float | None = None,
    ) -> bool:
        """Atomically acquire the cross-process turn lease for a conversation.

        Compression rotates a session into child segments, so the durable key
        is the lineage root rather than the current segment id. The walk and
        INSERT share one write transaction. Expired leases and leases whose
        structured local holder PID is known dead are reclaimed in that same
        transaction.
        """
        if not session_id or not holder:
            return False
        now = time.time()
        expires_at = now + max(0.1, float(ttl_seconds))

        def _do(conn):
            conversation_id = self._session_turn_lease_key_on_conn(conn, session_id)
            row = conn.execute(
                "SELECT holder, expires_at FROM session_turn_leases "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is not None:
                current_holder = row["holder"]
                if float(
                    row["expires_at"]
                ) <= now or _compression_lock_holder_process_is_dead(current_holder):
                    conn.execute(
                        "DELETE FROM session_turn_leases "
                        "WHERE conversation_id = ? AND holder = ?",
                        (conversation_id, current_holder),
                    )
            conn.execute(
                "INSERT OR IGNORE INTO session_turn_leases "
                "(conversation_id, holder, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, holder, now, expires_at),
            )
            owner = conn.execute(
                "SELECT holder FROM session_turn_leases WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            return owner is not None and owner["holder"] == holder

        return bool(self._execute_write(_do, patience_s=patience_s))

    def acquire_session_turn_lease(
        self,
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float = 300.0,
        wait_seconds: float = 1800.0,
        poll_interval_seconds: float = 1.0,
        on_wait=None,
        wait_notice_interval_seconds: float = 15.0,
        should_abort=None,
        acquire_patience_s: float = 0.5,
    ) -> bool:
        """Wait for a cross-process turn lease without holding a SQLite lock.

        ``on_wait(elapsed_seconds)`` is best-effort: invoked when the first
        attempt fails (elapsed ~0) and again about every
        ``wait_notice_interval_seconds`` while still waiting, so UIs can show
        that another process holds the conversation.

        When ``should_abort()`` returns True (for example the agent received
        ``/stop`` while waiting), acquisition stops immediately and returns
        False without consuming the full ``wait_seconds`` budget.
        """
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        wait_started = None
        last_notice_at = None
        notice_every = max(0.0, float(wait_notice_interval_seconds))
        while True:
            if should_abort is not None:
                try:
                    if should_abort():
                        return False
                except Exception:
                    logger.debug(
                        "session turn lease should_abort callback failed",
                        exc_info=True,
                    )
            try:
                if self.try_acquire_session_turn_lease(
                    session_id,
                    holder,
                    ttl_seconds=ttl_seconds,
                    patience_s=acquire_patience_s,
                ):
                    return True
            except sqlite3.Error as exc:
                # Long holder transactions (compression publish, large
                # flushes) can exhaust a single write-patience budget.
                # Keep polling until wait_seconds or should_abort.
                if classify_persistence_error(exc) != "locked":
                    raise
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                return False
            if wait_started is None:
                wait_started = now
            if on_wait is not None and (
                last_notice_at is None
                or notice_every == 0.0
                or (now - last_notice_at) >= notice_every
            ):
                try:
                    on_wait(max(0.0, now - wait_started))
                except Exception:
                    logger.debug(
                        "session turn lease on_wait callback failed",
                        exc_info=True,
                    )
                last_notice_at = now
            time.sleep(min(max(0.01, float(poll_interval_seconds)), remaining))

    def refresh_session_turn_lease(
        self,
        session_id: str,
        holder: str,
        *,
        ttl_seconds: float = 300.0,
    ) -> bool:
        """Extend a turn lease only while ``holder`` still owns it."""
        if not session_id or not holder:
            return False
        expires_at = time.time() + max(0.1, float(ttl_seconds))

        def _do(conn):
            conversation_id = self._session_turn_lease_key_on_conn(conn, session_id)
            cursor = conn.execute(
                "UPDATE session_turn_leases SET expires_at = ? "
                "WHERE conversation_id = ? AND holder = ?",
                (expires_at, conversation_id, holder),
            )
            return cursor.rowcount > 0

        return bool(self._execute_write(_do))

    def release_session_turn_lease(self, session_id: str, holder: str) -> None:
        """Release a turn lease iff ``holder`` still owns it; idempotent."""
        if not session_id or not holder:
            return

        def _do(conn):
            conversation_id = self._session_turn_lease_key_on_conn(conn, session_id)
            conn.execute(
                "DELETE FROM session_turn_leases "
                "WHERE conversation_id = ? AND holder = ?",
                (conversation_id, holder),
            )

        self._execute_write(_do)

    def get_compression_lock_holder(self, session_id: str) -> str | None:
        """Return the current (non-expired) holder for ``session_id``, or None.

        Diagnostic helper — not used by the locking protocol itself.
        """
        if not session_id:
            return None
        now = time.time()
        row = self._conn.execute(
            "SELECT holder FROM compression_locks "
            "WHERE session_id = ? AND expires_at >= ?",
            (session_id, now),
        ).fetchone()
        if row is None:
            return None
        return row["holder"] if isinstance(row, sqlite3.Row) else row[0]

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

    def record_auxiliary_usage(
        self,
        session_id: str,
        task: str,
        *,
        model: str | None = None,
        billing_provider: str | None = None,
        billing_base_url: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: float | None = None,
        api_call_count: int = 1,
    ) -> None:
        """Record an auxiliary LLM call's usage against *session_id* (issue #23270).

        Auxiliary calls (vision, compression, title_generation, web_extract,
        session_search, ...) historically discarded their usage, leaving the
        dashboard's per-model analytics blind to aux model spend. This writes
        a per-(model, provider, task) delta into ``session_model_usage`` —
        the same table the main loop's ``update_token_counts`` feeds — WITHOUT
        touching the ``sessions`` summary row. That separation is deliberate:
        the gateway overwrites session counters with absolute main-loop totals,
        so folding aux tokens into the summary row would either be clobbered
        or double-counted. Insights/analytics read the union of both.

        ``api_call_count`` defaults to 1 (one aux LLM call). Background-review
        forks record an aggregate of N fork API calls in one write with
        ``task='background_review'`` (issue #87250).

        Best-effort by contract: callers must never fail an aux call because
        accounting failed.
        """
        if not session_id or not task:
            return
        # FK on session_model_usage.session_id → sessions.id: ensure the row
        # exists (same INSERT OR IGNORE guard update_token_counts uses — the
        # initial create_session() can fail under concurrent SQLite locking).
        self._insert_session_row(session_id, "unknown")

        def _do(conn):
            self._record_model_usage(
                conn,
                session_id,
                model=model,
                billing_provider=billing_provider,
                billing_base_url=billing_base_url,
                billing_mode=None,
                input_tokens=input_tokens or 0,
                output_tokens=output_tokens or 0,
                cache_read_tokens=cache_read_tokens or 0,
                cache_write_tokens=cache_write_tokens or 0,
                reasoning_tokens=reasoning_tokens or 0,
                estimated_cost_usd=estimated_cost_usd,
                actual_cost_usd=None,
                cost_status=None,
                cost_source=None,
                api_call_count=(1 if api_call_count is None else int(api_call_count)),
                task=task,
            )

        self._execute_write(_do)

    def prune_empty_ghost_sessions(self, sessions_dir: "Path | None" = None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = time.time() - 86400  # Only sessions older than 24 hours

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
                conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", ids)
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
        cutoff = time.time() - 604800  # 7 days

        def _do(conn):
            now = time.time()
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

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get a session by ID."""
        # Cost/usage readers (/status, /usage, gateway endpoints) reach the
        # row through here; drain queued token deltas so they see exact
        # totals. No-op attribute check when nothing is queued.
        self.flush_token_counts()
        with self._read_ctx() as conn:
            cursor = conn.execute(
                "SELECT s.*, "
                "COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved "
                "FROM sessions s "
                "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
                "WHERE s.id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
        return self._session_row_dict(row) if row else None

    def get_dominant_session_model_route(
        self, session_id: str
    ) -> dict[str, Any] | None:
        """Return the main-loop model route that served most API calls.

        ``sessions`` is a legacy aggregate row and can hold model/provider fields
        written by different route changes. ``session_model_usage`` keeps the
        coherent per-call tuple, so persisted status and billing reads should use
        its dominant main-loop route when one is available.
        """
        self.flush_token_counts()
        with self._read_ctx() as conn:
            row = conn.execute(
                """SELECT model, billing_provider, billing_base_url, billing_mode,
                          api_call_count
                     FROM session_model_usage
                    WHERE session_id = ?
                      AND task = ''
                      AND model <> 'unknown'
                      AND billing_provider <> ''
                    ORDER BY api_call_count DESC,
                             (input_tokens + output_tokens + cache_read_tokens +
                              cache_write_tokens + reasoning_tokens) DESC,
                             last_seen DESC
                    LIMIT 1""",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> str | None:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = _escape_like(session_id_or_prefix)
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Columns excluded from compact_rows projections: only the payload-heavy
    # blob no list consumer renders. Everything else — including gateway
    # routing fields and desktop sidebar fields like git_branch — stays, and
    # the projection is derived from SCHEMA_SQL so columns added later via
    # declarative reconciliation are included automatically instead of
    # silently dropping out of list rows.
    _SESSION_COMPACT_EXCLUDED = frozenset(
        {"system_prompt", "system_prompt_hash", "git_metadata_generation"}
    )
    _session_compact_cols_sql: str | None = None

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
                raise CompressionSessionClosedError(session_id)
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

    def has_archived_messages(self, session_id: str) -> bool:
        """Return True if the session has any soft-archived (``active = 0``) rows.

        Cheap existence probe — does not load rows. NOTE: production rewrite
        paths no longer branch on this (they pass ``active_only=True``
        unconditionally — a probe can fail open or race a concurrent
        ``archive_and_compact``, #80216); kept for tests and diagnostics.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND active = 0 LIMIT 1",
                (session_id,),
            )
            return cursor.fetchone() is not None

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
            if lock_holder is not None:
                lock_row = conn.execute(
                    "SELECT holder, expires_at FROM compression_locks "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if (
                    lock_row is None
                    or lock_row["holder"] != lock_holder
                    or float(lock_row["expires_at"]) <= time.time()
                ):
                    raise SessionCompressionInProgressError(
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
                            parsed = json.loads(raw) if isinstance(raw, str) else raw
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

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def _is_explicit_fork_child_row(self, session: dict[str, Any]) -> bool:
        """True when ``session`` is a branch, delegate, or tool child of its parent.

        Markers only count as a fork when they point at ``parent_session_id``.
        Compression copies ``model_config`` onto the continuation
        (``publish_compression_child`` callers pass
        ``agent._session_init_model_config``), so a delegate's continuation
        carries ``_delegate_from=<the delegate's own parent>``. Presence-only
        matching would treat that real continuation as a fork — the same
        misclassification ``_NON_CONTINUATION_CHILD_FILTER_SQL`` already
        avoids by binding both markers to the queried parent.
        """
        if session.get("source") == "tool":
            return True
        raw = session.get("model_config")
        if not raw:
            return False
        try:
            cfg = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(cfg, dict):
            return False
        parent_id = session.get("parent_session_id")
        branched = cfg.get("_branched_from")
        delegated = cfg.get("_delegate_from")
        if parent_id:
            return branched == parent_id or delegated == parent_id
        return branched is not None or delegated is not None

    def _is_compression_child_row(self, child: dict[str, Any]) -> bool:
        parent_id = child.get("parent_session_id")
        if not parent_id or self._is_explicit_fork_child_row(child):
            return False
        parent = self.get_session(parent_id)
        return bool(parent and parent.get("end_reason") == "compression")

    def get_compression_lineage(self, session_id: str) -> list[str]:
        """Return compression ancestors through tip in chronological order."""
        session = self.get_session(session_id)
        if not session or self._is_explicit_fork_child_row(session):
            return [session_id] if session else []

        root = session
        ancestors = {root["id"]}
        while self._is_compression_child_row(root):
            parent = self.get_session(root["parent_session_id"])
            if not parent or parent["id"] in ancestors:
                break
            root = parent
            ancestors.add(root["id"])

        lineage = [root["id"]]
        seen = {root["id"]}
        current = root
        while current.get("end_reason") == "compression":
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE parent_session_id = ?
                    ORDER BY started_at ASC
                    """,
                    (current["id"],),
                ).fetchall()
            next_child = None
            for row in rows:
                candidate = dict(row)
                if self._is_compression_child_row(candidate):
                    next_child = candidate
                    break
            if not next_child or next_child["id"] in seen:
                break
            lineage.append(next_child["id"])
            seen.add(next_child["id"])
            current = next_child
            if current["id"] == session_id:
                # Continue to include later compression tips only when the
                # requested session itself was compacted.
                continue
        return lineage if session_id in lineage else [session_id]

    # ── Space reclamation ──

    # FTS5 virtual tables whose b-tree segments we merge on optimize. The
    # trigram table is created lazily / may be disabled, and the cjk-bigram
    # table only exists (and is only queryable) when the loadable tokenizer
    # is present — so we probe each before touching it (see optimize_fts).
    _FTS_TABLES = ("messages_fts", "messages_fts_trigram", "messages_fts_cjk")


class AsyncSessionDB:
    """Async door onto SessionDB: offloads each call via asyncio.to_thread so a blocking SQLite call never freezes the event loop. Generic forwarder — the audit confirms no method returns a live cursor/generator."""

    def __init__(self, db: "SessionDB") -> None:
        self._db = db

    def __getattr__(self, name: str):
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr

        async def _offloaded(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)

        return _offloaded
