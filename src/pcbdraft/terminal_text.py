"""Unicode-aware terminal text helpers shared by terminal surfaces."""

from __future__ import annotations

import unicodedata


def cell_width(text: str) -> int:
    """Return terminal cells occupied by ordinary printable text."""

    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def tail_to_cell_width(text: str, width: int) -> str:
    """Keep the newest input suffix that fits a terminal row."""

    result: list[str] = []
    used = 0
    for character in reversed(text):
        character_width = cell_width(character)
        if character_width and used + character_width > width:
            break
        result.append(character)
        used += character_width
    return "".join(reversed(result))


def split_to_cell_width(text: str, width: int) -> list[str]:
    """Split text into terminal rows without separating combining marks."""

    result: list[str] = []
    current: list[str] = []
    used = 0
    for character in text:
        character_width = cell_width(character)
        if current and character_width and used + character_width > width:
            result.append("".join(current))
            current = []
            used = 0
        current.append(character)
        used += character_width
    if current:
        result.append("".join(current))
    return result or [""]
