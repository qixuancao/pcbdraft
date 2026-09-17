"""Focused auxiliary response-projection and compatibility coverage."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from pcbdraft.model import auxiliary_client, auxiliary_response_projection


class AuxiliaryResponseProjectionTests(unittest.TestCase):
    def test_module_has_no_reverse_import_and_host_keeps_accounting_boundary(
        self,
    ) -> None:
        source = Path(auxiliary_response_projection.__file__).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("pcbdraft.model.auxiliary_client", imports)

        for name in (
            "_obj_get",
            "_extract_aux_response_text",
            "_recover_aux_response_message",
            "extract_content_or_reasoning",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(auxiliary_response_projection, name)))
                self.assertTrue(callable(getattr(auxiliary_client, name)))

        for retained in (
            "_validate_llm_response",
            "_complete_relay_auxiliary_call",
            "call_llm",
            "resolve_provider_client",
            "_get_cached_client",
        ):
            with self.subTest(retained=retained):
                self.assertIn(retained, auxiliary_client.__dict__)
                self.assertNotIn(retained, auxiliary_response_projection.__dict__)

    def test_responses_text_projection_reads_dict_and_object_shapes(self) -> None:
        direct = SimpleNamespace(output_text="  direct response  ")
        self.assertEqual(
            auxiliary_response_projection._extract_aux_response_text(direct),
            "direct response",
        )

        nested = {
            "output": [
                {"type": "reasoning", "content": [{"text": "hidden"}]},
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": " first "},
                        SimpleNamespace(type="text", text="second"),
                    ],
                },
            ]
        }
        self.assertEqual(
            auxiliary_response_projection._extract_aux_response_text(nested),
            "first\nsecond",
        )

    def test_recovery_normalizes_mutable_and_mapping_responses(self) -> None:
        mutable = SimpleNamespace(
            output_text="Recovered object",
            id="response-one",
            model="model-one",
        )
        recovered_mutable = auxiliary_response_projection._recover_aux_response_message(
            mutable
        )
        self.assertIs(recovered_mutable, mutable)
        self.assertEqual(mutable.choices[0].message.content, "Recovered object")

        recovered_mapping = auxiliary_response_projection._recover_aux_response_message(
            {"output_text": "Recovered mapping"}
        )
        self.assertIsNotNone(recovered_mapping)
        assert recovered_mapping is not None
        self.assertEqual(
            recovered_mapping.choices[0].message.content,
            "Recovered mapping",
        )

    def test_content_projection_prefers_visible_text_then_reasoning(self) -> None:
        visible = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="<think>private</think> Visible answer",
                    )
                )
            ]
        )
        self.assertEqual(
            auxiliary_response_projection.extract_content_or_reasoning(visible),
            "Visible answer",
        )

        reasoning = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="<reasoning>hidden</reasoning>",
                        reasoning=" first ",
                        reasoning_content="first",
                        reasoning_details=[{"summary": "second"}],
                    )
                )
            ]
        )
        self.assertEqual(
            auxiliary_response_projection.extract_content_or_reasoning(reasoning),
            "first\n\nsecond",
        )

    def test_legacy_private_patch_paths_feed_the_extracted_implementation(self) -> None:
        def object_get(_obj: object, key: str) -> object:
            return " patched text " if key == "output_text" else None

        with patch.object(auxiliary_client, "_obj_get", side_effect=object_get) as get:
            self.assertEqual(
                auxiliary_client._extract_aux_response_text(object()),
                "patched text",
            )
        get.assert_called_once_with(ANY, "output_text")

        response = SimpleNamespace(model="patched-model")
        with patch.object(
            auxiliary_client,
            "_extract_aux_response_text",
            return_value="patched recovery",
        ) as extract:
            recovered = auxiliary_client._recover_aux_response_message(response)
        extract.assert_called_once_with(response)
        self.assertIs(recovered, response)
        self.assertEqual(response.choices[0].message.content, "patched recovery")

    def test_host_validation_retains_accounting_and_relay_side_effects(self) -> None:
        response = SimpleNamespace(
            output_text="Recovered through validation",
            model="response-model",
        )
        with (
            patch("pcbdraft.model.aux_accounting.record_aux_usage") as record_usage,
            patch.object(
                auxiliary_client,
                "_record_relay_auxiliary_response_model",
            ) as record_model,
            patch.object(
                auxiliary_client,
                "_complete_relay_auxiliary_call",
            ) as complete,
        ):
            validated = auxiliary_client._validate_llm_response(
                response,
                task="compression",
                provider="custom",
                base_url="https://example.test/v1",
            )

        self.assertIs(validated, response)
        self.assertEqual(
            validated.choices[0].message.content,
            "Recovered through validation",
        )
        record_usage.assert_called_once_with(
            response,
            "compression",
            provider="custom",
            base_url="https://example.test/v1",
        )
        record_model.assert_called_once_with(response)
        complete.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
