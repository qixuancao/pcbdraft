"""Retired in-process environment replacement.

Installation repair belongs to the installer, not an imported TUI module.
"""

from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle

run_core_install = unsupported_lifecycle
repair_install = unsupported_lifecycle
