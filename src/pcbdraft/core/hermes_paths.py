"""Path resolution for the vendored Hermes runtime and its PCBDraft home.

The Hermes checkout under ``vendor/hermes`` is deliberately kept as a flat,
top-level-import layout (``cli.py``, ``run_agent.py``, ``agent/``,
``tools/``, ``hermes_cli/``, ...) so that its internal imports work unchanged.
This module owns where that runtime lives in the three supported situations:

* a source checkout (editable install): ``<repo>/vendor/hermes``;
* an installed wheel: the build copied the trimmed runtime into
  ``pcbdraft/data/vendor/hermes`` inside the distribution;
* an explicit override via ``PCBDRAFT_HERMES_DIR`` (tests and advanced users).

It also owns the PCBDraft-owned Hermes home (``HERMES_HOME``), which stays
under the PCBDraft configuration directory — never inside the PCB project
repository.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pcbdraft.core.platform_paths import pcbdraft_config_dir

__all__ = (
    "DEBUG_PLUGIN_DIR_NAME",
    "VENDOR_MARKER",
    "hermes_home",
    "hermes_vendor_dir",
    "install_vendor_path",
)

#: Directory name of the PCBDraft debug trace plugin under the Hermes home.
DEBUG_PLUGIN_DIR_NAME = "pcbdraft-debug"

#: Marker file proving a candidate directory is the Hermes runtime.
VENDOR_MARKER = "cli.py"


def hermes_vendor_dir() -> Path:
    """Return the vendored Hermes runtime directory.

    Resolution order: ``PCBDRAFT_HERMES_DIR`` env override, the wheel data
    directory (``pcbdraft/data/vendor/hermes``), then the source checkout's
    ``vendor/hermes`` next to the repository root.  Raises ``RuntimeError``
    with an actionable message when no candidate contains the Hermes CLI.
    """

    explicit = os.environ.get("PCBDRAFT_HERMES_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[1] / "data" / "vendor" / "hermes",
        here.parents[3] / "vendor" / "hermes",
    ):
        if (candidate / VENDOR_MARKER).is_file():
            return candidate
    raise RuntimeError(
        "vendored Hermes runtime not found; expected vendor/hermes next to "
        "the pcbdraft package (or pcbdraft/data/vendor/hermes in an installed "
        "wheel) or set PCBDRAFT_HERMES_DIR"
    )


def hermes_home() -> Path:
    """Return the PCBDraft-owned Hermes home directory.

    ``HERMES_HOME`` wins when set; otherwise the home lives under the
    PCBDraft configuration directory (honoring ``PCBDRAFT_CONFIG``) so Hermes
    state stays separate from the PCB project repository.
    """

    explicit = os.environ.get("HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return pcbdraft_config_dir() / "hermes"


def install_vendor_path() -> Path:
    """Insert the vendored Hermes runtime at the front of ``sys.path``.

    Idempotent: a path already on ``sys.path`` is not re-inserted.  Returns
    the resolved runtime directory.
    """

    vendor = hermes_vendor_dir()
    value = str(vendor)
    if value not in sys.path:
        sys.path.insert(0, value)
    return vendor
