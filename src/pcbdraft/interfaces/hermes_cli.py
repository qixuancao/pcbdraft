"""Boot and launch the vendored Hermes terminal as the PCBDraft CLI.

This module owns the interactive launch boundary only.  Path resolution lives
in :mod:`pcbdraft.core.hermes_paths`, model-config translation in
:mod:`pcbdraft.model.hermes_config`, the persona in :mod:`pcbdraft.agent.persona`,
tool registration in :mod:`pcbdraft.agent.hermes_tools`, the debug observer in
:mod:`pcbdraft.interfaces.hermes_plugin`, and the slash-command surface in
:mod:`pcbdraft.interfaces.commands`.

:func:`activate` inserts the vendored runtime at the front of ``sys.path``,
points ``HERMES_HOME`` at the PCBDraft-owned config directory, merges the
PCBDraft defaults and persona, prunes the command registry, registers the
PCB tools, and installs the debug plugin.  :func:`launch_cli` then patches
``HermesCLI.process_command`` so the PCBDraft slash commands dispatch to
:mod:`pcbdraft.interfaces.commands` handlers before Hermes sees them, and runs
the Hermes ``prompt_toolkit`` REPL.
"""

from __future__ import annotations

import functools
import logging
import sys
import threading
from pathlib import Path
from typing import Any

from pcbdraft.agent.permissions import PermissionMode
from pcbdraft.agent.persona import write_soul
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.hermes_paths import (
    DEBUG_PLUGIN_DIR_NAME,
    hermes_home,
)
from pcbdraft.interfaces.commands import apply_command_surface
from pcbdraft.model.hermes_config import write_hermes_config
from pcbdraft.services.provider_connection import (
    ConnectionOptions,
    activate_provider_runtime,
    connect,
    connection_status,
    format_connection_status,
)

__all__ = (
    "DEBUG_PLUGIN_DIR_NAME",
    "activate",
    "install_debug_plugin",
    "launch_cli",
    "register_pcb_tools",
)

_LOGGER = logging.getLogger(__name__)

