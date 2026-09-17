"""Focused coverage for the extracted authentication error formatting domain."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent import retry_utils
from pcbdraft.model import auth, auth_error_formatting


class AuthErrorFormattingTests(unittest.TestCase):
    def test_auth_reexports_same_objects_without_reverse_import(self) -> None:
        for name in (
            "AuthError",
            "is_rate_limited_auth_error",
            "_parse_retry_after_seconds",
            "format_auth_error",
            "_format_nous_entitlement_auth_error",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(auth, name),
                    getattr(auth_error_formatting, name),
                )

        tree = ast.parse(
            Path(auth_error_formatting.__file__).read_text(encoding="utf-8")
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

    def test_auth_error_fields_and_rate_limit_classification(self) -> None:
        limited = auth.AuthError(
            "quota reached",
            provider="openai-codex",
            code=auth.CODEX_RATE_LIMITED_CODE,
        )
        self.assertEqual(limited.provider, "openai-codex")
        self.assertEqual(limited.code, auth.CODEX_RATE_LIMITED_CODE)
        self.assertFalse(limited.relogin_required)
        self.assertTrue(auth.is_rate_limited_auth_error(limited))
        self.assertFalse(
            auth.is_rate_limited_auth_error(
                auth.AuthError(
                    "login",
                    code=auth.CODEX_RATE_LIMITED_CODE,
                    relogin_required=True,
                )
            )
        )
        self.assertFalse(auth.is_rate_limited_auth_error(RuntimeError("quota")))

    def test_rate_limit_classifier_uses_legacy_type_and_code_patch_paths(self) -> None:
        class PatchedAuthError(RuntimeError):
            def __init__(self, code: str) -> None:
                super().__init__("patched")
                self.code = code
                self.relogin_required = False

        error = PatchedAuthError("patched-rate-limit")
        with (
            patch.object(auth, "AuthError", PatchedAuthError),
            patch.object(auth, "CODEX_RATE_LIMITED_CODE", "patched-rate-limit"),
        ):
            self.assertTrue(auth.is_rate_limited_auth_error(error))
            self.assertEqual(auth.format_auth_error(error), "patched")

    def test_retry_after_parser_delegates_and_returns_whole_seconds(self) -> None:
        with patch.object(
            retry_utils, "parse_retry_after_seconds", return_value=2.9
        ) as parse:
            self.assertEqual(auth._parse_retry_after_seconds({"retry-after": "2.9"}), 2)
        parse.assert_called_once_with({"retry-after": "2.9"})

        with patch.object(retry_utils, "parse_retry_after_seconds", return_value=None):
            self.assertIsNone(auth._parse_retry_after_seconds({}))

    def test_formatting_covers_public_guidance_branches(self) -> None:
        self.assertEqual(auth.format_auth_error(ValueError("plain")), "plain")
        self.assertEqual(
            auth.format_auth_error(
                auth.AuthError(
                    "login expired",
                    relogin_required=True,
                )
            ),
            "login expired Run `pcbdraft connect` to re-authenticate.",
        )
        self.assertEqual(
            auth.format_auth_error(
                auth.AuthError("missing", code="subscription_required")
            ),
            "No active paid subscription found. Please purchase/activate a subscription, then retry.",
        )
        self.assertEqual(
            auth.format_auth_error(
                auth.AuthError("empty", code="insufficient_credits")
            ),
            "Subscription credits are exhausted. Top up/renew credits, then retry.",
        )
        self.assertEqual(
            auth.format_auth_error(
                auth.AuthError("offline", code="temporarily_unavailable")
            ),
            "offline Please retry in a few seconds.",
        )
        self.assertEqual(
            auth.format_auth_error(auth.AuthError("unchanged", code="other")),
            "unchanged",
        )

    def test_formatting_uses_legacy_classifier_and_nous_formatter_paths(self) -> None:
        generic = auth.AuthError("generic")
        with patch.object(
            auth, "is_rate_limited_auth_error", return_value=True
        ) as classify:
            self.assertEqual(auth.format_auth_error(generic), "generic")
        classify.assert_called_once_with(generic)

        for code in (
            "subscription_required",
            "insufficient_credits",
            "subscription_expired",
            "no_usable_credits",
            "account_missing",
            "member_spend_cap_exceeded",
        ):
            error = auth.AuthError("entitlement", provider="nous", code=code)
            with (
                self.subTest(code=code),
                patch.object(
                    auth,
                    "_format_nous_entitlement_auth_error",
                    return_value=f"nous:{code}",
                ) as format_nous,
            ):
                self.assertEqual(auth.format_auth_error(error), f"nous:{code}")
                format_nous.assert_called_once_with(error)

    def test_nous_formatter_enriches_or_falls_back(self) -> None:
        from pcbdraft.interfaces.tui import nous_account

        error = auth.AuthError(
            "credits unavailable",
            provider="nous",
            code="insufficient_credits",
        )
        account = {"plan": "test"}
        with (
            patch.object(
                nous_account,
                "get_nous_portal_account_info",
                return_value=account,
            ) as get_account,
            patch.object(
                nous_account,
                "format_nous_portal_entitlement_message",
                return_value="account-specific guidance",
            ) as format_account,
        ):
            self.assertEqual(
                auth._format_nous_entitlement_auth_error(error),
                "account-specific guidance",
            )
        get_account.assert_called_once_with(force_fresh=True)
        format_account.assert_called_once_with(
            account,
            capability="Nous model access",
        )

        with patch.object(
            nous_account,
            "get_nous_portal_account_info",
            side_effect=RuntimeError("offline"),
        ):
            self.assertEqual(
                auth._format_nous_entitlement_auth_error(error),
                "credits unavailable Check credits or billing in Nous Portal, then retry.",
            )


if __name__ == "__main__":
    unittest.main()
