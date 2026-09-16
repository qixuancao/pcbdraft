from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.tools import mcp_connection_policy, mcp_tool


class MCPConnectionPolicyCompatibilityTests(unittest.TestCase):
    def test_legacy_module_reexports_policy_symbols(self):
        names = (
            "InvalidMcpUrlError",
            "NonMcpEndpointError",
            "_unwrap_exception_group",
            "_contains_only_cancellation",
            "_validate_remote_mcp_url",
            "_resolve_client_cert",
            "_resolve_identity_header",
            "_make_redirect_header_stripper",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(mcp_tool, name),
                    getattr(mcp_connection_policy, name),
                )

    def test_legacy_wrappers_preserve_monkeypatch_dependencies(self):
        with patch.object(mcp_tool, "_is_auth_error", return_value=True) as auth:
            self.assertEqual(
                mcp_tool._classify_mcp_failure(RuntimeError("transport")),
                "permanent",
            )
        auth.assert_called_once()

        with patch.object(
            mcp_tool,
            "_resolve_identity_header",
            return_value=("X-Identity", "alice"),
        ) as resolver:
            headers = mcp_tool._apply_identity_header("docs", {}, {})
        self.assertEqual(headers, {"X-Identity": "alice"})
        resolver.assert_called_once_with("docs", {})

        with patch.object(
            mcp_tool,
            "_sanitize_error",
            side_effect=lambda text: f"safe:{text}",
        ):
            self.assertEqual(
                mcp_tool._format_connect_error(RuntimeError("failed")),
                "safe:failed",
            )


class MCPConnectionPolicyTests(unittest.TestCase):
    def test_remote_url_validation_preserves_http_constraints_and_text(self):
        self.assertEqual(
            mcp_connection_policy._validate_remote_mcp_url(
                "docs", "  https://example.test:8443/mcp?q=1  "
            ),
            "https://example.test:8443/mcp?q=1",
        )
        invalid = (
            (None, "expected a string"),
            ("", "empty url"),
            ("example.test/mcp", "scheme must be http or https"),
            ("file:///tmp/mcp", "scheme must be http or https"),
            ("https:///mcp", "missing host"),
            ("http://:8080", "missing hostname"),
        )
        for value, message in invalid:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    mcp_connection_policy.InvalidMcpUrlError,
                    message,
                ),
            ):
                mcp_connection_policy._validate_remote_mcp_url("docs", value)

    def test_client_certificate_resolution_validates_shapes_and_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cert = root / "client.pem"
            key = root / "client.key"
            cert.write_text("cert", encoding="utf-8")
            key.write_text("key", encoding="utf-8")

            self.assertEqual(
                mcp_connection_policy._resolve_client_cert(
                    "docs", {"client_cert": str(cert)}
                ),
                str(cert),
            )
            self.assertEqual(
                mcp_connection_policy._resolve_client_cert(
                    "docs",
                    {"client_cert": str(cert), "client_key": str(key)},
                ),
                (str(cert), str(key)),
            )
            self.assertEqual(
                mcp_connection_policy._resolve_client_cert(
                    "docs", {"client_cert": [str(cert), str(key), "secret"]}
                ),
                (str(cert), str(key), "secret"),
            )
            with self.assertRaisesRegex(FileNotFoundError, "client_cert not found"):
                mcp_connection_policy._resolve_client_cert(
                    "docs", {"client_cert": str(root / "missing.pem")}
                )
            with self.assertRaisesRegex(ValueError, "not both"):
                mcp_connection_policy._resolve_client_cert(
                    "docs",
                    {
                        "client_cert": [str(cert), str(key)],
                        "client_key": str(key),
                    },
                )

    def test_identity_header_never_overrides_explicit_header(self):
        config = {
            "identity_header": {
                "name": "X-User-Id",
                "value_from": "static",
                "value": "generated",
            }
        }
        headers = {"x-user-id": "explicit"}
        self.assertIs(
            mcp_connection_policy._apply_identity_header("docs", config, headers),
            headers,
        )
        self.assertEqual(headers, {"x-user-id": "explicit"})
        self.assertIsNone(
            mcp_connection_policy._resolve_identity_header(
                "docs", {"identity_header": {"name": "X-User-Id"}}
            )
        )

    def test_redirect_hook_strips_cross_origin_credentials(self):
        origin = SimpleNamespace(scheme="https", host="example.test", port=443)
        cross_origin_headers = {
            "authorization": "Bearer token",
            "x-package-secret": "secret",
            "accept": "application/json",
        }
        response = SimpleNamespace(
            is_redirect=True,
            next_request=SimpleNamespace(
                url=SimpleNamespace(scheme="https", host="other.test", port=443),
                headers=cross_origin_headers,
            ),
        )
        hook = mcp_connection_policy._make_redirect_header_stripper(
            origin,
            strict=True,
            configured_header_names=frozenset({"x-package-secret"}),
        )
        asyncio.run(hook(response))
        self.assertEqual(cross_origin_headers, {"accept": "application/json"})

        same_origin_headers = {"authorization": "Bearer token"}
        same_origin = SimpleNamespace(
            is_redirect=True,
            next_request=SimpleNamespace(url=origin, headers=same_origin_headers),
        )
        asyncio.run(hook(same_origin))
        self.assertEqual(same_origin_headers, {"authorization": "Bearer token"})

    def test_failure_classification_unwraps_groups_and_keeps_fatal_signals(self):
        self.assertEqual(
            mcp_connection_policy._classify_mcp_failure(
                mcp_connection_policy.InvalidMcpUrlError("bad url")
            ),
            "permanent",
        )
        self.assertEqual(
            mcp_connection_policy._classify_mcp_failure(FileNotFoundError("missing")),
            "permanent",
        )
        response = SimpleNamespace(status_code=403)
        http_error = RuntimeError("forbidden")
        http_error.response = response
        self.assertEqual(
            mcp_connection_policy._classify_mcp_failure(http_error),
            "permanent",
        )
        grouped = BaseExceptionGroup(
            "transport",
            [asyncio.CancelledError(), RuntimeError("connection reset")],
        )
        self.assertEqual(
            str(mcp_connection_policy._unwrap_exception_group(grouped)),
            "connection reset",
        )
        self.assertEqual(
            mcp_connection_policy._classify_mcp_failure(grouped),
            "transient",
        )
        with self.assertRaises(KeyboardInterrupt):
            mcp_connection_policy._unwrap_exception_group(
                BaseExceptionGroup("fatal", [KeyboardInterrupt()])
            )

    def test_connect_error_formatting_is_actionable_and_redacted(self):
        missing = FileNotFoundError(2, "No such file or directory", "npx")
        rendered = mcp_connection_policy._format_connect_error(missing)
        self.assertIn("missing executable 'npx'", rendered)
        self.assertIn("ensure Node.js is installed", rendered)

        secret = RuntimeError("request failed with sk-supersecret")
        rendered = mcp_connection_policy._format_connect_error(secret)
        self.assertEqual(rendered, "request failed with [REDACTED]")


if __name__ == "__main__":
    unittest.main()
