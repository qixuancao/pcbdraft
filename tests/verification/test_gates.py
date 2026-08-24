from __future__ import annotations

import unittest

from pcbdraft.verification.gates import _error_fingerprints, count_severities


class GateJsonTests(unittest.TestCase):
    def test_counts_nested_erc_and_drc_shapes(self) -> None:
        document = {
            "sheets": [
                {"violations": [{"severity": "error"}, {"severity": "warning"}]}
            ],
            "violations": [{"severity": "warning"}],
            "unconnected_items": [],
        }
        self.assertEqual(count_severities(document), (1, 2))

    def test_aggregate_fallback(self) -> None:
        self.assertEqual(count_severities({"errors": 3, "warnings": [{}, {}]}), (3, 2))

    def test_error_identity_ignores_moved_coordinates_but_keeps_native_items(
        self,
    ) -> None:
        def violation(*, second_uuid: str, x_mm: float) -> dict[str, object]:
            return {
                "severity": "error",
                "type": "clearance",
                "description": f"clearance near x={x_mm}",
                "json_location": f"$.violations[{x_mm}]",
                "items": [
                    {
                        "uuid": "track-stable",
                        "description": f"track at {x_mm}",
                        "pos": {"x": x_mm, "y": 2.0},
                    },
                    {
                        "uuid": second_uuid,
                        "description": f"footprint at {x_mm}",
                        "pos": {"x": x_mm, "y": 3.0},
                    },
                ],
            }

        before = {"violations": [violation(second_uuid="fp-stable", x_mm=1.0)]}
        moved = {"violations": [violation(second_uuid="fp-stable", x_mm=8.0)]}
        genuinely_new = {
            "violations": [violation(second_uuid="fp-different", x_mm=8.0)]
        }

        self.assertEqual(_error_fingerprints(before), _error_fingerprints(moved))
        self.assertNotEqual(
            _error_fingerprints(before), _error_fingerprints(genuinely_new)
        )


if __name__ == "__main__":
    unittest.main()
