"""Focused tests for request-scoped client ownership and caching."""

from __future__ import annotations

import ast
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop, request_client_lifecycle
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.request_client_lifecycle import RequestClientLifecycleMixin


class RequestClientLifecycleCompatibilityTests(unittest.TestCase):
    def test_module_does_not_import_legacy_loop(self) -> None:
        module_path = Path(request_client_lifecycle.__file__)
        tree = ast.parse(module_path.read_text())
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("pcbdraft.agent.loop", imported_modules)

    def test_agent_inherits_extracted_methods_without_wrappers(self) -> None:
        self.assertTrue(issubclass(AIAgent, RequestClientLifecycleMixin))
        for name in (
            "_request_client_cache_ref",
            "_create_request_openai_client",
            "_close_request_openai_client",
            "_abort_request_openai_client",
            "_request_anthropic_client_cache_ref",
            "_request_anthropic_client_key",
            "_create_request_anthropic_client",
            "_close_request_anthropic_client",
            "_abort_request_anthropic_client",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name),
                    getattr(RequestClientLifecycleMixin, name),
                )


class RequestClientLifecycleBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)
        self.agent._client_lock = threading.RLock()
        self.agent.provider = "custom"
        self.agent.model = "custom-model"
        self.agent.api_mode = "chat_completions"

    def test_openai_client_create_reuse_abort_and_owner_close(self) -> None:
        primary_client = object()
        request_client = object()
        self.agent._client_kwargs = {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
        }
        self.agent._ensure_primary_openai_client = MagicMock(
            return_value=primary_client
        )
        self.agent._create_openai_client = MagicMock(return_value=request_client)
        self.agent._is_openai_client_closed = MagicMock(return_value=False)
        self.agent._close_openai_client = MagicMock()
        self.agent._force_close_tcp_sockets = MagicMock(return_value=1)
        self.agent._client_log_context = MagicMock(return_value="context")
        self.agent._api_kwargs_have_image_parts = MagicMock(return_value=False)

        with patch.object(
            loop, "base_url_host_matches", return_value=False
        ) as host_matches:
            first = self.agent._create_request_openai_client(reason="first")
            self.agent._close_request_openai_client(first, reason="request_complete")
            second = self.agent._create_request_openai_client(reason="second")

        self.assertIs(first, request_client)
        self.assertIs(second, request_client)
        self.agent._create_openai_client.assert_called_once_with(
            {
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "max_retries": 0,
            },
            reason="first",
            shared=False,
        )
        self.agent._close_openai_client.assert_not_called()
        self.assertEqual(host_matches.call_count, 2)
        self.assertTrue(self.agent._request_client_cache["in_use"])

        self.agent._abort_request_openai_client(second, reason="interrupt")
        self.assertTrue(self.agent._request_client_cache["poisoned"])
        self.agent._force_close_tcp_sockets.assert_called_once_with(request_client)

        self.agent._close_request_openai_client(second, reason="request_complete")
        self.agent._close_openai_client.assert_called_once_with(
            request_client,
            reason="request_complete",
            shared=False,
        )
        self.assertEqual(
            self.agent._request_client_cache,
            {"client": None, "kwargs": None, "poisoned": False, "in_use": False},
        )

    def test_anthropic_client_reuses_cache_and_legacy_timeout_patch(self) -> None:
        request_client = MagicMock()
        self.agent.provider = "anthropic"
        self.agent.model = "claude-test"
        self.agent.api_mode = "anthropic"
        self.agent._anthropic_api_key = "test-key"
        self.agent._anthropic_base_url = "https://example.invalid"
        self.agent._oauth_1m_beta_disabled = True
        self.agent._is_openai_client_closed = MagicMock(return_value=False)

        with (
            patch.object(
                loop, "get_provider_request_timeout", return_value=41.0
            ) as request_timeout,
            patch(
                "pcbdraft.model.anthropic_adapter.build_anthropic_client",
                return_value=request_client,
            ) as build_client,
        ):
            first = self.agent._create_request_anthropic_client(reason="first")
            self.agent._close_request_anthropic_client(first, reason="request_complete")
            second = self.agent._create_request_anthropic_client(reason="second")

        self.assertIs(first, request_client)
        self.assertIs(second, request_client)
        build_client.assert_called_once_with(
            "test-key",
            "https://example.invalid",
            timeout=41.0,
            drop_context_1m_beta=True,
        )
        self.assertEqual(request_timeout.call_count, 3)
        self.assertTrue(self.agent._request_anthropic_client_cache["in_use"])
        request_client.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
