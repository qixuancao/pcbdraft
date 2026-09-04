"""Private PCBDraft runtime state, independent of the installed source layout."""

from __future__ import annotations

import os
from pathlib import Path

from pcbdraft.core.platform_paths import pcbdraft_config_dir


def runtime_home() -> Path:
    """Resolve native state while preserving existing PCBDraft connections.

    The old product-specific override and directory are read for migration.
    A standalone agent's home is never consulted or modified.
    """
    explicit = os.environ.get("PCBDRAFT_RUNTIME_HOME", "").strip()
    if not explicit:
        explicit = os.environ.get("PCBDRAFT_HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = pcbdraft_config_dir()
    native = base / "runtime"
    legacy = base / "hermes"
    if not native.exists() and legacy.is_dir():
        return legacy
    return native
