"""Command-line interface for PCBDraft."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pcbdraft import PRIMARY_CLI, PRODUCT_NAME, __version__
from pcbdraft.core.debug_trace import trace_enabled, trace_path
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.repository import configure_repository, current_repository
from pcbdraft.interfaces.hermes_cli import launch_cli
from pcbdraft.services.doctor import doctor_report, setup_runtime
from pcbdraft.services.provider_connection import (
    ConnectionOptions,
    connect,
    format_connection_status,
)

_ROOT_OPTIONS_WITH_VALUES = frozenset(
    {"--workspace", "--provider", "--project", "--timeout", "--approval-mode"}
)


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if timeout <= 0 or timeout > 86400:
        raise argparse.ArgumentTypeError("timeout must be > 0 and <= 86400 seconds")
    return timeout


def invoked_program(argv0: str | None = None) -> str:
    """Return the supported launcher name, defaulting module use to the primary CLI."""
    name = Path(argv0 if argv0 is not None else sys.argv[0]).name
    return name if name == PRIMARY_CLI else PRIMARY_CLI


def _root_command_index(tokens: Sequence[str]) -> int:
    """Locate the subcommand while respecting values of root-level options."""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _ROOT_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _ROOT_OPTIONS_WITH_VALUES):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index
    return len(tokens)


def _option_was_supplied(tokens: Sequence[str], option: str) -> bool:
    return any(token == option or token.startswith(f"{option}=") for token in tokens)


def build_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or invoked_program(),
        description=(
            f"{PRODUCT_NAME}: generate native KiCad projects from reviewable circuit plans."
        ),
        epilog=(
            "Run without a subcommand to launch the interactive Hermes-based terminal."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--workspace", metavar="DIR", help="terminal interface workspace"
    )
    parser.add_argument(
        "--provider",
        choices=(
            "auto",
            "hermes",
        ),
        default="auto",
        help="terminal planning provider",
    )
    parser.add_argument(
        "--project", dest="project_id", metavar="ID", help="project to open"
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=420.0,
        metavar="SEC",
        help="terminal action timeout",
    )
    parser.add_argument(
        "--approval-mode",
        choices=("workspace", "review", "read_only"),
        default="workspace",
        help="terminal tool permission policy",
    )
    subcommands = parser.add_subparsers(dest="command")

    doctor = subcommands.add_parser(
        "doctor", help="check local KiCad, Git, and model configuration"
    )
    doctor.add_argument(
        "--json", action="store_true", dest="as_json", help="emit machine-readable JSON"
    )

    connection = subcommands.add_parser(
        "connect", help="connect, switch, or reauthenticate a model provider"
    )
    connection.add_argument(
        "--no-browser",
        action="store_true",
        help="print OAuth URLs without opening a browser",
    )
    connection.add_argument(
        "--timeout",
        type=positive_timeout,
        dest="connection_timeout",
        metavar="SEC",
        help="provider authentication timeout",
    )
    connection.add_argument(
        "--region", choices=("global", "china"), help="provider account region"
    )
    connection.add_argument(
        "--refresh", action="store_true", help="refresh provider model discovery"
    )
    connection.add_argument(
        "--reauthenticate",
        action="store_true",
        help="request fresh provider authentication",
    )

    setup = subcommands.add_parser(
        "setup", help="detect KiCad and initialize missing stock-library tables"
    )
    setup.add_argument(
        "--json", action="store_true", dest="as_json", help="emit machine-readable JSON"
    )

    repository = subcommands.add_parser(
        "repository",
        help="show or set the persistent PCB project repository",
        description=(
            "PCBDraft stores every normal project below one persistent repository, "
            "not below the directory where the command was launched."
        ),
    )
    repository.add_argument(
        "DIRECTORY",
        nargs="?",
        help="new repository directory; omit to show the current location",
    )
    repository.add_argument(
        "--json", action="store_true", dest="as_json", help="emit machine-readable JSON"
    )
    trace = subcommands.add_parser(
        "trace",
        help="show recent agent debug trace events",
        description=(
            "Print the most recent events from the agent debug trace "
            "(one JSON line per conversation step: model requests, model "
            "responses, tool calls, and errors)."
        ),
    )
    trace.add_argument(
        "-n",
        "--lines",
        type=int,
        default=40,
        metavar="N",
        help="number of recent events to show (default: 40)",
    )
    trace.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit raw JSONL lines instead of a summary",
    )
    return parser


def _print_doctor(report: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        for name, result in report["tools"].items():
            status = "ok" if result["available"] else "missing/failed"
            if name == "kicad-cli" and result.get("available"):
                support = result.get("support", {})
                status = "ok" if support.get("supported") else "unsupported"
            version = result.get("version") or "unknown"
            path = result.get("path") or "not found"
            print(f"{name}: {status} — {version} ({path})")
        support = report["tools"]["kicad-cli"].get("support", {})
        if support.get("supported") and not support.get("exact_tested"):
            print(f"KiCad compatibility note: {support.get('reason')}")
        tables = report.get("library_tables", {})
        if tables:
            ready = sum(bool(item.get("configured")) for item in tables.values())
            print(f"KiCad library tables: {ready}/{len(tables)} ready")
        data = report.get("library_data", {})
        if data:
            ready = sum(bool(item.get("available")) for item in data.values())
            print(f"KiCad stock-library directories: {ready}/{len(data)} ready")
        model = report.get("model", {})
        if model.get("configured"):
            readiness = "ready" if model.get("usable") else "reauthentication required"
            print(
                f"Model connection: {model.get('provider')} / {model.get('model')} "
                f"({readiness})"
            )
        else:
            print("Model connection: not configured (run `pcbdraft connect`)")
    return 0 if report["ok"] else 1


def _print_trace(lines: int, as_json: bool) -> int:
    path = trace_path()
    if not trace_enabled():
        print(
            "agent debug trace is disabled (unset PCBDRAFT_DEBUG_TRACE to enable)",
            file=sys.stderr,
        )
        return 1
    if not path.exists():
        print(f"no agent debug trace yet: {path}", file=sys.stderr)
        return 1
    try:
        recent = path.read_text(encoding="utf-8").splitlines()[-max(1, lines) :]
    except OSError as exc:
        print(f"cannot read agent debug trace: {exc}", file=sys.stderr)
        return 1
    if as_json:
        for line in recent:
            if line.strip():
                print(line)
        return 0
    for line in recent:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(line)
            continue
        summary = _trace_summary(record)
        print(
            f"#{record.get('seq', '?')} {record.get('timestamp', '')} "
            f"{record.get('event', '?')}: {summary}"
        )
    print(f"— trace file: {path}")
    return 0


def _trace_summary(record: dict) -> str:
    data = record.get("data")
    data = data if isinstance(data, dict) else {}
    event = record.get("event", "?")
    if event == "model_request":
        return (
            f"call #{data.get('api_call_count')} model={data.get('model')} "
            f"provider={data.get('provider')} messages={data.get('message_count')} "
            f"tools={data.get('tool_count')}"
        )
    if event == "model_response":
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return (
            f"call #{data.get('api_call_count')} finish={data.get('finish_reason')} "
            f"tokens={usage.get('total_tokens')} in {data.get('api_duration_seconds')}s"
        )
    if event == "model_error":
        error = data.get("error")
        error = error if isinstance(error, dict) else {}
        return (
            f"HTTP {data.get('http_status')} {error.get('type', '')} "
            f"retryable={data.get('retryable')} reason={data.get('failover_reason')}"
        )
    if event in {"tool_start", "tool_end"}:
        return f"tool={data.get('tool_name')} status={data.get('status', 'starting')}"
    if event == "turn_complete":
        reply = str(data.get("assistant_response", ""))
        preview = reply[:80] + ("..." if len(reply) > 80 else "")
        return f"reply={preview!r}"
    if event in {"session_start", "session_end"}:
        return f"session={data.get('session_id')} model={data.get('model')}"
    return json.dumps(data, ensure_ascii=False)[:160]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    tokens = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(tokens)
    try:
        if args.command is None:
            # A caller-provided workspace keeps isolated automation working
            # under the interactive surface (application home, not the
            # persistent product repository).
            if args.workspace:
                os.environ["PCBDRAFT_HOME"] = args.workspace
            return launch_cli()
        if args.command == "trace":
            return _print_trace(args.lines, args.as_json)
        if args.command == "doctor":
            return _print_doctor(doctor_report(), args.as_json)
        if args.command == "connect":
            if not sys.stdin.isatty():
                raise PCBDraftError(
                    "`pcbdraft connect` requires an interactive terminal"
                )
            status = connect(
                ConnectionOptions(
                    no_browser=args.no_browser,
                    timeout=args.connection_timeout,
                    region=args.region,
                    refresh=args.refresh,
                    reauthenticate=args.reauthenticate,
                )
            )
            print(format_connection_status(status))
            return 0 if status.usable else 1
        if args.command == "setup":
            report = setup_runtime()
            if args.as_json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                _print_doctor(report, False)
                print("KiCad runtime and stock-library tables are ready.")
            return 0
        if args.command == "repository":
            repository = (
                configure_repository(args.DIRECTORY)
                if args.DIRECTORY
                else current_repository()
            )
            value = {
                "root": str(repository.root),
                "projects_root": str(repository.projects_root),
                "source": repository.source,
                "configured_now": repository.configured_now,
            }
            _emit(
                value,
                args.as_json,
                "PCB project repository: " + str(repository.root),
            )
            return 0
    except PCBDraftError as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return exc.exit_code
    parser.error("unknown command")
    return 2


def _emit(value: dict, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print(text)
