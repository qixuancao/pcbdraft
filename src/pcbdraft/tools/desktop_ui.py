#!/usr/bin/env python3
"""Bridge desktop-only tools to Hermes-desktop renderer events.

The preview pane, pane focus, and friends live in the desktop renderer, so
desktop-gated tools reach them through an emitter the desktop ``tui_gateway``
installs at session start via :func:`set_emitter`. Everywhere else it stays
``None`` and the tools report "desktop only". Routing keys off
``PCBDRAFT_RUNTIME_UI_SESSION_ID`` so the event lands on the window that owns the turn
(``_emit``/``write_json`` is ``_stdout_lock``-guarded, so emitting from the
tool's thread is safe).
"""

from collections.abc import Callable
from typing import Optional

from pcbdraft.agent.session_context import get_session_env

# (sid, event, payload) sink, installed by the desktop gateway.
_emit: Callable[[str, str, dict], None] | None = None


def set_emitter(fn: Callable[[str, str, dict], None] | None) -> None:
    """Install (or clear) the renderer-event sink. Called by the desktop gateway."""
    global _emit
    _emit = fn


def available() -> bool:
    """True when running under the desktop app (an emitter is wired)."""
    return _emit is not None


def emit(event: str, payload: dict) -> bool:
    """Route ``event`` to the window that owns the current turn.

    Returns ``False`` when no emitter is wired (i.e. not the desktop app)."""
    fn = _emit
    if fn is None:
        return False
    fn(get_session_env("PCBDRAFT_RUNTIME_UI_SESSION_ID", ""), event, payload)
    return True
