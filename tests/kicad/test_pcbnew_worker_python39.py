"""Compatibility checks for KiCad's isolated pcbnew worker."""

from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


class PcbnewWorkerPython39Tests(unittest.TestCase):
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
        worker_path = self._worker_path()
        specification = importlib.util.spec_from_file_location(
            "pcbdraft_test_pcbnew_worker", worker_path
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

    def test_worker_source_is_python39_compatible(self) -> None:
        source = self._worker_path().read_text(encoding="utf-8")

        ast.parse(source, filename="pcbnew_worker.py", feature_version=(3, 9))
        self.assertNotIn("itertools.pairwise", source)
        self.assertNotIn(".write_text(", source)

    def test_adjacent_pairs_preserve_outline_order(self) -> None:
        worker = self._load_worker()

        self.assertEqual(list(worker._adjacent_pairs(())), [])
        self.assertEqual(list(worker._adjacent_pairs(((0, 0),))), [])
        self.assertEqual(
            list(worker._adjacent_pairs(((0, 0), (2, 0), (2, 1), (0, 0)))),
            [
                ((0, 0), (2, 0)),
                ((2, 0), (2, 1)),
                ((2, 1), (0, 0)),
            ],
        )

    def test_canonical_board_is_identical_for_lf_and_crlf_input(self) -> None:
        worker = self._load_worker()
        board_lf = "(kicad_pcb\n  (generator pcbnew)\n  (version 20240108)\n)\n"
        expected = b"(kicad_pcb\n\t(version 20240108)\n\t(generator pcbnew)\n)\n"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            lf_path = directory / "lf.kicad_pcb"
            crlf_path = directory / "crlf.kicad_pcb"
            lf_path.write_bytes(board_lf.encode("utf-8"))
            crlf_path.write_bytes(board_lf.replace("\n", "\r\n").encode("utf-8"))

            worker._canonicalize_board_uuids(lf_path, {}, "board")
            worker._canonicalize_board_uuids(crlf_path, {}, "board")

            self.assertEqual(lf_path.read_bytes(), expected)
            self.assertEqual(crlf_path.read_bytes(), expected)

    def test_canonical_board_rejects_malformed_structure(self) -> None:
        worker = self._load_worker()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cases = (
                (
                    "missing_envelope",
                    b"(kicad_pcb\r\n  (version 20240108)\r\n",
                    "unexpected board envelope",
                ),
                (
                    "unbalanced_child",
                    b"(kicad_pcb\r\n  (version 20240108\r\n)\r\n",
                    "malformed board syntax",
                ),
                (
                    "bare_carriage_returns",
                    b"(kicad_pcb\r  (version 20240108)\r)\r",
                    "unexpected board envelope",
                ),
            )
            for name, content, message in cases:
                with self.subTest(name=name):
                    board_path = directory / f"{name}.kicad_pcb"
                    board_path.write_bytes(content)
                    with self.assertRaisesRegex(ValueError, message):
                        worker._canonicalize_board_uuids(board_path, {}, "board")

    def test_utf8_lf_writer_uses_python39_compatible_binary_output(self) -> None:
        worker = self._load_worker()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "output.txt"

            worker._write_utf8_lf(target, "alpha\r\nbeta\ngamma\n")

            self.assertEqual(target.read_bytes(), b"alpha\nbeta\ngamma\n")


if __name__ == "__main__":
    unittest.main()
