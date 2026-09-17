"""Track agent activity, provider limits, credits, and cache observations."""

# Observation and provider-metadata handling must never break the agent loop.
# ruff: noqa: BLE001, S110, UP031

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from pcbdraft.agent.session_activity import ActivityProvenance
from pcbdraft.core.runtime_utils import is_truthy_value as _default_is_truthy_value

logger = logging.getLogger(__name__)

_time_hook: Callable[[], float]
_monotonic_hook: Callable[[], float]
_env_get_hook: Callable[[str], str | None]
_is_truthy_value_hook: Callable[[Any], bool]
_debug_hook: Callable[..., None]
_info_hook: Callable[..., None]
_warning_hook: Callable[..., None]


def configure_activity_tracking_runtime(
    *,
    now: Callable[[], float] | None = None,
    monotonic: Callable[[], float] | None = None,
    env_get: Callable[[str], str | None] | None = None,
    is_truthy_value: Callable[[Any], bool] | None = None,
    debug: Callable[..., None] | None = None,
    info: Callable[..., None] | None = None,
    warning: Callable[..., None] | None = None,
) -> None:
    """Inject late-bound dependencies owned by the legacy agent module."""
    global _time_hook
    global _monotonic_hook
    global _env_get_hook
    global _is_truthy_value_hook
    global _debug_hook
    global _info_hook
    global _warning_hook

    if now is not None:
        _time_hook = now
    if monotonic is not None:
        _monotonic_hook = monotonic
    if env_get is not None:
        _env_get_hook = env_get
    if is_truthy_value is not None:
        _is_truthy_value_hook = is_truthy_value
    if debug is not None:
        _debug_hook = debug
    if info is not None:
        _info_hook = info
    if warning is not None:
        _warning_hook = warning


