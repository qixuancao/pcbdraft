"""Accepted low-voltage requirements fixture."""

from __future__ import annotations

from typing import Any


def controller_requirements_dict() -> dict[str, Any]:
    return {
        "schema": "copperwright-requirements",
        "version": 1,
        "design_id": "attiny_sensor_controller",
        "name": "ATtiny temperature controller",
        "revision": "1.0",
        "scope": {
            "domains": [
                "low_voltage_mcu",
                "sensor",
                "simple_control",
                "i2c",
            ],
            "max_voltage_v": 3.465,
            "max_current_a": 0.1,
            "max_power_w": 0.3465,
            "layers": 2,
            "intended_use": "Non-safety-critical environmental sensing prototype.",
            "risk_class": "prototype",
        },
        "functions": [
            {
                "id": "controller",
                "kind": "microcontroller",
                "intent": "Run deterministic sensor acquisition and status firmware.",
                "parameters": {"programming": "updi"},
            },
            {
                "id": "temperature",
                "kind": "temperature_sensor",
                "intent": "Measure local board temperature over I2C.",
                "parameters": {"accuracy_class": "general_purpose"},
            },
            {
                "id": "status",
                "kind": "status_indicator",
                "intent": "Expose a current-limited green status indication.",
                "parameters": {"color": "green"},
            },
            {
                "id": "external_bus",
                "kind": "i2c_connector",
                "intent": "Accept regulated 3.3 V and expose the sensor bus on a JST-SH connector.",
                "parameters": {"pin_order": ["GND", "3V3", "SDA", "SCL"]},
            },
            {
                "id": "programming",
                "kind": "updi_programming",
                "intent": "Expose UPDI, target-voltage sense, and ground on a serviceable header.",
                "parameters": {
                    "connector_pitch_mm": 2.54,
                    "power_pin_mode": "target_voltage_sense_only",
                },
            },
        ],
        "power": {
            "nominal_v": 3.3,
            "min_v": 3.0,
            "max_v": 3.465,
            "max_current_a": 0.1,
        },
        "interfaces": [
            {
                "id": "sensor_i2c",
                "kind": "i2c",
                "speed_hz": 100000,
                "external_connector": True,
                "bus_capacitance_pf_max": 200,
                "external_pullups": "forbidden",
            }
        ],
        "board": {
            "width_mm": 45,
            "height_mm": 30,
            "layers": 2,
            "thickness_mm": 1.6,
            "edge_clearance_mm": 0.5,
            "min_track_mm": 0.2,
            "min_clearance_mm": 0.1,
            "min_drill_mm": 0.3,
            "finish": "hasl_lead_free",
        },
        "priorities": [
            "electrical_correctness",
            "manufacturability",
            "compact_sensor_group",
        ],
        "source": {
            "name": "independent acceptance fixture",
            "locator": "tests/requirements_factory.py",
            "acquired_at": "2026-08-12",
        },
    }
