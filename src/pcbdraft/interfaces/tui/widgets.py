"""Reusable Textual widgets for the PCBDraft coding-agent terminal UI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from pcbdraft.agent.events import AgentActivity
from pcbdraft.interfaces.tui.commands import SlashCommand
from pcbdraft.interfaces.tui.projection import TuiProjection
from pcbdraft.interfaces.tui.review import ReviewSection
from pcbdraft.interfaces.tui.theme import PALETTE


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
        spinner: str = "◆",
        elapsed_seconds: int | None = None,
        compact: bool = False,
    ) -> None:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right")
        left = Text("◆", style=f"bold {PALETTE.brand}")
        left.append("  PCBDraft", style=f"bold {PALETTE.text_strong}")
        left.append("  " if compact else "   ")
        if not compact or projection.project_id is not None:
            left.append(projection.project_name, style=PALETTE.text_mid)
            left.append("  ·  ", style=PALETTE.text_faint)
        left.append(projection.status_label, style=_status_style(projection.status))
        if busy:
            right = Text(spinner + "  ", style=f"bold {PALETTE.brand_soft}")
            if not compact:
                right.append(
                    activity_label or "Agent is working", style=PALETTE.text_strong
                )
            if elapsed_seconds is not None:
                if not compact:
                    right.append("  ", style=PALETTE.text_faint)
                right.append(_elapsed_label(elapsed_seconds), style=PALETTE.text_soft)
        else:
            right = Text()
            if not compact:
                right.append(provider_name, style=PALETTE.text_soft)
                right.append("  ")
            right.append(
                "●" if provider_status == "ready" else "○",
                style=(
                    PALETTE.success if provider_status == "ready" else PALETTE.warning
                ),
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
        pending_approval: Mapping[str, Any] | None,
        logs_expanded: bool,
        busy: bool,
    ) -> None:
        follow_tail = self.is_vertical_scroll_end or self.max_scroll_y <= 0
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
            renderables.append(
                _activity_panel(activities, expanded=logs_expanded, busy=busy)
            )

        if pending_approval is not None and not busy:
            tool_name = str(pending_approval.get("tool_name", "pcb_tool"))
            risk = str(pending_approval.get("risk", "bounded"))
            effect = str(pending_approval.get("effect", "project_write"))
            baseline = pending_approval.get("baseline_revision")
            call_id = str(pending_approval.get("tool_call_id", ""))
            details = Text()
            details.append("Approval required for ", style=PALETTE.text_mid)
            details.append(tool_name, style=f"bold {PALETTE.warning}")
            details.append(
                f"  ·  {effect.replace('_', ' ')}  ·  {risk} risk",
                style=PALETTE.text_soft,
            )
            if isinstance(baseline, int):
                details.append(f"  ·  revision {baseline}", style=PALETTE.text_soft)
            details.append(
                "\nReview the retained plan or diff, then use ",
                style=PALETTE.text_mid,
            )
            details.append("/confirm", style=f"bold {PALETTE.brand_soft}")
            details.append(" once or ", style=PALETTE.text_mid)
            details.append("/discard", style=f"bold {PALETTE.brand_soft}")
            details.append(" to reject.", style=PALETTE.text_mid)
            if call_id:
                details.append(f"\ncall {call_id}", style=PALETTE.text_faint)
            renderables.append(
                Panel(
                    details,
                    border_style=PALETTE.warning_border,
                    title="Tool approval",
                    title_align="left",
                )
            )
        elif projection.status == "awaiting_confirmation" and not busy:
            renderables.append(
                Panel(
                    Text.from_markup(
                        f"[bold {PALETTE.warning}]Plan ready for review.[/] "
                        "Use [bold]/review[/] to inspect it and [bold]/confirm[/] "
                        "to generate the KiCad project."
                    ),
                    border_style=PALETTE.warning_border,
                    title="Plan checkpoint",
                    title_align="left",
                )
            )
        elif projection.status == "change_ready" and not busy:
            renderables.append(
                Panel(
                    Text.from_markup(
                        f"[bold {PALETTE.warning}]A semantic change is staged.[/] "
                        "Use [bold]/review[/], then [bold]/confirm[/] or "
                        "[bold]/discard[/]."
                    ),
                    border_style=PALETTE.warning_border,
                    title="Change checkpoint",
                    title_align="left",
                )
            )

        if not renderables:
            renderables.append(
                Panel(
                    Text("Describe a board or a requested change below."),
                    border_style=PALETTE.border,
                    title="Conversation",
                    title_align="left",
                )
            )
        self.query_one("#transcript-content", Static).update(Group(*renderables))
        if projection.project_id is None and not projection.messages and not activities:
            self.call_after_refresh(self.scroll_home, animate=False)
        elif follow_tail:
            self.call_after_refresh(self.scroll_end, animate=False)


class ProjectRail(VerticalScroll):
    """Persistent PCB facts, workflow, and readiness rail."""

    def compose(self) -> ComposeResult:
        yield Static(id="rail-content")

    def update_state(self, projection: TuiProjection) -> None:
        facts = Table.grid(padding=(0, 1), expand=True)
        facts.add_column(style=PALETTE.text_soft, no_wrap=True)
        facts.add_column(style=PALETTE.text_strong, justify="right")
        facts.add_row("Size", projection.board_size)
        facts.add_row("Layers", projection.layer_label)
        facts.add_row("Parts", _count_label(projection.component_count))
        facts.add_row("Nets", _count_label(projection.net_count))
        if projection.attention_required:
            facts.add_row(
                "Review",
                Text(
                    f"{projection.attention_required} finding(s)",
                    style=PALETTE.warning,
                ),
            )

        readiness_style = (
            PALETTE.success
            if projection.candidate_ready is True
            else PALETTE.error
            if projection.candidate_ready is False
            else PALETTE.warning
        )
        readiness = Text(projection.readiness_label, style=f"bold {readiness_style}")
        readiness.append("\n")
        readiness.append(
            "Human review is required before fabrication.",
            style=PALETTE.text_soft,
        )
        readiness.append("\n")
        readiness.append(f"Assurance: {projection.assurance}", style=PALETTE.text_muted)

        next_steps = Text()
        for index, (command, description) in enumerate(_next_actions(projection)):
            if index:
                next_steps.append("\n")
            next_steps.append(command, style=f"bold {PALETTE.brand_soft}")
            next_steps.append("  " + description, style=PALETTE.text_soft)

        content: list[RenderableType] = [
            Text("PROJECT", style=f"bold {PALETTE.brand}"),
            Text(projection.project_name, style=f"bold {PALETTE.text_strong}"),
            Text(projection.purpose, style=PALETTE.text_soft),
            Text(""),
            Text("AGENT", style=f"bold {PALETTE.brand}"),
            Text(projection.status_label, style=_status_style(projection.status)),
            Text(
                "Internal PCB tools run inside one conversation turn.",
                style=PALETTE.text_muted,
            ),
            Text(""),
            Text("NEXT", style=f"bold {PALETTE.brand}"),
            next_steps,
            Text(""),
            Text("PCB", style=f"bold {PALETTE.brand}"),
            facts,
            Text(""),
            Text("READINESS", style=f"bold {PALETTE.brand}"),
            readiness,
        ]
        self.query_one("#rail-content", Static).update(Group(*content))


class NoticeBar(Static):
    """One-line actionable notice or failure summary."""

    def update_state(self, *, notice: str, error: str, busy: bool) -> None:
        self.remove_class("notice-error", "notice-busy", "notice-muted")
        if error:
            self.add_class("notice-error")
            self.update(Text("×  " + error, style=f"bold {PALETTE.error}"))
        elif notice:
            self.add_class("notice-busy" if busy else "notice-muted")
            marker = "◆  " if busy else "·  "
            self.update(Text(marker + notice))
        else:
            self.add_class("notice-muted")
            self.update(Text("Ready"))


class AppFooter(Static):
    """Quiet, persistent navigation hints in place of Textual's busy footer."""

    def update_state(
        self,
        projection: TuiProjection,
        *,
        provider_status: str,
        compact: bool = False,
    ) -> None:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right")
        location = Text(
            "" if compact else projection.project_id or "local workspace",
            style=PALETTE.text_muted,
        )
        state = Text(
            (
                "^P commands  ·  ^X shortcuts  ·  F1 help"
                if compact
                else "ctrl+p commands   ·   ctrl+x shortcuts   ·   ctrl+c clear/stop   ·   f1 help"
            ),
            style=PALETTE.text_muted,
        )
        state.append(
            "   ●",
            style=(PALETTE.success if provider_status == "ready" else PALETTE.warning),
        )
        grid.add_row(location, state)
        self.update(grid)


