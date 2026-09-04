from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.kicad.ipc import KiCadIPCCompanion, map_ipc_board


def _base_scene() -> dict[str, object]:
    outline = [
        {"x1_mm": 0.0, "y1_mm": 0.0, "x2_mm": 20.0, "y2_mm": 0.0},
        {"x1_mm": 20.0, "y1_mm": 0.0, "x2_mm": 20.0, "y2_mm": 20.0},
        {"x1_mm": 20.0, "y1_mm": 20.0, "x2_mm": 0.0, "y2_mm": 20.0},
        {"x1_mm": 0.0, "y1_mm": 20.0, "x2_mm": 0.0, "y2_mm": 0.0},
    ]
    return {
        "schema": "pcbdraft-live-board-scene",
        "version": 1,
        "source": "committed_project",
        "committed": True,
        "project_id": "gui-test",
        "state_revision": 3,
        "design_revision": 2,
        "geometry_revision": 1,
        "content_hash": "a" * 64,
        "event_sequence": 4,
        "board": {
            "width_mm": 20.0,
            "height_mm": 20.0,
            "layer_count": 2,
            "layers": ["F.Cu", "B.Cu"],
            "outline": outline,
        },
        "outline": outline,
        "footprints": [
            {
                "id": "load_r",
                "reference": "R1",
                "label": "R1",
                "value": "4.7k",
                "part_id": "yageo.rc0603fr-074k7l",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "x_mm": 10.0,
                "y_mm": 10.0,
                "rotation_deg": 0.0,
                "side": "front",
                "fixed": False,
                "placed": True,
            }
        ],
        "routes": [
            {
                "id": "route_old",
                "net": "net_out",
                "net_name": "OUT",
                "layer_index": 0,
                "layer": "F.Cu",
                "x1_mm": 1.0,
                "y1_mm": 1.0,
                "x2_mm": 2.0,
                "y2_mm": 2.0,
                "width_mm": 0.2,
            }
        ],
        "vias": [],
        "unrouted_nets": [],
        "status": {
            "project_status": "generated",
            "updated_at": "2026-08-30T00:00:00Z",
            "design_available": True,
            "validation": None,
            "ipc": {"state": "disabled"},
        },
    }


class _Position:
    def __init__(self, x_mm: float, y_mm: float) -> None:
        # kicad-python's Vector2.x/y properties are raw integer nanometers.
        self.x = int(x_mm * 1_000_000)
        self.y = int(y_mm * 1_000_000)


class _LibraryId:
    library = "Resistor_SMD"
    name = "R_0603_1608Metric"


class _OfficialFootprint:
    def __init__(self) -> None:
        self.reference_field = SimpleNamespace(text=SimpleNamespace(value="R1"))
        self.position = _Position(9.0, 8.0)
        self.orientation = SimpleNamespace(degrees=90.0)
        self.layer = 34
        self.definition = SimpleNamespace(id=_LibraryId())


class _OfficialTrack:
    net = SimpleNamespace(name="OUT")
    layer = 34
    start = _Position(9.0, 8.0)
    end = _Position(12.0, 8.0)
    width = 250_000


class _OfficialVia:
    net = SimpleNamespace(name="OUT")
    position = _Position(12.0, 8.0)
    drill_diameter = 300_000
    diameter = 700_000
    padstack = SimpleNamespace(drill=SimpleNamespace(start_layer=3, end_layer=34))


class _OfficialBoard:
    def get_copper_layer_count(self) -> int:
        return 2

    def get_enabled_layers(self) -> list[int]:
        return [3, 34]

    def get_footprints(self) -> list[object]:
        return [_OfficialFootprint()]

    def get_tracks(self) -> list[object]:
        return [_OfficialTrack()]

    def get_vias(self) -> list[object]:
        return [_OfficialVia()]


