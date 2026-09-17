from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.model import auxiliary_client, auxiliary_vision


class AuxiliaryVisionContractTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import(self):
        source = Path(auxiliary_vision.__file__).read_text(encoding="utf-8")

        self.assertNotIn("import auxiliary_client", source)
        self.assertNotIn("from pcbdraft.model.auxiliary_client", source)

    def test_legacy_symbols_keep_identity_and_client_ownership_boundary(self):
        names = (
            "_main_model_supports_vision",
            "_normalize_vision_provider",
            "_resolve_strict_vision_backend",
            "_strict_vision_backend_available",
            "get_available_vision_backends",
            "resolve_vision_provider_client",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(auxiliary_client, name),
                    getattr(auxiliary_vision, name),
                )

        self.assertIs(
            auxiliary_client._VISION_AUTO_PROVIDER_ORDER,
            auxiliary_vision._VISION_AUTO_PROVIDER_ORDER,
        )
        self.assertIs(
            inspect.getmodule(auxiliary_client.resolve_provider_client),
            auxiliary_client,
        )
        self.assertIs(
            inspect.getmodule(auxiliary_client._get_cached_client),
            auxiliary_client,
        )

    def test_normalization_uses_legacy_patch_path(self):
        with patch.object(
            auxiliary_client,
            "_normalize_aux_provider",
            return_value="openrouter",
        ) as normalize:
            result = auxiliary_client._normalize_vision_provider("OR")

        self.assertEqual(result, "openrouter")
        normalize.assert_called_once_with("OR")

    def test_main_model_vision_capability_uses_shared_catalog(self):
        config = {"model": {"provider": "deepinfra"}}
        with (
            patch(
                "pcbdraft.agent.image_routing._lookup_supports_vision",
                return_value=False,
            ) as lookup,
            patch(
                "pcbdraft.model.configuration.load_config_readonly",
                return_value=config,
            ),
        ):
            result = auxiliary_client._main_model_supports_vision(
                "deepinfra",
                "text-model",
            )

        self.assertFalse(result)
        lookup.assert_called_once_with("deepinfra", "text-model", config)

    def test_strict_deepinfra_uses_legacy_routing_hooks(self):
        client = object()
        with (
            patch.object(
                auxiliary_client,
                "_normalize_vision_provider",
                return_value="deepinfra",
            ),
            patch.object(
                auxiliary_client,
                "_resolve_provider_vision_default",
                return_value="catalog/vision-model",
            ) as resolve_default,
            patch.object(
                auxiliary_client,
                "resolve_provider_client",
                return_value=(client, "catalog/vision-model"),
            ) as resolve_client,
        ):
            result = auxiliary_client._resolve_strict_vision_backend("deepinfra")

        self.assertEqual(result, (client, "catalog/vision-model"))
        resolve_default.assert_called_once_with("deepinfra")
        resolve_client.assert_called_once_with(
            "deepinfra",
            "catalog/vision-model",
            is_vision=True,
        )

    def test_available_backends_preserve_order_through_legacy_hooks(self):
        availability = {
            "openrouter": True,
            "nous": False,
            "deepinfra": True,
        }
        with (
            patch.object(
                auxiliary_client,
                "_read_main_provider",
                return_value="openrouter",
            ),
            patch.object(
                auxiliary_client,
                "_strict_vision_backend_available",
                side_effect=lambda provider: availability[provider],
            ) as is_available,
        ):
            result = auxiliary_client.get_available_vision_backends()

        self.assertEqual(result, ["openrouter", "deepinfra"])
        self.assertEqual(
            [call.args[0] for call in is_available.call_args_list],
            ["openrouter", "nous", "deepinfra"],
        )

    def test_direct_endpoint_override_stays_in_host_provider_router(self):
        client = object()
        normalized_runtime = {"provider": "main", "model": "main-model"}
        with (
            patch.object(
                auxiliary_client,
                "_normalize_main_runtime",
                return_value=normalized_runtime,
            ),
            patch.object(
                auxiliary_client,
                "_resolve_task_provider_model",
                return_value=(
                    "auto",
                    "vision-model",
                    "https://vision.example/v1",
                    "secret",
                    "chat_completions",
                ),
            ),
            patch.object(
                auxiliary_client,
                "_normalize_vision_provider",
                return_value="auto",
            ),
            patch.object(
                auxiliary_client,
                "resolve_provider_client",
                return_value=(client, "vision-model"),
            ) as resolve_client,
        ):
            result = auxiliary_client.resolve_vision_provider_client()

        self.assertEqual(result, ("custom", client, "vision-model"))
        resolve_client.assert_called_once_with(
            "custom",
            model="vision-model",
            async_mode=False,
            explicit_base_url="https://vision.example/v1",
            explicit_api_key="secret",
            api_mode="chat_completions",
            main_runtime=normalized_runtime,
        )

    def test_auto_mode_skips_nonvision_main_and_uses_fallback_order(self):
        client = object()
        with (
            patch.object(
                auxiliary_client,
                "_normalize_main_runtime",
                return_value={"provider": "kimi-coding", "model": "text-model"},
            ),
            patch.object(
                auxiliary_client,
                "_resolve_task_provider_model",
                return_value=("auto", None, None, None, None),
            ),
            patch.object(
                auxiliary_client,
                "_normalize_vision_provider",
                return_value="auto",
            ),
            patch.object(
                auxiliary_client,
                "_resolve_provider_vision_default",
                return_value=None,
            ),
            patch.object(
                auxiliary_client,
                "_resolve_strict_vision_backend",
                side_effect=[(None, None), (client, "fallback-model")],
            ) as resolve_strict,
            patch.object(auxiliary_client, "resolve_provider_client") as resolve_main,
        ):
            result = auxiliary_client.resolve_vision_provider_client("auto")

        self.assertEqual(result, ("nous", client, "fallback-model"))
        self.assertEqual(
            [call.args[0] for call in resolve_strict.call_args_list],
            ["openrouter", "nous"],
        )
        resolve_main.assert_not_called()

    def test_async_strict_provider_uses_legacy_async_adapter(self):
        sync_client = object()
        async_client = object()
        with (
            patch.object(
                auxiliary_client,
                "_normalize_main_runtime",
                return_value={},
            ),
            patch.object(
                auxiliary_client,
                "_resolve_task_provider_model",
                return_value=("openrouter", None, None, None, None),
            ),
            patch.object(
                auxiliary_client,
                "_normalize_vision_provider",
                return_value="openrouter",
            ),
            patch.object(
                auxiliary_client,
                "_resolve_strict_vision_backend",
                return_value=(sync_client, "default-model"),
            ),
            patch.object(
                auxiliary_client,
                "_to_async_client",
                return_value=(async_client, "async-model"),
            ) as to_async,
        ):
            result = auxiliary_client.resolve_vision_provider_client(
                "openrouter",
                async_mode=True,
            )

        self.assertEqual(result, ("openrouter", async_client, "async-model"))
        to_async.assert_called_once_with(
            sync_client,
            "default-model",
            is_vision=True,
        )


if __name__ == "__main__":
    unittest.main()
