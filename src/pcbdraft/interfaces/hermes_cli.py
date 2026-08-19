"""Boot and launch the vendored Hermes terminal as the PCBDraft CLI.

This module owns the interactive launch boundary only.  Path resolution lives
in :mod:`pcbdraft.core.hermes_paths`, model-config translation in
:mod:`pcbdraft.model.hermes_config`, the persona in :mod:`pcbdraft.agent.persona`,
tool registration in :mod:`pcbdraft.agent.hermes_tools`, the debug observer in
:mod:`pcbdraft.interfaces.hermes_plugin`, and the slash-command surface in
:mod:`pcbdraft.interfaces.commands`.

:func:`activate` inserts the vendored runtime at the front of ``sys.path``,
points ``HERMES_HOME`` at the PCBDraft-owned config directory, writes the
derived Hermes config and persona, prunes the command registry, registers the
PCB tools, and installs the debug plugin.  :func:`launch_cli` then patches
``HermesCLI.process_command`` so the PCBDraft slash commands dispatch to
:mod:`pcbdraft.interfaces.commands` handlers before Hermes sees them, and runs
the Hermes ``prompt_toolkit`` REPL.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path
from typing import Any

from pcbdraft.agent.persona import write_soul
from pcbdraft.core.hermes_paths import (
    DEBUG_PLUGIN_DIR_NAME,
    hermes_home,
    install_vendor_path,
)
from pcbdraft.interfaces.commands import apply_command_surface
from pcbdraft.model.config import ModelConfig
from pcbdraft.model.hermes_config import write_hermes_config

__all__ = (
    "DEBUG_PLUGIN_DIR_NAME",
    "activate",
    "install_debug_plugin",
    "launch_cli",
    "register_pcb_tools",
)

_PLUGIN_MANIFEST = """\
name: pcbdraft-debug
version: "1.0"
description: Record every PCBDraft agent conversation step to a debug trace.
author: PCBDraft contributors
kind: standalone
provides_hooks:
  - on_session_start
  - on_session_end
  - pre_api_request
  - post_api_request
  - api_request_error
  - pre_tool_call
  - post_tool_call
  - post_llm_call
"""

_PLUGIN_INIT = """\
\"\"\"Installed shim that loads the PCBDraft debug trace plugin body.\"\"\"

from pcbdraft.interfaces.hermes_plugin import register  # noqa: F401
"""


def register_pcb_tools() -> None:
    """Register the existing PCBDraft PCB tools into the Hermes registry."""

    from pcbdraft.agent.hermes_tools import register_all_pcb_tools

    register_all_pcb_tools()


def install_debug_plugin() -> Path:
    """Install the PCBDraft debug trace plugin into the Hermes home.

    Writes ``plugins/pcbdraft-debug/`` (manifest + loader shim) next to the
    Hermes config so the vendored runtime's standard plugin discovery loads
    it on every chat session.  Idempotent: files whose content already
    matches are left untouched.
    """

    plugin_dir = hermes_home() / "plugins" / DEBUG_PLUGIN_DIR_NAME
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = plugin_dir / "plugin.yaml"
    init_path = plugin_dir / "__init__.py"
    if (
        not manifest_path.exists()
        or manifest_path.read_text(encoding="utf-8") != _PLUGIN_MANIFEST
    ):
        manifest_path.write_text(_PLUGIN_MANIFEST, encoding="utf-8")
    if not init_path.exists() or init_path.read_text(encoding="utf-8") != _PLUGIN_INIT:
        init_path.write_text(_PLUGIN_INIT, encoding="utf-8")
    return plugin_dir


def activate(*, model: ModelConfig | None = None) -> None:
    """Prepare the vendored Hermes runtime for one PCBDraft process.

    Safe to call repeatedly: vendor-path insertion, config and persona writes,
    the command-surface rebuild, and tool registration are idempotent.
    """

    install_vendor_path()
    home = hermes_home()
    if not os.environ.get("HERMES_HOME"):
        os.environ["HERMES_HOME"] = str(home)
    home.mkdir(parents=True, exist_ok=True)
    # The vendored trim omits the messaging gateway; install no-op stubs for
    # the ``gateway.*`` modules Hermes tool code imports lazily at runtime.
    import gateway as _gateway_stub

    _gateway_stub.install()
    write_hermes_config(model)
    write_soul()
    # Prune the vendored command registry to the PCBDraft surface before any
    # help/autocomplete consumer is built (vendor files stay untouched).
    apply_command_surface()
    register_pcb_tools()
    install_debug_plugin()
    # Trigger Hermes built-in tool discovery (imports tools/*.py) so the
    # agent's schema includes both Hermes tools and the PCB tools.
    import model_tools  # noqa: F401


def _apply_process_command_patch() -> None:
    """Route PCBDraft slash commands through the command handlers.

    Hermes' plugin API cannot deliver ``/new`` and ``/open`` (built-in name
    conflicts) and the CLI help never lists plugin commands, so PCBDraft
    patches ``HermesCLI.process_command`` instead: the four repository
    commands plus the PCB workflow commands are dispatched to
    :mod:`pcbdraft.interfaces.commands` handlers; everything else — including
    the retained Hermes built-ins — falls through to the original method.
    Idempotent per process.
    """

    import cli as hermes_cli_module

    from pcbdraft.core.errors import PCBDraftError
    from pcbdraft.interfaces.commands import HANDLERS

    target = hermes_cli_module.HermesCLI
    if getattr(target, "_pcbdraft_command_patched", False):
        return
    original = target.process_command

    @functools.wraps(original)
    def _pcbdraft_process_command(self: Any, command: str) -> bool:
        tokens = command.strip().split(None, 1)
        base = tokens[0].lstrip("/").casefold() if tokens else ""
        handler = HANDLERS.get(base)
        if handler is None:
            return original(self, command)
        raw_args = tokens[1].strip() if len(tokens) > 1 else ""
        try:
            result = handler(raw_args)
        except PCBDraftError as exc:
            hermes_cli_module._cprint(f"\033[1;31m✗ {exc}{hermes_cli_module._RST}")
            return True
        if result:
            hermes_cli_module._cprint(result)
        return True

    target.process_command = _pcbdraft_process_command  # type: ignore[method-assign]
    target._pcbdraft_command_patched = True


def launch_cli(argv: list[str] | None = None) -> int:
    """Launch the Hermes interactive terminal (prompt_toolkit REPL) as PCBDraft.

    ``argv`` excludes the program name and defaults to the current process
    arguments.  Returns the process exit code.
    """

    activate()
    import hermes_cli.main as hermes_main

    _apply_process_command_patch()
    tokens = list(argv) if argv is not None else sys.argv[1:]
    previous = list(sys.argv)
    sys.argv = ["pcbdraft"] + tokens
    try:
        hermes_main.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = previous
    return 0
