"""``internal console`` subcommand parser."""

from __future__ import annotations

from collections.abc import Callable


def build_console_parser(subparsers, *, cmd_console: Callable) -> None:
    """Attach the safe PCBDraft Console REPL subcommand."""
    console_parser = subparsers.add_parser(
        "console",
        help="Open the safe PCBDraft command console",
        description=(
            "Open a curated PCBDraft command REPL. This is not a raw shell and "
            "does not expose the full PCBDraft CLI."
        ),
    )
    console_parser.set_defaults(func=cmd_console)