class ComposerInput(Input):
    """Input that releases printable Ctrl+X chords back to the application."""

    _LEADER_KEYS = frozenset({"n", "l", "b", "m", "r", "d", "s", "c", "h", "q"})

    def check_consume_key(self, key: str, character: str | None) -> bool:
        if getattr(self.app, "leader_active", False) and key in self._LEADER_KEYS:
            return False
        return super().check_consume_key(key, character)


class Composer(Vertical):
    """OpenCode-inspired prompt surface with persistent software cursor."""

    def compose(self) -> ComposeResult:
        self._context_hint = (
            "enter send   ·   / autocomplete   ·   ctrl+p commands   ·   ctrl+c clear"
        )
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
                    self._context_hint,
                    id="composer-hint",
                )

    def update_state(
        self,
        *,
        label: str,
        busy: bool,
        status: str,
        has_project: bool,
        provider_status: str,
    ) -> None:
        self.set_class(busy, "composer-busy")
        self.query_one("#composer-label", Static).update(
            "DRAFT · WORKING" if busy else label.upper()
        )
        self.query_one("#composer-prompt", Static).update("◆" if busy else "›")
        input_widget = self.query_one("#composer-input", Input)
        input_widget.placeholder = (
            "Keep drafting… Enter keeps this text; Esc stops the active turn"
            if busy
            else "Ask PCBDraft to design or change a board…"
        )
        self._context_hint = _composer_context_hint(
            busy=busy,
            status=status,
            has_project=has_project,
            provider_status=provider_status,
        )
        self._refresh_hint()

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
            hint = "ctrl+x · n new · l projects · b board · m models · r review · d tools · q quit"
        elif self.has_class("palette-open"):
            hint = "↑↓ navigate   ·   enter choose   ·   tab complete   ·   esc close"
        else:
            hint = self._context_hint
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
            empty = Text("  No matching commands", style=PALETTE.text_mid)
            empty.append("   keep typing or press Esc", style=PALETTE.text_muted)
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
            body.append("• ", style=PALETTE.blue)
            body.append(line, style=PALETTE.text)
        panels.append(
            Panel(
                body,
                title=section.title,
                title_align="left",
                border_style=PALETTE.border,
            )
        )
    return Group(*panels) if panels else Text("Nothing to review yet.")


