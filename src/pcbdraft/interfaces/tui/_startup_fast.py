"""Lightweight local startup queries using the canonical core runtime paths.

Importing this module is side-effect free. Path queries defer to core so native
platform defaults, explicit overrides and product-owned data migration agree.
Historical container markers are readable metadata, not permission to launch a
container or restart a service.
"""

from __future__ import annotations

import os
import sys

__all__ = [
    "active_profile_may_override_home",
    "container_mode_may_be_active",
    "is_container_startup_environment",
    "is_global_fast_version_argv",
    "is_termux_env",
    "is_termux_fast_version_argv",
    "print_fast_version_info",
    "project_root_str",
    "read_install_method",
    "read_openai_version",
    "try_fast_version",
]


def project_root_str() -> str:
    """Installed native resource root, independent of checkout layout."""
    from pcbdraft.core.resources import PACKAGE_ROOT

    return str(PACKAGE_ROOT)


def is_termux_env() -> bool:
    """Tiny Termux check for pre-import startup shortcuts."""
    prefix = os.environ.get("PREFIX", "")
    return bool(
        os.environ.get("TERMUX_VERSION")
        or "com.termux/files/usr" in prefix
        or prefix.startswith("/data/data/com.termux/")
    )


def is_termux_fast_version_argv(argv: list[str]) -> bool:
    return argv in (["--version"], ["-V"], ["version"])


def is_global_fast_version_argv(argv: list[str]) -> bool:
    return argv in (["--version"], ["-V"])


def is_container_startup_environment() -> bool:
    """True when we're already INSIDE a container (fast path is then safe)."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as handle:
            cgroup = handle.read()
    except OSError:
        return False
    return "docker" in cgroup or "podman" in cgroup or "/lxc/" in cgroup


def active_profile_may_override_home(runtime_root: str) -> bool:
    """Cheap probe: does an active non-default profile redirect PCBDRAFT_RUNTIME_HOME?"""
    active_profile = os.path.join(runtime_root, "active_profile")
    try:
        if os.path.exists(active_profile):
            with open(active_profile, encoding="utf-8") as handle:
                active = handle.read().strip()
            return bool(active and active != "default")
    except (OSError, UnicodeDecodeError):
        pass
    return False


def _resolved_home() -> str:
    from pcbdraft.core.runtime_environment import get_process_runtime_home

    return str(get_process_runtime_home())


def container_mode_may_be_active() -> bool:
    """Conservative probe for NixOS container-mode routing.

    False positives are fine (we fall through to the slow path, whose
    ``get_container_exec_info()`` does the authoritative check and routes
    into the container). False negatives are NOT fine — they'd print the
    host's version instead of the container's. Hence: any profile
    ambiguity → assume container mode may be active.
    """
    if os.environ.get("PCBDRAFT_RUNTIME_DEV") == "1":
        return False
    if is_container_startup_environment():
        return False

    runtime_home = os.environ.get("PCBDRAFT_RUNTIME_HOME", "").strip()
    if runtime_home:
        if os.path.exists(os.path.join(runtime_home, ".container-mode")):
            return True
        parent_name = os.path.basename(os.path.dirname(os.path.normpath(runtime_home)))
        return parent_name != "profiles" and active_profile_may_override_home(
            runtime_home
        )

    default_home = _resolved_home()
    if active_profile_may_override_home(default_home):
        return True
    return os.path.exists(os.path.join(default_home, ".container-mode"))


def read_openai_version() -> str | None:
    """Read OpenAI SDK version without importing ``importlib.metadata``."""
    for base in sys.path:
        if not base:
            base = os.getcwd()
        version_file = os.path.join(base, "openai", "_version.py")
        try:
            with open(version_file, encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped.startswith("__version__"):
                        continue
                    _key, _sep, value = stripped.partition("=")
                    value = value.split("#", 1)[0].strip().strip("\"'")
                    return value or None
        except OSError:
            continue
    return None


def read_install_method() -> str | None:
    """Read the installer's ``.install_method`` stamp, if present.

    Only the stamp (step 1 of ``config.detect_install_method``'s resolution
    order) — the managed/git/pip fallbacks need heavier imports and stay on
    the slow path. On the fast path home ambiguity is already excluded:
    ``container_mode_may_be_active()`` bails to the slow path whenever a
    non-default profile might redirect PCBDRAFT_RUNTIME_HOME.
    """
    stamp = os.path.join(_resolved_home(), ".install_method")
    try:
        with open(stamp, encoding="utf-8") as handle:
            method = handle.read().strip().lower()
        return method or None
    except OSError:
        return None


def print_fast_version_info() -> None:
    from pcbdraft.interfaces.tui import __release_date__, __version__

    print(f"PCBDraft v{__version__} ({__release_date__})")
    print(f"Install directory: {project_root_str()}")
    install_method = read_install_method()
    if install_method:
        print(f"Install method: {install_method}")

    print(f"Python: {sys.version.split()[0]}")

    openai_version = read_openai_version()
    print(
        f"OpenAI SDK: {openai_version}"
        if openai_version
        else "OpenAI SDK: Not installed"
    )
    print("Run 'pcbdraft doctor' for local runtime diagnostics.")


def try_fast_version(argv: list[str] | None = None) -> bool:
    """Handle an internal version request using local metadata only.

    Termux keeps its historical contract (also accepts the ``version``
    subcommand + the PCBDRAFT_RUNTIME_TERMUX_DISABLE_FAST_CLI escape hatch). Everywhere
    else: only ``--version``/``-V`` (the ``version`` subcommand stays on the
    slow path for full output incl. update check), and never when container
    mode may need to route the command into the container.
    """
    if argv is None:
        argv = sys.argv[1:]
    is_termux = is_termux_env()
    if is_termux and os.environ.get("PCBDRAFT_RUNTIME_TERMUX_DISABLE_FAST_CLI") == "1":
        return False
    if is_termux:
        if not is_termux_fast_version_argv(argv):
            return False
    elif not is_global_fast_version_argv(argv) or container_mode_may_be_active():
        return False

    print_fast_version_info()
    return True
