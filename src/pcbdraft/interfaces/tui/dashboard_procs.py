"""Retired desktop/dashboard process management.

The local web GUI owns its own lifecycle. Historical dashboard PID files and
command-line substrings are not authority to signal or respawn a process.
"""

from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle


def _scan_dashboard_processes(*_args, **_kwargs) -> list:
    return []


def _detect_concurrent_pcbdraft_instances(*_args, **_kwargs) -> list:
    return []


def _filter_dashboard_respawn_candidates(*_args, **_kwargs) -> list:
    return []


_kill_stale_dashboard_processes = unsupported_lifecycle
_reap_orphaned_desktop_local_serves = unsupported_lifecycle
