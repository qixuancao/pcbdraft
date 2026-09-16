"""Compatibility and import-boundary checks for the terminal implementation split."""

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.interfaces.tui import app, legacy_app

_TUI_ROOT = Path(__file__).resolve().parents[2] / "src/pcbdraft/interfaces/tui"


def _tui_app_imports(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "pcbdraft.interfaces.tui.app" or (
                node.level > 0 and node.module == "app"
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import) and any(
            alias.name == "pcbdraft.interfaces.tui.app" for alias in node.names
        ):
            lines.append(node.lineno)
    return lines


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

    def test_facade_is_the_only_tui_module_allowed_to_expose_legacy_app(self) -> None:
        facade_tree = ast.parse(
            (_TUI_ROOT / "app.py").read_text(encoding="utf-8"),
            filename=str(_TUI_ROOT / "app.py"),
        )
        local_imports = {
            alias.name
            for node in ast.walk(facade_tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "pcbdraft.interfaces.tui"
            for alias in node.names
        }
        self.assertEqual(local_imports, {"legacy_app"})

        reverse_imports = {
            str(path.relative_to(_TUI_ROOT)): _tui_app_imports(path)
            for path in _TUI_ROOT.rglob("*.py")
            if path.name != "app.py" and _tui_app_imports(path)
        }
        self.assertEqual(reverse_imports, {})
