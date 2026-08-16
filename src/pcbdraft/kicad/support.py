"""Fail-closed KiCad support policy for semantic generation and sync."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pcbdraft.core.errors import PCBDraftError

SUPPORTED_KICAD_MIN = (10, 0, 0)
SUPPORTED_KICAD_MAX_EXCLUSIVE = (10, 1, 0)
TESTED_KICAD_VERSIONS = ("10.0.5",)
_VERSION_RE = re.compile(r"(?:KiCad\s+)?(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)
_PRERELEASE_RE = re.compile(
    r"(?:^|[-+~.\s])(?:alpha|beta|dev|nightly|rc)\d*", re.IGNORECASE
)


@dataclass(frozen=True)
class KiCadSupport:
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


def evaluate_kicad_version(value: str) -> KiCadSupport:
    raw = value.strip() if isinstance(value, str) else ""
    match = _VERSION_RE.search(raw)
    policy = (
        "compatible stable range: >=10.0.0,<10.1.0; exact acceptance: "
        + ", ".join(TESTED_KICAD_VERSIONS)
    )
    if match is None:
        return KiCadSupport(
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
    exact = parsed in TESTED_KICAD_VERSIONS
    version = (major, minor, patch)
    prerelease = _PRERELEASE_RE.search(raw) is not None
    supported = (
        SUPPORTED_KICAD_MIN <= version < SUPPORTED_KICAD_MAX_EXCLUSIVE
        and not prerelease
    )
    return KiCadSupport(
        raw,
        parsed,
        supported,
        exact,
        policy,
        (
            None
            if exact
            else f"KiCad {parsed} is compatible but not this release's exact acceptance baseline"
        )
        if supported
        else (
            f"KiCad {parsed} is a prerelease build"
            if prerelease
            else f"KiCad {parsed} is outside the supported stable 10.0.x series"
        ),
    )


def assert_supported_kicad_version(value: str) -> KiCadSupport:
    support = evaluate_kicad_version(value)
    if not support.supported:
        raise PCBDraftError(
            "unsupported KiCad version: "
            + (support.reason or "unknown version")
            + f" ({support.raw_version!r}; {support.policy})"
        )
    return support
