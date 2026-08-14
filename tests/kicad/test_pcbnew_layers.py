"""Verify that pcbnew determines layer support from the installed KiCad build."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class PcbnewLayerSupportTests(unittest.TestCase):
    def test_worker_reports_installed_kicad_layer_limit_not_a_historical_cap(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        worker = root / "src" / "pcbdraft" / "kicad" / "pcbnew_worker.py"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            job = directory / "job.json"
            output = directory / "result.json"
            job.write_text(
                json.dumps(
                    {
                        "schema": "pcbdraft-pcbnew-job",
                        "version": 1,
                        "mode": "inspect",
                        "design_id": "layer-probe",
                        "board": {"layers": 33},
                        "components": [],
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    str(worker),
                    "inspect",
                    str(job),
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r"KiCad .* exposes .* copper layers")


if __name__ == "__main__":
    unittest.main()
