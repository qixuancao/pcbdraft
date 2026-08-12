from __future__ import annotations

import unittest

from pcb_agent.gates import count_severities


class GateJsonTests(unittest.TestCase):
    def test_counts_nested_erc_and_drc_shapes(self) -> None:
        document = {
            "sheets": [{"violations": [{"severity": "error"}, {"severity": "warning"}]}],
            "violations": [{"severity": "warning"}],
            "unconnected_items": [],
        }
        self.assertEqual(count_severities(document), (1, 2))

    def test_aggregate_fallback(self) -> None:
        self.assertEqual(count_severities({"errors": 3, "warnings": [{}, {}]}), (3, 2))


if __name__ == "__main__":
    unittest.main()

