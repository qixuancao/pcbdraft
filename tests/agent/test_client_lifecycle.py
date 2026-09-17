"""Focused tests for the AIAgent client and resource lifecycle mixin."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop
from pcbdraft.agent.client_lifecycle import ClientLifecycleMixin
from pcbdraft.agent.loop import AIAgent


class ClientLifecycleCompatibilityTests(unittest.TestCase):
    def test_agent_inherits_extracted_methods_without_wrappers(self) -> None:
        self.assertTrue(issubclass(AIAgent, ClientLifecycleMixin))
        for name in (
            "release_clients",
            "close",
            "_close_cached_request_openai_client",
            "_close_cached_request_anthropic_client",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name), getattr(ClientLifecycleMixin, name)
                )


class ClientLifecycleBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)
        self.agent.session_id = "session-1"
        self.agent._active_children_lock = threading.RLock()
        self.agent._active_children = []
        self.agent._client_lock = threading.RLock()

    def test_release_clients_keeps_session_resources_and_retires_clients(self) -> None:
        first_child = MagicMock()
        first_child.release_clients.side_effect = RuntimeError("release failed")
        second_child = MagicMock()
        self.agent._active_children = [first_child, second_child]
        client = object()
        self.agent.client = client
        self.agent._retire_shared_openai_client = MagicMock()
        self.agent._close_cached_request_openai_client = MagicMock()
        self.agent._close_cached_request_anthropic_client = MagicMock()
        self.agent.shutdown_memory_provider = MagicMock()

        with (
            patch.object(loop, "cleanup_vm") as cleanup_vm,
            patch.object(loop, "cleanup_browser") as cleanup_browser,
        ):
            self.agent.release_clients()

        first_child.release_clients.assert_called_once_with()
        first_child.close.assert_called_once_with()
        second_child.release_clients.assert_called_once_with()
        self.assertEqual(self.agent._active_children, [])
        self.agent._retire_shared_openai_client.assert_called_once_with(
            client, reason="cache_evict"
        )
        self.assertIsNone(self.agent.client)
        self.agent._close_cached_request_openai_client.assert_called_once_with(
            reason="cache_evict"
        )
        self.agent._close_cached_request_anthropic_client.assert_called_once_with(
            reason="cache_evict"
        )
        self.agent.shutdown_memory_provider.assert_not_called()
        cleanup_vm.assert_not_called()
        cleanup_browser.assert_not_called()

    def test_close_releases_every_owned_resource_through_legacy_paths(self) -> None:
        child = MagicMock()
        self.agent._active_children = [child]
        openai_client = object()
        self.agent.client = openai_client
        self.agent._close_openai_client = MagicMock()
        self.agent._close_cached_request_openai_client = MagicMock()
        self.agent._close_cached_request_anthropic_client = MagicMock()
        self.agent.shutdown_memory_provider = MagicMock()
        self.agent._session_messages = [{"role": "user", "content": "hello"}]
        anthropic_client = MagicMock()
        anthropic_client.close.side_effect = RuntimeError("close failed")
        self.agent._anthropic_client = anthropic_client
        codex_session = MagicMock()
        self.agent._codex_session = codex_session
        session_db = MagicMock()
        self.agent._session_db = session_db
        self.agent._end_session_on_close = True
        self.agent._owns_session_db = True

        with (
            patch.object(loop, "cleanup_vm") as cleanup_vm,
            patch.object(loop, "cleanup_browser") as cleanup_browser,
            patch.object(loop.logger, "debug") as debug,
            patch(
                "pcbdraft.tools.process_registry.process_registry.kill_all"
            ) as kill_all,
            patch(
                "pcbdraft.tools.computer_use.release_computer_use_session"
            ) as release_computer_use_session,
            patch("pcbdraft.interfaces.tui.mem_trim.trim_memory") as trim_memory,
        ):
            self.agent.close()

        self.agent.shutdown_memory_provider.assert_called_once_with(
            [{"role": "user", "content": "hello"}]
        )
        kill_all.assert_called_once_with(task_id="session-1")
        cleanup_vm.assert_called_once_with("session-1")
        cleanup_browser.assert_called_once_with("session-1")
        release_computer_use_session.assert_called_once_with("session-1")
        child.close.assert_called_once_with()
        self.assertEqual(self.agent._active_children, [])
        self.agent._close_openai_client.assert_called_once_with(
            openai_client, reason="agent_close", shared=True
        )
        self.assertIsNone(self.agent.client)
        self.agent._close_cached_request_openai_client.assert_called_once_with(
            reason="agent_close"
        )
        self.agent._close_cached_request_anthropic_client.assert_called_once_with(
            reason="agent_close"
        )
        self.assertIsNone(self.agent._anthropic_client)
        self.assertIsNone(self.agent._codex_session)
        codex_session.close.assert_called_once_with()
        debug.assert_called_once_with(
            "Shared Anthropic client close failed", exc_info=True
        )
        self.assertEqual(self.agent._session_messages, [])
        trim_memory.assert_called_once_with(force=True, reason="agent close")
        session_db.end_session.assert_called_once_with("session-1", "agent_close")
        session_db.close.assert_called_once_with()
        self.assertFalse(self.agent._owns_session_db)

    def test_cached_openai_teardown_closes_idle_and_aborts_inflight(self) -> None:
        idle_client = object()
        self.agent._request_client_cache = {
            "client": idle_client,
            "kwargs": {"api_key": "key"},
            "poisoned": True,
            "in_use": False,
        }
        self.agent._close_openai_client = MagicMock()
        self.agent._abort_request_openai_client = MagicMock()

        self.agent._close_cached_request_openai_client(reason="close")

        self.agent._close_openai_client.assert_called_once_with(
            idle_client, reason="close", shared=False
        )
        self.assertEqual(
            self.agent._request_client_cache,
            {"client": None, "kwargs": None, "poisoned": False, "in_use": False},
        )

        inflight_client = object()
        self.agent._request_client_cache.update(
            client=inflight_client,
            kwargs={},
            in_use=True,
        )
        self.agent._close_cached_request_openai_client(reason="close")
        self.agent._abort_request_openai_client.assert_called_once_with(
            inflight_client, reason="close_in_flight"
        )

    def test_cached_anthropic_teardown_closes_idle_and_aborts_inflight(self) -> None:
        idle_client = MagicMock()
        self.agent._request_anthropic_client_cache = {
            "client": idle_client,
            "key": ("anthropic",),
            "poisoned": True,
            "in_use": False,
        }
        self.agent._force_close_tcp_sockets = MagicMock()
        self.agent._abort_request_anthropic_client = MagicMock()

        self.agent._close_cached_request_anthropic_client(reason="close")

        self.agent._force_close_tcp_sockets.assert_called_once_with(idle_client)
        idle_client.close.assert_called_once_with()
        self.assertEqual(
            self.agent._request_anthropic_client_cache,
            {"client": None, "key": None, "poisoned": False, "in_use": False},
        )

        inflight_client = MagicMock()
        self.agent._request_anthropic_client_cache.update(
            client=inflight_client,
            key=("anthropic",),
            in_use=True,
        )
        self.agent._close_cached_request_anthropic_client(reason="close")
        self.agent._abort_request_anthropic_client.assert_called_once_with(
            inflight_client, reason="close_in_flight"
        )
        inflight_client.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
