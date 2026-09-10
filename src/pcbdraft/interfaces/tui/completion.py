"""Offline completion generation for the public PCBDraft command tree.

These are Python helpers, not a public ``completion`` subcommand. Generated
scripts can be saved and sourced by a shell integration.
"""

import argparse
from typing import Any


def _walk(parser: argparse.ArgumentParser) -> dict[str, Any]:
    flags = []
    commands = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:
                entry = _walk(action.choices[choice.dest])
                entry["help"] = _clean(choice.help or "")
                commands[choice.dest] = entry
        else:
            flags.extend(action.option_strings)
    return {"flags": flags, "subcommands": commands}


def _clean(text: str, maxlen: int = 60) -> str:
    return text.replace("'", "").replace('"', "").replace("\\", "")[:maxlen]


def _public_tree() -> dict[str, Any]:
    from pcbdraft.interfaces.cli import build_parser

    return _walk(build_parser())


def generate_bash(parser: argparse.ArgumentParser | None = None) -> str:
    # Always use the authoritative parser: an inherited caller must not advertise
    # unshipped commands by passing its old private parser.
    tree = _public_tree()
    words = " ".join([*tree["subcommands"], *tree["flags"]])
    cases = "\n".join(
        f'        {name}) words="{" ".join(info["flags"])}" ;;'
        for name, info in tree["subcommands"].items()
    )
    return f'''# PCBDraft bash completion; source this file.
_pcbdraft_completion() {{
    local cur="${{COMP_WORDS[COMP_CWORD]}}" words="{words}" token
    for token in "${{COMP_WORDS[@]:1:COMP_CWORD-1}}"; do
        case "$token" in
{cases}
        esac
    done
    COMPREPLY=($(compgen -W "$words" -- "$cur"))
}}
complete -F _pcbdraft_completion pcbdraft
'''


def generate_zsh(parser: argparse.ArgumentParser | None = None) -> str:
    tree = _public_tree()
    words = " ".join([*tree["subcommands"], *tree["flags"]])
    return (
        "#compdef pcbdraft\n"
        "_pcbdraft_completion() {\n"
        f"    local -a options=({words})\n"
        "    compadd -- $options\n}\n"
        "compdef _pcbdraft_completion pcbdraft\n"
    )


def generate_fish(parser: argparse.ArgumentParser | None = None) -> str:
    tree = _public_tree()
    lines = ["# PCBDraft fish completion; source this file."]
    for name, info in tree["subcommands"].items():
        lines.append(
            f"complete -c pcbdraft -n '__fish_use_subcommand' -a {name} -d '{info['help']}'"
        )
        for flag in info["flags"]:
            kind = "-l" if flag.startswith("--") else "-s"
            lines.append(
                f"complete -c pcbdraft -n '__fish_seen_subcommand_from {name}' "
                f"{kind} {flag.lstrip('-')}"
            )
    for flag in tree["flags"]:
        kind = "-l" if flag.startswith("--") else "-s"
        lines.append(f"complete -c pcbdraft {kind} {flag.lstrip('-')}")
    return "\n".join(lines) + "\n"
