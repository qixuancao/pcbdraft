from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.domain.ir import Design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.consistency import (
    NativeConsistencyReport,
    NativeDeltaCheck,
    NativeMismatch,
    NativeOperationDeltaReport,
)
from pcbdraft.kicad.routing import (
    RouteSegment,
    RouteVia,
    RoutingFailure,
    RoutingFailureError,
    RoutingResult,
)
from pcbdraft.model.providers import IntentProvider
from pcbdraft.services.application import ApplicationService
from tests.support.design_factory import minimal_design_dict


def _v2_design() -> Design:
    value = minimal_design_dict()
    value["version"] = 2
    value["native_intent"] = {
        "outline": [],
        "footprint_poses": [],
        "routes": [],
        "vias": [],
        "unrouted_nets": [],
        "provenance": "pcbdraft",
        "geometry_revision": 1,
    }
    return Design.from_dict(value)


def _group_design() -> Design:
    value = _v2_design().to_dict()
    second = dict(value["components"][0])
    second.update({"id": "load_r2", "reference": "R2"})
    second.pop("placement", None)
    value["components"].append(second)
    value["blocks"][0]["components"].append("load_r2")
    return Design.from_dict(value)


def _connect_group_arguments(*, invalid_middle: bool = False) -> dict[str, Any]:
    return {
        "connections": {
            "entries": [
                {
                    "net_id": "net_3v3",
                    "component_id": "load_r2",
                    "pin": "1",
                    "role": "load",
                },
                {
                    "net_id": "net_out",
                    "component_id": "missing" if invalid_middle else "load_r2",
                    "pin": "2",
                    "role": "signal",
                },
            ]
        }
    }


def _place_group_arguments() -> dict[str, Any]:
    return {
        "placements": {
            "entries": [
                {
                    "component_id": "load_r",
                    "x_mm": 6.0,
                    "y_mm": 7.0,
                    "rotation_deg": 90.0,
                    "side": "front",
                },
                {
                    "component_id": "load_r2",
                    "x_mm": 14.0,
                    "y_mm": 15.0,
                    "rotation_deg": 180.0,
                    "side": "back",
                },
            ]
        }
    }


class _Graph:
    def __init__(self, graph: PartGraph | None = None) -> None:
        self._graph = graph or PartGraph.bundled()

    def with_footprint_overrides(self, design: Design) -> _Graph:
        return _Graph(self._graph.with_footprint_overrides(design))

    def get(self, part_id: str) -> Any:
        return self._graph.get(part_id)


def _native_board_snapshot(design: Design) -> dict[str, Any]:
    graph = PartGraph.bundled().with_footprint_overrides(design)
    net_names = {item.id: item.name for item in design.nets}
    board_component_ids = {
        component.id
        for component in design.components
        if graph.get(component.part_id).footprint is not None
        and not component.attributes.get("exclude_from_board", False)
    }
    materialized_net_names = {
        net.name
        for net in design.nets
        if any(endpoint.component in board_component_ids for endpoint in net.endpoints)
        or any(item.net == net.id for item in design.native_intent.routes)
        or any(item.net == net.id for item in design.native_intent.vias)
    }
    endpoint_nets = {
        (endpoint.component, endpoint.pin): net.name
        for net in design.nets
        for endpoint in net.endpoints
    }
    width = design.board.width_mm
    height = design.board.height_mm
    points = ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height), (0.0, 0.0))
    return {
        "schema": "pcbdraft-pcbnew-result",
        "version": 1,
        "mode": "inspect_board",
        "kicad_version": "10.0.5",
        "components": [
            {
                "reference": component.reference,
                "value": component.value,
                "footprint": graph.get(component.part_id).footprint,
                "properties": {"Part_ID": graph.get(component.part_id).id},
                "x_mm": component.placement.x_mm if component.placement else 0.0,
                "y_mm": component.placement.y_mm if component.placement else 0.0,
                "rotation_deg": (
                    component.placement.rotation_deg if component.placement else 0.0
                ),
                "side": component.placement.side if component.placement else "front",
                "pads": [
                    {
                        "number": pin.footprint_pad,
                        "net": endpoint_nets.get(
                            (component.id, pin.number),
                            f"unconnected-({component.reference}-{pin.number})",
                        ),
                        "connectivity_component": endpoint_nets.get(
                            (component.id, pin.number),
                            f"nc:{component.reference}:{pin.number}",
                        ),
                        "no_connect": (component.id, pin.number) not in endpoint_nets,
                    }
                    for pin in graph.get(component.part_id).pins
                ],
            }
            for component in design.components
            if graph.get(component.part_id).footprint is not None
            and not component.attributes.get("exclude_from_board", False)
        ],
        "nets": sorted(materialized_net_names),
        "tracks": [
            *(
                {
                    "kind": "segment",
                    "net": net_names[item.net],
                    "x1_mm": item.x1_mm,
                    "y1_mm": item.y1_mm,
                    "x2_mm": item.x2_mm,
                    "y2_mm": item.y2_mm,
                    "width_mm": item.width_mm,
                    "layer_index": item.layer,
                }
                for item in design.native_intent.routes
            ),
            *(
                {
                    "kind": "via",
                    "net": net_names[item.net],
                    "x_mm": item.x_mm,
                    "y_mm": item.y_mm,
                    "width_mm": item.diameter_mm,
                    "drill_mm": item.drill_mm,
                    "from_layer": item.from_layer,
                    "to_layer": item.to_layer,
                }
                for item in design.native_intent.vias
            ),
        ],
        "zones": [],
        "outline": [
            {
                "x1_mm": first[0],
                "y1_mm": first[1],
                "x2_mm": second[0],
                "y2_mm": second[1],
            }
            for first, second in pairwise(points)
        ],
        "board": {
            "layers": design.board.layers,
            "thickness_mm": design.board.thickness_mm,
            "min_clearance_mm": design.board.min_clearance_mm,
            "min_track_mm": design.board.min_track_mm,
            "min_drill_mm": design.board.min_drill_mm,
            "edge_clearance_mm": design.board.edge_clearance_mm,
        },
        "connectivity": {
            "schema": "pcbdraft-pcb-connectivity",
            "version": 1,
            "status": "evaluated",
            "unconnected_count": 0,
            "components": [],
            "drc": {"status": "not_evaluated", "items": []},
        },
    }


def _native_schematic_snapshot(design: Design) -> dict[str, Any]:
    graph = PartGraph.bundled().with_footprint_overrides(design)
    connected = {
        (endpoint.component, endpoint.pin)
        for net in design.nets
        for endpoint in net.endpoints
    }
    partitions = [
        {
            "id": f"net:{net.id}",
            "labels": [net.name],
            "endpoints": [
                {
                    "reference": next(
                        component.reference
                        for component in design.components
                        if component.id == endpoint.component
                    ),
                    "pin": endpoint.pin,
                }
                for endpoint in net.endpoints
            ],
        }
        for net in design.nets
        if net.endpoints
    ]
    for component in design.components:
        for pin in graph.get(component.part_id).pins:
            if (component.id, pin.number) in connected:
                continue
            partitions.append(
                {
                    "id": f"nc:{component.reference}:{pin.number}",
                    "labels": [],
                    "endpoints": [
                        {"reference": component.reference, "pin": pin.number}
                    ],
                }
            )
    endpoint_rows = [
        {
            **endpoint,
            "partition": partition["id"],
            "no_connect": not partition["labels"],
        }
        for partition in partitions
        for endpoint in partition["endpoints"]
    ]
    return {
        "schema": "pcbdraft-schematic-snapshot",
        "version": 1,
        "root_uuid": "fixture",
        "components": [
            {
                "reference": component.reference,
                "value": component.value,
                "symbol": graph.get(component.part_id).symbol,
                "footprint": graph.get(component.part_id).footprint or "",
                "properties": {"Part_ID": graph.get(component.part_id).id},
            }
            for component in design.components
        ],
        "label_names": sorted(net.name for net in design.nets if net.endpoints),
        "label_count": sum(bool(net.endpoints) for net in design.nets),
        "no_connect_count": sum(item["no_connect"] for item in endpoint_rows),
        "connectivity": {
            "schema": "pcbdraft-schematic-connectivity",
            "version": 1,
            "status": "evaluated",
            "endpoints": endpoint_rows,
            "partitions": partitions,
            "unmapped_components": [],
            "unmapped_labels": [],
            "unmapped_no_connects": [],
        },
    }


def _managed(path: Path) -> SimpleNamespace:
    design = Design.from_dict(load_json_limited(path / "mock-design.json", 1024 * 1024))
    return SimpleNamespace(
        root=path,
        design=design,
        graph=_Graph(),
        requirements_path=path / "requirements.json",
        schematic_path=path / "mock.kicad_sch",
        board_path=path / "mock.kicad_pcb",
        plan=None,
        manifest={
            "hashes": {"ir": design.content_hash()},
            "files": {},
            "native_snapshots": {
                "board": _native_board_snapshot(design),
                "schematic": _native_schematic_snapshot(design),
                "project": {},
            },
        },
        drift=lambda: (),
        assert_synchronized=lambda: None,
    )


def _passing_consistency(revision: int = 1) -> NativeConsistencyReport:
    return NativeConsistencyReport(
        revision,
        "evaluated",
        "evaluated",
        "not_evaluated",
        (),
    )


def _materialize_project(
    request: object,
    design: Design,
    output: Path,
    **kwargs: object,
) -> SimpleNamespace:
    del request, kwargs
    output.mkdir(parents=True)
    atomic_write_json(output / "mock-design.json", design.to_dict())
    routing = RoutingResult(
        segments=(RouteSegment("OUT", 0, 1.0, 1.0, 2.0, 1.0, 0.25),),
        vias=(),
        unrouted=(),
        state="completed",
        expanded_nodes=1,
        diagnostics=(),
    )
    return SimpleNamespace(
        project=_managed(output), pcb=SimpleNamespace(routing=routing)
    )


