from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcbdraft.errors import ValidationError
from pcbdraft.patching import apply_operations, regression_reasons


def operation(old: str, new: str) -> dict[str, str]:
    return {
        "op": "replace_text",
        "relative_path": "demo.kicad_pcb",
        "old_text": old,
        "new_text": new,
        "reason": "test",
    }


class ReplaceTextTests(unittest.TestCase):
    def test_unique_replace_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "demo.kicad_pcb"
            target.write_text("before OLD after", encoding="utf-8")
            applied = apply_operations(root, [operation("OLD", "NEW")])
            self.assertEqual(target.read_text(encoding="utf-8"), "before NEW after")
            self.assertEqual(applied[0].relative_path, "demo.kicad_pcb")

    def test_non_unique_replace_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "demo.kicad_pcb"
            target.write_text("OLD and OLD", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "exactly once"):
                apply_operations(root, [operation("OLD", "NEW")])
            self.assertEqual(target.read_text(encoding="utf-8"), "OLD and OLD")

    def test_later_invalid_operation_does_not_partially_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "demo.kicad_pcb"
            target.write_text("OLD", encoding="utf-8")
            with self.assertRaises(ValidationError):
                apply_operations(
                    root, [operation("OLD", "NEW"), operation("MISSING", "X")]
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "OLD")


class RegressionPolicyTests(unittest.TestCase):
    def test_error_increase_and_tool_failure_reject(self) -> None:
        baseline = {
            "erc": {"tool_status": "ok", "counts": {"error": 1, "warning": 0}},
            "drc": {"tool_status": "ok", "counts": {"error": 0, "warning": 2}},
        }
        after = {
            "erc": {"tool_status": "ok", "counts": {"error": 2, "warning": 0}},
            "drc": {
                "tool_status": "tool_failed",
                "counts": {"error": None, "warning": None},
            },
        }
        reasons = regression_reasons(baseline, after)
        self.assertTrue(any("error count increased" in reason for reason in reasons))
        self.assertTrue(any("runnable to tool failure" in reason for reason in reasons))

    def test_warning_increase_alone_is_allowed(self) -> None:
        baseline = {
            gate: {"tool_status": "ok", "counts": {"error": 0, "warning": 0}}
            for gate in ("erc", "drc")
        }
        after = {
            gate: {"tool_status": "ok", "counts": {"error": 0, "warning": 50}}
            for gate in ("erc", "drc")
        }
        self.assertEqual(regression_reasons(baseline, after), [])


if __name__ == "__main__":
    unittest.main()
