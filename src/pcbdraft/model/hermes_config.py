"""Merge PCBDraft-owned defaults into Hermes' authoritative configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pcbdraft.core.hermes_paths import DEBUG_PLUGIN_DIR_NAME, hermes_home

__all__ = ("write_hermes_config",)


def _mapping(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, dict):
        value = {}
        config[key] = value
    return value


def write_hermes_config() -> Path:
    """Ensure PCBDraft defaults without changing Hermes provider ownership.

    The active ``model``, provider definitions, auxiliary models and every
    authentication reference remain byte-for-byte semantic Hermes state.  The
    vendored fail-closed atomic writer owns persistence and file permissions.
    """

    from hermes_cli.config import read_user_config_raw, save_config

    config_path = hermes_home() / "config.yaml"
    config = read_user_config_raw(config_path)
    _mapping(config, "model")["persist_switch_by_default"] = True
    _mapping(config, "display")["interface"] = "cli"
    _mapping(config, "platform_toolsets")["cli"] = ["hermes-cli", "pcbdraft"]
    plugins = _mapping(config, "plugins")
    enabled = plugins.get("enabled")
    enabled_list = [str(item) for item in enabled] if isinstance(enabled, list) else []
    if DEBUG_PLUGIN_DIR_NAME not in enabled_list:
        enabled_list.append(DEBUG_PLUGIN_DIR_NAME)
    plugins["enabled"] = enabled_list
    _mapping(_mapping(config, "tools"), "tool_search")["enabled"] = False
    save_config(config, strip_defaults=False)
    config_path.chmod(0o600)
    return config_path
