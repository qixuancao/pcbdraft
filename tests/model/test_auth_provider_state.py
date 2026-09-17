from __future__ import annotations

import ast
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.model import auth, auth_provider_state


class AuthProviderStateCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_symbols_remain(self):
        source = Path(auth_provider_state.__file__).read_text(encoding="utf-8")
        imports = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("pcbdraft.model.auth", imports)

        for name in (
            "get_provider_auth_state",
            "get_active_provider",
            "is_provider_explicitly_configured",
            "clear_provider_auth",
            "deactivate_provider",
            "_get_config_hint_for_unknown_provider",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(auth, name)))

    def test_legacy_state_queries_resolve_patched_store_hooks(self):
        store = {"active_provider": "docs"}
        provider_state = {"access_token": "token"}
        with (
            patch.object(auth, "_load_auth_store", return_value=store) as load,
            patch.object(
                auth,
                "_load_provider_state",
                return_value=provider_state,
            ) as load_provider,
        ):
            self.assertIs(auth.get_provider_auth_state("docs"), provider_state)
            self.assertEqual(auth.get_active_provider(), "docs")

        self.assertEqual(load.call_count, 2)
        load_provider.assert_called_once_with(store, "docs")

    def test_legacy_explicit_provider_uses_auth_and_config_selection(self):
        with patch.object(
            auth,
            "_load_auth_store",
            return_value={"active_provider": " DoCs "},
        ):
            self.assertTrue(auth.is_provider_explicitly_configured("docs"))

        config = {
            "moa": {
                "presets": {
                    "review": {
                        "reference_models": [{"provider": "docs"}],
                    }
                }
            }
        }
        with (
            patch.object(auth, "_load_auth_store", return_value={}),
            patch("pcbdraft.model.configuration.load_config", return_value=config),
        ):
            self.assertTrue(auth.is_provider_explicitly_configured("docs"))

    def test_legacy_explicit_provider_uses_registry_env_and_pool_hooks(self):
        provider = SimpleNamespace(
            auth_type="api_key",
            api_key_env_vars=("CLAUDE_CODE_OAUTH_TOKEN", "DOCS_API_KEY"),
        )
        with (
            patch.object(auth, "_load_auth_store", return_value={}),
            patch("pcbdraft.model.configuration.load_config", return_value={}),
            patch.object(auth, "PROVIDER_REGISTRY", {"docs": provider}),
            patch.object(
                auth.os,
                "getenv",
                side_effect=lambda name, default="": (
                    "secret-value" if name == "DOCS_API_KEY" else default
                ),
            ) as getenv,
            patch.object(
                auth,
                "has_usable_secret",
                side_effect=lambda value: bool(value),
            ) as usable,
            patch.object(auth, "read_credential_pool", return_value=[]) as pool,
        ):
            self.assertTrue(auth.is_provider_explicitly_configured("docs"))
        getenv.assert_called_once_with("DOCS_API_KEY", "")
        usable.assert_called_once_with("secret-value")
        pool.assert_not_called()

        oauth_provider = SimpleNamespace(auth_type="oauth", api_key_env_vars=())
        with (
            patch.object(auth, "_load_auth_store", return_value={}),
            patch("pcbdraft.model.configuration.load_config", return_value={}),
            patch.object(auth, "PROVIDER_REGISTRY", {"docs": oauth_provider}),
            patch.object(
                auth,
                "read_credential_pool",
                return_value=[{"source": "MANUAL:setup"}],
            ) as pool,
            patch.object(
                auth,
                "normalize_credential_source",
                side_effect=lambda source: source,
            ) as normalize,
        ):
            self.assertTrue(auth.is_provider_explicitly_configured("docs"))
        pool.assert_called_once_with("docs")
        normalize.assert_called_once_with("manual:setup")

    def test_legacy_clear_and_deactivate_use_patched_store_hooks(self):
        store = {
            "active_provider": "docs",
            "providers": {"docs": {"token": "secret"}, "other": {}},
            "credential_pool": {"docs": [{"token": "secret"}]},
        }
        lock = Mock(side_effect=lambda: nullcontext())
        save = Mock()
        with (
            patch.object(auth, "_auth_store_lock", lock),
            patch.object(auth, "_load_auth_store", return_value=store),
            patch.object(auth, "_save_auth_store", save),
        ):
            self.assertTrue(auth.clear_provider_auth())

        self.assertEqual(store["active_provider"], None)
        self.assertEqual(store["providers"], {"other": {}})
        self.assertEqual(store["credential_pool"], {})
        save.assert_called_once_with(store)

        second_store = {"active_provider": "other", "providers": {"other": {}}}
        save.reset_mock()
        with (
            patch.object(auth, "_auth_store_lock", lock),
            patch.object(auth, "_load_auth_store", return_value=second_store),
            patch.object(auth, "_save_auth_store", save),
        ):
            auth.deactivate_provider()
        self.assertIsNone(second_store["active_provider"])
        self.assertEqual(second_store["providers"], {"other": {}})
        save.assert_called_once_with(second_store)

    def test_legacy_config_hint_keeps_validator_path_and_text(self):
        issues = [
            SimpleNamespace(
                severity="error",
                message="custom provider is malformed",
                hint="repair the provider\nsecond line",
            ),
            SimpleNamespace(
                severity="warning",
                message="fallback is missing",
                hint="",
            ),
        ]
        with patch(
            "pcbdraft.model.configuration.validate_config_structure",
            return_value=issues,
        ) as validate:
            hint = auth._get_config_hint_for_unknown_provider("missing")

        validate.assert_called_once_with()
        self.assertEqual(
            hint,
            "Config issue detected — run 'pcbdraft doctor' for full diagnostics:\n"
            "  [ERROR] custom provider is malformed\n"
            "    → repair the provider\n"
            "  [WARNING] fallback is missing",
        )


class AuthProviderStateTests(unittest.TestCase):
    def test_config_selection_covers_primary_and_moa_slots(self):
        cases = (
            {"model": {"provider": "docs"}},
            {"moa": {"reference_models": [{"provider": "docs"}]}},
            {"moa": {"aggregator": {"provider": "docs"}}},
            {
                "moa": {
                    "presets": {
                        "review": {"aggregator": {"provider": "docs"}},
                    }
                }
            },
        )
        for config in cases:
            with self.subTest(config=config):
                self.assertTrue(
                    auth_provider_state._config_selects_provider(config, "docs")
                )
        self.assertFalse(auth_provider_state._config_selects_provider({}, "docs"))


if __name__ == "__main__":
    unittest.main()
