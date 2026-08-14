"""Reusable Textual widgets for the PCBDraft coding-agent terminal UI."""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .agent_events import AgentActivity
from .tui_commands import SlashCommand
from .tui_projection import TuiProjection
from .tui_review import ReviewSection


class AgentHeader(Static):
    """Project, model, and live agent status."""

    def update_state(
        self,
        projection: TuiProjection,
        *,
        provider_name: str,
        provider_status: str,
        activity_label: str,
        busy: bool,
    ) -> None:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right")
        left = Text("PCBDraft", style="bold #7dd3fc")
        left.append("  ")
        left.append(projection.project_name, style="bold #e5e7eb")
        left.append("  ·  ")
        left.append(projection.status_label, style=_status_style(projection.status))
        right = Text(provider_name, style="#a8b3cf")
        right.append("  ")
        right.append(
            "●" if provider_status == "ready" else "○",
            style="#5ee0a0" if provider_status == "ready" else "#f3c969",
        )
        grid.add_row(left, right)
        subtitle = Text()
        if busy:
            subtitle.append("● ", style="bold #7dd3fc")
            subtitle.append(activity_label or "Agent turn running", style="#c9d4ef")
        else:
            subtitle.append("Ready", style="#7e8ba8")
            subtitle.append(
                "  ·  Esc stops a turn  ·  / commands  ·  Ctrl+P projects",
                style="#5f6b85",
            )
        self.update(Group(grid, subtitle))


class TranscriptView(VerticalScroll):
    """Scrollable conversation with inline live PCB tool activity."""

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static(id="transcript-content")

    def update_state(
        self,
        projection: TuiProjection,
        activities: Sequence[AgentActivity],
        *,
        pending_user_text: str,
        logs_expanded: bool,
    ) -> None:
        renderables: list[RenderableType] = []
        if projection.project_id is None:
            renderables.append(_welcome_panel())

        for message in projection.messages:
            renderables.append(_message_panel(message.role, message.text, message.kind))

        pending = pending_user_text.strip()
        last_message = projection.messages[-1] if projection.messages else None
        if pending and not (
            last_message is not None
            and last_message.role == "user"
            and last_message.text.strip() == pending
        ):
            renderables.append(_message_panel("user", pending, "pending"))

        if activities:
            renderables.append(_activity_panel(activities, expanded=logs_expanded))

        if projection.status == "awaiting_confirmation":
            renderables.append(
                Panel(
                    Text.from_markup(
                        "[bold #f3c969]Plan ready for review.[/] "
                        "Use [bold]/review[/] to inspect it and [bold]/confirm[/] "
                        "to generate the KiCad project."
                    ),
                    border_style="#735f2c",
                    title="Approval boundary",
                    title_align="left",
                )
            )
        elif projection.status == "change_ready":
            renderables.append(
                Panel(
                    Text.from_markup(
                        "[bold #f3c969]A semantic change is staged.[/] "
                        "Use [bold]/review[/], then [bold]/confirm[/] or "
                        "[bold]/discard[/]."
                    ),
                    border_style="#735f2c",
                    title="Approval boundary",
                    title_align="left",
                )
            )

        if not renderables:
            renderables.append(
                Panel(
                    Text("Describe a board or a requested change below."),
                    border_style="#2b3448",
                    title="Conversation",
                    title_align="left",
                )
            )
        self.query_one("#transcript-content", Static).update(Group(*renderables))
        self.call_after_refresh(self.scroll_end, animate=False)


