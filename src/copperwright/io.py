"""Private, bounded, atomic filesystem I/O."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import CopperWrightError


def make_directory(path: Path, *, parents: bool = True) -> Path:
    """Create a directory and make the directory itself private."""
    try:
        path.mkdir(mode=0o700, parents=parents, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise CopperWrightError(f"cannot create private directory: {path}") from exc
    return path


def portable_record_path(path: Path) -> str:
    """Represent a record path without embedding a checkout-specific root."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.name


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace a file atomically, with a same-directory fsynced temporary."""
    if not path.parent.exists():
        make_directory(path.parent)
    elif not path.parent.is_dir():
        raise CopperWrightError(
            f"atomic write parent is not a directory: {path.parent}"
        )
    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".copperwright-", dir=path.parent
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
        path.chmod(mode)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise CopperWrightError(f"atomic write failed: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    try:
        rendered = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise CopperWrightError(f"cannot serialize JSON artifact: {path.name}") from exc
    atomic_write_text(path, rendered, mode=mode)


def read_bytes_limited(path: Path, limit: int) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CopperWrightError(f"cannot read artifact: {path}") from exc
    if size > limit:
        raise CopperWrightError(f"artifact exceeds {limit} byte limit: {path.name}")
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise CopperWrightError(f"cannot read artifact: {path}") from exc
    if len(data) > limit:
        raise CopperWrightError(f"artifact exceeds {limit} byte limit: {path.name}")
    return data


def read_text_limited(path: Path, limit: int) -> str:
    try:
        return read_bytes_limited(path, limit).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CopperWrightError(f"artifact is not UTF-8 text: {path.name}") from exc


def load_json_limited(path: Path, limit: int) -> Any:
    try:
        return json.loads(read_text_limited(path, limit))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise CopperWrightError(f"invalid JSON artifact: {path.name}") from exc


def privatize_tree(root: Path) -> None:
    """Tighten modes under a run-owned tree without following symlinks."""
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        current_path.chmod(0o700)
        for directory in directories:
            member = current_path / directory
            if not member.is_symlink():
                member.chmod(0o700)
        for filename in files:
            member = current_path / filename
            if not member.is_symlink():
                member.chmod(0o600)
