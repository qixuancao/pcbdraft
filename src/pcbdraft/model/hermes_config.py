"""Translate the PCBDraft model configuration into Hermes ``config.yaml``.

The active provider in ``~/.config/pcbdraft/config.toml`` becomes the Hermes
model block (OpenAI-compatible endpoint).  The generated file is derived
state — it is rewritten on every launch and can be regenerated at any time.
"""

from __future__ import annotations

import os
from pathlib import Path

from pcbdraft.core.hermes_paths import DEBUG_PLUGIN_DIR_NAME, hermes_home
from pcbdraft.model.config import ModelConfig, load_model_config

__all__ = ("write_hermes_config",)


def write_hermes_config(model: ModelConfig | None = None) -> Path:
    """Write Hermes ``config.yaml`` from the active PCBDraft model service.

    With no configured provider the file selects ``provider: auto`` so the
    interactive terminal can still start and guide the user through /connect.
    Returns the config path.
    """

    model = model if model is not None else load_model_config()
    provider = model.active
    config_path = hermes_home() / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if provider is None:
        config_path.write_text(
            "model:\n  provider: auto\n"
            f"plugins:\n  enabled:\n    - {DEBUG_PLUGIN_DIR_NAME}\n",
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        return config_path
    model_name = model.active_model or (provider.models[0] if provider.models else "")
    lines = [
        "model:",
        f'  default: "{model_name}"',
        "  provider: custom",
        f'  base_url: "{provider.base_url}"',
        f'  api_key: "{provider.api_key}"',
        "  allow_user_override: true",
        "",
        "display:",
        "  interface: cli",
        "",
        "platform_toolsets:",
        "  cli:",
        "    - hermes-cli",
        "    - pcbdraft",
        "",
        "plugins:",
        "  enabled:",
        f"    - {DEBUG_PLUGIN_DIR_NAME}",
        "",
        "tools:",
        "  tool_search:",
        "    enabled: off",
        "",
    ]
    config_path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(config_path, 0o600)
    return config_path
