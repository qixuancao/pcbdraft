"""Reusable Textual widgets for the PCBDraft coding-agent terminal UI."""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Group, RenderableType
from rich.padding import Padding
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
    """Compact project and agent status bar."""

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
        left = Text("◆", style="bold #e2a06b")
        left.append("  PCBDraft", style="bold #f0f2f5")
        left.append("   ")
        left.append(projection.project_name, style="#c9ced7")
        left.append("  ·  ", style="#4f5865")
        left.append(projection.status_label, style=_status_style(projection.status))
        if busy:
            right = Text("◆  ", style="bold #e2a06b")
            right.append(activity_label or "Agent is working", style="#d5d9e0")
        else:
            right = Text(provider_name, style="#8d96a3")
            right.append("  ")
            right.append(
                "●" if provider_status == "ready" else "○",
                style="#82c99a" if provider_status == "ready" else "#d9b968",
            )
        grid.add_row(left, right)
        self.update(grid)


class TranscriptView(VerticalScroll):
    """Scrollable conversation with inline live PCB tool activity."""

    # Keeping focus in the composer makes the software cursor reliably visible.
    # The transcript still supports mouse-wheel scrolling and the app routes
    # PageUp/PageDown here while the composer remains focused.
    can_focus = False

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
            renderables.append(_welcome_renderable())

        for message in projection.messages:
            renderables.append(
                _message_renderable(message.role, message.text, message.kind)
            )

        pending = pending_user_text.strip()
        last_message = projection.messages[-1] if projection.messages else None
        if pending and not (
            last_message is not None
            and last_message.role == "user"
            and last_message.text.strip() == pending
        ):
            renderables.append(_message_renderable("user", pending, "pending"))

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
                    border_style="#7f603b",
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
                    border_style="#7f603b",
                    title="Approval boundary",
                    title_align="left",
                )
            )

        if not renderables:
            renderables.append(
                Panel(
                    Text("Describe a board or a requested change below."),
                    border_style="#2c343e",
                    title="Conversation",
                    title_align="left",
                )
            )
        self.query_one("#transcript-content", Static).update(Group(*renderables))
        scroll_target = (
            self.scroll_home
            if projection.project_id is None
            and not projection.messages
            and not activities
            else self.scroll_end
        )
        self.call_after_refresh(scroll_target, animate=False)


class ProjectRail(VerticalScroll):
    """Persistent PCB facts, workflow, and readiness rail."""

    def compose(self) -> ComposeResult:
        yield Static(id="rail-content")

    def update_state(self, projection: TuiProjection) -> None:
        facts = Table.grid(padding=(0, 1), expand=True)
        facts.add_column(style="#77808e", no_wrap=True)
        facts.add_column(style="#d6dae1", justify="right")
        facts.add_row("Size", projection.board_size)
        facts.add_row("Layers", projection.layer_label)
        facts.add_row("Parts", _count_label(projection.component_count))
        facts.add_row("Nets", _count_label(projection.net_count))
        if projection.attention_required:
            facts.add_row(
                "Review",
                Text(
                    f"{projection.attention_required} finding(s)",
                    style="#d9b968",
                ),
            )

        pipeline = Text()
        stage_marker = {
            "complete": ("✓", "#82c99a"),
            "active": ("◆", "bold #e2a06b"),
            "blocked": ("!", "bold #d9b968"),
            "failed": ("×", "bold #e78284"),
            "pending": ("○", "#515a66"),
        }
        for index, stage in enumerate(projection.stages):
            marker, style = stage_marker[stage.state]
            pipeline.append(f"{marker} ", style=style)
            pipeline.append(stage.name, style="#d6dae1")
            if index < len(projection.stages) - 1:
                pipeline.append("\n│\n", style="#343c46")

        readiness_style = (
            "#82c99a"
            if projection.candidate_ready is True
            else "#e78284"
            if projection.candidate_ready is False
            else "#d9b968"
        )
        readiness = Text(projection.readiness_label, style=f"bold {readiness_style}")
        readiness.append("\n")
        readiness.append(
            "Human review is required before fabrication.",
            style="#858e9a",
        )
        readiness.append("\n")
        readiness.append(f"Assurance: {projection.assurance}", style="#626b78")

        content: list[RenderableType] = [
            Text("PROJECT", style="bold #a97652"),
            Text(projection.project_name, style="bold #eceff3"),
            Text(projection.purpose, style="#929aa6"),
            Text(""),
            Text("PCB", style="bold #a97652"),
            facts,
            Text(""),
            Text("AGENT PIPELINE", style="bold #a97652"),
            pipeline,
            Text(""),
            Text("READINESS", style="bold #a97652"),
            readiness,
        ]
        self.query_one("#rail-content", Static).update(Group(*content))