def _welcome_renderable() -> RenderableType:
    brand = Text("◆  PCBDRAFT", style=f"bold {PALETTE.brand}")
    brand.append("   hardware design agent", style=PALETTE.text_muted)
    title = Text(
        "Turn an idea into a reviewable KiCad project",
        style=f"bold {PALETTE.text_strong}",
    )
    body = Text()
    body.append(
        "Describe what the board should do",
        style=PALETTE.text,
    )
    body.append(
        " — for example: “Make a small USB-C powered temperature sensor board.”",
        style=PALETTE.text_soft,
    )
    body.append("\n\nPCBDraft will", style=f"bold {PALETTE.brand}")
    body.append(
        " understand the request, plan the circuit, choose routine board details, "
        "generate native KiCad files, and run checks.",
        style=PALETTE.text_mid,
    )
    body.append(
        "\n\nPlans, tool activity, generated files, and check evidence stay inspectable.",
        style=PALETTE.warning,
    )

    actions = Table.grid(padding=(0, 2))
    actions.add_column(style=f"bold {PALETTE.brand_soft}", no_wrap=True)
    actions.add_column(style=PALETTE.text_soft)
    actions.add_row("/new", "optionally start a named project")
    actions.add_row("/connect", "connect a model provider")
    actions.add_row("/models", "switch the active model")
    actions.add_row("/help", "show every command and shortcut")
    start = Text(
        "›  Type a board request and press Enter",
        style=f"bold {PALETTE.text_strong}",
    )
    start.append(
        "   ·   type / to autocomplete   ·   Ctrl+P for commands",
        style=PALETTE.text_muted,
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
        table.add_column(ratio=1, style=f"on {PALETTE.panel}")
        heading = Text("YOU", style=f"bold {PALETTE.text_strong}")
        if kind == "pending":
            heading.append("   QUEUED", style=f"bold {PALETTE.warning}")
        content = Group(heading, Text(text, style=PALETTE.text_strong))
        table.add_row(
            Text("▌", style=PALETTE.brand),
            Padding(content, (1, 2)),
        )
        return Padding(table, (1, 0, 0, 0))

    if role == "assistant":
        heading = Text("◆  PCBDraft", style=f"bold {PALETTE.success}")
        content = Markdown(
            text,
            code_theme="ansi_dark",
            style=PALETTE.text,
            hyperlinks=True,
        )
        return Padding(Group(heading, Padding(content, (0, 0, 0, 3))), (1, 1, 0, 1))

    heading = Text("·  SYSTEM", style=f"bold {PALETTE.text_soft}")
    return Padding(
        Group(heading, Padding(Text(text, style=PALETTE.text_mid), (0, 0, 0, 3))),
        (1, 1, 0, 1),
    )


def _activity_panel(
    activities: Sequence[AgentActivity], *, expanded: bool, busy: bool
) -> Panel:
    limit = 40 if expanded else 6
    visible = list(activities)
    if not expanded and any(item.tool_call_id is not None for item in visible):
        # Durable ToolRun records are the canonical conversation-level activity.
        # Project events remain available under `/logs on`, but showing both in
        # the compact view produces duplicate "job / phase / tool" rows.
        durable = [item for item in visible if item.tool_call_id is not None]
        unmatched_failures = [
            item
            for item in visible
            if item.tool_call_id is None and item.state == "failed"
        ]
        visible = [*unmatched_failures, *durable]
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1)
    table.add_column(style=PALETTE.text_muted, justify="right", no_wrap=True)
    marker = {
        "queued": ("○", PALETTE.text_muted),
        "running": ("◆", f"bold {PALETTE.brand}"),
        "completed": ("✓", PALETTE.success),
        "failed": ("×", f"bold {PALETTE.error}"),
        "info": ("·", PALETTE.text_muted),
    }
    for activity in visible[-limit:]:
        symbol, style = marker[activity.state]
        label = Text(activity.label, style=PALETTE.text)
        if expanded and activity.message:
            label.append(f"\n{activity.message}", style=PALETTE.text_soft)
        if expanded and activity.tool_call_id is not None:
            label.append("\n" + _tool_binding_line(activity), style=PALETTE.text_muted)
            if activity.arguments:
                label.append(
                    "\nargs  " + _bounded_json(activity.arguments),
                    style=PALETTE.text_soft,
                )
            if activity.result:
                label.append(
                    "\nresult  " + _bounded_json(activity.result),
                    style=PALETTE.text_soft,
                )
        identity = Text(activity.tool)
        if expanded and activity.turn_id:
            identity.append(
                f"\nturn {activity.turn_id[-12:]}", style=PALETTE.text_faint
            )
        table.add_row(Text(symbol, style=style), label, identity)
    title = "Agent activity · working" if busy else "Recent activity"
    hint = "expanded · /logs off" if expanded else "/logs to expand"
    return Panel(
        table,
        title=title,
        subtitle=hint,
        title_align="left",
        subtitle_align="right",
        border_style=PALETTE.busy_border if busy else PALETTE.border,
    )


