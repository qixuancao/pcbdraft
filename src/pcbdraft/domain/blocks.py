"""Versioned verified-block registry and deterministic block instantiation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import read_bytes_limited
from pcbdraft.core.resources import data_path
from pcbdraft.domain.ir import Component, Endpoint, FunctionalBlock, Placement
from pcbdraft.domain.parts import PartGraph

BLOCK_CATALOG_LIMIT = 4 * 1024 * 1024
BLOCK_CATALOG_SCHEMA = "pcbdraft-block-catalog"
BLOCK_CATALOG_VERSION = 1
BLOCK_STATES = {"unverified", "rule_validated", "human_verified", "production_verified"}


@dataclass(frozen=True)
class BlockDefinition:
    id: str
    version: str
    kind: str
    verification_state: str
    description: str
    required_parts: tuple[str, ...]
    ports: tuple[str, ...]
    constraints: tuple[str, ...]
    evidence: tuple[str, ...]
    verification_tests: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, path: str) -> BlockDefinition:
        if not isinstance(value, Mapping):
            raise ValidationError(f"{path} must be an object")
        required = {
            "id",
            "version",
            "kind",
            "verification_state",
            "description",
            "required_parts",
            "ports",
            "constraints",
            "evidence",
            "verification_tests",
        }
        if set(value) != required:
            raise ValidationError(f"{path} fields do not match the block schema")
        for name in (
            "required_parts",
            "ports",
            "constraints",
            "evidence",
            "verification_tests",
        ):
            if not isinstance(value[name], list) or not all(
                isinstance(entry, str) and entry for entry in value[name]
            ):
                raise ValidationError(
                    f"{path}.{name} must be an array of non-empty strings"
                )
            if len(value[name]) != len(set(value[name])):
                raise ValidationError(f"{path}.{name} contains duplicates")
        if value["verification_state"] not in BLOCK_STATES:
            raise ValidationError(f"{path}.verification_state is unsupported")
        return cls(
            id=value["id"],
            version=value["version"],
            kind=value["kind"],
            verification_state=value["verification_state"],
            description=value["description"],
            required_parts=tuple(sorted(value["required_parts"])),
            ports=tuple(sorted(value["ports"])),
            constraints=tuple(sorted(value["constraints"])),
            evidence=tuple(value["evidence"]),
            verification_tests=tuple(sorted(value["verification_tests"])),
        )


@dataclass(frozen=True)
class BlockInstance:
    block: FunctionalBlock
    components: tuple[Component, ...]
    ports: dict[str, tuple[Endpoint, ...]]


class BlockRegistry:
    def __init__(self, definitions: tuple[BlockDefinition, ...], graph: PartGraph):
        self._definitions = {definition.id: definition for definition in definitions}
        if len(self._definitions) != len(definitions):
            raise ValidationError("block catalog contains duplicate ids")
        self.graph = graph
        for definition in definitions:
            if definition.verification_state not in {
                "rule_validated",
                "human_verified",
                "production_verified",
            }:
                continue
            for part_id in definition.required_parts:
                graph.get(part_id)
            if not definition.verification_tests or not definition.evidence:
                raise ValidationError(
                    f"validated block lacks evidence/tests: {definition.id}"
                )

    @classmethod
    def bundled(cls, graph: PartGraph | None = None) -> BlockRegistry:
        path = data_path("blocks", "catalog.json")
        try:
            value = json.loads(read_bytes_limited(path, BLOCK_CATALOG_LIMIT))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot load bundled block catalog: {exc}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != BLOCK_CATALOG_SCHEMA
            or value.get("version") != BLOCK_CATALOG_VERSION
        ):
            raise ValidationError("unsupported bundled block catalog")
        if value.get("license") != "CC0-1.0" or not isinstance(
            value.get("blocks"), list
        ):
            raise ValidationError("block catalog license or records are malformed")
        definitions = tuple(
            BlockDefinition.from_dict(entry, f"$.blocks[{index}]")
            for index, entry in enumerate(value["blocks"])
        )
        return cls(definitions, graph or PartGraph.bundled())

    def definitions(self) -> tuple[BlockDefinition, ...]:
        return tuple(sorted(self._definitions.values(), key=lambda entry: entry.id))

    def get(self, block_id: str) -> BlockDefinition:
        try:
            return self._definitions[block_id]
        except KeyError as exc:
            raise ValidationError(f"unknown verified block: {block_id}") from exc

    def instantiate(self, block_id: str) -> BlockInstance:
        definition = self.get(block_id)
        if definition.verification_state not in {
            "rule_validated",
            "human_verified",
            "production_verified",
        }:
            raise ValidationError(f"block is not trusted for generation: {block_id}")
        builders = {
            "qwiic_power_input": _qwiic_power_input,
            "spi_power_input": _spi_power_input,
            "regulated_5v_input": _regulated_5v_input,
            "attiny402_core": _attiny402_core,
            "tmp102_i2c_sensor": _tmp102_sensor,
            "bme280_spi_sensor": _bme280_spi_sensor,
            "ap2112_3v3_ldo": _ap2112_ldo,
            "uart_service_connector": _uart_service_connector,
            "gpio_status_led": _status_led,
        }
        try:
            instance = builders[block_id](definition)
        except KeyError as exc:
            raise ValidationError(
                f"block has metadata but no deterministic implementation: {block_id}"
            ) from exc
        actual_parts = {component.part_id for component in instance.components}
        if actual_parts != set(definition.required_parts):
            raise ValidationError(
                f"block implementation/metadata part mismatch: {block_id}"
            )
        if set(instance.ports) != set(definition.ports):
            raise ValidationError(
                f"block implementation/metadata port mismatch: {block_id}"
            )
        if instance.block.components != tuple(
            component.id for component in instance.components
        ):
            raise ValidationError(f"block component identity mismatch: {block_id}")
        if any(component.block_id != block_id for component in instance.components):
            raise ValidationError(f"block component ownership mismatch: {block_id}")
        return instance


def _block(
    definition: BlockDefinition, components: tuple[Component, ...]
) -> FunctionalBlock:
    return FunctionalBlock(
        id=definition.id,
        kind=definition.kind,
        name=definition.description,
        version=definition.version,
        intent=definition.description,
        components=tuple(component.id for component in components),
        provenance=(f"block_{definition.id}",),
    )


def _component(
    component_id: str,
    reference: str,
    part_id: str,
    value: str,
    block_id: str,
    position: tuple[float, float],
    *,
    rotation: float = 0,
    fixed: bool = False,
    attributes: dict[str, Any] | None = None,
) -> Component:
    return Component(
        id=component_id,
        reference=reference,
        part_id=part_id,
        value=value,
        block_id=block_id,
        placement=Placement(position[0], position[1], rotation, "front", fixed),
        attributes=attributes or {},
    )


def _qwiic_power_input(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "qwiic_j1",
            "J1",
            "jst.sm04b-srss-tb",
            "QWIIC",
            definition.id,
            (40.5, 15),
            rotation=270,
            fixed=True,
        ),
        _component(
            "flag_3v3",
            "#FLG01",
            "kicad.pwr_flag",
            "PWR_FLAG",
            definition.id,
            (20, 20),
            attributes={"exclude_from_board": True},
        ),
        _component(
            "flag_gnd",
            "#FLG02",
            "kicad.pwr_flag",
            "PWR_FLAG",
            definition.id,
            (20, 25),
            attributes={"exclude_from_board": True},
        ),
    )
    ports = {
        "gnd": (
            Endpoint("qwiic_j1", "1", "source"),
            Endpoint("flag_gnd", "1", "source"),
        ),
        "vcc": (
            Endpoint("qwiic_j1", "2", "source"),
            Endpoint("flag_3v3", "1", "source"),
        ),
        "sda": (Endpoint("qwiic_j1", "3", "external"),),
        "scl": (Endpoint("qwiic_j1", "4", "external"),),
    }
    return BlockInstance(_block(definition, components), components, ports)


def _spi_power_input(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "spi_j1",
            "J1",
            "samtec.tsw-102-07-g-s",
            "3V3_POWER",
            definition.id,
            (40.5, 14.5),
            fixed=True,
        ),
        _component(
            "flag_3v3",
            "#FLG01",
            "kicad.pwr_flag",
            "PWR_FLAG",
            definition.id,
            (20, 20),
            attributes={"exclude_from_board": True},
        ),
        _component(
            "flag_gnd",
            "#FLG02",
            "kicad.pwr_flag",
            "PWR_FLAG",
            definition.id,
            (20, 25),
            attributes={"exclude_from_board": True},
        ),
    )
    ports: dict[str, tuple[Endpoint, ...]] = {
        "gnd": (
            Endpoint("spi_j1", "2", "source"),
            Endpoint("flag_gnd", "1", "source"),
        ),
        "vcc": (
            Endpoint("spi_j1", "1", "source"),
            Endpoint("flag_3v3", "1", "source"),
        ),
    }
    return BlockInstance(_block(definition, components), components, ports)


def _regulated_5v_input(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "power_j1",
            "J1",
            "samtec.tsw-102-07-g-s",
            "5V_INPUT",
            definition.id,
            (28, 25),
            rotation=90,
            fixed=True,
        ),
        _component(
            "flag_5v",
            "#FLG01",
            "kicad.pwr_flag",
            "PWR_FLAG",
            definition.id,
            (20, 20),
            attributes={"exclude_from_board": True},
        ),
        _component(
            "flag_gnd",
            "#FLG02",
            "kicad.pwr_flag",
            "PWR_FLAG",
            definition.id,
            (20, 25),
            attributes={"exclude_from_board": True},
        ),
    )
    ports: dict[str, tuple[Endpoint, ...]] = {
        "vin": (
            Endpoint("power_j1", "1", "source"),
            Endpoint("flag_5v", "1", "source"),
        ),
        "gnd": (
            Endpoint("power_j1", "2", "source"),
            Endpoint("flag_gnd", "1", "source"),
        ),
    }
    return BlockInstance(_block(definition, components), components, ports)


def _attiny402_core(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "mcu_u1",
            "U1",
            "microchip.attiny402-ssn",
            "ATtiny402-SS",
            definition.id,
            (17, 15),
            fixed=True,
        ),
        _component(
            "mcu_c1",
            "C1",
            "murata.grm188r71c104ka01d",
            "100n",
            definition.id,
            (17, 10.5),
            fixed=True,
        ),
        _component(
            "updi_j2",
            "J2",
            "samtec.tsw-103-07-g-s",
            "UPDI_VTREF_SENSE",
            definition.id,
            (4, 15),
            rotation=90,
            fixed=True,
        ),
    )
    ports = {
        "vcc": (
            Endpoint("mcu_u1", "1", "load"),
            Endpoint("mcu_c1", "1", "decoupling"),
            Endpoint("updi_j2", "2", "voltage_sense"),
        ),
        "gnd": (
            Endpoint("mcu_u1", "8", "return"),
            Endpoint("mcu_c1", "2", "decoupling"),
            Endpoint("updi_j2", "3", "external"),
        ),
        "i2c_sda": (Endpoint("mcu_u1", "4", "controller"),),
        "i2c_scl": (Endpoint("mcu_u1", "5", "controller"),),
        "spi_mosi": (Endpoint("mcu_u1", "4", "controller"),),
        "spi_miso": (Endpoint("mcu_u1", "5", "peripheral"),),
        "spi_sck": (Endpoint("mcu_u1", "7", "controller"),),
        "spi_cs": (Endpoint("mcu_u1", "2", "controller"),),
        "uart_tx": (Endpoint("mcu_u1", "4", "controller"),),
        "uart_rx": (Endpoint("mcu_u1", "5", "peripheral"),),
        "status_gpio": (Endpoint("mcu_u1", "2", "driver"),),
        "updi": (
            Endpoint("mcu_u1", "6", "programming"),
            Endpoint("updi_j2", "1", "external"),
        ),
    }
    return BlockInstance(_block(definition, components), components, ports)


def _tmp102_sensor(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "sensor_u2",
            "U2",
            "ti.tmp102bdrlr",
            "TMP102B",
            definition.id,
            (31, 15),
            attributes={"allow_unconnected_pins": ["3"]},
        ),
        _component(
            "sensor_c2",
            "C2",
            "murata.grm188r71c104ka01d",
            "100n",
            definition.id,
            (31, 13),
        ),
        _component(
            "pullup_r1",
            "R1",
            "yageo.rc0603fr-074k7l",
            "4.7k",
            definition.id,
            (27, 10),
            rotation=90,
        ),
        _component(
            "pullup_r2",
            "R2",
            "yageo.rc0603fr-074k7l",
            "4.7k",
            definition.id,
            (35, 10),
            rotation=90,
        ),
    )
    ports = {
        "vcc": (
            Endpoint("sensor_u2", "5", "load"),
            Endpoint("sensor_c2", "1", "decoupling"),
            Endpoint("pullup_r1", "1", "pullup"),
            Endpoint("pullup_r2", "1", "pullup"),
        ),
        "gnd": (
            Endpoint("sensor_u2", "2", "return"),
            Endpoint("sensor_u2", "4", "address_strap"),
            Endpoint("sensor_c2", "2", "decoupling"),
        ),
        "sda": (
            Endpoint("sensor_u2", "6", "peripheral"),
            Endpoint("pullup_r1", "2", "pullup"),
        ),
        "scl": (
            Endpoint("sensor_u2", "1", "peripheral"),
            Endpoint("pullup_r2", "2", "pullup"),
        ),
    }
    return BlockInstance(_block(definition, components), components, ports)


def _bme280_spi_sensor(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "sensor_u2",
            "U2",
            "bosch.bme280",
            "BME280",
            definition.id,
            (30, 10),
        ),
        _component(
            "sensor_c2",
            "C2",
            "murata.grm188r71c104ka01d",
            "100n",
            definition.id,
            (30, 14),
        ),
        _component(
            "cs_r1",
            "R1",
            "yageo.rc0603fr-0710kl",
            "10k",
            definition.id,
            (34, 10),
            rotation=90,
        ),
    )
    ports = {
        "vcc": (
            Endpoint("sensor_u2", "6", "load"),
            Endpoint("sensor_u2", "8", "load"),
            Endpoint("sensor_c2", "1", "decoupling"),
            Endpoint("cs_r1", "1", "pullup"),
        ),
        "gnd": (
            Endpoint("sensor_u2", "1", "return"),
            Endpoint("sensor_u2", "7", "return"),
            Endpoint("sensor_c2", "2", "decoupling"),
        ),
        "mosi": (Endpoint("sensor_u2", "3", "peripheral"),),
        "miso": (Endpoint("sensor_u2", "5", "peripheral"),),
        "sck": (Endpoint("sensor_u2", "4", "peripheral"),),
        "cs": (
            Endpoint("sensor_u2", "2", "peripheral"),
            Endpoint("cs_r1", "2", "pullup"),
        ),
    }
    return BlockInstance(_block(definition, components), components, ports)


def _ap2112_ldo(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "ldo_u2",
            "U2",
            "diodes.ap2112k-3.3trg1",
            "AP2112K-3.3",
            definition.id,
            (27, 15),
            attributes={"allow_unconnected_pins": ["4"]},
        ),
        _component(
            "ldo_c2",
            "C2",
            "murata.grm188r71a105ka61d",
            "1u",
            definition.id,
            (24, 13),
        ),
        _component(
            "ldo_c3",
            "C3",
            "murata.grm188r71a105ka61d",
            "1u",
            definition.id,
            (30, 13),
        ),
    )
    ports = {
        "vin": (
            Endpoint("ldo_u2", "1", "load"),
            Endpoint("ldo_u2", "3", "enable"),
            Endpoint("ldo_c2", "1", "decoupling"),
        ),
        "vout": (
            Endpoint("ldo_u2", "5", "source"),
            Endpoint("ldo_c3", "1", "decoupling"),
        ),
        "gnd": (
            Endpoint("ldo_u2", "2", "return"),
            Endpoint("ldo_c2", "2", "decoupling"),
            Endpoint("ldo_c3", "2", "decoupling"),
        ),
    }
    return BlockInstance(_block(definition, components), components, ports)


def _uart_service_connector(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "uart_j3",
            "J3",
            "samtec.tsw-104-07-g-s",
            "UART_3V3",
            definition.id,
            (40.5, 15),
            fixed=True,
        ),
    )
    ports: dict[str, tuple[Endpoint, ...]] = {
        "gnd": (Endpoint("uart_j3", "1", "external"),),
        "vcc": (Endpoint("uart_j3", "2", "voltage_sense"),),
        "tx": (Endpoint("uart_j3", "3", "external"),),
        "rx": (Endpoint("uart_j3", "4", "external"),),
    }
    return BlockInstance(_block(definition, components), components, ports)


def _status_led(definition: BlockDefinition) -> BlockInstance:
    components = (
        _component(
            "led_r3", "R3", "yageo.rc0603fr-071kl", "1k", definition.id, (18, 24)
        ),
        _component(
            "led_d1",
            "D1",
            "liteon.ltst-c190kgkt",
            "GREEN",
            definition.id,
            (25, 24),
            rotation=180,
        ),
    )
    ports = {
        "gpio": (Endpoint("led_r3", "1", "load"),),
        "anode": (Endpoint("led_r3", "2", "limiter"), Endpoint("led_d1", "2", "anode")),
        "gnd": (Endpoint("led_d1", "1", "cathode"),),
    }
    return BlockInstance(_block(definition, components), components, ports)
