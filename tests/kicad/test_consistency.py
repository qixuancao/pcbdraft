"""Focused regression tests for native KiCad consistency projections."""

from __future__ import annotations

import copy
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from kicad_sch_api import Schematic
from kicad_sch_api.core.pin_utils import list_component_pins

from pcbdraft.agent.tooling import PCB_TOOL_SPECS
from pcbdraft.domain.ir import Design, Endpoint
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.consistency import (
    NATIVE_OPERATION_POLICIES,
    NativeBoardPad,
    NativeBoardProjection,
    NativeConsistencyReport,
    NativeCopper,
    NativePartition,
    NativeSchematicProjection,
    compare_native_consistency,
    compare_native_operation_delta,
    inspect_native_consistency,
)
from pcbdraft.kicad.pcb import generate_pcb, inspect_native_board
from pcbdraft.kicad.schematic import generate_schematic, inspect_native_schematic
from tests.support.design_factory import minimal_design_dict


def _design(*, second_resistor: bool = False, no_connect: bool = False) -> Design:
    value = copy.deepcopy(minimal_design_dict())
    if no_connect:
        value["nets"] = [net for net in value["nets"] if net["id"] != "net_out"]
    if second_resistor:
        component = copy.deepcopy(value["components"][0])
        component.update({"id": "load_r2", "reference": "R2"})
        component["placement"]["x_mm"] = 15
        value["components"].append(component)
        value["blocks"][0]["components"].append("load_r2")
        value["nets"][0]["endpoints"].append(
            {"component": "load_r2", "pin": "1", "role": "load"}
        )
        net = copy.deepcopy(value["nets"][1])
        net.update({"id": "net_out2", "name": "OUT2"})
        net["endpoints"] = [{"component": "load_r2", "pin": "2", "role": "signal"}]
        value["nets"].append(net)
    return Design.from_dict(value)


def _board_design() -> Design:
    value = copy.deepcopy(minimal_design_dict())
    resistor = copy.deepcopy(value["components"][0])
    resistor.update({"id": "load_r2", "reference": "R2"})
    resistor["placement"]["x_mm"] = 15
    ground_flag = copy.deepcopy(value["components"][1])
    ground_flag.update({"id": "gnd_flag", "reference": "#FLG02"})
    value["components"].extend((resistor, ground_flag))
    value["blocks"][0]["components"].extend(("load_r2", "gnd_flag"))
    value["nets"] = [
        {
            "id": "net_3v3",
            "name": "3V3",
            "endpoints": [
                {"component": "source_flag", "pin": "1", "role": "source"},
                {"component": "load_r", "pin": "1", "role": "load"},
                {"component": "load_r2", "pin": "1", "role": "load"},
            ],
            "net_class": "power",
            "power_domain": "v3v3",
            "intent": "Power both native board pads.",
        },
        {
            "id": "net_gnd",
            "name": "GND",
            "endpoints": [
                {"component": "gnd_flag", "pin": "1", "role": "source"},
                {"component": "load_r", "pin": "2", "role": "return"},
                {"component": "load_r2", "pin": "2", "role": "return"},
            ],
            "net_class": "power",
            "intent": "Provide the router's required reference net.",
        },
    ]
    return Design.from_dict(value)


def _real_pcbnew_available() -> bool:
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


