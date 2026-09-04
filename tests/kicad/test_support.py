from __future__ import annotations

import unittest
from types import SimpleNamespace

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.kicad.support import (
    assert_supported_kicad_version,
    evaluate_kicad_version,
    probe_kicad_capabilities,
)


class KiCadSupportTests(unittest.TestCase):
    def test_tested_and_distribution_suffixed_versions_are_supported(self) -> None:
        exact = evaluate_kicad_version("10.0.5")
        self.assertTrue(exact.supported)
        self.assertTrue(exact.exact_tested)
        packaged = assert_supported_kicad_version("10.0.5-10.0.5~ubuntu26.04.1")
        self.assertEqual(packaged.parsed_version, "10.0.5")
        self.assertTrue(packaged.exact_tested)

    def test_other_series_prereleases_and_unparseable_versions_fail_closed(
        self,
    ) -> None:
        for version in (
            "9.0.6",
            "10.0.6-rc1",
            "nightly KiCad 10.0.6",
            "10.1.0",
            "11.0.0",
            "unknown",
        ):
            with self.subTest(version=version), self.assertRaises(PCBDraftError):
                assert_supported_kicad_version(version)

    def test_stable_patch_in_compatible_series_runs_with_disclosed_warning(
        self,
    ) -> None:
        result = evaluate_kicad_version("KiCad 10.0.4")
        self.assertTrue(result.supported)
        self.assertFalse(result.exact_tested)
        self.assertIn("compatible", result.reason or "")
        accepted = assert_supported_kicad_version("KiCad 10.0.99")
        self.assertEqual(accepted.parsed_version, "10.0.99")

    def test_capabilities_are_probed_independently(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(tuple(argv[1:]))
            output = {
                ("pcb", "export", "svg", "--help"): "--fit-page-to-board --mode-single",
                ("pcb", "render", "--help"): "--quality --side",
                ("sch", "erc", "--help"): "--format --exit-code-violations",
                ("pcb", "drc", "--help"): "--format",
            }[tuple(argv[1:])]
            return SimpleNamespace(
                stdout=output,
                stderr="",
                returncode=0,
                timed_out=False,
                output_limited=False,
            )

        capabilities = probe_kicad_capabilities("/opt/kicad-cli", runner=runner)
        self.assertTrue(capabilities["pcb_export_svg"]["available"])
        self.assertTrue(capabilities["pcb_render_3d"]["available"])
        self.assertTrue(capabilities["erc_json"]["available"])
        self.assertFalse(capabilities["drc_json"]["available"])
        self.assertEqual(len(calls), 4)

    def test_missing_cli_degrades_each_capability_without_launching(self) -> None:
        capabilities = probe_kicad_capabilities(None)
        self.assertTrue(capabilities)
        self.assertTrue(all(not row["available"] for row in capabilities.values()))


if __name__ == "__main__":
    unittest.main()
