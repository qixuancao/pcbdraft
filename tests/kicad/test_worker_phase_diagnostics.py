"""Focused phase-marker diagnostics checks for the isolated pcbnew worker."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.kicad.pcb import (
    WORKER_PHASE_LINES,
    WORKER_PHASE_PREFIX,
    _last_worker_phase,
    _run_worker,
)


class WorkerPhaseDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def _worker_path() -> Path:
        return (
            Path(__file__).resolve().parents[2]
            / "src"
            / "pcbdraft"
            / "kicad"
            / "pcbnew_worker.py"
        )

    def _load_worker(self):
        specification = importlib.util.spec_from_file_location(
            "pcbdraft_test_pcbnew_worker_diagnostics", self._worker_path()
        )
        self.assertIsNotNone(specification)
        self.assertIsNotNone(specification.loader)
        module = importlib.util.module_from_spec(specification)
        previous_pcbnew = sys.modules.get("pcbnew")
        sys.modules["pcbnew"] = types.ModuleType("pcbnew")
        try:
            specification.loader.exec_module(module)
        finally:
            if previous_pcbnew is None:
                sys.modules.pop("pcbnew", None)
            else:
                sys.modules["pcbnew"] = previous_pcbnew
        return module

    def test_worker_phase_emission_is_flushed_and_constant_only(self) -> None:
        worker = self._load_worker()
        marker = worker.WORKER_PHASE_INSPECT_BOARD_COMPONENTS

        with patch("builtins.print") as printer:
            worker._emit_phase(marker)

        printer.assert_called_once_with(
            f"{worker.WORKER_PHASE_PREFIX}{marker}",
            file=worker.sys.stderr,
            flush=True,
        )

    def test_phase_parser_rejects_unknown_and_private_marker_values(self) -> None:
        known = f"{WORKER_PHASE_PREFIX}inspect_board_tracks"
        forged = (
            f"{WORKER_PHASE_PREFIX}inspect_board_tracks private-board.kicad_pcb\n"
            f"{WORKER_PHASE_PREFIX}unknown_phase\n"
            "/private/project/board.kicad_pcb"
        )

        self.assertIn(known, WORKER_PHASE_LINES)
        self.assertEqual(
            _last_worker_phase(f"{known}\n{forged}".encode()),
            "inspect_board_tracks",
        )
        self.assertIsNone(_last_worker_phase(forged.encode()))

    def test_timeout_includes_only_the_last_allowlisted_phase(self) -> None:
        private_path = "/private/pcb-project/board.kicad_pcb"
        worker_result = SimpleNamespace(
            timed_out=True,
            output_limited=False,
            returncode=-9,
            stdout=b"private stdout",
            stderr=(
                f"{WORKER_PHASE_PREFIX}inspect_board_load_begin\n"
                f"{WORKER_PHASE_PREFIX}unknown /private/secret\n"
                "private stderr"
            ).encode(),
        )

        with (
            patch(
                "pcbdraft.kicad.pcb._system_python",
                return_value=Path("/fake/system-python"),
            ),
            patch(
                "pcbdraft.kicad.pcb.run_command",
                return_value=worker_result,
            ),
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
            "isolated pcbnew worker timed out "
            "(mode=inspect_board, timeout=12.5s, "
            "last_phase=inspect_board_load_begin)",
        )
        self.assertNotIn(private_path, message)
        self.assertNotIn("private", message)

    def test_loader_failure_leaves_load_begin_as_last_phase(self) -> None:
        worker = self._load_worker()
        private_path = "/private/project/board.kicad_pcb"

        with tempfile.TemporaryDirectory() as temporary:
            board_path = Path(temporary) / "board.kicad_pcb"
            board_path.write_bytes(b"not loaded")
            output = io.StringIO()
            with (
                patch.object(
                    worker.pcbnew,
                    "LoadBoard",
                    create=True,
                    side_effect=RuntimeError(private_path),
                ),
                contextlib.redirect_stderr(output),
                self.assertRaisesRegex(RuntimeError, "private/project"),
            ):
                worker.inspect_board_job({"board_path": str(board_path)})

        emitted = output.getvalue()
        self.assertEqual(
            emitted,
            f"{WORKER_PHASE_PREFIX}inspect_board_load_begin\n",
        )
        self.assertEqual(
            _last_worker_phase(emitted.encode()), "inspect_board_load_begin"
        )
        self.assertNotIn(private_path, emitted)


if __name__ == "__main__":
    unittest.main()
