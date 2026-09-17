from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pcbdraft.model import auxiliary_client, auxiliary_fallbacks


class AuxiliaryFallbackContractTests(unittest.TestCase):
    def setUp(self) -> None:
        auxiliary_client._reset_aux_unhealthy_cache()

    def tearDown(self) -> None:
        auxiliary_client._reset_aux_unhealthy_cache()

    def test_extracted_module_has_no_reverse_import(self):
        source = Path(auxiliary_fallbacks.__file__).read_text(encoding="utf-8")

        self.assertNotIn("import auxiliary_client", source)
        self.assertNotIn("from pcbdraft.model.auxiliary_client", source)

    def test_legacy_symbols_keep_identity_and_client_ownership_boundary(self):
        self.assertIs(
            auxiliary_client._normalize_chain_label,
            auxiliary_fallbacks._normalize_chain_label,
        )
        self.assertIs(
            auxiliary_client._call_fallback_candidate_sync,
            auxiliary_fallbacks._call_fallback_candidate_sync,
        )
        self.assertIs(
            auxiliary_client._try_main_fallback_chain,
            auxiliary_fallbacks._try_main_fallback_chain,
        )
        self.assertIs(
            auxiliary_client._FallbackDestination,
            auxiliary_fallbacks._FallbackDestination,
        )
        self.assertIs(
            auxiliary_client._aux_unhealthy_until,
            auxiliary_fallbacks._aux_unhealthy_until,
        )
        self.assertEqual(
            inspect.getmodule(auxiliary_client._resolve_fallback_entry),
            auxiliary_client,
        )
        self.assertEqual(
            inspect.getmodule(auxiliary_client._get_cached_client),
            auxiliary_client,
        )

    def test_unhealthy_mark_expires_at_ttl_through_legacy_clock_path(self):
        with patch.object(auxiliary_client.time, "time", return_value=100.0):
            auxiliary_client._mark_provider_unhealthy("codex", ttl=10.0)

        self.assertEqual(
            auxiliary_client._aux_unhealthy_until,
            {"openai-codex": 110.0},
        )
        with patch.object(auxiliary_client.time, "time", return_value=109.9):
            self.assertTrue(auxiliary_client._is_provider_unhealthy("openai-codex"))
        with patch.object(auxiliary_client.time, "time", return_value=110.0):
            self.assertFalse(auxiliary_client._is_provider_unhealthy("openai-codex"))
        self.assertEqual(auxiliary_client._aux_unhealthy_until, {})

    def test_payment_chain_uses_legacy_hooks_and_skips_unhealthy_candidate(self):
        client = object()
        skipped_candidate = MagicMock(return_value=(object(), "unused"))
        healthy_candidate = MagicMock(return_value=(client, "model-b"))

        with (
            patch.object(auxiliary_client, "_read_main_provider", return_value=""),
            patch.object(
                auxiliary_client,
                "_get_provider_chain",
                return_value=[
                    ("openrouter", skipped_candidate),
                    ("nous", healthy_candidate),
                ],
            ),
            patch.object(
                auxiliary_client,
                "_is_provider_unhealthy",
                side_effect=lambda label: label == "openrouter",
            ),
            patch.object(auxiliary_client, "_log_skip_unhealthy") as log_skip,
        ):
            result = auxiliary_client._try_payment_fallback(
                "custom", task="compression"
            )

        self.assertEqual(result, (client, "model-b", "nous"))
        skipped_candidate.assert_not_called()
        healthy_candidate.assert_called_once_with()
        log_skip.assert_called_once_with("openrouter", "compression")

    def test_configured_chain_continues_after_failed_candidate(self):
        client = object()
        chain = [
            {"provider": "provider-a", "model": "model-a"},
            {"provider": "provider-b", "model": "model-b"},
        ]

        with (
            patch.object(
                auxiliary_client,
                "_get_auxiliary_task_config",
                return_value={"fallback_chain": chain},
            ),
            patch.object(
                auxiliary_client,
                "_resolve_fallback_entry",
                side_effect=[(None, None), (client, "resolved-b")],
            ) as resolve_entry,
            patch.object(
                auxiliary_client,
                "_task_minimum_context_length",
                return_value=None,
            ),
        ):
            result = auxiliary_client._try_configured_fallback_chain(
                "title_generation",
                "primary",
            )

        self.assertEqual(
            result,
            (client, "resolved-b", "fallback_chain[1](provider-b)"),
        )
        self.assertEqual(resolve_entry.call_count, 2)


if __name__ == "__main__":
    unittest.main()
