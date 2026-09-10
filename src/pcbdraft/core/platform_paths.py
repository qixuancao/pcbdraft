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
    if current == "windows":
        value = environment.get("APPDATA", "").strip()
        if value:
            return Path(value).expanduser()
        return (home or Path.home()).expanduser() / "AppData" / "Roaming"
    if current == "darwin":
        return (home or Path.home()).expanduser() / "Library" / "Application Support"
    value = environment.get("XDG_CONFIG_HOME", "").strip()
    return (
        Path(value).expanduser()
        if value
        else (home or Path.home()).expanduser() / ".config"
    )


def pcbdraft_config_dir() -> Path:
    """Return the PCBDraft user configuration directory.

    Honors the ``PCBDRAFT_CONFIG`` env override (its parent directory is the
    config dir) so tests and advanced users can relocate every derived file
    — model config, PCBDraft runtime state, and the debug trace — together.
    """

    explicit = os.environ.get("PCBDRAFT_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser().parent
    return user_config_home() / "pcbdraft"


def user_data_home(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environment = os.environ if environment is None else environment
    current = (system or platform.system()).casefold()
    if current == "windows":
        value = environment.get("LOCALAPPDATA", "").strip()
        if value:
            return Path(value).expanduser()
        return (home or Path.home()).expanduser() / "AppData" / "Local"
    if current == "darwin":
        return (home or Path.home()).expanduser() / "Library" / "Application Support"
    value = environment.get("XDG_DATA_HOME", "").strip()
    return (
        Path(value).expanduser()
        if value
        else (home or Path.home()).expanduser() / ".local" / "share"
    )


def production_runtime_roots() -> tuple[Path, ...]:
    """Return credential/DB guard roots without opening any state files.

    Include the native platform default and any relocated product config root.
    Ignore runtime/profile overrides (tests deliberately sandbox those). Avoid
    ``Path.home`` so a test patch of that callable cannot disarm the guard.
    Callers may add their captured pre-sandbox runtime root separately.
    """
    expanded = os.path.expanduser("~")
    if expanded == "~":
        raise RuntimeError("cannot determine the PCBDraft production home")
    native = user_config_home(home=Path(expanded)) / "pcbdraft" / "runtime"
    roots = [native.resolve()]
    explicit = os.environ.get("PCBDRAFT_CONFIG", "").strip()
    if explicit:
        roots.append((Path(explicit).expanduser().parent / "runtime").resolve())
    return tuple(dict.fromkeys(roots))
