"""Small semantic spatial vocabularies shared by planning and KiCad stages.

The planner selects bounded names such as ``top_left`` or ``center``.  Only
deterministic local code converts those names into board coordinates, keeping
raw geometry outside the model-authored circuit-plan boundary.
"""

from __future__ import annotations

import math

BOARD_REGIONS = frozenset(
    {
        "bottom",
        "bottom_left",
        "bottom_right",
        "center",
        "left",
        "right",
        "top",
        "top_left",
        "top_right",
    }
)

COPPER_LAYER_SCOPES = frozenset({"all", "back", "front", "outer"})


def board_region_bounds(
    region: str,
    board_width_mm: float,
    board_height_mm: float,
) -> tuple[float, float, float, float]:
    """Return the deterministic board-third rectangle for a named region."""

    _validate_board(board_width_mm, board_height_mm)
    if region not in BOARD_REGIONS:
        raise ValueError(f"unsupported board region: {region}")
    x_thirds = (0.0, board_width_mm / 3, board_width_mm * 2 / 3, board_width_mm)
    y_thirds = (0.0, board_height_mm / 3, board_height_mm * 2 / 3, board_height_mm)
    if region == "left":
        return x_thirds[0], y_thirds[0], x_thirds[1], y_thirds[3]
    if region == "right":
        return x_thirds[2], y_thirds[0], x_thirds[3], y_thirds[3]
    if region == "top":
        return x_thirds[0], y_thirds[0], x_thirds[3], y_thirds[1]
    if region == "bottom":
        return x_thirds[0], y_thirds[2], x_thirds[3], y_thirds[3]
    parts = set(region.split("_"))
    horizontal = (
        "left" if "left" in parts else "right" if "right" in parts else "center"
    )
    vertical = "top" if "top" in parts else "bottom" if "bottom" in parts else "center"
    x_index = {"left": 0, "center": 1, "right": 2}[horizontal]
    y_index = {"top": 0, "center": 1, "bottom": 2}[vertical]
    return (
        x_thirds[x_index],
        y_thirds[y_index],
        x_thirds[x_index + 1],
        y_thirds[y_index + 1],
    )


def anchored_rectangle(
    anchor: str,
    width_mm: float,
    height_mm: float,
    board_width_mm: float,
    board_height_mm: float,
    edge_clearance_mm: float,
) -> tuple[float, float, float, float]:
    """Place a sized rectangle at a named anchor inside the usable board area."""

    _validate_board(board_width_mm, board_height_mm)
    if anchor not in BOARD_REGIONS:
        raise ValueError(f"unsupported board anchor: {anchor}")
    values = (width_mm, height_mm, edge_clearance_mm)
    if not all(math.isfinite(value) and value >= 0 for value in values):
        raise ValueError(
            "anchored rectangle dimensions must be finite and non-negative"
        )
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("anchored rectangle width and height must be positive")
    usable_width = board_width_mm - edge_clearance_mm * 2
    usable_height = board_height_mm - edge_clearance_mm * 2
    if width_mm > usable_width or height_mm > usable_height:
        raise ValueError("anchored rectangle does not fit inside the usable board area")

    left = edge_clearance_mm
    right = board_width_mm - edge_clearance_mm
    top = edge_clearance_mm
    bottom = board_height_mm - edge_clearance_mm
    horizontal = (
        "left"
        if anchor in {"left", "top_left", "bottom_left"}
        else "right"
        if anchor in {"right", "top_right", "bottom_right"}
        else "center"
    )
    vertical = (
        "top"
        if anchor in {"top", "top_left", "top_right"}
        else "bottom"
        if anchor in {"bottom", "bottom_left", "bottom_right"}
        else "center"
    )
    x1 = (
        left
        if horizontal == "left"
        else right - width_mm
        if horizontal == "right"
        else (left + right - width_mm) / 2
    )
    y1 = (
        top
        if vertical == "top"
        else bottom - height_mm
        if vertical == "bottom"
        else (top + bottom - height_mm) / 2
    )
    return x1, y1, x1 + width_mm, y1 + height_mm


def copper_layer_indices(scope: str, layer_count: int) -> tuple[int, ...]:
    """Resolve a semantic copper-layer scope to logical router layer indices."""

    if scope not in COPPER_LAYER_SCOPES:
        raise ValueError(f"unsupported copper-layer scope: {scope}")
    if (
        isinstance(layer_count, bool)
        or not isinstance(layer_count, int)
        or layer_count < 1
    ):
        raise ValueError("layer count must be a positive integer")
    if scope == "all":
        return tuple(range(layer_count))
    if scope == "front":
        return (0,)
    if scope == "back":
        return (layer_count - 1,)
    return tuple(sorted({0, layer_count - 1}))


def rectangles_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    tolerance: float = 1e-9,
) -> bool:
    """Return whether two open rectangles overlap by a positive area."""

    return (
        first[0] < second[2] - tolerance
        and first[2] > second[0] + tolerance
        and first[1] < second[3] - tolerance
        and first[3] > second[1] + tolerance
    )


def _validate_board(width_mm: float, height_mm: float) -> None:
    if not all(math.isfinite(value) and value > 0 for value in (width_mm, height_mm)):
        raise ValueError("board dimensions must be positive and finite")
