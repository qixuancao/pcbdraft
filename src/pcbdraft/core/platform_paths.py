"""Small dependency-free helpers for per-user application paths."""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from pathlib import Path


def user_config_home(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environment = os.environ if environment is None else environment
    current = (system or platform.system()).casefold()
    user_home = home or Path.home()
    if current == "windows":
        value = environment.get("APPDATA", "").strip()
        return Path(value) if value else user_home / "AppData" / "Roaming"
    if current == "darwin":
        return user_home / "Library" / "Application Support"
    value = environment.get("XDG_CONFIG_HOME", "").strip()
    return Path(value).expanduser() if value else user_home / ".config"


def user_data_home(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environment = os.environ if environment is None else environment
    current = (system or platform.system()).casefold()
    user_home = home or Path.home()
    if current == "windows":
        value = environment.get("LOCALAPPDATA", "").strip()
        return Path(value) if value else user_home / "AppData" / "Local"
    if current == "darwin":
        return user_home / "Library" / "Application Support"
    value = environment.get("XDG_DATA_HOME", "").strip()
    return Path(value).expanduser() if value else user_home / ".local" / "share"
