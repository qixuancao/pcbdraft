"""Capability boundary for inherited background services.

PCBDraft has no public gateway/service lifecycle. Profile data operations can
query this capability without probing the host or registering a service.
"""

import re
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle

ServiceManagerKind = Literal["systemd", "launchd", "windows", "s6", "none"]
S6_DYNAMIC_SCANDIR = Path("/run/service")
S6_SERVICE_PREFIX = "pcbdraft-gateway-"


def validate_profile_name(name: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("profile name must match [a-z0-9][a-z0-9_-]{0,63}")


@runtime_checkable
class ServiceManager(Protocol):
    kind: ServiceManagerKind

    def start(self, name: str) -> None: ...
    def stop(self, name: str) -> None: ...
    def restart(self, name: str) -> None: ...
    def is_running(self, name: str) -> bool: ...
    def supports_runtime_registration(self) -> bool: ...
    def register_profile_gateway(self, profile: str, **kwargs) -> None: ...
    def unregister_profile_gateway(self, profile: str) -> None: ...
    def list_profile_gateways(self) -> list[str]: ...


def detect_service_manager() -> ServiceManagerKind:
    return "none"


def _s6_running() -> bool:
    return False


class UnsupportedServiceManager:
    kind: ServiceManagerKind = "none"

    def __init__(self, scandir: Path = S6_DYNAMIC_SCANDIR) -> None:
        self.scandir = scandir

    def supports_runtime_registration(self) -> bool:
        return False

    def is_running(self, name: str) -> bool:
        return False

    def list_profile_gateways(self) -> list[str]:
        return []

    start = unsupported_lifecycle
    stop = unsupported_lifecycle
    restart = unsupported_lifecycle
    install = unsupported_lifecycle
    register_profile_gateway = unsupported_lifecycle
    unregister_profile_gateway = unsupported_lifecycle


SystemdServiceManager = UnsupportedServiceManager
LaunchdServiceManager = UnsupportedServiceManager
WindowsServiceManager = UnsupportedServiceManager
S6ServiceManager = UnsupportedServiceManager


def get_service_manager() -> ServiceManager:
    return UnsupportedServiceManager()
