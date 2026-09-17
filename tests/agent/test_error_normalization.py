"""Focused tests for AIAgent provider-error normalization."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pcbdraft.agent import loop
from pcbdraft.agent.error_normalization import ErrorNormalizationMixin
from pcbdraft.agent.loop import AIAgent


class ErrorNormalizationCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_agent_inherits_all_extracted_methods(self) -> None:
        self.assertTrue(issubclass(AIAgent, ErrorNormalizationMixin))
        for name in (
            "_is_entitlement_failure",
            "_decorate_xai_entitlement_error",
            "_coerce_api_error_detail",
            "_summarize_api_error",
            "_mask_api_key_for_logs",
            "_clean_error_message",
            "_extract_api_error_context",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name), getattr(ErrorNormalizationMixin, name)
                )

    def test_internal_static_method_patches_remain_late_bound(self) -> None:
        error = Exception("raw")
        error.body = {"error": {"message": {"detail": "nested"}}}
        error.status_code = 403

        with (
            patch.object(
                AIAgent, "_coerce_api_error_detail", return_value="coerced"
            ) as coerce,
            patch.object(
                AIAgent,
                "_decorate_xai_entitlement_error",
                side_effect=lambda detail: f"decorated:{detail}",
            ) as decorate,
        ):
            self.assertEqual(
                self.agent._summarize_api_error(error),
                "decorated:HTTP 403: coerced",
            )
        coerce.assert_called_once_with({"detail": "nested"})
        decorate.assert_called_once_with("HTTP 403: coerced")

    def test_loop_json_regex_and_redaction_patches_remain_dynamic(self) -> None:
        fake_json = SimpleNamespace(dumps=MagicMock(return_value="encoded"))
        with patch.object(loop, "json", fake_json):
            self.assertEqual(
                self.agent._coerce_api_error_detail({"other": 1}), "encoded"
            )
        fake_json.dumps.assert_called_once_with(
            {"other": 1}, ensure_ascii=False, sort_keys=True
        )

        fake_re = SimpleNamespace(search=MagicMock(return_value=None))
        with patch.object(loop, "re", fake_re):
            self.assertEqual(
                self.agent._summarize_api_error(Exception("<html>bad</html>")),
                "HTML error page (title not found)",
            )
        self.assertEqual(fake_re.search.call_count, 2)

        error = Exception("wrapped")
        error.response = SimpleNamespace(text='{"message":"api-key secret"}')
        with patch.object(
            loop, "redact_sensitive_text", return_value="redacted"
        ) as redact:
            self.assertEqual(self.agent._summarize_api_error(error), "redacted")
        redact.assert_called_once_with("api-key secret")

    def test_error_context_helper_patch_path_remains_dynamic(self) -> None:
        error = Exception("provider error")
        with patch(
            "pcbdraft.agent.agent_runtime_helpers.extract_api_error_context",
            return_value={"message": "normalized"},
        ) as extract:
            self.assertEqual(
                self.agent._extract_api_error_context(error),
                {"message": "normalized"},
            )
        extract.assert_called_once_with(error)


class ErrorNormalizationBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_entitlement_detection_rejects_stale_token_signals(self) -> None:
        entitlement = {
            "message": "You do not have an active Grok subscription",
        }
        self.assertTrue(self.agent._is_entitlement_failure(entitlement, 403))
        self.assertFalse(self.agent._is_entitlement_failure(entitlement, 429))

        stale_token = {
            "code": "The caller does not have permission to use Grok",
            "error": "[WKE=unauthenticated:expired]",
        }
        self.assertFalse(self.agent._is_entitlement_failure(stale_token, 403))
        stale_token["error"] = "OAuth2 access token could not be validated"
        self.assertFalse(self.agent._is_entitlement_failure(stale_token, 401))

    def test_xai_entitlement_decoration_is_specific_and_idempotent(self) -> None:
        raw = "You do not have an active Grok subscription"
        decorated = self.agent._decorate_xai_entitlement_error(raw)
        self.assertIn("X Premium+ does NOT include", decorated)
        self.assertEqual(
            self.agent._decorate_xai_entitlement_error(decorated), decorated
        )
        self.assertEqual(
            self.agent._decorate_xai_entitlement_error("ordinary provider error"),
            "ordinary provider error",
        )

    def test_structured_error_detail_coercion(self) -> None:
        self.assertEqual(
            self.agent._coerce_api_error_detail(
                {"error": {"detail": ["first", {"message": "second"}]}}
            ),
            "first; second",
        )
        self.assertEqual(
            self.agent._coerce_api_error_detail([None, "one", {"code": "two"}]),
            "one; two",
        )

    def test_error_summary_handles_network_html_and_stream_parse_errors(self) -> None:
        outer = Exception("Connection error")
        outer.__cause__ = OSError("Temporary failure in name resolution")
        self.assertIn("You may be offline", self.agent._summarize_api_error(outer))

        html_error = Exception(
            "<!DOCTYPE html><title>Gateway unavailable</title>"
            "Cloudflare Ray ID: <strong>abc123</strong>"
        )
        html_error.status_code = 503
        self.assertEqual(
            self.agent._summarize_api_error(html_error),
            "HTTP 503 — Gateway unavailable — Ray abc123",
        )
        self.assertEqual(
            self.agent._summarize_api_error(
                ValueError("Expected ident at line 2 column 4")
            ),
            "Malformed provider streaming response: Expected ident at line 2 column 4",
        )

    def test_api_key_masking_never_invokes_callable_provider(self) -> None:
        token_provider = MagicMock(return_value="secret")
        self.assertEqual(
            self.agent._mask_api_key_for_logs(token_provider), "<entra-id-bearer>"
        )
        token_provider.assert_not_called()
        self.assertIsNone(self.agent._mask_api_key_for_logs(None))
        self.assertEqual(self.agent._mask_api_key_for_logs("short"), "***")
        self.assertEqual(
            self.agent._mask_api_key_for_logs("1234567890abcdef"),
            "12345678...cdef",
        )

    def test_clean_error_message_handles_html_whitespace_and_length(self) -> None:
        self.assertEqual(self.agent._clean_error_message(""), "Unknown error")
        self.assertEqual(
            self.agent._clean_error_message("<!DOCTYPE html><html>bad</html>"),
            "Service temporarily unavailable (HTML error page returned)",
        )
        self.assertEqual(
            self.agent._clean_error_message("line one\n  line two"),
            "line one line two",
        )
        self.assertEqual(len(self.agent._clean_error_message("x" * 200)), 153)


if __name__ == "__main__":
    unittest.main()
