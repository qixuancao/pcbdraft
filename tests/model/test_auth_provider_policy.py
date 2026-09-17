"""Focused coverage for extracted provider credential policy helpers."""

from __future__ import annotations

import ast
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.model import auth, auth_provider_policy


class AuthProviderPolicyTests(unittest.TestCase):
    def test_auth_reexports_same_objects_without_reverse_import(self) -> None:
        names = (
            "_parse_iso_timestamp",
            "_is_expiring",
            "_coerce_ttl_seconds",
            "_optional_base_url",
            "_migrate_stale_nous_portal_url",
            "_validate_nous_inference_url_from_network",
            "_nous_inference_env_override",
            "_nous_portal_env_override",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(auth, name),
                    getattr(auth_provider_policy, name),
                )

        tree = ast.parse(
            Path(auth_provider_policy.__file__).read_text(encoding="utf-8")
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

    def test_parse_iso_timestamp_preserves_utc_and_naive_semantics(self) -> None:
        expected = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
        self.assertEqual(auth._parse_iso_timestamp("2030-01-02T03:04:05Z"), expected)
        self.assertEqual(auth._parse_iso_timestamp("2030-01-02T03:04:05"), expected)
        for value in (None, "", "  ", "not-a-time", 123):
            with self.subTest(value=value):
                self.assertIsNone(auth._parse_iso_timestamp(value))

    def test_time_policy_uses_legacy_parser_and_clock_paths(self) -> None:
        with (
            patch.object(auth, "_parse_iso_timestamp", return_value=120.0) as parse,
            patch.object(auth.time, "time", return_value=100.0),
        ):
            self.assertTrue(auth._is_expiring("deadline", 20))
            self.assertFalse(auth._is_expiring("deadline", 19))
        parse.assert_called_with("deadline")

        with patch.object(auth, "_parse_iso_timestamp", return_value=None):
            self.assertTrue(auth._is_expiring("missing", 0))

    def test_timestamp_parser_uses_legacy_datetime_path(self) -> None:
        parsed = SimpleNamespace(tzinfo=object(), timestamp=Mock(return_value=42.5))
        datetime_type = SimpleNamespace(fromisoformat=Mock(return_value=parsed))
        with patch.object(auth, "datetime", datetime_type):
            self.assertEqual(auth._parse_iso_timestamp("patched"), 42.5)
        datetime_type.fromisoformat.assert_called_once_with("patched")

    def test_ttl_and_optional_url_normalization_are_unchanged(self) -> None:
        for raw, expected in (("12", 12), (-1, 0), (None, 0), ("bad", 0)):
            with self.subTest(raw=raw):
                self.assertEqual(auth._coerce_ttl_seconds(raw), expected)

        for raw, expected in (
            (" https://example.test/path/// ", "https://example.test/path"),
            ("", None),
            (" /// ", None),
            (None, None),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(auth._optional_base_url(raw), expected)

    def test_stale_portal_migration_uses_legacy_policy_constants(self) -> None:
        providers = {"nous": {"portal_base_url": "https://stale.test/path"}}
        with (
            patch.object(auth, "_NOUS_STALE_PORTAL_HOSTS", frozenset({"stale.test"})),
            patch.object(auth, "DEFAULT_NOUS_PORTAL_URL", "https://portal.test"),
        ):
            auth._migrate_stale_nous_portal_url(providers)

        self.assertEqual(providers["nous"]["portal_base_url"], "https://portal.test")
        untouched = {"other": {"portal_base_url": "https://stale.test"}}
        auth._migrate_stale_nous_portal_url(untouched)
        self.assertEqual(
            untouched, {"other": {"portal_base_url": "https://stale.test"}}
        )

    def test_network_inference_url_uses_legacy_allowlist_and_logger(self) -> None:
        with patch.object(
            auth, "_ALLOWED_NOUS_INFERENCE_HOSTS", frozenset({"allowed.test"})
        ):
            self.assertEqual(
                auth._validate_nous_inference_url_from_network(
                    " https://allowed.test/v1/// "
                ),
                "https://allowed.test/v1",
            )
            self.assertIsNone(
                auth._validate_nous_inference_url_from_network(
                    "https://rejected.test/v1"
                )
            )

        logger = Mock()
        with patch.object(auth, "logger", logger):
            self.assertIsNone(
                auth._validate_nous_inference_url_from_network(
                    "http://inference-api.nousresearch.com/v1"
                )
            )
        logger.warning.assert_called_once()

    def test_environment_overrides_preserve_precedence_and_old_patch_paths(
        self,
    ) -> None:
        values = {
            "NOUS_INFERENCE_BASE_URL": " https://inference.test/v1/ ",
            "PCBDRAFT_RUNTIME_PORTAL_BASE_URL": " https://runtime.test/ ",
            "NOUS_PORTAL_BASE_URL": "https://fallback.test/",
        }
        with patch.object(auth.os, "getenv", side_effect=values.get):
            self.assertEqual(
                auth._nous_inference_env_override(), "https://inference.test/v1"
            )
            self.assertEqual(auth._nous_portal_env_override(), "https://runtime.test")

        with (
            patch.object(
                auth.os,
                "getenv",
                side_effect=lambda name: (
                    "fallback" if name == "NOUS_PORTAL_BASE_URL" else ""
                ),
            ),
            patch.object(
                auth, "_optional_base_url", return_value="normalized"
            ) as normalize,
        ):
            self.assertEqual(auth._nous_portal_env_override(), "normalized")
        normalize.assert_called_once_with("fallback")


if __name__ == "__main__":
    unittest.main()
