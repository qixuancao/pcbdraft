"""Compatibility checks for the staged terminal implementation split."""

import unittest
from unittest.mock import patch

from pcbdraft.interfaces.tui import app, legacy_app


class TuiAppFacadeTests(unittest.TestCase):
    def test_exports_legacy_entry_points_and_rendering_helper(self) -> None:
        self.assertIs(app.TerminalApp, legacy_app.TerminalApp)
        self.assertIs(app.main, legacy_app.main)
        self.assertIs(app._rich_text_from_ansi, legacy_app._rich_text_from_ansi)

    def test_forwards_monkeypatches_to_legacy_globals(self) -> None:
        original = legacy_app._cprint
        replacement = object()

        with patch.object(app, "_cprint", replacement):
            self.assertIs(legacy_app._cprint, replacement)

        self.assertIs(legacy_app._cprint, original)
        self.assertIs(app._cprint, original)
