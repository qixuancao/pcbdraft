"""Pure path resolution for PCBDraft-owned runtime state."""

from __future__ import annotations

import os
from pathlib import Path

from pcbdraft.core.platform_paths import pcbdraft_config_dir


def default_runtime_home() -> Path:
    """Return the product configuration runtime child, ignoring runtime overrides."""
    return pcbdraft_config_dir() / "runtime"


def runtime_home() -> Path:
    """Resolve the process runtime home without reading or migrating state.

    Only ``PCBDRAFT_RUNTIME_HOME`` overrides the default. Startup migration is
    explicitly performed by ``legacy_migration.migrate_legacy_runtime_home``.
    """
    explicit = os.environ.get("PCBDRAFT_RUNTIME_HOME", "").strip()
    return Path(explicit).expanduser() if explicit else default_runtime_home()
