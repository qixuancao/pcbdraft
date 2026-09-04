"""Complete, source-bound ERC/DRC evidence and fail-closed DRC deltas.

The complete identity multiset in this module is the machine-verdict input.  A
separate bounded diagnostic projection is provided for people, models, and the
Web workbench; truncating that projection never truncates release evidence.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.core.project import sha256_file
from pcbdraft.verification.gates import (
    GATE_JSON_LIMIT,
    count_severities,
    report_declares_truncation,
    rule_report_shape_valid,
)

RULE_EVIDENCE_SCHEMA = "pcbdraft-complete-rule-evidence"
RULE_EVIDENCE_VERSION = 1
DRC_DELTA_SCHEMA = "pcbdraft-incremental-drc-delta"
DRC_DELTA_VERSION = 1
SOURCE_FILE_LIMIT = 128 * 1024 * 1024
EVIDENCE_FILE_LIMIT = 32 * 1024 * 1024
DIAGNOSTIC_LIMIT = 100
WarningPolicy = Literal["report_only", "block_new"]


@dataclass(frozen=True)
class RuleEvidence:
    kind: str
    path: Path
    status: str
    complete: bool
    failure: str | None
    canonical_revision: int
    design_revision: int
    design_content_hash: str
    source_file_sha256: str
    tool_version: str | None
    raw_report: str | None
    raw_report_sha256: str | None
    raw_report_size: int | None
    error_count: int | None
    warning_count: int | None
    findings: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RULE_EVIDENCE_SCHEMA,
            "version": RULE_EVIDENCE_VERSION,
            "kind": self.kind,
            "status": self.status,
            "complete": self.complete,
            "failure": self.failure,
            "binding": {
                "canonical_revision": self.canonical_revision,
                "design_revision": self.design_revision,
                "design_content_hash": self.design_content_hash,
                "source_file_sha256": self.source_file_sha256,
                "tool_version": self.tool_version,
            },
            "raw": {
                "report": self.raw_report,
                "sha256": self.raw_report_sha256,
                "size_bytes": self.raw_report_size,
            },
            "counts": {
                "error": self.error_count,
                "warning": self.warning_count,
                "findings": len(self.findings),
            },
            "findings": list(self.findings),
            "diagnostic_view": bounded_diagnostic_view(self.findings),
        }


@dataclass(frozen=True)
class DrcDelta:
    comparable: bool
    passed: bool
    failure_kinds: tuple[str, ...]
    warning_policy: WarningPolicy
    baseline_binding: dict[str, Any]
    candidate_binding: dict[str, Any]
    new_errors: tuple[str, ...]
    waived_new_errors: tuple[str, ...]
    retained_errors: tuple[str, ...]
    fixed_errors: tuple[str, ...]
    new_warnings: tuple[str, ...]
    retained_warnings: tuple[str, ...]
    fixed_warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DRC_DELTA_SCHEMA,
            "version": DRC_DELTA_VERSION,
            "comparable": self.comparable,
            "passed": self.passed,
            "failure_kinds": list(self.failure_kinds),
            "warning_policy": self.warning_policy,
            "baseline_binding": self.baseline_binding,
            "candidate_binding": self.candidate_binding,
            "counts": {
                "new_errors": len(self.new_errors),
                "waived_new_errors": len(self.waived_new_errors),
                "retained_errors": len(self.retained_errors),
                "fixed_errors": len(self.fixed_errors),
                "new_warnings": len(self.new_warnings),
                "retained_warnings": len(self.retained_warnings),
                "fixed_warnings": len(self.fixed_warnings),
            },
            "identities": {
                "new_errors": list(self.new_errors),
                "waived_new_errors": list(self.waived_new_errors),
                "retained_errors": list(self.retained_errors),
                "fixed_errors": list(self.fixed_errors),
                "new_warnings": list(self.new_warnings),
                "retained_warnings": list(self.retained_warnings),
                "fixed_warnings": list(self.fixed_warnings),
            },
        }


def capture_rule_evidence(
    *,
    kind: str,
    raw_report: Path | None,
    output: Path,
    source_file: Path,
    canonical_revision: int,
    design_revision: int,
    design_content_hash: str,
    failure: str | None = None,
    expected_raw_sha256: str | None = None,
) -> RuleEvidence:
    """Persist complete evidence, or an explicit unavailable record on failure."""

    _validate_binding(
        canonical_revision=canonical_revision,
        design_revision=design_revision,
        design_content_hash=design_content_hash,
    )
    if kind not in {"erc", "drc"}:
        raise PCBDraftError(f"unsupported rule evidence kind: {kind}")
    source_hash = sha256_file(source_file, max_bytes=SOURCE_FILE_LIMIT)
    unavailable = failure
    document: Any = None
    raw_hash: str | None = None
    raw_size: int | None = None
    raw_name: str | None = None
    if raw_report is not None:
        if raw_report.parent.resolve() != output.parent.resolve():
            raise PCBDraftError("rule evidence and raw report must share a directory")
        raw_name = raw_report.name
    if unavailable is None:
        if raw_report is None or not raw_report.is_file() or raw_report.is_symlink():
            unavailable = "missing_raw_report"
        else:
            try:
                raw_size = raw_report.stat().st_size
                document = load_json_limited(raw_report, GATE_JSON_LIMIT)
                raw_hash = sha256_file(raw_report, max_bytes=GATE_JSON_LIMIT)
            except (OSError, PCBDraftError):
                unavailable = "damaged_or_truncated_raw_report"
    if (
        unavailable is None
        and expected_raw_sha256 is not None
        and raw_hash != expected_raw_sha256
    ):
        unavailable = "raw_report_hash_mismatch"

    findings: tuple[dict[str, Any], ...] = ()
    tool_version: str | None = None
    errors: int | None = None
    warnings: int | None = None
    if unavailable is None:
        if not isinstance(document, dict) or not rule_report_shape_valid(
            kind, document
        ):
            unavailable = "malformed_raw_report"
        elif report_declares_truncation(document):
            unavailable = "truncated_underlying_evidence"
        else:
            tool = document.get("kicad_version")
            if not isinstance(tool, str) or not tool.strip():
                unavailable = "missing_tool_version"
            else:
                tool_version = tool.strip()
                errors, warnings = count_severities(document)
                extracted = tuple(_complete_findings(document))
                if len(extracted) != errors + warnings:
                    unavailable = "incomplete_violation_identities"
                else:
                    findings = extracted

    evidence = RuleEvidence(
        kind=kind,
        path=output,
        status="complete" if unavailable is None else "unavailable",
        complete=unavailable is None,
        failure=unavailable,
        canonical_revision=canonical_revision,
        design_revision=design_revision,
        design_content_hash=design_content_hash,
        source_file_sha256=source_hash,
        tool_version=tool_version,
        raw_report=raw_name,
        raw_report_sha256=raw_hash,
        raw_report_size=raw_size,
        error_count=errors if unavailable is None else None,
        warning_count=warnings if unavailable is None else None,
        findings=findings if unavailable is None else (),
    )
    atomic_write_json(output, evidence.to_dict())
    return evidence


def load_rule_evidence(path: Path) -> RuleEvidence:
    """Load evidence and re-verify its raw bytes and complete identity multiset."""

    try:
        value = load_json_limited(path, EVIDENCE_FILE_LIMIT)
    except PCBDraftError as exc:
        raise PCBDraftError("rule evidence is missing or damaged") from exc
    if not isinstance(value, dict):
        raise PCBDraftError("rule evidence must be a JSON object")
    if (
        value.get("schema") != RULE_EVIDENCE_SCHEMA
        or value.get("version") != RULE_EVIDENCE_VERSION
    ):
        raise PCBDraftError("unsupported rule evidence schema")
    binding = value.get("binding")
    raw = value.get("raw")
    counts = value.get("counts")
    findings = value.get("findings")
    if (
        not isinstance(binding, dict)
        or not isinstance(raw, dict)
        or not isinstance(counts, dict)
    ):
        raise PCBDraftError("rule evidence metadata is malformed")
    if not isinstance(findings, list) or not all(
        isinstance(item, dict) for item in findings
    ):
        raise PCBDraftError("rule evidence findings are malformed")
    kind = value.get("kind")
    if kind not in {"erc", "drc"}:
        raise PCBDraftError("rule evidence kind is malformed")
    canonical_revision = binding.get("canonical_revision")
    design_revision = binding.get("design_revision")
    design_hash = binding.get("design_content_hash")
    _validate_binding(
        canonical_revision=canonical_revision,
        design_revision=design_revision,
        design_content_hash=design_hash,
    )
    canonical_revision = cast(int, canonical_revision)
    design_revision = cast(int, design_revision)
    design_hash = cast(str, design_hash)
    source_hash = binding.get("source_file_sha256")
    if not _is_sha256(source_hash):
        raise PCBDraftError("rule evidence source hash is malformed")
    source_hash = cast(str, source_hash)
    status = value.get("status")
    complete = value.get("complete")
    failure = value.get("failure")
    if status not in {"complete", "unavailable"} or not isinstance(complete, bool):
        raise PCBDraftError("rule evidence status is malformed")
    if failure is not None and not isinstance(failure, str):
        raise PCBDraftError("rule evidence failure is malformed")
    if status != "complete" or not complete or failure is not None:
        return RuleEvidence(
            kind=kind,
            path=path,
            status="unavailable",
            complete=False,
            failure=failure or "evidence_marked_incomplete",
            canonical_revision=canonical_revision,
            design_revision=design_revision,
            design_content_hash=design_hash,
            source_file_sha256=source_hash,
            tool_version=None,
            raw_report=None,
            raw_report_sha256=None,
            raw_report_size=None,
            error_count=None,
            warning_count=None,
            findings=(),
        )

    raw_name = raw.get("report")
    raw_hash = raw.get("sha256")
    raw_size = raw.get("size_bytes")
    tool_version = binding.get("tool_version")
    errors = counts.get("error")
    warnings = counts.get("warning")
    if (
        not isinstance(raw_name, str)
        or Path(raw_name).name != raw_name
        or not _is_sha256(raw_hash)
        or not isinstance(raw_size, int)
        or raw_size < 0
        or not isinstance(tool_version, str)
        or not tool_version
        or not isinstance(errors, int)
        or errors < 0
        or not isinstance(warnings, int)
        or warnings < 0
        or counts.get("findings") != len(findings)
    ):
        raise PCBDraftError("complete rule evidence metadata is malformed")
    raw_path = path.parent / raw_name
    if not raw_path.is_file() or raw_path.is_symlink():
        raise PCBDraftError("complete rule evidence raw report is unavailable")
    try:
        if raw_path.stat().st_size != raw_size:
            raise PCBDraftError("complete rule evidence raw report size mismatch")
        if sha256_file(raw_path, max_bytes=GATE_JSON_LIMIT) != raw_hash:
            raise PCBDraftError("complete rule evidence raw report hash mismatch")
        document = load_json_limited(raw_path, GATE_JSON_LIMIT)
    except OSError as exc:
        raise PCBDraftError("complete rule evidence raw report is unavailable") from exc
    if (
        not isinstance(document, dict)
        or not rule_report_shape_valid(kind, document)
        or report_declares_truncation(document)
        or document.get("kicad_version") != tool_version
    ):
        raise PCBDraftError("complete rule evidence raw report is malformed")
    rebuilt = tuple(_complete_findings(document))
    rebuilt_errors, rebuilt_warnings = count_severities(document)
    if (
        rebuilt_errors != errors
        or rebuilt_warnings != warnings
        or rebuilt != tuple(findings)
        or len(rebuilt) != errors + warnings
    ):
        raise PCBDraftError("complete rule evidence identities do not match raw report")
    return RuleEvidence(
        kind=kind,
        path=path,
        status=status,
        complete=True,
        failure=None,
        canonical_revision=canonical_revision,
        design_revision=design_revision,
        design_content_hash=design_hash,
        source_file_sha256=source_hash,
        tool_version=tool_version,
        raw_report=raw_name,
        raw_report_sha256=raw_hash,
        raw_report_size=raw_size,
        error_count=errors,
        warning_count=warnings,
        findings=tuple(findings),
    )


def materialize_rule_evidence(
    source: Path, destination: Path, *, raw_name: str
) -> RuleEvidence:
    """Copy already verified evidence into a self-contained evidence directory."""

    loaded = load_rule_evidence(source)
    if not loaded.complete or loaded.raw_report is None:
        atomic_write_json(destination, loaded.to_dict())
        return loaded
    if Path(raw_name).name != raw_name:
        raise PCBDraftError("materialized raw report name is unsafe")
    source_raw = source.parent / loaded.raw_report
    destination_raw = destination.parent / raw_name
    destination_raw.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source_raw, destination_raw)
    destination_raw.chmod(0o600)
    copied = RuleEvidence(
        kind=loaded.kind,
        path=destination,
        status=loaded.status,
        complete=loaded.complete,
        failure=loaded.failure,
        canonical_revision=loaded.canonical_revision,
        design_revision=loaded.design_revision,
        design_content_hash=loaded.design_content_hash,
        source_file_sha256=loaded.source_file_sha256,
        tool_version=loaded.tool_version,
        raw_report=raw_name,
        raw_report_sha256=loaded.raw_report_sha256,
        raw_report_size=loaded.raw_report_size,
        error_count=loaded.error_count,
        warning_count=loaded.warning_count,
        findings=loaded.findings,
    )
    atomic_write_json(destination, copied.to_dict())
    return load_rule_evidence(destination)


def compare_drc_evidence(
    baseline: RuleEvidence,
    candidate: RuleEvidence,
    *,
    expected_baseline_design_revision: int | None = None,
    expected_baseline_content_hash: str | None = None,
    expected_candidate_design_revision: int | None = None,
    expected_candidate_content_hash: str | None = None,
    warning_policy: WarningPolicy = "report_only",
    waived_identities: tuple[str, ...] = (),
) -> DrcDelta:
    """Compare complete DRC multisets; any unavailable binding fails closed."""

    if warning_policy not in {"report_only", "block_new"}:
        raise PCBDraftError("unsupported incremental DRC warning policy")
    failures: list[str] = []
    if baseline.kind != "drc" or candidate.kind != "drc":
        failures.append("not_drc_evidence")
    if not baseline.complete or baseline.status != "complete":
        failures.append("baseline_evidence_unavailable")
    if not candidate.complete or candidate.status != "complete":
        failures.append("candidate_evidence_unavailable")
    if (
        expected_baseline_design_revision is not None
        and baseline.design_revision != expected_baseline_design_revision
    ):
        failures.append("baseline_revision_mismatch")
    if (
        expected_baseline_content_hash is not None
        and baseline.design_content_hash != expected_baseline_content_hash
    ):
        failures.append("baseline_content_hash_mismatch")
    if (
        expected_candidate_design_revision is not None
        and candidate.design_revision != expected_candidate_design_revision
    ):
        failures.append("candidate_revision_mismatch")
    if (
        expected_candidate_content_hash is not None
        and candidate.design_content_hash != expected_candidate_content_hash
    ):
        failures.append("candidate_content_hash_mismatch")
    if (
        baseline.tool_version is not None
        and candidate.tool_version is not None
        and baseline.tool_version != candidate.tool_version
    ):
        failures.append("tool_version_mismatch")
    if failures:
        return _failed_delta(
            failures,
            baseline,
            candidate,
            warning_policy=warning_policy,
        )

    baseline_errors = _identity_counter(baseline.findings, "error")
    candidate_errors = _identity_counter(candidate.findings, "error")
    baseline_warnings = _identity_counter(baseline.findings, "warning")
    candidate_warnings = _identity_counter(candidate.findings, "warning")
    new_errors = _counter_items(candidate_errors - baseline_errors)
    retained_errors = _counter_items(candidate_errors & baseline_errors)
    fixed_errors = _counter_items(baseline_errors - candidate_errors)
    new_warnings = _counter_items(candidate_warnings - baseline_warnings)
    retained_warnings = _counter_items(candidate_warnings & baseline_warnings)
    fixed_warnings = _counter_items(baseline_warnings - candidate_warnings)
    waiver_set = set(waived_identities)
    waived_new = tuple(identity for identity in new_errors if identity in waiver_set)
    unwaived_new = tuple(
        identity for identity in new_errors if identity not in waiver_set
    )
    passed = not unwaived_new and (warning_policy == "report_only" or not new_warnings)
    return DrcDelta(
        comparable=True,
        passed=passed,
        failure_kinds=(),
        warning_policy=warning_policy,
        baseline_binding=_binding_view(baseline),
        candidate_binding=_binding_view(candidate),
        new_errors=unwaived_new,
        waived_new_errors=waived_new,
        retained_errors=retained_errors,
        fixed_errors=fixed_errors,
        new_warnings=new_warnings,
        retained_warnings=retained_warnings,
        fixed_warnings=fixed_warnings,
    )


def fail_closed_drc_delta(
    candidate: RuleEvidence,
    failure_kind: str,
    *,
    warning_policy: WarningPolicy = "report_only",
) -> DrcDelta:
    """Create an auditable blocked delta when baseline recovery itself failed."""

    if not failure_kind:
        raise PCBDraftError("fail-closed DRC delta requires a failure kind")
    return DrcDelta(
        comparable=False,
        passed=False,
        failure_kinds=(failure_kind,),
        warning_policy=warning_policy,
        baseline_binding={},
        candidate_binding=_binding_view(candidate),
        new_errors=(),
        waived_new_errors=(),
        retained_errors=(),
        fixed_errors=(),
        new_warnings=(),
        retained_warnings=(),
        fixed_warnings=(),
    )


def bounded_diagnostic_view(
    findings: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    limit: int = DIAGNOSTIC_LIMIT,
) -> dict[str, Any]:
    if limit < 0:
        raise PCBDraftError("diagnostic limit cannot be negative")
    prioritized = sorted(
        findings,
        key=lambda item: (
            0 if item.get("severity") == "error" else 1,
            str(item.get("fingerprint", "")),
        ),
    )
    records = [
        {
            "fingerprint": item.get("fingerprint"),
            "severity": item.get("severity"),
            "type": item.get("type"),
            "message": item.get("message"),
            "items": item.get("diagnostic_items", []),
        }
        for item in prioritized[:limit]
    ]
    return {
        "findings": records,
        "total": len(findings),
        "truncated": len(findings) > len(records),
        "machine_evidence_complete": True,
    }


def _complete_findings(document: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def visit(value: Any, context: dict[str, Any]) -> None:
        if isinstance(value, dict):
            inherited = dict(context)
            for key in ("sheet", "path", "uuid_path"):
                scalar = value.get(key)
                if isinstance(scalar, (str, int, float)) and not isinstance(
                    scalar, bool
                ):
                    inherited[key] = scalar
            severity = value.get("severity")
            if isinstance(severity, str) and severity.lower() in {"error", "warning"}:
                records.append(_finding(value, inherited))
                return
            for child in value.values():
                visit(child, inherited)
        elif isinstance(value, list):
            for child in value:
                visit(child, context)

    visit(document, {})
    return records


def _finding(value: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    severity = str(value["severity"]).lower()
    identity: dict[str, Any] = {"severity": severity}
    for key in ("type", "rule", "code", "check"):
        scalar = _identity_scalar(value.get(key))
        if scalar is not None:
            identity[key] = scalar
    if context:
        identity["context"] = context
    for key in ("pos", "position", "start", "end", "bbox", "layer", "net"):
        normalized = _identity_value(value.get(key))
        if normalized is not None:
            identity[key] = normalized
    raw_items = value.get("items")
    items: list[dict[str, Any]] = []
    diagnostic_items: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            item = _item_identity(raw_item)
            if item:
                items.append(item)
            if len(diagnostic_items) < 5:
                diagnostic_items.append(_item_diagnostic(raw_item))
    if items:
        identity["items"] = sorted(items, key=_canonical_json)
    if len(identity) == 1 or (len(identity) == 2 and "context" in identity):
        fallback = value.get("description", value.get("message"))
        if isinstance(fallback, str):
            identity["description"] = fallback
    fingerprint = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    message = value.get("description", value.get("message", ""))
    return {
        "fingerprint": fingerprint,
        "severity": severity,
        "type": str(value.get("type", value.get("rule", "unknown")))[:512],
        "message": str(message)[:1000],
        "identity": identity,
        "diagnostic_items": diagnostic_items,
    }


def _item_identity(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "uuid",
        "reference",
        "footprint",
        "pad",
        "net",
        "layer",
        "type",
        "shape",
        "pos",
        "position",
        "start",
        "end",
        "bbox",
        "size",
        "angle",
        "rotation",
    ):
        normalized = _identity_value(value.get(key))
        if normalized is not None:
            result[key] = normalized
    if not result and isinstance(value.get("description"), str):
        result["description"] = value["description"]
    return result


def _item_diagnostic(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("uuid", "reference", "footprint", "pad", "net", "layer", "type"):
        scalar = _identity_scalar(value.get(key))
        if scalar is not None:
            result[key] = scalar
    position = _identity_value(value.get("pos", value.get("position")))
    if position is not None:
        result["pos"] = position
    description = value.get("description")
    if isinstance(description, str):
        result["message"] = description[:512]
    return result


def _identity_value(value: Any) -> Any | None:
    scalar = _identity_scalar(value)
    if scalar is not None:
        return scalar
    if isinstance(value, list):
        return [item for child in value if (item := _identity_value(child)) is not None]
    if isinstance(value, dict):
        normalized = {
            str(key): item
            for key, child in sorted(value.items(), key=lambda pair: str(pair[0]))
            if (item := _identity_value(child)) is not None
        }
        return normalized or None
    return None


def _identity_scalar(value: Any) -> str | int | float | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _identity_counter(
    findings: tuple[dict[str, Any], ...], severity: str
) -> Counter[str]:
    return Counter(
        str(item["fingerprint"])
        for item in findings
        if item.get("severity") == severity
    )


def _counter_items(counter: Counter[str]) -> tuple[str, ...]:
    return tuple(
        identity for identity in sorted(counter) for _ in range(counter[identity])
    )


def _binding_view(evidence: RuleEvidence) -> dict[str, Any]:
    return {
        "canonical_revision": evidence.canonical_revision,
        "design_revision": evidence.design_revision,
        "design_content_hash": evidence.design_content_hash,
        "source_file_sha256": evidence.source_file_sha256,
        "tool_version": evidence.tool_version,
        "raw_report_sha256": evidence.raw_report_sha256,
    }


def _failed_delta(
    failures: list[str],
    baseline: RuleEvidence,
    candidate: RuleEvidence,
    *,
    warning_policy: WarningPolicy,
) -> DrcDelta:
    return DrcDelta(
        comparable=False,
        passed=False,
        failure_kinds=tuple(dict.fromkeys(failures)),
        warning_policy=warning_policy,
        baseline_binding=_binding_view(baseline),
        candidate_binding=_binding_view(candidate),
        new_errors=(),
        waived_new_errors=(),
        retained_errors=(),
        fixed_errors=(),
        new_warnings=(),
        retained_warnings=(),
        fixed_warnings=(),
    )


def _validate_binding(
    *,
    canonical_revision: Any,
    design_revision: Any,
    design_content_hash: Any,
) -> None:
    if (
        not isinstance(canonical_revision, int)
        or isinstance(canonical_revision, bool)
        or canonical_revision < 0
        or not isinstance(design_revision, int)
        or isinstance(design_revision, bool)
        or design_revision < 0
        or not _is_sha256(design_content_hash)
    ):
        raise PCBDraftError("rule evidence revision/content binding is malformed")


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
