from __future__ import annotations

import builtins
import unittest
from unittest.mock import patch

from pcbdraft.model import catalog
from pcbdraft.model.model_normalize import (
    normalize_model_for_provider,
    suggest_prefixed_model_id,
)


class ModelNormalizationTests(unittest.TestCase):
    def test_provider_specific_prefix_and_dot_rules(self) -> None:
        cases = (
            (
                "claude-sonnet-4.6",
                "openrouter",
                "anthropic/claude-sonnet-4.6",
            ),
            (
                "anthropic/claude-sonnet-4.6",
                "anthropic",
                "claude-sonnet-4-6",
            ),
            (
                "anthropic/claude-sonnet-4-6",
                "github-copilot",
                "claude-sonnet-4.6",
            ),
            ("ollama/glm-5.2", "custom", "ollama/glm-5.2"),
            ("custom/local-model", "custom", "local-model"),
        )

        for model, provider, expected in cases:
            with self.subTest(model=model, provider=provider):
                self.assertEqual(
                    normalize_model_for_provider(model, provider),
                    expected,
                )

    def test_catalogue_prefix_repair_requires_one_unique_match(self) -> None:
        with patch.object(
            catalog,
            "_PROVIDER_MODELS",
            {"nvidia": ["nvidia/nemotron-test"]},
        ):
            self.assertEqual(
                normalize_model_for_provider("nemotron-test", "nvidia"),
                "nvidia/nemotron-test",
            )
            self.assertEqual(
                suggest_prefixed_model_id("nvidia", "nemotron-test"),
                "nvidia/nemotron-test",
            )

        with patch.object(
            catalog,
            "_PROVIDER_MODELS",
            {
                "nvidia": [
                    "nvidia/nemotron-test",
                    "partner/nemotron-test",
                ]
            },
        ):
            self.assertEqual(
                normalize_model_for_provider("nemotron-test", "nvidia"),
                "nemotron-test",
            )
            self.assertIsNone(suggest_prefixed_model_id("nvidia", "nemotron-test"))

        with patch.object(catalog, "_PROVIDER_MODELS", {}):
            self.assertEqual(
                normalize_model_for_provider("local-nim", "nvidia"),
                "local-nim",
            )
            self.assertIsNone(suggest_prefixed_model_id("nvidia", "local-nim"))

    def test_alias_lookup_import_failure_falls_back_to_raw_provider(self) -> None:
        original_import = builtins.__import__

        def fail_catalog_import(name: str, *args: object, **kwargs: object):
            if name == "pcbdraft.model.catalog":
                raise ImportError("catalog unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_catalog_import):
            self.assertEqual(
                normalize_model_for_provider("custom/model", "CUSTOM"),
                "model",
            )

    def test_alias_lookup_runtime_failure_falls_back_to_raw_provider(self) -> None:
        with patch.object(
            catalog,
            "normalize_provider",
            side_effect=RuntimeError("catalog not initialized"),
        ):
            self.assertEqual(
                normalize_model_for_provider("custom/model", "CUSTOM"),
                "model",
            )

    def test_catalogue_runtime_failure_preserves_bare_model(self) -> None:
        original_import = builtins.__import__

        def fail_catalog_import(name: str, *args: object, **kwargs: object):
            if name == "pcbdraft.model.catalog":
                raise RuntimeError("catalog not initialized")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_catalog_import):
            self.assertEqual(
                normalize_model_for_provider("local-nim", "nvidia"),
                "local-nim",
            )

    def test_copilot_lookup_failure_uses_generic_fallback(self) -> None:
        with patch.object(
            catalog,
            "normalize_copilot_model_id",
            side_effect=RuntimeError("catalog not initialized"),
        ):
            self.assertEqual(
                normalize_model_for_provider("openai/gpt-5.4", "copilot"),
                "gpt-5.4",
            )


if __name__ == "__main__":
    unittest.main()
