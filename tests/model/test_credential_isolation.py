from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from pcbdraft.model import anthropic_adapter as anthropic
from pcbdraft.model import auth, credential_pool


class CredentialIsolationTests(unittest.TestCase):
    def test_anthropic_compatibility_reader_never_probes_cli_store(self) -> None:
        with (
            patch.object(
                anthropic, "_read_claude_code_credentials_from_keychain"
            ) as keychain_read,
            patch.object(
                anthropic, "_read_claude_code_credentials_from_file"
            ) as file_read,
        ):
            resolved = anthropic.read_claude_code_credentials()

        self.assertIsNone(resolved)
        keychain_read.assert_not_called()
        file_read.assert_not_called()

    def test_anthropic_explicit_environment_credentials_remain_usable(self) -> None:
        for variable, expected in (
            ("ANTHROPIC_TOKEN", "TEST_EXPLICIT_OAUTH"),
            ("CLAUDE_CODE_OAUTH_TOKEN", "TEST_EXPLICIT_SETUP_TOKEN"),
            ("ANTHROPIC_API_KEY", "TEST_EXPLICIT_API_KEY"),
        ):
            values = {
                "ANTHROPIC_TOKEN": "",
                "CLAUDE_CODE_OAUTH_TOKEN": "",
                "ANTHROPIC_API_KEY": "",
            }
            values[variable] = expected
            with (
                self.subTest(variable=variable),
                patch.object(
                    anthropic,
                    "_getenv",
                    side_effect=lambda name, default="", _values=values: _values.get(
                        name, default
                    ),
                ),
                patch.object(
                    anthropic, "read_claude_code_credentials"
                ) as external_read,
                patch.object(anthropic, "_resolve_anthropic_pool_token") as owned_pool,
            ):
                resolved = anthropic.resolve_anthropic_token()

            self.assertEqual(resolved, expected)
            external_read.assert_not_called()
            owned_pool.assert_not_called()

    def test_anthropic_resolver_uses_only_explicit_or_owned_credentials(self) -> None:
        values = {
            "ANTHROPIC_TOKEN": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "ANTHROPIC_API_KEY": "",
        }
        with (
            patch.object(
                anthropic,
                "_getenv",
                side_effect=lambda name, default="": values.get(name, default),
            ),
            patch.object(anthropic, "read_claude_code_credentials") as external_read,
            patch.object(
                anthropic,
                "_resolve_anthropic_pool_token",
                return_value="TEST_PCBDRAFT_OWNED_TOKEN",
            ),
        ):
            resolved = anthropic.resolve_anthropic_token()

        self.assertEqual(resolved, "TEST_PCBDRAFT_OWNED_TOKEN")
        external_read.assert_not_called()

    def test_anthropic_legacy_refresh_cannot_touch_external_cli_state(self) -> None:
        placeholder = {
            "accessToken": "TEST_EXTERNAL_ACCESS",
            "refreshToken": "TEST_EXTERNAL_REFRESH",
            "expiresAt": 0,
        }
        with (
            patch.object(anthropic, "read_claude_code_credentials") as external_read,
            patch.object(anthropic, "refresh_anthropic_oauth_pure") as network_refresh,
            patch.object(anthropic, "_write_claude_code_credentials") as external_write,
        ):
            resolved = anthropic._refresh_oauth_token(placeholder)

        self.assertIsNone(resolved)
        external_read.assert_not_called()
        network_refresh.assert_not_called()
        external_write.assert_not_called()

    def test_anthropic_pool_seed_does_not_read_claude_code(self) -> None:
        with (
            patch.object(credential_pool, "_load_auth_store", return_value={}),
            patch.object(auth, "is_provider_explicitly_configured", return_value=True),
            patch.object(credential_pool, "load_env", return_value={}),
            patch.object(credential_pool, "_get_secret", return_value=""),
            patch.object(anthropic, "read_hermes_oauth_credentials", return_value=None),
            patch.object(anthropic, "read_claude_code_credentials") as external_read,
        ):
            changed, active = credential_pool._seed_from_singletons("anthropic", [])

        self.assertFalse(changed)
        self.assertEqual(active, set())
        external_read.assert_not_called()

    def test_anthropic_connect_uses_pcbdraft_owned_oauth_flow(self) -> None:
        from pcbdraft.interfaces.tui import main as tui_main

        placeholder = {
            "access_token": "TEST_PCBDRAFT_ACCESS",
            "refresh_token": "TEST_PCBDRAFT_REFRESH",
            "expires_at_ms": 1,
        }
        with (
            patch.object(
                anthropic,
                "run_hermes_oauth_login_pure",
                return_value=placeholder,
            ) as owned_login,
            patch.object(anthropic, "save_hermes_oauth_credentials") as owned_write,
            patch.object(anthropic, "run_oauth_setup_token") as external_cli,
            redirect_stdout(StringIO()),
        ):
            connected = tui_main._run_anthropic_oauth_flow(object())

        self.assertTrue(connected)
        owned_login.assert_called_once_with()
        owned_write.assert_called_once_with(placeholder)
        external_cli.assert_not_called()

    def test_codex_missing_owned_state_never_recovers_from_cli(self) -> None:
        missing = auth.AuthError(
            "PCBDraft-owned Codex credentials are incomplete; reconnect in PCBDraft.",
            provider="openai-codex",
            code="codex_auth_missing_refresh_token",
            relogin_required=True,
        )
        with (
            patch.object(auth, "_read_codex_tokens", side_effect=missing),
            patch.object(auth, "_pool_codex_access_token", return_value=""),
            patch.object(auth, "_codex_pool_rate_limit_status", return_value=None),
            patch.object(auth, "_recover_codex_tokens_from_cli") as external_recovery,
            self.assertRaises(auth.AuthError) as raised,
        ):
            auth.resolve_codex_runtime_credentials(refresh_if_expiring=False)

        self.assertIs(raised.exception, missing)
        external_recovery.assert_not_called()

    def test_codex_compatibility_recovery_never_reads_or_copies_cli_state(self) -> None:
        with (
            patch.object(auth, "_import_codex_cli_tokens") as external_read,
            patch.object(auth, "_save_codex_tokens") as owned_write,
        ):
            recovered = auth._recover_codex_tokens_from_cli("TEST_REASON")

        self.assertIsNone(recovered)
        external_read.assert_not_called()
        owned_write.assert_not_called()

    def test_codex_refresh_rejection_never_recovers_from_cli(self) -> None:
        rejected = auth.AuthError(
            "PCBDraft-owned Codex refresh was rejected; reconnect in PCBDraft.",
            provider="openai-codex",
            code="codex_refresh_rejected",
            relogin_required=True,
        )
        with (
            patch.object(auth, "refresh_codex_oauth_pure", side_effect=rejected),
            patch.object(auth, "_recover_codex_tokens_from_cli") as external_recovery,
            patch.object(auth, "_save_codex_tokens") as owned_write,
            self.assertRaises(auth.AuthError) as raised,
        ):
            auth._refresh_codex_auth_tokens(
                {
                    "access_token": "TEST_PCBDRAFT_ACCESS",
                    "refresh_token": "TEST_PCBDRAFT_REFRESH",
                },
                1.0,
            )

        self.assertIs(raised.exception, rejected)
        external_recovery.assert_not_called()
        owned_write.assert_not_called()

    def test_codex_owned_state_remains_usable(self) -> None:
        with (
            patch.object(
                auth,
                "_read_codex_tokens",
                return_value={
                    "tokens": {
                        "access_token": "TEST_PCBDRAFT_ACCESS",
                        "refresh_token": "TEST_PCBDRAFT_REFRESH",
                    },
                    "last_refresh": "2026-09-06T00:00:00Z",
                },
            ),
            patch.object(auth, "_codex_access_token_is_expiring", return_value=False),
            patch.object(auth, "_recover_codex_tokens_from_cli") as external_recovery,
        ):
            resolved = auth.resolve_codex_runtime_credentials()

        self.assertEqual(resolved["api_key"], "TEST_PCBDRAFT_ACCESS")
        self.assertEqual(resolved["source"], "hermes-auth-store")
        external_recovery.assert_not_called()


if __name__ == "__main__":
    unittest.main()
