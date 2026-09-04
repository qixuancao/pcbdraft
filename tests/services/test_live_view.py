from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.core.locking import ResourceLock
from pcbdraft.domain.ir import Design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.previews import PreviewBundle
from pcbdraft.services.live_view import (
    ExactPreviewCache,
    LiveViewService,
    SceneLimits,
)
from tests.support.design_factory import minimal_design_dict


def _design() -> Design:
    value = copy.deepcopy(minimal_design_dict())
    value["version"] = 2
    resistor = copy.deepcopy(value["components"][0])
    resistor.update({"id": "load_r2", "reference": "R2"})
    resistor["placement"]["x_mm"] = 15.0
    value["components"].append(resistor)
    value["blocks"][0]["components"].append("load_r2")
    value["nets"][0]["endpoints"].append(
        {"component": "load_r2", "pin": "1", "role": "load"}
    )
    value["native_intent"] = {
        "outline": [],
        "footprint_poses": [],
        "routes": [],
        "vias": [],
        "unrouted_nets": ["net_3v3"],
        "provenance": "pcbdraft",
        "geometry_revision": 3,
    }
    return Design.from_dict(value)


def _native_snapshot() -> dict[str, Any]:
    return {
        "schema": "pcbdraft-pcbnew-result",
        "version": 1,
        "mode": "inspect_board",
        "kicad_version": "10.0.5",
        "components": [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "reference": "R1",
                "value": "4.7k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "x_mm": 9.0,
                "y_mm": 8.0,
                "rotation_deg": 90.0,
                "side": "front",
                "bbox": {"x_mm": 7.8, "y_mm": 7.1, "width_mm": 2.4, "height_mm": 1.8},
                "pads": [
                    {
                        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "number": "1",
                        "net": "3V3",
                        "x_mm": 8.4,
                        "y_mm": 8.0,
                        "width_mm": 0.8,
                        "height_mm": 0.9,
                        "layers": ["F.Cu", "F.Mask"],
                    }
                ],
            },
            {
                "uuid": "22222222-2222-2222-2222-222222222222",
                "reference": "R2",
                "value": "4.7k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "x_mm": 16.0,
                "y_mm": 14.0,
                "rotation_deg": 0.0,
                "side": "back",
                "bbox": {"x_mm": 14.8, "y_mm": 13.1, "width_mm": 2.4, "height_mm": 1.8},
                "pads": [],
            },
        ],
        "nets": ["3V3", "OUT"],
        "tracks": [
            {
                "kind": "segment",
                "uuid": "33333333-3333-3333-3333-333333333333",
                "net": "3V3",
                "x1_mm": 9.0,
                "y1_mm": 8.0,
                "x2_mm": 16.0,
                "y2_mm": 14.0,
                "width_mm": 0.25,
                "layer": "F.Cu",
                "layer_index": 0,
            },
            {
                "kind": "via",
                "uuid": "44444444-4444-4444-4444-444444444444",
                "net": "3V3",
                "x_mm": 12.0,
                "y_mm": 11.0,
                "width_mm": 0.7,
                "drill_mm": 0.3,
                "from_layer": 0,
                "to_layer": 1,
            },
        ],
        "zones": [],
        "outline": [
            {
                "uuid": "50000000-0000-0000-0000-000000000001",
                "x1_mm": 0.0,
                "y1_mm": 0.0,
                "x2_mm": 20.0,
                "y2_mm": 0.0,
            },
            {"x1_mm": 20.0, "y1_mm": 0.0, "x2_mm": 20.0, "y2_mm": 20.0},
            {"x1_mm": 20.0, "y1_mm": 20.0, "x2_mm": 0.0, "y2_mm": 20.0},
            {"x1_mm": 0.0, "y1_mm": 20.0, "x2_mm": 0.0, "y2_mm": 0.0},
        ],
        "board": {
            "layers": 2,
            "thickness_mm": 1.6,
            "min_clearance_mm": 0.2,
            "min_track_mm": 0.2,
            "min_drill_mm": 0.3,
            "edge_clearance_mm": 0.5,
        },
    }


