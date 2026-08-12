"""Human-readable review report rendering."""

from __future__ import annotations

import html
from typing import Any, Mapping


def _md(value: Any) -> str:
    """Render untrusted project/model text as one inert Markdown line."""
    text = " ".join(str(value).split())
    text = html.escape(text, quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "(", ")", "#", "|", ">"):
        text = text.replace(character, f"\\{character}")
    return text


def _items(values: list[str]) -> str:
    return "\n".join(f"- {_md(value)}" for value in values) if values else "- None identified"


def render_review_markdown(
    *,
    run_id: str,
    project: str,
    selected_files: Mapping[str, str],
    gates: Mapping[str, Any],
    review: Mapping[str, Any],
    violations: Mapping[str, Any] | None = None,
) -> str:
    gate_lines: list[str] = []
    for name in ("erc", "drc"):
        gate = gates.get(name, {})
        counts = gate.get("counts", {})
        gate_lines.append(
            f"- {name.upper()}: tool_status={gate.get('tool_status', 'missing')}, "
            f"exit_code={gate.get('exit_code')}, errors={counts.get('error')}, "
            f"warnings={counts.get('warning')}"
        )

    findings: list[str] = []
    for index, finding in enumerate(review.get("findings", []), start=1):
        evidence = "; ".join(_md(value) for value in finding["evidence"]) or "No concrete evidence supplied"
        findings.append(
            f"### {index}. [{_md(finding['severity'].upper())}] {_md(finding['title'])}\n\n"
            f"- Category: {_md(finding['category'])}\n"
            f"- Confidence: {_md(finding['confidence'])}\n"
            f"- Requires human: {str(finding['requires_human']).lower()}\n"
            f"- Evidence: {evidence}\n"
            f"- Rationale: {_md(finding['rationale'])}\n"
            f"- Proposed action: {_md(finding['proposed_action'])}"
        )
    finding_text = "\n\n".join(findings) if findings else "No AI heuristic findings returned."

    violation_lines: list[str] = []
    for gate_name in ("erc", "drc"):
        detail = (violations or {}).get(gate_name, {})
        for violation in detail.get("violations", [])[:50]:
            description = violation.get("description") or violation.get("type") or "unnamed violation"
            violation_lines.append(
                f"- {gate_name.upper()} [{_md(violation.get('severity'))}]: {_md(description)}"
            )
        if detail.get("violations_truncated"):
            violation_lines.append(f"- {gate_name.upper()}: additional violations omitted from Markdown; see evidence.json")
    violation_text = "\n".join(violation_lines) if violation_lines else "- No error/warning records in parsed gate JSON."

    return f"""# PCB design review

Run: {_md(run_id)}  
Project: {_md(project)}  
Schematic: {_md(selected_files['schematic'])}  
Board: {_md(selected_files['board'])}

## Scope and interpretation

The ERC/DRC section below is deterministic evidence produced by the local `kicad-cli`.
The design interpretation and findings are AI heuristics, not a safety or engineering sign-off.
This report does not establish functional correctness, SI/PI, thermal, EMI, timing, tolerance,
manufacturability, regulatory compliance, or production readiness.

## Deterministic ERC/DRC evidence

{chr(10).join(gate_lines)}

### Parsed violation evidence

{violation_text}

## AI heuristic overview

Risk: **{_md(review['risk'])}**

{_md(review['summary'])}

### Modules

{_items(review['modules'])}

### Interfaces

{_items(review['interfaces'])}

### Power domains

{_items(review['power_domains'])}

### Missing constraints

{_items(review['missing_constraints'])}

## AI heuristic findings

{finding_text}

## Unsupported checks

{_items(review['unsupported_checks'])}
"""
