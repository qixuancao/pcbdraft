from __future__ import annotations

import ast
import json
import os
import stat
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.model import auth, auth_qwen_oauth


class AuthQwenOAuthCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_symbols_remain(self):
        source = Path(auth_qwen_oauth.__file__).read_text(encoding="utf-8")
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
            "_qwen_cli_auth_path",
            "_read_qwen_cli_tokens",
            "_save_qwen_cli_tokens",
            "_qwen_access_token_is_expiring",
            "_refresh_qwen_cli_tokens",
            "_mark_qwen_oauth_active",
            "resolve_qwen_runtime_credentials",
            "get_qwen_auth_status",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(auth, name)))

    def test_legacy_token_round_trip_uses_atomic_replace_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / ".qwen" / "oauth_creds.json"
            replace = Mock(side_effect=os.replace)
            tokens = {
                "access_token": "access",
                "refresh_token": "refresh",
                "expiry_date": 123456,
            }
            with (
                patch.object(auth, "_qwen_cli_auth_path", return_value=auth_path),
                patch.object(auth, "atomic_replace", replace),
            ):
                saved = auth._save_qwen_cli_tokens(tokens)
                loaded = auth._read_qwen_cli_tokens()

            self.assertEqual(saved, auth_path)
            self.assertEqual(loaded, tokens)
            replace.assert_called_once()
            temporary, destination = replace.call_args.args
            self.assertEqual(destination, auth_path)
            self.assertFalse(temporary.exists())
            self.assertEqual(list(auth_path.parent.glob("oauth_creds.json.tmp.*")), [])
            self.assertEqual(json.loads(auth_path.read_text()), tokens)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(auth_path.stat().st_mode), 0o600)

    def test_refresh_uses_legacy_http_clock_and_save_hooks(self):
        response = SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "access_token": " refreshed-access ",
                "refresh_token": "refreshed-refresh",
                "expires_in": 30,
            },
        )
        post = Mock(return_value=response)
        save = Mock(return_value=Path("oauth_creds.json"))
        with (
            patch.object(auth, "httpx", SimpleNamespace(post=post)),
            patch.object(auth, "QWEN_OAUTH_TOKEN_URL", "https://token.example"),
            patch.object(auth, "QWEN_OAUTH_CLIENT_ID", "client-id"),
            patch.object(auth, "_save_qwen_cli_tokens", save),
            patch.object(auth.time, "time", return_value=1000.0),
        ):
            refreshed = auth._refresh_qwen_cli_tokens(
                {
                    "refresh_token": "old-refresh",
                    "token_type": "Custom",
                    "resource_url": "custom.example",
                },
                timeout_seconds=7.5,
            )

        post.assert_called_once_with(
            "https://token.example",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": "old-refresh",
                "client_id": "client-id",
            },
            timeout=7.5,
        )
        self.assertEqual(
            refreshed,
            {
                "access_token": "refreshed-access",
                "refresh_token": "refreshed-refresh",
                "token_type": "Custom",
                "resource_url": "custom.example",
                "expiry_date": 1_030_000,
            },
        )
        save.assert_called_once_with(refreshed)

    def test_expiry_check_uses_legacy_clock_and_fails_closed(self):
        with patch.object(auth.time, "time", return_value=100.0):
            self.assertFalse(auth._qwen_access_token_is_expiring(200_000, 50))
            self.assertTrue(auth._qwen_access_token_is_expiring(150_000, 50))
            self.assertTrue(auth._qwen_access_token_is_expiring("invalid", 50))

    def test_runtime_credentials_skip_refresh_for_fresh_token(self):
        auth_path = Path("profile/.qwen/oauth_creds.json")
        tokens = {"access_token": "fresh-access", "expiry_date": 999_000}
        expiring = Mock(return_value=False)
        refresh = Mock()
        with (
            patch.object(auth, "_read_qwen_cli_tokens", return_value=tokens),
            patch.object(auth, "_qwen_access_token_is_expiring", expiring),
            patch.object(auth, "_refresh_qwen_cli_tokens", refresh),
            patch.object(auth, "_qwen_cli_auth_path", return_value=auth_path),
            patch.object(
                auth.os,
                "getenv",
                return_value=" https://qwen.example/v1/ ",
            ) as getenv,
        ):
            credentials = auth.resolve_qwen_runtime_credentials(refresh_skew_seconds=45)

        expiring.assert_called_once_with(999_000, 45)
        refresh.assert_not_called()
        getenv.assert_called_once_with("PCBDRAFT_RUNTIME_QWEN_BASE_URL", "")
        self.assertEqual(
            credentials,
            {
                "provider": "qwen-oauth",
                "base_url": "https://qwen.example/v1",
                "api_key": "fresh-access",
                "source": "qwen-cli",
                "expires_at_ms": 999_000,
                "auth_file": str(auth_path),
            },
        )

    def test_runtime_credentials_force_refresh_through_legacy_hook(self):
        refresh = Mock(
            return_value={"access_token": "new-access", "expiry_date": 456_000}
        )
        with (
            patch.object(
                auth,
                "_read_qwen_cli_tokens",
                return_value={"access_token": "old-access", "expiry_date": 123_000},
            ),
            patch.object(auth, "_qwen_access_token_is_expiring") as expiring,
            patch.object(auth, "_refresh_qwen_cli_tokens", refresh),
            patch.object(
                auth,
                "_qwen_cli_auth_path",
                return_value=Path("oauth_creds.json"),
            ),
            patch.object(auth.os, "getenv", return_value=""),
        ):
            credentials = auth.resolve_qwen_runtime_credentials(force_refresh=True)

        expiring.assert_not_called()
        refresh.assert_called_once_with(
            {"access_token": "old-access", "expiry_date": 123_000}
        )
        self.assertEqual(credentials["api_key"], "new-access")
        self.assertEqual(credentials["base_url"], auth.DEFAULT_QWEN_BASE_URL)

    def test_mark_active_resolves_legacy_auth_store_hooks(self):
        store = {"providers": {}}
        lock = Mock(return_value=nullcontext())
        save_provider = Mock()
        save_store = Mock()
        with (
            patch.object(auth, "_auth_store_lock", lock),
            patch.object(auth, "_load_auth_store", return_value=store),
            patch.object(auth, "_save_provider_state", save_provider),
            patch.object(auth, "_save_auth_store", save_store),
        ):
            auth._mark_qwen_oauth_active({"base_url": "https://qwen.example/v1"})

        lock.assert_called_once_with()
        save_provider.assert_called_once_with(
            store,
            "qwen-oauth",
            {"base_url": "https://qwen.example/v1"},
        )
        save_store.assert_called_once_with(store)

    def test_status_uses_legacy_resolver_and_maps_auth_error(self):
        auth_path = Path("oauth_creds.json")
        with (
            patch.object(auth, "_qwen_cli_auth_path", return_value=auth_path),
            patch.object(
                auth,
                "resolve_qwen_runtime_credentials",
                side_effect=auth.AuthError(
                    "expired",
                    provider="qwen-oauth",
                    code="qwen_refresh_failed",
                ),
            ) as resolve,
        ):
            status = auth.get_qwen_auth_status()

        resolve.assert_called_once_with(refresh_if_expiring=True)
        self.assertEqual(
            status,
            {
                "logged_in": False,
                "auth_file": str(auth_path),
                "error": "expired",
            },
        )


if __name__ == "__main__":
    unittest.main()
