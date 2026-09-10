"""Locked, retryable history migration with native writes and legacy reads."""

from __future__ import annotations

import logging
import os
import tempfile
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from prompt_toolkit.history import FileHistory

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.locking import ResourceLock

_MIGRATED = b"# pcbdraft-history: legacy import completed v1\n"
_LOG = logging.getLogger(__name__)


def _history_lock(destination: Path) -> ResourceLock:
    return ResourceLock(destination, destination.parent / "locks", timeout=1.0)


def _read_native(destination: Path) -> bytes:
    try:
        return destination.read_bytes()
    except FileNotFoundError:
        return b""


def _publish_history_locked(destination: Path, data: bytes) -> None:
    """Publish a complete snapshot, leaving the original intact on write errors."""
    fd, temporary = tempfile.mkstemp(
        prefix=".pcbdraft-history-", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            _LOG.debug("Unable to remove unpublished history snapshot", exc_info=True)
        raise
    # Successful replacement consumes the temporary path. Do not perform any
    # fallible cleanup after commit that could make the caller retry this batch.


def _migrate_locked(destination: Path) -> None:
    """Merge and publish data + completion marker in one atomic replacement.

    The same lock is held by native appenders. A pre-existing destination can
    have been created after a failed migration, so its existence is not proof
    of completion. Preserve both histories, and recognize copies made by the
    former unmarked migration to avoid importing their legacy prefix twice.
    """
    legacy = destination.with_name(".hermes_history")
    current = _read_native(destination)
    if current.startswith(_MIGRATED) or not legacy.is_file():
        return
    data = legacy.read_bytes()
    if data and not current.startswith(data):
        current = data + b"\n" + current
    _publish_history_locked(destination, _MIGRATED + current)


def _attempt_migration_locked(destination: Path) -> bool:
    try:
        _migrate_locked(destination)
    except OSError:
        _LOG.warning(
            "Terminal history migration pending; legacy history remains readable",
            exc_info=True,
        )
        return False
    return True


def migrate_history_file(destination: Path) -> bool:
    """Return completion status; failures remain pending even if native exists."""
    try:
        with _history_lock(destination):
            return _attempt_migration_locked(destination)
    except (OSError, PCBDraftError):
        _LOG.warning(
            "Unable to lock terminal history migration; will retry", exc_info=True
        )
        return False


class NativeFileHistory(FileHistory):
    """Keep legacy prompts readable while failed publication is retried.

    All new prompts go to the native file. Migration and append share a lock,
    so a competing launch cannot replace a history file underneath an append.
    The embedded completion marker is committed with the copied bytes, not in
    a separate sidecar which could get ahead of or behind the file publication.
    """

    def __init__(self, filename: str) -> None:
        super().__init__(filename)
        self.migration_pending = True
        self._pending_appends: deque[str] = deque()
        self._append_lock = RLock()

    @property
    def append_pending(self) -> bool:
        """Whether prompts remain uncommitted, independent of legacy migration."""
        with self._append_lock:
            return bool(self._pending_appends)

    def _flush_appends_locked(self) -> None:
        """Commit the ordered queue once; partial I/O cannot duplicate a retry.

        Both the instance queue lock and the shared history lock must be held.
        A snapshot includes the latest on-disk history, so another instance's
        committed prompts are preserved. Dequeue only after atomic publication.
        """
        if not self._pending_appends:
            return
        destination = Path(self.filename)
        try:
            records = []
            for string in self._pending_appends:
                records.append(f"\n# {datetime.now(UTC)}\n")
                records.extend(f"+{line}\n" for line in string.split("\n"))
            _publish_history_locked(
                destination,
                _read_native(destination) + "".join(records).encode("utf-8"),
            )
        except OSError:
            _LOG.warning(
                "Terminal history append pending; queued prompts will be retried",
                exc_info=True,
            )
            return
        self._pending_appends.clear()

    def _load_with_legacy(self) -> list[str]:
        destination = Path(self.filename)
        native = list(reversed(self._pending_appends)) + list(
            super().load_history_strings()
        )
        if not self.migration_pending:
            return native
        legacy = destination.with_name(".hermes_history")
        current = _read_native(destination)
        if current.startswith(_MIGRATED) or not legacy.is_file():
            return native
        data = legacy.read_bytes()
        if data and current.startswith(data):
            return native
        return native + list(FileHistory(str(legacy)).load_history_strings())

    def load_history_strings(self) -> list[str]:
        destination = Path(self.filename)
        with self._append_lock:
            try:
                with _history_lock(destination):
                    self.migration_pending = not _attempt_migration_locked(destination)
                    self._flush_appends_locked()
                    return self._load_with_legacy()
            except (OSError, PCBDraftError):
                _LOG.warning(
                    "Unable to lock terminal history read; reading available histories",
                    exc_info=True,
                )
                return self._load_with_legacy()

    def flush_pending(self) -> None:
        """Retry queued appends without adding another prompt."""
        destination = Path(self.filename)
        with self._append_lock:
            if not self._pending_appends:
                return
            try:
                with _history_lock(destination):
                    self.migration_pending = not _attempt_migration_locked(destination)
                    self._flush_appends_locked()
            except (OSError, PCBDraftError):
                _LOG.warning(
                    "Unable to lock terminal history append; queued prompts will be retried",
                    exc_info=True,
                )

    def store_string(self, string: str) -> None:
        with self._append_lock:
            self._pending_appends.append(string)
            self.flush_pending()
