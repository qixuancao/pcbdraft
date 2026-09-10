from __future__ import annotations

import unittest
from unittest.mock import patch

import pcbdraft.interfaces.tui.app as tui_app
from pcbdraft.interfaces.tui import skin_engine


class AssistantResponseColorTests(unittest.TestCase):
    def test_streamed_body_inherits_terminal_foreground(self) -> None:
        class LightUnreadableSkin:
            @staticmethod
            def get_branding(_key: str, _fallback: str = "") -> str:
                return " PCBDraft "

            @staticmethod
            def get_color(_key: str, _fallback: str = "") -> str:
                raise AssertionError("streamed body must not read a skin foreground")

        cli = tui_app.TerminalApp.__new__(tui_app.TerminalApp)
        cli.show_reasoning = False
        cli.show_timestamps = False
        cli.final_response_markdown = "strip"
        cli._reasoning_box_opened = False
        cli._stream_box_opened = False
        cli._stream_buf = ""
        cli._stream_table_buf = []
        cli._in_stream_table = False
        cli._close_reasoning_box = lambda: None
        cli._scrollback_box_width = lambda: 48

        border_ansi = "\x1b[38;2;18;52;86m"
        with (
            patch.object(
                skin_engine, "get_active_skin", return_value=LightUnreadableSkin()
            ),
            patch.object(tui_app, "_ACCENT", border_ansi),
            patch.object(tui_app, "_cprint") as rendered,
        ):
            cli._emit_stream_text("visible response\n")

        self.assertIn(border_ansi, rendered.call_args_list[0].args[0])
        self.assertEqual(rendered.call_args_list[1].args[0], "visible response")
        self.assertEqual(cli._stream_text_ansi, "")

    def test_non_streamed_body_inherits_terminal_foreground(self) -> None:
        panel = tui_app._build_final_assistant_panel(
            "visible response",
            label=" PCBDraft ",
            border_color="#123456",
            markdown_mode="strip",
            width=48,
        )

        self.assertEqual(str(panel.style), "none")
        self.assertEqual(str(panel.border_style), "#123456")
        self.assertIn("#123456 bold", str(panel.title))


if __name__ == "__main__":
    unittest.main()
