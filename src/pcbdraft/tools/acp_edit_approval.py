"""Optional ACP edit gate: absent integrations are inert, active failures deny."""

from __future__ import annotations

import os
from contextvars import ContextVar, Token

_enabled: ContextVar[bool] = ContextVar("pcbdraft_acp_edit_approval", default=False)


def set_acp_edit_approval_enabled(enabled: bool) -> Token:
    """ACP session owners bind this before dispatch and reset it afterwards."""
    return _enabled.set(enabled)


def reset_acp_edit_approval_enabled(token: Token) -> None:
    _enabled.reset(token)


def maybe_require_edit_approval(name: str, arguments: dict):
    try:
        from acp_adapter.edit_approval import maybe_require_edit_approval as approve
    except ModuleNotFoundError as exc:
        if (
            exc.name in {"acp_adapter", "acp_adapter.edit_approval"}
            and not _enabled.get()
            and os.environ.get("_PCBDRAFT_ACP") != "1"
        ):
            return None
        raise
    return approve(name, arguments)
