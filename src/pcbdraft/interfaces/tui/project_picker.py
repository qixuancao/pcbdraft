"""Pure state and rendering helpers for the interactive PCB project picker."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

Fragment = tuple[str, str]


def project_picker_query(command_name: str, raw_args: str) -> str | None:
    """Return the initial picker query when a command should open the picker."""

    command = command_name.strip().casefold()
    query = raw_args.strip()
    if command in {"resume", "projects"}:
        return query
    if command == "open" and not query:
        return ""
    return None


@dataclass
class ProjectPickerState:
    """Mutable selection state; project records themselves are never mutated."""

    all_projects: list[dict[str, Any]]
    projects: list[dict[str, Any]]
    query: str = ""
    current_project_id: str | None = None
    selected_index: int = 0
    scroll_offset: int = 0

    @classmethod
    def create(
        cls,
        projects: Iterable[dict[str, Any]],
        *,
        query: str = "",
        matches: Iterable[dict[str, Any]] | None = None,
        current_project_id: str | None = None,
    ) -> ProjectPickerState:
        all_projects = list(projects)
        visible_projects = list(matches) if matches is not None else list(all_projects)
        return cls(
            all_projects=all_projects,
            projects=visible_projects,
            query=query,
            current_project_id=current_project_id,
        )

    def replace_matches(self, query: str, projects: Iterable[dict[str, Any]]) -> None:
        """Replace filtered results and reset selection to the newest match."""

        self.query = query
        self.projects = list(projects)
        self.selected_index = 0
        self.scroll_offset = 0

    def move(self, delta: int) -> None:
        """Move the highlight without wrapping past either end."""

        if not self.projects:
            self.selected_index = 0
            self.scroll_offset = 0
            return
        self.selected_index = min(
            len(self.projects) - 1,
            max(0, self.selected_index + delta),
        )

    def selected_project(self) -> dict[str, Any] | None:
        if 0 <= self.selected_index < len(self.projects):
            return self.projects[self.selected_index]
        return None

    def viewport(
        self, terminal_rows: int, *, lines_per_project: int
    ) -> tuple[int, int]:
        """Return a scrolling ``(start, count)`` viewport capped at ten projects."""

        available_lines = max(lines_per_project, terminal_rows - 9)
        visible = max(1, min(10, available_lines // lines_per_project))
        visible = min(visible, len(self.projects))
        if not visible:
            self.scroll_offset = 0
            return 0, 0
        max_offset = max(0, len(self.projects) - visible)
        offset = min(max(0, self.scroll_offset), max_offset)
        if self.selected_index < offset:
            offset = self.selected_index
        elif self.selected_index >= offset + visible:
            offset = self.selected_index - visible + 1
        self.scroll_offset = min(max(0, offset), max_offset)
        return self.scroll_offset, visible


def render_project_picker(
    state: ProjectPickerState,
    *,
    terminal_columns: int,
    terminal_rows: int,
) -> list[Fragment]:
    """Render a bounded picker panel using default foreground and reverse selection."""

    box_width = max(8, min(88, max(8, terminal_columns - 2)))
    content_width = max(8, box_width - 2)
    title = _fit_display("Open PCB project", box_width - 4)
    fragments: list[Fragment] = [
        ("class:project-picker-border", "╭─ "),
        ("class:project-picker-title", title),
        (
            "class:project-picker-border",
            " " + ("─" * max(0, box_width - _display_width(title) - 3)) + "╮\n",
        ),
    ]

    lines_per_project = 2 if content_width >= 40 else 3
    start, visible = state.viewport(terminal_rows, lines_per_project=lines_per_project)
    total = len(state.projects)
    if total:
        end = start + visible
        position = f"{start + 1}-{end} of {total}"
    else:
        position = "0 matches"
    query_label = f'Filter: "{_clean(state.query)}" · ' if state.query else ""
    _append_panel_line(
        fragments,
        _fit_display(
            f"{query_label}{position} · ↑/↓ select · Enter open · Esc cancel",
            content_width,
        ),
        box_width,
        "class:project-picker-hint",
    )

    if not total:
        _append_panel_line(
            fragments,
            _fit_display(
                "No matching projects. Keep typing or press Esc.", content_width
            ),
            box_width,
            "class:project-picker-item",
        )
    else:
        for index in range(start, start + visible):
            project = state.projects[index]
            selected = index == state.selected_index
            style = (
                "class:project-picker-selected"
                if selected
                else "class:project-picker-item"
            )
            prefix = "❯" if selected else " "
            current = (
                "current"
                if str(project.get("id", "")) == str(state.current_project_id or "")
                else ""
            )
            name = _clean(project.get("name") or "Untitled PCB project")
            status = _clean(project.get("status") or "unknown")
            short_id = _short_id(project.get("id"))
            updated = _friendly_updated(project.get("updated_at"))
            marker = " *" if current else ""
            _append_panel_line(
                fragments,
                _fit_display(f"{prefix}{marker} {name}", content_width),
                box_width,
                style,
            )
            if lines_per_project == 2:
                _append_panel_line(
                    fragments,
                    _fit_display(
                        f"  {status} · id:{short_id} · updated:{updated}",
                        content_width,
                    ),
                    box_width,
                    style,
                )
            else:
                _append_panel_line(
                    fragments,
                    _fit_display(f"  {status} · id:{short_id}", content_width),
                    box_width,
                    style,
                )
                compact_updated = updated[5:] if len(updated) >= 16 else updated
                _append_panel_line(
                    fragments,
                    _fit_display(f"  updated:{compact_updated}", content_width),
                    box_width,
                    style,
                )

    fragments.append(("class:project-picker-border", "╰" + ("─" * box_width) + "╯\n"))
    return fragments


def _append_panel_line(
    fragments: list[Fragment], text: str, box_width: int, style: str
) -> None:
    content_width = max(0, box_width - 2)
    fragments.append(("class:project-picker-border", "│ "))
    fragments.append((style, _pad_display(text, content_width)))
    fragments.append(("class:project-picker-border", " │\n"))


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\x1b", "").split())


def _short_id(value: Any) -> str:
    project_id = _clean(value)
    if len(project_id) <= 12:
        return project_id or "unknown"
    return project_id[-8:]


def _friendly_updated(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return "unknown"
    normalized = raw.replace("T", " ").removesuffix("Z")
    return normalized[:16] if len(normalized) >= 16 else normalized


def _display_width(value: str) -> int:
    width = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _fit_display(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if _display_width(value) <= width:
        return value
    if width == 1:
        return "…"
    budget = width - 1
    output: list[str] = []
    used = 0
    for char in value:
        char_width = _display_width(char)
        if used + char_width > budget:
            break
        output.append(char)
        used += char_width
    return "".join(output) + "…"


def _pad_display(value: str, width: int) -> str:
    fitted = _fit_display(value, width)
    return fitted + (" " * max(0, width - _display_width(fitted)))
