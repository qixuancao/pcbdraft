from __future__ import annotations

import asyncio
import math
import os
import unittest
from unittest.mock import patch

from pcbdraft.agent.deadline import (
    MAX_SAFE_TIMEOUT_S,
    _consume_abandoned,
    clamp_timeout,
    resolve_timeout,
)


class DeadlineTimeoutTests(unittest.TestCase):
    def test_clamp_preserves_unbounded_and_platform_safe_boundaries(self) -> None:
        self.assertIsNone(clamp_timeout(None))
        self.assertIsNone(clamp_timeout(0.0))
        self.assertIsNone(clamp_timeout(-1.0))
        self.assertIsNone(clamp_timeout(math.nan))
        self.assertEqual(clamp_timeout(math.inf), MAX_SAFE_TIMEOUT_S)
        self.assertEqual(
            clamp_timeout(10**1000),  # type: ignore[arg-type]
            MAX_SAFE_TIMEOUT_S,
        )
        self.assertIsNone(clamp_timeout(-(10**1000)))  # type: ignore[arg-type]

    def test_config_timeout_wins_over_environment_and_default(self) -> None:
        with (
            patch(
                "pcbdraft.agent.deadline._timeouts_section",
                return_value={"tools": {"batch": 9.0}},
            ),
            patch.dict(os.environ, {"PCBDRAFT_TEST_TIMEOUT": "4"}),
        ):
            self.assertEqual(
                resolve_timeout(
                    "tools.batch", default=2.0, env_var="PCBDRAFT_TEST_TIMEOUT"
                ),
                9.0,
            )

    def test_invalid_and_nan_config_values_fall_back_to_environment(self) -> None:
        for invalid in (True, "not-a-number", math.nan):
            with (
                self.subTest(invalid=invalid),
                patch(
                    "pcbdraft.agent.deadline._timeouts_section",
                    return_value={"tools": {"batch": invalid}},
                ),
                patch.dict(os.environ, {"PCBDRAFT_TEST_TIMEOUT": "4"}),
            ):
                self.assertEqual(
                    resolve_timeout(
                        "tools.batch",
                        default=2.0,
                        env_var="PCBDRAFT_TEST_TIMEOUT",
                    ),
                    4.0,
                )

    def test_overflowing_config_respects_its_sign_without_falling_through(self) -> None:
        for configured, expected in (
            (10**1000, MAX_SAFE_TIMEOUT_S),
            (-(10**1000), None),
        ):
            with (
                self.subTest(configured_positive=configured > 0),
                patch(
                    "pcbdraft.agent.deadline._timeouts_section",
                    return_value={"tools": {"batch": configured}},
                ),
                patch.dict(os.environ, {"PCBDRAFT_TEST_TIMEOUT": "4"}),
            ):
                self.assertEqual(
                    resolve_timeout(
                        "tools.batch",
                        default=2.0,
                        env_var="PCBDRAFT_TEST_TIMEOUT",
                    ),
                    expected,
                )

    def test_invalid_environment_falls_back_to_clamped_default(self) -> None:
        with (
            patch("pcbdraft.agent.deadline._timeouts_section", return_value={}),
            patch.dict(os.environ, {"PCBDRAFT_TEST_TIMEOUT": "invalid"}),
        ):
            self.assertEqual(
                resolve_timeout(
                    "tools.batch",
                    default=10**1000,  # type: ignore[arg-type]
                    env_var="PCBDRAFT_TEST_TIMEOUT",
                ),
                MAX_SAFE_TIMEOUT_S,
            )


class AbandonedFutureTests(unittest.IsolatedAsyncioTestCase):
    async def test_consume_handles_cancelled_pending_and_failed_futures(self) -> None:
        loop = asyncio.get_running_loop()

        cancelled = loop.create_future()
        cancelled.cancel()
        _consume_abandoned(cancelled)

        pending = loop.create_future()
        _consume_abandoned(pending)
        pending.cancel()

        failure = RuntimeError("expected test failure")
        failed = loop.create_future()
        failed.set_exception(failure)
        _consume_abandoned(failed)
        self.assertIs(failed.exception(), failure)


if __name__ == "__main__":
    unittest.main()
