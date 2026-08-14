from __future__ import annotations

import unittest
from unittest.mock import patch

from pcbdraft.doctor import doctor_report


class DoctorTests(unittest.TestCase):
    def test_model_is_optional_for_deterministic_core(self) -> None:
        def fake_check(name: str, _args: list[str]) -> dict[str, object]:
            version = "10.0.5" if name == "kicad-cli" else "git version 2.50.0"
            return {"available": True, "version": version}

        with patch("pcbdraft.doctor._check", side_effect=fake_check):
            report = doctor_report()
        self.assertTrue(report["ok"])
        self.assertTrue(report["core_ok"])
        self.assertFalse(report["model_available"])

    def test_unsupported_kicad_fails_core_profile(self) -> None:
        def fake_check(name: str, _args: list[str]) -> dict[str, object]:
            version = "11.0.0" if name == "kicad-cli" else f"{name} 1.0"
            return {"available": True, "version": version}

        with patch("pcbdraft.doctor._check", side_effect=fake_check):
            report = doctor_report()
        self.assertFalse(report["ok"])
        self.assertFalse(report["core_ok"])


if __name__ == "__main__":
    unittest.main()
