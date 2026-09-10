"""Run project recipe verification: ``python -m pcbdraft.agent.verify``."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--detect-only", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument(
        "--phase", action="append", choices=("bootstrap", "build", "test", "start")
    )
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--ready-timeout", type=float, default=30)
    parser.add_argument("--skip-start", action="store_true")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    from pcbdraft.interfaces.tui.verify_cmd import run_verify_command

    return run_verify_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