class NoticeBar(Static):
    """One-line actionable notice or failure summary."""

    def update_state(self, *, notice: str, error: str, busy: bool) -> None:
        self.remove_class("notice-error", "notice-busy", "notice-muted")
        if error:
            self.add_class("notice-error")
            self.update(Text("×  " + error, style="bold #ef9294"))
        elif notice:
            self.add_class("notice-busy" if busy else "notice-muted")
            marker = "◆  " if busy else "·  "
            self.update(Text(marker + notice))
        else:
            self.add_class("notice-muted")
            self.update(Text("Ready"))


class AppFooter(Static):
    """Quiet, persistent navigation hints in place of Textual's busy footer."""

    def update_state(self, projection: TuiProjection, *, provider_status: str) -> None:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right")
        location = Text(
            projection.project_id or "local workspace",
            style="#646d79",
        )
        state = Text(
            "ctrl+p commands   ·   ctrl+x shortcuts   ·   f1 help",
            style="#646d79",
        )
        state.append(
            "   ●",
            style="#82c99a" if provider_status == "ready" else "#d9b968",
        )
        grid.add_row(location, state)
        self.update(grid)


class ComposerInput(Input):
    """Input that releases printable Ctrl+X chords back to the application."""

    _LEADER_KEYS = frozenset({"n", "l", "m", "r", "d", "s", "c", "h", "q"})

    def check_consume_key(self, key: str, character: str | None) -> bool:
        if getattr(self.app, "leader_active", False) and key in self._LEADER_KEYS:
            return False
        return super().check_consume_key(key, character)


class Composer(Vertical):
    """OpenCode-inspired prompt surface with persistent software cursor."""

    def compose(self) -> ComposeResult:
        with Vertical(id="composer-surface"):
            with Horizontal(id="composer-row"):
                yield Static("›", id="composer-prompt")
                yield ComposerInput(
                    placeholder="Ask PCBDraft to design or change a board…",
                    id="composer-input",
                    compact=True,
                    select_on_focus=False,
                )
            with Horizontal(id="composer-meta"):
                yield Static("MESSAGE", id="composer-label")
                yield Static(
                    "enter send   ·   / autocomplete   ·   ctrl+p commands",
                    id="composer-hint",
                )

    def update_state(self, *, label: str, busy: bool) -> None:
        self.set_class(busy, "composer-busy")
        self.query_one("#composer-label", Static).update(
            "WORKING" if busy else label.upper()
        )
        self.query_one("#composer-prompt", Static).update("◆" if busy else "›")
        input_widget = self.query_one("#composer-input", Input)
        input_widget.placeholder = (
            "Agent is working… Esc or /stop interrupts at a safe boundary"
            if busy
            else "Ask PCBDraft to design or change a board…"
        )

    def set_palette_open(self, open_: bool) -> None:
        """Swap the prompt hint while slash completion owns the arrow keys."""

        self.set_class(open_, "palette-open")
        self._refresh_hint()

    def set_leader_active(self, active: bool) -> None:
        """Show the available second keys while Ctrl+X is pending."""

        self.set_class(active, "leader-active")
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        if self.has_class("leader-active"):
            hint = (
                "ctrl+x · n new · l projects · m models · r review · d details · q quit"
            )
        elif self.has_class("palette-open"):
            hint = "↑↓ navigate   ·   enter choose   ·   tab complete   ·   esc close"
        else:
            hint = "enter send   ·   / autocomplete   ·   ctrl+p commands"
        self.query_one("#composer-hint", Static).update(hint)


