"""Import-safe boundary for the retired messaging gateway.

PCBDraft ships no messaging service. Queries describe that product capability,
not unrelated processes on the host. Mutations explicitly fail without invoking
systemd, launchd, a process scanner, or a package installer.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from pcbdraft.core.runtime_environment import (
    get_runtime_home,
    is_container,  # noqa: F401 — retained platform-query export
    is_termux,  # noqa: F401 — retained platform-query export
    is_wsl,  # noqa: F401 — retained platform-query export
)
from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle


@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    manager: str = "unsupported"
    service_installed: bool = False
    service_running: bool = False
    gateway_pids: tuple[int, ...] = ()
    service_scope: str | None = None

    @property
    def running(self) -> bool:
        return self.service_running or bool(self.gateway_pids)

    @property
    def has_process_service_mismatch(self) -> bool:
        return self.service_installed and self.running and not self.service_running


@dataclass(frozen=True)
class ProfileGatewayProcess:
    profile: str
    path: Path
    pid: int


class UserSystemdUnavailableError(RuntimeError):
    pass


class SystemScopeRequiresRootError(RuntimeError):
    pass


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform == "win32"


def _profile_suffix() -> str:
    home = get_runtime_home()
    return f"-{home.name}" if home.parent.name == "profiles" else ""


def get_service_name() -> str:
    return f"pcbdraft-gateway{_profile_suffix()}"


def get_systemd_unit_path(system: bool = False) -> Path:
    root = (
        Path("/etc/systemd/system")
        if system
        else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "systemd"
        / "user"
    )
    return root / f"{get_service_name()}.service"


def get_launchd_plist_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "LaunchAgents"
        / f"ai.pcbdraft.gateway{_profile_suffix()}.plist"
    )


def get_gateway_runtime_snapshot(system: bool = False) -> GatewayRuntimeSnapshot:
    return GatewayRuntimeSnapshot()


def find_gateway_pids(*_args, **_kwargs) -> list[int]:
    return []


def find_profile_gateway_processes(*_args, **_kwargs) -> list[ProfileGatewayProcess]:
    return []


def _format_gateway_pids(pids, *_args, **_kwargs) -> str:
    return ", ".join(map(str, pids))


def _is_service_running(*_args, **_kwargs) -> bool:
    return False


supports_systemd_services = _is_service_running
has_conflicting_systemd_units = _is_service_running
_system_scope_wizard_would_need_root = _is_service_running
_probe_launchd_service_running = _is_service_running


def _probe_systemd_service_running(system: bool = False) -> tuple[bool, bool]:
    return False, False


def get_installed_systemd_scopes() -> list[str]:
    return []


def get_systemd_linger_status() -> tuple[None, str]:
    return None, "PCBDraft messaging services are unsupported"


def _all_platforms() -> list:
    return []


def _platform_status(*_args, **_kwargs) -> str:
    return "unsupported"


gateway_command = unsupported_lifecycle
ensure_gateway_service = unsupported_lifecycle
run_gateway = unsupported_lifecycle
systemd_install = unsupported_lifecycle
systemd_uninstall = unsupported_lifecycle
systemd_start = unsupported_lifecycle
systemd_stop = unsupported_lifecycle
systemd_restart = unsupported_lifecycle
systemd_status = unsupported_lifecycle
launchd_install = unsupported_lifecycle
launchd_uninstall = unsupported_lifecycle
launchd_start = unsupported_lifecycle
launchd_stop = unsupported_lifecycle
launchd_restart = unsupported_lifecycle
launchd_status = unsupported_lifecycle
kill_gateway_processes = unsupported_lifecycle
stop_profile_gateway = unsupported_lifecycle
_configure_platform = unsupported_lifecycle
_setup_qqbot = unsupported_lifecycle
_print_system_scope_remediation = unsupported_lifecycle
