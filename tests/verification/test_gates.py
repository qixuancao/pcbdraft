from __future__ import annotations

import unittest

from pcbdraft.verification.gates import count_severities, structured_violations


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

    def test_full_counts_are_independent_of_bounded_display(self) -> None:
        document = {
            "violations": [
                {"severity": "error", "type": f"clearance-{index}"}
                for index in range(101)
            ]
        }

        structured = structured_violations(document)

        self.assertEqual(count_severities(document), (101, 0))
        self.assertEqual(len(structured["violations"]), 100)
        self.assertEqual(structured["violation_count_seen"], 101)
        self.assertTrue(structured["violations_truncated"])


if __name__ == "__main__":
    unittest.main()
