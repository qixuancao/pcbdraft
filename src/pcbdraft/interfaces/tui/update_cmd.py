"""Retired source-checkout updater.

PCBDraft installation is owned by the package installer. Importing this module
does not fetch source, rewrite Git history, replace environments, or restart
processes. Existing callers get an explicit unsupported result.
"""

from pcbdraft.core.errors import PCBDraftError


def unsupported_lifecycle(*_args, **_kwargs):
    """Reject inherited lifecycle operations before any side effects."""
    raise PCBDraftError(
        "Unsupported lifecycle operation: PCBDraft does not provide the inherited "
        "updater, uninstaller, messaging gateway, or desktop backend service. "
        "Manage installation with the installer that installed PCBDraft; "
        "run `pcbdraft --help` for supported commands."
    )


cmd_update = unsupported_lifecycle
_cmd_update_impl = unsupported_lifecycle
_cmd_update_check = unsupported_lifecycle