class ProjectRail(VerticalScroll):
    """Persistent PCB facts, workflow, and readiness rail."""

    def compose(self) -> ComposeResult:
        yield Static(id="rail-content")

    def update_state(self, projection: TuiProjection) -> None:
        facts = Table.grid(padding=(0, 1), expand=True)
        facts.add_column(style="#7e8ba8", no_wrap=True)
        facts.add_column(style="#dbe5f5", justify="right")
        facts.add_row("Size", projection.board_size)
        facts.add_row("Layers", projection.layer_label)
        facts.add_row("Parts", _count_label(projection.component_count))
        facts.add_row("Nets", _count_label(projection.net_count))
        if projection.attention_required:
            facts.add_row(
                "Review",
                Text(
                    f"{projection.attention_required} finding(s)",
                    style="#f3c969",
                ),
            )

        pipeline = Text()
        stage_marker = {
            "complete": ("✓", "#5ee0a0"),
            "active": ("●", "bold #7dd3fc"),
            "blocked": ("!", "bold #f3c969"),
            "failed": ("×", "bold #ff7b88"),
            "pending": ("○", "#536078"),
        }
        for index, stage in enumerate(projection.stages):
            marker, style = stage_marker[stage.state]
            pipeline.append(f"{marker} ", style=style)
            pipeline.append(stage.name, style="#dbe5f5")
            if index < len(projection.stages) - 1:
                pipeline.append("\n│\n", style="#354058")

        readiness_style = (
            "#5ee0a0"
            if projection.candidate_ready is True
            else "#ff7b88"
            if projection.candidate_ready is False
            else "#f3c969"
        )
        readiness = Text(projection.readiness_label, style=f"bold {readiness_style}")
        readiness.append("\n")
        readiness.append(
            "Human engineering review is required before fabrication.",
            style="#8c98b3",
        )
        readiness.append("\n")
        readiness.append(f"Assurance: {projection.assurance}", style="#65728e")

        content: list[RenderableType] = [
            Text("PROJECT", style="bold #65728e"),
            Text(projection.project_name, style="bold #eef2ff"),
            Text(projection.purpose, style="#9ca8c0"),
            Text(""),
            Text("PCB", style="bold #65728e"),
            facts,
            Text(""),
            Text("AGENT PIPELINE", style="bold #65728e"),
            pipeline,
            Text(""),
            Text("READINESS", style="bold #65728e"),
            readiness,
        ]
        self.query_one("#rail-content", Static).update(Group(*content))


class NoticeBar(Static):
    """One-line actionable notice or failure summary."""

    def update_state(self, *, notice: str, error: str, busy: bool) -> None:
        self.remove_class("notice-error", "notice-busy", "notice-muted")
        if error:
            self.add_class("notice-error")
            self.update(Text("×  " + error, style="bold #ff9aa5"))
        elif notice:
            self.add_class("notice-busy" if busy else "notice-muted")
            marker = "●  " if busy else "·  "
            self.update(Text(marker + notice))
        else:
            self.add_class("notice-muted")
            self.update(
                Text(
                    "Describe a board in plain language; dimensions and layers are automatic."
                )
            )


