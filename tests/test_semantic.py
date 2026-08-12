from __future__ import annotations

import json
import unittest

from pcb_agent.semantic import PROMPT_CONTEXT_LIMIT, _fit_prompt_context


class SemanticContextTests(unittest.TestCase):
    def test_large_context_is_bounded_without_looping(self) -> None:
        context = {
            "exports": {"schematic_netlist": {"available": True}},
            "schematic": {
                "available": True,
                "data": {
                    "nets": [{"name": "N" * 512}] * 5_000,
                    "components": [{"reference": "R" * 512}] * 5_000,
                },
            },
            "board_connectivity": {
                "available": True,
                "data": {"records": ["X" * 512] * 5_000},
            },
            "board_statistics": {"available": True, "data": {"width": "10 mm"}},
        }

        result = _fit_prompt_context(context)

        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.assertLessEqual(len(encoded), PROMPT_CONTEXT_LIMIT)
        self.assertTrue(
            result["schematic"]["data"].get("nets_truncated_for_prompt")
            or result["board_connectivity"]["data"].get("records_truncated_for_prompt")
        )


if __name__ == "__main__":
    unittest.main()
