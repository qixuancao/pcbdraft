from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.domain.ir import Design
from pcbdraft.kicad.routing import RouteSegment, RouteVia, RoutingResult
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


class _Graph:
    def with_footprint_overrides(self, design: Design) -> _Graph:
        del design
        return self


def _managed(path: Path) -> SimpleNamespace:
    design = Design.from_dict(load_json_limited(path / "mock-design.json", 1024 * 1024))
    return SimpleNamespace(
        root=path,
        design=design,
        graph=_Graph(),
        requirements_path=path / "requirements.json",
        plan=None,
        manifest={"hashes": {"ir": design.content_hash()}},
        assert_synchronized=lambda: None,
    )


def _seed_managed_project(root: Path) -> tuple[ApplicationService, str, Design]:
    provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
    service = ApplicationService(root, provider=provider)
    draft = service.create_draft("Board")
    project_id = str(draft["project"]["id"])
    design = _v2_design()
    project = service._open(project_id)
    project.design_root.mkdir()
    atomic_write_json(project.design_root / "mock-design.json", design.to_dict())
    return service, project_id, design


class FlatPCBServiceTests(unittest.TestCase):
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
            kwargs = materialize_mock.call_args.kwargs
            self.assertIs(kwargs["auto_place"], False)
            self.assertEqual(kwargs["route_net_ids"], frozenset())
            self.assertIs(kwargs["allow_incomplete"], True)

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

    def test_empty_project_publish_failure_removes_partial_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = cast(IntentProvider, SimpleNamespace(provider_id="test"))
            service = ApplicationService(Path(temp), provider=provider)

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

            with (
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=materialize,
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
                service.create_empty_project("Empty")

            self.assertEqual(list(service.projects_root.iterdir()), [])

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