def _tool_binding_line(activity: AgentActivity) -> str:
    details: list[str] = []
    if activity.source:
        details.append(f"source {activity.source.replace('_', ' ')}")
    if activity.effect:
        details.append(activity.effect.replace("_", " "))
    if activity.risk:
        details.append(f"{activity.risk} risk")
    if activity.before_revision is not None:
        revision = f"rev {activity.before_revision}"
        if activity.after_revision is not None:
            revision += f" → {activity.after_revision}"
        details.append(revision)
    if activity.tool_call_id:
        details.append(f"call {activity.tool_call_id[-12:]}")
    if activity.args_hash:
        details.append(f"args {activity.args_hash[:10]}")
    return "  ·  ".join(details)


def _bounded_json(value: Mapping[str, Any], *, limit: int = 600) -> str:
    try:
        rendered = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return "<unavailable>"
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1].rstrip() + "…"


def _command_prompt(command: SlashCommand, *, selected: bool = False) -> Text:
    prompt = Text(
        "›" if selected else " ",
        style=f"bold {PALETTE.brand}" if selected else "",
    )
    prompt.append("  ")
    prompt.append(
        f"{command.usage:<20}",
        style=(f"bold {PALETTE.brand_soft}" if selected else f"bold {PALETTE.text}"),
    )
    prompt.append(
        command.description,
        style=PALETTE.text if selected else PALETTE.text_muted,
    )
    return prompt


