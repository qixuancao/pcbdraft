from __future__ import annotations

import unittest

from pcb_agent.ir import Design, Scope
from pcb_agent.scope import evaluate_scope
from tests.design_factory import minimal_design_dict


class AcceptanceScopeTests(unittest.TestCase):
    def test_low_voltage_two_layer_control_is_accepted(self) -> None:
        design = Design.from_dict(minimal_design_dict())
        self.assertTrue(evaluate_scope(design.scope).accepted)

    def test_mains_rf_and_safety_critical_are_explicitly_rejected(self) -> None:
        value = minimal_design_dict()["scope"]
        value["domains"] = ["mains", "rf"]
        value["max_voltage_v"] = 325
        value["layers"] = 8
        value["risk_class"] = "safety_critical"
        decision = evaluate_scope(Scope.from_dict(value))
        self.assertFalse(decision.accepted)
        joined = " ".join(decision.reasons)
        self.assertIn("high-risk", joined)
        self.assertIn("60 VDC", joined)
        self.assertIn("2-4", joined)


if __name__ == "__main__":
    unittest.main()
