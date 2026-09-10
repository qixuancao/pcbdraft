"""Retired Windows messaging-service backend.

No Scheduled Tasks, Startup entries, or unrelated application processes are
inspected or changed. Paths are retained only for native diagnostic callers.
"""

from pathlib import Path

from pcbdraft.core.runtime_environment import get_runtime_home
from pcbdraft.interfaces.tui.gateway import _profile_suffix
from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle


def get_task_name() -> str:
    return f"PCBDraftGateway{_profile_suffix()}"


def get_task_script_path() -> Path:
    return get_runtime_home() / "services" / "pcbdraft-gateway.cmd"


def is_installed() -> bool:
    return False


is_task_registered = is_installed
install = unsupported_lifecycle
uninstall = unsupported_lifecycle
start = unsupported_lifecycle
stop = unsupported_lifecycle
restart = unsupported_lifecycle
status = unsupported_lifecycle
windowless_gateway_restart_spec = unsupported_lifecycle
