from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.model.config import ModelConfig
from pcbdraft.services.doctor import doctor_report, setup_runtime


class DoctorTests(unittest.TestCase):
    def ready_report(self, fake_check):
        tables = {
            "sym-lib-table": {"configured": True},
            "fp-lib-table": {"configured": True},
        }
        with (
            patch("pcbdraft.services.doctor._check", side_effect=fake_check),
            patch(
                "pcbdraft.services.doctor.kicad_data_directory",
                return_value=Path.cwd(),
            ),
            patch(
                "pcbdraft.services.doctor.library_table_status",
                return_value=tables,
            ),
            patch(
                "pcbdraft.services.doctor.load_model_config",
                return_value=ModelConfig(
                    active_provider=None,
                    active_model=None,
                    providers=(),
                    path=Path("/tmp/pcbdraft-doctor-test/config.toml"),
                ),
            ),
        ):
            return doctor_report()

    def test_model_is_optional_for_deterministic_core(self) -> None:
        def fake_check(name: str, _args: list[str]) -> dict[str, object]:
            version = "10.0.5" if name == "kicad-cli" else "git version 2.50.0"
            return {"available": True, "version": version}

        report = self.ready_report(fake_check)
        self.assertTrue(report["ok"])
        self.assertTrue(report["core_ok"])
        self.assertFalse(report["model_available"])

    def test_unsupported_kicad_fails_core_profile(self) -> None:
        def fake_check(name: str, _args: list[str]) -> dict[str, object]:
            version = "11.0.0" if name == "kicad-cli" else f"{name} 1.0"
            return {"available": True, "version": version}

        report = self.ready_report(fake_check)
        self.assertFalse(report["ok"])
        self.assertFalse(report["core_ok"])

    def test_git_is_optional_after_package_installation(self) -> None:
        def fake_check(name: str, _args: list[str]) -> dict[str, object]:
            if name == "git":
                return {"available": False, "version": None}
            return {"available": True, "version": "10.0.4"}

        report = self.ready_report(fake_check)
        self.assertTrue(report["core_ok"])
        self.assertFalse(report["tools"]["git"]["available"])

    def test_missing_stock_libraries_fail_readiness_but_not_core_probe(self) -> None:
        def fake_check(name: str, _args: list[str]) -> dict[str, object]:
            version = "10.0.4" if name == "kicad-cli" else f"{name} 1.0"
            return {"available": True, "version": version}

        tables = {
            "sym-lib-table": {"configured": False},
            "fp-lib-table": {"configured": False},
        }
        with (
            patch("pcbdraft.services.doctor._check", side_effect=fake_check),
            patch(
                "pcbdraft.services.doctor.kicad_data_directory",
                return_value=Path("missing-kicad-data"),
            ),
            patch(
                "pcbdraft.services.doctor.library_table_status",
                return_value=tables,
            ),
        ):
            report = doctor_report()

        self.assertTrue(report["core_ok"])
        self.assertFalse(report["ok"])

    def test_setup_rejects_missing_bundled_pcbnew_runtime(self) -> None:
        report = {
            "tools": {
                "kicad-cli": {
                    "available": True,
                    "support": {"supported": True},
                },
                "pcbnew-python": {"available": False},
            },
            "library_data": {},
        }
        with (
            patch("pcbdraft.services.doctor.doctor_report", return_value=report),
            self.assertRaisesRegex(PCBDraftError, "pcbnew Python"),
        ):
            setup_runtime()


if __name__ == "__main__":
    unittest.main()
