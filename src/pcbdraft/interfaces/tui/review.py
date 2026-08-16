"""Compact plan and semantic-diff summaries for terminal review surfaces."""

from __future__ import annotations

import json
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
                tuple(_validation_lines(validation)),
            )
        )
    return _bounded_sections(result)


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
        if len(raw_symbols) > 128:
            symbols.append(f"… {len(raw_symbols) - 128} more symbol(s) omitted")
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
            evidence = _text_list(item.get("evidence"))
            if evidence:
                shown = evidence[:4]
                check_lines.append(
                    "  Evidence: " + " · ".join(_display_text(value) for value in shown)
                )
                if len(evidence) > len(shown):
                    check_lines.append(
                        f"  … {len(evidence) - len(shown)} more evidence item(s) omitted"
                    )
            action = item.get("action")
            if isinstance(action, str) and action.strip():
                check_lines.append("  Action: " + action.strip())
        if len(findings) > 64:
            check_lines.append(f"… {len(findings) - 64} more finding(s) omitted")
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
    collection_items = list(collections.items())
    for name, raw in collection_items[:12]:
        value = _mapping(raw)
        added = _text_list(value.get("added"))
        removed = _text_list(value.get("removed"))
        modified = value.get("modified")
        modified_ids: list[str] = []
        if isinstance(modified, list):
            for raw_item in modified[:64]:
                item = _mapping(raw_item)
                if item.get("id") is None:
                    continue
                item_id = str(item["id"])
                modified_ids.append(item_id)
                fields = _mapping(item.get("fields"))
                for field, raw_change in list(fields.items())[:12]:
                    change = _mapping(raw_change)
                    lines.append(
                        f"{name}.{item_id}.{field}: "
                        f"{_display_value(change.get('before'))} → "
                        f"{_display_value(change.get('after'))}"
                    )
                if len(fields) > 12:
                    lines.append(
                        f"… {len(fields) - 12} more field change(s) omitted for {item_id}"
                    )
            if len(modified) > 64:
                lines.append(
                    f"… {len(modified) - 64} more modified {name} object(s) omitted"
                )
        if added:
            lines.append(f"{name} added: {', '.join(added)}")
        if removed:
            lines.append(f"{name} removed: {', '.join(removed)}")
        if modified_ids:
            lines.append(f"{name} modified: {', '.join(modified_ids)}")
    if len(collection_items) > 12:
        lines.append(f"… {len(collection_items) - 12} more collection(s) omitted")
    board_fields = _mapping(diff.get("board_fields"))
    if board_fields:
        for field, raw_change in board_fields.items():
            change = _mapping(raw_change)
            lines.append(
                f"Board.{field}: {_display_value(change.get('before'))} → "
                f"{_display_value(change.get('after'))}"
            )
    metadata_fields = _mapping(diff.get("metadata_fields"))
    if metadata_fields:
        for field, raw_change in metadata_fields.items():
            change = _mapping(raw_change)
            lines.append(
                f"Metadata.{field}: {_display_value(change.get('before'))} → "
                f"{_display_value(change.get('after'))}"
            )
    validation = _mapping(active_change.get("validation"))
    if validation:
        lines.extend(
            (
                f"Candidate ready: {_yes_no(validation.get('candidate_ready'))}",
                f"External production-gate records complete: {_yes_no(validation.get('production_evidence_complete'))}",
            )
        )
    return [ReviewSection("Staged semantic change", tuple(lines))]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value[:128] if isinstance(item, str) and item]


def _validation_lines(validation: Mapping[str, Any]) -> list[str]:
    lines = [
        f"Candidate ready: {_yes_no(validation.get('candidate_ready'))}",
        f"External production-gate records complete: {_yes_no(validation.get('production_evidence_complete'))}",
        "Production attestation: unsupported",
        f"Assurance: {validation.get('assurance', 'unknown')}",
    ]
    levels = validation.get("levels")
    notable: list[str] = []
    if isinstance(levels, list):
        for raw_level in levels:
            level = _mapping(raw_level)
            checks = level.get("checks")
            if not isinstance(checks, list):
                continue
            for raw_check in checks:
                check = _mapping(raw_check)
                outcome = str(check.get("outcome", "unknown"))
                state = str(check.get("state", "unknown"))
                if outcome == "pass" and state == "completed":
                    continue
                identifier = check.get("id", "check")
                summary = _display_text(check.get("summary", "No summary"))
                notable.append(
                    f"[{outcome.upper()}] {identifier} ({state}) — {summary}"
                )
    if notable:
        lines.append("Failed, incomplete, or human-required checks:")
        lines.extend(notable)
    return lines


def _bounded_sections(
    sections: list[ReviewSection], *, max_sections: int = 12, max_lines: int = 320
) -> tuple[ReviewSection, ...]:
    """Keep adversarial or very large retained evidence usable in a modal."""

    selected = sections[:max_sections]
    omitted_sections = len(sections) - len(selected)
    result: list[ReviewSection] = []
    remaining = max_lines
    for index, section in enumerate(selected):
        if remaining <= 0:
            omitted_sections += len(selected) - index
            break
        original = tuple(_display_text(line, limit=500) for line in section.lines)
        per_section = min(80, remaining)
        if len(original) > per_section:
            keep = max(0, per_section - 1)
            lines = (
                *original[:keep],
                f"… {len(original) - keep} more line(s) omitted from this section",
            )
        else:
            lines = original
        result.append(ReviewSection(section.title, tuple(lines)))
        remaining -= len(lines)
    if omitted_sections > 0:
        marker = ReviewSection(
            "More review data", (f"{omitted_sections} section(s) omitted.",)
        )
        if len(result) >= max_sections:
            result[-1] = marker
        elif remaining > 0:
            result.append(marker)
    return tuple(result)


def _yes_no(value: Any) -> str:
    return "yes" if value is True else "no" if value is False else "unknown"


def _display_value(value: Any, *, limit: int = 160) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


def _display_text(value: Any, *, limit: int = 240) -> str:
    rendered = " ".join(str(value).split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"
