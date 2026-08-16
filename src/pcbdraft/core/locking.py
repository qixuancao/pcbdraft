"""Process-safe advisory locks for runtime-managed design resources."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Self

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import make_directory

if sys.platform == "win32":  # pragma: no cover - exercised by the Windows CI job
    import msvcrt
else:  # pragma: no cover - branch selection is platform-specific
    import fcntl


class ResourceLock:
    """Exclusive process lock keyed by a canonical resource path.

    The lock directory is runtime-owned rather than inside an untrusted KiCad
    project. Locks are advisory: every writer in PCBDraft uses them, but
    unrelated editors still require hash-based conflict detection.
    """

    def __init__(
        self, resource: str | Path, lock_parent: str | Path, *, timeout: float = 10.0
    ):
        if timeout < 0 or timeout > 300:
            raise ValidationError("lock timeout must be between 0 and 300 seconds")
        self.resource = Path(resource).resolve(strict=False)
        self.lock_parent = Path(lock_parent).resolve(strict=False)
        self.timeout = timeout
        key = hashlib.sha256(os.fsencode(str(self.resource))).hexdigest()
        self.path = self.lock_parent / f"{key}.lock"
        self._fd: int | None = None

    def acquire(self) -> Self:
        make_directory(self.lock_parent)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise PCBDraftError(f"cannot open runtime lock: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValidationError("runtime lock is not a private regular file")
            if sys.platform == "win32" and info.st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._try_platform_lock(fd)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise PCBDraftError(
                            f"resource is locked by another runtime process: {self.resource}"
                        ) from exc
                    time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
                except OSError as exc:
                    if sys.platform != "win32":
                        raise
                    if time.monotonic() >= deadline:
                        raise PCBDraftError(
                            f"resource is locked by another runtime process: {self.resource}"
                        ) from exc
                    time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            metadata = f"pid={os.getpid()}\nresource_sha256={self.path.stem}\n".encode()
            if sys.platform == "win32":
                # Keep the byte covered by ``msvcrt.locking`` in place.
                os.ftruncate(fd, 1)
                os.lseek(fd, 1, os.SEEK_SET)
            else:
                os.ftruncate(fd, 0)
            os.write(fd, metadata)
            os.fsync(fd)
            self._fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _try_platform_lock(fd: int) -> None:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _platform_unlock(fd: int) -> None:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(fd, fcntl.LOCK_UN)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self._platform_unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