def _next_actions(projection: TuiProjection) -> tuple[tuple[str, str], ...]:
    """Return a short, state-specific action list for the project rail."""

    if projection.project_id is None:
        return (
            ("message", "describe a board to begin"),
            ("/projects", "open an existing one"),
        )
    if projection.status == "planning_required":
        return (("/connect", "connect a planning model"), ("/models", "choose a model"))
    if projection.status == "needs_clarification":
        return (
            ("message", "answer the open questions"),
            ("/review", "inspect context"),
        )
    if projection.status == "generation_unavailable":
        return (
            ("message", "adjust the request or constraints"),
            ("/review", "inspect why"),
        )
    if projection.status == "awaiting_confirmation":
        return (("/review", "inspect the plan"), ("/confirm", "generate KiCad files"))
    if projection.status == "change_ready":
        return (("/review", "inspect the diff"), ("/confirm", "apply the change"))
    if projection.status == "provider_error":
        return (("/retry", "retry the model turn"), ("/logs", "inspect the error"))
    if projection.status in {
        "generation_failed",
        "repair_failed",
        "release_failed",
        "interrupted",
    }:
        return (("/review", "inspect findings"), ("/retry", "retry the last turn"))
    if projection.status == "validation_failed":
        return (
            ("/review", "inspect failed checks"),
            ("message", "ask the agent to revise"),
        )
    if projection.status == "generated":
        return (("/validate", "run PCB checks"), ("/review", "inspect generated work"))
    if projection.status == "validated":
        return (("/review", "inspect evidence"), ("/release", "build release evidence"))
    if projection.status == "released":
        return (
            ("/review", "inspect retained evidence"),
            ("/status", "refresh project state"),
        )
    return (
        ("message", "describe the board or next change"),
        ("/help", "show all actions"),
    )


def _composer_context_hint(
    *,
    busy: bool,
    status: str,
    has_project: bool,
    provider_status: str,
) -> str:
    if busy:
        return "working · draft stays here   ·   esc stop   ·   /logs activity"
    if not has_project:
        return "describe a board and press enter   ·   /projects open   ·   ctrl+p commands"
    if status == "planning_required":
        return "/connect planning model   ·   /models choose   ·   /help"
    if status == "needs_clarification":
        return "answer the agent's questions   ·   /review context   ·   /help"
    if status == "generation_unavailable":
        return "adjust the request   ·   /review constraints   ·   /help"
    if provider_status != "ready" and status in {"draft", "ready"}:
        return "/connect provider   ·   /models choose   ·   ctrl+p commands"
    if status == "awaiting_confirmation":
        return "/review inspect plan   ·   /confirm generate   ·   esc close"
    if status == "change_ready":
        return "/review inspect diff   ·   /confirm apply   ·   /discard"
    if status == "provider_error":
        return "/retry model turn   ·   /logs error details   ·   /models switch"
    if status in {
        "generation_failed",
        "repair_failed",
        "release_failed",
        "interrupted",
    }:
        return "/review findings   ·   /retry last turn   ·   /logs details"
    if status == "validation_failed":
        return "/review failed checks   ·   describe a revision   ·   /logs details"
    if status == "generated":
        return "/validate run checks   ·   /review inspect"
    if status == "validated":
        return "/review evidence   ·   /release build bundle"
    return "enter send   ·   / autocomplete   ·   ctrl+p commands   ·   ctrl+c clear"


def _elapsed_label(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"{minutes}:{remainder:02d}" if minutes else f"{remainder}s"


def _count_label(value: int | None) -> str:
    return str(value) if value is not None else "After planning"


def _status_style(status: str) -> str:
    if "failed" in status or status == "interrupted":
        return f"bold {PALETTE.error}"
    if status in {"validated", "released"}:
        return f"bold {PALETTE.success}"
    if status in {"interpreting", "generating", "repairing", "validating", "releasing"}:
        return f"bold {PALETTE.brand_soft}"
    return PALETTE.warning
