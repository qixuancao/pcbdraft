"""Small, deterministic coverage projection for explicit user requirements.

Acceptance bindings can only point at retained validation checks.  Manual and
unsupported bindings stay visibly blocked; model-authored prose is never
promoted into evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcbdraft.domain.ir import Design

TASK_COVERAGE_SCHEMA = "pcbdraft-task-coverage"
TASK_COVERAGE_VERSION = 1
ACCEPTANCE_BINDING_PATTERN = (
    r"^(?:check|constraint|manual|unsupported):[a-z][a-z0-9_.-]{0,127}$"
)
_ACCEPTANCE_BINDING = re.compile(ACCEPTANCE_BINDING_PATTERN)


def _validation_checks(validation: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    levels = validation.get("levels")
    if not isinstance(levels, list):
        return checks
    for level in levels:
        members = level.get("checks") if isinstance(level, Mapping) else None
        if not isinstance(members, list):
            continue
        for check in members:
            check_id = check.get("id") if isinstance(check, Mapping) else None
            if isinstance(check_id, str) and check_id not in checks:
                checks[check_id] = dict(check)
    return checks


def candidate_gate_status(
    validation: object,
    *,
    design_revision: int,
    design_content_hash: str,
) -> dict[str, Any]:
    """Describe whether aggregate candidate evidence is current and passing."""

    if not isinstance(validation, Mapping):
        return {
            "outcome": "incomplete",
            "passed": False,
            "reason": "candidate_validation_missing",
            "source_design_revision": None,
            "source_content_hash": None,
        }
    source_revision = validation.get("source_design_revision")
    source_hash = validation.get("source_content_hash")
    current = source_revision == design_revision and source_hash == design_content_hash
    if not current:
        return {
            "outcome": "incomplete",
            "passed": False,
            "reason": "candidate_validation_stale",
            "source_design_revision": source_revision,
            "source_content_hash": source_hash,
        }
    passed = validation.get("candidate_ready") is True
    return {
        "outcome": "passed" if passed else "failed",
        "passed": passed,
        "reason": "candidate_validation_passed" if passed else "candidate_validation_failed",
        "run_id": validation.get("run_id"),
        "source_design_revision": source_revision,
        "source_content_hash": source_hash,
    }


def evaluate_task_coverage(
    design: Design,
    validation: object,
    *,
    design_revision: int,
) -> dict[str, Any]:
    """Bind each declared acceptance item to current, non-model evidence."""

    design_hash = design.content_hash()
    gate = candidate_gate_status(
        validation,
        design_revision=design_revision,
        design_content_hash=design_hash,
    )
    validation_current = gate["reason"] not in {
        "candidate_validation_missing",
        "candidate_validation_stale",
    }
    retained = dict(validation) if isinstance(validation, Mapping) else {}
    checks = _validation_checks(retained) if validation_current else {}
    items: list[dict[str, Any]] = []
    if not design.requirements:
        return {
            "schema": TASK_COVERAGE_SCHEMA,
            "version": TASK_COVERAGE_VERSION,
            "outcome": "incomplete",
            "complete": False,
            "reason": "requirements_missing",
            "source_design_revision": design_revision,
            "source_content_hash": design_hash,
            "validation_run_id": retained.get("run_id"),
            "items": [],
        }

    for requirement in design.requirements:
        if not requirement.acceptance:
            items.append(
                {
                    "requirement_id": requirement.id,
                    "acceptance": None,
                    "state": "missing",
                    "outcome": "incomplete",
                    "evidence": [],
                }
            )
            continue
        for acceptance in requirement.acceptance:
            state = "unsupported"
            outcome = "blocked"
            evidence: list[str] = []
            target: str | None = None
            if _ACCEPTANCE_BINDING.fullmatch(acceptance):
                kind, identifier = acceptance.split(":", 1)
                if kind == "manual":
                    state = "human_required"
                elif kind == "unsupported":
                    state = "unsupported"
                else:
                    target = (
                        identifier
                        if kind == "check"
                        else f"l3.constraint.{identifier}"
                    )
                    check = checks.get(target)
                    if check is None:
                        if validation_current:
                            state = "unsupported"
                            outcome = "blocked"
                        else:
                            state = "stale" if isinstance(validation, Mapping) else "unavailable"
                            outcome = "incomplete"
                    else:
                        check_state = check.get("state")
                        check_outcome = check.get("outcome")
                        evidence = [
                            item
                            for item in check.get("evidence", [])
                            if isinstance(item, str)
                        ]
                        if check_state in {"completed", "not_applicable"} and check_outcome == "pass":
                            state = "verified"
                            outcome = "passed"
                        elif check_outcome == "fail":
                            state = "verified"
                            outcome = "failed"
                        elif check_state == "human_required":
                            state = "human_required"
                            outcome = "blocked"
                        else:
                            state = "unavailable"
                            outcome = "incomplete"
            items.append(
                {
                    "requirement_id": requirement.id,
                    "acceptance": acceptance,
                    "target_check": target,
                    "state": state,
                    "outcome": outcome,
                    "evidence": evidence,
                }
            )

    outcomes = {item["outcome"] for item in items}
    overall = (
        "failed"
        if "failed" in outcomes
        else "blocked"
        if "blocked" in outcomes
        else "incomplete"
        if "incomplete" in outcomes
        else "passed"
    )
    return {
        "schema": TASK_COVERAGE_SCHEMA,
        "version": TASK_COVERAGE_VERSION,
        "outcome": overall,
        "complete": overall == "passed",
        "reason": "all_acceptance_items_verified" if overall == "passed" else "acceptance_items_unresolved",
        "source_design_revision": design_revision,
        "source_content_hash": design_hash,
        "validation_run_id": retained.get("run_id"),
        "items": items,
    }
