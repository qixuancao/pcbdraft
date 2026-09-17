"""Focused tests for the AIAgent activity and provider-state mixin."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from pcbdraft.agent import loop
from pcbdraft.agent.activity_tracking import ActivityTrackingMixin
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.session_activity import ActivityProvenance


class ActivityTrackingCompatibilityTests(unittest.TestCase):
    def test_agent_inherits_extracted_methods_without_wrappers(self) -> None:
        names = (
            "_touch_activity",
            "_persist_session_activity_if_due",
            "_reset_activity_labels_after_turn",
            "_capture_rate_limits",
            "get_rate_limit_state",
            "_capture_anthropic_response_headers",
            "_capture_credits",
            "_emit_credits_notices",
            "_credits_notices_enabled",
            "get_credits_state",
            "get_credits_spent_micros",
            "_check_openrouter_cache_status",
            "get_activity_summary",
        )

        self.assertTrue(issubclass(AIAgent, ActivityTrackingMixin))
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name), getattr(ActivityTrackingMixin, name)
                )


class ActivityTrackingBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)
        self.agent.session_id = "session-1"
        self.agent._session_db = MagicMock()
        self.agent._session_activity_last_persist_mono = 0.0
        self.agent._rate_limit_state = None
        self.agent._credits_state = None
        self.agent._credits_session_start_micros = None
        self.agent._credits_notices_enabled_cache = None
        self.agent._credits_latch = None
        self.agent._or_cache_hits = 0
        self.agent.provider = "openrouter"
        self.agent.model = "model-1"
        self.agent.base_url = "https://example.test/v1"

    def test_touch_persists_once_per_window_through_legacy_clock_paths(self) -> None:
        fake_time = SimpleNamespace(
            time=MagicMock(return_value=123.0),
            monotonic=MagicMock(side_effect=[100.0, 101.0]),
        )
        fake_environ = MagicMock()
        fake_environ.get.return_value = None
        fake_os = SimpleNamespace(environ=fake_environ)

        with patch.object(loop, "time", fake_time), patch.object(loop, "os", fake_os):
            self.agent._touch_activity(
                "  processing board  ",
                provenance=ActivityProvenance.AGENT_COMPRESSION,
            )
            self.agent._persist_session_activity_if_due()

        self.assertEqual(self.agent._last_activity_ts, 123.0)
        self.assertEqual(self.agent._last_activity_desc, "processing board")
        self.assertEqual(
            self.agent._last_activity_provenance,
            ActivityProvenance.AGENT_COMPRESSION,
        )
        self.agent._session_db.touch_session_activity.assert_called_once_with(
            "session-1",
            123.0,
            description="processing board",
            provenance=ActivityProvenance.AGENT_COMPRESSION,
        )
        fake_environ.get.assert_called_once_with("PCBDRAFT_RUNTIME_KANBAN_TASK")

    def test_session_activity_write_failure_uses_legacy_debug_logger(self) -> None:
        self.agent._last_activity_ts = 123.0
        self.agent._last_activity_desc = "working"
        self.agent._last_activity_provenance = ActivityProvenance.UNKNOWN
        self.agent._session_db.touch_session_activity.side_effect = RuntimeError(
            "database busy"
        )
        fake_time = SimpleNamespace(monotonic=MagicMock(return_value=100.0))

        with (
            patch.object(loop, "time", fake_time),
            patch.object(loop.logger, "debug") as debug,
        ):
            self.agent._persist_session_activity_if_due()

        debug.assert_called_once_with(
            "session activity heartbeat write failed (ignored)",
            exc_info=True,
        )

    def test_activity_reset_clears_labels_but_keeps_timestamp(self) -> None:
        self.agent._last_activity_ts = 123.0
        self.agent._last_activity_desc = "working"
        self.agent._last_activity_provenance = ActivityProvenance.AGENT_COMPRESSION

        self.agent._reset_activity_labels_after_turn()

        self.assertEqual(self.agent._last_activity_ts, 123.0)
        self.assertEqual(self.agent._last_activity_desc, "")
        self.assertEqual(
            self.agent._last_activity_provenance, ActivityProvenance.UNKNOWN
        )
        self.agent._session_db.clear_session_activity_labels.assert_called_once_with(
            "session-1"
        )

    def test_rate_limit_and_anthropic_header_capture(self) -> None:
        response = SimpleNamespace(headers={"x-ratelimit-limit": "100"})
        state = object()
        with patch(
            "pcbdraft.agent.rate_limit_tracker.parse_rate_limit_headers",
            return_value=state,
        ) as parse:
            self.agent._capture_rate_limits(response)

        parse.assert_called_once_with(response.headers, provider="openrouter")
        self.assertIs(self.agent.get_rate_limit_state(), state)

        self.agent._capture_rate_limits = MagicMock()
        self.agent._capture_credits = MagicMock()
        self.agent._capture_anthropic_response_headers(response)
        self.agent._capture_rate_limits.assert_called_once_with(response)
        self.agent._capture_credits.assert_called_once_with(response)

    def test_credit_capture_keeps_state_and_legacy_env_log_patches(self) -> None:
        state = SimpleNamespace(
            remaining_micros=800,
            remaining_usd="$0.0008",
            paid_access=True,
            denominator_kind="grant",
            used_fraction=0.2,
            age_seconds=2.0,
            disabled_reason=None,
        )
        response = SimpleNamespace(headers={"x-nous-credits-remaining": "800"})
        self.agent._credits_session_start_micros = 1000
        self.agent._emit_credits_notices = MagicMock()
        fake_os = SimpleNamespace(environ={"PCBDRAFT_RUNTIME_DEV_CREDITS": "enabled"})

        with (
            patch.object(loop, "os", fake_os),
            patch.object(loop, "is_truthy_value", return_value=True) as truthy,
            patch.object(loop.logger, "info") as info,
            patch(
                "pcbdraft.model.credits_tracker.dev_fixture_credits_state",
                return_value=None,
            ),
            patch(
                "pcbdraft.model.credits_tracker.parse_credits_headers",
                return_value=state,
            ) as parse,
        ):
            self.agent._capture_credits(response)

        truthy.assert_called_once_with("enabled")
        parse.assert_called_once_with(response.headers, provider="openrouter")
        self.assertIs(self.agent.get_credits_state(), state)
        self.assertEqual(self.agent.get_credits_spent_micros(), 200)
        self.agent._emit_credits_notices.assert_called_once_with()
        info.assert_called_once()

    def test_credit_notices_clear_before_show_and_cache_config(self) -> None:
        self.agent.notice_callback = object()
        self.agent.notice_clear_callback = object()
        self.agent._credits_state = object()
        timeline = MagicMock()
        self.agent._emit_notice_clear = timeline.clear
        self.agent._emit_notice = timeline.show

        with patch(
            "pcbdraft.model.configuration.load_config",
            return_value={"display": {"credits_notices": True}},
        ) as load_config:
            self.assertTrue(self.agent._credits_notices_enabled())
            self.assertTrue(self.agent._credits_notices_enabled())
        load_config.assert_called_once_with()

        with (
            patch(
                "pcbdraft.model.credits_tracker.new_credits_latch",
                return_value={"fresh": True},
            ),
            patch(
                "pcbdraft.model.credits_tracker.is_free_tier_model",
                return_value=False,
            ),
            patch(
                "pcbdraft.model.credits_tracker.evaluate_credits_notices",
                return_value=(["show"], ["clear"]),
            ),
        ):
            self.agent._emit_credits_notices()

        self.assertEqual(timeline.mock_calls, [call.clear("clear"), call.show("show")])

    def test_credit_notice_failure_uses_legacy_logger_patch(self) -> None:
        self.agent.notice_callback = object()
        self.agent.notice_clear_callback = None
        self.agent._credits_notices_enabled_cache = True
        self.agent._credits_state = object()
        self.agent._credits_latch = {}

        with (
            patch(
                "pcbdraft.model.credits_tracker.is_free_tier_model",
                side_effect=RuntimeError("pricing unavailable"),
            ),
            patch.object(loop.logger, "warning") as warning,
        ):
            self.agent._emit_credits_notices()

        warning.assert_called_once_with(
            "credits notice evaluation/emit failed", exc_info=True
        )

    def test_openrouter_cache_and_activity_summary(self) -> None:
        with patch.object(loop.logger, "info") as info:
            self.agent._check_openrouter_cache_status(
                SimpleNamespace(headers={"x-openrouter-cache-status": "hit"})
            )
        self.assertEqual(self.agent._or_cache_hits, 1)
        info.assert_called_once_with("OpenRouter response cache HIT (total: %d)", 1)

        self.agent._last_activity_ts = 123.0
        self.agent._last_activity_desc = "working"
        self.agent._last_activity_provenance = ActivityProvenance.UNKNOWN
        self.agent._current_tool = "pcb_generate_candidate"
        self.agent._api_call_count = 2
        self.agent.max_iterations = 5
        self.agent.iteration_budget = SimpleNamespace(used=3, max_total=8)

        summary = self.agent.get_activity_summary()
        self.assertEqual(summary["last_activity_at"], 123.0)
        self.assertEqual(summary["last_activity_description"], "working")
        self.assertEqual(summary["current_tool"], "pcb_generate_candidate")
        self.assertEqual(summary["budget_used"], 3)


if __name__ == "__main__":
    unittest.main()