class _Managed:
    def __init__(self, root: Path, design: Design) -> None:
        self.root = root.resolve()
        self.design = design
        self.graph = PartGraph.bundled()
        self.manifest = {
            "files": {"manifest": "committed.txt"},
            "native_snapshots": {"board": _native_snapshot()},
            "generation": {"pcb": {"routing": {"unrouted": ["3V3"]}}},
        }
        self.synchronized_checks = 0

    def assert_synchronized(self) -> None:
        self.synchronized_checks += 1


class _Service:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.projects_root = root / "projects"
        self.project = self.projects_root / "gui-test"
        self.project.mkdir(parents=True)
        self.locks_root = root / "locks"
        self.locks_root.mkdir()
        self.view = {
            "project": {
                "id": "gui-test",
                "name": "GUI test",
                "status": "generated",
                "updated_at": "2026-08-30T00:00:00Z",
                "design_revision": 7,
                "provider": "test",
            },
            "state": {
                "revision": 11,
                "design_revision": 7,
                "event_sequence": 19,
            },
            "artifacts": {
                "validation": {
                    "candidate_ready": True,
                    "production_ready": False,
                    "source_design_revision": 7,
                    "report": "/must/not/leak.json",
                }
            },
        }

    def project_root(self, project_id: str) -> Path:
        if project_id != "gui-test":
            raise ValidationError("unexpected project")
        return self.project

    def try_open_project_snapshot(
        self, project_id: str, *, timeout: float = 0.0
    ) -> dict[str, Any] | None:
        self.project_root(project_id)
        lock = ResourceLock(self.project, self.locks_root, timeout=timeout)
        try:
            lock.acquire()
        except PCBDraftError as exc:
            if "resource is locked by another runtime process" in str(exc):
                return None
            raise
        try:
            return self.view
        finally:
            lock.release()

    def open_project(self, project_id: str) -> dict[str, Any]:
        self.project_root(project_id)
        return self.view


