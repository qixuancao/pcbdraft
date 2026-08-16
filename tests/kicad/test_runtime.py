from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.kicad.runtime import (
    ensure_kicad_library_tables,
    find_kicad_cli,
    kicad_data_directory,
    kicad_user_config_directory,
    library_table_status,
)


class KiCadRuntimeTests(unittest.TestCase):
    def test_documented_user_config_paths_are_platform_specific(self) -> None:
        home = Path("/users/alex")
        self.assertEqual(
            kicad_user_config_directory(system="Linux", environment={}, home=home),
            home / ".config" / "kicad" / "10.0",
        )
        self.assertEqual(
            kicad_user_config_directory(system="Darwin", environment={}, home=home),
            home / "Library" / "Preferences" / "kicad" / "10.0",
        )
        self.assertEqual(
            kicad_user_config_directory(
                system="Windows",
                environment={"APPDATA": "C:/Users/Alex/AppData/Roaming"},
                home=home,
            ),
            Path("C:/Users/Alex/AppData/Roaming/kicad/10.0"),
        )

    def test_environment_overrides_desktop_installer_path_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "kicad-cli"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            symbols = root / "symbols"
            symbols.mkdir()
            environment = {
                "KICAD_CLI": str(executable),
                "KICAD10_SYMBOL_DIR": str(symbols),
            }
            self.assertEqual(
                find_kicad_cli(system="Linux", environment=environment),
                str(executable.resolve()),
            )
            self.assertEqual(
                kicad_data_directory(
                    "symbols", system="Linux", environment=environment
                ),
                symbols,
            )

    def test_missing_library_tables_are_initialized_but_invalid_ones_survive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            templates = root / "templates"
            templates.mkdir()
            (templates / "sym-lib-table").write_text(
                "(sym_lib_table)\n", encoding="utf-8"
            )
            (templates / "fp-lib-table").write_text(
                "(fp_lib_table)\n", encoding="utf-8"
            )
            home = root / "home"
            environment = {"KICAD_TEMPLATE_DIR": str(templates)}
            ready = ensure_kicad_library_tables(
                system="Linux", environment=environment, home=home
            )
            self.assertTrue(all(item["configured"] for item in ready.values()))
            config = home / ".config" / "kicad" / "10.0"
            (config / "sym-lib-table").write_text("broken\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "was not replaced"):
                ensure_kicad_library_tables(
                    system="Linux", environment=environment, home=home
                )
            self.assertEqual(
                (config / "sym-lib-table").read_text(encoding="utf-8"), "broken\n"
            )
            status = library_table_status(
                system="Linux", environment=environment, home=home
            )
            self.assertFalse(status["sym-lib-table"]["configured"])

    def test_unknown_data_kind_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            kicad_data_directory("models")


if __name__ == "__main__":
    unittest.main()
