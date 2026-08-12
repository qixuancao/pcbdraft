"""Deterministic requirements-to-semantic-design compiler for the accepted scope."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .blocks import BlockRegistry
from .errors import ValidationError
from .io import read_bytes_limited
from .ir import (
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
from .parts import PartGraph
from .scope import assert_supported

REQUIREMENTS_SCHEMA = "pcb-agent-requirements"
REQUIREMENTS_VERSION = 1
SUPPORTED_FUNCTIONS = {
    "microcontroller",
    "temperature_sensor",
    "status_indicator",
    "i2c_connector",
    "updi_programming",
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
    functions = {entry["kind"]: entry for entry in spec.functions}
    required = SUPPORTED_FUNCTIONS
    missing = required - set(functions)
    if missing:
        raise ValidationError(
            "the built-in low-voltage controller profile requires functions: "
            + ", ".join(sorted(missing))
        )
    if len(functions) != len(spec.functions):
        raise ValidationError(
            "the built-in profile accepts one instance of each supported function"
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
    i2c = _i2c_contract(spec.interfaces)
    provenance = _provenance(spec, resolved_registry)
    requirements = tuple(
        Requirement(
            id=f"req_{function['id']}",
            text=function["intent"],
            acceptance=_function_acceptance(function["kind"]),
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
            {"id": "power_budget", "kind": "power_budget", "required": True},
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
            "compiler": "pcb-agent-runtime",
            "profile": "attiny402_tmp102_controller_v1",
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


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _power_contract(value: Mapping[str, Any]) -> dict[str, float]:
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
    if result["max_v"] > 3.6:
        raise ValidationError(
            "the verified TMP102 profile requires a power maximum <= 3.6 V"
        )
    return result


def _i2c_contract(values: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    matches = [entry for entry in values if entry.get("kind") == "i2c"]
    if len(matches) != 1:
        raise ValidationError("requirements must define exactly one I2C interface")
    value = matches[0]
    allowed = {"id", "kind", "speed_hz", "external_connector"}
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
    return dict(value)


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
                source="pcb-agent-runtime bundled block catalog",
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


def _function_acceptance(kind: str) -> tuple[str, ...]:
    return {
        "microcontroller": (
            "MCU power, ground, I2C, UPDI, and status GPIO contracts are connected.",
        ),
        "temperature_sensor": (
            "TMP102B supply, address strap, I2C, pull-up, and decoupling rules pass.",
        ),
        "status_indicator": (
            "LED polarity and calculated current remain within recorded ratings.",
        ),
        "i2c_connector": (
            "External connector pin order is GND/3V3/SDA/SCL and edge placement is enforced.",
        ),
        "updi_programming": (
            "UPDI, supply, and return are exposed on an accessible header.",
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
            {"max_distance_mm": 3.0, "min_capacitance_f": 1e-7},
            "release_blocking",
            "MCU supply transient return must be local.",
            block_source["core"],
        ),
        Constraint(
            "decouple_sensor",
            "decoupling",
            ("sensor_u2", "sensor_c2", "net_3v3", "net_gnd"),
            {"max_distance_mm": 2.0, "min_capacitance_f": 1e-7},
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
                "max_power_w": power["nominal_v"] * power["max_current_a"],
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
                "auto_route": True,
            },
            "release_blocking",
            "Bounded low-speed I2C routing contract.",
            block_source["sensor"],
        ),
        Constraint(
            "routing_power",
            "routing",
            ("net_3v3", "net_gnd"),
            {"width_mm": 0.5, "auto_route": True},
            "release_blocking",
            "Power routes require additional width.",
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
                "edge_clearance_mm": spec.board.edge_clearance_mm,
                "assembly_side": "front",
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
        "$id": "https://pcb-agent-runtime.invalid/schema/requirements-v1.json",
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
