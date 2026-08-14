"""Bounded, serializable feedback for automatic PCB plan repair."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .errors import ValidationError

REPAIR_FEEDBACK_SCHEMA = "pcbdraft-agent-repair-feedback"
REPAIR_FEEDBACK_VERSION = 1
MAX_AUTOMATIC_REPAIRS = 2
MAX_REPAIR_FINDINGS = 32
_PHASES = {"generation", "validation"}


def _bounded_text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"repair feedback {field} must be non-empty text")
    normalized = " ".join(value.replace("\x00", "").split())
    encoded = normalized.encode("utf-8")
    if len(encoded) > limit:
        encoded = encoded[:limit]
        normalized = encoded.decode("utf-8", "ignore").rstrip()
    if not normalized:
        raise ValidationError(f"repair feedback {field} is empty after normalization")
    return normalized


def normalize_repair_feedback(value: Any) -> dict[str, Any]:
    """Validate the only diagnostic shape allowed into a repair prompt."""

    required = {"schema", "version", "phase", "attempt", "summary", "findings"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValidationError("repair feedback has an invalid shape")
    if (
        value["schema"] != REPAIR_FEEDBACK_SCHEMA
        or value["version"] != REPAIR_FEEDBACK_VERSION
    ):
        raise ValidationError("unsupported repair feedback schema/version")
    phase = value["phase"]
    if phase not in _PHASES:
        raise ValidationError("repair feedback phase is invalid")
    attempt = value["attempt"]
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or attempt > MAX_AUTOMATIC_REPAIRS
    ):
        raise ValidationError("repair feedback attempt is out of bounds")
    raw_findings = value["findings"]
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_REPAIR_FINDINGS:
        raise ValidationError("repair feedback findings are invalid")
    findings = [_bounded_text(item, "finding", limit=2048) for item in raw_findings]
    if len(set(findings)) != len(findings):
        raise ValidationError("repair feedback findings contain duplicates")
    return {
        "schema": REPAIR_FEEDBACK_SCHEMA,
        "version": REPAIR_FEEDBACK_VERSION,
        "phase": phase,
        "attempt": attempt,
        "summary": _bounded_text(value["summary"], "summary", limit=2048),
        "findings": findings,
    }


def generation_feedback(
    view: Mapping[str, Any], error: BaseException, *, attempt: int
) -> dict[str, Any]:
    """Build sanitized generation/routing feedback from retained attempt evidence."""

    findings: list[str] = []
    conversation = view.get("conversation")
    messages = (
        conversation.get("messages") if isinstance(conversation, Mapping) else None
    )
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, Mapping) or message.get("kind") != "failure":
                continue
            text = message.get("text")
            if isinstance(text, str) and text.strip():
                findings.append(text)
                break
    attempts = view.get("attempts")
    if not findings and isinstance(attempts, list):
        for record in attempts:
            if not isinstance(record, Mapping) or record.get("status") != "failed":
                continue
            message = record.get("error")
            if isinstance(message, str) and message.strip():
                findings.append(message)
                break
    if not findings:
        findings.append(
            f"{type(error).__name__}: generation failed without retained public detail"
        )
    return normalize_repair_feedback(
        {
            "schema": REPAIR_FEEDBACK_SCHEMA,
            "version": REPAIR_FEEDBACK_VERSION,
            "phase": "generation",
            "attempt": attempt,
            "summary": "Native KiCad generation or bounded routing failed.",
            "findings": findings,
        }
    )


def validation_feedback_from_levels(
    levels: Any, *, attempt: int
) -> dict[str, Any] | None:
    """Build repair feedback from serialized deterministic validation levels."""

    if not isinstance(levels, list):
        return None
    findings: list[str] = []
    for level in levels:
        if not isinstance(level, Mapping) or level.get("level") not in {
            "L1",
            "L2",
            "L3",
        }:
            continue
        checks = level.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if (
                not isinstance(check, Mapping)
                or check.get("state") != "completed"
                or check.get("outcome") != "fail"
            ):
                continue
            identifier = str(check.get("id", "validation"))
            summary = str(check.get("summary", "deterministic check failed"))
            metrics = check.get("metrics")
            detail = ""
            if isinstance(metrics, Mapping):
                detail = (
                    " metrics="
                    + json.dumps(
                        dict(metrics),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )[:1024]
                )
            findings.append(f"{identifier}: {summary}{detail}")
            if len(findings) >= MAX_REPAIR_FINDINGS:
                break
    if not findings:
        return None
    return normalize_repair_feedback(
        {
            "schema": REPAIR_FEEDBACK_SCHEMA,
            "version": REPAIR_FEEDBACK_VERSION,
            "phase": "validation",
            "attempt": attempt,
            "summary": "Deterministic component, KiCad ERC/DRC, or semantic checks failed.",
            "findings": findings,
        }
    )


def validation_feedback(
    view: Mapping[str, Any], *, attempt: int
) -> dict[str, Any] | None:
    """Extract only actionable completed deterministic L1-L3 failures."""

    artifacts = view.get("artifacts")
    validation = artifacts.get("validation") if isinstance(artifacts, Mapping) else None
    levels = validation.get("levels") if isinstance(validation, Mapping) else None
    return validation_feedback_from_levels(levels, attempt=attempt)
