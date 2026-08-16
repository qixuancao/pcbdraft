"""Compatibility facade for the conversational PCB-planning API.

The implementation is intentionally split by responsibility:

* :mod:`pcbdraft.agent.plan` owns versioned planner contracts;
* :mod:`pcbdraft.agent.part_resolver` owns local KiCad-library inspection;
* :mod:`pcbdraft.agent.review` owns engineering-plan review; and
* :mod:`pcbdraft.agent.compiler` owns deterministic lowering to semantic IR.

Keep this facade so integrations written against the original public module
path continue to work while production code imports the narrow owner module.
"""

from __future__ import annotations

from pcbdraft.agent.compiler import (
    AgentCompilation,
    compile_agent_plan,
    planner_symbol_context,
)
from pcbdraft.agent.part_resolver import (
    SYMBOL_FILE_LIMIT,
    LocalKiCadPartResolver,
    SymbolCandidate,
)
from pcbdraft.agent.plan import (
    AGENT_PLAN_LEGACY_VERSION,
    AGENT_PLAN_SCHEMA,
    AGENT_PLAN_VERSION,
    AGENT_REQUEST_SCHEMA,
    AGENT_REQUEST_VERSION,
    MAX_PLAN_ASSERTIONS,
    MAX_PLAN_BLOCKS,
    MAX_PLAN_COMPONENTS,
    MAX_PLAN_CONSTRAINTS,
    MAX_PLAN_INTERFACES,
    MAX_PLAN_NETS,
    MAX_PLAN_PARAMETERS,
    MAX_PLAN_POWER_DOMAINS,
    AgentDesignRequest,
    CircuitPlan,
    PlanAssertion,
    PlanBlock,
    PlanComponent,
    PlanConstraint,
    PlanInterface,
    PlanNet,
    PlanPowerDomain,
    circuit_plan_schema,
)
from pcbdraft.agent.review import (
    PLAN_REVIEW_SCHEMA,
    PLAN_REVIEW_VERSION,
    AgentPlanReview,
    PlanReviewFinding,
    review_agent_plan,
)

__all__ = (
    "AGENT_PLAN_LEGACY_VERSION",
    "AGENT_PLAN_SCHEMA",
    "AGENT_PLAN_VERSION",
    "AGENT_REQUEST_SCHEMA",
    "AGENT_REQUEST_VERSION",
    "MAX_PLAN_ASSERTIONS",
    "MAX_PLAN_BLOCKS",
    "MAX_PLAN_COMPONENTS",
    "MAX_PLAN_CONSTRAINTS",
    "MAX_PLAN_INTERFACES",
    "MAX_PLAN_NETS",
    "MAX_PLAN_PARAMETERS",
    "MAX_PLAN_POWER_DOMAINS",
    "PLAN_REVIEW_SCHEMA",
    "PLAN_REVIEW_VERSION",
    "SYMBOL_FILE_LIMIT",
    "AgentCompilation",
    "AgentDesignRequest",
    "AgentPlanReview",
    "CircuitPlan",
    "LocalKiCadPartResolver",
    "PlanAssertion",
    "PlanBlock",
    "PlanComponent",
    "PlanConstraint",
    "PlanInterface",
    "PlanNet",
    "PlanPowerDomain",
    "PlanReviewFinding",
    "SymbolCandidate",
    "circuit_plan_schema",
    "compile_agent_plan",
    "planner_symbol_context",
    "review_agent_plan",
)
