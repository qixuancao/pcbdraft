"""Terminal startup and model-wizard handoff for the native PCBDraft TUI."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from typing import Any

from pcbdraft.agent.permissions import PermissionMode
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.services.provider_connection import (
    ConnectionOptions,
    connect,
    connection_status,
    format_connection_status,
)

_LOGGER = logging.getLogger(__name__)
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
    """Return whether the current PCBDraft exit is a wizard handoff."""

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


def _project_ids_from_session_history(messages: list[dict[str, Any]]) -> set[str]:
    """Compatibility import for terminal-focused callers and tests."""

    from pcbdraft.services.project_history import project_ids_from_history

    return project_ids_from_history(messages)


def _validated_project_session_id(cli: Any, project_id: str) -> str | None:
    """Find one resumable CLI session whose PCB evidence belongs only to a project."""

    session_db = getattr(cli, "_session_db", None)
    from pcbdraft.agent.tool_bindings import get_service
    from pcbdraft.services.project_history import find_project_session_id

    return find_project_session_id(
        session_db, get_service(recover_interrupted=False), project_id
    )


def _resume_project_conversation(cli: Any, project_id: str) -> bool:
    """Resume and display the complete TUI session associated with a PCB project."""

    target_id = _validated_project_session_id(cli, project_id)
    if not target_id:
        return False
    if target_id == getattr(cli, "session_id", None):
        if getattr(cli, "conversation_history", None):
            return True
        cli._resumed = True
        if cli._preload_resumed_session():
            cli._display_resumed_history(force=True)
            return True
        return False
    previous_id = getattr(cli, "session_id", None)
    cli._handle_resume_command(f"/resume {target_id}", force_display=True)
    return getattr(cli, "session_id", None) == target_id and target_id != previous_id


def _rotate_project_conversation(cli: Any, project_id: str | None = None) -> None:
    """Resume a project's prior chat, or start a fresh isolated conversation."""

    if project_id and _resume_project_conversation(cli, project_id):
        return

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
                    "PCBDraft transcript flush failed before project rotation",
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


def register_pcb_tools(*, permission_mode: PermissionMode = "workspace") -> None:
    from pcbdraft.agent.tool_bindings import register_all_pcb_tools

    register_all_pcb_tools(permission_mode=permission_mode)


def activate(*, permission_mode: PermissionMode = "workspace") -> None:
    """Initialize private settings, native tools, and lifecycle contracts."""
    from pcbdraft.agent.conversations import initialize_runtime

    initialize_runtime(permission_mode=permission_mode)


def _launch_once(argv: list[str], model_turn_limit: int | None) -> int:
    parser = argparse.ArgumentParser(prog="pcbdraft")
    parser.add_argument("--oneshot", "-z")
    parser.add_argument("--query", "-q")
    parser.add_argument("--usage-file")
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--resume")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--max-turns", type=int, default=90)
    args = parser.parse_args(argv)
    limit = model_turn_limit if model_turn_limit is not None else args.max_turns
    if isinstance(limit, bool) or not 1 <= limit <= 100_000:
        raise ValidationError("model-turn limit is invalid")
    if args.oneshot is not None or args.query is not None:
        from pcbdraft.interfaces.tui.oneshot import run_oneshot

        return run_oneshot(
            args.oneshot if args.oneshot is not None else args.query,
            model=args.model,
            provider=args.provider,
            toolsets=["pcbdraft"],
            usage_file=args.usage_file,
            max_iterations=limit,
        )
    from pcbdraft.interfaces.tui.app import main

    main(
        model=args.model,
        provider=args.provider,
        toolsets="pcbdraft",
        resume=args.resume,
        compact=args.compact,
        max_turns=limit,
    )
    return 0


def launch_cli(
    argv: list[str] | None = None,
    *,
    permission_mode: PermissionMode = "workspace",
    model_turn_limit: int | None = None,
) -> int:
    """Run the native terminal; the connection wizard owns the main thread."""
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
    tokens = list(argv) if argv is not None else []
    try:
        while True:
            exit_code = 0
            try:
                exit_code = _launch_once(tokens, model_turn_limit)
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
                from pcbdraft.agent.tool_bindings import refresh_service_provider

                refresh_service_provider()
            print(format_connection_status(status))
            print("Returning to the PCBDraft terminal.")
    finally:
        _take_deferred_connection()
