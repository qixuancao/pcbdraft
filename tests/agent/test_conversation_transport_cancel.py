from __future__ import annotations

import subprocess
import sys
import unittest


class NativeConversationTransportCancellationTests(unittest.TestCase):
    def assert_cancelled_transport(self, mode: str) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.agent.native_conversation_cancel_fixture",
                mode,
            ],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"NATIVE_TRANSPORT_CANCEL_OK:{mode}", result.stdout)

    def test_cancel_while_waiting_for_response_headers(self) -> None:
        self.assert_cancelled_transport("no_headers")

    def test_cancel_while_waiting_for_next_sse_event(self) -> None:
        self.assert_cancelled_transport("mid_sse")
