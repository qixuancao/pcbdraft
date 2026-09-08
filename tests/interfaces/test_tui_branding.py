from __future__ import annotations

import io
import os
import re
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from rich.console import Console

import pcbdraft.interfaces.tui.app as tui_app
from pcbdraft.interfaces.tui import banner, skin_engine, tips
from pcbdraft.interfaces.tui.commands import COMMAND_REGISTRY


def _registered_slash_commands() -> set[str]:
    return {
        f"/{name}"
        for command in COMMAND_REGISTRY
        if not command.gateway_only
        for name in (command.name, *command.aliases)
    }


class TUIBrandingTests(unittest.TestCase):
    def test_random_tips_only_advertise_registered_commands(self) -> None:
        registered = _registered_slash_commands()
        advertised = {
            f"/{name}"
            for tip in tips.TIPS
            for name in re.findall(r"/([a-z][a-z0-9-]*)", tip)
        }

        self.assertTrue(advertised)
        self.assertLessEqual(advertised, registered)
        self.assertNotIn("hermes", "\n".join(tips.TIPS).lower())
        self.assertNotIn("gateway", "\n".join(tips.TIPS).lower())
        self.assertIn(tips.get_random_tip(), tips.TIPS)

    def test_help_is_derived_only_from_closed_command_registry(self) -> None:
        rendered: list[str] = []

        class CaptureConsole:
            @staticmethod
            def print(value: object = "", *args, **kwargs) -> None:
                del args, kwargs
                rendered.append(str(value))

        cli = tui_app.TerminalApp.__new__(tui_app.TerminalApp)
        with (
            patch.object(
                tui_app,
                "_cprint",
                side_effect=lambda value="": rendered.append(str(value)),
            ),
            patch.object(tui_app, "ChatConsole", return_value=CaptureConsole()),
        ):
            cli.show_help()

        output = "\n".join(rendered)
        for command in _registered_slash_commands():
            self.assertIn(command, output)
        for residue in (
            "Hermes",
            "Skill Commands",
            "Skill Bundles",
            "Quick Commands",
            "/skin",
            "/image",
            "/paste",
        ):
            self.assertNotIn(residue, output)

    def test_banner_renders_pcbdraft_without_product_promotions(self) -> None:
        output = io.StringIO()
        console = Console(file=output, width=120, color_system=None)
        requested_skin_keys: list[str] = []
        empty_availability = {
            "unavailable_toolsets": [],
            "lazy_tools": [],
            "disabled_tools": [],
        }

        def skin_color(key: str, fallback: str) -> str:
            requested_skin_keys.append(key)
            return fallback

        with (
            patch.object(banner, "_skin_color", side_effect=skin_color),
            patch.object(
                banner.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((120, 40)),
            ),
            patch("pcbdraft.model.configuration.load_config", return_value={}),
            patch(
                "pcbdraft.agent.extensions.manager.get_portable_mcp_server_names_nowait",
                return_value=[],
            ),
            patch.object(
                banner,
                "get_latest_release_tag",
                side_effect=AssertionError("banner must not query upstream releases"),
            ) as release_lookup,
            patch.object(
                banner,
                "get_update_result",
                side_effect=AssertionError(
                    "banner must not advertise standalone updates"
                ),
            ) as update_lookup,
        ):
            banner.build_welcome_banner(
                console=console,
                model="Nous-Hermes-4",
                cwd="/workspace/board",
                tools=[],
                enabled_toolsets=[],
                session_id="session-1",
                get_toolset_for_tool=lambda _name: None,
                availability=empty_availability,
            )

        release_lookup.assert_not_called()
        update_lookup.assert_not_called()
        rendered = output.getvalue()
        self.assertIn("PCBDraft", rendered)
        self.assertIn("Nous-Hermes-4", rendered)
        self.assertIn("/help for commands", rendered)
        self.assertNotIn("banner_text", requested_skin_keys)
        for residue in (
            "Available Skills",
            "Nous Research",
            "update available",
            "hermes setup",
            "Ares Agent",
        ):
            self.assertNotIn(residue, rendered)

    def test_builtin_skins_keep_theme_but_not_product_identity(self) -> None:
        for name, definition in skin_engine._BUILTIN_SKINS.items():
            with self.subTest(skin=name):
                branding = definition["branding"]
                self.assertEqual(branding["agent_name"], "PCBDraft")
                self.assertEqual(branding["response_label"].strip(), "PCBDraft")
                self.assertIn("PCBDraft", branding["welcome"])
                self.assertIn("PCBDraft", branding["goodbye"])
                self.assertEqual(branding["help_header"], "PCBDraft Commands")

    def test_exit_summary_does_not_advertise_upstream_resume_commands(self) -> None:
        cli = tui_app.TerminalApp.__new__(tui_app.TerminalApp)
        cli.conversation_history = [
            {"role": "user", "content": "draft a board"},
            {"role": "assistant", "content": "ready"},
        ]
        cli.session_start = datetime.now(UTC).replace(tzinfo=None)
        cli.session_id = "session-1"
        cli._session_db = None

        with patch("builtins.print") as rendered:
            cli._print_exit_summary(clear_screen=False)

        output = "\n".join(
            " ".join(map(str, call.args)) for call in rendered.call_args_list
        )
        self.assertIn("Session:        session-1", output)
        self.assertNotIn("Resume this session", output)
        self.assertNotIn("hermes", output.lower())

    def test_transparent_modal_body_styles_inherit_terminal_foreground(self) -> None:
        skin = skin_engine._build_skin_config(skin_engine._BUILTIN_SKINS["default"])
        with patch.object(skin_engine, "get_active_skin", return_value=skin):
            styles = skin_engine.get_prompt_toolkit_style_overrides()

        self.assertEqual(styles["clarify-question"], "bold")
        self.assertEqual(styles["sudo-text"], "")
        self.assertEqual(styles["approval-desc"], "bold")


if __name__ == "__main__":
    unittest.main()
