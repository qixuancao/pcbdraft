from __future__ import annotations

import unittest

from pcbdraft.ir import Design, Scope
from pcbdraft.scope import evaluate_scope
from tests.design_factory import minimal_design_dict


class AcceptanceScopeTests(unittest.TestCase):
    def test_low_voltage_two_layer_control_is_accepted(self) -> None:
        design = Design.from_dict(minimal_design_dict())
        self.assertTrue(evaluate_scope(design.scope).accepted)

    def test_six_layer_stackup_is_accepted_for_native_generation(self) -> None:
        value = minimal_design_dict()
        value["scope"]["layers"] = 6
        value["board"]["layers"] = 6
        decision = evaluate_scope(Design.from_dict(value).scope)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reasons, ())

    def test_complex_domains_are_warned_about_without_blocking_generation(self) -> None:
        value = minimal_design_dict()["scope"]
        value["domains"] = ["aviation", "high_power", "mains", "medical", "rf"]
        value["max_voltage_v"] = 325
        value["max_current_a"] = 20
        value["max_power_w"] = 6500
        value["risk_class"] = "safety_critical"
        decision = evaluate_scope(Scope.from_dict(value))
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reasons, ())
        joined = " ".join(decision.warnings)
        for domain in ("aviation", "high_power", "mains", "medical", "rf"):
            self.assertIn(domain, joined)
        self.assertIn("does not validate", joined)


if __name__ == "__main__":
    unittest.main()
