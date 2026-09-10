"""Merge PCBDraft defaults into the authoritative runtime configuration."""

from __future__ import annotations

import copy
import stat
from pathlib import Path
from typing import Any

from pcbdraft.core.runtime_paths import runtime_home

__all__ = ("write_runtime_config",)


def _mapping(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, dict):
        value = {}
        config[key] = value
    return value


def write_runtime_config() -> Path:
    """Ensure PCBDraft defaults without changing provider ownership.

    The active ``model``, provider definitions, auxiliary models and every
    authentication reference retain their existing semantics.  The
    vendored fail-closed atomic writer owns persistence and file permissions.
    """

    from pcbdraft.model.configuration import read_user_config_raw, save_config

    config_path = runtime_home() / "config.yaml"
    original = read_user_config_raw(config_path)
    config = copy.deepcopy(original)
    _mapping(config, "model")["persist_switch_by_default"] = True
    _mapping(config, "display")["interface"] = "cli"
    # The PCB agent receives only the closed concrete PCB toolbox. General
    # shell/file/code tools are intentionally outside the default authority.
    _mapping(config, "platform_toolsets")["cli"] = ["pcbdraft"]
    plugins = _mapping(config, "plugins")
    enabled = plugins.get("enabled")
    enabled_list = [str(item) for item in enabled] if isinstance(enabled, list) else []
    # Retire the old disk-loaded observer shim. Contracts are built into the loop.
    plugins["enabled"] = [name for name in enabled_list if name != "pcbdraft-debug"]
    _mapping(_mapping(config, "tools"), "tool_search")["enabled"] = False
    if config != original:
        save_config(config, strip_defaults=False)
    if stat.S_IMODE(config_path.stat().st_mode) != 0o600:
        config_path.chmod(0o600)
    return config_path
