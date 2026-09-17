"""Focused coverage for extracted provider endpoint resolution."""

from __future__ import annotations

import ast
import hashlib
import unittest
from contextlib import nullcontext
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.model import auth, auth_provider_endpoints


class AuthProviderEndpointsTests(unittest.TestCase):
    def test_auth_reexports_same_objects_without_reverse_import(self) -> None:
        names = (
            "is_actual_local_base_url",
            "normalize_actual_base_url",
            "get_anthropic_key",
            "_resolve_kimi_base_url",
            "has_usable_secret",
            "_resolve_api_key_provider_secret",
            "_probe_single_zai_endpoint",
            "detect_zai_endpoint",
            "_resolve_zai_base_url",
            "_normalize_lmstudio_runtime_base_url",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(auth, name),
                    getattr(auth_provider_endpoints, name),
                )

        tree = ast.parse(
            Path(auth_provider_endpoints.__file__).read_text(encoding="utf-8")
        )
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("pcbdraft.model.auth", imports)

    def test_actual_normalization_uses_legacy_constants_and_helper(self) -> None:
        with patch.object(auth, "DEFAULT_ACTUAL_BASE_URL", "https://default.test/v1"):
            self.assertEqual(
                auth.normalize_actual_base_url(""), "https://default.test/v1"
            )

        with patch.object(auth, "is_actual_local_base_url", return_value=True) as local:
            self.assertEqual(
                auth.normalize_actual_base_url("http://custom.test"),
                "http://custom.test/v1",
            )
        local.assert_called_once_with("http://custom.test")

        self.assertTrue(auth.is_actual_local_base_url("http://[::1]:8080/v1"))
        self.assertFalse(auth.is_actual_local_base_url("https://example.com/v1"))

    def test_anthropic_and_kimi_resolution_use_legacy_registry_constants(self) -> None:
        registry = {"anthropic": SimpleNamespace(api_key_env_vars=("FIRST", "SECOND"))}
        with (
            patch.object(auth, "PROVIDER_REGISTRY", registry),
            patch(
                "pcbdraft.model.configuration.get_env_value_prefer_dotenv",
                side_effect=lambda name: {"FIRST": "", "SECOND": "token"}[name],
            ),
        ):
            self.assertEqual(auth.get_anthropic_key(), "token")

        with patch.object(auth, "KIMI_CODE_BASE_URL", "https://kimi.test/coding"):
            self.assertEqual(
                auth._resolve_kimi_base_url("sk-kimi-value", "default", ""),
                "https://kimi.test/coding",
            )
        self.assertEqual(
            auth._resolve_kimi_base_url("sk-kimi-value", "default", "override"),
            "override",
        )

    def test_secret_resolution_uses_legacy_secret_classifier(self) -> None:
        config = SimpleNamespace(api_key_env_vars=("PROVIDER_KEY",))
        with (
            patch(
                "pcbdraft.model.configuration.get_env_value_prefer_dotenv",
                return_value=" selected ",
            ),
            patch.object(auth, "has_usable_secret", return_value=True) as usable,
        ):
            self.assertEqual(
                auth._resolve_api_key_provider_secret("provider", config),
                ("selected", "PROVIDER_KEY"),
            )
        usable.assert_called_once_with("selected")

        with patch.object(auth, "_PLACEHOLDER_SECRET_VALUES", {"patched"}):
            self.assertFalse(auth.has_usable_secret("patched"))
            self.assertTrue(auth.has_usable_secret("original-placeholder"))

    def test_zai_probe_uses_legacy_httpx_path_and_model_order(self) -> None:
        post = Mock(
            side_effect=[
                SimpleNamespace(status_code=404),
                SimpleNamespace(status_code=200),
            ]
        )
        endpoint = ("coding", "https://zai.test/v4", ["first", "second"], "Coding")
        with patch.object(auth, "httpx", SimpleNamespace(post=post)):
            result = auth._probe_single_zai_endpoint("secret", endpoint, 2.5)

        self.assertEqual(
            result,
            {
                "id": "coding",
                "base_url": "https://zai.test/v4",
                "model": "second",
                "label": "Coding",
            },
        )
        self.assertEqual(
            [item.kwargs["json"]["model"] for item in post.call_args_list],
            ["first", "second"],
        )

    def test_zai_detection_uses_legacy_endpoint_and_probe_paths(self) -> None:
        endpoints = [
            ("first", "https://first.test", ["model"], "First"),
            ("second", "https://second.test", ["model"], "Second"),
        ]
        both_started = Barrier(2)

        def probe(_api_key, endpoint, _timeout):
            both_started.wait(timeout=1)
            return {
                "id": endpoint[0],
                "base_url": endpoint[1],
                "model": "model",
                "label": endpoint[3],
            }

        with (
            patch.object(auth, "ZAI_ENDPOINTS", endpoints),
            patch.object(auth, "_probe_single_zai_endpoint", side_effect=probe) as run,
        ):
            result = auth.detect_zai_endpoint("secret", timeout=0.1)

        self.assertIn(result["id"], {"first", "second"})
        self.assertEqual(run.call_count, 2)

    def test_zai_resolution_uses_legacy_cache_and_persistence_paths(self) -> None:
        api_key = "secret"
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        store = {"providers": {}}
        cached_state = {
            "detected_endpoint": {
                "base_url": "https://cached.test/v4",
                "key_hash": key_hash,
            }
        }
        with (
            patch.object(auth, "_load_auth_store", return_value=store),
            patch.object(auth, "_load_provider_state", return_value=cached_state),
            patch.object(auth, "detect_zai_endpoint") as detect,
        ):
            self.assertEqual(
                auth._resolve_zai_base_url(api_key, "https://default.test", ""),
                "https://cached.test/v4",
            )
        detect.assert_not_called()

        detected = {
            "id": "global",
            "base_url": "https://detected.test/v4",
            "model": "glm",
            "label": "Global",
        }
        with (
            patch.object(auth, "_load_auth_store", return_value=store) as load,
            patch.object(auth, "_load_provider_state", return_value={}),
            patch.object(auth, "detect_zai_endpoint", return_value=detected),
            patch.object(auth, "_auth_store_lock", return_value=nullcontext()),
            patch.object(auth, "_store_provider_state") as save_state,
            patch.object(auth, "_save_auth_store") as save_store,
        ):
            result = auth._resolve_zai_base_url(api_key, "https://default.test", "")

        self.assertEqual(result, "https://detected.test/v4")
        self.assertEqual(load.call_count, 2)
        saved_state = save_state.call_args.args[2]
        self.assertEqual(saved_state["detected_endpoint"]["key_hash"], key_hash)
        self.assertFalse(save_state.call_args.kwargs["set_active"])
        save_store.assert_called_once_with(store)

    def test_lmstudio_normalization_is_unchanged(self) -> None:
        cases = {
            "http://localhost:1234/api/v1": "http://localhost:1234/v1",
            "http://localhost:1234/api": "http://localhost:1234/v1",
            "http://localhost:1234/v1/": "http://localhost:1234/v1",
            "": "http://127.0.0.1:1234/v1",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    auth._normalize_lmstudio_runtime_base_url(raw), expected
                )


if __name__ == "__main__":
    unittest.main()
