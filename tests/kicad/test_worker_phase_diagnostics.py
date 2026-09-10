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
    WORKER_PHASE_ALLOWLIST,
    WORKER_PHASE_LINES,
    WORKER_PHASE_PREFIX,
    _last_worker_phase,
    _run_worker,
)


class _FakePosition:
    x = 100
    y = 200


class _FakeSegment:
    def GetNetname(self):
        return "/private/secret-net"

    def GetStart(self):
        return _FakePosition()

    def GetEnd(self):
        return _FakePosition()

    def GetWidth(self):
        return 50

    def GetLayer(self):
        return 0


class _FakeVia:
    def TopLayer(self):
        return 0

    def BottomLayer(self):
        return 1

    def GetNetname(self):
        return "/private/secret-via-net"

    def GetPosition(self):
        return _FakePosition()

    def GetWidth(self):
        return 60

    def GetDrillValue(self):
        return 30


class _FailingVia(_FakeVia):
    def TopLayer(self):
        raise RuntimeError("/private/secret-via-getter")


class _FailingTrackIterator:
    def __iter__(self):
        return self

    def __next__(self):
        raise RuntimeError("/private/secret-track-iterator")


class _FakeBoard:
    def __init__(self, tracks):
        self._tracks = tracks

    def GetCopperLayerCount(self):
        return 2

    def GetFootprints(self):
        return ()

    def Tracks(self):
        return self._tracks

    def Zones(self):
        return ()

    def GetDrawings(self):
        return ()

    def GetLayerName(self, layer):
        return {0: "F.Cu", 1: "B.Cu"}[layer]

    def GetDesignSettings(self):
        return SimpleNamespace(
            GetBoardThickness=lambda: 100,
            m_MinClearance=10,
            m_TrackMinWidth=10,
            m_MinThroughDrill=10,
            m_CopperEdgeClearance=10,
        )

    def GetNetInfo(self):
        return SimpleNamespace(NetsByName=dict)


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

    @staticmethod
    def _configure_track_worker(worker, tracks):
        board = _FakeBoard(tracks)
        worker.pcbnew.F_Cu = 0
        worker.pcbnew.B_Cu = 1
        worker.pcbnew.PCB_VIA = _FakeVia
        worker.pcbnew.PCB_TRACK = _FakeSegment
        worker.pcbnew.ToMM = lambda value: value / 1000
        worker.pcbnew.GetBuildVersion = lambda: "10.0.0"
        worker.pcbnew.LoadBoard = lambda _path: board
        return board

    def _inspect_fake_tracks(self, worker, tracks):
        self._configure_track_worker(worker, tracks)
        with tempfile.TemporaryDirectory() as temporary:
            board_path = Path(temporary) / "board.kicad_pcb"
            board_path.write_bytes(b"not loaded")
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                result = worker.inspect_board_job({"board_path": str(board_path)})
        return result, output.getvalue()

    def test_worker_and_parent_track_marker_allowlists_are_synchronized(self) -> None:
        worker = self._load_worker()

        self.assertEqual(worker.WORKER_PHASE_MARKERS, WORKER_PHASE_ALLOWLIST)
        self.assertTrue(
            all(
                f"{WORKER_PHASE_PREFIX}{marker}" in WORKER_PHASE_LINES
                for marker in worker.WORKER_PHASE_MARKERS
            )
        )

    def test_tracks_iterator_failure_reports_fixed_last_marker(self) -> None:
        worker = self._load_worker()
        self._configure_track_worker(worker, _FailingTrackIterator())

        with tempfile.TemporaryDirectory() as temporary:
            board_path = Path(temporary) / "board.kicad_pcb"
            board_path.write_bytes(b"not loaded")
            output = io.StringIO()
            with (
                contextlib.redirect_stderr(output),
                self.assertRaisesRegex(RuntimeError, "secret-track-iterator"),
            ):
                worker.inspect_board_job({"board_path": str(board_path)})

        emitted = output.getvalue()
        self.assertEqual(
            _last_worker_phase(emitted.encode()),
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_ITERATOR_NEXT,
        )
        self.assertNotIn("secret", emitted)
        self.assertTrue(
            all(line in WORKER_PHASE_LINES for line in emitted.splitlines())
        )

    def test_tracks_via_getter_failure_reports_fixed_last_marker(self) -> None:
        worker = self._load_worker()
        self._configure_track_worker(worker, [_FailingVia()])
        worker.pcbnew.PCB_VIA = _FailingVia

        with tempfile.TemporaryDirectory() as temporary:
            board_path = Path(temporary) / "board.kicad_pcb"
            board_path.write_bytes(b"not loaded")
            output = io.StringIO()
            with (
                contextlib.redirect_stderr(output),
                self.assertRaisesRegex(RuntimeError, "secret-via-getter"),
            ):
                worker.inspect_board_job({"board_path": str(board_path)})

        emitted = output.getvalue()
        self.assertEqual(
            _last_worker_phase(emitted.encode()),
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_VIA_TOP_LAYER,
        )
        self.assertNotIn("secret", emitted)
        self.assertTrue(
            all(line in WORKER_PHASE_LINES for line in emitted.splitlines())
        )

    def test_tracks_markers_cover_getters_and_complete_without_dynamic_values(
        self,
    ) -> None:
        worker = self._load_worker()
        result, emitted = self._inspect_fake_tracks(
            worker, [_FakeVia(), _FakeSegment()]
        )
        lines = emitted.splitlines()
        expected = (
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_CONTAINER_BEGIN,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_CONTAINER_END,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_ITERATOR_NEXT,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_TYPE_CLASSIFICATION,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_VIA_TOP_LAYER,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_VIA_BOTTOM_LAYER,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_NET_NAME,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_NUMERIC_GEOMETRY,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_SEGMENT_LAYER,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_COMPLETE,
        )

        self.assertEqual(
            sorted(track["kind"] for track in result["tracks"]), ["segment", "via"]
        )
        self.assertTrue(
            all(f"{WORKER_PHASE_PREFIX}{marker}" in lines for marker in expected)
        )
        self.assertLess(
            lines.index(f"{WORKER_PHASE_PREFIX}{expected[0]}"),
            lines.index(f"{WORKER_PHASE_PREFIX}{expected[-1]}"),
        )
        self.assertNotIn("secret", emitted)
        self.assertTrue(all(line in WORKER_PHASE_LINES for line in lines))

    def test_tracks_item_markers_stop_after_first_eight_items(self) -> None:
        worker = self._load_worker()
        limit = worker.WORKER_TRACK_DIAGNOSTIC_ITEM_LIMIT
        result, emitted = self._inspect_fake_tracks(
            worker, [_FakeSegment() for _ in range(limit + 3)]
        )
        lines = emitted.splitlines()
        prefix = WORKER_PHASE_PREFIX

        self.assertEqual(len(result["tracks"]), limit + 3)
        for marker in (
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_ITERATOR_NEXT,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_TYPE_CLASSIFICATION,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_NET_NAME,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_NUMERIC_GEOMETRY,
            worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_SEGMENT_LAYER,
        ):
            self.assertEqual(lines.count(f"{prefix}{marker}"), limit)
        self.assertEqual(
            lines.count(
                f"{prefix}"
                f"{worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_ITEM_MARKERS_SUPPRESSED}"
            ),
            1,
        )
        self.assertEqual(
            lines.count(f"{prefix}{worker.WORKER_PHASE_INSPECT_BOARD_TRACKS_COMPLETE}"),
            1,
        )
        self.assertTrue(all(line in WORKER_PHASE_LINES for line in lines))
        self.assertNotIn("secret", emitted)


if __name__ == "__main__":
    unittest.main()
