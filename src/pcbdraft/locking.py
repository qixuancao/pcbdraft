"""Process-safe advisory locks for runtime-managed design resources."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import time
from pathlib import Path
from types import TracebackType
from typing import Self

from .errors import PCBDraftError, ValidationError
from .io import make_directory


class ResourceLock:
    """Exclusive Linux ``flock`` keyed by a canonical resource path.

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
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
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
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise PCBDraftError(
                            f"resource is locked by another runtime process: {self.resource}"
                        ) from exc
                    time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            os.ftruncate(fd, 0)
            os.write(
                fd, f"pid={os.getpid()}\nresource_sha256={self.path.stem}\n".encode()
            )
            os.fsync(fd)
            self._fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
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
