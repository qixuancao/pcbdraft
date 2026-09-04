"""Fail-closed KiCad support policy for semantic generation and sync."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.process import run_command

SUPPORTED_KICAD_MIN = (10, 0, 0)
SUPPORTED_KICAD_MAX_EXCLUSIVE = (10, 1, 0)
KICAD_ACCEPTANCE_BASELINE = "10.0.5"
TESTED_KICAD_VERSIONS = (KICAD_ACCEPTANCE_BASELINE,)
CAPABILITY_PROBES = {
    "pcb_export_svg": (
        ("pcb", "export", "svg", "--help"),
        ("--fit-page-to-board", "--mode-single"),
        "primary exact 2D canvas",
    ),
    "pcb_render_3d": (
        ("pcb", "render", "--help"),
        ("--quality", "--side"),
        "optional 3D preview",
    ),
    "erc_json": (
        ("sch", "erc", "--help"),
        ("--format", "--exit-code-violations"),
        "final ERC evidence",
    ),
    "drc_json": (
        ("pcb", "drc", "--help"),
        ("--format", "--exit-code-violations"),
        "final DRC evidence",
    ),
}
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
            "acceptance_baseline": KICAD_ACCEPTANCE_BASELINE,
            "compatible_series": "10.0.x stable",
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


def probe_kicad_capabilities(
    executable: str | None,
    *,
    timeout: float = 5.0,
    runner: Callable[..., Any] = run_command,
) -> dict[str, dict[str, object]]:
    """Probe each used CLI surface independently from bounded help output."""

    result: dict[str, dict[str, object]] = {}
    for name, (arguments, tokens, purpose) in CAPABILITY_PROBES.items():
        argv = [executable, *arguments] if executable else None
        if executable is None:
            result[name] = {
                "available": False,
                "purpose": purpose,
                "argv": None,
                "limitation": "kicad-cli is unavailable",
            }
            continue
        try:
            completed = runner(
                argv,
                cwd=None,
                timeout=timeout,
                max_output_bytes=256 * 1024,
            )
        except (OSError, PCBDraftError) as exc:
            result[name] = {
                "available": False,
                "purpose": purpose,
                "argv": argv,
                "limitation": type(exc).__name__,
            }
            continue
        output = f"{completed.stdout}\n{completed.stderr}"
        missing = [token for token in tokens if token not in output]
        available = (
            completed.returncode == 0
            and not completed.timed_out
            and not completed.output_limited
            and not missing
        )
        result[name] = {
            "available": available,
            "purpose": purpose,
            "argv": argv,
            "exit_code": completed.returncode,
            "timed_out": completed.timed_out,
            "output_limited": completed.output_limited,
            "missing_markers": missing,
            "limitation": (
                None
                if available
                else "command unavailable or required options are missing"
            ),
        }
    return result
