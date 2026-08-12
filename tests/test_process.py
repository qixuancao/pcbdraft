from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from pcb_agent.process import run_command


class BoundedProcessTests(unittest.TestCase):
    def test_timeout_kills_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_command(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                cwd=Path(temporary),
                timeout=0.1,
                max_output_bytes=1024,
            )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.output_limited)
        self.assertLess(result.duration_seconds, 2.0)

    def test_combined_output_limit_kills_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_command(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 1000000); sys.stdout.flush()",
                ],
                cwd=Path(temporary),
                timeout=5,
                max_output_bytes=1024,
            )
        self.assertTrue(result.output_limited)
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 1024)


if __name__ == "__main__":
    unittest.main()
