"""Hermes-based autonomous PCB agent bridge.

PCBDraft no longer drives a fixed, deterministic plan->generate->validate
tool sequence.  It boots the vendored Hermes agent runtime
(``vendor/hermes``), registers the existing PCB tools into Hermes' tool
registry, and lets one agent decide which tool to call next based on the
conversation and the current project state.
"""

from __future__ import annotations

from pcbdraft.hermes.bridge import (
    activate,
    hermes_home,
    hermes_vendor_dir,
    launch_chat,
    register_pcb_tools,
    write_hermes_config,
    write_soul,
)

__all__ = (
    "activate",
    "hermes_home",
    "hermes_vendor_dir",
    "launch_chat",
    "register_pcb_tools",
    "write_hermes_config",
    "write_soul",
)