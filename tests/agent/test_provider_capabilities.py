"""Focused tests for provider and API capability policy."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.provider_capabilities import ProviderCapabilitiesMixin


class ProviderCapabilitiesCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_agent_inherits_all_extracted_methods(self) -> None:
        self.assertTrue(issubclass(AIAgent, ProviderCapabilitiesMixin))
        for name in (
            "_is_direct_openai_url",
            "_is_azure_openai_url",
            "_is_github_copilot_url",
            "_resolved_api_call_timeout",
            "_resolved_api_call_stale_timeout_base",
            "_compute_non_stream_stale_timeout",
            "_codex_silent_hang_hint",
            "_is_openrouter_url",
            "_is_copilot_url",
            "_is_copilot_provider",
            "_is_codex_backend",
            "_anthropic_prompt_cache_policy",
            "_direct_native_anthropic_tool_cache_capability",
            "_model_requires_responses_api",
            "_provider_model_requires_responses_api",
            "_max_tokens_param",
            "_requested_output_cap_from_api_kwargs",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name), getattr(ProviderCapabilitiesMixin, name)
                )

    def test_legacy_loop_helpers_remain_late_bound(self) -> None:
        self.agent._base_url_hostname = ""
        self.agent._base_url_lower = "https://example.invalid/v1"
        self.agent.provider = "custom"
        self.agent.model = "custom-model"

        with patch.object(
            loop, "base_url_hostname", return_value="api.openai.com"
        ) as hostname:
            self.assertTrue(self.agent._is_direct_openai_url())
        hostname.assert_called_once_with(self.agent._base_url_lower)

        with patch.object(
            loop, "get_provider_request_timeout", return_value=42.0
        ) as timeout:
            self.assertEqual(self.agent._resolved_api_call_timeout(), 42.0)
        timeout.assert_called_once_with("custom", "custom-model")

        with patch.object(
            AIAgent, "_model_requires_responses_api", return_value=True
        ) as requires:
            self.assertTrue(
                self.agent._provider_model_requires_responses_api(
                    "future-model", provider="openai"
                )
            )
        requires.assert_called_once_with("future-model")

    def test_shared_anthropic_helper_patch_paths_remain_dynamic(self) -> None:
        with patch(
            "pcbdraft.agent.agent_runtime_helpers.anthropic_prompt_cache_policy",
            return_value=(True, False),
        ) as policy:
            self.assertEqual(
                self.agent._anthropic_prompt_cache_policy(provider="anthropic"),
                (True, False),
            )
        policy.assert_called_once_with(
            self.agent,
            provider="anthropic",
            base_url=None,
            api_mode=None,
            model=None,
        )

        with patch(
            "pcbdraft.agent.agent_runtime_helpers."
            "_direct_native_anthropic_tool_cache_capability",
            return_value=True,
        ) as capability:
            self.assertTrue(
                self.agent._direct_native_anthropic_tool_cache_capability(
                    api_mode="anthropic"
                )
            )
        capability.assert_called_once_with(
            self.agent,
            provider=None,
            base_url=None,
            api_mode="anthropic",
            model=None,
        )


class ProviderCapabilitiesBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)
        self.agent.provider = "custom"
        self.agent.model = "custom-model"
        self.agent.api_mode = "chat_completions"
        self.agent.base_url = "https://example.invalid/v1"
        self.agent._base_url = self.agent.base_url
        self.agent._base_url_lower = self.agent.base_url.lower()
        self.agent._base_url_hostname = "example.invalid"

    def test_endpoint_and_provider_classification(self) -> None:
        self.assertTrue(self.agent._is_direct_openai_url("https://api.openai.com/v1"))
        self.assertTrue(
            self.agent._is_azure_openai_url(
                "https://resource.openai.azure.com/openai/v1"
            )
        )
        self.assertTrue(
            self.agent._is_github_copilot_url(
                "https://api.githubcopilot.com/chat/completions"
            )
        )

        self.agent._base_url_lower = "https://openrouter.ai/api/v1"
        self.assertTrue(self.agent._is_openrouter_url())
        self.agent._base_url_lower = "https://models.github.ai/inference"
        self.assertTrue(self.agent._is_copilot_url())
        self.agent.provider = "github-copilot"
        self.agent._base_url_lower = "https://example.invalid/v1"
        self.assertTrue(self.agent._is_copilot_provider())

        self.agent.api_mode = "codex_responses"
        self.agent._base_url_hostname = "chatgpt.com"
        self.agent._base_url_lower = "https://chatgpt.com/backend-api/codex"
        self.assertTrue(self.agent._is_codex_backend())

    def test_timeout_precedence_and_legacy_environment_hooks(self) -> None:
        with (
            patch.object(loop, "get_provider_request_timeout", return_value=None),
            patch.object(loop, "env_float", return_value=321.0) as env_value,
        ):
            self.assertEqual(self.agent._resolved_api_call_timeout(), 321.0)
        env_value.assert_called_once_with("PCBDRAFT_RUNTIME_API_TIMEOUT", 1800.0)

        fake_os = SimpleNamespace(getenv=MagicMock(return_value="77.5"))
        with (
            patch.object(loop, "get_provider_stale_timeout", return_value=None),
            patch.object(loop, "os", fake_os),
        ):
            self.assertEqual(
                self.agent._resolved_api_call_stale_timeout_base(), (77.5, False)
            )
        fake_os.getenv.assert_called_once_with(
            "PCBDRAFT_RUNTIME_API_CALL_STALE_TIMEOUT"
        )

    def test_non_stream_stale_timeout_handles_local_and_large_requests(self) -> None:
        with (
            patch.object(
                AIAgent,
                "_resolved_api_call_stale_timeout_base",
                return_value=(90.0, True),
            ),
            patch.object(loop, "is_local_endpoint", return_value=True),
        ):
            self.assertEqual(
                self.agent._compute_non_stream_stale_timeout({"messages": []}),
                float("inf"),
            )

        self.agent._base_url = "https://example.invalid/v1"
        with (
            patch.object(
                AIAgent,
                "_resolved_api_call_stale_timeout_base",
                return_value=(90.0, False),
            ),
            patch(
                "pcbdraft.agent.chat_completion_helpers."
                "estimate_request_context_tokens",
                return_value=120_000,
            ),
        ):
            self.assertEqual(
                self.agent._compute_non_stream_stale_timeout({"messages": []}),
                240.0,
            )

    def test_codex_silent_hang_hint_is_narrow_and_regex_patchable(self) -> None:
        self.agent.api_mode = "codex_responses"
        self.agent.provider = "openai-codex"
        self.agent.model = "gpt-5.5-codex"
        self.assertIn("gpt-5.5-codex", self.agent._codex_silent_hang_hint())
        self.assertIsNone(self.agent._codex_silent_hang_hint("gpt-5.50"))

        fake_re = SimpleNamespace(search=MagicMock(return_value=None))
        with patch.object(loop, "re", fake_re):
            self.assertIsNone(self.agent._codex_silent_hang_hint())
        fake_re.search.assert_called_once()

    def test_model_routing_and_token_parameters(self) -> None:
        self.assertTrue(self.agent._model_requires_responses_api("openai/gpt-5.4"))
        self.assertFalse(
            self.agent._provider_model_requires_responses_api(
                "gpt-5.4", provider="nous"
            )
        )
        self.assertFalse(
            self.agent._provider_model_requires_responses_api(
                "gpt-5.4", provider="custom"
            )
        )

        self.agent.model = "gpt-4.1"
        self.agent._base_url_hostname = "api.openai.com"
        self.assertEqual(
            self.agent._max_tokens_param(100), {"max_completion_tokens": 100}
        )
        self.agent.model = "legacy-model"
        self.agent._base_url_hostname = "example.invalid"
        with patch.object(
            loop, "model_forces_max_completion_tokens", return_value=False
        ):
            self.assertEqual(self.agent._max_tokens_param(100), {"max_tokens": 100})

    def test_requested_output_cap_uses_wire_precedence_and_positive_values(
        self,
    ) -> None:
        self.assertEqual(
            self.agent._requested_output_cap_from_api_kwargs(
                {
                    "max_output_tokens": "512",
                    "max_completion_tokens": 256,
                    "max_tokens": 128,
                }
            ),
            512,
        )
        self.assertEqual(
            self.agent._requested_output_cap_from_api_kwargs(
                {"max_output_tokens": 0, "max_tokens": "128"}
            ),
            128,
        )
        self.assertIsNone(
            self.agent._requested_output_cap_from_api_kwargs({"max_tokens": "bad"})
        )


if __name__ == "__main__":
    unittest.main()
