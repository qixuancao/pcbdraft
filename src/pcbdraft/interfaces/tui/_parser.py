"""Parser compatibility helpers backed by the sole public PCBDraft CLI."""

PRE_ARGPARSE_INHERITED_FLAGS: list[tuple[str, bool]] = []


def build_top_level_parser():
    """Return the public parser; no inherited chat/backend subparser exists."""
    import argparse

    from pcbdraft.interfaces.cli import build_parser

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return parser, subparsers, None
