from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


class _RaisingClient:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("synthetic SDK close failure")


class _OwnedSessionDB:
    def __init__(self) -> None:
        self.ended: list[tuple[str, str]] = []
        self.close_calls = 0

    def end_session(self, session_id: str, reason: str) -> None:
        self.ended.append((session_id, reason))

    def close(self) -> None:
        self.close_calls += 1


class AnthropicClientLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime_home = Path(self.temporary.name) / "runtime"
        self.environment = patch.dict(
            os.environ, {"PCBDRAFT_RUNTIME_HOME": str(self.runtime_home)}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @staticmethod
    def _agent() -> Any:
        from pcbdraft.agent.loop import AIAgent

        return AIAgent(
            base_url="http://127.0.0.1:9",
            api_key="test-key",
            provider="anthropic",
            api_mode="anthropic_messages",
            model="claude-sonnet-4-6",
            enabled_toolsets=["pcbdraft"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
        )

    def test_close_releases_shared_anthropic_client_and_clears_reference(self) -> None:
        agent = self._agent()
        client = agent._anthropic_client
        self.addCleanup(client.close)

        self.assertFalse(client.is_closed())
        agent.close()

        self.assertTrue(client.is_closed())
        self.assertIsNone(agent._anthropic_client)
        agent.close()
        self.assertTrue(client.is_closed())

    def test_anthropic_close_failure_does_not_skip_later_cleanup(self) -> None:
        agent = self._agent()
        original_client = agent._anthropic_client
        original_client.close()
        raising_client = _RaisingClient()
        session_db = _OwnedSessionDB()
        agent._anthropic_client = raising_client
        agent._session_db = session_db
        agent._owns_session_db = True
        agent._end_session_on_close = True
        agent._session_messages = [{"role": "user", "content": "discard on close"}]

        agent.close()

        self.assertEqual(raising_client.close_calls, 1)
        self.assertIsNone(agent._anthropic_client)
        self.assertEqual(agent._session_messages, [])
        self.assertEqual(session_db.ended, [(agent.session_id, "agent_close")])
        self.assertEqual(session_db.close_calls, 1)
        self.assertFalse(agent._owns_session_db)


if __name__ == "__main__":
    unittest.main()
