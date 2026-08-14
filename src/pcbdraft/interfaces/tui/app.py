"""Textual application shell for PCBDraft's UI-neutral agent runtime."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.events import Key, Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from pcbdraft.interfaces.tui.commands import SlashCommand, command_suggestions
from pcbdraft.interfaces.tui.projection import project_projection
from pcbdraft.interfaces.tui.review import review_sections
from pcbdraft.interfaces.tui.widgets import (
    AgentHeader,
    AppFooter,
    CommandPalette,
    Composer,
    NoticeBar,
    ProjectRail,
    TranscriptView,
    review_renderable,
)
from pcbdraft.model.config import (
    ModelChoice,
    load_model_config,
    preset,
    provider_presets,
)


class ProjectPickerScreen(ModalScreen[str | None]):
    """Keyboard and mouse project selector."""

    BINDINGS = (
        Binding("escape", "cancel", "Close"),
        Binding("q", "cancel", "Close", show=False),
    )

    def __init__(self, projects: Sequence[dict[str, Any]], selected: int = 0) -> None:
        super().__init__()
        self.projects = tuple(projects)
        self.selected = selected

    def compose(self) -> ComposeResult:
        options: list[Option] = []
        for project in self.projects:
            project_id = project.get("id")
            if not isinstance(project_id, str):
                continue
            prompt = Text(str(project.get("name", "Untitled")), style="bold #e6edf8")
            prompt.append("\n")
            prompt.append(
                f"{str(project.get('status', 'unknown')).replace('_', ' ')}  ·  {project_id}",
                style="#77849f",
            )
            options.append(Option(prompt, id=project_id))
        with Container(classes="modal-card", id="project-picker-card"):
            yield Static("Open a project", classes="modal-title")
            yield Static(
                "Recent local PCBDraft projects. No work is replayed when opened.",
                classes="modal-subtitle",
            )
            yield OptionList(*options, id="projects-list")
            yield Static("Enter open  ·  Esc close", classes="modal-hint")

    def on_mount(self) -> None:
        picker = self.query_one("#projects-list", OptionList)
        if picker.option_count:
            picker.highlighted = max(0, min(self.selected, picker.option_count - 1))
            picker.focus()

    @on(OptionList.OptionSelected, "#projects-list")
    def choose_project(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CommandPickerScreen(ModalScreen[str | None]):
    """Searchable command palette opened by the conventional Ctrl+P shortcut."""

    BINDINGS = (Binding("escape", "cancel", "Close"),)

    def __init__(self) -> None:
        super().__init__()
        self._commands: tuple[SlashCommand, ...] = ()

    def compose(self) -> ComposeResult:
        with Container(classes="modal-card command-picker-card"):
            yield Static("Commands", classes="modal-title")
            yield Static(
                "Search every PCBDraft action without leaving the prompt.",
                classes="modal-subtitle",
            )
            yield Input(placeholder="Search commands…", id="command-filter")
            yield OptionList(id="commands-list")
            yield Static(
                "↑/↓ move  ·  Enter run  ·  Esc close  ·  Ctrl+X quick actions",
                classes="modal-hint",
            )

    def on_mount(self) -> None:
        self._refresh_options("")
        self.query_one("#command-filter", Input).focus()

    def on_key(self, event: Key) -> None:
        command_filter = self.query_one("#command-filter", Input)
        if not command_filter.has_focus:
            return
        picker = self.query_one("#commands-list", OptionList)
        if event.key in {"up", "ctrl+p"}:
            picker.action_cursor_up()
        elif event.key in {"down", "ctrl+n"}:
            picker.action_cursor_down()
        else:
            return
        event.prevent_default()
        event.stop()

    @on(Input.Changed, "#command-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self._refresh_options(event.value)

    @on(Input.Submitted, "#command-filter")
    def filter_submitted(self, _event: Input.Submitted) -> None:
        self._select_highlighted()

    @on(OptionList.OptionSelected, "#commands-list")
    def command_selected(self, event: OptionList.OptionSelected) -> None:
        command_name = event.option.id
        if isinstance(command_name, str) and command_name != "__empty__":
            self.dismiss(command_name)

    def _refresh_options(self, query: str) -> None:
        needle = query.strip().removeprefix("/").casefold()
        self._commands = tuple(
            command
            for command in command_suggestions("/")
            if not needle
            or needle in command.name.casefold()
            or needle in command.usage.casefold()
            or needle in command.description.casefold()
        )
        options: list[Option] = []
        for command in self._commands:
            prompt = Text(f"{command.usage:<22}", style="bold #eab17f")
            prompt.append(command.description, style="#8b94a0")
            options.append(Option(prompt, id=command.name))
        if not options:
            options.append(
                Option("No matching commands", id="__empty__", disabled=True)
            )
        picker = self.query_one("#commands-list", OptionList)
        picker.clear_options()
        picker.add_options(options)
        picker.highlighted = 0 if self._commands else None

    def _select_highlighted(self) -> None:
        highlighted = self.query_one("#commands-list", OptionList).highlighted
        if highlighted is None:
            return
        try:
            command = self._commands[highlighted]
        except IndexError:
            return
        self.dismiss(command.name)

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class ProviderForm:
    """Validated-at-the-boundary values returned by the connect dialog."""

    provider_id: str
    api_key: str
    base_url: str
    model: str
    name: str | None = None


@dataclass(frozen=True)
class ModelSelection:
    provider_id: str
    model: str


class ProviderPickerScreen(ModalScreen[str | None]):
    """OpenCode-style provider catalog with connected-state gutters."""

    BINDINGS = (
        Binding("escape", "cancel", "Close"),
        Binding("q", "cancel", "Close", show=False),
    )

    def __init__(self) -> None:
        super().__init__()
        try:
            self.config = load_model_config()
        except Exception:  # noqa: BLE001 - dialog must render configuration errors
            self.config = None

    def compose(self) -> ComposeResult:
        options: list[Option] = []
        connected = (
            {provider.id: provider for provider in self.config.providers}
            if self.config is not None
            else {}
        )
        for provider in provider_presets():
            connection = connected.get(provider.id)
            marker = "✓ connected" if connection is not None else "○ not connected"
            selected_model = (
                self.config.active_model
                if connection is not None
                and self.config is not None
                and self.config.active_provider == provider.id
                and self.config.active_model
                else provider.default_model
            )
            prompt = Text(provider.name, style="bold #e6edf8")
            prompt.append(
                "  " + marker,
                style="#5ee0a0" if connection is not None else "#6d7891",
            )
            prompt.append(
                "\n" + provider.hint + "  ·  " + selected_model,
                style="#9eb9da",
            )
            options.append(Option(prompt, id=provider.id))
        custom = Text("Custom OpenAI-compatible", style="bold #e6edf8")
        custom.append(
            "\nAny provider with a /chat/completions endpoint", style="#9eb9da"
        )
        options.append(Option(custom, id="custom"))
        with Container(classes="modal-card provider-picker-card"):
            yield Static("Connect a provider", classes="modal-title")
            yield Static(
                "Choose a preset, enter the key once, then pick a model.",
                classes="modal-subtitle",
            )
            yield OptionList(*options, id="provider-list")
            yield Static(
                "↑/↓ move  ·  Enter choose  ·  Esc close", classes="modal-hint"
            )

    def on_mount(self) -> None:
        picker = self.query_one("#provider-list", OptionList)
        if picker.option_count:
            picker.highlighted = 0
            picker.focus()

    @on(OptionList.OptionSelected, "#provider-list")
    def choose_provider(self, event: OptionList.OptionSelected) -> None:
        provider_id = event.option.id
        self.dismiss(provider_id if isinstance(provider_id, str) else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProviderConnectScreen(ModalScreen[ProviderForm | None]):
    """Small credential form; the API key field is always masked."""

    BINDINGS = (
        Binding("escape", "cancel", "Close"),
        Binding("ctrl+s", "save", "Save", show=False),
    )

    def __init__(self, provider_id: str) -> None:
        super().__init__()
        self.provider_id = provider_id
        self.preset = preset(provider_id)

    def compose(self) -> ComposeResult:
        is_custom = self.preset is None
        provider_name = self.preset.name if self.preset else "Custom provider"
        base_url = self.preset.base_url if self.preset else "https://api.example.com/v1"
        model = self.preset.default_model if self.preset else ""
        with Container(classes="modal-card provider-form-card"):
            yield Static(f"Connect {provider_name}", classes="modal-title")
            yield Static(
                self.preset.hint
                if self.preset
                else "Enter any OpenAI-compatible endpoint.",
                classes="modal-subtitle",
            )
            if is_custom:
                yield Static("Provider id", classes="field-label")
                yield Input(value="custom", id="provider-id", placeholder="my-provider")
                yield Static("Display name", classes="field-label")
                yield Input(value="Custom provider", id="provider-name")
            yield Static("Base URL", classes="field-label")
            yield Input(value=base_url, id="provider-base-url")
            yield Static("Model", classes="field-label")
            yield Input(value=model, id="provider-model")
            yield Static("API key", classes="field-label")
            yield Input(placeholder="sk-…", password=True, id="provider-api-key")
            yield Static(
                "The key is written only to PCBDraft's chmod 600 config file.",
                classes="form-note",
            )
            yield Static("", id="provider-form-error", classes="form-error")
            yield Static(
                "Tab next field  ·  Ctrl+S save  ·  Esc cancel", classes="modal-hint"
            )
            with Horizontal(classes="modal-actions"):
                yield Button(
                    "Save and choose model", id="save-provider", variant="primary"
                )
                yield Button("Cancel", id="cancel-provider")

    def on_mount(self) -> None:
        self.query_one("#provider-api-key", Input).focus()

    @on(Button.Pressed, "#save-provider")
    def save_button(self) -> None:
        self._save()

    @on(Button.Pressed, "#cancel-provider")
    def cancel_button(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def submit_input(self, _event: Input.Submitted) -> None:
        self._save()

    def _save(self) -> None:
        def value(selector: str) -> str:
            return self.query_one(selector, Input).value.strip()

        provider_id = self.provider_id
        name: str | None = None
        if self.preset is None:
            provider_id = value("#provider-id").casefold()
            name = value("#provider-name")
        result = ProviderForm(
            provider_id=provider_id,
            api_key=value("#provider-api-key"),
            base_url=value("#provider-base-url"),
            model=value("#provider-model"),
            name=name,
        )
        if not result.api_key:
            self.query_one("#provider-form-error", Static).update(
                "API key cannot be empty."
            )
            return
        if not result.base_url or not result.model:
            self.query_one("#provider-form-error", Static).update(
                "Base URL and model are required."
            )
            return
        self.dismiss(result)

    def action_save(self) -> None:
        self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelPickerScreen(ModalScreen[ModelSelection | None]):
    """Searchable model picker grouped by connected provider."""

    BINDINGS = (
        Binding("escape", "cancel", "Close"),
        Binding("q", "cancel", "Close", show=False),
    )

    def __init__(self) -> None:
        super().__init__()
        try:
            self.config = load_model_config()
        except Exception:  # noqa: BLE001 - the controller reports the detail
            self.config = None
        self._choices: tuple[ModelChoice, ...] = ()

    def compose(self) -> ComposeResult:
        with Container(classes="modal-card model-picker-card"):
            yield Static("Select a model", classes="modal-title")
            yield Static(
                "Search by provider or model name. Selection applies to this PCBDraft config.",
                classes="modal-subtitle",
            )
            yield Input(placeholder="Filter models…", id="model-filter")
            yield OptionList(id="models-list")
            yield Static(
                "Tab list  ·  ↑/↓ move  ·  Enter choose  ·  Esc close",
                classes="modal-hint",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Add provider", id="model-connect", variant="primary")
                yield Button("Cancel", id="cancel-model")

    def on_mount(self) -> None:
        self._refresh_options("")
        self.query_one("#model-filter", Input).focus()

    def on_key(self, event: Key) -> None:
        """Navigate results without moving focus out of the search field."""

        model_filter = self.query_one("#model-filter", Input)
        if not model_filter.has_focus:
            return
        picker = self.query_one("#models-list", OptionList)
        if event.key == "up":
            picker.action_cursor_up()
        elif event.key == "down":
            picker.action_cursor_down()
        else:
            return
        event.prevent_default()
        event.stop()

    @on(Input.Changed, "#model-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self._refresh_options(event.value)

    @on(Input.Submitted, "#model-filter")
    def filter_submitted(self, _event: Input.Submitted) -> None:
        """Select the currently highlighted result after keyboard filtering."""

        highlighted = self.query_one("#models-list", OptionList).highlighted
        if self._choices and highlighted is not None:
            self._dismiss_choice(highlighted)

    def _refresh_options(self, query: str) -> None:
        options: list[Option] = []
        if self.config is not None:
            self._choices = self.config.choices(query)
            for index, choice in enumerate(self._choices):
                prompt = Text(choice.model, style="bold #e6edf8")
                prompt.append("  " + choice.provider_name, style="#8491ac")
                if choice.active:
                    prompt.append("  ✓ active", style="#5ee0a0")
                options.append(Option(prompt, id=str(index)))
        else:
            self._choices = ()
        if not options:
            options.append(
                Option(
                    "No connected models — use /connect first",
                    id="empty",
                    disabled=True,
                )
            )
        picker = self.query_one("#models-list", OptionList)
        picker.clear_options()
        picker.add_options(options)
        if self._choices:
            picker.highlighted = 0

    @on(OptionList.OptionSelected, "#models-list")
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        if not isinstance(event.option.id, str) or event.option.id == "empty":
            return
        try:
            index = int(event.option.id)
        except (ValueError, IndexError):
            return
        self._dismiss_choice(index)

    @on(Button.Pressed, "#model-connect")
    def connect_provider(self) -> None:
        # A private sentinel keeps the screen result typed without coupling the
        # modal to the controller or pushing screens from inside a widget.
        self.dismiss(ModelSelection("__connect__", ""))

    @on(Button.Pressed, "#cancel-model")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def _dismiss_choice(self, index: int) -> None:
        try:
            choice = self._choices[index]
        except IndexError:
            return
        self.dismiss(ModelSelection(choice.provider_id, choice.model))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReviewScreen(ModalScreen[None]):
    """Scrollable plan, semantic diff, and validation review."""

    BINDINGS = (
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
    )

    def __init__(self, sections: Sequence[Any]) -> None:
        super().__init__()
        self.sections = tuple(sections)

    def compose(self) -> ComposeResult:
        with Container(classes="modal-card", id="review-card"):
            yield Static("Engineering review", classes="modal-title")
            yield Static(
                "Plan, staged changes, and retained validation evidence",
                classes="modal-subtitle",
            )
            with VerticalScroll(id="review-scroll"):
                yield Static(review_renderable(self.sections), id="review-content")
            yield Static("↑/↓ scroll  ·  Esc close", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#review-scroll", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Short discoverable command reference."""

    BINDINGS = (
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
    )

    def compose(self) -> ComposeResult:
        shortcuts = Table.grid(padding=(0, 2))
        shortcuts.add_column(style="bold #eab17f", no_wrap=True)
        shortcuts.add_column(style="#a2aab5")
        shortcuts.add_row("Ctrl+P", "open the command palette")
        shortcuts.add_row("Ctrl+X, N", "new project")
        shortcuts.add_row("Ctrl+X, L", "list projects")
        shortcuts.add_row("Ctrl+X, M", "switch model")
        shortcuts.add_row("Ctrl+X, R", "open engineering review")
        shortcuts.add_row("Ctrl+X, D", "toggle tool details")
        shortcuts.add_row("Ctrl+X, S", "refresh project status")
        shortcuts.add_row("Ctrl+X, C", "connect a provider")
        shortcuts.add_row("Ctrl+X, H", "open this help")
        shortcuts.add_row("Ctrl+X, Q", "quit or stop the active turn")
        shortcuts.add_row("Ctrl+D", "quit when the prompt is empty")
        shortcuts.add_row("Esc", "close a menu or interrupt the active turn")
        shortcuts.add_row("PageUp / PageDown", "scroll the conversation")

        rows = []
        for command in command_suggestions("/"):
            line = Text(command.usage, style="bold #eab17f")
            line.append("\n  " + command.description, style="#858e9a")
            rows.append(line)
        with Container(classes="modal-card", id="help-card"):
            yield Static("Keyboard and commands", classes="modal-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(
                    Group(
                        Text("KEYBOARD", style="bold #a97652"),
                        shortcuts,
                        Text("\nSLASH COMMANDS", style="bold #a97652"),
                        *rows,
                    ),
                    id="help-content",
                )
            yield Static(
                "Ctrl+P commands  ·  Type / for inline completion  ·  Esc close",
                classes="modal-hint",
            )

    def action_close(self) -> None:
        self.dismiss(None)