class Composer(Vertical):
    """Prompt label, multiline-feeling single-line input, and context hint."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="composer-row"):
            yield Static("Message", id="composer-label")
            yield Input(
                placeholder="Describe a PCB, request a change, or type / for commands",
                id="composer-input",
            )
        yield Static(
            "Enter send  ·  /connect provider  ·  /models model  ·  Ctrl+R review",
            id="composer-hint",
        )

    def update_state(self, *, label: str, busy: bool) -> None:
        self.query_one("#composer-label", Static).update(label)
        input_widget = self.query_one("#composer-input", Input)
        input_widget.placeholder = (
            "Agent is working — Esc or /stop cancels at a safe boundary"
            if busy
            else "Describe a PCB or type / for commands  (try /connect or /models)"
        )


class CommandPalette(OptionList):
    """Filterable slash-command palette controlled by the composer."""

    def set_commands(self, commands: Sequence[SlashCommand]) -> None:
        highlighted = self.highlighted
        self.clear_options()
        self.add_options(
            Option(_command_prompt(command), id=command.name[1:])
            for command in commands
        )
        if commands:
            self.highlighted = min(highlighted or 0, len(commands) - 1)

    def selected_command(self) -> SlashCommand | None:
        if self.option_count == 0 or self.highlighted is None:
            return None
        option = self.get_option_at_index(self.highlighted)
        if option.id is None:
            return None
        name = "/" + option.id
        from .tui_commands import SLASH_COMMANDS

        return next(
            (command for command in SLASH_COMMANDS if command.name == name), None
        )


def review_renderable(sections: Sequence[ReviewSection]) -> RenderableType:
    """Render bounded review sections without interpreting engineering state."""

    panels: list[RenderableType] = []
    for section in sections:
        body = Text()
        for index, line in enumerate(section.lines):
            if index:
                body.append("\n")
            body.append("• ", style="#61708d")
            body.append(line, style="#d8e1f2")
        panels.append(
            Panel(
                body,
                title=section.title,
                title_align="left",
                border_style="#3c4963",
            )
        )
    return Group(*panels) if panels else Text("Nothing to review yet.")


def _welcome_panel() -> Panel:
    title = Text("Turn an idea into a reviewable KiCad project", style="bold #eef2ff")
    body = Text()
    body.append("\nDescribe what the board should do", style="#c9d4ef")
    body.append(
        " — for example: “Make a small USB-C powered temperature sensor board.”",
        style="#8996b2",
    )
    body.append("\n\nPCBDraft will", style="bold #7dd3fc")
    body.append(
        " understand the request, plan the circuit, choose routine board details, "
        "generate native KiCad files, and run checks.",
        style="#c9d4ef",
    )
    body.append("\n\nYou approve the plan before files are generated.", style="#f3c969")
    body.append("\n\nQuick actions", style="bold #7dd3fc")
    body.append("\n  /connect   add an API key or local provider", style="#a9b8d0")
    body.append("\n  /models    switch the active model", style="#a9b8d0")
    body.append("\n  /help      see every command", style="#a9b8d0")
    return Panel(
        Group(title, body),
        title="Start here",
        title_align="left",
        border_style="#365273",
        padding=(1, 2),
    )


def _message_panel(role: str, text: str, kind: str) -> Panel:
    title, border = {
        "user": ("You", "#3277a8"),
        "assistant": ("PCBDraft", "#34745a"),
    }.get(role, ("System", "#5f5777"))
    subtitle = " sending" if kind == "pending" else ""
    return Panel(
        Text(text, style="#e5eaf3"),
        title=title,
        subtitle=subtitle,
        title_align="left",
        subtitle_align="right",
        border_style=border,
        padding=(0, 1),
    )


def _activity_panel(activities: Sequence[AgentActivity], *, expanded: bool) -> Panel:
    limit = 40 if expanded else 10
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1)
    table.add_column(style="#687590", justify="right", no_wrap=True)
    marker = {
        "queued": ("○", "#7e8ba8"),
        "running": ("●", "bold #7dd3fc"),
        "completed": ("✓", "#5ee0a0"),
        "failed": ("×", "bold #ff7b88"),
        "info": ("·", "#73809c"),
    }
    for activity in activities[-limit:]:
        symbol, style = marker[activity.state]
        label = Text(activity.label, style="#dbe5f5")
        label.append(f"\n{activity.message}", style="#7e8ba8") if (
            expanded and activity.message
        ) else None
        table.add_row(Text(symbol, style=style), label, Text(activity.tool))
    hint = "expanded · /logs off" if expanded else "/logs to expand"
    return Panel(
        table,
        title="Agent tools",
        subtitle=hint,
        title_align="left",
        subtitle_align="right",
        border_style="#435274",
    )


def _command_prompt(command: SlashCommand) -> Text:
    prompt = Text(command.usage, style="bold #b8dcff")
    prompt.append("  ")
    prompt.append(command.description, style="#8d9ab5")
    return prompt


def _count_label(value: int | None) -> str:
    return str(value) if value is not None else "After planning"


def _status_style(status: str) -> str:
    if "failed" in status or status == "interrupted":
        return "bold #ff7b88"
    if status in {"validated", "released"}:
        return "bold #5ee0a0"
    if status in {"interpreting", "generating", "repairing", "validating", "releasing"}:
        return "bold #7dd3fc"
    return "#f3c969"
