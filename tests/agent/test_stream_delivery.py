from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from pcbdraft.agent.stream_delivery import StreamDeliveryMixin


class _PassthroughScrubber:
    def feed(self, text: str) -> str:
        return text


class _DeliveryHarness(StreamDeliveryMixin):
    def __init__(self) -> None:
        self.stream_delta_callback = None
        self._stream_callback = None
        self.reasoning_callback = None
        self.tool_gen_callback = None
        self.interim_assistant_callback = None
        self._stream_think_scrubber = _PassthroughScrubber()
        self._stream_context_scrubber = _PassthroughScrubber()
        self._current_streamed_assistant_text = ""
        self._stream_needs_break = False
        self._stream_writer_lock = threading.Lock()
        self._stream_writer_token = 0
        self._stream_writer_tls = threading.local()
        self._stream_writer_dropped = 0
        self._delivered_interim_texts: set[str] = set()
        self._current_turn_id = "turn-1"
        self._api_call_count = 2
        self.session_id = "session-1"
        self.model = "model-1"
        self.provider = "provider-1"
        self.platform = "cli"
        self.show_commentary = True

    @staticmethod
    def _strip_think_blocks(text: str) -> str:
        return text


class StreamDeliveryMixinTest(unittest.TestCase):
    def test_agent_keeps_stream_delivery_methods_through_mixin(self) -> None:
        from pcbdraft.agent.loop import AIAgent

        self.assertTrue(issubclass(AIAgent, StreamDeliveryMixin))
        self.assertIs(
            AIAgent._fire_stream_delta, StreamDeliveryMixin._fire_stream_delta
        )

    def test_text_delta_reaches_callbacks_and_tracking(self) -> None:
        delivery = _DeliveryHarness()
        seen: list[str] = []
        delivery.stream_delta_callback = seen.append
        delivery._stream_needs_break = True

        with patch(
            "pcbdraft.agent.plugin_stream_hooks.enqueue_plugin_stream_hook"
        ) as enqueue:
            delivery._fire_stream_delta("hello")

        self.assertEqual(seen, ["\n\nhello"])
        self.assertEqual(delivery._current_streamed_assistant_text, "\n\nhello")
        enqueue.assert_called_once_with(
            "on_stream_delta",
            turn_id="turn-1",
            iteration=2,
            session_id="session-1",
            model="model-1",
            provider="provider-1",
            surface="cli",
            delta="\n\nhello",
            kind="text",
        )

    def test_superseded_writer_cannot_deliver_text(self) -> None:
        delivery = _DeliveryHarness()
        seen: list[str] = []
        delivery.stream_delta_callback = seen.append
        delivery._stream_writer_tls.token = 1
        delivery._stream_writer_token = 2

        delivery._fire_stream_delta("stale")

        self.assertEqual(seen, [])
        self.assertEqual(delivery._current_streamed_assistant_text, "")
        self.assertEqual(delivery._stream_writer_dropped, 1)

    def test_codex_commentary_is_delivered_once(self) -> None:
        delivery = _DeliveryHarness()
        seen: list[tuple[str, bool]] = []
        delivery.interim_assistant_callback = lambda text, *, already_streamed: (
            seen.append((text, already_streamed))
        )
        message = {
            "codex_message_items": [
                {
                    "type": "message",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Checking board"}],
                }
            ],
            "content": "final answer must stay hidden",
        }

        with patch("pcbdraft.agent.plugin_stream_hooks.enqueue_plugin_stream_hook"):
            delivery._emit_interim_assistant_message(message)
            delivery._emit_interim_assistant_message(message)

        self.assertEqual(seen, [("Checking board", False)])

    def test_tool_generation_callback_remains_best_effort(self) -> None:
        delivery = _DeliveryHarness()
        seen: list[str] = []
        delivery.tool_gen_callback = seen.append
        delivery._fire_tool_gen_started("write_file")
        self.assertEqual(seen, ["write_file"])


if __name__ == "__main__":
    unittest.main()
