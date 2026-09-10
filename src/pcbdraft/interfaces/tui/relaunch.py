"""Relaunch through the actual PCBDraft public entry point."""

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path


def resolve_pcbdraft_bin() -> str | None:
    """Resolve only the product launcher, never pytest or an unrelated argv[0]."""
    argv0 = sys.argv[0]
    if Path(argv0).name.lower() in {"pcbdraft", "pcbdraft.exe"}:
        candidate = os.path.abspath(argv0)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("pcbdraft")


def _build_inherited_flag_table() -> list[tuple[str, bool]]:
    from pcbdraft.interfaces.cli import build_parser

    return [
        (option, action.nargs != 0)
        for action in build_parser()._actions
        if action.dest not in {"help", "version", "command"}
        for option in action.option_strings
    ]


def _extract_inherited_flags(argv: Sequence[str]) -> list[str]:
    table = dict(_build_inherited_flag_table())
    flags: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--" or not arg.startswith("-"):
            break  # flags of a subcommand do not become root options
        key, sep, _value = arg.partition("=")
        if key in table:
            if sep or not table[key]:
                flags.append(arg)
            elif index + 1 < len(argv):
                flags.extend((arg, argv[index + 1]))
                index += 1
        index += 1
    return flags


def build_relaunch_argv(
    extra_args: Sequence[str],
    *,
    preserve_inherited: bool = True,
    original_argv: Sequence[str] | None = None,
) -> list[str]:
    from pcbdraft.interfaces.cli import build_parser

    src = sys.argv[1:] if original_argv is None else original_argv
    tokens = _extract_inherited_flags(src) if preserve_inherited else []
    tokens.extend(extra_args)
    # Reject old chat/serve/desktop/profile flags before exec/spawn. There is no
    # valid public translation for a conversation ID or old backend protocol.
    build_parser().parse_args(tokens)
    launcher = resolve_pcbdraft_bin()
    prefix = [launcher] if launcher else [sys.executable, "-m", "pcbdraft"]
    return [*prefix, *tokens]


def relaunch(
    extra_args: Sequence[str],
    *,
    preserve_inherited: bool = True,
    original_argv: Sequence[str] | None = None,
) -> None:
    argv = build_relaunch_argv(
        extra_args,
        preserve_inherited=preserve_inherited,
        original_argv=original_argv,
    )
    if sys.platform == "win32":
        import subprocess

        try:
            # Product executable and arguments were validated above.
            raise SystemExit(subprocess.run(argv, check=False).returncode)  # noqa: S603
        except KeyboardInterrupt:
            raise SystemExit(130) from None
    os.execvp(argv[0], argv)  # noqa: S606 — validated native product relaunch
