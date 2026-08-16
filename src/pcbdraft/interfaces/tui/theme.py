"""Semantic brand theme for the PCBDraft terminal interface."""

from __future__ import annotations

from dataclasses import dataclass, fields

from textual.theme import Theme


@dataclass(frozen=True, slots=True)
class SemanticPalette:
    """Named colors shared by Rich renderables and Textual CSS."""

    background_deep: str = "#080d0c"
    background: str = "#0b1110"
    surface: str = "#111a18"
    panel: str = "#18231f"
    panel_raised: str = "#15211d"
    border: str = "#2d4038"
    border_strong: str = "#426454"
    selection: str = "#1a332b"
    selection_hover: str = "#182a24"
    brand: str = "#e28b54"
    brand_soft: str = "#ffad73"
    text: str = "#eef5f1"
    text_strong: str = "#eef5f1"
    text_mid: str = "#d0ddd7"
    text_soft: str = "#9cada5"
    text_muted: str = "#91a49b"
    text_faint: str = "#768b82"
    blue: str = "#72b59a"
    blue_soft: str = "#9fd1bd"
    success: str = "#4bc88b"
    warning: str = "#e9c864"
    error: str = "#ff7c78"
    error_surface: str = "#2b1718"
    input_selection: str = "#315a4a"
    warning_border: str = "#806f3e"
    busy_border: str = "#8a583c"


PALETTE = SemanticPalette()


THEME_VARIABLE_DEFAULTS: dict[str, str] = {
    f"pcb-{field.name.replace('_', '-')}": getattr(PALETTE, field.name)
    for field in fields(PALETTE)
}
THEME_VARIABLE_DEFAULTS.update(
    {
        "input-cursor-background": PALETTE.brand_soft,
        "input-cursor-foreground": PALETTE.background,
        "input-cursor-text-style": "bold",
        "input-selection-background": PALETTE.input_selection,
        "input-selection-foreground": PALETTE.text_strong,
        "block-cursor-background": PALETTE.brand_soft,
        "block-cursor-foreground": PALETTE.background,
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": PALETTE.selection,
        "block-cursor-blurred-foreground": PALETTE.text_mid,
        "block-cursor-blurred-text-style": "none",
        "scrollbar": PALETTE.border_strong,
        "scrollbar-hover": PALETTE.blue,
        "scrollbar-active": PALETTE.brand,
        "scrollbar-background": PALETTE.background,
        "scrollbar-background-hover": PALETTE.surface,
        "scrollbar-background-active": PALETTE.surface,
        "scrollbar-corner-color": PALETTE.background_deep,
        "screen-selection-background": PALETTE.input_selection,
        "screen-selection-foreground": PALETTE.text_strong,
    }
)


PCBDRAFT_THEME = Theme(
    name="pcbdraft-dark",
    primary=PALETTE.brand,
    secondary=PALETTE.blue,
    warning=PALETTE.warning,
    error=PALETTE.error,
    success=PALETTE.success,
    accent=PALETTE.brand_soft,
    foreground=PALETTE.text,
    background=PALETTE.background,
    surface=PALETTE.surface,
    panel=PALETTE.panel,
    boost=PALETTE.panel_raised,
    dark=True,
    variables=dict(THEME_VARIABLE_DEFAULTS),
)


__all__ = ["PALETTE", "PCBDRAFT_THEME", "THEME_VARIABLE_DEFAULTS"]