def _schematic_snapshot(
    partitions: list[tuple[str, list[str], list[tuple[str, str]]]],
    *,
    no_connects: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    no_connects = no_connects or set()
    endpoint_partition = {
        endpoint: identifier
        for identifier, _labels, endpoints in partitions
        for endpoint in endpoints
    }
    references = sorted({reference for reference, _pin in endpoint_partition})
    return {
        "schema": "pcbdraft-schematic-snapshot",
        "version": 1,
        "root_uuid": "fixture",
        "components": [{"reference": reference} for reference in references],
        "label_names": sorted(
            {
                label
                for _identifier, labels, _endpoints in partitions
                for label in labels
            }
        ),
        "label_count": sum(
            len(labels) for _identifier, labels, _endpoints in partitions
        ),
        "no_connect_count": len(no_connects),
        "connectivity": {
            "schema": "pcbdraft-schematic-connectivity",
            "version": 1,
            "status": "evaluated",
            "endpoints": [
                {
                    "reference": reference,
                    "pin": pin,
                    "partition": endpoint_partition[(reference, pin)],
                    "no_connect": (reference, pin) in no_connects,
                }
                for reference, pin in sorted(endpoint_partition)
            ],
            "partitions": [
                {
                    "id": identifier,
                    "labels": labels,
                    "endpoints": [
                        {"reference": reference, "pin": pin}
                        for reference, pin in endpoints
                    ],
                }
                for identifier, labels, endpoints in partitions
            ],
            "unmapped_components": [],
            "unmapped_labels": [],
            "unmapped_no_connects": [],
        },
    }


def _board_snapshot(
    pads: dict[str, list[tuple[str, str, str, bool]]],
    *,
    tracks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "pcbdraft-pcbnew-result",
        "version": 1,
        "mode": "inspect_board",
        "kicad_version": "10.0.5",
        "components": [
            {
                "reference": reference,
                "pads": [
                    {
                        "number": number,
                        "net": net,
                        "connectivity_component": component,
                        "no_connect": no_connect,
                    }
                    for number, net, component, no_connect in entries
                ],
            }
            for reference, entries in sorted(pads.items())
        ],
        "tracks": tracks or [],
        "zones": [],
        "board": {},
        "connectivity": {
            "schema": "pcbdraft-pcb-connectivity",
            "version": 1,
            "status": "evaluated",
            "unconnected_count": 0,
            "components": [],
            "drc": {"status": "not_evaluated", "items": []},
        },
    }


def _base_schematic() -> dict[str, Any]:
    return _schematic_snapshot(
        [
            ("p1", ["3V3"], [("#FLG01", "1"), ("R1", "1")]),
            ("p2", ["OUT"], [("R1", "2")]),
        ]
    )


def _base_board() -> dict[str, Any]:
    return _board_snapshot(
        {
            "R1": [
                ("1", "/3V3", "c1", False),
                ("2", "/OUT", "c2", False),
            ]
        }
    )


def _native_design() -> Design:
    value = copy.deepcopy(minimal_design_dict())
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


def _delta_board(design: Design) -> NativeBoardProjection:
    graph = PartGraph.bundled().with_footprint_overrides(design)
    names = {item.id: item.name for item in design.nets}
    endpoint_nets = {
        (endpoint.component, endpoint.pin): net.name
        for net in design.nets
        for endpoint in net.endpoints
    }
    points = (
        (0.0, 0.0),
        (design.board.width_mm, 0.0),
        (design.board.width_mm, design.board.height_mm),
        (0.0, design.board.height_mm),
        (0.0, 0.0),
    )
    snapshot = {
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
        "nets": [item.name for item in design.nets],
        "tracks": [
            *(
                {
                    "kind": "segment",
                    "net": names[item.net],
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
                    "net": names[item.net],
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
    return NativeBoardProjection.from_snapshot(snapshot)


def _delta_schematic(design: Design) -> NativeSchematicProjection:
    graph = PartGraph.bundled().with_footprint_overrides(design)
    connected = {
        (endpoint.component, endpoint.pin)
        for net in design.nets
        for endpoint in net.endpoints
    }
    references = {item.id: item.reference for item in design.components}
    partitions = [
        {
            "id": f"net:{net.id}",
            "labels": [net.name],
            "endpoints": [
                {"reference": references[item.component], "pin": item.pin}
                for item in net.endpoints
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
    endpoints = [
        {
            **endpoint,
            "partition": partition["id"],
            "no_connect": not partition["labels"],
        }
        for partition in partitions
        for endpoint in partition["endpoints"]
    ]
    return NativeSchematicProjection.from_snapshot(
        {
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
            "no_connect_count": sum(item["no_connect"] for item in endpoints),
            "connectivity": {
                "schema": "pcbdraft-schematic-connectivity",
                "version": 1,
                "status": "evaluated",
                "endpoints": endpoints,
                "partitions": partitions,
                "unmapped_components": [],
                "unmapped_labels": [],
                "unmapped_no_connects": [],
            },
        }
    )


class NativeConsistencyTests(unittest.TestCase):
    def test_native_operation_policy_matrix_is_explicit(self) -> None:
        authoritative_writes = {
            spec.name for spec in PCB_TOOL_SPECS if spec.effect == "authoritative_write"
        } - {"create_project"}
        self.assertEqual(set(NATIVE_OPERATION_POLICIES), authoritative_writes)
        self.assertEqual(
            {
                name
                for name, policy in NATIVE_OPERATION_POLICIES.items()
                if policy == "semantic_only"
            },
            {
                "add_block",
                "remove_block",
                "add_power_domain",
                "update_power_domain",
                "remove_power_domain",
                "add_interface",
                "update_interface",
                "remove_interface",
            },
        )
        self.assertEqual(
            {
                name
                for name, policy in NATIVE_OPERATION_POLICIES.items()
                if policy == "deferred_native_input"
            },
            {"add_constraint", "update_constraint", "remove_constraint"},
        )
        self.assertEqual(NATIVE_OPERATION_POLICIES["update_board_rules"], "board_rules")
        self.assertEqual(
            NATIVE_OPERATION_POLICIES["assign_footprint"], "footprint_assignment"
        )
        self.assertEqual(
            NATIVE_OPERATION_POLICIES["connect_group"], "connectivity_group"
        )
        self.assertEqual(
            NATIVE_OPERATION_POLICIES["place_group"], "footprint_transform_group"
        )

    def test_group_operation_delta_verifies_union_scope_atomically(self) -> None:
        value = _native_design().to_dict()
        second = copy.deepcopy(value["components"][0])
        second.update({"id": "load_r2", "reference": "R2"})
        second.pop("placement", None)
        value["components"].append(second)
        value["blocks"][0]["components"].append("load_r2")
        value["native_intent"]["routes"] = [
            {
                "id": "route_3v3",
                "net": "net_3v3",
                "layer": 0,
                "x1_mm": 1.0,
                "y1_mm": 1.0,
                "x2_mm": 2.0,
                "y2_mm": 1.0,
                "width_mm": 0.25,
            },
            {
                "id": "route_out",
                "net": "net_out",
                "layer": 0,
                "x1_mm": 3.0,
                "y1_mm": 3.0,
                "x2_mm": 4.0,
                "y2_mm": 3.0,
                "width_mm": 0.25,
            },
        ]
        before_connect = Design.from_dict(value)

        connected_value = before_connect.to_dict()
        next(item for item in connected_value["nets"] if item["id"] == "net_3v3")[
            "endpoints"
        ].append({"component": "load_r2", "pin": "1", "role": "load"})
        next(item for item in connected_value["nets"] if item["id"] == "net_out")[
            "endpoints"
        ].append({"component": "load_r2", "pin": "2", "role": "signal"})
        connected = Design.from_dict(connected_value)
        connect_arguments = {
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
                        "component_id": "load_r2",
                        "pin": "2",
                        "role": "signal",
                    },
                ]
            }
        }
        before_connect_board = _delta_board(before_connect)
        connected_board = _delta_board(connected)
        connect_report = compare_native_operation_delta(
            "connect_group",
            connect_arguments,
            before_connect,
            connected,
            before_connect_board,
            connected_board,
            before_schematic=_delta_schematic(before_connect),
            after_schematic=_delta_schematic(connected),
            graph=PartGraph.bundled().with_footprint_overrides(connected),
        )
        self.assertTrue(connect_report.passed, connect_report.to_dict())
        self.assertEqual(connect_report.policy, "connectivity_group")

        swapped_target_copper = replace(
            connected_board,
            copper=tuple(
                replace(
                    item,
                    net=(
                        "OUT"
                        if item.net == "3V3"
                        else "3V3"
                        if item.net == "OUT"
                        else item.net
                    ),
                )
                for item in connected_board.copper
            ),
        )
        swapped_report = compare_native_operation_delta(
            "connect_group",
            connect_arguments,
            before_connect,
            connected,
            before_connect_board,
            swapped_target_copper,
            before_schematic=_delta_schematic(before_connect),
            after_schematic=_delta_schematic(connected),
            graph=PartGraph.bundled().with_footprint_overrides(connected),
        )
        self.assertFalse(swapped_report.passed)
        self.assertFalse(
            next(
                item.passed
                for item in swapped_report.checks
                if item.name == "native_group_net_copper_preserved"
            )
        )

        unrelated_zone = NativeCopper(
            "zone",
            "AUX",
            1.0,
            ("layer=0", "pad_connection=thermal", "area=1"),
        )
        before_with_unrelated = replace(
            before_connect_board,
            copper=(*before_connect_board.copper, unrelated_zone),
            nets=(*before_connect_board.nets, "AUX"),
        )
        after_with_unrelated = replace(
            connected_board,
            copper=(*connected_board.copper, unrelated_zone),
            nets=(*connected_board.nets, "AUX"),
        )
        first_pose = after_with_unrelated.footprint_poses[0]
        unrelated_changes = {
            "component": replace(
                after_with_unrelated,
                footprint_poses=(
                    replace(first_pose, x_mm=first_pose.x_mm + 1.0),
                    *after_with_unrelated.footprint_poses[1:],
                ),
            ),
            "net": replace(
                after_with_unrelated,
                nets=tuple(item for item in after_with_unrelated.nets if item != "AUX"),
            ),
            "zone": replace(
                after_with_unrelated,
                copper=tuple(
                    item
                    for item in after_with_unrelated.copper
                    if item != unrelated_zone
                ),
            ),
        }
        for kind, changed_board in unrelated_changes.items():
            with self.subTest(unrelated_native_delta=kind):
                changed_report = compare_native_operation_delta(
                    "connect_group",
                    connect_arguments,
                    before_connect,
                    connected,
                    before_with_unrelated,
                    changed_board,
                    before_schematic=_delta_schematic(before_connect),
                    after_schematic=_delta_schematic(connected),
                    graph=PartGraph.bundled().with_footprint_overrides(connected),
                )
                self.assertFalse(changed_report.passed)
                self.assertFalse(
                    next(
                        item.passed
                        for item in changed_report.checks
                        if item.name == "no_unrelated_native_delta"
                    )
                )

        placed_value = connected.to_dict()
        for component_id, x_mm, y_mm in (
            ("load_r", 4.0, 5.0),
            ("load_r2", 14.0, 15.0),
        ):
            component = next(
                item
                for item in placed_value["components"]
                if item["id"] == component_id
            )
            component["placement"] = {
                "x_mm": x_mm,
                "y_mm": y_mm,
                "rotation_deg": 90.0,
                "side": "front",
                "fixed": True,
            }
            placed_value["native_intent"]["footprint_poses"] = [
                item
                for item in placed_value["native_intent"]["footprint_poses"]
                if item["component"] != component_id
            ]
            placed_value["native_intent"]["footprint_poses"].append(
                {"component": component_id, **component["placement"]}
            )
        placed = Design.from_dict(placed_value)
        place_arguments = {
            "placements": {
                "entries": [
                    {
                        "component_id": "load_r",
                        "x_mm": 4.0,
                        "y_mm": 5.0,
                        "rotation_deg": 90.0,
                        "side": "front",
                    },
                    {
                        "component_id": "load_r2",
                        "x_mm": 14.0,
                        "y_mm": 15.0,
                        "rotation_deg": 90.0,
                        "side": "front",
                    },
                ]
            }
        }
        place_report = compare_native_operation_delta(
            "place_group",
            place_arguments,
            connected,
            placed,
            _delta_board(connected),
            _delta_board(placed),
            before_schematic=_delta_schematic(connected),
            after_schematic=_delta_schematic(placed),
            graph=PartGraph.bundled().with_footprint_overrides(placed),
        )
        self.assertTrue(place_report.passed, place_report.to_dict())
        self.assertEqual(place_report.policy, "footprint_transform_group")

    def test_remaining_operation_policies_match_scoped_native_deltas(self) -> None:
        base = _native_design()
        cases: list[tuple[str, dict[str, Any], Design, Design, str]] = []

        rules_value = base.to_dict()
        rules_value["board"]["min_track_mm"] = 0.3
        rules = Design.from_dict(rules_value)
        cases.append(
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
                base,
                rules,
                "board_rules",
            )
        )

        footprint_value = base.to_dict()
        footprint_value["components"][0]["attributes"]["footprint"] = (
            "Resistor_SMD:R_0805_2012Metric"
        )
        footprint = Design.from_dict(footprint_value)
        cases.append(
            (
                "assign_footprint",
                {
                    "component_id": "load_r",
                    "footprint": "Resistor_SMD:R_0805_2012Metric",
                },
                base,
                footprint,
                "footprint_assignment",
            )
        )

        value_update = base.to_dict()
        value_update["components"][0]["value"] = "10k"
        updated = Design.from_dict(value_update)
        cases.append(
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
                base,
                updated,
                "component_update",
            )
        )

        added_value = base.to_dict()
        component_value = {
            "id": "load_r2",
            "reference": "R2",
            "part_id": "yageo.rc0603fr-074k7l",
            "value": "4.7k",
            "block_id": "power_block",
            "attributes": {},
        }
        added_value["components"].append(component_value)
        added_value["blocks"][0]["components"].append("load_r2")
        added_component = Design.from_dict(added_value)
        cases.append(
            (
                "add_component",
                {
                    "value": {
                        key: component_value[key]
                        for key in component_value
                        if key != "attributes"
                    }
                },
                base,
                added_component,
                "component_add",
            )
        )
        cases.append(
            (
                "remove_component",
                {"component_id": "load_r2"},
                added_component,
                base,
                "component_remove",
            )
        )

        net_value = {
            "id": "net_aux",
            "name": "AUX",
            "net_class": "signal",
            "power_domain": None,
            "interface": None,
            "intent": "Auxiliary test net.",
        }
        with_net_value = base.to_dict()
        with_net_value["nets"].append({**net_value, "endpoints": []})
        with_net = Design.from_dict(with_net_value)
        cases.append(("add_net", {"value": net_value}, base, with_net, "net_add"))
        cases.append(
            (
                "remove_net",
                {"net_id": "net_aux"},
                with_net,
                base,
                "net_remove",
            )
        )

        connected_value = added_component.to_dict()
        target_net = next(
            item for item in connected_value["nets"] if item["id"] == "net_out"
        )
        target_net["endpoints"].append(
            {"component": "load_r2", "pin": "1", "role": "signal"}
        )
        connected = Design.from_dict(connected_value)
        pin_arguments = {
            "net_id": "net_out",
            "component_id": "load_r2",
            "pin": "1",
            "role": "signal",
        }
        cases.append(
            ("connect_pin", pin_arguments, added_component, connected, "connectivity")
        )
        cases.append(
            (
                "disconnect_pin",
                pin_arguments,
                connected,
                added_component,
                "connectivity",
            )
        )

        renamed_value = base.to_dict()
        next(item for item in renamed_value["nets"] if item["id"] == "net_out")[
            "name"
        ] = "OUTPUT"
        renamed = Design.from_dict(renamed_value)
        cases.append(
            (
                "rename_net",
                {"net_id": "net_out", "name": "OUTPUT"},
                base,
                renamed,
                "net_rename",
            )
        )

        semantic_value = base.to_dict()
        semantic_value["blocks"].append(
            {
                "id": "spare_block",
                "kind": "spare",
                "name": "Spare",
                "version": "1",
                "intent": "Semantic grouping only.",
                "components": [],
                "provenance": [],
            }
        )
        semantic = Design.from_dict(semantic_value)
        cases.append(
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
                base,
                semantic,
                "semantic_only",
            )
        )

        for operation, arguments, before_design, candidate, expected_policy in cases:
            with self.subTest(operation=operation):
                report = compare_native_operation_delta(
                    operation,
                    arguments,
                    before_design,
                    candidate,
                    _delta_board(before_design),
                    _delta_board(candidate),
                    before_schematic=_delta_schematic(before_design),
                    after_schematic=_delta_schematic(candidate),
                    graph=PartGraph.bundled().with_footprint_overrides(candidate),
                )
                self.assertTrue(report.passed, report.to_dict())
                self.assertEqual(report.policy, expected_policy)
                if operation in {"update_board_rules", "update_component"}:
                    self.assertIn(
                        "semantic_only_fields",
                        {item.name for item in report.checks},
                    )

    def test_empty_added_net_allows_absent_native_projection(self) -> None:
        base = _native_design()
        net_value = {
            "id": "net_aux",
            "name": "AUX",
            "net_class": "signal",
            "power_domain": None,
            "interface": None,
            "intent": "Auxiliary test net.",
        }
        candidate_value = base.to_dict()
        candidate_value["nets"].append({**net_value, "endpoints": []})
        candidate = Design.from_dict(candidate_value)
        after_board = _delta_board(candidate)
        after_board = replace(
            after_board,
            nets=tuple(name for name in after_board.nets if name != "AUX"),
        )

        report = compare_native_operation_delta(
            "add_net",
            {"value": net_value},
            base,
            candidate,
            _delta_board(base),
            after_board,
            before_schematic=_delta_schematic(base),
            after_schematic=_delta_schematic(candidate),
            graph=PartGraph.bundled().with_footprint_overrides(candidate),
        )

        self.assertTrue(report.passed, report.to_dict())
        projection = next(
            item for item in report.checks if item.name == "native_net_projection"
        )
        self.assertTrue(projection.passed)
        self.assertEqual(
            projection.expected,
            "native_projection=not_applicable_empty_net,name=AUX",
        )
        self.assertIn("board_net=False", projection.observed)

    def test_empty_added_net_rejects_native_electrical_membership(self) -> None:
        base = _native_design()
        net_value = {
            "id": "net_aux",
            "name": "AUX",
            "net_class": "signal",
            "power_domain": None,
            "interface": None,
            "intent": "Auxiliary test net.",
        }
        candidate_value = base.to_dict()
        candidate_value["nets"].append({**net_value, "endpoints": []})
        candidate = Design.from_dict(candidate_value)
        graph = PartGraph.bundled().with_footprint_overrides(candidate)
        after_board = replace(
            _delta_board(candidate),
            nets=tuple(name for name in _delta_board(candidate).nets if name != "AUX"),
        )
        after_schematic = _delta_schematic(candidate)
        conflicts = (
            (
                "schematic_partition",
                replace(
                    after_schematic,
                    partitions=(
                        *after_schematic.partitions,
                        NativePartition("aux", ("AUX",), ()),
                    ),
                ),
                after_board,
            ),
            (
                "board_pad",
                after_schematic,
                replace(
                    after_board,
                    pads=(
                        *after_board.pads,
                        NativeBoardPad(Endpoint("J99", "1"), "AUX", "aux", False),
                    ),
                ),
            ),
            (
                "board_copper",
                after_schematic,
                replace(
                    after_board,
                    copper=(
                        *after_board.copper,
                        NativeCopper("segment", "AUX", 1.0),
                    ),
                ),
            ),
        )

        for label, conflicting_schematic, conflicting_board in conflicts:
            with self.subTest(conflict=label):
                report = compare_native_operation_delta(
                    "add_net",
                    {"value": net_value},
                    base,
                    candidate,
                    _delta_board(base),
                    conflicting_board,
                    before_schematic=_delta_schematic(base),
                    after_schematic=conflicting_schematic,
                    graph=graph,
                )

                self.assertFalse(report.passed, report.to_dict())
                projection = next(
                    item
                    for item in report.checks
                    if item.name == "native_net_projection"
                )
                self.assertFalse(projection.passed)

    def test_nonempty_added_net_requires_exact_native_projection(self) -> None:
        before_value = _native_design().to_dict()
        component = copy.deepcopy(before_value["components"][0])
        component.update({"id": "load_r2", "reference": "R2"})
        component.pop("placement", None)
        before_value["components"].append(component)
        before_value["blocks"][0]["components"].append("load_r2")
        before = Design.from_dict(before_value)

        net_value = {
            "id": "net_aux",
            "name": "AUX",
            "net_class": "signal",
            "power_domain": None,
            "interface": None,
            "intent": "Auxiliary test net.",
        }
        candidate_value = before.to_dict()
        candidate_value["nets"].append(
            {
                **net_value,
                "endpoints": [{"component": "load_r2", "pin": "1", "role": "signal"}],
            }
        )
        candidate = Design.from_dict(candidate_value)
        graph = PartGraph.bundled().with_footprint_overrides(candidate)

        missing = compare_native_operation_delta(
            "add_net",
            {"value": net_value},
            before,
            candidate,
            _delta_board(before),
            _delta_board(before),
            before_schematic=_delta_schematic(before),
            after_schematic=_delta_schematic(before),
            graph=graph,
        )
        self.assertFalse(missing.passed)
        missing_projection = next(
            item for item in missing.checks if item.name == "native_net_projection"
        )
        self.assertFalse(missing_projection.passed)
        self.assertEqual(
            missing_projection.expected,
            "name=AUX,schematic_endpoints=1,pads=1",
        )

        materialized = compare_native_operation_delta(
            "add_net",
            {"value": net_value},
            before,
            candidate,
            _delta_board(before),
            _delta_board(candidate),
            before_schematic=_delta_schematic(before),
            after_schematic=_delta_schematic(candidate),
            graph=graph,
        )
        self.assertTrue(materialized.passed, materialized.to_dict())

        copper_candidate_value = before.to_dict()
        copper_candidate_value["nets"].append({**net_value, "endpoints": []})
        copper_candidate_value["native_intent"]["routes"] = [
            {
                "id": "route_aux",
                "net": "net_aux",
                "layer": 0,
                "x1_mm": 1.0,
                "y1_mm": 1.0,
                "x2_mm": 2.0,
                "y2_mm": 1.0,
                "width_mm": 0.25,
            }
        ]
        copper_candidate = Design.from_dict(copper_candidate_value)
        missing_copper_board = replace(
            _delta_board(copper_candidate),
            copper=tuple(
                item
                for item in _delta_board(copper_candidate).copper
                if item.net != "AUX"
            ),
        )
        missing_copper = compare_native_operation_delta(
            "add_net",
            {"value": net_value},
            before,
            copper_candidate,
            _delta_board(before),
            missing_copper_board,
            before_schematic=_delta_schematic(before),
            after_schematic=_delta_schematic(copper_candidate),
            graph=PartGraph.bundled().with_footprint_overrides(copper_candidate),
        )
        self.assertFalse(missing_copper.passed, missing_copper.to_dict())
        missing_copper_projection = next(
            item
            for item in missing_copper.checks
            if item.name == "native_net_projection"
        )
        self.assertFalse(missing_copper_projection.passed)
        self.assertIn("copper=0", missing_copper_projection.observed)

    def test_semantic_only_families_require_identical_native_projection(self) -> None:
        base = _native_design()
        candidates: list[tuple[str, Design, str]] = []

        block_value = base.to_dict()
        block_value["blocks"].append(
            {
                "id": "spare_block",
                "kind": "spare",
                "name": "Spare",
                "version": "1",
                "intent": "Semantic grouping only.",
                "components": [],
                "provenance": [],
            }
        )
        candidates.append(("add_block", Design.from_dict(block_value), "semantic_only"))

        power_value = base.to_dict()
        power_value["power_domains"][0]["intent"] = "Updated semantic rating note."
        candidates.append(
            ("update_power_domain", Design.from_dict(power_value), "semantic_only")
        )

        interface_value = base.to_dict()
        interface_value["interfaces"].append(
            {
                "id": "aux_interface",
                "kind": "gpio",
                "power_domain": "v3v3",
                "members": [],
                "params": {},
                "intent": "Semantic interface declaration.",
            }
        )
        candidates.append(
            ("add_interface", Design.from_dict(interface_value), "semantic_only")
        )

        constraint_value = base.to_dict()
        constraint_value["constraints"][0]["rationale"] = (
            "Updated semantic manufacturing rationale."
        )
        candidates.append(
            (
                "update_constraint",
                Design.from_dict(constraint_value),
                "deferred_native_input",
            )
        )

        for operation, candidate, expected_policy in candidates:
            with self.subTest(operation=operation):
                report = compare_native_operation_delta(
                    operation,
                    {},
                    base,
                    candidate,
                    _delta_board(base),
                    _delta_board(candidate),
                    before_schematic=_delta_schematic(base),
                    after_schematic=_delta_schematic(candidate),
                )
                self.assertTrue(report.passed, report.to_dict())
                self.assertEqual(report.policy, expected_policy)

        candidate = candidates[0][1]
        changed_board = _delta_board(candidate)
        first = changed_board.component_artifacts[0]
        changed_board = replace(
            changed_board,
            component_artifacts=(
                replace(first, value=first.value + "-unexpected"),
                *changed_board.component_artifacts[1:],
            ),
        )
        report = compare_native_operation_delta(
            "add_block",
            {},
            base,
            candidate,
            _delta_board(base),
            changed_board,
            before_schematic=_delta_schematic(base),
            after_schematic=_delta_schematic(candidate),
        )
        self.assertFalse(report.passed)

    def test_scoped_operation_rejects_unrelated_schematic_delta(self) -> None:
        base = _native_design()
        value = base.to_dict()
        value["nets"].append(
            {
                "id": "net_aux",
                "name": "AUX",
                "endpoints": [],
                "net_class": "signal",
                "power_domain": None,
                "interface": None,
                "intent": "Auxiliary test net.",
            }
        )
        candidate = Design.from_dict(value)
        after_schematic = _delta_schematic(candidate)
        first = after_schematic.component_artifacts[0]
        after_schematic = replace(
            after_schematic,
            component_artifacts=(
                replace(first, value=first.value + "-unexpected"),
                *after_schematic.component_artifacts[1:],
            ),
        )

        report = compare_native_operation_delta(
            "add_net",
            {
                "value": {
                    "id": "net_aux",
                    "name": "AUX",
                    "net_class": "signal",
                    "power_domain": None,
                    "interface": None,
                    "intent": "Auxiliary test net.",
                }
            },
            base,
            candidate,
            _delta_board(base),
            _delta_board(candidate),
            before_schematic=_delta_schematic(base),
            after_schematic=after_schematic,
            graph=PartGraph.bundled(),
        )

        self.assertFalse(report.passed)
        self.assertFalse(report.checks[-1].passed)

    def test_component_delta_proves_pose_and_preserves_connected_net_copper(
        self,
    ) -> None:
        routed_value = _native_design().to_dict()
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
        before = Design.from_dict(routed_value)
        candidate_value = before.to_dict()
        candidate_value["components"][0]["value"] = "10k"
        candidate = Design.from_dict(candidate_value)
        arguments = {
            "component_id": "load_r",
            "changes": {"entries": [{"field": "value", "value": "10k"}]},
        }

        missing_copper = replace(
            _delta_board(candidate),
            copper=tuple(
                item for item in _delta_board(candidate).copper if item.net != "OUT"
            ),
        )
        report = compare_native_operation_delta(
            "update_component",
            arguments,
            before,
            candidate,
            _delta_board(before),
            missing_copper,
            before_schematic=_delta_schematic(before),
            after_schematic=_delta_schematic(candidate),
        )
        self.assertFalse(report.passed)
        self.assertFalse(
            next(
                item.passed
                for item in report.checks
                if item.name == "no_unrelated_native_delta"
            )
        )

        wrong_pose_board = _delta_board(candidate)
        wrong_pose_board = replace(
            wrong_pose_board,
            footprint_poses=tuple(
                replace(item, x_mm=item.x_mm + 1.0) if item.reference == "R1" else item
                for item in wrong_pose_board.footprint_poses
            ),
        )
        report = compare_native_operation_delta(
            "update_component",
            arguments,
            before,
            candidate,
            _delta_board(before),
            wrong_pose_board,
            before_schematic=_delta_schematic(before),
            after_schematic=_delta_schematic(candidate),
        )
        self.assertFalse(report.passed)
        self.assertFalse(
            next(
                item.passed
                for item in report.checks
                if item.name == "native_component_pose"
            )
        )

    def test_net_delta_preserves_target_copper_across_rename(self) -> None:
        routed_value = _native_design().to_dict()
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
        before = Design.from_dict(routed_value)
        candidate_value = before.to_dict()
        next(item for item in candidate_value["nets"] if item["id"] == "net_out")[
            "name"
        ] = "OUTPUT"
        candidate = Design.from_dict(candidate_value)
        after = replace(
            _delta_board(candidate),
            copper=tuple(
                item for item in _delta_board(candidate).copper if item.net != "OUTPUT"
            ),
        )

        report = compare_native_operation_delta(
            "rename_net",
            {"net_id": "net_out", "name": "OUTPUT"},
            before,
            candidate,
            _delta_board(before),
            after,
            before_schematic=_delta_schematic(before),
            after_schematic=_delta_schematic(candidate),
        )

        self.assertFalse(report.passed)
        self.assertFalse(
            next(
                item.passed
                for item in report.checks
                if item.name == "native_net_copper_preserved"
            )
        )

    def test_zone_refill_allowance_preserves_zone_identity(self) -> None:
        before = _native_design()
        candidate_value = before.to_dict()
        component = {
            "id": "load_r2",
            "reference": "R2",
            "part_id": "yageo.rc0603fr-074k7l",
            "value": "4.7k",
            "block_id": "power_block",
            "attributes": {},
        }
        candidate_value["components"].append(component)
        candidate_value["blocks"][0]["components"].append("load_r2")
        candidate = Design.from_dict(candidate_value)
        arguments = {
            "value": {
                key: value for key, value in component.items() if key != "attributes"
            }
        }
        before_board = replace(
            _delta_board(before),
            copper=(
                NativeCopper(
                    "zone",
                    "3V3",
                    10.0,
                    ("layer=B.Cu", "connection=thermal_relief", "area=10"),
                ),
            ),
        )
        refill_board = replace(
            _delta_board(candidate),
            copper=(
                NativeCopper(
                    "zone",
                    "3V3",
                    9.0,
                    ("layer=B.Cu", "connection=thermal_relief", "area=9"),
                ),
            ),
        )
        report = compare_native_operation_delta(
            "add_component",
            arguments,
            before,
            candidate,
            before_board,
            refill_board,
            before_schematic=_delta_schematic(before),
            after_schematic=_delta_schematic(candidate),
        )
        self.assertTrue(report.passed, report.to_dict())

        for changed_zone in (
            NativeCopper(
                "zone",
                "OUT",
                9.0,
                ("layer=B.Cu", "connection=thermal_relief", "area=9"),
            ),
            NativeCopper(
                "zone",
                "3V3",
                9.0,
                ("layer=F.Cu", "connection=thermal_relief", "area=9"),
            ),
            NativeCopper(
                "zone",
                "3V3",
                9.0,
                ("layer=B.Cu", "connection=solid", "area=9"),
            ),
        ):
            with self.subTest(changed_zone=changed_zone):
                report = compare_native_operation_delta(
                    "add_component",
                    arguments,
                    before,
                    candidate,
                    before_board,
                    replace(refill_board, copper=(changed_zone,)),
                    before_schematic=_delta_schematic(before),
                    after_schematic=_delta_schematic(candidate),
                )
                self.assertFalse(report.passed)
                self.assertFalse(report.checks[-1].passed)

    def test_unroute_preserves_target_zone_while_allowing_refill_area(self) -> None:
        routed_value = _native_design().to_dict()
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
        unrouted_value = routed.to_dict()
        unrouted_value["native_intent"]["routes"] = []
        unrouted_value["native_intent"]["unrouted_nets"] = ["net_out"]
        unrouted = Design.from_dict(unrouted_value)
        before_board = replace(
            _delta_board(routed),
            copper=(
                *_delta_board(routed).copper,
                NativeCopper(
                    "zone",
                    "OUT",
                    10.0,
                    ("layer=B.Cu", "connection=thermal_relief", "area=10"),
                ),
            ),
        )
        after_zone = NativeCopper(
            "zone",
            "OUT",
            9.0,
            ("layer=B.Cu", "connection=thermal_relief", "area=9"),
        )
        after_board = replace(_delta_board(unrouted), copper=(after_zone,))

        report = compare_native_operation_delta(
            "unroute_net",
            {"net_id": "net_out"},
            routed,
            unrouted,
            before_board,
            after_board,
        )
        self.assertTrue(report.passed, report.to_dict())

        report = compare_native_operation_delta(
            "unroute_net",
            {"net_id": "net_out"},
            routed,
            unrouted,
            before_board,
            replace(after_board, copper=()),
        )
        self.assertFalse(report.passed)
        self.assertFalse(
            next(
                item.passed
                for item in report.checks
                if item.name == "native_target_zones_preserved"
            )
        )

    def test_board_rule_zone_allowance_tracks_native_field_changes(self) -> None:
        before = _native_design()
        zone = NativeCopper(
            "zone",
            "3V3",
            10.0,
            ("layer=B.Cu", "connection=thermal_relief", "area=10"),
        )
        before_board = replace(_delta_board(before), copper=(zone,))

        finish_value = before.to_dict()
        finish_value["board"]["finish"] = "enig"
        finish = Design.from_dict(finish_value)
        refill = replace(
            _delta_board(finish),
            copper=(
                replace(zone, measure=9.0, geometry=(*zone.geometry[:-1], "area=9")),
            ),
        )
        report = compare_native_operation_delta(
            "update_board_rules",
            {"changes": {"entries": [{"field": "finish", "value": "enig"}]}},
            before,
            finish,
            before_board,
            refill,
        )
        self.assertFalse(report.passed)
        self.assertFalse(report.checks[-1].passed)

        layers_value = before.to_dict()
        layers_value["board"]["layers"] = 4
        layers_value["scope"]["layers"] = 4
        layers = Design.from_dict(layers_value)
        internal_zone = replace(
            zone,
            measure=9.0,
            geometry=("layer=In1.Cu", "connection=thermal_relief", "area=9"),
        )
        report = compare_native_operation_delta(
            "update_board_rules",
            {"changes": {"entries": [{"field": "layers", "value": 4}]}},
            before,
            layers,
            before_board,
            replace(_delta_board(layers), copper=(internal_zone,)),
        )
        self.assertTrue(report.passed, report.to_dict())

    def test_operation_delta_covers_outline_footprint_via_and_unroute(self) -> None:
        base = _native_design()
        cases: list[tuple[str, dict[str, Any], Design, Design]] = []

        outline_value = base.to_dict()
        outline_value["board"]["width_mm"] = 32.0
        outline_value["native_intent"]["geometry_revision"] += 1
        outline = Design.from_dict(outline_value)
        cases.append(
            ("set_board_outline", {"width_mm": 32.0, "height_mm": 30.0}, base, outline)
        )

        pose_value = base.to_dict()
        target = next(
            item for item in pose_value["components"] if item["id"] == "load_r"
        )
        target["placement"]["x_mm"] = 14.0
        pose_value["native_intent"]["footprint_poses"] = [
            {"component": "load_r", **target["placement"]}
        ]
        pose_value["native_intent"]["geometry_revision"] += 1
        moved = Design.from_dict(pose_value)
        cases.append(
            ("move_footprint", {"component_id": "load_r", "x_mm": 14.0}, base, moved)
        )

        via_value = base.to_dict()
        via_value["native_intent"]["vias"] = [
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
        via_value["native_intent"]["geometry_revision"] += 1
        with_via = Design.from_dict(via_value)
        cases.append(
            ("add_via", {"via_id": "via_out", "net_id": "net_out"}, base, with_via)
        )

        routed_value = base.to_dict()
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
        unrouted_value = routed.to_dict()
        unrouted_value["native_intent"]["routes"] = []
        unrouted_value["native_intent"]["unrouted_nets"] = ["net_out"]
        unrouted_value["native_intent"]["geometry_revision"] += 1
        unrouted = Design.from_dict(unrouted_value)
        cases.append(("unroute_net", {"net_id": "net_out"}, routed, unrouted))

        for operation, arguments, before_design, candidate in cases:
            with self.subTest(operation=operation):
                report = compare_native_operation_delta(
                    operation,
                    arguments,
                    before_design,
                    candidate,
                    _delta_board(before_design),
                    _delta_board(candidate),
                )
                self.assertTrue(report.passed, report.to_dict())
                self.assertEqual(
                    report.to_dict()["schema_version"],
                    "native-operation-delta-v1",
                )

    def test_operation_delta_rejects_unrelated_native_change(self) -> None:
        before = _native_design()
        value = before.to_dict()
        value["board"]["width_mm"] = 32.0
        value["native_intent"]["geometry_revision"] += 1
        candidate = Design.from_dict(value)
        after = _delta_board(candidate)
        unrelated = after.footprint_poses[0]
        after = replace(
            after,
            footprint_poses=(
                replace(unrelated, x_mm=unrelated.x_mm + 1.0),
                *after.footprint_poses[1:],
            ),
        )

        report = compare_native_operation_delta(
            "set_board_outline",
            {"width_mm": 32.0, "height_mm": 30.0},
            before,
            candidate,
            _delta_board(before),
            after,
        )

        self.assertFalse(report.passed)
        self.assertFalse(
            next(
                item.passed
                for item in report.checks
                if item.name == "no_unrelated_native_delta"
            )
        )

    def test_matching_projections_pass_and_report_round_trips(self) -> None:
        schematic_projection = NativeSchematicProjection.from_snapshot(
            _base_schematic()
        )
        board_projection = NativeBoardProjection.from_snapshot(_base_board())
        self.assertEqual(schematic_projection.component_artifacts, ())
        self.assertEqual(board_projection.component_artifacts, ())
        self.assertEqual(board_projection.nets, ())
        legacy_none = _base_schematic()
        for component in legacy_none["components"]:
            component.update(
                {
                    "value": "PWR_FLAG",
                    "symbol": "power:PWR_FLAG",
                    "footprint": None,
                    "properties": {"Part_ID": "kicad.pwr_flag"},
                }
            )
        legacy_projection = NativeSchematicProjection.from_snapshot(legacy_none)
        self.assertTrue(
            all(not item.footprint for item in legacy_projection.component_artifacts)
        )
        report = compare_native_consistency(
            _design(),
            schematic_projection,
            board_projection,
            candidate_revision=7,
        )

        self.assertTrue(report.consistency_passed)
        self.assertFalse(report.passed)
        self.assertEqual(report.verification_status, "unknown")
        self.assertEqual(report.drc_status, "not_evaluated")
        self.assertEqual(NativeConsistencyReport.from_dict(report.to_dict()), report)

    def test_schematic_merge_split_and_missing_endpoint_are_stable(self) -> None:
        fixtures = {
            "unintended_net_merge": _schematic_snapshot(
                [
                    (
                        "p1",
                        ["3V3", "OUT"],
                        [("#FLG01", "1"), ("R1", "1"), ("R1", "2")],
                    )
                ]
            ),
            "native_net_split": _schematic_snapshot(
                [
                    ("p1", ["3V3"], [("#FLG01", "1")]),
                    ("p2", ["3V3"], [("R1", "1")]),
                    ("p3", ["OUT"], [("R1", "2")]),
                ]
            ),
            "missing_native_endpoint": _schematic_snapshot(
                [("p1", ["3V3"], [("#FLG01", "1"), ("R1", "1")])]
            ),
        }
        for expected_code, snapshot in fixtures.items():
            with self.subTest(expected_code):
                report = compare_native_consistency(
                    _design(),
                    NativeSchematicProjection.from_snapshot(snapshot),
                    NativeBoardProjection.from_snapshot(_base_board()),
                    candidate_revision=8,
                )
                codes = [item.code for item in report.mismatches]
                self.assertIn(expected_code, codes)
                self.assertEqual(codes, sorted(codes))

    def test_equivalent_no_connect_marker_is_accepted(self) -> None:
        schematic = _schematic_snapshot(
            [
                ("p1", ["3V3"], [("#FLG01", "1"), ("R1", "1")]),
                ("p2", [], [("R1", "2")]),
            ],
            no_connects={("R1", "2")},
        )
        board = _board_snapshot(
            {
                "R1": [
                    ("1", "/3V3", "c1", False),
                    ("2", "unconnected-(R1-2-Pad2)", "c2", False),
                ]
            }
        )
        report = compare_native_consistency(
            _design(no_connect=True),
            NativeSchematicProjection.from_snapshot(schematic),
            NativeBoardProjection.from_snapshot(board),
            candidate_revision=9,
        )
        self.assertTrue(report.consistency_passed)

        schematic["connectivity"]["endpoints"][-1]["no_connect"] = False
        report = compare_native_consistency(
            _design(no_connect=True),
            NativeSchematicProjection.from_snapshot(schematic),
            NativeBoardProjection.from_snapshot(board),
            candidate_revision=10,
        )
        self.assertIn("missing_no_connect", {item.code for item in report.mismatches})

    def test_route_requires_nonzero_copper_and_one_connectivity_component(self) -> None:
        design = _design(second_resistor=True)
        schematic = _schematic_snapshot(
            [
                (
                    "p1",
                    ["3V3"],
                    [("#FLG01", "1"), ("R1", "1"), ("R2", "1")],
                ),
                ("p2", ["OUT"], [("R1", "2")]),
                ("p3", ["OUT2"], [("R2", "2")]),
            ]
        )
        unrouted = _board_snapshot(
            {
                "R1": [("1", "/3V3", "c1", False), ("2", "/OUT", "c3", False)],
                "R2": [("1", "/3V3", "c2", False), ("2", "/OUT2", "c4", False)],
            }
        )
        report = compare_native_consistency(
            design,
            NativeSchematicProjection.from_snapshot(schematic),
            NativeBoardProjection.from_snapshot(unrouted),
            candidate_revision=11,
            require_routed_net_ids=frozenset({"net_3v3"}),
        )
        self.assertTrue(
            {"native_zero_copper", "native_connectivity_failed"}
            <= {item.code for item in report.mismatches}
        )

        routed = copy.deepcopy(unrouted)
        routed["components"][1]["pads"][0]["connectivity_component"] = "c1"
        routed["tracks"] = [
            {
                "kind": "segment",
                "net": "/3V3",
                "x1_mm": 1.0,
                "y1_mm": 1.0,
                "x2_mm": 2.0,
                "y2_mm": 1.0,
            }
        ]
        report = compare_native_consistency(
            design,
            NativeSchematicProjection.from_snapshot(schematic),
            NativeBoardProjection.from_snapshot(routed),
            candidate_revision=12,
            require_routed_net_ids=frozenset({"net_3v3"}),
        )
        self.assertTrue(report.consistency_passed)
        self.assertFalse(report.passed)

    def test_wrong_pad_net_and_duplicate_physical_pad_are_projected(self) -> None:
        board = _base_board()
        board["components"][0]["pads"].append(
            copy.deepcopy(board["components"][0]["pads"][0])
        )
        board["components"][0]["pads"].append(
            {
                "number": "MP",
                "net": "",
                "connectivity_component": "c-mount",
                "no_connect": False,
            }
        )
        projection = NativeBoardProjection.from_snapshot(board)
        self.assertEqual(len(projection.pads), 3)

        mechanical_report = compare_native_consistency(
            _design(),
            NativeSchematicProjection.from_snapshot(_base_schematic()),
            projection,
            candidate_revision=13,
        )
        self.assertTrue(mechanical_report.consistency_passed)

        board["components"][0]["pads"][0]["net"] = "/OUT"
        board["components"][0]["pads"] = [
            pad for pad in board["components"][0]["pads"] if pad["number"] != "MP"
        ]
        board["components"][0]["pads"].pop()
        report = compare_native_consistency(
            _design(),
            NativeSchematicProjection.from_snapshot(_base_schematic()),
            NativeBoardProjection.from_snapshot(board),
            candidate_revision=13,
        )
        self.assertIn(
            "native_pad_net_mismatch", {item.code for item in report.mismatches}
        )

    def test_schematic_reader_connectivity_is_opt_in(self) -> None:
        design = _design()
        graph = PartGraph.bundled()
        with tempfile.TemporaryDirectory(prefix="pcbdraft-consistency-") as temporary:
            path = Path(temporary) / "fixture.kicad_sch"
            generate_schematic(design, path, graph=graph)
            default = inspect_native_schematic(path)
            enriched = inspect_native_schematic(path, include_connectivity=True)

        self.assertNotIn("connectivity", default)
        projection = NativeSchematicProjection.from_snapshot(enriched)
        self.assertEqual(projection.status, "evaluated")
        self.assertEqual(
            {
                _display
                for _display in sorted(
                    f"{e.component}.{e.pin}" for e in projection.endpoints
                )
            },
            {"#FLG01.1", "R1.1", "R1.2"},
        )
        partition_by_endpoint = {
            f"{endpoint.component}.{endpoint.pin}": partition.id
            for partition in projection.partitions
            for endpoint in partition.endpoints
        }
        self.assertEqual(
            partition_by_endpoint["#FLG01.1"], partition_by_endpoint["R1.1"]
        )
        self.assertNotEqual(
            partition_by_endpoint["R1.1"], partition_by_endpoint["R1.2"]
        )

    def test_schematic_reader_requires_a_junction_at_wire_crossings(self) -> None:
        design = _design(second_resistor=True)
        with tempfile.TemporaryDirectory(prefix="pcbdraft-consistency-") as temporary:
            root = Path(temporary)
            for with_junction in (False, True):
                with self.subTest(with_junction=with_junction):
                    path = root / f"crossing-{with_junction}.kicad_sch"
                    generate_schematic(design, path)
                    schematic = Schematic.load(path)
                    schematic.labels.clear()
                    schematic.no_connects.clear()
                    pins = {
                        str(component.reference): {
                            str(number): (point.x, point.y)
                            for number, point in list_component_pins(component)
                        }
                        for component in schematic.components
                    }
                    horizontal_y = pins["R1"]["1"][1]
                    crossing = (52.0, horizontal_y)
                    schematic.wires.add(start=pins["R1"]["1"], end=pins["R2"]["1"])
                    schematic.wires.add(
                        points=[
                            pins["#FLG01"]["1"],
                            (52.0, pins["#FLG01"]["1"][1]),
                            (52.0, horizontal_y + 10.0),
                        ]
                    )
                    if with_junction:
                        schematic.junctions.add(crossing, grid_units=False)
                    schematic.save(path, preserve_format=False)

                    projection = NativeSchematicProjection.from_snapshot(
                        inspect_native_schematic(path, include_connectivity=True)
                    )
                    partition_by_endpoint = {
                        (endpoint.component, endpoint.pin): partition.id
                        for partition in projection.partitions
                        for endpoint in partition.endpoints
                    }
                    resistor_partition = partition_by_endpoint[("R1", "1")]
                    self.assertEqual(
                        resistor_partition, partition_by_endpoint[("R2", "1")]
                    )
                    if with_junction:
                        self.assertEqual(
                            resistor_partition,
                            partition_by_endpoint[("#FLG01", "1")],
                        )
                    else:
                        self.assertNotEqual(
                            resistor_partition,
                            partition_by_endpoint[("#FLG01", "1")],
                        )


@unittest.skipUnless(_real_pcbnew_available(), "real KiCad CLI/pcbnew unavailable")
class NativeBoardConnectivityTests(unittest.TestCase):
    def test_back_side_pose_and_inner_via_layer_pair_round_trip(self) -> None:
        value = _board_design().to_dict()
        value["version"] = 2
        value["board"]["layers"] = 4
        value["scope"]["layers"] = 4
        resistor = next(item for item in value["components"] if item["id"] == "load_r")
        resistor["placement"].update({"rotation_deg": 37.0, "side": "back"})
        value["native_intent"] = {
            "outline": [],
            "footprint_poses": [{"component": resistor["id"], **resistor["placement"]}],
            "routes": [],
            "vias": [
                {
                    "id": "inner_via",
                    "net": "net_3v3",
                    "x_mm": 10.0,
                    "y_mm": 14.0,
                    "diameter_mm": 0.7,
                    "drill_mm": 0.35,
                    "from_layer": 1,
                    "to_layer": 2,
                }
            ],
            "unrouted_nets": [],
            "provenance": "pcbdraft",
            "geometry_revision": 2,
        }
        design = Design.from_dict(value)

        with tempfile.TemporaryDirectory(prefix="pcbdraft-native-pose-") as temporary:
            board_path = Path(temporary) / "pose.kicad_pcb"
            generate_pcb(
                design,
                board_path,
                auto_place=False,
                route_net_ids=frozenset(),
                allow_incomplete=True,
                require_routed=False,
            )
            snapshot = inspect_native_board(design, board_path)

        native_resistor = next(
            item for item in snapshot["components"] if item["reference"] == "R1"
        )
        self.assertEqual(native_resistor["side"], "back")
        self.assertEqual(native_resistor["rotation_deg"], 37.0)
        native_via = next(
            item
            for item in snapshot["tracks"]
            if item["kind"] == "via" and item["net"].lstrip("/") == "3V3"
        )
        self.assertEqual((native_via["from_layer"], native_via["to_layer"]), (1, 2))
        self.assertEqual(native_via["drill_mm"], 0.35)

    def test_native_copper_drives_transitive_pad_connectivity(self) -> None:
        design = _board_design()
        with tempfile.TemporaryDirectory(prefix="pcbdraft-connectivity-") as temporary:
            root = Path(temporary)
            schematic_path = root / "fixture.kicad_sch"
            generate_schematic(design, schematic_path)

            unrouted_path = root / "unrouted.kicad_pcb"
            generate_pcb(
                design,
                unrouted_path,
                auto_place=False,
                route_net_ids=frozenset(),
                allow_incomplete=True,
                require_routed=False,
            )
            default_snapshot = inspect_native_board(design, unrouted_path)
            self.assertNotIn("connectivity", default_snapshot)
            self.assertTrue({"/3V3", "/GND"} <= set(default_snapshot["nets"]))
            resistor = next(
                item
                for item in default_snapshot["components"]
                if item["reference"] == "R1"
            )
            self.assertEqual(resistor["properties"]["Part_ID"], "yageo.rc0603fr-074k7l")
            self.assertTrue(
                all(
                    set(pad) == {"number", "net"}
                    for component in default_snapshot["components"]
                    for pad in component["pads"]
                )
            )
            unrouted_report = inspect_native_consistency(
                design,
                schematic_path,
                unrouted_path,
                candidate_revision=14,
                require_routed_net_ids=frozenset({"net_3v3"}),
            )
            self.assertTrue(
                {"native_connectivity_failed", "native_zero_copper"}
                <= {item.code for item in unrouted_report.mismatches}
            )

            routed_path = root / "routed.kicad_pcb"
            generate_pcb(
                design,
                routed_path,
                auto_place=False,
                route_net_ids=frozenset({"net_3v3"}),
                allow_incomplete=True,
                require_routed=False,
            )
            routed_snapshot = inspect_native_board(
                design, routed_path, include_connectivity=True
            )
            self.assertGreater(len(routed_snapshot["tracks"]), 1)
            component_by_pad = {
                (component["reference"], pad["number"]): pad["connectivity_component"]
                for component in routed_snapshot["components"]
                for pad in component["pads"]
            }
            self.assertEqual(
                component_by_pad[("R1", "1")], component_by_pad[("R2", "1")]
            )
            routed_report = inspect_native_consistency(
                design,
                schematic_path,
                routed_path,
                candidate_revision=15,
                require_routed_net_ids=frozenset({"net_3v3"}),
            )
            self.assertTrue(routed_report.consistency_passed)
            self.assertEqual(routed_report.verification_status, "unknown")
            self.assertFalse(routed_report.passed)


if __name__ == "__main__":
    unittest.main()
