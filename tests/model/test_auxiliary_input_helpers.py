"""Focused coverage for pure auxiliary-client input helpers."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.model import auxiliary_client, auxiliary_input_helpers


class AuxiliaryInputHelpersTests(unittest.TestCase):
    def test_legacy_exports_match_acyclic_helper_module(self) -> None:
        self.assertIs(
            auxiliary_client._safe_isinstance,
            auxiliary_input_helpers._safe_isinstance,
        )
        self.assertIs(
            auxiliary_client._extract_url_query_params,
            auxiliary_input_helpers._extract_url_query_params,
        )

        tree = ast.parse(
            Path(auxiliary_input_helpers.__file__).read_text(encoding="utf-8")
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
        self.assertNotIn("pcbdraft.model.auxiliary_client", imports)

    def test_safe_isinstance_handles_non_type_patch_values(self) -> None:
        self.assertTrue(auxiliary_input_helpers._safe_isinstance(1, int))
        self.assertFalse(auxiliary_input_helpers._safe_isinstance("1", int))
        self.assertFalse(auxiliary_input_helpers._safe_isinstance(1, object()))

    def test_url_query_defaults_are_split_without_losing_fragment(self) -> None:
        clean, params = auxiliary_input_helpers._extract_url_query_params(
            "https://example.test/v1?api-version=2025-01-01&label=hello+world"
            "&repeat=first&repeat=second&blank=#section"
        )

        self.assertEqual(clean, "https://example.test/v1#section")
        self.assertEqual(
            params,
            {
                "api-version": "2025-01-01",
                "label": "hello world",
                "repeat": "first",
            },
        )
        self.assertEqual(
            auxiliary_input_helpers._extract_url_query_params(
                "https://example.test/v1#section"
            ),
            ("https://example.test/v1#section", None),
        )

    def test_legacy_safe_isinstance_patch_controls_host_dispatch(self) -> None:
        client = object()
        with patch.object(
            auxiliary_client, "_safe_isinstance", return_value=True
        ) as safe_isinstance:
            result = auxiliary_client._maybe_wrap_anthropic(
                client,
                model="model",
                api_key="key",
                base_url="https://example.test/v1",
            )

        self.assertIs(result, client)
        safe_isinstance.assert_called_once_with(
            client, auxiliary_client.AnthropicAuxiliaryClient
        )


if __name__ == "__main__":
    unittest.main()
