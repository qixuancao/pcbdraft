"""Acceptance-scope policy for automated PCB workflows."""

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
REJECTED_DOMAINS = {
    "ddr",
    "pcie",
    "serdes",
    "rf",
    "mains",
    "medical",
    "aviation",
    "safety_critical",
    "high_power",
}


@dataclass(frozen=True)
class ScopeDecision:
    accepted: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"accepted": self.accepted, "reasons": list(self.reasons)}


def evaluate_scope(scope: Scope) -> ScopeDecision:
    reasons: list[str] = []
    unknown = sorted(set(scope.domains) - SUPPORTED_DOMAINS - REJECTED_DOMAINS)
    rejected = sorted(set(scope.domains) & REJECTED_DOMAINS)
    if rejected:
        reasons.append(f"unsupported high-risk domains: {', '.join(rejected)}")
    if unknown:
        reasons.append(
            f"domains require an explicit policy extension: {', '.join(unknown)}"
        )
    if not 2 <= scope.layers <= 4:
        reasons.append("automated generation supports only 2-4 copper layers")
    if scope.max_voltage_v > 60:
        reasons.append(
            "declared maximum voltage exceeds the 60 VDC low-voltage boundary"
        )
    if scope.max_power_w > 100 or scope.max_current_a > 10:
        reasons.append(
            "declared power/current exceeds the bounded simple-control scope"
        )
    if scope.risk_class not in {"prototype", "non_safety_critical"}:
        reasons.append(
            "automated generation is limited to prototype/non-safety-critical use"
        )
    if not set(scope.domains) & {"low_voltage_mcu", "sensor", "simple_control"}:
        reasons.append(
            "scope must include a supported low-voltage MCU, sensor, or control domain"
        )
    return ScopeDecision(accepted=not reasons, reasons=tuple(reasons))


def assert_supported(design: Design) -> None:
    decision = evaluate_scope(design.scope)
    if not decision.accepted:
        raise ValidationError(
            "design is outside the automated acceptance scope: "
            + "; ".join(decision.reasons)
        )