class PCBDraftApp(App[int], inherit_bindings=False):
    """Coding-agent-style terminal client over :class:`TuiController`."""

    CSS_PATH = "styles.tcss"
    TITLE = "PCBDraft"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = (
        Binding("ctrl+p", "commands", "Commands", priority=True),
        Binding("ctrl+x", "leader", "Quick actions", priority=True),
        Binding("n", "leader_new", "", show=False, priority=True),
        Binding("l", "leader_projects", "", show=False, priority=True),
        Binding("m", "leader_models", "", show=False, priority=True),
        Binding("r", "leader_review", "", show=False, priority=True),
        Binding("d", "leader_details", "", show=False, priority=True),
        Binding("s", "leader_status", "", show=False, priority=True),
        Binding("c", "leader_connect", "", show=False, priority=True),
        Binding("h", "leader_help", "", show=False, priority=True),
        Binding("q", "leader_quit", "", show=False, priority=True),
        Binding("ctrl+d", "eof_or_delete", "Quit", show=False, priority=True),
        Binding("f5", "refresh_project", "Refresh", show=False),
        Binding("f1", "help", "Help", show=False),
    )

    def __init__(self, controller: Any) -> None:
        super().__init__()
        self.controller = controller
        self._palette_dismissed = False
        self._palette_dismissed_for: str | None = None
        self._palette_names: tuple[str, ...] | None = None
        self._transcript_signature: tuple[Any, ...] | None = None
        self._leader_active = False
        self._leader_timer: Any | None = None

    def compose(self) -> ComposeResult:
        yield AgentHeader(id="agent-header")
        with Horizontal(id="main-area"):
            yield TranscriptView(id="transcript")
            yield ProjectRail(id="project-rail")
        yield CommandPalette(id="command-palette")
        yield Composer(id="composer")
        yield NoticeBar(id="notice-bar")
        yield AppFooter(id="app-footer")

    def on_mount(self) -> None:
        self.query_one("#command-palette", CommandPalette).styles.display = "none"
        self._sync_ui(force_transcript=True)
        composer = self.query_one("#composer-input", Input)
        composer.cursor_blink = False
        composer.focus()
        self.set_interval(0.1, self._poll_controller, name="agent-events")
        self._set_responsive_layout(self.size.width)

    def on_resize(self, event: Resize) -> None:
        self._set_responsive_layout(event.size.width)

    @property
    def leader_active(self) -> bool:
        return self._leader_active

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Enable printable leader bindings only while Ctrl+X is pending."""

        if action == "commands":
            return (
                not isinstance(self.screen, ModalScreen) and not self._palette_visible()
            )
        if action == "leader":
            return not isinstance(self.screen, ModalScreen)
        if action.startswith("leader_"):
            return self._leader_active and not isinstance(self.screen, ModalScreen)
        if action == "eof_or_delete":
            return not isinstance(self.screen, ModalScreen)
        return super().check_action(action, parameters)

    def on_key(self, event: Key) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if self._leader_active:
            self._clear_leader()
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                return
        palette = self.query_one("#command-palette", CommandPalette)
        if self._palette_visible():
            if event.key in {"up", "ctrl+p"}:
                palette.action_cursor_up()
                event.prevent_default()
                event.stop()
                return
            if event.key in {"down", "ctrl+n"}:
                palette.action_cursor_down()
                event.prevent_default()
                event.stop()
                return
            if event.key == "tab":
                self._complete_palette_command()
                event.prevent_default()
                event.stop()
                return
        if event.key in {"pageup", "pagedown"}:
            transcript = self.query_one("#transcript", TranscriptView)
            if event.key == "pageup":
                transcript.scroll_page_up(animate=False)
            else:
                transcript.scroll_page_down(animate=False)
            event.prevent_default()
            event.stop()
            return
        if event.key == "escape":
            if self._palette_visible():
                self._palette_dismissed = True
                self._palette_dismissed_for = self.query_one(
                    "#composer-input", Input
                ).value
                palette.styles.display = "none"
                self.query_one("#composer", Composer).set_palette_open(False)
            elif self.controller.is_busy:
                self.controller.stop_active()
                self._sync_ui(force_transcript=True)
            elif self.controller.mode != "message":
                self.controller.cancel_overlay()
                self._sync_ui(force_transcript=True)
            event.prevent_default()
            event.stop()

    @on(Input.Changed, "#composer-input")
    def input_changed(self, event: Input.Changed) -> None:
        if self._leader_active:
            self._clear_leader()
        if event.value != self._palette_dismissed_for:
            self._palette_dismissed = False
            self._palette_dismissed_for = None
        self._update_palette(event.value)

    @on(Input.Submitted, "#composer-input")
    def input_submitted(self, event: Input.Submitted) -> None:
        if self._palette_visible():
            command = self.query_one(
                "#command-palette", CommandPalette
            ).selected_command()
            if command is not None and self._command_needs_completion(
                event.value, command
            ):
                self._set_composer_command(command)
                return

        text = event.value
        if not text.strip() and self.controller.mode == "message":
            return
        event.input.value = ""
        self._hide_palette()
        result = self.controller.submit(text)
        self._after_controller_operation(text=text)
        if result == "quit":
            self.exit(0)

    @on(OptionList.OptionSelected, "#command-palette")
    def palette_selected(self, event: OptionList.OptionSelected) -> None:
        command = self.query_one("#command-palette", CommandPalette).selected_command()
        if command is not None:
            self._set_composer_command(command)

    def action_commands(self) -> None:
        self._clear_leader()
        self.push_screen(CommandPickerScreen(), self._command_picker_closed)

    def action_leader(self) -> None:
        self._hide_palette()
        self._leader_active = True
        self.query_one("#composer", Composer).set_leader_active(True)
        self.refresh_bindings()
        if self._leader_timer is not None:
            self._leader_timer.stop()
        self._leader_timer = self.set_timer(
            2.0,
            self._clear_leader,
            name="ctrl-x-leader",
        )

    def action_leader_new(self) -> None:
        self._run_leader_action("new")

    def action_leader_projects(self) -> None:
        self._run_leader_action("projects")

    def action_leader_models(self) -> None:
        self._run_leader_action("models")

    def action_leader_review(self) -> None:
        self._run_leader_action("review")

    def action_leader_details(self) -> None:
        self._run_leader_action("logs")

    def action_leader_status(self) -> None:
        self._run_leader_action("status")

    def action_leader_connect(self) -> None:
        self._run_leader_action("connect")

    def action_leader_help(self) -> None:
        self._clear_leader()
        self.action_help()

    def action_leader_quit(self) -> None:
        self._clear_leader()
        self.action_quit_or_stop()

    def action_eof_or_delete(self) -> None:
        composer = self.query_one("#composer-input", Input)
        if composer.value or not composer.selection.is_empty:
            composer.action_delete_right()
            return
        self.action_quit_or_stop()

    def action_new_project(self) -> None:
        self._run_action("new")

    def action_projects(self) -> None:
        self._run_action("projects")

    def action_review(self) -> None:
        self._run_action("review")

    def action_toggle_logs(self) -> None:
        self._run_action("logs")

    def action_refresh_project(self) -> None:
        self._run_action("status")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_or_stop(self) -> None:
        if self.controller.is_busy:
            self.controller.stop_active()
            self._sync_ui(force_transcript=True)
        else:
            self.exit(0)

    def _run_leader_action(self, action: str) -> None:
        self._clear_leader()
        self._run_action(action)

    def _clear_leader(self) -> None:
        self._leader_active = False
        self.refresh_bindings()
        if self._leader_timer is not None:
            self._leader_timer.stop()
            self._leader_timer = None
        if self.is_mounted:
            self.query_one("#composer", Composer).set_leader_active(False)

    def _run_action(self, action: str) -> None:
        result = self.controller.action(action)
        self._after_controller_operation()
        if result == "quit":
            self.exit(0)

    def _after_controller_operation(self, *, text: str = "") -> None:
        self._sync_ui(force_transcript=True)
        if self.controller.mode == "project_picker":
            self.push_screen(
                ProjectPickerScreen(
                    self.controller.projects, self.controller.picker_index
                ),
                self._project_picker_closed,
            )
        elif self.controller.mode == "provider_picker":
            self.push_screen(ProviderPickerScreen(), self._provider_picker_closed)
        elif self.controller.mode == "provider_form":
            provider_id = getattr(self.controller, "pending_provider_id", None)
            if isinstance(provider_id, str) and provider_id:
                self.push_screen(
                    ProviderConnectScreen(provider_id), self._provider_form_closed
                )
        elif self.controller.mode == "model_picker":
            self.push_screen(ModelPickerScreen(), self._model_picker_closed)
        elif self.controller.mode == "review":
            sections = review_sections(self.controller.view or {})
            self.push_screen(ReviewScreen(sections), self._review_closed)
        elif text.strip().casefold() == "/help":
            self.push_screen(HelpScreen())
        self.query_one("#composer-input", Input).focus()

    def _project_picker_closed(self, project_id: str | None) -> None:
        if project_id:
            self.controller.open_project(project_id)
        else:
            self.controller.cancel_overlay()
        self._sync_ui(force_transcript=True)
        self.query_one("#composer-input", Input).focus()

    def _command_picker_closed(self, command_name: str | None) -> None:
        if command_name is None:
            self.query_one("#composer-input", Input).focus()
            return
        command = next(
            (
                candidate
                for candidate in command_suggestions("/")
                if candidate.name == command_name
            ),
            None,
        )
        if command is None:
            return
        if command.requires_argument:
            self._set_composer_command(command)
            return
        result = self.controller.submit(command.name)
        self._after_controller_operation(text=command.name)
        if result == "quit":
            self.exit(0)

    def _review_closed(self, _result: None) -> None:
        self.controller.cancel_overlay()
        self._sync_ui(force_transcript=True)
        self.query_one("#composer-input", Input).focus()

    def _provider_picker_closed(self, provider_id: str | None) -> None:
        if provider_id:
            self.controller.begin_provider_form(provider_id)
            self.push_screen(
                ProviderConnectScreen(provider_id), self._provider_form_closed
            )
        else:
            self.controller.cancel_overlay()
        self._sync_ui(force_transcript=True)

    def _provider_form_closed(self, result: ProviderForm | None) -> None:
        if result is not None:
            self.controller.save_provider_connection(
                provider_id=result.provider_id,
                api_key=result.api_key,
                base_url=result.base_url,
                model=result.model,
                name=result.name,
            )
        else:
            self.controller.cancel_overlay()
        self._sync_ui(force_transcript=True)
        self.query_one("#composer-input", Input).focus()

    def _model_picker_closed(self, result: ModelSelection | None) -> None:
        if result is not None:
            if result.provider_id == "__connect__":
                self.controller.show_provider_picker()
                self.push_screen(ProviderPickerScreen(), self._provider_picker_closed)
            else:
                self.controller.choose_model(result.provider_id, result.model)
        else:
            self.controller.cancel_overlay()
        self._sync_ui(force_transcript=True)
        if not isinstance(self.screen, ModalScreen):
            self.query_one("#composer-input", Input).focus()

    def _poll_controller(self) -> None:
        if self.controller.poll():
            self._sync_ui(force_transcript=True)

    def _sync_ui(self, *, force_transcript: bool = False) -> None:
        projection = project_projection(
            self.controller.view,
            self.controller.activities,
            busy=self.controller.is_busy,
        )
        self.query_one("#agent-header", AgentHeader).update_state(
            projection,
            provider_name=self.controller.provider_name,
            provider_status=self.controller.provider_status,
            activity_label=self.controller.activity_label,
            busy=self.controller.is_busy,
        )
        self.query_one("#project-rail", ProjectRail).update_state(projection)
        self.query_one("#notice-bar", NoticeBar).update_state(
            notice=self.controller.notice,
            error=self.controller.error,
            busy=self.controller.is_busy,
        )
        self.query_one("#app-footer", AppFooter).update_state(
            projection,
            provider_status=self.controller.provider_status,
        )
        self.query_one("#composer", Composer).update_state(
            label=self.controller.input_label,
            busy=self.controller.is_busy,
        )

        last_activity = (
            self.controller.activities[-1] if self.controller.activities else None
        )
        signature = (
            projection.project_id,
            projection.status,
            len(projection.messages),
            projection.messages[-1].text if projection.messages else "",
            len(self.controller.activities),
            last_activity.sequence if last_activity else 0,
            last_activity.state if last_activity else "",
            self.controller.logs_expanded,
            self.controller.pending_user_text,
        )
        if force_transcript or signature != self._transcript_signature:
            self.query_one("#transcript", TranscriptView).update_state(
                projection,
                self.controller.activities,
                pending_user_text=self.controller.pending_user_text,
                logs_expanded=self.controller.logs_expanded,
            )
            self._transcript_signature = signature

    def _update_palette(self, text: str) -> None:
        command_name_only = text.startswith("/") and not any(
            character.isspace() for character in text[1:]
        )
        commands = command_suggestions(text) if command_name_only else ()
        palette = self.query_one("#command-palette", CommandPalette)
        names = tuple(command.name for command in commands)
        visible = command_name_only and not self._palette_dismissed
        if names != self._palette_names:
            palette.set_commands(commands)
            self._palette_names = names
        palette.styles.display = "block" if visible else "none"
        self.query_one("#composer", Composer).set_palette_open(visible)

    def _palette_visible(self) -> bool:
        return (
            self.query_one("#command-palette", CommandPalette).styles.display != "none"
        )

    def _hide_palette(self) -> None:
        self._palette_dismissed = True
        self._palette_dismissed_for = self.query_one("#composer-input", Input).value
        self.query_one("#command-palette", CommandPalette).styles.display = "none"
        self.query_one("#composer", Composer).set_palette_open(False)

    def _complete_palette_command(self) -> None:
        command = self.query_one("#command-palette", CommandPalette).selected_command()
        if command is not None:
            self._set_composer_command(command)

    def _set_composer_command(self, command: SlashCommand) -> None:
        composer = self.query_one("#composer-input", Input)
        composer.value = command.name + (" " if command.accepts_argument else "")
        composer.cursor_position = len(composer.value)
        self._hide_palette()
        composer.focus()

    @staticmethod
    def _command_needs_completion(text: str, command: SlashCommand) -> bool:
        typed = text[1:]
        name, separator, _argument = typed.partition(" ")
        if name.casefold() != command.name[1:].casefold():
            return True
        return command.requires_argument and not separator

    def _set_responsive_layout(self, width: int) -> None:
        rail = self.query_one("#project-rail", ProjectRail)
        rail.styles.display = "none" if width < 110 else "block"
