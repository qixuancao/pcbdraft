"""Focused diagnostics checks for bounded pcbnew worker timeouts."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.kicad.pcb import WORKER_OUTPUT_LIMIT, _run_worker


class WorkerTimeoutDiagnosticsTests(unittest.TestCase):
    def test_timeout_reports_mode_and_configured_timeout_without_details(self) -> None:
        private_path = "/private/pcb-project/board.kicad_pcb"
        worker_result = SimpleNamespace(
            timed_out=True,
            output_limited=False,
            returncode=-9,
            stdout=b"secret worker stdout",
            stderr=b"secret worker stderr",
        )

        with (
            patch(
                "pcbdraft.kicad.pcb._system_python",
                return_value=Path("/fake/system-python"),
            ),
            patch(
                "pcbdraft.kicad.pcb.run_command",
                return_value=worker_result,
            ) as run_command,
            self.assertRaises(PCBDraftError) as raised,
        ):
            _run_worker(
                "inspect_board",
                {"board_path": private_path},
                Path(private_path),
                system_python=None,
                timeout=12.5,
            )

        message = str(raised.exception)
        self.assertEqual(
            message,
            "isolated pcbnew worker timed out (mode=inspect_board, timeout=12.5s)",
        )
        self.assertIn("isolated pcbnew worker timed out", message)
        self.assertNotIn(private_path, message)
        self.assertNotIn("secret worker", message)
        run_command.assert_called_once()
        self.assertEqual(run_command.call_args.kwargs["timeout"], 12.5)
        self.assertEqual(
            run_command.call_args.kwargs["max_output_bytes"], WORKER_OUTPUT_LIMIT
        )


if __name__ == "__main__":
    unittest.main()