_PLUGIN_MANIFEST = """\
name: pcbdraft-debug
version: "1.0"
description: Record every PCBDraft agent conversation step to a debug trace.
author: PCBDraft contributors
kind: standalone
provides_hooks:
  - on_session_start
  - on_session_end
  - on_session_finalize
  - on_session_reset
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

_deferred_connection_options: ConnectionOptions | None = None
_deferred_connection_lock = threading.Lock()


def _defer_connection(options: ConnectionOptions) -> None:
    """Record one wizard request for the outer main-thread launch loop."""

    global _deferred_connection_options
    with _deferred_connection_lock:
        if _deferred_connection_options is None:
            _deferred_connection_options = options


def _take_deferred_connection() -> ConnectionOptions | None:
    """Consume the pending wizard request, if the exiting REPL left one."""

    global _deferred_connection_options
    with _deferred_connection_lock:
        options = _deferred_connection_options
        _deferred_connection_options = None
    return options


def _has_deferred_connection() -> bool:
    """Return whether the current Hermes exit is a wizard handoff."""

    with _deferred_connection_lock:
        return _deferred_connection_options is not None


def _slash_connection_options(raw_args: str) -> ConnectionOptions:
    tokens = {token.casefold() for token in raw_args.split()}
    return ConnectionOptions(
        no_browser="--no-browser" in tokens,
        refresh="--refresh" in tokens,
        reauthenticate=bool(
            tokens & {"reauthenticate", "--reauthenticate", "--reauth"}
        ),
    )


def register_pcb_tools(*, permission_mode: PermissionMode = "workspace") -> None:
    """Register the existing PCBDraft PCB tools into the Hermes registry."""

    from pcbdraft.agent.hermes_tools import register_all_pcb_tools

    register_all_pcb_tools(permission_mode=permission_mode)


def install_debug_plugin() -> Path:
    """Install the PCBDraft debug trace plugin into the Hermes home.

    Writes ``plugins/pcbdraft-debug/`` (manifest + loader shim) next to the
    Hermes config so the vendored runtime's standard plugin discovery loads
    it on every chat session.  Idempotent: files whose content already
    matches are left untouched.
    """

    plugin_dir = hermes_home() / "plugins" / DEBUG_PLUGIN_DIR_NAME
    plugin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    plugin_dir.chmod(0o700)
    manifest_path = plugin_dir / "plugin.yaml"
    init_path = plugin_dir / "__init__.py"
    if (
        not manifest_path.exists()
        or manifest_path.read_text(encoding="utf-8") != _PLUGIN_MANIFEST
    ):
        manifest_path.write_text(_PLUGIN_MANIFEST, encoding="utf-8")
    if not init_path.exists() or init_path.read_text(encoding="utf-8") != _PLUGIN_INIT:
        init_path.write_text(_PLUGIN_INIT, encoding="utf-8")
    manifest_path.chmod(0o600)
    init_path.chmod(0o600)
    return plugin_dir


def activate(*, permission_mode: PermissionMode = "workspace") -> None:
    """Prepare the vendored Hermes runtime for one PCBDraft process.

    Safe to call repeatedly: vendor-path insertion, config and persona writes,
    the command-surface rebuild, and tool registration are idempotent.
    """

    activate_provider_runtime()
    # The vendored trim omits the messaging gateway; install no-op stubs for
    # the ``gateway.*`` modules Hermes tool code imports lazily at runtime.
    import gateway as _gateway_stub

    _gateway_stub.install()
    write_hermes_config()
    write_soul()
    # Prune the vendored command registry to the PCBDraft surface before any
    # help/autocomplete consumer is built (vendor files stay untouched).
    apply_command_surface()
    register_pcb_tools(permission_mode=permission_mode)
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

    from pcbdraft.interfaces.commands import HANDLERS

    target = hermes_cli_module.HermesCLI
    if getattr(target, "_pcbdraft_command_patched", False):
        return
    original = target.process_command

    def _rotate_project_conversation(cli: Any) -> None:
        """Start a fresh Hermes conversation after a trusted project boundary."""

        rotate = getattr(cli, "new_session", None)
        if not callable(rotate):
            # Lightweight adapter tests may call the patched method on a stub.
            return
        prior_history = getattr(cli, "conversation_history", None)
        if isinstance(prior_history, list):
            agent = getattr(cli, "agent", None)
            flush = getattr(agent, "_flush_messages_to_session_db", None)
            if callable(flush) and prior_history:
                try:
                    flush(prior_history, conversation_history=prior_history)
                except Exception:
                    _LOGGER.debug(
                        "Hermes transcript flush failed before project rotation",
                        exc_info=True,
                    )
            # Do not feed project A through session-boundary memory extraction
            # where it could re-enter project B's next request.
            cli.conversation_history = []
        try:
            rotate(silent=True)
        except BaseException:
            if isinstance(prior_history, list):
                cli.conversation_history = prior_history
            raise

    @functools.wraps(original)
    def _pcbdraft_process_command(self: Any, command: str) -> bool:
        tokens = command.strip().split(None, 1)
        base = tokens[0].lstrip("/").casefold() if tokens else ""
        raw_args = tokens[1].strip() if len(tokens) > 1 else ""
        if base == "connect" or (base == "model" and raw_args in {"", "--refresh"}):
            options = (
                _slash_connection_options(raw_args)
                if base == "connect"
                else ConnectionOptions(refresh=raw_args == "--refresh")
            )
            _defer_connection(options)
            hermes_cli_module._cprint(
                "  Leaving the terminal briefly to open the model connection wizard..."
            )
            return False
        handler = HANDLERS.get(base)
        if handler is None:
            return original(self, command)
        try:
            result = handler(raw_args)
        except PCBDraftError as exc:
            hermes_cli_module._cprint(f"\033[1;31m✗ {exc}{hermes_cli_module._RST}")
            return True
        if (base in {"new", "open"} and raw_args) or (base == "project" and raw_args):
            _rotate_project_conversation(self)
        if result:
            hermes_cli_module._cprint(result)
        return True

    target.process_command = _pcbdraft_process_command  # type: ignore[method-assign]
    target._pcbdraft_command_patched = True


def _apply_connection_lifecycle_patch() -> None:
    """Keep process cleanup from destroying a wizard-bound relaunch.

    Hermes' normal ``run()`` teardown restores prompt_toolkit and closes the
    current session before calling its process-global cleanup. The latter also
    shuts shared clients and arms a force-exit watchdog, so it must run only on
    the final terminal exit, not while PCBDraft briefly hands the main thread
    to the connection wizard.
    """

    import cli as hermes_cli_module

    if getattr(hermes_cli_module, "_pcbdraft_connection_lifecycle_patched", False):
        return
    original = hermes_cli_module._run_cleanup

    @functools.wraps(original)
    def _pcbdraft_run_cleanup(*args: Any, **kwargs: Any) -> Any:
        if _has_deferred_connection():
            # prompt_toolkit has already returned. Retain Hermes' explicit
            # terminal-mode seat belt while leaving process-global resources
            # alive for the next in-process terminal instance.
            hermes_cli_module._reset_terminal_input_modes_on_exit()
            return None
        return original(*args, **kwargs)

    hermes_cli_module._run_cleanup = _pcbdraft_run_cleanup
    hermes_cli_module._pcbdraft_connection_lifecycle_patched = True


def _apply_model_persistence_patch() -> None:
    """Make every successful PCBDraft ``/model`` switch authoritative.

    Hermes normally treats explicit provider switches as session-only even
    when ``persist_switch_by_default`` is enabled.  PCBDraft has one provider
    authority, so its process-local adapter makes picker, target, and provider
    forms global and rejects Hermes' ephemeral-only flags.
    """

    import cli as hermes_cli_module
    from hermes_cli import model_switch

    target = hermes_cli_module.HermesCLI
    if getattr(target, "_pcbdraft_model_persistence_patched", False):
        return
    original = target._handle_model_switch
    original_cli_apply = target._confirm_and_apply_cli_model_switch
    original_picker_apply = target._confirm_and_apply_model_switch_result

    @functools.wraps(original)
    def _persistent_model_switch(self: Any, command: str) -> Any:
        request = model_switch.parse_model_switch_args(
            command.split(None, 1)[1] if len(command.split(None, 1)) > 1 else ""
        )
        if request.is_once or request.is_session:
            hermes_cli_module._cprint(
                "  ✗ PCBDraft keeps one persistent model; use /model without "
                "--once or --session."
            )
            return None
        return original(self, command)

    def _persist_every_switch(
        is_global: bool,
        is_session: bool,
        *,
        is_once: bool = False,
        explicit_provider: str = "",
    ) -> bool:
        del is_global, is_session, is_once, explicit_provider
        return True

    @functools.wraps(original_cli_apply)
    def _refresh_after_cli_switch(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return original_cli_apply(self, *args, **kwargs)
        finally:
            from pcbdraft.agent.hermes_tools import refresh_service_provider

            refresh_service_provider()

    @functools.wraps(original_picker_apply)
    def _refresh_after_picker_switch(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return original_picker_apply(self, *args, **kwargs)
        finally:
            from pcbdraft.agent.hermes_tools import refresh_service_provider

            refresh_service_provider()

    target._handle_model_switch = _persistent_model_switch
    target._confirm_and_apply_cli_model_switch = _refresh_after_cli_switch
    target._confirm_and_apply_model_switch_result = _refresh_after_picker_switch
    target._pcbdraft_model_persistence_patched = True
    model_switch.resolve_persist_behavior = _persist_every_switch


def launch_cli(
    argv: list[str] | None = None,
    *,
    permission_mode: PermissionMode = "workspace",
) -> int:
    """Launch the Hermes interactive terminal (prompt_toolkit REPL) as PCBDraft.

    ``argv`` excludes the program name and defaults to the current process
    arguments.  Returns the process exit code.
    """

    # A prior failed/aborted invocation in this process must never make an
    # otherwise normal terminal exit look like a new wizard request.
    _take_deferred_connection()
    activate(permission_mode=permission_mode)
    status = connection_status()
    if not status.usable:
        if not sys.stdin.isatty():
            raise PCBDraftError(
                "no usable model provider is connected; run `pcbdraft connect` "
                "from an interactive terminal"
            )
        print("A model connection is required before the PCB terminal can start.")
        status = connect()
        if status.outcome == "cancelled" or not status.usable:
            print(format_connection_status(status))
            print("Connection was not completed. Run `pcbdraft connect` to try again.")
            return 1
    import hermes_cli.main as hermes_main

    _apply_process_command_patch()
    _apply_connection_lifecycle_patch()
    _apply_model_persistence_patch()
    tokens = list(argv) if argv is not None else sys.argv[1:]
    previous = list(sys.argv)
    sys.argv = ["pcbdraft"] + tokens
    try:
        while True:
            exit_code = 0
            try:
                hermes_main.main()
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
            requested = _take_deferred_connection()
            if requested is None:
                return exit_code
            try:
                status = connect(requested)
            except PCBDraftError as exc:
                print(f"✗ {exc}", file=sys.stderr)
                print("Returning to the PCBDraft terminal.")
                continue
            if status.outcome != "cancelled":
                from pcbdraft.agent.hermes_tools import refresh_service_provider

                refresh_service_provider()
            print(format_connection_status(status))
            print("Returning to the PCBDraft terminal.")
    finally:
        _take_deferred_connection()
        sys.argv = previous
