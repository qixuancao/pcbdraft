"""Context-local identity and environment for terminal and Web conversations."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_session_id: ContextVar[str | None] = ContextVar("pcbdraft_session_id", default=None)
_environment: ContextVar[Mapping[str, str] | None] = ContextVar(
    "pcbdraft_session_env", default=None
)
_stateless: ContextVar[bool] = ContextVar("pcbdraft_stateless", default=False)


def get_session_env(name: str, default: str | None = None) -> str | None:
    if name in {"PCBDRAFT_RUNTIME_SESSION_ID", "SESSION_ID"}:
        return _session_id.get() or default
    return (_environment.get() or {}).get(name, os.environ.get(name, default))


def set_current_session_id(value: str | None) -> None:
    _session_id.set(value)


@contextmanager
def scoped_current_session_id(value: str | None = None) -> Iterator[None]:
    token = _session_id.set(value)
    try:
        yield
    finally:
        _session_id.reset(token)


@contextmanager
def session_environment(values: Mapping[str, str]) -> Iterator[None]:
    token = _environment.set(dict(values))
    try:
        yield
    finally:
        _environment.reset(token)


def session_context_engaged() -> bool:
    return _session_id.get() is not None


def declare_stateless_channel() -> None:
    _stateless.set(True)


def async_delivery_supported() -> bool:
    # Neither the PCB terminal nor Web jobs provide background message delivery.
    return False


def session_is_messaging_surface() -> bool:
    return False
