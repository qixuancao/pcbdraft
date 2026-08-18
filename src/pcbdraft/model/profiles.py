"""Declarative wire profiles for supported OpenAI-compatible providers."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderWireProfile:
    """Provider-specific request fields kept separate from credentials."""

    output_mode: str = "json_schema"
    max_tokens_field: str = "max_tokens"
    max_output_tokens: int = 6000
    temperature: float | None = 0.0
    require_supported_parameters: bool = False
    separate_reasoning: bool = False
    agent_protocol: str = "deterministic"
    extra_request_fields: Mapping[str, Any] = field(default_factory=dict)


_DEFAULT = ProviderWireProfile()
_PROFILES: dict[str, ProviderWireProfile] = {
    # DeepSeek's public Chat Completions API currently documents JSON Object,
    # not JSON Schema, so the full schema remains a local trust boundary.
    "deepseek": ProviderWireProfile(output_mode="json_object"),
    # MiniMax M2-family endpoints reject response_format=json_schema. The
    # schema is supplied in the prompt and enforced locally after decoding.
    "minimax": ProviderWireProfile(
        output_mode="prompt",
        max_tokens_field="max_completion_tokens",
        max_output_tokens=2048,
        temperature=None,
        separate_reasoning=True,
    ),
    # Kimi and current OpenAI chat endpoints use max_completion_tokens and can
    # reject legacy sampling parameters on reasoning-capable models.
    "kimi": ProviderWireProfile(
        max_tokens_field="max_completion_tokens", temperature=None
    ),
    "openai": ProviderWireProfile(
        max_tokens_field="max_completion_tokens",
        temperature=None,
        agent_protocol="openai-responses",
    ),
    # OpenRouter can route only through backends that support every requested
    # parameter, avoiding a silent downgrade of structured output.
    "openrouter": ProviderWireProfile(
        temperature=None, require_supported_parameters=True
    ),
    "ollama": ProviderWireProfile(),
    # SiliconFlow hosts thinking-capable Qwen/DeepSeek families that by default
    # burn the whole output budget on reasoning, so disable thinking to keep
    # structured JSON replies fast and deterministic.
    "siliconflow": ProviderWireProfile(extra_request_fields={"enable_thinking": False}),
}

_HOST_PROFILE_IDS = {
    "api.deepseek.com": "deepseek",
    "api.minimax.io": "minimax",
    "api.minimaxi.com": "minimax",
    "api.moonshot.ai": "kimi",
    "api.moonshot.cn": "kimi",
    "api.openai.com": "openai",
    "openrouter.ai": "openrouter",
    "api.siliconflow.cn": "siliconflow",
    "api.siliconflow.com": "siliconflow",
}


def provider_wire_profile(provider_id: str, base_url: str) -> ProviderWireProfile:
    """Resolve one canonical request profile by ID, then exact endpoint host."""

    normalized = provider_id.strip().casefold()
    profile = _PROFILES.get(normalized)
    if profile is not None:
        return profile
    hostname = (urllib.parse.urlsplit(base_url).hostname or "").casefold()
    detected = _HOST_PROFILE_IDS.get(hostname)
    return _PROFILES.get(detected, _DEFAULT) if detected is not None else _DEFAULT
