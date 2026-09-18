"""Authoritative support declarations for deterministic design constraints.

This module is intentionally data-only.  The model tool schema, semantic IR,
semantic evaluator, and layered validation all consume the same closed set so a
new string cannot silently acquire release semantics without an implementation.
"""

from __future__ import annotations

from dataclasses import dataclass


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


def constraint_support(kind: str) -> ConstraintSupport | None:
    """Return the declared verifier contract, or ``None`` when unsupported."""

    return CONSTRAINT_SUPPORT.get(kind)
