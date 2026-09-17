"""Focused tests for provider credential refresh and client reconfiguration."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop, provider_credential_refresh
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.provider_credential_refresh import ProviderCredentialRefreshMixin

_EXTRACTED_METHODS = (
    "_try_refresh_codex_client_credentials",
    "_try_refresh_nous_client_credentials",
    "_try_refresh_env_client_credentials",
    "_try_refresh_vertex_client_credentials",
    "_try_refresh_copilot_client_credentials",
    "_try_recover_stale_copilot_credential",
    "_try_refresh_anthropic_client_credentials",
    "_apply_client_headers_for_base_url",
    "_apply_user_default_headers",
    "_swap_credential",
    "_reapply_route_client_config",
    "_recover_with_credential_pool",
    "_credential_pool_may_recover_rate_limit",
)


class ProviderCredentialRefreshCompatibilityTests(unittest.TestCase):
    def test_module_has_no_reverse_import_and_agent_inherits_methods(self) -> None:
        source = Path(provider_credential_refresh.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )

        self.assertNotIn("pcbdraft.agent.loop", imports)
        self.assertTrue(issubclass(AIAgent, ProviderCredentialRefreshMixin))
        for name in _EXTRACTED_METHODS:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name),
                    getattr(ProviderCredentialRefreshMixin, name),
                )
        self.assertIs(inspect.getmodule(AIAgent.run_conversation), loop)


class ProviderCredentialRefreshBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)
        self.agent.provider = "custom"
        self.agent.model = "model"
        self.agent.api_mode = "chat_completions"
        self.agent.base_url = "https://old.example/v1"
        self.agent.api_key = "old-key"
        self.agent._client_kwargs = {
            "api_key": "old-key",
            "base_url": self.agent.base_url,
        }

    def test_codex_refresh_preserves_account_guard_and_rebuilds_on_new_token(
        self,
    ) -> None:
        self.agent.provider = "openai-codex"
        self.agent.api_mode = "codex_responses"
        self.agent.api_key = "active-key"
        self.agent._replace_primary_openai_client = MagicMock(return_value=True)

        with patch(
            "pcbdraft.model.auth.resolve_codex_runtime_credentials",
            side_effect=[
                {
                    "api_key": "active-key",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                },
                {
                    "api_key": "new-key",
                    "base_url": "https://chatgpt.com/backend-api/codex/",
                },
            ],
        ) as resolve:
            refreshed = self.agent._try_refresh_codex_client_credentials()

        self.assertTrue(refreshed)
        self.assertEqual(self.agent.api_key, "new-key")
        self.assertEqual(self.agent.base_url, "https://chatgpt.com/backend-api/codex")
        self.assertEqual(resolve.call_count, 2)
        resolve.assert_any_call(refresh_if_expiring=False)
        resolve.assert_any_call(force_refresh=True)
        self.agent._replace_primary_openai_client.assert_called_once_with(
            reason="openai-codex_credential_refresh"
        )

        self.agent.api_key = "manual-pool-key"
        self.agent._replace_primary_openai_client.reset_mock()
        with patch(
            "pcbdraft.model.auth.resolve_codex_runtime_credentials",
            return_value={"api_key": "singleton-key"},
        ) as guarded_resolve:
            self.assertFalse(self.agent._try_refresh_codex_client_credentials())
        guarded_resolve.assert_called_once_with(refresh_if_expiring=False)
        self.agent._replace_primary_openai_client.assert_not_called()

    def test_nous_refresh_uses_legacy_timeout_hook_and_rebuilds_client(self) -> None:
        self.agent.provider = "nous"
        self.agent._replace_primary_openai_client = MagicMock(return_value=True)

        with (
            patch.object(loop, "env_float", return_value=23.0) as env_timeout,
            patch(
                "pcbdraft.model.auth.resolve_nous_runtime_credentials",
                return_value={
                    "api_key": "nous-key",
                    "base_url": "https://portal.nousresearch.com/v1/",
                },
            ) as resolve,
        ):
            refreshed = self.agent._try_refresh_nous_client_credentials(force=False)

        self.assertTrue(refreshed)
        env_timeout.assert_called_once_with(
            "PCBDRAFT_RUNTIME_NOUS_TIMEOUT_SECONDS",
            15,
        )
        resolve.assert_called_once_with(timeout_seconds=23.0, force_refresh=False)
        self.assertEqual(self.agent.api_key, "nous-key")
        self.assertEqual(self.agent.base_url, "https://portal.nousresearch.com/v1")
        self.assertNotIn("default_headers", self.agent._client_kwargs)
        self.agent._replace_primary_openai_client.assert_called_once_with(
            reason="nous_credential_refresh"
        )

    def test_route_headers_use_legacy_matcher_and_dynamic_user_merge(self) -> None:
        self.agent._client_kwargs["default_headers"] = {"stale": "value"}
        self.agent._apply_user_default_headers = MagicMock()

        def host_matches(_url, host):
            return host == "openrouter.ai"

        with (
            patch.object(
                loop, "base_url_host_matches", side_effect=host_matches
            ) as match,
            patch(
                "pcbdraft.model.auxiliary_client.build_or_headers",
                return_value={"provider": "openrouter"},
            ) as build_headers,
            patch(
                "pcbdraft.model.configuration."
                "apply_custom_provider_extra_headers_to_client_kwargs"
            ) as apply_extra,
        ):
            self.agent._apply_client_headers_for_base_url(
                "https://openrouter.ai/api/v1"
            )

        self.assertEqual(
            self.agent._client_kwargs["default_headers"],
            {"provider": "openrouter"},
        )
        self.assertGreater(match.call_count, 0)
        build_headers.assert_called_once_with()
        self.agent._apply_user_default_headers.assert_called_once_with()
        apply_extra.assert_called_once_with(
            self.agent._client_kwargs,
            "https://openrouter.ai/api/v1",
        )

    def test_openai_credential_swap_reapplies_route_and_rebuilds(self) -> None:
        self.agent._reapply_route_client_config = MagicMock()
        self.agent._replace_primary_openai_client = MagicMock(return_value=True)
        entry = SimpleNamespace(
            id="entry-2",
            runtime_api_key="rotated-key",
            runtime_base_url="https://new.example/v1/",
        )

        self.agent._swap_credential(entry)

        self.assertEqual(self.agent._credential_pool_entry_id, "entry-2")
        self.assertEqual(self.agent.api_key, "rotated-key")
        self.assertEqual(self.agent.base_url, "https://new.example/v1")
        self.assertEqual(
            self.agent._client_kwargs,
            {
                "api_key": "rotated-key",
                "base_url": "https://new.example/v1",
            },
        )
        self.agent._reapply_route_client_config.assert_called_once_with(
            route_changed=True
        )
        self.agent._replace_primary_openai_client.assert_called_once_with(
            reason="credential_rotation"
        )

    def test_anthropic_refresh_uses_legacy_endpoint_and_timeout_hooks(self) -> None:
        self.agent.provider = "anthropic"
        self.agent.api_mode = "anthropic_messages"
        self.agent._anthropic_api_key = "old-anthropic-key"
        self.agent._anthropic_base_url = "https://api.anthropic.com"
        self.agent._anthropic_client = MagicMock()
        new_client = MagicMock()

        with (
            patch.object(loop, "base_url_host_matches", return_value=False) as match,
            patch.object(
                loop,
                "get_provider_request_timeout",
                return_value=41.0,
            ) as timeout,
            patch(
                "pcbdraft.model.anthropic_adapter.resolve_anthropic_token",
                return_value="new-anthropic-key",
            ),
            patch(
                "pcbdraft.model.anthropic_adapter.build_anthropic_client",
                return_value=new_client,
            ) as build,
            patch(
                "pcbdraft.model.anthropic_adapter._is_oauth_token",
                return_value=True,
            ) as is_oauth,
        ):
            refreshed = self.agent._try_refresh_anthropic_client_credentials()

        self.assertTrue(refreshed)
        match.assert_called_once_with("https://api.anthropic.com", "azure.com")
        timeout.assert_called_once_with("anthropic", "model")
        build.assert_called_once_with(
            "new-anthropic-key",
            "https://api.anthropic.com",
            timeout=41.0,
        )
        is_oauth.assert_called_once_with("new-anthropic-key")
        self.assertIs(self.agent._anthropic_client, new_client)
        self.assertTrue(self.agent._is_anthropic_oauth)

    def test_credential_pool_recovery_delegates_and_availability_stays_local(
        self,
    ) -> None:
        expected = (True, False)
        with patch(
            "pcbdraft.agent.agent_runtime_helpers.recover_with_credential_pool",
            return_value=expected,
        ) as recover:
            actual = self.agent._recover_with_credential_pool(
                status_code=429,
                has_retried_429=False,
                error_context={"code": "rate_limit"},
                billing_unverified=True,
            )

        self.assertEqual(actual, expected)
        recover.assert_called_once_with(
            self.agent,
            status_code=429,
            has_retried_429=False,
            classified_reason=None,
            error_context={"code": "rate_limit"},
            billing_unverified=True,
        )

        self.agent._credential_pool = None
        self.assertFalse(self.agent._credential_pool_may_recover_rate_limit())
        self.agent._credential_pool = SimpleNamespace(has_available=lambda: True)
        self.assertTrue(self.agent._credential_pool_may_recover_rate_limit())


if __name__ == "__main__":
    unittest.main()
