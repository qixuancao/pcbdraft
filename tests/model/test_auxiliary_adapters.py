from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.model import auxiliary_adapters, auxiliary_client


class _Responses:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="done")],
                )
            ],
            usage=SimpleNamespace(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
            ),
        )


class _OpenAIClient:
    api_key = "key"
    base_url = "https://api.githubcopilot.com"

    def __init__(self) -> None:
        self.responses = _Responses()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class AuxiliaryAdaptersTest(unittest.TestCase):
    def test_auxiliary_client_reexports_identical_adapter_classes(self) -> None:
        names = (
            "_CodexCompletionsAdapter",
            "CodexAuxiliaryClient",
            "AsyncCodexAuxiliaryClient",
            "_AnthropicCompletionsAdapter",
            "AnthropicAuxiliaryClient",
            "AsyncAnthropicAuxiliaryClient",
            "_BedrockCompletionsAdapter",
            "BedrockAuxiliaryClient",
            "AsyncBedrockAuxiliaryClient",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(auxiliary_client, name),
                    getattr(auxiliary_adapters, name),
                )

    def test_codex_adapter_preserves_chat_completion_shape(self) -> None:
        real_client = _OpenAIClient()
        client = auxiliary_client.CodexAuxiliaryClient(real_client, "gpt-test")

        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "system rules"},
                {"role": "user", "content": "hello"},
            ],
            timeout=12,
        )

        self.assertEqual(response.choices[0].message.content, "done")
        self.assertEqual(response.usage.total_tokens, 5)
        self.assertEqual(real_client.responses.kwargs["instructions"], "system rules")
        self.assertEqual(real_client.responses.kwargs["timeout"], 12)
        self.assertTrue(real_client.responses.kwargs["stream"])

    def test_anthropic_adapter_normalizes_response(self) -> None:
        normalized = SimpleNamespace(
            content="answer",
            tool_calls=None,
            reasoning="thought",
            finish_reason="stop",
        )
        transport = SimpleNamespace(normalize_response=Mock(return_value=normalized))
        sdk_response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=4, output_tokens=6, total_tokens=10)
        )
        adapter = auxiliary_client._AnthropicCompletionsAdapter(object(), "claude-test")

        with (
            patch(
                "pcbdraft.model.anthropic_adapter.build_anthropic_kwargs",
                return_value={"model": "claude-test"},
            ) as build,
            patch(
                "pcbdraft.model.anthropic_adapter.create_anthropic_message",
                return_value=sdk_response,
            ) as create,
            patch("pcbdraft.model.transports.get_transport", return_value=transport),
        ):
            response = adapter.create(messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(response.choices[0].message.content, "answer")
        self.assertEqual(response.choices[0].message.reasoning, "thought")
        self.assertEqual(response.usage.total_tokens, 10)
        build.assert_called_once()
        create.assert_called_once()

    def test_bedrock_adapter_maps_openai_options_to_converse(self) -> None:
        adapter = auxiliary_client._BedrockCompletionsAdapter(
            "us-east-1", "anthropic.claude-test"
        )
        response = object()

        with patch(
            "pcbdraft.model.bedrock_adapter.call_converse", return_value=response
        ) as converse:
            actual = adapter.create(
                messages=[{"role": "user", "content": "hi"}],
                max_completion_tokens=321,
                temperature=0.2,
                stop="END",
            )

        self.assertIs(actual, response)
        converse.assert_called_once_with(
            region="us-east-1",
            model="anthropic.claude-test",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=321,
            temperature=0.2,
            top_p=None,
            stop_sequences=["END"],
        )

    def test_async_adapter_runs_sync_adapter_without_changing_result(self) -> None:
        sync_adapter = SimpleNamespace(create=Mock(return_value="async-result"))
        adapter = auxiliary_client._AsyncCodexCompletionsAdapter(sync_adapter)

        result = asyncio.run(adapter.create(model="gpt-test"))

        self.assertEqual(result, "async-result")
        sync_adapter.create.assert_called_once_with(model="gpt-test")

    def test_runtime_hooks_follow_established_auxiliary_client_patch_path(self) -> None:
        marker = object()
        with patch.object(auxiliary_client, "_runtime_main_value", return_value=marker):
            self.assertIs(auxiliary_adapters._runtime_main_value("cache_scope"), marker)
        with patch.object(
            auxiliary_client, "_evict_cached_client_instance", return_value=True
        ) as evict:
            self.assertTrue(auxiliary_adapters._evict_cached_client_instance(marker))
        evict.assert_called_once_with(marker)


if __name__ == "__main__":
    unittest.main()