class ActivityTrackingMixin:
    """Provide best-effort activity and provider-state observations."""

    def _touch_activity(
        self,
        desc: str,
        *,
        provenance: ActivityProvenance | None = None,
        force_persist: bool = False,
    ) -> None:
        """Update current activity and its durable SessionDB projection."""
        from pcbdraft.agent.session_activity import (
            bound_activity_description,
            normalize_activity_provenance,
            reset_session_activity_persist_window,
        )

        self._last_activity_ts = _time_hook()
        self._last_activity_desc = bound_activity_description(desc)
        self._last_activity_provenance = normalize_activity_provenance(provenance)
        if _env_get_hook("PCBDRAFT_RUNTIME_KANBAN_TASK"):
            try:
                from pcbdraft.tools.kanban_tools import (
                    heartbeat_current_worker_from_env,
                    inject_new_comments_from_env,
                )

                heartbeat_current_worker_from_env()
                inject_new_comments_from_env(self)
            except Exception:
                pass
        if force_persist:
            reset_session_activity_persist_window(self)
        self._persist_session_activity_if_due()

    def _persist_session_activity_if_due(self) -> None:
        """Rate-limit durable activity heartbeat writes for SessionDB readers."""
        session_id = getattr(self, "session_id", None)
        session_db = getattr(self, "_session_db", None)
        if not session_id or session_db is None:
            return
        touch = getattr(session_db, "touch_session_activity", None)
        if not callable(touch):
            return
        from pcbdraft.agent.session_activity import (
            SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS,
            normalize_activity_provenance,
        )

        now_mono = _monotonic_hook()
        last_mono = getattr(self, "_session_activity_last_persist_mono", 0.0)
        if (now_mono - last_mono) < SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS:
            return
        self._session_activity_last_persist_mono = now_mono
        try:
            touch(
                session_id,
                getattr(self, "_last_activity_ts", None),
                description=getattr(self, "_last_activity_desc", None),
                provenance=normalize_activity_provenance(
                    getattr(self, "_last_activity_provenance", None)
                ),
            )
        except Exception:
            _debug_hook(
                "session activity heartbeat write failed (ignored)",
                exc_info=True,
            )

    def _reset_activity_labels_after_turn(self) -> None:
        """Clear mid-turn labels while retaining the activity timestamp."""
        self._last_activity_desc = ""
        self._last_activity_provenance = ActivityProvenance.UNKNOWN
        session_id = getattr(self, "session_id", None)
        session_db = getattr(self, "_session_db", None)
        if not session_id or session_db is None:
            return
        clear = getattr(session_db, "clear_session_activity_labels", None)
        if not callable(clear):
            return
        try:
            clear(session_id)
        except Exception:
            pass

    def _capture_rate_limits(self, http_response: Any) -> None:
        """Parse and cache provider rate-limit response headers."""
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        try:
            from pcbdraft.agent.rate_limit_tracker import parse_rate_limit_headers

            state = parse_rate_limit_headers(headers, provider=self.provider)
            if state is not None:
                self._rate_limit_state = state
        except Exception:
            pass

    def get_rate_limit_state(self):
        """Return the last captured rate-limit state."""
        return self._rate_limit_state

    def _capture_anthropic_response_headers(self, http_response: Any) -> None:
        """Capture rate limits and credits from an Anthropic response."""
        self._capture_rate_limits(http_response)
        self._capture_credits(http_response)

    def _capture_credits(self, http_response: Any) -> None:
        """Parse credits headers, retain fresh state, and emit notices."""
        try:
            from pcbdraft.model.credits_tracker import dev_fixture_credits_state

            fixture = dev_fixture_credits_state()
        except Exception:
            fixture = None
        if fixture is not None:
            self._credits_state = fixture
            if self._credits_session_start_micros is None:
                self._credits_session_start_micros = fixture.remaining_micros
            latch = getattr(self, "_credits_latch", None)
            if isinstance(latch, dict):
                latch["seen_below_90"] = True
            used = fixture.used_fraction
            _info_hook(
                "credits ▸ [FIXTURE] remaining=%d (%s) · paid=%s · denom=%s · used=%s "
                "(real headers bypassed — `echo clear` / unset PCBDRAFT_RUNTIME_DEV_CREDITS_FIXTURE to restore)",
                fixture.remaining_micros,
                fixture.remaining_usd or "?",
                fixture.paid_access,
                fixture.denominator_kind,
                ("%.0f%%" % (used * 100)) if used is not None else "n/a",
            )
            self._emit_credits_notices()
            return
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        dev_enabled = _is_truthy_value_hook(
            _env_get_hook("PCBDRAFT_RUNTIME_DEV_CREDITS")
        )

        try:
            from pcbdraft.model.credits_tracker import parse_credits_headers

            state = parse_credits_headers(headers, provider=self.provider)
        except Exception:
            return
        if state is None:
            if dev_enabled:
                _info_hook(
                    "credits ▸ response had no valid x-nous-credits-* headers "
                    "(miss — producer off / non-Nous path / >TTL stale)"
                )
            return

        self._credits_state = state
        if self._credits_session_start_micros is None:
            self._credits_session_start_micros = state.remaining_micros
        if dev_enabled:
            spent = self.get_credits_spent_micros()
            used = state.used_fraction
            _info_hook(
                "credits ▸ remaining=%d (%s) · paid=%s · denom=%s · used=%s "
                "· Δspent=%s · age=%s%s",
                state.remaining_micros,
                state.remaining_usd or "?",
                state.paid_access,
                state.denominator_kind,
                ("%.0f%%" % (used * 100)) if used is not None else "n/a",
                ("%.1f¢" % (spent / 10000)) if spent is not None else "n/a",
                ("%.0fs" % state.age_seconds)
                if state.age_seconds != float("inf")
                else "n/a",
                (" · disabled=%s" % state.disabled_reason)
                if state.disabled_reason
                else "",
            )
        self._emit_credits_notices()

    def _emit_credits_notices(self) -> None:
        """Evaluate credits thresholds and emit clears before notices."""
        if (
            getattr(self, "notice_callback", None) is None
            and getattr(self, "notice_clear_callback", None) is None
        ):
            return
        if not self._credits_notices_enabled():
            return
        state = getattr(self, "_credits_state", None)
        if state is None:
            return
        try:
            from pcbdraft.model.credits_tracker import (
                evaluate_credits_notices,
                is_free_tier_model,
                new_credits_latch,
            )

            latch = getattr(self, "_credits_latch", None)
            if latch is None:
                latch = self._credits_latch = new_credits_latch()
            model_is_free = is_free_tier_model(
                getattr(self, "model", "") or "",
                getattr(self, "base_url", "") or "",
            )
            to_show, to_clear = evaluate_credits_notices(
                state, latch, model_is_free=model_is_free
            )
            for key in to_clear:
                self._emit_notice_clear(key)
            for notice in to_show:
                self._emit_notice(notice)
        except Exception:
            _warning_hook("credits notice evaluation/emit failed", exc_info=True)

    def _credits_notices_enabled(self) -> bool:
        """Read and cache the credits-notice display preference."""
        cached = getattr(self, "_credits_notices_enabled_cache", None)
        if cached is not None:
            return cached
        enabled = True
        try:
            from pcbdraft.model.configuration import load_config

            config = load_config() or {}
            display = config.get("display") if isinstance(config, dict) else None
            if isinstance(display, dict) and "credits_notices" in display:
                enabled = bool(display.get("credits_notices"))
        except Exception:
            enabled = True
        self._credits_notices_enabled_cache = enabled
        return enabled

    def get_credits_state(self):
        """Return the last captured credits state."""
        return self._credits_state

    def get_credits_spent_micros(self):
        """Return session-cumulative credits spent in micros, when known."""
        if self._credits_session_start_micros is None or self._credits_state is None:
            return None
        return self._credits_session_start_micros - self._credits_state.remaining_micros

    def _check_openrouter_cache_status(self, http_response: Any) -> None:
        """Capture OpenRouter cache hits for usage reporting."""
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        try:
            status = headers.get("x-openrouter-cache-status")
            if not status:
                return
            if status.upper() == "HIT":
                self._or_cache_hits += 1
                _info_hook(
                    "OpenRouter response cache HIT (total: %d)", self._or_cache_hits
                )
            else:
                _debug_hook("OpenRouter response cache %s", status.upper())
        except Exception:
            pass

    def get_activity_summary(self) -> dict:
        """Return the shared activity snapshot plus agent loop counters."""
        from pcbdraft.agent.session_activity import build_activity_snapshot

        provenance = getattr(self, "_last_activity_provenance", None)
        if provenance is None:
            provenance = ActivityProvenance.UNKNOWN
        return build_activity_snapshot(
            last_activity_at=getattr(self, "_last_activity_ts", None),
            last_activity_description=getattr(self, "_last_activity_desc", None) or "",
            last_activity_provenance=provenance,
            extra={
                "current_tool": self._current_tool,
                "api_call_count": self._api_call_count,
                "max_iterations": self.max_iterations,
                "budget_used": self.iteration_budget.used,
                "budget_max": self.iteration_budget.max_total,
            },
        )


_time_hook = time.time
_monotonic_hook = time.monotonic
_env_get_hook = os.environ.get
_is_truthy_value_hook = _default_is_truthy_value
_debug_hook = logger.debug
_info_hook = logger.info
_warning_hook = logger.warning