class KiCadIPCMappingTests(unittest.TestCase):
    def test_official_kipy_shaped_objects_map_to_compact_scene_layers(self) -> None:
        base = _base_scene()
        before = copy.deepcopy(base)

        scene = map_ipc_board(base, _OfficialBoard())

        self.assertEqual(scene["source"], "kicad_ipc")
        self.assertFalse(scene["committed"])
        self.assertEqual(scene["footprints"][0]["reference"], "R1")
        self.assertEqual(scene["footprints"][0]["x_mm"], 9.0)
        self.assertEqual(scene["footprints"][0]["rotation_deg"], 90.0)
        self.assertEqual(scene["footprints"][0]["side"], "back")
        self.assertEqual(
            scene["footprints"][0]["footprint"],
            "Resistor_SMD:R_0603_1608Metric",
        )
        self.assertEqual(scene["routes"][0]["layer"], "B.Cu")
        self.assertEqual(scene["routes"][0]["layer_index"], 1)
        self.assertEqual(scene["routes"][0]["width_mm"], 0.25)
        self.assertEqual(scene["vias"][0]["from_layer"], 0)
        self.assertEqual(scene["vias"][0]["to_layer"], 1)
        self.assertEqual(scene["vias"][0]["drill_mm"], 0.3)
        self.assertEqual(base, before)

    def test_generic_mapping_keeps_explicit_millimeter_fields(self) -> None:
        board = SimpleNamespace(
            get_copper_layer_count=lambda: 2,
            get_footprints=lambda: [
                {
                    "reference": "R1",
                    "x_mm": 7.5,
                    "y_mm": 6.5,
                    "rotation_deg": 45.0,
                    "side": "front",
                }
            ],
            get_tracks=lambda: [
                {
                    "net_name": "OUT",
                    "layer": "F.Cu",
                    "x1_mm": 1.0,
                    "y1_mm": 2.0,
                    "x2_mm": 3.0,
                    "y2_mm": 4.0,
                    "width_mm": 0.2,
                }
            ],
            get_vias=lambda: [
                {
                    "net_name": "OUT",
                    "x_mm": 3.0,
                    "y_mm": 4.0,
                    "diameter_mm": 0.7,
                    "drill_mm": 0.3,
                    "from_layer": 0,
                    "to_layer": 1,
                }
            ],
        )

        scene = map_ipc_board(_base_scene(), board)

        self.assertEqual(scene["footprints"][0]["x_mm"], 7.5)
        self.assertEqual(scene["routes"][0]["width_mm"], 0.2)
        self.assertEqual(scene["vias"][0]["diameter_mm"], 0.7)

    def test_poll_is_bounded_cached_and_reports_nested_status(self) -> None:
        now = [0.0]
        calls = [0]

        def connector() -> _OfficialBoard:
            calls[0] += 1
            return _OfficialBoard()

        companion = KiCadIPCCompanion(
            enabled=True,
            connector=connector,
            poll_interval=0.5,
            clock=lambda: now[0],
        )
        first = companion.poll(_base_scene())
        now[0] = 0.1
        second = companion.poll(_base_scene())

        self.assertEqual(first["status"]["state"], "online")
        self.assertTrue(first["status"]["read_only"])
        self.assertIn("scene", first)
        self.assertEqual(second, first)
        self.assertEqual(calls[0], 1)

        now[0] = 0.5
        companion.poll(_base_scene())
        self.assertEqual(calls[0], 2)

    def test_offline_and_missing_optional_package_are_graceful(self) -> None:
        offline = KiCadIPCCompanion(enabled=True, connector=lambda: None).poll(
            _base_scene()
        )
        self.assertEqual(offline["status"]["state"], "offline")
        self.assertNotIn("scene", offline)

        missing = ModuleNotFoundError("No module named 'kipy'", name="kipy")
        with patch("pcbdraft.kicad.ipc.importlib.import_module", side_effect=missing):
            unavailable = KiCadIPCCompanion(enabled=True).poll(_base_scene())
        self.assertEqual(unavailable["status"]["state"], "unavailable")

    def test_poll_interval_below_kicad_notification_bound_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "between 0.5 and 60"):
            KiCadIPCCompanion(poll_interval=0.49)

    def test_companion_defaults_off_for_headless_health(self) -> None:
        result = KiCadIPCCompanion(
            connector=lambda: (_ for _ in ()).throw(AssertionError("must not connect"))
        ).poll(_base_scene())
        self.assertEqual(result["status"]["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
