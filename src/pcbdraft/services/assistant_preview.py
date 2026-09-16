"""Bounded, stateful assistant text previews for transient UI streams."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from pcbdraft.agent.redact import redact_sensitive_text
from pcbdraft.core.redaction import sanitize_user_text

MAX_ASSISTANT_PREVIEW_BYTES = 16 * 1024
MAX_ASSISTANT_DELTA_BYTES = 4 * 1024

AssistantPreviewSink = Callable[[str, str, str], None]
logger = logging.getLogger(__name__)


def _utf8_prefix(value: str, limit: int) -> tuple[str, int]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value, len(encoded)
    prefix = encoded[:limit].decode("utf-8", errors="ignore")
    return prefix, len(prefix.encode("utf-8"))


def _safe_preview_text(value: str) -> str:
    # Run the strict public-boundary patterns first so common prefixed tokens
    # are removed completely rather than retaining the diagnostic head/tail
    # mask used by the agent's internal logs.
    return sanitize_user_text(
        redact_sensitive_text(sanitize_user_text(value), force=True)
    )


class SafeAssistantPreview:
    """Release deltas only after a boundary that closes split credentials.

    Model providers may split ``api_key`` or ``sk-`` credentials across
    arbitrary callbacks.  Holding the unfinished word means redaction always
    sees the complete candidate.  A hard turn limit prevents an unbroken model
    response from growing memory without bound; once reached, the retained
    prefix is safely flushed and later preview bytes are ignored.
    """

    def __init__(
        self,
        project_id: str,
        turn_id: str,
        sink: AssistantPreviewSink,
        *,
        max_total_bytes: int = MAX_ASSISTANT_PREVIEW_BYTES,
        max_delta_bytes: int = MAX_ASSISTANT_DELTA_BYTES,
    ) -> None:
        self.project_id = project_id
        self.turn_id = turn_id
        self.sink = sink
        self.max_total_bytes = max(1, max_total_bytes)
        self.max_delta_bytes = max(1, max_delta_bytes)
        self._buffer = ""
        self._accepted_bytes = 0
        self._finished = False
        self._lock = threading.RLock()

    def feed(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            return
        with self._lock:
            if self._finished or self._accepted_bytes >= self.max_total_bytes:
                return
            remaining = self.max_total_bytes - self._accepted_bytes
            accepted, accepted_bytes = _utf8_prefix(value, remaining)
            self._accepted_bytes += accepted_bytes
            self._buffer += accepted.replace("\x00", "")
            exhausted = accepted_bytes < len(value.encode("utf-8", errors="replace"))
            boundary = max(
                (
                    index + 1
                    for index, char in enumerate(self._buffer)
                    if char.isspace()
                ),
                default=0,
            )
            if boundary:
                ready, self._buffer = self._buffer[:boundary], self._buffer[boundary:]
                self._emit(ready)
            if exhausted or self._accepted_bytes >= self.max_total_bytes:
                self._emit(self._buffer)
                self._buffer = ""

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._emit(self._buffer)
            self._buffer = ""

    def _emit(self, value: str) -> None:
        safe = _safe_preview_text(value)
        while safe:
            delta, _size = _utf8_prefix(safe, self.max_delta_bytes)
            if not delta:
                break
            try:
                self.sink(self.project_id, self.turn_id, delta)
            except Exception:
                # A preview is advisory.  UI transport failures must never
                # change the authoritative conversation outcome.
                logger.debug("assistant preview sink failed", exc_info=True)
            safe = safe[len(delta) :]
