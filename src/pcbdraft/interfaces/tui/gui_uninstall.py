"""Compatibility queries for the retired packaged desktop application.

The supported ``pcbdraft gui`` is a local web interface, not an installed
Electron application. No external application's files are discovered or removed.
"""

from pathlib import Path

from pcbdraft.core.runtime_environment import get_runtime_home
from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle


def desktop_userdata_dir() -> Path:
    return get_runtime_home() / "desktop"


def source_built_gui_artifacts(runtime_home: Path) -> list[Path]:
    return []


def packaged_gui_app_paths() -> list[Path]:
    return []


def agent_is_installed(runtime_home: Path) -> bool:
    from importlib.util import find_spec

    return find_spec("pcbdraft") is not None


def gui_is_installed(runtime_home: Path) -> bool:
    return False


def gui_install_summary(runtime_home: Path | None = None) -> dict:
    return {
        "supported": False,
        "installed": False,
        "source_artifacts": [],
        "packaged_apps": [],
        "detail": "Packaged desktop lifecycle is unsupported; use `pcbdraft gui`.",
    }


uninstall_gui = unsupported_lifecycle
