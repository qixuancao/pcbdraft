"""Fail-closed KiCad compatibility policy for semantic generation and sync."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import PcbAgentError

SUPPORTED_KICAD_MAJOR = 10
TESTED_KICAD_VERSIONS = ("10.0.5",)
_VERSION_RE = re.compile(r"(?:KiCad\s+)?(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)


@dataclass(frozen=True)
class KiCadCompatibility:
    raw_version: str
    parsed_version: str | None
    supported: bool
    exact_tested: bool
    policy: str
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "raw_version": self.raw_version,
            "parsed_version": self.parsed_version,
            "supported": self.supported,
            "exact_tested": self.exact_tested,
            "policy": self.policy,
            "tested_versions": list(TESTED_KICAD_VERSIONS),
            "reason": self.reason,
        }


def evaluate_kicad_version(value: str) -> KiCadCompatibility:
    raw = value.strip() if isinstance(value, str) else ""
    match = _VERSION_RE.search(raw)
    policy = f"KiCad {SUPPORTED_KICAD_MAJOR}.x only; exact acceptance on {', '.join(TESTED_KICAD_VERSIONS)}"
    if match is None:
        return KiCadCompatibility(
            raw,
            None,
            False,
            False,
            policy,
            "version string could not be parsed",
        )
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    parsed = f"{major}.{minor}.{patch}"
    supported = major == SUPPORTED_KICAD_MAJOR
    exact = parsed in TESTED_KICAD_VERSIONS
    return KiCadCompatibility(
        raw,
        parsed,
        supported,
        exact,
        policy,
        None if supported else f"KiCad major {major} is outside the supported major",
    )


def assert_supported_kicad_version(value: str) -> KiCadCompatibility:
    compatibility = evaluate_kicad_version(value)
    if not compatibility.supported:
        raise PcbAgentError(
            "unsupported KiCad compatibility: "
            + (compatibility.reason or "unknown version")
            + f" ({compatibility.raw_version!r}; {compatibility.policy})"
        )
    return compatibility
