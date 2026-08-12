from __future__ import annotations

import unittest

from pcb_agent.compatibility import (
    assert_supported_kicad_version,
    evaluate_kicad_version,
)
from pcb_agent.errors import PcbAgentError


class KiCadCompatibilityTests(unittest.TestCase):
    def test_tested_and_distribution_suffixed_versions_are_supported(self) -> None:
        exact = evaluate_kicad_version("10.0.5")
        self.assertTrue(exact.supported)
        self.assertTrue(exact.exact_tested)
        packaged = assert_supported_kicad_version("10.0.5-10.0.5~ubuntu26.04.1")
        self.assertEqual(packaged.parsed_version, "10.0.5")
        self.assertTrue(packaged.exact_tested)

    def test_other_majors_and_unparseable_versions_fail_closed(self) -> None:
        for version in ("9.0.6", "11.0.0-rc1", "unknown"):
            with self.subTest(version=version), self.assertRaises(PcbAgentError):
                assert_supported_kicad_version(version)

    def test_untested_patch_in_supported_major_is_declared_not_exact(self) -> None:
        result = assert_supported_kicad_version("KiCad 10.1.0")
        self.assertTrue(result.supported)
        self.assertFalse(result.exact_tested)


if __name__ == "__main__":
    unittest.main()
