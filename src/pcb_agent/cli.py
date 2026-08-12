"""Command-line interface for pcb-agent-runtime."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import __version__
from .doctor import doctor_report
from .errors import PcbAgentError, TransactionRejected
from .workflows import run_apply, run_patch, run_review

DEFAULT_TIMEOUT = 600.0


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if timeout <= 0 or timeout > 86400:
        raise argparse.ArgumentTypeError("timeout must be > 0 and <= 86400 seconds")
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcb-agent",
        description="Evidence-driven KiCad reviewer and transactional safe patcher.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser("doctor", help="check local Codex, KiCad, and Git tools")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")

    review = subcommands.add_parser("review", help="run deterministic gates and an AI heuristic review")
    review.add_argument("PROJECT", help="KiCad project directory")
    review.add_argument("--output", metavar="DIR", help="parent directory for the new run")
    review.add_argument("--timeout", type=positive_timeout, default=DEFAULT_TIMEOUT, metavar="SEC")

    patch = subcommands.add_parser("patch", help="prepare a replace-only transaction in staging")
    patch.add_argument("PROJECT", help="KiCad project directory")
    patch.add_argument("--request", required=True, metavar="TEXT", help="requested change")
    patch.add_argument("--output", metavar="DIR", help="parent directory for the new transaction")
    patch.add_argument("--timeout", type=positive_timeout, default=DEFAULT_TIMEOUT, metavar="SEC")

    apply_parser = subcommands.add_parser("apply", help="verify and atomically apply a ready transaction")
    apply_parser.add_argument("RUN_DIR", help="ready patch transaction directory")
    return parser


def _print_doctor(report: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        for name, result in report["tools"].items():
            status = "ok" if result["available"] else "missing/failed"
            version = result.get("version") or "unknown"
            path = result.get("path") or "not found"
            print(f"{name}: {status} — {version} ({path})")
    return 0 if report["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _print_doctor(doctor_report(), args.as_json)
        if args.command == "review":
            run_dir = run_review(
                args.PROJECT,
                output_parent=args.output,
                timeout=args.timeout,
            )
            print(f"review complete: {run_dir}")
            return 0
        if args.command == "patch":
            run_dir = run_patch(
                args.PROJECT,
                request=args.request,
                output_parent=args.output,
                timeout=args.timeout,
            )
            print(f"transaction ready: {run_dir}")
            return 0
        if args.command == "apply":
            run_dir = run_apply(args.RUN_DIR)
            print(f"transaction applied: {run_dir}")
            return 0
    except TransactionRejected as exc:
        print(f"pcb-agent: {exc}", file=sys.stderr)
        return exc.exit_code
    except PcbAgentError as exc:
        print(f"pcb-agent: {exc}", file=sys.stderr)
        return exc.exit_code
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
