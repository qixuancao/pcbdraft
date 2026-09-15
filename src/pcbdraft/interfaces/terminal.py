"""Terminal startup and model-wizard handoff for the native PCBDraft TUI."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections.abc import Mapping
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
_PROJECT_SESSION_SCAN_LIMIT = 50


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
    """Return project IDs proven by structured PCB tool results."""

    project_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        tool_name = str(message.get("tool_name") or payload.get("tool") or "")
        if not tool_name.startswith("pcb_"):
            continue
        project_id = payload.get("project_id")
        if isinstance(project_id, str) and project_id:
            project_ids.add(project_id)
    return project_ids


def _validated_project_session_id(cli: Any, project_id: str) -> str | None:
    """Find one resumable CLI session whose PCB evidence belongs only to a project."""

    session_db = getattr(cli, "_session_db", None)
    if session_db is None or not project_id:
        return None

    receipt_candidate_ids: list[str] = []
    try:
        from pcbdraft.agent.tool_bindings import get_service
        from pcbdraft.core.io import load_json_limited
        from pcbdraft.services.progress import ProductSessionTerminalReceipt

        project_root = get_service(recover_interrupted=False).project_root(project_id)
        receipt_root = project_root / "product-sessions"
        receipts: list[tuple[str, str]] = []
        if receipt_root.is_dir() and not receipt_root.is_symlink():
            for path in receipt_root.iterdir():
                if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                    continue
                try:
                    receipt = ProductSessionTerminalReceipt.from_dict(
                        load_json_limited(path, 1024 * 1024)
                    )
                except Exception as exc:  # noqa: BLE001 - skip one malformed receipt
                    _LOGGER.debug(
                        "Ignoring invalid product-session receipt %s: %s", path, exc
                    )
                    continue
                if receipt.project_id == project_id:
                    receipts.append((receipt.created_at, receipt.session_id))
        receipt_candidate_ids.extend(
            session_id for _created_at, session_id in sorted(receipts, reverse=True)
        )
    except Exception:
        _LOGGER.debug(
            "Could not read product-session receipts for project %s",
            project_id,
            exc_info=True,
        )

    # Older projects may predate product-session terminal receipts. Search a
    # bounded recent window, then accept only exact structured PCB tool output.
    try:
        recent = session_db.list_sessions_rich(
            source="cli",
            limit=_PROJECT_SESSION_SCAN_LIMIT,
            min_message_count=1,
            order_by_last_active=True,
            compact_rows=True,
        )
        recent_candidate_ids = [str(row.get("id") or "") for row in recent]
    except Exception:
        _LOGGER.debug("Could not list legacy project sessions", exc_info=True)
        recent_candidate_ids = []

    seen: set[str] = set()
    # Recent sessions come first so a valid follow-up chat without a terminal
    # receipt is not hidden behind an older completed product turn. Receipts
    # remain the unbounded fallback for projects older than the recent window.
    for candidate_id in recent_candidate_ids + receipt_candidate_ids:
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        try:
            metadata = session_db.get_session(candidate_id)
            if not metadata or metadata.get("source") != "cli":
                continue
            resolved_id = (
                session_db.resolve_resume_session_id(candidate_id) or candidate_id
            )
            safety_check = getattr(session_db, "assert_resume_safe", None)
            if callable(safety_check):
                safety_check(resolved_id)
            model_history, display_history = session_db.get_resume_conversations(
                resolved_id
            )
        except Exception as exc:  # noqa: BLE001 - one bad session must not block fallback
            _LOGGER.debug("Ignoring unusable project session %s: %s", candidate_id, exc)
            continue
        if model_history and _project_ids_from_session_history(display_history) == {
            project_id
        }:
            return resolved_id
    return None


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
