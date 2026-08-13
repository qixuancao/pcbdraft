"""Small valid semantic design used by focused unit tests."""

from __future__ import annotations

from typing import Any


def minimal_design_dict() -> dict[str, Any]:
    return {
        "schema": "copperwright-ir",
        "version": 1,
        "design_id": "minimal_control",
        "name": "Minimal control fixture",
        "revision": "1.0",
        "scope": {
            "domains": ["low_voltage_mcu"],
            "max_voltage_v": 3.6,
            "max_current_a": 0.1,
            "max_power_w": 0.36,
            "layers": 2,
            "intended_use": "non-safety-critical unit fixture",
            "risk_class": "prototype",
        },
        "requirements": [
            {
                "id": "req_power",
                "text": "Provide a bounded 3.3 V domain.",
                "acceptance": ["All loads remain inside their supply ratings."],
                "risk": "low",
                "provenance": ["user_spec"],
            }
        ],
        "provenance": [
            {
                "id": "user_spec",
                "kind": "requirement",
                "source": "unit test",
                "locator": "tests/design_factory.py",
                "method": "user_supplied",
                "confidence": 1.0,
            }
        ],
        "blocks": [
            {
                "id": "power_block",
                "kind": "test_load",
                "name": "Power load",
                "version": "1",
                "intent": "Exercise power and passive contracts.",
                "components": ["source_flag", "load_r"],
                "provenance": ["user_spec"],
            }
        ],
        "power_domains": [
            {
                "id": "v3v3",
                "nominal_v": 3.3,
                "min_v": 3.0,
                "max_v": 3.6,
                "max_current_a": 0.1,
                "source": {"component": "source_flag", "pin": "1", "role": "source"},
                "intent": "Externally supplied low-voltage rail.",
            }
        ],
        "interfaces": [],
        "components": [
            {
                "id": "load_r",
                "reference": "R1",
                "part_id": "yageo.rc0603fr-074k7l",
                "value": "4.7k",
                "block_id": "power_block",
                "placement": {
                    "x_mm": 10,
                    "y_mm": 10,
                    "rotation_deg": 0,
                    "side": "front",
                    "fixed": False,
                },
                "attributes": {},
            },
            {
                "id": "source_flag",
                "reference": "#FLG01",
                "part_id": "kicad.pwr_flag",
                "value": "PWR_FLAG",
                "block_id": "power_block",
                "attributes": {"exclude_from_board": True},
            },
        ],
        "nets": [
            {
                "id": "net_3v3",
                "name": "3V3",
                "endpoints": [
                    {"component": "source_flag", "pin": "1", "role": "source"},
                    {"component": "load_r", "pin": "1", "role": "load"},
                ],
                "net_class": "power",
                "power_domain": "v3v3",
                "intent": "Power input to the fixture load.",
            },
            {
                "id": "net_out",
                "name": "OUT",
                "endpoints": [{"component": "load_r", "pin": "2", "role": "signal"}],
                "net_class": "signal",
                "intent": "Single-ended test point.",
            },
        ],
        "constraints": [
            {
                "id": "board_rules",
                "kind": "manufacturing_rules",
                "targets": ["board"],
                "params": {"min_clearance_mm": 0.2},
                "severity": "release_blocking",
                "rationale": "Match the declared board contract.",
                "provenance": ["user_spec"],
            }
        ],
        "board": {
            "width_mm": 20,
            "height_mm": 20,
            "layers": 2,
            "thickness_mm": 1.6,
            "edge_clearance_mm": 0.5,
            "min_track_mm": 0.2,
            "min_clearance_mm": 0.2,
            "min_drill_mm": 0.3,
            "finish": "hasl_lead_free",
        },
        "analyses": [{"id": "power_budget", "kind": "power_budget", "required": True}],
        "metadata": {"fixture": True},
    }
