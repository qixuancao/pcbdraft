"""Focused tests for extracted auxiliary provider configuration helpers."""

from __future__ import annotations

import base64
import json
import os
import unittest
from unittest.mock import patch

from pcbdraft.model import auxiliary_client as legacy
from pcbdraft.model import auxiliary_provider_config as provider_config


class AuxiliaryProviderConfigCompatibilityTests(unittest.TestCase):
    def test_legacy_module_reexports_extracted_helpers_by_identity(self) -> None:
        names = (
            "_normalize_aux_provider",
            "_fixed_temperature_for_model",
            "_compression_threshold_for_model",
            "_fast_model_from_catalog",
            "_get_aux_model_for_provider",
            "_resolve_provider_vision_default",
            "_apply_user_default_headers",
            "build_or_headers",
            "build_nvidia_nim_headers",
            "_nous_extra_body",
            "_codex_cloudflare_headers",
            "_is_dual_surface_anthropic_host",
            "_to_openai_base_url",
        )

        for name in names:
            with self.subTest(name=name):
                self.assertIs(getattr(legacy, name), getattr(provider_config, name))
        self.assertIs(legacy.OMIT_TEMPERATURE, provider_config.OMIT_TEMPERATURE)

    def test_legacy_runtime_patch_paths_remain_effective(self) -> None:
        with patch.object(legacy, "_read_main_provider", return_value="google"):
            self.assertEqual(legacy._normalize_aux_provider("main"), "gemini")
        with patch.object(
            legacy,
            "_get_auxiliary_task_config",
            return_value={"prefer_fast_model": True},
        ):
            self.assertTrue(legacy._task_prefers_fast_model("title_generation"))
        with patch.object(
            legacy,
            "_fast_model_from_catalog",
            return_value="vendor/fast-model",
        ):
            self.assertEqual(
                legacy._get_aux_model_for_provider("unknown", prefer_fast=True),
                "vendor/fast-model",
            )
        with patch.object(legacy, "_nous_portal_tags", return_value=["client=test"]):
            self.assertEqual(legacy._nous_extra_body(), {"tags": ["client=test"]})


class AuxiliaryProviderConfigBehaviorTests(unittest.TestCase):
    def test_provider_aliases_and_model_contracts(self) -> None:
        self.assertEqual(
            provider_config._normalize_aux_provider("Codex"), "openai-codex"
        )
        self.assertEqual(
            provider_config._normalize_aux_provider("custom: moonshot"), "kimi-coding"
        )
        self.assertIs(
            provider_config._fixed_temperature_for_model("moonshot/kimi-k2"),
            provider_config.OMIT_TEMPERATURE,
        )
        self.assertEqual(
            provider_config._compression_threshold_for_model(
                "gpt-5.6-sol", "openai-codex"
            ),
            0.85,
        )
        self.assertEqual(
            provider_config._compression_threshold_for_model(
                "arcee-ai/trinity-large-thinking", "openrouter"
            ),
            0.75,
        )

    def test_header_builders_keep_provider_specific_behavior(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PCBDRAFT_RUNTIME_OPENROUTER_CACHE": "true",
                "PCBDRAFT_RUNTIME_OPENROUTER_CACHE_TTL": "600",
            },
            clear=False,
        ):
            headers = provider_config.build_or_headers({"response_cache": False})
        self.assertEqual(headers["X-Title"], "PCBDraft")
        self.assertEqual(headers["X-OpenRouter-Cache"], "true")
        self.assertEqual(headers["X-OpenRouter-Cache-TTL"], "600")

        self.assertEqual(
            provider_config.build_nvidia_nim_headers(
                "https://integrate.api.nvidia.com/v1"
            ),
            {"X-BILLING-INVOKE-ORIGIN": "PCBDraft"},
        )
        self.assertEqual(
            provider_config.build_nvidia_nim_headers("https://nim.internal/v1"),
            {},
        )

    def test_codex_headers_decode_account_id_without_rejecting_bad_tokens(self) -> None:
        claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"}}
        payload = (
            base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        )

        headers = provider_config._codex_cloudflare_headers(f"x.{payload}.y")

        self.assertEqual(headers["originator"], "codex_cli_rs")
        self.assertEqual(headers["ChatGPT-Account-ID"], "acct-123")
        self.assertNotIn(
            "ChatGPT-Account-ID",
            provider_config._codex_cloudflare_headers("not-a-jwt"),
        )

    def test_host_normalization_only_rewrites_known_surfaces(self) -> None:
        self.assertEqual(
            provider_config._to_openai_base_url("https://api.minimax.io/anthropic"),
            "https://api.minimax.io/v1",
        )
        self.assertEqual(
            provider_config._to_openai_base_url(
                "https://open.bigmodel.cn/api/anthropic"
            ),
            "https://open.bigmodel.cn/api/coding/paas/v4",
        )
        self.assertEqual(
            provider_config._to_openai_base_url(
                "https://anthropic-only.example/v1/anthropic"
            ),
            "https://anthropic-only.example/v1/anthropic",
        )
        self.assertEqual(
            provider_config._to_openai_base_url("https://api.kimi.com/coding"),
            "https://api.kimi.com/coding/v1",
        )


if __name__ == "__main__":
    unittest.main()
