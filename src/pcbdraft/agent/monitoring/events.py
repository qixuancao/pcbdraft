"""Typed gateway monitoring events.

Content-free service-health and redacted diagnostic events for the gateway
daemon. These are the only event shapes the monitoring plane emits: no
prompts, messages, tool args/results, session history, or usage analytics.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


def _now_ns() -> int:
    return time.time_ns()


@dataclass(slots=True)
class GatewayHealthEvent:
    """Content-free gateway health snapshot or lifecycle event."""

    name: str
    gateway_state: str | None = None
    old_state: str | None = None
    new_state: str | None = None
    exit_reason: str | None = None
    restart_requested: bool | None = None
    active_agents: int = 0
    gateway_busy: bool = False
    gateway_drainable: bool = False
    platform_count: int = 0
    fatal_platform_count: int = 0
    profile: str | None = None
    install_id: str | None = None
    version: str | None = None
    supervision_mode: str | None = None
    pid: int | None = None
    ts_ns: int = field(default_factory=_now_ns)

    def to_dict(self) -> dict[str, Any]:
        return {"event": "gateway_health", **asdict(self)}


@dataclass(slots=True)
class GatewayDiagnosticEvent:
    """Redacted gateway diagnostic event for operator-owned observability."""

    name: str
    subsystem: str
    error_class: str = "unknown"
    error_code: str | None = None
    platform: str | None = None
    old_state: str | None = None
    new_state: str | None = None
    profile: str | None = None
    version: str | None = None
    severity: str = "warning"
    ts_ns: int = field(default_factory=_now_ns)
    source_logger: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"event": "gateway_diagnostic", **asdict(self)}


@dataclass(slots=True)
class CronExecutionEvent:
    """Content-free durable cron execution lifecycle projection."""

    status: str
    job_key: str
    source: str = "unknown"
    duration_ms: int | None = None
    delivery_outcome: str | None = None
    error_class: str | None = None
    ts_ns: int = field(default_factory=_now_ns)

    def to_dict(self) -> dict[str, Any]:
        return {"event": "cron_execution", **asdict(self)}


__all__ = [
    "GatewayHealthEvent",
    "GatewayDiagnosticEvent",
    "CronExecutionEvent",
]
