from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from pcbdraft.core.process import run_command
from pcbdraft.domain.blocks import BlockRegistry
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.requirements import RequirementsSpec, compile_requirements
from pcbdraft.kicad.pcb import generate_pcb
from pcbdraft.kicad.schematic import generate_schematic
from pcbdraft.model.providers import IntentProvider
from pcbdraft.services.application import ApplicationService
from tests.support.requirements_factory import controller_requirements_dict


def _real_kicad_available() -> bool:
    if shutil.which("kicad-cli") is None or not Path("/usr/bin/python3").is_file():
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", "import pcbnew"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@unittest.skipUnless(_real_kicad_available(), "real KiCad CLI/pcbnew unavailable")
class NativeKiCadGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = PartGraph.bundled()
        cls.design = compile_requirements(
            RequirementsSpec.from_dict(controller_requirements_dict()),
            graph=cls.graph,
            registry=BlockRegistry.bundled(cls.graph),
        )

    def test_native_project_is_reproducible_and_passes_erc_drc_parity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcbdraft-kicad-test-") as temporary:
            root = Path(temporary)
            generated = []
            for directory_name in ("first", "second"):
                directory = root / directory_name
                directory.mkdir()
                schematic = generate_schematic(
                    self.design, directory / "controller.kicad_sch", graph=self.graph
                )
                pcb = generate_pcb(
                    self.design, directory / "controller.kicad_pcb", graph=self.graph
                )
                self.assertEqual(pcb.routing.state, "completed")
                self.assertEqual(pcb.routing.unrouted, ())
                generated.append((schematic, pcb))

            first_schematic, first_pcb = generated[0]
            second_schematic, second_pcb = generated[1]
            self.assertEqual(first_schematic.sha256, second_schematic.sha256)
            self.assertEqual(first_pcb.sha256, second_pcb.sha256)
            self.assertEqual(first_pcb.project_sha256, second_pcb.project_sha256)
            self.assertEqual(len(first_pcb.reference_planes), 1)
            self.assertEqual(first_pcb.reference_planes[0]["net"], "/GND")
            self.assertEqual(first_pcb.reference_planes[0]["layer"], "B.Cu")
            self.assertTrue(first_pcb.reference_planes[0]["filled"])
            self.assertGreater(first_pcb.reference_planes[0]["area_mm2"], 0)
            self.assertEqual(
                first_pcb.reference_planes[0]["pad_connection"], "thermal_relief"
            )
            self.assertGreaterEqual(
                sum(via.net == "GND" for via in first_pcb.routing.vias), 2
            )
            self.assertTrue(
                all(
                    metric["outcome"] == "pass"
                    and metric["metric"] == "minimum_relevant_copper_pad_edge_gap"
                    for metric in first_pcb.constraint_metrics.values()
                )
            )
            project = json.loads(first_pcb.project_path.read_text(encoding="utf-8"))
            self.assertEqual(
                project["board"]["design_settings"]["rule_severities"][
                    "track_not_centered_on_via"
                ],
                "error",
            )
            self.assertEqual(
                project["erc"]["rule_severities"]["footprint_filter"],
                "ignore",
            )

            erc_path = root / "erc.json"
            drc_path = root / "drc.json"
            commands = (
                (
                    "erc",
                    [
                        "kicad-cli",
                        "sch",
                        "erc",
                        "--format",
                        "json",
                        "--output",
                        str(erc_path),
                        str(first_schematic.path),
                    ],
                ),
                (
                    "drc",
                    [
                        "kicad-cli",
                        "pcb",
                        "drc",
                        "--format",
                        "json",
                        "--output",
                        str(drc_path),
                        "--schematic-parity",
                        str(first_pcb.path),
                    ],
                ),
            )
            for name, argv in commands:
                result = run_command(
                    argv,
                    cwd=first_pcb.path.parent,
                    timeout=30,
                    max_output_bytes=1024 * 1024,
                )
                self.assertFalse(result.timed_out, name)
                self.assertFalse(result.output_limited, name)
                self.assertEqual(result.returncode, 0, result.stderr.decode())

            erc = json.loads(erc_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item for sheet in erc["sheets"] for item in sheet["violations"]],
                [],
            )
            self.assertEqual(
                {item["key"] for item in erc["ignored_checks"]},
                {"simulation_model_issue", "footprint_filter"},
            )
            drc = json.loads(drc_path.read_text(encoding="utf-8"))
            self.assertEqual(drc["violations"], [])
            self.assertEqual(drc["unconnected_items"], [])
            self.assertEqual(drc["schematic_parity"], [])
            self.assertEqual(
                {item["key"] for item in drc["ignored_checks"]},
                {
                    "tuning_profile_track_geometries",
                    "footprint_filters_mismatch",
                },
            )

    def test_flat_add_materializes_unplaced_stock_footprint_without_ir_pose(
        self,
    ) -> None:
        provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
        with tempfile.TemporaryDirectory(prefix="pcbdraft-flat-part-") as temporary:
            service = ApplicationService(Path(temporary), provider=provider)
            view = service.create_empty_project("Flat LED")
            project_id = str(view["project"]["id"])
            revision = int(view["state"]["revision"])
            part = {
                "id": "kicad.generic-led-5mm-green",
                "kind": "led",
                "description": "Green 5 mm LED",
                "symbol": "Device:LED",
                "footprint": "LED_THT:LED_D5.0mm",
                "bom": True,
                "pins": [
                    {
                        "number": "1",
                        "name": "K",
                        "electrical_type": "passive",
                        "functions": ["cathode"],
                        "required": True,
                        "footprint_pad": "1",
                    },
                    {
                        "number": "2",
                        "name": "A",
                        "electrical_type": "passive",
                        "functions": ["anode"],
                        "required": True,
                        "footprint_pad": "2",
                    },
                ],
            }
            operations = (
                ("register_kicad_part", {"value": part}),
                (
                    "add_block",
                    {
                        "value": {
                            "id": "indicator",
                            "kind": "indicator",
                            "name": "LED indicator",
                            "version": "1",
                            "intent": "Visible indicator",
                        }
                    },
                ),
                (
                    "add_component",
                    {
                        "value": {
                            "id": "led",
                            "reference": "D1",
                            "part_id": part["id"],
                            "value": "Green",
                            "block_id": "indicator",
                        }
                    },
                ),
            )
            for tool_name, arguments in operations:
                view = service.execute_pcb_tool(
                    project_id,
                    tool_name,
                    arguments,
                    timeout=60.0,
                    expected_revision=revision,
                )
                revision = int(view["state"]["revision"])

            inspected = service.execute_pcb_tool(
                project_id,
                "inspect_design",
                {},
                timeout=10.0,
                expected_revision=revision,
            )
            component = inspected["tool_result"]["design"]["components"][0]
            self.assertEqual(component["reference"], "D1")
            self.assertNotIn("placement", component)


if __name__ == "__main__":
    unittest.main()
