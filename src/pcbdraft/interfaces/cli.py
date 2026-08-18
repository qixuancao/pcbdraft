"""Command-line interface for PCBDraft."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pcbdraft import PRIMARY_CLI, PRODUCT_NAME, __version__
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.repository import configure_repository, current_repository
from pcbdraft.hermes.bridge import launch_chat as launch_hermes_chat
from pcbdraft.services.doctor import doctor_report, setup_runtime

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
            "Run without a subcommand to launch the full-screen terminal interface."
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
            "openai-compatible",
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
    return 0 if report["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    tokens = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(tokens)
    try:
        if args.command is None:
            return launch_hermes_chat()
        if args.command == "doctor":
            return _print_doctor(doctor_report(), args.as_json)
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
