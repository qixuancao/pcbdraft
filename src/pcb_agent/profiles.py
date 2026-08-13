"""Verified end-user design profiles built on the deterministic runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError
from .requirements import RequirementsSpec


@dataclass(frozen=True)
class ProductProfile:
    id: str
    title: str
    summary: str
    verified_capabilities: tuple[str, ...]
    unavailable_capabilities: tuple[str, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "verified_capabilities": list(self.verified_capabilities),
            "unavailable_capabilities": list(self.unavailable_capabilities),
        }


I2C_CONTROLLER = ProductProfile(
    id="low_voltage_i2c_controller_v1",
    title="ATtiny402 + TMP102 I2C sensor/controller",
    summary=(
        "Externally regulated 3.3 V temperature/controller board with Qwiic, "
        "UPDI, and a status indicator."
    ),
    verified_capabilities=(
        "low_voltage_mcu",
        "temperature_sensor",
        "i2c_100khz",
        "external_regulated_3v3",
        "updi",
        "status_indicator",
        "two_or_four_layers",
    ),
    unavailable_capabilities=(
        "on_board_regulation",
        "usb",
        "wireless",
        "safety_critical",
        "physical_environmental_signoff",
    ),
)


_PROFILES = {I2C_CONTROLLER.id: I2C_CONTROLLER}


def product_profiles() -> tuple[ProductProfile, ...]:
    return tuple(_PROFILES[key] for key in sorted(_PROFILES))


def get_product_profile(profile_id: str) -> ProductProfile:
    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        raise ValidationError(
            f"unknown or unverified product profile: {profile_id}"
        ) from exc


def safe_design_id(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    if not value or not value[0].isalpha():
        value = "copperwright_board"
    return value[:64].rstrip("_") or "copperwright_board"


def build_requirements(
    profile_id: str,
    *,
    design_name: str,
    design_id: str,
    layers: int,
    width_mm: float,
    height_mm: float,
    source_locator: str,
    source_date: str,
) -> RequirementsSpec:
    """Construct strict requirements using only a fully verified profile contract."""

    get_product_profile(profile_id)
    if profile_id != I2C_CONTROLLER.id:
        raise ValidationError(f"profile compiler is unavailable: {profile_id}")
    if layers not in {2, 4}:
        raise ValidationError("verified profiles accept only 2 or 4 copper layers")
    if not 40 <= width_mm <= 120 or not 28 <= height_mm <= 100:
        raise ValidationError(
            "this profile requires a 40-120 mm × 28-100 mm board envelope"
        )
    value: dict[str, Any] = {
        "schema": "pcb-agent-requirements",
        "version": 1,
        "design_id": safe_design_id(design_id),
        "name": design_name,
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
            "layers": layers,
            "intended_use": (
                "Non-safety-critical low-voltage sensing/control prototype."
            ),
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
                "intent": (
                    "Accept regulated 3.3 V and expose the sensor bus on a JST-SH "
                    "connector."
                ),
                "parameters": {"pin_order": ["GND", "3V3", "SDA", "SCL"]},
            },
            {
                "id": "programming",
                "kind": "updi_programming",
                "intent": (
                    "Expose UPDI, target-voltage sense, and ground on a serviceable "
                    "header."
                ),
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
            "width_mm": width_mm,
            "height_mm": height_mm,
            "layers": layers,
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
            "name": "confirmed CopperWright conversation",
            "locator": source_locator,
            "acquired_at": source_date,
        },
    }
    return RequirementsSpec.from_dict(value)
