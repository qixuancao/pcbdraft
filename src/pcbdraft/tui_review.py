"""Compact plan and semantic-diff summaries for terminal review surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReviewSection:
    title: str
    lines: tuple[str, ...]


def review_sections(view: Mapping[str, Any]) -> tuple[ReviewSection, ...]:
    """Extract bounded human-readable facts from an application project view."""

    result: list[ReviewSection] = []
    proposal = _mapping(_mapping(view.get("conversation")).get("proposal"))
    brief = _mapping(proposal.get("brief"))
    if brief:
        result.extend(_plan_sections(proposal, brief))
    active_change = _mapping(view.get("active_change"))
    if active_change:
        result.extend(_change_sections(active_change))
    artifacts = _mapping(view.get("artifacts"))
    validation = _mapping(artifacts.get("validation"))
    if validation:
        result.append(
            ReviewSection(
                "Current validation",
                (
                    f"Candidate ready: {_yes_no(validation.get('candidate_ready'))}",
                    f"Production ready: {_yes_no(validation.get('production_ready'))}",
                    f"Assurance: {validation.get('assurance', 'unknown')}",
                ),
            )
        )
    return tuple(result[:12])


def _plan_sections(
    proposal: Mapping[str, Any], brief: Mapping[str, Any]
) -> list[ReviewSection]:
    board = _mapping(brief.get("board"))
    identity = _mapping(brief.get("identity"))
    plan_lines = [f"Purpose: {brief.get('purpose', 'not stated')}"]
    if board:
        plan_lines.append(
            "Board: "
            f"{board.get('width_mm', '?')} × {board.get('height_mm', '?')} mm · "
            f"{board.get('layers', '?')} copper layer(s)"
        )
    requested = _text_list(identity.get("requested_parts"))
    plan_lines.append(
        "Requested parts: " + (", ".join(requested) if requested else "none named")
    )
    result = [ReviewSection("Circuit plan", tuple(plan_lines))]

    symbols: list[str] = []
    raw_symbols = identity.get("planned_symbols")
    if isinstance(raw_symbols, list):
        for entry in raw_symbols[:128]:
            item = _mapping(entry)
            if item:
                symbols.append(
                    f"{item.get('reference', '?')}: {item.get('symbol', 'unknown')}"
                )
    if symbols:
        result.append(ReviewSection("Planned symbols", tuple(symbols)))

    assumptions = _text_list(brief.get("assumptions"))
    warnings = _text_list(_mapping(proposal.get("scope")).get("warnings"))
    evidence_lines = [*(f"Assumption: {item}" for item in assumptions)]
    evidence_lines.extend(f"Warning: {item}" for item in warnings)
    if evidence_lines:
        result.append(ReviewSection("Assumptions and warnings", tuple(evidence_lines)))

    review = _mapping(brief.get("plan_review"))
    summary = _mapping(review.get("summary"))
    check_lines = [
        (
            "Attention required: "
            f"{summary.get('attention_required', 0)} · "
            f"failed: {summary.get('failed', 0)}"
        )
    ]
    findings = review.get("findings")
    if isinstance(findings, list):
        for finding in findings[:64]:
            item = _mapping(finding)
            if not item:
                continue
            outcome = str(item.get("outcome", item.get("state", "unknown"))).upper()
            identifier = item.get("id", item.get("title", "check"))
            detail = item.get("summary", item.get("rationale", ""))
            suffix = f" — {detail}" if detail else ""
            check_lines.append(f"[{outcome}] {identifier}{suffix}")
    result.append(ReviewSection("Plan checks", tuple(check_lines)))
    qualification = _mapping(review.get("component_qualification"))
    qualification_summary = _mapping(qualification.get("summary"))
    if qualification_summary:
        states = _mapping(qualification_summary.get("states"))
        datasheets = _mapping(qualification_summary.get("datasheets"))
        result.append(
            ReviewSection(
                "Component evidence",
                (
                    f"Components: {qualification_summary.get('components', 0)}",
                    "Qualification states: "
                    + (
                        ", ".join(f"{key}={value}" for key, value in states.items())
                        or "none"
                    ),
                    "Datasheets: "
                    + (
                        ", ".join(f"{key}={value}" for key, value in datasheets.items())
                        or "none"
                    ),
                    f"Pad mapping failures: {qualification_summary.get('pad_mapping_failures', 0)}",
                ),
            )
        )
    return result


def _change_sections(active_change: Mapping[str, Any]) -> list[ReviewSection]:
    diff = _mapping(active_change.get("diff"))
    summary = _mapping(diff.get("summary"))
    lines = [
        f"Request: {active_change.get('request', 'automatic repair')}",
        (
            "Objects: "
            f"+{summary.get('objects_added', 0)} "
            f"−{summary.get('objects_removed', 0)} "
            f"~{summary.get('objects_modified', 0)}"
        ),
    ]
    collections = _mapping(diff.get("collections"))
    for name, raw in list(collections.items())[:12]:
        value = _mapping(raw)
        added = _text_list(value.get("added"))
        removed = _text_list(value.get("removed"))
        modified = value.get("modified")
        modified_ids: list[str] = []
        if isinstance(modified, list):
            modified_ids = [
                str(item.get("id"))
                for item in modified[:64]
                if isinstance(item, Mapping) and item.get("id") is not None
            ]
        if added:
            lines.append(f"{name} added: {', '.join(added)}")
        if removed:
            lines.append(f"{name} removed: {', '.join(removed)}")
        if modified_ids:
            lines.append(f"{name} modified: {', '.join(modified_ids)}")
    board_fields = _mapping(diff.get("board_fields"))
    if board_fields:
        lines.append("Board fields changed: " + ", ".join(sorted(board_fields)))
    validation = _mapping(active_change.get("validation"))
    if validation:
        lines.extend(
            (
                f"Candidate ready: {_yes_no(validation.get('candidate_ready'))}",
                f"Production ready: {_yes_no(validation.get('production_ready'))}",
            )
        )
    return [ReviewSection("Staged semantic change", tuple(lines))]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value[:128] if isinstance(item, str) and item]


def _yes_no(value: Any) -> str:
    return "yes" if value is True else "no" if value is False else "unknown"
