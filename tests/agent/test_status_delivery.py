from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from pcbdraft.agent.status_delivery import StatusDeliveryMixin


class _StatusHarness(StatusDeliveryMixin):
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.log_prefix = "[agent] "
        self._retry_status_buffer: list[tuple[str, str]] = []
        self._pending_fallback_notice = None
        self.api_mode = "anthropic_messages"

    def _emit_status(self, message: str) -> None:
        self.events.append(("status", message))

    def _emit_warning(self, message: str) -> None:
        self.events.append(("warn", message))

    def _vprint(self, message: str, *, force: bool = False) -> None:
        self.events.append(("vprint", f"{force}:{message}"))


class StatusDeliveryMixinTest(unittest.TestCase):
    def test_agent_keeps_status_methods_and_diagnostic_header_on_old_class(
        self,
    ) -> None:
        from pcbdraft.agent.loop import AIAgent

        self.assertTrue(issubclass(AIAgent, StatusDeliveryMixin))
        self.assertIs(AIAgent._safe_print, StatusDeliveryMixin._safe_print)
        self.assertIs(AIAgent._emit_stream_drop, StatusDeliveryMixin._emit_stream_drop)
        self.assertIs(
            AIAgent._STREAM_DIAG_HEADERS,
            StatusDeliveryMixin._STREAM_DIAG_HEADERS,
        )

    def test_buffer_flush_preserves_order_kind_and_clears_state(self) -> None:
        delivery = _StatusHarness()
        delivery._buffer_status("retrying")
        delivery._buffer_vprint("endpoint failed")
        delivery._retry_status_buffer.append(("warn", "degraded"))
        delivery._pending_fallback_notice = "switched provider"

        delivery._flush_status_buffer()

        self.assertEqual(
            delivery.events,
            [
                ("status", "retrying"),
                ("vprint", "True:[agent] endpoint failed"),
                ("warn", "degraded"),
            ],
        )
        self.assertEqual(delivery._retry_status_buffer, [])
        self.assertIsNone(delivery._pending_fallback_notice)

    def test_stream_diagnostics_capture_response_and_classify_parse_errors(
        self,
    ) -> None:
        delivery = _StatusHarness()
        delivery._STREAM_DIAG_HEADERS = ("x-request-id",)
        diag = delivery._stream_diag_init()
        response = SimpleNamespace(
            status_code=206,
            headers={"x-request-id": "request-123", "server": "hidden"},
        )

        delivery._stream_diag_capture_response(diag, response)

        self.assertEqual(diag["http_status"], 206)
        self.assertEqual(diag["headers"], {"x-request-id": "request-123"})
        self.assertTrue(
            delivery._is_provider_stream_parse_error(
                ValueError("expected ident at line 1 column 149")
            )
        )
        self.assertFalse(
            delivery._is_provider_stream_parse_error(
                json.JSONDecodeError("bad json", "{", 0)
            )
        )


if __name__ == "__main__":
    unittest.main()
