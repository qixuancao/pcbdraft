"""Detailed JSONL debug tracing for PCBDraft agent conversations.

Every step of a live agent turn — session start, each model request and
response, each provider error, each tool call and its result, and the final
turn reply — is appended as one JSON line to a rotating ``agent-trace.jsonl``
file.  The trace answers two questions during testing: which step failed,
and what exactly the agent did at every step.

The writer is deliberately defensive: tracing must never break the agent
process, so every failure (disk full, unwritable path, unserializable
payload) is swallowed and, at most, reported once through ``logging``.

Enable/disable and location:
* ``PCBDRAFT_DEBUG_TRACE`` — ``0``/``off``/``false``/``no`` disables the
  trace; any other non-empty value (and the default unset state) keeps it
  on.
* ``PCBDRAFT_DEBUG_TRACE_PATH`` — explicit trace file path.  Defaults to
  ``<pcbdraft config dir>/debug/agent-trace.jsonl`` next to the user's
  ``config.toml``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from pcbdraft.core.platform_paths import pcbdraft_config_dir
from pcbdraft.core.runs import utc_timestamp

__all__ = (
    "MAX_TRACE_BYTES",
    "TRACE_BACKUPS",
    "TRACE_ENV",
    "TRACE_PATH_ENV",
    "DebugTraceWriter",
    "default_trace_path",
    "record_event",
    "reset_trace_writer",
    "trace_enabled",
    "trace_path",
)

logger = logging.getLogger(__name__)

TRACE_ENV = "PCBDRAFT_DEBUG_TRACE"
TRACE_PATH_ENV = "PCBDRAFT_DEBUG_TRACE_PATH"

#: Rotate once the live trace file grows past this size.
MAX_TRACE_BYTES = 16 * 1024 * 1024

#: Rotated files kept as ``.1`` … ``.N`` before the oldest is deleted.
TRACE_BACKUPS = 2

_DISABLED_VALUES = frozenset({"0", "off", "false", "no"})

_MAX_DEPTH = 8
_MAX_STRING = 8_000
_MAX_SEQUENCE = 200
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "x_api_key",
        "api_token",
        "access_token",
        "refresh_token",
        "password",
    }
)


def trace_enabled() -> bool:
    """Return whether detailed agent tracing is enabled for this process."""

    value = os.environ.get(TRACE_ENV, "").strip().casefold()
    return value not in _DISABLED_VALUES


def default_trace_path() -> Path:
    """Return the default trace path under the PCBDraft config directory."""

    return pcbdraft_config_dir() / "debug" / "agent-trace.jsonl"


def trace_path() -> Path:
    """Return the effective trace file path for this process."""

    explicit = os.environ.get(TRACE_PATH_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return default_trace_path()


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact one trace payload so logs stay reviewable."""

    if depth > _MAX_DEPTH:
        return f"<{type(value).__name__} depth limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            return (
                value[:_MAX_STRING] + f"...[truncated {len(value) - _MAX_STRING} chars]"
            )
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if _is_mapping(value):
        try:
            entries = list(value.items())
        except Exception:  # noqa: BLE001 - fall back to a plain string view
            return _bounded_text(str(value))
        items = entries[:_MAX_SEQUENCE]
        truncated = len(entries) - len(items)
        result: dict[str, Any] = {}
        for key, item in items:
            clean_key = key if isinstance(key, str) else str(key)
            if clean_key.casefold().replace("-", "_") in _SENSITIVE_KEYS:
                result[clean_key] = "***redacted***"
            else:
                result[clean_key] = _sanitize(item, depth=depth + 1)
        if truncated > 0:
            result["..."] = f"<{truncated} more entries omitted>"
        return result
    if _is_sequence(value):
        try:
            items = list(value)
        except Exception:  # noqa: BLE001 - fall back to a plain string view
            return _bounded_text(str(value))
        truncated = len(items) - _MAX_SEQUENCE
        result_list = [
            _sanitize(item, depth=depth + 1) for item in items[:_MAX_SEQUENCE]
        ]
        if truncated > 0:
            result_list.append(f"<{truncated} more items omitted>")
        return result_list
    return _bounded_text(str(value))


def _is_mapping(value: Any) -> bool:
    return hasattr(value, "items") and hasattr(value, "__getitem__")


def _is_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple)) or (
        hasattr(value, "__len__")
        and hasattr(value, "__iter__")
        and not isinstance(value, (str, bytes, bytearray))
    )


def _bounded_text(text: str) -> str:
    if len(text) > _MAX_STRING:
        return text[:_MAX_STRING] + f"...[truncated {len(text) - _MAX_STRING} chars]"
    return text


class DebugTraceWriter:
    """Thread-safe append-only JSONL writer with size-based rotation."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = MAX_TRACE_BYTES,
        backups: int = TRACE_BACKUPS,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.backups = max(0, int(backups))
        self._lock = threading.Lock()
        self._sequence = 0
        self._warned = False

    def record(self, event: str, **fields: Any) -> None:
        """Append one trace line; never raises into the agent loop."""

        try:
            with self._lock:
                line = self._render(event, fields)
                self._append(line)
        except Exception as exc:  # noqa: BLE001 - tracing must never break the agent
            if not self._warned:
                self._warned = True
                logger.warning("agent debug trace write failed: %s", exc)

    def _render(self, event: str, fields: dict[str, Any]) -> str:
        self._sequence += 1
        record = {
            "seq": self._sequence,
            "timestamp": utc_timestamp(),
            "pid": os.getpid(),
            "event": event,
            "data": _sanitize(fields),
        }
        return json.dumps(record, ensure_ascii=False, default=str)

    def _append(self, line: str) -> None:
        payload = (line + "\n").encode("utf-8")
        self._rotate_if_needed(len(payload))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as stream:
            stream.write(payload)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            current = self.path.stat().st_size
        except OSError:
            return
        if current + incoming <= self.max_bytes:
            return
        if self.backups <= 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(oldest)
                oldest = source
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))


_writer: DebugTraceWriter | None = None
_writer_lock = threading.Lock()


def record_event(event: str, **fields: Any) -> None:
    """Record one trace event using the process-wide writer."""

    global _writer
    if not trace_enabled():
        return
    if _writer is None:
        with _writer_lock:
            if _writer is None:
                _writer = DebugTraceWriter(trace_path())
    _writer.record(event, **fields)


def reset_trace_writer() -> None:
    """Drop the cached writer (tests and path/env changes)."""

    global _writer
    with _writer_lock:
        _writer = None
