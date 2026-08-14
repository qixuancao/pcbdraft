"""Textual application shell for PCBDraft's UI-neutral agent runtime."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rich.console import Group
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.events import Key, Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from .model_config import (
    ModelChoice,
    load_model_config,
    preset,
    provider_presets,
)
from .tui_commands import SlashCommand, command_suggestions
from .tui_projection import project_projection
from .tui_review import review_sections
from .tui_widgets import (
    AgentHeader,
    CommandPalette,
    Composer,
    NoticeBar,
    ProjectRail,
    TranscriptView,
    review_renderable,
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

    @on(Input.Changed, "#model-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self._refresh_options(event.value)

    @on(Input.Submitted, "#model-filter")
    def filter_submitted(self, _event: Input.Submitted) -> None:
        """Make type-filter-then-Enter select the first matching model."""

        if self._choices:
            self._dismiss_choice(0)

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
        rows = []
        for command in command_suggestions("/"):
            line = Text(command.usage, style="bold #b8dcff")
            line.append("\n  " + command.description, style="#8996b2")
            rows.append(line)
        with Container(classes="modal-card", id="help-card"):
            yield Static("Commands and shortcuts", classes="modal-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(Group(*rows), id="help-content")
            yield Static(
                "Type / to filter commands  ·  Esc close", classes="modal-hint"
            )

    def action_close(self) -> None:
        self.dismiss(None)


class PCBDraftApp(App[int]):
    """Coding-agent-style terminal client over :class:`TuiController`."""

    CSS_PATH = "tui.tcss"
    TITLE = "PCBDraft"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = (
        Binding("ctrl+n", "new_project", "New"),
        Binding("ctrl+p", "projects", "Projects"),
        Binding("ctrl+r", "review", "Review"),
        Binding("ctrl+l", "toggle_logs", "Details"),
        Binding("f5", "refresh_project", "Refresh", show=False),
        Binding("ctrl+q", "quit_or_stop", "Quit"),
        Binding("f1", "help", "Help", show=False),
    )

    def __init__(self, controller: Any) -> None:
        super().__init__()
        self.controller = controller
        self._palette_dismissed = False
        self._palette_names: tuple[str, ...] = ()
        self._transcript_signature: tuple[Any, ...] | None = None

    def compose(self) -> ComposeResult:
        yield AgentHeader(id="agent-header")
        with Horizontal(id="main-area"):
            yield TranscriptView(id="transcript")
            yield ProjectRail(id="project-rail")
        yield NoticeBar(id="notice-bar")
        yield CommandPalette(id="command-palette")
        yield Composer(id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#command-palette", CommandPalette).styles.display = "none"
        self._sync_ui(force_transcript=True)
        self.query_one("#composer-input", Input).focus()
        self.set_interval(0.1, self._poll_controller, name="agent-events")
        self._set_responsive_layout(self.size.width)

    def on_resize(self, event: Resize) -> None:
        self._set_responsive_layout(event.size.width)

    def on_key(self, event: Key) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        palette = self.query_one("#command-palette", CommandPalette)
        if self._palette_visible():
            if event.key == "up":
                palette.action_cursor_up()
                event.prevent_default()
                event.stop()
                return
            if event.key == "down":
                palette.action_cursor_down()
                event.prevent_default()
                event.stop()
                return
            if event.key == "tab":
                self._complete_palette_command()
                event.prevent_default()
                event.stop()
                return
        if event.key == "escape":
            if self._palette_visible():
                self._palette_dismissed = True
                palette.styles.display = "none"
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
        self._palette_dismissed = False
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
        commands = command_suggestions(text)
        palette = self.query_one("#command-palette", CommandPalette)
        names = tuple(command.name for command in commands)
        visible = (
            bool(commands) and text.startswith("/") and not self._palette_dismissed
        )
        if names != self._palette_names:
            palette.set_commands(commands)
            self._palette_names = names
        palette.styles.display = "block" if visible else "none"

    def _palette_visible(self) -> bool:
        return (
            self.query_one("#command-palette", CommandPalette).styles.display != "none"
        )

    def _hide_palette(self) -> None:
        self._palette_dismissed = True
        self.query_one("#command-palette", CommandPalette).styles.display = "none"

    def _complete_palette_command(self) -> None:
        command = self.query_one("#command-palette", CommandPalette).selected_command()
        if command is not None:
            self._set_composer_command(command)

    def _set_composer_command(self, command: SlashCommand) -> None:
        composer = self.query_one("#composer-input", Input)
        composer.value = command.name + (" " if command.accepts_argument else "")
        composer.cursor_position = len(composer.value)
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
        rail.styles.display = "none" if width < 100 else "block"