def _seed_managed_project(
    root: Path, *, design: Design | None = None
) -> tuple[ApplicationService, str, Design]:
    provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
    service = ApplicationService(root, provider=provider)
    draft = service.create_draft("Board")
    project_id = str(draft["project"]["id"])
    design = design or _v2_design()
    project = service._open(project_id)
    project.design_root.mkdir()
    atomic_write_json(project.design_root / "mock-design.json", design.to_dict())
    return service, project_id, design


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _generic_led_part(*, footprint_pad: str = "1") -> dict[str, Any]:
    return {
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
                "footprint_pad": footprint_pad,
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


class FlatPCBServiceTests(unittest.TestCase):
    def test_transaction_inspect_rejects_a_symlinked_transaction_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp) / "project"
            outside = Path(temp) / "outside"
            project_root.mkdir()
            outside.mkdir()
            (project_root / "transactions").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(ValidationError, "artifact is unavailable"):
                ApplicationService._inspect_transaction_artifact(
                    SimpleNamespace(root=project_root),
                    "transaction:20260823T120000Z-1234abcd",
                )

    def test_transaction_inspect_is_current_project_scoped_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, _design = _seed_managed_project(Path(temp))
            project = service._open(project_id)
            transaction_id = "20260823T120000Z-1234abcd"
            transaction = project.root / "transactions" / transaction_id
            transaction.mkdir(parents=True)
            atomic_write_json(
                transaction / "receipt.json",
                {
                    "schema": "pcbdraft-flat-operation-receipt",
                    "version": 2,
                    "status": "failed",
                    "operation": "route_net",
                    "error_code": "native_connectivity_failed",
                    "failure": "target endpoints remain disconnected",
                    "baseline_revision": 0,
                    "candidate_revision": 1,
                    "committed_revision": None,
                    "rollback_performed": False,
                    "rollback": {
                        "state": "not_required",
                        "performed": False,
                        "live_unchanged": True,
                    },
                    "postconditions": [
                        {"name": f"condition_{index}", "passed": False}
                        for index in range(20)
                    ],
                    "artifact": {
                        "receipt": "receipt.json",
                        "native_consistency": "native-consistency.json",
                    },
                },
            )

            with patch(
                "pcbdraft.services.application.open_managed_project",
                side_effect=lambda path: _managed(Path(path)),
            ):
                inspected = service.execute_pcb_tool(
                    project_id,
                    "inspect_transaction",
                    {"artifact_id": f"transaction:{transaction_id}"},
                    timeout=1.0,
                    expected_revision=0,
                )["tool_result"]

            self.assertEqual(inspected["artifact_id"], f"transaction:{transaction_id}")
            self.assertEqual(
                inspected["detail"]["error_code"], "native_connectivity_failed"
            )
            self.assertEqual(len(inspected["detail"]["postconditions"]), 16)
            self.assertTrue(inspected["detail_truncated"])
            self.assertEqual(
                inspected["detail"]["available_details"],
                ["native_consistency", "receipt"],
            )
            self.assertNotIn(
                str(project.root),
                str(inspected),
            )

            with patch(
                "pcbdraft.services.application.open_managed_project",
                side_effect=lambda path: _managed(Path(path)),
            ):
                for invalid in (
                    "../transaction:20260823T120000Z-1234abcd",
                    "transaction:../../other-project",
                    "20260823T120000Z-1234abcd",
                ):
                    with (
                        self.subTest(artifact_id=invalid),
                        self.assertRaisesRegex(ValidationError, "identity is invalid"),
                    ):
                        service.execute_pcb_tool(
                            project_id,
                            "inspect_transaction",
                            {"artifact_id": invalid},
                            timeout=1.0,
                            expected_revision=0,
                        )

            def inspect_transaction(artifact_id: str) -> dict[str, Any]:
                with patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ):
                    return service.execute_pcb_tool(
                        project_id,
                        "inspect_transaction",
                        {"artifact_id": artifact_id},
                        timeout=1.0,
                        expected_revision=0,
                    )

            symlink_id = "20260823T120001Z-1234abcd"
            (project.root / "transactions" / symlink_id).symlink_to(
                transaction, target_is_directory=True
            )
            with self.assertRaisesRegex(ValidationError, "artifact is unavailable"):
                inspect_transaction(f"transaction:{symlink_id}")

            oversized_id = "20260823T120002Z-1234abcd"
            oversized = project.root / "transactions" / oversized_id
            oversized.mkdir()
            atomic_write_json(
                oversized / "receipt.json",
                {
                    "schema": "pcbdraft-flat-operation-receipt",
                    "version": 2,
                    "status": "failed",
                    "operation": "route_net",
                    "failure": "x" * (257 * 1024),
                },
            )
            with self.assertRaisesRegex(PCBDraftError, "byte limit"):
                inspect_transaction(f"transaction:{oversized_id}")

            malformed_id = "20260823T120003Z-1234abcd"
            malformed = project.root / "transactions" / malformed_id
            malformed.mkdir()
            atomic_write_json(
                malformed / "receipt.json",
                {
                    "schema": "pcbdraft-flat-operation-receipt",
                    "version": 999,
                    "status": "failed",
                    "operation": "route_net",
                },
            )
            with self.assertRaisesRegex(ValidationError, "receipt is invalid"):
                inspect_transaction(f"transaction:{malformed_id}")

            deep_id = "20260823T120004Z-1234abcd"
            deep = project.root / "transactions" / deep_id
            deep.mkdir()
            nested: dict[str, Any] = {"leaf": True}
            for _index in range(12):
                nested = {"nested": nested}
            atomic_write_json(
                deep / "receipt.json",
                {
                    "schema": "pcbdraft-flat-operation-receipt",
                    "version": 2,
                    "status": "failed",
                    "operation": "route_net",
                    "routing_failure": nested,
                },
            )
            with self.assertRaisesRegex(ValidationError, "receipt is invalid"):
                inspect_transaction(f"transaction:{deep_id}")

            other = service.create_empty_project("Other project")
            other_project_id = str(other["project"]["id"])
            other_project = service._open(other_project_id)
            other_id = "20260823T120005Z-1234abcd"
            other_transaction = other_project.root / "transactions" / other_id
            other_transaction.mkdir(parents=True)
            atomic_write_json(
                other_transaction / "receipt.json",
                {
                    "schema": "pcbdraft-flat-operation-receipt",
                    "version": 2,
                    "status": "failed",
                    "operation": "route_net",
                },
            )
            with self.assertRaisesRegex(ValidationError, "artifact is unavailable"):
                inspect_transaction(f"transaction:{other_id}")

    def test_connect_group_commits_one_revision_and_one_native_materialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(
                Path(temp), design=_group_design()
            )
            materialize = Mock(side_effect=_materialize_project)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
            ):
                result = service.apply_pcb_operation(
                    project_id,
                    "connect_group",
                    _connect_group_arguments(),
                    timeout=12.0,
                    expected_revision=0,
                )

            committed = service._open(project_id)
            design = _managed(committed.design_root).design
            connected = {
                (endpoint.component, endpoint.pin): net.id
                for net in design.nets
                for endpoint in net.endpoints
            }
            self.assertEqual(connected[("load_r2", "1")], "net_3v3")
            self.assertEqual(connected[("load_r2", "2")], "net_out")
            self.assertEqual(committed.state["revision"], 1)
            self.assertEqual(committed.state["design_revision"], 1)
            materialize.assert_called_once()
            tool_result = result["tool_result"]
            self.assertEqual(
                tool_result["transaction_scope"],
                {"kind": "connect_group", "entry_count": 2},
            )
            self.assertNotIn("connections", tool_result)
            self.assertEqual(tool_result["progress_before"]["source_revision"], 0)
            self.assertEqual(tool_result["progress_after"]["source_revision"], 1)
            self.assertIn("classification", tool_result["progress_delta"])
            transaction = (
                committed.root / "transactions" / tool_result["transaction_id"]
            )
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertEqual(receipt["status"], "applied")
            self.assertEqual(receipt["candidate_revision"], 1)
            native_delta = load_json_limited(
                transaction / "native-operation-delta.json", 1024 * 1024
            )
            self.assertEqual(native_delta["policy"], "connectivity_group")
            self.assertTrue(native_delta["passed"])
            self.assertNotEqual(design, before)

    def test_place_group_commits_without_per_write_drc(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, _before = _seed_managed_project(
                Path(temp), design=_group_design()
            )
            materialize = Mock(side_effect=_materialize_project)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
                patch(
                    "pcbdraft.services.application.run_drc_evidence",
                    side_effect=AssertionError(
                        "physical writes must not invoke run_drc_evidence"
                    ),
                    create=True,
                ),
            ):
                result = service.apply_pcb_operation(
                    project_id,
                    "place_group",
                    _place_group_arguments(),
                    timeout=12.0,
                    expected_revision=0,
                )

            project = service._open(project_id)
            components = {
                item.id: item
                for item in _managed(project.design_root).design.components
            }
            self.assertEqual(components["load_r"].placement.x_mm, 6.0)
            self.assertEqual(components["load_r2"].placement.x_mm, 14.0)
            self.assertEqual(components["load_r2"].placement.side, "back")
            materialize.assert_called_once()
            self.assertEqual(
                result["tool_result"]["transaction_scope"],
                {"kind": "place_group", "entry_count": 2},
            )
            transaction = (
                project.root / "transactions" / result["tool_result"]["transaction_id"]
            )
            native_delta = load_json_limited(
                transaction / "native-operation-delta.json", 1024 * 1024
            )
            self.assertEqual(native_delta["policy"], "footprint_transform_group")
            self.assertTrue(native_delta["passed"])
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertNotIn("drc_delta", receipt)
            self.assertNotIn("drc_before", receipt["artifact"])
            self.assertNotIn("drc_after", receipt["artifact"])

    def test_group_prevalidation_rejects_invalid_middle_entry_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(
                Path(temp), design=_group_design()
            )
            original = service._open(project_id)
            before_state = dict(original.state)
            materialize = Mock()
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize,
                ),
                self.assertRaisesRegex(
                    ValidationError, "semantic_transaction_invalid_endpoint"
                ),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "connect_group",
                    _connect_group_arguments(invalid_middle=True),
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, before_state)
            self.assertEqual(_managed(restored.design_root).design, before)
            materialize.assert_not_called()
            transactions = restored.root / "transactions"
            self.assertFalse(transactions.exists() and any(transactions.iterdir()))

    def test_group_rejects_bounds_and_stale_revision_before_materialization(
        self,
    ) -> None:
        design = _group_design()
        graph = PartGraph.bundled().with_footprint_overrides(design)
        out_of_bounds = _place_group_arguments()
        out_of_bounds["placements"]["entries"][1]["x_mm"] = 21.0
        with self.assertRaisesRegex(
            ValidationError, "semantic_transaction_out_of_bounds"
        ):
            ApplicationService._flat_semantic_operations(
                "place_group", out_of_bounds, design, graph=graph
            )

        existing = design.components[0].placement
        self.assertIsNotNone(existing)
        assert existing is not None
        equivalent_rotation = {
            "placements": {
                "entries": [
                    {
                        "component_id": design.components[0].id,
                        "x_mm": existing.x_mm,
                        "y_mm": existing.y_mm,
                        "rotation_deg": existing.rotation_deg + 360.0,
                        "side": existing.side,
                    }
                ]
            }
        }
        with self.assertRaisesRegex(ValidationError, "semantic_transaction_duplicate"):
            ApplicationService._flat_semantic_operations(
                "place_group", equivalent_rotation, design, graph=graph
            )

        with tempfile.TemporaryDirectory() as temp:
            service, project_id, _before = _seed_managed_project(
                Path(temp), design=design
            )
            materialize = Mock()
            with (
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize,
                ),
                self.assertRaisesRegex(ValidationError, "project changed before"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "connect_group",
                    _connect_group_arguments(),
                    timeout=12.0,
                    expected_revision=1,
                )
            materialize.assert_not_called()

    def test_group_resolves_all_targets_and_rejects_retained_copper_up_front(
        self,
    ) -> None:
        design = _group_design()
        graph = PartGraph.bundled().with_footprint_overrides(design)
        connection = _connect_group_arguments()
        invalid_connections = {
            "net": {**connection["connections"]["entries"][0], "net_id": "missing"},
            "component": {
                **connection["connections"]["entries"][0],
                "component_id": "missing",
            },
            "pin": {**connection["connections"]["entries"][0], "pin": "missing"},
        }
        for kind, entry in invalid_connections.items():
            with (
                self.subTest(missing=kind),
                self.assertRaisesRegex(
                    ValidationError,
                    "semantic_transaction_invalid_net"
                    if kind == "net"
                    else "semantic_transaction_invalid_endpoint",
                ),
            ):
                ApplicationService._flat_semantic_operations(
                    "connect_group",
                    {"connections": {"entries": [entry]}},
                    design,
                    graph=graph,
                )

        no_footprint = {
            "placements": {
                "entries": [
                    {
                        "component_id": "source_flag",
                        "x_mm": 2.0,
                        "y_mm": 2.0,
                        "rotation_deg": 0.0,
                        "side": "front",
                    }
                ]
            }
        }
        with self.assertRaisesRegex(
            ValidationError, "semantic_transaction_invalid_component"
        ):
            ApplicationService._flat_semantic_operations(
                "place_group", no_footprint, design, graph=graph
            )

        routed_value = design.to_dict()
        routed_value["native_intent"]["routes"] = [
            {
                "id": "route_out",
                "net": "net_out",
                "layer": 0,
                "x1_mm": 10.0,
                "y1_mm": 10.0,
                "x2_mm": 12.0,
                "y2_mm": 10.0,
                "width_mm": 0.25,
            }
        ]
        routed = Design.from_dict(routed_value)
        with self.assertRaisesRegex(ValidationError, "semantic_transaction_conflict"):
            ApplicationService._flat_semantic_operations(
                "place_group",
                {
                    "placements": {
                        "entries": [
                            _place_group_arguments()["placements"]["entries"][0]
                        ]
                    }
                },
                routed,
                graph=PartGraph.bundled().with_footprint_overrides(routed),
            )

    def test_connect_group_native_failure_keeps_the_entire_group_uncommitted(
        self,
    ) -> None:
        mismatch = NativeConsistencyReport(
            1,
            "evaluated",
            "evaluated",
            "not_evaluated",
            (
                NativeMismatch(
                    "native_net_split",
                    "schematic",
                    "net_out",
                    "one partition",
                    "two partitions",
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(
                Path(temp), design=_group_design()
            )
            original_state = dict(service._open(project_id).state)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=mismatch,
                ),
                self.assertRaisesRegex(
                    ValidationError, "native KiCad consistency postcondition failed"
                ),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "connect_group",
                    _connect_group_arguments(),
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original_state)
            self.assertEqual(_managed(restored.design_root).design, before)
            receipt_path = next((restored.root / "transactions").glob("*/receipt.json"))
            receipt = load_json_limited(receipt_path, 1024 * 1024)
            self.assertEqual(receipt["error_code"], "native_consistency_failed")
            self.assertTrue(receipt["rollback"]["live_unchanged"])

    def test_connect_group_materialization_failure_keeps_group_uncommitted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(
                Path(temp), design=_group_design()
            )
            original_state = dict(service._open(project_id).state)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=ValidationError("injected group native failure"),
                ),
                self.assertRaisesRegex(ValidationError, "group native failure"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "connect_group",
                    _connect_group_arguments(),
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original_state)
            self.assertEqual(_managed(restored.design_root).design, before)
            receipt_path = next((restored.root / "transactions").glob("*/receipt.json"))
            receipt = load_json_limited(receipt_path, 1024 * 1024)
            self.assertEqual(receipt["error_code"], "native_materialization_failed")
            self.assertEqual(
                receipt["transaction_scope"],
                {"kind": "connect_group", "entry_count": 2},
            )
            self.assertTrue(receipt["rollback"]["live_unchanged"])
            self.assertNotIn("native_consistency", receipt["artifact"])

    def test_connect_group_publication_failure_restores_the_entire_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(
                Path(temp), design=_group_design()
            )
            original_state = dict(service._open(project_id).state)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
                patch.object(
                    service,
                    "_write_records",
                    side_effect=PCBDraftError("injected group publication failure"),
                ),
                self.assertRaisesRegex(PCBDraftError, "group publication failure"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "connect_group",
                    _connect_group_arguments(),
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original_state)
            self.assertEqual(_managed(restored.design_root).design, before)
            receipt_path = next((restored.root / "transactions").glob("*/receipt.json"))
            receipt = load_json_limited(receipt_path, 1024 * 1024)
            self.assertEqual(receipt["error_code"], "publication_failed")
            self.assertTrue(receipt["rollback_performed"])
            self.assertEqual(receipt["rollback"]["state"], "restored")

    def test_legacy_native_snapshot_is_reinspected_before_delta_use(self) -> None:
        from pcbdraft.services import application as application_module

        design = _v2_design()
        legacy = _native_board_snapshot(design)
        for component in legacy["components"]:
            for key in ("x_mm", "y_mm", "rotation_deg", "side"):
                component.pop(key)
        legacy["board"] = {"layers": design.board.layers}
        managed = SimpleNamespace(
            manifest={"native_snapshots": {"board": legacy}},
            design=design,
            board_path=Path("legacy.kicad_pcb"),
        )
        current = _native_board_snapshot(design)

        with patch.object(
            application_module, "inspect_native_board", return_value=current
        ) as inspect:
            projection = application_module._native_board_projection(managed)

        inspect.assert_called_once_with(
            design,
            managed.board_path,
            include_connectivity=True,
        )
        self.assertEqual(
            {item.reference for item in projection.footprint_poses},
            set(projection.components),
        )
        self.assertTrue(projection.board_rules)

        with (
            patch.object(
                application_module,
                "inspect_native_board",
                side_effect=PCBDraftError("native reinspection unavailable"),
            ),
            self.assertRaisesRegex(PCBDraftError, "reinspection unavailable"),
        ):
            application_module._native_board_projection(managed)

        with (
            patch.object(
                application_module,
                "inspect_native_board",
                return_value=legacy,
            ),
            self.assertRaisesRegex(
                ValidationError,
                "board reinspection lacks operation-delta evidence",
            ),
        ):
            application_module._native_board_projection(managed)

        legacy_schematic = _native_schematic_snapshot(design)
        legacy_schematic.pop("connectivity")
        schematic_managed = SimpleNamespace(
            manifest={"native_snapshots": {"schematic": legacy_schematic}},
            schematic_path=Path("legacy.kicad_sch"),
        )
        with (
            patch.object(
                application_module,
                "inspect_native_schematic",
                return_value=legacy_schematic,
            ),
            self.assertRaisesRegex(
                ValidationError,
                "schematic reinspection lacks operation-delta evidence",
            ),
        ):
            application_module._native_schematic_projection(schematic_managed)

    def test_model_symbol_search_returns_ids_without_describing_candidates(
        self,
    ) -> None:
        with patch(
            "pcbdraft.agent.part_resolver.LocalKiCadPartResolver"
        ) as resolver_type:
            resolver = resolver_type.return_value
            resolver.find_ids.return_value = ("Device:R", "Device:R_US")

            facts = ApplicationService.inspect_installed_library(
                "search_symbols", {"query": "Device:R"}
            )

        self.assertEqual(facts["symbols"], ["Device:R", "Device:R_US"])
        resolver.find_ids.assert_called_once_with("Device:R", limit=24)
        resolver.describe.assert_not_called()

    def test_bad_installed_part_mapping_retains_failure_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temp), provider=provider)
            view = service.create_empty_project("Bad mapping")
            project_id = str(view["project"]["id"])
            project = service._open(project_id)
            before_state = dict(project.state)
            before_design = _tree_bytes(project.design_root)

            with self.assertRaisesRegex(ValidationError, "missing footprint pad"):
                service.register_kicad_part(
                    project_id,
                    _generic_led_part(footprint_pad="99"),
                    timeout=30.0,
                    expected_revision=int(project.state["revision"]),
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, before_state)
            self.assertEqual(_tree_bytes(restored.design_root), before_design)
            receipt_path = next((restored.root / "transactions").glob("*/receipt.json"))
            receipt = load_json_limited(receipt_path, 1024 * 1024)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(
                receipt["schema"],
                "pcbdraft-kicad-part-registration-receipt",
            )
            self.assertEqual(receipt["progress_delta"]["classification"], "neutral")
            self.assertEqual(
                receipt["progress_before"]["source_revision"],
                receipt["progress_after"]["source_revision"],
            )

    def test_part_record_publication_failure_rolls_back_catalog_and_design(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temp), provider=provider)
            view = service.create_empty_project("Publication rollback")
            project_id = str(view["project"]["id"])
            project = service._open(project_id)
            before_state = dict(project.state)
            before_design = _tree_bytes(project.design_root)

            with (
                patch.object(
                    service,
                    "_write_records",
                    side_effect=PCBDraftError("injected part record failure"),
                ),
                self.assertRaisesRegex(PCBDraftError, "injected part record failure"),
            ):
                service.register_kicad_part(
                    project_id,
                    _generic_led_part(),
                    timeout=60.0,
                    expected_revision=int(project.state["revision"]),
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, before_state)
            self.assertEqual(_tree_bytes(restored.design_root), before_design)
            receipt_path = next((restored.root / "transactions").glob("*/receipt.json"))
            receipt = load_json_limited(receipt_path, 1024 * 1024)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["progress_delta"]["classification"], "neutral")
            self.assertTrue(receipt["rollback"]["live_unchanged"])

    def test_part_record_and_failure_receipt_errors_never_leave_applied_claim(
        self,
    ) -> None:
        from pcbdraft.services import application as application_module

        with tempfile.TemporaryDirectory() as temp:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temp), provider=provider)
            view = service.create_empty_project("Double publication failure")
            project_id = str(view["project"]["id"])
            project = service._open(project_id)
            before_state = dict(project.state)
            before_design = _tree_bytes(project.design_root)
            real_write = application_module.atomic_write_json

            def reject_failed_publication_receipt(
                path: Path, value: Any, *args: Any, **kwargs: Any
            ) -> None:
                if (
                    path.name == "receipt.json"
                    and isinstance(value, dict)
                    and value.get("status") == "failed"
                    and value.get("error_code") == "publication_failed"
                ):
                    raise PCBDraftError("injected failed receipt write failure")
                real_write(path, value, *args, **kwargs)

            with (
                patch.object(
                    service,
                    "_write_records",
                    side_effect=PCBDraftError("injected part record failure"),
                ),
                patch.object(
                    application_module,
                    "atomic_write_json",
                    side_effect=reject_failed_publication_receipt,
                ),
                self.assertRaisesRegex(PCBDraftError, "part record failure"),
            ):
                service.register_kicad_part(
                    project_id,
                    _generic_led_part(),
                    timeout=60.0,
                    expected_revision=int(project.state["revision"]),
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, before_state)
            self.assertEqual(_tree_bytes(restored.design_root), before_design)
            receipt_path = next((restored.root / "transactions").glob("*/receipt.json"))
            receipt = load_json_limited(receipt_path, 1024 * 1024)
            self.assertNotEqual(receipt["status"], "applied")
            self.assertNotIn("applied_at", receipt)

    def test_identical_part_noop_rechecks_revision_under_project_lock(self) -> None:
        from pcbdraft.services import application as application_module

        with tempfile.TemporaryDirectory() as temp:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temp), provider=provider)
            view = service.create_empty_project("No-op CAS")
            project_id = str(view["project"]["id"])
            with patch.object(
                application_module,
                "compare_native_operation_delta",
                wraps=application_module.compare_native_operation_delta,
            ) as native_delta:
                view = service.register_kicad_part(
                    project_id,
                    _generic_led_part(),
                    timeout=60.0,
                    expected_revision=int(view["state"]["revision"]),
                )
            self.assertIsNotNone(native_delta.call_args.kwargs["before_schematic"])
            self.assertIsNotNone(native_delta.call_args.kwargs["after_schematic"])
            project = service._open(project_id)
            registration_receipt_path = next(
                (project.root / "transactions").glob("*/receipt.json")
            )
            registration_receipt = load_json_limited(
                registration_receipt_path, 1024 * 1024
            )
            self.assertEqual(registration_receipt["version"], 2)
            self.assertNotIn("manifest_hashes", registration_receipt)
            self.assertEqual(
                registration_receipt["native_scope"],
                "catalog_plus_native_rematerialization",
            )
            self.assertTrue(registration_receipt["consistency_passed"])
            self.assertTrue(registration_receipt["native_delta"]["passed"])
            self.assertEqual(
                registration_receipt["progress_after"]["source_revision"],
                registration_receipt["progress_before"]["source_revision"] + 1,
            )
            transaction_root = registration_receipt_path.parent
            self.assertTrue((transaction_root / "native-consistency.json").is_file())
            self.assertTrue(
                (transaction_root / "native-operation-delta.json").is_file()
            )

            transaction_ids = {
                item.name for item in (project.root / "transactions").iterdir()
            }
            noop = service.register_kicad_part(
                project_id,
                _generic_led_part(),
                timeout=60.0,
                expected_revision=int(view["state"]["revision"]),
            )
            self.assertFalse(noop["tool_result"]["changed"])
            new_transaction = next(
                item
                for item in (project.root / "transactions").iterdir()
                if item.name not in transaction_ids
            )
            noop_receipt = load_json_limited(
                new_transaction / "receipt.json", 1024 * 1024
            )
            self.assertEqual(noop_receipt["status"], "noop")
            self.assertEqual(
                noop_receipt["native_scope"], "catalog_noop_no_native_write"
            )
            expected_revision = int(view["state"]["revision"])

            real_open_managed = application_module.open_managed_project
            calls = 0

            def open_and_inject_concurrent_revision(path: Path) -> Any:
                nonlocal calls
                managed = real_open_managed(path)
                calls += 1
                if calls == 1:
                    concurrent = service._open(project_id)
                    concurrent.state["revision"] += 1
                    atomic_write_json(
                        concurrent.root / "project.json", concurrent.state
                    )
                return managed

            with (
                patch.object(
                    application_module,
                    "open_managed_project",
                    side_effect=open_and_inject_concurrent_revision,
                ),
                self.assertRaisesRegex(
                    ValidationError,
                    "changed while part registration was inspected",
                ),
            ):
                service.register_kicad_part(
                    project_id,
                    _generic_led_part(),
                    timeout=60.0,
                    expected_revision=expected_revision,
                )

    def test_flat_publish_failure_restores_design_and_application_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            original = service._open(project_id)
            original_state = dict(original.state)

            def materialize(
                request: object,
                design: Design,
                output: Path,
                **kwargs: object,
            ) -> SimpleNamespace:
                del request, kwargs
                output.mkdir(parents=True)
                atomic_write_json(output / "mock-design.json", design.to_dict())
                return SimpleNamespace(project=_managed(output))

            materialize_mock = Mock(side_effect=materialize)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize_mock,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
                patch.object(
                    service,
                    "_write_records",
                    side_effect=PCBDraftError("injected record publication failure"),
                ),
                self.assertRaisesRegex(
                    PCBDraftError, "injected record publication failure"
                ),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original_state)
            self.assertEqual(_managed(restored.design_root).design, before)
            self.assertEqual(list((restored.root / "events").iterdir()), [])
            transaction = next((restored.root / "transactions").iterdir())
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["version"], 2)
            self.assertEqual(receipt["error_code"], "publication_failed")
            self.assertTrue(receipt["rollback_performed"])
            self.assertEqual(receipt["rollback"]["state"], "restored")
            self.assertTrue(receipt["consistency_passed"])
            kwargs = materialize_mock.call_args.kwargs
            self.assertIs(kwargs["auto_place"], False)
            self.assertEqual(kwargs["route_net_ids"], frozenset())
            self.assertIs(kwargs["allow_incomplete"], True)

    def test_flat_event_publication_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            original = service._open(project_id)
            real_event = service._event

            def write_event_then_fail(*args: Any, **kwargs: Any) -> None:
                real_event(*args, **kwargs)
                raise PCBDraftError("injected event publication failure")

            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
                patch.object(
                    service,
                    "_event",
                    side_effect=write_event_then_fail,
                ),
                self.assertRaisesRegex(PCBDraftError, "event publication failure"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original.state)
            self.assertEqual(_managed(restored.design_root).design, before)
            self.assertEqual(list((restored.root / "events").iterdir()), [])
            transaction = next((restored.root / "transactions").iterdir())
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertEqual(receipt["error_code"], "publication_failed")
            self.assertEqual(receipt["rollback"]["state"], "restored")
            self.assertIsNone(receipt["committed_revision"])
            self.assertIsNone(receipt["committed_design_revision"])
            self.assertNotIn("applied_at", receipt)

    def test_flat_receipt_publication_failure_rolls_back(self) -> None:
        from pcbdraft.services import application as application_module

        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            original = service._open(project_id)
            real_write = application_module.atomic_write_json
            injected = False

            def fail_applied_receipt(
                path: Path, value: Any, *args: Any, **kwargs: Any
            ) -> None:
                nonlocal injected
                if (
                    not injected
                    and path.name == "receipt.json"
                    and isinstance(value, dict)
                    and value.get("status") == "applied"
                ):
                    injected = True
                    raise PCBDraftError("injected receipt publication failure")
                real_write(path, value, *args, **kwargs)

            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
                patch.object(
                    application_module,
                    "atomic_write_json",
                    side_effect=fail_applied_receipt,
                ),
                self.assertRaisesRegex(PCBDraftError, "receipt publication failure"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original.state)
            self.assertEqual(_managed(restored.design_root).design, before)
            transaction = next((restored.root / "transactions").iterdir())
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertEqual(receipt["error_code"], "publication_failed")
            self.assertEqual(receipt["rollback"]["state"], "restored")

    def test_flat_write_persists_native_consistency_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, _before = _seed_managed_project(Path(temp))
            initial = service._open(project_id)
            initial.state["revision"] = 3
            atomic_write_json(initial.root / "project.json", initial.state)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
            ):
                result = service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=3,
                )

            project = service._open(project_id)
            transaction = (
                project.root / "transactions" / result["tool_result"]["transaction_id"]
            )
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            native = NativeConsistencyReport.from_dict(
                load_json_limited(transaction / "native-consistency.json", 1024 * 1024)
            )
            self.assertEqual(receipt["status"], "applied")
            self.assertEqual(receipt["version"], 2)
            self.assertNotIn("manifest_hashes", receipt)
            self.assertTrue(receipt["consistency_passed"])
            self.assertTrue(native.consistency_passed)
            self.assertEqual(receipt["candidate_revision"], 1)
            self.assertEqual(receipt["committed_design_revision"], 1)
            self.assertEqual(native.candidate_revision, 1)
            self.assertEqual(project.state["revision"], 4)
            self.assertEqual(project.state["design_revision"], 1)
            self.assertEqual(
                receipt["progress_before"]["schema"], "pcbdraft-progress-vector"
            )
            self.assertEqual(receipt["progress_before"]["source_revision"], 0)
            self.assertEqual(receipt["progress_after"]["source_revision"], 1)
            self.assertIn(
                receipt["progress_delta"]["classification"],
                {"improved", "neutral", "regressed", "indeterminate"},
            )
            self.assertEqual(receipt["stage_after"]["release_gate_passed"], False)

    def test_failed_materialization_does_not_claim_missing_native_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, _before = _seed_managed_project(Path(temp))
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=ValidationError("injected materialization failure"),
                ),
                self.assertRaisesRegex(ValidationError, "materialization failure"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=0,
                )

            transaction = next(
                (service._open(project_id).root / "transactions").iterdir()
            )
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertEqual(receipt["status"], "failed")
            self.assertNotIn("native_consistency", receipt["artifact"])
            for name, relative_path in receipt["artifact"].items():
                if name == "transaction_id":
                    continue
                self.assertTrue((transaction / relative_path).is_file())

    def test_native_postcondition_failure_keeps_live_revision_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            original = service._open(project_id)
            mismatch = NativeConsistencyReport(
                1,
                "evaluated",
                "evaluated",
                "not_evaluated",
                (
                    NativeMismatch(
                        "native_net_split",
                        "schematic",
                        "net_out",
                        "one partition",
                        "two partitions",
                    ),
                ),
            )
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=mismatch,
                ),
                self.assertRaisesRegex(
                    ValidationError, "native KiCad consistency postcondition failed"
                ),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original.state)
            self.assertEqual(_managed(restored.design_root).design, before)
            transaction = next((restored.root / "transactions").iterdir())
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertEqual(receipt["error_code"], "native_consistency_failed")
            self.assertFalse(receipt["rollback_performed"])
            self.assertTrue(receipt["rollback"]["live_unchanged"])
            self.assertEqual(
                receipt["progress_before"]["source_revision"],
                receipt["progress_after"]["source_revision"],
            )
            self.assertEqual(receipt["progress_delta"]["classification"], "neutral")

    def test_native_parse_failure_persists_fail_closed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    side_effect=ValidationError("injected native parse failure"),
                ),
                self.assertRaisesRegex(ValidationError, "inspection failed"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "set_board_outline",
                    {"width_mm": 30.0, "height_mm": 24.0},
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(_managed(restored.design_root).design, before)
            transaction = next((restored.root / "transactions").iterdir())
            report = NativeConsistencyReport.from_dict(
                load_json_limited(transaction / "native-consistency.json", 1024 * 1024)
            )
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertFalse(report.consistency_passed)
            self.assertEqual(receipt["error_code"], "native_verification_failed")

    def test_route_native_false_success_is_rejected_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            original = service._open(project_id)
            mismatch = NativeConsistencyReport(
                1,
                "evaluated",
                "evaluated",
                "not_evaluated",
                (
                    NativeMismatch(
                        "native_zero_copper",
                        "board",
                        "net_out",
                        "non-zero native copper",
                        "absent",
                    ),
                    NativeMismatch(
                        "native_connectivity_failed",
                        "board",
                        "net_out",
                        "one connectivity component",
                        "two components",
                    ),
                ),
            )
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=mismatch,
                ),
                self.assertRaisesRegex(
                    ValidationError, "native_connectivity_failed"
                ) as caught,
            ):
                service.apply_pcb_operation(
                    project_id,
                    "route_net",
                    {"net_id": "net_out"},
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original.state)
            self.assertEqual(_managed(restored.design_root).design, before)
            transaction = next((restored.root / "transactions").iterdir())
            self.assertEqual(
                getattr(caught.exception, "transaction_id", None), transaction.name
            )
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertEqual(receipt["error_code"], "native_connectivity_failed")
            self.assertEqual(
                receipt["routing_failure"]["code"], "native_connectivity_failed"
            )
            self.assertEqual(
                receipt["progress_after"]["metrics"]["routing_failure_count"]["value"],
                1,
            )
            self.assertEqual(receipt["progress_delta"]["classification"], "regressed")
            self.assertFalse(
                next(
                    item["passed"]
                    for item in receipt["postconditions"]
                    if item["name"] == "native_nonzero_copper"
                )
            )

    def test_route_final_materialization_failure_has_native_commit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            original = service._open(project_id)
            materializations = 0

            def fail_final_materialization(
                request: object,
                design: Design,
                output: Path,
                **kwargs: object,
            ) -> SimpleNamespace:
                nonlocal materializations
                materializations += 1
                if materializations == 2:
                    raise ValidationError("injected final native failure")
                return _materialize_project(request, design, output, **kwargs)

            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=fail_final_materialization,
                ),
                self.assertRaisesRegex(ValidationError, "final native failure"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "route_net",
                    {"net_id": "net_out"},
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            self.assertEqual(restored.state, original.state)
            self.assertEqual(_managed(restored.design_root).design, before)
            transaction = next((restored.root / "transactions").iterdir())
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            self.assertEqual(receipt["error_code"], "native_commit_failed")
            self.assertEqual(receipt["routing_failure"]["code"], "native_commit_failed")

    def test_native_delta_operation_families_commit_or_roll_back(self) -> None:
        cases = (
            (
                "set_board_outline",
                {"width_mm": 30.0, "height_mm": 24.0},
                None,
            ),
            (
                "move_footprint",
                {"component_id": "load_r", "x_mm": 12.0, "y_mm": 11.0},
                None,
            ),
            (
                "add_via",
                {
                    "via_id": "via_out",
                    "net_id": "net_out",
                    "x_mm": 12.0,
                    "y_mm": 12.0,
                    "diameter_mm": 0.7,
                    "drill_mm": 0.35,
                    "from_layer": 0,
                    "to_layer": 1,
                },
                None,
            ),
            ("remove_via", {"via_id": "via_out"}, "via"),
            ("unroute_net", {"net_id": "net_out"}, "route"),
        )
        for tool_name, arguments, starting_geometry in cases:
            for should_commit in (True, False):
                with (
                    self.subTest(tool=tool_name, commit=should_commit),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    service, project_id, before = _seed_managed_project(Path(temp))
                    if starting_geometry is not None:
                        value = before.to_dict()
                        if starting_geometry == "via":
                            value["native_intent"]["vias"] = [
                                {
                                    "id": "via_out",
                                    "net": "net_out",
                                    "x_mm": 12.0,
                                    "y_mm": 12.0,
                                    "diameter_mm": 0.7,
                                    "drill_mm": 0.35,
                                    "from_layer": 0,
                                    "to_layer": 1,
                                }
                            ]
                        else:
                            value["native_intent"]["routes"] = [
                                {
                                    "id": "route_out",
                                    "net": "net_out",
                                    "layer": 0,
                                    "x1_mm": 10.0,
                                    "y1_mm": 10.0,
                                    "x2_mm": 12.0,
                                    "y2_mm": 10.0,
                                    "width_mm": 0.25,
                                }
                            ]
                        before = Design.from_dict(value)
                        project = service._open(project_id)
                        atomic_write_json(
                            project.design_root / "mock-design.json", before.to_dict()
                        )
                    original = service._open(project_id)
                    failure = NativeOperationDeltaReport(
                        tool_name,
                        (
                            NativeDeltaCheck(
                                "injected_native_delta",
                                False,
                                "expected native change",
                                "missing native change",
                            ),
                        ),
                    )
                    delta_context = (
                        nullcontext()
                        if should_commit
                        else patch(
                            "pcbdraft.services.application.compare_native_operation_delta",
                            return_value=failure,
                        )
                    )
                    with (
                        patch(
                            "pcbdraft.services.application.open_managed_project",
                            side_effect=lambda path: _managed(Path(path)),
                        ),
                        patch(
                            "pcbdraft.services.application.load_generation_request",
                            return_value=object(),
                        ),
                        patch(
                            "pcbdraft.services.application.materialize_managed_design",
                            side_effect=_materialize_project,
                        ),
                        patch(
                            "pcbdraft.services.application.inspect_native_consistency",
                            return_value=_passing_consistency(),
                        ),
                        delta_context,
                    ):
                        if should_commit:
                            service.apply_pcb_operation(
                                project_id,
                                tool_name,
                                arguments,
                                timeout=12.0,
                                expected_revision=0,
                            )
                        else:
                            with self.assertRaisesRegex(
                                ValidationError, "operation delta postcondition failed"
                            ):
                                service.apply_pcb_operation(
                                    project_id,
                                    tool_name,
                                    arguments,
                                    timeout=12.0,
                                    expected_revision=0,
                                )

                    restored = service._open(project_id)
                    transaction = next((restored.root / "transactions").iterdir())
                    receipt = load_json_limited(
                        transaction / "receipt.json", 1024 * 1024
                    )
                    if should_commit:
                        self.assertEqual(restored.state["revision"], 1)
                        self.assertTrue(receipt["native_delta"]["passed"])
                    else:
                        self.assertEqual(restored.state, original.state)
                        self.assertEqual(_managed(restored.design_root).design, before)
                        self.assertEqual(receipt["error_code"], "native_delta_failed")
                        self.assertTrue(receipt["rollback"]["live_unchanged"])

    def test_disconnect_empty_net_commits_with_native_projection_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            candidate_value = before.to_dict()
            next(item for item in candidate_value["nets"] if item["id"] == "net_out")[
                "endpoints"
            ] = []
            candidate = Design.from_dict(candidate_value)
            arguments = {
                "net_id": "net_out",
                "component_id": "load_r",
                "pin": "2",
                "role": "signal",
            }

            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
            ):
                result = service.apply_pcb_operation(
                    project_id,
                    "disconnect_pin",
                    arguments,
                    timeout=12.0,
                    expected_revision=0,
                )

            restored = service._open(project_id)
            transaction = next((restored.root / "transactions").iterdir())
            receipt = load_json_limited(transaction / "receipt.json", 1024 * 1024)
            projection = next(
                item
                for item in receipt["postconditions"]
                if item["name"] == "native_net_projection"
            )

            self.assertEqual(result["tool_result"]["operation"], "disconnect_pin")
            self.assertEqual(restored.state["revision"], 1)
            self.assertEqual(restored.state["design_revision"], 1)
            self.assertEqual(receipt["status"], "applied")
            self.assertEqual(receipt["operation"], "disconnect_pin")
            self.assertEqual(
                receipt["transaction_scope"],
                {"kind": "disconnect_pin", "entry_count": 1},
            )
            self.assertTrue(receipt["native_delta"]["passed"])
            self.assertEqual(receipt["native_delta"]["policy"], "connectivity")
            self.assertEqual(receipt["committed_revision"], 1)
            self.assertEqual(receipt["committed_design_revision"], 1)
            self.assertEqual(receipt["rollback"]["state"], "committed")
            self.assertFalse(receipt["rollback"]["live_unchanged"])
            self.assertTrue(projection["passed"])
            self.assertEqual(
                projection["expected"],
                "native_projection=not_applicable_empty_net,name=OUT",
            )
            self.assertIn("board_net=False", projection["observed"])
            self.assertIn("schematic_partitions=0", projection["observed"])
            self.assertIn("pads=0", projection["observed"])
            self.assertIn("copper=0", projection["observed"])
            self.assertEqual(_managed(restored.design_root).design, candidate)

    def test_remaining_native_operation_families_commit_or_roll_back(self) -> None:
        component = {
            "id": "load_r2",
            "reference": "R2",
            "part_id": "yageo.rc0603fr-074k7l",
            "value": "4.7k",
            "block_id": "power_block",
        }
        net = {
            "id": "net_aux",
            "name": "AUX",
            "net_class": "signal",
            "power_domain": None,
            "interface": None,
            "intent": "Auxiliary test net.",
        }
        pin = {
            "net_id": "net_out",
            "component_id": "load_r2",
            "pin": "1",
            "role": "signal",
        }
        cases = (
            (
                "update_board_rules",
                {
                    "changes": {
                        "entries": [
                            {"field": "min_track_mm", "value": 0.3},
                            {"field": "finish", "value": "enig"},
                        ]
                    }
                },
                None,
                "board_rules",
            ),
            (
                "assign_footprint",
                {
                    "component_id": "load_r",
                    "footprint": "Resistor_SMD:R_0805_2012Metric",
                },
                None,
                "footprint_assignment",
            ),
            (
                "update_component",
                {
                    "component_id": "load_r",
                    "changes": {
                        "entries": [
                            {"field": "value", "value": "10k"},
                            {"field": "block_id", "value": "power_block"},
                        ]
                    },
                },
                None,
                "component_update",
            ),
            ("add_component", {"value": component}, None, "component_add"),
            (
                "remove_component",
                {"component_id": "load_r2"},
                "component",
                "component_remove",
            ),
            ("add_net", {"value": net}, None, "net_add"),
            ("remove_net", {"net_id": "net_aux"}, "net", "net_remove"),
            ("connect_pin", pin, "component", "connectivity"),
            ("disconnect_pin", pin, "connected", "connectivity"),
            (
                "rename_net",
                {"net_id": "net_out", "name": "OUTPUT"},
                None,
                "net_rename",
            ),
            (
                "add_block",
                {
                    "value": {
                        "id": "spare_block",
                        "kind": "spare",
                        "name": "Spare",
                        "version": "1",
                        "intent": "Semantic grouping only.",
                    }
                },
                None,
                "semantic_only",
            ),
            (
                "update_constraint",
                {
                    "value": {
                        "id": "board_rules",
                        "kind": "manufacturing_rules",
                        "targets": ["board"],
                        "params": [{"name": "min_clearance_mm", "value": 0.25}],
                        "severity": "release_blocking",
                        "rationale": "Use the updated manufacturing clearance.",
                    }
                },
                None,
                "deferred_native_input",
            ),
        )
        for tool_name, arguments, setup, policy in cases:
            for should_commit in (True, False):
                with (
                    self.subTest(tool=tool_name, commit=should_commit),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    service, project_id, before = _seed_managed_project(Path(temp))
                    if setup is not None:
                        value = before.to_dict()
                        value["components"].append({**component, "attributes": {}})
                        value["blocks"][0]["components"].append("load_r2")
                        if setup == "net":
                            value["nets"].append({**net, "endpoints": []})
                        elif setup == "connected":
                            next(
                                item
                                for item in value["nets"]
                                if item["id"] == "net_out"
                            )["endpoints"].append(
                                {
                                    "component": "load_r2",
                                    "pin": "1",
                                    "role": "signal",
                                }
                            )
                        before = Design.from_dict(value)
                        project = service._open(project_id)
                        atomic_write_json(
                            project.design_root / "mock-design.json", before.to_dict()
                        )
                    original = service._open(project_id)
                    original_tree = _tree_bytes(original.design_root)
                    failure = NativeOperationDeltaReport(
                        tool_name,
                        (
                            NativeDeltaCheck(
                                "injected_native_delta",
                                False,
                                "expected native change",
                                "missing native change",
                            ),
                        ),
                        policy,
                    )
                    delta_context = (
                        nullcontext()
                        if should_commit
                        else patch(
                            "pcbdraft.services.application.compare_native_operation_delta",
                            return_value=failure,
                        )
                    )
                    with (
                        patch(
                            "pcbdraft.services.application.open_managed_project",
                            side_effect=lambda path: _managed(Path(path)),
                        ),
                        patch(
                            "pcbdraft.services.application.load_generation_request",
                            return_value=object(),
                        ),
                        patch(
                            "pcbdraft.services.application.materialize_managed_design",
                            side_effect=_materialize_project,
                        ),
                        patch(
                            "pcbdraft.services.application.inspect_native_consistency",
                            return_value=_passing_consistency(),
                        ),
                        delta_context,
                    ):
                        if should_commit:
                            service.apply_pcb_operation(
                                project_id,
                                tool_name,
                                arguments,
                                timeout=12.0,
                                expected_revision=0,
                            )
                        else:
                            with self.assertRaisesRegex(
                                ValidationError,
                                "operation delta postcondition failed",
                            ):
                                service.apply_pcb_operation(
                                    project_id,
                                    tool_name,
                                    arguments,
                                    timeout=12.0,
                                    expected_revision=0,
                                )

                    restored = service._open(project_id)
                    transaction = next((restored.root / "transactions").iterdir())
                    receipt = load_json_limited(
                        transaction / "receipt.json", 1024 * 1024
                    )
                    if should_commit:
                        self.assertEqual(restored.state["revision"], 1)
                        self.assertEqual(receipt["native_delta"]["policy"], policy)
                        self.assertTrue(receipt["native_delta"]["passed"])
                        if tool_name == "add_net":
                            projection = next(
                                item
                                for item in receipt["postconditions"]
                                if item["name"] == "native_net_projection"
                            )
                            self.assertEqual(
                                projection["expected"],
                                "native_projection=not_applicable_empty_net,name=AUX",
                            )
                            self.assertIn("board_net=False", projection["observed"])
                    else:
                        self.assertEqual(restored.state, original.state)
                        self.assertEqual(
                            _tree_bytes(restored.design_root), original_tree
                        )
                        self.assertEqual(_managed(restored.design_root).design, before)
                        self.assertIsNone(receipt["committed_revision"])
                        self.assertEqual(receipt["error_code"], "native_delta_failed")
                        self.assertFalse(receipt["rollback_performed"])
                        self.assertEqual(receipt["rollback"]["state"], "not_required")
                        self.assertTrue(receipt["rollback"]["live_unchanged"])

    def test_routed_footprint_transform_is_rejected_before_materialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, before = _seed_managed_project(Path(temp))
            value = before.to_dict()
            value["native_intent"]["routes"] = [
                {
                    "id": "route_out",
                    "net": "net_out",
                    "layer": 0,
                    "x1_mm": 10.0,
                    "y1_mm": 10.0,
                    "x2_mm": 12.0,
                    "y2_mm": 10.0,
                    "width_mm": 0.25,
                }
            ]
            before = Design.from_dict(value)
            project = service._open(project_id)
            atomic_write_json(
                project.design_root / "mock-design.json", before.to_dict()
            )
            original = service._open(project_id)
            materialize = Mock(side_effect=_materialize_project)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize,
                ),
            ):
                cases = (
                    (
                        "move_footprint",
                        {"component_id": "load_r", "x_mm": 14.0, "y_mm": 10.0},
                    ),
                    (
                        "rotate_footprint",
                        {"component_id": "load_r", "rotation_deg": 90.0},
                    ),
                    ("unplace_footprint", {"component_id": "load_r"}),
                    (
                        "assign_footprint",
                        {
                            "component_id": "load_r",
                            "footprint": "Resistor_SMD:R_0805_2012Metric",
                        },
                    ),
                )
                for tool_name, arguments in cases:
                    with (
                        self.subTest(tool=tool_name),
                        self.assertRaisesRegex(
                            ValidationError,
                            "blocked until associated retained copper",
                        ),
                    ):
                        service.apply_pcb_operation(
                            project_id,
                            tool_name,
                            arguments,
                            timeout=12.0,
                            expected_revision=0,
                        )

            materialize.assert_not_called()
            restored = service._open(project_id)
            self.assertEqual(restored.state, original.state)
            self.assertEqual(_managed(restored.design_root).design, before)
            receipts = [
                load_json_limited(path, 1024 * 1024)
                for path in (restored.root / "transactions").glob("*/receipt.json")
            ]
            self.assertEqual(
                [
                    receipt["error_code"]
                    for receipt in receipts
                    if receipt["operation"]
                    in {
                        "assign_footprint",
                        "move_footprint",
                        "rotate_footprint",
                        "unplace_footprint",
                    }
                ],
                ["routed_footprint_transform_unsupported"] * 4,
            )

    def test_route_tool_selects_only_its_requested_net_for_materialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, _before = _seed_managed_project(Path(temp))
            materialize_mock = Mock(side_effect=ValidationError("routing stopped"))
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize_mock,
                ),
                self.assertRaisesRegex(ValidationError, "routing stopped"),
            ):
                service.apply_pcb_operation(
                    project_id,
                    "route_net",
                    {"net_id": "net_out"},
                    timeout=12.0,
                    expected_revision=0,
                )

            kwargs = materialize_mock.call_args.kwargs
            self.assertIs(kwargs["auto_place"], False)
            self.assertEqual(kwargs["route_net_ids"], frozenset({"net_out"}))
            self.assertIs(kwargs["allow_incomplete"], True)

    def test_repeated_route_failure_is_stopped_before_a_third_side_effect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, _before = _seed_managed_project(Path(temp))
            failure = RoutingFailure(
                code="no_legal_channel",
                net="OUTPUT",
                endpoints=("U1.1", "R1.1"),
                blocking_summary="fixture channel is blocked",
                state_revision=0,
                state_context=("placement=fixture", "layers=board:2", "order=0/1"),
            )
            materialize = Mock(side_effect=RoutingFailureError(failure))
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize,
                ),
            ):
                for _attempt in range(2):
                    with self.assertRaises(RoutingFailureError):
                        service.apply_pcb_operation(
                            project_id,
                            "route_net",
                            {"net_id": "net_out"},
                            timeout=12.0,
                            expected_revision=0,
                        )
                with self.assertRaisesRegex(ValidationError, "route retry stopped"):
                    service.apply_pcb_operation(
                        project_id,
                        "route_net",
                        {"net_id": "net_out"},
                        timeout=12.0,
                        expected_revision=0,
                    )

            self.assertEqual(materialize.call_count, 2)
            receipts = [
                load_json_limited(path, 1024 * 1024)
                for path in sorted(
                    (service._open(project_id).root / "transactions").glob(
                        "*/receipt.json"
                    )
                )
            ]
            self.assertEqual(len(receipts), 3)
            blocked = next(
                item
                for item in receipts
                if item["error_code"] == "strategy_change_required"
            )
            self.assertFalse(blocked["convergence"]["allowed"])
            self.assertEqual(blocked["convergence_state"]["design_revision"], 0)

    def test_corrupt_route_history_fails_closed_before_another_side_effect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, _before = _seed_managed_project(Path(temp))
            failure = RoutingFailure(
                code="no_legal_channel",
                net="OUTPUT",
                endpoints=("U1.1", "R1.1"),
                blocking_summary="fixture channel is blocked",
                state_revision=0,
                state_context=(
                    "placement=fixture",
                    "layers=board:2",
                    "order=0/1",
                ),
            )
            materialize = Mock(side_effect=RoutingFailureError(failure))
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    materialize,
                ),
            ):
                with self.assertRaises(RoutingFailureError):
                    service.apply_pcb_operation(
                        project_id,
                        "route_net",
                        {"net_id": "net_out"},
                        timeout=12.0,
                        expected_revision=0,
                    )
                first_receipt = next(
                    (service._open(project_id).root / "transactions").glob(
                        "*/receipt.json"
                    )
                )
                corrupted = load_json_limited(first_receipt, 1024 * 1024)
                corrupted.pop("schema")
                atomic_write_json(first_receipt, corrupted)

                with self.assertRaisesRegex(ValidationError, "route retry stopped"):
                    service.apply_pcb_operation(
                        project_id,
                        "route_net",
                        {"net_id": "net_out"},
                        timeout=12.0,
                        expected_revision=0,
                    )

            self.assertEqual(materialize.call_count, 1)
            valid_receipts = []
            for path in (service._open(project_id).root / "transactions").glob(
                "*/receipt.json"
            ):
                try:
                    valid_receipts.append(load_json_limited(path, 1024 * 1024))
                except PCBDraftError:
                    continue
            blocked = next(
                item
                for item in valid_receipts
                if item.get("error_code") == "strategy_change_required"
            )
            self.assertEqual(
                blocked["convergence"]["reason"],
                "convergence_history_invalid",
            )

    def test_empty_project_final_publish_failure_removes_only_private_stage(
        self,
    ) -> None:
        from pcbdraft.services import application as application_module

        with tempfile.TemporaryDirectory() as temp:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temp), provider=provider)
            published_before_replace: list[str] = []
            takeover_targets: list[Path] = []
            materialized = False
            real_replace = application_module.os.replace

            def materialize(
                request: object,
                design: Design,
                output: Path,
                **kwargs: object,
            ) -> SimpleNamespace:
                nonlocal materialized
                del request, kwargs
                materialized = True
                output.mkdir(parents=True)
                atomic_write_json(output / "mock-design.json", design.to_dict())
                return SimpleNamespace(project=_managed(output))

            def fail_final_replace(source: object, destination: object) -> None:
                destination_path = Path(destination)
                if destination_path.parent == service.projects_root:
                    self.assertTrue(materialized)
                    published_before_replace.extend(
                        str(item["id"]) for item in service.list_projects()
                    )
                    destination_path.mkdir()
                    (destination_path / "other-owner.txt").write_text(
                        "preserve concurrent owner",
                        encoding="utf-8",
                    )
                    takeover_targets.append(destination_path)
                    raise PCBDraftError("injected final publication failure")
                real_replace(source, destination)

            with (
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=materialize,
                ),
                patch.object(
                    application_module.os,
                    "replace",
                    side_effect=fail_final_replace,
                ),
                self.assertRaisesRegex(
                    PCBDraftError, "injected final publication failure"
                ),
            ):
                service.create_empty_project("Empty")

            self.assertEqual(published_before_replace, [])
            self.assertEqual(len(takeover_targets), 1)
            self.assertEqual(
                (takeover_targets[0] / "other-owner.txt").read_text(encoding="utf-8"),
                "preserve concurrent owner",
            )
            self.assertFalse(
                any(
                    child.name.startswith(".")
                    for child in service.projects_root.iterdir()
                )
            )

    def test_empty_project_materialize_failure_never_publishes_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temp), provider=provider)
            published_during_materialize: list[str] = []

            def fail_materialize(*_args: object, **_kwargs: object) -> None:
                published_during_materialize.extend(
                    str(item["id"]) for item in service.list_projects()
                )
                raise PCBDraftError("injected materialize failure")

            with (
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=fail_materialize,
                ),
                self.assertRaisesRegex(PCBDraftError, "injected materialize failure"),
            ):
                service.create_empty_project("Private until ready")

            self.assertEqual(published_during_materialize, [])
            self.assertEqual(list(service.projects_root.iterdir()), [])

    def test_empty_project_publishes_once_after_private_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temp), provider=provider)
            visible_during_materialize: list[str] = []

            def materialize(
                request: object,
                design: Design,
                output: Path,
                **kwargs: object,
            ) -> SimpleNamespace:
                del request, kwargs
                visible_during_materialize.extend(
                    str(item["id"]) for item in service.list_projects()
                )
                output.mkdir(parents=True)
                atomic_write_json(output / "mock-design.json", design.to_dict())
                return SimpleNamespace(project=_managed(output))

            with (
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=materialize,
                ),
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
            ):
                view = service.create_empty_project("Published once")

            self.assertEqual(visible_during_materialize, [])
            self.assertEqual(view["state"]["status"], "generated")
            self.assertEqual(view["state"]["revision"], 1)
            children = list(service.projects_root.iterdir())
            self.assertEqual(
                [child.name for child in children], [view["project"]["id"]]
            )

    def test_add_and_update_tools_do_not_act_as_upserts(self) -> None:
        value = _v2_design().to_dict()
        value["interfaces"] = [
            {
                "id": "control",
                "kind": "gpio",
                "power_domain": "v3v3",
                "members": [{"component": "load_r", "pin": "1", "role": "signal"}],
                "params": {},
                "intent": "Existing control interface.",
            }
        ]
        design = Design.from_dict(value)
        existing = {
            "power_domain": design.power_domains[0].to_dict(),
            "interface": design.interfaces[0].to_dict(),
            "constraint": design.constraints[0].to_dict(),
        }
        for collection, domain_value in existing.items():
            tool = f"add_{collection}"
            argument_value = dict(domain_value)
            if collection in {"interface", "constraint"}:
                argument_value["params"] = [
                    {"name": name, "value": item}
                    for name, item in domain_value["params"].items()
                ]
            with (
                self.subTest(tool=tool),
                self.assertRaisesRegex(ValidationError, "cannot add existing"),
            ):
                ApplicationService._flat_semantic_operation(
                    tool, {"value": argument_value}, design
                )

            missing = dict(argument_value)
            missing["id"] = f"missing_{collection}"
            with (
                self.subTest(tool=f"update_{collection}"),
                self.assertRaisesRegex(ValidationError, "cannot update absent"),
            ):
                ApplicationService._flat_semantic_operation(
                    f"update_{collection}", {"value": missing}, design
                )

    def test_explicit_drc_keeps_complete_evidence_with_bounded_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service, project_id, design = _seed_managed_project(Path(temp))
            revision = int(service._open(project_id).state["revision"])

            def run_check(
                _managed_project: object,
                _kind: str,
                *,
                output: Path,
                timeout: float,
            ) -> SimpleNamespace:
                del timeout
                output.mkdir(parents=True)
                violations = [
                    {
                        "severity": "error",
                        "type": "unconnected_items",
                        "description": f"Missing connection {index}",
                        "items": [
                            {
                                "description": "F.Cu C1 pad 2 [/GND]",
                                "uuid": f"c1-{index}",
                                "pos": {"x": float(index), "y": 15.5},
                            },
                            {
                                "description": "F.Cu U1 pad 2 [/GND]",
                                "uuid": f"u1-{index}",
                                "pos": {"x": 16.3625, "y": 17.5},
                            },
                        ],
                    }
                    for index in range(25)
                ]
                report = output / "check.json"
                atomic_write_json(
                    report,
                    {
                        "schema": "pcbdraft-individual-check",
                        "check": "run_drc",
                        "state": "completed",
                        "outcome": "fail",
                        "details": {"failure": None, "violations": violations},
                        "tool_run": {"raw_report": "drc.raw.json"},
                    },
                )
                atomic_write_json(
                    output / "receipt.json",
                    {
                        "schema": "pcbdraft-individual-check-receipt",
                        "status": "complete",
                    },
                )
                return SimpleNamespace(
                    report_path=report,
                    report_sha256="a" * 64,
                    state="completed",
                    outcome="fail",
                    design_content_hash=design.content_hash(),
                )

            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.run_individual_check",
                    side_effect=run_check,
                ),
            ):
                result = service.run_pcb_check(
                    project_id,
                    "run_drc",
                    timeout=12.0,
                    expected_revision=revision,
                )

            self.assertEqual(result["tool_result"]["outcome"], "fail")
            diagnostics = result["tool_result"]["diagnostics"]
            self.assertEqual(
                diagnostics["counts"], {"error": 25, "warning": 0, "total": 25}
            )
            self.assertEqual(diagnostics["violation_count_seen"], 25)
            self.assertEqual(len(diagnostics["violations"]), 20)
            self.assertTrue(diagnostics["violations_truncated"])
            self.assertTrue(diagnostics["details_truncated"])
            self.assertEqual(diagnostics["remaining_violation_count"], 5)
            self.assertEqual(
                diagnostics["full_details_report"], result["tool_result"]["report"]
            )
            self.assertTrue(diagnostics["raw_report"].endswith("/drc.raw.json"))
            self.assertEqual(
                diagnostics["violations"][0]["message"], "Missing connection 0"
            )
            self.assertEqual(
                diagnostics["violations"][0]["items"][0]["description"],
                "F.Cu C1 pad 2 [/GND]",
            )
            self.assertEqual(
                diagnostics["violations"][0]["items"][1]["pos"],
                {"x": 16.3625, "y": 17.5},
            )

    def test_individual_checks_do_not_dispatch_aggregate_validation(self) -> None:
        service = object.__new__(ApplicationService)
        for kind in ("check_semantics", "check_connectivity", "run_erc", "run_drc"):
            with (
                self.subTest(kind=kind),
                patch.object(
                    service, "run_pcb_check", return_value={"kind": kind}
                ) as individual,
                patch.object(service, "validate_project") as aggregate,
            ):
                result = service.execute_pcb_tool(
                    "board", kind, {}, timeout=12.0, expected_revision=4
                )
                self.assertEqual(result, {"kind": kind})
                individual.assert_called_once_with(
                    "board", kind, timeout=12.0, expected_revision=4
                )
                aggregate.assert_not_called()

    def test_individual_renders_and_exports_do_not_dispatch_aggregates(self) -> None:
        service = object.__new__(ApplicationService)
        for kind in ("render_schematic", "render_board", "render_3d"):
            with (
                self.subTest(kind=kind),
                patch.object(
                    service, "render_pcb_output", return_value={"kind": kind}
                ) as individual,
                patch.object(service, "generate_project_previews") as aggregate,
            ):
                service.execute_pcb_tool(
                    "board", kind, {}, timeout=12.0, expected_revision=4
                )
                individual.assert_called_once_with(
                    "board", kind, timeout=12.0, expected_revision=4
                )
                aggregate.assert_not_called()
        for kind in (
            "export_gerbers",
            "export_drill",
            "export_bom",
            "export_pick_place",
            "export_step",
        ):
            with (
                self.subTest(kind=kind),
                patch.object(
                    service, "export_pcb_output", return_value={"kind": kind}
                ) as individual,
                patch.object(service, "build_release") as aggregate,
            ):
                service.execute_pcb_tool(
                    "board", kind, {}, timeout=12.0, expected_revision=4
                )
                individual.assert_called_once_with(
                    "board", kind, timeout=12.0, expected_revision=4
                )
                aggregate.assert_not_called()

    def test_flat_board_tool_maps_to_one_typed_operation(self) -> None:
        design = _v2_design()

        operation = ApplicationService._flat_semantic_operation(
            "set_board_outline",
            {"width_mm": 30.0, "height_mm": 24.0},
            design,
        )

        self.assertEqual(operation["op"], "set_board_outline")
        self.assertEqual(operation["args"], {"width_mm": 30.0, "height_mm": 24.0})
        self.assertNotIn("operations", operation["args"])

    def test_generated_selected_net_route_is_retained_with_stable_ids(self) -> None:
        design = _v2_design()
        routing = RoutingResult(
            segments=(RouteSegment("OUT", 0, 1.0, 1.0, 2.0, 1.0, 0.25),),
            vias=(RouteVia("OUT", 2.0, 1.0, 0.7, 0.35, 0, 1),),
            unrouted=(),
            state="completed",
            expanded_nodes=1,
            diagnostics=(),
        )

        first = ApplicationService._retain_generated_route(design, "net_out", routing)
        second = ApplicationService._retain_generated_route(design, "net_out", routing)

        self.assertEqual(first.native_intent.routes, second.native_intent.routes)
        self.assertEqual(first.native_intent.vias, second.native_intent.vias)
        self.assertEqual(first.native_intent.routes[0].net, "net_out")
        self.assertEqual(first.native_intent.vias[0].net, "net_out")

    def test_selected_net_route_preserves_other_retained_geometry(self) -> None:
        value = _v2_design().to_dict()
        value["native_intent"]["routes"] = [
            {
                "id": "route_3v3_1",
                "net": "net_3v3",
                "layer": 0,
                "x1_mm": 1.0,
                "y1_mm": 2.0,
                "x2_mm": 2.0,
                "y2_mm": 2.0,
                "width_mm": 0.25,
            }
        ]
        value["native_intent"]["vias"] = [
            {
                "id": "via_3v3_1",
                "net": "net_3v3",
                "x_mm": 2.0,
                "y_mm": 2.0,
                "diameter_mm": 0.7,
                "drill_mm": 0.35,
                "from_layer": 0,
                "to_layer": 1,
            }
        ]
        design = Design.from_dict(value)
        routing = RoutingResult(
            segments=(RouteSegment("OUT", 0, 3.0, 3.0, 4.0, 3.0, 0.25),),
            vias=(),
            unrouted=(),
            state="completed",
            expanded_nodes=1,
            diagnostics=(),
        )

        routed = ApplicationService._retain_generated_route(design, "net_out", routing)

        self.assertEqual(
            [item.id for item in routed.native_intent.routes if item.net == "net_3v3"],
            ["route_3v3_1"],
        )
        self.assertEqual(
            [item.id for item in routed.native_intent.vias if item.net == "net_3v3"],
            ["via_3v3_1"],
        )

    def test_incomplete_selected_net_route_is_rejected(self) -> None:
        design = Design.from_dict(minimal_design_dict())
        routing = RoutingResult((), (), ("OUT",), "heuristic", 1, ("blocked",))

        with self.assertRaisesRegex(ValidationError, "could not complete"):
            ApplicationService._retain_generated_route(design, "net_out", routing)


if __name__ == "__main__":
    unittest.main()
