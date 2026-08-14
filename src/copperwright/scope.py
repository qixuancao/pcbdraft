"""Technical generation envelope and non-blocking domain diagnostics."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError
from .ir import Design, Scope

SUPPORTED_DOMAINS = {
    "low_voltage_mcu",
    "sensor",
    "simple_control",
    "i2c",
    "spi",
    "uart",
    "usb2_basic",
    "ldo",
    "simple_buck",
}
COMPLEX_DOMAINS = {
    "ddr",
    "pcie",
    "serdes",
    "rf",
    "mains",
    "high_voltage",
    "medical",
    "aviation",
    "safety_critical",
    "high_power",
}


@dataclass(frozen=True)
class ScopeDecision:
    accepted: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def evaluate_scope(scope: Scope) -> ScopeDecision:
    """Report actual backend limits without rejecting a design domain."""

    reasons: list[str] = []
    warnings: list[str] = []
    unknown = sorted(set(scope.domains) - SUPPORTED_DOMAINS - COMPLEX_DOMAINS)
    complex_domains = sorted(set(scope.domains) & COMPLEX_DOMAINS)
    if complex_domains:
        warnings.append(
            "requested complex domains will be attempted with the normal KiCad path, "
            "but CopperWright does not validate domain-specific electrical, regulatory, "
            f"RF, thermal, or safety behavior: {', '.join(complex_domains)}"
        )
    if unknown:
        warnings.append(
            "no domain-specific validation is implemented for: " + ", ".join(unknown)
        )
    if scope.layers not in {2, 4}:
        reasons.append(
            "automated KiCad generation supports 2- or 4-copper-layer stackups"
        )
    if scope.max_voltage_v > 60:
        warnings.append(
            "declared voltage exceeds 60 V; CopperWright does not validate clearance, "
            "insulation, touch safety, or regulatory compliance"
        )
    if scope.max_power_w > 100 or scope.max_current_a > 10:
        warnings.append(
            "declared power/current is outside the locally validated evidence set; "
            "thermal and power-integrity behavior is not established"
        )
    if scope.risk_class not in {"prototype", "non_safety_critical", "unspecified"}:
        warnings.append(
            f"risk class {scope.risk_class} is recorded as user intent, not as a "
            "CopperWright safety or compliance claim"
        )
    return ScopeDecision(
        accepted=not reasons,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
    )


def assert_scope_supported(scope: Scope) -> None:
    """Reject only a concrete backend limitation, never a domain label."""
    decision = evaluate_scope(scope)
    if not decision.accepted:
        raise ValidationError(
            "design cannot reach the current KiCad generator: "
            + "; ".join(decision.reasons)
        )


def assert_supported(design: Design) -> None:
    assert_scope_supported(design.scope)
