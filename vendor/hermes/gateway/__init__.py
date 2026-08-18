"""PCBDraft vendored-trim stub for the Hermes ``gateway`` package.

The gateway (messaging platforms, transport, dashboard) is intentionally not
vendored.  A few core agent/tool modules import ``gateway.*`` lazily at
runtime to detect messaging-session context.  This stub provides semantically
correct no-op implementations for the symbols those modules touch, and a
generic fallback for anything else, so the CLI/PCB agent runs without the
gateway while remaining import-compatible with Hermes tool code.
"""

from __future__ import annotations

import sys
import types

__all__ = ("install",)


def _dummy_callable(name: str):
    def _f(*_args, **_kwargs):
        return None

    _f.__name__ = name
    _f.__doc__ = f"gateway stub for {name}"
    return _f


class _StubModule(types.ModuleType):
    def __init__(self, name: str, *args, **kwargs) -> None:
        super().__init__(name, *args, **kwargs)
        self.__path__ = []  # namespace-style: missing submodules ImportError cleanly

    def __getattr__(self, item: str):
        if item.startswith("__"):
            raise AttributeError(item)
        value = _dummy_callable(f"{self.__name__}.{item}")
        setattr(self, item, value)
        return value


_SESSION_CONTEXT_CODE = """
import contextlib
from types import SimpleNamespace


class _Unset:
    def __repr__(self):
        return "<unset>"


_UNSET = _Unset()
_VAR_MAP = {}
_SESSION_ASYNC_DELIVERY = False


def get_session_env(name, default=None):
    return default


def set_current_session_id(value):
    return None


def scoped_current_session_id(value=None):
    return contextlib.nullcontext()


def session_context_engaged():
    return False


def async_delivery_supported():
    return False


def session_is_messaging_surface():
    return False


def declare_stateless_channel(*args, **kwargs):
    return None
"""

_STATUS_CODE = """
def _pid_exists(pid):
    return False


def get_process_start_time(pid):
    return 0


def get_running_pid(profile=None):
    return None


def get_running_pid_cached(profile=None):
    return None


def get_runtime_status_running_pid(profile=None):
    return None


def is_gateway_running():
    return False


def terminate_pid(pid, **kwargs):
    return False


def write_planned_stop_marker(**kwargs):
    return None


def read_runtime_status(profile=None):
    return {}


def runtime_status_is_stale(**kwargs):
    return False


def runtime_status_pid_is_live(**kwargs):
    return False


def parse_active_agents(**kwargs):
    return []


def derive_gateway_busy(**kwargs):
    return False


def derive_gateway_drainable(**kwargs):
    return False


def looks_like_gateway_command_line(**kwargs):
    return False


def looks_like_gateway_runtime_command_line(**kwargs):
    return False


def resolve_gateway_liveness(**kwargs):
    return None


def record_start_and_check_storm(**kwargs):
    return None


def normalize_updated_at(**kwargs):
    return None


def _get_process_start_time(pid):
    return 0


def _read_pid_record(**kwargs):
    return None


def _pid_from_record(**kwargs):
    return None
"""

_CONFIG_CODE = """
def load_gateway_config(*args, **kwargs):
    return {}


def _getenv(*args, **kwargs):
    return None


def _env_multiplex_profiles_override(*args, **kwargs):
    return None


def _normalize_multiplex_profile_allowlist(*args, **kwargs):
    return None


def coerce_systemd_watchdog_seconds(*args, **kwargs):
    return None
"""

_PLATFORM_REGISTRY_CODE = """
class _StubRegistry:
    def get(self, *args, **kwargs):
        return None

    def get_all(self, *args, **kwargs):
        return []

    def all(self, *args, **kwargs):
        return []

    def keys(self):
        return []

    def items(self):
        return []

    def values(self):
        return []

    def register(self, *args, **kwargs):
        return None

    def __contains__(self, key):
        return False

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __getitem__(self, key):
        raise KeyError(key)


platform_registry = _StubRegistry()
"""


def _make_module(fullname: str, code: str) -> types.ModuleType:
    module = types.ModuleType(fullname)
    module.__file__ = f"<pcbdraft gateway stub {fullname}>"
    exec(compile(code, f"<{fullname}>", "exec"), module.__dict__)
    return module


_SUBMODULE_CODE = {
    "gateway.session_context": _SESSION_CONTEXT_CODE,
    "gateway.status": _STATUS_CODE,
    "gateway.config": _CONFIG_CODE,
    "gateway.platform_registry": _PLATFORM_REGISTRY_CODE,
}

#: Submodules that only need the generic attribute fallback.
_GENERIC_SUBMODULES = (
    "gateway.run",
    "gateway.relay",
    "gateway.restart",
    "gateway.lifecycle_ledger",
    "gateway.memory_status",
    "gateway.disk_status",
    "gateway.drain_control",
    "gateway.readiness",
    "gateway.pairing",
    "gateway.mirror",
    "gateway.channel_directory",
    "gateway.platforms",
    "gateway.platforms.base",
    "gateway.platforms.whatsapp_common",
    "gateway.platforms.signal_format",
    "gateway.platforms.signal_rate_limit",
    "gateway.platforms.weixin",
    "gateway.platforms.yuanbao",
    "gateway.platforms.yuanbao_sticker",
    "gateway.platforms.qqbot",
    "gateway.platforms.bluebubbles",
    "gateway.platforms.wecom",
    "gateway.platforms.telegram",
    "gateway.platforms.discord",
    "gateway.platforms.slack",
    "gateway.platforms.matrix",
    "gateway.platforms.signal",
    "gateway.platforms.whatsapp",
    "gateway.platforms.email",
    "gateway.platforms.sms",
    "gateway.platforms.homeassistant",
    "gateway.platforms.irc",
    "gateway.platforms.mattermost",
    "gateway.platforms.feishu",
    "gateway.platforms.dingtalk",
    "gateway.platforms.line",
    "gateway.platforms.ntfy",
    "gateway.platforms.simplex",
    "gateway.platforms.photon",
    "gateway.platforms.raft",
    "gateway.platforms.google_chat",
    "gateway.platforms.buzz",
)


def install() -> None:
    """Register stub ``gateway.*`` modules into ``sys.modules``."""

    parent = _make_module("gateway", "")
    parent.__path__ = []  # namespace package marker
    sys.modules.setdefault("gateway", parent)
    for fullname, code in _SUBMODULE_CODE.items():
        sys.modules.setdefault(fullname, _make_module(fullname, code))
    for fullname in _GENERIC_SUBMODULES:
        if fullname not in sys.modules:
            sys.modules[fullname] = _StubModule(fullname)


install()