"""Read-only desktop metadata; inherited Electron installation is retired."""

import os
from pathlib import Path

from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle

DESKTOP_ENTRY_NAME = "pcbdraft.desktop"


def is_supported() -> bool:
    return False


def desktop_entry_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "applications" / DESKTOP_ENTRY_NAME


def resolve_exec_command() -> str:
    """Return a real product command for metadata consumers."""
    from pcbdraft.interfaces.tui.relaunch import build_relaunch_argv

    return " ".join(
        _quote_exec_arg(arg)
        for arg in build_relaunch_argv(["gui"], preserve_inherited=False)
    )


def _quote_exec_arg(arg: str) -> str:
    # Desktop entries have field-code expansion even inside quoted arguments.
    escaped = arg.replace("%", "%%").replace("\\", "\\\\\\\\")
    for char in ('"', "`", "$"):
        escaped = escaped.replace(char, "\\\\" + char)
    return f'"{escaped}"'


def render_desktop_entry(exec_command: str, icon: str) -> str:
    return (
        "[Desktop Entry]\nType=Application\nName=PCBDraft\n"
        "Comment=Open the local PCB interface\n"
        f"Exec={exec_command}\nIcon={icon}\nTerminal=false\nCategories=Development;\n"
    )


install_desktop_entry = unsupported_lifecycle
refresh_desktop_databases = unsupported_lifecycle
