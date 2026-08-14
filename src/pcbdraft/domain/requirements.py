"""Legacy deterministic requirements-to-semantic-design fixture compiler.

The conversational product path is the generic agent-plan runtime. This module
remains for independently reproducible regression fixtures of the KiCad backend.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import read_bytes_limited
from pcbdraft.domain.blocks import BlockRegistry
from pcbdraft.domain.ir import (
    IR_FILE_LIMIT,
    BoardSpec,
    Constraint,
    Design,
    Endpoint,
    Interface,
    Net,
    PowerDomain,
    Provenance,
    Requirement,
    Scope,
    _identifier,
    _json_value,
    _strict_mapping,
    _string,
    canonical_json_bytes,
)
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.scope import assert_scope_supported, assert_supported

REQUIREMENTS_SCHEMA = "pcbdraft-requirements"
REQUIREMENTS_VERSION = 1
SUPPORTED_FUNCTIONS = {
    "microcontroller",
    "temperature_sensor",
    "environmental_sensor",
    "status_indicator",
    "i2c_connector",
    "uart_connector",
    "updi_programming",
    "ldo_regulator",
    "power_input",
}
GENERATION_PROFILE_ID = "low_voltage_i2c_controller_v1"
I2C_PROFILE_FUNCTIONS = {
    "microcontroller",
    "temperature_sensor",
    "status_indicator",
    "i2c_connector",
    "updi_programming",
}
SPI_PROFILE_ID = "low_voltage_spi_environment_v1"
SPI_PROFILE_FUNCTIONS = {
    "microcontroller",
    "environmental_sensor",
    "power_input",
    "updi_programming",
}
UART_LDO_PROFILE_ID = "low_voltage_uart_ldo_controller_v1"
UART_LDO_PROFILE_FUNCTIONS = {
    "microcontroller",
    "status_indicator",
    "uart_connector",
    "updi_programming",
    "ldo_regulator",
    "power_input",
}
PROFILE_FUNCTION_SETS = {
    GENERATION_PROFILE_ID: I2C_PROFILE_FUNCTIONS,
    SPI_PROFILE_ID: SPI_PROFILE_FUNCTIONS,
    UART_LDO_PROFILE_ID: UART_LDO_PROFILE_FUNCTIONS,
}
PROFILE_DOMAINS = {
    GENERATION_PROFILE_ID: {
        "low_voltage_mcu",
        "sensor",
        "simple_control",
        "i2c",
    },
    SPI_PROFILE_ID: {
        "low_voltage_mcu",
        "sensor",
        "simple_control",
        "spi",
    },
    UART_LDO_PROFILE_ID: {
        "low_voltage_mcu",
        "simple_control",
        "uart",
        "ldo",
    },
}
GENERATION_PROFILE_DOMAINS = PROFILE_DOMAINS[GENERATION_PROFILE_ID]
PROFILE_FUNCTION_PARAMETERS: dict[str, dict[str, dict[str, Any]]] = {
    GENERATION_PROFILE_ID: {
        "microcontroller": {"programming": "updi"},
        "temperature_sensor": {"accuracy_class": "general_purpose"},
        "status_indicator": {"color": "green"},
        "i2c_connector": {"pin_order": ["GND", "3V3", "SDA", "SCL"]},
        "updi_programming": {
            "connector_pitch_mm": 2.54,
            "power_pin_mode": "target_voltage_sense_only",
        },
    },
    SPI_PROFILE_ID: {
        "microcontroller": {"programming": "updi"},
        "environmental_sensor": {"part": "BME280", "mode": "spi_4wire"},
        "power_input": {"nominal_v": 3.3, "polarity": ["3V3", "GND"]},
        "updi_programming": {
            "connector_pitch_mm": 2.54,
            "power_pin_mode": "target_voltage_sense_only",
        },
    },
    UART_LDO_PROFILE_ID: {
        "microcontroller": {"programming": "updi"},
        "status_indicator": {"color": "green"},
        "uart_connector": {"pin_order": ["GND", "3V3", "TX", "RX"]},
        "updi_programming": {
            "connector_pitch_mm": 2.54,
            "power_pin_mode": "target_voltage_sense_only",
        },
        "ldo_regulator": {"part": "AP2112K-3.3", "output_v": 3.3},
        "power_input": {"nominal_v": 5.0, "polarity": ["VIN", "GND"]},
    },
}


@dataclass(frozen=True)
class RequirementsSpec:
    design_id: str
    name: str
    revision: str
    scope: Scope
    functions: tuple[dict[str, Any], ...]
    power: dict[str, Any]
    interfaces: tuple[dict[str, Any], ...]
    board: BoardSpec
    priorities: tuple[str, ...]
    source: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> RequirementsSpec:
        item = _strict_mapping(
            value,
            "$",
            required={
                "schema",
                "version",
                "design_id",
                "name",
                "revision",
                "scope",
                "functions",
                "power",
                "interfaces",
                "board",
                "priorities",
                "source",
            },
            optional=set(),
        )
        if (
            item["schema"] != REQUIREMENTS_SCHEMA
            or item["version"] != REQUIREMENTS_VERSION
        ):
            raise ValidationError("unsupported requirements schema/version")
        functions = item["functions"]
        if not isinstance(functions, list) or not functions:
            raise ValidationError("$.functions must be a non-empty array")
        normalized_functions: list[dict[str, Any]] = []
        function_ids: set[str] = set()
        for index, function in enumerate(functions):
            entry = _strict_mapping(
                function,
                f"$.functions[{index}]",
                required={"id", "kind", "intent"},
                optional={"parameters"},
            )
            function_id = _identifier(entry["id"], f"$.functions[{index}].id")
            if function_id in function_ids:
                raise ValidationError(f"duplicate function id: {function_id}")
            function_ids.add(function_id)
            kind = _identifier(entry["kind"], f"$.functions[{index}].kind")
            if kind not in SUPPORTED_FUNCTIONS:
                raise ValidationError(f"unsupported requirements function: {kind}")
            parameters = entry.get("parameters", {})
            if not isinstance(parameters, Mapping):
                raise ValidationError(
                    f"$.functions[{index}].parameters must be an object"
                )
            normalized_functions.append(
                {
                    "id": function_id,
                    "kind": kind,
                    "intent": _string(
                        entry["intent"], f"$.functions[{index}].intent", limit=2048
                    ),
                    "parameters": _json_value(parameters),
                }
            )
        for name in ("power", "source"):
            if not isinstance(item[name], Mapping):
                raise ValidationError(f"$.{name} must be an object")
        interfaces = item["interfaces"]
        if not isinstance(interfaces, list) or not all(
            isinstance(entry, Mapping) for entry in interfaces
        ):
            raise ValidationError("$.interfaces must be an array of objects")
        priorities = item["priorities"]
        if not isinstance(priorities, list) or not all(
            isinstance(entry, str) and entry for entry in priorities
        ):
            raise ValidationError("$.priorities must be an array of non-empty strings")
        return cls(
            design_id=_identifier(item["design_id"], "$.design_id"),
            name=_string(item["name"], "$.name", limit=256),
            revision=_string(item["revision"], "$.revision", limit=64),
            scope=Scope.from_dict(item["scope"]),
            functions=tuple(
                sorted(normalized_functions, key=lambda entry: entry["id"])
            ),
            power=_json_value(item["power"], "$.power"),
            interfaces=tuple(
                sorted(
                    (_json_value(entry) for entry in interfaces),
                    key=lambda entry: entry.get("id", ""),
                )
            ),
            board=BoardSpec.from_dict(item["board"]),
            priorities=tuple(priorities),
            source=_json_value(item["source"], "$.source"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REQUIREMENTS_SCHEMA,
            "version": REQUIREMENTS_VERSION,
            "design_id": self.design_id,
            "name": self.name,
            "revision": self.revision,
            "scope": self.scope.to_dict(),
            "functions": list(self.functions),
            "power": _json_value(self.power),
            "interfaces": list(self.interfaces),
            "board": self.board.to_dict(),
            "priorities": list(self.priorities),
            "source": _json_value(self.source),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def load_requirements(path: str | Path) -> RequirementsSpec:
    source = Path(path)
    try:
        value = json.loads(read_bytes_limited(source, IR_FILE_LIMIT))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load requirements {source}: {exc}") from exc
    return RequirementsSpec.from_dict(value)


def compile_requirements(
    spec: RequirementsSpec,
    *,
    graph: PartGraph | None = None,
    registry: BlockRegistry | None = None,
    check_libraries: bool = True,
) -> Design:
    """Compile accepted functional requirements without probabilistic decisions."""
    resolved_graph = graph or PartGraph.bundled()
    resolved_registry = registry or BlockRegistry.bundled(resolved_graph)
    if spec.scope.layers != spec.board.layers:
        raise ValidationError(
            "requirements scope and board specify different layer counts"
        )
    # Check only concrete backend prerequisites before the legacy fixture's
    # narrower, profile-specific feature check below.
    assert_scope_supported(spec.scope)
    functions = {entry["kind"]: entry for entry in spec.functions}
    if len(functions) != len(spec.functions):
        raise ValidationError(
            "a verified profile accepts only one instance of each function kind"
        )
    profile_id = _select_profile(set(functions))
    unsupported_profile_domains = set(spec.scope.domains) - PROFILE_DOMAINS[profile_id]
    if unsupported_profile_domains:
        raise ValidationError(
            f"the {profile_id} bundled generator does not implement domains: "
            + ", ".join(sorted(unsupported_profile_domains))
        )
    _validate_profile_functions(profile_id, functions)
    if profile_id == SPI_PROFILE_ID:
        return _compile_spi_profile(
            spec, functions, resolved_graph, resolved_registry, check_libraries
        )
    if profile_id == UART_LDO_PROFILE_ID:
        return _compile_uart_ldo_profile(
            spec, functions, resolved_graph, resolved_registry, check_libraries
        )
    instances = [
        resolved_registry.instantiate("qwiic_power_input"),
        resolved_registry.instantiate("attiny402_core"),
        resolved_registry.instantiate("tmp102_i2c_sensor"),
        resolved_registry.instantiate("gpio_status_led"),
    ]
    components = tuple(
        component for instance in instances for component in instance.components
    )
    blocks = tuple(instance.block for instance in instances)
    ports = {instance.block.id: instance.ports for instance in instances}

    power = _power_contract(spec.power)
    _validate_power_scope(spec, power)
    i2c = _i2c_contract(spec.interfaces)
    provenance = _provenance(spec, resolved_registry)
    requirements = tuple(
        Requirement(
            id=f"req_{function['id']}",
            text=function["intent"],
            acceptance=_function_acceptance(
                function["kind"], function.get("parameters", {})
            ),
            risk="low",
            provenance=("user_requirements",),
        )
        for function in spec.functions
    ) + (
        Requirement(
            id="req_manufacturing",
            text="Produce a fabricable, assembly-ready two-to-four-layer design candidate.",
            acceptance=(
                "ERC and DRC contain no errors.",
                "BOM records resolve to active canonical parts with source evidence.",
                "Gerber, drill, placement, and manifest artifacts are reproducible.",
            ),
            risk="medium",
            provenance=("user_requirements",),
        ),
    )

    nets = (
        Net(
            id="net_3v3",
            name="3V3",
            endpoints=_merge_ports(
                ports,
                ("qwiic_power_input", "vcc"),
                ("attiny402_core", "vcc"),
                ("tmp102_i2c_sensor", "vcc"),
            ),
            net_class="power",
            power_domain="v3v3",
            interface=None,
            intent="Externally supplied regulated 3.3 V rail.",
        ),
        Net(
            id="net_gnd",
            name="GND",
            endpoints=_merge_ports(
                ports,
                ("qwiic_power_input", "gnd"),
                ("attiny402_core", "gnd"),
                ("tmp102_i2c_sensor", "gnd"),
                ("gpio_status_led", "gnd"),
            ),
            net_class="power",
            power_domain="v3v3",
            interface=None,
            intent="Common low-voltage return plane.",
        ),
        Net(
            id="net_i2c_sda",
            name="I2C_SDA",
            endpoints=_merge_ports(
                ports,
                ("qwiic_power_input", "sda"),
                ("attiny402_core", "i2c_sda"),
                ("tmp102_i2c_sensor", "sda"),
            ),
            net_class="i2c",
            power_domain="v3v3",
            interface="sensor_i2c",
            intent="3.3 V open-drain I2C data line with one 4.7 kOhm pull-up.",
        ),
        Net(
            id="net_i2c_scl",
            name="I2C_SCL",
            endpoints=_merge_ports(
                ports,
                ("qwiic_power_input", "scl"),
                ("attiny402_core", "i2c_scl"),
                ("tmp102_i2c_sensor", "scl"),
            ),
            net_class="i2c",
            power_domain="v3v3",
            interface="sensor_i2c",
            intent="3.3 V open-drain I2C clock line with one 4.7 kOhm pull-up.",
        ),
        Net(
            id="net_updi",
            name="UPDI",
            endpoints=ports["attiny402_core"]["updi"],
            net_class="signal",
            power_domain="v3v3",
            interface=None,
            intent="Accessible single-wire programming/debug connection.",
        ),
        Net(
            id="net_led_ctrl",
            name="LED_CTRL",
            endpoints=_merge_ports(
                ports,
                ("attiny402_core", "status_gpio"),
                ("gpio_status_led", "gpio"),
            ),
            net_class="signal",
            power_domain="v3v3",
            interface=None,
            intent="MCU-driven status indicator control.",
        ),
        Net(
            id="net_led_anode",
            name="LED_ANODE",
            endpoints=ports["gpio_status_led"]["anode"],
            net_class="signal",
            power_domain="v3v3",
            interface=None,
            intent="Current-limited LED anode connection.",
        ),
    )
    power_domain = PowerDomain(
        id="v3v3",
        nominal_v=power["nominal_v"],
        min_v=power["min_v"],
        max_v=power["max_v"],
        max_current_a=power["max_current_a"],
        source=Endpoint("flag_3v3", "1", "source"),
        intent="Externally regulated Qwiic-compatible low-voltage supply.",
    )
    interface = Interface(
        id="sensor_i2c",
        kind="i2c",
        power_domain="v3v3",
        members=tuple(
            endpoint
            for net in nets
            if net.interface == "sensor_i2c"
            for endpoint in net.endpoints
        ),
        controller=Endpoint("mcu_u1", "4", "controller"),
        params={
            "speed_hz": i2c["speed_hz"],
            "voltage_v": power["nominal_v"],
            "pullup_ohm": 4700,
            "topology": "open_drain",
            "bus_capacitance_pf_max": i2c["bus_capacitance_pf_max"],
            "external_pullups": i2c["external_pullups"],
            "rise_time_limit_ns": i2c["rise_time_limit_ns"],
        },
        intent="Board-local and connector-exposed environmental sensor bus.",
    )
    constraints = _constraints(spec, power, i2c)
    design = Design(
        design_id=spec.design_id,
        name=spec.name,
        revision=spec.revision,
        scope=spec.scope,
        requirements=tuple(sorted(requirements, key=lambda entry: entry.id)),
        provenance=provenance,
        blocks=blocks,
        power_domains=(power_domain,),
        interfaces=(interface,),
        components=components,
        nets=nets,
        constraints=constraints,
        board=spec.board,
        analyses=(
            {
                "id": "power_budget",
                "kind": "power_budget",
                "required": False,
                "reason": "Firmware-dependent maximum load remains part of qualified L6 review.",
            },
            {
                "id": "led_current",
                "kind": "ohms_law",
                "required": True,
                "supply_v": power["nominal_v"],
                "forward_v": 2.1,
                "resistance_ohm": 1000,
            },
            {
                "id": "digital_function",
                "kind": "functional_simulation",
                "required": False,
                "reason": "firmware behavior is outside PCB netlist evidence",
            },
        ),
        metadata={
            "compiler": "pcbdraft",
            "profile": GENERATION_PROFILE_ID,
            "priorities": list(spec.priorities),
            "requirements_hash": _sha256(spec.canonical_bytes()),
            "source": spec.source,
        },
    )
    # Reparse the compiler output through the public strict schema; direct dataclass
    # construction must not become a path around structural validation.
    design = Design.from_dict(design.to_dict())
    assert_supported(design)
    resolved_graph.assert_design(design, check_libraries=check_libraries)
    return design


def _fixture_requirements(spec: RequirementsSpec) -> tuple[Requirement, ...]:
    functional = tuple(
        Requirement(
            id=f"req_{function['id']}",
            text=function["intent"],
            acceptance=_function_acceptance(
                function["kind"], function.get("parameters", {})
            ),
            risk="low",
            provenance=("user_requirements",),
        )
        for function in spec.functions
    )
    return functional + (
        Requirement(
            id="req_manufacturing",
            text="Produce a fabricable, assembly-ready two-to-four-layer design candidate.",
            acceptance=(
                "ERC and DRC contain no errors.",
                "BOM records resolve to active canonical parts with source evidence.",
                "Gerber, drill, placement, and manifest artifacts are reproducible.",
            ),
            risk="medium",
            provenance=("user_requirements",),
        ),
    )


def _finalize_fixture_design(
    design: Design, graph: PartGraph, *, check_libraries: bool
) -> Design:
    normalized = Design.from_dict(design.to_dict())
    assert_supported(normalized)
    graph.assert_design(normalized, check_libraries=check_libraries)
    return normalized


def _manufacturing_constraint(spec: RequirementsSpec) -> Constraint:
    return Constraint(
        "manufacturing",
        "manufacturing_rules",
        ("board",),
        {
            "min_track_mm": spec.board.min_track_mm,
            "min_clearance_mm": spec.board.min_clearance_mm,
            "min_drill_mm": spec.board.min_drill_mm,
            "min_hole_clearance_mm": max(spec.board.min_clearance_mm, 0.15),
            "min_hole_to_hole_mm": max(spec.board.min_clearance_mm, 0.2),
            "edge_clearance_mm": spec.board.edge_clearance_mm,
            "assembly_side": "front",
            "process_profile": "generic_standard_low_voltage_2_4_layer_v1",
            "fabricator": "not_selected",
            "capability_verification": "external_l4_required",
        },
        "release_blocking",
        "Fabrication limits are explicit design inputs.",
        ("user_requirements",),
    )


def _spi_contract(values: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    matches = [entry for entry in values if entry.get("kind") == "spi"]
    if len(matches) != 1:
        raise ValidationError("requirements must define exactly one SPI interface")
    value = matches[0]
    allowed = {"id", "kind", "clock_hz", "mode", "external_connector"}
    if set(value) != allowed:
        raise ValidationError(
            "SPI interface fields do not match the supported contract"
        )
    if value.get("id") != "sensor_spi" or value.get("external_connector") is not False:
        raise ValidationError(
            "the verified SPI profile requires a board-local sensor_spi interface"
        )
    clock = value.get("clock_hz")
    if (
        isinstance(clock, bool)
        or not isinstance(clock, int)
        or not 10_000 <= clock <= 1_000_000
    ):
        raise ValidationError("SPI clock_hz must be an integer from 10 kHz to 1 MHz")
    if value.get("mode") != 0:
        raise ValidationError("the verified BME280 profile requires SPI mode 0")
    return dict(value)


def _compile_spi_profile(
    spec: RequirementsSpec,
    functions: dict[str, dict[str, Any]],
    graph: PartGraph,
    registry: BlockRegistry,
    check_libraries: bool,
) -> Design:
    del functions
    power = _power_contract(spec.power)
    _validate_power_scope(spec, power)
    spi = _spi_contract(spec.interfaces)
    instances = [
        registry.instantiate("spi_power_input"),
        registry.instantiate("attiny402_core"),
        registry.instantiate("bme280_spi_sensor"),
    ]
    components = tuple(
        component for instance in instances for component in instance.components
    )
    ports = {instance.block.id: instance.ports for instance in instances}
    nets = (
        Net(
            "net_3v3",
            "3V3",
            _merge_ports(
                ports,
                ("spi_power_input", "vcc"),
                ("attiny402_core", "vcc"),
                ("bme280_spi_sensor", "vcc"),
            ),
            "power",
            "v3v3",
            None,
            "Externally supplied regulated 3.3 V rail.",
        ),
        Net(
            "net_gnd",
            "GND",
            _merge_ports(
                ports,
                ("spi_power_input", "gnd"),
                ("attiny402_core", "gnd"),
                ("bme280_spi_sensor", "gnd"),
            ),
            "power",
            "v3v3",
            None,
            "Common low-voltage return plane.",
        ),
        Net(
            "net_spi_mosi",
            "SPI_MOSI",
            _merge_ports(
                ports,
                ("attiny402_core", "spi_mosi"),
                ("bme280_spi_sensor", "mosi"),
            ),
            "spi",
            "v3v3",
            "sensor_spi",
            "ATtiny402 host-to-BME280 SPI data.",
        ),
        Net(
            "net_spi_miso",
            "SPI_MISO",
            _merge_ports(
                ports,
                ("attiny402_core", "spi_miso"),
                ("bme280_spi_sensor", "miso"),
            ),
            "spi",
            "v3v3",
            "sensor_spi",
            "BME280-to-ATtiny402 SPI data.",
        ),
        Net(
            "net_spi_sck",
            "SPI_SCK",
            _merge_ports(
                ports,
                ("attiny402_core", "spi_sck"),
                ("bme280_spi_sensor", "sck"),
            ),
            "spi",
            "v3v3",
            "sensor_spi",
            "Mode-0 SPI clock bounded to 1 MHz.",
        ),
        Net(
            "net_spi_cs",
            "SPI_CS",
            _merge_ports(
                ports,
                ("attiny402_core", "spi_cs"),
                ("bme280_spi_sensor", "cs"),
            ),
            "spi",
            "v3v3",
            "sensor_spi",
            "Active-low BME280 chip select with 10 kOhm inactive bias.",
        ),
        Net(
            "net_updi",
            "UPDI",
            ports["attiny402_core"]["updi"],
            "signal",
            "v3v3",
            None,
            "Accessible single-wire programming/debug connection.",
        ),
    )
    domain = PowerDomain(
        "v3v3",
        power["nominal_v"],
        power["min_v"],
        power["max_v"],
        power["max_current_a"],
        Endpoint("flag_3v3", "1", "source"),
        "Externally regulated low-voltage supply.",
    )
    interface = Interface(
        "sensor_spi",
        "spi",
        "v3v3",
        tuple(
            endpoint
            for net in nets
            if net.interface == "sensor_spi"
            for endpoint in net.endpoints
        ),
        Endpoint("mcu_u1", "4", "controller"),
        {
            "clock_hz": spi["clock_hz"],
            "mode": spi["mode"],
            "voltage_v": power["nominal_v"],
            "topology": "four_wire_single_peripheral",
            "external_connector": False,
        },
        "Board-local BME280 SPI interface with a separately bounded power header.",
    )
    constraints = (
        Constraint(
            "decouple_mcu",
            "decoupling",
            ("mcu_u1", "mcu_c1", "net_3v3", "net_gnd"),
            {
                "max_distance_mm": 3.0,
                "distance_metric": "minimum_relevant_copper_pad_edge_gap",
                "geometry_evidence": "native_footprint_pad_rectangles",
                "min_capacitance_f": 1e-7,
            },
            "release_blocking",
            "MCU supply transient return must be local.",
            ("block_attiny402_core",),
        ),
        Constraint(
            "decouple_sensor",
            "decoupling",
            ("sensor_u2", "sensor_c2", "net_3v3", "net_gnd"),
            {
                "max_distance_mm": 4.25,
                "distance_metric": "minimum_relevant_copper_pad_edge_gap",
                "geometry_evidence": "native_footprint_pad_rectangles",
                "min_capacitance_f": 1e-7,
            },
            "release_blocking",
            "BME280 supply decoupling must be local.",
            ("block_bme280_spi_sensor",),
        ),
        Constraint(
            "spi_electrical_budget",
            "spi_electrical_budget",
            (
                "sensor_spi",
                "sensor_u2",
                "cs_r1",
                "net_spi_mosi",
                "net_spi_miso",
                "net_spi_sck",
                "net_spi_cs",
            ),
            {
                "clock_hz": spi["clock_hz"],
                "sensor_clock_limit_hz": 10_000_000,
                "mode": 0,
                "voltage_v": power["nominal_v"],
                "cs_pullup_ohm": 10_000,
                "topology": "four_wire_single_peripheral",
            },
            "release_blocking",
            "Bound SPI mode, clock, voltage, topology, and inactive chip select.",
            ("block_bme280_spi_sensor",),
        ),
        Constraint(
            "sensor_group",
            "functional_group",
            ("sensor_u2", "sensor_c2", "cs_r1", "spi_j1"),
            {"max_diameter_mm": 20.0, "objective": "short_spi_and_decoupling"},
            "required",
            "Keep the sensor interface compact.",
            ("block_bme280_spi_sensor",),
        ),
        Constraint(
            "spi_edge",
            "edge_placement",
            ("spi_j1", "board"),
            {"edge": "right", "max_edge_distance_mm": 4.0},
            "release_blocking",
            "SPI connector must remain mechanically accessible.",
            ("block_spi_power_input",),
        ),
        Constraint(
            "updi_edge",
            "edge_placement",
            ("updi_j2", "board"),
            {"edge": "left", "max_edge_distance_mm": 4.0},
            "required",
            "Programming header must remain accessible.",
            ("block_attiny402_core",),
        ),
        Constraint(
            "updi_power_policy",
            "source_ownership",
            ("updi_j2", "spi_j1", "net_3v3"),
            {
                "physical_source_component": "spi_j1",
                "sense_component": "updi_j2",
                "sense_pin": "2",
                "sense_role": "voltage_sense",
                "simultaneous_external_power_sources": "forbidden",
            },
            "release_blocking",
            "UPDI VTREF remains sense-only while the SPI header owns the rail.",
            ("block_attiny402_core",),
        ),
        Constraint(
            "power_budget",
            "power_budget",
            ("v3v3",),
            {
                "max_current_a": power["max_current_a"],
                "max_power_w": spec.scope.max_power_w,
                "voltage_basis_v": spec.scope.max_voltage_v,
                "envelope": "simultaneous_declared_scope_maxima",
            },
            "release_blocking",
            "Keep all loads inside the external supply budget.",
            ("user_requirements",),
        ),
        Constraint(
            "routing_spi",
            "routing",
            ("net_spi_mosi", "net_spi_miso", "net_spi_sck", "net_spi_cs"),
            {
                "width_mm": 0.25,
                "max_length_mm": 100,
                "continuous_reference_net": "net_gnd",
                "min_reference_stitching_vias": 2,
                "auto_route": True,
                "neckdown_width_mm": spec.board.min_track_mm,
                "neckdown_max_length_mm_per_pad": 2.25,
            },
            "release_blocking",
            "Bounded low-speed SPI routing contract.",
            ("block_bme280_spi_sensor",),
        ),
        Constraint(
            "routing_power",
            "routing",
            ("net_3v3", "net_gnd"),
            {
                "width_mm": 0.25,
                "auto_route": True,
                "neckdown_width_mm": spec.board.min_track_mm,
                "neckdown_max_length_mm_per_pad": 3.0,
            },
            "release_blocking",
            "The bounded 100 mA rail uses a manufacturable width.",
            ("user_requirements",),
        ),
        _manufacturing_constraint(spec),
    )
    design = Design(
        design_id=spec.design_id,
        name=spec.name,
        revision=spec.revision,
        scope=spec.scope,
        requirements=tuple(
            sorted(_fixture_requirements(spec), key=lambda item: item.id)
        ),
        provenance=_provenance(spec, registry),
        blocks=tuple(instance.block for instance in instances),
        power_domains=(domain,),
        interfaces=(interface,),
        components=components,
        nets=nets,
        constraints=tuple(sorted(constraints, key=lambda item: item.id)),
        board=spec.board,
        analyses=(
            {
                "id": "power_budget",
                "kind": "power_budget",
                "required": False,
                "reason": "Firmware-dependent maximum load remains part of qualified L6 review.",
            },
            {
                "id": "digital_function",
                "kind": "functional_simulation",
                "required": False,
                "reason": "firmware behavior is outside PCB netlist evidence",
            },
        ),
        metadata={
            "compiler": "pcbdraft",
            "profile": SPI_PROFILE_ID,
            "priorities": list(spec.priorities),
            "requirements_hash": _sha256(spec.canonical_bytes()),
            "source": spec.source,
        },
    )
    return _finalize_fixture_design(design, graph, check_libraries=check_libraries)


def _uart_contract(values: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    matches = [entry for entry in values if entry.get("kind") == "uart"]
    if len(matches) != 1:
        raise ValidationError("requirements must define exactly one UART interface")
    value = matches[0]
    allowed = {
        "id",
        "kind",
        "baud",
        "data_bits",
        "parity",
        "stop_bits",
        "external_connector",
    }
    if set(value) != allowed:
        raise ValidationError(
            "UART interface fields do not match the supported contract"
        )
    if value.get("id") != "service_uart" or value.get("external_connector") is not True:
        raise ValidationError(
            "the verified UART profile requires service_uart with an external connector"
        )
    if value.get("baud") not in {9600, 19200, 38400, 57600, 115200}:
        raise ValidationError("UART baud is outside the verified set")
    if (value.get("data_bits"), value.get("parity"), value.get("stop_bits")) != (
        8,
        "none",
        1,
    ):
        raise ValidationError("the verified UART profile requires 8-N-1 framing")
    return dict(value)


def _compile_uart_ldo_profile(
    spec: RequirementsSpec,
    functions: dict[str, dict[str, Any]],
    graph: PartGraph,
    registry: BlockRegistry,
    check_libraries: bool,
) -> Design:
    del functions
    input_power = _power_contract(spec.power, max_supported_v=6.0)
    _validate_power_scope(spec, input_power)
    if not (
        math.isclose(input_power["nominal_v"], 5.0, abs_tol=1e-12)
        and input_power["min_v"] >= 4.75
        and input_power["max_v"] <= 5.25
        and input_power["max_current_a"] <= 0.1
    ):
        raise ValidationError(
            "the AP2112 profile requires regulated 5 V input, 4.75-5.25 V, at no more than 100 mA"
        )
    uart = _uart_contract(spec.interfaces)
    instances = [
        registry.instantiate("regulated_5v_input"),
        registry.instantiate("ap2112_3v3_ldo"),
        registry.instantiate("attiny402_core"),
        registry.instantiate("uart_service_connector"),
        registry.instantiate("gpio_status_led"),
    ]
    components = tuple(
        component for instance in instances for component in instance.components
    )
    ports = {instance.block.id: instance.ports for instance in instances}
    nets = (
        Net(
            "net_vin",
            "VIN_5V",
            _merge_ports(
                ports,
                ("regulated_5v_input", "vin"),
                ("ap2112_3v3_ldo", "vin"),
            ),
            "power",
            "vin5",
            None,
            "Externally supplied regulated 5 V input to the AP2112K.",
        ),
        Net(
            "net_3v3",
            "3V3",
            _merge_ports(
                ports,
                ("ap2112_3v3_ldo", "vout"),
                ("attiny402_core", "vcc"),
                ("uart_service_connector", "vcc"),
            ),
            "power",
            "v3v3",
            None,
            "AP2112K-regulated 3.3 V rail.",
        ),
        Net(
            "net_gnd",
            "GND",
            _merge_ports(
                ports,
                ("regulated_5v_input", "gnd"),
                ("ap2112_3v3_ldo", "gnd"),
                ("attiny402_core", "gnd"),
                ("uart_service_connector", "gnd"),
                ("gpio_status_led", "gnd"),
            ),
            "power",
            None,
            None,
            "Common input and regulated-rail return plane.",
        ),
        Net(
            "net_uart_tx",
            "UART_TX",
            _merge_ports(
                ports,
                ("attiny402_core", "uart_tx"),
                ("uart_service_connector", "tx"),
            ),
            "uart",
            "v3v3",
            "service_uart",
            "3.3 V ATtiny402 transmit output to the service header.",
        ),
        Net(
            "net_uart_rx",
            "UART_RX",
            _merge_ports(
                ports,
                ("attiny402_core", "uart_rx"),
                ("uart_service_connector", "rx"),
            ),
            "uart",
            "v3v3",
            "service_uart",
            "3.3 V service-header receive input to the ATtiny402.",
        ),
        Net(
            "net_updi",
            "UPDI",
            ports["attiny402_core"]["updi"],
            "signal",
            "v3v3",
            None,
            "Accessible single-wire programming/debug connection.",
        ),
        Net(
            "net_led_ctrl",
            "LED_CTRL",
            _merge_ports(
                ports,
                ("attiny402_core", "status_gpio"),
                ("gpio_status_led", "gpio"),
            ),
            "signal",
            "v3v3",
            None,
            "MCU-driven status indicator control.",
        ),
        Net(
            "net_led_anode",
            "LED_ANODE",
            ports["gpio_status_led"]["anode"],
            "signal",
            "v3v3",
            None,
            "Current-limited LED anode connection.",
        ),
    )
    domains = (
        PowerDomain(
            "vin5",
            input_power["nominal_v"],
            input_power["min_v"],
            input_power["max_v"],
            input_power["max_current_a"],
            Endpoint("flag_5v", "1", "source"),
            "Externally regulated 5 V input envelope.",
        ),
        PowerDomain(
            "v3v3",
            3.3,
            3.2,
            3.4,
            input_power["max_current_a"],
            Endpoint("ldo_u2", "5", "source"),
            "AP2112K fixed 3.3 V regulated output envelope.",
        ),
    )
    interface = Interface(
        "service_uart",
        "uart",
        "v3v3",
        tuple(
            endpoint
            for net in nets
            if net.interface == "service_uart"
            for endpoint in net.endpoints
        ),
        Endpoint("mcu_u1", "4", "controller"),
        {
            "baud": uart["baud"],
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
            "voltage_v": 3.3,
            "logic": "single_ended_cmos_not_rs232",
            "external_connector": True,
        },
        "Board-level 3.3 V CMOS UART service connection; not RS-232 tolerant.",
    )
    constraints = (
        Constraint(
            "decouple_mcu",
            "decoupling",
            ("mcu_u1", "mcu_c1", "net_3v3", "net_gnd"),
            {
                "max_distance_mm": 3.0,
                "distance_metric": "minimum_relevant_copper_pad_edge_gap",
                "geometry_evidence": "native_footprint_pad_rectangles",
                "min_capacitance_f": 1e-7,
            },
            "release_blocking",
            "MCU supply transient return must be local.",
            ("block_attiny402_core",),
        ),
        Constraint(
            "decouple_ldo_input",
            "decoupling",
            ("ldo_u2", "ldo_c2", "net_vin", "net_gnd"),
            {
                "max_distance_mm": 4.0,
                "distance_metric": "minimum_relevant_copper_pad_edge_gap",
                "geometry_evidence": "native_footprint_pad_rectangles",
                "min_capacitance_f": 1e-6,
            },
            "release_blocking",
            "AP2112K input bypass capacitor must be local.",
            ("block_ap2112_3v3_ldo",),
        ),
        Constraint(
            "decouple_ldo_output",
            "decoupling",
            ("ldo_u2", "ldo_c3", "net_3v3", "net_gnd"),
            {
                "max_distance_mm": 4.0,
                "distance_metric": "minimum_relevant_copper_pad_edge_gap",
                "geometry_evidence": "native_footprint_pad_rectangles",
                "min_capacitance_f": 1e-6,
            },
            "release_blocking",
            "AP2112K output stability capacitor must be local.",
            ("block_ap2112_3v3_ldo",),
        ),
        Constraint(
            "ldo_regulation_budget",
            "ldo_regulation_budget",
            ("ldo_u2", "ldo_c2", "ldo_c3", "vin5", "v3v3"),
            {
                "input_min_v": input_power["min_v"],
                "input_max_v": input_power["max_v"],
                "output_v": 3.3,
                "load_limit_a": input_power["max_current_a"],
                "part_current_limit_a": 0.6,
                "input_capacitance_f": 1e-6,
                "output_capacitance_f": 1e-6,
                "enable_policy": "tied_to_vin",
            },
            "release_blocking",
            "Bound AP2112K input, output, load, bypass, stability, and enable contracts.",
            ("block_ap2112_3v3_ldo",),
        ),
        Constraint(
            "uart_electrical_budget",
            "uart_electrical_budget",
            ("service_uart", "net_uart_tx", "net_uart_rx", "uart_j3"),
            {
                "baud": uart["baud"],
                "data_bits": 8,
                "parity": "none",
                "stop_bits": 1,
                "voltage_v": 3.3,
                "logic": "single_ended_cmos_not_rs232",
            },
            "release_blocking",
            "Bound UART framing, voltage, and connector-level logic contract.",
            ("block_uart_service_connector",),
        ),
        Constraint(
            "ldo_group",
            "functional_group",
            ("ldo_u2", "ldo_c2", "ldo_c3"),
            {"max_diameter_mm": 10.0, "objective": "short_regulator_loops"},
            "required",
            "Keep input and output capacitor loops compact.",
            ("block_ap2112_3v3_ldo",),
        ),
        Constraint(
            "uart_edge",
            "edge_placement",
            ("uart_j3", "board"),
            {"edge": "right", "max_edge_distance_mm": 4.0},
            "release_blocking",
            "UART service connector must remain accessible.",
            ("block_uart_service_connector",),
        ),
        Constraint(
            "power_input_edge",
            "edge_placement",
            ("power_j1", "board"),
            {"edge": "bottom", "max_edge_distance_mm": 4.0},
            "release_blocking",
            "Power input must remain accessible at the board edge.",
            ("block_regulated_5v_input",),
        ),
        Constraint(
            "updi_edge",
            "edge_placement",
            ("updi_j2", "board"),
            {"edge": "left", "max_edge_distance_mm": 4.0},
            "required",
            "Programming header must remain accessible.",
            ("block_attiny402_core",),
        ),
        Constraint(
            "updi_power_policy",
            "source_ownership",
            ("updi_j2", "ldo_u2", "net_3v3"),
            {
                "physical_source_component": "ldo_u2",
                "sense_component": "updi_j2",
                "sense_pin": "2",
                "sense_role": "voltage_sense",
                "simultaneous_external_power_sources": "forbidden",
            },
            "release_blocking",
            "UPDI VTREF is sense-only and the LDO owns the 3.3 V rail.",
            ("block_attiny402_core",),
        ),
        Constraint(
            "led_current",
            "current_limit",
            ("led_r3", "led_d1", "net_led_ctrl"),
            {
                "supply_v": 3.3,
                "forward_v": 2.1,
                "resistance_ohm": 1000,
                "max_current_a": 0.005,
            },
            "release_blocking",
            "Bound indicator current and MCU pin load.",
            ("block_gpio_status_led",),
        ),
        Constraint(
            "power_budget",
            "power_budget",
            ("vin5", "v3v3"),
            {
                "max_current_a": input_power["max_current_a"],
                "max_power_w": spec.scope.max_power_w,
                "voltage_basis_v": spec.scope.max_voltage_v,
                "envelope": "simultaneous_declared_scope_maxima",
            },
            "release_blocking",
            "Keep regulator input and output loads inside the declared envelope.",
            ("user_requirements",),
        ),
        Constraint(
            "routing_uart",
            "routing",
            ("net_uart_tx", "net_uart_rx"),
            {
                "width_mm": 0.25,
                "max_length_mm": 100,
                "continuous_reference_net": "net_gnd",
                "min_reference_stitching_vias": 2,
                "auto_route": True,
                "neckdown_width_mm": spec.board.min_track_mm,
                "neckdown_max_length_mm_per_pad": 2.25,
            },
            "release_blocking",
            "Bounded low-speed UART routing contract.",
            ("block_uart_service_connector",),
        ),
        Constraint(
            "routing_power",
            "routing",
            ("net_vin", "net_3v3", "net_gnd"),
            {
                "width_mm": 0.3,
                "auto_route": True,
                "neckdown_width_mm": spec.board.min_track_mm,
                "neckdown_max_length_mm_per_pad": 2.25,
            },
            "release_blocking",
            "The bounded 100 mA rails use a manufacturable width.",
            ("user_requirements",),
        ),
        _manufacturing_constraint(spec),
    )
    design = Design(
        design_id=spec.design_id,
        name=spec.name,
        revision=spec.revision,
        scope=spec.scope,
        requirements=tuple(
            sorted(_fixture_requirements(spec), key=lambda item: item.id)
        ),
        provenance=_provenance(spec, registry),
        blocks=tuple(instance.block for instance in instances),
        power_domains=domains,
        interfaces=(interface,),
        components=components,
        nets=nets,
        constraints=tuple(sorted(constraints, key=lambda item: item.id)),
        board=spec.board,
        analyses=(
            {
                "id": "power_budget",
                "kind": "power_budget",
                "required": False,
                "reason": "Firmware-dependent maximum load remains part of qualified L6 review.",
            },
            {
                "id": "led_current",
                "kind": "ohms_law",
                "required": True,
                "supply_v": 3.3,
                "forward_v": 2.1,
                "resistance_ohm": 1000,
            },
            {
                "id": "digital_function",
                "kind": "functional_simulation",
                "required": False,
                "reason": "firmware behavior is outside PCB netlist evidence",
            },
        ),
        metadata={
            "compiler": "pcbdraft",
            "profile": UART_LDO_PROFILE_ID,
            "priorities": list(spec.priorities),
            "requirements_hash": _sha256(spec.canonical_bytes()),
            "source": spec.source,
        },
    )
    return _finalize_fixture_design(design, graph, check_libraries=check_libraries)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _power_contract(
    value: Mapping[str, Any], *, max_supported_v: float = 3.6
) -> dict[str, float]:
    required = {"nominal_v", "min_v", "max_v", "max_current_a"}
    if set(value) != required:
        raise ValidationError(
            "$.power must contain nominal_v, min_v, max_v, and max_current_a"
        )
    result: dict[str, float] = {}
    for name in required:
        number = value[name]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or number <= 0
        ):
            raise ValidationError(f"$.power.{name} must be a positive number")
        result[name] = float(number)
    if not result["min_v"] <= result["nominal_v"] <= result["max_v"]:
        raise ValidationError("$.power nominal voltage is outside its range")
    if result["max_v"] > max_supported_v:
        raise ValidationError(
            f"the verified profile requires a power maximum <= {max_supported_v:g} V"
        )
    return result


def _validate_power_scope(spec: RequirementsSpec, power: Mapping[str, float]) -> None:
    """Require one unambiguous rectangular supply/current/power envelope."""
    expected_power_w = spec.scope.max_voltage_v * spec.scope.max_current_a
    mismatches: list[str] = []
    if not math.isclose(power["max_v"], spec.scope.max_voltage_v, abs_tol=1e-12):
        mismatches.append("maximum voltage")
    if not math.isclose(
        power["max_current_a"], spec.scope.max_current_a, abs_tol=1e-12
    ):
        mismatches.append("maximum current")
    if not math.isclose(spec.scope.max_power_w, expected_power_w, abs_tol=1e-12):
        mismatches.append("maximum power")
    if mismatches:
        raise ValidationError(
            "the bundled profile requires power and scope to describe the same "
            "simultaneous maximum envelope; mismatched " + ", ".join(mismatches)
        )


def _i2c_contract(values: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    matches = [entry for entry in values if entry.get("kind") == "i2c"]
    if len(matches) != 1:
        raise ValidationError("requirements must define exactly one I2C interface")
    value = matches[0]
    allowed = {
        "id",
        "kind",
        "speed_hz",
        "external_connector",
        "bus_capacitance_pf_max",
        "external_pullups",
    }
    if set(value) != allowed:
        raise ValidationError(
            "I2C interface fields do not match the supported contract"
        )
    if value.get("id") != "sensor_i2c" or value.get("external_connector") is not True:
        raise ValidationError(
            "the built-in profile requires sensor_i2c with an external connector"
        )
    speed = value.get("speed_hz")
    if (
        isinstance(speed, bool)
        or not isinstance(speed, int)
        or not 10_000 <= speed <= 400_000
    ):
        raise ValidationError("I2C speed_hz must be an integer from 10 kHz to 400 kHz")
    capacitance = value.get("bus_capacitance_pf_max")
    if (
        isinstance(capacitance, bool)
        or not isinstance(capacitance, (int, float))
        or not math.isfinite(float(capacitance))
        or not 1 <= float(capacitance) <= 400
    ):
        raise ValidationError(
            "I2C bus_capacitance_pf_max must be a finite number from 1 to 400 pF"
        )
    if value.get("external_pullups") != "forbidden":
        raise ValidationError(
            "the bundled profile requires external_pullups=forbidden so the total pull-up is bounded"
        )
    rise_time_limit_ns = 1000.0 if speed <= 100_000 else 300.0
    rise_time_ns = 0.8473 * 4700 * float(capacitance) * 1e-3
    if rise_time_ns > rise_time_limit_ns + 1e-9:
        raise ValidationError(
            "the declared I2C capacitance and 4.7 kOhm pull-ups exceed the rise-time budget"
        )
    return {
        **dict(value),
        "bus_capacitance_pf_max": float(capacitance),
        "rise_time_limit_ns": rise_time_limit_ns,
        "calculated_rise_time_ns": rise_time_ns,
    }


def _select_profile(functions: set[str]) -> str:
    for profile_id, required in PROFILE_FUNCTION_SETS.items():
        if functions == required:
            return profile_id
    if functions < I2C_PROFILE_FUNCTIONS:
        missing = I2C_PROFILE_FUNCTIONS - functions
        raise ValidationError(
            "the built-in low-voltage controller profile requires functions: "
            + ", ".join(sorted(missing))
        )
    raise ValidationError(
        "requirements do not match any complete verified generation profile; "
        "supported profile function sets are: "
        + "; ".join(
            f"{profile_id}=[{', '.join(sorted(required))}]"
            for profile_id, required in PROFILE_FUNCTION_SETS.items()
        )
    )


def _validate_profile_functions(
    profile_id: str, functions: dict[str, dict[str, Any]]
) -> None:
    """Reject profile parameters that would otherwise be silently ignored."""
    for kind, expected in PROFILE_FUNCTION_PARAMETERS[profile_id].items():
        actual = functions[kind]["parameters"]
        if actual != expected:
            raise ValidationError(
                f"{kind} parameters must exactly match the supported profile contract"
            )


def _provenance(
    spec: RequirementsSpec, registry: BlockRegistry
) -> tuple[Provenance, ...]:
    source_locator = spec.source.get("locator", "requirements input")
    source_name = spec.source.get("name", "user requirements")
    entries = [
        Provenance(
            id="user_requirements",
            kind="requirement",
            source=str(source_name),
            locator=str(source_locator),
            acquired_at=spec.source.get("acquired_at"),
            method="user_supplied",
            confidence=1.0,
            notes="The runtime preserves this as user intent; it is not independently certified.",
        )
    ]
    for definition in registry.definitions():
        entries.append(
            Provenance(
                id=f"block_{definition.id}",
                kind="verified_block",
                source="PCBDraft bundled block catalog",
                locator=f"data/blocks/catalog.json#{definition.id}@{definition.version}",
                acquired_at="2026-08-12",
                method="rule_validated",
                confidence=0.95,
                notes=f"State={definition.verification_state}; not human or production validated.",
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.id))


def _merge_ports(
    ports: dict[str, dict[str, tuple[Endpoint, ...]]],
    *selectors: tuple[str, str],
) -> tuple[Endpoint, ...]:
    return tuple(
        sorted(
            endpoint
            for block_id, port in selectors
            for endpoint in ports[block_id][port]
        )
    )


def _function_acceptance(
    kind: str, parameters: Mapping[str, Any] | None = None
) -> tuple[str, ...]:
    del parameters
    return {
        "microcontroller": (
            "MCU power, ground, I2C, UPDI, and status GPIO contracts are connected.",
        ),
        "temperature_sensor": (
            "TMP102B supply, address strap, I2C, pull-up, and decoupling rules pass.",
        ),
        "environmental_sensor": (
            "BME280 supply, four-wire SPI, chip-select bias, and decoupling rules pass.",
        ),
        "status_indicator": (
            "LED polarity and calculated current remain within recorded ratings.",
        ),
        "i2c_connector": (
            "External connector pin order is GND/3V3/SDA/SCL and edge placement is enforced.",
        ),
        "spi_connector": (
            "External connector pin order is GND/3V3/MOSI/MISO/SCK/CS and edge placement is enforced.",
        ),
        "uart_connector": (
            "The edge-accessible service connector exposes GND/3V3/TX/RX at 3.3 V CMOS levels.",
        ),
        "updi_programming": (
            "UPDI, target-voltage sense, and return are exposed on an accessible header without a second supply source.",
        ),
        "ldo_regulator": (
            "AP2112K input/output range, load, enable, and stability-capacitor contracts pass.",
        ),
        "power_input": (
            "A polarity-defined regulated low-voltage input is edge-accessible and stays within the selected profile envelope.",
        ),
    }[kind]


def _constraints(
    spec: RequirementsSpec, power: dict[str, float], i2c: dict[str, Any]
) -> tuple[Constraint, ...]:
    source = ("user_requirements",)
    block_source = {
        "core": ("block_attiny402_core",),
        "sensor": ("block_tmp102_i2c_sensor",),
        "led": ("block_gpio_status_led",),
        "input": ("block_qwiic_power_input",),
    }
    constraints = (
        Constraint(
            "decouple_mcu",
            "decoupling",
            ("mcu_u1", "mcu_c1", "net_3v3", "net_gnd"),
            {
                "max_distance_mm": 3.0,
                "distance_metric": "minimum_relevant_copper_pad_edge_gap",
                "geometry_evidence": "native_footprint_pad_rectangles",
                "min_capacitance_f": 1e-7,
            },
            "release_blocking",
            "MCU supply transient return must be local.",
            block_source["core"],
        ),
        Constraint(
            "decouple_sensor",
            "decoupling",
            ("sensor_u2", "sensor_c2", "net_3v3", "net_gnd"),
            {
                "max_distance_mm": 2.0,
                "distance_metric": "minimum_relevant_copper_pad_edge_gap",
                "geometry_evidence": "native_footprint_pad_rectangles",
                "min_capacitance_f": 1e-7,
            },
            "release_blocking",
            "Sensor decoupling must be local.",
            block_source["sensor"],
        ),
        Constraint(
            "i2c_pullups",
            "interface_pullups",
            ("sensor_i2c", "pullup_r1", "pullup_r2", "net_i2c_sda", "net_i2c_scl"),
            {"resistance_ohm": 4700, "count": 2, "speed_hz": i2c["speed_hz"]},
            "release_blocking",
            "Open-drain I2C lines require one pull-up each.",
            block_source["sensor"],
        ),
        Constraint(
            "i2c_electrical_budget",
            "i2c_electrical_budget",
            ("sensor_i2c", "net_i2c_sda", "net_i2c_scl", "pullup_r1", "pullup_r2"),
            {
                "speed_hz": i2c["speed_hz"],
                "pullup_ohm": 4700,
                "bus_capacitance_pf_max": i2c["bus_capacitance_pf_max"],
                "rise_time_limit_ns": i2c["rise_time_limit_ns"],
                "calculated_rise_time_ns": i2c["calculated_rise_time_ns"],
                "sink_current_limit_ma": 3.0,
                "calculated_sink_current_ma": power["max_v"] / 4700 * 1000,
                "external_pullups": i2c["external_pullups"],
            },
            "release_blocking",
            "Bound the declared I2C RC rise time, low-level sink current, and external pull-up policy.",
            block_source["sensor"],
        ),
        Constraint(
            "sensor_group",
            "functional_group",
            ("sensor_u2", "sensor_c2", "pullup_r1", "pullup_r2", "qwiic_j1"),
            {"max_diameter_mm": 18.0, "objective": "short_i2c_and_decoupling"},
            "required",
            "Keep the sensor interface compact.",
            block_source["sensor"],
        ),
        Constraint(
            "qwiic_edge",
            "edge_placement",
            ("qwiic_j1", "board"),
            {"edge": "right", "max_edge_distance_mm": 2.0},
            "release_blocking",
            "Cable connector must remain mechanically accessible.",
            block_source["input"],
        ),
        Constraint(
            "updi_edge",
            "edge_placement",
            ("updi_j2", "board"),
            {"edge": "left", "max_edge_distance_mm": 4.0},
            "required",
            "Programming header must remain accessible.",
            block_source["core"],
        ),
        Constraint(
            "updi_power_policy",
            "source_ownership",
            ("updi_j2", "qwiic_j1", "net_3v3"),
            {
                "physical_source_component": "qwiic_j1",
                "sense_component": "updi_j2",
                "sense_pin": "2",
                "sense_role": "voltage_sense",
                "simultaneous_external_power_sources": "forbidden",
            },
            "release_blocking",
            "UPDI pin 2 is target-voltage sense only; the programmer must not source the rail while Qwiic power is present.",
            block_source["core"],
        ),
        Constraint(
            "led_current",
            "current_limit",
            ("led_r3", "led_d1", "net_led_ctrl"),
            {
                "supply_v": power["nominal_v"],
                "forward_v": 2.1,
                "resistance_ohm": 1000,
                "max_current_a": 0.005,
            },
            "release_blocking",
            "Bound indicator current and MCU pin load.",
            block_source["led"],
        ),
        Constraint(
            "power_budget",
            "power_budget",
            ("v3v3",),
            {
                "max_current_a": power["max_current_a"],
                "max_power_w": spec.scope.max_power_w,
                "voltage_basis_v": spec.scope.max_voltage_v,
                "envelope": "simultaneous_declared_scope_maxima",
            },
            "release_blocking",
            "Keep all loads inside the external supply budget.",
            source,
        ),
        Constraint(
            "routing_i2c",
            "routing",
            ("net_i2c_sda", "net_i2c_scl"),
            {
                "width_mm": 0.25,
                "max_length_mm": 100,
                "continuous_reference_net": "net_gnd",
                "min_reference_stitching_vias": 2,
                "auto_route": True,
                "neckdown_width_mm": spec.board.min_track_mm,
                "neckdown_max_length_mm_per_pad": 2.25,
            },
            "release_blocking",
            "Bounded low-speed I2C routing contract.",
            block_source["sensor"],
        ),
        Constraint(
            "routing_power",
            "routing",
            ("net_3v3", "net_gnd"),
            {
                "width_mm": 0.25,
                "auto_route": True,
                "neckdown_width_mm": spec.board.min_track_mm,
                "neckdown_max_length_mm_per_pad": 2.25,
            },
            "release_blocking",
            "The bounded 100 mA rail uses a manufacturable 0.25 mm minimum width.",
            source,
        ),
        Constraint(
            "manufacturing",
            "manufacturing_rules",
            ("board",),
            {
                "min_track_mm": spec.board.min_track_mm,
                "min_clearance_mm": spec.board.min_clearance_mm,
                "min_drill_mm": spec.board.min_drill_mm,
                "min_hole_clearance_mm": max(spec.board.min_clearance_mm, 0.15),
                "min_hole_to_hole_mm": max(spec.board.min_clearance_mm, 0.2),
                "edge_clearance_mm": spec.board.edge_clearance_mm,
                "assembly_side": "front",
                "process_profile": "generic_standard_low_voltage_2_4_layer_v1",
                "fabricator": "not_selected",
                "capability_verification": "external_l4_required",
            },
            "release_blocking",
            "Fabrication limits are explicit design inputs.",
            source,
        ),
    )
    return tuple(sorted(constraints, key=lambda entry: entry.id))


def requirements_json_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pcbdraft.invalid/schema/requirements-v1.json",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "version",
            "design_id",
            "name",
            "revision",
            "scope",
            "functions",
            "power",
            "interfaces",
            "board",
            "priorities",
            "source",
        ],
        "properties": {
            "schema": {"const": REQUIREMENTS_SCHEMA},
            "version": {"const": REQUIREMENTS_VERSION},
            "design_id": {"type": "string"},
            "name": {"type": "string"},
            "revision": {"type": "string"},
            "scope": {"type": "object"},
            "functions": {"type": "array", "items": {"type": "object"}, "minItems": 1},
            "power": {"type": "object"},
            "interfaces": {"type": "array", "items": {"type": "object"}},
            "board": {"type": "object"},
            "priorities": {"type": "array", "items": {"type": "string"}},
            "source": {"type": "object"},
        },
    }