class CommandPalette(OptionList):
    """Filterable slash-command palette controlled by the composer."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._commands: tuple[SlashCommand, ...] = ()
        self._painted_highlight: int | None = None

    def set_commands(self, commands: Sequence[SlashCommand]) -> None:
        self._commands = tuple(commands)
        self._painted_highlight = None
        self.clear_options()
        if not commands:
            empty = Text("  No matching commands", style="#68717d")
            empty.append("   keep typing or press Esc", style="#4f5864")
            self.add_option(Option(empty, id="__empty__", disabled=True))
            self.highlighted = None
            return

        self.add_options(
            Option(_command_prompt(command), id=command.name[1:])
            for command in self._commands
        )
        self.highlighted = 0
        self._paint_cursor()

    def watch_highlighted(self, highlighted: int | None) -> None:
        super().watch_highlighted(highlighted)
        self._paint_cursor()

    def _paint_cursor(self) -> None:
        """Draw a real selection marker while focus stays in the input."""

        if not hasattr(self, "_commands"):
            return
        if self.option_count != len(self._commands):
            return
        if self._painted_highlight == self.highlighted:
            return
        self._painted_highlight = self.highlighted
        for index, command in enumerate(self._commands):
            self.replace_option_prompt_at_index(
                index,
                _command_prompt(command, selected=index == self.highlighted),
            )
        self.refresh()

    def selected_command(self) -> SlashCommand | None:
        if self.highlighted is None:
            return None
        try:
            return self._commands[self.highlighted]
        except IndexError:
            return None


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


def _welcome_renderable() -> RenderableType:
    brand = Text("◆  PCBDRAFT", style="bold #e2a06b")
    brand.append("   hardware design agent", style="#626b77")
    title = Text("Turn an idea into a reviewable KiCad project", style="bold #eceff3")
    body = Text()
    body.append("Describe what the board should do", style="#cbd0d8")
    body.append(
        " — for example: “Make a small USB-C powered temperature sensor board.”",
        style="#858e9a",
    )
    body.append("\n\nPCBDraft will", style="bold #e2a06b")
    body.append(
        " understand the request, plan the circuit, choose routine board details, "
        "generate native KiCad files, and run checks.",
        style="#bdc3cc",
    )
    body.append("\n\nYou approve the plan before files are generated.", style="#d9b968")

    actions = Table.grid(padding=(0, 2))
    actions.add_column(style="bold #eab17f", no_wrap=True)
    actions.add_column(style="#919aa6")
    actions.add_row("/connect", "connect a model provider")
    actions.add_row("/models", "switch the active model")
    actions.add_row("/help", "show every command and shortcut")
    start = Text("›  Start typing below", style="bold #d8dde4")
    start.append(
        "   ·   type / to autocomplete   ·   Ctrl+P for commands",
        style="#69727f",
    )
    return Padding(
        Group(
            brand, Text(""), title, Text(""), body, Text(""), actions, Text(""), start
        ),
        (1, 3),
    )


def _message_renderable(role: str, text: str, kind: str) -> RenderableType:
    if role == "user":
        table = Table.grid(expand=True, padding=0)
        table.add_column(width=1, no_wrap=True)
        table.add_column(ratio=1, style="on #151a21")
        heading = Text("YOU", style="bold #b7bec8")
        if kind == "pending":
            heading.append("   QUEUED", style="bold #d9b968")
        content = Group(heading, Text(text, style="#eef0f3"))
        table.add_row(
            Text("▌", style="#e2a06b"),
            Padding(content, (1, 2)),
        )
        return Padding(table, (1, 0, 0, 0))

    if role == "assistant":
        heading = Text("◆  PCBDraft", style="bold #82c99a")
        content = Text(text, style="#d8dde4")
        return Padding(Group(heading, Padding(content, (0, 0, 0, 3))), (1, 1, 0, 1))

    heading = Text("·  SYSTEM", style="bold #8f829f")
    return Padding(
        Group(heading, Padding(Text(text, style="#b9bec7"), (0, 0, 0, 3))),
        (1, 1, 0, 1),
    )


def _activity_panel(activities: Sequence[AgentActivity], *, expanded: bool) -> Panel:
    limit = 40 if expanded else 10
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1)
    table.add_column(style="#687590", justify="right", no_wrap=True)
    marker = {
        "queued": ("○", "#77808e"),
        "running": ("◆", "bold #e2a06b"),
        "completed": ("✓", "#82c99a"),
        "failed": ("×", "bold #e78284"),
        "info": ("·", "#717a87"),
    }
    for activity in activities[-limit:]:
        symbol, style = marker[activity.state]
        label = Text(activity.label, style="#d6dae1")
        label.append(f"\n{activity.message}", style="#77808e") if (
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
        border_style="#343f4b",
    )


def _command_prompt(command: SlashCommand, *, selected: bool = False) -> Text:
    prompt = Text("›" if selected else " ", style="bold #f0b47e" if selected else "")
    prompt.append("  ")
    prompt.append(
        f"{command.usage:<20}", style="bold #f0b47e" if selected else "bold #d9dde3"
    )
    prompt.append(command.description, style="#c5cbd3" if selected else "#7f8895")
    return prompt


def _count_label(value: int | None) -> str:
    return str(value) if value is not None else "After planning"


def _status_style(status: str) -> str:
    if "failed" in status or status == "interrupted":
        return "bold #e78284"
    if status in {"validated", "released"}:
        return "bold #82c99a"
    if status in {"interpreting", "generating", "repairing", "validating", "releasing"}:
        return "bold #e2a06b"
    return "#d9b968"
