"""Focused coverage for the PCBDraft native TUI completion surface."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from prompt_toolkit.auto_suggest import Suggestion
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from pcbdraft.interfaces.tui.commands import (
    COMMANDS,
    SlashCommandAutoSuggest,
    SlashCommandCompleter,
    resolve_command,
)


def _complete(completer: SlashCommandCompleter, text: str):
    return list(completer.get_completions(Document(text), CompleteEvent()))


class SlashCommandCompletionTests(unittest.TestCase):
    def test_root_completion_contains_only_registered_commands(self) -> None:
        provider_calls: list[str] = []

        def skill_commands():
            provider_calls.append("skill")
            return {"/unused-skill": {"description": "legacy skill"}}

        def skill_bundles():
            provider_calls.append("bundle")
            return {"/unused-bundle": {"description": "legacy bundle"}}

        completions = _complete(
            SlashCommandCompleter(
                skill_commands_provider=skill_commands,
                skill_bundles_provider=skill_bundles,
            ),
            "/",
        )

        self.assertEqual(
            {completion.text for completion in completions},
            {command.lstrip("/") for command in COMMANDS},
        )
        self.assertTrue(
            all(
                resolve_command(completion.text) is not None
                for completion in completions
            )
        )
        self.assertEqual(provider_calls, [])

    def test_removed_hermes_commands_have_no_dynamic_completions(self) -> None:
        completer = SlashCommandCompleter()

        for text in ("/skin ", "/personality ", "/tools ", "/handoff "):
            with self.subTest(text=text):
                self.assertEqual(_complete(completer, text), [])

    def test_command_filter_applies_to_completion_and_auto_suggest(self) -> None:
        class FakeHistory:
            def get_suggestion(self, buffer, document):
                return Suggestion(" from history")

        completer = SlashCommandCompleter(
            command_filter=lambda command: command != "/release"
        )

        self.assertEqual(_complete(completer, "/rel"), [])
        auto_suggest = SlashCommandAutoSuggest(
            history_suggest=FakeHistory(), completer=completer
        )
        self.assertIsNone(auto_suggest.get_suggestion(None, Document("/rel")))
        self.assertIsNone(
            auto_suggest.get_suggestion(None, Document("/release candidate"))
        )

    def test_auto_suggest_does_not_revive_removed_commands_from_history(self) -> None:
        class FakeHistory:
            def get_suggestion(self, buffer, document):
                return Suggestion(" from history")

        auto_suggest = SlashCommandAutoSuggest(history_suggest=FakeHistory())

        self.assertIsNone(auto_suggest.get_suggestion(None, Document("/skin d")))
        self.assertEqual(
            auto_suggest.get_suggestion(None, Document("board request")).text,
            " from history",
        )

    def test_context_and_path_completions_remain_available(self) -> None:
        completer = SlashCommandCompleter()
        context_texts = {completion.text for completion in _complete(completer, "@")}
        self.assertIn("@file:", context_texts)
        self.assertIn("@folder:", context_texts)

        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            os.chdir(temporary)
            try:
                Path("board.kicad_pcb").touch()
                path_texts = {
                    completion.text for completion in _complete(completer, "./boa")
                }
            finally:
                os.chdir(previous_cwd)
        self.assertIn("board.kicad_pcb", path_texts)

    def test_model_help_matches_persistent_pcbdraft_behavior(self) -> None:
        model = resolve_command("model")

        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.description, "Switch the persistent PCBDraft model")
        self.assertNotIn("--session", model.args_hint)
        self.assertNotIn("--once", model.args_hint)


if __name__ == "__main__":
    unittest.main()
