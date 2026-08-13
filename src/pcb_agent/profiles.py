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

SPI_ENVIRONMENT = ProductProfile(
    id="low_voltage_spi_environment_v1",
    title="ATtiny402 + BME280 SPI environmental sensor",
    summary=(
        "Externally regulated 3.3 V environmental sensor/controller with a "
        "dedicated power header and UPDI."
    ),
    verified_capabilities=(
        "low_voltage_mcu",
        "environmental_sensor",
        "board_local_spi_mode0_1mhz",
        "external_regulated_3v3",
        "updi",
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

UART_LDO_CONTROLLER = ProductProfile(
    id="low_voltage_uart_ldo_controller_v1",
    title="ATtiny402 UART controller with AP2112K 3.3 V LDO",
    summary=(
        "Regulated 5 V-input controller with on-board 3.3 V LDO, a 3.3 V CMOS "
        "UART service header, UPDI, and a status indicator."
    ),
    verified_capabilities=(
        "low_voltage_mcu",
        "uart_3v3_cmos",
        "regulated_5v_input",
        "ap2112_3v3_ldo",
        "updi",
        "status_indicator",
        "two_or_four_layers",
    ),
    unavailable_capabilities=(
        "rs232_voltage_levels",
        "usb",
        "buck_converter",
        "wireless",
        "safety_critical",
        "physical_environmental_signoff",
    ),
)


_PROFILES = {
    I2C_CONTROLLER.id: I2C_CONTROLLER,
    SPI_ENVIRONMENT.id: SPI_ENVIRONMENT,
    UART_LDO_CONTROLLER.id: UART_LDO_CONTROLLER,
}


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
    if profile_id == SPI_ENVIRONMENT.id:
        value["scope"]["domains"] = [
            "low_voltage_mcu",
            "sensor",
            "simple_control",
            "spi",
        ]
        value["functions"] = [
            {
                "id": "controller",
                "kind": "microcontroller",
                "intent": "Acquire environmental measurements and expose deterministic control firmware hooks.",
                "parameters": {"programming": "updi"},
            },
            {
                "id": "environment",
                "kind": "environmental_sensor",
                "intent": "Measure local temperature, humidity, and pressure with a BME280 in four-wire SPI mode.",
                "parameters": {"part": "BME280", "mode": "spi_4wire"},
            },
            {
                "id": "power",
                "kind": "power_input",
                "intent": "Accept polarity-defined externally regulated 3.3 V input.",
                "parameters": {"nominal_v": 3.3, "polarity": ["3V3", "GND"]},
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
        ]
        value["interfaces"] = [
            {
                "id": "sensor_spi",
                "kind": "spi",
                "clock_hz": 1_000_000,
                "mode": 0,
                "external_connector": False,
            }
        ]
        value["priorities"] = [
            "electrical_correctness",
            "manufacturability",
            "compact_spi_sensor_group",
        ]
    elif profile_id == UART_LDO_CONTROLLER.id:
        value["scope"] = {
            "domains": [
                "low_voltage_mcu",
                "simple_control",
                "uart",
                "ldo",
            ],
            "max_voltage_v": 5.25,
            "max_current_a": 0.1,
            "max_power_w": 0.525,
            "layers": layers,
            "intended_use": (
                "Non-safety-critical low-voltage serial control prototype."
            ),
            "risk_class": "prototype",
        }
        value["functions"] = [
            {
                "id": "controller",
                "kind": "microcontroller",
                "intent": "Run deterministic low-voltage serial control firmware.",
                "parameters": {"programming": "updi"},
            },
            {
                "id": "status",
                "kind": "status_indicator",
                "intent": "Expose a current-limited green status indication.",
                "parameters": {"color": "green"},
            },
            {
                "id": "serial",
                "kind": "uart_connector",
                "intent": "Expose a 3.3 V CMOS 8-N-1 UART service interface.",
                "parameters": {"pin_order": ["GND", "3V3", "TX", "RX"]},
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
            {
                "id": "regulator",
                "kind": "ldo_regulator",
                "intent": "Regulate the 5 V input to a bounded 3.3 V rail using AP2112K.",
                "parameters": {"part": "AP2112K-3.3", "output_v": 3.3},
            },
            {
                "id": "power",
                "kind": "power_input",
                "intent": "Accept polarity-defined externally regulated 5 V input.",
                "parameters": {"nominal_v": 5.0, "polarity": ["VIN", "GND"]},
            },
        ]
        value["power"] = {
            "nominal_v": 5.0,
            "min_v": 4.75,
            "max_v": 5.25,
            "max_current_a": 0.1,
        }
        value["interfaces"] = [
            {
                "id": "service_uart",
                "kind": "uart",
                "baud": 115_200,
                "data_bits": 8,
                "parity": "none",
                "stop_bits": 1,
                "external_connector": True,
            }
        ]
        value["priorities"] = [
            "electrical_correctness",
            "regulator_loop_compactness",
            "manufacturability",
        ]
    return RequirementsSpec.from_dict(value)