class LiveSceneTests(unittest.TestCase):
    def test_root_hierarchy_copper_names_resolve_to_the_same_semantic_net(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _Service(Path(temporary))
            design_root = service.project / "design"
            design_root.mkdir()
            managed = _Managed(design_root, _design())
            native = managed.manifest["native_snapshots"]["board"]
            for track in native["tracks"]:
                track["net"] = "/3V3"
            native["components"][0]["pads"] = [
                {"number": "4", "net": "unconnected-(U1-NC-Pad4)"}
            ]
            live = LiveViewService(service, managed_loader=lambda _path: managed)  # type: ignore[arg-type]

            scene = live.snapshot("gui-test")

            self.assertIsNotNone(scene)
            assert scene is not None
            self.assertEqual([route["net"] for route in scene["routes"]], ["net_3v3"])
            self.assertEqual([route["net_name"] for route in scene["routes"]], ["3V3"])
            self.assertEqual([via["net"] for via in scene["vias"]], ["net_3v3"])
            self.assertEqual([via["net_name"] for via in scene["vias"]], ["3V3"])

    def test_unknown_and_nested_copper_net_names_fail_closed(self) -> None:
        for native_name in (
            "/sheet_a/3V3",
            "/unknown",
            "unconnected-(U1-NC-Pad4)",
        ):
            with (
                self.subTest(native_name=native_name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                service = _Service(Path(temporary))
                design_root = service.project / "design"
                design_root.mkdir()
                managed = _Managed(design_root, _design())
                managed.manifest["native_snapshots"]["board"]["tracks"][0]["net"] = (
                    native_name
                )
                live = LiveViewService(
                    service, managed_loader=lambda _path, managed=managed: managed
                )  # type: ignore[arg-type]

                with self.assertRaisesRegex(ValidationError, "unknown semantic net"):
                    live.snapshot("gui-test")

    def test_ambiguous_root_hierarchy_copper_name_fails_closed(self) -> None:
        value = _design().to_dict()
        value["nets"][1]["name"] = "/3V3"
        ambiguous_design = Design.from_dict(value)
        with tempfile.TemporaryDirectory() as temporary:
            service = _Service(Path(temporary))
            design_root = service.project / "design"
            design_root.mkdir()
            managed = _Managed(design_root, ambiguous_design)
            for track in managed.manifest["native_snapshots"]["board"]["tracks"]:
                track["net"] = "/3V3"
            live = LiveViewService(service, managed_loader=lambda _path: managed)  # type: ignore[arg-type]

            with self.assertRaisesRegex(ValidationError, "ambiguous semantic net"):
                live.snapshot("gui-test")

    def test_scene_projects_one_synchronized_committed_board_without_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _Service(Path(temporary))
            design_root = service.project / "design"
            design_root.mkdir()
            managed = _Managed(design_root, _design())
            live = LiveViewService(service, managed_loader=lambda _path: managed)  # type: ignore[arg-type]

            scene = live.snapshot("gui-test")

            self.assertIsNotNone(scene)
            assert scene is not None
            self.assertEqual(scene["schema"], "pcbdraft-live-board-scene")
            self.assertEqual(scene["state_revision"], 11)
            self.assertEqual(scene["design_revision"], 7)
            self.assertEqual(scene["geometry_revision"], 3)
            self.assertEqual(scene["board"]["layers"], ["F.Cu", "B.Cu"])
            self.assertEqual(scene["outline"], scene["board"]["outline"])
            self.assertEqual(
                [item["reference"] for item in scene["footprints"]], ["R1", "R2"]
            )
            self.assertEqual(scene["footprints"][0]["pads"], ["1", "2"])
            self.assertEqual(scene["footprints"][0]["nets"][0]["name"], "3V3")
            self.assertEqual(scene["status"]["counts"]["nets"], 2)
            self.assertEqual(scene["footprints"][0]["x_mm"], 9.0)
            self.assertEqual(
                scene["footprints"][0]["uuid"],
                "11111111-1111-1111-1111-111111111111",
            )
            self.assertEqual(scene["footprints"][0]["bounds_source"], "native_kicad")
            self.assertEqual(scene["footprints"][0]["bounds"]["width_mm"], 2.4)
            self.assertEqual(
                scene["footprints"][0]["pad_geometry"][0]["uuid"],
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            )
            self.assertEqual(scene["spatial_index"]["pads"][0]["net"], "net_3v3")
            self.assertEqual(scene["canvas"]["primary"]["kind"], "kicad_svg")
            self.assertEqual(
                scene["canvas"]["primary"]["content_hash"], scene["content_hash"]
            )
            self.assertEqual(len(scene["routes"]), 1)
            self.assertEqual(len(scene["vias"]), 1)
            self.assertEqual(len(scene["unrouted_nets"][0]["lines"]), 1)
            self.assertNotIn(str(service.project), json.dumps(scene))
            self.assertNotIn("must/not/leak", json.dumps(scene))
            self.assertGreaterEqual(managed.synchronized_checks, 1)

    def test_busy_writer_returns_none_and_the_caller_can_keep_its_old_frame(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _Service(Path(temporary))
            design_root = service.project / "design"
            design_root.mkdir()
            managed = _Managed(design_root, _design())
            live = LiveViewService(service, managed_loader=lambda _path: managed)  # type: ignore[arg-type]

            with ResourceLock(service.project, service.locks_root):
                self.assertIsNone(live.snapshot("gui-test", timeout=0))

    def test_missing_native_outline_uses_the_committed_design_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _Service(Path(temporary))
            design_root = service.project / "design"
            design_root.mkdir()
            managed = _Managed(design_root, _design())
            del managed.manifest["native_snapshots"]["board"]["outline"]
            live = LiveViewService(service, managed_loader=lambda _path: managed)  # type: ignore[arg-type]

            scene = live.snapshot("gui-test")

            self.assertIsNotNone(scene)
            assert scene is not None
            self.assertEqual(len(scene["outline"]), 4)
            self.assertEqual(scene["outline"], scene["board"]["outline"])

    def test_scene_caps_fail_closed_and_diff_reports_moved_footprints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _Service(Path(temporary))
            design_root = service.project / "design"
            design_root.mkdir()
            managed = _Managed(design_root, _design())
            bounded = LiveViewService(
                service,
                limits=SceneLimits(max_routes=0),
                managed_loader=lambda _path: managed,
            )  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValidationError, "route limit"):
                bounded.snapshot("gui-test")

            live = LiveViewService(service, managed_loader=lambda _path: managed)  # type: ignore[arg-type]
            before = live.snapshot("gui-test")
            assert before is not None
            after = copy.deepcopy(before)
            after["footprints"][0]["x_mm"] += 1.0
            diff = live.diff(before, after)
            self.assertEqual(diff["footprints"]["moved"], ["load_r"])


class ExactPreviewCacheTests(unittest.TestCase):
    def test_cache_hit_does_not_mutate_project_and_3d_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = _Service(root)
            design_root = service.project / "design"
            design_root.mkdir()
            (design_root / "committed.txt").write_text("unchanged", encoding="utf-8")
            (design_root / "untracked-secret.txt").write_text(
                "must not be copied", encoding="utf-8"
            )
            design = _design()

            def loader(path: str | Path) -> _Managed:
                return _Managed(Path(path), design)

            calls: list[str] = []

            def generator(
                project: _Managed,
                output: Path,
                kind: str,
                *,
                timeout: float,
            ) -> PreviewBundle:
                del timeout
                calls.append(kind)
                self.assertFalse((project.root / "untracked-secret.txt").exists())
                output.mkdir()
                if kind == "render_board":
                    file_key, filename, payload = "board_svg", "board.svg", b"<svg/>"
                else:
                    file_key, filename, payload = (
                        "board_render",
                        "board-top.png",
                        b"png",
                    )
                artifact = output / filename
                artifact.write_bytes(payload)
                digest = hashlib.sha256(payload).hexdigest()
                receipt = output / "receipt.json"
                atomic_write_json(
                    receipt,
                    {
                        "schema": "pcbdraft-preview-bundle",
                        "version": 1,
                        "created_at": "2026-08-30T00:00:00Z",
                        "renders": [kind],
                        "design_content_hash": project.design.content_hash(),
                        "files": {
                            file_key: {
                                "path": filename,
                                "bytes": len(payload),
                                "sha256": digest,
                            }
                        },
                        "tool_runs": [],
                    },
                )
                return PreviewBundle(
                    output,
                    receipt,
                    {file_key: artifact},
                    project.design.content_hash(),
                )

            state_before = copy.deepcopy(service.view)
            cache = ExactPreviewCache(
                service,
                root / "cache",
                generator=generator,
                managed_loader=loader,
            )  # type: ignore[arg-type]

            board = cache.artifact("gui-test", "board_svg", generate=True)
            self.assertIsNotNone(board)
            assert board is not None
            self.assertEqual(board.name, "board.svg")
            self.assertEqual(cache.artifact("gui-test", "board_svg"), board)
            self.assertIsNone(cache.artifact("gui-test", "board_3d"))
            three_d = cache.artifact("gui-test", "board_3d", generate=True)
            self.assertIsNotNone(three_d)
            self.assertEqual(calls, ["render_board", "render_3d"])
            self.assertEqual(service.view, state_before)
            self.assertEqual(
                (design_root / "committed.txt").read_text(encoding="utf-8"),
                "unchanged",
            )

    def test_artifact_kind_is_an_allowlist_not_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _Service(Path(temporary))
            cache = ExactPreviewCache(service, Path(temporary) / "cache")  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValidationError, "unknown exact preview kind"):
                cache.artifact("gui-test", "../project.json")


if __name__ == "__main__":
    unittest.main()
