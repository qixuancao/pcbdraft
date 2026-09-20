"""Authoritative support and write contracts for deterministic constraints.

The model tool schema, semantic IR,
semantic evaluator, and layered validation all consume the same closed set so a
new string cannot silently acquire release semantics without an implementation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pcbdraft.core.errors import ValidationError


@dataclass(frozen=True)
class ConstraintSupport:
    kind: str
    verification: str


_DEFINITIONS = (
    ConstraintSupport("assertion", "semantic_rule"),
    ConstraintSupport("board_keepout", "semantic_rule_and_recorded_metric"),
    ConstraintSupport("connector_pinout", "semantic_rule"),
    ConstraintSupport("current_limit", "semantic_rule"),
    ConstraintSupport("decoupling", "semantic_rule_and_geometry"),
    ConstraintSupport("differential_pair", "semantic_rule_and_recorded_metric"),
    ConstraintSupport("edge_placement", "semantic_rule_and_geometry"),
    ConstraintSupport("functional_group", "semantic_rule_and_geometry"),
    ConstraintSupport("i2c_electrical_budget", "semantic_rule"),
    ConstraintSupport("interface_pullups", "semantic_rule"),
    ConstraintSupport("ldo_regulation_budget", "semantic_rule"),
    ConstraintSupport("manufacturing_rules", "semantic_rule"),
    ConstraintSupport("net_label", "semantic_rule"),
    ConstraintSupport("placement_region", "semantic_rule_and_recorded_metric"),
    ConstraintSupport("power_budget", "semantic_rule"),
    ConstraintSupport("routing", "semantic_rule_and_routing_evidence"),
    ConstraintSupport("source_ownership", "semantic_rule"),
    ConstraintSupport("spi_electrical_budget", "semantic_rule"),
    ConstraintSupport("uart_electrical_budget", "semantic_rule"),
)

CONSTRAINT_SUPPORT = {item.kind: item for item in _DEFINITIONS}
SUPPORTED_CONSTRAINT_KINDS = tuple(sorted(CONSTRAINT_SUPPORT))

# These are intentionally kept here, next to the authoritative support registry,
# rather than in the IR loader.  Existing projects may contain an invalid
# constraint and must remain inspectable so validation can report the defect.
ASSERTION_PREDICATES = (
    "all_power_inputs_connected",
    "components_share_net",
    "interface_net_count",
    "net_endpoint_count",
)
MANUFACTURING_RULE_FIELDS = (
    "edge_clearance_mm",
    "min_clearance_mm",
    "min_drill_mm",
    "min_track_mm",
)
CURRENT_LIMIT_REQUIRED_FIELDS = ("forward_v", "max_current_a", "supply_v")
CURRENT_LIMIT_OPTIONAL_FIELDS = ("resistance_ohm",)


def validate_constraint_write(
    kind: Any,
    params: Any,
) -> None:
    """Reject malformed parameters at the authoritative flat-write boundary.

    ``Constraint.from_dict`` deliberately remains a structural loader: old
    projects with a bad constraint must still open and produce a validation
    failure.  This helper is for new ``add/update_constraint`` writes only.
    """

    if not isinstance(kind, str) or not kind:
        raise ValidationError("constraint.kind is required")
    if not isinstance(params, Mapping):
        raise ValidationError(f"{kind} constraint params must be an object")
    if kind == "assertion":
        _validate_assertion_write(params)
    elif kind == "manufacturing_rules":
        _validate_named_numeric_write(
            kind,
            params,
            required=MANUFACTURING_RULE_FIELDS,
            optional=(),
        )
    elif kind == "current_limit":
        _validate_named_numeric_write(
            kind,
            params,
            required=CURRENT_LIMIT_REQUIRED_FIELDS,
            optional=CURRENT_LIMIT_OPTIONAL_FIELDS,
        )


def _validate_assertion_write(params: Mapping[str, Any]) -> None:
    predicate = params.get("predicate")
    supported = ", ".join(ASSERTION_PREDICATES)
    if predicate not in ASSERTION_PREDICATES:
        raise ValidationError(
            "assertion params require a supported predicate "
            f"({supported}); human/mechanical review is not an automatic assertion"
        )
    allowed = {"predicate", "minimum", "maximum"}
    unknown = set(params) - allowed
    if unknown:
        raise ValidationError(
            "assertion params contain unsupported fields: " + ", ".join(sorted(unknown))
        )
    if predicate in {"net_endpoint_count", "interface_net_count"}:
        minimum = params.get("minimum")
        maximum = params.get("maximum")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
            raise ValidationError(
                "count assertions require minimum as a non-negative integer"
            )
        if maximum is not None and (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum < minimum
        ):
            raise ValidationError(
                "count assertions require maximum null or an integer at least minimum"
            )
    elif any(params.get(name) is not None for name in ("minimum", "maximum")):
        raise ValidationError(
            f"{predicate} assertions do not support count bounds; use null"
        )


def _validate_named_numeric_write(
    kind: str,
    params: Mapping[str, Any],
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...],
) -> None:
    expected = set(required) | set(optional)
    unknown = set(params) - expected
    missing = set(required) - set(params)
    if unknown or missing:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unsupported " + ", ".join(sorted(unknown)))
        raise ValidationError(
            f"{kind} constraint params require only the supported fields: "
            + "; ".join(details)
        )
    for name in expected & set(params):
        value = params[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            or (name == "resistance_ohm" and float(value) <= 0)
        ):
            raise ValidationError(
                f"{kind} constraint parameter {name} must be a finite non-negative number"
                + (" greater than zero" if name == "resistance_ohm" else "")
            )
    if kind == "current_limit":
        supply = float(params["supply_v"])
        forward = float(params["forward_v"])
        maximum = float(params["max_current_a"])
        if supply <= 0 or maximum <= 0 or forward >= supply:
            raise ValidationError(
                "current_limit requires supply_v > 0, max_current_a > 0, "
                "and 0 <= forward_v < supply_v"
            )


def constraint_support(kind: str) -> ConstraintSupport | None:
    """Return the declared verifier contract, or ``None`` when unsupported."""

    return CONSTRAINT_SUPPORT.get(kind)
